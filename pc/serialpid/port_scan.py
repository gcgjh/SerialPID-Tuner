"""串口探测与诊断（card21）。

设计目标：把"枚举不到"这件事拆成**三种可区分状态**，而不是一律返回空列表。

    ok      枚举成功，至少一个设备
    empty   枚举成功，但系统里确实没有串口设备
    error   枚举过程本身失败（pyserial 缺失、平台后端导入失败、驱动异常、超时…）

旧实现（``transport.list_serial_ports``）只返回 ``List[str]``，失败和空表长得
一模一样；GUI 再用 ``ports or ["COM1"]`` 补一个假占位，用户彻底分不清"没插"
和"坏了"。本模块把 pyserial 提供的描述/硬件 ID/厂商/序列号全部保留下来，
并把异常类型与消息带回去，供界面如实提示（界面接线归 card25，本模块不碰 Qt）。

对外 API（card25 可直接用）：

    scan_ports(timeout_s=2.0) -> PortScanResult
    diagnose(result=None, timeout_s=2.0) -> str      可复制的诊断文本

``PortScanResult.status`` 取 ``"ok" / "empty" / "error"``；``summary`` 是一句人话，
``hint`` 是给用户的下一步建议。模块不依赖 GUI，可在纯 Python 环境单测。
"""

from __future__ import annotations

import os
import platform
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence

try:  # pyserial 是运行依赖；测试环境缺失时也要能给出"依赖缺失"这个状态
    import serial  # type: ignore
    from serial.tools import list_ports as _list_ports  # type: ignore
except ImportError:  # pragma: no cover - 由单测用 monkeypatch 覆盖
    serial = None  # type: ignore[assignment]
    _list_ports = None  # type: ignore[assignment]

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"

#: 默认枚举上限。正常 Windows 枚举在几毫秒内完成；虚拟串口驱动偶尔会卡住，
#: 超过这个时间就按"枚举超时"上报，绝不把界面线程拖死。
DEFAULT_SCAN_TIMEOUT_S = 2.0

_DEVICE_DIGITS = re.compile(r"(\d+)")
_EMPTY_WORDS = {"", "n/a", "na", "none", "unknown", "null"}


@dataclass(frozen=True)
class PortInfo:
    """一个串口设备的完整信息（字段缺失时为空串，不抛异常）。"""

    device: str
    description: str = ""
    hwid: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None

    @property
    def display_name(self) -> str:
        """给下拉框用的一行名称：设备名 + 描述/厂商（没有描述就只显示设备名）。

        某些驱动（如 CH340）会把设备名塞进描述里（``USB 串行设备 (COM3)``），
        直接拼会变成 ``COM3 · USB 串行设备 (COM3)`` 这种重复，先清掉。
        """
        desc = self.description
        if self.device:
            desc = desc.replace(f"({self.device})", "").replace(self.device, "")
        desc = desc.strip(" -·")
        parts = [self.device]
        for extra in (desc, self.manufacturer):
            if extra and extra.lower() != self.device.lower() and extra not in parts:
                parts.append(extra)
        return " · ".join(parts)

    @property
    def vid_pid(self) -> str:
        if self.vid is None or self.pid is None:
            return ""
        return f"{self.vid:04X}:{self.pid:04X}"

    def detail_line(self) -> str:
        bits = [self.device]
        if self.description:
            bits.append(self.description)
        if self.manufacturer:
            bits.append(f"厂商 {self.manufacturer}")
        if self.vid_pid:
            bits.append(f"VID:PID {self.vid_pid}")
        if self.hwid:
            bits.append(f"HWID {self.hwid}")
        if self.serial_number:
            bits.append(f"SN {self.serial_number}")
        return " | ".join(bits)


@dataclass
class PortScanResult:
    """一次端口枚举的完整结果（成功 / 空 / 失败三态）。"""

    status: str
    ports: List[PortInfo] = field(default_factory=list)
    error_type: str = ""
    error_message: str = ""
    elapsed_ms: float = 0.0
    pyserial_version: str = ""
    platform_name: str = ""
    backend_available: bool = False
    backend_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def is_empty(self) -> bool:
        return self.status == STATUS_EMPTY

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR

    @property
    def summary(self) -> str:
        if self.ok:
            return f"发现 {len(self.ports)} 个串口设备"
        if self.is_empty:
            return "未发现串口设备"
        return f"端口枚举失败（{self.error_type or '未知异常'}）"

    @property
    def hint(self) -> str:
        if self.ok:
            return "选择目标端口后点「连接」"
        if self.is_empty:
            return "请检查 USB 线与驱动是否正常，或改用「内置模拟器」离线体验"
        return f"{self.error_message or '枚举异常'}；可复制诊断文本反馈给开发者"


def _clean_text(value: object) -> str:
    """把 pyserial 可能给出的 None / 'n/a' / 乱码空值统一成空串。"""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in _EMPTY_WORDS else text


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _device_sort_key(info: PortInfo) -> tuple:
    """COM2 排在 COM10 前面（按数字段比较，而不是字典序）。"""
    key: list[tuple[int, object]] = []
    for chunk in _DEVICE_DIGITS.split(info.device):
        if chunk.isdigit():
            key.append((0, int(chunk)))
        elif chunk:
            key.append((1, chunk.lower()))
    return tuple(key)


