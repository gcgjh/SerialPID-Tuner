# -*- coding: utf-8 -*-
"""PID 预设参数管理（多组 Kp/Ki/Kd/SP 分组保存与加载）。

存储格式（``presets.json``，UTF-8，无 BOM）::

    {
      "schema": "serialpid.presets",
      "version": 1,
      "updated_at": "2026-11-05T22:20:00+08:00",
      "presets": [
        {"name": "保守起步", "kp": 1.0, "ki": 0.02, "kd": 0.0,
         "sp": 30.0, "note": "…", "updated_at": "2026-11-05T22:20:00+08:00"}
      ]
    }

约定：
    * 预设名不区分大小写唯一，长度 1..32；
    * 增益必须是有限数值且 |值| <= 1e6，SP 可留空（None 表示“沿用当前设定值”）；
    * 任何修改都是“先写盘成功，再改内存”，避免保存失败后内存与磁盘不一致；
    * 写盘为同目录临时文件 + ``os.replace``，断电不会留下半截 JSON。

默认路径：Windows 为 ``%APPDATA%/SerialPID-Tuner/presets.json``，
其它系统为 ``~/.config/serialpid-tuner/presets.json``。
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

SCHEMA_NAME: str = "serialpid.presets"
SCHEMA_VERSION: int = 1
NAME_MAX_LEN: int = 32
NOTE_MAX_LEN: int = 200
GAIN_LIMIT: float = 1e6

_UNSET = object()


class PresetError(ValueError):
    """预设管理异常基类。"""


class PresetNotFoundError(PresetError):
    """指定名称的预设不存在。"""


class PresetExistsError(PresetError):
    """同名预设已存在。"""


class InvalidPresetError(PresetError):
    """预设字段不合法。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_presets_path() -> str:
    """跨平台默认存储路径。"""
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        return os.path.join(appdata, "SerialPID-Tuner", "presets.json")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(xdg, "serialpid-tuner", "presets.json")


