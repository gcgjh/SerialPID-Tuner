"""卡13：预设管理接进参数面板（保存 / 加载 / 删除 / 路径 / 兼容读取）。

要点：加载只填界面、绝不自动下发——用例用假 worker 记录 send 调用来守住
这条安全边界；路径用例通过 ``SERIALPID_CONFIG`` 验证便携运行同目录。
"""

from __future__ import annotations

import json
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

from serialpid.config import DEFAULT_PARAM_LIMITS  # noqa: E402
from serialpid.protocol import PacketType  # noqa: E402


class _FakeWorker:
    """只记录下发调用，验证“加载不下发 / 点下发才发”。"""

    def __init__(self) -> None:
        self.sent = []

    def send(self, pkt_type, payload) -> None:
        self.sent.append((pkt_type, payload))


def _app():
    return QApplication.instance() or QApplication([])


def _panel(tmp_path, worker=None, **kwargs):
    from serialpid.gui.param_panel import ParamPanel

    return ParamPanel(worker, config_path=str(tmp_path / "config.json"), **kwargs)


skip_no_qt = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")


@skip_no_qt
def test_save_then_restart_persists_and_loads(tmp_path):
    """保存后重启程序（新面板读同一路径），预设仍在且能加载。"""
    _app()
    panel = _panel(tmp_path)
    panel._kp.setValue(4.5)
    panel._ki.setValue(0.25)
    panel._kd.setValue(0.07)
    panel._sp.setValue(42.0)

    saved = panel.store_preset("温控-保守", "先只留 P 观察方向")
    assert saved is not None
    assert "温控-保守" in panel.preset_names()

    restarted = _panel(tmp_path)
    assert restarted.presets_path == panel.presets_path
    assert "温控-保守" in restarted.preset_names()
    restarted._preset_box.setCurrentText("温控-保守")
    restarted._load_preset()
    assert restarted._kp.value() == pytest.approx(4.5)
    assert restarted._ki.value() == pytest.approx(0.25)
    assert restarted._kd.value() == pytest.approx(0.07)
    assert restarted._sp.value() == pytest.approx(42.0)
    assert "当前预设：温控-保守" in restarted._preset_status.text()


@skip_no_qt
def test_load_fills_fields_without_sending(tmp_path):
    """加载只填界面，worker 零调用；再点下发才真正写板子。"""
    _app()
    worker = _FakeWorker()
    panel = _panel(tmp_path, worker)

    panel._kp.setValue(3.0)
    panel._ki.setValue(0.2)
    panel.store_preset("候选组")

    panel._kp.setValue(1.0)
    panel._ki.setValue(0.01)
    panel._preset_box.setCurrentText("候选组")
    panel._load_preset()

    assert panel._kp.value() == pytest.approx(3.0)
    assert panel._ki.value() == pytest.approx(0.2)
    assert worker.sent == [], "加载绝不允许自动下发"

    panel._send_pid()
    assert len(worker.sent) == 1
    assert worker.sent[0][0] == PacketType.PID_SET


