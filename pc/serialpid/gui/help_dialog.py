# -*- coding: utf-8 -*-
"""内置帮助对话框（卡30）：F1 打开，内容与用户手册同源。

设计要点：

* **同源**：帮助内容不另写一份，直接从 ``docs/user_guide.md`` 抽取三节
  （五分钟上手 / 界面速查 / 常见问题）后渲染；手册改了，帮助跟着改。
* **可降级**：打包成单文件后 ``docs/`` 不一定随包，找不到手册时退回内置
  精简文案（与手册同口径，只保留最常查的三块），不让 F1 弹空窗。
* **可测**：抽取与拼装是纯字符串函数，GUI 只负责显示，测试不依赖渲染。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QTextDocument
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from .theme import COLORS

#: 帮助对话框要展示的三节（按显示顺序）；匹配标题里的关键字
HELP_SECTIONS: Tuple[str, ...] = ("五分钟上手", "界面速查", "常见问题")

#: 找不到手册时的兜底文案（与 user_guide.md 同口径的精简版）
FALLBACK_HELP = """# SerialPID-Tuner 帮助

## 五分钟上手

1. 打开程序，通道选「内置模拟器」——不用插硬件、不用装驱动。
2. 点「连接」，等状态变成「已连接」。
3. 右侧曲线开始滚动，说明整条链路（协议 / 绘图 / 参数）已经通了。

## 界面速查

| 区域 | 一句话 |
| --- | --- |
| 顶部工具栏 | 选通道、选端口、连接 / 断开；右侧显示采样条数与速率 |
| 参数面板 | 改 Kp / Ki / Kd / SP，点「下发参数」，看 PID_ACK 回显 |
| 阶跃面板 | 下发阶跃命令，并用「分析最近曲线」算超调 / 调节时间 |
| Z-N 面板 | 填 Ku / Tu 或 K / L / T，算出 P / PI / PID 初值 |
| 曲线面板 | SP / PV / OUT 实时滚动，滚轮缩放、拖动平移 |
| 日志面板 | 事件记录与 CSV 导出；「清空日志」不动曲线 |
| 仿真对象参数 | 内置模拟器的对象增益 / 时间常数 / 延迟 / 噪声 |

## 常见问题

