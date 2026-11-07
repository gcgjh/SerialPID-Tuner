"""上位机入口：python -m serialpid.main（在 pc/ 目录下运行）。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .gui.main_window import MainWindow
from .gui.theme import apply_dark


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("SerialPID-Tuner")
    apply_dark(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
