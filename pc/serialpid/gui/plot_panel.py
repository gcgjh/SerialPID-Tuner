"""实时曲线面板：上半 SP/PV，下半 OUT。

v4（card11 修复 · 时间轴回绕 / 复位）：
  内部横轴不再是"当前样本原始毫秒 - 首个样本毫秒"（那个式子隐含"时间戳永不
  回退、永不过 32 位边界、基准永远有效"三个前提，真实硬件三样都不成立）。
  现在用"原始 u32 毫秒 + 回绕补偿 - 会话零点"，并且区分两种负跳变：
    * 回绕（差值约 -2^32，跑满 49.7 天一次）：补偿一个周期，数据接着画；
    * 复位（差值是小幅负数：MCU 重新上电 / 软复位 / 换板子）：判为新会话，
      清空已画数据、零点重置到当前帧，横轴从 0 重新计时，绝不把两段会话
      拼到同一条时间轴（那会让调节时间读数失真）。
  时间轴重启只有一个入口 ``restart_timeline``：连接建立、检测到复位、
  手动清空三条路径都走它，避免只修好其中一条。

v3（card7 修复 · 横轴缩放救回）：
  横轴有"跟随 / 自由"两种模式，与纵轴自动/手动、暂停互不相干：
    * 跟随：每帧按时间窗滚动横轴（旧行为），适合实时盯盘；
    * 自由：横轴完全交给用户。用鼠标滚轮 / 拖动 / 框选缩放即自动进入
      "已暂停跟随"，横轴不再被每帧设定，放大看超调段不会弹回。
  工具条：跟随最新（一键恢复滚动）、适应全部（横轴拉到全量数据）、
  以及"已暂停重绘 / 已暂停跟随"组合状态显示。
  自由模式下按用户视口切片再抽稀（视口内保持全分辨率）；视口若落在已被
  队列淘汰的区间，自动把视口跟到数据前沿并给出提示，不会静默变白。

v2（card2 修复）：
  1. 数据有上限：曲线数据存为有上限的环形队列，默认 20000 点（可配置），
     超出自动丢弃最旧点 —— 杜绝无限增长导致进程被系统杀掉。
  2. 不再每帧全量重画：append 只入队 + 打脏标记，30fps 定时器统一刷新，
     新数据到达频率（20Hz）远低于刷新频率（30fps），重画次数与数据量解耦。
  3. 时间窗：重画只取最近 N 秒切片；点数超过画布像素宽 2 倍时按像素桶
     抽稀，且每个桶同时保留 min/max —— 阶跃的超调尖峰不会被等间隔取样
     平滑掉，超调量 / 峰值时间等整定读数不失真。
  4. 面板内自带工具条：暂停 / 继续、清空、时间窗选择、纵轴自动 / 手动。
  5. 暂停期间数据照常入队，只是不画；恢复时用最新切片补齐，不丢点。
  6. 清空时重置时间基准，避免横轴跳变。
  7. 时间窗与队列上限冲突时以上限为准，工具条给出灰色提示。

性能说明（3ms 预算的取舍）：
  - 存储用预分配的 numpy 环形队列：append 写入 O(1)、淘汰 O(1)，重画时
    按时间窗切出 numpy 切片（C 级 memcpy），全程不再有 Python list 拷贝 /
    逐元素转 array —— 这是 2 万点单帧 <3ms 的关键。
  - 抽稀到约 2×像素宽后交给 pyqtgraph，画布只处理最多几千个点。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import TelemetryBuffer, TelemetrySample

_UNSET = object()  # 区分"未传参"与"显式传 None(=全部窗口)"

# 时间轴常量（card11）：
# 协议时间戳是 32 位无符号毫秒，一个完整周期约 49.7 天。
_U32_SPAN = 1 << 32
_U32_MASK = _U32_SPAN - 1
# 负跳变的判据门槛：幅度达到半个周期才算"回绕"，否则一律按"复位"处理。
# 回绕的真实差值接近 -2^32；复位的差值只是"从当前进度跳回 0"的小幅负数。
# 这个门槛把两者分开——千万不要改成"取模"：复位时取模会得到一个巨大的正数。
_WRAP_GUARD = 1 << 31

_FOLLOW_COLOR = "#888888"
_FREE_COLOR = "#e6a23c"


class PlotPanel(QWidget):
    DEFAULT_MAX_POINTS = 20000      # 与 model.TelemetryBuffer 默认上限一致
    DEFAULT_WINDOW_SEC = 60.0
    DEFAULT_FPS = 30
    FALLBACK_WIDTH = 1280           # 离屏/未布局时无法取真实像素宽，用保守值

    # (显示名, 窗口秒数)；None = 全部（仍受队列上限约束）
    WINDOW_PRESETS: Tuple[Tuple[str, Optional[float]], ...] = (
        ("10s", 10.0),
        ("30s", 30.0),
        ("60s", 60.0),
        ("120s", 120.0),
        ("全部", None),
    )

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        max_points: Optional[int] = None,
        window_seconds: object = _UNSET,  # 不传=默认 60s；传 None=全部（受上限约束）
        refresh_fps: Optional[int] = None,
        buffer: Optional[TelemetryBuffer] = None,
    ) -> None:
        super().__init__(parent)

        self._cap = int(max_points or self.DEFAULT_MAX_POINTS)
        if self._cap < 16:
            self._cap = 16
        if window_seconds is _UNSET:
            self._window: Optional[float] = self.DEFAULT_WINDOW_SEC
        else:
            self._window = window_seconds  # type: ignore[assignment]
        self._fps = int(refresh_fps or self.DEFAULT_FPS)

        # 共享模型缓冲（可选）：接线后与主窗口/日志看到同一份有上限数据
        self._buf: Optional[TelemetryBuffer] = buffer

        # 渲染源：numpy 环形队列 [t_sec, sp, pv, out]，长度恒 <= cap，
        # append/淘汰 O(1)，重画按时间窗切 numpy 切片（无 Python 逐点拷贝）
        self._ring: np.ndarray = np.empty((self._cap, 4), dtype=np.float64)
        self._count = 0
        self._head = 0

        # 时间轴状态（card11）：内部 x = (原始 u32 毫秒 + 回绕补偿 - 会话零点) / 1000。
        # 三件事分开存，改之前先读 _advance_timeline 的注释：
        self._t_last_raw: Optional[int] = None   # 上一帧原始时间戳（已按 u32 取模）
        self._t_wrap_offset: int = 0             # 回绕累计补偿，每回绕一次 +2^32
        self._t_base: Optional[int] = None       # 会话零点（原始 + 补偿），换会话即更新
        self._timeline_restarts: int = 0         # 时间轴重启次数（诊断 / 测试用）
        self._timeline_note: str = ""            # 最近一次重启原因，工具条短暂显示

        self._dirty = False
        self._paused = False
        self._y_auto = True
        self._follow = True      # 横轴模式：True=跟随最新，False=自由（用户掌控）

        self._build_ui()

        # 统一刷新定时器：新数据只置脏标记，30fps 兜底重画
        self._timer = QTimer(self)
        self._timer.setInterval(max(16, int(round(1000.0 / self._fps))))
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()

        self._update_mode_label()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ---- 工具条：暂停 / 清空 / 时间窗 / 纵轴 / 横轴模式 / 提示 ----
        self._pause_btn = QPushButton("暂停")
        self._pause_btn.setFixedWidth(52)
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setFixedWidth(52)

        self._window_box = QComboBox()
        for label, _sec in self.WINDOW_PRESETS:
            self._window_box.addItem(label)
        # 当前窗口秒数选中对应项
        current = self._window
        for i, (_label, sec) in enumerate(self.WINDOW_PRESETS):
            if sec == current:
                self._window_box.setCurrentIndex(i)
                break

        self._yauto_check = QCheckBox("纵轴自动")
        self._yauto_check.setChecked(True)

        self._follow_btn = QPushButton("跟随最新")
        self._fit_btn = QPushButton("适应全部")

        self._mode_label = QLabel("")
        self._mode_label.setStyleSheet(f"color: {_FOLLOW_COLOR}; font-size: 11px;")

        self._cap_hint = QLabel("")
        self._cap_hint.setStyleSheet("color: #888888; font-size: 11px;")

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(6)
        bar.addWidget(self._pause_btn)
        bar.addWidget(self._clear_btn)
        bar.addWidget(QLabel("时间窗"))
        bar.addWidget(self._window_box)
        bar.addWidget(self._yauto_check)
        bar.addWidget(self._follow_btn)
        bar.addWidget(self._fit_btn)
        bar.addWidget(self._mode_label)
        bar.addWidget(self._cap_hint, stretch=1)
        layout.addLayout(bar)

        # ---- 曲线 ----
        self._pv_plot = pg.PlotWidget(title="设定值 / 过程量")
        self._pv_plot.showGrid(x=True, y=True, alpha=0.3)
        self._sp_curve = self._pv_plot.plot(pen=pg.mkPen("#999999", style=Qt.DashLine), name="SP")
        self._pv_curve = self._pv_plot.plot(pen=pg.mkPen("#2ecc71", width=2), name="PV")
        self._pv_plot.addLegend()

        self._out_plot = pg.PlotWidget(title="输出 OUT")
        self._out_plot.showGrid(x=True, y=True, alpha=0.3)
        self._out_curve = self._out_plot.plot(pen=pg.mkPen("#e67e22", width=2), name="OUT")
        self._out_plot.setLabel("bottom", "时间", units="s")

        layout.addWidget(self._pv_plot, stretch=3)
        layout.addWidget(self._out_plot, stretch=1)

        self._pause_btn.clicked.connect(self._toggle_pause)
        self._clear_btn.clicked.connect(self.clear)
        self._window_box.currentIndexChanged.connect(self._on_window_changed)
        self._yauto_check.toggled.connect(self._on_yauto_toggled)
        self._follow_btn.clicked.connect(self.follow_latest)
        self._fit_btn.clicked.connect(self.fit_all)

        # 用户鼠标缩放 / 拖动 / 框选 → 自动进入自由模式（程序 setXRange 不触发本信号）
        for plot in (self._pv_plot, self._out_plot):
            plot.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)

    # ------------------------------------------------------------------ API

    @property
    def point_count(self) -> int:
        return self._count

    def is_paused(self) -> bool:
        return self._paused

    def is_following(self) -> bool:
        """横轴当前是否处于跟随模式。"""
        return self._follow

    def follow_latest(self) -> None:
        """回到跟随模式：横轴按时间窗滚动。"""
        self._follow = True
        self._update_mode_label()
        self._dirty = True

    def fit_all(self) -> None:
        """适应全部：横轴拉到全量数据范围，并停在自由模式（否则下一帧会弹回）。"""
        self._follow = False
        self._update_mode_label()
        if self._count > 0:
            full = self._extract(0)
            x0 = float(full[0, 0])
            x1 = float(full[-1, 0])
            pad = (x1 - x0) * 0.02 if x1 > x0 else 1.0
            for plot in (self._pv_plot, self._out_plot):
                plot.getViewBox().setXRange(x0 - pad, x1 + pad, padding=0)
        self._dirty = True

    def append(self, sample: TelemetrySample) -> None:
        x_sec = self._advance_timeline(int(sample.t_ms))
        row = (x_sec, sample.sp, sample.pv, sample.out)
        if self._count < self._cap:
            self._ring[(self._head + self._count) % self._cap] = row
            self._count += 1
        else:
            # 队列满：头指针前进一格，新点覆盖最旧点（淘汰 O(1)）
            self._head = (self._head + 1) % self._cap
            self._ring[(self._head + self._count - 1) % self._cap] = row
        if self._buf is not None:
            self._buf.append(sample)
        self._dirty = True   # 只置标记，重画交给定时器

    def restart_timeline(self, note: str = "时间轴已重启") -> None:
        """时间轴重启的唯一对外入口（card11）。

        三条路径都走这里，不要各写各的临时处理：
          * 连接建立（``MainWindow`` 收到连接成功）——重连即新会话；
          * 收到复位后的第一帧（``_advance_timeline`` 内部判定）；
          * 用户点"清空"（``clear`` 委托到这里）。
        语义：丢弃已画数据、零点归零、回到跟随模式。若不丢弃旧数据，新会话
        的点会落在旧的 x 轴上，横轴非单调，二分切片与抽稀分桶同时失效。
        """
        if self._buf is not None:
            self._buf.clear()
        self._restart_timeline(note)

    def clear(self) -> None:
        """手动清空：与重连 / 复位共用同一入口，避免只修好其中一条路径。"""
        self.restart_timeline("手动清空，时间轴从 0 重新计时")

    def _restart_timeline(self, note: str) -> None:
        """重置时间轴状态（不照顾外部缓冲，由 restart_timeline 统一负责）。"""
        self._count = 0
        self._head = 0
        self._t_base = None
        self._t_last_raw = None
        self._t_wrap_offset = 0
        # 时间基准重置后，自由模式的旧视口已无意义，回到跟随避免空白
        self._follow = True
        self._timeline_restarts += 1
        self._timeline_note = note
        self._update_mode_label()
        self._dirty = True

    def _advance_timeline(self, t_ms: int) -> float:
        """把原始 u32 毫秒推进成单调递增的内部横轴（秒）。

        判据全部写在这里，改错之前请先读完：

        * **回绕**：时间戳从 0xFFFFFFFF 附近滚到 0，差值约等于 -2^32
          （-42.9 亿毫秒），判据 ``差值 <= -2^31``（幅度达到半个周期）。
          处理是给补偿加一个完整周期，时间轴接着走、数据不清——这是同一场
          会话里的正常现象，约 49.7 天出现一次。
        * **复位**：MCU 重新上电 / 软复位 / 换板子后时间戳从 0 重新计数，
          差值是一个幅度远小于半个周期的负数（判据与回绕互补）。处理是按
          新会话重开时间轴：清掉旧数据、零点挪到当前帧、横轴从 0 起算。
          之所以不"补偿后接着走"，是因为两段数据分属不同会话，拼在同一条
          轴上会让调节时间、峰值时间这些读数失真。

        绝不能改成"当前减首个再取模"：回绕时对，复位时会把负差值映射成
        一个巨大的正数，曲线直接飞出屏幕（比当前缺陷更糟）。
        """
        raw = t_ms & _U32_MASK
        if self._t_base is None:
            # 首帧 / 刚重启：本帧即零点
            self._t_base = raw
            self._t_wrap_offset = 0
            self._t_last_raw = raw
            return 0.0

        delta = raw - self._t_last_raw  # 可负，两个 u32 相减在 Python 里不溢出
        if delta < 0:
            if delta >= -_WRAP_GUARD:
                # 复位：新会话。清数据后本帧作为新零点。
                self.restart_timeline("检测到时间戳复位，横轴已从 0 重新计时")
                self._t_base = raw
                self._t_last_raw = raw
                return 0.0
            # 回绕：同一会话，补偿一个完整周期后继续
            self._t_wrap_offset += _U32_SPAN

        self._t_last_raw = raw
        # 整数做加减，结果精确；毫秒转秒只在最后做一次，长跑无累积漂移
        return (raw + self._t_wrap_offset - self._t_base) / 1000.0

    @property
    def timeline_restarts(self) -> int:
        """时间轴重启次数（连接建立 / 检测到复位 / 手动清空各计一次）。"""
        return self._timeline_restarts

    def render_now(self) -> None:
        """同步重画一帧（测试 / 手动刷新用）。"""
        self._dirty = False
        self._render()

    def _extract(self, lo: int = 0) -> np.ndarray:
        """按逻辑顺序取出 [lo, count) 区间的环形数据（副本，(m,4)）。"""
        start = self._head + lo
        end = self._head + self._count
        if end <= self._cap:
            return self._ring[start:end].copy()
        if start >= self._cap:
            return self._ring[start - self._cap: end - self._cap].copy()
        return np.concatenate((self._ring[start:self._cap], self._ring[0:end - self._cap]))

    # ------------------------------------------------------------------ 状态

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause_btn.setText("继续" if self._paused else "暂停")
        if not self._paused:
            # 恢复时立即补齐一帧（数据在暂停期间已照常入队）
            self._dirty = True
        self._update_mode_label()

    def _on_window_changed(self, _index: int) -> None:
        self._window = self.WINDOW_PRESETS[self._window_box.currentIndex()][1]
        # 换窗口 = 用户想重新跟随一个新跨度，自动回到跟随模式
        self._follow = True
        self._update_mode_label()
        self._dirty = True

    def _on_yauto_toggled(self, checked: bool) -> None:
        # 纵轴与横轴模式正交：只动 y 的自动范围
        self._y_auto = checked
        for plot in (self._pv_plot, self._out_plot):
            plot.getViewBox().enableAutoRange(y=checked)

    def _on_manual_range(self, axes) -> None:
        """用户鼠标缩放 / 平移 → 横轴进入自由模式，并把另一张图的横轴同步过来。"""
        x_changed = True
        try:
            getter = getattr(axes, "get", None)
            if callable(getter):
                x_changed = bool(getter("x"))
        except Exception:  # 信号参数形态变化时宁可进入自由模式，不要弹回
            x_changed = True
        if not x_changed:
            return

        self._follow = False
        self._update_mode_label()
        self._dirty = True

        # 两张图的横轴同段：用户在哪张图上缩放，另一张跟过去
        source = self.sender()
        if source is not None:
            try:
                xl, xr = source.viewRange()[0]
            except Exception:
                return
            for plot in (self._pv_plot, self._out_plot):
                vb = plot.getViewBox()
                if vb is not source:
                    vb.setXRange(xl, xr, padding=0)

    def _update_mode_label(self) -> None:
        """工具条状态：区分"不重绘"与"横轴自由"，两者可同时成立。"""
        parts = []
        if self._paused:
            parts.append("已暂停重绘")
        parts.append("跟随最新" if self._follow else "已暂停跟随")
        self._mode_label.setText(" · ".join(parts))
        color = _FOLLOW_COLOR if self._follow else _FREE_COLOR
        self._mode_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _on_timer(self) -> None:
        if self._dirty and not self._paused:
            self._dirty = False
            self._render()

    # ------------------------------------------------------------------ 渲染

    @staticmethod
    def _decimate_rows(
        xa: np.ndarray,
        ymat: np.ndarray,  # (3, n)
        n_buckets: int,
    ) -> Tuple[np.ndarray, List[np.ndarray], bool]:
        """像素桶抽稀：每桶保留 min 与 max，返回 (x, [y_sp,y_pv,y_out], 是否抽稀)。

        点数 <= 2×桶数时不抽稀；否则按 x 跨度均分桶，桶内 min/max 各取一点，
        x 取桶中心 —— 阶跃超调尖峰不会被等间隔取样抹平。三条曲线共享同一
        份 x 桶划分，min/max 一次 reduceat(axis=1) 算完。
        """
        n = len(xa)
        rows = ymat.shape[0]
        if n <= 2 * n_buckets or n_buckets <= 1:
            return xa, [ymat[r] for r in range(rows)], False

        span = float(xa[-1] - xa[0])
        if span <= 0.0:
            return xa, [ymat[r] for r in range(rows)], False

        # 每个点落入哪个桶（xa 单调，idx 单调不减）
        idx = np.clip((xa - xa[0]) / span * n_buckets, 0.0, float(n_buckets - 1)).astype(np.int64)
        starts = np.searchsorted(idx, np.arange(n_buckets), side="left")
        ends = np.searchsorted(idx, np.arange(n_buckets), side="right")
        mask = ends > starts
        s = starts[mask]
        e = ends[mask]
        xc = (xa[s] + xa[e - 1]) * 0.5

        ymin = np.minimum.reduceat(ymat, s, axis=1)
        ymax = np.maximum.reduceat(ymat, s, axis=1)

        m = len(s)
        outx = np.empty(2 * m, dtype=float)
        outx[0::2] = xc
        outx[1::2] = xc
        outys: List[np.ndarray] = []
        for r in range(rows):
            outy = np.empty(2 * m, dtype=float)
            outy[0::2] = ymin[r]
            outy[1::2] = ymax[r]
            outys.append(outy)
        return outx, outys, True

    def _set_shared_x_range(self, xl: float, xr: float) -> None:
        for plot in (self._pv_plot, self._out_plot):
            plot.getViewBox().setXRange(xl, xr, padding=0)

    def _slice_for_view(self, full: np.ndarray, xl: float, xr: float) -> np.ndarray:
        """自由模式：按用户视口切片（视口内保持全分辨率），两端各留 1 点。"""
        xs = full[:, 0]
        lo = int(np.searchsorted(xs, xl, side="left")) - 1
        hi = int(np.searchsorted(xs, xr, side="right")) + 1
        lo = max(0, lo)
        hi = min(len(xs), hi)
        if hi - lo < 2:
            lo = min(max(0, lo), max(0, len(xs) - 2))
            hi = min(len(xs), lo + 2)
        return full[lo:hi]

    def _render(self) -> None:
        n = self._count
        width = max(self.FALLBACK_WIDTH, self._pv_plot.width())
        n_buckets = max(8, width)

        if n == 0:
            for curve in (self._sp_curve, self._pv_curve, self._out_curve):
                curve.setData([], [])
            self._cap_hint.setText("")
            return

        full = self._extract(0)
        x_first = float(full[0, 0])
        x_end = float(full[-1, 0])
        hint = ""

        if self._follow:
            # ---- 跟随模式：按时间窗滚动，横轴由本函数按帧设定 ----
            lo = 0
            if self._window is not None:
                cutoff = x_end - self._window
                if cutoff > x_first:
                    lo = int(np.searchsorted(full[:, 0], cutoff, side="left"))
            data = full if lo == 0 else self._extract(lo)
        else:
            # ---- 自由模式：横轴完全交给用户，按视口切片重绘 ----
            vr = self._pv_plot.getViewBox().viewRange()[0]
            xl, xr = float(vr[0]), float(vr[1])
            if xr < x_first:
                # 视口整个落在已被淘汰的区间：画面会全白，把视口跟到数据前沿
                span_all = x_end - x_first
                pad = span_all * 0.02 if span_all > 0 else 1.0
                self._set_shared_x_range(
                    x_first - pad, (x_end + pad) if span_all > 0 else x_first + 1.0
                )
                vr = self._pv_plot.getViewBox().viewRange()[0]
                xl, xr = float(vr[0]), float(vr[1])
                hint = "视口已随数据前移（原区间数据已被淘汰）"
            elif xl < x_first:
                hint = "视口左侧数据已被淘汰（超出队列上限）"
            data = self._slice_for_view(full, xl, xr)

        xw = data[:, 0]
        ymat = data[:, 1:].T  # (3, m)：sp, pv, out
        x0 = float(xw[0])
        span = float(xw[-1] - x0)

        # ---- 共享 x 的像素桶抽稀（三条曲线一次算完）----
        outx, outys, dec = self._decimate_rows(xw, ymat, n_buckets)
        connect = "pairs" if dec else "all"

        self._sp_curve.setData(outx, outys[0], connect=connect)
        self._pv_curve.setData(outx, outys[1], connect=connect)
        self._out_curve.setData(outx, outys[2], connect=connect)

        # ---- 纵轴照常按自动/手动；横轴只有跟随模式才按帧设定 ----
        for plot in (self._pv_plot, self._out_plot):
            plot.getViewBox().enableAutoRange(x=False, y=self._y_auto)
        if self._follow:
            pad = span * 0.02 if span > 0 else 1.0
            left = x0 - pad
            if x0 >= 0.0:
                # 会话从 0 起算时，2% padding 不许把横轴推成负区间（card11 验收）
                left = max(0.0, left)
            self._set_shared_x_range(left, x_end + pad)
        else:
            # 自由模式：不碰横轴，但两张图必须同段（防止一处缩放另一处不同步）
            vr = self._pv_plot.getViewBox().viewRange()[0]
            xl, xr = float(vr[0]), float(vr[1])
            ovr = self._out_plot.getViewBox().viewRange()[0]
            if abs(float(ovr[0]) - xl) > 1e-9 or abs(float(ovr[1]) - xr) > 1e-9:
                self._out_plot.getViewBox().setXRange(xl, xr, padding=0)

        # ---- 提示：时间轴重启 > 淘汰 > 上限 ----
        if self._timeline_note and x_end >= 3.0:
            self._timeline_note = ""   # 新会话已跑满 3 秒，重启提示让位给常规提示
        at_cap = self._count >= self._cap or (self._buf is not None and len(self._buf) >= self._cap)
        if self._timeline_note:
            self._cap_hint.setText(self._timeline_note)
        elif hint:
            self._cap_hint.setText(hint)
        elif at_cap and self._follow:
            self._cap_hint.setText("已达数据上限，时间窗按上限截断")
        else:
            self._cap_hint.setText("")
