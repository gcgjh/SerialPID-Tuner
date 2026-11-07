# -*- coding: utf-8 -*-
"""历史遥测曲线导出 CSV（供 Origin / Excel / Python 分析）。

与 GUI 的约定（见 docs/TASKS.md）：首行必须是表头，默认列顺序
``t_ms,SP,PV,OUT,err``；默认编码 UTF-8 **带 BOM**（Excel 双击不乱码）。

样本来源可以是：
    * ``TelemetrySample`` 之类对象（属性 ``t_ms/sp/pv/out/err``，大写 ``SP/PV/OUT`` 亦识别）；
    * 协议解包后的元组 ``(t_ms, SP, PV, OUT, err)``；
    * 字典（键名同上）。

示例::

    from serialpid.csv_export import export_csv
    export_csv(buffer.samples(), "run_20261105.csv",
               metadata={"port": "COM3", "baud": 115200, "Kp": 2.0})

    # 流式落盘（长时间记录，边收边写）
    with CsvWriter("long_run.csv") as w:
        for sample in stream:
            w.write(sample)
"""

from __future__ import annotations

import csv
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# 协议字段顺序，也是默认表头顺序
DEFAULT_FIELDS: Tuple[str, ...] = ("t_ms", "SP", "PV", "OUT", "err")

# 每个标准列可接受的别名（按优先级）；兼容对象属性、字典键与不同命名习惯
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "t_ms": ("t_ms", "tms", "time_ms", "timestamp", "T"),
    "SP": ("SP", "sp", "setpoint", "target"),
    "PV": ("PV", "pv", "measured", "value"),
    "OUT": ("OUT", "out", "output", "u"),
    "err": ("err", "error", "e"),
}

_FIELD_INDEX: Dict[str, int] = {name: i for i, name in enumerate(DEFAULT_FIELDS)}

_U32_WRAP: int = 1 << 32


class CsvExportError(ValueError):
    """导出失败或样本数据不合法。"""


# ---------------------------------------------------------------------------
# 取值与格式化
# ---------------------------------------------------------------------------


def _extract(sample: Any, field_name: str) -> Any:
    """按字段名从对象 / 字典 / 元组中取值。"""
    aliases = _ALIASES[field_name]
    if isinstance(sample, Mapping):
        for key in aliases:
            if key in sample:
                return sample[key]
    elif isinstance(sample, (tuple, list)):
        index = _FIELD_INDEX[field_name]
        if len(sample) <= index:
            raise CsvExportError(
                f"样本元组至少需要 {len(DEFAULT_FIELDS)} 个元素，实际 {len(sample)} 个"
            )
        return sample[index]
    else:
        for attr in aliases:
            if hasattr(sample, attr):
                return getattr(sample, attr)
    raise CsvExportError(
        f"样本缺少字段 {field_name}（可用别名：{', '.join(aliases)}）"
    )


def _format_float(value: Any, fmt: str) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CsvExportError(f"字段值不是数值：{value!r}") from None
    if not math.isfinite(number):
        # NaN/Inf 写成空单元格，避免污染 Origin 拟合与 pandas 统计
        return ""
    return fmt.format(number)


