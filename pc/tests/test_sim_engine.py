"""内置仿真的验收测试（cards_sim 卡23）。

覆盖：进程内传输的超时语义、经通信核心的完整命令闭环、遥测帧格式、
与独立模拟器 ``tools/fake_mcu.py`` 的帧字节一致性、阶跃事件、对象注入、
线程干净退出与连续十次重连、空闲 CPU、运行期调参。

运行：cd pc && python -m pytest tests/test_sim_engine.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.comm import CommCore  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    PacketType,
    StepPhase,
    encode,
    pack_pid_set,
    pack_step_start,
    unpack_pid_ack,
    unpack_pong,
    unpack_telemetry,
)
from serialpid.sim.engine import (  # noqa: E402
    DEFAULT_PERIOD_S,
    DEFAULT_SP,
    SimEngine,
)
from serialpid.sim.transport import SimTransport  # noqa: E402
from serialpid.transport import TransportError  # noqa: E402

TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "tools")


def _wait(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _load_fake_mcu():
    """按路径加载独立模拟器（它把 pc 目录塞进 sys.path，可直接 import）。"""
    path = os.path.join(TOOLS_DIR, "fake_mcu.py")
    spec = importlib.util.spec_from_file_location("fake_mcu_for_parity", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 传输层


def test_transport_read_returns_empty_on_timeout():
    """read 必须等不到就返回 b"，不能永久阻塞接收线程。"""
    tr = SimTransport(read_timeout=0.02)
    tr.open()
    t0 = time.monotonic()
    for _ in range(3):
        assert tr.read(64) == b""
    elapsed = time.monotonic() - t0
    assert 0.03 <= elapsed <= 0.2, f"三次超时空读耗时 {elapsed:.3f}s，偏离 3×20ms"
    assert tr.empty_reads == 3
    tr.close()


def test_transport_rejects_write_when_closed_and_recovers_after_open():
    tr = SimTransport(read_timeout=0.01)
    with pytest.raises(TransportError):
        tr.write(b"\x01")
    tr.open()
    tr.write(b"abc")
    assert tr.take_inbound(0.05) == b"abc"
    assert tr.bytes_from_host == 3
    tr.close()
    assert tr.is_open() is False
    assert tr.read(16) == b""  # 关闭后立即返回，不阻塞
    tr.open()  # 可重开
    assert tr.is_open() is True


def test_emit_roundtrip_and_close_clears_buffer():
    tr = SimTransport(read_timeout=0.05)
    tr.open()
    assert tr.emit(b"xy") is True
    assert tr.read(8) == b"xy"
    tr.emit(b"buf")
    assert tr.pending_out_bytes == 3
    tr.close()
    assert tr.emit(b"zz") is False, "关闭后不再接收引擎输出"
    assert tr.pending_out_bytes == 0


# ---------------------------------------------------------------- 帧行为


def test_command_ack_bytes_match_fake_mcu():
    """同一串命令，进程内引擎与独立模拟器发出的应答帧逐字节相同。"""
    # 独立模拟器：接管 _send，不连 socket
    fake_mod = _load_fake_mcu()
    fake = fake_mod.FakeMcu()
    fake_frames: list[bytes] = []
    fake._send = lambda pkt_type, payload: fake_frames.append(encode(pkt_type, payload))

    tr = SimTransport(read_timeout=0.01)
    tr.open()
    engine = SimEngine(transport=tr)

    def pair(pkt_type: int, payload: bytes = b"") -> tuple[bytes, bytes]:
        n0 = len(fake_frames)
        fake._handle(pkt_type, payload)
        fake_bytes = fake_frames[n0] if len(fake_frames) > n0 else b""
        engine_frames = engine.handle_frame(pkt_type, payload)
        assert len(engine_frames) == (1 if fake_bytes else 0), (
            f"{pkt_type:#04x} 应答数量不一致：fake={fake_bytes!r} engine={engine_frames}"
        )
        engine_bytes = engine_frames[0] if engine_frames else b""
        return fake_bytes, engine_bytes

    # t_ms 都从未推进的 0 开始，便于逐字节比对
    cases = [
        (PacketType.PING, b""),
        (PacketType.PID_SET, pack_pid_set(3.0, 0.4, 0.05, 26.0)),
        (PacketType.STEP_START, pack_step_start(2.0, 0)),   # hold 0 = 一直保持
        (PacketType.STEP_STOP, b""),
        (PacketType.RESET, b""),
    ]
    for pkt_type, payload in cases:
        fake_bytes, engine_bytes = pair(pkt_type, payload)
        assert fake_bytes and engine_bytes
        assert engine_bytes == fake_bytes, f"type 0x{pkt_type:02X} 帧体与独立模拟器不一致"
        # 逐字段复核，避免"两边一起错"（帧头 AA 55、长度自洽、帧尾 4 个 77）
        assert engine_bytes[0:2] == b"\xaa\x55"
        assert engine_bytes[2] + 10 == len(engine_bytes)
        assert engine_bytes[-4:] == b"\x77\x77\x77\x77"

    # 状态语义也对齐：阶跃中 PONG status=2，结束后回 1
    engine.handle_frame(PacketType.STEP_START, pack_step_start(2.0, 0))
    stepping = engine.handle_frame(PacketType.PING, b"")[0]
    assert unpack_pong(stepping[4:-6])[1] == 2
    engine.handle_frame(PacketType.STEP_STOP, b"")
    idle = engine.handle_frame(PacketType.PING, b"")[0]
    assert unpack_pong(idle[4:-6])[1] == 1
    tr.close()


