# -*- coding: utf-8 -*-
"""链路设置（card32）：心跳开关 / 间隔 / 容忍次数，以及统计清零。

为什么是"改一下立即生效"而不是"确定/取消"：

* 心跳能不能救回一条半死的链路，是现场立等可见的事；攒到点确定才生效，
  用户会以为开关没用；
* 参数非法由输入框的上下限和通信核心各守一道，没有"填错了再报错"的中间态。

为什么关掉心跳时把间隔/次数置灰但保留数值：再打开还是上次那组，现场用户
不用重填。判死口径随开关一起收敛——关掉就回到旧版的空闲 1.0 s。

仿真链路的故障注入不在这里：那是「故障注入（仿真链路）」面板（card33）的职责，
两边都做会出现"一处写着 0%、另一处还在丢帧"的自相矛盾，所以本对话框只管心跳
与统计这两件**与链路死活判定直接相关**的设置。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
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

from ..comm import (
    HEARTBEAT_INTERVAL_MAX_S,
    HEARTBEAT_INTERVAL_MIN_S,
    HEARTBEAT_MAX_MISSED_MAX,
    HEARTBEAT_MAX_MISSED_MIN,
    IDLE_TIMEOUT_NO_HEARTBEAT_S,
)

#: 再打开心跳时的回落间隔（配置里是关闭状态时用这个填输入框）
DEFAULT_INTERVAL_S = 1.0


class LinkSettingsDialog(QDialog):
    """链路设置对话框：心跳三参数 + 统计清零。

    ``heartbeatChanged(interval_s, max_missed)`` 里 ``interval_s == 0`` 表示关闭；
    对话框本身不写配置、不改通信核心——这些由主窗口做，方便离屏单测。
    """

    heartbeatChanged = Signal(float, int)
    statsReset = Signal()

    def __init__(
        self,
        *,
        heartbeat_interval: float = 0.0,
        heartbeat_max_missed: int = 3,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("链路设置")
        self.setMinimumWidth(460)
        self._loading = True
        self._last_interval = (
            float(heartbeat_interval) if heartbeat_interval > 0.0 else DEFAULT_INTERVAL_S
        )

        # ---- 心跳 ----
        self.heartbeat_enabled = QCheckBox("开启心跳（周期 PING，收到 PONG 即算往返时延）")
        self.heartbeat_enabled.setChecked(heartbeat_interval > 0.0)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(HEARTBEAT_INTERVAL_MIN_S, HEARTBEAT_INTERVAL_MAX_S)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(
            float(heartbeat_interval) if heartbeat_interval > 0.0 else DEFAULT_INTERVAL_S
        )
        self.interval_spin.setToolTip(
            "发送 PING 的间隔（0.2–60 s）。太短会占链路带宽，太长则掉线发现得慢。\n"
            "低质量链路（无线透传 / 长线）建议 2–5 s，别用 0.2 s。"
        )

        self.missed_spin = QSpinBox()
        self.missed_spin.setRange(HEARTBEAT_MAX_MISSED_MIN, HEARTBEAT_MAX_MISSED_MAX)
        self.missed_spin.setValue(max(int(heartbeat_max_missed), HEARTBEAT_MAX_MISSED_MIN))
        self.missed_spin.setSuffix(" 次")
        self.missed_spin.setToolTip(
            "连续多少次 PING 没有 PONG 才判死。前提是同一窗口内**没有任何帧**：\n"
            "对端只顾着发遥测、没空回 PONG 时不会判死。"
        )

        self._idle_hint = QLabel(
            f"关闭心跳时按空闲 {IDLE_TIMEOUT_NO_HEARTBEAT_S:.1f} s 判死（与旧版一致）；"
            "开启后判死窗口 = 间隔 × 次数 + 1 s。"
        )
        self._idle_hint.setWordWrap(True)
        self._idle_hint.setStyleSheet("color: #9aa3b2;")

        heart_form = QFormLayout()
        heart_form.addRow("心跳间隔", self.interval_spin)
        heart_form.addRow("容忍次数", self.missed_spin)
        heart_box = QVBoxLayout()
        heart_box.addWidget(self.heartbeat_enabled)
        heart_box.addLayout(heart_form)
        heart_box.addWidget(self._idle_hint)
        heart_group = QGroupBox("心跳")
        heart_group.setLayout(heart_box)

        # ---- 统计清零 ----
        self.reset_button = QPushButton("统计清零")
        self.reset_button.setToolTip(
            "把收发帧数 / 坏帧 / 约丢帧 / RTT 从 0 重新计；不断开连接、不清帧日志。\n"
            "断开重连会自动清零。"
        )
        stats_note = QLabel(
            "清零只影响统计口径，不改变连接与曲线；断开重连后也会自动从 0 开始。"
        )
        stats_note.setWordWrap(True)
        stats_note.setStyleSheet("color: #9aa3b2;")
        stats_row = QHBoxLayout()
        stats_row.addWidget(self.reset_button)
        stats_row.addWidget(stats_note, stretch=1)
        stats_box = QVBoxLayout()
        stats_box.addLayout(stats_row)
        stats_group = QGroupBox("统计")
        stats_group.setLayout(stats_box)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(close_btn)

        top = QLabel("改动立即生效并写入 config.json，重启后保留。")
        top.setWordWrap(True)
        top.setStyleSheet("color: #9aa3b2;")

        layout = QVBoxLayout(self)
        layout.addWidget(top)
        layout.addWidget(heart_group)
        layout.addWidget(stats_group)
        layout.addLayout(close_row)

        self._sync_enabled()
        self._loading = False
        self.heartbeat_enabled.toggled.connect(self._on_heartbeat_toggled)
        self.interval_spin.valueChanged.connect(self._emit_heartbeat)
        self.missed_spin.valueChanged.connect(self._emit_heartbeat)
        self.reset_button.clicked.connect(self.statsReset.emit)

    # ---- 对外 -------------------------------------------------------------

    def heartbeat_values(self) -> tuple:
        """当前心跳参数：``(间隔秒, 容忍次数)``，间隔 0 表示关闭。"""
        if not self.heartbeat_enabled.isChecked():
            return 0.0, int(self.missed_spin.value())
        return float(self.interval_spin.value()), int(self.missed_spin.value())

    def set_heartbeat_values(self, interval_s: float, max_missed: int) -> None:
        """外部改值（测试与"恢复默认"用）；不触发信号。"""
        self._loading = True
        try:
            self.heartbeat_enabled.setChecked(float(interval_s) > 0.0)
            if float(interval_s) > 0.0:
                self._last_interval = float(interval_s)
                self.interval_spin.setValue(float(interval_s))
            else:
                self.interval_spin.setValue(self._last_interval)
            self.missed_spin.setValue(int(max_missed))
            self._sync_enabled()
        finally:
            self._loading = False

    # ---- 内部 -------------------------------------------------------------

    def _sync_enabled(self) -> None:
        """关掉心跳时置灰但保留数值，再打开沿用上次那组。"""
        heart_on = self.heartbeat_enabled.isChecked()
        self.interval_spin.setEnabled(heart_on)
        self.missed_spin.setEnabled(heart_on)

    def _on_heartbeat_toggled(self, checked: bool) -> None:
        if checked:
            # 再打开时用关掉前那组间隔，用户不用重填
            self.interval_spin.setValue(max(self._last_interval, HEARTBEAT_INTERVAL_MIN_S))
        else:
            self._last_interval = float(self.interval_spin.value())
        self._sync_enabled()
        self._emit_heartbeat()

    def _emit_heartbeat(self, *_args) -> None:
        if self._loading:
            return
        interval, missed = self.heartbeat_values()
        if interval > 0.0:
            self._last_interval = interval
        self.heartbeatChanged.emit(interval, missed)
