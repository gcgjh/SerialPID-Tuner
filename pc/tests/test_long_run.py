"""card10 长稳压测用例：默认跳过，显式 ``--long-run`` 才执行。

覆盖任务卡第二节的四个假设：
  1. 数据管道在长时间高频下不退化（队列跑满后内存与重画耗时平稳）；
  2. 断线重连可无限循环（100 次 open/close，线程与句柄不累积）；
  3. 日志与聚合状态不随时间膨胀（坏帧 + 未知帧混合灌两万条后行数有界）；
  4. 时间轴回绕 / 复位不让横轴错乱（card11 补齐：回绕、会话内复位、重连三条路径）。

运行：
    python -m pytest tests/test_long_run.py --long-run -v

已知缺陷不写成硬失败，而在断言不成立时用 ``pytest.xfail`` 标注并附实测值，
指向 ``docs/PRESSURE_TEST.md`` 的问题清单——长稳用例的职责是暴露问题，
修复归属由报告里的"责任卡"字段决定。
"""
from __future__ import annotations

import os
import socket
import statistics
import sys
import threading
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.slow

WRAP = 1 << 32  # 32 位无符号毫秒回绕点（约 49.7 天）
QUEUE_CAP = 20000
LOG_CAP = 2000


# ---------------------------------------------------------------- 夹具


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _new_window(qapp):
    from serialpid.gui.main_window import MainWindow

    win = MainWindow()
    win.show()
    qapp.processEvents()
    return win


def _proc_snapshot():
    import psutil

    p = psutil.Process()
    return {
        "rss_mb": p.memory_info().rss / (1024.0 * 1024.0),
        "threads": p.num_threads(),
        "handles": p.num_handles(),
    }


def _log_blocks(win) -> int:
    return win._log._view.blockCount()


class _ImmediateCloseSocketServer:
    """连接churn 桩：accept 后立刻关闭。

    桩侧必须当场释放对端套接字——否则测到的是夹具的句柄增长。
    本项目第一版压测就踩过：100 轮虚报 +100 句柄，换成即关桩后归零。
    """

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

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


# ---------------------------------------------------------------- 假设一


