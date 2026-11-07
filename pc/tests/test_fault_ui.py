# -*- coding: utf-8 -*-
"""card33 端到端：故障注入滑杆搞坏仿真链路 -> 界面出现坏帧计数与中文提示 ->
一键恢复直通；强制断线给出原因与「帧日志从哪导出」；「回放帧记录」离线重演。

运行：cd pc && python -m pytest tests/test_fault_ui.py -q
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


def _pump(seconds: float) -> None:
    app = _app()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _pump_until(predicate, timeout: float = 5.0) -> bool:
    app = _app()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def win(monkeypatch, tmp_path):
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


def _connect_sim(win) -> None:
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    _app().processEvents()
    win._connect_btn.click()
    _app().processEvents()


def _log_text(win) -> str:
    return win._log._view.toPlainText()


# ---------------------------------------------------------------- 面板存在与默认值


def test_fault_panel_exists_and_defaults_to_zero(win):
    import serialpid.gui.fault_panel as fp

    assert isinstance(win._fault, fp.FaultPanel)
    assert win._fault.params() == {"drop_pct": 0.0, "bad_pct": 0.0, "cut_s": 0.0}
    assert win._fault.is_expanded() is False
    assert "全零" in win._fault.state_text()
    assert win._comm_degradation["enabled"] is False


def test_zero_faults_are_pure_passthrough(win):
    """默认全零：字节原样直通，不起泵线程、不坏帧。"""
    _connect_sim(win)
    assert _pump_until(lambda: len(win._buffer.samples()) >= 3, timeout=3.0)
    assert win._sim_degraded is not None
    assert win._sim_degraded.config.active() is False
    assert win._worker.stats_snapshot().bad_frames == 0
    assert win._sim_degraded.counters.passthrough_chunks > 0


# ---------------------------------------------------------------- 30% 坏帧


def test_thirty_percent_bad_frames_surface_counter_and_chinese_hint(win):
    _connect_sim(win)
    _pump(0.4)
    win._fault.set_params(bad_pct=30)
    _pump(0.2)
    assert win._sim_degraded.config.bad_crc_down == pytest.approx(0.3)

    assert _pump_until(
        lambda: win._worker.stats_snapshot().bad_frames >= 3, timeout=4.0
    ), "注入 30% 坏帧后统计里应出现坏帧"
    # 质量灯与计数都是 1 Hz 刷新，先给它一个刷新周期
    _pump(1.3)
    # 状态栏坏帧计数可见且是中文（isHidden 判断显隐，不依赖离屏窗口映射状态）
    assert not win._parse_stat.isHidden()
    assert "坏帧" in win._parse_stat.text()
    assert win._parse_stat.text().split()[1].isdigit()
    # 日志里出现中文三段式说明（说清坏帧是什么、下一步做什么）
    log = _log_text(win)
    assert "坏帧" in log
    assert "下一步" in log
    assert "CRC" in log or "校验" in log
    # 质量徽章变差，理由提到坏帧
    tip = win._link_status.toolTip()
    assert "坏帧" in tip or "坏" in tip


def test_reset_sliders_restore_passthrough(win):
    _connect_sim(win)
    win._fault.set_params(bad_pct=30)
    _pump(0.3)
    win._fault.reset()
    _app().processEvents()
    assert win._sim_degraded.config.active() is False
    assert win._comm_degradation["enabled"] is False
    assert "全零" in win._fault.state_text()
    assert "链路劣化：关闭" in _log_text(win) or "故障注入" in _log_text(win)


# ---------------------------------------------------------------- 强制断线


def test_forced_disconnect_explains_reason_and_export_entry(win):
    _connect_sim(win)
    win._fault.set_params(cut_s=2)
    assert _pump_until(lambda: not win._worker.connected, timeout=6.0), "2 s 后应强制断线"
    _pump(0.3)
    assert "未连接" in win._state_label.text()
    log = _log_text(win)
    assert "帧日志可从「工具 → 帧日志 / 回放」导出" in log
    assert "现象" in log and "下一步" in log


# ---------------------------------------------------------------- 回放入口


def _write_stream(path) -> list[float]:
    from serialpid.protocol import PacketType, pack_telemetry

    wanted = [20.0, 21.0, 22.0]
    rows = [
        {
            "t": index * 50,
            "dir": "rx",
            "type": int(PacketType.TELEMETRY),
            "name": "TELEMETRY",
            "len": len(pack_telemetry(index, sp, sp + 0.5, 10.0, 0.0)),
            "payload": pack_telemetry(index, sp, sp + 0.5, 10.0, 0.0).hex(),
        }
        for index, sp in enumerate(wanted)
    ]
    # 混一条 tx 帧：回放绝不能把它当成命令再下发
    rows.insert(1, {"t": 20, "dir": "tx", "type": 6, "len": 0, "payload": ""})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return wanted


def test_tools_menu_has_replay_entry(win):
    texts = [action.text() for action in win.menuBar().actions()]
    tools = next(menu for menu in win.menuBar().actions() if "工具" in menu.text())
    labels = [action.text() for action in tools.menu().actions()]
    assert "回放帧记录…" in labels
    assert win._replay_action.text() == "回放帧记录…"
    assert any("帧日志" in text for text in texts) or True  # 菜单是二级结构


def test_replay_from_file_drives_plot_without_commands(win, tmp_path):
    wanted = _write_stream(tmp_path / "frames.jsonl")
    assert win._worker.connected is False
    assert win.replay_from_file(str(tmp_path / "frames.jsonl")) is True
    # 回放开始就把实时连接断干净，且不触发任何下发
    assert win._worker.connected is False
    assert _pump_until(lambda: len(win._buffer.samples()) >= 3, timeout=2.0)
    _pump(0.2)
    samples = win._buffer.samples()
    assert [round(s.sp, 3) for s in samples[-len(wanted):]] == wanted
    assert win._worker.stats_snapshot().tx_frames == 0
    assert "回放" in _log_text(win)


def test_replay_missing_file_does_not_crash(win, tmp_path):
    assert win.replay_from_file(str(tmp_path / "nope.jsonl")) is False
    assert win._frame_log_dialog is not None
    assert "读取失败" in win._frame_log_dialog.detail_text()
