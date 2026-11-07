# -*- coding: utf-8 -*-
"""卡35 四条交互路径的自动走查（离屏渲染 + 截图留证）。

为什么要有这个脚本：单测能证明"函数返回值对"，但证明不了"F1 按下去真弹窗、
向导三屏真的能翻、关于框真的能复制、截图重跑真的稳定"。这个脚本把四条路径
按用户的操作顺序跑一遍，每步留一张图，并把结果写成一份可复核的记录。

用法（在仓库根或 pc/ 下都行）：

    python tools/walkthrough_35.py
    python tools/walkthrough_35.py --out docs/images/acceptance-35

输出：
    * ``<out>/*.png``           每条路径的现场截图
    * ``<out>/report.json``     每一步的通过/失败与关键断言值
    * 终端打印一张简洁的结果表

**注意**：这是"脚本走查"，不是人对真机窗口的手工点验。它覆盖不了真实弹窗
时序（模态阻塞、焦点抢夺、Alt-Tab 之后快捷键是否还灵），所以 docs/
acceptance-35.md 里仍列了需要人工点一遍的最短清单。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]
PC_DIR = REPO_ROOT / "pc"
for path in (str(PC_DIR), str(REPO_ROOT / "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

from PySide6.QtWidgets import QApplication  # noqa: E402

import make_screenshots as shots  # noqa: E402


class Recorder:
    """收集检查结果，失败也继续跑完（一次跑完能一次看完所有问题）。"""

    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.steps.append({"step": name, "ok": bool(condition), "detail": detail})
        return bool(condition)

    def note(self, name: str, detail: str) -> None:
        self.steps.append({"step": name, "ok": True, "detail": detail, "note": True})

    @property
    def failed(self) -> List[Dict[str, Any]]:
        return [step for step in self.steps if not step["ok"]]


def _fresh_config(tmp: str, *, onboarding_done: bool = True) -> None:
    """准备一份临时配置。

    默认把 ``ui.onboarding_done`` 先写成 true：主窗口构造后会 singleShot 弹向导，
    走查脚本一旦 pump 事件就会被模态 exec 卡死——除了"测向导"那一步，其它路径
    都不该让向导插进来。
    """
    path = Path(tmp) / "config.json"
    os.environ["SERIALPID_CONFIG"] = str(path)
    if onboarding_done:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ui": {"onboarding_done": True}}, ensure_ascii=False),
            encoding="utf-8",
        )


def _save(widget, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap = widget.grab()
    if pixmap.isNull():
        raise RuntimeError(f"截图失败：{path.name}")
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"截图写入失败：{path}")
    return path


def _app() -> QApplication:
    app = shots.make_app()
    return app


def _new_window(app: QApplication, size=(1280, 860)):
    from serialpid.gui.main_window import MainWindow

    win = MainWindow()
    win.resize(*size)
    win.show()
    shots._pump(app)
    return win


# ---------------------------------------------------------------------------
# 路径一：首次引导
# ---------------------------------------------------------------------------


def walk_onboarding(app: QApplication, out: Path, rec: Recorder) -> None:
    import serialpid.gui.onboarding as onboarding

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_config(tmp, onboarding_done=False)
        rec.check("onboarding/首启未看过", onboarding.onboarding_done() is False)

        dialog = onboarding.OnboardingDialog()
        dialog.resize(720, 520)
        dialog.show()
        shots._pump(app)
        for index in range(dialog.page_count()):
            _save(dialog, out / f"onboarding-{index + 1}.png")
            rec.check(
                f"onboarding/第 {index + 1} 屏可显示",
                dialog.current_index() == index,
                dialog._step_label.text(),
            )
            if index < dialog.page_count() - 1:
                dialog._go_next()
                shots._pump(app)
        rec.check("onboarding/末屏按钮变成完成", dialog._next.text() == "完成")

        dialog._skip_all()
        shots._pump(app)
        rec.check(
            "onboarding/跳过被记住",
            onboarding.onboarding_done() is True,
            str(dialog.skipped),
        )
        config_path = Path(os.environ["SERIALPID_CONFIG"])
        rec.check("onboarding/配置落盘", config_path.is_file(), str(config_path))
        dialog.close()

        # 模拟重启：新实例 + 新窗口，不该再弹
        again = onboarding.OnboardingDialog()
        rec.check("onboarding/重开不再弹", onboarding.onboarding_done() is True)
        again.close()

        win = _new_window(app)
        try:
            rec.check(
                "onboarding/主窗口不再弹",
                win.maybe_show_onboarding() is False,
            )
            _save(win, out / "onboarding-4-main-window.png")
        finally:
            win.close()
            shots._pump(app)


# ---------------------------------------------------------------------------
# 路径二：F1 帮助
# ---------------------------------------------------------------------------


def walk_help(app: QApplication, out: Path, rec: Recorder) -> None:
    import serialpid.gui.help_dialog as hd
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    with tempfile.TemporaryDirectory() as tmp:
        _fresh_config(tmp)
        win = _new_window(app)
        try:
            opened: List[str] = []
            real_build = win.build_help_dialog

            class _CapturedThenDone:
                """先抓真实对话框的图，再用替身替 exec()：exec() 会重新显示并阻塞，
                没有人在窗口里点"关闭"，脚本就会永远卡在事件循环里。"""

                def __init__(self, title: str) -> None:
                    self._title = title

                def windowTitle(self) -> str:
                    return self._title

                def exec(self) -> int:
                    return 0

            def _build():
                dialog = real_build()
                dialog.show()
                shots._pump(app)
                _save(dialog, out / "help-f1.png")
                opened.append(dialog.windowTitle())
                dialog.close()
                return _CapturedThenDone(dialog.windowTitle())

            win.build_help_dialog = _build  # type: ignore[assignment]
            QTest.keyClick(win, Qt.Key.Key_F1)
            shots._pump(app)
            rec.check("help/按 F1 弹出帮助", opened == ["使用帮助"], str(opened))
        finally:
            win.close()
            shots._pump(app)

        markdown = hd.load_help_markdown()
        rec.check("help/内容来自手册同源", markdown != hd.FALLBACK_HELP)
        rec.check(
            "help/手册路径可读", hd.guide_path().is_file(), str(hd.guide_path())
        )
        for section in hd.HELP_SECTIONS:
            rec.check(f"help/含「{section}」", f"## {section}" in markdown)
        rec.check(
            "help/来源标签指向真实手册",
            "user_guide.md" in hd.help_source_label(),
            hd.help_source_label(),
        )

        # 畸形 markdown 容错：坏链接 + 未闭合代码块
        broken = "# 帮助\n\n## 五分钟上手\n\n看[诊断](javascript:void(0))。\n\n```text\n现象：打不开\n"
        dialog = hd.HelpDialog(markdown=broken)
        dialog.show()
        shots._pump(app)
        text = dialog.browser().toPlainText()
        rec.check("help/坏 markdown 不崩", "打不开" in text)
        rec.check(
            "help/坏 markdown 仍与原文同源",
            dialog.rendered_markdown().startswith("# 帮助"),
        )
        _save(dialog, out / "help-malformed.png")
        dialog.close()


# ---------------------------------------------------------------------------
# 路径三：关于框
# ---------------------------------------------------------------------------


def walk_about(app: QApplication, out: Path, rec: Recorder) -> None:
    import serialpid.gui.about_dialog as ad
    from serialpid import selftest

    dialog = ad.AboutDialog()
    dialog.show()
    shots._pump(app)
    _save(dialog, out / "about.png")
    values = dialog.values()
    rec.check("about/版本号来自包属性", values["程序版本"] == ad.program_version())
    rec.check("about/含串口后端", "串口后端" in values, values.get("串口后端", ""))
    rec.check(
        "about/串口后端与自检同源",
        values["串口后端"] == selftest.serial_backend_summary(),
    )
    for dep in ("PySide6", "pyqtgraph", "pyserial", "numpy"):
        rec.check(f"about/依赖 {dep} 有版本", values[dep].strip() != "", values[dep])

    copied = dialog.copy_diagnostics()
    clipboard = QApplication.clipboard().text()
    rec.check("about/复制诊断到剪贴板", clipboard == copied)
    rec.check(
        "about/诊断含三段之一：串口后端",
        "串口后端：" in copied,
        copied.splitlines()[:1] or "",
    )
    rec.check("about/诊断可整段粘贴", copied.startswith("SerialPID-Tuner 诊断信息"))
    dialog.close()


# ---------------------------------------------------------------------------
# 路径四：截图脚本
# ---------------------------------------------------------------------------


def walk_screenshots(app: QApplication, out: Path, rec: Recorder) -> None:
    import numpy as np
    from PySide6.QtGui import QImage

    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        first = shots.capture_all(tmp1, size=(1280, 860))
        second = shots.capture_all(tmp2, size=(1280, 860))
        rec.check(
            "shots/两次文件名一致",
            [p.name for p in first] == [p.name for p in second],
        )
        for before, after in zip(first, second):
            a, b = QImage(str(before)), QImage(str(after))
            rows, stride = a.height(), a.bytesPerLine()
            x = np.frombuffer(a.constBits(), dtype=np.uint8, count=rows * stride)
            y = np.frombuffer(b.constBits(), dtype=np.uint8, count=rows * stride)
            ratio = float((x != y).mean())
            rec.check(
                f"shots/{before.name} 重跑无漂移",
                ratio < 0.002,
                f"diff={ratio:.4%}",
            )
            rec.check(
                f"shots/{before.name} 尺寸稳定",
                a.size() == b.size(),
                f"{a.width()}x{a.height()}",
            )

    # 中文不是豆腐块：不同汉字必须画出不同墨迹
    from PySide6.QtGui import QColor, QFont, QPainter

    families = shots.load_cjk_fonts()
    rec.check("shots/加载到中文字体", bool(families), "、".join(families[:3]))

    def render(char: str) -> bytes:
        image = QImage(64, 64, QImage.Format.Format_ARGB32)
        image.fill(QColor("#000000"))
        painter = QPainter(image)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont(app.font().family(), 32))
        painter.drawText(image.rect(), 0x0084, char)
        painter.end()
        return bytes(image.constBits())

    first_glyph, second_glyph = render("连"), render("口")
    rec.check("shots/中文非豆腐块", first_glyph != second_glyph)


# ---------------------------------------------------------------------------


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="卡35 四条交互路径自动走查")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "images" / "acceptance-35"),
        help="证据图输出目录",
    )
    args = parser.parse_args(argv)
    out = Path(args.out)

    # 自检换成桩：走查不该去写用户日志目录
    import serialpid.selftest as st

    st.run_selftest = lambda **_kwargs: st.SelfTestReport(checks=[])  # type: ignore[assignment]

    rec = Recorder()
    app = _app()
    for name, walker in (
        ("onboarding", walk_onboarding),
        ("help", walk_help),
        ("about", walk_about),
        ("screenshots", walk_screenshots),
    ):
        print(f"---- {name} ----", flush=True)
        try:
            walker(app, out, rec)
        except Exception as exc:  # noqa: BLE001 - 一条路径炸了也要把其余跑完
            rec.check(f"{name}/整体", False, f"{type(exc).__name__}: {exc}")
        print(f"---- {name} 完成（累计 {len(rec.steps)} 项）----", flush=True)

    report = {
        "generated_by": "tools/walkthrough_35.py（离屏脚本走查）",
        "out": str(out),
        "total": len(rec.steps),
        "failed": len(rec.failed),
        "steps": rec.steps,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    width = max(len(step["step"]) for step in rec.steps) + 2
    for step in rec.steps:
        flag = "OK  " if step["ok"] else "FAIL"
        print(f"{flag} {step['step']:<{width}} {step['detail']}")
    print(f"\n合计 {report['total']} 项，失败 {report['failed']} 项")
    print(f"证据与报告：{out}")
    return 0 if not rec.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
