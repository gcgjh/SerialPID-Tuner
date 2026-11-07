"""卡27-E：仿真参数持久化 + 引擎手动步进（不起线程）。

验收口径：
* 改完参数关掉再开还在（``sim_state.json``，与 ``config.json`` 同目录）；
* 文件损坏先备份再用默认、越界值夹回范围并给警告，绝不阻塞启动；
* ``manual=True`` 时不起线程，只有显式 ``step()`` 才推进，且同序列逐字节可复现。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from serialpid.protocol import PacketType, parse_frame, unpack_telemetry  # noqa: E402
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.params import default_params  # noqa: E402
from serialpid.sim.plant import Plant, PlantParams  # noqa: E402
from serialpid.sim.sim_config import (  # noqa: E402
    clear,
    default_state_path,
    load_params,
    save_params,
)


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "sim_state.json"
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    assert default_state_path() == str(path), "应与 config.json 同目录"
    return path


def test_save_load_roundtrip(state_file):
    params = default_params()
    params.update({"gain": 3.5, "tau1": 2.25, "tau2": 9.0, "delay": 12, "noise": 1.4,
                   "quant": 0.25, "period_ms": 40})
    save_params(params)
    loaded, warning = load_params()
    assert warning is None
    assert loaded == pytest.approx(params)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["schema"] == "serialpid.sim"
    assert payload["params"]["delay"] == 12


def test_missing_file_uses_defaults(state_file):
    assert not state_file.exists()
    loaded, warning = load_params()
    assert loaded == pytest.approx(default_params())
    assert warning is None


def test_out_of_range_is_clamped_with_warning(state_file):
    # 手改过文件（或旧版本写进来的越界值）：加载时必须夹回并给警告
    state_file.write_text(
        json.dumps({"schema": "serialpid.sim", "params": {"gain": 999, "tau1": -5, "delay": 999}}),
        encoding="utf-8",
    )
    loaded, warning = load_params()
    assert loaded["gain"] == 50.0
    assert loaded["tau1"] == 0.05
    assert loaded["delay"] == 40.0
    assert warning and "上限" in warning


def test_save_clamps_before_write(state_file):
    save_params({"gain": 999, "delay": 999})
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["params"]["gain"] == 50.0
    assert payload["params"]["delay"] == 40.0


def test_corrupt_file_is_backed_up_and_defaults_used(state_file):
    state_file.write_text("{ 这不是 json", encoding="utf-8")
    loaded, warning = load_params()
    assert loaded == pytest.approx(default_params())
    assert warning and "损坏" in warning
    backups = [name for name in os.listdir(state_file.parent) if ".corrupt-" in name]
    assert backups, "损坏文件应留一份备份而不是直接丢"


def test_clear_removes_state(state_file):
    save_params(default_params())
    assert clear() is True
    assert clear() is False


def test_panel_remembers_params_across_restart(state_file):
    from PySide6.QtWidgets import QApplication

    from serialpid.gui.sim_panel import SimPanel

    app = QApplication.instance() or QApplication([])
    assert app is not None

    first = SimPanel()
    assert first.params() == pytest.approx(default_params())
    first.set_params({"gain": 3.5, "tau1": 2.2, "delay": 7}, emit=True)
    assert first.save_now() is True
    assert state_file.exists()

    second = SimPanel()
    assert second.params()["gain"] == pytest.approx(3.5)
    assert second.params()["tau1"] == pytest.approx(2.2)
    assert second.params()["delay"] == pytest.approx(7)


def test_panel_shows_warning_when_state_corrupt(state_file):
    from PySide6.QtWidgets import QApplication

    from serialpid.gui.sim_panel import SimPanel

    app = QApplication.instance() or QApplication([])
    assert app is not None

    state_file.write_text("{} 坏文件", encoding="utf-8")
    panel = SimPanel()
    assert panel.load_warning, "损坏状态应留下可展示的警告"
    assert panel.params() == pytest.approx(default_params())


def test_manual_mode_does_not_run_thread():
    engine = SimEngine(manual=True)
    engine.start()
    try:
        assert engine.is_running()
        assert engine._thread is None, "手动模式不应起线程"
        pv_before = engine.pv
        time.sleep(0.15)
        assert engine.pv == pv_before, "手动模式在没人 step 时不该自己走"
    finally:
        engine.stop()
    assert not engine.is_running()


def test_manual_step_is_byte_reproducible():
    def run() -> bytes:
        engine = SimEngine(manual=True, plant=Plant(PlantParams()))
        engine.start()
        frames = engine.step(10)
        engine.stop()
        return b"".join(frames)

    first, second = run(), run()
    assert first == second
    assert len(first) == 10 * (10 + 20), "10 拍应出 10 个遥测帧"


def test_manual_step_advances_pid_and_plant():
    engine = SimEngine(manual=True, plant=Plant(PlantParams()))
    engine.start()
    engine.step(20)
    frames = engine.step(1, dt_s=0.05)
    assert len(frames) == 1
    telemetry = unpack_telemetry(parse_frame(frames[0]).payload)
    t_ms, sp, pv, out, err = telemetry
    assert pv > 20.0, f"SP {sp} / PV 20 起步，推了 20 拍 PV 应已上升，实际 {pv}"
    assert engine.snapshot()["manual"] is True
    engine.stop()


def test_manual_step_survives_zero_dt():
    engine = SimEngine(manual=True, plant=Plant(PlantParams()))
    engine.start()
    frames = engine.step(1, dt_s=0.0)
    assert frames == [], "dt=0 不该推进也不该发帧"
    engine.stop()
