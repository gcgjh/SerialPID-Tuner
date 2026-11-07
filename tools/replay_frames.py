"""把录制好的帧流按原时序通过 TCP 播出去（界面零改动就能看现场回放）。

场景：现场抓了一份 ``frames.jsonl``，想在界面里再看一遍当时曲线怎么抖的。
不用改上位机通道列表——起这个服务，界面按老办法连 ``127.0.0.1:9100`` 即可，
收到的帧与录制时逐字节一致、时序按 ``--speed`` 缩放。

用法::

    python tools/replay_frames.py --file frames.jsonl --port 9100
    python tools/replay_frames.py --file frames.jsonl --speed 0.5 --loop

只播下行（引擎→上位机）帧；上行命令是当时上位机发的，回放时不该再灌回去。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from serialpid.sim.recorder import FrameReplay  # noqa: E402


def serve(path: str, port: int, speed: float, loop: bool, host: str = "127.0.0.1") -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(1)
    sock.settimeout(0.2)

    replay = FrameReplay(path, speed=speed, loop=loop, direction="down")
    print(f"回放文件：{path}（{replay.total} 帧，倍速 {speed:g}）")
    if replay.warnings:
        print(f"注意：有 {len(replay.warnings)} 行记录无法解析，已跳过")
    print(f"监听 {host}:{port}（Ctrl+C 退出）")

    try:
        while True:
            try:
                client, addr = sock.accept()
            except socket.timeout:
                continue
            print(f"上位机接入: {addr}，开始回放")
            replay.seek(0)
            try:
                while True:
                    frames = replay.due_frames()
                    for raw in frames:
                        client.sendall(raw)
                    if replay.position >= replay.total and not loop:
                        print("回放结束（保持连接，等待断开）")
                        break
                    time.sleep(0.005)
            except (BrokenPipeError, ConnectionResetError, OSError):
                print("上位机断开")
            finally:
                try:
                    client.close()
                except OSError:
                    pass
            if not loop:
                # 单次服务模式：播完继续等下一位接入，重新从头发送
                continue
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="SerialPID-Tuner 帧流回放服务")
    parser.add_argument("--file", required=True, help="录制得到的 JSONL")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--speed", type=float, default=1.0, help="倍速：2 表示两倍速")
    parser.add_argument("--loop", action="store_true", help="循环播放")
    args = parser.parse_args()
    serve(args.file, args.port, args.speed, args.loop, args.host)


if __name__ == "__main__":
    main()
