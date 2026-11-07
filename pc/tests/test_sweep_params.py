"""卡27-D（续）：批量扫参脚本的网格解析、指标算法与产物。

验收口径：
* 给定 Kp/Ki 网格能跑完，输出 CSV 每个组合一行；
* 汇总表给每行 Ki 标出"开始振荡的临界 Kp"，没有就明写未振荡；
* 指标算法本身正确（超调 / 调节时间 / 稳态误差 / 过零），扫参循环里不做
  字符串拼接（耗时只算仿真，不算脚本开销）。
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import pytest  # noqa: E402

import sweep_params  # noqa: E402
from serialpid.sim.presets import PRESETS  # noqa: E402


def test_parse_grid_linspace_inclusive():
    grid = sweep_params.parse_grid("1:100:20", "kp")
    assert len(grid) == 20
    assert grid[0] == pytest.approx(1.0)
    assert grid[-1] == pytest.approx(100.0)
    assert grid == sorted(grid)


def test_parse_grid_explicit_list_and_single_point():
    assert sweep_params.parse_grid("0.5, 2, 3.5", "ki") == [0.5, 2.0, 3.5]
    assert sweep_params.parse_grid("5:5:1", "kp") == [5.0]
    with pytest.raises(Exception):
        sweep_params.parse_grid("1:2", "kp")
    with pytest.raises(Exception):
        sweep_params.parse_grid("1:2:0", "kp")


def test_metrics_on_ideal_step():
    samples = [20.0] * 10 + [25.0] * 30
    result = sweep_params.metrics(samples, sp=25.0, ambient=20.0, period_s=0.05)
    assert result["overshoot_pct"] == 0.0
    assert result["settling_s"] == pytest.approx(10 * 0.05)
    assert result["steady_error"] == pytest.approx(0.0)
    assert result["zero_crossings"] == 0
    assert result["oscillating"] == 0


def test_metrics_detect_overshoot():
    samples = [20.0] * 10 + [30.0] + [25.0] * 40
    result = sweep_params.metrics(samples, sp=25.0, ambient=20.0, period_s=0.05)
    assert result["overshoot_pct"] == pytest.approx(100.0)
    assert result["settling_s"] is not None


def test_metrics_detect_oscillation_and_steady_error():
    samples = [20.0] * 5 + [25.0, 27.0, 23.0, 27.0, 23.0, 27.0, 23.0] * 10
    result = sweep_params.metrics(samples, sp=25.0, ambient=20.0, period_s=0.05)
    assert result["zero_crossings"] >= 2
    assert result["oscillating"] == 1
    assert abs(result["steady_error"]) < 1.0


def test_sweep_runs_full_grid_and_writes_csv(tmp_path):
    rows = sweep_params.sweep(
        kp_grid=[1.0, 5.0], ki_grid=[0.0, 1.0], kd=0.1,
        plant_params=__import__("serialpid.sim.plant", fromlist=["PlantParams"]).PlantParams(),
        sp=25.0, ticks=80, period_s=0.05,
    )
    assert len(rows) == 4
    assert {row["kp"] for row in rows} == {1.0, 5.0}
    out = tmp_path / "sweep.csv"
    summary = sweep_params.write_outputs(rows, str(out), [1.0, 5.0], [0.0, 1.0],
                                         {"预设": "测试"})
    assert out.exists() and summary.endswith("_summary.md")
    with open(out, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["kp", "ki", "kd", "overshoot_pct", "settling_s",
                                    "steady_error", "peak", "zero_crossings", "ripple",
                                    "oscillating"]
        assert len(list(reader)) == 4


def test_summary_marks_critical_gain(tmp_path):
    rows = [
        {"kp": 1.0, "ki": 0.0, "kd": 0.1, "overshoot_pct": 5.0, "settling_s": 1.0,
         "steady_error": 0.1, "oscillating": 0},
        {"kp": 10.0, "ki": 0.0, "kd": 0.1, "overshoot_pct": 120.0, "settling_s": "",
         "steady_error": 0.9, "oscillating": 1},
        {"kp": 1.0, "ki": 0.5, "kd": 0.1, "overshoot_pct": 8.0, "settling_s": 1.2,
         "steady_error": 0.2, "oscillating": 0},
        {"kp": 10.0, "ki": 0.5, "kd": 0.1, "overshoot_pct": 30.0, "settling_s": 2.0,
         "steady_error": 0.3, "oscillating": 0},
    ]
    path = sweep_params.write_outputs(rows, str(tmp_path / "s.csv"), [1.0, 10.0], [0.0, 0.5],
                                      {"预设": "测试"})
    text = open(path, encoding="utf-8").read()
    assert "临界 Kp" in text
    assert "| 0 | 10 |" in text, "Ki=0 应标出临界 Kp=10"
    assert "未振荡（网格上限内）" in text, "Ki=0.5 未振荡应明写"


def test_main_end_to_end(tmp_path):
    out = tmp_path / "result.csv"
    assert sweep_params.main([
        "--kp", "2:40:3", "--ki", "0:0.6:2", "--kd", "0.2",
        "--preset", "大延迟对象", "--ticks", "120", "--out", str(out),
    ]) == 0
    assert out.exists()
    summary = tmp_path / "result_summary.md"
    assert summary.exists() and "临界 Kp" in summary.read_text(encoding="utf-8")
    summary_text = summary.read_text(encoding="utf-8")
    assert "大延迟对象" in summary_text, "汇总表应记下用的是哪个预设"


def test_preset_names_available_for_script():
    assert "大延迟对象" in PRESETS
