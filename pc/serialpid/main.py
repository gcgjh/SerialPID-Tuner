"""上位机入口：python -m serialpid.main（在 pc/ 目录下运行）。"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from .config import default_config_path
from .gui.main_window import MainWindow
from .gui.theme import apply_dark


def is_first_run() -> bool:
    """配置不存在 => 第一次运行（必须在主窗口读配置前判断）。"""
    return not os.path.exists(default_config_path())


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SerialPID-Tuner")
    apply_dark(app)
    # 首次运行判定必须在构造主窗口之前：主窗口会读配置，配置文件缺失时
    # config.load_config 会顺手把默认配置写出来，之后再判就永远"不是第一次"了。
    window = MainWindow(show_onboarding=is_first_run())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
