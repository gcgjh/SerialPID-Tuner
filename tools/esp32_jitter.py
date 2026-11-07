"""采 ESP32 控制任务抖动的实测输出（配 BOARD_JITTER_LOG=1 的测量构建）。

用法::

    python tools/esp32_jitter.py --port COM5 --seconds 20

固件每 5 s 打一行::

    CTRL jitter: max interval 11 ms over 500 ticks (target 10 ms)

``max interval`` 是相邻两圈控制任务的**最大**间隔（只记最大值，不记平均：
平均会被偶发长间隔掩盖）。脚本把这些行抽出来、算统计、落盘到 ``docs/``。
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time

LINE_RE = re.compile(r"CTRL jitter: max interval (\d+) ms over (\d+) ticks \(target (\d+) ms\)")


def main() -> int:
    parser = argparse.ArgumentParser(description="采集 ESP32 控制任务抖动")
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "docs"))
    args = parser.parse_args()

    import serial  # 延迟导入：--help 不需要串口库

    samples = []
    started = time.monotonic()
    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.05)
    except Exception as exc:  # noqa: BLE001 - 端口占用/不存在都算环境问题
        print(f"打开 {args.port} 失败：{exc}")
        return 2

    try:
        while time.monotonic() - started < args.seconds:
            chunk = ser.read(1024)
            if not chunk:
                continue
            for raw in chunk.split(b"\n"):
                text = raw.decode("utf-8", errors="ignore").strip()
                if not text:
                    continue
                match = LINE_RE.search(text)
                if match:
                    samples.append((round(time.monotonic() - started, 2),
                                    int(match.group(1)), int(match.group(2)),
                                    int(match.group(3))))
    finally:
        ser.close()

    if not samples:
        print("没有采到抖动行：确认烧的是测量构建（BOARD_JITTER_LOG=1），且串口没被上位机占用。")
        return 1

    maxima = [row[1] for row in samples]
    target = samples[-1][3]
    print(f"采到 {len(samples)} 行；目标周期 {target} ms")
    for at, peak, ticks, _t in samples:
        print(f"  +{at:>5.2f}s  max interval {peak:>3} ms over {ticks} ticks")

    print(f"\n最大间隔：{max(maxima)} ms（目标 {target} ms，超出 {max(maxima) - target} ms）")
    print(f"中位数：{statistics.median(maxima)} ms")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "esp32_jitter.txt")
    with open(path, "w", encoding="utf-8") as fp:
        for at, peak, ticks, _t in samples:
            fp.write(f"+{at:.2f}s max_interval_ms={peak} ticks={ticks} target_ms={target}\n")
    print(f"落盘：{os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
