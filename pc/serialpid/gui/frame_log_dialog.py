# -*- coding: utf-8 -*-
"""帧日志对话框（card28 / card33）：逐帧列表 + 导出 + 回放。

三块功能服务同一件事——"现场到底发生了什么"：

* **列表**：时间 / 方向 / 类型 / 长度 / 载荷摘要五列，滚着看最近数千帧。
  用 ``QAbstractTableModel`` 而不是逐行塞控件：几千行只建索引不建控件，
  滚动时才现取文本，界面不会卡。
* **导出**：把环形缓冲写成文本（给人看）或 JSONL（给机器读），用户把文件
  发给别人，对方看到的是原始帧而不是复述。文件偏大时先问一句体积。
* **回放**：读回 JSONL（本工具导出的、``sim/recorder.py`` 录的都认），按原始
  时序把 rx 帧重新喂给主窗口，曲线就能重现当时的样子。支持 0.5x / 1x / 4x、
  暂停与逐帧步进。

回放只放 rx 帧：tx 是上位机自己发的命令，重放那些只会重复下发命令，把界面
搞成"自己跟自己对话"。整个回放过程是只读重演，不碰任何真实通道。
"""

from __future__ import annotations

import os
import time
from typing import List, Optional, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
)

from ..frame_trace import (
    DIR_LABELS,
    FrameTrace,
    ReplayCursor,
    TraceEntry,
    load_jsonl_lenient,
    rx_only,
    summarize,
)
from ..messages import UserMessage
from .theme import COLORS

#: 回放刷新间隔（毫秒）。20 Hz 遥测下 20 ms 足够平滑，也不至于把主线程压满。
REPLAY_TICK_MS = 20
#: 倍速可选值（卡 33 点名要 0.5x / 1x / 4x，其余档位留作细看）
SPEED_CHOICES = ("0.5", "1", "2", "4", "10")
#: 导出超过这个体积就先问一句（2000 帧文本约 250 KB，几千帧就会到 MB 级）
EXPORT_SIZE_WARN_BYTES = 256 * 1024
#: 载荷摘要显示前几个字节
PAYLOAD_PREVIEW_BYTES = 8


def payload_summary(payload: bytes, limit: int = PAYLOAD_PREVIEW_BYTES) -> str:
    """载荷十六进制摘要：前 ``limit`` 字节，超出用省略号收尾。"""
    body = bytes(payload)
    head = " ".join(f"{byte:02X}" for byte in body[:limit])
    if len(body) > limit:
        return f"{head} …（共 {len(body)}B）"
    return head or "（空）"


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / 1024:.0f} KB"


