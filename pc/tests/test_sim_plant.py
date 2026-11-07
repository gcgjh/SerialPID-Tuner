"""卡24：仿真对象模型（二阶惯性 / 纯延迟 / 执行器饱和 / 测量噪声与量化 / 闭环接输出）。

每条用例对应卡片验收的一条；阈值不是拍脑袋写的，来自同参数下的实测扫描：

    Kp 扫描（SP 20→25，tau=2.0/0.5，dt=50ms，12s，PI 的 Ki=0.02）
        Kp=0.10 → 峰值 21.234（无超调，12s 内都没爬到目标）
        Kp=2.00 → 峰值 23.488
        Kp=8.00 → 峰值 25.579
        Kp=16.0 → 峰值 27.302（超调 2.302）

    延迟扫描（Kp=8，其余同上）
        delay=0  → 末尾峰峰值 0.002（稳态）
        delay=16 → 末尾峰峰值 4.556（持续振荡）

    旧实现在同样"Kp 差一万倍"的对照里 PV 最大只差 0.09，这里要求差 > 3，
    才能证明对象确实跟着输出走。
"""

from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.sim.plant import (  # noqa: E402
    MAX_DELAY_STEPS,
    OUT_MAX,
    OUT_MIN,
    Plant,
    PlantParams,
)

DT = 0.05


def _closed_loop(
    kp: float,
    ki: float = 0.02,
    delay: int = 0,
    steps: int = 240,
    sp: float = 25.0,
    params: PlantParams | None = None,
    dt: float = DT,
) -> list[float]:
    """最小 PI 控制器 + 仿真对象跑闭环，返回每拍测量值。

    这里刻意不用产品里的 PID 实现——它只是验证"对象吃输出、闭环会随参数变"的
    测试夹具，不是被测对象。
    """
    base = params or PlantParams(gain=1.0, tau1_s=2.0, tau2_s=0.5, ambient=20.0)
    base.delay_steps = delay
    plant = Plant(base, dt_s=dt)
    integral = 0.0
    pvs: list[float] = []
    for _ in range(steps):
        err = sp - plant.pv
        integral = max(-50.0, min(50.0, integral + err * dt))
        pvs.append(plant.step(kp * err + ki * integral))
    return pvs


def _tail_peak_to_peak(pvs: list[float], n: int = 80) -> float:
    tail = pvs[-n:]
    return max(tail) - min(tail)


# --------------------------------------------------------------------------- #
# 一阶 / 二阶
# --------------------------------------------------------------------------- #


def test_first_order_matches_time_constant():
    """一级惯性：2 秒（1 个 tau）应到约 63%，稳态偏移 = gain * u。"""
    plant = Plant(PlantParams(gain=2.0, tau1_s=2.0, tau2_s=0.0, ambient=20.0))
    for _ in range(40):        # 40 × 50 ms = 2.0 s
        plant.step(50.0)
    # 连续解 20 + 100*(1-e^-1)=83.21；后向欧拉 83.67，取 82~85 区间
    assert 82.0 < plant.pv < 85.0, plant.pv

    for _ in range(1200):      # 再跑 60 s 到稳态
        plant.step(50.0)
    assert abs(plant.pv - 120.0) < 0.5, plant.pv


def test_second_order_is_slower_than_first_order():
    """两级一阶串联：同样阶跃下 90% 上升时间明显更长。"""
    def t90(tau2: float) -> float:
        plant = Plant(PlantParams(gain=2.0, tau1_s=2.0, tau2_s=tau2, ambient=20.0))
        for i in range(400):
            pv = plant.step(50.0)
            if pv >= 20.0 + 0.9 * 100.0:
                return i * DT
        raise AssertionError("未上升到 90%")

    first, second = t90(0.0), t90(0.5)
    assert second > first + 0.3, (first, second)   # 实测 4.50 s vs 5.05 s


