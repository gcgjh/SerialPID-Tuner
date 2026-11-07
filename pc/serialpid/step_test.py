"""阶跃响应分析与自动阶跃测试状态机（PC 侧纯逻辑层）。

本模块不依赖串口、不依赖 Qt，也不依赖 numpy，可在离线脚本 / 单测 /
上位机界面中复用。职责分两块：

1. :func:`analyze_step` —— 给定遥测序列 (t, SP, PV)，自动定位阶跃起点，
   计算控制品质指标：超调量、峰值时间、上升时间 (10%-90%)、调节时间、
   稳态误差、欠调量。
2. :class:`StepTester` —— 自动阶跃测试状态机：先在初始值上静置，到点后
   通过注入的回调下发跳变目标值，标记阶跃起点；曲线进入稳态带或超时后
   自动出指标并回调通知界面。

串口下发由注入的 ``setpoint_cb(sp: float)`` 完成，本模块不直接打开串口；
GUI 只负责把遥测样本喂进来、把指标显示出去。
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class Sample:
    """单条遥测样本（与协议层 TELEMETRY 帧字段对齐）。"""

    t_ms: float
    sp: float
    pv: float
    out: float = 0.0
    err: float = 0.0


@dataclass
class StepMetrics:
    """阶跃响应品质指标。时间单位 ms，幅值单位与被控量一致。"""

    step_index: int                     # 阶跃起点在序列中的下标
    step_t_ms: float                    # 阶跃起点时刻 ms
    sp_initial: float                   # 阶跃前设定值（均值）
    sp_final: float                     # 阶跃后设定值（均值）
    pv_initial: float                   # 阶跃前被控量（均值）
    pv_final: float                     # 稳态被控量（尾部均值估计）
    delta_sp: float                     # 设定值跳变幅度 SP_final - SP_initial
    direction: str                      # "rising" / "falling"
    band: float                         # 调节时间所用稳态带（相对 |ΔSP|）
    overshoot_pct: float                # 超调量 %（相对 |ΔSP|，无超调为 0）
    peak_value: float                   # 峰值（上升沿为最大值，下降沿为最小值）
    peak_t_ms: Optional[float]          # 峰值时刻（相对阶跃起点的偏移）
    undershoot_pct: float               # 欠调量 %（相对 |ΔSP|）
    rise_time_ms: Optional[float]       # 10%-90% 上升时间 ms，不可得为 None
    settling_time_ms: Optional[float]   # 调节时间 ms，始终未入带为 None
    settled: bool                       # 是否在观测窗内进入稳态带
    steady_state_error: float           # 稳态误差 PV_final - SP_final
    steady_state_error_pct: float       # 稳态误差 %（相对 |ΔSP|）
    note: str = ""                      # 告警 / 备注信息

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    def summary(self) -> str:
        """单行可读摘要，供状态栏 / 日志直接展示。"""
        st = "已稳态" if self.settled else "未收敛"
        lines = [
            f"阶跃 {self.sp_initial:.3f} → {self.sp_final:.3f}（{self.direction}）",
            f"超调量 {self.overshoot_pct:.2f}%　峰值 {self.peak_value:.3f}"
            + (f" @ {self.peak_t_ms:.0f} ms" if self.peak_t_ms is not None else ""),
            f"调节时间 "
            + (f"{self.settling_time_ms:.0f} ms" if self.settling_time_ms is not None else "—"),
            f"稳态误差 {self.steady_state_error:.3f}"
            + f"（{self.steady_state_error_pct:.2f}%）　{st}",
        ]
        if self.note:
            lines.append(f"备注：{self.note}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 指标计算
# --------------------------------------------------------------------------- #


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def analyze_step(
    times: Sequence[float],
    sp: Sequence[float],
    pv: Sequence[float],
    band: float = 0.02,
    tail_ratio: float = 0.2,
    step_index: Optional[int] = None,
    min_tail: int = 3,
    min_suffix_ratio: float = 0.1,
) -> StepMetrics:
    """分析一段阶跃响应曲线并返回指标。

    参数:
        times: 时刻序列，单位 ms（单调递增即可，不要求等间隔）。
        sp: 设定值序列，与 ``times`` 等长。
        pv: 被控量序列，与 ``times`` 等长。
        band: 稳态带，相对跳变幅度 |ΔSP|，默认 0.02（2%）；常用 0.05。
        tail_ratio: 估计稳态值时取末尾多少比例样本做均值，默认 20%。
        step_index: 手动指定阶跃起点下标；``None`` 时按 SP 最大跳变自动定位。
        min_tail: 稳态估计尾部最少保留样本数。
        min_suffix_ratio: 判定收敛所需的最小后缀长度（占阶跃后样本比例），
            避免末端单点落进稳态带就被误判为已收敛。

     Raises:
        ValueError: 序列过短、长度不一致或检测不到阶跃。
    """
    n = len(times)
    if not (len(sp) == n and len(pv) == n):
        raise ValueError("times / sp / pv 长度必须一致")
    if n < 4:
        raise ValueError("样本过少（至少 4 个点），无法分析阶跃响应")

    # 1. 定位阶跃起点 -------------------------------------------------------
    if step_index is None:
        best_k, best_d = 1, 0.0
        for k in range(1, n):
            d = abs(sp[k] - sp[k - 1])
            if d > best_d:
                best_k, best_d = k, d
        if best_d <= 1e-9:
            raise ValueError("SP 序列中未检测到阶跃变化")
        step_index = best_k
    else:
        step_index = max(1, min(step_index, n - 1))

    # 2. 阶跃前后基准值 ------------------------------------------------------
    if step_index > 0:
        sp_initial = _mean(sp[:step_index])
        pv_initial = _mean(pv[:step_index])
    else:
        sp_initial = sp[0]
        pv_initial = pv[0]
    sp_final = _mean(sp[step_index:])
    delta = sp_final - sp_initial
    if abs(delta) <= 1e-9:
        raise ValueError("阶跃幅度过小，无法计算相对指标")

    t_post = list(times[step_index:])
    pv_post = list(pv[step_index:])
    rising = delta > 0.0

    # 3. 稳态值（尾部均值，抑制单点噪声） ------------------------------------
    tail_n = max(min_tail, int(math.ceil(tail_ratio * len(pv_post))))
    pv_final = _mean(pv_post[-tail_n:])

    # 4. 峰值 / 超调 / 欠调 --------------------------------------------------
    if rising:
        peak_idx = max(range(len(pv_post)), key=lambda i: pv_post[i])
        peak = pv_post[peak_idx]
        overshoot_abs = peak - pv_final
        trough = min(pv_post)
        undershoot_abs = pv_initial - trough
    else:
        peak_idx = min(range(len(pv_post)), key=lambda i: pv_post[i])
        peak = pv_post[peak_idx]
        overshoot_abs = pv_final - peak
        trough = max(pv_post)
        undershoot_abs = trough - pv_initial

    overshoot_pct = max(0.0, overshoot_abs / abs(delta) * 100.0)
    undershoot_pct = max(0.0, undershoot_abs / abs(delta) * 100.0)
    peak_t_ms = t_post[peak_idx] - times[step_index]

    # 5. 调节时间：进入稳态带后不再穿出 --------------------------------------
    band_abs = band * abs(delta)
    min_suffix = max(2, math.ceil(min_suffix_ratio * len(pv_post)))
    settled_idx: Optional[int] = None
    for j in range(step_index, n):
        if (n - j) < min_suffix:
            break
        if all(abs(pv[k] - pv_final) <= band_abs for k in range(j, n)):
            settled_idx = j
            break
    settled = settled_idx is not None
    settling_time_ms: Optional[float] = (
        times[settled_idx] - times[step_index] if settled else None
    )

    # 6. 上升时间 10% - 90% --------------------------------------------------
    lo = pv_initial + 0.1 * delta
    hi = pv_initial + 0.9 * delta
    rise_time_ms: Optional[float] = None
    t_lo = t_hi = None
    for i, v in enumerate(pv_post):
        if t_lo is None:
            if (rising and v >= lo) or (not rising and v <= lo):
                t_lo = t_post[i]
        else:
            if (rising and v >= hi) or (not rising and v <= hi):
                t_hi = t_post[i]
                break
    if t_lo is not None and t_hi is not None:
        rise_time_ms = t_hi - t_lo

    # 7. 稳态误差 ------------------------------------------------------------
    sse = pv_final - sp_final
    sse_pct = sse / abs(delta) * 100.0

    note_parts: List[str] = []
    if not settled:
        note_parts.append("观测窗内未进入稳态带，调节时间不可得")
    if abs(sse_pct) > 5.0:
        note_parts.append("稳态误差偏大，建议检查积分项或被控对象偏差")

    return StepMetrics(
        step_index=step_index,
        step_t_ms=times[step_index],
        sp_initial=sp_initial,
        sp_final=sp_final,
        pv_initial=pv_initial,
        pv_final=pv_final,
        delta_sp=delta,
        direction="rising" if rising else "falling",
        band=band,
        overshoot_pct=overshoot_pct,
        peak_value=peak,
        peak_t_ms=peak_t_ms,
        undershoot_pct=undershoot_pct,
        rise_time_ms=rise_time_ms,
        settling_time_ms=settling_time_ms,
        settled=settled,
        steady_state_error=sse,
        steady_state_error_pct=sse_pct,
        note="；".join(note_parts),
    )


# --------------------------------------------------------------------------- #
# 自动阶跃测试状态机
# --------------------------------------------------------------------------- #


class StepState:
    IDLE = "idle"            # 未开始
    ARMED = "armed"          # 已在初始值上静置，等待跳变
    RUNNING = "running"       # 已跳变，采集响应曲线
    CONFIRMING = "confirming"  # 已进入稳态带，保持确认窗口中
    DONE = "done"            # 出指标


class StepTester:
    """自动阶跃测试流程控制器。

    典型用法（界面侧）::

        tester = StepTester(setpoint_cb=link.set_setpoint,
                            on_metrics=panel.show_metrics)
        tester.begin(initial_sp=25.0, target_sp=50.0)
        # 每收到一帧 TELEMETRY：
        tester.on_sample(t_ms, sp, pv, out, err)

    参数:
        setpoint_cb: 下发设定值的回调，签名 ``cb(sp: float) -> None``，
            由串口层 / 仿真层注入，本模块不关心其内部实现。
        band: 稳态带（相对 |ΔSP|），同时用于稳态判定与指标计算。
        pre_settle_ms: 跳变前在初始值上静置时长，ms。
        timeout_ms: 跳变后最长采集时长，超时强制出指标并标注未收敛。
        stable_window_ms: 判定进入稳态所需的最近窗口时长，ms。
        tail_ratio: 指标计算时稳态尾部均值比例，透传给 :func:`analyze_step`。
        on_metrics: 出指标后的回调，签名 ``cb(metrics: StepMetrics) -> None``。
    """

    def __init__(
        self,
        setpoint_cb: Callable[[float], None],
        band: float = 0.02,
        pre_settle_ms: float = 1000.0,
        timeout_ms: float = 30000.0,
        stable_window_ms: float = 500.0,
        tail_ratio: float = 0.2,
        on_metrics: Optional[Callable[[StepMetrics], None]] = None,
    ) -> None:
        self.setpoint_cb = setpoint_cb
        self.band = band
        self.pre_settle_ms = pre_settle_ms
        self.timeout_ms = timeout_ms
        self.stable_window_ms = stable_window_ms
        self.tail_ratio = tail_ratio
        self.on_metrics = on_metrics

        self.state: str = StepState.IDLE
        self.samples: List[Sample] = []
        self.metrics: Optional[StepMetrics] = None
        self.last_error: str = ""

        self.initial_sp = 0.0
        self.target_sp = 0.0
        self._t0: Optional[float] = None
        self._step_t: Optional[float] = None
        self._step_index: Optional[int] = None
        self._confirm_deadline: Optional[float] = None

    # -- 流程控制 ----------------------------------------------------------- #

    def begin(self, initial_sp: float, target_sp: float) -> None:
        """开始一轮测试：清空历史，进入静置等待跳变状态。"""
        self.initial_sp = initial_sp
        self.target_sp = target_sp
        self.samples = []
        self.metrics = None
        self._t0 = None
        self._step_t = None
        self._step_index = None
        self._confirm_deadline = None
        self.state = StepState.ARMED

    def stop(self) -> None:
        """手动停止并按已采集曲线出指标。"""
        if self.state != StepState.DONE:
            self._finish("手动停止测试")

    def on_sample(
        self,
        t_ms: float,
        sp: float,
        pv: float,
        out: float = 0.0,
        err: float = 0.0,
    ) -> None:
        """喂入一条遥测样本；状态机内部决定静置 / 跳变 / 判定稳态。"""
        if self.state == StepState.DONE:
            return
        self.samples.append(Sample(t_ms, sp, pv, out, err))

        if self.state == StepState.ARMED:
            if self._t0 is None:
                self._t0 = t_ms
            if t_ms - self._t0 >= self.pre_settle_ms:
                # 自动跳变：通过回调把目标值下发给被控端，并标记阶跃起点
                self.setpoint_cb(self.target_sp)
                self._step_t = t_ms
                self._step_index = len(self.samples) - 1
                self.state = StepState.RUNNING
        elif self.state in (StepState.RUNNING, StepState.CONFIRMING):
            self._check_done(t_ms)

    # -- 内部 ---------------------------------------------------------------- #

    def _check_done(self, t_ms: float) -> None:
        delta = abs(self.target_sp - self.initial_sp)
        band_abs = self.band * delta

        if self._step_t is not None and t_ms - self._step_t >= self.timeout_ms:
            self._finish("超时：曲线未在规定时间内进入稳态带")
            return

        recent = [
            s for s in self.samples if s.t_ms >= t_ms - self.stable_window_ms
        ]
        stable = len(recent) >= 2 and all(
            abs(s.pv - self.target_sp) <= band_abs for s in recent
        )

        if self.state == StepState.RUNNING:
            if stable:
                # 先进入确认状态，再保持一个确认窗口，保证尾部采到稳态样本
                self.state = StepState.CONFIRMING
                self._confirm_deadline = t_ms + self.stable_window_ms
        elif self.state == StepState.CONFIRMING:
            if not stable:
                self.state = StepState.RUNNING  # 又穿出稳态带，重新确认
            elif self._confirm_deadline is not None and t_ms >= self._confirm_deadline:
                self._finish()

    def _finish(self, note: str = "") -> None:
        self.state = StepState.DONE
        self.last_error = ""
        if len(self.samples) < 2:
            return
        times = [s.t_ms for s in self.samples]
        sp = [s.sp for s in self.samples]
        pv = [s.pv for s in self.samples]
        try:
            self.metrics = analyze_step(
                times,
                sp,
                pv,
                band=self.band,
                tail_ratio=self.tail_ratio,
                step_index=self._step_index,
            )
        except ValueError as exc:
            # 样本不足 / 无有效阶跃：保留曲线与 CSV，指标留空并记录原因
            self.metrics = None
            self.last_error = str(exc)
            return
        if note:
            self.metrics.note = (
                (self.metrics.note + " | " + note).strip(" |")
            )
        if self.on_metrics is not None:
            self.on_metrics(self.metrics)

    # -- 导出 ---------------------------------------------------------------- #

    def export_csv(self, path: str) -> int:
        """把已采集样本写成 CSV，返回写入行数（不含表头）。"""
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["t_ms", "sp", "pv", "out", "err"])
            for s in self.samples:
                writer.writerow([
                    f"{s.t_ms:.3f}", f"{s.sp:.4f}", f"{s.pv:.4f}",
                    f"{s.out:.4f}", f"{s.err:.4f}",
                ])
        return len(self.samples)
