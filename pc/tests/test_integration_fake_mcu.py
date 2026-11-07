"""端到端集成测试：CommCore 连接 fake_mcu 子进程，完成 PING / PID 下发 / 遥测接收。

运行：在 pc/ 目录下  python -m pytest tests/test_integration_fake_mcu.py -v
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.comm import CommCore  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    PacketType,
    pack_pid_set,
    unpack_pid_ack,
    unpack_telemetry,
)

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
FAKE_MCU = os.path.join(ROOT, "tools", "fake_mcu.py")
TEST_PORT = 19011


def _wait_port(port: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"模拟器端口 {port} 未就绪")


def test_fake_mcu_full_loopback() -> None:
    proc = subprocess.Popen(
        [sys.executable, FAKE_MCU, "--port", str(TEST_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    received: dict = {"tel": [], "ack": [], "pong": []}
    core: CommCore | None = None
    try:
        _wait_port(TEST_PORT)

        def on_frame(t: int, p: bytes) -> None:
            if t == PacketType.TELEMETRY:
                received["tel"].append(unpack_telemetry(p))
            elif t == PacketType.PID_ACK:
                received["ack"].append(unpack_pid_ack(p))
            elif t == PacketType.PONG:
                received["pong"].append(p)

        core = CommCore(on_frame=on_frame, idle_timeout=5.0)
        assert core.open("tcp", f"127.0.0.1:{TEST_PORT}"), "无法连接模拟器"

        core.send(PacketType.PING)
        core.send(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, 30.0))

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and len(received["tel"]) < 10:
            time.sleep(0.05)

        assert received["pong"], "未收到 PONG"
        assert len(received["ack"]) == 1, f"PID_ACK 数量异常: {received['ack']}"
        kp, ki, kd, sp, result = received["ack"][0]
        assert result == 0
        assert (kp, ki, kd, sp) == pytest.approx((2.0, 0.5, 0.1, 30.0))
        assert len(received["tel"]) >= 10, f"遥测帧数不足: {len(received['tel'])}"
        last = received["tel"][-1]
        assert last[1] == pytest.approx(30.0)  # SP 已更新
    finally:
        if core is not None:
            core.close()
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
