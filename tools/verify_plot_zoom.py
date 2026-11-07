"""card7 修复验收：横轴跟随/自由双模式、缩放不复位、视口淘汰保护。

运行：python tools/verify_plot_zoom.py   （需 .venv 环境）
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pc"))

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from serialpid.gui.plot_panel import PlotPanel  # noqa: E402
from serialpid.model import TelemetrySample  # noqa: E402

app = QApplication.instance() or QApplication([])

fails: list[str] = []


def check(name: str, cond: bool, detail: str) -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}: {detail}")
    if not cond:
        fails.append(name)


def feed(panel: PlotPanel, n: int, start_i: int = 0, step_ms: int = 50) -> int:
    """从 start_i 继续喂 n 个点（20Hz），返回下一个序号。"""
    i = start_i
    for _ in range(n):
        i += 1
        t = (i + 1) * step_ms          # 首点 t=100ms → t0=100ms，x 从 0 起
        panel.append(TelemetrySample(t, 25.0, 24.0 + (i % 7) * 0.1, 30.0, 1.0))
    return i


def xrange(panel: PlotPanel) -> tuple[float, float]:
    vr = panel._pv_plot.getViewBox().viewRange()[0]
    return float(vr[0]), float(vr[1])


def zoom_manually(panel: PlotPanel, xl: float, xr: float) -> None:
    """模拟用户框选放大：先设范围，再发 ViewBox 的 manual 信号（真实鼠标路径）。"""
    vb = panel._pv_plot.getViewBox()
    vb.setXRange(xl, xr, padding=0)
    vb.sigRangeChangedManually.emit({"x": True, "y": False})


# ---- 1) 跟随模式基线：2000 点 = 100s，60s 窗口 → 横轴落在尾段 ----
p = PlotPanel()          # 默认窗口 60s，cap 20000
feed(p, 2000)
p.render_now()
x0, x1 = xrange(p)
check("跟随模式基线", p.is_following() and x1 > 99.0 and (x1 - x0) < 65.0,
      f"跟随中，横轴 {x0:.1f}~{x1:.1f}s（窗口 60s、数据 0~99.95s）")

# ---- 2) 用户手动缩放到 10–20s → 自动进入自由模式 ----
zoom_manually(p, 10.0, 20.0)
check("手动缩放进入自由模式", not p.is_following(), "is_following()=False")
check("工具条显示已暂停跟随", "已暂停跟随" in p._mode_label.text(),
      f"状态文案「{p._mode_label.text()}」")

# ---- 3) 连续 100 帧渲染，横轴不回弹 ----
i = 2000
worst = 0.0
for _ in range(100):
    i = feed(p, 1, i)
    p.render_now()
    xa, xb = xrange(p)
    worst = max(worst, abs(xa - 10.0), abs(xb - 20.0))
check("100 帧后横轴不回弹", worst < 1e-6,
      f"最大偏移 {worst:.6f}s（应恒为 10.0~20.0）")

# ---- 4) 自由模式下曲线继续更新（数据在变、坐标不动）----
xv, yv = p._pv_curve.getData()
check("自由模式数据在视口内可见", len(yv) > 0 and float(xv[-1]) >= 19.9,
      f"绘制点数 {len(yv)}，覆盖到 x={float(xv[-1]):.2f}s")
xa0, xb0 = xrange(p)
cnt0 = p.point_count
i = feed(p, 20, i)
p.render_now()
xa1, xb1 = xrange(p)
check("自由模式坐标不动、曲线仍更新",
      abs(xa0 - xa1) < 1e-6 and abs(xb0 - xb1) < 1e-6 and p.point_count == cnt0 + 20,
      f"横轴保持 {xa1:.2f}~{xb1:.2f}s，点数 {cnt0}→{p.point_count}")

# ---- 5) 跟随最新：一键恢复滚动 ----
p._follow_btn.click()
p.render_now()
xa, xb = xrange(p)
check("跟随最新恢复滚动", p.is_following() and xb > 101.0 and (xb - xa) < 65.0,
      f"跟随中，横轴 {xa:.1f}~{xb:.1f}s")
check("跟随最新后文案复位", "已暂停跟随" not in p._mode_label.text(),
      f"状态文案「{p._mode_label.text()}」")

# ---- 6) 时间窗切到 30s → 自动回到跟随 ----
zoom_manually(p, 10.0, 20.0)
check("再次进入自由模式", not p.is_following(), "")
idx30 = [i for i, (lb, _s) in enumerate(PlotPanel.WINDOW_PRESETS) if lb == "30s"][0]
p._window_box.setCurrentIndex(idx30)   # 触发 currentIndexChanged
p.render_now()
xa, xb = xrange(p)
check("换窗口自动回跟随", p.is_following() and (xb - xa) < 35.0 and xb > 101.0,
      f"30s 窗口，横轴 {xa:.1f}~{xb:.1f}s")

# ---- 7) 适应全部：横轴拉到全量范围 ----
zoom_manually(p, 10.0, 20.0)
p._fit_btn.click()
p.render_now()
full = p._extract(0)
xa, xb = xrange(p)
f0, f1 = float(full[0, 0]), float(full[-1, 0])
check("适应全部覆盖全量数据", (not p.is_following()) and xa <= f0 + 1e-6 and xb >= f1 - 1e-6,
      f"横轴 {xa:.2f}~{xb:.2f}s，数据 {f0:.2f}~{f1:.2f}s")

# ---- 8) 暂停与自由组合：状态可区分，暂停不重绘 ----
p._pause_btn.click()                 # 暂停
p._pause_btn.click()                 # 继续
p._pause_btn.click()                 # 再暂停
check("暂停+自由文案区分", "已暂停重绘" in p._mode_label.text()
      and "已暂停跟随" in p._mode_label.text(),
      f"状态文案「{p._mode_label.text()}」")
cnt_before = p.point_count
i = feed(p, 10, i)                   # 暂停期间数据照常入队
check("暂停期间数据仍入队", p.point_count == cnt_before + 10,
      f"{cnt_before} → {p.point_count} 点")
p._pause_btn.click()                 # 继续
p._dirty = True
p._on_timer()
xa2, xb2 = xrange(p)
check("继续后仍停在自由视口", abs(xa2 - xa) < 1e-6 and abs(xb2 - xb) < 1e-6,
      f"横轴 {xa2:.2f}~{xb2:.2f}s 不变")

# ---- 9) 纵轴自动/手动与横轴模式正交 ----
p._follow_btn.click()                # 先回跟随
p.render_now()
p._yauto_check.setChecked(False)
p._yauto_check.setChecked(True)
check("纵轴开关不影响横轴模式", p.is_following(),
      "切换纵轴自动/手动后仍是跟随模式")

# ---- 10) 视口落在已淘汰区间：自动前移并提示，不静默变白 ----
p2 = PlotPanel(max_points=20)
feed(p2, 20)                         # 20 点 → x 0~0.95s
p2.render_now()
zoom_manually(p2, 0.0, 0.5)
p2.render_now()
i2 = feed(p2, 30, 20)                # 再进 30 点，队首推进到 x≈1.5s，原视口全被淘汰
p2.render_now()
xa, xb = xrange(p2)
full2 = p2._extract(0)
f0, f1 = float(full2[0, 0]), float(full2[-1, 0])
check("视口全被淘汰时跟到数据前沿",
      xa <= f0 + 1e-9 and xb >= f1 - 1e-9 and (xb - xa) < 1.2,
      f"视口 {xa:.3f}~{xb:.3f}s 覆盖数据 {f0:.3f}~{f1:.3f}s（带 2% 边距）")
check("淘汰提示不静默", "淘汰" in p2._cap_hint.text(),
      f"提示「{p2._cap_hint.text()}」")

# ---- 11) 视口左半被淘汰：只提示、不抢视口 ----
p2.render_now()
vb = p2._pv_plot.getViewBox()
vb.setXRange(f0 - 0.1, f1, padding=0)
vb.sigRangeChangedManually.emit({"x": True, "y": False})
p2.render_now()
check("部分淘汰给提示", "淘汰" in p2._cap_hint.text(),
      f"提示「{p2._cap_hint.text()}」")
xa, xb = xrange(p2)
check("部分淘汰不抢视口", abs(xa - (f0 - 0.1)) < 1e-6,
      f"视口保持 {xa:.3f}~{xb:.3f}s")

# ---- 12) 清空回到跟随，避免旧时间基准造成空白 ----
p2.clear()
check("清空回到跟随模式", p2.is_following(), "clear() 后 is_following()=True")

print()
if fails:
    print("验收失败项:", fails)
    sys.exit(1)
print("card7 全部验收项通过")
