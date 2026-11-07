"""阶跃测试面板：MCU 命令下发 + PC 侧响应指标离线分析。

两条路径互不耦合：

* **命令路径**（原有）：幅值 / 保持时长 -> ``STEP_START`` / ``STEP_STOP`` 帧，
  由固件执行阶跃；``STEP_EVENT`` 回报阶段状态。
* **分析路径**（PC 侧，不改 MCU）：把通信层喂入的遥测样本缓存起来，
  点击“分析最近曲线”后用 :func:`serialpid.step_test.analyze_step` 离线计算
  超调量、峰值、上升时间、调节时间、稳态误差。

界面呈现（卡18）：
    * 指标区有两态——未分析只显示一行提示，分析后（或样本不足报错后）才铺
      指标行；两态放在 ``QStackedWidget`` 里，高度取两页最大值，切换不抖动。
    * 指标按重要性排序：超调量、调节时间在前，上升/峰值时间用次级色。
    * 值列右对齐、等宽数字、撑满剩余宽度（不再固定 140px 让右侧空一大片），
      数值贴右缘且不随文本长度跳动。
    * 命令按钮等宽（同一布局各占一份拉伸），“停止”用醒目偏红色。
    * 阶段状态与命令按钮同组，保持时长旁给出秒换算。
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..messages import UserMessage
from ..model import TelemetrySample
from ..protocol import PacketType, pack_step_start
from ..step_test import StepMetrics, analyze_step
from .theme import COLORS

PHASE_TEXT = {0: "空闲", 1: "阶跃运行中", 2: "阶跃完成"}
#: 指标行顺序 = 重要性顺序；第二项为 True 表示次级信息（用次级色显示）
_METRIC_ROWS = (
    ("超调量", False),
    ("调节时间", False),
    ("稳态误差", False),
    ("峰值", False),
    ("上升时间", True),
    ("峰值时间", True),
    ("状态", False),
)
#: 值列最小宽度：右对齐 + 等宽数字 + 撑满剩余宽度，数值贴右缘且不跳动
_VALUE_MIN_WIDTH = 110
#: 命令按钮底色/边框：深色主题下 palette 的 Button 与面板底色几乎同色，
#: 不加边框时两个按钮看起来像一行文字
_BTN_QSS = (
    "QPushButton {"
    f"background: {COLORS['panel']};"
    f"border: 1px solid {COLORS['border']};"
    "border-radius: 4px; padding: 3px 6px;"
    "}"
)
_BAND_VALUES = {"1%": 0.01, "2%": 0.02, "5%": 0.05}
_HINT_IDLE = "尚未分析，点击上方按钮计算指标"
#: 阶跃分析样本上限；与 TelemetryBuffer / 绘图队列一致，防止长跑无界增长。
MAX_SAMPLES = 20000


class StepPanel(QWidget):
    def __init__(self, worker, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._samples: Deque[TelemetrySample] = deque(maxlen=MAX_SAMPLES)
        self._metrics: Optional[StepMetrics] = None

        self._amplitude = QDoubleSpinBox()
        self._amplitude.setRange(-1000.0, 1000.0)
        self._amplitude.setDecimals(2)
        self._amplitude.setValue(5.0)

        self._hold_ms = QSpinBox()
        self._hold_ms.setRange(0, 600_000)
        self._hold_ms.setValue(5000)
        # 毫秒输入旁的秒换算：调参时按秒思考更自然，下发仍按毫秒
        self._hold_hint = QLabel(self._hold_text(self._hold_ms.value()))
        self._hold_hint.setStyleSheet(f"color: {COLORS['muted']};")

        self._start = QPushButton("开始阶跃")
        self._start.setStyleSheet(_BTN_QSS)
        self._stop = QPushButton("停止")
        self._stop.setToolTip("立即下发 STEP_STOP，让固件停止当前阶跃")
        self._stop.setStyleSheet(
            _BTN_QSS
            + "QPushButton {"
            + f"color: {COLORS['err']}; border-color: {COLORS['err']};"
            + "}"
        )
        self._phase_label = QLabel("空闲")
        self._phase_label.setToolTip("固件回报的阶跃阶段（来自 STEP_EVENT）")
        self._phase_label.setStyleSheet(f"color: {COLORS['muted']}; font-size: 11px;")

        # 两个命令按钮各占一份拉伸 = 等宽；阶段状态紧贴同一行末尾
        cmd_row = QHBoxLayout()
        cmd_row.addWidget(self._start, 1)
        cmd_row.addWidget(self._stop, 1)
        cmd_row.addWidget(self._phase_label)

        hold_row = QHBoxLayout()
        hold_row.addWidget(self._hold_ms)
        hold_row.addWidget(self._hold_hint, 1)

        cmd_box = QGroupBox("阶跃命令（下发固件）")
        cmd_form = QFormLayout(cmd_box)
        cmd_form.addRow("幅值", self._amplitude)
        cmd_form.addRow("保持时长 (ms)", hold_row)
        cmd_form.addRow(cmd_row)

        # ---- PC 侧指标分析 ----
        self._band = QComboBox()
        self._band.addItems(list(_BAND_VALUES.keys()))
        self._band.setCurrentText("2%")
        self._analyze = QPushButton("分析最近曲线")

        self._metrics_labels: dict[str, QLabel] = {}
        metrics_form = QFormLayout()
        metrics_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        mono = QFont()
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setFamilies(["Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"])
        mono.setPointSize(9)
        for key, secondary in _METRIC_ROWS:
            name = QLabel(key)
            if secondary:
                name.setStyleSheet(f"color: {COLORS['muted']};")
            lab = QLabel("—")
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lab.setMinimumWidth(_VALUE_MIN_WIDTH)
            lab.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            lab.setFont(mono)
            if secondary:
                lab.setStyleSheet(f"color: {COLORS['muted']};")
            self._metrics_labels[key] = lab
            metrics_form.addRow(name, lab)

        self._metrics_body = QWidget()
        body_layout = QVBoxLayout(self._metrics_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addLayout(metrics_form)

        # 空态 / 错误态：只一行文字，不铺七行破折号
        self._hint = QLabel(_HINT_IDLE)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {COLORS['muted']};")

        # QStackedWidget 的高度取所有页的最大 sizeHint：
        # 切换两态时分组框高度不变，左面板不会跟着抖
        self._metrics_stack = QStackedWidget()
        self._metrics_stack.addWidget(self._hint)
        self._metrics_stack.addWidget(self._metrics_body)

        ana_box = QGroupBox("响应指标（离线，时间单位毫秒）")
        ana_box.setToolTip("PC 侧离线分析，不改 MCU；时间类指标统一以毫秒显示")
        ana_layout = QVBoxLayout(ana_box)
        ana_row = QHBoxLayout()
        ana_row.addWidget(QLabel("稳态带"))
        ana_row.addWidget(self._band)
        ana_row.addWidget(self._analyze)
        ana_layout.addLayout(ana_row)
        ana_layout.addWidget(self._metrics_stack, 1)

        outer = QVBoxLayout(self)
        outer.addWidget(cmd_box)
        outer.addWidget(ana_box)

        self._start.clicked.connect(self._start_step)
        self._stop.clicked.connect(self._stop_step)
        self._analyze.clicked.connect(self._analyze_curve)
        self._hold_ms.valueChanged.connect(self._on_hold_changed)

    # ---- 小工具 ----

    @staticmethod
    def _hold_text(ms: int) -> str:
        return f"≈ {ms / 1000:.1f} 秒"

    def _on_hold_changed(self, ms: int) -> None:
        self._hold_hint.setText(self._hold_text(ms))

    # ---- 命令路径 ----

    def _start_step(self) -> None:
        self.clear_samples()
        self._worker.send(
            PacketType.STEP_START,
            pack_step_start(self._amplitude.value(), self._hold_ms.value()),
        )

    def _stop_step(self) -> None:
        self._worker.send(PacketType.STEP_STOP)

    def show_phase(self, phase: int) -> None:
        text = PHASE_TEXT.get(phase, f"未知({phase})")
        self._phase_label.setText(text)
        color = {
            0: COLORS["muted"],
            1: COLORS["warn"],
            2: COLORS["ok"],
        }.get(phase, COLORS["muted"])
        self._phase_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    # ---- 分析路径 ----

    def append_sample(self, sample: TelemetrySample) -> None:
        """由主窗口在收到 TELEMETRY 时调用，缓存待分析样本。"""
        self._samples.append(sample)

    def clear_samples(self) -> None:
        self._samples.clear()
        self._metrics = None
        for lab in self._metrics_labels.values():
            lab.setText("—")
        self._show_hint(_HINT_IDLE)

    def _show_hint(self, text: str, *, error: bool = False) -> None:
        """切到一行提示态；``error`` 用错误色（分析失败 / 样本不足）。"""
        self._hint.setText(text)
        self._hint.setStyleSheet(
            f"color: {COLORS['err']};" if error else f"color: {COLORS['muted']};"
        )
        self._metrics_stack.setCurrentWidget(self._hint)

    def _analyze_curve(self) -> None:
        if len(self._samples) < 4:
            self._metrics = None
            # 卡30：错误提示统一三段式。指标区只有一行高，所以正文保持短句，
            # 完整三段式放在悬停提示里——既不撑破布局，也不丢掉"下一步"。
            self._show_hint("样本不足（<4 点）｜先跑一段含阶跃的曲线再分析", error=True)
            self._hint.setToolTip(
                UserMessage(
                    "样本不足（<4 点），无法计算指标",
                    "当前缓冲里的遥测点少于 4 个，阶跃前后都采样不足",
                    "连接设备或内置模拟器，跑一段含明显阶跃的曲线，再点「分析最近曲线」。",
                ).as_block()
            )
            return
        times = [s.t_ms for s in self._samples]
        sp = [s.sp for s in self._samples]
        pv = [s.pv for s in self._samples]
        band = _BAND_VALUES[self._band.currentText()]
        try:
            self._metrics = analyze_step(times, sp, pv, band=band)
        except ValueError as exc:
            self._metrics = None
            self._show_hint("分析失败｜检查曲线里有没有明确的阶跃", error=True)
            self._hint.setToolTip(
                UserMessage(
                    "分析失败，算不出响应指标",
                    f"曲线里检测不到可用的阶跃：{exc}",
                    "确认 SP 在记录区间内至少跳变一次；换个稳态带（1% / 2% / 5%）再试。",
                    detail=f"{type(exc).__name__}: {exc}",
                ).as_block()
            )
            return
        self._render_metrics(self._metrics)
        self._metrics_stack.setCurrentWidget(self._metrics_body)

    def _render_metrics(self, m: StepMetrics) -> None:
        self._set("超调量", f"{m.overshoot_pct:.2f} %")
        self._set(
            "调节时间",
            "—" if m.settling_time_ms is None else f"{m.settling_time_ms:.0f} ms",
        )
        self._set(
            "稳态误差",
            f"{m.steady_state_error:.3f}（{m.steady_state_error_pct:.2f}%）",
        )
        self._set("峰值", f"{m.peak_value:.3f}")
        self._set("上升时间", "—" if m.rise_time_ms is None else f"{m.rise_time_ms:.0f} ms")
        self._set("峰值时间", "—" if m.peak_t_ms is None else f"{m.peak_t_ms:.0f} ms")
        self._set("状态", "已出指标" if m.settled else "未收敛（未进稳态带）")

    def _set(self, key: str, text: str) -> None:
        self._metrics_labels[key].setText(text)
