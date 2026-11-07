"""离线 MCU 模拟器（TCP 薄壳，卡27 起只做通道，不再自己算控制）。

协议会话、命令分发、阶跃状态机、PID 与被控对象都收敛到 ``serialpid.sim.engine``：
本文件只负责监听 TCP、把收到的字节喂给引擎、把引擎输出写回 socket。
这样"内置模拟器"和"TCP 模拟器"只有一套实现，不会再出现改了一边忘另一边。

命令行与输出格式保持不变（集成测试按子进程拉起它）::

    python tools/fake_mcu.py --port 9000

上位机 TCP 目标填 127.0.0.1:9000。

帧字节与 ``SimEngine`` 逐字节一致——两者本来就是同一个实现。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from serialpid.protocol import (  # noqa: E402
    PacketType,
    encode,
    expected_payload_len,
    parse_frame,
)
from serialpid.sim.engine import SimEngine  # noqa: E402
from serialpid.sim.transport import SimTransport  # noqa: E402

CTRL_PERIOD_S = 0.05  # 20 Hz 遥测/控制周期（引擎默认，保留常量供外部引用）


class _SocketOut(SimTransport):
    """引擎输出直接落到 socket：薄壳没有"上位机侧读缓冲"，不需要再存一份。"""

    def __init__(self, owner: "FakeMcu") -> None:
        super().__init__(read_timeout=0.01)
        self._owner = owner

    def emit(self, data: bytes) -> bool:  # noqa: D401 - 与 SimTransport 同语义
        payload = bytes(data)
        self.bytes_to_host += len(payload)
        # 没有客户端时也照样走 _send：它在无客户端时静默丢弃，
        # 而测试会把 _send 换成捕获函数，不连 socket 也能验帧。
        return self._owner._write_raw(payload)


class FakeMcu:
    """TCP 薄壳：监听、收发、打印；控制逻辑全部委托 ``SimEngine``。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9000) -> None:
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._client: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connected = False

        self._transport = _SocketOut(self)
        self._engine = SimEngine(
            transport=self._transport,
            period_s=CTRL_PERIOD_S,
            on_event=self._on_event,
        )

    # ---- 引擎事件 -> 原命令行输出 ---------------------------------------- #

    @staticmethod
    def _on_event(name: str, fields: dict) -> None:
        if name == "PID_SET":
            print(
                f"[PID_SET] Kp={fields['kp']:.3f} Ki={fields['ki']:.3f} "
                f"Kd={fields['kd']:.3f} SP={fields['sp']:.2f}"
            )
        elif name == "SP_SET":
            print(f"[SP_SET] SP={fields['sp']:.2f}")
        elif name == "STEP_START":
            print(
                f"[STEP_START] 幅值={fields['amplitude']:.2f} "
                f"保持={fields['hold_ms']}ms -> SP={fields['sp']:.2f}"
            )
        elif name == "STEP_STOP":
            print("[STEP_STOP] 停止")
        elif name == "RESET":
            print("[RESET] 状态复位")
        elif name == "STEP_DONE":
            print("[STEP_EVENT] 完成")

    # ---- 收发 ------------------------------------------------------------ #

    def _handle(self, pkt_type: int, payload: bytes) -> None:
        """处理一条命令（测试可直接调用，不必连 socket）。"""
        expected = expected_payload_len(pkt_type)
        if expected is not None and len(payload) != expected:
            print(f"[WARN] 载荷解析失败：type=0x{pkt_type:02X} 期望 {expected}B，实收 {len(payload)}B")
            return
        self._engine.handle_frame(pkt_type, payload)

    def _send(self, pkt_type: int, payload: bytes) -> None:
        """把一帧写到当前客户端（测试可替换成捕获函数，不连 socket）。"""
        raw = encode(pkt_type, payload)
        client = self._client
        if client is None:
            return
        try:
            with self._lock:
                client.sendall(raw)
        except OSError:
            pass

    def _write_raw(self, raw: bytes) -> bool:
        """引擎给出完整帧字节：解回类型/载荷后走 ``_send``（保持原接口）。"""
        try:
            frame = parse_frame(raw)
        except ValueError:
            return False
        self._send(frame.type, frame.payload)
        return True

    # ---- 服务循环 -------------------------------------------------------- #

    def serve(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(1)
        self._sock.settimeout(0.2)
        print(f"fake_mcu 监听 {self.host}:{self.port}（Ctrl+C 退出）")

        while not self._stop.is_set():
            try:
                client, addr = self._sock.accept()
            except socket.timeout:
                continue
            print(f"上位机接入: {addr}")
            self._client = client
            self._client.settimeout(0.2)
            self._connected = True
            self._engine.start()
            try:
                self._read_loop()
            finally:
                self._connected = False
                self._engine.stop()
                self._client = None
            print("上位机断开")

    def _read_loop(self) -> None:
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
            self._engine.feed(data)

    def stop(self) -> None:
        self._stop.set()
        self._connected = False
        self._engine.stop()
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass


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
