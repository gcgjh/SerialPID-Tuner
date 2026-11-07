"""批量扫参：给定 Kp/Ki 网格，跑闭环阶跃，算指标并标出振荡临界点。

回答的是最实际的一个问题：**"我这套参数在这个对象上稳不稳？"** 以前只能一个
参数一个参数手调、盯着曲线看，现在一次跑完 20×5 个组合，直接给出超调量、
调节时间、稳态误差，并标出"从哪个 Kp 开始振荡"。

用法::

    python tools/sweep_params.py                          # 默认 Kp 1..100 20 点、Ki 0..2 5 点
    python tools/sweep_params.py --kp 1:100:20 --ki 0:2:5 --preset 大延迟对象
    python tools/sweep_params.py --kp 1:200:30 --tau1 2 --tau2 5 --delay 10 --out big_delay.csv

产物：CSV（每个组合一行）+ 同名 ``*_summary.md`` 汇总表（每个 Ki 一行，含临界 Kp）。
脚本走的是 ``SimEngine`` 同一套 PID/对象/组帧逻辑，不另写一份仿真。
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from serialpid.protocol import PacketType, pack_pid_set  # noqa: E402
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.params import plant_kwargs  # noqa: E402
from serialpid.sim.plant import Plant, PlantParams  # noqa: E402
from serialpid.sim.presets import PRESETS, describe  # noqa: E402
from serialpid.transport import Transport  # noqa: E402


class _Sink(Transport):
    """丢弃输出的通道：扫参只关心状态，不需要真的把帧存起来。"""

    def __init__(self) -> None:
        self.emitted = 0

    def open(self, target: str = "sink", baud: int = 115200) -> None:
        return None

    def read(self, n: int = 1024) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        return None

    def close(self) -> None:
        return None

    def is_open(self) -> bool:
        return True

    def emit(self, data: bytes) -> bool:
        self.emitted += 1
        return True


def parse_grid(text: str, name: str) -> List[float]:
    """解析 ``start:stop:count``（含两端）；也接受逗号分隔的显式列表。"""
    text = text.strip()
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 3:
            raise argparse.ArgumentTypeError(f"{name} 网格格式应为 start:stop:count，得到 {text!r}")
        start, stop, count = float(parts[0]), float(parts[1]), int(parts[2])
        if count <= 0:
            raise argparse.ArgumentTypeError(f"{name} 点数必须为正")
        if count == 1:
            return [start]
        step = (stop - start) / (count - 1)
        return [round(start + step * i, 6) for i in range(count)]
    return [float(item) for item in text.split(",") if item.strip()]


def simulate(
    kp: float,
    ki: float,
    kd: float,
    sp: float = 25.0,
    ticks: int = 600,
    period_s: float = 0.05,
    plant_params: Optional[PlantParams] = None,
) -> List[float]:
    """跑一次闭环阶跃，返回逐拍 PV 序列（第 0 拍为对象初始值）。"""
    params = plant_params or PlantParams()
    plant = Plant(params, dt_s=period_s)
    engine = SimEngine(transport=_Sink(), plant=plant, period_s=period_s)
    engine.handle_frame(PacketType.PID_SET, pack_pid_set(kp, ki, kd, sp))
    samples: List[float] = [engine.pv]
    for _ in range(max(0, int(ticks))):
        engine.step(1, period_s)
        samples.append(engine.pv)
    return samples


def metrics(
    samples: Sequence[float],
    sp: float,
    ambient: float,
    period_s: float,
    band: float = 0.02,
) -> Dict[str, float]:
    """由 PV 曲线算超调 / 调节时间 / 稳态误差 / 过零次数。"""
    step = max(1e-9, abs(sp - ambient))
    peak = max(samples)
    overshoot = max(0.0, (peak - sp) / step * 100.0)

    tolerance = band * step
    settling_s: Optional[float] = None
    for index in range(len(samples)):
        if all(abs(value - sp) <= tolerance for value in samples[index:]):
            settling_s = index * period_s
            break

    tail = samples[max(1, int(len(samples) * 0.8)):]
    steady_error = statistics.fmean(tail) - sp if tail else 0.0

    crossings = 0
    last_sign = 0
    for value in samples:
        error = value - sp
        sign = 1 if error > tolerance else (-1 if error < -tolerance else 0)
        if sign == 0:
            continue
        if last_sign != 0 and sign != last_sign:
            crossings += 1
        last_sign = sign

    half = samples[len(samples) // 2:]
    ripple = (max(half) - min(half)) if half else 0.0
    oscillating = crossings >= 2 and ripple > 2 * tolerance

    return {
        "overshoot_pct": round(overshoot, 3),
        "settling_s": round(settling_s, 3) if settling_s is not None else "",
        "steady_error": round(steady_error, 4),
        "peak": round(peak, 3),
        "zero_crossings": crossings,
        "ripple": round(ripple, 4),
        "oscillating": int(bool(oscillating)),
    }


def sweep(
    kp_grid: Sequence[float],
    ki_grid: Sequence[float],
    kd: float,
    plant_params: PlantParams,
    sp: float,
    ticks: int,
    period_s: float,
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    ambient = float(plant_params.ambient)
    for kp in kp_grid:
        for ki in ki_grid:
            samples = simulate(kp, ki, kd, sp=sp, ticks=ticks, period_s=period_s,
                               plant_params=plant_params)
            row: Dict[str, object] = {"kp": kp, "ki": ki, "kd": kd}
            row.update(metrics(samples, sp, ambient, period_s))
            rows.append(row)
    return rows


def write_outputs(rows: List[Dict[str, object]], out_path: str, kp_grid: Sequence[float],
                  ki_grid: Sequence[float], meta: Dict[str, object]) -> str:
    """写 CSV 与汇总 Markdown；返回汇总文件路径。"""
    directory = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(directory, exist_ok=True)
    fieldnames = ["kp", "ki", "kd", "overshoot_pct", "settling_s", "steady_error",
                  "peak", "zero_crossings", "ripple", "oscillating"]
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})

    summary_path = os.path.splitext(out_path)[0] + "_summary.md"
    lines = ["# PID 扫参汇总", ""]
    for key, value in meta.items():
        lines.append(f"- {key}：{value}")
    lines += [
        "",
        "| Ki | 临界 Kp（开始振荡） | 最大超调 % | 最小调节时间 s | 末点稳态误差 |",
        "|---|---|---|---|---|",
    ]
    by_ki: Dict[float, List[Dict[str, object]]] = {}
    for row in rows:
        by_ki.setdefault(float(row["ki"]), []).append(row)
    for ki in ki_grid:
        group = by_ki.get(float(ki), [])
        if not group:
            continue
        critical = next(
            (float(r["kp"]) for r in sorted(group, key=lambda x: float(x["kp"]))
             if int(r["oscillating"])),
            None,
        )
        max_over = max(float(r["overshoot_pct"]) for r in group)
        settle_values = [float(r["settling_s"]) for r in group if r["settling_s"] != ""]
        min_settle = min(settle_values) if settle_values else None
        worst_error = max(abs(float(r["steady_error"])) for r in group)
        lines.append(
            f"| {ki:g} | {'未振荡（网格上限内）' if critical is None else f'{critical:g}'} "
            f"| {max_over:.1f} | {'—' if min_settle is None else f'{min_settle:.2f}'} "
            f"| {worst_error:.3f} |"
        )
    with open(summary_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return summary_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    from serialpid.sim.params import default_params

    parser = argparse.ArgumentParser(description="SerialPID-Tuner 批量扫参（闭环阶跃）")
    parser.add_argument("--kp", default="1:100:20", help="Kp 网格 start:stop:count 或逗号列表")
    parser.add_argument("--ki", default="0:2:5", help="Ki 网格 start:stop:count 或逗号列表")
    parser.add_argument("--kd", type=float, default=0.1)
    parser.add_argument("--sp", type=float, default=25.0)
    parser.add_argument("--ticks", type=int, default=600, help="每个组合跑多少拍（50ms/拍）")
    parser.add_argument("--preset", choices=sorted(PRESETS), help="对象场景预设")
    parser.add_argument("--gain", type=float, help="对象增益（覆盖预设）")
    parser.add_argument("--tau1", type=float)
    parser.add_argument("--tau2", type=float)
    parser.add_argument("--delay", type=int)
    parser.add_argument("--noise", type=float)
    parser.add_argument("--quant", type=float)
    parser.add_argument("--period-ms", type=int, default=50)
    parser.add_argument("--out", default="sweep_result.csv", help="CSV 输出路径")
    args = parser.parse_args(argv)

    base = dict(PRESETS[args.preset]) if args.preset else default_params()
    for key, value in (
        ("gain", args.gain), ("tau1", args.tau1), ("tau2", args.tau2),
        ("delay", args.delay), ("noise", args.noise), ("quant", args.quant),
    ):
        if value is not None:
            base[key] = value
    base["period_ms"] = args.period_ms

    plant_params = PlantParams(**plant_kwargs(base))
    kp_grid = parse_grid(args.kp, "kp")
    ki_grid = parse_grid(args.ki, "ki")
    period_s = float(args.period_ms) / 1000.0

    rows = sweep(kp_grid, ki_grid, args.kd, plant_params, args.sp, args.ticks, period_s)

    meta = {
        "预设": args.preset or "自定义（面板默认）",
        "预设说明": describe(args.preset) if args.preset else "—",
        "对象": (
            f"增益 {plant_params.gain:g} · τ1 {plant_params.tau1_s:g}s · "
            f"τ2 {plant_params.tau2_s:g}s · 延迟 {plant_params.delay_steps} 拍 · "
            f"噪声 σ {plant_params.noise_std:g} · 量化 {plant_params.quantum:g}"
        ),
        "Kp 网格": f"{len(kp_grid)} 点 {kp_grid[0]:g}..{kp_grid[-1]:g}",
        "Ki 网格": f"{len(ki_grid)} 点 {ki_grid[0]:g}..{ki_grid[-1]:g}",
        "Kd": f"{args.kd:g}",
        "每组合拍数": args.ticks,
        "控制周期": f"{args.period_ms} ms",
        "组合数": len(rows),
    }
    summary = write_outputs(rows, args.out, kp_grid, ki_grid, meta)
    print(f"CSV：{os.path.abspath(args.out)}")
    print(f"汇总：{os.path.abspath(summary)}")
    print()
    with open(summary, "r", encoding="utf-8") as handle:
        print(handle.read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
