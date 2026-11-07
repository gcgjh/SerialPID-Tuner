# -*- coding: utf-8 -*-
"""card28 主动心跳回归：RTT 可观测、N 次无应答判死、有遥测不误杀。

覆盖验收：
    * 打开心跳后 PC 侧周期性发 PING，收到 PONG 能算出往返时延；
    * 连续 N 次没有 PONG 且期间没有任何数据 -> 断线原因 ``heartbeat-timeout``；
    * 对端忙到不回 PONG 但一直在发遥测 -> 不判死（只是质量提示）；
    * 六类断线原因都有中文化文案，自动重连退避表固定。

运行：cd pc && python -m pytest tests/test_heartbeat.py -q
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.comm import (  # noqa: E402
    DISCONNECT_TEXT,
    DROP_CLOSED,
    DROP_DEVICE_REMOVED,
    DROP_DRIVER_ERROR,
    DROP_HEARTBEAT,
    DROP_PEER_CLOSED,
    DROP_READ_ERROR,
    DROP_TIMEOUT,
    DROP_WRITE_ERROR,
    RECONNECT_MAX_ATTEMPTS,
    CommCore,
    brief_disconnect,
    describe_disconnect,
    reconnect_delay,
)
from serialpid.frame_trace import DIR_TX  # noqa: E402
from serialpid.link_stats import QUALITY_WARN  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    FrameDecoder,
    PacketType,
    encode,
    pack_pong,
    pack_telemetry,
)


class _Server:
    """最小 TCP 服务器：接受一个连接，按子类行为回应。"""

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._srv.settimeout(0.2)
        conn = None
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        if conn is None:
            return
        conn.settimeout(0.05)
        try:
            self.on_connected(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def on_connected(self, conn: socket.socket) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


class _PongServer(_Server):
    """收到 PING 就回 PONG。"""

    def on_connected(self, conn: socket.socket) -> None:
        decoder = FrameDecoder()
        while not self._stop.is_set():
            try:
                data = conn.recv(1024)
            except socket.timeout:
                continue
            if not data:
                return
            for frame in decoder.feed(data):
                if frame.type == PacketType.PING:
                    conn.sendall(encode(PacketType.PONG, pack_pong(0x10, 0)))


class _SilentServer(_Server):
    """接受连接后既不回 PONG 也不发数据。"""

    def on_connected(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn.recv(1024)
            except socket.timeout:
                continue


class _TelemetryOnlyServer(_Server):
    """只发遥测，从不回 PONG（模拟"固件忙到没实现心跳应答"）。"""

    def on_connected(self, conn: socket.socket) -> None:
        start = time.monotonic()
        t_ms = 0
        while not self._stop.is_set():
            conn.sendall(encode(PacketType.TELEMETRY, pack_telemetry(t_ms, 28.0, 27.9, 12.0, 0.1)))
            t_ms += 50
            # 按绝对时刻排节拍，避免 sleep 累积漂移
            target = start + t_ms / 1000.0
            while time.monotonic() < target and not self._stop.is_set():
                time.sleep(0.005)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 心跳可观测


def test_heartbeat_sends_ping_and_measures_rtt() -> None:
    server = _PongServer()
    connected: list[tuple[bool, str]] = []
    core = CommCore(
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        idle_timeout=0.0,
        heartbeat_interval=0.25,
        heartbeat_max_missed=3,
    )
    try:
        assert core.open("tcp", f"127.0.0.1:{server.port}")
        assert core.heartbeat_interval == pytest.approx(0.25)

        def pings() -> list:
            return [
                e for e in core.trace.snapshot()
                if e.direction == DIR_TX and e.pkt_type == PacketType.PING
            ]

        # 等到"至少两次 PING 发出"且"RTT 已经算出来"：既证明周期性，也证明能测
        assert _wait_until(
            lambda: len(pings()) >= 2 and (core.stats_snapshot().rtt_avg_ms or 0.0) > 0.0,
            timeout=2.5,
        ), f"心跳没有周期性发 PING / 没算出 RTT: {core.stats_snapshot()}"

        snapshot = core.stats_snapshot()
        assert snapshot.quality != "idle"
        assert snapshot.rtt_avg_ms is not None and snapshot.rtt_avg_ms > 0.0
        assert snapshot.heartbeat_misses == 0, "PONG 已经到了，欠账却没结清"

        # 心跳可观测：帧日志里能数出 PC 发出的 PING
        assert len(pings()) >= 2, f"心跳没有周期性发出 PING: {len(pings())}"
        assert snapshot.tx_frames >= len(pings())
    finally:
        core.close()
        server.close()


# ---------------------------------------------------------------- 判死


def test_drop_after_n_misses_without_any_data() -> None:
    server = _SilentServer()
    connected: list[tuple[bool, str]] = []
    core = CommCore(
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        idle_timeout=0.0,  # 关掉空闲兜底：这里要验证的就是心跳判死
        heartbeat_interval=0.2,
        heartbeat_max_missed=3,
    )
    try:
        assert core.open("tcp", f"127.0.0.1:{server.port}")
        assert _wait_until(
            lambda: connected and connected[-1][0] is False, timeout=2.5
        ), f"心跳没有判死: {connected}"
        assert connected[-1] == (False, DROP_HEARTBEAT), connected[-1]
        # 欠账记进快照：界面能显示"连续几次没应答"
        assert core.stats_snapshot().heartbeat_misses >= 3
        assert not core.connected
    finally:
        core.close()
        server.close()


def test_telemetry_without_pong_is_not_dropped() -> None:
    """风险项：对端在发遥测、只是没空回 PONG，不能判死。"""
    server = _TelemetryOnlyServer()
    connected: list[tuple[bool, str]] = []
    core = CommCore(
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        idle_timeout=0.0,
        heartbeat_interval=0.2,
        heartbeat_max_missed=3,
    )
    try:
        assert core.open("tcp", f"127.0.0.1:{server.port}")
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if connected and connected[-1][0] is False:
                break
            time.sleep(0.01)
        assert core.connected, f"有遥测却被判死: {connected}"
        snapshot = core.stats_snapshot()
        assert snapshot.rx_frames > 0, "遥测没进来"
        assert snapshot.heartbeat_misses >= 3, "欠账没累计"
        # 质量降级为提示（WARN），但不是断线
        assert snapshot.quality == QUALITY_WARN
    finally:
        core.close()
        server.close()


# ---------------------------------------------------------------- 文案与退避


@pytest.mark.parametrize(
    "reason",
    [
        DROP_TIMEOUT,
        DROP_HEARTBEAT,
        DROP_PEER_CLOSED,
        DROP_DEVICE_REMOVED,
        DROP_DRIVER_ERROR,
        DROP_WRITE_ERROR,
        DROP_READ_ERROR,
        DROP_CLOSED,
    ],
)
def test_every_reason_has_chinese_phenomenon_and_next_step(reason: str) -> None:
    phenomenon, next_step = describe_disconnect(reason)
    assert phenomenon and next_step
    # 不允许把内部串原样甩给用户（例如 timeout / read-error）
    assert "timeout" not in phenomenon.lower()
    assert "error" not in phenomenon.lower()
    assert brief_disconnect(reason)


def test_six_fault_reasons_have_distinct_brief_labels() -> None:
    faults = [
        DROP_TIMEOUT,
        DROP_HEARTBEAT,
        DROP_PEER_CLOSED,
        DROP_DEVICE_REMOVED,
        DROP_DRIVER_ERROR,
        DROP_READ_ERROR,
    ]
    labels = {brief_disconnect(reason) for reason in faults}
    assert len(labels) == len(faults), f"断线提示重名: {labels}"
    assert set(DISCONNECT_TEXT) >= set(faults)


def test_unknown_reason_is_reported_honestly() -> None:
    phenomenon, next_step = describe_disconnect("some-weird-reason")
    assert "未归类" in phenomenon
    assert "贴给开发者" in next_step


def test_reconnect_backoff_sequence_and_cap() -> None:
    assert [reconnect_delay(i) for i in range(1, 6)] == [1.0, 2.0, 5.0, 10.0, 60.0]
    assert reconnect_delay(6) == 60.0
    assert reconnect_delay(0) == 1.0
    assert RECONNECT_MAX_ATTEMPTS == 7


def test_reason_constants_match_expected_strings() -> None:
    """旧行为兼容：``timeout`` 这个串被既有用例断言，不能改。"""
    assert DROP_TIMEOUT == "timeout"
    assert DROP_DRIVER_ERROR == "driver-error"
