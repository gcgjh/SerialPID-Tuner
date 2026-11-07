"""卡22：启动自检 —— 三态结果、诊断文本、命令行 JSON、失败项如实上报。

要点：自检必须**只报告不修复**，所以用例全部断言"状态与原因"，不去断言程序
替用户做了补救；无控制台（windowed）场景用 ``sys.stdout = None`` 模拟。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from serialpid import selftest as st  # noqa: E402

REQUIRED_KEYS = {
    "python_runtime",
    "pyserial",
    "serial_backend",
    "qt_plugins",
    "config_dir",
    "log_dir",
    "icon_asset",
    "protocol_io",
}


def _run(tmp_path, **kwargs):
    return st.run_selftest(
        config_path=str(tmp_path / "config.json"),
        log_dir=str(tmp_path / "logs"),
        **kwargs,
    )


def test_selftest_covers_required_checks_and_has_no_failures(tmp_path):
    report = _run(tmp_path)
    keys = {check.key for check in report.checks}
    assert keys == REQUIRED_KEYS, f"检查项缺失或多余：{REQUIRED_KEYS ^ keys}"
    assert report.status != st.STATUS_FAIL, (
        "正常环境下不应有失败项：" + "; ".join(f"{c.key}={c.status}({c.detail})" for c in report.failed)
    )
    assert report.counts[st.STATUS_PASS] >= 6
    # 自检不能成为启动负担
    assert report.duration_ms < 1000, f"自检耗时 {report.duration_ms:.0f} ms 超过一秒"


def test_report_is_logged_and_kept_in_memory(tmp_path):
    report = _run(tmp_path)
    assert report.log_path and os.path.isfile(report.log_path), "结果必须落盘"
    payload = json.loads(open(report.log_path, encoding="utf-8").read())
    assert payload["checks"] and payload["status"] == report.status
    assert payload["duration_ms"] >= 0

    text_log = os.path.join(str(tmp_path / "logs"), st._TEXT_LOG)
    assert os.path.isfile(text_log), "还应追加一段可粘贴的人类可读文本"
    content = open(text_log, encoding="utf-8").read()
    assert "[OK]" in content and "小计：" in content

    assert st.latest_report() is report, "界面要通过 latest_report() 读到这次结果"


def test_config_dir_not_writable_is_reported_as_blocking_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_probe_writable", lambda directory: (False, "PermissionError: 模拟只读"))
    report = _run(tmp_path)
    check = report.get("config_dir")
    assert check is not None
    assert check.status == st.STATUS_FAIL
    assert "不可写" in check.detail and "PermissionError" in check.detail
    assert check.hint, "失败项必须给出可操作建议"
    assert report.blocking, "配置目录不可写要标成阻断项，其余失败才允许降级运行"


def test_missing_icon_reports_resource_missing(tmp_path):
    report = _run(tmp_path, icon_path=str(tmp_path / "no-such-icon.ico"))
    check = report.get("icon_asset")
    assert check.status == st.STATUS_FAIL
    assert "缺失" in check.detail
    assert report.ok is False


def test_missing_pyserial_is_reported_not_swallowed(tmp_path, monkeypatch):
    # 同进程里别的用例可能已经 import 过 serial 的子模块，逐个清掉才等价于"依赖缺失"
    monkeypatch.setitem(sys.modules, "serial", None)
    monkeypatch.delitem(sys.modules, "serial.serialwin32", raising=False)
    monkeypatch.delitem(sys.modules, "serial.serialposix", raising=False)
    monkeypatch.delitem(sys.modules, "serial.tools.list_ports", raising=False)
    report = _run(tmp_path)
    pyserial = report.get("pyserial")
    backend = report.get("serial_backend")
    assert pyserial.status == st.STATUS_FAIL, "pyserial 缺失必须报失败，不能静默成空列表"
    assert "pyserial 导入失败" in pyserial.detail
    assert backend.status == st.STATUS_FAIL
    assert report.status == st.STATUS_FAIL


def test_protocol_check_roundtrip_and_crc_rejection():
    status, detail, _ = st._check_protocol()
    assert status == st.STATUS_PASS
    assert "坏 CRC 帧被拒绝" in detail


def test_diagnostic_text_is_pasteable(tmp_path):
    report = _run(tmp_path)
    text = st.format_report_text(report)
    for key in REQUIRED_KEYS:
        assert key in text, f"诊断文本缺少 {key} 这一项"
    assert "Python" in text and "小计：" in text


def test_cli_json_outputs_file_and_exit_code(tmp_path):
    out = tmp_path / "result.json"
    code = st.main([
        "--json", "--out", str(out), "--no-log",
        "--config", str(tmp_path / "config.json"), "--log-dir", str(tmp_path / "logs"),
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] in (st.STATUS_PASS, st.STATUS_WARN)
    assert [c["key"] for c in payload["checks"]]


def test_cli_exit_code_is_nonzero_when_check_fails(tmp_path):
    out = tmp_path / "result.json"
    code = st.main([
        "--json", "--out", str(out), "--no-log",
        "--config", str(tmp_path / "config.json"), "--log-dir", str(tmp_path / "logs"),
        "--icon", str(tmp_path / "broken.ico"),
    ])
    assert code == 1, "有失败项时退出码必须非零，否则打包脚本会以为包没问题"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == st.STATUS_FAIL
    assert payload["ok"] is False


def test_cli_rejects_unknown_argument(tmp_path):
    assert st.main(["--not-a-flag"]) == 2


def test_windowed_build_without_stdout_still_writes_result(tmp_path, monkeypatch):
    """打包版是无控制台窗口的：sys.stdout 为 None，结果必须能靠 --out 落盘。"""
    out = tmp_path / "exe-result.json"
    monkeypatch.setattr(sys, "stdout", None)
    code = st.main([
        "--json", "--out", str(out), "--no-log",
        "--config", str(tmp_path / "config.json"), "--log-dir", str(tmp_path / "logs"),
    ])
    assert code == 0
    assert out.is_file() and json.loads(out.read_text(encoding="utf-8"))["checks"]


def test_qt_plugin_missing_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        st, "_check_qt_plugins", lambda: (st.STATUS_FAIL, "平台插件缺失：qwindows.dll", "重装 PySide6")
    )
    report = _run(tmp_path)
    check = report.get("qt_plugins")
    assert check.status == st.STATUS_FAIL and "缺失" in check.detail
