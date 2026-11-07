# -*- coding: utf-8 -*-
"""应用配置（JSON）：参数上下限等产品配置项，界面按配置生成输入框上下限。

被控对象的合理量级（增益几百、SP 大范围）是产品配置而不是界面细节。
本模块只负责**界面允许输入的范围**；下发到固件前的数值有限性校验
（NaN / inf / 超出 float32）保留在 ``protocol.pack_pid_set`` 中——
范围可以放宽，防线不能拆。

默认路径：
    Windows  ``%APPDATA%/SerialPID-Tuner/config.json``
    其它系统  ``~/.config/serialpid-tuner/config.json``
可用环境变量 ``SERIALPID_CONFIG`` 覆盖（便携运行 / 测试注入）。

行为：
    * 文件缺失 -> 写入默认配置后返回默认值；
    * 文件损坏 / JSON 非法 -> 原文件备份为 ``config.corrupt-*.json``，
      返回默认值并给出警告；
    * 数值非法（非有限数、min>max、step<=0）-> 该项回落默认值并警告；
    * 未知键一律忽略，向后兼容。
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Dict, Optional, Tuple

SCHEMA_NAME = "serialpid.config"
CONFIG_VERSION = 1

_DOC_LINES = [
    "SerialPID-Tuner 应用配置；修改后重启程序生效。",
    "param_limits：界面参数输入框的上下限 / 步进 / 默认值。",
    "注意：放宽范围不会关闭下发前的数值有限性校验（NaN / inf / 超出 float32 仍会被拒绝）。",
    "示例：把 Kp 上限放宽到 500 -> \"kp\": {\"min\": 0, \"max\": 500, \"step\": 0.001, \"default\": 2.0}",
]

DEFAULT_PARAM_LIMITS: Dict[str, Dict[str, float]] = {
    "kp": {"min": 0.0, "max": 100.0, "step": 0.001, "default": 2.0},
    "ki": {"min": 0.0, "max": 100.0, "step": 0.001, "default": 0.5},
    "kd": {"min": 0.0, "max": 100.0, "step": 0.001, "default": 0.1},
    "sp": {"min": -1000.0, "max": 1000.0, "step": 0.01, "default": 25.0},
}

DEFAULT_CONFIG: Dict = {
    "schema": SCHEMA_NAME,
    "version": CONFIG_VERSION,
    "_说明": _DOC_LINES,
    "param_limits": DEFAULT_PARAM_LIMITS,
}


def default_config_path() -> str:
    """返回默认配置文件路径；``SERIALPID_CONFIG`` 存在时优先。"""
    env = os.environ.get("SERIALPID_CONFIG")
    if env:
        return env
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = os.environ["APPDATA"]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = xdg or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "SerialPID-Tuner", "config.json")


def _coerce_limits(key: str, raw: object) -> Tuple[Optional[Dict[str, float]], Optional[str]]:
    """校验单个参数的范围字典；非法时返回 (None, 原因)。"""
    fields = ("min", "max", "step", "default")
    if not isinstance(raw, dict):
        return None, f"{key} 范围配置不是对象，已用默认值"
    try:
        values = {f: float(raw[f]) for f in fields}
    except (KeyError, TypeError, ValueError):
        return None, f"{key} 范围配置缺少合法数值字段，已用默认值"
    for f in fields:
        if not math.isfinite(values[f]):
            return None, f"{key}.{f} 不是有限数值，已用默认值"
    if values["min"] > values["max"]:
        return None, f"{key} min({values['min']:g}) > max({values['max']:g})，已用默认值"
    if values["step"] <= 0.0:
        return None, f"{key}.step({values['step']:g}) 必须为正数，已用默认值"
    lo, hi, default = values["min"], values["max"], values["default"]
    if not (lo <= default <= hi):
        values["default"] = lo
    return values, None


def load_config(path: Optional[str] = None, *, strict: bool = False) -> Tuple[Dict, Optional[str]]:
    """读取应用配置。

    返回 ``(config, warning)``：``warning`` 为文件缺失 / 损坏 / 字段非法时
    的非致命说明（正常情况为 ``None``）。``strict=True`` 时损坏文件直接抛
    ``ValueError``（供调用方决定自己的恢复策略，如预设管理器）。
    """
    cfg_path = path if path is not None else default_config_path()
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["param_limits"] = json.loads(json.dumps(DEFAULT_PARAM_LIMITS))
    warnings: list = []
    payload = None

    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            if strict:
                raise ValueError(f"配置文件损坏：{cfg_path}: {exc}") from exc
            backup = _backup_corrupt(cfg_path)
            warnings.append(f"配置文件损坏（{exc}），已备份为 {backup}，使用默认配置")
    else:
        try:
            save_config(config, cfg_path)
        except OSError as exc:  # 只读目录 / 权限不足：不阻塞启动
            warnings.append(f"无法写入默认配置 {cfg_path}: {exc}")
        return config, (warnings[0] if warnings else None)

    if not isinstance(payload, dict):
        if strict:
            raise ValueError(f"配置文件内容不是对象：{cfg_path}")
        backup = _backup_corrupt(cfg_path)
        warnings.append(f"配置文件内容不是对象，已备份为 {backup}，使用默认配置")
        return config, warnings[0]

    raw_limits = payload.get("param_limits", {})
    if not isinstance(raw_limits, dict):
        warnings.append("param_limits 不是对象，已使用默认范围")
    else:
        for key in DEFAULT_PARAM_LIMITS:
            if key not in raw_limits:
                continue
            limits, reason = _coerce_limits(key, raw_limits[key])
            if limits is not None:
                config["param_limits"][key] = limits
            else:
                warnings.append(reason)

    if payload.get("schema") not in (None, SCHEMA_NAME):
        warnings.append(f"未知配置 schema {payload.get('schema')!r}，已按兼容方式读取")
    return config, (warnings[0] if warnings else None)


def save_config(config: Dict, path: Optional[str] = None) -> None:
    """原子写配置（临时文件 + os.replace），中途失败不留半截 JSON。"""
    cfg_path = path if path is not None else default_config_path()
    directory = os.path.dirname(cfg_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{cfg_path}.tmp-{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _backup_corrupt(cfg_path: str) -> str:
    backup = f"{cfg_path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}.json"
    try:
        os.replace(cfg_path, backup)
    except OSError:
        backup = cfg_path
    return backup
