"""断流 / 重连线程生命周期回归测试。

对应缺陷卡：
- 空闲超时后线程必须退出，不能残留僵尸线程、不能忙等烧满一个核；
- 反复重连活跃线程数不增长；
- 旧线程收尾做身份比较，不得关掉后建立的新连接；
- 掉线只通知一次，不做自动重连。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.comm import CommCore  # noqa: E402


class _SilentServer:
    """接受一个连接后保持静默，不发任何数据，用于触发空闲超时。"""

    def __init__(self) -> None:
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # 收尾时套接字已关，正常退出线程
            self._conn = conn
            conn.settimeout(0.2)
            while not self._stop.is_set():
                try:
                    if not conn.recv(1024):
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass


def test_idle_timeout_drops_and_thread_exits() -> None:
    srv = _SilentServer()
    connected: list[tuple[bool, str]] = []
    errors: list[str] = []
    core = CommCore(
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        on_error=errors.append,
        idle_timeout=0.3,
    )
    cpu0 = time.process_time()
    try:
        assert core.open("tcp", f"127.0.0.1:{srv.port}")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if connected and not connected[-1][0]:
                break
            time.sleep(0.02)

        assert connected[-1] == (False, "timeout"), connected
        assert errors == [], f"不应有错误回调: {errors}"

        thread = core._thread
        assert thread is not None, "线程句柄丢失"
        # 回调先于线程 return 触发，检查存活要留一个收敛窗口（并发负载下尤其明显）
        deadline2 = time.monotonic() + 0.5
        while thread.is_alive() and time.monotonic() < deadline2:
            time.sleep(0.01)
        assert not thread.is_alive(), "断流后接收线程未退出（线程泄漏）"

        cpu = time.process_time() - cpu0
        assert cpu < 0.2, f"忙等 CPU 过高: {cpu:.3f}s"
    finally:
        core.close()
        srv.close()


def test_reconnect_cycles_no_thread_growth() -> None:
    srv = _SilentServer()
    core = CommCore(idle_timeout=5.0)
    base = {t.name for t in threading.enumerate()}
    try:
        for _ in range(5):
            assert core.open("tcp", f"127.0.0.1:{srv.port}")
            core.close()

        time.sleep(0.1)
        alive = [
            t for t in threading.enumerate()
            if t.is_alive() and t.name not in base
        ]
        assert alive == [], f"重连后线程泄漏: {[t.name for t in alive]}"
    finally:
        core.close()
        srv.close()


def test_drop_then_reconnect_keeps_new_connection() -> None:
    srv1 = _SilentServer()
    srv2 = _SilentServer()
    connected: list[tuple[bool, str]] = []
    core = CommCore(
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        idle_timeout=0.3,
    )
    try:
        # 第一轮：静默对端触发超时掉线
        assert core.open("tcp", f"127.0.0.1:{srv1.port}")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if connected and not connected[-1][0]:
                break
            time.sleep(0.02)
        assert connected[-1] == (False, "timeout")
        thread = core._thread
        assert thread is not None
        deadline2 = time.monotonic() + 0.5
        while thread.is_alive() and time.monotonic() < deadline2:
            time.sleep(0.01)
        assert not thread.is_alive(), "断流后接收线程未退出（线程泄漏）"

        # 第二轮：重连，旧线程收尾不得关掉新连接
        assert core.open("tcp", f"127.0.0.1:{srv2.port}")
        assert core.connected
        time.sleep(0.2)
        assert core.connected, "新连接被旧线程收尾误关"
    finally:
        core.close()
        srv1.close()
        srv2.close()
