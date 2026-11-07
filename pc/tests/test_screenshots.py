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


# ---------------------------------------------------------------- 卡35：幂等与中文


def _pixel_diff_ratio(first: Path, second: Path) -> float:
    """两张图的像素差异比例（0 = 逐像素一致）。

    截图里带时钟与链路统计，跨秒重跑不可能逐像素相同；用比例判定"是否只是
    动态文字在变"，比"完全一致"更贴近事实，也能抓住"整屏错位 / 字体变了"。
    """
    import numpy as np

    a, b = QImage(str(first)), QImage(str(second))
    assert a.size() == b.size(), "重跑后尺寸变了"
    rows, stride = a.height(), a.bytesPerLine()
    buf_a = np.frombuffer(a.constBits(), dtype=np.uint8, count=rows * stride)
    buf_b = np.frombuffer(b.constBits(), dtype=np.uint8, count=rows * stride)
    return float((buf_a != buf_b).mean())


def test_capture_all_is_idempotent_on_rerun(monkeypatch, tmp_path):
    """同参数重跑：同名覆盖、不多出文件、画面不漂移（卡35 验收项）。"""
    import serialpid.selftest as st

    monkeypatch.setattr(
        st, "run_selftest", lambda **_kwargs: st.SelfTestReport(checks=[])
    )
    out = tmp_path / "images"
    first_paths = shots.capture_all(out, size=(1360, 900))
    second_paths = shots.capture_all(out, size=(1360, 900))
    assert [p.name for p in second_paths] == [p.name for p in first_paths]
    # 只覆盖同名文件：目录里不多出 png（没有 -1/-2 副本，也没有旧尺寸残留）
    pngs = sorted(p.name for p in out.glob("*.png"))
    assert pngs == sorted(name for name, _d in shots.SHOTS.values())
    for path in second_paths:
        assert path.stat().st_size > 5_000

    # 复刻一次"重跑前后"的对比：把终态再存一份，两两比像素。
    # 实测漂移只有 0.00%~0.02%（日志时钟的秒数在变），阈值取 0.2%：
    # 再宽就抓不住"字体回退成方块 / 整屏错位"这类真问题。
    shadow = tmp_path / "shadow"
    restored = shots.capture_all(shadow, size=(1360, 900))
    for before, after in zip(second_paths, restored):
        ratio = _pixel_diff_ratio(before, after)
        assert ratio < 0.002, f"{before.name} 重跑漂移 {ratio:.3%}（只该有日志时钟在变）"


def test_cjk_glyphs_render_as_real_glyphs_not_tofu():
    """离屏平台不加载系统字体会把中文画成方块：两个不同汉字必须画出不同墨迹。

    只看"文件非空"抓不到这种问题——方块截图照样有几千字节。
    """
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    app = shots.make_app()
    families = shots.load_cjk_fonts()
    assert families, "没有加载到任何中文字体，截图会全是方块（豆腐块）"

    def render(char: str) -> bytes:
        image = QImage(64, 64, QImage.Format.Format_ARGB32)
        image.fill(QColor("#000000"))
        painter = QPainter(image)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont(app.font().family(), 32))
        painter.drawText(image.rect(), 0x0084, char)  # AlignCenter
        painter.end()
        return bytes(image.constBits())

    first, second = render("连"), render("口")
    assert any(first), "汉字没有画出任何墨迹"
    assert first != second, "两个字画出来一模一样 —— 说明用的是同一个方块占位字形"
