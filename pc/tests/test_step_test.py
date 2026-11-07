"""阶跃分析模块离线单测。

运行：在 pc/ 目录下  python -m pytest tests/test_step_test.py -v
无 pytest 时：       python tests/test_step_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.step_test import (  # noqa: E402
    StepState,
    StepTester,
    analyze_step,
)


def _rise_curve() -> tuple[list[float], list[float], list[float]]:
    """0→10 阶跃，峰值 12（20% 超调），最终收敛到 10。"""
    times = [i * 10.0 for i in range(20)]
    sp = [0.0] * 10 + [10.0] * 10
    pv = [0.0] * 10 + [1.0, 3.0, 6.0, 9.0, 12.0, 11.0, 10.3, 10.0, 10.0, 10.0]
    return times, sp, pv


def test_overshoot_and_settling() -> None:
    times, sp, pv = _rise_curve()
    m = analyze_step(times, sp, pv, band=0.02)
    assert m.direction == "rising"
    assert abs(m.overshoot_pct - 20.0) < 1e-6
    assert m.settled is True
    assert m.settling_time_ms is not None
    assert abs(m.steady_state_error) < 1e-6
    assert m.peak_value == 12.0


def test_no_overshoot_monotonic() -> None:
    times = [i * 10.0 for i in range(16)]
    sp = [0.0] * 6 + [10.0] * 10
    pv = [0.0] * 6 + [2.0, 5.0, 8.0, 9.5, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    m = analyze_step(times, sp, pv, band=0.05)
    assert m.overshoot_pct == 0.0
    assert m.settled is True
    assert m.rise_time_ms is not None


def test_never_settles() -> None:
    times = [i * 10.0 for i in range(20)]
    sp = [0.0] * 5 + [10.0] * 15
    pv = [0.0] * 5 + [8.0, 12.0] * 7 + [10.0]
    pv = pv[:20]
    m = analyze_step(times, sp, pv, band=0.02)
    assert m.settled is False
    assert m.settling_time_ms is None


def test_falling_step() -> None:
    times = [i * 10.0 for i in range(16)]
    sp = [10.0] * 6 + [0.0] * 10
    pv = [10.0] * 6 + [8.0, 5.0, 2.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    m = analyze_step(times, sp, pv, band=0.05)
    assert m.direction == "falling"
    assert abs(m.steady_state_error) < 1e-6
    assert m.settled is True


def test_tester_state_machine_jump_and_finish() -> None:
    calls: list[float] = []
    tester = StepTester(
        setpoint_cb=lambda sp: calls.append(sp),
        pre_settle_ms=100.0,
        stable_window_ms=50.0,
        timeout_ms=1000.0,
        band=0.05,
    )
    tester.begin(0.0, 10.0)

    for t in (0.0, 40.0, 80.0):
        tester.on_sample(t, 0.0, 0.0)
    assert tester.state == StepState.ARMED

    # 超过静置时长 → 自动跳变并回调下发目标值
    tester.on_sample(120.0, 10.0, 0.5)
    assert calls == [10.0]
    assert tester.state == StepState.RUNNING

    for t, pv in (
        (130.0, 6.0), (160.0, 9.8), (190.0, 10.0),
        (220.0, 10.0), (250.0, 10.0),
    ):
        tester.on_sample(t, 10.0, pv)

    assert tester.state == StepState.DONE
    assert tester.metrics is not None
    assert tester.metrics.settled is True


def test_tester_export_csv() -> None:
    tester = StepTester(setpoint_cb=lambda sp: None, pre_settle_ms=0.0)
    tester.begin(0.0, 5.0)
    tester.on_sample(0.0, 0.0, 0.0)
    tester.on_sample(10.0, 5.0, 1.0)
    tester.stop()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "step.csv")
        n = tester.export_csv(path)
        assert n == 2
        with open(path, encoding="utf-8-sig") as f:
            head = f.readline().strip()
        assert head == "t_ms,sp,pv,out,err"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
