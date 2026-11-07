# -*- coding: utf-8 -*-
"""生成文档用的标准界面截图（卡30）。

一条命令重跑，输出到 ``docs/images/``，文件名固定，因此每次发版重跑一次，
README 与用户手册里的图永远是最新的：

    python tools/make_screenshots.py
    python tools/make_screenshots.py --out docs/images --size 1440x900
    python tools/make_screenshots.py --only main,simbanner

为什么必须离屏渲染：

* 截图脚本要能进 CI、能在没有显示器的机器上跑，``QT_QPA_PLATFORM=offscreen``
  是可重复的前提；
* **离屏平台不加载系统字体库**（``QFontDatabase.families()`` 为空），中文会
  渲染成方块。所以本脚本显式 ``addApplicationFont`` 加载系统中文字体后再建
  界面。这一点在真实平台上看不出差别，但漏了就会得到一整套"豆腐块"截图。

曲线数据是脚本合成的示意数据（不是实测日志），只用于说明界面长什么样。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Tuple

# 必须在导入 PySide6 之前决定平台插件：offscreen 不依赖桌面与显示器
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
PC_DIR = REPO_ROOT / "pc"
if str(PC_DIR) not in sys.path:
    sys.path.insert(0, str(PC_DIR))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from serialpid.protocol import PacketType, pack_telemetry  # noqa: E402
from serialpid.selftest import CheckResult, SelfTestReport  # noqa: E402

#: 中文字体候选路径（Windows 优先，其次 Linux/macOS 常见路径）
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

#: 六张标准图的固定文件名与说明（README / 手册引用这些名字）
SHOTS = {
    "main": ("01-main-window.png", "主界面：曲线 + 阶跃指标"),
    "sim": ("02-sim-panel.png", "内置模拟器 + 仿真参数面板"),
    "port": ("03-port-diagnosis.png", "端口诊断对话框"),
    "banner": ("04-selftest-banner.png", "启动自检横幅（警告态）"),
    "step": ("05-step-metrics.png", "阶跃指标区特写"),
    "status": ("06-statusbar.png", "状态栏特写"),
}


def load_cjk_fonts() -> list[str]:
    """把系统中文字体注册进 Qt，返回成功加载的字体族名。"""
    loaded: list[str] = []
    for path in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        if font_id < 0:
            continue
        families = QFontDatabase.applicationFontFamilies(font_id)
        loaded.extend(str(name) for name in families)
    return loaded


def make_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    load_cjk_fonts()
    from serialpid.gui.theme import apply_dark

    apply_dark(app)
    # 字体族按可用性挑：中文优先雅黑，找不到就沿用 Qt 默认（仍能出图，只是字丑）
    families = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC"):
        if name in families:
            app.setFont(QFont(name, 9))
            break
    return app


def _pump(app: QApplication) -> None:
    """让定时器真正跑一轮：曲线是定时器驱动重绘的，只 processEvents 会停在空态。"""
    try:
        from PySide6.QtTest import QTest

        QTest.qWait(150)
    except Exception:  # pragma: no cover - QtTest 缺失时退回睡眠
        import time

        time.sleep(0.15)
    for _ in range(3):
        app.processEvents()


def _fake_connected(win: QWidget, detail: str = "内置模拟器") -> None:
    """把界面切到"已连接"外观（截图脚本不真的开通道，数据是示意数据）。"""
    win._on_connection(True, detail)
    _pump(QApplication.instance())


def _fake_link(win: QWidget, *, bad_frames: int = 0) -> None:
    """给状态栏一枚达标的链路快照；真实快照要等心跳，截图不能干等。"""
    from serialpid.link_stats import QUALITY_OK, LinkSnapshot

    win._link_status.update_snapshot(
        LinkSnapshot(
            rx_frames=200,
            tx_frames=6,
            bad_frames=bad_frames,
            fps=20.0,
            rtt_last_ms=11.8,
            rtt_avg_ms=12.4,
            rtt_max_ms=18.2,
            jitter_ms=1.6,
            uptime_s=10.0,
            quality=QUALITY_OK,
            quality_reason="链路正常",
        )
    )


def _new_window(app: QApplication, tmpdir: str, size: Tuple[int, int]):
    os.environ["SERIALPID_CONFIG"] = os.path.join(tmpdir, "config.json")
    from serialpid.gui.main_window import MainWindow

    win = MainWindow()
    win.resize(*size)
    win.show()
    _pump(app)
    return win


# ---------------------------------------------------------------------------
# 示意数据
# ---------------------------------------------------------------------------


def _sample_at(t_ms: int) -> Tuple[float, float, float]:
    """一条闭环响应示意：SP 在 4 s 从 25 跳到 30，PV 按二阶欠阻尼响应走。

    用二阶阶跃响应而不是随手写的指数，是为了让「响应指标」区真的算出东西：
    超调量 ≈ 12.6%、峰值时间 ≈ 2.7 s、调节时间 ≈ 6 s。截图里的指标要和手册
    讲的口径对得上，不能是一行 0.00% / 17950 ms 的假数据。
    """
    step_at = 4000
    sp = 25.0 if t_ms < step_at else 30.0
    if t_ms < step_at:
        pv = 25.0 - 0.03 * math.sin(t_ms / 900.0)
    else:
        zeta, omega_n = 0.55, 1.4
        x = (t_ms - step_at) / 1000.0
        omega_d = omega_n * math.sqrt(1.0 - zeta * zeta)
        envelope = math.exp(-zeta * omega_n * x)
        pv = 25.0 + 5.0 * (
            1.0
            - envelope
            * (
                math.cos(omega_d * x)
                + zeta / math.sqrt(1.0 - zeta * zeta) * math.sin(omega_d * x)
            )
        )
    out = 42.0 + 22.0 * (pv - 25.0) / 5.0
    out = max(0.0, min(100.0, out))
    return sp, pv, out


def feed_telemetry(win: QWidget, seconds: float = 22.0, dt_ms: int = 50) -> None:
    """按协议帧喂遥测：曲线、指标、状态栏、采样率都走真实处理路径。"""
    count = int(seconds * 1000 / dt_ms)
    for index in range(count):
        t_ms = index * dt_ms
        sp, pv, out = _sample_at(t_ms)
        win._on_frame(
            PacketType.TELEMETRY,
            pack_telemetry(t_ms, sp, pv, out, sp - pv),
        )


def scroll_left_to(win: QWidget, widget: QWidget) -> None:
    """把左栏滚到指定面板顶部（截图要露出那块内容）。"""
    bar = win._left_scroll.verticalScrollBar()
    y = widget.mapTo(win._left_scroll.widget(), widget.rect().topLeft()).y()
    bar.setValue(max(0, min(bar.maximum(), y - 6)))


# ---------------------------------------------------------------------------
# 六张图
# ---------------------------------------------------------------------------


def capture_main(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        try:
            win._check_banner.hide()
            _fake_connected(win)
            _fake_link(win)
            feed_telemetry(win)
            win._step._analyze_curve()  # 指标区从"未分析"切到有值
            scroll_left_to(win, win._param)
            _pump(app)
            return _save(win, out / SHOTS["main"][0])
        finally:
            win.close()


def capture_sim(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        try:
            win._check_banner.hide()
            import serialpid.gui.main_window as mw

            win._transport.setCurrentText(mw.CHANNEL_SIM)
            _pump(app)
            win._sim.set_expanded(True)
            _fake_connected(win, "内置模拟器")
            _fake_link(win)
            feed_telemetry(win, seconds=8.0)
            _pump(app)
            bar = win._left_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            _pump(app)
            return _save(win, out / SHOTS["sim"][0])
        finally:
            win.close()


def capture_port(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    from serialpid.port_scan import PortScanResult

    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        dialog = None
        try:
            win._port_scan = PortScanResult(
                status="empty",
                backend_available=True,
                elapsed_ms=23.5,
                pyserial_version="3.5",
                platform_name="Windows-11-10.0.26200-SP0",
            )
            dialog, _text = win.build_port_diagnostics_dialog()
            dialog.show()
            _pump(app)
            return _save_scaled(dialog, out / SHOTS["port"][0], factor=1.5)
        finally:
            if dialog is not None:
                dialog.close()
            win.close()


def capture_banner(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        try:
            win._check_banner.set_report(
                SelfTestReport(
                    checks=[
                        CheckResult("python_runtime", "Python 运行时", "pass", "3.13.3"),
                        CheckResult(
                            "icon_asset",
                            "图标资源",
                            "warn",
                            "找不到图标，使用默认窗口图标（不影响功能）",
                        ),
                    ],
                    generated_at="2026-11-07 14:50:00",
                )
            )
            feed_telemetry(win, seconds=6.0)
            _pump(app)
            return _save(win, out / SHOTS["banner"][0])
        finally:
            win.close()


def capture_step(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        try:
            win._check_banner.hide()
            feed_telemetry(win)
            win._step._analyze_curve()
            _pump(app)
            return _save_scaled(win._step, out / SHOTS["step"][0], factor=2.0)
        finally:
            win.close()


def capture_status(app: QApplication, out: Path, size: Tuple[int, int]) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        win = _new_window(app, tmp, size)
        try:
            win._check_banner.hide()
            _fake_connected(win, "内置模拟器")
            # 坏帧计数放在 3：状态栏的"坏帧 N"只在出过问题时才出现
            _fake_link(win, bad_frames=3)
            feed_telemetry(win, seconds=10.0)
            # 造 3 个坏帧：状态栏显示"坏帧 3"，说明它只在出问题后出现
            for _ in range(3):
                win._on_frame(PacketType.TELEMETRY, b"")
            _pump(app)
            return _save_status_strip(win.statusBar(), out / SHOTS["status"][0])
        finally:
            win.close()


def _save(widget: QWidget, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"截图失败（{path.name}）：QPixmap 为空")
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"截图写入失败：{path}")
    return path


def _save_scaled(widget: QWidget, path: Path, *, factor: float) -> Path:
    """特写图放大保存，文档里缩到一栏宽仍然读得清。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"截图失败（{path.name}）：QPixmap 为空")
    scaled = pixmap.scaled(
        int(pixmap.width() * factor),
        int(pixmap.height() * factor),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    if not scaled.save(str(path), "PNG"):
        raise RuntimeError(f"截图写入失败：{path}")
    return path


def _save_status_strip(widget: QWidget, path: Path, *, factor: int = 2) -> Path:
    """状态栏太宽：长条缩进文档后字就没了，所以把两端裁出来上下拼。

    上排是左侧实时值，下排是右侧链路质量（采样率 / 坏帧计数）；中间本来就是
    空白，裁掉不影响信息。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    source = widget.grab()
    if source.isNull():
        raise RuntimeError(f"截图失败（{path.name}）：QPixmap 为空")
    image = source.toImage()
    width, height = image.width(), image.height()
    left = QPixmap.fromImage(image.copy(0, 0, min(620, width), height))
    # 右端留 25 px 不给：那是窗口缩放手柄，裁进图里像个乱码点
    right_x = max(0, width - 500)
    right_w = min(475, width - 25 - right_x)
    right = QPixmap.fromImage(image.copy(right_x, 0, right_w, height))
    left = left.scaled(
        left.width() * factor,
        left.height() * factor,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    right = right.scaled(
        right.width() * factor,
        right.height() * factor,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    gap = 10
    canvas = QPixmap(max(left.width(), right.width()), left.height() + gap + right.height())
    canvas.fill(QColor("#1e222a"))
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, left)
    painter.drawPixmap(0, left.height() + gap, right)
    painter.end()
    if not canvas.save(str(path), "PNG"):
        raise RuntimeError(f"截图写入失败：{path}")
    return path


CAPTURES = {
    "main": capture_main,
    "sim": capture_sim,
    "port": capture_port,
    "banner": capture_banner,
    "step": capture_step,
    "status": capture_status,
}


def capture_all(
    out_dir: str | os.PathLike[str],
    *,
    size: Tuple[int, int] = (1440, 1024),
    only: Iterable[str] | None = None,
) -> list[Path]:
    """生成全部（或指定）截图，返回文件路径列表。"""
    app = make_app()
    out = Path(out_dir)
    keys = list(only) if only else list(CAPTURES)
    unknown = [key for key in keys if key not in CAPTURES]
    if unknown:
        raise ValueError(f"未知截图项：{', '.join(unknown)}")
    return [CAPTURES[key](app, out, size) for key in keys]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成文档标准截图（离屏渲染）")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "images"),
        help="输出目录，默认 docs/images",
    )
    parser.add_argument("--size", default="1440x1024", help="窗口尺寸，如 1440x900")
    parser.add_argument(
        "--only",
        default="",
        help="只生成指定项（逗号分隔）：" + "、".join(SHOTS[key][0] for key in SHOTS),
    )
    args = parser.parse_args(argv)

    try:
        width, height = (int(part) for part in args.size.lower().split("x"))
    except ValueError:
        print(f"尺寸格式应为 宽x高，收到：{args.size}", file=sys.stderr)
        return 2

    only = [part.strip() for part in args.only.split(",") if part.strip()]
    # 允许按文件名（01-main-window）或短名（main）指定
    only = [
        next(
            (key for key, (name, _desc) in SHOTS.items() if item in (key, name)),
            item,
        )
        for item in only
    ]

    paths = capture_all(args.out, size=(width, height), only=only or None)
    for path in paths:
        print(f"已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
