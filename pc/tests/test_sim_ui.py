# -*- coding: utf-8 -*-
"""card25 接口接线回归：内置模拟器、端口三态、自检横幅、串口失败提示。

覆盖验收：
    * 通道下拉第三项是「内置模拟器」，选中后目标框/波特率/刷新都置灰；
    * 串口枚举为空或失败时不出现假 ``COM1``，只给一条不可选中的说明项；
    * 点连接即创建引擎 + 进程内通道，一秒内就有遥测进缓冲；
    * 7 个参数改值即时生效（对象参数 + 控制周期）；
    * 5 个场景预设能把参数整组填进去，并重置对象状态；
    * 断开 / 切通道都会把引擎停掉（后台不留空转线程）；
    * 启动自检全通过 -> 横幅隐藏；警告 -> 黄色；失败 -> 红色且可复制诊断；
    * 串口打开失败按「占用 / 不存在 / 权限」给不同提示。

运行：cd pc && python -m pytest tests/test_sim_ui.py -q
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import Qt  # noqa: E402
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
    """跑事件循环若干秒（离屏下 pumps 就能让跨线程信号送达主线程）。"""
    app = _app()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def win(monkeypatch, tmp_path):
    """构造主窗口；自检换成桩，避免每个用例都去扫磁盘/写日志。"""
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    def _fake_selftest(**_kwargs):
        return st.SelfTestReport(checks=[])

    monkeypatch.setattr(st, "run_selftest", _fake_selftest)
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    app = _app()
    window = mw.MainWindow()
    window.show()
    app.processEvents()
    yield window
    window.close()
    app.processEvents()


# ---------------------------------------------------------------- 通道与端口


def test_channel_has_third_sim_option(win):
    texts = [win._transport.itemText(i) for i in range(win._transport.count())]
    assert texts == ["TCP", "Serial", "内置模拟器"]


def test_sim_channel_disables_target_baud_refresh(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    _app().processEvents()
    assert win._target.currentText() == mw.SIM_TARGET_TEXT
    assert not win._target.isEnabled()
    assert not win._baud.isEnabled()
    assert not win._refresh.isEnabled()


def test_serial_empty_state_has_no_fake_com1(win, monkeypatch):
    import serialpid.gui.main_window as mw
    from serialpid.port_scan import PortScanResult

    monkeypatch.setattr(
        mw, "scan_ports",
        lambda *a, **k: PortScanResult(status="empty", backend_available=True),
    )
    win._transport.setCurrentText(mw.CHANNEL_SERIAL)
    _app().processEvents()

    labels = [win._target.itemText(i) for i in range(win._target.count())]
    assert labels == [mw.PORT_EMPTY_TEXT]
    assert "COM1" not in labels
    model = win._target.model()
    assert model.item(0) is not None and not model.item(0).isEnabled()
    # 说明项不可当成设备去连
    assert win._serial_target() == ""
    assert win._diag.isVisibleTo(win)


def test_serial_error_state_points_to_diagnostics(win, monkeypatch):
    import serialpid.gui.main_window as mw
    from serialpid.port_scan import PortScanResult, diagnose

    result = PortScanResult(
        status="error",
        error_type="TimeoutError",
        error_message="枚举超时",
    )
    monkeypatch.setattr(mw, "scan_ports", lambda *a, **k: result)
    win._transport.setCurrentText(mw.CHANNEL_SERIAL)
    _app().processEvents()

    assert win._target.itemText(0) == mw.PORT_ERROR_TEXT
    assert win._serial_target() == ""
    # 诊断文本能拿到（真点了会弹模态框，这里只验文本来源）
    text = diagnose(result)
    assert "端口" in text and "超时" in text


def test_serial_list_shows_devices_with_bare_data(win, monkeypatch):
    import serialpid.gui.main_window as mw
    from serialpid.port_scan import PortInfo, PortScanResult

    ports = [
        PortInfo("COM3", "USB 串行设备 (COM3)", "USB VID:PID=1A86:7523", "wch.cn"),
        PortInfo("COM9", "CH340", "USB VID:PID=1A86:7523"),
    ]
    monkeypatch.setattr(mw, "scan_ports", lambda *a, **k: PortScanResult(status="ok", ports=ports))
    win._transport.setCurrentText(mw.CHANNEL_SERIAL)
    _app().processEvents()

    assert win._target.currentData() == "COM3"
    assert win._target.currentText() == ports[0].display_name
    assert win._target.itemData(1, Qt.ToolTipRole) == ports[1].detail_line()
    assert win._serial_target() == "COM3"


# ---------------------------------------------------------------- 串口失败提示


@pytest.mark.parametrize(
    "message, expected",
    [
        ("could not open port 'COM3': PermissionError(13, '拒绝访问。')", "占用或权限"),
        ("could not open port 'COM8': FileNotFoundError(2, '系统找不到指定的文件。')", "不存在"),
        ("could not open port 'COM4': SerialException(31, 'WinError 31 连接到系统的设备没有发挥作用')", "独占"),
    ],
)
def test_serial_error_hints_are_actionable(message, expected):
    from serialpid.gui.main_window import _explain_serial_error

    advice = _explain_serial_error(message)
    assert expected in advice
    assert advice != message


def test_serial_error_unknown_passes_through():
    from serialpid.gui.main_window import _explain_serial_error

    assert _explain_serial_error("something odd") == "something odd"
    assert _explain_serial_error("") == "未知原因"


def test_serial_open_failure_logs_actionable_advice(win, monkeypatch):
    """真连串口失败时，日志里要给"下一步做什么"，而不是原样抛 pyserial 原文。"""
    import serialpid.gui.main_window as mw
    from serialpid.port_scan import PortInfo, PortScanResult

    monkeypatch.setattr(
        mw, "scan_ports",
        lambda *a, **k: PortScanResult(status="ok", ports=[PortInfo("COM3", "USB 串行设备")]),
    )
    win._transport.setCurrentText(mw.CHANNEL_SERIAL)
    _app().processEvents()

    message = "could not open port 'COM3': PermissionError(13, '拒绝访问。')"

    def fake_open(*args, **kwargs):
        win._worker.errorOccurred.emit(message)
        return False

    monkeypatch.setattr(win._worker, "open", fake_open)
    win._connect_btn.click()
    _app().processEvents()

    text = win._log._view.toPlainText()
    assert "COM3" in text
    assert "占用或权限" in text


# ---------------------------------------------------------------- 仿真接线


def test_sim_connect_streams_telemetry_within_one_second(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    win._connect_btn.click()
    _pump(1.0)

    assert win._worker.connected
    assert win._sim_engine is not None and win._sim_engine.is_running()
    assert len(win._buffer) >= 5, "一秒内应该已经收到遥测"
    assert win._state_label.text() == "已连接"


def test_sim_params_apply_live(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    win._connect_btn.click()
    _pump(0.2)
    engine = win._sim_engine
    assert engine is not None

    panel = win._sim
    panel.set_params({"gain": 3.5, "tau1": 0.5, "tau2": 1.0, "delay": 2,
                      "noise": 0.8, "quant": 0.02, "period_ms": 100})
    params = engine.plant.params
    assert params.gain == 3.5
    assert params.tau1_s == 0.5
    assert params.tau2_s == 1.0
    assert params.delay_steps == 2
    assert params.noise_std == 0.8
    assert params.quantum == 0.02
    # 周期改动经 set_period 落到引擎（并联动对象步长）
    assert engine.snapshot()["period_s"] == pytest.approx(0.1)
    assert engine.plant.dt_s == pytest.approx(0.1)


def test_sim_presets_fill_all_seven_params(win):
    from serialpid.gui.sim_panel import PARAM_ORDER, PRESETS

    win._sim.set_expanded(True)
    for name, expected in PRESETS.items():
        assert win._sim.apply_preset(name) is True
        got = win._sim.params()
        assert set(got) == set(PARAM_ORDER)
        for key in PARAM_ORDER:
            assert got[key] == pytest.approx(expected[key]), f"{name}.{key}"


def test_sim_preset_resets_plant_state(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    win._connect_btn.click()
    _pump(0.6)
    engine = win._sim_engine
    before = engine.snapshot()["t_ms"]
    assert before > 0
    win._sim.apply_preset("慢速大惯性热对象")
    _app().processEvents()
    after = engine.snapshot()["t_ms"]
    assert after < before, "切场景应当把对象/时间基准一起重置"
    assert engine.plant.params.tau1_s == pytest.approx(12.0)
    assert engine.plant.params.tau2_s == pytest.approx(45.0)


def test_disconnect_stops_sim_engine(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    win._connect_btn.click()
    _pump(0.3)
    assert win._sim_engine is not None
    win._connect_btn.click()
    _app().processEvents()
    assert win._sim_engine is None
    assert not win._worker.connected
    # 界面状态要跟着收口：按钮回到"连接"、状态不再是"已连接"
    assert win._connect_btn.text() == "连接"
    assert "未连接" in win._state_label.text()


def test_switching_channel_while_connected_disconnects(win):
    import serialpid.gui.main_window as mw

    win._transport.setCurrentText(mw.CHANNEL_SIM)
    win._connect_btn.click()
    _pump(0.3)
    win._transport.setCurrentText(mw.CHANNEL_TCP)
    _app().processEvents()
    assert win._sim_engine is None
    assert not win._worker.connected
    assert win._connect_btn.text() == "连接"
    assert "未连接" in win._state_label.text()


def test_sim_panel_collapsed_by_default(win):
    assert win._sim.is_expanded() is False


def test_expanding_sim_panel_scrolls_left_column_to_it(win):
    """面板在参数区最下方：展开后左栏要滚到底，否则用户以为点了没反应。"""
    import serialpid.gui.main_window as mw

    seen = []
    win._sim.expandedChanged.connect(seen.append)
    win._transport.setCurrentText(mw.CHANNEL_SIM)
    _app().processEvents()

    bar = win._left_scroll.verticalScrollBar()
    assert bar.maximum() > 0, "内容应当长到需要滚动"
    win._sim.set_expanded(True)
    _app().processEvents()
    assert seen == [True]
    assert bar.value() == bar.maximum()


# ---------------------------------------------------------------- 自检横幅


def _banner():
    from serialpid.gui.main_window import _CheckBanner

    banner = _CheckBanner()
    banner.show()
    _app().processEvents()
    return banner


def _report(entries):
    from serialpid.selftest import CheckResult, SelfTestReport

    return SelfTestReport(
        checks=[CheckResult(*entry) for entry in entries],
        generated_at="2026-11-07 10:00:00",
    )


def test_banner_hidden_when_everything_passes():
    banner = _banner()
    banner.set_report(_report([("python_runtime", "Python 运行时", "pass", "3.13")]))
    _app().processEvents()
    assert banner.isHidden()
    assert banner.text() == ""


def test_banner_warn_is_yellow():
    from serialpid.gui.theme import COLORS

    banner = _banner()
    banner.set_report(_report([
        ("python_runtime", "Python 运行时", "pass", "3.13"),
        ("icon_asset", "图标资源", "warn", "找不到图标，用默认窗口图标"),
    ]))
    _app().processEvents()
    assert not banner.isHidden()
    style = banner._label.styleSheet()
    assert COLORS["warn"] in style
    assert "1 项警告" in banner._label.text()
    assert banner.text()


def test_banner_fail_is_red_and_copyable():
    from serialpid.gui.theme import COLORS

    banner = _banner()
    banner.set_report(_report([
        ("config_dir", "配置目录", "fail", "目录不可写"),
        ("log_dir", "日志目录", "warn", "日志写不进"),
    ]))
    _app().processEvents()
    assert not banner.isHidden()
    style = banner._label.styleSheet()
    assert COLORS["err"] in style
    assert "1 项失败" in banner._label.text()
    assert banner._copy.isVisibleTo(banner)
    text = banner.text()
    assert "config_dir" in text and "log_dir" in text
    assert "目录不可写" in text


def test_plant_kwargs_mapping_matches_plant_fields():
    from serialpid.gui.main_window import _plant_kwargs
    from serialpid.sim.plant import PlantParams

    panel = {"gain": 2.0, "tau1": 1.2, "tau2": 0.7, "delay": 4,
             "noise": 0.3, "quant": 0.05, "period_ms": 50}
    kwargs = _plant_kwargs(panel)
    fields = set(PlantParams().__dataclass_fields__)
    assert set(kwargs) <= fields
    assert "period_ms" not in kwargs
    assert isinstance(kwargs["delay_steps"], int)
    params = PlantParams(**kwargs)
    assert params.gain == 2.0 and params.tau1_s == 1.2 and params.tau2_s == 0.7
    assert params.delay_steps == 4 and params.noise_std == 0.3 and params.quantum == 0.05
