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
