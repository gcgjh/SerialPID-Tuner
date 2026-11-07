"""进程内仿真传输：内存队列代替串口/TCP，对上仍实现 ``Transport`` 接口。

设计约束（见 cards_sim 卡23）：

* ``read`` 必须"等不到就返回 0 字节"，绝不能阻塞通信线程的接收循环；
* ``write`` 只把字节投给引擎，不做任何等待；
* ``close`` 可重入、幂等，断开后立刻重连不能残留上一轮数据。

线程安全：上位机侧（通信线程）读写 ``read/write/close``，引擎侧（仿真线程）
调用 ``take_inbound`` / ``emit``。两侧共用一把 ``_cv`` 保护输出缓冲，
输入侧用 ``queue.Queue`` 自带锁。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

from ..transport import Transport, TransportError

#: 单次 ``read`` 最多阻塞的秒数（超时返回 b""，供上层做断流判定）
DEFAULT_READ_TIMEOUT_S = 0.05

#: 输出缓冲上限，防止上位机不读时无限增长（超过即丢最旧数据并计数）
MAX_OUT_BYTES = 256 * 1024

#: 输入队列里的关闭哨兵
_CLOSED = object()


class SimTransport(Transport):
    """进程内通道：上位机写入的字节进 ``_inbox``，引擎发出的字节进 ``_buffer``。"""

    def __init__(self, read_timeout: float = DEFAULT_READ_TIMEOUT_S) -> None:
        self._read_timeout = float(read_timeout)
        self._inbox: "queue.Queue[object]" = queue.Queue()
        self._buffer = bytearray()
        self._cv = threading.Condition()
        self._open = False
        self._lock = threading.Lock()
        # 统计（测试与界面观测用）
        self.bytes_from_host = 0
        self.bytes_to_host = 0
        self.dropped_bytes = 0
        self.read_calls = 0
        self.empty_reads = 0

    # ---- 传输接口 -------------------------------------------------------- #

    def open(self, target: str = "sim", baud: int = 115200) -> None:
        """打开通道。``target``/``baud`` 仅为接口对齐，仿真通道忽略二者。"""
        with self._lock:
            self._clear_inbox()
            with self._cv:
                self._buffer.clear()
                self._open = True
        self.bytes_from_host = self.bytes_to_host = self.dropped_bytes = 0
        self.read_calls = self.empty_reads = 0

    def read(self, n: int = 1024) -> bytes:
        """取出引擎发出的字节；``read_timeout`` 内无数据返回 b""。"""
        deadline = time.monotonic() + self._read_timeout
        with self._cv:
            self.read_calls += 1
            while True:
                if not self._open:
                    return b""
                if self._buffer:
                    data = bytes(self._buffer[:n])
                    del self._buffer[:n]
                    return data
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.empty_reads += 1
                    return b""
                self._cv.wait(remaining)

    def write(self, data: bytes) -> None:
        """上位机 -> 引擎。打开状态下入队，不阻塞。"""
        with self._lock:
            if not self._open:
                raise TransportError("仿真通道未打开")
            payload = bytes(data)
            self._inbox.put(payload)
            self.bytes_from_host += len(payload)

    def close(self) -> None:
        """关闭通道：置位、清空双向缓冲、唤醒阻塞的读。可重复调用。"""
        with self._lock:
            was_open = self._open
            self._open = False
            self._clear_inbox()
            with self._cv:
                self._buffer.clear()
                self._cv.notify_all()
            if was_open:
                self._inbox.put(_CLOSED)

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    # ---- 引擎侧接口 ------------------------------------------------------ #

    def take_inbound(self, timeout: float = 0.0) -> bytes:
        """取走上位机写入的字节；``timeout`` 内没有则返回 b""。"""
        try:
            chunk = self._inbox.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return b""
        if chunk is _CLOSED:
            return b""
        return chunk  # type: ignore[return-value]

    def emit(self, data: bytes) -> bool:
        """引擎 -> 上位机。通道已关闭时丢弃并返回 False。"""
        payload = bytes(data)
        with self._cv:
            if not self._open:
                return False
            self._buffer.extend(payload)
            overflow = len(self._buffer) - MAX_OUT_BYTES
            if overflow > 0:
                del self._buffer[:overflow]
                self.dropped_bytes += overflow
            self.bytes_to_host += len(payload)
            self._cv.notify_all()
            return True

    # ---- 观测 ------------------------------------------------------------ #

    @property
    def pending_out_bytes(self) -> int:
        """尚未被上位机读走的字节数。"""
        with self._cv:
            return len(self._buffer)

    @property
    def pending_in_bytes(self) -> int:
        return self._inbox.qsize()

    def statistics(self) -> dict:
        with self._cv:
            buffered = len(self._buffer)
        return {
            "open": self.is_open(),
            "bytes_from_host": self.bytes_from_host,
            "bytes_to_host": self.bytes_to_host,
            "buffered": buffered,
            "dropped": self.dropped_bytes,
            "read_calls": self.read_calls,
            "empty_reads": self.empty_reads,
        }

    def _clear_inbox(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return


def drain_queue(transport: SimTransport, limit: int = 64) -> list[bytes]:
    """把 ``_inbox`` 里的剩余片段一次性取空（调试/测试用）。"""
    chunks: list[bytes] = []
    while len(chunks) < limit:
        chunk = transport.take_inbound(0.0)
        if not chunk:
            break
        chunks.append(chunk)
    return chunks
