"""PyInstaller / 脚本直跑入口（薄启动器）。

模块入口为 ``serialpid.main``（支持 ``python -m serialpid.main``）；
本文件把 ``pc/`` 加入 ``sys.path`` 后转发过去，便于打包工具以单文件脚本方式定位。

    python pc/main.py                      # 开发直跑
    python pc/main.py --selftest --json    # 启动自检（card22，不加载 Qt）
    pyinstaller pc/main.py ...             # 打包

``--selftest`` 必须在本文件里分流、**早于** ``serialpid.main`` 的导入：后者会拉起
PySide6 与整套界面模块，若 Qt 插件缺失会直接崩，等于让自检本身也没机会报错。
打包脚本正是用 ``dist\\SerialPID-Tuner.exe --selftest --json --out <文件>`` 验证出包
完整性的（windowed 进程没有 stdout，结果必须落盘）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SELFTEST_FLAG = "--selftest"


if __name__ == "__main__":
    if _SELFTEST_FLAG in sys.argv:
        from serialpid.selftest import main as selftest_main

        rest = sys.argv[sys.argv.index(_SELFTEST_FLAG) + 1:]
        sys.exit(selftest_main(rest))

    from serialpid.main import main

    sys.exit(main())
