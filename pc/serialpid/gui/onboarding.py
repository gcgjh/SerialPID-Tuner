# -*- coding: utf-8 -*-
"""首次运行引导（卡30）：三屏把"从打开到看见曲线"讲完。

约束（来自任务卡）：

* 只在**没有配置文件**的第一次启动出现，点过"跳过"也要记住，不能连续弹；
* 轻量：三屏、一个跳过入口、不强制走完；
* 界面上说的三步必须和真实界面一致（通道 → 连接 → 曲线），不写"理想流程"。

记忆写在配置 ``ui.onboarding_done``。任何关闭方式（走完 / 跳过 / 点 X）
都算"看过了"——用户不需要为了不再弹窗而专门点一次跳过。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import load_config, save_config
from .help_dialog import docs_dir
from .theme import COLORS

#: (标题, 正文, 配图文件名)。配图不存在时只显示文字，不留空框。
PAGES: Tuple[Tuple[str, str, str], ...] = (
    (
        "第一步 · 选通道",
        "顶部<b>通道</b>下拉里有三个选项：\n\n"
        "• <b>内置模拟器</b>：不插硬件、不装驱动，直接在本程序里跑一个被控对象；\n"
        "• <b>TCP</b>：连 tools/fake_mcu.py 模拟器（需要另开终端）；\n"
        "• <b>Serial</b>：连真实板子，选对 COM 口与 115200 波特率。\n\n"
        "第一次用，建议先选<b>内置模拟器</b>。",
        "02-sim-panel.png",
    ),
    (
        "第二步 · 连接",
        "点右上角的<b>连接</b>按钮。\n\n"
        "状态从「未连接」变成<b>「已连接」</b>，顶部右侧开始显示采样条数与速率。\n\n"
        "连不上时不要猜：日志面板会给三段式提示——<b>现象 / 原因 / 下一步</b>，"
        "照着最后一行做就行。",
        "01-main-window.png",
    ),
    (
        "第三步 · 看到曲线",
        "右侧曲线开始滚动，SP（设定值）、PV（被控量）、OUT（输出）同屏显示。\n\n"
        "接下来就能改 Kp / Ki / Kd 下发参数、跑阶跃看指标。\n\n"
        "随时按 <b>F1</b> 打开内置帮助，内容与用户手册同源。",
        "05-step-metrics.png",
    ),
)


class OnboardingDialog(QDialog):
    """三屏向导；关闭即记住，不再第二次出现。"""

    def __init__(self, parent=None, *, remember_on_close: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("第一次使用 · 三步看到曲线")
        self.resize(720, 520)
        self._remember_on_close = remember_on_close
        self._index = 0
        self.skipped = False
        self.remembered = False

        self._stack = QStackedWidget()
        for title, body, image in PAGES:
            self._stack.addWidget(self._build_page(title, body, image))

        self._step_label = QLabel("")
        self._step_label.setStyleSheet(f"color: {COLORS['muted']};")

        self._back = QPushButton("上一步")
        self._back.clicked.connect(self._go_back)
        self._next = QPushButton("下一步")
        self._next.clicked.connect(self._go_next)
        self._skip = QPushButton("跳过")
        self._skip.setToolTip("直接进入主界面；之后可以在「帮助」菜单里重新查看")
        self._skip.clicked.connect(self._skip_all)

        row = QHBoxLayout()
        row.addWidget(self._skip)
        row.addWidget(self._step_label, 1)
        row.addWidget(self._back)
        row.addWidget(self._next)

        box = QVBoxLayout(self)
        box.addWidget(self._stack, 1)
        box.addLayout(row)
        self._apply_page()

    # ---- 构造 ----

    def _build_page(self, title: str, body: str, image: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel(title)
        heading.setStyleSheet(
            f"font-size: 15pt; font-weight: 600; color: {COLORS['text']};"
        )
        text = QLabel(body)
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        # 顶部对齐：默认是垂直居中，配图区一撑高，正文就漂到半空中（卡35 走查）
        text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        text.setStyleSheet(f"color: {COLORS['text']}; font-size: 10.5pt;")
        layout.addWidget(heading)
        layout.addWidget(text)
        layout.addSpacing(8)

        # 截图在 docs/images/ 下：只拼 docs_dir() 会找不到图，向导就变成
        # 一大片空白 + 文字（卡35 走查时抓到的）
        path = Path(docs_dir()) / "images" / image
        if not path.is_file():
            path = Path(docs_dir()) / image   # 兼容把图放在 docs/ 根下的旧布局
        if path.is_file():
            pix = QPixmap(str(path))
            if not pix.isNull():
                # 宽高都要限：05-step-metrics.png 是竖长条，只限宽会把三屏里
                # 的图文挤出窗口（卡35 走查时第 3 屏正文被压到顶上）
                shot = QLabel()
                shot.setPixmap(
                    pix.scaled(
                        QSize(560, 260),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                shot.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                shot.setToolTip(f"示意截图：docs/images/{image}")
                layout.addWidget(shot)
        layout.addStretch(1)   # 余量留底部，正文与配图贴着顶部排（不上下分离）
        return page

    # ---- 导航 ----

    def page_count(self) -> int:
        return self._stack.count()

    def current_index(self) -> int:
        return self._index

    def _apply_page(self) -> None:
        self._stack.setCurrentIndex(self._index)
        self._step_label.setText(f"{self._index + 1} / {self.page_count()}")
        self._back.setEnabled(self._index > 0)
        self._next.setText("完成" if self._index == self.page_count() - 1 else "下一步")

    def _go_back(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._apply_page()

    def _go_next(self) -> None:
        if self._index < self.page_count() - 1:
            self._index += 1
            self._apply_page()
            return
        self.remember()
        self.accept()

    def _skip_all(self) -> None:
        self.skipped = True
        self.remember()
        self.reject()

    # ---- 记忆 ----

    def remember(self) -> bool:
        """写 ``ui.onboarding_done = true``；写失败不阻塞关闭，返回是否写成功。"""
        if self.remembered or not self._remember_on_close:
            return self.remembered
        try:
            config, _warning = load_config()
            ui = config.get("ui")
            if not isinstance(ui, dict):
                ui = {}
            ui["onboarding_done"] = True
            config["ui"] = ui
            save_config(config)
        except Exception:  # noqa: BLE001 - 配置写不进时下次再问一次，不能因此崩
            return False
        self.remembered = True
        return True

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.remember()
        super().closeEvent(event)


def onboarding_done() -> bool:
    """读配置判断是否已看过引导；读不到按"没看过"处理。"""
    try:
        config, _warning = load_config()
    except Exception:  # noqa: BLE001
        return False
    ui = config.get("ui") if isinstance(config, dict) else None
    return bool(isinstance(ui, dict) and ui.get("onboarding_done"))


__all__ = ["OnboardingDialog", "PAGES", "onboarding_done"]
