# -*- coding: utf-8 -*-
"""card28 链路质量统计回归：丢帧推断、复位/回绕分流、RTT 与质量判定。

覆盖验收：「状态栏质量指示随丢帧变化」背后的纯逻辑。
运行：cd pc && python -m pytest tests/test_link_stats.py -q
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.link_stats import (  # noqa: E402
    QUALITY_BAD,
    QUALITY_IDLE,
    QUALITY_OK,
    QUALITY_WARN,
    LinkStats,
)

U32 = 1 << 32


class _Clock:
    """可控时钟：让"窗口内帧率"和会话时长可复算，不依赖真实等待。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now


def _feed(stats: LinkStats, stamps) -> None:
    for t_ms in stamps:
        stats.note_telemetry(t_ms)


# ---------------------------------------------------------------- 丢帧推断


def test_first_telemetry_establishes_period_without_loss() -> None:
    stats = LinkStats(clock=_Clock())
    assert stats.note_telemetry(1000) == 0
    assert stats.note_telemetry(1050) == 0
    assert stats.note_telemetry(1100) == 0
    assert stats.snapshot().lost_frames == 0


def test_gap_infers_missing_frames() -> None:
    """周期 50 ms，隔了 200 ms：中间少了 3 帧。"""
    stats = LinkStats(clock=_Clock())
    _feed(stats, [0, 50, 100])
    assert stats.note_telemetry(300) == 3
    assert stats.snapshot().lost_frames == 3


def test_gap_within_1_5_period_is_not_loss() -> None:
    """50 ms 周期下 70 ms 的间隔算抖动，不记丢帧（口径写在 details 里）。"""
    stats = LinkStats(clock=_Clock())
    _feed(stats, [0, 50, 100])
    assert stats.note_telemetry(170) == 0
    assert stats.snapshot().lost_frames == 0


def test_u32_timestamp_wrap_is_not_reset_or_loss() -> None:
    """时间戳回绕：0xFFFF_FFxx -> 0x0000_00xx 是计数器翻转，不是复位。"""
    stats = LinkStats(clock=_Clock())
    stamps = [U32 - 150, U32 - 100, U32 - 50, 0, 50, 100]
    total = 0
    for t_ms in stamps:
        total += stats.note_telemetry(t_ms)
    assert total == 0, "回绕被误判成丢帧"
    assert stats.snapshot().lost_frames == 0


def test_device_reset_jump_is_neither_loss_nor_wrap() -> None:
    """设备复位：时间戳从大值回落到小值，负差超过 2^31 -> 只挪基准。"""
    stats = LinkStats(clock=_Clock())
    _feed(stats, [1000, 1050, 1100])
    assert stats.note_telemetry(0) == 0
    assert stats.snapshot().lost_frames == 0
    # 复位后的新会话继续按新基准推丢帧
    _feed(stats, [50, 100])
    assert stats.note_telemetry(300) == 3


# ---------------------------------------------------------------- 质量判定


def test_bad_frames_drive_quality_to_bad() -> None:
    stats = LinkStats(clock=_Clock())
    for _ in range(10):
        stats.note_rx(20)
    stats.note_bad(3)  # 3 / 13 ≈ 23% > 5% 阈值
    snap = stats.snapshot()
    assert snap.quality == QUALITY_BAD
    assert "坏帧" in snap.quality_reason


def test_single_bad_frame_is_only_warn() -> None:
    stats = LinkStats(clock=_Clock())
    for _ in range(40):
        stats.note_rx(20)
    stats.note_bad(1)
    assert stats.snapshot().quality == QUALITY_WARN


def test_lost_frames_drive_quality_to_warn_then_bad() -> None:
    stats = LinkStats(clock=_Clock())
    for _ in range(10):
        stats.note_rx(20)
    _feed(stats, [0, 50, 100])
    stats.note_telemetry(200)  # 少 1 帧 -> 1/11 ≈ 9%
    assert stats.snapshot().quality == QUALITY_WARN
    stats.note_telemetry(500)  # 再少 5 帧 -> 6/16 ≈ 37%
    assert stats.snapshot().quality == QUALITY_BAD


def test_heartbeat_misses_warn_but_not_bad() -> None:
    """有遥测却没 PONG：链路还活着，只提示，不判死。"""
    stats = LinkStats(clock=_Clock())
    for _ in range(30):
        stats.note_rx(20)
    stats.note_ping()
    stats.note_ping()
    snap = stats.snapshot()
    assert snap.quality == QUALITY_WARN
    assert snap.heartbeat_misses == 2


def test_pong_settles_heartbeat_debt() -> None:
    stats = LinkStats(clock=_Clock())
    stats.note_ping()
    stats.note_ping()
    stats.note_pong()
    assert stats.snapshot().heartbeat_misses == 0


def test_rtt_average_max_and_jitter() -> None:
    stats = LinkStats(clock=_Clock())
    stats.note_rtt(10.0)
    stats.note_rtt(30.0)
    snap = stats.snapshot()
    # 指数平滑：avg = 0.8*10 + 0.2*30 = 14；抖动先取偏差 20，再平滑到 4
    assert abs(snap.rtt_avg_ms - 14.0) < 0.01
    assert snap.rtt_last_ms == 30.0
    assert snap.rtt_max_ms == 30.0
    assert abs(snap.jitter_ms - 4.0) < 0.01


def test_snapshot_when_disconnected_is_idle() -> None:
    stats = LinkStats(clock=_Clock())
    stats.note_rx(20)
    snap = stats.snapshot(connected=False)
    assert snap.quality == QUALITY_IDLE
    assert snap.summary() == "未连接"


def test_tx_and_byte_rate_are_counted() -> None:
    stats = LinkStats(clock=_Clock())
    stats.note_tx(10)
    stats.note_tx(10)
    stats.note_rx(20)
    snap = stats.snapshot()
    assert snap.tx_frames == 2
    assert snap.rx_frames == 1
    assert snap.bytes_per_s > 0
    assert snap.quality == QUALITY_OK


def test_details_explains_estimate_caliber() -> None:
    """悬停提示必须写清"约丢"是推断值，否则会被当成精确计数。"""
    stats = LinkStats(clock=_Clock())
    _feed(stats, [0, 50, 100])
    stats.note_telemetry(300)
    details = stats.snapshot().details()
    assert "约丢" in details
    assert "推断" in details or "估算" in details


def test_reset_clears_session_counters() -> None:
    clock = _Clock()
    stats = LinkStats(clock=clock)
    stats.note_rx(20)
    stats.note_bad(2)
    stats.note_ping()
    clock.now = 5.0
    stats.reset()
    snap = stats.snapshot()
    assert snap.rx_frames == 0
    assert snap.bad_frames == 0
    assert snap.heartbeat_misses == 0
    assert snap.uptime_s < 0.01
