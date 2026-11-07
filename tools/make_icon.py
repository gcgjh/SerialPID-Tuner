"""生成上位机应用图标 ``assets/app.ico`` / ``assets/app.png`` / ``assets/app.icns``。

用法（在仓库根目录）::

    python tools/make_icon.py            # 三个都生成
    python tools/make_icon.py --check    # 只检查是否齐全，缺了退出码 1

仅依赖 Pillow；颜色与 ``gui/theme.py`` 深色主题一致。
图标内容：深色底 + 橙色阶跃设定值虚线 + 蓝色被控物量响应曲线。

三个格式各自的用处（卡37 跨平台打包）：

* ``.ico``：Windows exe 图标（多尺寸 16–256）；
* ``.icns``：macOS ``.app`` 图标（需要 1024 才能出 Retina 的 512@2x）；
* ``.png``：文档 / 关于框 / 图床用。

``.icns`` 用 Pillow 直接写（它在任何平台上都能生成，不必等 macOS 机器），
所以 CI 的 macOS job 也拿得到图标。
"""

from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw

BG = (30, 34, 42, 255)       # #1e222a
SP = (255, 180, 84, 255)     # #ffb454
PV = (78, 161, 255, 255)     # #4ea1ff
GRID = (58, 65, 80, 255)     # #3a4150

SIZE = 256
#: .icns 的源画布：macOS 的 512@2x 需要 1024px
ICNS_SOURCE = 1024
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")

#: Windows 图标内的尺寸档
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


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


def paths(out_dir: str | None = None) -> dict:
    base = out_dir or OUT_DIR
    return {
        "ico": os.path.join(base, "app.ico"),
        "png": os.path.join(base, "app.png"),
        "icns": os.path.join(base, "app.icns"),
    }


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成应用图标（ico / png / icns）")
    parser.add_argument("--check", action="store_true", help="只检查三个图标是否存在")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认 assets/）")
    args = parser.parse_args(argv)

    out = args.out_dir or OUT_DIR
    target = paths(out)

    if args.check:
        missing = [name for name, path in target.items() if not os.path.isfile(path)]
        if missing:
            print(f"[ERR] 缺少图标：{'、'.join(missing)}（跑 python tools/make_icon.py）")
            return 1
        print(f"[OK] 图标齐全：{'、'.join(target)}")
        return 0

    os.makedirs(out, exist_ok=True)
    base = _draw_icon(SIZE)
    base.save(target["ico"], sizes=ICO_SIZES)
    base.save(target["png"])

    # .icns 从 1024 画布出发：macOS 的 512@2x 就是 1024，拿 256 放大只会糊
    big = _draw_icon(ICNS_SOURCE) if ICNS_SOURCE != SIZE else base
    big.save(target["icns"], format="ICNS")

    for label, path in (("Windows 图标", target["ico"]),
                        ("预览图", target["png"]),
                        ("macOS 图标", target["icns"])):
        print(f"[OK] {label}已生成: {os.path.abspath(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