def _format_int(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        raise CsvExportError(f"t_ms 不是整数：{value!r}") from None


class _ElapsedClock:
    """把 MCU 的 u32 毫秒时间戳换算成单调递增的相对秒数（处理回绕）。"""

    def __init__(self) -> None:
        self._base: Optional[int] = None
        self._prev: Optional[int] = None
        self._offset: int = 0

    def seconds(self, tick_ms: Any) -> Optional[float]:
        if tick_ms is None:
            return None
        try:
            tick = int(tick_ms)
        except (TypeError, ValueError):
            raise CsvExportError(f"t_ms 不是整数：{tick_ms!r}") from None
        if self._base is None:
            self._base = tick
            self._prev = tick
        elif self._prev is not None and tick < self._prev:
            # 只有“倒退”接近半个 u32 才认定为回绕，忽略偶发乱序小抖动
            if (self._prev - tick) > 0x7FFFFFFF:
                self._offset += _U32_WRAP
        self._prev = tick
        return (tick + self._offset - self._base) / 1000.0


# ---------------------------------------------------------------------------
# 列构造
# ---------------------------------------------------------------------------


def _resolve_fields(fields: Iterable[str], derive_elapsed: bool) -> List[str]:
    names = list(fields)
    unknown = [n for n in names if n not in _ALIASES]
    if unknown:
        raise CsvExportError(
            f"未知列名：{', '.join(unknown)}；可选：{', '.join(DEFAULT_FIELDS)}"
        )
    if derive_elapsed:
        # elapsed_s 紧跟 t_ms，便于 Origin 直接当 X 轴
        if "t_ms" in names:
            names.insert(names.index("t_ms") + 1, "elapsed_s")
        else:
            names.insert(0, "elapsed_s")
    return names


def _row(
    sample: Any,
    columns: Sequence[str],
    derive_elapsed: bool,
    clock: _ElapsedClock,
    float_fmt: str,
) -> List[str]:
    row: List[str] = []
    for column in columns:
        if column == "elapsed_s":
            seconds = clock.seconds(_extract(sample, "t_ms"))
            row.append("" if seconds is None else float_fmt.format(seconds))
        elif column == "t_ms":
            row.append(_format_int(_extract(sample, "t_ms")))
        else:
            row.append(_format_float(_extract(sample, column), float_fmt))
    return row


def _metadata_lines(metadata: Optional[Mapping[str, Any]]) -> List[str]:
    if not metadata:
        return []
    lines = []
    for key, value in metadata.items():
        if value is None:
            continue
        text = str(value).replace("\r", " ").replace("\n", " ")
        lines.append(f"# {key}={text}")
    return lines


# ---------------------------------------------------------------------------
# 一次性导出
# ---------------------------------------------------------------------------


def export_csv(
    samples: Iterable[Any],
    path: Union[str, os.PathLike[str]],
    *,
    fields: Iterable[str] = DEFAULT_FIELDS,
    metadata: Optional[Mapping[str, Any]] = None,
    comment_meta: bool = False,
    derive_elapsed: bool = False,
    encoding: str = "utf-8-sig",
    delimiter: str = ",",
    float_fmt: str = "{:.6f}",
    atomic: bool = True,
) -> int:
    """把样本序列写成 CSV，返回写入的数据行数（不含表头）。

    :param fields: 列顺序，默认 ``t_ms,SP,PV,OUT,err``。
    :param metadata: 元数据字典；``comment_meta=True`` 时写为 ``# key=value`` 注释行
        （默认关闭，保证**首行是表头**）。
    :param derive_elapsed: 追加相对时间列 ``elapsed_s``（自动处理 u32 回绕）。
    :param encoding: 默认 ``utf-8-sig``（Excel 直开）；Origin/Python 若不想要 BOM 可传 ``utf-8``。
    :param atomic: 先写同目录临时文件再替换，避免写一半留下坏文件。
    :raises CsvExportError: 样本缺字段、数值非法或文件写入失败。
    """
    destination = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(destination))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    columns = _resolve_fields(fields, derive_elapsed)
    clock = _ElapsedClock()
    rows = 0

    def _write(target: str) -> int:
        written = 0
        with open(target, "w", encoding=encoding, newline="") as handle:
            writer = csv.writer(handle, delimiter=delimiter)
            for line in _metadata_lines(metadata if comment_meta else None):
                handle.write(line + "\n")
            writer.writerow(columns)
            for index, sample in enumerate(samples):
                try:
                    writer.writerow(
                        _row(sample, columns, derive_elapsed, clock, float_fmt)
                    )
                except CsvExportError as exc:
                    raise CsvExportError(f"第 {index + 1} 条样本导出失败：{exc}") from exc
                written += 1
        return written

    if not atomic:
        try:
            return _write(destination)
        except OSError as exc:
            raise CsvExportError(f"写入 CSV 失败：{destination}（{exc}）") from exc

    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".serialpid-", suffix=".csv.tmp", dir=parent or None
        )
        os.close(fd)  # 交给 open() 以指定编码重写
        rows = _write(tmp_path)
        os.replace(tmp_path, destination)
    except OSError as exc:
        raise CsvExportError(f"写入 CSV 失败：{destination}（{exc}）") from exc
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return rows


# ---------------------------------------------------------------------------
# 流式写入（长时间记录）
# ---------------------------------------------------------------------------


class CsvWriter:
    """边收边写的 CSV 写入器（通信线程或记录线程内使用）。

    默认 ``flush_every=20``：20 Hz 遥测下约每秒落盘一次，兼顾实时可查与开销。
    """

    def __init__(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        fields: Iterable[str] = DEFAULT_FIELDS,
        metadata: Optional[Mapping[str, Any]] = None,
        comment_meta: bool = False,
        derive_elapsed: bool = False,
        encoding: str = "utf-8-sig",
        delimiter: str = ",",
        float_fmt: str = "{:.6f}",
        flush_every: int = 20,
        append: bool = False,
    ) -> None:
        self._path = os.fspath(path)
        self._columns = _resolve_fields(fields, derive_elapsed)
        self._metadata = metadata
        self._comment_meta = comment_meta
        self._derive_elapsed = derive_elapsed
        self._encoding = encoding
        self._delimiter = delimiter
        self._float_fmt = float_fmt
        self._flush_every = max(int(flush_every), 1)
        self._append = append
        self._clock = _ElapsedClock()
        self._handle: Optional[Any] = None
        self._writer: Optional[Any] = None
        self._rows = 0

    # -- 生命周期 -----------------------------------------------------------

    def open(self) -> "CsvWriter":
        if self._handle is not None:
            return self
        parent = os.path.dirname(os.path.abspath(self._path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        need_header = True
        if self._append and os.path.exists(self._path) and os.path.getsize(self._path) > 0:
            need_header = False
        try:
            self._handle = open(
                self._path, "a" if self._append else "w",
                encoding=self._encoding, newline="",
            )
        except OSError as exc:
            raise CsvExportError(f"打开 CSV 失败：{self._path}（{exc}）") from exc
        self._writer = csv.writer(self._handle, delimiter=self._delimiter)
        if need_header:
            for line in _metadata_lines(self._metadata if self._comment_meta else None):
                self._handle.write(line + "\n")
            self._writer.writerow(self._columns)
            self._handle.flush()
        return self

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
            finally:
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self) -> "CsvWriter":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- 写入 ---------------------------------------------------------------

    def write(self, sample: Any) -> None:
        """写入一条样本；首次调用会自动打开文件并写表头。"""
        if self._handle is None:
            self.open()
        assert self._writer is not None
        try:
            self._writer.writerow(
                _row(sample, self._columns, self._derive_elapsed, self._clock, self._float_fmt)
            )
        except CsvExportError as exc:
            raise CsvExportError(f"第 {self._rows + 1} 条样本写入失败：{exc}") from exc
        self._rows += 1
        if self._rows % self._flush_every == 0:
            self._handle.flush()

    def write_many(self, samples: Iterable[Any]) -> int:
        for sample in samples:
            self.write(sample)
        return self._rows

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def path(self) -> str:
        return self._path
