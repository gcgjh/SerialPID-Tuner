# -*- coding: utf-8 -*-
"""关于对话框（卡30）：版本、依赖、许可证，以及一键复制诊断信息。

用户报 bug 时最缺的是"他手上是什么版本、什么环境"。这个对话框只做两件事：

* 把**程序版本与关键依赖版本**摆出来（含 Python、操作系统）；
* 「复制诊断信息」把同一份内容放进剪贴板，直接粘到 issue / 聊天窗口。

版本号只有一个来源：包属性 ``serialpid.__version__``。若打包环境里没定义，
退回 ``FALLBACK_VERSION``，不与 README / CHANGELOG 各写一份。
"""

from __future__ import annotations

import platform
from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..config import default_config_path
from .theme import COLORS

#: 包属性没定义时的兜底版本（与 README / CHANGELOG 的当前版本一致）
FALLBACK_VERSION = "0.2.1"

_DEPENDENCIES = (
    ("PySide6", "PySide6"),
    ("pyqtgraph", "pyqtgraph"),
    ("pyserial", "pyserial"),
    ("numpy", "numpy"),
)


def program_version() -> str:
    """程序版本：优先读包属性，保证与打包脚本 / CHANGELOG 同一来源。"""
    try:
        import serialpid

        value = getattr(serialpid, "__version__", "")
    except Exception:  # noqa: BLE001 - 版本读不到不该影响关于框打开
        value = ""
    text = str(value).strip()
    return text if text else FALLBACK_VERSION


def dependency_version(distribution: str) -> str:
    """已安装依赖的版本；未安装如实返回「未安装」，不伪造。"""
    try:
        from importlib import metadata

        return metadata.version(distribution)
    except Exception:  # noqa: BLE001 - PackageNotFoundError / 元数据损坏都同样处理
        return "未安装"


def environment_rows() -> List[Tuple[str, str]]:
    """关于框与诊断文本共用的环境清单（顺序即显示顺序）。"""
    rows: List[Tuple[str, str]] = [
        ("程序版本", program_version()),
        ("Python", f"{platform.python_version()} ({platform.machine()})"),
        ("操作系统", platform.platform()),
    ]
    rows.extend((name, dependency_version(dist)) for name, dist in _DEPENDENCIES)
    rows.append(("配置文件", default_config_path()))
    return rows


def diagnostic_text() -> str:
    """可整段复制的诊断信息（与关于框同源，便于用户直接贴给开发者）。"""
    lines = ["SerialPID-Tuner 诊断信息"]
    lines.extend(f"{name}：{value}" for name, value in environment_rows())
    lines.append("许可证：MIT")
    return "\n".join(lines)


class AboutDialog(QDialog):
    """关于：版本 / 依赖 / 许可证 + 复制诊断。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("关于 SerialPID-Tuner")
        self.resize(560, 420)

        title = QLabel("SerialPID-Tuner")
        title.setStyleSheet(
            f"font-size: 18pt; font-weight: 600; color: {COLORS['text']};"
        )
        subtitle = QLabel("串口 PID 调试上位机 · MIT License")
        subtitle.setStyleSheet(f"color: {COLORS['muted']};")

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        self._value_labels: dict[str, QLabel] = {}
        for row, (name, value) in enumerate(environment_rows()):
            key = QLabel(name)
            key.setStyleSheet(f"color: {COLORS['muted']};")
            val = QLabel(value)
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            val.setWordWrap(True)
            self._value_labels[name] = val
            grid.addWidget(key, row, 0)
            grid.addWidget(val, row, 1)
        grid.setColumnStretch(1, 1)

        self._copy_btn = QPushButton("复制诊断信息")
        self._copy_btn.setToolTip("把版本与环境信息复制到剪贴板，报问题时直接粘贴")
        self._copy_btn.clicked.connect(self.copy_diagnostics)
        self._copied = QLabel("")
        self._copied.setStyleSheet(f"color: {COLORS['ok']};")
        close_btn = QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self._copied, 1)
        row.addWidget(self._copy_btn)
        row.addWidget(close_btn)

        box = QVBoxLayout(self)
        box.addWidget(title)
        box.addWidget(subtitle)
        box.addSpacing(8)
        box.addLayout(grid)
        box.addStretch(1)
        box.addLayout(row)

    # ---- 对外接口 ----

    def diagnostics(self) -> str:
        return diagnostic_text()

    def copy_diagnostics(self) -> str:
        """复制并给一行反馈；返回复制内容，便于测试核对。"""
        text = diagnostic_text()
        QApplication.clipboard().setText(text)
        self._copied.setText("已复制到剪贴板")
        return text

    def values(self) -> dict[str, str]:
        return {name: label.text() for name, label in self._value_labels.items()}


__all__ = [
    "AboutDialog",
    "FALLBACK_VERSION",
    "dependency_version",
    "diagnostic_text",
    "environment_rows",
    "program_version",
]
