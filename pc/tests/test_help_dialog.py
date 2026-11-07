# -*- coding: utf-8 -*-
"""卡30 内置帮助：章节抽取、与手册同源、F1 入口、截图缩放。

帮助不另写文案——它必须从 ``docs/user_guide.md`` 抽三节。这里既测纯字符串
抽取，也测对话框真的拿到了手册里的内容（而不是兜底文案）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QUrl  # noqa: E402
    from PySide6.QtGui import QTextDocument  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover - 依赖环境
    _HAS_QT = False

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")

from serialpid.gui import help_dialog as hd  # noqa: E402


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------- 纯字符串


def test_extract_sections_matches_numbered_titles_and_stops_at_same_level():
    markdown = "\n".join(
        [
            "# 手册",
            "",
            "## 1. 五分钟上手",
            "",
            "三步走。",
            "",
            "### 小标题",
            "",
            "细节。",
            "",
            "## 2. 界面速查",
            "",
            "| 区域 | 说明 |",
            "",
            "## 3. 常见问题",
            "",
            "先看日志。",
        ]
    )
    picked = hd.extract_sections(markdown)
    assert set(picked) == set(hd.HELP_SECTIONS)
    assert picked["五分钟上手"][0] == "1. 五分钟上手"
    assert "### 小标题" in picked["五分钟上手"][1]      # 子标题跟着父章节
    assert picked["界面速查"][1].strip() == "| 区域 | 说明 |"
    assert picked["常见问题"][1].strip() == "先看日志。"


def test_build_help_markdown_orders_sections_and_strips_numbering():
    markdown = "\n".join(
        [
            "## 3. 常见问题",
            "答案。",
            "## 1. 五分钟上手",
            "步骤。",
            "## 2. 界面速查",
            "表格。",
        ]
    )
    built = hd.build_help_markdown(markdown)
    assert built.startswith("# SerialPID-Tuner 帮助")
    assert built.index("## 五分钟上手") < built.index("## 界面速查")
    assert built.index("## 界面速查") < built.index("## 常见问题")
    assert "3. 常见问题" not in built


def test_build_help_markdown_returns_empty_when_nothing_matches():
    assert hd.build_help_markdown("# 别的文档\n\n没有这三节。") == ""


def test_load_help_markdown_falls_back_when_docs_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("SERIALPID_DOCS_DIR", str(tmp_path / "empty"))
    assert hd.load_help_markdown() == hd.FALLBACK_HELP
    assert "内容来源" in hd.help_source_label()


def test_load_help_markdown_uses_real_guide():
    markdown = hd.load_help_markdown()
    assert markdown != hd.FALLBACK_HELP
    assert "## 五分钟上手" in markdown
    assert "## 界面速查" in markdown
    assert "## 常见问题" in markdown
    # 同源的证据：手册里才有的话出现在帮助里
    assert "内置模拟器" in markdown


# ---------------------------------------------------------------- 对话框


def test_help_dialog_renders_markdown_and_keeps_source_label():
    _app()  # 离屏下必须在建控件之前有 QApplication
    markdown = "# 帮助\n\n## 五分钟上手\n\n1. 打开程序\n2. 选模拟器\n3. 点连接\n"
    dialog = hd.HelpDialog(markdown=markdown)
    try:
        assert dialog.markdown() == markdown
        text = dialog.browser().toPlainText()
        assert "五分钟上手" in text
        assert "点连接" in text
    finally:
        dialog.close()


def test_help_browser_scales_wide_screenshot():
    """手册截图 1440 px 宽，帮助窗口只有 900，必须缩到 820 以内。"""
    image_path = hd.docs_dir() / "images" / "01-main-window.png"
    if not image_path.is_file():
        pytest.skip("截图未生成（先跑 tools/make_screenshots.py）")
    _app()  # 先有 QApplication 再建控件：离屏下顺序错了会直接 abort
    browser = hd._ScaledBrowser()
    image = browser.loadResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl.fromLocalFile(str(image_path)),
    )
    assert image is not None
    assert image.width() <= hd._ScaledBrowser._MAX_WIDTH
    assert image.height() > 0


def test_main_window_has_help_menu_and_f1_shortcut(monkeypatch, tmp_path):
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    monkeypatch.setattr(
        st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[])
    )
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    app = _app()
    win = mw.MainWindow()
    try:
        menus = [action.text() for action in win.menuBar().actions()]
        assert "帮助" in menus
        assert win._help_shortcut.key().toString() == "F1"
        assert win.build_help_dialog().windowTitle() == "使用帮助"
        assert win.build_about_dialog().windowTitle() == "关于 SerialPID-Tuner"
    finally:
        win.close()
        app.processEvents()
