"""Ziegler-Nichols 自整定辅助面板（纯 PC 侧计算，不改动 MCU）。

面板只做两件事：
1. 录入临界增益/周期（闭环法）或对象 K/L/T（开环反应曲线法），按经典
   Z-N 公式算出 P / PI / PID 三组 Kp/Ki/Kd；
2. 把用户选中的一组参数经 ``applied`` 信号抛给主窗口，由链路层通过
   既有 ``PID_SET`` 帧下发。面板本身不打开串口、不碰固件。

公式文案唯一来源是本文件的 ``CLOSED_FORMULAS`` / ``OPEN_FORMULAS``：
每个控制律一行（长表达式另起一行），参数名与数值分开写，不使用连除式；
系数与 ``zn_tuning.py`` 逐项对应，并由 ``tests/test_zn_panel.py`` 交叉核对。
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..zn_tuning import ZNParams, zn_closed_loop, zn_open_loop
from .theme import COLORS

#: 闭环临界振荡法公式：(控制律, 公式行…)。系数与 zn_tuning.zn_closed_loop 一致。
#: 一行只写一个量，比例系数与积分时间分开放，避免"0.45Ku/Tu÷1.2"这种连除式。
CLOSED_FORMULAS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("P", ("Kp = 0.50 × Ku",)),
    ("PI", ("Kp = 0.45 × Ku", "Ti = Tu / 1.2")),
    ("PID", ("Kp = 0.60 × Ku", "Ti = Tu / 2", "Td = Tu / 8")),
)

#: 开环反应曲线法公式：(控制律, 公式行…)。系数与 zn_tuning.zn_open_loop 一致。
OPEN_FORMULAS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("P", ("Kp = T / (K × L)",)),
    ("PI", ("Kp = 0.90 × T / (K × L)", "Ti = 3.33 × L")),
    ("PID", ("Kp = 1.20 × T / (K × L)", "Ti = 2 × L", "Td = 0.5 × L")),
)

#: Ki / Kd 换算关系（三处口径之一：界面 · zn_tuning 注释 · 用户手册）
GAIN_CONVERSION = "Ki = Kp / Ti，Kd = Kp × Td（Ti、Td 单位 s）"

_NOT_READY_TEXT = "尚未计算，点击上方「计算 Z-N 参数」"
_LAWS = ("P", "PI", "PID")


def _make_spin(lo: float, hi: float, default: float, step: float, unit: str = "") -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(lo, hi)
    spin.setDecimals(4)
    spin.setSingleStep(step)
    spin.setValue(default)
    spin.setSuffix(f" {unit}" if unit else "")
    return spin


class _WrappedLabel(QLabel):
    """可换行文本标签：首选高度按当前宽度算真实行数（空间够就不裁切），
    最小高度只报一行 —— 空间紧张时可以压缩，不会把左面板最小高度顶上去。
    """

    def sizeHint(self):  # noqa: N802 (Qt 命名)
        hint = super().sizeHint()
        width = self.width() if self.width() > 0 else hint.width()
        needed = self.heightForWidth(width) if width > 0 else -1
        height = needed if needed > 0 else hint.height()
        return QSize(hint.width(), max(height, self.fontMetrics().height()))

    def minimumSizeHint(self):  # noqa: N802 (Qt 命名)
        hint = super().minimumSizeHint()
        return QSize(hint.width(), self.fontMetrics().height())


def formula_lines(
    formulas: Sequence[Tuple[str, Tuple[str, ...]]],
    intro: str,
    conversion: str,
) -> list[str]:
    """把公式常数展开成逐行文案（构建标签与测试共用同一份拼法）。

    首行用 ``P`` / ``PI`` / ``PID`` 对齐到同一列，续行缩进到公式起始处，
    这样每行只放一个量，窄面板里不会被拆成参差的长短行。
    """
    lines = [intro]
    for law, body in formulas:
        for index, text in enumerate(body):
            prefix = law.ljust(4) if index == 0 else " " * 4
            lines.append(prefix + text)
    lines.append(conversion)
    return lines


def _build_formula_label(
    formulas: Sequence[Tuple[str, Tuple[str, ...]]],
    intro: str,
    conversion: str,
) -> QLabel:
    """公式区：一段可换行纯文本，每个控制律一块，每行只写一个量。

    刻意用单段文本而不是表格：340 像素面板里 4 列表格的最小宽度会顶破面板
    （见 card19 风险段）；单段文本的最小高度与旧版相当，不会把左面板的最
    小高度再抬高，空间足够时又按真实行数展开、不裁切。
    """
    label = _WrappedLabel("\n".join(formula_lines(formulas, intro, conversion)))
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setObjectName("zn_formula")
    label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    label.setStyleSheet(f"color: {COLORS['muted']};")
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    label.setSizePolicy(policy)
    return label


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
        self._closed_stack = self._make_result_area(self._closed_table)

        self._closed_mode = QComboBox()
        self._closed_mode.addItems(list(_LAWS))
        self._closed_mode.currentTextChanged.connect(
            lambda mode: self._highlight(self._closed_table, self._last_closed, mode)
        )
        send = QPushButton("下发所选参数")
        send.clicked.connect(lambda: self._send(self._last_closed, self._closed_mode.currentText()))

        formulas = _build_formula_label(
            CLOSED_FORMULAS,
            "先用纯比例作用增大 Kp 至出现等幅振荡，记录此时的 Ku 与振荡周期 Tu。",
            GAIN_CONVERSION,
        )

        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.addLayout(form)
        lay.addWidget(btn)
        lay.addWidget(self._closed_stack)
        row = QHBoxLayout()
        row.addWidget(QLabel("选择控制律"))
        row.addWidget(self._closed_mode)
        row.addWidget(send)
        lay.addLayout(row)
        lay.addWidget(formulas)
        lay.addStretch(1)
        return page

    def _on_closed_calc(self) -> None:
        try:
            self._last_closed = zn_closed_loop(self._ku.value(), self._tu.value())
        except ValueError as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        self._fill_table(self._closed_table, self._last_closed)
        self._closed_stack.setCurrentIndex(1)
        self._highlight(self._closed_table, self._last_closed, self._closed_mode.currentText())

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
        self._open_stack = self._make_result_area(self._open_table)

        self._open_mode = QComboBox()
        self._open_mode.addItems(list(_LAWS))
        self._open_mode.currentTextChanged.connect(
            lambda mode: self._highlight(self._open_table, self._last_open, mode)
        )
        send = QPushButton("下发所选参数")
        send.clicked.connect(lambda: self._send(self._last_open, self._open_mode.currentText()))

        formulas = _build_formula_label(
            OPEN_FORMULAS,
            "对开环对象施加阶跃，由反应曲线切线法辨识 K、L、T。",
            GAIN_CONVERSION,
        )

        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 6, 4, 6)
        lay.addLayout(form)
        lay.addWidget(btn)
        lay.addWidget(self._open_stack)
        row = QHBoxLayout()
        row.addWidget(QLabel("选择控制律"))
        row.addWidget(self._open_mode)
        row.addWidget(send)
        lay.addLayout(row)
        lay.addWidget(formulas)
        lay.addStretch(1)
        return page

    def _on_open_calc(self) -> None:
        try:
            self._last_open = zn_open_loop(self._k.value(), self._l.value(), self._t.value())
        except ValueError as exc:
            QMessageBox.warning(self, "输入无效", str(exc))
            return
        self._fill_table(self._open_table, self._last_open)
        self._open_stack.setCurrentIndex(1)
        self._highlight(self._open_table, self._last_open, self._open_mode.currentText())

    # -- 结果区：未计算提示 ↔ 结果表格 -------------------------------------- #

    @staticmethod
    def _make_result_area(table: QTableWidget) -> QStackedWidget:
        """结果区两页：占位提示 / 结果表格。

        用 QStackedWidget 而不是直接隐藏控件：两页高度取最大值，切换时
        结果区不会跳高跳矮。
        """
        stack = QStackedWidget()
        placeholder = QLabel(_NOT_READY_TEXT)
        placeholder.setWordWrap(True)
        placeholder.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        placeholder.setStyleSheet(f"color: {COLORS['muted']};")
        placeholder.setObjectName("zn_placeholder")
        stack.addWidget(placeholder)
        stack.addWidget(table)
        stack.setCurrentIndex(0)
        return stack

    @staticmethod
    def _make_table() -> QTableWidget:
        table = QTableWidget(3, 6)
        table.setHorizontalHeaderLabels(["控制律", "Kp", "Ki", "Kd", "Ti (s)", "Td (s)"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _fill_table(table: QTableWidget, result: Dict[str, ZNParams]) -> None:
        for row, mode in enumerate(_LAWS):
            p = result[mode]
            cells = [
                mode, f"{p.kp:.4g}", f"{p.ki:.4g}", f"{p.kd:.4g}",
                "—" if p.ti is None else f"{p.ti:.4g}",
                "—" if p.td is None else f"{p.td:.4g}",
            ]
            for col, text in enumerate(cells):
                table.setItem(row, col, QTableWidgetItem(text))

    @staticmethod
    def _highlight(table: QTableWidget, result: Dict[str, ZNParams], mode: str) -> None:
        """高亮下拉框当前选中的控制律行（加粗 + 强调色）。"""
        if not result:
            return
        bold = QFont()
        bold.setBold(True)
        for row, law in enumerate(_LAWS):
            active = law == mode and law in result
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item is None:
                    continue
                font = QFont(item.font())
                font.setBold(active)
                item.setFont(font)
                item.setForeground(QColor(COLORS["accent"] if active else COLORS["text"]))

    def _send(self, result: Dict[str, ZNParams], mode: str) -> None:
        if mode not in result:
            QMessageBox.information(self, "提示", "请先计算参数，再选择控制律下发。")
            return
        p = result[mode]
        self.applied.emit(p.kp, p.ki, p.kd)


# 兼容别名：旧名 ZnTuningPanel 仍可导入
ZnTuningPanel = ZnPanel