def _check_gain(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidPresetError(f"{field_name} 必须是数值，收到 {value!r}") from None
    if not math.isfinite(number):
        raise InvalidPresetError(f"{field_name} 必须是有限数值，收到 {value!r}")
    if abs(number) > GAIN_LIMIT:
        raise InvalidPresetError(f"{field_name} 超出允许范围 |{field_name}| <= {GAIN_LIMIT:g}")
    return number


def _check_name(name: Any) -> str:
    if not isinstance(name, str):
        raise InvalidPresetError(f"预设名必须是字符串，收到 {name!r}")
    cleaned = name.strip()
    if not cleaned:
        raise InvalidPresetError("预设名不能为空")
    if len(cleaned) > NAME_MAX_LEN:
        raise InvalidPresetError(f"预设名最长 {NAME_MAX_LEN} 字符，实际 {len(cleaned)}")
    if any(ch in cleaned for ch in "\r\n\t"):
        raise InvalidPresetError("预设名不能包含换行或制表符")
    return cleaned


def _check_note(note: Any) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise InvalidPresetError(f"备注必须是字符串，收到 {note!r}")
    text = note.strip()
    if len(text) > NOTE_MAX_LEN:
        raise InvalidPresetError(f"备注最长 {NOTE_MAX_LEN} 字符，实际 {len(text)}")
    return text


@dataclass
class Preset:
    """一组 PID 预设。``sp`` 为 None 表示不固定设定值。"""

    name: str
    kp: float
    ki: float
    kd: float
    sp: Optional[float] = None
    note: str = ""
    updated_at: str = field(default_factory=_now_iso)

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kp": self.kp,
            "ki": self.ki,
            "kd": self.kd,
            "sp": self.sp,
            "note": self.note,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Preset":
        if not isinstance(data, dict):
            raise InvalidPresetError(f"预设条目必须是对象，收到 {type(data).__name__}")
        if "name" not in data:
            raise InvalidPresetError("预设条目缺少 name")
        for key in ("kp", "ki", "kd"):
            if key not in data:
                raise InvalidPresetError(f"预设 {data.get('name')!r} 缺少 {key}")
        sp_raw = data.get("sp", None)
        preset = cls(
            name=_check_name(data["name"]),
            kp=_check_gain(data["kp"], "kp"),
            ki=_check_gain(data["ki"], "ki"),
            kd=_check_gain(data["kd"], "kd"),
            sp=None if sp_raw is None else _check_gain(sp_raw, "sp"),
            note=_check_note(data.get("note", "")),
            updated_at=str(data.get("updated_at") or _now_iso()),
        )
        return preset

    def copy(self) -> "Preset":
        return copy.deepcopy(self)

    def validate(self) -> "Preset":
        """就地规范化并校验，返回自身。"""
        self.name = _check_name(self.name)
        self.kp = _check_gain(self.kp, "kp")
        self.ki = _check_gain(self.ki, "ki")
        self.kd = _check_gain(self.kd, "kd")
        self.sp = None if self.sp is None else _check_gain(self.sp, "sp")
        self.note = _check_note(self.note)
        if not self.updated_at:
            self.updated_at = _now_iso()
        return self


def default_presets() -> List[Preset]:
    """首次运行时写入的示例预设（仅为起点，不是整定结果）。"""
    now = _now_iso()
    return [
        Preset(
            name="保守起步", kp=1.0, ki=0.02, kd=0.0, sp=30.0,
            note="先只留 P 观察响应，确认方向正确后再加积分", updated_at=now,
        ),
        Preset(
            name="中速响应", kp=2.0, ki=0.10, kd=0.02, sp=30.0,
            note="通用温控起点；Ki/Kd 量纲取决于固件控制周期，需实机微调", updated_at=now,
        ),
        Preset(
            name="激进整定", kp=5.0, ki=0.50, kd=0.10, sp=30.0,
            note="快速对象用；易振荡，务必配合输出限幅与抗积分饱和", updated_at=now,
        ),
    ]


@dataclass
class ImportResult:
    """``import_from`` 的合并结果。"""

    added: int = 0
    updated: int = 0
    skipped: int = 0


class PresetManager:
    """预设集合的读写与增删改查（线程安全，供 GUI 主线程与后台线程共用）。"""

    def __init__(
        self,
        path: Optional[Union[str, os.PathLike[str]]] = None,
        *,
        strict: bool = False,
        create_defaults: bool = True,
    ) -> None:
        """:param strict: True 时文件损坏直接抛异常；False 时备份坏文件并用默认值继续。"""
        self._path = os.fspath(path) if path is not None else default_presets_path()
        self._lock = threading.RLock()
        self._presets: Dict[str, Preset] = {}  # key = name.casefold()
        self.warning: Optional[str] = None
        self._strict = strict
        self._create_defaults = create_defaults
        self.reload()

    # -- 基本信息 -----------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def __len__(self) -> int:
        with self._lock:
            return len(self._presets)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name.strip().casefold() in self._presets

    # -- 读取 ---------------------------------------------------------------

    def reload(self) -> None:
        """从磁盘重新加载；文件不存在时按 ``create_defaults`` 初始化。"""
        with self._lock:
            if not os.path.exists(self._path):
                presets = default_presets() if self._create_defaults else []
                mapping = {p.name.casefold(): p for p in presets}
                self._presets = mapping
                if self._create_defaults:
                    try:
                        self._write_file(mapping)
                    except PresetError as exc:
                        self.warning = str(exc)
                return

            try:
                mapping, skipped = self._read_file(self._path)
            except PresetError as exc:
                if self._strict:
                    raise
                backup = self._backup_corrupt()
                presets = default_presets() if self._create_defaults else []
                self._presets = {p.name.casefold(): p for p in presets}
                self.warning = f"{exc}；已备份为 {backup}" if backup else str(exc)
                if self._create_defaults:
                    try:
                        self._write_file(self._presets)
                    except PresetError as save_exc:
                        self.warning = f"{self.warning}；重建默认预设失败：{save_exc}"
                return

            self._presets = mapping
            self.warning = (
                f"{self._path} 中有 {skipped} 条预设不合法已跳过" if skipped else None
            )

    def list(self) -> List[Preset]:
        """按写入顺序返回全部预设（返回副本，修改不会影响内部状态）。"""
        with self._lock:
            return [p.copy() for p in self._presets.values()]

    def names(self) -> List[str]:
        with self._lock:
            return [p.name for p in self._presets.values()]

    def get(self, name: str) -> Preset:
        key = _check_name(name).casefold()
        with self._lock:
            preset = self._presets.get(key)
            if preset is None:
                raise PresetNotFoundError(f"预设不存在：{name!r}")
            return preset.copy()

    # -- 写入 ---------------------------------------------------------------

    def add(
        self,
        name: str,
        kp: float,
        ki: float,
        kd: float,
        sp: Optional[float] = None,
        note: str = "",
    ) -> Preset:
        """新增预设；同名（忽略大小写）已存在时抛 ``PresetExistsError``。"""
        preset = Preset(name=name, kp=kp, ki=ki, kd=kd, sp=sp, note=note).validate()
        preset.updated_at = _now_iso()
        with self._lock:
            if preset.name.casefold() in self._presets:
                raise PresetExistsError(f"预设已存在：{preset.name!r}")
            new_map = dict(self._presets)
            new_map[preset.name.casefold()] = preset
            self._commit(new_map)
            return preset.copy()

    def save(self, preset: Preset, *, overwrite: bool = True) -> Preset:
        """整条写入（存在即覆盖）；``overwrite=False`` 时同名抛异常。"""
        cleaned = preset.validate()
        cleaned.updated_at = _now_iso()
        with self._lock:
            key = cleaned.name.casefold()
            if not overwrite and key in self._presets:
                raise PresetExistsError(f"预设已存在：{cleaned.name!r}")
            new_map = dict(self._presets)
            new_map[key] = cleaned
            self._commit(new_map)
            return cleaned.copy()

    def update(
        self,
        name: str,
        *,
        kp: Any = _UNSET,
        ki: Any = _UNSET,
        kd: Any = _UNSET,
        sp: Any = _UNSET,
        note: Any = _UNSET,
    ) -> Preset:
        """局部更新；``sp=None`` 表示清除设定值，因此用哨兵区分“不改”。"""
        with self._lock:
            key = _check_name(name).casefold()
            current = self._presets.get(key)
            if current is None:
                raise PresetNotFoundError(f"预设不存在：{name!r}")
            changes: Dict[str, Any] = {}
            if kp is not _UNSET:
                changes["kp"] = kp
            if ki is not _UNSET:
                changes["ki"] = ki
            if kd is not _UNSET:
                changes["kd"] = kd
            if sp is not _UNSET:
                changes["sp"] = sp
            if note is not _UNSET:
                changes["note"] = note
            updated = replace(current, **changes)
            updated.validate()
            updated.updated_at = _now_iso()
            new_map = dict(self._presets)
            new_map[key] = updated
            self._commit(new_map)
            return updated.copy()

    def delete(self, name: str) -> None:
        with self._lock:
            key = _check_name(name).casefold()
            if key not in self._presets:
                raise PresetNotFoundError(f"预设不存在：{name!r}")
            new_map = dict(self._presets)
            del new_map[key]
            self._commit(new_map)

    def rename(self, old_name: str, new_name: str) -> Preset:
        cleaned_new = _check_name(new_name)
        with self._lock:
            old_key = _check_name(old_name).casefold()
            current = self._presets.get(old_key)
            if current is None:
                raise PresetNotFoundError(f"预设不存在：{old_name!r}")
            new_key = cleaned_new.casefold()
            if new_key != old_key and new_key in self._presets:
                raise PresetExistsError(f"预设已存在：{cleaned_new!r}")
            renamed = replace(current, name=cleaned_new, updated_at=_now_iso())
            new_map = dict(self._presets)
            del new_map[old_key]
            new_map[new_key] = renamed
            self._commit(new_map)
            return renamed.copy()

    def restore_defaults(self, *, clear: bool = False) -> List[Preset]:
        """恢复示例预设；``clear=True`` 先清空现有预设。"""
        with self._lock:
            base = {} if clear else dict(self._presets)
            for preset in default_presets():
                key = preset.name.casefold()
                if key in base:
                    # 已有同名自定义预设时，默认项加后缀避免覆盖用户数据
                    candidate = f"{preset.name} (默认)"
                    serial = 2
                    while candidate.casefold() in base:
                        candidate = f"{preset.name} (默认{serial})"
                        serial += 1
                    preset.name = candidate
                    key = candidate.casefold()
                base[key] = preset
            self._commit(base)
            return [p.copy() for p in base.values()]

    # -- 导入导出 -----------------------------------------------------------

    def export_to(self, path: Union[str, os.PathLike[str]]) -> int:
        """把当前预设另存为 JSON，返回条数（不影响默认存储）。"""
        destination = os.fspath(path)
        parent = os.path.dirname(os.path.abspath(destination))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with self._lock:
            self._write_file(self._presets, destination)
            return len(self._presets)

    def import_from(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        overwrite: bool = False,
    ) -> ImportResult:
        """从 JSON 合并预设；``overwrite=False`` 时同名条目跳过。"""
        mapping, skipped = self._read_file(os.fspath(path))
        result = ImportResult(skipped=skipped)
        with self._lock:
            new_map = dict(self._presets)
            for key, preset in mapping.items():
                if key in new_map:
                    if not overwrite:
                        result.skipped += 1
                        continue
                    new_map[key] = preset
                    result.updated += 1
                else:
                    new_map[key] = preset
                    result.added += 1
            if result.added or result.updated:
                self._commit(new_map)
            return result

    # -- 内部：磁盘 ---------------------------------------------------------

    def _commit(self, new_map: Dict[str, Preset]) -> None:
        """先落盘，成功后才替换内存，保证两者一致。"""
        self._write_file(new_map)
        self._presets = new_map

    def _write_file(
        self,
        mapping: Dict[str, Preset],
        destination: Optional[str] = None,
    ) -> None:
        target = destination or self._path
        payload = {
            "schema": SCHEMA_NAME,
            "version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "presets": [p.to_dict() for p in mapping.values()],
        }
        tmp_path = f"{target}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except OSError as exc:
            raise PresetError(f"保存预设失败：{target}（{exc}）") from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _read_file(self, path: str) -> Tuple[Dict[str, Preset], int]:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise PresetError(f"预设文件不存在：{path}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise PresetError(f"读取预设文件失败：{path}（{exc}）") from exc

        if not isinstance(data, dict):
            raise PresetError(f"预设文件格式错误：顶层应为对象，实际 {type(data).__name__}")
        if data.get("schema") not in (None, SCHEMA_NAME):
            raise PresetError(f"预设文件 schema 不匹配：{data.get('schema')!r}")
        entries = data.get("presets")
        if not isinstance(entries, list):
            raise PresetError("预设文件格式错误：缺少 presets 数组")

        mapping: Dict[str, Preset] = {}
        skipped = 0
        for entry in entries:
            try:
                preset = Preset.from_dict(entry)
            except InvalidPresetError:
                skipped += 1
                continue
            key = preset.name.casefold()
            if key in mapping:
                skipped += 1
                continue
            mapping[key] = preset
        return mapping, skipped

    def _backup_corrupt(self) -> Optional[str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base, ext = os.path.splitext(self._path)
        backup = f"{base}.corrupt-{stamp}{ext or '.json'}"
        try:
            shutil.copy2(self._path, backup)
            return backup
        except OSError:
            return None

    def __repr__(self) -> str:  # pragma: no cover - 调试显示
        return f"PresetManager(path={self._path!r}, count={len(self)})"
