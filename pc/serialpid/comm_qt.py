"""CommCore 的 Qt 适配：把通信线程回调转成队列信号，供 UI 线程安全接收。

链路统计与帧日志挂在核心上（而不是信号里逐帧推给界面）：界面按自己的节奏
拉快照（1 Hz 足够），帧日志按需导出/回放，避免 20 Hz 遥测再叠加一路信号开销。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from .comm import CommCore
from .frame_trace import DEFAULT_CAPACITY, FrameTrace
from .link_stats import LinkSnapshot, LinkStats


class CommWorker(QObject):
    frameReceived = Signal(int, bytes)
    connectionChanged = Signal(bool, str)
    errorOccurred = Signal(str)

    def __init__(
        self,
        parent=None,
        *,
        heartbeat_interval: float = 0.0,
        heartbeat_max_missed: int = 3,
        idle_timeout: float = 1.0,
        trace_capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        super().__init__(parent)
        self._trace = FrameTrace(capacity=trace_capacity)
        self._stats = LinkStats()
        self._core = CommCore(
            on_frame=lambda t, p: self.frameReceived.emit(t, p),
            on_connect=lambda ok, detail: self.connectionChanged.emit(ok, detail),
            on_error=self.errorOccurred.emit,
            idle_timeout=idle_timeout,
            heartbeat_interval=heartbeat_interval,
            heartbeat_max_missed=heartbeat_max_missed,
            stats=self._stats,
            trace=self._trace,
        )

    def open(self, kind: str, target: str, baud: int = 115200, transport=None) -> bool:
        """``transport`` 给定时走进程内通道（内置模拟器），否则按 kind 现造。"""
        return self._core.open(kind, target, baud, transport=transport)

    def close(self) -> None:
        self._core.close()

    def send(self, pkt_type: int, payload: bytes = b"") -> None:
        self._core.send(pkt_type, payload)

    @property
    def connected(self) -> bool:
        return self._core.connected

    @property
    def heartbeat_interval(self) -> float:
        return self._core.heartbeat_interval

    def stats_snapshot(self) -> LinkSnapshot:
        """当前链路质量快照（界面线程调用，线程安全）。"""
        return self._core.stats_snapshot()

    @property
    def trace(self) -> FrameTrace:
        return self._trace

    def export_trace_text(self) -> str:
        return self._trace.to_text()

    def export_trace_jsonl(self) -> str:
        return self._trace.to_jsonl()
