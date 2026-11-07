"""事件日志与 CSV 导出面板。"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import TelemetryBuffer


class LogPanel(QWidget):
    def __init__(self, buffer: TelemetryBuffer, parent=None) -> None:
        super().__init__(parent)
        self._buffer = buffer

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(2000)

        self._export = QPushButton("导出 CSV")
        self._clear_log = QPushButton("清空日志")
        self._clear_plot = QPushButton("清空曲线")

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._export)
        btn_row.addWidget(self._clear_log)
        btn_row.addWidget(self._clear_plot)

        box = QGroupBox("日志 / 记录")
        layout = QVBoxLayout(box)
        layout.addWidget(self._view)
        layout.addLayout(btn_row)

        outer = QVBoxLayout(self)
        outer.addWidget(box)

        self._export.clicked.connect(self._export_csv)
        self._clear_log.clicked.connect(self._view.clear)

    def log(self, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._view.appendPlainText(f"[{ts}] {message}")

    def clear_buffer_signal(self):
        """由主窗口连接：清空曲线与缓冲。"""
        return self._clear_plot

    def clear_log_signal(self):
        """由主窗口连接：清空日志文字（聚合状态由主窗口一并重置）。"""
        return self._clear_log

    def _export_csv(self) -> None:
        samples = self._buffer.samples()
        if not samples:
            self.log("缓冲为空，无数据可导出")
            return
        default = f"pid_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default, "CSV (*.csv)")
        if not path:
            return
        # UTF-8 BOM，Excel 直开不乱码
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["t_ms", "SP", "PV", "OUT", "err"])
            for s in samples:
                writer.writerow([s.t_ms, f"{s.sp:.4f}", f"{s.pv:.4f}", f"{s.out:.4f}", f"{s.err:.4f}"])
        self.log(f"已导出 {len(samples)} 条记录 -> {Path(path).name}")
