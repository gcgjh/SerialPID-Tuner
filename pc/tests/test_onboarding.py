# -*- coding: utf-8 -*-
"""卡30 首启三屏向导：只弹一次、可跳过、关掉也算看过。"""

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

from serialpid.gui.onboarding import (  # noqa: E402
    OnboardingDialog,
    PAGES,
    onboarding_done,
)


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def fresh_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SERIALPID_CONFIG", str(tmp_path / "config.json"))
    return tmp_path / "config.json"


def test_three_pages_and_first_run_state(fresh_config):
    assert len(PAGES) == 3
    assert PAGES[0][0].startswith("第一步")
    assert PAGES[-1][0].startswith("第三步")
    assert not onboarding_done()


def test_navigation_labels_and_finish_button(fresh_config):
    _app()
    dialog = OnboardingDialog()
    try:
        assert dialog.page_count() == 3
        assert dialog.current_index() == 0
        assert not dialog._back.isEnabled()
        assert dialog._next.text() == "下一步"
        dialog._go_next()
        assert dialog.current_index() == 1
        dialog._go_next()
        assert dialog.current_index() == 2
        assert dialog._next.text() == "完成"
        dialog._go_back()
        assert dialog.current_index() == 1
    finally:
        dialog.close()


def test_skip_remembers_and_does_not_pop_again(fresh_config):
    _app()
    dialog = OnboardingDialog()
    try:
        assert not onboarding_done()
        dialog._skip_all()
        assert dialog.skipped is True
        assert dialog.result() == dialog.DialogCode.Rejected
    finally:
        dialog.close()
    # 记住了：下一次装向导前的判断直接为真，不会再弹
    assert onboarding_done() is True


def test_close_also_remembers(fresh_config):
    _app()
    dialog = OnboardingDialog()
    dialog.show()
    dialog.close()
    assert onboarding_done() is True


def test_remember_is_idempotent_and_survives_reload(fresh_config):
    from serialpid.config import load_config

    _app()
    dialog = OnboardingDialog()
    try:
        assert dialog.remember() is True
        assert dialog.remember() is True
    finally:
        dialog.close()
    config, _warning = load_config()
    assert config["ui"]["onboarding_done"] is True


def test_each_page_shows_its_illustration(fresh_config):
    """三屏都配了图：图必须真的贴上（曾经漏了 docs/images/ 这一层，向导全空白）。"""
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QLabel

    from serialpid.gui.help_dialog import docs_dir

    _app()
    for _title, _body, image in PAGES:
        assert (docs_dir() / "images" / image).is_file(), f"手册截图缺失：{image}"

    dialog = OnboardingDialog()
    try:
        for index, (_title, _body, image) in enumerate(PAGES):
            dialog._stack.setCurrentIndex(index)
            _app().processEvents()
            labels = dialog._stack.currentWidget().findChildren(QLabel)
            shots = [
                label.pixmap()
                for label in labels
                if label.pixmap() is not None and not label.pixmap().isNull()
            ]
            assert shots, f"第 {index + 1} 屏没有贴上配图 {image}"
            assert isinstance(shots[0], QPixmap) and shots[0].width() <= 560
    finally:
        dialog.close()


def test_skip_is_remembered_across_restart_in_portable_mode(tmp_path, monkeypatch):
    """便携模式（SERIALPID_CONFIG 指到 U 盘路径）：跳过一次，重开进程也不再弹。

    这里用"重新读配置 + 重建对话框"模拟重启：便携模式下配置跟着走，
    用户换台机器插上 U 盘也应当保持"看过"。
    """
    portable_dir = tmp_path / "usb"
    portable_dir.mkdir()
    config_path = portable_dir / "config.json"
    monkeypatch.setenv("SERIALPID_CONFIG", str(config_path))

    _app()
    first = OnboardingDialog()
    try:
        first._skip_all()
    finally:
        first.close()
    assert config_path.is_file(), "便携模式下配置没有落到 SERIALPID_CONFIG 指定的路径"
    assert onboarding_done() is True

    # 模拟重启：新建对话框（记忆标志归零），但配置已经记过 → 判断仍为"看过"
    second = OnboardingDialog()
    try:
        assert second.remembered is False, "新实例不该带着上一次的记忆标志"
        assert onboarding_done() is True
    finally:
        second.close()


def test_maybe_show_onboarding_pops_once_then_stays_quiet(fresh_config, monkeypatch):
    """首次弹出一次；点过「跳过」之后，同一次运行里再也不弹。"""
    import serialpid.selftest as st
    import serialpid.gui.main_window as mw
    import serialpid.gui.onboarding as ob

    monkeypatch.setattr(
        st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[])
    )
    # exec() 会阻塞，这里换成"登记 + 立即返回"：测的是"弹没弹、弹了几次"
    shown = []
    monkeypatch.setattr(ob.OnboardingDialog, "exec", lambda self: shown.append(self) or 0)

    app = _app()
    win = mw.MainWindow()
    try:
        assert win.maybe_show_onboarding() is True
        assert len(shown) == 1
        shown[0]._skip_all()          # 用户点「跳过」
        assert win.maybe_show_onboarding() is False
        assert len(shown) == 1, "跳过之后又弹了一次"
    finally:
        win.close()
        app.processEvents()


def test_main_window_skips_wizard_when_already_seen(fresh_config, monkeypatch):
    from serialpid.config import load_config, save_config

    config, _warning = load_config()
    ui = config.setdefault("ui", {})
    assert isinstance(ui, dict)
    ui["onboarding_done"] = True
    save_config(config)

    import serialpid.selftest as st
    import serialpid.gui.main_window as mw

    monkeypatch.setattr(
        st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[])
    )
    app = _app()
    win = mw.MainWindow()
    try:
        assert win.maybe_show_onboarding() is False
    finally:
        win.close()
        app.processEvents()
