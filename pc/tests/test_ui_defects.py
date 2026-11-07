# -*- coding: utf-8 -*-
"""card3 三个界面缺陷的回归测试。

覆盖验收：
    * 长度 0/1/3/4/15/16/18 的畸形帧：不抛异常、日志有记录、计数累计；
    * ERROR 帧“声明消息长度 vs 载荷实际长度”不匹配按截断处理；
    * 参数范围配置文件：改增益上限重启后生效；坏文件回退默认；
    * 1000 个未知类型帧：日志新增 < 15 行；
    * 现有界面冒烟（离屏 Qt）不回归。

运行：cd pc && python -m pytest tests/test_ui_defects.py -q
"""

from __future__ import annotations

import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid.config import (  # noqa: E402
    DEFAULT_PARAM_LIMITS,
    default_config_path,
    load_config,
    save_config,
)
from serialpid.frame_log import ParseErrorLogger, UnknownFrameLogger  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    PAYLOAD_FMT,
    PacketType,
    EMPTY_PAYLOAD_TYPES,
    expected_payload_len,
    pack_error,
    pack_pid_ack,
    pack_pong,
    pack_step_event,
    pack_telemetry,
    payload_error_reason,
    unpack_telemetry,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover - 依赖环境
    _HAS_QT = False


# ---------------------------------------------------------------------------
# 缺陷一：长度预检表（与 PROTOCOL.md §4 / PAYLOAD_FMT 同源）
# ---------------------------------------------------------------------------


def test_precheck_table_matches_protocol_formats():
    """预检表必须与 PAYLOAD_FMT（§4 载荷定义）逐字节一致。"""
    for pkt_type, fmt in PAYLOAD_FMT.items():
        assert expected_payload_len(pkt_type) == struct.calcsize(fmt)
    for pkt_type in EMPTY_PAYLOAD_TYPES:
        assert expected_payload_len(pkt_type) == 0
    assert expected_payload_len(PacketType.ERROR) is None
    assert expected_payload_len(0x7F) is None  # 未知类型不预检


@pytest.mark.parametrize("pkt_type", [PacketType.TELEMETRY, PacketType.PID_ACK,
                                      PacketType.PONG, PacketType.STEP_EVENT])
@pytest.mark.parametrize("length", [0, 1, 3, 4, 15, 16, 18])
def test_malformed_lengths_rejected(pkt_type, length):
    """卡片验收：长度 0/1/3/4/15/16/18 的畸形帧全部被预检拦下。"""
    assert payload_error_reason(pkt_type, bytes(length)) is not None


def test_valid_payloads_pass_precheck():
    assert payload_error_reason(PacketType.TELEMETRY, pack_telemetry(1, 2, 3, 4, 5)) is None
    assert payload_error_reason(PacketType.PID_ACK, pack_pid_ack(2.0, 0.5, 0.1, 30.0, 0)) is None
    assert payload_error_reason(PacketType.PONG, pack_pong(0x13, 1)) is None
    assert payload_error_reason(PacketType.STEP_EVENT, pack_step_event(1, 500)) is None
    for t in EMPTY_PAYLOAD_TYPES:
        assert payload_error_reason(t, b"") is None
    assert payload_error_reason(PacketType.ERROR, pack_error(7, "ok")) is None


def test_error_declared_len_mismatch_treated_as_truncated():
    # 声明 5 字节实际 2 字节：按截断处理，不越界读
    reason = payload_error_reason(PacketType.ERROR, b"\x01\x05ab")
    assert reason is not None and "截断" in reason
    # 只有 1 字节（连 msg_len 都不完整）
    assert payload_error_reason(PacketType.ERROR, b"\x01") is not None
    # 声明 0 字节消息是合法的
    assert payload_error_reason(PacketType.ERROR, b"\x01\x00") is None


def test_malformed_frames_do_not_raise_and_are_logged():
    """模拟主窗口槽函数：28 个畸形帧全部不抛异常、日志有记录、计数累计。"""

    class FakeSlot:
        def __init__(self):
            self.errors = 0
            self.lines = []

        def on_frame(self, pkt_type, payload):
            try:
                reason = payload_error_reason(pkt_type, payload)
                if reason is not None:
                    self.errors += 1
                    self.lines.append(reason)
                    return
                if pkt_type == PacketType.TELEMETRY:
                    unpack_telemetry(payload)
            except Exception as exc:
                self.errors += 1
                self.lines.append(f"{type(exc).__name__}: {exc}")

    slot = FakeSlot()
    for pkt_type in (PacketType.TELEMETRY, PacketType.PID_ACK,
                     PacketType.PONG, PacketType.STEP_EVENT):
        for length in (0, 1, 3, 4, 15, 16, 18):
            slot.on_frame(pkt_type, bytes(length))
    assert slot.errors == 28
    assert len(slot.lines) == 28  # 每条畸形帧都有日志记录


