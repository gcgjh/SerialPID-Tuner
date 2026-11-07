"""仿真被控对象：输出驱动的二阶惯性 + 纯延迟 + 执行器饱和 + 测量噪声/量化。

与旧模拟器的关键差别：对象吃的是**控制器输出（执行量）**，不是设定值。
旧实现 ``pv += dt * (sp - pv) / tau`` 把 PID 算出来的 out 丢掉，对象只是对 SP
做指数滤波——数学上不可能超调，改 Kp 曲线几乎不变。本模型把 out 接进对象，
闭环才会随控制参数变化。

结构（每拍一次 :meth:`Plant.step`）::

    u_cmd ──饱和──> u ──延迟N拍──> u_d ──增益──> 一级惯性(tau1) ──> 二级惯性(tau2)
                                                                        │
    pv = ambient + x2 ──加噪声──> ──量化──> 测量值（返回、写遥测）

约定：
    * ``ambient`` 是环境偏置（室温 / 静息值），对象状态是**相对**它的偏移，
      所以 ``u = 0`` 时对象自然回到 ``ambient``，而不是掉到 0。
    * 稳态偏移 = ``gain * u``，即 PV 稳态 = ``ambient + gain * u``。
    * 二阶 = 两级一阶串联；``tau2_s <= 0`` 时退化为一级惯性。
    * 噪声只叠加在**测量值**上，不写进 x1 / x2 / 延迟缓冲，所以同一种子重跑
      逐位一致，闭环研究可复现。
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import asdict, dataclass, fields
from typing import Deque, Dict, List, Optional

#: 执行器默认量程
OUT_MIN, OUT_MAX = 0.0, 100.0
#: 纯延迟拍数上限（50 ms 一拍即 50 s）。参数录错时不让缓冲无限增长。
MAX_DELAY_STEPS = 1000
#: 默认控制周期（20 Hz），与固件 / 旧模拟器一致
DEFAULT_DT_S = 0.05


@dataclass
class PlantParams:
    """对象参数（全部可运行期修改，单位见字段注释）。"""

    gain: float = 1.0          # 对象增益：稳态偏移 = gain * u
    tau1_s: float = 2.0        # 第一级时间常数（秒），<=0 视为瞬时
    tau2_s: float = 0.5        # 第二级时间常数（秒），<=0 退化为一级惯性
    delay_steps: int = 0       # 纯延迟拍数（执行量延迟 N 拍才作用于对象）
    noise_std: float = 0.0     # 测量噪声标准差（被控量单位），只作用于测量值
    quantum: float = 0.0       # 测量量化步长（传感器分辨率），0 表示不量化
    out_min: float = OUT_MIN   # 执行器下限（饱和）
    out_max: float = OUT_MAX   # 执行器上限（饱和）
    ambient: float = 20.0      # 环境偏置 / 静息值
    seed: int = 12345          # 测量噪声随机种子

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


class Plant:
    """二阶惯性对象。

    典型用法（引擎每拍调用）::

        plant = Plant(PlantParams(gain=2.0, tau1_s=2.0, tau2_s=0.5, delay_steps=3))
        pv = plant.step(out_from_pid)          # 返回本拍测量值
        telemetry(out=plant.last_out, pv=pv)   # 遥测里发饱和后的执行量

    参数:
        params: 对象参数；``None`` 用默认。
        dt_s: 默认步长（秒）；:meth:`step` 未显式传 dt 时用它。
    """

    def __init__(
        self,
        params: Optional[PlantParams] = None,
        dt_s: float = DEFAULT_DT_S,
    ) -> None:
        self.params = params or PlantParams()
        self._dt_s = self._check_dt(dt_s)
        self._history: Deque[float] = deque()
        self._warnings: List[str] = []
        self._sanitize()
        self.reset()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def pv(self) -> float:
        """最近一次测量值（含噪声与量化）。"""
        return self._y

    @property
    def last_out(self) -> float:
        """最近一拍饱和后的执行量（遥测的 out 字段用它，而不是未限幅值）。"""
        return self._u

    @property
    def warnings(self) -> List[str]:
        return list(self._warnings)

    @property
    def delay_buffer_len(self) -> int:
        """延迟缓冲当前长度（改小延迟拍数后应立刻跟着缩短）。"""
        return len(self._history)

    def state(self) -> Dict[str, float]:
        """对象内部状态快照（供测试 / 面板观察，噪声不在其中）。"""
        return {
            "x1": self._x1,
            "x2": self._x2,
            "pv_true": self.params.ambient + self._x2,
            "pv": self._y,
            "u": self._u,
        }

    def snapshot(self) -> Dict[str, object]:
        """参数 + 状态 + 警告的完整快照，便于日志与界面展示。"""
        return {
            "params": self.params.to_dict(),
            "state": self.state(),
            "dt_s": self._dt_s,
            "warnings": self.warnings,
        }

    def reset(self, ambient: Optional[float] = None) -> None:
        """把对象与随机数发生器送回初始状态（同一种子重跑结果完全一致）。"""
        if ambient is not None and math.isfinite(ambient):
            self.params.ambient = float(ambient)
        self._x1 = 0.0
        self._x2 = 0.0
        self._u = 0.0
        self._y = self.params.ambient
        self._history.clear()
        self._history.append(0.0)
        self._rng = random.Random(self.params.seed)

    # ------------------------------------------------------------------ #
    # 参数
    # ------------------------------------------------------------------ #

    def set_params(self, **kwargs: float) -> List[str]:
        """运行期改参数（不需要重建对象）。

        改 ``delay_steps`` 会立刻按新长度裁剪延迟缓冲；改 ``seed`` 会重建随机
        数发生器。返回本次新增的警告（空列表表示参数正常）。

        未知字段不抛异常，只记一条警告：面板/引擎会成批下发参数，一个拼错的
        名字不应该把整个仿真线程带崩（``sim/engine.py`` 直接调本方法且不捕获）。
        """
        known = {f.name for f in fields(PlantParams)}
        before = len(self._warnings)
        for key in sorted(set(kwargs) - known):
            self._warn(f"未知参数 {key}，已忽略")
        for key, value in kwargs.items():
            if key in known:
                setattr(self.params, key, value)
        self._sanitize()
        self._trim_history()
        return self._warnings[before:]

    def set_dt(self, dt_s: float) -> None:
        """改默认步长（引擎改控制周期时用）。"""
        self._dt_s = self._check_dt(dt_s)

    @property
    def dt_s(self) -> float:
        return self._dt_s

    # ------------------------------------------------------------------ #
    # 每拍推进
    # ------------------------------------------------------------------ #

    def step(self, out: float, dt_s: Optional[float] = None) -> float:
        """推进一步，返回本拍测量值。

        参数:
            out: 控制器输出（未限幅也可以，内部先做执行器饱和）。
            dt_s: 本拍步长；``None`` 用构造时的默认步长。
        """
        dt = self._check_dt(self._dt_s if dt_s is None else dt_s)
        p = self.params

        # 1) 执行器饱和：非有限值保持上一拍，避免 NaN 污染整个对象
        if math.isfinite(out):
            u = min(p.out_max, max(p.out_min, float(out)))
        else:
            u = self._u
            self._warn(f"控制器输出非有限值（{out!r}），保持上一拍执行量")
        self._u = u

        # 2) 纯延迟：取 delay_steps 拍之前的执行量；缓冲还没攒满那段视为未通电（0）
        self._history.append(u)
        self._trim_history()
        n = p.delay_steps
        delayed = self._history[0] if len(self._history) == n + 1 else 0.0

        # 3) 两级一阶惯性（后向欧拉，步长/时间常数比已夹住，不会发散）
        a1 = _alpha(dt, p.tau1_s)
        self._x1 += a1 * (p.gain * delayed - self._x1)
        if p.tau2_s > 0.0:
            a2 = _alpha(dt, p.tau2_s)
            self._x2 += a2 * (self._x1 - self._x2)
        else:
            self._x2 = self._x1

        # 4) 测量：噪声与量化只加在这里，不写回内部状态
        y = p.ambient + self._x2
        if p.noise_std > 0.0:
            y += self._rng.normalvariate(0.0, p.noise_std)
        if p.quantum > 0.0:
            y = round(y / p.quantum) * p.quantum
        self._y = y
        return y

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_dt(dt_s: float) -> float:
        try:
            v = float(dt_s)
        except (TypeError, ValueError):
            return DEFAULT_DT_S
        if not math.isfinite(v) or v <= 0.0:
            return DEFAULT_DT_S
        return v

    def _trim_history(self) -> None:
        """按当前延迟拍数裁剪缓冲：延迟改小时旧数据立刻丢掉，不堵在队列里。"""
        limit = max(0, int(self.params.delay_steps)) + 1
        while len(self._history) > limit:
            self._history.popleft()
        if not self._history:
            self._history.append(0.0)

    def _warn(self, text: str) -> None:
        if text not in self._warnings:
            self._warnings.append(text)

    def _sanitize(self) -> None:
        """把越界/非有限参数夹回可运行范围，并记一条警告（不抛异常）。"""
        p = self.params

        def num(name: str, value: float, default: float, lo: float, hi: float) -> float:
            try:
                v = float(value)
            except (TypeError, ValueError):
                self._warn(f"{name} 非数值（{value!r}），按 {default} 处理")
                return default
            if not math.isfinite(v):
                self._warn(f"{name} 非有限值（{value!r}），按 {default} 处理")
                return default
            if v < lo:
                self._warn(f"{name}={v:g} 超出下限 {lo:g}，已夹到 {lo:g}")
                return lo
            if v > hi:
                self._warn(f"{name}={v:g} 超出上限 {hi:g}，已夹到 {hi:g}")
                return hi
            return v

        p.gain = num("gain", p.gain, 1.0, 0.0, 1e4)
        p.tau1_s = num("tau1_s", p.tau1_s, 0.0, 0.0, 1e6)
        p.tau2_s = num("tau2_s", p.tau2_s, 0.0, 0.0, 1e6)
        p.delay_steps = int(num("delay_steps", p.delay_steps, 0, 0, MAX_DELAY_STEPS))
        p.noise_std = num("noise_std", p.noise_std, 0.0, 0.0, 1e4)
        p.quantum = num("quantum", p.quantum, 0.0, 0.0, 1e4)
        p.ambient = num("ambient", p.ambient, 20.0, -1e6, 1e6)

        p.out_min = num("out_min", p.out_min, OUT_MIN, -1e6, 1e6)
        p.out_max = num("out_max", p.out_max, OUT_MAX, -1e6, 1e6)
        if p.out_max <= p.out_min:
            self._warn(
                f"执行器量程无效（out_min={p.out_min} >= out_max={p.out_max}），"
                f"重置为 {OUT_MIN}..{OUT_MAX}"
            )
            p.out_min, p.out_max = OUT_MIN, OUT_MAX

        try:
            p.seed = int(p.seed)
        except (TypeError, ValueError):
            p.seed = 12345


def _alpha(dt_s: float, tau_s: float) -> float:
    """后向欧拉系数 dt/tau，夹在 [0, 1]。

    tau 极小时 dt/tau 会远大于 1，直接把状态甩过目标并来回振荡；夹到 1 表示
    "本拍直接跟到目标"，物理上是瞬时响应，数值上不发散。
    """
    if tau_s <= 0.0:
        return 1.0
    return min(1.0, dt_s / tau_s)
