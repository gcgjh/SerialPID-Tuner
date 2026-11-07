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