def test_gain_scales_steady_state():
    plant = Plant(PlantParams(gain=3.0, tau1_s=0.0, tau2_s=0.0, ambient=20.0))
    plant.step(40.0)
    assert abs(plant.pv - (20.0 + 3.0 * 40.0)) < 1e-6, plant.pv


# --------------------------------------------------------------------------- #
# 延迟
# --------------------------------------------------------------------------- #


def test_delay_holds_plant_for_n_steps():
    """delay_steps=4：前 4 拍对象完全不动，第 5 拍才吃到第一个执行量。"""
    plant = Plant(PlantParams(gain=1.0, tau1_s=0.2, tau2_s=0.0, delay_steps=4))
    pvs = [plant.step(50.0) for _ in range(10)]
    assert pvs[:4] == [20.0, 20.0, 20.0, 20.0], pvs[:4]
    assert pvs[4] > 20.0, pvs[4]


def test_delay_buffer_shrinks_immediately():
    """延迟改小时缓冲立刻裁剪，旧数据不许堵在队列里。"""
    plant = Plant(PlantParams(gain=1.0, tau1_s=0.5, delay_steps=16))
    for _ in range(20):
        plant.step(50.0)
    assert plant.delay_buffer_len == 17
    plant.set_params(delay_steps=2)
    assert plant.delay_buffer_len <= 3, plant.delay_buffer_len


# --------------------------------------------------------------------------- #
# 饱和
# --------------------------------------------------------------------------- #


def test_actuator_saturation_clamps_and_reports():
    plant = Plant(PlantParams(gain=1.0, tau1_s=0.0, tau2_s=0.0, ambient=20.0))
    plant.step(150.0)                       # 超上限
    assert plant.last_out == OUT_MAX, plant.last_out
    assert abs(plant.pv - (20.0 + OUT_MAX)) < 1e-6

    plant.step(-30.0)                       # 低于下限
    assert plant.last_out == OUT_MIN, plant.last_out
    assert abs(plant.pv - (20.0 + OUT_MIN)) < 1e-6


def test_non_finite_output_keeps_last_actuator():
    """控制器给 NaN 时不许污染对象：执行量保持上一拍，对象照常接着推。"""
    plant = Plant(PlantParams(gain=1.0, tau1_s=1.0, tau2_s=0.0))
    ref = Plant(PlantParams(gain=1.0, tau1_s=1.0, tau2_s=0.0))
    plant.step(30.0)
    ref.step(30.0)

    plant.step(float("nan"))
    ref.step(30.0)                      # 参照：继续用上一拍执行量
    assert plant.last_out == 30.0, plant.last_out
    assert math.isfinite(plant.pv)
    assert plant.state()["x1"] == pytest.approx(ref.state()["x1"])
    assert any("非有限" in w for w in plant.warnings)


# --------------------------------------------------------------------------- #
# 噪声 / 量化
# --------------------------------------------------------------------------- #


def test_noise_only_touches_measurement():
    """噪声只进测量值：关噪声与开噪声的两个对象，内部状态逐位相同。"""
    clean = Plant(PlantParams(gain=2.0, tau1_s=2.0, tau2_s=0.5, noise_std=0.0))
    noisy = Plant(PlantParams(gain=2.0, tau1_s=2.0, tau2_s=0.5, noise_std=0.5, seed=7))
    for _ in range(30):
        clean.step(50.0)
        noisy.step(50.0)
    assert clean.state()["x2"] == noisy.state()["x2"]          # 内部零污染
    assert abs(clean.pv - noisy.pv) > 0.01                    # 测量确有毛刺


def test_quantum_quantizes_measurement():
    plant = Plant(PlantParams(gain=1.0, tau1_s=0.5, tau2_s=0.0, quantum=0.5))
    vals = [plant.step(3.7) for _ in range(8)]
    for v in vals:
        assert abs(round(v / 0.5) * 0.5 - v) < 1e-9, v
    assert len(set(vals)) < len(vals)      # 出现阶梯而不是每拍都变


