"""Z-N 整定计算模块离线单测。

运行：在 pc/ 目录下  python -m pytest tests/test_zn_tuning.py -v
无 pytest 时：       python tests/test_zn_tuning.py
"""
from __future__ import annotations

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.zn_tuning import (  # noqa: E402
    estimate_reaction_curve,
    zn_closed_loop,
    zn_open_loop,
)


def test_closed_loop_known_values() -> None:
    res = zn_closed_loop(ku=2.0, tu=2.0)

    assert abs(res["P"].kp - 1.0) < 1e-9
    assert res["P"].ki == 0.0 and res["P"].kd == 0.0

    pi = res["PI"]
    assert abs(pi.kp - 0.9) < 1e-9
    assert abs(pi.ti - 2.0 / 1.2) < 1e-9
    assert abs(pi.ki - 0.9 / (2.0 / 1.2)) < 1e-9

    pid = res["PID"]
    assert abs(pid.kp - 1.2) < 1e-9
    assert abs(pid.ti - 1.0) < 1e-9
    assert abs(pid.td - 0.25) < 1e-9
    assert abs(pid.ki - 1.2) < 1e-9
    assert abs(pid.kd - 0.3) < 1e-9


def test_open_loop_known_values() -> None:
    # K=1, L=0.5 s, T=2 s
    res = zn_open_loop(k=1.0, l=0.5, t=2.0)

    assert abs(res["P"].kp - 4.0) < 1e-9

    pi = res["PI"]
    assert abs(pi.kp - 3.6) < 1e-9
    assert abs(pi.ti - 3.33 * 0.5) < 1e-9

    pid = res["PID"]
    assert abs(pid.kp - 4.8) < 1e-9
    assert abs(pid.ti - 1.0) < 1e-9
    assert abs(pid.td - 0.25) < 1e-9
    assert abs(pid.ki - 4.8) < 1e-9
    assert abs(pid.kd - 1.2) < 1e-9


def test_invalid_inputs_rejected() -> None:
    for fn in (lambda: zn_closed_loop(0.0, 2.0), lambda: zn_closed_loop(1.0, 0.0)):
        try:
            fn()
            raise AssertionError("应抛出 ValueError")
        except ValueError:
            pass
    for fn in (
        lambda: zn_open_loop(0.0, 0.5, 2.0),
        lambda: zn_open_loop(1.0, 0.0, 2.0),
        lambda: zn_open_loop(1.0, 0.5, 0.0),
    ):
        try:
            fn()
            raise AssertionError("应抛出 ValueError")
        except ValueError:
            pass


def test_reaction_curve_identification() -> None:
    # 一阶惯性 + 纯滞后：K=1, L=0.2 s, T=0.8 s
    times, sp, pv = [], [], []
    step_t = 500.0
    delay = 200.0
    tau = 800.0
    for i in range(101):
        t = i * 50.0
        times.append(t)
        sp.append(0.0 if t < step_t else 1.0)
        if t < step_t + delay:
            pv.append(0.0)
        else:
            pv.append(1.0 - math.exp(-(t - step_t - delay) / tau))

    rc = estimate_reaction_curve(times, sp, pv)
    assert abs(rc.k - 1.0) < 0.15
    assert abs(rc.l - 0.2) < 0.10
    assert abs(rc.t - 0.8) < 0.25


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