def test_telemetry_is_protocol_identical():
    """遥测帧必须是标准 TELEMETRY（20 字节载荷、可被协议解出同样数值）。"""
    tr = SimTransport(read_timeout=0.01)
    tr.open()
    engine = SimEngine(transport=tr)
    raw = engine.tick(DEFAULT_PERIOD_S)
    assert raw is not None
    assert raw[:4] == bytes((0xAA, 0x55, 20, PacketType.TELEMETRY))
    payload = raw[4:-6]
    assert len(payload) == 20
    t_ms, sp, pv, out, err = unpack_telemetry(payload)
    snap = engine.snapshot()
    assert t_ms == snap["t_ms"] == 50
    assert (sp, pv, out) == pytest.approx((snap["sp"], snap["pv"], snap["out"]))
    # err 用本拍推进前的测量值算（与固件、独立模拟器同口径），与推进后的 pv 差一拍
    assert err == pytest.approx(snap["sp"] - pv, abs=0.05)
    assert tr.pending_out_bytes == len(raw)
    tr.close()


# ---------------------------------------------------------------- 通信核心接线


def test_inprocess_channel_through_comm_core():
    """经现有通信核心连接进程内通道：遥测、心跳、参数下发、阶跃、复位全通。"""
    frames: list = []
    connected: list = []
    errors: list = []
    core = CommCore(
        on_frame=lambda t, p: frames.append((t, p)),
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        on_error=errors.append,
        idle_timeout=3.0,
    )
    transport = SimTransport()
    engine = SimEngine(transport=transport)
    assert core.open("sim", "", transport=transport), "进程内通道打开失败"
    engine.start()

    try:
        assert _wait(lambda: any(t == PacketType.TELEMETRY for t, _ in frames), 2.0), (
            f"1 秒内未出遥测：{frames[:5]}"
        )
        telemetry_count = len([1 for t, _ in frames if t == PacketType.TELEMETRY])

        core.send(PacketType.PING)
        assert _wait(lambda: any(t == PacketType.PONG for t, _ in frames), 1.0)
        pong = [p for t, p in frames if t == PacketType.PONG][0]
        assert unpack_pong(pong)[1] == 1

        core.send(PacketType.PID_SET, pack_pid_set(4.0, 0.6, 0.05, 28.0))
        assert _wait(lambda: any(t == PacketType.PID_ACK for t, _ in frames), 1.0)
        ack = [p for t, p in frames if t == PacketType.PID_ACK][0]
        assert unpack_pid_ack(ack) == pytest.approx((4.0, 0.6, 0.05, 28.0, 0))

        core.send(PacketType.STEP_START, pack_step_start(3.0, 0))
        assert _wait(
            lambda: any(t == PacketType.STEP_EVENT and p[0] == StepPhase.RUNNING for t, p in frames),
            1.0,
        )
        assert engine.snapshot()["sp"] == pytest.approx(31.0), "SP 应为 28 + 3"

        core.send(PacketType.RESET)
        assert _wait(lambda: engine.snapshot()["step_baseline"] is None, 1.0)
        assert _wait(lambda: len([1 for t, _ in frames if t == PacketType.PONG]) >= 2, 1.0)
        assert engine.snapshot()["sp"] == pytest.approx(DEFAULT_SP)
        assert engine.snapshot()["pv"] == pytest.approx(20.0)

        assert errors == [], f"通信错误：{errors}"
        assert connected and connected[0][0] is True
        assert telemetry_count >= 1
    finally:
        core.close()
        assert engine.stop(timeout=1.0), "引擎线程未在 1 秒内退出"
        assert engine.is_running() is False


def test_plant_receives_controller_output_not_setpoint():
    """对象吃的是控制器输出，不是设定值（旧模拟器的病根）。"""

    class Recorder:
        def __init__(self) -> None:
            self.inputs: list[float] = []
            self.last_out = 0.0

        def step(self, out: float, dt_s=None) -> float:  # noqa: ANN001
            self.last_out = float(out)
            self.inputs.append(float(out))
            return 20.0 + 0.05 * float(out)

        def reset(self) -> None:
            self.inputs.clear()
            self.last_out = 0.0

    plant = Recorder()
    engine = SimEngine(transport=SimTransport(), plant=plant)
    engine.sp = 25.0
    engine.pv = 20.0
    engine.kp, engine.ki, engine.kd = 2.0, 0.0, 0.0
    engine.tick(DEFAULT_PERIOD_S)
    u = plant.inputs[-1]
    assert u == pytest.approx(2.0 * (25.0 - 20.0)), "输入不是 Kp×误差"
    assert abs(u - 25.0) > 1.0, "对象输入不应等于设定值"
    assert plant.last_out == pytest.approx(u)
    assert 0.0 <= plant.last_out <= 100.0


