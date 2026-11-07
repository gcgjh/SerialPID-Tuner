# -*- coding: utf-8 -*-
"""card28 界面接线回归：状态栏质量灯、ERROR 中文化、六类断线提示、
自动重连（默认关 + 退避 + 次数）、帧日志导出与回放。

运行：cd pc && python -m pytest tests/test_link_ui.py -q
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
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
    """构造主窗口；自检换成桩，配置写到临时目录，避免污染真实配置。"""
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    monkeypatch.setattr(st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[]))
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    app = _app()
    window = mw.MainWindow()
    window.show()
    app.processEvents()
    yield window
    window.close()
    app.processEvents()


def _log_text(win) -> str:
    return win._log._view.toPlainText()


# ---------------------------------------------------------------- 状态栏质量


def test_link_status_is_a_permanent_status_bar_widget(win):
    assert win._link_status is not None
    assert win.statusBar().isAncestorOf(win._link_status)
    assert win._link_status.text() == "未连接"


def test_refresh_feeds_widget_from_worker_stats(win, monkeypatch):
    """1 Hz 刷新路径：worker 快照 -> 控件文本/质量。"""
    from serialpid.link_stats import LinkStats

    stats = LinkStats()
    for _ in range(40):
        stats.note_rx(20)
    stats.note_bad(1)
    monkeypatch.setattr(win._worker, "stats_snapshot", lambda: stats.snapshot())
    win._refresh_link_status()
    assert win._link_status.quality() == "warn"
    assert win._link_status.text().startswith("链路偏差")

    # 坏帧比例升高 -> 红
    stats.note_bad(10)
    win._refresh_link_status()
    assert win._link_status.quality() == "bad"
    assert win._link_status.text().startswith("链路差")


def test_widget_shows_estimated_loss_count(win):
    from serialpid.link_stats import LinkStats

    stats = LinkStats()
    for _ in range(20):
        stats.note_rx(20)
    for t_ms in (0, 50, 100):
        stats.note_telemetry(t_ms)
    stats.note_telemetry(300)  # 少 3 帧
    win._link_status.update_snapshot(stats.snapshot())
    assert "约丢 3" in win._link_status.text()
    # 详情口径写清"约数"：用户不该把它当精确计数
    assert "推断" in win._link_status.toolTip()


def test_timer_is_running_at_one_hz(win):
    import serialpid.gui.main_window as mw

    assert win._stats_timer.isActive()
    assert win._stats_timer.interval() == mw.LINK_STATUS_REFRESH_MS


# ---------------------------------------------------------------- ERROR 中文化


def test_error_frame_is_logged_in_chinese(win):
    from serialpid.protocol import PacketType, pack_error

    win._on_frame(PacketType.ERROR, pack_error(0x02, "payload len 15 != 16"))
    text = _log_text(win)
    assert "载荷长度" in text, text
    assert "固件原话" in text
    assert "建议" in text
    assert "code=0x02" in text


def test_unknown_error_code_still_explains_itself(win):
    from serialpid.protocol import PacketType, pack_error

    win._on_frame(PacketType.ERROR, pack_error(0x7F, "mystery"))
    text = _log_text(win)
    assert "未定义的错误码 0x7F" in text
    assert "贴给开发者" in text


# ---------------------------------------------------------------- 断线提示


@pytest.mark.parametrize(
    "reason",
    [
        "timeout",
        "heartbeat-timeout",
        "peer-closed",
        "device-removed",
        "driver-error",
        "read-error",
    ],
)
def test_six_disconnect_reasons_are_localized(win, reason):
    win._on_connection(False, reason)
    assert win._state_label.text().startswith("未连接（")
    assert reason not in win._state_label.text(), "状态栏还挂着内部英文串"
    text = _log_text(win)
    assert "现象：" in text and "下一步：" in text


def test_disconnect_log_is_full_three_part_with_cause(win):
    """卡35：状态栏之外，日志里必须是完整的三行（含"原因："），不是只有结论。"""
    win._on_connection(False, "device-removed")
    text = _log_text(win)
    assert "现象：设备被拔出" in text
    assert "原因：" in text
    assert "下一步：" in text
    message = win._last_disconnect_message
    assert message is not None and message.cause


def test_heartbeat_timeout_logs_miss_count_and_setting_hint(win):
    """心跳判死要带 N 次与容忍上限，并指向「工具 → 链路设置…」。"""
    win._comm_settings["heartbeat_max_missed"] = 4
    win._worker._stats.note_heartbeat_gap(4)  # 模拟判死当刻"欠了 4 次心跳"
    win._on_connection(False, "heartbeat-timeout")
    text = _log_text(win)
    assert "连续 4 次" in text
    assert "容忍上限 4 次" in text
    assert "链路设置" in text


def test_worker_error_is_three_part_not_raw_prefix(win):
    """线程回报的原始异常翻成三段式；不再出现 "错误: xxx" 这种只有结论的行。"""
    win._on_worker_error("读取异常: ClearCommError failed (拒绝访问)")
    text = _log_text(win)
    assert "错误: " not in text
    assert "现象：" in text and "原因：" in text and "下一步：" in text
    assert "读取异常" in text, "原始异常要留在原因里，方便贴日志"
    assert win._last_worker_error is not None


def test_user_disconnect_says_so(win):
    win._on_connection(False, "closed")
    assert "已断开" in win._state_label.text()
    assert "无需处理" in _log_text(win)


def test_connected_resets_retry_counter(win):
    win._retry_attempt = 5
    win._on_connection(True, "COM3")
    assert win._retry_attempt == 0
    assert win._state_label.text() == "已连接"


# ---------------------------------------------------------------- 自动重连


def test_auto_reconnect_is_off_by_default(win):
    assert not win._auto_reconnect.isChecked()
    win._reconnect_args = ("serial", "COM9", 115200)
    assert win._schedule_reconnect("device-removed") is False
    assert not win._reconnect_timer.isActive()


def test_auto_reconnect_backoff_and_attempt_counter(win):
    win._auto_reconnect.setChecked(True)
    win._reconnect_args = ("serial", "COM9", 115200)

    assert win._schedule_reconnect("heartbeat-timeout") is True
    assert win._retry_attempt == 1
    assert win._reconnect_timer.isActive()
    assert win._reconnect_timer.interval() == 1000
    assert "第 1 次" in win._state_label.text()
    assert "1 s 后" in win._state_label.text()

    assert win._schedule_reconnect("heartbeat-timeout") is True
    assert win._retry_attempt == 2
    assert win._reconnect_timer.interval() == 2000

    assert win._schedule_reconnect("heartbeat-timeout") is True
    assert win._reconnect_timer.interval() == 5000


def test_auto_reconnect_gives_up_after_max_attempts(win):
    import serialpid.comm as comm

    win._auto_reconnect.setChecked(True)
    win._reconnect_args = ("serial", "COM9", 115200)
    win._retry_attempt = comm.RECONNECT_MAX_ATTEMPTS
    assert win._schedule_reconnect("driver-error") is False
    assert "自动重连已放弃" in win._state_label.text()
    assert "放弃" in _log_text(win)


def test_user_disconnect_never_schedules_reconnect(win):
    win._auto_reconnect.setChecked(True)
    win._reconnect_args = ("serial", "COM9", 115200)
    win._user_disconnected = True
    assert win._schedule_reconnect("heartbeat-timeout") is False


def test_unchecking_cancels_pending_retry(win):
    win._auto_reconnect.setChecked(True)
    win._reconnect_args = ("serial", "COM9", 115200)
    assert win._schedule_reconnect("device-removed") is True
    win._auto_reconnect.setChecked(False)
    assert not win._reconnect_timer.isActive()
    assert win._retry_attempt == 0
    assert "自动重连已关闭" in _log_text(win)


def test_failed_reopen_schedules_next_attempt(win, monkeypatch):
    from serialpid.comm_qt import CommWorker

    win._auto_reconnect.setChecked(True)
    win._reconnect_args = ("serial", "COM9", 115200)
    win._retry_attempt = 1
    monkeypatch.setattr(win._worker, "open", lambda *a, **k: False)
    monkeypatch.setattr(CommWorker, "connected", property(lambda self: False))
    win._last_open_error = "拒绝访问"
    win._attempt_reconnect()
    assert win._retry_attempt == 2
    assert win._reconnect_timer.isActive()


# ---------------------------------------------------------------- 帧日志


def test_frame_log_dialog_exports_and_replays(win, tmp_path):
    """导出 -> 读回 -> 回放，曲线数据必须与录下来的逐点一致。"""
    from serialpid.frame_trace import DIR_RX
    from serialpid.protocol import PacketType, pack_telemetry

    rows = [(0, 20.0), (50, 21.0), (100, 22.0)]
    for t_ms, sp in rows:
        win._worker.trace.append(
            DIR_RX, PacketType.TELEMETRY,
            pack_telemetry(t_ms, sp, sp - 1.0, 10.0, 0.5), t_ms=t_ms,
        )

    dialog = win.build_frame_log_dialog()
    assert "缓冲 3 / 2000 帧" in dialog.summary_text()

    path = tmp_path / "frames.jsonl"
    assert dialog.export_to(str(path), "jsonl")
    assert path.exists() and path.read_text(encoding="utf-8").count("\n") == 3

    assert dialog.load_file(str(path))
    assert dialog.cursor() is not None
    assert dialog.cursor().total == 3

    dialog.step_once()
    dialog.step_once()
    dialog.step_once()
    samples = win._buffer.samples()
    assert len(samples) == 3, f"回放没有把帧送进界面: {len(samples)}"
    assert [round(s.sp, 2) for s in samples] == [20.0, 21.0, 22.0]
    assert [s.t_ms for s in samples] == [0, 50, 100]
    assert "回放结束" in _log_text(win)


def test_replay_does_not_feed_tx_frames(win, tmp_path):
    """回放只放 rx：tx 是上位机自己发的命令，重放只会重复下发。"""
    from serialpid.frame_trace import DIR_RX, DIR_TX
    from serialpid.protocol import PacketType, pack_telemetry

    win._worker.trace.append(DIR_TX, PacketType.PID_SET, b"\x00" * 16, t_ms=0)
    win._worker.trace.append(
        DIR_RX, PacketType.TELEMETRY, pack_telemetry(0, 5.0, 4.0, 3.0, 1.0), t_ms=10
    )
    dialog = win.build_frame_log_dialog()
    path = tmp_path / "mixed.jsonl"
    assert dialog.export_to(str(path), "jsonl")
    assert dialog.load_file(str(path))
    assert dialog.cursor().total == 1


def test_frame_log_text_export_mentions_frames(win, tmp_path):
    from serialpid.frame_trace import DIR_RX
    from serialpid.protocol import PacketType, pack_telemetry

    win._worker.trace.append(
        DIR_RX, PacketType.TELEMETRY, pack_telemetry(0, 28.0, 27.5, 10.0, -0.5), t_ms=0
    )
    dialog = win.build_frame_log_dialog()
    path = tmp_path / "frames.txt"
    assert dialog.export_to(str(path), "txt")
    content = path.read_text(encoding="utf-8")
    assert "帧日志" in content and "TELEMETRY" in content
    assert "SP=28.000" in content


def test_frame_log_menu_exists(win):
    assert hasattr(win, "_frame_log_action")
    assert "帧日志" in win._frame_log_action.text()
