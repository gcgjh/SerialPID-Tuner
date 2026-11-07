"""卡27-A：``tools/fake_mcu.py`` 是薄壳——与核心引擎逐字节同一套应答。

验收口径：
* 同一条命令序列，两个入口（TCP 薄壳 / ``SimEngine``）产出的应答帧**逐字节相同**；
* 薄壳里不再有第二份控制逻辑（无 PID 算式、无对象模型、无阶跃状态机）；
* CLI 参数与启动文案不变（集成测试按子进程拉起它）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import pytest  # noqa: E402

import fake_mcu  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    PacketType,
    encode,
    pack_pid_set,
    pack_sp_set,
    pack_step_start,
)
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.frames import is_frame, split_frames  # noqa: E402
from serialpid.sim.transport import SimTransport  # noqa: E402

COMMANDS = [
    (PacketType.PING, b""),
    (PacketType.PID_SET, pack_pid_set(2.5, 0.4, 0.08, 30.0)),
    (PacketType.SP_SET, pack_sp_set(28.0)),
    (PacketType.STEP_START, pack_step_start(4.0, 2000)),
    (PacketType.STEP_STOP, b""),
    (PacketType.RESET, b""),
    (PacketType.PING, b""),
]


def _shell_replies() -> bytes:
    fake = fake_mcu.FakeMcu()
    chunks = []
    fake._send = lambda pkt_type, payload: chunks.append(encode(pkt_type, payload))
    for pkt_type, payload in COMMANDS:
        fake._handle(pkt_type, payload)
    return b"".join(chunks)


def _engine_replies() -> bytes:
    engine = SimEngine(transport=SimTransport(), manual=True)
    chunks = []
    for pkt_type, payload in COMMANDS:
        chunks.extend(engine.handle_frame(pkt_type, payload))
    return b"".join(chunks)


def test_two_entries_byte_identical():
    shell = _shell_replies()
    engine = _engine_replies()
    assert len(shell) > 0
    assert shell == engine, (
        "TCP 薄壳与核心引擎的应答不再逐字节一致——两套实现又分叉了"
    )


def test_replies_are_valid_frames():
    frames = split_frames(_shell_replies())
    assert frames and len(frames) <= len(COMMANDS)
    assert all(is_frame(frame) for frame in frames)
    types = [frame[3] for frame in frames]
    assert PacketType.PONG in types
    assert PacketType.PID_ACK in types


def test_shell_has_no_second_implementation():
    import inspect

    source = inspect.getsource(fake_mcu)
    forbidden = [
        "kp * err",           # PID 算式
        "self._integral",     # 积分状态
        "class Plant",        # 对象模型
        "class LagPlant",     # 旧的一阶惯性实现
    ]
    for marker in forbidden:
        assert marker not in source, f"薄壳里出现了控制实现痕迹：{marker!r}"
    assert "SimEngine" in source
    assert "handle_frame" in source


def _wait_port(port: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"端口 {port} 未就绪")


def test_tcp_entry_returns_same_pong_bytes():
    port = 19021
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, "tools", "fake_mcu.py"), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        _wait_port(port)
        assert "fake_mcu 监听 127.0.0.1" in proc.stdout.readline()
        pong = None
        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as client:
            client.sendall(encode(PacketType.PING))
            deadline = time.monotonic() + 2.0
            raw = b""
            while time.monotonic() < deadline and pong is None:
                raw += client.recv(4096)
                pong = next(
                    (frame for frame in split_frames(raw) if frame[3] == PacketType.PONG),
                    None,
                )
        engine_pong = SimEngine(transport=SimTransport(), manual=True).handle_frame(
            PacketType.PING, b""
        )[0]
        assert pong == engine_pong, "TCP 入口返回的 PONG 与引擎字节不同"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_cli_contract_unchanged():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    result = subprocess.run(
        [sys.executable, os.path.join(root, "tools", "fake_mcu.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0
    assert "--host" in result.stdout and "--port" in result.stdout
    assert "离线模拟器" in result.stdout
    # 默认值靠类属性保证（help 里 argparse 不会打印默认值）
    fake = fake_mcu.FakeMcu()
    assert (fake.host, fake.port) == ("127.0.0.1", 9000)
    assert fake_mcu.CTRL_PERIOD_S == 0.05
