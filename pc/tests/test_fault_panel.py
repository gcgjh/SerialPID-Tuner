# -*- coding: utf-8 -*-
"""card33 故障注入面板（纯控件，不连窗口）：默认全零、折算 LinkConfig、
滑杆实时发信号、一键恢复直通。

运行：cd pc && python -m pytest tests/test_fault_panel.py -q
"""

from __future__ import annotations

import os
import sys

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


def _panel():
    from serialpid.gui.fault_panel import FaultPanel

    _app()
    return FaultPanel()


def test_defaults_are_all_zero_and_passthrough() -> None:
    """三个滑杆默认全零是硬要求：正常演示路径必须零开销直通。"""
    panel = _panel()
    assert panel.params() == {"drop_pct": 0.0, "bad_pct": 0.0, "cut_s": 0.0}
    config = panel.config()
    assert config.enabled is False
    assert config.drop_down == 0.0
    assert config.bad_crc_down == 0.0
    assert config.disconnect_after_s == 0.0
    assert config.active() is False
    assert "全零" in panel.state_text()


def test_panel_starts_collapsed() -> None:
    panel = _panel()
    assert panel.is_expanded() is False
    panel.set_expanded(True)
    assert panel.is_expanded() is True


def test_sliders_map_to_downlink_link_config() -> None:
    panel = _panel()
    panel.set_params(drop_pct=20, bad_pct=10, cut_s=5)
    config = panel.config()
    assert config.enabled is True
    assert config.drop_down == pytest.approx(0.2)
    assert config.bad_crc_down == pytest.approx(0.1)
    assert config.disconnect_after_s == pytest.approx(5.0)
    # 上行不受影响：命令要是也被丢，"参数没生效"与"链路坏了"就分不清
    assert config.drop_up == 0.0 and config.bad_crc_up == 0.0
    assert "丢帧" in panel.state_text() and "断线" in panel.state_text()


def test_config_signal_carries_link_config() -> None:
    panel = _panel()
    seen = []
    panel.configChanged.connect(seen.append)
    panel.set_params(bad_pct=30)
    assert seen and seen[-1].bad_crc_down == pytest.approx(0.3)


def test_emit_false_keeps_quiet() -> None:
    panel = _panel()
    seen = []
    panel.configChanged.connect(seen.append)
    panel.set_params(drop_pct=10, emit=False)
    assert seen == []
    assert panel.config().drop_down == pytest.approx(0.1)


def test_reset_returns_to_passthrough_and_notifies() -> None:
    panel = _panel()
    panel.set_params(drop_pct=30, bad_pct=20, cut_s=10)
    seen = []
    panel.configChanged.connect(seen.append)
    panel.reset()
    assert panel.params() == {"drop_pct": 0.0, "bad_pct": 0.0, "cut_s": 0.0}
    assert panel.config().enabled is False
    assert seen and seen[-1].enabled is False


def test_values_are_clamped_to_slider_range() -> None:
    panel = _panel()
    panel.set_params(drop_pct=99, bad_pct=99, cut_s=999)
    from serialpid.gui.fault_panel import BAD_MAX_PCT, CUT_MAX_S, DROP_MAX_PCT

    assert panel.params()["drop_pct"] == DROP_MAX_PCT
    assert panel.params()["bad_pct"] == BAD_MAX_PCT
    assert panel.params()["cut_s"] == CUT_MAX_S
