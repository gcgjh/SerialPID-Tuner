# -*- coding: utf-8 -*-
"""故障注入面板（card33）：把仿真链路的劣化做成三个滑杆。

为什么只做"下行（设备→上位机）"：用户能看见的异常（曲线断点、坏帧红字、
突然掉线）都来自下行。上行命令也一起丢会让人以为"参数没生效"，分不清是
链路坏还是固件不干活，所以这里只坏下行。

三个滑杆**默认全零**（硬要求）：``LinkConfig.enabled`` 为假、泵线程不启动、
字节流原样直通，正常演示路径零开销。"恢复零注入"一键回到直通。
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..sim.degradation import LinkConfig
from .theme import COLORS

_HINT_STYLE = f"color: {COLORS['muted']}; font-size: 11px;"

#: 三个滑杆的取值范围（百分比 / 秒）
DROP_MAX_PCT = 50
BAD_MAX_PCT = 30
CUT_MAX_S = 60


class FaultPanel(QWidget):
    """可折叠的故障注入区：丢帧率 / 坏帧率 / N 秒后强制断线。"""

    #: 参数变化（``LinkConfig``）。主窗口据此调 ``DegradedTransport.apply``。
    configChanged = Signal(object)
    #: 折叠状态变化（主窗口据此把左栏滚到面板处）
    expandedChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._emitting = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        self._toggle = QPushButton("▸ 故障注入（仿真链路，点击展开）")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setStyleSheet(
            "QPushButton {"
            f"background: {COLORS['panel']};"
            f"border: 1px solid {COLORS['border']};"
            "border-radius: 4px; padding: 4px 6px; text-align: left;"
            "}"
        )
        self._toggle.toggled.connect(self._on_toggle)
        root.addWidget(self._toggle)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(4, 4, 4, 4)
        body.setSpacing(4)

        self._drop, self._drop_value, self._drop_row = self._make_row(
            "丢帧率（设备→上位机）", 0, DROP_MAX_PCT, "%"
        )
        self._bad, self._bad_value, self._bad_row = self._make_row(
            "坏帧率（CRC 损坏）", 0, BAD_MAX_PCT, "%"
        )
        self._cut, self._cut_value, self._cut_row = self._make_row(
            "N 秒后强制断线（0 = 不断）", 0, CUT_MAX_S, " s"
        )
        for row in (self._drop_row, self._bad_row, self._cut_row):
            body.addLayout(row)

        note = QLabel(
            "三个滑杆默认全零 = 直通。拉开后立刻生效：丢帧会在曲线上留空档、"
            "坏帧会让状态栏出现坏帧计数与中文提示，断线到点后通信直接结束。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(_HINT_STYLE)
        body.addWidget(note)

        self._state = QLabel("")
        self._state.setWordWrap(True)
        self._state.setStyleSheet(_HINT_STYLE)
        body.addWidget(self._state)

        reset_row = QHBoxLayout()
        self._reset = QPushButton("恢复零注入")
        self._reset.clicked.connect(self.reset)
        reset_row.addWidget(self._reset)
        reset_row.addStretch(1)
        body.addLayout(reset_row)

        self._body.hide()
        root.addWidget(self._body)
        self._refresh_state()

    # ---- 构造辅助 ---------------------------------------------------------

    def _make_row(self, label: str, low: int, high: int, unit: str):
        name = QLabel(label)
        name.setMinimumWidth(150)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setSingleStep(1)
        slider.setPageStep(5)
        slider.setValue(low)
        slider.setToolTip(f"{label}：{low}–{high}{unit}，默认 {low}{unit}")
        slider.valueChanged.connect(self._on_change)
        value = QLabel(f"{low}{unit}")
        value.setMinimumWidth(48)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(name)
        row.addWidget(slider, stretch=1)
        row.addWidget(value)
        return slider, value, row

    # ---- 交互 -------------------------------------------------------------

    def _on_toggle(self, checked: bool) -> None:
        self._body.setVisible(bool(checked))
        self._toggle.setText(
            ("▾ 故障注入（仿真链路）" if checked else "▸ 故障注入（仿真链路，点击展开）")
        )
        self.expandedChanged.emit(bool(checked))

    def _on_change(self, _value: int = 0) -> None:
        self._refresh_state()
        if self._emitting:
            return
        self.configChanged.emit(self.config())

    def _refresh_state(self) -> None:
        self._drop_value.setText(f"{self._drop.value()}%")
        self._bad_value.setText(f"{self._bad.value()}%")
        self._cut_value.setText(f"{self._cut.value()} s")
        config = self.config()
        if not config.enabled:
            self._state.setText("当前：全零（链路直通，零开销）")
        else:
            self._state.setText(f"当前：{config.describe()}")

    def reset(self) -> None:
        """一键回到直通（三个滑杆归零）。"""
        self._emitting = True
        try:
            for slider in (self._drop, self._bad, self._cut):
                slider.setValue(0)
        finally:
            self._emitting = False
        self._refresh_state()
        self.configChanged.emit(self.config())

    # ---- 对外接口 ---------------------------------------------------------

    def config(self) -> LinkConfig:
        """当前三滑杆折算成劣化参数（只有非零时才 ``enabled``）。"""
        drop = self._drop.value() / 100.0
        bad = self._bad.value() / 100.0
        cut = float(self._cut.value())
        return LinkConfig(
            enabled=bool(drop or bad or cut),
            drop_down=drop,
            bad_crc_down=bad,
            disconnect_after_s=cut,
        )

    def params(self) -> Dict[str, float]:
        """界面值的纯 Python 快照（百分比 / 秒），便于测试与日志。"""
        return {
            "drop_pct": float(self._drop.value()),
            "bad_pct": float(self._bad.value()),
            "cut_s": float(self._cut.value()),
        }

    def set_params(
        self,
        drop_pct: float = 0.0,
        bad_pct: float = 0.0,
        cut_s: float = 0.0,
        *,
        emit: bool = True,
    ) -> None:
        self._emitting = not emit
        try:
            self._drop.setValue(int(round(drop_pct)))
            self._bad.setValue(int(round(bad_pct)))
            self._cut.setValue(int(round(cut_s)))
        finally:
            self._emitting = False
        self._refresh_state()
        if emit:
            self.configChanged.emit(self.config())

    def is_expanded(self) -> bool:
        # 用按钮勾选状态而不是 isVisible()：父窗口还没 show 时子控件不可见，
        # 那样测出来"没展开"会与用户实际状态不一致。
        return bool(self._toggle.isChecked())

    def set_expanded(self, expanded: bool) -> None:
        if self._toggle.isChecked() != bool(expanded):
            self._toggle.setChecked(bool(expanded))
        else:
            self._on_toggle(bool(expanded))

    def state_text(self) -> str:
        return self._state.text()
