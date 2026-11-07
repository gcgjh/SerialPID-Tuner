"""仿真参数的唯一定义（面板、预设、持久化、扫参脚本共用这一份）。

此前参数的范围/默认值/文案写在 ``gui/sim_panel.py``，持久化与扫参若各抄一份，
就会出现"面板允许 40 拍延迟、扫参脚本按 20 拍夹取"这种口径分裂（卡27 要消除的
正是这类分叉）。因此把口径收敛到核心层：

* :data:`PARAM_SPECS` —— 每个界面参数的标签、单位、范围、步进、默认值、一句人话；
* :data:`PLANT_FIELD_MAP` —— 面板键 -> ``PlantParams`` 字段名的唯一映射；
* :func:`default_params` / :func:`coerce_params` —— 默认快照与越界夹取。

本模块不依赖 Qt，供无界面脚本直接 import。
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

#: 六个核心对象参数 + 控制周期；键顺序即界面顺序
CORE_PARAMS: Tuple[str, ...] = ("gain", "tau1", "tau2", "delay", "noise", "quant")
PARAM_ORDER: Tuple[str, ...] = CORE_PARAMS + ("period_ms",)

#: 名称 / 单位 / 范围 / 步进 / 默认 / 一句人话解释
PARAM_SPECS: Dict[str, Dict[str, object]] = {
    "gain": {
        "label": "对象增益",
        "unit": "×",
        "low": 0.05,
        "high": 50.0,
        "step": 0.05,
        "decimals": 2,
        "default": 1.0,
        "hint": "执行量每走 1% 能让过程量涨多少；调大更容易超调。",
    },
    "tau1": {
        "label": "时间常数 1",
        "unit": "s",
        "low": 0.05,
        "high": 120.0,
        "step": 0.1,
        "decimals": 2,
        "default": 1.5,
        "hint": "第一级惯性的快慢；越小反应越快、越难稳。",
    },
    "tau2": {
        "label": "时间常数 2",
        "unit": "s",
        "low": 0.05,
        "high": 300.0,
        "step": 0.1,
        "decimals": 2,
        "default": 4.0,
        "hint": "第二级惯性的快慢；两级串联才有超调。",
    },
    "delay": {
        "label": "纯延迟",
        "unit": "拍",
        "low": 0,
        "high": 40,
        "step": 1,
        "decimals": 0,
        "default": 0,
        "hint": "执行量晚几拍才起作用；延迟是振荡的主因。",
    },
    "noise": {
        "label": "测量噪声 σ",
        "unit": "",
        "low": 0.0,
        "high": 5.0,
        "step": 0.01,
        "decimals": 2,
        "default": 0.0,
        "hint": "只加在测量值上，曲线抖但对象内部不受污染。",
    },
    "quant": {
        "label": "量化步长",
        "unit": "",
        "low": 0.0,
        "high": 2.0,
        "step": 0.001,
        "decimals": 3,
        "default": 0.0,
        "hint": "传感器分辨率；设成 0.1 就能看到阶梯。",
    },
    "period_ms": {
        "label": "控制周期",
        "unit": "ms",
        "low": 10,
        "high": 200,
        "step": 5,
        "decimals": 0,
        "default": 50,
        "hint": "引擎节拍，也是“拍数”的换算基准（50 ms = 20 Hz）。",
    },
}

#: 面板参数键 -> ``PlantParams`` 字段名（唯一映射，界面与脚本都从这里取）
PLANT_FIELD_MAP: Dict[str, str] = {
    "gain": "gain",
    "tau1": "tau1_s",
    "tau2": "tau2_s",
    "delay": "delay_steps",
    "noise": "noise_std",
    "quant": "quantum",
}

#: 整型字段（面板里有小数步进的除外）
_INT_FIELDS = ("delay_steps",)


def default_params() -> Dict[str, float]:
    """面板默认快照（按 ``PARAM_ORDER`` 顺序，值为 float）。"""
    return {key: float(PARAM_SPECS[key]["default"]) for key in PARAM_ORDER}


def plant_kwargs(params: Dict[str, float]) -> Dict[str, float]:
    """把面板快照转成 ``PlantParams`` 构造函数/``set_params`` 的关键字参数。

    只映射已知键，``period_ms`` 不进对象参数（它由 ``SimEngine.set_period`` 管）；
    缺项不补默认，避免把默认值意外覆盖到对象上。
    """
    out: Dict[str, float] = {}
    for key, field in PLANT_FIELD_MAP.items():
        if key not in params:
            continue
        value = params[key]
        out[field] = int(value) if field in _INT_FIELDS else float(value)
    return out


def coerce_params(values: Dict[str, object]) -> Tuple[Dict[str, float], List[str]]:
    """校验/夹取一份参数快照，返回 ``(合法快照, 警告列表)``。

    规则：未知键忽略并记警告；非数值/非有限值回落默认；越界夹到边界并记警告。
    参数录错不该让程序崩溃——这与 ``Plant._sanitize`` 的降级哲学一致。
    """
    params = default_params()
    warnings: List[str] = []
    for key, value in values.items():
        if key not in PARAM_SPECS:
            warnings.append(f"未知仿真参数 {key}，已忽略")
            continue
        spec = PARAM_SPECS[key]
        try:
            number = float(value)
        except (TypeError, ValueError):
            warnings.append(f"{key} 非数值（{value!r}），已用默认 {spec['default']}")
            continue
        if not math.isfinite(number):
            warnings.append(f"{key} 非有限值（{value!r}），已用默认 {spec['default']}")
            continue
        lo, hi = float(spec["low"]), float(spec["high"])
        if number < lo:
            warnings.append(f"{key}={number:g} 低于下限 {lo:g}，已夹到 {lo:g}")
            number = lo
        elif number > hi:
            warnings.append(f"{key}={number:g} 高于上限 {hi:g}，已夹到 {hi:g}")
            number = hi
        params[key] = number
    return params, warnings


__all__ = [
    "CORE_PARAMS",
    "PARAM_ORDER",
    "PARAM_SPECS",
    "PLANT_FIELD_MAP",
    "default_params",
    "plant_kwargs",
    "coerce_params",
]
