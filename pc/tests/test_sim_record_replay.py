"""卡27-C：帧流录制与回放（JSONL、倍速、单步、经通信核心进界面）。

验收口径：
* 录制只存帧与相对时间，不含绝对路径/设备号；
* 回放灌进 ``CommCore`` 的帧序列与录制时逐帧一致（界面看到的就是当时那套）；
* 回放支持倍速与单步，单步后继续播放不追赶；
* 坏行跳过并给警告，不因为一行坏掉整份录音。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.comm import CommCore  # noqa: E402
from serialpid.protocol import PacketType, encode, pack_pid_set  # noqa: E402
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.plant import Plant  # noqa: E402
from serialpid.sim.recorder import (  # noqa: E402
    FrameReplay,
    FrameRecorder,
    RecordingTransport,
    ReplayTransport,
    load_records,
)
from serialpid.sim.transport import SimTransport  # noqa: E402


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)


def _wait(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _record_session(path: str, ticks: int = 40) -> list:
    recorder = FrameRecorder(path)
    transport = RecordingTransport(SimTransport(), recorder)
    engine = SimEngine(transport=transport, manual=True, plant=Plant())
    engine.start()
    engine.handle_frame(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, 28.0))
    engine.step(ticks)
    engine.stop()
    recorder.close()
    return load_records(path)[0]


def test_recording_is_jsonl_and_has_no_paths(tmp_path):
    path = str(tmp_path / "frames.jsonl")
    records = _record_session(path)
    down = [r for r in records if r.direction == "down"]
    telemetry = [r for r in down if r.raw[3] == PacketType.TELEMETRY]
    assert len(telemetry) == 40, f"应有 40 帧遥测，另 1 帧是 PID_ACK（共 {len(down)} 下行）"
    assert len(down) == 41
    assert all(r.t_ms >= 0 for r in down)
    with open(path, "r", encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    assert set(first) == {"t_ms", "dir", "type", "len", "raw"}
    assert first["dir"] == "down"
    text = open(path, "r", encoding="utf-8").read()
    assert str(tmp_path) not in text, "录制文件不应包含绝对路径"
    # 时间戳单调不减
    assert all(b.t_ms >= a.t_ms for a, b in zip(down, down[1:]))


def test_replay_through_comm_core_matches_recording(tmp_path):
    path = str(tmp_path / "frames.jsonl")
    records = _record_session(path)
    recorded = [(r.raw[3], r.raw[4:-6]) for r in records if r.direction == "down"]

    received = []
    core = CommCore(on_frame=lambda t, p: received.append((t, p)), idle_timeout=5.0)
    transport = ReplayTransport(path, speed=20.0)
    assert core.open("replay", "", transport=transport), "回放通道未打开"
    try:
        assert _wait(lambda: len(received) >= len(recorded), 5.0), (
            f"只收到 {len(received)}/{len(recorded)} 帧"
        )
        assert received[: len(recorded)] == recorded
    finally:
        core.close()


def test_speed_and_single_step(tmp_path):
    clock = FakeClock()
    path = str(tmp_path / "frames.jsonl")
    recorder = FrameRecorder(path, clock=clock)
    frames = [encode(PacketType.TELEMETRY, bytes([i]) * 20) for i in range(3)]
    for index, raw in enumerate(frames):
        clock.now = index * 0.05
        recorder.record("down", raw)
    recorder.close()

    replay = FrameReplay(path, clock=clock)
    clock.now = 0.0
    replay.seek(0)
    assert replay.due_frames(0.0) == [frames[0]]
    assert replay.due_frames(0.02) == []
    assert replay.due_frames(0.06) == [frames[1]]
    assert replay.due_frames(0.11) == [frames[2]]

    # 单步：暂停后 due 不再出，点一下走一帧
    replay.seek(0)
    clock.now = 0.0
    replay.pause()
    assert replay.due_frames(1.0) == []
    assert replay.next_frame() == frames[0]
    assert replay.next_frame() == frames[1]
    replay.resume()
    assert replay.due_frames(1.0) == [frames[2]], "单步后继续播放不应跳帧或重复"


def test_speed_two_shortens_timeline(tmp_path):
    clock = FakeClock()
    path = str(tmp_path / "frames.jsonl")
    recorder = FrameRecorder(path, clock=clock)
    frames = [encode(PacketType.TELEMETRY, bytes([i]) * 20) for i in range(3)]
    for index, raw in enumerate(frames):
        clock.now = index * 0.05
        recorder.record("down", raw)
    recorder.close()

    replay = FrameReplay(path, speed=2.0, clock=clock)
    clock.now = 0.0
    replay.seek(0)
    assert replay.total == 3
    assert replay.due_frames(0.03) == [frames[0], frames[1]]
    assert replay.due_frames(0.06) == [frames[2]]
    assert abs(replay.remaining_s()) >= 0.0


def test_bad_lines_are_skipped_with_warning(tmp_path):
    path = tmp_path / "frames.jsonl"
    good = encode(PacketType.PING)
    path.write_text(
        json.dumps({"t_ms": 0, "dir": "down", "raw": good.hex()}) + "\n"
        + "{ 这不是 json\n"
        + json.dumps({"t_ms": 10, "dir": "up", "raw": good.hex()}) + "\n",
        encoding="utf-8",
    )
    records, warnings = load_records(str(path))
    assert len(records) == 2
    assert warnings and "无法解析" in warnings[0]
    replay = FrameReplay(str(path))
    assert replay.total == 1, "回放默认只取下行帧"


def test_replay_ignores_host_writes(tmp_path):
    path = str(tmp_path / "frames.jsonl")
    _record_session(path, ticks=5)
    transport = ReplayTransport(path)
    transport.open()
    transport.write(encode(PacketType.PID_SET, pack_pid_set(9, 9, 9, 9)))
    assert transport.inner.pending_in_bytes == 0
    transport.close()
