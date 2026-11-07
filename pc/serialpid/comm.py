"""通信核心：独立线程独占传输通道，对外以回调交付帧。

Qt 适配层在 GUI 侧把回调转成信号；本模块不依赖 PySide6，可在无显示环境单测。

回调约定：
- on_frame(type: int, payload: bytes)
- on_connect(connected: bool, detail: str)
- on_error(message: str)

线程生命周期要点（断流 / 重连场景）：
- 传输对象作为参数传给线程，线程全程只操作自己那份本地引用，不读共享字段，
  从根上避免旧线程退出时关掉新连接。
- 空闲超时分支必须三件事一起做：标记断开、通知上层、跳出循环；只标记不退出
  会把线程变成永远空转的僵尸线程。
- 收尾关闭传输前做身份比较：共享字段仍指向自己这份才关闭，否则放过新连接。
- 连接状态的跨线程读写走同一把锁，避免超时边界上重复通知 / 误报。
- 本模块不做自动重连：掉线必须显式可见，静默重连会掩盖真实硬件故障。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

from .protocol import FrameDecoder, encode
from .transport import SerialTransport, TcpTransport, Transport, TransportError

Callback = Callable[..., None]

#: 空读时的节流切片（秒）。TCP 对端关闭后 read 会立即返回空字节，
#: 不节流会变成每秒几十万次的忙等；串口本身有读超时，此切片只是兜底。
_EMPTY_SLEEP = 0.02


class CommCore:
    def __init__(
        self,
        on_frame: Optional[Callback] = None,
        on_connect: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
        idle_timeout: float = 1.0,
    ) -> None:
        self._on_frame = on_frame or (lambda t, p: None)
        self._on_connect = on_connect or (lambda ok, detail: None)
        self._on_error = on_error or (lambda msg: None)
        self._idle_timeout = idle_timeout

        self._lock = threading.Lock()
        self._transport: Optional[Transport] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self._connected = False
        self._last_rx = 0.0

    # ---- 对外接口 ----

    def open(self, kind: str, target: str, baud: int = 115200) -> bool:
        """kind: 'serial' 或 'tcp'。成功启动接收线程返回 True。"""
        if self.connected:
            self.close()

        transport: Transport = SerialTransport() if kind == "serial" else TcpTransport()
        try:
            transport.open(target, baud)
        except TransportError as exc:
            self._on_error(str(exc))
            self._on_connect(False, str(exc))
            return False

        self._stop.clear()
        with self._lock:
            self._transport = transport
            self._connected = True
            self._last_rx = time.monotonic()
        # 传输对象按参数传入线程，线程不再回读 self._transport
        self._thread = threading.Thread(target=self._run, args=(transport,), daemon=True)
        self._thread.start()
        self._on_connect(True, target)
        return True

    def close(self) -> None:
        """显式关闭：置停止标志、等待线程退出、关闭共享传输。

        线程已退出时 ``join`` 立即返回，不会白等满超时上限。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

        with self._lock:
            self._thread = None
            if self._transport is not None:
                try:
                    self._transport.close()
                except OSError:
                    pass
                self._transport = None
            was_connected = self._connected
            self._connected = False

        if was_connected:
            self._on_connect(False, "closed")

    def send(self, pkt_type: int, payload: bytes = b"") -> None:
        """线程安全入队；实际写由接收线程完成。"""
        self._out.put(encode(pkt_type, payload))

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    # ---- 线程主体 ----

    def _run(self, tr: Transport) -> None:
        dec = FrameDecoder()
        last_rx = self._last_rx
        empty_streak = 0
        # 空读硬上限：按超时窗口折算，与空闲超时互为兜底判据；
        # idle_timeout <= 0 表示关闭空闲超时，此判据一并停用。
        max_empty = (
            max(1, int(self._idle_timeout / _EMPTY_SLEEP) + 2)
            if self._idle_timeout > 0
            else None
        )

        try:
            while not self._stop.is_set():
                try:
                    data = tr.read(1024)
                except OSError as exc:
                    self._on_error(f"读取异常: {exc}")
                    self._drop("read-error")
                    return

                if data:
                    empty_streak = 0
                    last_rx = time.monotonic()
                    for frame in dec.feed(data):
                        try:
                            self._on_frame(frame.type, frame.payload)
                        except Exception as exc:  # noqa: BLE001 - 回调异常不能打死线程
                            self._on_error(f"帧回调异常: {exc}")
                else:
                    empty_streak += 1
                    if self._idle_timeout > 0:
                        idle = time.monotonic() - last_rx > self._idle_timeout
                        if idle or (max_empty is not None and empty_streak >= max_empty):
                            self._drop("timeout")
                            return
                    # 节流：避免对端关闭后 read 立即返回造成忙等占满一个核
                    time.sleep(_EMPTY_SLEEP)

                # 排空发送队列
                while True:
                    try:
                        raw = self._out.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        tr.write(raw)
                    except OSError as exc:
                        self._on_error(f"写入异常: {exc}")
                        self._drop("write-error")
                        return
        finally:
            # 身份比较：共享字段仍指向自己这份传输才关闭并复位状态；
            # 若已被重连替换成新传输，则放过，绝不关掉新连接、也不清新连接状态。
            with self._lock:
                if self._transport is tr:
                    try:
                        tr.close()
                    except OSError:
                        pass
                    self._transport = None
                    self._connected = False

    def _drop(self, reason: str) -> None:
        """标记断开并通知上层；线程随后自行退出（不自动重连）。

        重复掉线 / 边界竞态下只通知一次。
        """
        with self._lock:
            if not self._connected:
                return
            self._connected = False
        self._on_connect(False, reason)
