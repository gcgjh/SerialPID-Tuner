# -*- coding: utf-8 -*-
"""card28 / card33 帧级日志与回放回归：环形缓冲、JSONL 往返、坏行策略、
两套格式兼容、倍速与单步。

覆盖验收：「帧日志可导出、可回放」「畸形文件坏行跳过并计数」。
运行：cd pc && python -m pytest tests/test_frame_trace.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.frame_trace import (  # noqa: E402
    DIR_RX,
    DIR_TX,
    FrameTrace,
    ReplayCursor,
    TraceEntry,
    load_jsonl,
    load_jsonl_lenient,
    rx_only,
    summarize,
)
from serialpid.protocol import PacketType, pack_telemetry, unpack_telemetry  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _trace(capacity: int = 2000) -> FrameTrace:
    return FrameTrace(capacity=capacity, clock=_Clock())


# ---------------------------------------------------------------- 环形缓冲


def test_ring_buffer_keeps_latest_and_counts_dropped() -> None:
    trace = _trace(capacity=3)
    for index in range(5):
        trace.append(DIR_RX, PacketType.TELEMETRY, bytes([index]), t_ms=index * 10)
    entries = trace.snapshot()
    assert len(entries) == 3
    assert trace.dropped() == 2
    assert [e.t_ms for e in entries] == [20, 30, 40]


def test_reset_clears_entries_and_dropped() -> None:
    trace = _trace(capacity=2)
    for index in range(4):
        trace.append(DIR_RX, PacketType.PONG, b"", t_ms=index)
    trace.reset()
    assert len(trace) == 0
    assert trace.dropped() == 0


def test_relative_timestamp_uses_clock_origin() -> None:
    clock = _Clock()
    trace = FrameTrace(capacity=10, clock=clock)
    clock.now = 1.0
    trace.append(DIR_RX, PacketType.PONG)
    clock.now = 1.25
    entry = trace.append(DIR_RX, PacketType.PONG)
    assert entry is not None
    assert entry.t_ms == 250


def test_bad_direction_is_rejected() -> None:
    trace = _trace()
    with pytest.raises(ValueError):
        trace.append("up", PacketType.PONG)


# ---------------------------------------------------------------- 导出


def test_jsonl_round_trip_preserves_frames() -> None:
    trace = _trace()
    payload = pack_telemetry(1234, 28.0, 27.9, 12.5, 0.1)
    trace.append(DIR_RX, PacketType.TELEMETRY, payload, t_ms=0)
    trace.append(DIR_TX, PacketType.PID_SET, b"\x01\x02", t_ms=50)

    text = trace.to_jsonl()
    back = load_jsonl(text)
    assert [e.t_ms for e in back] == [0, 50]
    assert [e.direction for e in back] == [DIR_RX, DIR_TX]
    assert back[0].payload == payload
    assert unpack_telemetry(back[0].payload)[1] == pytest.approx(28.0)


def test_export_has_no_port_or_path_fields() -> None:
    """导出会给别人看：只留帧本身与相对时刻，不留端口 / 路径 / 序列号。"""
    trace = _trace()
    trace.append(DIR_RX, PacketType.TELEMETRY, pack_telemetry(0, 1, 1, 1, 0), t_ms=0)
    text = trace.to_jsonl()
    assert "COM" not in text
    for line in text.splitlines():
        assert set(_json_keys(line)) == {
            "t", "dir", "dir_label", "type", "name", "len", "payload"
        }


def test_jsonl_has_direction_label_and_bounds_length() -> None:
    """方向既给机器码也给中文标签；长度字段与实际载荷一致。"""
    import json

    trace = _trace()
    payload = pack_telemetry(7, 28, 27.5, 10, 0.5)
    trace.append(DIR_RX, PacketType.TELEMETRY, payload, t_ms=10)
    trace.append(DIR_TX, PacketType.PING, b"", t_ms=20)
    rows = [json.loads(line) for line in trace.to_jsonl().splitlines()]
    assert rows[0]["dir"] == "rx" and rows[0]["dir_label"] == "接收"
    assert rows[1]["dir"] == "tx" and rows[1]["dir_label"] == "发送"
    assert rows[0]["len"] == len(payload) == len(bytes.fromhex(rows[0]["payload"]))
    assert rows[0]["name"] == "TELEMETRY"


def test_lenient_loader_skips_bad_lines_and_counts() -> None:
    good = '{"t":0,"dir":"rx","type":1,"len":0,"payload":""}'
    text = "\n".join([good, "{oops", good, '{"t":1,"dir":"x","type":1,"payload":""}', good])
    entries, skipped, notes = load_jsonl_lenient(text)
    assert len(entries) == 3
    assert skipped == 2
    assert "第 2 行" in notes[0]
    # 严格版对同一份文件仍然报错，两条路径各司其职
    with pytest.raises(ValueError):
        load_jsonl(text)


def test_lenient_loader_reads_recorder_format() -> None:
    """``sim/recorder.py`` 录的整帧格式也能读：raw 里解出类型与载荷。"""
    from serialpid.protocol import encode

    raw = encode(PacketType.TELEMETRY, pack_telemetry(5, 28, 27, 10, 0))
    line = (
        '{"t_ms":40,"dir":"down","type":1,"len":%d,"raw":"%s"}'
        % (len(raw), raw.hex())
    )
    entries, skipped, _notes = load_jsonl_lenient(line)
    assert skipped == 0
    assert entries[0].direction == "rx"
    assert entries[0].pkt_type == PacketType.TELEMETRY
    assert unpack_telemetry(entries[0].payload)[1] == pytest.approx(28.0)


def test_direction_aliases_fold_to_rx_tx() -> None:
    for line, expect in (
        ('{"t":0,"dir":"up","type":2,"payload":""}', "tx"),
        ('{"t":0,"dir":"down","type":2,"payload":""}', "rx"),
    ):
        assert load_jsonl(line)[0].direction == expect


def test_memory_estimate_is_bounded() -> None:
    trace = FrameTrace(capacity=50, clock=_Clock())
    for index in range(50):
        trace.append(DIR_RX, PacketType.PONG, b"", t_ms=index)
    size = trace.memory_estimate_bytes()
    assert size > 50 * 100
    assert size < 200 * 1024  # 50 条空载荷帧不该超过 200 KB


def _json_keys(line: str) -> list[str]:
    import json

    return list(json.loads(line).keys())


def test_bad_line_raises_with_line_number() -> None:
    trace = _trace()
    trace.append(DIR_RX, PacketType.PONG, b"", t_ms=0)
    good = trace.to_jsonl()
    broken = good + "{not json}\n"
    with pytest.raises(ValueError) as info:
        load_jsonl(broken)
    assert "第 2 行" in str(info.value)


def test_length_mismatch_raises() -> None:
    line = '{"t":0,"dir":"rx","type":1,"len":3,"payload":"aabb"}'
    with pytest.raises(ValueError) as info:
        load_jsonl(line)
    assert "len" in str(info.value)


def test_illegal_hex_raises() -> None:
    line = '{"t":0,"dir":"rx","type":1,"len":2,"payload":"zz11"}'
    with pytest.raises(ValueError):
        load_jsonl(line)


def test_blank_lines_are_tolerated() -> None:
    line = '{"t":0,"dir":"rx","type":1,"len":0,"payload":""}'
    assert len(load_jsonl("\n" + line + "\n\n")) == 1


def test_text_export_is_readable() -> None:
    trace = _trace()
    trace.append(DIR_RX, PacketType.TELEMETRY, pack_telemetry(100, 28, 27.5, 10, -0.5), t_ms=100)
    text = trace.to_text()
    assert "+0.100s" in text
    assert "←" in text
    assert "遥测" in text or "TELEMETRY" in text


# ---------------------------------------------------------------- 回放


def _cursor() -> ReplayCursor:
    entries = [
        TraceEntry(0, DIR_RX, PacketType.TELEMETRY, pack_telemetry(0, 1, 1, 1, 0)),
        TraceEntry(100, DIR_RX, PacketType.TELEMETRY, pack_telemetry(100, 1, 1, 1, 0)),
        TraceEntry(200, DIR_RX, PacketType.TELEMETRY, pack_telemetry(200, 1, 1, 1, 0)),
        TraceEntry(300, DIR_RX, PacketType.TELEMETRY, pack_telemetry(300, 2, 1, 1, 1)),
    ]
    return ReplayCursor(entries, speed=1.0)


def test_advance_releases_frames_in_original_order() -> None:
    cursor = _cursor()
    assert len(cursor.advance(0.0)) == 1  # 第一条帧在 0 ms，立刻就该放出来
    assert cursor.position == 1
    assert len(cursor.advance(50.0)) == 0
    assert len(cursor.advance(50.0)) == 1
    assert cursor.position == 2
    assert cursor.elapsed_ms == pytest.approx(100.0)


def test_speed_multiplies_wall_clock() -> None:
    cursor = _cursor()
    cursor.set_speed(5.0)
    # 20 ms 墙上时间 × 5 倍 = 100 ms 回放时间，够放到第二条帧
    due = cursor.advance(20.0)
    assert [e.t_ms for e in due] == [0, 100]


def test_speed_is_clamped() -> None:
    cursor = _cursor()
    assert cursor.set_speed(0.01) == 0.25
    assert cursor.set_speed(9999) == 20.0
    assert cursor.set_speed(float("nan")) == 1.0


def test_step_forces_one_frame_and_catches_up_clock() -> None:
    cursor = _cursor()
    first = cursor.step()
    assert len(first) == 1 and first[0].t_ms == 0
    cursor.advance(30.0)  # 墙上时间只走了 30 ms，不该放出第二条
    second = cursor.step()
    assert second[0].t_ms == 100
    assert cursor.elapsed_ms >= 100.0


def test_progress_and_duration() -> None:
    cursor = _cursor()
    assert cursor.total == 4
    assert cursor.duration_ms == pytest.approx(300.0)
    cursor.advance(400.0)
    assert cursor.finished
    assert cursor.progress() == 1.0
    assert cursor.step() == []


def test_base_is_first_frame_so_truncated_files_replay() -> None:
    """从中间截取的日志也能回放：基准取第一条帧而不是 0。"""
    entries = [
        TraceEntry(5000, DIR_RX, PacketType.PONG, b""),
        TraceEntry(5050, DIR_RX, PacketType.PONG, b""),
    ]
    cursor = ReplayCursor(entries)
    assert len(cursor.advance(0.0)) == 1
    assert cursor.duration_ms == pytest.approx(50.0)


def test_rx_only_drops_commands() -> None:
    entries = [
        TraceEntry(0, DIR_TX, PacketType.PID_SET, b""),
        TraceEntry(10, DIR_RX, PacketType.TELEMETRY, b""),
        TraceEntry(20, DIR_TX, PacketType.PING, b""),
    ]
    kept = rx_only(entries)
    assert len(kept) == 1 and kept[0].direction == DIR_RX


def test_summarize_counts_by_name() -> None:
    entries = [
        TraceEntry(0, DIR_RX, PacketType.TELEMETRY, b""),
        TraceEntry(10, DIR_RX, PacketType.TELEMETRY, b""),
        TraceEntry(20, DIR_RX, PacketType.PONG, b""),
    ]
    counts = summarize(entries)
    assert sum(counts.values()) == 3
    assert max(counts.values()) == 2


def test_empty_trace_exports_gracefully() -> None:
    trace = _trace()
    assert trace.to_jsonl() == ""
    assert "帧数 0" in trace.to_text()
    cursor = ReplayCursor([])
    assert cursor.finished and cursor.progress() == 1.0