def test_same_seed_reproducible():
    plant = Plant(PlantParams(gain=1.0, tau1_s=2.0, tau2_s=0.5, noise_std=0.3, seed=7))
    plant.reset()
    r1 = [plant.step(50.0) for _ in range(20)]
    plant.reset()
    r2 = [plant.step(50.0) for _ in range(20)]
    assert r1 == r2, "同一种子重跑结果必须逐位一致"


# --------------------------------------------------------------------------- #
# 闭环：输出真的接进了对象
# --------------------------------------------------------------------------- #


def test_overshoot_grows_with_kp():
    """Kp 从 0.1 涨到 16：从"12 秒都没爬到目标"变成"明显超调"。"""
    peaks = {}
    for kp in (0.1, 2.0, 8.0, 16.0):
        peaks[kp] = max(_closed_loop(kp))
    ordered = [peaks[k] for k in (0.1, 2.0, 8.0, 16.0)]
    assert ordered == sorted(ordered), ordered            # 单调
    assert peaks[0.1] < 23.0, peaks[0.1]                  # 实测 21.234，无超调
    assert peaks[16.0] > 26.0, peaks[16.0]                # 实测 27.302，有超调


def test_output_drives_plant_not_setpoint():
    """旧实现 Kp 差一万倍 PV 只差 0.09；这里必须差出量级。"""
    slow = _closed_loop(0.1)
    fast = _closed_loop(16.0)
    gap = max(abs(a - b) for a, b in zip(slow, fast))
    assert gap > 3.0, gap        # 实测峰值差 6.07


def test_delay_turns_system_into_oscillation():
    """同一 Kp 下加延迟：从稳态直接进持续振荡。"""
    steady = _tail_peak_to_peak(_closed_loop(8.0, delay=0))
    ringing = _tail_peak_to_peak(_closed_loop(8.0, delay=16))
    assert steady < 0.1, steady          # 实测 0.002
    assert ringing > 3.0, ringing        # 实测 4.556


# --------------------------------------------------------------------------- #
# 参数运行期修改 / 极限参数
# --------------------------------------------------------------------------- #


def test_params_change_takes_effect_live():
    """时间常数改小后，同样输入的一拍推进量明显变大。"""
    plant = Plant(PlantParams(gain=1.0, tau1_s=10.0, tau2_s=0.0))
    plant.step(50.0)
    x = plant.state()["x1"]
    plant.set_params(tau1_s=10.0)
    plant.step(50.0)
    slow = plant.state()["x1"] - x

    plant.set_params(tau1_s=0.2)
    x = plant.state()["x1"]
    plant.step(50.0)
    fast = plant.state()["x1"] - x
    assert fast > slow * 10, (slow, fast)


def test_extreme_params_do_not_diverge():
    """极端参数：时间常数归零、延迟超限、量程写反，都不能崩或发散。"""
    plant = Plant(PlantParams(
        gain=1.0, tau1_s=0.0, tau2_s=0.0, delay_steps=99999,
        out_min=60.0, out_max=10.0,
    ))
    assert plant.params.delay_steps == MAX_DELAY_STEPS
    assert (plant.params.out_min, plant.params.out_max) == (OUT_MIN, OUT_MAX)
    assert any("超出上限" in w for w in plant.warnings)
    assert any("量程无效" in w for w in plant.warnings)

    for _ in range(50):
        pv = plant.step(500.0)
        assert math.isfinite(pv)
        assert OUT_MIN <= plant.last_out <= OUT_MAX
        assert -1e6 < pv < 1e6, pv


def test_unknown_param_is_ignored_with_warning():
    """拼错的参数名只记警告：引擎/面板成批下发时不能把仿真线程带崩。"""
    plant = Plant(PlantParams(gain=2.0))
    warnings = plant.set_params(plant_tau=3.0, tau1_s=1.0)
    assert any("未知参数 plant_tau" in w for w in warnings), warnings
    assert plant.params.tau1_s == 1.0          # 合法的照常生效
    assert plant.params.gain == 2.0
