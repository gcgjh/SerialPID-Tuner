"""卡20：参数面板排布——预设行不挤压、下发按钮等宽、输入框撑满、状态有区分。

几何断言按左栏 340 px 的真实约束量，不看源码里的宽度数值；阈值留了余量，
换字体或换语言时只要控件仍能被正常阅读就不会误报。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except ImportError:  # pragma: no cover - 无 PySide6 环境
    _HAS_QT = False

import pytest  # noqa: E402

from serialpid.protocol import PacketType  # noqa: E402

#: 主窗口左栏固定宽度（卡16 若改成可调宽度，这里跟着改）
PANEL_WIDTH = 340


class _Worker:
    """可控连接状态的假 worker：既记录下发，也能切换"连没连上"。"""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.sent = []

    def send(self, pkt_type, payload) -> None:
        self.sent.append((pkt_type, payload))


def _app():
    return QApplication.instance() or QApplication([])


def _panel(tmp_path, worker=None):
    from serialpid.gui.param_panel import ParamPanel

    # 必须先有 QApplication 再建控件：无 app 时构造 QWidget 会让 Qt 直接 abort
    # （进程返回 0xC0000409，连 pytest 的失败摘要都刷不出来）。
    app = _app()
    panel = ParamPanel(worker, config_path=str(tmp_path / "config.json"))
    panel.resize(PANEL_WIDTH, panel.sizeHint().height())
    panel.show()
    app.processEvents()
    return panel


skip_no_qt = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")


@skip_no_qt
def test_preset_row_keeps_dropdown_readable(tmp_path):
    """预设行改成「下拉 + 菜单按钮」后：面板宽需求不超侧栏，下拉能显示完整名字。"""
    panel = _panel(tmp_path)
    assert panel.sizeHint().width() < PANEL_WIDTH, (
        f"面板宽需求 {panel.sizeHint().width()} px 超过左栏 {PANEL_WIDTH} px，"
        "控件会被挤压成旧模样"
    )
    assert panel._preset_box.width() >= 180, (
        f"下拉框只有 {panel._preset_box.width()} px，长预设名会显示不全"
    )

    actions = [a.text() for a in panel._preset_menu_btn.menu().actions()]
    assert len(actions) == 3, f"保存 / 加载 / 管理三个动作应全在菜单里，实际 {actions}"
    assert any("保存" in a for a in actions)
    assert any("加载" in a for a in actions)
    assert any("管理" in a for a in actions)


@skip_no_qt
def test_send_buttons_equal_width(tmp_path):
    """两个下发按钮等宽：靠拉伸系数，不用固定像素。"""
    panel = _panel(tmp_path)
    pid_w = panel._apply_pid.width()
    sp_w = panel._apply_sp.width()
    assert pid_w == sp_w, f"下发按钮宽窄不一：{pid_w} vs {sp_w}"
    assert pid_w >= 100, f"下发按钮只有 {pid_w} px，点击区域过小"


@skip_no_qt
def test_parameter_fields_fill_form_width(tmp_path):
    """四个参数输入框撑满表单，右侧不留大片空白。"""
    panel = _panel(tmp_path)
    group = panel._kp.parentWidget()
    for name, box in (
        ("Kp", panel._kp),
        ("Ki", panel._ki),
        ("Kd", panel._kd),
        ("SP", panel._sp),
    ):
        right_gap = group.width() - (box.x() + box.width())
        assert right_gap <= 16, f"{name} 输入框右侧留白 {right_gap} px，未撑满表单"


@skip_no_qt
def test_empty_dropdown_shows_explicit_placeholder(tmp_path, monkeypatch):
    """预设为空时下拉显示「未选择预设」，加载 / 管理动作置灰。"""
    panel = _panel(tmp_path)
    monkeypatch.setattr(panel, "_confirm_delete", lambda name: True)
    for name in list(panel.preset_names()):
        panel.delete_preset(name)

    assert panel._preset_box.currentText() == "未选择预设"
    assert panel._preset_action_load.isEnabled() is False
    assert panel._preset_action_manage.isEnabled() is False


@skip_no_qt
def test_preset_count_moved_to_tooltip(tmp_path):
    """「已保存 N 组」不再占状态行，挪到下拉 tooltip。"""
    panel = _panel(tmp_path)
    count = len(panel.preset_names())
    assert f"已保存 {count} 组" in panel._preset_box.toolTip()
    assert "已保存" not in panel._preset_status.text(), (
        f"状态行还在塞计数：{panel._preset_status.text()!r}"
    )


@skip_no_qt
def test_ack_status_distinguishes_not_connected_sent_and_result(tmp_path):
    """状态行四种含义各有一套文案与颜色：未连接 / 已发等待 / ACK 接受 / ACK 拒绝。"""
    worker = _Worker(connected=False)
    panel = _panel(tmp_path, worker)

    panel._send_pid()
    assert worker.sent and worker.sent[-1][0] == PacketType.PID_SET
    assert panel._ack_label.text().startswith("未连接"), (
        f"没连上却不说没发出去：{panel._ack_label.text()!r}"
    )
    assert "#e67e22" in panel._ack_label.styleSheet()

    worker.connected = True
    panel._send_pid()
    assert "已下发" in panel._ack_label.text() and "等待 ACK" in panel._ack_label.text()

    panel.show_ack(1.0, 0.5, 0.1, 25.0, 0)
    assert "ACK 已接受" in panel._ack_label.text()
    assert "#2ecc71" in panel._ack_label.styleSheet()

    panel.show_ack(1.0, 0.5, 0.1, 25.0, 1)
    assert "ACK 拒绝" in panel._ack_label.text()
    assert "#e74c3c" in panel._ack_label.styleSheet()