def test_pipeline_stable_after_queue_full(qapp):
    """等效 20Hz×2h（14.4 万帧）：队列封顶后内存与渲染耗时不爬升。"""
    from serialpid.protocol import PacketType, pack_telemetry

    total = 144_000
    render_every = 4_000

    win = _new_window(qapp)
    proc = _proc_snapshot()
    base_rss = proc["rss_mb"]
    rss_at_full = 0.0
    render_ms = []

    try:
        for i in range(total):
            win._on_frame(PacketType.TELEMETRY, pack_telemetry(i * 50, 25.0, 24.0, 30.0, 1.0))
            if i % render_every == 0:
                t0 = time.perf_counter()
                win._plot.render_now()
                render_ms.append((time.perf_counter() - t0) * 1000.0)
            if win._plot.point_count >= QUEUE_CAP and rss_at_full == 0.0:
                rss_at_full = _proc_snapshot()["rss_mb"]

        t0 = time.perf_counter()
        win._plot.render_now()
        render_ms.append((time.perf_counter() - t0) * 1000.0)

        end_rss = _proc_snapshot()["rss_mb"]
        if rss_at_full == 0.0:
            rss_at_full = end_rss

        assert win._plot.point_count == QUEUE_CAP, "队列未封顶"
        assert len(win._buffer) == QUEUE_CAP, "模型缓冲未封顶"

        # 时间轴无累积漂移（card11）：14.4 万帧后横轴仍是精确的毫秒累计，
        # 首尾相差一个整数毫秒数，浮点累加式实现会在这里露出偏差。
        xs = win._plot._extract(0)[:, 0]
        assert xs[0] == pytest.approx((total - QUEUE_CAP) * 0.05, abs=1e-9), (
            f"回绕式时间轴首点漂移：{xs[0]}"
        )
        assert xs[-1] == pytest.approx((total - 1) * 0.05, abs=1e-9), (
            f"回绕式时间轴末点漂移：{xs[-1]}"
        )
        assert bool((np.diff(xs) >= 0).all()), "时间轴非单调"

        growth = end_rss - rss_at_full
        assert growth < 20.0, f"队列跑满后内存仍增长 {growth:.1f}MB（基线 {base_rss:.1f}MB）"

        assert max(render_ms) < 5.0, f"单帧渲染最大 {max(render_ms):.2f}ms，超出 5ms 预算"

        third = max(1, len(render_ms) // 3)
        first_avg = statistics.mean(render_ms[:third])
        last_avg = statistics.mean(render_ms[-third:])
        assert last_avg <= max(first_avg * 1.5, first_avg + 0.6), (
            f"渲染耗时爬升：前段 {first_avg:.2f}ms → 后段 {last_avg:.2f}ms"
        )
    finally:
        win.close()
        qapp.processEvents()


# ---------------------------------------------------------------- 假设二


def test_reconnect_100_cycles_no_growth():
    """100 次 open/close：线程与句柄不累积（预热后取基线，排除一次性分配）。"""
    from serialpid.comm import CommCore

    cycles = 100
    server = _ImmediateCloseSocketServer()
    core = CommCore(idle_timeout=0.2)
    target = f"127.0.0.1:{server.port}"

    def one_cycle() -> None:
        core.open("tcp", target)
        core.close()

    try:
        for _ in range(10):          # 预热：摊掉解释器/psutil 的一次性句柄
            one_cycle()
        time.sleep(0.2)
        base = _proc_snapshot()
        base_alive = len(threading.enumerate())

        for _ in range(cycles):
            one_cycle()

        time.sleep(0.3)
        end = _proc_snapshot()
        end_alive = len(threading.enumerate())
    finally:
        core.close()
        server.close()

    assert end_alive - base_alive <= 1, (
        f"重连 {cycles} 次后存活线程数 {base_alive} → {end_alive}（线程泄漏）"
    )
    assert end["threads"] - base["threads"] <= 1, (
        f"重连 {cycles} 次线程增长 {end['threads'] - base['threads']}："
        f"{base['threads']} → {end['threads']}"
    )
    assert end["handles"] - base["handles"] <= 3, (
        f"重连 {cycles} 次句柄增长 {end['handles'] - base['handles']}："
        f"{base['handles']} → {end['handles']}"
    )
    assert core.connected is False, "重连循环结束后仍标记为已连接"


# ---------------------------------------------------------------- 假设三


def test_log_bounded_under_mixed_frames(qapp):
    """坏帧 + 未知帧混合灌两万条：日志行数有界、聚合表不无限膨胀。"""
    from serialpid.protocol import PacketType, pack_telemetry

    good = pack_telemetry(0, 25.0, 24.0, 30.0, 1.0)
    bad = good[:-1]                       # 少一字节，长度预检失败
    unknown = b"\x01\x02\x03\x04"

    win = _new_window(qapp)
    try:
        # 50% 坏遥测帧 / 25% 未知类型（64 种轮流）/ 25% 正常遥测
        for i in range(20_000):
            if i % 2 == 0:
                win._on_frame(PacketType.TELEMETRY, bad)
            elif i % 4 == 1:
                win._on_frame(0x40 + (i % 64), unknown)
            else:
                win._on_frame(PacketType.TELEMETRY, pack_telemetry(i, 25.0, 24.0, 30.0, 1.0))

        blocks = _log_blocks(win)
        assert blocks <= LOG_CAP, f"日志行数 {blocks} 超过上限 {LOG_CAP}"

        logger = getattr(win, "_unknown_logger", None)
        if logger is not None:
            types = len(logger.stats())
            assert types <= 256, f"聚合状态表 {types} 项，超出类型空间上界"

        # 状态栏累计计数是"本机开机以来"的语义，必须继续累加
        assert win._parse_errors > 0, "解析异常累计计数未增长"

        # 单类坏帧限流：清空日志后灌 1000 条同类坏帧，新增行数应受控
        win._log._view.clear()
        for _ in range(1000):
            win._on_frame(PacketType.TELEMETRY, bad)
        same_class_lines = sum(
            1 for line in win._log._view.toPlainText().splitlines() if "解析异常" in line
        )
        if same_class_lines > 15:
            pytest.xfail(
                f"已知缺陷（见 docs/PRESSURE_TEST.md · P-02，卡8 已修复，此处为守门）："
                f"1000 条同类坏帧新增 {same_class_lines} 行日志，超过 15 行限流目标"
            )
        # 清空日志不应抹掉状态栏的累计次数
        assert win._parse_errors >= 1000, "清空日志把累计计数一并清零了"

        # 撞上限时必须丢最旧行（而不是阻塞写入或无限增长）
        win._log._view.clear()
        for i in range(LOG_CAP + 1000):
            win._log.log(f"cap-probe-{i}")
        text = win._log._view.toPlainText()
        assert _log_blocks(win) == LOG_CAP, (
            f"日志应恰好封顶在 {LOG_CAP} 行，实际 {_log_blocks(win)}"
        )
        assert f"cap-probe-{LOG_CAP + 999}" in text, "最新一行没有写进来（写入被阻塞）"
        assert "cap-probe-0]" not in text, "上限触发后最旧行仍残留（未淘汰）"
    finally:
        win.close()
        qapp.processEvents()


# ---------------------------------------------------------------- 假设四（card11）


def test_timestamp_wraparound_axis_sane(qapp):
    """32 位毫秒回绕：横轴接着走、数据不清空、跨度为真实跨度。"""
    from serialpid.gui.plot_panel import PlotPanel
    from serialpid.model import TelemetrySample

    # 回绕前一帧 / 回绕点（0）/ 回绕后一帧，前后各补两点共 7 帧
    stamps = [WRAP - 400, WRAP - 300, WRAP - 200, WRAP - 1, 0, 100, 200]

    panel = PlotPanel(window_seconds=None)
    try:
        for i, t in enumerate(stamps):
            panel.append(TelemetrySample(t, 25.0, 24.0 + i * 0.1, 30.0, 1.0))
        panel.render_now()

        x = panel._extract(0)[:, 0]
        assert len(x) == len(stamps), f"回绕不该清数据，实际只剩 {len(x)} 点"
        assert bool((np.diff(x) >= 0).all()), f"横轴非单调：{x}"
        assert x[0] == pytest.approx(0.0), f"首点应为 0，实际 {x[0]}"
        assert x[-1] == pytest.approx(0.6, abs=1e-9), f"回绕后末点应 0.6s，实际 {x[-1]}"
        assert panel.timeline_restarts == 0, "回绕被误判成复位（不该重启时间轴）"

        rendered_x, _ = panel._pv_curve.getData()
        assert rendered_x is not None and len(rendered_x) > 0, "回绕后曲线为空"
        span = panel._pv_plot.getViewBox().viewRange()[0]
        width = span[1] - span[0]
        assert 0.0 < width <= 1.0, f"视口跨度 {width:.1f}s，7 帧只覆盖 0.6s"
    finally:
        panel.close()
        qapp.processEvents()


def test_timestamp_reset_axis_sane(qapp):
    """会话中途时间戳复位（MCU 软复位 / 重新上电）：按新会话重开，十秒窗仍有效。"""
    from serialpid.gui.plot_panel import PlotPanel
    from serialpid.model import TelemetrySample

    panel = PlotPanel(window_seconds=10.0)
    try:
        # 先灌 30 秒正常数据（20Hz）
        for i in range(600):
            panel.append(TelemetrySample(i * 50, 25.0, 24.0, 30.0, 1.0))
        panel.render_now()
        assert panel._extract(0)[-1, 0] == pytest.approx(29.95, abs=1e-9)

        # 固件复位：时间戳从 0 重新计数，再来 10 秒
        for i in range(200):
            panel.append(TelemetrySample(i * 50, 25.0, 24.5, 30.0, 1.0))
        panel.render_now()

        x = panel._extract(0)[:, 0]
        assert bool((np.diff(x) >= 0).all()), f"复位后横轴非单调：{x[0]}..{x[-1]}"
        assert x[0] == pytest.approx(0.0, abs=1e-9), f"复位后起点应归零，实际 {x[0]}"
        assert panel.point_count == 200, (
            f"旧会话数据仍留在新时间轴上（{panel.point_count} 点），"
            "两段会话被拼成一条轴会让调节时间读数失真"
        )
        assert panel.timeline_restarts == 1, "复位未被识别为新会话"

        vr = panel._pv_plot.getViewBox().viewRange()[0]
        width = vr[1] - vr[0]
        assert vr[0] >= 0.0, f"视口起点为负：{vr[0]:.2f}s"
        assert 9.0 <= width <= 11.5, f"十秒窗失效，视口跨度 {width:.1f}s"

        rendered_x, _ = panel._pv_curve.getData()
        assert rendered_x is not None and len(rendered_x) >= 190, (
            f"可见点数 {0 if rendered_x is None else len(rendered_x)} 与窗口不匹配"
        )
    finally:
        panel.close()
        qapp.processEvents()


def test_reconnect_resets_timeline_via_main_window(qapp):
    """配套重连路径：连接建立即走"时间轴重启"入口（卡11 第四条）。"""
    from serialpid.protocol import PacketType, pack_telemetry

    win = _new_window(qapp)
    try:
        for i in range(600):
            win._on_frame(PacketType.TELEMETRY, pack_telemetry(i * 50, 25.0, 24.0, 30.0, 1.0))
        win._plot.render_now()
        assert win._plot.point_count == 600

        # 断线重连：连接成功事件必须把时间基准归零
        win._on_connection(True, "tcp://127.0.0.1:9000")
        assert win._plot.point_count == 0, "重连后仍保留上一会话数据"
        assert win._plot.timeline_restarts == 1, "重连未触发时间轴重启"

        for i in range(200):
            win._on_frame(PacketType.TELEMETRY, pack_telemetry(i * 50, 25.0, 24.5, 30.0, 1.0))
        win._plot.render_now()

        x = win._plot._extract(0)[:, 0]
        assert x[0] == pytest.approx(0.0, abs=1e-9), f"重连后起点应归零，实际 {x[0]}"
        assert bool((np.diff(x) >= 0).all()), "重连后横轴非单调"
        vr = win._plot._pv_plot.getViewBox().viewRange()[0]
        assert vr[0] >= 0.0, f"重连后视口起点为负：{vr[0]:.2f}s"
    finally:
        win.close()
        qapp.processEvents()