class TraceTableModel(QAbstractTableModel):
    """把帧记录摊成五列表格（只存数据，不建控件）。"""

    HEADERS = ("时刻", "方向", "类型", "长度", "载荷摘要")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: List[TraceEntry] = []

    # ---- 数据 -------------------------------------------------------------

    def set_entries(self, entries: Sequence[TraceEntry]) -> None:
        self.beginResetModel()
        self._rows = list(entries)
        self.endResetModel()

    def entries(self) -> List[TraceEntry]:
        return self._rows

    # ---- Qt 模型接口 -------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt 命名
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self._rows[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            return self.cell_text(entry, column)
        if role == Qt.ToolTipRole:
            return f"+{entry.t_ms / 1000.0:.3f}s {entry.describe()}"
        if role == Qt.ForegroundRole and column == 1:
            # 两个方向给不同颜色：扫一眼就能分清"设备说的"和"我们发的"
            return QColor(COLORS["accent"] if entry.direction == "tx" else COLORS["muted"])
        return None

    # ---- 供测试与外部调用 --------------------------------------------------

    def cell_text(self, entry: TraceEntry, column: int) -> str:
        if column == 0:
            return f"+{entry.t_ms / 1000.0:.3f}s"
        if column == 1:
            return DIR_LABELS.get(entry.direction, entry.direction)
        if column == 2:
            return entry.name
        if column == 3:
            return str(len(entry.payload))
        if column == 4:
            return payload_summary(entry.payload)
        return ""


class FrameLogDialog(QDialog):
    """帧日志 / 回放窗口。

    用法::

        dlg = FrameLogDialog(self, worker.trace)
        dlg.framePlayed.connect(self._dispatch_frame)   # 回放帧按原时序送进界面
        dlg.replayStarted.connect(self._on_replay_started)
        dlg.exec()
    """

    #: 回放中到点的一帧（类型, 载荷），主窗口接到自己的帧分发槽
    framePlayed = Signal(int, bytes)
    #: 回放开始（主窗口据此清空现有曲线，避免两段数据拼在一条时间轴上）
    replayStarted = Signal()
    #: 回放结束（到最后一帧或用户停止）
    replayFinished = Signal()

    def __init__(
        self,
        parent=None,
        trace: Optional[FrameTrace] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("帧日志与回放")
        self.resize(880, 560)
        self._trace = trace
        self._cursor: Optional[ReplayCursor] = None
        self._last_tick = time.perf_counter()
        self._playing = False

        self._summary = QLabel("")
        self._kinds = QLabel("")
        self._kinds.setWordWrap(True)
        self._kinds.setStyleSheet(f"color: {COLORS['muted']};")

        self._model = TraceTableModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(22)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)

        #: 消息区：导出结果、坏行提示、回放进度说明（不混进帧列表）
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumBlockCount(200)
        self._detail.setMaximumHeight(96)
        self._detail.setPlaceholderText("导出 / 读取 / 回放的提示会出现在这里")

        self._export_text = QPushButton("导出文本")
        self._export_jsonl = QPushButton("导出 JSONL")
        self._refresh = QPushButton("刷新")
        self._close = QPushButton("关闭")

        self._file_label = QLabel("未选择文件")
        self._file_label.setStyleSheet(f"color: {COLORS['muted']};")
        self._file_label.setWordWrap(True)
        self._choose = QPushButton("选择文件…")
        self._speed = QComboBox()
        self._speed.addItems(SPEED_CHOICES)
        self._speed.setCurrentText("1")
        self._play = QPushButton("播放")
        self._step = QPushButton("单步")
        self._stop = QPushButton("停止")
        self._progress = QLabel("0 / 0")

        self._timer = QTimer(self)
        self._timer.setInterval(REPLAY_TICK_MS)
        self._timer.timeout.connect(self._on_tick)

        self._build_layout()
        self.refresh()

    # ---- 布局 -------------------------------------------------------------

    def _build_layout(self) -> None:
        box = QVBoxLayout(self)
        box.setSpacing(6)

        box.addWidget(QLabel("缓冲概览"))
        box.addWidget(self._summary)
        box.addWidget(self._kinds)
        box.addWidget(self._table, stretch=1)
        box.addWidget(self._detail)

        row1 = QHBoxLayout()
        row1.addWidget(self._refresh)
        row1.addWidget(self._export_text)
        row1.addWidget(self._export_jsonl)
        row1.addStretch(1)
        row1.addWidget(self._close)
        box.addLayout(row1)

        box.addWidget(QLabel("回放（读取导出的 JSONL；只读重演，不下发任何命令）"))
        row2 = QHBoxLayout()
        row2.addWidget(self._choose)
        row2.addWidget(self._file_label, stretch=1)
        row2.addWidget(QLabel("倍速"))
        row2.addWidget(self._speed)
        box.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(self._play)
        row3.addWidget(self._step)
        row3.addWidget(self._stop)
        row3.addStretch(1)
        row3.addWidget(self._progress)
        box.addLayout(row3)

        self._refresh.clicked.connect(self.refresh)
        self._export_text.clicked.connect(self.export_text)
        self._export_jsonl.clicked.connect(self.export_jsonl)
        self._close.clicked.connect(self.reject)
        self._choose.clicked.connect(self.choose_file)
        self._play.clicked.connect(self.toggle_play)
        self._step.clicked.connect(self.step_once)
        self._stop.clicked.connect(self.stop_replay)
        self._set_controls(False)

    # ---- 概览与导出 -------------------------------------------------------

    def refresh(self) -> None:
        """重新统计并刷新列表（打开窗口、导出前各刷一次）。"""
        if self._trace is None:
            self._summary.setText("没有可用的帧日志")
            self._kinds.setText("")
            self._model.set_entries([])
            self._set_export_enabled(False)
            return
        entries = self._trace.snapshot()
        dropped = self._trace.dropped()
        self._model.set_entries(entries)
        self._set_export_enabled(bool(entries))
        size = self._trace.memory_estimate_bytes()
        if not entries:
            self._summary.setText(
                f"缓冲为空（容量 {self._trace.capacity} 帧）"
                + (f"；更早的 {dropped} 帧已滚出缓冲" if dropped else "")
            )
            self._kinds.setText("")
            return
        span = (entries[-1].t_ms - entries[0].t_ms) / 1000.0
        tail = f"；更早的 {dropped} 帧已滚出缓冲" if dropped else ""
        self._summary.setText(
            f"缓冲 {len(entries)} / {self._trace.capacity} 帧，"
            f"覆盖最近 {span:.1f} s，约占 {_human_size(size)}{tail}"
        )
        counts = summarize(entries)
        self._kinds.setText(
            "类型分布：" + "，".join(f"{name} {count}" for name, count in sorted(counts.items()))
        )
        if self._model.rowCount() > 1:
            self._table.scrollToBottom()

    def _set_export_enabled(self, enabled: bool) -> None:
        for button in (self._export_text, self._export_jsonl):
            button.setEnabled(enabled)

    def export_text(self) -> bool:
        return self._export("帧日志文本", "txt")

    def export_jsonl(self) -> bool:
        return self._export("帧日志 JSONL", "jsonl")

    def _render(self, suffix: str) -> str:
        if self._trace is None:
            return ""
        return (
            self._trace.to_jsonl() if suffix.lower() == "jsonl" else self._trace.to_text()
        )

    def _export(self, caption: str, suffix: str) -> bool:
        if self._trace is None:
            return False
        path, _selected = QFileDialog.getSaveFileName(
            self, caption, "", f"*.{suffix}"
        )
        if not path:
            return False
        content = self._render(suffix)
        size = len(content.encode("utf-8"))
        if size >= EXPORT_SIZE_WARN_BYTES and not self.confirm_large_export(path, size):
            return False
        return self._write(path, content)

    def confirm_large_export(self, path: str, size: int) -> bool:
        """大文件先问一句（默认"取消"，避免用户误点后等半天）。"""
        answer = QMessageBox.question(
            self,
            "文件偏大",
            f"这份帧日志约 {_human_size(size)}（{path}）。\n"
            "继续导出会写满这个文件，也便于直接发给别人；取消则重新选个位置。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Ok,
        )
        return answer == QMessageBox.Ok

    def export_to(self, path: str, suffix: str = "txt") -> bool:
        """把缓冲写成文件（不弹对话框、不弹体积确认，便于测试与脚本调用）。"""
        if self._trace is None:
            return False
        return self._write(path, self._render(suffix))

    def _write(self, path: str, content: str) -> bool:
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        except OSError as exc:
            self._notify(
                UserMessage(
                    "导出失败，帧日志没有写出来",
                    f"目标路径不可写或不合法：{exc}",
                    "换一个有写权限的目录（例如桌面）再导出；缓冲里的帧还在。",
                    detail=str(exc),
                )
            )
            return False
        self._note(f"已导出到 {path}（{_human_size(len(content.encode('utf-8')))}）")
        return True

    def _notify(self, message: UserMessage) -> None:
        self._detail.appendPlainText(message.as_block())

    def _note(self, text: str) -> None:
        self._detail.appendPlainText(text)

    # ---- 回放 -------------------------------------------------------------

    def choose_file(self) -> Optional[str]:
        path, _selected = QFileDialog.getOpenFileName(
            self, "选择帧日志", "", "JSONL (*.jsonl);;所有文件 (*)"
        )
        if not path:
            return None
        if not self.load_file(path):
            return None
        return path

    def load_file(self, path: str) -> bool:
        """读入一条 JSONL 帧流并重置回放游标。

        坏行**跳过并计数**：文件来自外部，一行坏掉不该让整份录音打不开；
        但跳过了必须说清楚，所以提示里带行号与条数。
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            self._notify(
                UserMessage(
                    "读取失败，帧日志没有打开",
                    f"文件缺失、没有读权限，或不是 UTF-8 文本：{exc}",
                    "确认文件还在原位置、能被记事本打开，再重试。",
                    detail=str(exc),
                )
            )
            return False

        entries, skipped, notes = load_jsonl_lenient(text)
        playable = rx_only(entries)
        self._cursor = ReplayCursor(playable, speed=float(self._speed.currentText()))
        self._file_label.setText(
            f"{os.path.basename(path)} · 读入 {len(entries)} 帧 / 可回放 rx {len(playable)} 帧"
            + (f" · 跳过 {skipped} 行坏记录" if skipped else "")
        )
        self._progress.setText(f"0 / {len(playable)}")
        self._set_controls(bool(playable))
        if skipped:
            self._notify(
                UserMessage(
                    f"文件里有 {skipped} 行无法解析，已跳过（其余 {len(playable)} 帧照常回放）",
                    "行格式不对、字段缺失或载荷不是合法十六进制：" + "；".join(notes[:3]),
                    "如果这段本该有帧，说明导出时就被截断了；只想看能读的部分就不用管。",
                    detail="\n".join(notes),
                )
            )
        if not entries:
            self._note("文件里没有任何可解析的帧（可能不是本工具导出的 JSONL）")
        elif not playable:
            self._note("文件里只有上行（tx）帧，没有可回放的下行帧")
        return True

    def toggle_play(self) -> None:
        if self._cursor is None or self._cursor.finished:
            self._start_replay()
            return
        if self._playing:
            self._pause()
        else:
            self._play_now()

    def _start_replay(self) -> None:
        if self._cursor is None or self._cursor.total == 0:
            return
        self._cursor.reset()
        self.replayStarted.emit()
        self._play_now()

    def _play_now(self) -> None:
        self._cursor.set_speed(float(self._speed.currentText()))
        self._playing = True
        self._play.setText("暂停")
        self._last_tick = time.perf_counter()
        self._timer.start()

    def _pause(self) -> None:
        self._playing = False
        self._play.setText("播放")
        self._timer.stop()

    def step_once(self) -> None:
        """单步：强制放出下一帧（暂停状态下也能用）。"""
        if self._cursor is None:
            return
        if not self._playing and self._cursor.position == 0:
            self.replayStarted.emit()
        self._pause()
        for entry in self._cursor.step():
            self._emit(entry)
        self._sync_progress()
        if self._cursor.finished:
            self._finish()

    def stop_replay(self) -> None:
        self._pause()
        if self._cursor is not None:
            self._cursor.reset()
        self._progress.setText(f"0 / {self._cursor.total if self._cursor else 0}")

    def _on_tick(self) -> None:
        if self._cursor is None:
            return
        now = time.perf_counter()
        dt_ms = max((now - self._last_tick) * 1000.0, 0.0)
        self._last_tick = now
        for entry in self._cursor.advance(dt_ms):
            self._emit(entry)
        self._sync_progress()
        if self._cursor.finished:
            self._finish()

    def _emit(self, entry: TraceEntry) -> None:
        self.framePlayed.emit(entry.pkt_type, entry.payload)

    def _sync_progress(self) -> None:
        if self._cursor is None:
            return
        self._progress.setText(f"{self._cursor.position} / {self._cursor.total}")

    def _finish(self) -> None:
        self._pause()
        self._progress.setText(
            f"{self._cursor.position} / {self._cursor.total}（已结束）"
            if self._cursor
            else "0 / 0"
        )
        self.replayFinished.emit()

    def _set_controls(self, enabled: bool) -> None:
        for widget in (self._play, self._step, self._stop):
            widget.setEnabled(enabled)

    # ---- 测试友好 ---------------------------------------------------------

    def cursor(self) -> Optional[ReplayCursor]:
        return self._cursor

    def is_playing(self) -> bool:
        return self._playing

    def progress_text(self) -> str:
        return self._progress.text()

    def summary_text(self) -> str:
        return self._summary.text()

    def kinds_text(self) -> str:
        return self._kinds.text()

    def detail_text(self) -> str:
        return self._detail.toPlainText()

    def table(self) -> QTableView:
        return self._table

    def model(self) -> TraceTableModel:
        return self._model

    def frame_rows(self) -> int:
        return self._model.rowCount()

    def column_headers(self) -> List[str]:
        return list(TraceTableModel.HEADERS)

    def cell_text(self, row: int, column: int) -> str:
        entry = self._model.entries()[row]
        return self._model.cell_text(entry, column)

    def speed_choices(self) -> List[str]:
        return [self._speed.itemText(index) for index in range(self._speed.count())]

    def set_speed_choice(self, text: str) -> bool:
        index = self._speed.findText(text)
        if index < 0:
            return False
        self._speed.setCurrentIndex(index)
        return True

    def tick_once(self, dt_ms: float = REPLAY_TICK_MS) -> None:
        """测试用：不等真实定时器，直接推进一次。"""
        if self._cursor is None:
            return
        for entry in self._cursor.advance(dt_ms):
            self._emit(entry)
        self._sync_progress()
        if self._cursor.finished:
            self._finish()
