# -*- coding: utf-8 -*-
"""card17 图表可视化回归：图例样式、轴标签与单位、空态、工具条文案、横轴联动。

覆盖验收：
    * 图例有两条真实条目（不是"有图例对象、没图例条目"），底色来自主题、非纯黑；
    * 空图不画 0–1 假刻度：无数据时隐藏坐标轴、横轴固定 0–10s、居中给提示；
    * 第一帧数据到达即撤提示、还原坐标轴；
    * 上下两图都有完整轴标签，单位从 ``config.plot_units`` 读；
    * 工具条只有一个「跟随最新」按钮，状态用「跟随中 / 自由查看」；
    * 在一张图上缩放，另一张图横轴同步（自由模式仍可手动缩放）。

运行：cd pc && python -m pytest tests/test_plot_panel_ui.py -q
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from serialpid.config import DEFAULT_PLOT_UNITS  # noqa: E402
from serialpid.gui.plot_panel import PlotPanel  # noqa: E402
from serialpid.model import TelemetrySample  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _feed(panel: PlotPanel, n: int, step_ms: int = 50, jump_at: int = 0) -> None:
    for i in range(n):
        sp = 25.0 if i >= jump_at else 20.0
        pv = 20.0 if i < jump_at else 20.0 + min(4.6, (i - jump_at) * 0.1)
        panel.append(TelemetrySample((i + 1) * step_ms, sp, pv, 40.0, 0.0))


def _legend_labels(legend) -> list[str]:
    labels = []
    for entry in legend.items:
        label = entry[1] if isinstance(entry, tuple) else getattr(entry, "text", entry)
        labels.append(getattr(label, "text", str(label)))
    return labels


def test_empty_state_has_no_fake_ticks_and_hint(qapp):
    panel = PlotPanel()
    try:
        panel.show()
        qapp.processEvents()
        panel.render_now()

        # 空态：坐标轴隐藏，不给 0–1 的假刻度；横轴固定 0–10s
        for plot in (panel._pv_plot, panel._out_plot):
            assert not plot.getAxis("left").isVisible(), "空图仍显示纵轴刻度"
            assert not plot.getAxis("bottom").isVisible(), "空图仍显示横轴刻度"
            vr = plot.getViewBox().viewRange()[0]
            assert vr == pytest.approx([0.0, 10.0]), f"空态横轴不是固定 0–10s：{vr}"

        assert panel._empty_text.isVisible(), "空图没有「等待数据」提示"
        assert not panel._legend.isVisible(), "空图仍显示空图例"
        assert panel.point_count == 0
    finally:
        panel.close()


def test_first_frame_clears_empty_state(qapp):
    panel = PlotPanel()
    try:
        panel.show()
        qapp.processEvents()
        panel.render_now()
        assert panel._empty_text.isVisible()

        _feed(panel, 20)
        panel.render_now()
        qapp.processEvents()

        assert not panel._empty_text.isVisible(), "有数据了提示还在"
        for plot in (panel._pv_plot, panel._out_plot):
            assert plot.getAxis("left").isVisible()
            assert plot.getAxis("bottom").isVisible()
        assert panel._legend.isVisible(), "有数据时图例应显示"
    finally:
        panel.close()


def test_legend_has_entries_and_theme_background(qapp):
    panel = PlotPanel()
    try:
        panel.show()
        _feed(panel, 120, jump_at=60)
        panel.render_now()
        qapp.processEvents()

        labels = _legend_labels(panel._legend)
        # 两条曲线都要在图例里（上一版 addLegend 晚于 plot，条目数为 0）
        assert labels == ["设定值 SP", "过程量 PV"], f"图例条目不对：{labels}"

        color = panel._legend.brush().color() if callable(panel._legend.brush) else panel._legend.brush().color()
        rgb = color.getRgb()[:3]
        assert rgb != (0, 0, 0), "图例底色仍是纯黑（深色界面上等于隐形）"
        assert color.alpha() >= 200, f"图例底色透明度 {color.alpha()}，应接近不透明"
        pen = panel._legend.pen() if callable(panel._legend.pen) else panel._legend.pen
        assert pen.color().name().lower() != "#c8c8c8", "图例边框仍是浅色主题默认色"
    finally:
        panel.close()


def test_axis_labels_cover_all_axes_with_units(qapp):
    panel = PlotPanel(units={"pv": "℃", "sp": "℃", "out": "%", "time": "s"})
    try:
        axes = {
            "pv_left": panel._pv_plot.getAxis("left"),
            "pv_bottom": panel._pv_plot.getAxis("bottom"),
            "out_left": panel._out_plot.getAxis("left"),
            "out_bottom": panel._out_plot.getAxis("bottom"),
        }
        for name, axis in axes.items():
            assert axis.labelText, f"{name} 没有轴标签"
            assert axis.labelUnits, f"{name} 没有单位"
        assert axes["pv_left"].labelUnits == "℃"
        assert axes["pv_bottom"].labelUnits == "s"
        assert axes["out_left"].labelUnits == "%"
        assert axes["out_bottom"].labelUnits == "s"
    finally:
        panel.close()


def test_units_come_from_config(qapp, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"plot_units": {"pv": "K", "out": "A", "time": "min"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SERIALPID_CONFIG", str(cfg_path))
    panel = PlotPanel()
    try:
        assert panel._pv_plot.getAxis("left").labelUnits == "K"
        assert panel._pv_plot.getAxis("bottom").labelUnits == "min"
        assert panel._out_plot.getAxis("left").labelUnits == "A"
        # 配置里没写的项保留默认
        assert panel._units["sp"] == DEFAULT_PLOT_UNITS["sp"]
    finally:
        panel.close()


def test_toolbar_has_single_follow_button_and_distinct_status(qapp):
    panel = PlotPanel()
    try:
        buttons = [b.text() for b in panel.findChildren(QPushButton)]
        assert buttons.count("跟随最新") == 1, f"「跟随最新」出现 {buttons.count('跟随最新')} 次：{buttons}"
        assert panel._mode_label.text() == "跟随中"

        vb = panel._pv_plot.getViewBox()
        vb.setXRange(5.0, 15.0, padding=0)
        vb.sigRangeChangedManually.emit({"x": True, "y": False})
        assert panel._mode_label.text() == "自由查看"
        assert "跟随最新" not in panel._mode_label.text(), "状态标签又用了按钮文案"
    finally:
        panel.close()


def test_manual_zoom_on_out_plot_syncs_pv_plot(qapp):
    panel = PlotPanel()
    try:
        panel.show()
        _feed(panel, 200)
        panel.render_now()
        qapp.processEvents()

        vb_out = panel._out_plot.getViewBox()
        vb_out.setXRange(2.0, 4.0, padding=0)
        vb_out.sigRangeChangedManually.emit({"x": True, "y": False})
        qapp.processEvents()

        assert not panel.is_following(), "在下图缩放后没有进入自由模式"
        pv_range = panel._pv_plot.getViewBox().viewRange()[0]
        assert pv_range == pytest.approx([2.0, 4.0]), f"上图横轴没跟上：{pv_range}"
        out_range = vb_out.viewRange()[0]
        assert out_range == pytest.approx([2.0, 4.0])
    finally:
        panel.close()


def test_render_keeps_timeline_after_ui_state_changes(qapp):
    """空态与数据态来回切换后，时间轴仍连续（card11 行为不被 card17 破坏）。"""
    panel = PlotPanel()
    try:
        _feed(panel, 40)
        panel.render_now()
        x = panel._extract(0)[:, 0]
        assert bool((x[1:] >= x[:-1]).all())
        panel.clear()
        panel.render_now()
        assert panel.point_count == 0 and panel._empty_view
        _feed(panel, 10)
        panel.render_now()
        assert panel._extract(0)[0, 0] == pytest.approx(0.0)
    finally:
        panel.close()
