"""仿真参数的持久化：下次打开还是上次那套对象。

为什么单独一份文件，不塞进 ``config.json``：``config.json`` 由界面侧整体读写
（窗口尺寸等记忆项），它只认自己的键，未知键在加载时会被丢掉。把仿真参数放进
去等于每次开窗都把它抹掉。因此另存 ``sim_state.json``，与配置文件同目录
（``SERIALPID_CONFIG`` 指到哪，它就跟着到哪，便携运行口径不变）。

行为与 ``config.py`` 一致：文件缺失写默认、内容损坏先备份再用默认、未知键忽略、
越界值夹回范围并给警告。绝不因为一份状态文件坏掉就阻塞启动。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional, Tuple

from .params import coerce_params, default_params

SCHEMA_NAME = "serialpid.sim"
STATE_VERSION = 1

_DOC_LINES = [
    "SerialPID-Tuner 内置模拟器参数记忆；每次改参数自动写入，下次启动读回。",
    "params：gain/tau1/tau2/delay/noise/quant/period_ms，单位见 sim/params.py。",
    "删除本文件即恢复默认；越界或非法值会被夹回范围并记录警告。",
]


def default_state_path() -> str:
    """``sim_state.json`` 的默认路径：与 ``config.json`` 同目录。"""
    from ..config import default_config_path

    config_path = default_config_path()
    directory = os.path.dirname(config_path) or "."
    return os.path.join(directory, "sim_state.json")


def load_params(path: Optional[str] = None) -> Tuple[Dict[str, float], Optional[str]]:
    """读取仿真参数；返回 ``(完整快照, 警告)``，缺失/损坏都用默认值。"""
    state_path = path if path is not None else default_state_path()
    params = default_params()
    if not os.path.exists(state_path):
        return params, None
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        backup = _backup_corrupt(state_path)
        return params, f"仿真参数文件损坏（{exc}），已备份为 {backup}，使用默认值"
    if not isinstance(payload, dict):
        backup = _backup_corrupt(state_path)
        return params, f"仿真参数文件内容不是对象，已备份为 {backup}，使用默认值"
    raw = payload.get("params", payload)
    if not isinstance(raw, dict):
        return params, "仿真参数 params 不是对象，已使用默认值"
    merged, warnings = coerce_params(raw)
    return merged, (warnings[0] if warnings else None)


def save_params(params: Dict[str, object], path: Optional[str] = None) -> str:
    """原子写入仿真参数（临时文件 + ``os.replace``），返回落盘路径。"""
    state_path = path if path is not None else default_state_path()
    directory = os.path.dirname(state_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    clean, _warnings = coerce_params(params)
    payload = {
        "schema": SCHEMA_NAME,
        "version": STATE_VERSION,
        "_说明": _DOC_LINES,
        "params": clean,
    }
    tmp = f"{state_path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, state_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return state_path


def clear(path: Optional[str] = None) -> bool:
    """删除参数文件（恢复默认）；不存在返回 ``False``。"""
    state_path = path if path is not None else default_state_path()
    try:
        os.remove(state_path)
        return True
    except OSError:
        return False


def _backup_corrupt(state_path: str) -> str:
    backup = f"{state_path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}.json"
    try:
        os.replace(state_path, backup)
    except OSError:
        backup = state_path
    return backup


__all__ = ["SCHEMA_NAME", "default_state_path", "load_params", "save_params", "clear"]
