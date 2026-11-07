"""卡27-B：链路劣化层（丢帧 / 坏 CRC / 延迟抖动 / 强制断线 / 突发噪声）。

验收口径：
* 五类故障都能稳定复现，同种子连续跑十次计数与产出字节完全一致；
* 默认关闭时是零开销直通，字节与不包一层时完全相同；
* 打开后丢的是整帧、坏的是 CRC 域，不是把字节流切碎；
* 强制断线后 ``is_open`` 为假、写抛 ``TransportError``、引擎不出帧。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.protocol import (  # noqa: E402
    CrcError,
    PacketType,
    encode,
    parse_frame,
    pack_pid_set,
)
from serialpid.sim.degradation import DegradedTransport, LinkConfig  # noqa: E402
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.frames import (  # noqa: E402
    corrupt_crc,
    is_frame,
    make_noise,
    split_frames,
)
from serialpid.sim.transport import SimTransport  # noqa: E402
from serialpid.transport import TransportError  # noqa: E402


class FakeClock:
    """可手动推进的时钟，让延迟/断线测试不靠 sleep。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _session(config: LinkConfig, ticks: int = 30, clock=None):
    """起一个手动引擎，返回 (引擎, 劣化通道, 内层通道)。"""
    inner = SimTransport(read_timeout=0.01)
    transport = DegradedTransport(inner, config, clock=clock or __import__("time").monotonic)
    engine = SimEngine(transport=transport, manual=True)
    engine.start()
    return engine, transport, inner


def _drain(transport: DegradedTransport) -> bytes:
    chunks = []
    while True:
        data = transport.read(4096)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


# ---------------------------------------------------------------- 帧工具


def test_split_frames_roundtrip():
    raw = encode(PacketType.PING) + encode(PacketType.TELEMETRY, b"\x00" * 20)
    frames = split_frames(raw)
    assert b"".join(frames) == raw
    assert all(is_frame(frame) for frame in frames)


def test_corrupt_crc_only_touches_crc_field():
    raw = encode(PacketType.PID_ACK, b"\x00" * 17)
    broken = corrupt_crc(raw)
    assert len(broken) == len(raw)
    assert broken != raw
    diff = [i for i, (a, b) in enumerate(zip(raw, broken)) if a != b]
    assert diff == [len(raw) - 6], f"只应改 CRC 低字节，实际差异位置 {diff}"
    with pytest.raises(CrcError):
        parse_frame(broken)


def test_noise_never_forms_header():
    noise = make_noise(__import__("random").Random(1), 200)
    assert len(noise) == 200
    assert b"\xaa\x55" not in noise


# ---------------------------------------------------------------- 直通


def test_disabled_is_zero_overhead_passthrough():
    cfg = LinkConfig(enabled=False, drop_down=1.0, bad_crc_down=1.0)
    engine, transport, _inner = _session(cfg)
    engine.step(5)
    data = _drain(transport)
    assert transport.counters.passthrough_chunks > 0
    assert transport.counters.dropped_down == 0
    # 5 帧遥测，逐帧都是合法帧
    frames = split_frames(data)
    assert len(frames) == 5 and all(is_frame(frame) for frame in frames)
    engine.stop()


# ---------------------------------------------------------------- 丢帧


def test_drop_frames_deterministic_across_ten_runs():
    cfg = LinkConfig(enabled=True, drop_down=0.4, seed=4242)
    results = []
    for _ in range(10):
        engine, transport, _inner = _session(cfg, ticks=20)
        engine.step(20)
        data = _drain(transport)
        results.append((transport.counters.dropped_down, data))
        engine.stop()
    assert len({r[0] for r in results}) == 1, f"十次丢帧数不一致：{[r[0] for r in results]}"
    assert len({r[1] for r in results}) == 1, "十次产出字节不一致"
    assert 0 < results[0][0] < 20, "丢帧概率应生效但不是全丢"
    # 丢的是整帧：剩下的一定仍是合法帧
    assert all(is_frame(frame) for frame in split_frames(results[0][1]))


def test_drop_up_prevents_command_ack():
    cfg = LinkConfig(enabled=True, drop_up=1.0)
    inner = SimTransport()
    transport = DegradedTransport(inner, cfg)
    transport.open()
    transport.write(encode(PacketType.PING))
    assert transport.counters.dropped_up == 1
    assert inner.take_inbound(0.0) == b"", "被丢的上行帧不应进入引擎侧"
    transport.close()


def test_bad_crc_down_corrupts_every_frame():
    cfg = LinkConfig(enabled=True, bad_crc_down=1.0)
    engine, transport, _inner = _session(cfg, ticks=3)
    engine.step(3)
    data = _drain(transport)
    frames = split_frames(data)
    assert len(frames) == 3
    assert transport.counters.bad_crc_down == 3
    for frame in frames:
        with pytest.raises(CrcError):
            parse_frame(frame)
    engine.stop()


# ---------------------------------------------------------------- 延迟 / 抖动