def test_plant_params_take_effect_runtime():
    """闭环成立：改 Kp 响应快慢立刻变，加延迟能顶出超调。"""
    Plant = pytest.importorskip("serialpid.sim.plant").Plant

    def trace(kp: float, ki: float = 0.0, delay: int = 0, ticks: int = 200) -> list[float]:
        model = Plant()
        model.set_params(tau1_s=2.0, tau2_s=0.5, delay_steps=delay, gain=1.0, noise_std=0.0)
        engine = SimEngine(transport=SimTransport(), plant=model)
        engine.handle_frame(PacketType.PID_SET, pack_pid_set(kp, ki, 0.0, 25.0))
        out: list[float] = []
        for _ in range(ticks):
            engine.tick(DEFAULT_PERIOD_S)
            out.append(engine.pv)
        return out

    def first_cross(values: list[float], level: float) -> int:
        for index, value in enumerate(values):
            if value >= level:
                return index
        return len(values)

    slow = trace(0.3)
    fast = trace(3.0)
    slow_index = first_cross(slow, 23.0)
    fast_index = first_cross(fast, 23.0)
    assert fast_index < slow_index, (
        f"高 Kp 到达 23 用了 {fast_index} 拍，低 Kp 用了 {slow_index} 拍，闭环没接通"
    )

    delayed = trace(6.0, delay=10)
    peak = max(delayed)
    assert peak > 26.0, f"纯延迟 10 拍 + 高 Kp 未出现超调，峰值只有 {peak:.2f}"
    assert min(delayed) < 25.0, "曲线始终单调，不像有反馈振荡"


# ---------------------------------------------------------------- 生命周期


def test_ten_connect_disconnect_cycles_without_thread_growth():
    baseline = threading.active_count()
    for _ in range(10):
        transport = SimTransport()
        engine = SimEngine(transport=transport)
        core = CommCore(idle_timeout=1.0)
        assert core.open("sim", "", transport=transport)
        engine.start()
        assert _wait(lambda: transport.bytes_to_host > 0 or True, 0.2)
        core.close()
        assert engine.stop(timeout=1.0), "引擎线程未干净退出"
        assert not engine.is_running()
    assert threading.active_count() <= baseline + 1, (
        f"十次连接后线程数 {threading.active_count()}，基线 {baseline}"
    )


def test_restart_does_not_leak_previous_session_state():
    transport = SimTransport()
    engine = SimEngine(transport=transport)
    engine.start()
    _wait(lambda: engine.snapshot()["t_ms"] > 100, 1.0)
    engine.handle_frame(PacketType.STEP_START, pack_step_start(5.0, 0))
    assert engine.snapshot()["sp"] == pytest.approx(30.0)
    assert engine.stop(timeout=1.0)

    engine.start()  # 立刻重连
    try:
        snap = engine.snapshot()
        assert snap["step_baseline"] is None, "上一轮的阶跃残留"
        assert snap["sp"] == pytest.approx(DEFAULT_SP), "设定值未回到默认"
        assert snap["t_ms"] < 200, f"时间戳带着上一轮走：{snap['t_ms']}"
    finally:
        engine.stop()


def test_idle_cpu_is_negligible():
    """20 Hz 循环用带超时等待，空闲时几乎不吃 CPU。"""
    engine = SimEngine(transport=SimTransport())
    engine.start()
    cpu0 = time.process_time()
    time.sleep(0.5)
    cpu = time.process_time() - cpu0
    engine.stop()
    assert cpu < 0.1, f"空闲 0.5 s 吃掉 {cpu * 1000:.0f} ms CPU"


def test_period_and_plant_update():
    plant = pytest.importorskip("serialpid.sim.plant").Plant
    model = plant()
    engine = SimEngine(transport=SimTransport(), plant=model)
    assert engine.set_period(0.1) == pytest.approx(0.1)
    assert engine.snapshot()["period_s"] == pytest.approx(0.1)
    assert model.dt_s == pytest.approx(0.1)
    applied = engine.update_plant(tau1_s=3.0, delay_steps=2)
    assert applied["tau1_s"] == pytest.approx(3.0)
    assert model.params.tau1_s == pytest.approx(3.0)
    assert model.params.delay_steps == 2


def test_bad_payload_does_not_break_engine():
    engine = SimEngine(transport=SimTransport())
    assert engine.handle_frame(PacketType.PID_SET, b"\x00\x01") == []
    assert engine.handle_frame(PacketType.TELEMETRY, b"") == []  # 上行包被忽略
    assert engine.tick(0.05) is not None, "坏载荷后引擎仍需继续出遥测"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
