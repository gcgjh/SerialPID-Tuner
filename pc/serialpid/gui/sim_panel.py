"""内置模拟器对象参数面板（card25）。

面板只做三件事：摆参数、填预设、把改后的参数打成快照发出去。

* **不持有引擎**：引擎在工作线程里，面板在主线程改值。面板只发 ``paramsChanged(dict)``，
  由主窗口转交引擎；引擎侧按快照加锁读，避免跨线程裸读控件。
* **默认折叠**：左栏已经靠滚动条撑着，展开后也不能把日志挤没——面板只提供
  ``set_expanded(bool)``，高度由自身内容决定，日志在滚动区外不受影响。
* **每项一句人话**：标签只写名字，解释放在行下方的次级色小字里，换主题也不丢。

参数键（与 sim/plant.py 的对象参数一一对应）::

    gain      对象增益：执行量（0~100 %）放大成过程量变化的倍数
    tau1      第一级时间常数（s）
    tau2      第二级时间常数（s）
    delay     纯延迟（控制周期拍数）
    noise     测量噪声标准差（过程量单位）
    quant     测量量化步长（过程量单位）
    period_ms 控制周期（ms），即引擎节拍

预设覆盖卡25 要求的五类对象：快速小惯性、慢速大惯性热对象、大延迟、带噪声传感器、
接近临界振荡的难调对象。切换预设会一次性填好全部参数并只发一次 ``paramsChanged``。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .theme import COLORS

#: 六个核心对象参数 + 控制周期；键顺序即界面顺序
CORE_PARAMS: Tuple[str, ...] = ("gain", "tau1", "tau2", "delay", "noise", "quant")
PARAM_ORDER: Tuple[str, ...] = CORE_PARAMS + ("period_ms",)

#: 名称 / 单位 / 范围 / 步进 / 默认 / 一句人话解释
_PARAM_SPECS: Dict[str, Dict[str, object]] = {
    "gain": {
        "label": "对象增益",
        "unit": "×",
        "low": 0.05,
        "high": 50.0,
        "step": 0.05,
        "decimals": 2,
        "default": 1.0,
        "hint": "执行量每走 1% 能让过程量涨多少；调大更容易超调。",
    },
    "tau1": {
        "label": "时间常数 1",
        "unit": "s",
        "low": 0.05,
        "high": 120.0,
        "step": 0.1,
        "decimals": 2,
        "default": 1.5,
        "hint": "第一级惯性的快慢；越小反应越快、越难稳。",
    },
    "tau2": {
        "label": "时间常数 2",
        "unit": "s",
        "low": 0.05,
        "high": 300.0,
        "step": 0.1,
        "decimals": 2,
        "default": 4.0,
        "hint": "第二级惯性的快慢；两级串联才有超调。",
    },
    "delay": {
        "label": "纯延迟",
        "unit": "拍",
        "low": 0,
        "high": 40,
        "step": 1,
        "decimals": 0,
        "default": 0,
        "hint": "执行量晚几拍才起作用；延迟是振荡的主因。",
    },
    "noise": {
        "label": "测量噪声 σ",
        "unit": "",
        "low": 0.0,
        "high": 5.0,
        "step": 0.01,
        "decimals": 2,
        "default": 0.0,
        "hint": "只加在测量值上，曲线抖但对象内部不受污染。",
    },
    "quant": {
        "label": "量化步长",
        "unit": "",
        "low": 0.0,
        "high": 2.0,
        "step": 0.001,
        "decimals": 3,
        "default": 0.0,
        "hint": "传感器分辨率；设成 0.1 就能看到阶梯。",
    },
    "period_ms": {
        "label": "控制周期",
        "unit": "ms",
        "low": 10,
        "high": 200,
        "step": 5,
        "decimals": 0,
        "default": 50,
        "hint": "引擎节拍，也是“拍数”的换算基准（50 ms = 20 Hz）。",
    },
}

#: 场景预设：名字 -> 参数覆盖（只写与默认不同的键也可以，这里写全便于复现）
PRESETS: Dict[str, Dict[str, float]] = {
    "快速小惯性": {
        "gain": 1.0, "tau1": 0.3, "tau2": 0.8, "delay": 0,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "慢速大惯性热对象": {
        "gain": 1.0, "tau1": 12.0, "tau2": 45.0, "delay": 0,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "大延迟对象": {
        "gain": 1.0, "tau1": 2.0, "tau2": 5.0, "delay": 10,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
    "带噪声传感器": {
        "gain": 1.0, "tau1": 1.2, "tau2": 3.0, "delay": 0,
        "noise": 1.2, "quant": 0.05, "period_ms": 50,
    },
    "接近临界振荡": {
        "gain": 8.0, "tau1": 0.8, "tau2": 2.5, "delay": 6,
        "noise": 0.0, "quant": 0.0, "period_ms": 50,
    },
}

_HINT_STYLE = f"color: {COLORS['muted']}; font-size: 8pt;"


class SimPanel(QWidget):
    """仿真参数面板：可折叠，改值即发快照。"""

    paramsChanged = Signal(dict)
    presetChanged = Signal(str)
    #: 折叠状态变化。主窗口据此把左栏滚到面板处——展开后身体在滚动区下沿，
    #: 不滚一下用户会以为"点了没反应"。
    expandedChanged = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spins: Dict[str, object] = {}
        self._emitting = False

        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # ---- 折叠头：一行按钮，展开时箭头朝下 ----
        self._toggle = QPushButton("▸ 仿真对象参数（点击展开）")
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

        # ---- 折叠体 ----
        self._body = QWidget()
        body_box = QVBoxLayout(self._body)
        body_box.setContentsMargins(4, 4, 4, 4)
        body_box.setSpacing(6)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("场景预设"))
        self._preset = QComboBox()
        self._preset.addItem("自定义")
        self._preset.addItems(list(PRESETS))
        self._preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._preset.currentTextChanged.connect(self._on_preset_text)
        preset_row.addWidget(self._preset, stretch=1)
        body_box.addLayout(preset_row)

        box = QGroupBox("对象与传感器")
        grid = QGridLayout(box)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(1, 1)
        for row, key in enumerate(PARAM_ORDER):
            spec = _PARAM_SPECS[key]
            spin = self._make_spin(key, spec)
            self._spins[key] = spin
            head = QLabel(str(spec["label"]))
            if spec["unit"]:
                head.setText(f"{spec['label']} ({spec['unit']})")
            grid.addWidget(head, row * 2, 0)
            grid.addWidget(spin, row * 2, 1)
            hint = QLabel(str(spec["hint"]))
            hint.setWordWrap(True)
            hint.setStyleSheet(_HINT_STYLE)
            grid.addWidget(hint, row * 2 + 1, 0, 1, 2)
        body_box.addWidget(box)

        self._live_note = QLabel("改动立即生效，不用断开重连；切预设会重置对象状态。")
        self._live_note.setWordWrap(True)
        self._live_note.setStyleSheet(_HINT_STYLE)
        body_box.addWidget(self._live_note)

        self._body.hide()
        root.addWidget(self._body)

        self._sync_preset_label()

    # ---- 构造辅助 ----

    def _make_spin(self, key: str, spec: Dict[str, object]):
        if int(spec["decimals"]) == 0 and float(spec["step"]) >= 1.0:
            spin = QSpinBox()
            spin.setRange(int(spec["low"]), int(spec["high"]))
            spin.setSingleStep(int(spec["step"]))
        else:
            spin = QDoubleSpinBox()
            spin.setDecimals(int(spec["decimals"]))
            spin.setRange(float(spec["low"]), float(spec["high"]))
            spin.setSingleStep(float(spec["step"]))
        spin.setValue(type(spin.value())(spec["default"]))
        spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        spin.setToolTip(str(spec["hint"]))
        # 每改一个值就立刻发一次快照：卡25 要求“参数修改实时生效”
        spin.valueChanged.connect(lambda _v, k=key: self._emit_params())
        return spin

    # ---- 对外接口 ----

    def params(self) -> Dict[str, float]:
        """当前参数快照（纯 Python dict，可安全跨线程传）。"""
        return {key: float(spin.value()) for key, spin in self._spins.items()}

    def set_params(self, values: Dict[str, float], *, emit: bool = True) -> None:
        """按字典填参数；未知键忽略，越界值夹到合法区间。"""
        self._emitting = not emit
        try:
            for key, value in values.items():
                spin = self._spins.get(key)
                if spin is None:
                    continue
                spin.setValue(type(spin.value())(float(value)))
        finally:
            self._emitting = not emit
        self._sync_preset_label(values)
        if emit:
            self.paramsChanged.emit(self.params())

    def apply_preset(self, name: str) -> bool:
        """应用场景预设；``name`` 不存在返回 False。"""
        preset = PRESETS.get(name)
        if preset is None:
            return False
        self._preset.blockSignals(True)
        self._preset.setCurrentText(name)
        self._preset.blockSignals(False)
        # 预设是一次性全量替换，合成一次快照发出，避免逐个 spin 触发连锁重算
        self.set_params(preset, emit=True)
        self.presetChanged.emit(name)
        return True

    def preset_names(self) -> List[str]:
        return list(PRESETS)

    def is_expanded(self) -> bool:
        return self._toggle.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self._toggle.setChecked(bool(expanded))

    # ---- 内部 ----

    def _on_toggle(self, checked: bool) -> None:
        self._body.setVisible(bool(checked))
        arrow = "▾" if checked else "▸"
        tail = "点击折叠" if checked else "点击展开"
        self._toggle.setText(f"{arrow} 仿真对象参数（{tail}）")
        self.expandedChanged.emit(bool(checked))

    def _emit_params(self) -> None:
        if self._emitting:
            return
        self._sync_preset_label()
        self.paramsChanged.emit(self.params())

    def _on_preset_text(self, name: str) -> None:
        if name in PRESETS:
            self.apply_preset(name)

    def _sync_preset_label(self, values: Dict[str, float] | None = None) -> None:
        """参数与某个预设完全一致时把下拉切到该预设，否则显示“自定义”。"""
        current = self.params() if values is None else {
            key: float(spin.value()) for key, spin in self._spins.items()
        }
        matched = "自定义"
        for name, preset in PRESETS.items():
            if all(abs(current.get(k, 0.0) - v) <= 1e-9 for k, v in preset.items()):
                matched = name
                break
        self._preset.blockSignals(True)
        self._preset.setCurrentText(matched)
        self._preset.blockSignals(False)


__all__ = ["SimPanel", "CORE_PARAMS", "PARAM_ORDER", "PRESETS"]