@skip_no_qt
def test_out_of_range_preset_clamped_with_explicit_hint(tmp_path):
    """旧预设超出当前收窄后的范围：按边界填入并明确提示，不静默。"""
    _app()
    legacy = {
        "schema": "serialpid.presets",
        "version": 1,
        "presets": [
            {"name": "大增益", "kp": 500.0, "ki": 0.02, "kd": 0.0, "sp": 30.0}
        ],
    }
    (tmp_path / "presets.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    panel = _panel(tmp_path)
    panel._preset_box.setCurrentText("大增益")
    panel._load_preset()

    assert panel._kp.maximum() == DEFAULT_PARAM_LIMITS["kp"]["max"] == 100.0
    assert panel._kp.value() == 100.0
    status = panel._preset_status.text()
    assert "超出当前范围" in status
    assert "已按边界 100 填入" in status
    assert "Kp 500" in status


@skip_no_qt
def test_delete_requires_confirmation(tmp_path, monkeypatch):
    """删除必须二次确认：拒绝则保留，确认才真删。"""
    _app()
    panel = _panel(tmp_path)
    panel.store_preset("待删组")
    assert "待删组" in panel.preset_names()

    monkeypatch.setattr(panel, "_confirm_delete", lambda name: False)
    assert panel.delete_preset("待删组") is False
    assert "待删组" in panel.preset_names()

    monkeypatch.setattr(panel, "_confirm_delete", lambda name: True)
    assert panel.delete_preset("待删组") is True
    assert "待删组" not in panel.preset_names()


@skip_no_qt
def test_rename_preset_updates_dropdown_and_loaded_name(tmp_path):
    _app()
    panel = _panel(tmp_path)
    panel.store_preset("旧名")
    panel._preset_box.setCurrentText("旧名")
    panel._load_preset()
    assert "当前预设：旧名" in panel._preset_status.text()

    assert panel.rename_preset("旧名", "新名") is True
    assert "新名" in panel.preset_names()
    assert "旧名" not in panel.preset_names()
    assert panel._preset_box.currentText() == "新名"
    assert "当前预设已重命名为：新名" in panel._preset_status.text()

    # 重名冲突：拒绝并保持原样
    panel.store_preset("占位")
    assert panel.rename_preset("新名", "占位") is False
    assert "新名" in panel.preset_names()


@skip_no_qt
def test_overwrite_needs_confirmation(tmp_path, monkeypatch):
    _app()
    panel = _panel(tmp_path)
    panel.store_preset("同名")
    monkeypatch.setattr(panel, "_confirm_overwrite", lambda name: False)
    assert panel.store_preset("同名", note="改备注") is None
    monkeypatch.setattr(panel, "_confirm_overwrite", lambda name: True)
    assert panel.store_preset("同名", note="改备注") is not None
    assert panel._presets.get("同名").note == "改备注"


@skip_no_qt
def test_presets_path_follows_config_env(tmp_path, monkeypatch):
    """便携运行：SERIALPID_CONFIG 指到哪，presets.json 就跟到哪。"""
    from serialpid.gui.param_panel import ParamPanel

    portable = tmp_path / "usb"
    portable.mkdir()
    cfg = portable / "config.json"
    monkeypatch.setenv("SERIALPID_CONFIG", str(cfg))

    _app()
    panel = ParamPanel(None)
    assert panel.presets_path == str(portable / "presets.json")
    assert os.path.isfile(panel.presets_path), "首次运行应在同目录写入预设文件"
    assert os.path.isfile(str(cfg)), "配置与预设必须同目录"


@skip_no_qt
def test_legacy_preset_file_without_version_or_note_loads(tmp_path):
    """老格式（无 version / note / sp / updated_at）能读进来并给缺省值。"""
    _app()
    legacy = {
        "schema": "serialpid.presets",
        "presets": [{"name": "老条目", "kp": 1.0, "ki": 0.05, "kd": 0.0}],
    }
    (tmp_path / "presets.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )
    panel = _panel(tmp_path)
    assert "老条目" in panel.preset_names()
    preset = panel._presets.get("老条目")
    assert preset.note == ""
    assert preset.sp is None
    assert preset.updated_at

    panel._preset_box.setCurrentText("老条目")
    panel._load_preset()
    assert "未固定 SP" in panel._preset_status.text()


@skip_no_qt
def test_default_preset_name_has_object_and_timestamp():
    from serialpid.gui.param_panel import OBJECT_NAME, ParamPanel

    _app()
    name = ParamPanel._default_preset_name()
    assert name.startswith(f"{OBJECT_NAME}-")
    # PID-YYYYMMDD-HHMM
    stamp = name[len(OBJECT_NAME) + 1:]
    assert len(stamp) == 13 and stamp[8] == "-" and stamp[:8].isdigit()
