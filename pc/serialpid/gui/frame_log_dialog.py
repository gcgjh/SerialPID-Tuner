# -*- coding: utf-8 -*-
"""帧日志对话框（card28）：概览 + 导出 + 回放。

两块功能放在同一个窗口里，是因为它们服务同一件事——"现场到底发生了什么"：

* **导出**：把环形缓冲里最近的帧写成文本（给人看）或 JSONL（给机器读），
  用户把文件发给别人，对方看到的是原始帧而不是复述；
* **回放**：读回 JSONL，按原始时序把 rx 帧重新喂给主窗口，曲线就能重现当时
  的样子。支持倍速与单步，方便盯着某一帧看。

回放只放 rx 帧：tx 是上位机自己发的命令，重放那些只会重复下发命令，把界面
搞成"自己跟自己对话"。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..frame_trace import FrameTrace, ReplayCursor, load_jsonl, rx_only, summarize
from ..messages import UserMessage
from .theme import COLORS

#: 回放刷新间隔（毫秒）。20 Hz 遥测下 20 ms 足够平滑，也不至于把主线程压满。
REPLAY_TICK_MS = 20
#: 倍速可选值
SPEED_CHOICES = ("0.5", "1", "2", "5", "10")


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
        self.resize(620, 460)
        self._trace = trace
        self._cursor: Optional[ReplayCursor] = None
        self._last_tick = time.perf_counter()
        self._playing = False

        self._summary = QLabel("")
        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setMaximumBlockCount(200)
        self._detail.setPlaceholderText("点「刷新概览」查看最近帧的类型分布")

        self._export_text = QPushButton("导出文本")
        self._export_jsonl = QPushButton("导出 JSONL")
        self._refresh = QPushButton("刷新概览")
        self._close = QPushButton("关闭")

        self._file_label = QLabel("未选择文件")
        self._file_label.setStyleSheet(f"color: {COLORS['muted']};")
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
        box.addWidget(self._detail, stretch=1)

        row1 = QHBoxLayout()
        row1.addWidget(self._refresh)
        row1.addWidget(self._export_text)
        row1.addWidget(self._export_jsonl)
        row1.addStretch(1)
        row1.addWidget(self._close)
        box.addLayout(row1)

        box.addWidget(QLabel("回放（读取导出的 JSONL）"))
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
        """重新统计缓冲里的帧（打开窗口、导出前各刷一次）。"""
        if self._trace is None:
            self._summary.setText("没有可用的帧日志")
            self._detail.setPlainText("")
            self._set_export_enabled(False)
            return
        entries = self._trace.snapshot()
        dropped = self._trace.dropped()
        self._set_export_enabled(bool(entries))
        if not entries:
            self._summary.setText(
                f"缓冲为空（容量 {self._trace.capacity} 帧）"
                + (f"；更早的 {dropped} 帧已滚出缓冲" if dropped else "")
            )
            self._detail.setPlainText("")
            return
        span = (entries[-1].t_ms - entries[0].t_ms) / 1000.0
        tail = f"；更早的 {dropped} 帧已滚出缓冲" if dropped else ""
        self._summary.setText(
            f"缓冲 {len(entries)} / {self._trace.capacity} 帧，"
            f"覆盖最近 {span:.1f} s{tail}"
        )
        counts = summarize(entries)
        recent = entries[-12:]
        lines = ["类型分布："]
        lines += [f"  {name}: {count} 帧" for name, count in sorted(counts.items())]
        lines += ["", "最近 12 帧："]
        lines += [
            f"  +{e.t_ms / 1000.0:8.3f}s {'←' if e.direction == 'rx' else '→'} {e.describe()}"
            for e in recent
        ]
        self._detail.setPlainText("\n".join(lines))

    def _set_export_enabled(self, enabled: bool) -> None:
        for button in (self._export_text, self._export_jsonl):
            button.setEnabled(enabled)

    def export_text(self) -> bool:
        return self._export("帧日志文本", "txt")

    def export_jsonl(self) -> bool:
        return self._export("帧日志 JSONL", "jsonl")

    def _export(self, caption: str, suffix: str) -> bool:
        if self._trace is None:
            return False
        path, _selected = QFileDialog.getSaveFileName(
            self, caption, "", f"*.{suffix}"
        )
        if not path:
            return False
        return self.export_to(path, suffix)

    def export_to(self, path: str, suffix: str = "txt") -> bool:
        """把缓冲写成文件；失败时在详情框里说明原因（不弹模态框，便于测试）。"""
        if self._trace is None:
            return False
        content = (
            self._trace.to_jsonl() if suffix.lower() == "jsonl" else self._trace.to_text()
        )
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
        except OSError as exc:
            self._detail.appendPlainText(
                UserMessage(
                    "导出失败，帧日志没有写出来",
                    f"目标路径不可写或不合法：{exc}",
                    "换一个有写权限的目录（例如桌面）再导出；缓冲里的帧还在。",
                    detail=str(exc),
                ).as_block()
            )
            return False
        self._detail.appendPlainText(f"已导出到 {path}")
        return True

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
        """读入一条 JSONL 帧流并重置回放游标。"""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            entries = rx_only(load_jsonl(text))
        except (OSError, ValueError) as exc:
            self._detail.appendPlainText(
                UserMessage(
                    "读取失败，帧日志没有打开",
                    f"文件缺失、不是 UTF-8 文本，或 JSONL 行格式不对：{exc}",
                    "确认选的是本工具导出的 .jsonl（每行一个 JSON 对象）再重试。",
                    detail=str(exc),
                ).as_block()
            )
            return False
        self._cursor = ReplayCursor(entries, speed=float(self._speed.currentText()))
        self._file_label.setText(f"{os.path.basename(path)} · rx {len(entries)} 帧")
        self._progress.setText(f"0 / {len(entries)}")
        self._set_controls(bool(entries))
        if not entries:
            self._detail.appendPlainText("文件里没有可回放的 rx 帧")
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

    def _emit(self, entry) -> None:
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

    def detail_text(self) -> str:
        return self._detail.toPlainText()

    def tick_once(self, dt_ms: float = REPLAY_TICK_MS) -> None:
        """测试用：不等真实定时器，直接推进一次。"""
        if self._cursor is None:
            return
        for entry in self._cursor.advance(dt_ms):
            self._emit(entry)
        self._sync_progress()
        if self._cursor.finished:
            self._finish()
