"""深色主题配色与 QApplication 调色板。"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


#: 集中配色，面板与曲线共用，保证界面风格一致
COLORS = {
    "bg": "#1e222a",
    "panel": "#262b35",
    "border": "#3a4150",
    "text": "#e6e9ef",
    "muted": "#9aa3b2",
    "accent": "#4ea1ff",
    "ok": "#3fb950",
    "warn": "#d29922",
    "err": "#f85149",
    "sp": "#ffb454",     # 设定值曲线
    "pv": "#4ea1ff",     # 被控量曲线
    "out": "#a371f7",    # 输出量曲线
}


def apply_dark(app: QApplication) -> None:
    """把应用调色板切换为深色主题。"""
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(COLORS["bg"]))
    pal.setColor(QPalette.WindowText, QColor(COLORS["text"]))
    pal.setColor(QPalette.Base, QColor(COLORS["panel"]))
    pal.setColor(QPalette.AlternateBase, QColor(COLORS["bg"]))
    pal.setColor(QPalette.ToolTipBase, QColor(COLORS["panel"]))
    pal.setColor(QPalette.ToolTipText, QColor(COLORS["text"]))
    pal.setColor(QPalette.Text, QColor(COLORS["text"]))
    pal.setColor(QPalette.Button, QColor(COLORS["panel"]))
    pal.setColor(QPalette.ButtonText, QColor(COLORS["text"]))
    pal.setColor(QPalette.BrightText, QColor(COLORS["err"]))
    pal.setColor(QPalette.Link, QColor(COLORS["accent"]))
    pal.setColor(QPalette.Highlight, QColor(COLORS["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.PlaceholderText, QColor(COLORS["muted"]))
    app.setPalette(pal)

    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
