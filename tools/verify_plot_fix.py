"""card2 修复验收：直接对照缺陷卡第八节验收标准。

运行：python tools/verify_plot_fix.py   （需 .venv 环境）
"""

from __future__ import annotations

import os
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from serialpid.gui.plot_panel import PlotPanel  # noqa: E402
from serialpid.model import TelemetrySample  # noqa: E402

app = QApplication.instance() or QApplication([])

fails: list[str] = []


def check(name: str, cond: bool, detail: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}: {detail}")
    if not cond:
        fails.append(name)


def feed(panel: PlotPanel, n: int, step_ms: int = 50, peak_at: int | None = None,
         peak_val: float = 0.0, sp: float = 25.0, start_ms: int = 0) -> None:
    """连续喂 n 个点。

    ``start_ms`` 必须接着上一批的时间戳往下走：card11 之后，时间戳倒退
    会被判成"MCU 复位"，清空重开时间轴（这是正确行为，不是缺陷）。
    分批喂数据时若每次从 0 重新计时，等于每批都伪造一次复位。
    """
    t = start_ms
    for i in range(n):
        t += step_ms
        pv = 24.0 + (i % 7) * 0.1
        if peak_at is not None and i == peak_at:
            pv = peak_val
        panel.append(TelemetrySample(t, sp, pv, 30.0, sp - pv))


# ---- 1) 7 万点喂入，进程存活，内部长度恒 <= 上限 ----
p = PlotPanel()   # 默认 cap=20000
feed(p, 70000)
check("70k 存活且有限长", p.point_count == 20000,
      f"保留 {p.point_count} 点（cap=20000，超出自动丢最旧）")

# ---- 2) 单帧渲染耗时 < 3ms（上限路径：窗口=全部 → 20000 点 → 像素桶抽稀）----
p2 = PlotPanel(window_seconds=None)
feed(p2, 70000)
for _ in range(5):   # 热启动（pyqtgraph 首帧建曲线/图例不计入稳态耗时）
    p2.render_now()
costs: list[float] = []
for _ in range(30):
    t0 = time.perf_counter()
    p2.render_now()
    costs.append((time.perf_counter() - t0) * 1000)
avg = statistics.mean(costs)
mx = max(costs)
check("渲染耗时 < 3ms", mx < 3.0,
      f"30 次单帧平均 {avg:.2f}ms / 最大 {mx:.2f}ms（20000 点抽稀后绘制）")

# ---- 3) 时间窗 5s：视图范围正确 + 超调尖峰仍可见 ----
p3 = PlotPanel(window_seconds=5.0)
feed(p3, 400, peak_at=120, peak_val=27.5)   # 20Hz * 5s = 100 点窗口
p3.render_now()
xv, yv = p3._pv_curve.getData()
xspan = float(xv[-1] - xv[0])
check("5s 窗口范围正确", xspan <= 5.05 and xspan >= 4.5,
      f"窗口跨度 {xspan:.2f}s（应为 ~5s）")

# 尖峰可见性需要触发抽稀：窗口=全部 → 20000 点中含超调尖峰
p4 = PlotPanel(window_seconds=None)
feed(p4, 20000, peak_at=9976, peak_val=27.5)
p4.render_now()
_xv, yv = p4._pv_curve.getData()
check("抽稀保留超调尖峰(min/max 桶)", float(np.max(yv)) >= 27.49,
      f"渲染后 PV max={float(np.max(yv)):.2f}（原始峰值 27.5，等间隔取样会抹平）")

# ---- 4) 暂停 10 秒再继续：数据照常入队，恢复后曲线连续无缺口 ----
p5 = PlotPanel()
feed(p5, 200)                     # 10s
p5._toggle_pause()                # 暂停
check("暂停状态生效", p5.is_paused(), "")
feed(p5, 200, start_ms=10000)     # 暂停期间再入 200 点（时间戳接着走，别伪造复位）
check("暂停期间数据照常入队", p5.point_count == 400,
      f"队列 {p5.point_count} 点（暂停不清空、不丢点）")
p5._toggle_pause()                # 恢复
p5._dirty = True
p5._on_timer()                    # 补一帧
xs_raw = p5._extract(0)[:, 0]
diffs = np.diff(xs_raw)
check("恢复后曲线连续无缺口", float(np.max(diffs)) <= 0.0501,
      f"最大点间隔 {float(np.max(diffs)) * 1000:.2f}ms（20Hz 名义 50ms）")

# ---- 5) 清空重置时间基准 ----
p5.clear()
p5.append(TelemetrySample(100000, 25.0, 24.0, 30.0, 1.0))
x_after = float(p5._extract(0)[0, 0])
check("清空后时间基准重置", abs(x_after) < 1e-6,
      f"清空后首点 x={x_after:.3f}s（应从 0 起，横轴不跳变）")

# ---- 6) 纵轴自动/手动开关不抛异常 ----
p5._yauto_check.setChecked(False)
p5._yauto_check.setChecked(True)
check("纵轴自动/手动切换", True, "开关无异常")

# ---- 7) 环形队列满后滚动淘汰正确性（乱序错位检查）----
p6 = PlotPanel(max_points=100)
feed(p6, 500)
x6 = p6._extract(0)[:, 0]
check("环形淘汰后时间单调", bool(np.all(np.diff(x6) > 0)),
      f"满队列滚动后 {len(x6)} 点仍按时间升序（环形不串位）")
# t0=首样本 50ms 起算：500 次 append 保留最后 100 次（#401..#500）
# 首点 x=(401*50-50)/1000=20.0，末点 x=(500*50-50)/1000=24.95
check("环形淘汰数量正确",
      p6.point_count == 100 and abs(float(x6[0]) - 20.0) < 1e-6 and abs(float(x6[-1]) - 24.95) < 1e-6,
      f"保留区间 x ∈ [{float(x6[0]):.3f}, {float(x6[-1]):.3f}]（应为 [20.000, 24.950]，共 100 点）")

print()
if fails:
    print("验收失败项:", fails)
    sys.exit(1)
print("card2 全部验收项通过")