def _enumerate_serial_ports() -> Sequence[object]:
    """真正调用 pyserial；pyserial 缺失时抛 ImportError 交给上层如实上报。"""
    if _list_ports is None:
        raise ImportError("pyserial 未安装或 serial.tools.list_ports 不可用")
    return list(_list_ports.comports())


def _coerce_ports(raw: object) -> List[PortInfo]:
    """把 pyserial 的 ListPortInfo 列表转成 PortInfo，容忍字段为空/类型异常。"""
    if not raw:
        return []
    ports: List[PortInfo] = []
    seen: set[str] = set()
    for item in raw:  # type: ignore[union-attr]
        device = _clean_text(getattr(item, "device", ""))
        if not device or device.casefold() in seen:
            continue
        seen.add(device.casefold())
        ports.append(
            PortInfo(
                device=device,
                description=_clean_text(getattr(item, "description", "")),
                hwid=_clean_text(getattr(item, "hwid", "")),
                manufacturer=_clean_text(getattr(item, "manufacturer", "")),
                serial_number=_clean_text(getattr(item, "serial_number", "")),
                vid=_as_int(getattr(item, "vid", None)),
                pid=_as_int(getattr(item, "pid", None)),
            )
        )
    ports.sort(key=_device_sort_key)
    return ports


def _pyserial_version() -> str:
    if serial is None:
        return "未安装"
    return str(getattr(serial, "__version__", None) or getattr(serial, "VERSION", "未知"))


def _backend_probe() -> tuple[bool, str]:
    """确认平台串口后端模块能导入——枚举失败时这是关键的分诊信息。"""
    if serial is None:
        return False, "pyserial 未安装"
    module = "serial.serialwin32" if os.name == "nt" else "serial.serialposix"
    try:
        __import__(module)
    except Exception as exc:  # noqa: BLE001 - 这里就是要报告异常本身
        return False, f"{module} 导入失败：{type(exc).__name__}: {exc}"
    return True, f"{module} 可用"


def scan_ports(timeout_s: float = DEFAULT_SCAN_TIMEOUT_S) -> PortScanResult:
    """枚举串口，带超时保护；任何异常都变成 ``status="error"`` 返回，不向上抛。"""
    try:
        limit = max(float(timeout_s), 0.01)
    except (TypeError, ValueError):
        limit = DEFAULT_SCAN_TIMEOUT_S

    box: dict[str, object] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            box["ports"] = _enumerate_serial_ports()
        except Exception as exc:  # noqa: BLE001 - 失败要带类型和消息回去
            box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=_worker, name="port-scan", daemon=True)
    started = time.monotonic()
    thread.start()
    finished = done.wait(limit)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    version = _pyserial_version()
    backend_ok, backend_detail = _backend_probe()
    common = dict(
        elapsed_ms=round(elapsed_ms, 2),
        pyserial_version=version,
        platform_name=platform.platform(),
        backend_available=backend_ok,
        backend_detail=backend_detail,
    )

    if not finished:
        return PortScanResult(
            status=STATUS_ERROR,
            error_type="TimeoutError",
            error_message=f"端口枚举超过 {limit:.2f}s 未返回（可能是虚拟串口驱动卡住）",
            **common,
        )

    error = box.get("error")
    if error is not None:
        return PortScanResult(
            status=STATUS_ERROR,
            error_type=type(error).__name__,
            error_message=str(error) or repr(error),
            **common,
        )

    try:
        ports = _coerce_ports(box.get("ports"))
    except Exception as exc:  # noqa: BLE001 - 字段异常也不能让界面崩
        return PortScanResult(
            status=STATUS_ERROR,
            error_type=type(exc).__name__,
            error_message=f"端口信息解析失败：{exc}",
            **common,
        )

    return PortScanResult(
        status=STATUS_OK if ports else STATUS_EMPTY,
        ports=ports,
        **common,
    )


def diagnose(result: Optional[PortScanResult] = None, timeout_s: float = DEFAULT_SCAN_TIMEOUT_S) -> str:
    """生成一段可复制粘贴的诊断文本（不包含用户目录等隐私路径）。"""
    scan = result or scan_ports(timeout_s)
    lines = [
        "SerialPID-Tuner 串口诊断",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"平台：{scan.platform_name or platform.platform()}",
        f"Python：{platform.python_version()}",
        f"pyserial：{scan.pyserial_version or _pyserial_version()}",
        f"串口后端：{scan.backend_detail or _backend_probe()[1]}",
        f"枚举状态：{scan.status}（{scan.summary}）",
        f"枚举耗时：{scan.elapsed_ms:.2f} ms",
        f"设备数量：{len(scan.ports)}",
    ]
    if scan.is_error:
        lines.append(f"异常类型：{scan.error_type}")
        lines.append(f"异常消息：{scan.error_message}")
    if scan.ports:
        lines.append("设备明细：")
        for index, port in enumerate(scan.ports, 1):
            lines.append(f"  {index}. {port.detail_line()}")
    else:
        lines.append("设备明细：无")
    lines.append(f"建议：{scan.hint}")
    return "\n".join(lines)


__all__ = [
    "STATUS_OK",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "DEFAULT_SCAN_TIMEOUT_S",
    "PortInfo",
    "PortScanResult",
    "scan_ports",
    "diagnose",
]
