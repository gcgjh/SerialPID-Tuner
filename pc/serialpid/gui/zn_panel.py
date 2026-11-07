"""Ziegler-Nichols 自整定辅助面板（纯 PC 侧计算，不改动 MCU）。

面板只做两件事：
1. 录入临界增益/周期（闭环法）或对象 K/L/T（开环反应曲线法），按经典
   Z-N 公式算出 P / PI / PID 三组 Kp/Ki/Kd；
2. 把用户选中的一组参数经 ``applied`` 信号抛给主窗口，由链路层通过
   既有 ``PID_SET`` 帧下发。面板本身不打开串口、不碰固件。
"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QComboBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Signal

from ..zn_tuning import ZNParams, zn_closed_loop, zn_open_loop


def _make_spin(lo: float, hi: float, default: float, step: float, unit: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(lo, hi)
    spin.setDecimals(4)
    spin.setSingleStep(step)
    spin.setValue(default)
    spin.setSuffix(f" {unit}" if unit else "")
    return spin


class ZnPanel(QWidget):
    """Z-N 整定面板。``applied(Kp, Ki, Kd)`` 信号在点击下发时触发。"""

    applied = Signal(float, float, float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._last_closed: Dict[str, ZNParams] = {}
        self._last_open: Dict[str, ZNParams] = {}

        tabs = QTabWidget()
        tabs.addTab(self._build_closed_tab(), "闭环 · 临界振荡法")
        tabs.addTab(self._build_open_tab(), "开环 · 反应曲线法")

        root = QVBoxLayout(self)
        root.addWidget(tabs)

    # -- 闭环临界振荡法 ----------------------------------------------------- #

    def _build_closed_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout()
        self._ku = _make_spin(0.001, 100000.0, 2.0, 0.1, "Ku")
        self._tu = _make_spin(0.001, 100.0, 2.0, 0.1, "s")
        form.addRow("临界增益 Ku", self._ku)
        form.addRow("临界周期 Tu", self._tu)

        btn = QPushButton("计算 Z-N 参数")
        btn.clicked.connect(self._on_closed_calc)

        self._closed_table = self._make_table()
        self._closed_mode = QComboBox()
        self._closed_mode.addItems(["P", "PI", "PID"])
        send = QPushButton("下发所选参数")
        send.clicked.connect(lambda: self._send(self._last_closed, self._closed_mode.currentText()))

        hint = QLabel(
            "先用纯比例作用增大 Kp 至出现等幅振荡，记录此时的 Ku 与振荡周期 Tu；\n"
            "公式：P=0.5Ku，PI=0.45Ku/Tu÷1.2，PID=0.6Ku、Ti=Tu/2、Td=Tu/8。"
        )
        hint.setWordWrap(True)

        lay = QVBoxLayout(page)
        lay.addLayout(form)
        lay.addWidget(btn)
        lay.addWidget(self._closed_table)
        row = QHBoxLayout()
        row.addWidget(QLabel("选择控制律"))
        row.addWidget(self._closed_mode)
        row.addWidget(send)
        lay.addLayout(row)
        lay.addWidget(hint)
        return page

    def _on_closed_calc(self) -> None:
        try:
            self._last_closed = zn_closed_loop(self._ku.value(), self._tu.value())
        except ValueError as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        self._fill_table(self._closed_table, self._last_closed)

    # -- 开环反应曲线法 ----------------------------------------------------- #

    def _build_open_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout()
        self._k = _make_spin(0.001, 100000.0, 1.0, 0.1, "K")
        self._l = _make_spin(0.001, 100.0, 0.5, 0.05, "s")
        self._t = _make_spin(0.001, 1000.0, 2.0, 0.1, "s")
        form.addRow("对象增益 K", self._k)
        form.addRow("纯滞后 L", self._l)
        form.addRow("时间常数 T", self._t)

        btn = QPushButton("计算 Z-N 参数")
        btn.clicked.connect(self._on_open_calc)

        self._open_table = self._make_table()
        self._open_mode = QComboBox()
        self._open_mode.addItems(["P", "PI", "PID"])
        send = QPushButton("下发所选参数")
        send.clicked.connect(lambda: self._send(self._last_open, self._open_mode.currentText()))

        hint = QLabel(
            "对开环对象施加阶跃，由响应曲线切线法辨识增益 K、滞后 L、时间常数 T；\n"
            "公式：PID Kp=1.2T/(KL)、Ti=2L、Td=0.5L。Ki=Kp/Ti，Kd=Kp·Td。"
        )
        hint.setWordWrap(True)

        lay = QVBoxLayout(page)
        lay.addLayout(form)
        lay.addWidget(btn)
        lay.addWidget(self._open_table)
        row = QHBoxLayout()
        row.addWidget(QLabel("选择控制律"))
        row.addWidget(self._open_mode)
        row.addWidget(send)
        lay.addLayout(row)
        lay.addWidget(hint)
        return page

    def _on_open_calc(self) -> None:
        try:
            self._last_open = zn_open_loop(self._k.value(), self._l.value(), self._t.value())
        except ValueError as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        self._fill_table(self._open_table, self._last_open)

    # -- 共用表格与下发 ----------------------------------------------------- #

    @staticmethod
    def _make_table() -> QTableWidget:
        table = QTableWidget(3, 6)
        table.setHorizontalHeaderLabels(["控制律", "Kp", "Ki", "Kd", "Ti (s)", "Td (s)"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _fill_table(table: QTableWidget, result: Dict[str, ZNParams]) -> None:
        for row, mode in enumerate(("P", "PI", "PID")):
            p = result[mode]
            cells = [
                mode, f"{p.kp:.4g}", f"{p.ki:.4g}", f"{p.kd:.4g}",
                "—" if p.ti is None else f"{p.ti:.4g}",
                "—" if p.td is None else f"{p.td:.4g}",
            ]
            for col, text in enumerate(cells):
                table.setItem(row, col, QTableWidgetItem(text))

    def _send(self, result: Dict[str, ZNParams], mode: str) -> None:
        if mode not in result:
            QMessageBox.information(self, "提示", "请先计算参数，再选择控制律下发。")
            return
        p = result[mode]
        self.applied.emit(p.kp, p.ki, p.kd)


# 兼容别名：旧名 ZnTuningPanel 仍可导入
ZnTuningPanel = ZnPanel
