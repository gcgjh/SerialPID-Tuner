# -*- coding: utf-8 -*-
"""card32 回归：心跳界面化（默认关 / 运行期可改 / 写回配置）与链路质量徽章三态。

覆盖验收：
1. 默认不开心跳，且行为与旧版一致（空闲 1.0 s 判死）；
2. GUI 能开心跳并看到 RTT（不重连、下一轮接收循环即生效）；
3. 仿真链路劣化 20% 丢帧 -> 徽章黄、50% -> 徽章红（真实 CommCore + DegradedTransport）；
4. 悬停文本有完整统计（收发帧数 / 坏帧 / 约丢帧率 / RTT 均值·最大·抖动）；
5. 统计清零与重连自动重置；数字用等宽字体请求防抖。

运行：cd pc && python -m pytest tests/test_link_settings.py -q
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover - 依赖环境
    _HAS_QT = False

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def win(monkeypatch, tmp_path):
    """主窗口；自检打桩、配置写临时目录（绝不碰真实 config.json）。"""
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    monkeypatch.setattr(st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[]))
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    app = _app()
    window = mw.MainWindow()
    window.show()
    app.processEvents()
    window._tmp_config = str(tmp_path / "config.json")
    yield window
    window.close()
    app.processEvents()


def _log_text(win) -> str:
    return win._log._view.toPlainText()


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _open_sim(*, heartbeat: float = 0.0, missed: int = 3, idle: float = 1.0):
    """起一个真实 CommCore + 进程内通道（不连串口、不连 TCP）。"""
    from serialpid.comm import CommCore
    from serialpid.sim.transport import SimTransport

    transport = SimTransport()
    core = CommCore(
        idle_timeout=idle, heartbeat_interval=heartbeat, heartbeat_max_missed=missed
    )
    assert core.open("sim", "sim", 0, transport=transport)
    return core, transport


# ---------------------------------------------------------------- 默认与夹取


def test_heartbeat_is_off_by_default(win):
    """默认关（card32 口径）：低质量链路开了心跳会频繁断开重连、干扰演示。"""
    import serialpid.gui.main_window as mw

    assert mw.DEFAULT_HEARTBEAT_INTERVAL_S == 0.0
    assert win._worker.heartbeat_interval == 0.0
    # 不开心跳 = 旧版行为：空闲 1.0 s 判死，不能留着上次算出来的大窗口
    assert win._worker.idle_timeout == 1.0


def test_clamp_keeps_zero_as_off_and_bounds_the_rest():
    from serialpid.comm import (
        HEARTBEAT_INTERVAL_MAX_S,
        HEARTBEAT_INTERVAL_MIN_S,
        HEARTBEAT_MAX_MISSED_MAX,
        HEARTBEAT_MAX_MISSED_MIN,
        clamp_heartbeat,
    )

    assert clamp_heartbeat(0.0, 3) == (0.0, 3), "0 必须保留为关闭，不能被夹成下限"
    assert clamp_heartbeat(0.01, 0)[0] == HEARTBEAT_INTERVAL_MIN_S
    assert clamp_heartbeat(999.0, 99) == (
        HEARTBEAT_INTERVAL_MAX_S,
        HEARTBEAT_MAX_MISSED_MAX,
    )
    assert clamp_heartbeat(1.0, 0)[1] == HEARTBEAT_MAX_MISSED_MIN


def test_set_heartbeat_recomputes_death_window_at_runtime():
    core, _transport = _open_sim(idle=5.0)
    try:
        core.set_heartbeat(0.5, 2)
        assert (core.heartbeat_interval, core.heartbeat_max_missed) == (0.5, 2)
        assert core.idle_timeout == pytest.approx(0.5 * 2 + 1.0)
        # 关掉必须回到 1.0 s，而不是沿用 2.0 s
        core.set_heartbeat(0.0)
        assert core.heartbeat_interval == 0.0
        assert core.idle_timeout == 1.0
    finally:
        core.close()


# ---------------------------------------------------------------- 界面 -> 通信


def test_dialog_defaults_reflect_config(win):
    dialog = win.build_link_settings_dialog()
    assert dialog.heartbeat_enabled.isChecked() is False
    assert dialog.heartbeat_values() == (0.0, 3)
    # 关掉时置灰但保留一个可用的间隔（再打开不用重填）
    assert dialog.interval_spin.isEnabled() is False
    assert dialog.interval_spin.value() == pytest.approx(1.0)


def test_enabling_heartbeat_applies_without_reconnect(win):
    dialog = win.build_link_settings_dialog()
    dialog.heartbeat_enabled.setChecked(True)
    dialog.interval_spin.setValue(0.5)
    dialog.missed_spin.setValue(2)

    assert win._worker.heartbeat_interval == pytest.approx(0.5)
    assert win._worker.heartbeat_max_missed == 2
    assert win._worker.idle_timeout == pytest.approx(2.0)
    text = _log_text(win)
    assert "心跳已开启" in text and "0.5 s" in text
    assert "2.0 s" in text, "日志没写清判死窗口，用户没法核对"


def test_disabling_heartbeat_restores_legacy_behavior(win):
    dialog = win.build_link_settings_dialog()
    dialog.heartbeat_enabled.setChecked(True)
    dialog.interval_spin.setValue(3.0)
    assert win._worker.idle_timeout == pytest.approx(3.0 * 3 + 1.0)

    dialog.heartbeat_enabled.setChecked(False)
    assert win._worker.heartbeat_interval == 0.0
    assert win._worker.idle_timeout == 1.0
    assert "与旧版行为一致" in _log_text(win)


def test_heartbeat_settings_are_persisted(win):
    dialog = win.build_link_settings_dialog()
    dialog.heartbeat_enabled.setChecked(True)
    dialog.interval_spin.setValue(2.5)
    dialog.missed_spin.setValue(4)

    cfg = json.loads(open(win._tmp_config, encoding="utf-8").read())
    comm = cfg["comm"]
    assert comm["heartbeat_interval_s"] == pytest.approx(2.5)
    assert comm["heartbeat_max_missed"] == 4
    # idle 由间隔 × 次数推导：留着旧的手工值会让改间隔后窗口不跟着变
    assert "idle_timeout_s" not in comm


def test_persisted_settings_are_read_back(monkeypatch, tmp_path):
    import serialpid.gui.main_window as mw

    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    cfg = {"comm": {"heartbeat_interval_s": 1.5, "heartbeat_max_missed": 2}}
    settings = mw.comm_settings(cfg)
    assert settings["heartbeat_interval_s"] == pytest.approx(1.5)
    assert settings["heartbeat_max_missed"] == 2
    assert settings["idle_timeout_s"] == pytest.approx(1.5 * 2 + mw.HEARTBEAT_IDLE_MARGIN_S)


def test_no_ping_when_heartbeat_is_off_then_ping_after_enabling():
    """不开心跳不发 PING；运行期打开后下一轮循环就发——不用断开重连。"""
    core, _transport = _open_sim(heartbeat=0.0, idle=5.0)
    try:
        time.sleep(0.3)
        assert core.stats_snapshot().tx_frames == 0

        core.set_heartbeat(0.2, 3)
        assert _wait_until(lambda: core.stats_snapshot().tx_frames >= 1, timeout=1.5)
    finally:
        core.close()


def test_pong_settles_rtt_on_live_heartbeat():
    """真实链路往返：PING 发出去、PONG 回来，RTT 落到统计里（状态栏才有的显示）。"""
    from serialpid.protocol import PacketType, encode, pack_pong

    core, transport = _open_sim(heartbeat=0.2, missed=3)
    try:
        assert _wait_until(lambda: core.stats_snapshot().tx_frames >= 1, timeout=1.5)
        transport.emit(encode(PacketType.PONG, pack_pong(0x02, 0)))
        assert _wait_until(
            lambda: core.stats_snapshot().rtt_avg_ms is not None, timeout=1.5
        )
        snap = core.stats_snapshot()
        assert snap.rtt_last_ms is not None and snap.rtt_last_ms >= 0.0
        assert snap.rtt_max_ms is not None and snap.rtt_max_ms >= snap.rtt_last_ms
        assert snap.jitter_ms is not None
    finally:
        core.close()


def test_rtt_sliding_window_keeps_recent_samples():
    """RTT 最近 N 次滑动队列：换线后的新值会把旧值挤出窗口。"""
    from serialpid.link_stats import RTT_SAMPLES, LinkStats

    stats = LinkStats()
    for _ in range(30):
        stats.note_rtt(100.0)  # 旧的差线
    for _ in range(RTT_SAMPLES):
        stats.note_rtt(10.0)  # 换线后的好线
    snap = stats.snapshot()
    assert snap.rtt_recent_n == RTT_SAMPLES
    assert snap.rtt_recent_avg_ms == pytest.approx(10.0)
    assert snap.rtt_max_ms == pytest.approx(100.0), "会话最大值仍要留住历史峰值"
    stats.reset()
    assert stats.snapshot().rtt_recent_n == 0


# ---------------------------------------------------------------- 徽章三态


def _degraded_snapshot(drop_down: float, frames: int = 200) -> object:
    """真实劣化层 + 真实通信核心：按 50 ms 周期喂 N 帧遥测。"""
    from serialpid.comm import CommCore
    from serialpid.protocol import PacketType, encode, pack_telemetry
    from serialpid.sim.degradation import DegradedTransport, LinkConfig
    from serialpid.sim.transport import SimTransport

    inner = SimTransport()
    degraded = DegradedTransport(
        inner, LinkConfig(enabled=True, drop_down=drop_down, seed=20261107)
    )
    core = CommCore(idle_timeout=5.0, heartbeat_interval=0.0)
    assert core.open("sim", "sim", 0, transport=degraded)
    try:
        for i in range(frames):
            degraded.emit(
                encode(PacketType.TELEMETRY, pack_telemetry(i * 50, 20.0, 19.0, 10.0, 1.0))
            )
        _wait_until(lambda: core.stats_snapshot().rx_frames > 0, timeout=1.0)
        # 等接收线程把缓冲读空（读数连续两轮不变即视为稳定）
        _wait_until(lambda: _stable(core), timeout=2.0)
        return core.stats_snapshot()
    finally:
        core.close()


def _stable(core) -> bool:
    a = core.stats_snapshot().rx_frames
    time.sleep(0.02)
    b = core.stats_snapshot().rx_frames
    return a == b


def test_badge_ok_when_link_is_clean():
    snap = _degraded_snapshot(0.0)
    assert snap.rx_frames > 0
    assert snap.quality == "ok"
    assert snap.lost_frames == 0


def test_badge_yellow_at_20pct_drop_and_red_at_50pct(win):
    """验收题：20% 丢帧转黄、50% 转红；颜色之外文字也要能读出来。"""
    from serialpid.gui.theme import COLORS

    warn_snap = _degraded_snapshot(0.20)
    win._link_status.update_snapshot(warn_snap)
    assert warn_snap.lost_frames > 0
    assert warn_snap.quality == "warn"
    assert win._link_status.quality() == "warn"
    assert win._link_status.text().startswith("链路偏差")
    assert COLORS["warn"] in win._link_status._dot.styleSheet()

    bad_snap = _degraded_snapshot(0.50)
    win._link_status.update_snapshot(bad_snap)
    assert bad_snap.quality == "bad"
    assert win._link_status.quality() == "bad"
    assert win._link_status.text().startswith("链路差")
    assert COLORS["err"] in win._link_status._dot.styleSheet()


def test_badge_tooltip_has_full_statistics(win):
    from serialpid.link_stats import LinkStats

    stats = LinkStats()
    for _ in range(40):
        stats.note_rx(20)
    for t_ms in (0, 50, 100):
        stats.note_telemetry(t_ms)
    stats.note_telemetry(250)  # 推断少 3 帧
    stats.note_rtt(12.0)
    stats.note_rtt(20.0)
    win._link_status.update_snapshot(stats.snapshot())
    tip = win._link_status.toolTip()
    assert "帧：收" in tip and "/ 发" in tip, "悬停没给收发帧数"
    assert "坏 " in tip
    assert "约丢" in tip and "推断" in tip
    assert "均值" in tip and "最大" in tip and "抖动" in tip


def test_badge_uses_monospace_font_request(win):
    """数字防抖：只断言"请求了什么"（离屏字体库为空，解析结果不可信）。"""
    font = win._link_status._text.font()
    assert font.styleHint() == QFont.StyleHint.Monospace
    families = font.families()
    assert "Consolas" in families
    assert "monospace" in families


def test_badge_shows_rtt_once_heartbeat_is_on(win, monkeypatch):
    """GUI 打开心跳后，状态栏要能读到往返时延（不是只有链路上有）。"""
    from serialpid.link_stats import LinkStats

    dialog = win.build_link_settings_dialog()
    dialog.heartbeat_enabled.setChecked(True)

    stats = LinkStats()
    for _ in range(40):
        stats.note_rx(20)
    stats.note_rtt(8.0)
    stats.note_rtt(14.0)
    monkeypatch.setattr(win._worker, "stats_snapshot", lambda: stats.snapshot())
    win._refresh_link_status()
    assert stats.snapshot().rtt_avg_ms is not None
    assert "ms" in win._link_status.text(), win._link_status.text()
    assert "往返时延" in win._link_status.toolTip()


# ---------------------------------------------------------------- 故障红灯


def test_fault_badge_turns_red_and_survives_one_hz_refresh(win):
    """心跳判死：红灯 + 原因；1 Hz 刷新不能把它刷回灰的"未连接"。"""
    from serialpid.gui.theme import COLORS

    win._on_connection(False, "heartbeat-timeout")
    assert win._link_status.quality() == "bad"
    assert win._link_status.text().startswith("链路断：")
    assert COLORS["err"] in win._link_status._dot.styleSheet()
    assert "心跳" in win._link_status.toolTip()

    win._refresh_link_status()
    assert win._link_status.quality() == "bad", "定时刷新把故障红灯刷掉了"
    assert win._link_status.text().startswith("链路断：")

    # 连上就不再是"链路断"：故障标记清掉，徽章回到实时质量
    win._on_connection(True, "COM7")
    assert win._link_fault is None
    assert win._link_status.is_fault() is False


def test_user_disconnect_is_not_a_fault(win):
    win._on_connection(False, "closed")
    assert win._link_fault is None
    assert win._link_status.is_fault() is False


# ---------------------------------------------------------------- 统计清零


def test_stats_reset_clears_counters_without_dropping_link():
    from serialpid.protocol import PacketType, encode, pack_telemetry

    core, transport = _open_sim(idle=5.0)
    try:
        for i in range(5):
            transport.emit(
                encode(PacketType.TELEMETRY, pack_telemetry(i * 50, 20.0, 19.0, 10.0, 1.0))
            )
        assert _wait_until(lambda: core.stats_snapshot().rx_frames >= 5, timeout=1.5)
        assert core.stats_snapshot().lost_frames == 0

        core.reset_stats()
        snap = core.stats_snapshot()
        assert snap.rx_frames == 0 and snap.tx_frames == 0
        assert snap.lost_frames == 0 and snap.bad_frames == 0
        assert snap.uptime_s < 0.5
        assert core.connected, "清零把连接也断了"
    finally:
        core.close()


def test_worker_reset_stats_is_wired_to_dialog(win):
    dialog = win.build_link_settings_dialog()
    dialog.reset_button.click()
    assert "链路统计已清零" in _log_text(win)


def test_reconnect_starts_a_fresh_session():
    """断开重连后统计自动从 0 开始（CommCore.open 里做 reset）。"""
    from serialpid.protocol import PacketType, encode, pack_telemetry
    from serialpid.sim.transport import SimTransport

    from serialpid.comm import CommCore

    transport = SimTransport()
    core = CommCore(idle_timeout=5.0)
    assert core.open("sim", "sim", 0, transport=transport)
    for i in range(5):
        transport.emit(
            encode(PacketType.TELEMETRY, pack_telemetry(i * 50, 20.0, 19.0, 10.0, 1.0))
        )
    assert _wait_until(lambda: core.stats_snapshot().rx_frames >= 5, timeout=1.5)
    core.close()

    transport2 = SimTransport()
    assert core.open("sim", "sim", 0, transport=transport2)
    try:
        assert core.stats_snapshot().rx_frames == 0
    finally:
        core.close()