# ---------------------------------------------------------------------------
# 缺陷二：参数范围配置文件
# ---------------------------------------------------------------------------


def test_missing_config_creates_defaults(tmp_path):
    path = str(tmp_path / "config.json")
    config, warning = load_config(path)
    assert warning is None
    assert config["schema"] == "serialpid.config"
    assert config["param_limits"]["kp"]["max"] == 100.0
    assert config["param_limits"]["sp"]["min"] == -1000.0
    assert os.path.exists(path)


def test_editing_max_takes_effect_after_restart(tmp_path):
    """验收：配置里把增益上限改成 500，重启（重新加载）后生效。"""
    path = str(tmp_path / "config.json")
    config, _ = load_config(path)
    config["param_limits"]["kp"]["max"] = 500.0
    save_config(config, path)

    reloaded, warning = load_config(path)  # 模拟重启
    assert warning is None
    assert reloaded["param_limits"]["kp"]["max"] == 500.0


def test_invalid_range_falls_back_with_warning(tmp_path):
    path = str(tmp_path / "config.json")
    save_config({"schema": "serialpid.config", "version": 1,
                 "param_limits": {"kp": {"min": 20.0, "max": 10.0,
                                          "step": 0.001, "default": 15.0}}}, path)
    config, warning = load_config(path)
    assert warning is not None and "kp" in warning
    assert config["param_limits"]["kp"] == DEFAULT_PARAM_LIMITS["kp"]


def test_corrupt_config_backs_up_and_falls_back(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json!!")
    config, warning = load_config(path)
    assert warning is not None and "备份" in warning
    assert config["param_limits"]["kp"]["max"] == 100.0
    backups = list(tmp_path.glob("config.json.corrupt-*.json"))
    assert len(backups) == 1
    # strict=True：损坏文件不静默恢复，直接抛错（供调用方自定恢复策略）
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{still not json")
    with pytest.raises(ValueError):
        load_config(path, strict=True)


def test_unknown_keys_ignored(tmp_path):
    path = str(tmp_path / "config.json")
    save_config({"schema": "serialpid.config", "version": 1,
                 "param_limits": DEFAULT_PARAM_LIMITS,
                 "future_option": 123}, path)
    config, warning = load_config(path)
    assert warning is None and "future_option" not in config


def test_env_override_for_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "c.json"))
    assert default_config_path() == str(tmp_path / "c.json")


# ---------------------------------------------------------------------------
# 缺陷三：未识别帧日志聚合
# ---------------------------------------------------------------------------


def test_1000_unknown_frames_produce_less_than_15_lines():
    """验收：灌 1000 个未知类型帧，日志新增 < 15 行。"""
    clock_values = [0.0] + [i * 0.001 for i in range(1, 1000)]
    clock = iter(clock_values)
    logger = UnknownFrameLogger(clock=lambda: next(clock))
    lines = []
    for _ in range(1000):
        line = logger.note(0x7F, 3)
        if line is not None:
            lines.append(line)
    assert len(lines) < 15
    assert lines[0].startswith("未处理帧 type=0x7F len=3")  # 首次完整信息
    assert any("累计" in line for line in lines[1:])          # 之后按 100 次汇总


def test_time_based_summary_at_least_every_5s():
    clock_values = [i * 6.0 for i in range(10)]
    clock = iter(clock_values)
    logger = UnknownFrameLogger(clock=lambda: next(clock))
    lines = [line for i in range(10) if (line := logger.note(0x63, 1)) is not None]
    assert len(lines) == 10  # 首次 + 每次间隔 >= 5 秒都汇总一行


def test_types_counted_separately():
    clock_values = [i * 0.001 for i in range(300)]
    clock = iter(clock_values)
    logger = UnknownFrameLogger(clock=lambda: next(clock))
    lines = []
    for _ in range(150):
        for t in (0x7F, 0x63):
            line = logger.note(t, 2)
            if line is not None:
                lines.append(line)
    # 每个类型各：首次 1 行 + 满 100 次汇总 1 行
    firsts = sum(1 for l in lines if "len=2" in l)
    assert firsts == 2
    assert logger.stats() == {0x7F: 150, 0x63: 150}


