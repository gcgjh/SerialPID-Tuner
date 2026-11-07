"""离线 MCU 模拟器（TCP 传输，帧格式与实物串口完全一致）。

用途：无硬件时让上位机完成连接、看曲线、参数下发、阶跃测试、CSV 导出全流程。
植物模型为一阶惯性 + 比例输出，仅用于演示链路与绘图，不代表真实 PID 整定结果。

用法：
    python tools/fake_mcu.py --port 9000
上位机 TCP 目标填 127.0.0.1:9000。
"""

from __future__ import annotations

import argparse
import socket
import struct
import threading
import time
from typing import Optional

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from serialpid.protocol import (  # noqa: E402
    PacketType,
    FrameDecoder,
    encode,
    pack_pid_set,
    unpack_pid_ack,  # noqa: F401  (保留对称导入)
    unpack_telemetry,  # noqa: F401
)

CTRL_PERIOD_S = 0.05  # 20 Hz 遥测/控制周期
PLANT_TAU_S = 2.0      # 一阶惯性时间常数
OUT_MIN, OUT_MAX = 0.0, 100.0


class FakeMcu:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # 控制器/植物状态
        self.kp, self.ki, self.kd = 2.0, 0.5, 0.1
        self.sp = 25.0
        self.pv = 20.0
        self.out = 0.0
        self._integral = 0.0
        self._last_err = 0.0
        self._t_ms = 0

        # 阶跃状态
        self._step_baseline: Optional[float] = None
        self._step_until = 0.0
        self._step_revert = False

    # ---- 命令处理 ----

    def _handle(self, pkt_type: int, payload: bytes) -> None:
        if pkt_type == PacketType.PING:
            self._send(PacketType.PONG, struct.pack("<BB", 0x10, self._status()))
        elif pkt_type == PacketType.PID_SET:
            kp, ki, kd, sp = struct.unpack("<4f", payload)
            self.kp, self.ki, self.kd, self.sp = kp, ki, kd, sp
            ack = struct.pack("<4fB", kp, ki, kd, sp, 0)
            self._send(PacketType.PID_ACK, ack)
            print(f"[PID_SET] Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")
        elif pkt_type == PacketType.SP_SET:
            (self.sp,) = struct.unpack("<f", payload)
            print(f"[SP_SET] SP={self.sp:.2f}")
        elif pkt_type == PacketType.STEP_START:
            amplitude, hold_ms = struct.unpack("<fI", payload)
            self._step_baseline = self.sp
            self.sp = self.sp + amplitude
            self._step_until = time.monotonic() + hold_ms / 1000.0
            self._step_revert = hold_ms > 0
            self._send(PacketType.STEP_EVENT, struct.pack("<BI", 1, self._t_ms))
            print(f"[STEP_START] 幅值={amplitude:.2f} 保持={hold_ms}ms -> SP={self.sp:.2f}")
        elif pkt_type == PacketType.STEP_STOP:
            self._revert_step()
            self._send(PacketType.STEP_EVENT, struct.pack("<BI", 0, self._t_ms))
            print("[STEP_STOP] 停止")
        elif pkt_type == PacketType.RESET:
            self.pv, self.out = 20.0, 0.0
            self._integral, self._last_err = 0.0, 0.0
            self._revert_step()
            self._send(PacketType.PONG, struct.pack("<BB", 0x10, self._status()))
            print("[RESET] 状态复位")

    def _revert_step(self) -> None:
        if self._step_baseline is not None:
            self.sp = self._step_baseline
        self._step_baseline = None
        self._step_revert = False

    def _status(self) -> int:
        if self._step_baseline is not None:
            return 2
        return 1

    # ---- 周期控制 + 遥测 ----

    def _control_step(self, dt: float) -> None:
        # 阶跃到时回退
        if self._step_revert and time.monotonic() >= self._step_until:
            self._revert_step()
            self._send(PacketType.STEP_EVENT, struct.pack("<BI", 2, self._t_ms))
            print("[STEP_EVENT] 完成")

        err = self.sp - self.pv
        # 简易 PID（含积分限幅），仅演示用
        self._integral += err * dt
        self._integral = max(-50.0, min(50.0, self._integral))
        deriv = (err - self._last_err) / dt if dt > 0 else 0.0
        self._last_err = err
        self.out = self.kp * err + self.ki * self._integral + self.kd * deriv
        self.out = max(OUT_MIN, min(OUT_MAX, self.out))

        # 一阶惯性植物
        self.pv += dt * (self.sp - self.pv) / PLANT_TAU_S
        self._t_ms += int(dt * 1000)

        payload = struct.pack("<I4f", self._t_ms, self.sp, self.pv, self.out, err)
        self._send(PacketType.TELEMETRY, payload)

    def _send(self, pkt_type: int, payload: bytes) -> None:
        if self._client is None:
            return
        try:
            with self._lock:
                self._client.sendall(encode(pkt_type, payload))
        except OSError:
            pass

    # ---- 服务循环 ----

    def serve(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        print(f"fake_mcu 监听 {self.host}:{self.port}（Ctrl+C 退出）")

        ctrl = threading.Thread(target=self._control_loop, daemon=True)
        ctrl.start()

        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            print(f"上位机接入: {addr}")
            self._client = client
            self._client.settimeout(0.2)
            self._read_loop()
            print("上位机断开")
            self._client = None

    def _control_loop(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.2)
            last = now
            if self._client is not None:
                self._control_step(dt)
            time.sleep(CTRL_PERIOD_S)

    def _read_loop(self) -> None:
        dec = FrameDecoder()
        assert self._client is not None
        while not self._stop.is_set():
            try:
                data = self._client.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            for frame in dec.feed(data):
                try:
                    self._handle(frame.type, frame.payload)
                except struct.error as exc:
                    print(f"[WARN] 载荷解析失败: {exc}")

    def stop(self) -> None:
        self._stop.set()
        if self._client:
            self._client.close()
        if self._sock:
            self._sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SerialPID-Tuner 离线模拟器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    mcu = FakeMcu(args.host, args.port)
    try:
        mcu.serve()
    except KeyboardInterrupt:
        mcu.stop()
        print("\n已退出")


if __name__ == "__main__":
    main()
