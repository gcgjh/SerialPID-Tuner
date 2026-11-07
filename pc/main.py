"""PyInstaller / 脚本直跑入口（薄启动器）。

模块入口为 ``serialpid.main``（支持 ``python -m serialpid.main``）；
本文件把 ``pc/`` 加入 ``sys.path`` 后转发过去，便于打包工具以单文件脚本方式定位。

    python pc/main.py            # 开发直跑
    pyinstaller pc/main.py ...   # 打包
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serialpid.main import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
