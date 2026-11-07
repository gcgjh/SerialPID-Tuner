# -*- coding: utf-8 -*-
"""card42 回归：内置模拟器反复重连 50 次，内存 / 句柄 / 线程 / Qt 信号不得累积。

背景：70 秒冒烟压测曾报「内存 120 MB/h、句柄 +7、线程 +4」。拉长到 30 分钟后
RSS 在曲线队列满载（20000 点）后稳定在 111.7 MB，属有界缓冲填充；本用例把
最可疑的「每次重连新建 SimEngine / 传输对象」路径单独拉出来，跑 50 轮并检查：

* 每轮断开后引擎对象必须被回收（weakref 全死）；
* Python 已跟踪内存净增长 < 1 MB；
* 进程句柄漂移 ≤ 4、线程漂移 ≤ 2；
* 本次新起的仿真线程在 3 s 收敛窗口内全部退出（只盯自己起的，避免把并发跑测试时
  别的用例的线程算到自己头上，见 ``_wait_sim_threads_gone`` 的注释）；
* worker 三个信号的 receiver 数在 50 轮前后完全一致（Qt 重复 connect 的经典泄漏）。

运行：cd pc && python -m pytest tests/test_reconnect_resources.py -q
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import weakref

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover - 依赖环境
    _HAS_QT = False

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")

CYCLES = 50
WARMUP = 5
_SIGNATURES = (
    "2frameReceived(int,PyObject)",
    "2connectionChanged(bool,QString)",
    "1errorOccurred(QString)",
)


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _pump(seconds: float) -> None:
    app = _app()
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture()
def win(monkeypatch, tmp_path):
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    monkeypatch.setattr(st, "run_selftest", lambda **_kw: st.SelfTestReport(checks=[]))
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    _app()  # 必须先有 QApplication 再构造任何 QWidget，否则进程静默 abort
    window = mw.MainWindow()
    window.show()
    _app().processEvents()
    yield window
    if window._worker.connected:
        window._connect_btn.click()
    window.close()
    _app().processEvents()


def _one_cycle(win) -> None:
    assert not win._worker.connected, "上一轮没有断开干净"
    win._connect_btn.click()
    _pump(0.12)
    assert win._worker.connected
    assert win._sim_engine is not None
    win._connect_btn.click()
    _pump(0.12)
    assert not win._worker.connected
    assert win._sim_engine is None


def _receiver_counts(win) -> tuple:
    return tuple(win._worker.receivers(sig) for sig in _SIGNATURES)


def _sim_thread_ids() -> set:
    return {
        t.ident for t in threading.enumerate()
        if t.is_alive() and t.name == "SimEngine"
    }


def _wait_sim_threads_gone(expected: set, timeout: float = 3.0) -> set:
    """等本次自己起的仿真线程退出；返回超时后仍在的那些 ident。

    为什么不能直接断言"全进程没有 SimEngine 线程"：这条断言在整套用例并发跑时
    会撞窗口——2026-11-07 卡44 复核实测，断开后引擎对象的 weakref 已全死、
    句柄 362→362、线程 44→44、Python 内存 +0.12 MB、信号 receiver 数不变，
    但 4 条刚收到停止信号的线程还在 ``engine.py:201 time.sleep`` 的切片里，
    断言时刻正好落在它们退出前（线程栈已取证）。此时失败读数与"泄漏"完全矛盾。
    因此这里只盯"本次新起的线程"，并给一个有界收敛窗口；窗口外仍活着就是真残留。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        leftover = _sim_thread_ids() & expected
        if not leftover:
            return set()
        _pump(0.05)
    return _sim_thread_ids() & expected


def test_fifty_reconnects_do_not_grow_resources(win) -> None:
    import tracemalloc

    import psutil

    from serialpid.gui.main_window import CHANNEL_SIM

    win._transport.setCurrentText(CHANNEL_SIM)
    proc = psutil.Process()
    baseline_threads = _sim_thread_ids()

    # 预热：首轮有一次性 socket / Qt 线程池分配，不计入基线
    for _ in range(WARMUP):
        _one_cycle(win)
    _pump(0.3)
    gc.collect()

    receivers_before = _receiver_counts(win)
    handles_before = proc.num_handles()
    threads_before = proc.num_threads()
    started_tm = False
    if not tracemalloc.is_tracing():
        tracemalloc.start(10)
        started_tm = True
    mem_before = tracemalloc.get_traced_memory()[0]

    engines: list[weakref.ReferenceType] = []
    for _ in range(CYCLES - WARMUP):
        engine = win._sim_engine
        _one_cycle(win)
        if engine is not None:
            engines.append(weakref.ref(engine))

    _pump(0.4)
    gc.collect()
    _pump(0.1)

    handles_after = proc.num_handles()
    threads_after = proc.num_threads()
    mem_after = tracemalloc.get_traced_memory()[0]
    receivers_after = _receiver_counts(win)

    alive_engines = [ref for ref in engines if ref() is not None]
    leftover_threads = _wait_sim_threads_gone(
        _sim_thread_ids() - baseline_threads, timeout=3.0
    )

    if started_tm:
        tracemalloc.stop()

    growth_mb = (mem_after - mem_before) / (1024.0 * 1024.0)
    print(
        f"[probe] {CYCLES} 次重连：句柄 {handles_before}→{handles_after}，"
        f"线程 {threads_before}→{threads_after}，Python 内存 "
        f"{mem_before / 1048576.0:.2f}→{mem_after / 1048576.0:.2f} MB，"
        f"信号 receivers {receivers_before}→{receivers_after}，"
        f"未退出的自起线程 {len(leftover_threads)}"
    )
    assert not alive_engines, f"{len(alive_engines)}/{len(engines)} 个 SimEngine 未被回收"
    assert not leftover_threads, (
        f"3 s 收敛窗口内仍有 {len(leftover_threads)} 条本次自起的仿真线程未退出："
        f"{sorted(leftover_threads)}"
    )
    assert receivers_after == receivers_before, (
        f"Qt 信号 receiver 数变化：{receivers_before} → {receivers_after}"
    )
    assert handles_after - handles_before <= 4, (
        f"{CYCLES} 轮重连句柄增长 {handles_before} → {handles_after}"
    )
    assert abs(threads_after - threads_before) <= 2, (
        f"{CYCLES} 轮重连线程漂移 {threads_before} → {threads_after}"
    )
    assert growth_mb < 1.0, f"{CYCLES} 轮重连 Python 内存增长 {growth_mb:.2f} MB"
