"""card10 长稳压测脚本：可独立运行、输出进度与结构化结果。

三种模式
    gui        直接向界面链路喂帧（不走网络），压 缓冲/绘图/日志 的长时间表现
    tcp        连 TCP 模拟器收真实遥测帧，可周期性断线重连
    reconnect  纯连接生命周期：open/close N 次，看线程与句柄是否累积

用法（仓库根目录、项目 venv 解释器）
    python tools/pressure_test.py --mode gui --frames 144000          # 20Hz×2h 数据量
    python tools/pressure_test.py --mode gui --duration 120 --fps 20   # 按真实节拍跑
    python tools/pressure_test.py --mode gui --frames 20000 --bad-ratio 0.5 --unknown-ratio 0.25
    python tools/pressure_test.py --mode tcp --target 127.0.0.1:9000 --duration 120 --reconnect-every 30
    python tools/pressure_test.py --mode reconnect --cycles 100
    # 任一模追加 --json out.json 落结构化结果，供报告引用

设计约束（对应任务卡第四节）
    * 计时只用 time.monotonic / perf_counter，不用 wall clock 字符串；
    * 主循环内不做字符串格式化、不建临时对象：进度与采样按窗口触发，
      payload 用预打包字节，未知帧类型用取模轮转；
    * 采样器自身开销摊薄：内存/句柄每 ``--sample-every`` 帧采一次，
      渲染耗时每 ``--render-every`` 帧测一次，避免夹具成为瓶颈。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "pc"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CB = 1024.0 * 1024.0


# ---------------------------------------------------------------- 采样工具


def _process():
    import psutil

    return psutil.Process()


def _snapshot(proc) -> Dict[str, Any]:
    """一次进程级采样：常驻内存 / 线程数 / 句柄数（Windows 无 fd 概念，句柄等价）。"""
    rss_mb = proc.memory_info().rss / CB
    out: Dict[str, Any] = {
        "rss_mb": rss_mb,
        "threads": proc.num_threads(),
        "handles": None,
    }
    try:
        out["handles"] = proc.num_handles()
    except (AttributeError, NotImplementedError):
        pass
    return out


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "avg": statistics.mean(values),
        "p50": _quantile(values, 0.50),
        "p95": _quantile(values, 0.95),
        "max": max(values),
    }


# ---------------------------------------------------------------- 本地桩服务


class _LocalTcpServer:
    """连接churn 桩：接受即关闭，不做任何收发。

    注意：桩自己必须在 accept 后立刻关掉对端套接字。若把连接对象留在列表里
    等会儿再关，测出来的"句柄增长"是夹具的而不是被测代码的——这是本脚本
    第一版踩过的坑（100 轮重连虚报 +100 句柄，换成即关桩后归零）。
    """

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
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
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)


# ---------------------------------------------------------------- gui 模式


def _load_gui():
    try:
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        from serialpid.gui.main_window import MainWindow  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
        raise SystemExit(f"gui 模式需要 PySide6：{exc}") from exc

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.show()  # 离屏平台下只为触发布局，不弹真实窗口
    app.processEvents()
    return app, win


def _build_pattern(bad_ratio: float, unk_ratio: float) -> List[str]:
    """把注入比例换算成有限长度的重复序列。

    比例按 1/N 近似（如 0.02 → 每 50 帧一帧），坏帧与未知帧在序列里占不同
    位置，避免"坏帧取模周期整除未知帧周期"导致未知帧永远不触发。
    """
    from math import gcd  # noqa: PLC0415

    def period(ratio: float) -> int:
        if ratio <= 0.0:
            return 0
        if ratio >= 1.0:
            return 1
        return max(1, int(round(1.0 / ratio)))

    pb, pu = period(bad_ratio), period(unk_ratio)
    if pb == 0 and pu == 0:
        return []
    if pb and pu:
        length = pb * pu // gcd(pb, pu)
    else:
        length = max(pb, pu)
    nb = length // pb if pb else 0
    nu = length // pu if pu else 0
    if nb + nu > length:  # 两比例之和超过 1，未知帧让位给坏帧
        nb = min(nb, length)
        nu = max(0, length - nb)
    return ["bad"] * nb + ["unknown"] * nu + ["ok"] * (length - nb - nu)


def _frame_kind(pattern: List[str], i: int) -> str:
    return pattern[i % len(pattern)] if pattern else "ok"


def run_gui(args) -> Dict[str, Any]:
    from serialpid.protocol import PacketType, pack_telemetry  # noqa: PLC0415

    app, win = _load_gui()
    proc = _process()

    total = args.frames or max(1, int(args.duration * args.fps))
    step_ms = 1000.0 / args.fps
    render_every = max(1, args.render_every)
    sample_every = max(1, args.sample_every)

    good_payload = pack_telemetry(0, 25.0, 24.0, 30.0, 1.0)
    bad_payload = good_payload[:-1]          # 少一字节 → 长度预检失败
    unknown_payload = b"\x01\x02\x03\x04"
    pkt_unknown_base = 0x40
    pkt_unknown_span = 64
    pattern = args._pattern

    rss_at_full = 0.0
    peak_rss = 0.0
    samples: List[Dict[str, Any]] = []
    render_ms: List[float] = []
    counts = {"bad": 0, "unknown": 0, "ok": 0}
    unk_seq = 0

    t_wall0 = time.monotonic()
    t_pace0 = time.monotonic()
    last_progress = t_wall0
    last_sample = 0
    cap = win._plot.DEFAULT_MAX_POINTS

    def progress(i: int, snap_now: Dict[str, Any]) -> None:
        elapsed = time.monotonic() - t_wall0
        rate = i / elapsed if elapsed > 0 else 0.0
        tail = f" | 最近渲染 {render_ms[-1]:.2f}ms" if render_ms else ""
        print(
            f"[{args.mode}] {i}/{total} 帧 | {rate:,.0f} 帧/秒 | "
            f"RSS {snap_now['rss_mb']:.1f}MB | 线程 {snap_now['threads']} | "
            f"句柄 {snap_now['handles']} | 日志 {win._log._view.blockCount()} 行{tail}",
            flush=True,
        )

    snap = _snapshot(proc)
    samples.append({"frame": 0, **snap})

    try:
        for i in range(total):
            kind = _frame_kind(pattern, i)
            if kind == "bad":
                win._on_frame(PacketType.TELEMETRY, bad_payload)
                counts["bad"] += 1
            elif kind == "unknown":
                pkt = pkt_unknown_base + (unk_seq % pkt_unknown_span)
                unk_seq += 1
                win._on_frame(pkt, unknown_payload)
                counts["unknown"] += 1
            else:
                t_ms = int(i * step_ms) & 0xFFFFFFFF
                win._on_frame(PacketType.TELEMETRY, pack_telemetry(t_ms, 25.0, 24.0, 30.0, 1.0))
                counts["ok"] += 1

            if not args.fast:
                target = t_pace0 + (i + 1) / args.fps
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

            if i % render_every == 0:
                t0 = time.perf_counter()
                win._plot.render_now()
                render_ms.append((time.perf_counter() - t0) * 1000.0)

            if i - last_sample >= sample_every:
                last_sample = i
                snap = _snapshot(proc)
                samples.append({"frame": i, **snap})
                peak_rss = max(peak_rss, snap["rss_mb"])
                if win._plot.point_count >= cap and rss_at_full == 0.0:
                    rss_at_full = snap["rss_mb"]

            now = time.monotonic()
            if now - last_progress >= args.progress_interval:
                last_progress = now
                progress(i + 1, _snapshot(proc))

        # 收尾补一帧渲染，确保最后一次数据也进入绘制路径
        t0 = time.perf_counter()
        win._plot.render_now()
        render_ms.append((time.perf_counter() - t0) * 1000.0)
        elapsed = time.monotonic() - t_wall0

        end = _snapshot(proc)
        peak_rss = max(peak_rss, end["rss_mb"])
        if rss_at_full == 0.0:
            rss_at_full = end["rss_mb"]

        parse_lines = sum(
            1
            for line in win._log._view.toPlainText().splitlines()
            if "解析异常" in line
        )

        result: Dict[str, Any] = {
            "mode": "gui",
            "frames": total,
            "counts": counts,
            "wall_s": elapsed,
            "fps_actual": total / elapsed if elapsed > 0 else 0.0,
            "paced": not args.fast,
            "rss": {
                "start_mb": samples[0]["rss_mb"],
                "at_full_mb": rss_at_full,
                "end_mb": end["rss_mb"],
                "peak_mb": peak_rss,
                "growth_after_full_mb": end["rss_mb"] - rss_at_full,
            },
            "threads": {"start": samples[0]["threads"], "end": end["threads"]},
            "handles": {"start": samples[0]["handles"], "end": end["handles"]},
            "render_ms": _stats(render_ms),
            "log": {
                "blocks": win._log._view.blockCount(),
                "parse_error_lines": parse_lines,
                "parse_error_total": win._parse_errors,
                "unknown_types": len(win._unknown_logger.stats()),
            },
            "queue_points": win._plot.point_count,
            "buffer_points": len(win._buffer),
        }
    finally:
        win.close()
        app.processEvents()

    # 渲染耗时趋势：队列跑满前 / 后两段分别看均价，判"是否缓慢爬升"
    if render_ms:
        third = max(1, len(render_ms) // 3)
        result["render_ms"]["first_third_avg"] = statistics.mean(render_ms[:third])
        result["render_ms"]["last_third_avg"] = statistics.mean(render_ms[-third:])
    result["timeline"] = samples
    return result


# ---------------------------------------------------------------- tcp 模式


def run_tcp(args) -> Dict[str, Any]:
    from serialpid.comm import CommCore  # noqa: PLC0415
    from serialpid.protocol import PacketType  # noqa: PLC0415

    app, win = _load_gui()
    proc = _process()

    frames_received = 0
    last_frame_at = 0.0
    reconnect_s = float(args.reconnect_every or 0.0)
    reconnects = 0

    def on_frame(pkt_type: int, payload: bytes) -> None:
        nonlocal frames_received, last_frame_at
        frames_received += 1
        last_frame_at = time.monotonic()
        if pkt_type == PacketType.TELEMETRY:
            win._on_frame(pkt_type, payload)

    core = CommCore(on_frame=on_frame, idle_timeout=args.idle_timeout)

    samples: List[Dict[str, Any]] = []
    render_ms: List[float] = []
    snap = _snapshot(proc)
    samples.append({"frame": 0, **snap})

    target_s = float(args.duration)
    t_wall0 = time.monotonic()
    last_progress = t_wall0
    last_render = 0.0
    last_reconnect = t_wall0

    try:
        if not core.open("tcp", args.target):
            raise SystemExit(f"连接失败：{args.target}")
        print(f"[tcp] 已连接 {args.target}，计划压测 {target_s:.0f}s"
              f"{'，每 %.0fs 重连一次' % reconnect_s if reconnect_s else ''}", flush=True)

        while True:
            now = time.monotonic()
            if now - t_wall0 >= target_s:
                break

            # 渲染耗时按固定间隔测，避免每个帧都计时
            if now - last_render >= 1.0 / max(1.0, args.render_hz):
                last_render = now
                t0 = time.perf_counter()
                win._plot.render_now()
                render_ms.append((time.perf_counter() - t0) * 1000.0)

            if reconnect_s and now - last_reconnect >= reconnect_s:
                last_reconnect = now
                core.close()
                time.sleep(0.05)
                core.open("tcp", args.target)
                reconnects += 1

            if now - last_progress >= args.progress_interval:
                last_progress = now
                snap = _snapshot(proc)
                samples.append({"frame": frames_received, **snap})
                print(
                    f"[tcp] {now - t_wall0:.0f}/{target_s:.0f}s | 收帧 {frames_received} | "
                    f"RSS {snap['rss_mb']:.1f}MB | 句柄 {snap['handles']} | 重连 {reconnects}",
                    flush=True,
                )
            time.sleep(0.01)
    finally:
        core.close()
        win.close()
        app.processEvents()

    elapsed = time.monotonic() - t_wall0
    end = _snapshot(proc)
    gap = (time.monotonic() - last_frame_at) if last_frame_at else elapsed

    return {
        "mode": "tcp",
        "target": args.target,
        "duration_s": elapsed,
        "frames_received": frames_received,
        "reconnects": reconnects,
        "last_frame_gap_s": gap,
        "rss": {"start_mb": samples[0]["rss_mb"], "end_mb": end["rss_mb"], "peak_mb": max(s["rss_mb"] for s in samples)},
        "threads": {"start": samples[0]["threads"], "end": end["threads"]},
        "handles": {"start": samples[0]["handles"], "end": end["handles"]},
        "render_ms": _stats(render_ms),
        "log": {"blocks": win._log._view.blockCount(), "parse_error_lines": 0},
        "timeline": samples,
    }


# ---------------------------------------------------------------- reconnect 模式


def run_reconnect(args) -> Dict[str, Any]:
    from serialpid.comm import CommCore  # noqa: PLC0415

    proc = _process()
    server = _LocalTcpServer()
    core = CommCore(idle_timeout=min(args.idle_timeout, 0.2))
    target = f"127.0.0.1:{server.port}"

    cycles = max(1, args.cycles)
    timeline: List[Dict[str, Any]] = []
    failures = 0

    t_wall0 = time.monotonic()
    last_progress = t_wall0

    def cycle_once() -> bool:
        ok = core.open("tcp", target)
        core.close()
        return ok

    try:
        # 预热：先跑几轮，把解释器/psutil/socket 一次性分配的句柄摊掉，
        # 否则基线里混着首轮预热开销，会把"一次性 +3"误读成每轮泄漏。
        warm = min(10, cycles)
        for _ in range(warm):
            cycle_once()
        time.sleep(0.2)
        base = _snapshot(proc)
        timeline.append({"cycle": 0, **base})

        for i in range(1, cycles + 1):
            if not cycle_once():
                failures += 1
            if i % 10 == 0:
                snap = _snapshot(proc)
                timeline.append({"cycle": i, **snap})
                if time.monotonic() - last_progress >= args.progress_interval:
                    last_progress = time.monotonic()
                    print(
                        f"[reconnect] {i}/{cycles} 次 | 句柄 {snap['handles']} | "
                        f"线程 {snap['threads']} | RSS {snap['rss_mb']:.1f}MB",
                        flush=True,
                    )
        # 句柄释放可能有毫秒级延迟，等待一个窗口再取终值
        time.sleep(0.3)
        end = _snapshot(proc)
        timeline.append({"cycle": cycles, **end})
    finally:
        core.close()
        server.close()
    elapsed = time.monotonic() - t_wall0
    return {
        "mode": "reconnect",
        "cycles": cycles,
        "warmup_cycles": warm,
        "failures": failures,
        "wall_s": elapsed,
        "ms_per_cycle": elapsed * 1000.0 / cycles,
        "handles": {"start": base["handles"], "end": end["handles"]},
        "threads": {"start": base["threads"], "end": end["threads"]},
        "rss": {"start_mb": base["rss_mb"], "end_mb": end["rss_mb"], "peak_mb": max(s["rss_mb"] for s in timeline)},
        "timeline": timeline,
    }


# ---------------------------------------------------------------- 入口


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="card10 长稳压测（数据管道 / 重连 / 日志边界）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--mode", choices=("gui", "tcp", "reconnect"), default="gui")
    p.add_argument("--frames", type=int, default=0, help="总帧数；0 表示按 --duration×--fps 推算")
    p.add_argument("--duration", type=float, default=120.0, help="时长（秒）；仅未给 --frames 时生效")
    p.add_argument("--fps", type=float, default=20.0, help="名义帧率（决定 t_ms 步进）")
    p.add_argument("--bad-ratio", type=float, default=0.0, help="坏帧（载荷短一字节）占比 0~1")
    p.add_argument("--unknown-ratio", type=float, default=0.0, help="未知类型帧占比 0~1")
    p.add_argument("--reconnect-every", type=float, default=0.0, help="tcp 模式断线重连间隔（秒），0=不断线")
    p.add_argument("--cycles", type=int, default=100, help="reconnect 模式的 open/close 次数")
    p.add_argument("--target", default="127.0.0.1:9000", help="tcp 模式模拟器地址")
    p.add_argument("--idle-timeout", type=float, default=1.0, help="通信空闲超时（秒）")
    p.add_argument("--fast", action="store_true", help="不做真实节拍等待，按最快速度喂帧（等效数据量压测）")
    p.add_argument("--render-every", type=int, default=2000, help="gui 模式每多少帧测一次渲染耗时")
    p.add_argument("--render-hz", type=float, default=30.0, help="tcp 模式下渲染测量频率")
    p.add_argument("--sample-every", type=int, default=4000, help="每多少帧采一次内存/句柄")
    p.add_argument("--progress-interval", type=float, default=10.0, help="进度打印间隔（秒）")
    p.add_argument("--json", default="", help="把结构化结果写入该路径")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    # 注入比例换算成重复序列（逐帧取模，结果可复现）
    args._pattern = _build_pattern(args.bad_ratio, args.unknown_ratio)
    if args.frames and args.duration <= 0:
        args.fast = True
    if args.frames and not args.fast:
        # 显式给了帧数又没要求真实节拍时，默认按数据量跑（2h 等效压测不必真等 2h）
        args.fast = True

    t0 = time.monotonic()
    if args.mode == "gui":
        result = run_gui(args)
    elif args.mode == "tcp":
        result = run_tcp(args)
    else:
        result = run_reconnect(args)
    result["script_wall_s"] = time.monotonic() - t0
    result["args"] = {k: v for k, v in vars(args).items() if not k.startswith("_")}

    print()
    print("=== 结果 ===")
    rss = result.get("rss", {})
    print(f"模式 {result['mode']} | 用时 {result['script_wall_s']:.2f}s")
    print(f"内存 起始 {rss.get('start_mb', 0):.1f}MB → 结束 {rss.get('end_mb', 0):.1f}MB"
          f" | 峰值 {rss.get('peak_mb', 0):.1f}MB"
          + (f" | 跑满队列后增长 {rss['growth_after_full_mb']:.1f}MB" if "growth_after_full_mb" in rss else ""))
    h = result.get("handles", {})
    print(f"句柄 {h.get('start')} → {h.get('end')} | 线程 {result.get('threads', {}).get('start')} → "
          f"{result.get('threads', {}).get('end')}")
    r = result.get("render_ms", {})
    if r.get("count"):
        print(f"渲染 {r['count']} 次：均价 {r['avg']:.2f}ms / P95 {r['p95']:.2f}ms / 最大 {r['max']:.2f}ms")
    if result["mode"] == "gui":
        lg = result["log"]
        print(f"日志 {lg['blocks']} 行（上限 2000）| 解析异常日志 {lg['parse_error_lines']} 行"
              f" | 累计计数 {lg['parse_error_total']} | 聚合类型 {lg['unknown_types']} 种")
        print(f"队列 {result['queue_points']} 点 / 缓冲 {result['buffer_points']} 点")
    if result["mode"] == "reconnect":
        print(f"重连 {result['cycles']} 次，失败 {result['failures']}，平均 {result['ms_per_cycle']:.1f}ms/次")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
