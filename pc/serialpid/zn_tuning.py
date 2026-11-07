"""Ziegler-Nichols 自整定辅助计算（纯 PC 侧，不改动 MCU）。

提供两套经典 Z-N 公式，全部在 PC 上算完，再由界面把 Kp/Ki/Kd 经既有
``PID_SET`` 帧下发给固件；MCU 只执行既定 PID 运算，不参与整定过程。

方法一 · 闭环临界振荡法（continuous cycling）：
    先只留比例作用，增大 Kp 直到输出出现等幅振荡，记录临界增益 Ku 与
    临界周期 Tu（s），再按下表反推各控制律参数。

方法二 · 开环反应曲线法（process reaction / 切线法）：
    对开环对象施加阶跃，从响应曲线辨识对象增益 K、纯滞后 L（s）、
    时间常数 T（s），再按下表反推参数。

公式表（经典 Ziegler-Nichols）::

    闭环法            Kp            Ti          Td
    P               0.50 Ku        —           —
    PI              0.45 Ku        Tu/1.2      —
    PID             0.60 Ku        Tu/2        Tu/8

    开环法            Kp            Ti          Td
    P               T/(K L)        —           —
    PI              0.90 T/(K L)   3.33 L      —
    PID             1.20 T/(K L)   2 L         0.5 L

其中 Ki = Kp / Ti，Kd = Kp * Td（Ti/Td 单位均为 s）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class ZNParams:
    """一组整定结果。Ti/Td 为 None 表示该控制律不含此环节。"""

    mode: str            # "P" / "PI" / "PID"
    kp: float
    ki: float
    kd: float
    ti: Optional[float]
    td: Optional[float]
    source: str          # "closed_loop" / "open_loop"

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    def summary(self) -> str:
        return f"{self.mode}: Kp={self.kp:.4g}  Ki={self.ki:.4g}  Kd={self.kd:.4g}"


@dataclass
class ReactionCurve:
    """反应曲线辨识结果。L / T 单位为 s，K 为无量纲增益。"""

    k: float
    l: float
    t: float
    step_t_ms: float
    inflection_t_ms: float
    note: str = ""


# --------------------------------------------------------------------------- #
# 闭环临界振荡法
# --------------------------------------------------------------------------- #


def zn_closed_loop(ku: float, tu: float) -> Dict[str, ZNParams]:
    """由临界增益 Ku、临界周期 Tu（s）计算 P / PI / PID 三组参数。"""
    if ku <= 0:
        raise ValueError("Ku 必须大于 0")
    if tu <= 0:
        raise ValueError("Tu 必须大于 0（单位 s）")

    result: Dict[str, ZNParams] = {}

    # P
    kp = 0.50 * ku
    result["P"] = ZNParams("P", kp, 0.0, 0.0, None, None, "closed_loop")

    # PI
    kp = 0.45 * ku
    ti = tu / 1.2
    result["PI"] = ZNParams("PI", kp, kp / ti, 0.0, ti, None, "closed_loop")

    # PID
    kp = 0.60 * ku
    ti = tu / 2.0
    td = tu / 8.0
    result["PID"] = ZNParams("PID", kp, kp / ti, kp * td, ti, td, "closed_loop")

    return result


# --------------------------------------------------------------------------- #
# 开环反应曲线法
# --------------------------------------------------------------------------- #


def zn_open_loop(k: float, l: float, t: float) -> Dict[str, ZNParams]:
    """由对象增益 K、滞后 L（s）、时间常数 T（s）计算 P / PI / PID 三组参数。"""
    if k <= 0:
        raise ValueError("对象增益 K 必须大于 0")
    if l <= 0:
        raise ValueError("纯滞后 L 必须大于 0（单位 s）")
    if t <= 0:
        raise ValueError("时间常数 T 必须大于 0（单位 s）")

    result: Dict[str, ZNParams] = {}

    # P
    kp = t / (k * l)
    result["P"] = ZNParams("P", kp, 0.0, 0.0, None, None, "open_loop")

    # PI
    kp = 0.90 * t / (k * l)
    ti = 3.33 * l
    result["PI"] = ZNParams("PI", kp, kp / ti, 0.0, ti, None, "open_loop")

    # PID
    kp = 1.20 * t / (k * l)
    ti = 2.0 * l
    td = 0.5 * l
    result["PID"] = ZNParams("PID", kp, kp / ti, kp * td, ti, td, "open_loop")

    return result


# --------------------------------------------------------------------------- #
# 反应曲线辨识（切线法）
# --------------------------------------------------------------------------- #


def _moving_average(xs: Sequence[float], win: int) -> List[float]:
    if win <= 1:
        return list(xs)
    half = win // 2
    out: List[float] = []
    for i in range(len(xs)):
        lo = max(0, i - half)
        hi = min(len(xs), i + half + 1)
        out.append(sum(xs[lo:hi]) / (hi - lo))
    return out


def estimate_reaction_curve(
    times: Sequence[float],
    sp: Sequence[float],
    pv: Sequence[float],
    step_index: Optional[int] = None,
    smooth_win: int = 3,
) -> ReactionCurve:
    """从开环阶跃响应曲线辨识 K、L、T（切线法）。

    参数:
        times: 时刻序列 ms。
        sp: 设定值序列。
        pv: 被控量序列（开环响应，未闭环）。
        step_index: 阶跃起点下标；``None`` 时按 SP 最大跳变自动定位。
        smooth_win: 斜率估计前的滑动平均窗宽，抑制测量噪声。

    返回:
        :class:`ReactionCurve`，其中 L / T 已换算为秒。
    """
    n = len(times)
    if not (len(sp) == n and len(pv) == n) or n < 6:
        raise ValueError("曲线过短或长度不一致，无法辨识反应曲线")

    if step_index is None:
        best_k, best_d = 1, 0.0
        for k in range(1, n):
            d = abs(sp[k] - sp[k - 1])
            if d > best_d:
                best_k, best_d = k, d
        if best_d <= 1e-9:
            raise ValueError("未检测到阶跃")
        step_index = best_k

    tail_n = max(3, math.ceil(0.2 * (n - step_index)))
    sp0 = sum(sp[:step_index]) / step_index if step_index > 0 else sp[0]
    pv0 = sum(pv[:step_index]) / step_index if step_index > 0 else pv[0]
    sp1 = sum(sp[-tail_n:]) / tail_n
    pv1 = sum(pv[-tail_n:]) / tail_n

    delta_sp = sp1 - sp0
    delta_pv = pv1 - pv0
    if abs(delta_sp) <= 1e-9:
        raise ValueError("阶跃幅度过小")
    k_gain = delta_pv / delta_sp

    smooth = _moving_average(pv, smooth_win)

    # 找拐点：阶跃后斜率绝对值最大处
    slopes: List[float] = []
    for i in range(step_index + 1, n):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            slopes.append(0.0)
            continue
        slopes.append((smooth[i] - smooth[i - 1]) / dt)  # 每 ms 变化率
    if not slopes:
        raise ValueError("阶跃后样本不足")

    if delta_pv >= 0:
        rel = max(range(len(slopes)), key=lambda i: slopes[i])
        m_ms = slopes[rel]
        if m_ms <= 1e-9:
            raise ValueError("未找到正向上升沿，无法辨识切线")
    else:
        rel = min(range(len(slopes)), key=lambda i: slopes[i])
        m_ms = slopes[rel]
        if m_ms >= -1e-9:
            raise ValueError("未找到负向下降沿，无法辨识切线")

    inflection_idx = step_index + 1 + rel
    t_inf = times[inflection_idx]
    pv_inf = smooth[inflection_idx]

    # 切线与初值线交点 → 纯滞后 L；切线与终值线交点 → 时间常数 T
    t_cross_init = t_inf - (pv_inf - pv0) / m_ms
    t_cross_final = t_inf + (pv1 - pv_inf) / m_ms
    l_ms = t_cross_init - times[step_index]
    t_ms_ = (t_cross_final - times[step_index]) - l_ms

    note = ""
    if l_ms < 0:
        note = "辨识出的滞后为负，请检查阶跃起点或噪声；建议手动指定 step_index"
        l_ms = 0.0
    if t_ms_ <= 0:
        note = (note + " | " if note else "") + "时间常数非正，曲线可能未到达稳态"

    return ReactionCurve(
        k=k_gain,
        l=l_ms / 1000.0,
        t=max(t_ms_, 0.0) / 1000.0,
        step_t_ms=times[step_index],
        inflection_t_ms=t_inf,
        note=note,
    )
