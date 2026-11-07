"""内置模拟器对象参数面板（card25 / 卡27）。

面板只做三件事：摆参数、填预设、把改后的参数打成快照发出去。

* **不持有引擎**：引擎在工作线程里，面板在主线程改值。面板只发 ``paramsChanged(dict)``，
  由主窗口转交引擎；引擎侧按快照加锁读，避免跨线程裸读控件。
* **默认折叠**：左栏已经靠滚动条撑着，展开后也不能把日志挤没——面板只提供
  ``set_expanded(bool)``，高度由自身内容决定，日志在滚动区外不受影响。
* **每项一句人话**：标签只写名字，解释放在行下方的次级色小字里，换主题也不丢。
* **参数口径从核心层来**：范围/默认/预设都在 ``serialpid.sim.params`` 与
  ``serialpid.sim.presets``，面板只负责控件与读写——持久化与扫参脚本共用同一套
  定义，不会再出现"面板能填 40 拍、脚本只认 20 拍"。

参数键（与 sim/plant.py 的对象参数一一对应）::

    gain      对象增益：执行量（0~100 %）放大成过程量变化的倍数
    tau1      第一级时间常数（s）
    tau2      第二级时间常数（s）
    delay     纯延迟（控制周期拍数）
    noise     测量噪声标准差（过程量单位）
    quant     测量量化步长（过程量单位）
    period_ms 控制周期（ms），即引擎节拍

改值即发快照并延迟落盘（``sim_state.json``，与 config.json 同目录），下次启动读回。
"""

from __future__ import annotations

from typing import Dict, List

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
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

from ..sim import sim_config
from ..sim.params import CORE_PARAMS, PARAM_ORDER, PARAM_SPECS
from ..sim.presets import DESCRIPTIONS, PRESETS
from .theme import COLORS

#: 兼容旧引用（卡25 的测试按这两个名字取用）
_PARAM_SPECS: Dict[str, Dict[str, object]] = PARAM_SPECS

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
        #: 落盘路径（与 config.json 同目录；测试可通过 SERIALPID_CONFIG 改变）
        self._state_path = sim_config.default_state_path()
        self.load_warning: str = ""
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(200)
        self._save_timer.timeout.connect(self.save_now)

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
        for name in PRESETS:
            self._preset.addItem(name)
            # 预设名下面挂一句"对应什么真实对象"，用户先认对象再谈参数
            index = self._preset.count() - 1
            self._preset.setItemData(index, DESCRIPTIONS.get(name, ""), Qt.ToolTipRole)
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

        self._load_saved()
        self._sync_preset_label()

    # ---- 持久化 ---------------------------------------------------------- #

    def _load_saved(self) -> None:
        """启动时读回上次那套参数；读不到就用默认，沿原样不打扰用户。"""
        try:
            values, warning = sim_config.load_params(self._state_path)
        except Exception as exc:  # noqa: BLE001 - 状态文件问题不该挡启动
            self.load_warning = f"仿真参数读取失败：{exc}"
            return
        self.load_warning = warning or ""
        self._emitting = True
        try:
            for key, value in values.items():
                spin = self._spins.get(key)
                if spin is None:
                    continue
                spin.setValue(type(spin.value())(value))
        finally:
            self._emitting = False

    def save_now(self) -> bool:
        """立即落盘（不弹任何提示）；返回是否写入成功。"""
        self._save_timer.stop()
        try:
            sim_config.save_params(self.params(), self._state_path)
            return True
        except OSError:
            return False

    def _schedule_save(self) -> None:
        self._save_timer.start()

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
            self._schedule_save()
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
        self._schedule_save()
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