def test_flush_and_reset():
    logger = UnknownFrameLogger(clock=lambda: 0.0)
    for _ in range(50):
        logger.note(0x55, 4)
    flushed = logger.flush()
    assert flushed is not None and "累计 50 次" in flushed
    assert logger.flush() is None  # 无新增则不再输出
    logger.reset()
    assert logger.stats() == {}


# ---------------------------------------------------------------------------
# 离屏 Qt：真实界面层的验收模拟（PySide6 缺失时跳过）
# ---------------------------------------------------------------------------


def _app():
    return QApplication.instance() or QApplication([])


@pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")
def test_param_panel_ranges_come_from_config(tmp_path):
    """界面上下限由配置生成：kp 上限改 500 后新面板即生效。"""
    from serialpid.gui.param_panel import ParamPanel

    _app()
    path = str(tmp_path / "config.json")
    config, _ = load_config(path)
    config["param_limits"]["kp"]["max"] = 500.0
    config["param_limits"]["sp"]["min"] = -5000.0
    save_config(config, path)

    panel = ParamPanel(None, config_path=path)
    assert panel._kp.maximum() == 500.0
    assert panel._sp.minimum() == -5000.0
    # 未改动的参数保持默认
    assert panel._ki.maximum() == 100.0


@pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")
def test_main_window_survives_malformed_and_unknown_storm(tmp_path, monkeypatch):
    """完整主窗口：畸形帧不冻结、状态栏计数、未知帧日志 < 15 行。"""
    from serialpid.gui.main_window import MainWindow

    _app()
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    win = MainWindow()

    # 1) 长度 0/1/3/4/15/16/18 的畸形帧：不抛异常、计数累计、日志有记录
    for pkt_type in (PacketType.TELEMETRY, PacketType.PID_ACK,
                     PacketType.PONG, PacketType.STEP_EVENT):
        for length in (0, 1, 3, 4, 15, 16, 18):
            win._on_frame(pkt_type, bytes(length))
    assert win._parse_errors == 28
    assert win._parse_stat.text() == "解析异常 28"
    text = win._log._view.toPlainText()
    # 卡8：坏帧改为按「帧类型 + 错误原因」聚合，日志里是分类首行而不再是流水账
    assert "解析异常 type=0x20" in text
    assert "载荷应为 20 字节，实际 0 字节" in text

    # 2) ERROR 截断帧也走防护：不越界、不抛异常
    win._on_frame(PacketType.ERROR, b"\x01\x05ab")
    assert win._parse_errors == 29
    assert "截断" in win._log._view.toPlainText()

    # 3) 1000 个未知类型帧：日志新增 < 15 行（注入假时钟，避免依赖墙钟）
    fake = iter([0.0] + [i * 0.001 for i in range(1, 1000)])
    win._unknown_logger = UnknownFrameLogger(clock=lambda: next(fake))
    before = win._log._view.blockCount()
    for _ in range(1000):
        win._on_frame(0x7F, b"\x01\x02\x03")
    unknown_lines = [
        l for l in win._log._view.toPlainText().splitlines()
        if "type=0x7F" in l
    ]
    assert len(unknown_lines) < 15
    assert len(unknown_lines) == 10  # 1 首行 + 9 次汇总（每满 100）
    assert win._log._view.blockCount() - before < 15

    # 4) 风暴之后正常帧照常处理（曲线不停更）
    win._on_frame(PacketType.TELEMETRY,
                  pack_telemetry(100, 25.0, 24.5, 60.0, 0.5))
    assert len(win._buffer) == 1


@pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")
def test_step_panel_samples_bounded():
    """长跑回归：阶跃分析样本超过上限后淘汰最旧点，列表不无限增长。"""
    from serialpid.gui.step_panel import StepPanel, MAX_SAMPLES
    from serialpid.model import TelemetrySample

    _app()
    panel = StepPanel(None)
    for i in range(MAX_SAMPLES + 3):
        panel.append_sample(TelemetrySample(i, 20.0, 19.5, 30.0, 0.5))
    assert len(panel._samples) == MAX_SAMPLES
    # 最旧的 3 点已被淘汰，保留的首个样本 t_ms == 3
    assert panel._samples[0].t_ms == 3
    panel.clear_samples()
    assert len(panel._samples) == 0


# ---------------------------------------------------------------------------
# 卡8：坏帧日志限流（解析异常按「帧类型 + 错误原因」聚合）
# ---------------------------------------------------------------------------


