# -*- coding: utf-8 -*-
"""状态栏链路质量指示（card28）：一个圆点 + 三个数字，悬停看详情。

设计取舍：
* 状态栏位置有限，只放「质量灯 + 帧率 + 往返时延 + 约丢帧数」；
* 细节（收发帧数、坏帧占比、最大时延、抖动、会话时长、口径说明）放悬停
  提示里——需要的时候能拿到，不需要的时候不占地方；
* 本控件只认 :class:`LinkSnapshot`，不反向依赖通信层，方便离屏单测。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..link_stats import (
    QUALITY_BAD,
    QUALITY_IDLE,
    QUALITY_OK,
    QUALITY_WARN,
    LinkSnapshot,
)
from .theme import COLORS

#: 质量等级 -> 灯的颜色
_QUALITY_COLORS = {
    QUALITY_OK: COLORS["ok"],
    QUALITY_WARN: COLORS["warn"],
    QUALITY_BAD: COLORS["err"],
    QUALITY_IDLE: COLORS["muted"],
}
#: 质量等级 -> 灯的可读文本（无障碍 / 截图时也认得出）
_QUALITY_LABELS = {
    QUALITY_OK: "链路",
    QUALITY_WARN: "链路偏差",
    QUALITY_BAD: "链路差",
    QUALITY_IDLE: "未连接",
}


class LinkStatusWidget(QWidget):
    """状态栏常驻的质量指示。``update_snapshot`` 由界面定时器（1 Hz）驱动。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(f"color: {COLORS['muted']};")
        self._text = QLabel("未连接")
        self._text.setStyleSheet(f"color: {COLORS['muted']};")

        box = QHBoxLayout(self)
        box.setContentsMargins(6, 0, 6, 0)
        box.setSpacing(4)
        box.addWidget(self._dot)
        box.addWidget(self._text)

        self._snapshot = LinkSnapshot()
        self.setToolTip(self._snapshot.details())

    # ---- 对外 -------------------------------------------------------------

    def update_snapshot(self, snapshot: LinkSnapshot) -> None:
        """把最新快照画出来；同一帧内多次调用不会抖。"""
        self._snapshot = snapshot
        color = _QUALITY_COLORS.get(snapshot.quality, COLORS["muted"])
        self._dot.setStyleSheet(f"color: {color};")
        if snapshot.quality == QUALITY_IDLE:
            # 未连接时快照本身就是"未连接"，别再拼一个质量词叠成"未连接 未连接"
            text = snapshot.summary()
        else:
            text = f"{_QUALITY_LABELS.get(snapshot.quality, '链路')} {snapshot.summary()}"
        self._text.setText(text.strip())
        self._text.setStyleSheet(
            f"color: {color if snapshot.quality != QUALITY_IDLE else COLORS['muted']};"
        )
        self.setToolTip(snapshot.details())

    def snapshot(self) -> LinkSnapshot:
        return self._snapshot

    def text(self) -> str:
        """当前显示的一行文本（测试与日志用）。"""
        return self._text.text()

    def quality(self) -> str:
        return self._snapshot.quality
