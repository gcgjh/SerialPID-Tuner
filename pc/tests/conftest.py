"""长稳压测用例的门控（card10）+ 全进程唯一的 QApplication。

默认跳过带 ``slow`` 标记的用例，避免秒级日常回归被拖成分钟级；
显式开启：

    python -m pytest tests/test_long_run.py --long-run -v

``slow`` 标记在此登记，避免 pytest 报 "unknown marker" 警告。
"""

from __future__ import annotations

import os

import pytest

# 本机直接跑 GUI 用例时给一个不必开窗的平台；CI 里已经显式设了 xcb / offscreen，
# setdefault 不会覆盖它。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def qt_application():
    """会话级 QApplication：整个 pytest 进程共用一个，持有到会话结束。

    2026-11-07 的 ubuntu CI 里，pytest 全量（529 passed）跑完后解释器收尾时报
    ``QObject: shared QObject was deleted directly``，随后 Segfault / Bus error
    （exit 139 / 135），只在 Linux runner 复现，macOS 与 Windows 都没有。

    原因是各测试文件自己 ``QApplication([])``：没有全局引用时该 app 只是本文件
    的局部变量，用例结束就被回收，下一个文件再建一个。"建了又删"多轮之后 Qt 的
    全局对象（字体库、样式、事件分发）状态被揉碎，退出时才炸。这里会话级建一次
    并一直持有，测试文件里的 ``QApplication.instance()`` 自然拿到同一个。
    """
    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # pragma: no cover - 没装 PySide6 时 GUI 用例自己 skip
        yield None
        return

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--long-run",
        action="store_true",
        default=False,
        help="运行长稳压测用例（默认跳过；完整跑一次约 10-30 秒起）",
    )


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "slow: 长稳压测用例，需 --long-run 显式开启")


def pytest_collection_modifyitems(config, items) -> None:
    if config.getoption("--long-run"):
        return
    skip = pytest.mark.skip(reason="长稳用例默认跳过，加 --long-run 开启")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
