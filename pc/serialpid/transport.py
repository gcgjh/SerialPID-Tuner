"""传输层抽象：实物串口与 TCP 模拟器实现同一接口。

协议帧格式与传输通道无关；通信层只面对本接口，便于无硬件时用 TCP 闭环测试。
"""

from __future__ import annotations

import abc
import socket
from typing import List, Tuple

try:  # pyserial 为运行依赖，测试环境缺失时延迟报错
    import serial  # type: ignore
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover - 依赖缺失场景
    serial = None


class TransportError(Exception):
    """传输层打开/读写失败。"""


class PeerClosed(TransportError):
    """对端主动关闭了连接（TCP 收到正常的 FIN，而不是超时或链路中断）。

    单独立一个类型是为了让上层把"对端正常退出"与"链路故障"分开提示：
    前者该确认对方程序是否还在跑，后者才该去查线、查驱动。
    """


class Transport(abc.ABC):
    """统一收发接口；read 须支持超时返回 0 字节（线程循环可退出）。

    ``read`` 允许抛异常表示链路已不可用：:class:`PeerClosed` 表示对端正常关闭，
    其它 ``OSError`` 由通信层按 errno / 系统提示归类（设备拔出 / 驱动异常）。
    """

    @abc.abstractmethod
    def open(self, target: str, baud: int = 115200) -> None: ...

    @abc.abstractmethod
    def read(self, n: int = 1024) -> bytes: ...

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def is_open(self) -> bool: ...


def list_serial_ports() -> List[str]:
    """返回系统串口号列表（如 COM3）；pyserial 缺失时返回空表。"""
    if serial is None:
        return []
    return [p.device for p in _list_ports.comports()]


class SerialTransport(Transport):
    """实物 UART 传输。"""

    def __init__(self) -> None:
        self._ser = None

    def open(self, target: str, baud: int = 115200) -> None:
        if serial is None:
            raise TransportError("pyserial 未安装")
        try:
            self._ser = serial.Serial(
                port=target, baudrate=baud, bytesize=8, parity="N",
                stopbits=1, timeout=0.1,
            )
        except Exception as exc:  # noqa: BLE001 - 统一包装为传输错误
            raise TransportError(f"串口打开失败 {target}: {exc}") from exc

    def read(self, n: int = 1024) -> bytes:
        if self._ser is None:
            return b""
        return self._ser.read(n) or b""

    def write(self, data: bytes) -> None:
        if self._ser is None:
            raise TransportError("串口未打开")
        self._ser.write(data)

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open


class TcpTransport(Transport):
    """TCP 传输，用于连接 fake_mcu 模拟器或测试桩。

    target 格式：`host:port`，如 `127.0.0.1:9000`。
    """

    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def open(self, target: str, baud: int = 115200) -> None:
        try:
            host, port_str = target.rsplit(":", 1)
            self._sock = socket.create_connection((host, int(port_str)), timeout=1.0)
            self._sock.settimeout(0.1)
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"TCP 连接失败 {target}: {exc}") from exc

    def read(self, n: int = 1024) -> bytes:
        if self._sock is None:
            return b""
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b""
        except OSError as exc:
            self.close()
            raise OSError(f"TCP 读失败: {exc}") from exc
        if not data:
            # recv 返回空字节 = 对端发了 FIN（正常关闭），不是"暂时没数据"。
            # 与超时区分开，上层才能给出"对端程序已退出"这种准确提示。
            self.close()
            raise PeerClosed("对端关闭了 TCP 连接")
        return data

    def write(self, data: bytes) -> None:
        if self._sock is None:
            raise TransportError("TCP 未连接")
        self._sock.sendall(data)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def is_open(self) -> bool:
        return self._sock is not None
