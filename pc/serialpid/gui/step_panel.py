"""阶跃测试面板：MCU 命令下发 + PC 侧响应指标离线分析。

两条路径互不耦合：

* **命令路径**（原有）：幅值 / 保持时长 -> ``STEP_START`` / ``STEP_STOP`` 帧，
  由固件执行阶跃；``STEP_EVENT`` 回报阶段状态。
* **分析路径**（PC 侧，不改 MCU）：把通信层喂入的遥测样本缓存起来，
  点击“分析最近曲线”后用 :func:`serialpid.step_test.analyze_step` 离线计算
  超调量、峰值、上升时间、调节时间、稳态误差。
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..model import TelemetrySample
from ..protocol import PacketType, pack_step_start
from ..step_test import StepMetrics, analyze_step

PHASE_TEXT = {0: "空闲", 1: "阶跃运行中", 2: "阶跃完成"}
_BAND_VALUES = {"1%": 0.01, "2%": 0.02, "5%": 0.05}
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

        self._start = QPushButton("开始阶跃")
        self._stop = QPushButton("停止")
        self._phase_label = QLabel("空闲")
        self._phase_label.setStyleSheet("color: #888;")

        cmd_row = QHBoxLayout()
        cmd_row.addWidget(self._start)
        cmd_row.addWidget(self._stop)

        cmd_box = QGroupBox("阶跃命令（下发固件）")
        cmd_form = QFormLayout(cmd_box)
        cmd_form.addRow("幅值", self._amplitude)
        cmd_form.addRow("保持时长 (ms)", self._hold_ms)
        cmd_form.addRow(cmd_row)
        cmd_form.addRow(self._phase_label)

        # ---- PC 侧指标分析 ----
        self._band = QComboBox()
        self._band.addItems(list(_BAND_VALUES.keys()))
        self._band.setCurrentText("2%")
        self._analyze = QPushButton("分析最近曲线")
        self._metrics_labels: dict[str, QLabel] = {}
        metric_grid = QFormLayout()
        for key in ("状态", "超调量", "峰值", "峰值时间", "上升时间", "调节时间", "稳态误差"):
            lab = QLabel("—")
            self._metrics_labels[key] = lab
            metric_grid.addRow(key, lab)

        ana_box = QGroupBox("响应指标（PC 侧离线分析）")
        ana_layout = QVBoxLayout(ana_box)
        ana_row = QHBoxLayout()
        ana_row.addWidget(QLabel("稳态带"))
        ana_row.addWidget(self._band)
        ana_row.addWidget(self._analyze)
        ana_layout.addLayout(ana_row)
        ana_layout.addLayout(metric_grid)

        outer = QVBoxLayout(self)
        outer.addWidget(cmd_box)
        outer.addWidget(ana_box)

        self._start.clicked.connect(self._start_step)
        self._stop.clicked.connect(self._stop_step)
        self._analyze.clicked.connect(self._analyze_curve)

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
        color = {0: "#888", 1: "#f39c12", 2: "#2ecc71"}.get(phase, "#888")
        self._phase_label.setStyleSheet(f"color: {color};")

    # ---- 分析路径 ----

    def append_sample(self, sample: TelemetrySample) -> None:
        """由主窗口在收到 TELEMETRY 时调用，缓存待分析样本。"""
        self._samples.append(sample)

    def clear_samples(self) -> None:
        self._samples.clear()
        self._metrics = None
        for lab in self._metrics_labels.values():
            lab.setText("—")

    def _analyze_curve(self) -> None:
        if len(self._samples) < 4:
            self._set("状态", "样本不足（<4 点）")
            return
        times = [s.t_ms for s in self._samples]
        sp = [s.sp for s in self._samples]
        pv = [s.pv for s in self._samples]
        band = _BAND_VALUES[self._band.currentText()]
        try:
            self._metrics = analyze_step(times, sp, pv, band=band)
        except ValueError as exc:
            self._set("状态", f"分析失败：{exc}")
            return
        self._render_metrics(self._metrics)

    def _render_metrics(self, m: StepMetrics) -> None:
        self._set("状态", "已出指标" if m.settled else "未收敛")
        self._set("超调量", f"{m.overshoot_pct:.2f} %")
        self._set("峰值", f"{m.peak_value:.3f}")
        self._set("峰值时间", "—" if m.peak_t_ms is None else f"{m.peak_t_ms:.0f} ms")
        self._set("上升时间", "—" if m.rise_time_ms is None else f"{m.rise_time_ms:.0f} ms")
        self._set(
            "调节时间",
            "—" if m.settling_time_ms is None else f"{m.settling_time_ms:.0f} ms",
        )
        self._set(
            "稳态误差",
            f"{m.steady_state_error:.3f}（{m.steady_state_error_pct:.2f}%）",
        )

    def _set(self, key: str, text: str) -> None:
        self._metrics_labels[key].setText(text)
