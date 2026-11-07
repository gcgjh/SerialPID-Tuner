"""卡27-D：8 个场景预设（含"对应什么真实对象"）与响应形态差异。

验收口径：
* 预设从 5 个补到 8 个（大增益易振荡 / 执行器饱和 / 粗糙量化 / 纯延迟主导都在）；
* 每个预设覆盖全部 7 个参数、值在面板范围内、带一句真实对象说明；
* 切换后响应形态确有差异（不是换个名字同一根曲线）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.protocol import PacketType, pack_pid_set  # noqa: E402
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.params import PARAM_ORDER, PARAM_SPECS, plant_kwargs  # noqa: E402
from serialpid.sim.plant import Plant, PlantParams  # noqa: E402
from serialpid.sim.presets import DESCRIPTIONS, PRESETS, describe, get, match, names  # noqa: E402


def _run(name: str, ticks: int = 400, kp: float = 2.0, ki: float = 0.5, kd: float = 0.1):
    """按预设跑一次闭环阶跃，返回 (pv 序列, out 序列)。"""
    preset = PRESETS[name]
    plant = Plant(PlantParams(**plant_kwargs(preset)))
    engine = SimEngine(transport=None, plant=plant, period_s=preset["period_ms"] / 1000.0,
                       manual=True)
    engine.start()
    engine.handle_frame(PacketType.PID_SET, pack_pid_set(kp, ki, kd, 25.0))
    pv, out = [], []
    for _ in range(ticks):
        engine.step(1)
        pv.append(engine.pv)
        out.append(engine.out)
    engine.stop()
    return pv, out


def test_eight_presets_cover_all_params_and_have_descriptions():
    assert len(PRESETS) == 8, f"应为 8 个预设，实际 {len(PRESETS)}：{names()}"
    for name, preset in PRESETS.items():
        assert set(preset) == set(PARAM_ORDER), f"{name} 缺参数：{set(PARAM_ORDER) - set(preset)}"
        for key, value in preset.items():
            spec = PARAM_SPECS[key]
            assert float(spec["low"]) <= value <= float(spec["high"]), f"{name}.{key}={value} 越界"
        assert describe(name), f"{name} 缺'对应什么真实对象'说明"
        assert name in DESCRIPTIONS


def test_preset_helpers():
    assert set(names()) == set(PRESETS)
    assert get("快速小惯性") == PRESETS["快速小惯性"]
    assert get("不存在") is None
    assert match(PRESETS["纯延迟主导"]) == "纯延迟主导"
    assert match({**PRESETS["纯延迟主导"], "delay": 3}) is None
    assert describe("不存在") == ""


def test_fast_and_slow_objects_react_differently():
    fast, _ = _run("快速小惯性", ticks=60)
    slow, _ = _run("慢速大惯性热对象", ticks=60)
    assert abs(fast[-1] - slow[-1]) > 2.0, (
        f"快/慢对象 60 拍后 PV 几乎一样：fast={fast[-1]:.2f} slow={slow[-1]:.2f}"
    )


def test_pure_delay_preset_keeps_pv_flat_until_dead_time():
    pv, _ = _run("纯延迟主导", ticks=100)
    # delay=30 拍：前 29 拍执行量还没起作用，PV 应停在环境值 20
    assert all(abs(value - 20.0) < 1e-9 for value in pv[:29]), "纯延迟对象前 29 拍不该动"
    assert pv[40] > 20.5, "过了死区仍不响应，延迟没接上"


def test_delay_and_pure_delay_are_distinguishable():
    delayed, _ = _run("大延迟对象", ticks=80)
    pure, _ = _run("纯延迟主导", ticks=80)
    gap = max(abs(a - b) for a, b in zip(delayed, pure))
    assert gap > 1.0, (
        f"大延迟（10 拍 + 惯性）与纯延迟（30 拍）曲线应明显不同，最大差只有 {gap:.2f}"
    )


def test_noisy_sensor_has_ripple_but_clean_preset_does_not():
    noisy, _ = _run("带噪声传感器", ticks=300)
    clean, _ = _run("快速小惯性", ticks=300)
    tail_noisy = noisy[-100:]
    tail_clean = clean[-100:]
    spread_noisy = max(tail_noisy) - min(tail_noisy)
    spread_clean = max(tail_clean) - min(tail_clean)
    assert spread_noisy > 2.0, f"带噪声预设的末段波动只有 {spread_noisy:.2f}"
    assert spread_noisy > spread_clean * 2, "噪声预设与干净预设曲线应能区分"


def test_coarse_quantum_shows_steps():
    pv, _ = _run("粗糙量化", ticks=300)
    quantum = PRESETS["粗糙量化"]["quant"]
    near = [round(value / quantum) * quantum for value in pv]
    assert all(abs(value - snapped) <= quantum for value, snapped in zip(pv, near))
    unique = len({round(value, 6) for value in pv})
    assert unique < 60, f"量化 0.5 的曲线不该有 {unique} 个不同取值"


def test_actuator_saturation_hits_limit():
    _, out = _run("执行器饱和", ticks=200)
    assert min(out) <= 0.0 + 1e-9 or max(out) >= 100.0, (
        f"执行器饱和预设应把输出顶到限幅，实际范围 {min(out):.2f}..{max(out):.2f}"
    )


def test_high_gain_preset_oscillates():
    pv, _ = _run("大增益易振荡", ticks=400, kp=6.0, ki=0.8)
    overshoot = max(pv) - 25.0
    crossings = sum(
        1 for a, b in zip(pv, pv[1:]) if (a - 25.0) * (b - 25.0) < 0
    )
    assert overshoot > 2.0 or crossings >= 2, (
        f"大增益预设既没超调（{overshoot:.2f}）也没过零（{crossings} 次）"
    )


def test_all_eight_shapes_are_distinct():
    """八个预设的末段响应签名两两不同（防止只改名字不改参数）。"""
    signatures = {}
    for name in names():
        pv, out = _run(name, ticks=120)
        signatures[name] = (
            round(pv[30], 2), round(pv[60], 2), round(pv[-1], 2), round(max(out), 1)
        )
    assert len(set(signatures.values())) == 8, f"有预设响应重复：{signatures}"