- **连接失败**：先看日志里的「现象 / 原因 / 下一步」；串口被占用时关掉串口助手。
- **曲线不动**：确认固件在发 TELEMETRY，且两侧波特率都是 115200 8N1。
- **数据乱码 / 坏帧**：多为波特率或协议版本不一致；换短 USB 线再试。
- **指标算不出**：先跑一段含明显阶跃的曲线，样本至少 4 点。
- **中文乱码**：控制台代码页问题，不影响 CSV（文件是 UTF-8 带 BOM）。
- **exe 被拦**：单文件打包常见误报，加信任后重试。
"""

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# 路径与文本
# ---------------------------------------------------------------------------


def docs_dir() -> Path:
    """文档目录：环境变量 > 打包解包目录 > 仓库内 ``docs/``。"""
    env = os.environ.get("SERIALPID_DOCS_DIR")
    if env:
        return Path(env)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "docs"
    return Path(__file__).resolve().parents[3] / "docs"


def guide_path() -> Path:
    return docs_dir() / "user_guide.md"


def _strip_numbering(title: str) -> str:
    """去掉标题里的序号（"1. 五分钟上手" -> "五分钟上手"）。"""
    return re.sub(r"^[\d.\s、]+", "", title).strip()


def _headings(lines: List[str]) -> List[Tuple[int, int, str]]:
    """返回 (行号, 级别, 标题) 列表。"""
    found: List[Tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            found.append((index, len(match.group(1)), match.group(2).strip()))
    return found


def extract_sections(
    markdown: str, keys: Iterable[str] = HELP_SECTIONS
) -> Dict[str, Tuple[str, str]]:
    """按标题关键字抽取章节正文（含子标题），返回 {关键字: (标题, 正文)}。

    只匹配标题行，不匹配正文里的同名词；章节在下一个同级或更高级标题处结束。
    """
    lines = markdown.splitlines()
    headings = _headings(lines)
    picked: Dict[str, Tuple[str, str]] = {}
    for key in keys:
        for position, (index, level, title) in enumerate(headings):
            if key not in _strip_numbering(title):
                continue
            end = len(lines)
            for next_index, next_level, _next_title in headings[position + 1:]:
                if next_level <= level:
                    end = next_index
                    break
            body = "\n".join(lines[index + 1:end]).strip("\n")
            picked[key] = (title, body)
            break
    return picked


def build_help_markdown(markdown: str, keys: Iterable[str] = HELP_SECTIONS) -> str:
    """把抽取到的章节拼成一份独立的帮助文档。

    找不到任何一节时返回空串，由调用方决定是否退回内置文案——不臆造内容。
    """
    sections = extract_sections(markdown, keys)
    if not sections:
        return ""
    blocks: List[str] = ["# SerialPID-Tuner 帮助", ""]
    for key in keys:
        if key not in sections:
            continue
        title, body = sections[key]
        blocks.append(f"## {_strip_numbering(title) or key}")
        blocks.append("")
        blocks.append(body)
        blocks.append("")
    return "\n".join(blocks).strip() + "\n"


def load_help_markdown() -> str:
    """读取并拼装帮助内容；手册缺失 / 不可读 / 抽不到三节时退回内置文案。"""
    try:
        text = guide_path().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return FALLBACK_HELP
    built = build_help_markdown(text)
    return built if built else FALLBACK_HELP


def help_source_label() -> str:
    """帮助内容来源（显示在对话框底部，便于确认是不是最新手册）。"""
    try:
        path = guide_path()
        if path.is_file():
            return f"内容来源：{path}"
    except OSError:
        pass
    return "内容来源：内置精简文案（未找到 docs/user_guide.md）"


# ---------------------------------------------------------------------------
# 对话框
# ---------------------------------------------------------------------------

_STYLE = f"""
body {{ color: {COLORS['text']}; }}
h1 {{ font-size: 16pt; }}
h2 {{ font-size: 13pt; color: {COLORS['accent']}; }}
table {{ border-collapse: collapse; }}
th, td {{ border: 1px solid {COLORS['border']}; padding: 4px 8px; }}
code, pre {{ background: {COLORS['panel']}; color: {COLORS['text']}; }}
"""


class _ScaledBrowser(QTextBrowser):
    """按对话框宽度缩图：手册里的截图是 1440 px 宽的，直接塞进 900 px 的
    帮助窗口会撑出横向滚动条。这里只缩显示尺寸，不写回文件。"""

    _MAX_WIDTH = 820

    def loadResource(self, type, name):  # noqa: A002 - Qt 的固定命名
        if type == QTextDocument.ResourceType.ImageResource:
            local = name.toLocalFile()
            if local:
                image = QImage(local)
                if not image.isNull():
                    if image.width() > self._MAX_WIDTH:
                        image = image.scaledToWidth(
                            self._MAX_WIDTH,
                            mode=Qt.TransformationMode.SmoothTransformation,
                        )
                    return image
        return super().loadResource(type, name)


class HelpDialog(QDialog):
    """F1 帮助：三块内容一体滚动，底部可打开完整手册。"""

    def __init__(self, parent=None, markdown: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("使用帮助")
        self.resize(900, 680)
        self._markdown = load_help_markdown() if markdown is None else markdown

        self._browser = _ScaledBrowser()
        self._browser.setOpenExternalLinks(False)
        self._browser.setOpenLinks(False)
        # markdown 里的相对图片路径按 docs/ 解析，帮助里的截图才能显示
        self._browser.document().setBaseUrl(
            QUrl.fromLocalFile(str(docs_dir()) + os.sep)
        )
        self._browser.document().setDefaultStyleSheet(_STYLE)
        self._browser.setMarkdown(self._markdown)
        self._browser.setStyleSheet(
            f"QTextBrowser {{ background: {COLORS['bg']}; color: {COLORS['text']};"
            f" border: 1px solid {COLORS['border']}; }}"
        )

        self._source = QLabel(help_source_label())
        self._source.setStyleSheet(f"color: {COLORS['muted']}; font-size: 9pt;")

        open_btn = QPushButton("打开完整手册")
        open_btn.clicked.connect(self._open_guide)
        open_btn.setEnabled(guide_path().is_file())
        close_btn = QPushButton("关闭")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)

        row = QHBoxLayout()
        row.addWidget(self._source, 1)
        row.addWidget(open_btn)
        row.addWidget(close_btn)

        box = QVBoxLayout(self)
        box.addWidget(self._browser, 1)
        box.addLayout(row)

    # ---- 测试 / 外部调用 ----

    def markdown(self) -> str:
        return self._markdown

    def browser(self) -> QTextBrowser:
        return self._browser

    def _open_guide(self) -> None:
        path = guide_path()
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


__all__ = [
    "FALLBACK_HELP",
    "HELP_SECTIONS",
    "HelpDialog",
    "build_help_markdown",
    "docs_dir",
    "extract_sections",
    "guide_path",
    "help_source_label",
    "load_help_markdown",
]
