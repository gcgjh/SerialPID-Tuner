# -*- coding: utf-8 -*-
"""card21 串口探测回归：结构化信息 + 三态（有设备 / 空 / 异常）+ 超时 + 诊断文本。

覆盖验收：
    * 枚举到的设备带描述、硬件 ID、厂商、序列号，且 COM2 排在 COM10 前；
    * 无设备时返回 empty + 明确提示，绝不出现假 COM1；
    * 模拟异常返回 error 详情（类型 + 消息），而不是空列表；
    * 枚举卡住时超时上报，不把调用方拖死；
    * pyserial 缺失按 ImportError 上报；
    * 同一次进程内重复扫描能反映设备增减（热插拔）；
    * 诊断文本含 pyserial 版本、平台、耗时、设备明细；
    * 模块不依赖 Qt，可纯 Python 导入。

运行：cd pc && python -m pytest tests/test_port_scan.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid import port_scan  # noqa: E402


def _fake_port(device: str, **kwargs) -> SimpleNamespace:
    base = dict(
        device=device,
        description="USB-SERIAL CH340",
        hwid="USB VID:PID=1A86:8011 SER=",
        manufacturer="wch.cn",
        serial_number="A1B2C3",
        vid=0x1A86,
        pid=0x8011,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_found_devices_are_structured_and_sorted(monkeypatch):
    monkeypatch.setattr(
        port_scan,
        "_enumerate_serial_ports",
        lambda: [_fake_port("COM10"), _fake_port("COM2", description="CH340")],
    )
    result = port_scan.scan_ports()

    assert result.status == port_scan.STATUS_OK
    assert [p.device for p in result.ports] == ["COM2", "COM10"], "COM2 应排在 COM10 前"
    first = result.ports[0]
    assert first.description == "CH340"
    assert first.hwid.startswith("USB VID:PID=1A86")
    assert first.manufacturer == "wch.cn"
    assert first.serial_number == "A1B2C3"
    assert first.vid_pid == "1A86:8011"
    assert first.display_name == "COM2 · CH340 · wch.cn"
    assert result.summary.startswith("发现 2 个")


def test_empty_is_a_distinct_state_without_fake_com1(monkeypatch):
    monkeypatch.setattr(port_scan, "_enumerate_serial_ports", lambda: [])
    result = port_scan.scan_ports()

    assert result.status == port_scan.STATUS_EMPTY
    assert result.ports == []
    assert not result.ok and not result.is_error
    assert result.summary == "未发现串口设备"
    assert "COM1" not in repr(result)
    assert "内置模拟器" in result.hint


def test_enumerate_exception_returns_details_not_empty_list(monkeypatch):
    def _boom():
        raise OSError(13, "拒绝访问：串口驱动异常")

    monkeypatch.setattr(port_scan, "_enumerate_serial_ports", _boom)
    result = port_scan.scan_ports()

    assert result.status == port_scan.STATUS_ERROR
    assert result.ports == []
    # OSError(13) 在 Python 里会被规范成 PermissionError，类型必须原样带回去
    assert result.error_type == "PermissionError"
    assert "拒绝访问" in result.error_message
    assert "PermissionError" in result.summary
    text = port_scan.diagnose(result)
    assert "异常类型：PermissionError" in text
    assert "拒绝访问" in text


def test_slow_enumeration_times_out_instead_of_blocking(monkeypatch):
    def _slow():
        time.sleep(0.6)
        return []

    monkeypatch.setattr(port_scan, "_enumerate_serial_ports", _slow)
    t0 = time.monotonic()
    result = port_scan.scan_ports(timeout_s=0.05)
    spent = time.monotonic() - t0

    assert result.status == port_scan.STATUS_ERROR
    assert result.error_type == "TimeoutError"
    assert "0.05" in result.error_message
    assert spent < 0.5, f"超时保护没生效，阻塞了 {spent:.2f}s"
    assert result.elapsed_ms < 500


def test_missing_pyserial_is_reported_as_import_error(monkeypatch):
    monkeypatch.setattr(port_scan, "_list_ports", None)
    monkeypatch.setattr(port_scan, "serial", None)

    result = port_scan.scan_ports()

    assert result.status == port_scan.STATUS_ERROR
    assert result.error_type == "ImportError"
    assert "pyserial" in result.error_message
    assert result.pyserial_version == "未安装"
    assert result.backend_available is False


def test_messy_fields_do_not_crash_and_duplicates_are_dropped(monkeypatch):
    monkeypatch.setattr(
        port_scan,
        "_enumerate_serial_ports",
        lambda: [
            _fake_port("COM7", description=None, manufacturer="n/a", serial_number=None, vid=None, pid=None),
            _fake_port("COM7"),
            SimpleNamespace(device=""),
        ],
    )
    result = port_scan.scan_ports()

    assert result.status == port_scan.STATUS_OK
    assert len(result.ports) == 1
    port = result.ports[0]
    assert port.description == "" and port.manufacturer == ""
    assert port.vid_pid == ""
    assert port.display_name == "COM7"


def test_rescan_reflects_hotplug(monkeypatch):
    devices: list[SimpleNamespace] = []

    def _enumerate():
        return list(devices)

    monkeypatch.setattr(port_scan, "_enumerate_serial_ports", _enumerate)

    first = port_scan.scan_ports()
    assert first.status == port_scan.STATUS_EMPTY

    devices.append(_fake_port("COM3"))
    second = port_scan.scan_ports()
    assert second.status == port_scan.STATUS_OK
    assert [p.device for p in second.ports] == ["COM3"]

    devices.clear()
    third = port_scan.scan_ports()
    assert third.status == port_scan.STATUS_EMPTY


def test_diagnose_text_has_everything_needed_for_a_bug_report(monkeypatch, tmp_path):
    monkeypatch.setattr(port_scan, "_enumerate_serial_ports", lambda: [_fake_port("COM3")])
    result = port_scan.scan_ports()
    text = port_scan.diagnose(result)

    for token in ("pyserial", "平台", "枚举耗时", "枚举状态", "设备明细", "COM3", "CH340"):
        assert token in text, f"诊断文本缺少 {token}"
    assert "SerialPID-Tuner 串口诊断" in text
    # 不泄露用户目录（临时目录路径不应出现在诊断里）
    assert str(tmp_path) not in text


def test_real_scan_smoke_returns_structured_result():
    result = port_scan.scan_ports(timeout_s=1.0)
    assert result.status in {port_scan.STATUS_OK, port_scan.STATUS_EMPTY, port_scan.STATUS_ERROR}
    assert result.elapsed_ms >= 0
    if result.ok:
        assert all(isinstance(p, port_scan.PortInfo) for p in result.ports)
        assert all(p.device for p in result.ports)


def test_module_imports_without_qt():
    pc_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    code = (
        "import sys;"
        f"sys.path.insert(0, {pc_dir!r});"
        "import serialpid.port_scan;"
        "print('QT' if 'PySide6' in sys.modules else 'NOQT')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "NOQT", "port_scan 不应把 Qt 拖进无 GUI 环境"
