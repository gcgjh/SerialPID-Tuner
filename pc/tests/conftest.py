"""长稳压测用例的门控（card10）。

默认跳过带 ``slow`` 标记的用例，避免秒级日常回归被拖成分钟级；
显式开启：

    python -m pytest tests/test_long_run.py --long-run -v

``slow`` 标记在此登记，避免 pytest 报 "unknown marker" 警告。
"""

from __future__ import annotations

import pytest


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
