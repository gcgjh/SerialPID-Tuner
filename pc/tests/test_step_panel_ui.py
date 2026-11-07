"""卡18：阶跃面板信息呈现（两态 / 右对齐值列 / 等宽按钮 / 单位换算）。

用例覆盖：
    * 未分析只一行提示，不铺七行破折号；
    * 分析失败（样本不足 / 无阶跃）同样只显示一行错误；
    * 分析后七项齐全，顺序按重要性、值列右对齐固定宽度、等宽数字；
    * 两个命令按钮等宽、「停止」用主题错误色；
    * 阶段状态与按钮同组；保持时长有秒换算；
    * 两态切换时分组框高度不跳动。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except ImportError:  # pragma: no cover - 无 PySide6 环境
    _HAS_QT = False

import pytest  # noqa: E402

from serialpid.gui.theme import COLORS  # noqa: E402
from serialpid.model import TelemetrySample  # noqa: E402

skip_no_qt = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")

_METRIC_ORDER = ("超调量", "调节时间", "稳态误差", "峰值", "上升时间", "峰值时间", "状态")


def _app():
    return QApplication.instance() or QApplication([])


def _panel():
    from serialpid.gui.step_panel import StepPanel

    _app()
    panel = StepPanel(None)
    panel.resize(320, 300)
    panel.show()
    _app().processEvents()
    return panel


def _feed_step_response(panel, n: int = 40, settle: bool = True) -> None:
    """喂一段带阶跃的遥测：前 10 点 SP=20，之后 SP=25，PV 一阶上升。"""
    for i in range(n):
        t = i * 100
        if i < 10:
            sp, pv = 20.0, 19.9
        else:
            sp = 25.0
            k = i - 10
            pv = 20.0 + min(5.0, k * 0.55)
            if k == 5:
                pv = 26.1  # 轻微超调
        panel.append_sample(TelemetrySample(t, sp, pv, 30.0, sp - pv))


@skip_no_qt
def test_idle_shows_single_hint_not_seven_dashes():
    panel = _panel()
    assert panel._metrics_stack.currentWidget() is panel._hint
    assert "尚未分析" in panel._hint.text()
    assert panel._metrics_body.isVisible() is False
    # 七行 label 仍在（分析后要用），但此刻一个都没显示出来
    visible = [k for k, lab in panel._metrics_labels.items() if lab.isVisible()]
    assert visible == [], f"未分析时不该显示指标行：{visible}"


@skip_no_qt
def test_analysis_reveals_seven_metrics_in_priority_order():
    panel = _panel()
    _feed_step_response(panel)
    panel._analyze_curve()

    assert panel._metrics_stack.currentWidget() is panel._metrics_body
    assert panel._metrics is not None
    keys = list(panel._metrics_labels.keys())
    assert tuple(keys) == _METRIC_ORDER, keys
    for key in _METRIC_ORDER:
        assert panel._metrics_labels[key].text() not in ("", "—") or key in (
            "上升时间",
            "峰值时间",
            "调节时间",
        )
    assert panel._metrics_labels["超调量"].text().endswith("%")
    assert panel._metrics_labels["峰值时间"].text().endswith("ms")


@skip_no_qt
def test_insufficient_samples_shows_one_error_line():
    panel = _panel()
    for i in range(3):
        panel.append_sample(TelemetrySample(i * 100, 20.0, 19.0, 0.0, 1.0))
    panel._analyze_curve()

    assert panel._metrics_stack.currentWidget() is panel._hint
    assert "样本不足" in panel._hint.text()
    assert COLORS["err"] in panel._hint.styleSheet()
    assert panel._metrics is None


@skip_no_qt
def test_no_step_shows_one_error_line():
    panel = _panel()
    for i in range(10):
        panel.append_sample(TelemetrySample(i * 100, 20.0, 19.0, 0.0, 1.0))
    panel._analyze_curve()

    assert panel._metrics_stack.currentWidget() is panel._hint
    assert "分析失败" in panel._hint.text()
    assert panel._metrics_body.isVisible() is False


@skip_no_qt
def test_values_right_aligned_monospace_and_flush_right():
    panel = _panel()
    from PySide6.QtGui import QFont

    rights = []
    for lab in panel._metrics_labels.values():
        assert lab.alignment() & Qt.AlignmentFlag.AlignRight
        # offscreen 平台不加载字体库（QFontDatabase.families() 为空），
        # 因此断言"请求了等宽族 + Monospace 提示"，而不是固定 pitch 解析结果
        font = lab.font()
        assert font.styleHint() == QFont.StyleHint.Monospace
        assert "Consolas" in font.families()
        rights.append(lab.mapTo(panel._metrics_body, lab.rect().topRight()).x())

    # 所有值右缘落在同一条竖线上（右对齐不跳动），且贴近面板右边界
    assert max(rights) - min(rights) <= 1, f"值列右缘不齐：{rights}"
    assert panel._metrics_body.width() - max(rights) <= 24, (
        f"值列没有撑满，右侧空 {panel._metrics_body.width() - max(rights)}px"
    )


@skip_no_qt
def test_command_buttons_equal_width_and_visually_distinct():
    panel = _panel()
    _app().processEvents()
    assert abs(panel._start.width() - panel._stop.width()) <= 1, (
        f"两按钮不等宽：{panel._start.width()} vs {panel._stop.width()}"
    )
    # 深色底上必须画出边框，否则按钮看起来就是一行文字
    assert "border" in panel._start.styleSheet()
    assert "border" in panel._stop.styleSheet()
    assert COLORS["err"] in panel._stop.styleSheet()
    assert COLORS["err"] not in panel._start.styleSheet()
    # 阶段状态与命令按钮同一行：纵向中心线接近
    start_y = panel._start.mapTo(panel, panel._start.rect().center()).y()
    phase_y = panel._phase_label.mapTo(panel, panel._phase_label.rect().center()).y()
    assert abs(start_y - phase_y) <= 6


@skip_no_qt
def test_hold_duration_has_seconds_hint():
    panel = _panel()
    assert "5.0 秒" in panel._hold_hint.text()
    panel._hold_ms.setValue(600_000)
    assert "600.0 秒" in panel._hold_hint.text()
    panel._hold_ms.setValue(0)
    assert "0.0 秒" in panel._hold_hint.text()
    # 下发仍按毫秒打包（协议字段不动）
    assert panel._hold_ms.suffix() == ""
    assert panel._hold_ms.value() == 0


@skip_no_qt
def test_switching_state_does_not_change_height():
    panel = _panel()
    idle_h = panel._metrics_stack.height()
    _feed_step_response(panel)
    panel._analyze_curve()
    _app().processEvents()
    assert panel._metrics_stack.height() == idle_h, "两态切换导致高度跳动"

    panel.clear_samples()
    _app().processEvents()
    assert panel._metrics_stack.currentWidget() is panel._hint
    assert panel._metrics_stack.height() == idle_h
