# -*- coding: utf-8 -*-
"""卡30 文档资产：六张标准截图一条命令重生成、文件名固定、接线图可生成。

用例真的会把六张图渲染出来（离屏），因为"脚本能跑"本身就是验收项；只检查
文件名不够——字体没加载时图里全是方块，脚本照样返回 0。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PC_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PC_DIR.parent
sys.path.insert(0, str(PC_DIR))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import pytest  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QImage  # noqa: E402

    _HAS_QT = True
except Exception:  # pragma: no cover
    _HAS_QT = False

pytestmark = pytest.mark.skipif(not _HAS_QT, reason="PySide6 不可用")

import make_screenshots as shots  # noqa: E402


def test_six_standard_filenames_are_frozen():
    assert {
        key: name for key, (name, _desc) in shots.SHOTS.items()
    } == {
        "main": "01-main-window.png",
        "sim": "02-sim-panel.png",
        "port": "03-port-diagnosis.png",
        "banner": "04-selftest-banner.png",
        "step": "05-step-metrics.png",
        "status": "06-statusbar.png",
    }


def test_docs_images_match_frozen_names():
    """仓库里的正式截图必须就是这六个名字，README / 手册引用不会悬空。"""
    images = REPO_ROOT / "docs" / "images"
    if not images.is_dir():
        pytest.skip("docs/images 尚未生成（先跑 tools/make_screenshots.py）")
    for name, _desc in shots.SHOTS.values():
        assert (images / name).is_file(), name


def test_capture_all_regenerates_six_readable_pngs(monkeypatch, tmp_path):
    import serialpid.selftest as st

    # 截图脚本会构造真窗口；把自检换成桩，别让测试去写用户日志目录
    monkeypatch.setattr(
        st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[])
    )
    paths = shots.capture_all(tmp_path, size=(1360, 900))
    assert len(paths) == 6
    assert {path.name for path in paths} == {name for name, _d in shots.SHOTS.values()}
    for path in paths:
        assert path.stat().st_size > 5_000, f"{path.name} 太小，可能是空图"
        image = QImage(str(path))
        assert not image.isNull()
        assert image.width() >= 300 and image.height() >= 20


def test_cli_only_accepts_short_name_or_file_name(tmp_path):
    target = tmp_path / "one"
    exit_code = shots.main(
        ["--out", str(target), "--size", "1280x800", "--only", "03-port-diagnosis.png"]
    )
    assert exit_code == 0
    assert (target / "03-port-diagnosis.png").is_file()


def test_wiring_diagram_renders(tmp_path, monkeypatch):
    import make_wiring_diagram as wiring

    monkeypatch.setattr(
        sys, "argv", ["make_wiring_diagram.py"]
    )
    path = wiring.render(tmp_path / "wiring.png")
    assert path.is_file() and path.stat().st_size > 10_000
    image = QImage(str(path))
    assert not image.isNull()
    assert image.width() == wiring.W and image.height() == wiring.H
