"""CommCore 的 Qt 适配：把通信线程回调转成队列信号，供 UI 线程安全接收。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from .comm import CommCore


class CommWorker(QObject):
    frameReceived = Signal(int, bytes)
    connectionChanged = Signal(bool, str)
    errorOccurred = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._core = CommCore(
            on_frame=lambda t, p: self.frameReceived.emit(t, p),
            on_connect=lambda ok, detail: self.connectionChanged.emit(ok, detail),
            on_error=self.errorOccurred.emit,
        )

    def open(self, kind: str, target: str, baud: int = 115200) -> bool:
        return self._core.open(kind, target, baud)

    def close(self) -> None:
        self._core.close()

    def send(self, pkt_type: int, payload: bytes = b"") -> None:
        self._core.send(pkt_type, payload)

    @property
    def connected(self) -> bool:
        return self._core.connected
