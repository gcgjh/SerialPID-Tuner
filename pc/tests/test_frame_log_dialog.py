# -*- coding: utf-8 -*-
"""card33 帧日志对话框：五列列表、数千帧不卡、导出体积提示、畸形文件容错、
倍速（0.5 / 1 / 4）与暂停逐帧步进、回放只放 rx。

运行：cd pc && python -m pytest tests/test_frame_log_dialog.py -q
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover - 依赖环境
    _HAS_QT = False

from serialpid.frame_trace import DIR_RX, DIR_TX, FrameTrace, TraceEntry  # noqa: E402
from serialpid.protocol import PacketType, pack_telemetry  # noqa: E402

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _dialog(trace=None):
    from serialpid.gui.frame_log_dialog import FrameLogDialog

    _app()
    return FrameLogDialog(None, trace)


def _fill(trace: FrameTrace, count: int, step_ms: int = 50) -> None:
    for index in range(count):
        trace.append(
            DIR_RX,
            PacketType.TELEMETRY,
            pack_telemetry(index, 28.0, 27.5, 10.0, -0.5),
            t_ms=index * step_ms,
        )


# ---------------------------------------------------------------- 列表


def test_table_has_five_columns_with_readable_values() -> None:
    trace = FrameTrace(capacity=2000, clock=_Clock())
    payload = pack_telemetry(100, 28.0, 27.5, 10.0, -0.5)
    trace.append(DIR_RX, PacketType.TELEMETRY, payload, t_ms=100)
    trace.append(DIR_TX, PacketType.PING, b"", t_ms=120)
    dialog = _dialog(trace)
    dialog.refresh()

    assert dialog.column_headers() == ["时刻", "方向", "类型", "长度", "载荷摘要"]
    assert dialog.frame_rows() == 2
    assert dialog.cell_text(0, 0) == "+0.100s"
    assert dialog.cell_text(0, 1) == "接收"
    assert dialog.cell_text(0, 2) == "TELEMETRY"
    assert dialog.cell_text(0, 3) == str(len(payload))
    # 摘要按字节分组：前 8 字节十六进制 + 总长度（20B 会截断）
    assert dialog.cell_text(0, 4).startswith("64 00 00 00")
    assert "共 20B" in dialog.cell_text(0, 4)
    assert dialog.cell_text(1, 1) == "发送"
    # 摘要行仍保留"缓冲 N / 容量 帧"的口径（既有测试与文档都引用它）
    assert "缓冲 2 / 2000 帧" in dialog.summary_text()


def test_thousands_of_frames_render_without_blocking() -> None:
    """几千帧只建索引不建控件：刷新一次应在几十毫秒量级。"""
    trace = FrameTrace(capacity=20000, clock=_Clock())
    _fill(trace, 8000)
    dialog = _dialog(trace)

    started = time.perf_counter()
    dialog.refresh()
    elapsed = time.perf_counter() - started

    assert dialog.frame_rows() == 8000
    assert elapsed < 0.5, f"8000 帧刷新用了 {elapsed:.3f}s"
    assert "缓冲 8000 / 20000 帧" in dialog.summary_text()


def test_payload_summary_truncates_long_payload() -> None:
    from serialpid.gui.frame_log_dialog import PAYLOAD_PREVIEW_BYTES, payload_summary

    assert payload_summary(b"") == "（空）"
    short = payload_summary(b"\x01\x02")
    assert short == "01 02"
    long_body = bytes(range(20))
    text = payload_summary(long_body)
    assert text.count("XX") == 0
    assert "…（共 20B）" in text
    assert len(text.split("…")[0].split()) == PAYLOAD_PREVIEW_BYTES


# ---------------------------------------------------------------- 导出


def test_jsonl_export_is_readable_and_has_no_private_fields(tmp_path) -> None:
    trace = FrameTrace(capacity=2000, clock=_Clock())
    _fill(trace, 3)
    dialog = _dialog(trace)
    path = tmp_path / "frames.jsonl"
    assert dialog.export_to(str(path), "jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    row = json.loads(lines[0])
    assert set(row) == {"t", "dir", "dir_label", "type", "name", "len", "payload"}
    assert row["dir"] == "rx"
    text = path.read_text(encoding="utf-8")
    assert "COM" not in text and str(tmp_path) not in text


def test_large_export_asks_before_writing(tmp_path, monkeypatch) -> None:
    from serialpid.gui import frame_log_dialog as mod

    trace = FrameTrace(capacity=4000, clock=_Clock())
    _fill(trace, 3000)
    dialog = _dialog(trace)
    path = tmp_path / "big.jsonl"
    monkeypatch.setattr(
        mod.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), "")
    )
    asked: list[int] = []

    def refuse(target: str, size: int) -> bool:
        asked.append(size)
        return False

    monkeypatch.setattr(dialog, "confirm_large_export", refuse)
    assert dialog.export_jsonl() is False
    assert asked and asked[0] > mod.EXPORT_SIZE_WARN_BYTES
    assert not path.exists(), "用户取消后不该留下半份文件"

    monkeypatch.setattr(dialog, "confirm_large_export", lambda target, size: True)
    assert dialog.export_jsonl() is True
    assert path.exists()


def test_export_to_small_file_does_not_ask(tmp_path, monkeypatch) -> None:
    trace = FrameTrace(capacity=2000, clock=_Clock())
    _fill(trace, 3)
    dialog = _dialog(trace)
    called: list[int] = []
    monkeypatch.setattr(
        dialog, "confirm_large_export", lambda target, size: called.append(size) or True
    )
    assert dialog.export_to(str(tmp_path / "small.txt"), "txt")
    assert called == [], "小文件不该打断用户"


# ---------------------------------------------------------------- 畸形文件


def test_malformed_file_is_skipped_and_counted(tmp_path) -> None:
    good = '{"t":0,"dir":"rx","type":1,"len":0,"payload":""}'
    third = '{"t":50,"dir":"rx","type":1,"len":0,"payload":""}'
    last = '{"t":100,"dir":"rx","type":1,"len":0,"payload":""}'
    path = tmp_path / "broken.jsonl"
    path.write_text(
        "\n".join([good, "{not json", third, '{"t":30,"dir":"侧向","type":1,"payload":""}', last])
        + "\n",
        encoding="utf-8",
    )
    dialog = _dialog()
    assert dialog.load_file(str(path)) is True
    assert dialog.cursor() is not None
    assert dialog.cursor().total == 3
    detail = dialog.detail_text()
    assert "2 行无法解析，已跳过" in detail
    assert "第 2 行" in detail and "第 4 行" in detail
    assert "跳过 2 行坏记录" in dialog._file_label.text()


def test_recorder_format_file_can_be_replayed(tmp_path) -> None:
    from serialpid.protocol import encode

    raw = encode(PacketType.TELEMETRY, pack_telemetry(9, 28.0, 27.0, 10.0, 0.0))
    path = tmp_path / "recorder.jsonl"
    path.write_text(
        json.dumps(
            {
                "t_ms": 0,
                "dir": "down",
                "type": PacketType.TELEMETRY,
                "len": len(raw),
                "raw": raw.hex(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dialog = _dialog()
    assert dialog.load_file(str(path)) is True
    assert dialog.cursor().total == 1


def test_missing_file_reports_three_part_message(tmp_path) -> None:
    dialog = _dialog()
    assert dialog.load_file(str(tmp_path / "absent.jsonl")) is False
    detail = dialog.detail_text()
    assert "读取失败" in detail and "下一步" in detail


# ---------------------------------------------------------------- 倍速与单步


def _write_stream(path, times) -> None:
    rows = [
        {
            "t": t,
            "dir": "rx",
            "type": PacketType.TELEMETRY,
            "name": "TELEMETRY",
            "len": 0,
            "payload": "",
        }
        for t in times
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_speed_choices_include_half_one_and_four() -> None:
    from serialpid.gui.frame_log_dialog import SPEED_CHOICES

    assert {"0.5", "1", "4"} <= set(SPEED_CHOICES)
    dialog = _dialog()
    assert dialog.set_speed_choice("4") is True


def test_play_pause_and_step_frame_by_frame(tmp_path) -> None:
    path = tmp_path / "stream.jsonl"
    _write_stream(path, [0, 50, 100])
    dialog = _dialog()
    assert dialog.load_file(str(path))

    played: list[int] = []
    dialog.framePlayed.connect(lambda kind, payload: played.append(kind))

    dialog.toggle_play()
    assert dialog.is_playing() is True
    dialog._pause()
    assert dialog.is_playing() is False

    # 暂停状态下逐帧步进三次：每步只放一帧
    for expected in (1, 2, 3):
        dialog.step_once()
        assert len(played) == expected
        assert dialog.progress_text().startswith(f"{expected} / 3")

    dialog.step_once()
    assert len(played) == 3
    assert "已结束" in dialog.progress_text()


def test_replay_skips_tx_frames(tmp_path) -> None:
    rows = [
        {"t": 0, "dir": "rx", "type": 1, "len": 0, "payload": ""},
        {"t": 20, "dir": "tx", "type": 5, "len": 0, "payload": ""},
        {"t": 40, "dir": "rx", "type": 1, "len": 0, "payload": ""},
    ]
    path = tmp_path / "mixed.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    dialog = _dialog()
    assert dialog.load_file(str(path))
    # 只回放 rx：tx 是当时自己发的命令，重放会变成"自己跟自己对话"
    assert dialog.cursor().total == 2


def test_entries_helper_direction_labels() -> None:
    """方向标签集中在 frame_trace，界面与 JSONL 共用一份口径。"""
    from serialpid.frame_trace import DIR_LABELS

    assert DIR_LABELS[DIR_RX] == "接收"
    assert DIR_LABELS[DIR_TX] == "发送"
    assert TraceEntry(0, DIR_RX, PacketType.PONG, b"").name == "PONG"
