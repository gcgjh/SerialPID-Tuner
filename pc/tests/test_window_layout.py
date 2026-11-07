# -*- coding: utf-8 -*-
"""card16 窗口布局回归：窗口不再被最小高度撑出屏幕。

覆盖验收：
    * 请求 1440×900 / 1366×768 / 1024×680 都不再被强制撑高；
    * 窗口最小尺寸放得进 1366×768（内容靠滚动，不靠撑窗口）；
    * 左栏进滚动区，内容宽度不超出视口（不出现右侧被裁）；
    * 日志至少能看到 6 行，不被拉伸系数吃成一行；
    * 解析异常计数为 0 时不占状态栏，出现异常才显示；
    * 窗口尺寸与左栏宽度写进配置，下次启动恢复。

运行：cd pc && python -m pytest tests/test_window_layout.py -q
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.protocol import PacketType  # noqa: E402

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


def _make_window(monkeypatch, config_path):
    """在指定配置文件下构造并显示主窗口（show 之后几何才是真的）。"""
    from serialpid.gui.main_window import MainWindow

    monkeypatch.setenv("SERIALPID_CONFIG", str(config_path))
    app = _app()
    win = MainWindow()
    win.show()
    app.processEvents()
    return win


def test_requested_size_is_honoured(monkeypatch, tmp_path):
    """1440×900 请求就是 1440×900：不再被最小尺寸要求顶到 1167 高。"""
    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        for width, height in ((1440, 900), (1366, 768), (1024, 680)):
            win.resize(width, height)
            _app().processEvents()
            assert (win.width(), win.height()) == (width, height), (
                f"请求 {width}x{height} 被改成了 {win.width()}x{win.height()}"
            )
    finally:
        win.close()


def test_minimum_size_fits_1366x768(monkeypatch, tmp_path):
    """窗口最小尺寸放得进 1366×768，且最小尺寸要求不再由左栏内容决定。"""
    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        assert win.minimumWidth() == 960
        assert win.minimumHeight() == 640
        hint = win.minimumSizeHint()
        assert hint.height() <= 700, f"最小尺寸要求仍高达 {hint.height()}，1366×768 放不下"
    finally:
        win.close()


def test_left_column_in_scroll_area(monkeypatch, tmp_path):
    """左栏参数面板包在滚动区：内容宽不超出视口，纵向可滚到底；日志钉在底部不进滚动区。"""
    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        win.resize(1366, 768)
        _app().processEvents()
        scroll = win._left_scroll
        viewport = scroll.viewport()
        content = scroll.widget()
        assert content.width() <= viewport.width(), (
            f"滚动内容宽 {content.width()} 超出视口 {viewport.width()}，右侧会被裁掉"
        )
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 0, "1366×768 下左栏放不下却没出现滚动条"
        bar.setValue(bar.maximum())
        _app().processEvents()
        # 滚到底后内容末端（Z-N 面板底边）进入视口，即底部可到达
        zn_top = win._zn.mapTo(viewport, win._zn.rect().topLeft()).y()
        zn_bottom = zn_top + win._zn.height()
        assert bar.value() == bar.maximum()
        assert zn_bottom <= viewport.height() + 2, (
            f"滚到底后内容末端仍在视口外：{zn_bottom} > 视口 {viewport.height()}"
        )
        # 日志在滚动区外（固定底部），任何滚动位置都看得见
        assert win._log.parentWidget() is not content, "日志被塞进滚动区，矮屏上会被推出屏幕"
        log_top = win._log.mapTo(win, win._log.rect().topLeft()).y()
        log_bottom = log_top + win._log.height()
        assert log_top >= 0 and log_bottom <= win.height(), "日志超出窗口底部"
    finally:
        win.close()


def test_log_panel_keeps_at_least_six_lines(monkeypatch, tmp_path):
    """日志面板有最小高度：不再被拉伸系数压成一行。"""
    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        win.resize(1440, 900)
        _app().processEvents()
        view = win._log._view
        lines = view.height() // max(1, view.fontMetrics().lineSpacing())
        assert lines >= 6, f"日志只能看到约 {lines} 行"
        assert win._log.minimumHeight() >= 150
    finally:
        win.close()


def test_wheel_scrolls_left_panel_not_plot(monkeypatch, tmp_path):
    """滚轮归属：在左栏滚 = 滚面板，落在图上 = 图自己处理，互不串台。"""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        win.resize(1366, 768)
        _app().processEvents()
        bar = win._left_scroll.verticalScrollBar()
        viewport = win._left_scroll.viewport()

        def wheel(widget, dy):
            pos = QPointF(widget.rect().center())
            ev = QWheelEvent(
                pos,
                QPointF(widget.mapToGlobal(widget.rect().center())),
                QPoint(0, 0),
                QPoint(0, dy),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(widget, ev)
            _app().processEvents()

        bar.setValue(0)
        wheel(viewport, -120)  # 向下滚
        assert bar.value() > 0, "左栏滚轮没有滚动面板"

        bar.setValue(0)
        wheel(win._plot, -120)  # 图上滚动由图处理，不应让左栏跟着动
        assert bar.value() == 0, "图上滚轮把左栏也滚了（滚动事件被串台）"
    finally:
        win.close()


def test_parse_error_label_hidden_when_zero(monkeypatch, tmp_path):
    """解析异常为 0 时不占状态栏；出现异常后显示并带次数。"""
    win = _make_window(monkeypatch, tmp_path / "config.json")
    try:
        assert win._parse_stat.isVisible() is False
        win._on_frame(PacketType.TELEMETRY, b"")  # 长度不符 -> 记一条解析异常
        assert win._parse_errors >= 1
        assert win._parse_stat.text() == f"坏帧 {win._parse_errors}"
        assert win._parse_stat.isVisible() is True
    finally:
        win.close()


def test_window_geometry_persisted_and_restored(monkeypatch, tmp_path):
    """窗口尺寸与左栏宽度写进配置，下次启动恢复；越界值会被夹住。"""
    from serialpid.config import load_config

    cfg_path = tmp_path / "config.json"
    win = _make_window(monkeypatch, cfg_path)
    try:
        win.resize(1200, 760)
        _app().processEvents()
        win._save_ui_state()
    finally:
        win.close()

    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert raw["ui"]["window_width"] == 1200
    assert raw["ui"]["window_height"] == 760
    assert raw["ui"]["left_panel_width"] == win._left_width

    again = _make_window(monkeypatch, cfg_path)
    try:
        assert (again.width(), again.height()) == (1200, 760)
    finally:
        again.close()

    # 非法/越界配置：夹到合法区间，不阻塞启动
    cfg, _warn = load_config(cfg_path)
    cfg["ui"]["window_width"] = 99999
    cfg["ui"]["window_height"] = "坏值"
    cfg["ui"]["left_panel_width"] = 10
    from serialpid.config import save_config

    save_config(cfg, cfg_path)
    clamped = _make_window(monkeypatch, cfg_path)
    try:
        # 99999 这种离谱宽度会被夹到理智上限，而不是照搬
        assert 960 <= clamped.width() <= 4096
        assert clamped.height() >= 640
        assert clamped._left_width == 300
    finally:
        clamped.close()
