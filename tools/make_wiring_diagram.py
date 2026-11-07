# -*- coding: utf-8 -*-
"""生成接线章节的引脚示意图（卡30）。

    python tools/make_wiring_diagram.py
    python tools/make_wiring_diagram.py --out docs/images/wiring-usb-ttl.png

为什么用脚本画而不是随手截图：接线图要说明的是"哪根线接到哪根针"，重要的是
交叉关系与共地，界面截图给不了。脚本生成的好处是改口径（换引脚、换电平）时
图跟着一起改，不会出现文档写了 PA9/PA10、图上还是老的。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[1]

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
)

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

W, H = 1600, 840
INK = QColor("#1b2430")
MUTED = QColor("#5b6879")
BOX_FILL = QColor("#f2f5f9")
BOX_BORDER = QColor("#c3ccd8")
TX_COLOR = QColor("#2f6fdd")
RX_COLOR = QColor("#c8741f")
GND_COLOR = QColor("#59636f")
NOTE_FILL = QColor("#fbf7ea")
NOTE_BORDER = QColor("#e4d9b6")


def _load_fonts() -> None:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)


def _font(size: int, *, bold: bool = False) -> QFont:
    families = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun"):
        if name in families:
            font = QFont(name, size)
            font.setBold(bold)
            return font
    font = QFont()
    font.setPointSize(size)
    font.setBold(bold)
    return font


def _box(
    painter: QPainter,
    rect: QRectF,
    title: str,
    lines: list[str],
    *,
    body_top: float | None = None,
) -> None:
    painter.setPen(QPen(BOX_BORDER, 1.2))
    painter.setBrush(BOX_FILL)
    painter.drawRoundedRect(rect, 10, 10)
    painter.setPen(INK)
    painter.setFont(_font(15, bold=True))
    painter.drawText(
        QRectF(rect.x() + 16, rect.y() + 12, rect.width() - 32, 26),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        title,
    )
    painter.setFont(_font(11))
    painter.setPen(MUTED)
    y = body_top if body_top is not None else rect.y() + 44
    for line in lines:
        painter.drawText(
            QRectF(rect.x() + 16, y, rect.width() - 32, 40),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            line,
        )
        y += 22


def _pin(painter: QPainter, x: float, y: float, label: str, color: QColor,
         *, left_side: bool, sub: str = "") -> None:
    painter.setPen(QPen(color, 1.4))
    painter.setBrush(QColor("#ffffff"))
    painter.drawEllipse(QPointF(x, y), 6, 6)
    painter.setFont(_font(11, bold=True))
    painter.setPen(INK)
    if left_side:
        painter.drawText(QRectF(x, y - 24, 150, 18), Qt.AlignmentFlag.AlignLeft, label)
        if sub:
            painter.setFont(_font(9))
            painter.setPen(MUTED)
            painter.drawText(QRectF(x, y + 8, 150, 16), Qt.AlignmentFlag.AlignLeft, sub)
    else:
        painter.drawText(
            QRectF(x - 150, y - 24, 150, 18), Qt.AlignmentFlag.AlignRight, label
        )
        if sub:
            painter.setFont(_font(9))
            painter.setPen(MUTED)
            painter.drawText(
                QRectF(x - 150, y + 8, 150, 16), Qt.AlignmentFlag.AlignRight, sub
            )


def _wire(painter: QPainter, start: QPointF, end: QPointF, color: QColor) -> None:
    painter.setPen(QPen(color, 2.2))
    painter.drawLine(start, end)
    painter.setBrush(color)
    # 箭头三角，指向信号流向（TX 侧发出，RX 侧收到）
    angle = 0.0
    vec = end - start
    length = (vec.x() ** 2 + vec.y() ** 2) ** 0.5
    if length:
        import math

        angle = math.degrees(math.atan2(-vec.y(), vec.x()))
    painter.save()
    painter.translate(end)
    painter.rotate(angle)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(0, 0),
                QPointF(-9, -4.5),
                QPointF(-9, 4.5),
            ]
        )
    )
    painter.restore()


def render(path: Path) -> Path:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    _load_fonts()
    image = QImage(W, H, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    # 标题
    painter.setPen(INK)
    painter.setFont(_font(21, bold=True))
    painter.drawText(
        QRectF(60, 26, W - 120, 34),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        "USB-TTL 与目标板接线示意（USART1 · 3.3 V TTL）",
    )
    painter.setFont(_font(11))
    painter.setPen(MUTED)
    painter.drawText(
        QRectF(60, 60, W - 120, 22),
        Qt.AlignmentFlag.AlignLeft,
        "波特率 115200 · 8N1，与固件 PID_APP 的串口配置一致；只接 TX/RX 不接地是最常见的\"能连上但丢帧\"原因。",
    )

    # 三个方块
    _box(
        painter,
        QRectF(60, 250, 300, 190),
        "上位机（PC）",
        [
            "SerialPID-Tuner 运行端",
            "USB 口：数据 + 供电",
            "无需驱动（USB-TTL 自带）",
        ],
    )
    _box(
        painter,
        QRectF(500, 180, 390, 380),
        "USB-TTL 模块",
        [
            "CH340 / CP2102 均可",
            "TXD：模块发 → 单片机收",
            "RXD：模块收 ← 单片机发",
            "GND：与目标板共地",
        ],
        body_top=455,
    )
    _box(
        painter,
        QRectF(1120, 180, 440, 380),
        "目标板（STM32 / 兼容固件）",
        [
            "USART1：PA9 = TX，PA10 = RX",
            "电平 3.3 V TTL",
            "5 V 单片机需分压或电平转换",
        ],
        body_top=455,
    )

    # USB 连线
    _wire(painter, QPointF(360, 345), QPointF(500, 345), QColor("#8a94a3"))
    painter.setFont(_font(10))
    painter.setPen(MUTED)
    painter.drawText(QRectF(360, 300, 140, 18), Qt.AlignmentFlag.AlignLeft, "USB")

    # 引脚：TXD 在上、RXD 在下；目标板 PA9(TX) 在上、PA10(RX) 在下 —— 两根线交叉
    ttl_x, mcu_x = 890.0, 1120.0
    txd_y, rxd_y, gnd_y = 260.0, 340.0, 420.0
    _pin(painter, ttl_x, txd_y, "TXD", TX_COLOR, left_side=False, sub="模块发")
    _pin(painter, ttl_x, rxd_y, "RXD", RX_COLOR, left_side=False, sub="模块收")
    _pin(painter, ttl_x, gnd_y, "GND", GND_COLOR, left_side=False, sub="共地")

    _pin(painter, mcu_x, txd_y, "PA9（TX）", TX_COLOR, left_side=True, sub="板子发")
    _pin(painter, mcu_x, rxd_y, "PA10（RX）", RX_COLOR, left_side=True, sub="板子收")
    _pin(painter, mcu_x, gnd_y, "GND", GND_COLOR, left_side=True, sub="共地")

    # 交叉：模块 TXD → 板子 RX；模块 RXD → 板子 TX
    _wire(painter, QPointF(ttl_x + 6, txd_y), QPointF(mcu_x - 6, rxd_y), TX_COLOR)
    _wire(painter, QPointF(ttl_x + 6, rxd_y), QPointF(mcu_x - 6, txd_y), RX_COLOR)
    _wire(painter, QPointF(ttl_x + 6, gnd_y), QPointF(mcu_x - 6, gnd_y), GND_COLOR)

    # 交叉说明
    painter.setFont(_font(10))
    painter.setPen(TX_COLOR)
    painter.drawText(
        QRectF(800, 250, 320, 18),
        Qt.AlignmentFlag.AlignCenter,
        "TX 接 RX（交叉）",
    )
    painter.setPen(RX_COLOR)
    painter.drawText(
        QRectF(800, 350, 320, 18),
        Qt.AlignmentFlag.AlignCenter,
        "RX 接 TX（交叉）",
    )

    # 底部要点
    notes = [
        ("① 交叉接线", "模块 TXD 接目标板 RX，模块 RXD 接目标板 TX；同名对同名接法收不到任何数据。"),
        ("② 必须共地", "只接 TX/RX、不接 GND 时参考电平漂着，表现为能连上、偶发坏帧，严重时整帧丢失。"),
        ("③ 电平匹配", "本固件是 3.3 V TTL；接 5 V 单片机请分压或用电平转换板，直连可能烧 RX 脚。"),
        ("④ 参数一致", "115200 / 8 数据位 / 无校验 / 1 停止位（8N1），与固件 uart 初始化一致，不一致就是\"坏帧\"刷屏。"),
    ]
    painter.setPen(QPen(NOTE_BORDER, 1.2))
    painter.setBrush(NOTE_FILL)
    painter.drawRoundedRect(QRectF(60, 600, W - 120, 200), 10, 10)
    y = 620
    for head, body in notes:
        painter.setFont(_font(12, bold=True))
        painter.setPen(INK)
        painter.drawText(QRectF(84, y, 150, 22), Qt.AlignmentFlag.AlignLeft, head)
        painter.setFont(_font(11))
        painter.setPen(MUTED)
        painter.drawText(
            QRectF(230, y, W - 320, 22),
            Qt.AlignmentFlag.AlignLeft,
            body,
        )
        y += 40

    painter.setFont(_font(9))
    painter.setPen(MUTED)
    painter.drawText(
        QRectF(60, H - 34, W - 120, 18),
        Qt.AlignmentFlag.AlignLeft,
        "图由 tools/make_wiring_diagram.py 生成；引脚以固件 mcu/ 的实际初始化代码为准。",
    )
    painter.end()

    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"写图失败：{path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 USART1 接线示意图")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "docs" / "images" / "wiring-usb-ttl.png"),
        help="输出 PNG 路径",
    )
    args = parser.parse_args(argv)
    path = render(Path(args.out))
    print(f"已生成 {path}（{path.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
