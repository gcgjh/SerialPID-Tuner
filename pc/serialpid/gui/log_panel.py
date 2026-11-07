"""事件日志与 CSV 导出面板。

导出只做两件事：从遥测缓冲取样本、弹保存对话框；格式（列序、六位小数、
UTF-8 带 BOM、CRLF、原子写）全部由 ``serialpid.csv_export.export_telemetry``
一处实现 —— 不要在按钮回调里自己 ``csv.writer`` 拼字符串（card12：
同一能力只允许一个实现，否则"测试测的"和"用户拿到的"会分叉）。
"""

from __future__ import annotations

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

from ..csv_export import CsvExportError, export_telemetry
from ..messages import UserMessage
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

        self._export.clicked.connect(self._on_export_clicked)
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

    # ------------------------------------------------------------------ 导出

    def _on_export_clicked(self) -> None:
        if not self._buffer.samples():
            self.log("缓冲为空，无数据可导出")
            return
        default = f"pid_log_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", default, "CSV (*.csv)")
        if not path:
            return
        self.export_to(path)

    def export_to(self, path: str) -> int:
        """把缓冲内全部样本写到 ``path``，返回写入行数（失败返回 0 并记日志）。

        这是界面侧的导出入口；不弹对话框，便于自动化测试与交叉验证。
        """
        samples = self._buffer.samples()
        if not samples:
            self.log("缓冲为空，无数据可导出")
            return 0
        try:
            rows = export_telemetry(samples, path)
        except (CsvExportError, OSError) as exc:
            # 卡30：日志里的失败也按「现象 / 原因 / 下一步」三段式
            self.log(
                UserMessage(
                    "导出失败，CSV 没有写出来",
                    f"目标路径不可写或不合法：{exc}",
                    "换一个有写权限的目录（例如桌面）再导出；当前数据仍在缓冲里，不会丢。",
                    detail=str(exc),
                ).as_block()
            )
            return 0
        self.log(f"已导出 {rows} 条记录（六位小数）-> {Path(path).name}")
        return rows
