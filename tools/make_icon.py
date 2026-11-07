"""生成上位机应用图标 ``assets/app.ico``（含多尺寸）与 ``assets/app.png``。

用法（在仓库根目录）::

    python tools/make_icon.py

仅依赖 Pillow；颜色与 ``gui/theme.py`` 深色主题一致。
图标内容：深色底 + 橙色阶跃设定值虚线 + 蓝色被控量响应曲线。
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

BG = (30, 34, 42, 255)       # #1e222a
SP = (255, 180, 84, 255)     # #ffb454
PV = (78, 161, 255, 255)     # #4ea1ff
GRID = (58, 65, 80, 255)     # #3a4150

SIZE = 256
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")


def _draw_icon(size: int) -> Image.Image:
    """按给定尺寸绘制一帧图标（坐标先按 256 画布算，再缩放）。"""
    scale = size / SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角深色底
    radius = int(56 * scale)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)

    # 网格线（弱化）
    for gx in (0.25, 0.5, 0.75):
        x = int(gx * size)
        d.line([(x, int(24 * scale)), (x, int(232 * scale))], fill=GRID, width=max(1, int(2 * scale)))
    for gy in (0.35, 0.65):
        y = int(gy * size)
        d.line([(int(24 * scale), y), (int(232 * scale), y)], fill=GRID, width=max(1, int(2 * scale)))

    # SP 阶跃虚线：先低后高
    def px(x: float) -> int:
        return int(x * scale)

    sp_y_low, sp_y_high = 178.0, 78.0
    d.line([(px(28), px(sp_y_low)), (px(110), px(sp_y_low))], fill=SP, width=max(2, int(7 * scale)))
    d.line([(px(110), px(sp_y_low)), (px(110), px(sp_y_high))], fill=SP, width=max(2, int(7 * scale)))
    d.line([(px(110), px(sp_y_high)), (px(228), px(sp_y_high))], fill=SP, width=max(2, int(7 * scale)))

    # PV 响应曲线：上升 + 轻微超调 + 回落逼近
    pv_points = [
        (28, 182), (70, 180), (110, 170), (132, 128),
        (150, 86), (168, 70), (186, 84), (204, 80), (228, 79),
    ]
    pv_xy = [(px(x), px(y)) for x, y in pv_points]
    d.line(pv_xy, fill=PV, width=max(3, int(10 * scale)), joint="curve")
    for x, y in pv_xy:
        d.ellipse([x - int(4 * scale), y - int(4 * scale),
                   x + int(4 * scale), y + int(4 * scale)], fill=PV)

    return img


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    base = _draw_icon(SIZE)

    ico_path = os.path.join(OUT_DIR, "app.ico")
    png_path = os.path.join(OUT_DIR, "app.png")

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(ico_path, sizes=sizes)
    base.save(png_path)

    print(f"[OK] 图标已生成: {os.path.abspath(ico_path)}")
    print(f"[OK] 预览图已生成: {os.path.abspath(png_path)}")


if __name__ == "__main__":
    main()