def _clock_seq(values):
    it = iter(values)
    return lambda: next(it)


def _bad_reason(pkt_type, length):
    reason = payload_error_reason(pkt_type, bytes(length))
    assert reason is not None
    return reason


def test_parse_error_logger_throttles_same_kind():
    """1000 个同类坏帧：日志新增不超过 15 行（原来是一行一条刷满 2000）。"""
    reason = _bad_reason(PacketType.TELEMETRY, 15)
    logger = ParseErrorLogger(clock=_clock_seq([i * 0.001 for i in range(1000)]))
    lines = []
    for _ in range(1000):
        line = logger.note(PacketType.TELEMETRY, reason)
        if line is not None:
            lines.append(line)
    assert len(lines) <= 15, f"同类坏帧限流失效：新增 {len(lines)} 行"
    assert logger.stats() == {(PacketType.TELEMETRY, reason): 1000}


def test_parse_error_logger_separates_type_and_reason():
    """三类坏帧各 500：分类键独立聚合，互不串味。"""
    cases = [
        (PacketType.TELEMETRY, _bad_reason(PacketType.TELEMETRY, 15)),
        (PacketType.PONG, _bad_reason(PacketType.PONG, 3)),
        (PacketType.STEP_EVENT, _bad_reason(PacketType.STEP_EVENT, 0)),
    ]
    logger = ParseErrorLogger(clock=_clock_seq([i * 0.001 for i in range(1500)]))
    for _ in range(500):
        for pkt_type, reason in cases:
            logger.note(pkt_type, reason)
    stats = logger.stats()
    assert len(stats) == 3, f"分类键未独立：{stats}"
    assert all(total == 500 for total in stats.values()), stats


def test_parse_error_logger_reemits_first_line_after_quiet():
    """静默 30 秒后同类坏帧再出现：重新输出首行，并带上此前累计。"""
    reason = _bad_reason(PacketType.TELEMETRY, 15)
    logger = ParseErrorLogger(
        clock=_clock_seq([i * 0.001 for i in range(10)] + [45.0])
    )
    lines = [logger.note(PacketType.TELEMETRY, reason) for _ in range(10)]
    assert lines[0] is not None
    assert all(line is None for line in lines[1:]), lines
    again = logger.note(PacketType.TELEMETRY, reason)
    assert again is not None
    assert again.startswith("解析异常 type=0x20")
    assert "此前累计 10 次" in again


@pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")
def test_bad_frame_storm_keeps_log_bounded(tmp_path, monkeypatch):
    """主窗口端到端：1000 个坏帧日志新增 ≤15 行，2000 行上限仍在。"""
    from serialpid.gui.main_window import MainWindow

    _app()
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    win = MainWindow()

    before = win._log._view.blockCount()
    for _ in range(1000):
        win._on_frame(PacketType.TELEMETRY, bytes(15))
    added = win._log._view.blockCount() - before
    assert added <= 15, f"1000 个坏帧新增 {added} 行日志"
    # 状态栏计数照常逐条累加（职责与日志聚合分开）
    assert win._parse_errors == 1000
    assert win._parse_stat.text() == "解析异常 1000"

    # 上限保留：超出后丢弃最旧行，不阻塞写入
    for i in range(3000):
        win._log.log(f"填充 {i}")
    assert win._log._view.blockCount() <= 2001
    text = win._log._view.toPlainText()
    assert "填充 2999" in text   # 最新行写入成功
    assert "填充 0" not in text  # 最旧行已被丢弃


@pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")
def test_clear_log_resets_aggregator_but_keeps_status_counter(tmp_path, monkeypatch):
    """清空日志：聚合状态归零，状态栏累计计数保留。"""
    from serialpid.gui.main_window import MainWindow

    _app()
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    win = MainWindow()

    for _ in range(500):
        win._on_frame(PacketType.TELEMETRY, bytes(15))
    for _ in range(200):
        win._on_frame(0x7F, b"\x01")
    assert win._parse_errors == 500
    assert win._parse_logger.stats()
    assert win._unknown_logger.stats()

    win._log.clear_log_signal().click()
    assert win._log._view.toPlainText() == ""
    assert win._parse_logger.stats() == {}
    assert win._unknown_logger.stats() == {}
    # 状态栏是「本机开机以来发生过多少次」的语义，不随清空日志归零
    assert win._parse_errors == 500
    assert win._parse_stat.text() == "解析异常 500"
