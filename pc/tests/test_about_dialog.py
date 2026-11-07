# -*- coding: utf-8 -*-
"""卡30 关于框：版本单一来源、依赖清单、许可证明细、复制诊断。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")

from serialpid.gui import about_dialog as ad  # noqa: E402


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_program_version_single_source():
    import serialpid

    assert ad.program_version() == serialpid.__version__
    assert serialpid.__version__  # 空串会让 README 与 exe 属性同时失准


def test_environment_rows_cover_program_python_dependencies_and_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    rows = dict(ad.environment_rows())
    names = [name for name, _value in ad.environment_rows()]
    assert names[:3] == ["程序版本", "Python", "操作系统"]
    for dep in ("PySide6", "pyqtgraph", "pyserial", "numpy"):
        assert dep in rows
        assert rows[dep].strip()  # 已安装给版本号，未安装给"未安装"
    assert "config.json" in rows["配置文件"] or tmp_path.name in rows["配置文件"]


def test_dependency_version_reports_missing_package_honestly():
    assert ad.dependency_version("definitely-not-installed-pkg-xyz") == "未安装"
    assert ad.dependency_version("PySide6")


def test_dependency_version_survives_distribution_name_mismatch():
    """PySide6 实际装成 PySide6_Essentials：能跑就必须报出版本，不能写"未安装"。"""
    version = ad.dependency_version("PySide6")
    assert version != "未安装", "Qt 明明能导入，关于框却说未安装"
    assert version[0].isdigit() or version.startswith("已安装"), version


def test_diagnostic_text_is_paste_ready():
    text = ad.diagnostic_text()
    assert text.startswith("SerialPID-Tuner 诊断信息")
    assert text.endswith("许可证：MIT")
    lines = text.splitlines()
    assert any(line.startswith("程序版本：") for line in lines)
    assert any(line.startswith("PySide6：") for line in lines)
    # 每行都是"名：值"，用户复制出去不用再解释
    for line in lines[1:]:
        assert "：" in line


def test_about_dialog_lists_values_and_copies_to_clipboard():
    app = _app()
    dialog = ad.AboutDialog()
    try:
        values = dialog.values()
        assert values["程序版本"] == ad.program_version()
        assert "PySide6" in values
        copied = dialog.copy_diagnostics()
        assert copied == ad.diagnostic_text()
        assert QApplication.clipboard().text() == copied
        assert dialog._copied.text() == "已复制到剪贴板"
        app.processEvents()
    finally:
        dialog.close()


def test_diagnostic_text_includes_serial_backend_status():
    """卡35：诊断信息必须含串口后端状态，否则"打不开串口"类报障无从分诊。"""
    from serialpid import selftest

    rows = dict(ad.environment_rows())
    assert "串口后端" in rows
    assert rows["串口后端"].strip()
    # 与启动自检同源：不允许关于框自己另写一套判定，否则两处会互相打脸
    assert rows["串口后端"] == selftest.serial_backend_summary()
    assert any(
        line.startswith("串口后端：") for line in ad.diagnostic_text().splitlines()
    )


def test_serial_backend_summary_names_module_when_backend_missing(monkeypatch):
    """后端模块导入失败时写出模块名与异常类型，而不是一句"未知错误"。"""
    import serialpid.selftest as st

    monkeypatch.setattr(st, "_serial_backend_name", lambda: "serial.no_such_backend")
    summary = st.serial_backend_summary()
    assert "serial.no_such_backend" in summary
    assert "不可用" in summary


def test_about_dialog_shows_serial_backend_row():
    app = _app()
    dialog = ad.AboutDialog()
    try:
        values = dialog.values()
        assert "串口后端" in values
        assert values["串口后端"] == ad.serial_backend_status()
        app.processEvents()
    finally:
        dialog.close()
