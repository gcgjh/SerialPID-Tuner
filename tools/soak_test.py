"""card38 小时级长稳压测：最接近真实使用的挂机负载 + 固定间隔资源采样。

为什么另写一个脚本
    已有的 ``tools/pressure_test.py`` 是"数据量 / 重连次数"的加速压测（几分钟
    跑完等效工作量）。真实使用形态是**按时挂机**：调温控一挂一夜，期间反复
    调参、跑阶跃、偶尔掉线。本脚本用内置模拟器闭环 + 真实事件循环跑真实秒数，
    每 10 s 采一次进程与界面指标，边跑边写 CSV——崩了也留得下数据。

负载（全部走真实界面路径，不是直接调内部函数）
    * 内置模拟器 20 Hz 遥测（``_transport`` 选「内置模拟器」后点连接）
    * 每 ``--param-every`` 秒下发一次参数（PID_SET / SP_SET 交替，模拟调参节奏）
    * 每 ``--step-every`` 秒触发一次阶跃并自动停止
    * 每 ``--reconnect-every`` 秒断开再重连一次
    * 帧日志环形缓冲与曲线队列全程跑满（240 帧 ≈ 12 s / 20000 点 ≈ 17 min 后满载）

指标列（CSV）
    t_s / event / rss_mb / traced_mb / handles / threads / render_ms / rtt_ms /
    fps / rx_frames / tx_frames / bad_frames / trace_frames / plot_points /
    buffer_points / log_rows / connected / errors

用法（仓库根目录、项目 venv 解释器）
    python tools/soak_test.py --minutes 5                    # 冒烟：确认各项机制都触发
    python tools/soak_test.py --hours 4  --out docs/soak/soak-4h.csv
    python tools/soak_test.py --hours 8  --out docs/soak/soak-8h.csv --summary docs/soak/soak-8h.md
    python tools/soak_test.py --minutes 60 --baseline         # 空跑基线：量脚本自身开销
    python tools/soak_test.py --minutes 1 --show              # 真有窗口（默认离屏）

设计约束（对应任务卡第四节）
    * 采样 10 s 一次（``--sample-every``），高频采样本身就是扰动；
    * 采样点写进 ``--errors-log`` 之外一律不抛：任何回调异常都记数并落盘，绝不让
      异常把长跑打断（也绝不吞掉——报告里要能看到）；
    * 配置写到输出目录旁的临时文件，不污染用户 ``%APPDATA%`` 里的真实配置；
    * Windows 默认 ``SetThreadExecutionState`` 阻止睡眠，结束时恢复。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import statistics
import sys
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "pc"))

CB = 1024.0 * 1024.0

#: CSV 列顺序固定：报告脚本 / Excel 都按列名读，不依赖位置
COLUMNS = (
    "t_s",
    "event",
    "rss_mb",
    "traced_mb",
    "handles",
    "threads",
    "render_ms",
    "rtt_ms",
    "fps",
    "rx_frames",
    "tx_frames",
    "bad_frames",
    "trace_frames",
    "plot_points",
    "buffer_points",
    "log_rows",
    "connected",
    "errors",
)

#: 判稳阈值（报告里逐项写明，避免"看起来平稳"这种含糊结论）
RSS_SLOPE_WARN_MB_PER_HOUR = 5.0
RSS_TOTAL_WARN_MB = 20.0
HANDLE_DRIFT_WARN = 5
THREAD_DRIFT_WARN = 2
RENDER_DRIFT_WARN = 1.2   # 后半段均值 / 前半段均值
MIN_ROWS_FOR_VERDICT = 4
#: 短于这个时长只算"跑通校验"，不判稳定性：绘图队列要 ~17 min 才满载，
#: 前几分钟的 RSS 上涨主要是队列填充，拿来判泄漏必然误报。
MIN_DURATION_FOR_VERDICT_S = 600.0


# ---------------------------------------------------------------- CSV 记录


class Recorder:
    """边跑边写的 CSV 记录器：每行立即 flush，进程被杀也留得下已有数据。"""

    def __init__(self, path: str, columns: tuple = COLUMNS) -> None:
        self.path = path
        self.columns = tuple(columns)
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(path, "w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=list(self.columns), extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()
        self.rows: List[Dict[str, Any]] = []

    def write(self, row: Dict[str, Any]) -> None:
        self._writer.writerow({key: _cell(row.get(key)) for key in self.columns})
        self._fh.flush()
        self.rows.append(dict(row))

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:  # pragma: no cover - 关闭失败无处可报
            pass


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}"
    return value


# ---------------------------------------------------------------- 统计分析


def rss_slope_mb_per_hour(rows: List[Dict[str, Any]]) -> float:
    """最小二乘拟合 RSS 对时间的斜率（MB/小时）。

    跳过前两条样本：刚启动时解释器 / Qt / pyqtgraph 的一次性分配不应被算成
    "每小时增长"，否则 4 小时报告会把启动开销误判成泄漏。
    """
    usable = [
        (float(row["t_s"]), float(row["rss_mb"]))
        for row in rows
        if row.get("rss_mb") not in (None, "") and row.get("t_s") not in (None, "")
    ]
    if len(usable) < 3:
        return 0.0
    usable = usable[2:]
    n = len(usable)
    mean_x = sum(point[0] for point in usable) / n
    mean_y = sum(point[1] for point in usable) / n
    sxx = sum((point[0] - mean_x) ** 2 for point in usable)
    if sxx <= 0.0:
        return 0.0
    sxy = sum((point[0] - mean_x) * (point[1] - mean_y) for point in usable)
    return (sxy / sxx) * 3600.0


def _values(rows: List[Dict[str, Any]], key: str) -> List[float]:
    out: List[float] = []
    for row in rows:
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(rows: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把 CSV 行归纳成结论所需的数字与三态判定（平稳 / 需解释 / 数据不足）。"""
    meta = dict(meta or {})
    all_samples = [row for row in rows if row.get("rss_mb") not in (None, "")]
    # 预热窗口：Qt 线程池、pyqtgraph 字体/位图缓存、socket 池都在头几十秒里建立，
    # 算进判定会把"启动一次性分配"读成漂移/泄漏（实测基线里线程 38→27 即此）。
    warmup_s = float(meta.get("warmup_s") or 0.0)

    def _t_of(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("t_s"))
        except (TypeError, ValueError):
            return 0.0

    samples = [row for row in all_samples if _t_of(row) >= warmup_s]
    duration_h = 0.0
    if samples:
        try:
            duration_h = (_t_of(samples[-1]) - _t_of(samples[0])) / 3600.0
        except (KeyError, TypeError, ValueError):
            duration_h = 0.0

    rss = _values(samples, "rss_mb")
    traced = _values(samples, "traced_mb")
    handles = _values(samples, "handles")
    threads = _values(samples, "threads")
    render = _values(samples, "render_ms")
    rtt = _values(samples, "rtt_ms")

    slope = rss_slope_mb_per_hour(samples)
    duration_s = duration_h * 3600.0
    too_short = duration_s < MIN_DURATION_FOR_VERDICT_S
    result: Dict[str, Any] = {
        "samples": len(samples),
        "duration_h": duration_h,
        "short_run": too_short,
        "rss": {
            "start_mb": rss[0] if rss else None,
            "end_mb": rss[-1] if rss else None,
            "peak_mb": max(rss) if rss else None,
            "delta_mb": (rss[-1] - rss[0]) if rss else None,
            "slope_mb_per_hour": slope,
            "verdict": _memory_verdict(samples, rss, slope, too_short),
        },
        "traced_mb": {
            "start": traced[0] if traced else None,
            "end": traced[-1] if traced else None,
            "peak": max(traced) if traced else None,
        },
        "handles": {
            "start": int(handles[0]) if handles else None,
            "end": int(handles[-1]) if handles else None,
            "min": int(min(handles)) if handles else None,
            "max": int(max(handles)) if handles else None,
            "drift": (int(max(handles) - min(handles)) if handles else None),
            "verdict": _drift_verdict(handles, HANDLE_DRIFT_WARN, "句柄", too_short),
        },
        "threads": {
            "start": int(threads[0]) if threads else None,
            "end": int(threads[-1]) if threads else None,
            "min": int(min(threads)) if threads else None,
            "max": int(max(threads)) if threads else None,
            "drift": (int(max(threads) - min(threads)) if threads else None),
            "verdict": _drift_verdict(threads, THREAD_DRIFT_WARN, "线程", too_short),
        },
        "render_ms": _render_stats(render, too_short),
        "rtt_ms": {
            "avg": statistics.mean(rtt) if rtt else None,
            "p50": _quantile(rtt, 0.5) if rtt else None,
            "p95": _quantile(rtt, 0.95) if rtt else None,
            "max": max(rtt) if rtt else None,
        },
        "counters": {
            "rx_frames": _last(samples, "rx_frames"),
            "tx_frames": _last(samples, "tx_frames"),
            "bad_frames": _last(samples, "bad_frames"),
            "trace_frames": _last(samples, "trace_frames"),
            "plot_points": _last(samples, "plot_points"),
            "log_rows": _last(samples, "log_rows"),
            "errors": _last(samples, "errors"),
        },
        "events": {},
    }
    for row in rows:
        name = row.get("event")
        if name:
            result["events"][str(name)] = result["events"].get(str(name), 0) + 1
    result["meta"] = meta
    result["verdict"] = _overall_verdict(result)
    return result


