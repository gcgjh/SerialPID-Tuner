"""通信层 TCP 闭环测试：本地桩服务应答 PING/PONG。

运行：在 pc/ 目录下  python tests/test_comm_loopback.py
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.comm import CommCore  # noqa: E402
from serialpid.protocol import PacketType, FrameDecoder  # noqa: E402


def _echo_server(ready: threading.Event, frames: list) -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    ready.set()

    def serve() -> None:
        conn, _ = srv.accept()
        conn.settimeout(0.2)
        dec = FrameDecoder()
        try:
            while True:
                try:
                    data = conn.recv(1024)
                except socket.timeout:
                    continue
                if not data:
                    break
                for frame in dec.feed(data):
                    frames.append(frame.type)
                    if frame.type == PacketType.PING:
                        payload = struct.pack("<BB", 0x10, 1)  # fw_ver 1.0, status 运行
                        from serialpid.protocol import encode
                        conn.sendall(encode(PacketType.PONG, payload))
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return port


def main() -> None:
    ready = threading.Event()
    server_frames: list = []
    port = _echo_server(ready, server_frames)
    ready.wait(1.0)

    received: list = []
    connected: list = []
    errors: list = []

    core = CommCore(
        on_frame=lambda t, p: received.append((t, p)),
        on_connect=lambda ok, detail: connected.append((ok, detail)),
        on_error=errors.append,
        idle_timeout=5.0,
    )

    assert core.open("tcp", f"127.0.0.1:{port}"), "TCP 连接失败"
    deadline = time.monotonic() + 2.0
    while not connected and time.monotonic() < deadline:
        time.sleep(0.02)
    assert connected and connected[0][0] is True, f"未连上: {connected}"

    core.send(PacketType.PING)
    deadline = time.monotonic() + 2.0
    while not received and time.monotonic() < deadline:
        time.sleep(0.02)

    assert errors == [], f"通信错误: {errors}"
    assert len(received) == 1, f"未收到 PONG: {received}"
    pong_type, pong_payload = received[0]
    assert pong_type == PacketType.PONG
    fw_ver, status = struct.unpack("<BB", pong_payload)
    assert (fw_ver, status) == (0x10, 1)
    assert server_frames == [PacketType.PING]

    core.close()
    print("PASS test_comm_loopback: PING -> PONG 闭环成功")


if __name__ == "__main__":
    main()
