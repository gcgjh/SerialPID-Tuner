"""GUI 离屏冒烟测试（无显示器环境自动跳过）。

运行：在 pc/ 目录下  python tests/test_gui_smoke.py
覆盖：主窗口装配、遥测喂入、阶跃指标分析、Z-N 计算与信号下发通路。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError:
    print("SKIP PySide6 不可用")
    sys.exit(0)

from serialpid.gui.main_window import MainWindow  # noqa: E402
from serialpid.gui.theme import apply_dark  # noqa: E402
from serialpid.protocol import PacketType, pack_telemetry  # noqa: E402


def main() -> None:
    app = QApplication(sys.argv)
    apply_dark(app)
    win = MainWindow()

    # 喂入一段带阶跃的遥测曲线：前 8 点 SP=20，之后跳到 SP=25
    for i in range(30):
        t = i * 50
        if i < 8:
            sp, pv = 20.0, 19.7
        else:
            sp = 25.0
            k = i - 8
            pv = 20.0 + min(5.0, k * 0.6)
            if k == 6:
                pv = 26.2  # 轻微超调
        win._on_frame(
            PacketType.TELEMETRY,
            pack_telemetry(t, sp, pv, 30.0, sp - pv),
        )

    # Z-N 闭环法计算 + 选中 PID 下发（信号通路，不真的连串口）
    win._zn._ku.setValue(2.0)
    win._zn._tu.setValue(2.0)
    win._zn._on_closed_calc()
    assert "PID" in win._zn._last_closed
    win._zn._send(win._zn._last_closed, "PID")

    # 阶跃面板离线分析
    win._step._analyze_curve()
    assert win._step._metrics is not None
    print("metrics:", win._step._metrics.summary().replace("\n", " | "))

    # 清空通路
    win._clear_records()
    assert len(win._step._samples) == 0

    print("PASS gui_smoke")


if __name__ == "__main__":
    main()
