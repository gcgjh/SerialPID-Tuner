"""版本号单一来源的守护测试（card26）。

这里的每一条都对应一种"版本号漂移"：包内版本改了、版本资源没改，README 写着 v0.1、
exe 属性里却是 1.0.0.0，用户报 bug 时报的版本号和源码对不上。守护的方式是把所有
派生位置都拉进来比对，而不是靠发布前肉眼检查。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "pc"))

import serialpid  # noqa: E402


def _load_version_tool() -> ModuleType:
    """按路径加载 tools/make_version_info.py（tests 里不依赖 tools 是包）。"""
    path = REPO_ROOT / "tools" / "make_version_info.py"
    spec = importlib.util.spec_from_file_location("serialpid_make_version_info", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _load_version_tool()


def _read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_version_is_three_part_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", serialpid.__version__), serialpid.__version__


def test_version_info_file_matches_package_version() -> None:
    fields = TOOL.parse_version_info(_read("scripts/version_info.txt"))
    expected = TOOL.dotted_quad(serialpid.__version__)
    assert fields.get("filevers") == expected
    assert fields.get("FileVersion") == expected
    assert fields.get("ProductVersion") == expected


def test_version_info_is_generated_not_hand_edited() -> None:
    """磁盘内容必须与生成器渲染结果逐字一致，否则说明有人手改了常量。"""
    current = _read("scripts/version_info.txt").replace("\r\n", "\n").strip()
    rendered = TOOL.render(serialpid.__version__).replace("\r\n", "\n").strip()
    assert current == rendered


def test_changelog_highest_version_matches_package_version() -> None:
    versions = re.findall(r"^##\s*\[(\d+\.\d+\.\d+)\]", _read("docs/CHANGELOG.md"), re.MULTILINE)
    assert versions, "CHANGELOG 里没有 X.Y.Z 形式的版本标题"
    assert versions[0] == serialpid.__version__


def test_readme_current_version_matches_package_version() -> None:
    match = re.search(r"当前版本：\*\*([0-9]+\.[0-9]+\.[0-9]+)\*\*", _read("README.md"))
    assert match, "README 缺少 `当前版本：**X.Y.Z**` 这一行"
    assert match.group(1) == serialpid.__version__
