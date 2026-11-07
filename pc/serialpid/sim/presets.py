"""仿真场景预设（8 个），一处定义、面板与脚本共用。

每个预设是一整套对象参数（含控制周期），切换时整组替换，避免"只改了两个
参数、其余还是上一场景"的串味。``description`` 写的是**对应什么真实对象**——
用户选预设时先认对象，再谈参数。

原 ``gui/sim_panel.py`` 里的 5 个预设整体搬到这里（名称保留，卡25 的测试
按名字取用），并按卡27 补齐执行器饱和 / 粗糙量化 / 纯延迟主导三类场景，
"大增益易振荡"由原「接近临界振荡」改名而来（同一物理现象，与新增三类合计 8 个）。
"""

from __future__ import annotations

from typing import Dict, Iterator, List, Optional

#: 预设名 -> 参数覆盖（键必须覆盖 ``PARAM_ORDER`` 全部 7 项）
PRESETS: Dict[str, Dict[str, float]] = {
    "快速小惯性": {
        "gain": 1.0, "tau1": 0.3, "tau2": 0.8, "delay": 0,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "慢速大惯性热对象": {
        "gain": 1.0, "tau1": 12.0, "tau2": 45.0, "delay": 0,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "大延迟对象": {
        "gain": 1.0, "tau1": 2.0, "tau2": 5.0, "delay": 10,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "带噪声传感器": {
        "gain": 1.0, "tau1": 1.2, "tau2": 3.0, "delay": 0,
        "noise": 1.2, "quant": 0.05, "period_ms": 50,
    },
    "大增益易振荡": {
        "gain": 8.0, "tau1": 0.8, "tau2": 2.5, "delay": 6,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "执行器饱和": {
        "gain": 30.0, "tau1": 3.0, "tau2": 8.0, "delay": 1,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "粗糙量化": {
        "gain": 1.0, "tau1": 1.2, "tau2": 3.0, "delay": 0,
        "noise": 0.8, "quant": 0.5, "period_ms": 50,
    },
    "纯延迟主导": {
        "gain": 1.0, "tau1": 0.3, "tau2": 0.8, "delay": 30,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
}

#: 预设名 -> 一句话"对应什么真实对象 / 会看到什么"
DESCRIPTIONS: Dict[str, str] = {
    "快速小惯性": "小热容探头、小功率加热片：几十毫秒就跟着动，曲线几乎不超调。",
    "慢速大惯性热对象": "大功率加热炉、热水箱：十几秒才见反应，整定周期长，容易等不及。",
    "大延迟对象": "长管道、远距离测温：延迟半秒左右，延迟与惯性同量级。",
    "带噪声传感器": "廉价热电偶加长线干扰：曲线上有毛刺，读数最小分辨约 0.05。",
    "大增益易振荡": "高放大倍数执行器（小加热器 + 大功率驱动）：Kp 略大就等幅振荡。",
    "执行器饱和": "大惯性对象配小执行器：输出很快顶到 0 或 100，积分饱和后回不来。",
    "粗糙量化": "8 位 ADC / 0.5 度分辨率传感器：曲线呈明显阶梯，微分项被放大。",
    "纯延迟主导": "传送带、气动输送：一秒半的纯滞后，惯性可忽略，先空走一段才看到反应。",
}


def names() -> List[str]:
    """预设名列表（顺序即界面下拉顺序）。"""
    return list(PRESETS)


def get(name: str) -> Optional[Dict[str, float]]:
    """按名取预设副本；不存在返回 ``None``。"""
    preset = PRESETS.get(name)
    return dict(preset) if preset is not None else None


def describe(name: str) -> str:
    """预设的一句话说明；未知名字返回空串。"""
    return DESCRIPTIONS.get(name, "")


def match(params: Dict[str, float], tol: float = 1e-9) -> Optional[str]:
    """参数快照与哪个预设完全一致（用于下拉回显）；都不匹配返回 ``None``。"""
    for name, preset in PRESETS.items():
        if all(abs(float(params.get(key, 0.0)) - value) <= tol for key, value in preset.items()):
            return name
    return None


def items() -> Iterator[tuple]:
    return iter(PRESETS.items())


__all__ = ["PRESETS", "DESCRIPTIONS", "names", "get", "describe", "match", "items"]