def _last(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
    values = _values(rows, key)
    return values[-1] if values else None


def _memory_verdict(
    rows: List[Dict[str, Any]], rss: List[float], slope: float, too_short: bool = False
) -> str:
    if len(rss) < MIN_ROWS_FOR_VERDICT:
        return "采样不足，无法判定"
    if too_short:
        return "短跑校验：队列仍在填充，不做稳定性判定"
    delta = rss[-1] - rss[0]
    if abs(slope) <= RSS_SLOPE_WARN_MB_PER_HOUR and abs(delta) <= RSS_TOTAL_WARN_MB:
        return "平稳"
    if slope > RSS_SLOPE_WARN_MB_PER_HOUR:
        return "单调增长（需用 tracemalloc 分代对比确认是真泄漏还是分配器行为）"
    return "有波动但未单调增长（需结合事件时间点解释）"


def _drift_verdict(values: List[float], limit: float, name: str, too_short: bool = False) -> str:
    if len(values) < MIN_ROWS_FOR_VERDICT:
        return "采样不足，无法判定"
    if too_short:
        return "短跑校验：不做稳定性判定"
    drift = max(values) - min(values)
    if drift <= limit:
        return "平稳"
    return f"{name}漂移 {int(drift)}（超过阈值 {int(limit)}，需按重连/线程事件归因）"


def _render_stats(values: List[float], too_short: bool = False) -> Dict[str, Any]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None, "verdict": "无数据"}
    half = max(1, len(values) // 2)
    first = statistics.mean(values[:half])
    last = statistics.mean(values[-half:])
    ratio = (last / first) if first > 0 else 1.0
    if too_short:
        verdict = f"短跑校验：后半 / 前半 = {ratio:.2f}（不做稳定性判定）"
    elif first <= 0:
        verdict = "前半段无有效采样"
    elif ratio <= RENDER_DRIFT_WARN:
        verdict = f"未爬升（后半 / 前半 = {ratio:.2f}）"
    else:
        verdict = f"爬升 {ratio:.2f} 倍（后半 {last:.2f}ms / 前半 {first:.2f}ms）"
    return {
        "count": len(values),
        "p50": _quantile(values, 0.50),
        "p95": _quantile(values, 0.95),
        "max": max(values),
        "first_half_avg": first,
        "last_half_avg": last,
        "ratio": ratio,
        "verdict": verdict,
    }


def _overall_verdict(result: Dict[str, Any]) -> str:
    if result["samples"] < MIN_ROWS_FOR_VERDICT:
        return "采样不足，无法判定"
    if result.get("short_run"):
        return "短跑校验：脚本与各负载机制均跑通，不做稳定性判定"
    bad = []
    if "平稳" not in result["rss"]["verdict"] and "波动" not in result["rss"]["verdict"]:
        bad.append("内存")
    if "平稳" not in result["handles"]["verdict"]:
        bad.append("句柄")
    if "平稳" not in result["threads"]["verdict"]:
        bad.append("线程")
    if "未爬升" not in result["render_ms"]["verdict"]:
        bad.append("渲染耗时")
    if (result["counters"]["errors"] or 0) > 0:
        bad.append("异常")
    if not bad:
        return "全部指标平稳"
    return "需要解释：" + " / ".join(bad)


def format_summary_md(meta: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """把结论写成一份可直接归档的报告片段（docs/soak/ 下）。"""
    rss = summary["rss"]
    handles = summary["handles"]
    threads = summary["threads"]
    render = summary["render_ms"]
    rtt = summary["rtt_ms"]
    counters = summary["counters"]

    def fmt(value: Any, unit: str = "", digits: int = 1) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.{digits}f}{unit}"
        return f"{value}{unit}"

    lines = [
        f"# 长稳压测报告 · {meta.get('mode', 'soak')}",
        "",
        f"- 开始：{meta.get('started_at', '—')}",
        f"- 时长：{summary['duration_h']:.2f} h（{summary['samples']} 个采样点，"
        f"每 {meta.get('sample_every', '—')} s 一次；判定窗口跳过前 "
        f"{meta.get('warmup_s', 0):g} s 启动预热）",
        f"- 负载：{meta.get('load', '—')}",
        f"- 环境：{meta.get('platform', '—')} · Python {meta.get('python', '—')} · "
        f"Qt {meta.get('qt', '—')}",
        f"- CSV：`{meta.get('csv', '—')}`",
        "",
        "## 结论",
        "",
        f"**{summary['verdict']}**",
        "",
        "| 指标 | 起始 | 结束 | 峰值 / 最大 | 判定 |",
        "| --- | --- | --- | --- | --- |",
        f"| 工作集内存 | {fmt(rss['start_mb'])} MB | {fmt(rss['end_mb'])} MB | "
        f"{fmt(rss['peak_mb'])} MB | {rss['verdict']}（斜率 {fmt(rss['slope_mb_per_hour'])} MB/h） |",
        f"| 句柄 | {fmt(handles['start'])} | {fmt(handles['end'])} | {fmt(handles['max'])} | {handles['verdict']} |",
        f"| 线程 | {fmt(threads['start'])} | {fmt(threads['end'])} | {fmt(threads['max'])} | {threads['verdict']} |",
        f"| 单帧渲染 | {fmt(render.get('first_half_avg'), ' ms', 2)}（前半） | "
        f"{fmt(render.get('last_half_avg'), ' ms', 2)}（后半） | {fmt(render.get('max'), ' ms', 2)} | "
        f"{render['verdict']} |",
        f"| 心跳 RTT | {fmt(rtt['p50'], ' ms')}（P50） | {fmt(rtt['p95'], ' ms')}（P95） | "
        f"{fmt(rtt['max'], ' ms')} | — |",
        "",
        "## 计数",
        "",
        f"- 收帧 {fmt(counters['rx_frames'])} / 发帧 {fmt(counters['tx_frames'])} / "
        f"坏帧 {fmt(counters['bad_frames'])}",
        f"- 帧日志 {fmt(counters['trace_frames'])} 帧 · 曲线 {fmt(counters['plot_points'])} 点 · "
        f"日志 {fmt(counters['log_rows'])} 行",
        f"- 脚本内捕获异常 {fmt(counters['errors'])} 次"
        + (f"（详见 `{meta.get('errors_log')}`）" if (counters['errors'] or 0) else ""),
        f"- 事件：{_format_events(summary['events'])}",
        "",
    ]
    if summary["traced_mb"]["peak"] is not None:
        lines += [
            "## tracemalloc 对照",
            "",
            f"Python 已跟踪内存：起始 {fmt(summary['traced_mb']['start'])} MB → "
            f"结束 {fmt(summary['traced_mb']['end'])} MB（峰值 {fmt(summary['traced_mb']['peak'])} MB）。"
            "与工作集对比可区分「分配器行为」与「真泄漏」。",
            "",
        ]
    lines += [
        "## 口径与限制",
        "",
        "- 工作集（RSS）包含 Python 分配器缓存、Qt / pyqtgraph 的位图与字体缓存；",
        "  RSS 上涨不等于 Python 对象泄漏，必须与 tracemalloc 一列对照。",
        "- 句柄 / 线程用 psutil 取进程级数字，含脚本自身的 socket、采样临时对象。",
        "- 启动前 60 s 是 Qt 线程池 / 字体位图缓存的建立期，判定窗口已跳过；原始数据仍在 CSV 里。",
        "- 收帧 / 发帧 / 坏帧计数器自最近一次连接起算（断线重连会清零）；跨会话总量按事件行累加。",
        "- 本机若同时跑测试或其他构建任务，渲染耗时会被抢占干扰，报告需注明。",
    ]
    return "\n".join(lines) + "\n"


def _format_events(events: Dict[str, Any]) -> str:
    if not events:
        return "无"
    return "；".join(f"{name} {count} 次" for name, count in sorted(events.items()))


# ---------------------------------------------------------------- 系统能力


def keep_awake() -> bool:
    """Windows 上阻止系统睡眠；其它平台返回 False（不假装支持）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return True
    except Exception:  # pragma: no cover - 权限/老系统
        return False


def allow_sleep() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


def _process():
    import psutil  # noqa: PLC0415

    return psutil.Process()


# ---------------------------------------------------------------- 主流程


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="card38 小时级长稳压测（内置模拟器闭环 + 真实事件循环）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument("--hours", type=float, default=0.0, help="时长（小时）")
    duration.add_argument("--minutes", type=float, default=0.0, help="时长（分钟）")
    parser.add_argument("--seconds", type=float, default=0.0, help="时长（秒），优先级最低")
    parser.add_argument("--sample-every", type=float, default=10.0, help="采样间隔（秒）")
    parser.add_argument("--warmup-s", type=float, default=60.0,
                        help="判定窗口跳过前 N 秒（Qt 线程池 / 缓存建立期，算进去必然误报）")
    parser.add_argument("--param-every", type=float, default=60.0, help="参数下发间隔（秒），0=不调参")
    parser.add_argument("--step-every", type=float, default=900.0, help="阶跃间隔（秒），0=不跑阶跃")
    parser.add_argument("--reconnect-every", type=float, default=3600.0, help="断线重连间隔（秒），0=不断")
    parser.add_argument("--step-hold-s", type=float, default=20.0, help="每次阶跃持续多少秒后停")
    parser.add_argument("--baseline", action="store_true", help="空跑基线：不连模拟器、不下发，只量脚本自身开销")
    parser.add_argument("--show", action="store_true", help="显示真实窗口（默认离屏，避免挂机夜里被人点掉）")
    parser.add_argument("--tracemalloc", action="store_true", help="同时记录 Python 已跟踪内存（区分分配器行为 / 真泄漏）")
    parser.add_argument("--tracemalloc-snapshots", default="",
                        help="分代快照时刻（秒，逗号分隔，如 60,1500）；需同时开 --tracemalloc，"
                             "结束时把首末快照按分配点对比写 <csv>.tracemalloc.txt")
    parser.add_argument("--out", default="", help="CSV 输出路径；默认 docs/soak/<mode>-<时间>.csv")
    parser.add_argument("--summary", default="", help="Markdown 报告输出路径；默认与 CSV 同名 .md")
    parser.add_argument("--errors-log", default="", help="异常记录路径；默认与 CSV 同名 .errors.log")
    parser.add_argument("--json", default="", help="结构化结果（供其它脚本引用）")
    parser.add_argument("--no-keep-awake", action="store_true", help="不调用 SetThreadExecutionState")
    return parser


def _duration_s(args) -> float:
    if args.hours:
        return float(args.hours) * 3600.0
    if args.minutes:
        return float(args.minutes) * 60.0
    if args.seconds:
        return float(args.seconds)
    return 3600.0


def _default_paths(args, mode: str) -> tuple:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(_HERE, "..", "docs", "soak")
    base = args.out or os.path.join(out_dir, f"{mode}-{stamp}.csv")
    summary = args.summary or os.path.splitext(base)[0] + ".md"
    errors = args.errors_log or os.path.splitext(base)[0] + ".errors.log"
    return base, summary, errors


class SoakRun:
    """把负载挂在一张真实主窗口上，用 Qt 定时器采样（不阻塞事件循环）。"""

    def __init__(self, args, duration_s: float, mode: str) -> None:
        self.args = args
        self.duration_s = duration_s
        self.mode = mode
        self.csv_path, self.summary_path, self.errors_path = _default_paths(args, mode)
        self.recorder = Recorder(self.csv_path)
        self.started = time.monotonic()
        self.errors = 0
        self.kept_awake = False
        self.app = None
        self.win = None
        self.proc = _process()
        self._param_phase = 0
        self._param_sent = 0
        self._steps = 0
        self._reconnects = 0
        self._timers = []
        self._tracemalloc = None
        self._tm_snapshots: List[tuple] = []
        self._tm_diff_path = ""

    # ---- 基础设施 ----

    def _note_error(self, where: str, exc: BaseException) -> None:
        self.errors += 1
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.errors_path)), exist_ok=True)
            with open(self.errors_path, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n")
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
                fh.write("\n")
        except Exception:  # pragma: no cover - 记不下来也要继续跑
            pass

    def _safe(self, where: str, fn: Callable[[], Any], event: str = "") -> Any:
        started = time.perf_counter()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 长跑绝不能因一次回调异常而中断
            self._note_error(where, exc)
            self.sample(event or f"error:{where}", extra_ms=(time.perf_counter() - started) * 1000.0)
            return None

    def _snapshot(self) -> Dict[str, Any]:
        rss_mb = self.proc.memory_info().rss / CB
        try:
            handles = self.proc.num_handles()
        except (AttributeError, NotImplementedError, TypeError):
            handles = None
        elapsed = time.monotonic() - self.started
        row: Dict[str, Any] = {
            "t_s": round(elapsed, 1),
            "rss_mb": round(rss_mb, 2),
            "handles": handles,
            "threads": self.proc.num_threads(),
            "errors": self.errors,
        }
        if self._tracemalloc is not None:
            row["traced_mb"] = round(self._tracemalloc.get_traced_memory()[0] / CB, 2)
        # 界面侧指标全部容错读取：控件改名不该让长跑脚本崩
        win = self.win
        if win is not None:
            try:
                snapshot = win._worker.stats_snapshot()
                row["fps"] = round(float(snapshot.fps), 2)
                row["rx_frames"] = int(snapshot.rx_frames)
                row["tx_frames"] = int(snapshot.tx_frames)
                row["bad_frames"] = int(snapshot.bad_frames)
                rtt = snapshot.rtt_recent_avg_ms
                if rtt is None:
                    rtt = snapshot.rtt_avg_ms
                row["rtt_ms"] = round(float(rtt), 2) if rtt is not None else None
                row["connected"] = int(bool(win._worker.connected))
            except Exception as exc:  # noqa: BLE001
                self._note_error("snapshot.worker", exc)
            for key, getter in (
                ("trace_frames", lambda: len(win._worker.trace)),
                ("plot_points", lambda: int(win._plot.point_count)),
                ("buffer_points", lambda: len(win._buffer)),
                ("log_rows", lambda: int(win._log._view.blockCount())),
            ):
                try:
                    row[key] = getter()
                except Exception as exc:  # noqa: BLE001
                    self._note_error(f"snapshot.{key}", exc)
        return row

    def sample(self, event: str = "", extra_ms: Optional[float] = None) -> Dict[str, Any]:
        row = self._snapshot()
        row["event"] = event
        if extra_ms is not None:
            row["render_ms"] = round(extra_ms, 2)
        else:
            row["render_ms"] = self._measure_render()
        self.recorder.write(row)
        return row

    def _measure_render(self) -> Optional[float]:
        """显式重绘一次并计时（与 pressure_test 同口径，10 s 一次不扰动节拍）。"""
        if self.win is None:
            return None
        try:
            started = time.perf_counter()
            self.win._plot.render_now()
            return round((time.perf_counter() - started) * 1000.0, 2)
        except Exception as exc:  # noqa: BLE001
            self._note_error("render", exc)
            return None

    # ---- tracemalloc 分代对比 ----

    def _take_tm_snapshot(self, at_s: float) -> None:
        """按计划取一份 tracemalloc 快照（不阻塞事件循环，单次几十 ms）。"""
        if self._tracemalloc is None:
            return
        try:
            snap = self._tracemalloc.take_snapshot()
            current, peak = self._tracemalloc.get_traced_memory()
            self._tm_snapshots.append((float(at_s), snap, current / CB, peak / CB))
            self.sample(f"tracemalloc@{at_s:g}")
        except Exception as exc:  # noqa: BLE001
            self._note_error("tracemalloc.snapshot", exc)

    def _write_tm_diff(self) -> None:
        """把首末快照按分配点对比，Top 20 写盘；分配器噪声会散，真泄漏会指到行。"""
        if not self._tm_snapshots:
            return
        self._tm_diff_path = os.path.splitext(self.csv_path)[0] + ".tracemalloc.txt"
        path = self._tm_diff_path
        lines = [
            "# tracemalloc 分代对比",
            f"# 进程 {os.getpid()} · 快照 {len(self._tm_snapshots)} 份",
        ]
        for at_s, _snap, current_mb, peak_mb in self._tm_snapshots:
            lines.append(f"# t=+{at_s:g}s 已跟踪 {current_mb:.2f} MB（峰值 {peak_mb:.2f} MB）")
        if len(self._tm_snapshots) >= 2:
            first = self._tm_snapshots[0]
            last = self._tm_snapshots[-1]
            gained = last[2] - first[2]
            span_s = max(last[0] - first[0], 1e-9)
            lines += [
                "",
                f"## 首末对比：+{first[0]:g}s → +{last[0]:g}s（{span_s / 60.0:.1f} min）",
                f"已跟踪内存 {first[2]:.2f} → {last[2]:.2f} MB（{gained:+.2f} MB，"
                f"{gained * 3600.0 / span_s:+.1f} MB/h）",
                "",
                "Top 20 分配点（按净增长字节）：",
            ]
            try:
                stats = last[1].compare_to(first[1], "lineno")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"对比失败：{type(exc).__name__}: {exc}")
                stats = []
            shown = 0
            for stat in stats:
                if stat.size_diff <= 0:
                    continue
                shown += 1
                if shown > 20:
                    break
                site = stat.traceback.format()[-1].strip() if stat.traceback else "(unknown)"
                lines.append(
                    f"{shown:>2}. {stat.size_diff / 1024.0:+9.1f} KiB "
                    f"({stat.count_diff:+d} 块) {site}"
                )
            if shown == 0:
                lines.append("（没有净增长的分配点）")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # ---- 负载动作 ----

    def _tick_param(self) -> None:
        if self.mode != "soak" or self.win is None:
            return
        # 两档交替：PID_SET 与 SP_SET 走同一条 send 路径，但参数不同
        self._param_phase = (self._param_phase + 1) % 4
        values = (1.0, 1.5, 2.0, 1.5)[self._param_phase]
        try:
            self.win._param._kp.setValue(values)
            self.win._param._ki.setValue(0.02 + 0.01 * self._param_phase)
            if self._param_phase % 2 == 0:
                self.win._param._send_pid()
            else:
                self.win._param._sp.setValue(25.0 + float(self._param_phase))
                self.win._param._send_sp()
            self._param_sent += 1
            self.sample("param")
        except Exception as exc:  # noqa: BLE001
            self._note_error("param", exc)

    def _tick_step_start(self) -> None:
        if self.mode != "soak" or self.win is None:
            return
        try:
            self.win._step._start_step()
            self._steps += 1
            self.sample("step-start")
            # 按设定时长自动停止：阶跃命令本身带保持时长，这里再显式 STOP，
            # 让"停止"也走一遍真实下发路径（同时给曲线留一段完整响应）。
            self._timers.append(
                _single_shot(self._tick_step_stop, int(self.args.step_hold_s * 1000))
            )
        except Exception as exc:  # noqa: BLE001
            self._note_error("step-start", exc)

    def _tick_step_stop(self) -> None:
        if self.mode != "soak" or self.win is None:
            return
        try:
            self.win._step._stop_step()
            self.sample("step-stop")
        except Exception as exc:  # noqa: BLE001
            self._note_error("step-stop", exc)

    def _tick_reconnect(self) -> None:
        if self.mode != "soak" or self.win is None:
            return
        try:
            self.win._connect_btn.click()          # 断开
            self.sample("disconnect")
            self._timers.append(
                _single_shot(lambda: self._reconnect_back(), 3000)
            )
        except Exception as exc:  # noqa: BLE001
            self._note_error("disconnect", exc)

    def _reconnect_back(self) -> None:
        try:
            self.win._connect_btn.click()          # 重连
            self._reconnects += 1
            self.sample("reconnect")
        except Exception as exc:  # noqa: BLE001
            self._note_error("reconnect", exc)

    # ---- 启动 / 收尾 ----

    def _shutdown(self) -> None:
        """按顺序拆掉负载，别让后台线程往已删除的 QObject 上 emit。

        踩过的坑：直接 ``app.quit()`` 后在解释器退出阶段由 Qt 析构窗口，
        通信线程还在读帧，``frameReceived.emit`` 抛 "Signal source has been
        deleted"——控制台里一条吓人的 Traceback，数据其实没问题。这里显式
        先停下层、再停界面、最后 deleteLater + 抽事件循环，把顺序钉死。
        """
        for timer in self._timers:
            try:
                timer.stop()
            except Exception:  # pragma: no cover
                pass
        win = self.win
        if win is None:
            return
        self._safe("stop_sim", win._stop_sim_engine)
        self._safe("worker_close", win._worker.close)
        self._safe("window_close", win.close)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                if not getattr(win._worker, "connected", False):
                    break
            except Exception:  # noqa: BLE001
                break
            self.app.processEvents()
            time.sleep(0.05)
        try:
            win.deleteLater()
        except Exception:  # noqa: BLE001
            pass
        self.app.processEvents()
        time.sleep(0.2)
        self.win = None

    def run(self) -> Dict[str, Any]:
        from PySide6.QtCore import QTimer  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        if not self.args.show:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        if self.args.tracemalloc:
            import tracemalloc  # noqa: PLC0415

            tracemalloc.start(10)
            self._tracemalloc = tracemalloc

        # 配置隔离：长跑会写 ui 状态 / 预设，绝不能碰用户真实配置
        cfg_dir = os.path.dirname(os.path.abspath(self.csv_path))
        os.makedirs(cfg_dir, exist_ok=True)
        os.environ["SERIALPID_CONFIG"] = os.path.join(cfg_dir, "soak-config.json")

        if not self.args.no_keep_awake:
            self.kept_awake = keep_awake()

        self.app = QApplication.instance() or QApplication([])
        from serialpid.gui.main_window import CHANNEL_SIM, MainWindow  # noqa: PLC0415

        self.win = MainWindow(show_onboarding=False)
        self.win.show()
        self.app.processEvents()

        if self.mode == "soak":
            self.win._transport.setCurrentText(CHANNEL_SIM)
            self.app.processEvents()
            self._safe("connect", self.win._connect_btn.click)
            self._safe("heartbeat", lambda: self.win._worker.set_heartbeat(1.0, 3))

        self.sample("start")

        if self._tracemalloc is not None and self.args.tracemalloc_snapshots:
            for token in str(self.args.tracemalloc_snapshots).split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    at_s = float(token)
                except ValueError:
                    self._note_error(
                        "tracemalloc.parse",
                        ValueError(f"分代快照时刻不是数字：{token!r}"),
                    )
                    continue
                if not (0.0 < at_s <= self.duration_s):
                    continue
                QTimer.singleShot(
                    int(at_s * 1000),
                    lambda at=at_s: self._safe(
                        "tracemalloc.snapshot", lambda: self._take_tm_snapshot(at)
                    ),
                )

        if self.args.warmup_s and self.args.warmup_s > 0:
            QTimer.singleShot(
                int(self.args.warmup_s * 1000),
                lambda: self._safe("warmup_end", lambda: self.sample("warmup-end")),
            )

        for interval, slot in (
            (self.args.sample_every, self.sample),
            (self.args.param_every, self._tick_param),
            (self.args.step_every, self._tick_step_start),
            (self.args.reconnect_every, self._tick_reconnect),
        ):
            if interval and interval > 0:
                timer = QTimer()
                timer.setInterval(max(1, int(interval * 1000)))
                timer.timeout.connect(lambda fn=slot: self._safe(fn.__name__, fn))
                timer.start()
                self._timers.append(timer)

        QTimer.singleShot(int(self.duration_s * 1000), self.app.quit)
        self.app.exec()

        self.sample("end")
        self._shutdown()
        self.recorder.close()

        meta: Dict[str, Any] = {
            "mode": self.mode,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - self.duration_s)),
            "duration_s": round(self.duration_s, 1),
            "sample_every": self.args.sample_every,
            "warmup_s": self.args.warmup_s,
            "csv": os.path.relpath(os.path.abspath(self.csv_path), os.path.join(_HERE, "..")),
            "errors_log": os.path.relpath(os.path.abspath(self.errors_path), os.path.join(_HERE, "..")),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "qt": _qt_version(),
            "params_sent": self._param_sent,
            "steps": self._steps,
            "reconnects": self._reconnects,
            "kept_awake": self.kept_awake,
            "tracemalloc": bool(self.args.tracemalloc),
            "load": (
                "空跑基线（无链路、无下发）"
                if self.mode == "baseline"
                else f"内置模拟器 20Hz 遥测；每 {self.args.param_every:g}s 调参、"
                f"每 {self.args.step_every:g}s 阶跃、每 {self.args.reconnect_every:g}s 断线重连"
            ),
        }
        summary = summarize(self.recorder.rows, meta)
        with open(self.summary_path, "w", encoding="utf-8") as fh:
            fh.write(format_summary_md(meta, summary))
        self._write_tm_diff()
        if self._tm_diff_path:
            meta["tracemalloc_diff"] = os.path.relpath(
                os.path.abspath(self._tm_diff_path), os.path.join(_HERE, "..")
            )
        if self.args.json:
            with open(self.args.json, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, ensure_ascii=False, indent=2)

        allow_sleep()
        print()
        print("=== 结果 ===")
        print(f"模式 {self.mode} | 墙钟 {self.duration_s / 3600:.2f} h，"
              f"判定窗口 {summary['duration_h']:.2f} h | {summary['verdict']}")
        rss = summary["rss"]
        print(
            f"内存 {rss['start_mb']:.1f} → {rss['end_mb']:.1f} MB（峰值 {rss['peak_mb']:.1f}，"
            f"斜率 {rss['slope_mb_per_hour']:.2f} MB/h）| {rss['verdict']}"
        )
        print(
            f"句柄 {summary['handles']['start']} → {summary['handles']['end']}"
            f"（{summary['handles']['verdict']}）| 线程 {summary['threads']['start']} → "
            f"{summary['threads']['end']}（{summary['threads']['verdict']}）"
        )
        print(f"渲染：{summary['render_ms']['verdict']}")
        print(f"CSV {self.csv_path}")
        print(f"报告 {self.summary_path}")
        return summary


def _single_shot(fn: Callable[[], None], delay_ms: int):
    from PySide6.QtCore import QTimer  # noqa: PLC0415

    timer = QTimer()
    timer.setSingleShot(True)
    timer.setInterval(int(delay_ms))
    timer.timeout.connect(fn)
    timer.start()
    return timer


def _qt_version() -> str:
    try:
        import PySide6  # noqa: PLC0415

        return str(PySide6.__version__)
    except Exception:  # pragma: no cover
        return "—"


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    duration_s = _duration_s(args)
    if duration_s <= 0:
        raise SystemExit("时长必须大于 0（--hours / --minutes / --seconds）")
    mode = "baseline" if args.baseline else "soak"
    run = SoakRun(args, duration_s, mode)
    run.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