def test_delay_holds_frames_until_due():
    clock = FakeClock()
    cfg = LinkConfig(enabled=True, delay_ms=100)
    engine, transport, inner = _session(cfg, clock=clock)
    engine.step(3)
    assert transport.counters.delayed == 3
    assert inner.pending_out_bytes == 0
    transport.pump(clock.now + 0.05)
    assert inner.pending_out_bytes == 0, "还没到点就被投递了"
    transport.pump(clock.now + 0.11)
    assert transport.counters.delayed == 3
    assert len(split_frames(_drain_without_pump(inner))) == 3
    engine.stop()


def _drain_without_pump(inner: SimTransport) -> bytes:
    chunks = []
    while inner.pending_out_bytes:
        data = inner.read(4096)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def test_jitter_spreads_due_times_but_keeps_all_frames():
    clock = FakeClock()
    cfg = LinkConfig(enabled=True, jitter_ms=50, seed=7)
    engine, transport, inner = _session(cfg, clock=clock)
    engine.step(10)
    transport.pump(clock.now + 0.06)
    assert len(split_frames(_drain_without_pump(inner))) == 10
    engine.stop()


# ---------------------------------------------------------------- 断线 / 噪声


def test_forced_disconnect_after_timeout():
    clock = FakeClock()
    cfg = LinkConfig(enabled=True, disconnect_after_s=0.2)
    engine, transport, _inner = _session(cfg, clock=clock)
    engine.step(2)
    assert transport.is_open() is True
    clock.advance(0.25)
    assert transport.emit(b"") is False or transport.is_open() is False
    assert transport.is_open() is False
    assert transport.counters.forced_close == 1
    with pytest.raises(TransportError):
        transport.write(encode(PacketType.PING))
    # 再 emit 直接被拒
    raw = engine.step(1)
    assert raw is not None, "引擎本身还在跑"
    assert transport.read(64) == b""
    engine.stop()


def test_noise_burst_periodic_and_counted():
    clock = FakeClock()
    cfg = LinkConfig(enabled=True, noise_period_s=0.1, noise_burst_bytes=6)
    engine, transport, inner = _session(cfg, clock=clock)
    transport.pump(clock.now + 0.11)
    assert transport.counters.noise_bytes == 6
    transport.pump(clock.now + 0.12)
    assert transport.counters.noise_bytes == 6, "同一周期内不应重复插噪声"
    transport.pump(clock.now + 0.22)
    assert transport.counters.noise_bytes == 12
    engine.stop()


# ---------------------------------------------------------------- 运行期调参


def test_apply_toggles_at_runtime():
    cfg = LinkConfig(enabled=False)
    engine, transport, _inner = _session(cfg, ticks=4)
    transport.apply(LinkConfig(enabled=True, drop_down=1.0))
    engine.step(4)
    assert transport.counters.dropped_down == 4
    transport.apply(LinkConfig(enabled=False))
    engine.step(2)
    data = _drain(transport)
    assert len(split_frames(data)) == 2
    engine.stop()


def test_error_on_bad_crc_does_not_break_engine():
    """坏帧进引擎：解包器丢帧，引擎继续出遥测（不能让仿真线程崩）。"""
    cfg = LinkConfig(enabled=True, bad_crc_up=1.0)
    engine, transport, _inner = _session(cfg, ticks=2)
    transport.write(encode(PacketType.PING))
    engine.step(2)
    assert transport.counters.bad_crc_up == 1
    assert len(split_frames(_drain(transport))) == 2
    engine.stop()


# ---------------------------------------------------------------- 五类 × 十次


FAULTS = {
    "丢帧": LinkConfig(enabled=True, drop_down=0.4, seed=9),
    "坏CRC": LinkConfig(enabled=True, bad_crc_down=0.4, seed=9),
    "延迟抖动": LinkConfig(enabled=True, delay_ms=80, jitter_ms=30, seed=9),
    "强制断线": LinkConfig(enabled=True, disconnect_after_s=0.05),
    "突发噪声": LinkConfig(enabled=True, noise_period_s=0.05, noise_burst_bytes=4, seed=9),
}


def _run_fault(cfg: LinkConfig):
    """按同一条时间线跑一次，返回 (收到的字节, 计数快照)。"""
    clock = FakeClock()
    engine, transport, _inner = _session(cfg, ticks=10, clock=clock)
    for _ in range(10):
        engine.step(1)
        clock.advance(0.02)
        transport.pump(clock.now)
    data = _drain(transport)
    engine.stop()
    counters = tuple(sorted(vars(transport.counters).items()))
    return data, counters


@pytest.mark.parametrize("name", sorted(FAULTS))
def test_each_fault_is_deterministic_across_ten_runs(name):
    runs = [_run_fault(FAULTS[name]) for _ in range(10)]
    assert len({run[0] for run in runs}) == 1, f"{name}：十次产出字节不一致"
    assert len({run[1] for run in runs}) == 1, f"{name}：十次计数不一致：{runs[0][1]}"
