"""card19 验收：Z-N 界面公式文案与算法实现逐项一致（文案对、算法对）。

覆盖：
    * 界面公式常数 ↔ ``zn_tuning`` 实际系数（防"代码对、说明错"再次发生）；
    * 未计算时是提示而不是空表头；计算后三行完整且当前控制律高亮；
    * 全篇符号统一：只用 ``×`` 与 ``/``，不出现 ``÷`` 与连除式；
    * 340 像素面板内公式不裁切（heightForWidth 实测）；
    * 界面 / 算法注释 / 用户手册三处口径一致。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from serialpid import zn_tuning  # noqa: E402
from serialpid.gui.zn_panel import (  # noqa: E402
    CLOSED_FORMULAS,
    GAIN_CONVERSION,
    OPEN_FORMULAS,
    ZnPanel,
)

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "docs")


def _join(formulas) -> str:
    return " ".join(" ".join(lines) for _law, lines in formulas)


CLOSED_TEXT = _join(CLOSED_FORMULAS)
OPEN_TEXT = _join(OPEN_FORMULAS)


# --------------------------------------------------------------------------- #
# 1. 文案常数 ↔ 实现系数
# --------------------------------------------------------------------------- #


def test_closed_formula_matches_implementation() -> None:
    """按文案手算能得到与 zn_closed_loop 相同的系数。"""
    ku, tu = 1.0, 1.0
    got = zn_tuning.zn_closed_loop(ku, tu)

    # P：Kp = 0.50 × Ku
    assert got["P"].kp == 0.50 * ku
    assert "0.50 × Ku" in CLOSED_TEXT

    # PI：Kp = 0.45 × Ku，Ti = Tu / 1.2（不是 0.45Ku/Tu/1.2 那种连除）
    assert got["PI"].kp == 0.45 * ku
    assert got["PI"].ti == tu / 1.2
    assert "0.45 × Ku" in CLOSED_TEXT
    assert "Tu / 1.2" in CLOSED_TEXT

    # PID：Kp = 0.60 × Ku，Ti = Tu / 2，Td = Tu / 8
    assert got["PID"].kp == 0.60 * ku
    assert got["PID"].ti == tu / 2
    assert got["PID"].td == tu / 8
    assert "0.60 × Ku" in CLOSED_TEXT
    assert "Tu / 2" in CLOSED_TEXT
    assert "Tu / 8" in CLOSED_TEXT


def test_open_formula_matches_implementation() -> None:
    """开环法文案与 zn_open_loop 一致。"""
    k, l, t = 1.0, 1.0, 1.0
    got = zn_tuning.zn_open_loop(k, l, t)

    assert got["P"].kp == t / (k * l)
    assert "T / (K × L)" in OPEN_TEXT

    assert got["PI"].kp == 0.90 * t / (k * l)
    assert got["PI"].ti == 3.33 * l
    assert "0.90 × T / (K × L)" in OPEN_TEXT
    assert "3.33 × L" in OPEN_TEXT

    assert got["PID"].kp == 1.20 * t / (k * l)
    assert got["PID"].ti == 2.0 * l
    assert got["PID"].td == 0.5 * l
    assert "1.20 × T / (K × L)" in OPEN_TEXT
    assert "2 × L" in OPEN_TEXT
    assert "0.5 × L" in OPEN_TEXT


def test_symbols_unified_no_ambiguous_division() -> None:
    """全篇只用 × 与 /：不出现 ÷，不出现把两个量混写的连除式。"""
    for text in (CLOSED_TEXT, OPEN_TEXT, GAIN_CONVERSION):
        assert "÷" not in text, f"出现除号 ÷：{text}"
        assert "Ku/Tu" not in text and "Ku / Tu" not in text, "比例系数与积分时间被混写"
    # 周期统一大写 Tu（不出现小写 tu 作为符号）
    assert "tu" not in CLOSED_TEXT.replace("Tu", "").lower()
    # 换算关系与实现一致：Ki = Kp / Ti，Kd = Kp × Td
    assert "Ki = Kp / Ti" in GAIN_CONVERSION and "Kd = Kp × Td" in GAIN_CONVERSION


# --------------------------------------------------------------------------- #
# 2. 结果区空态与高亮
# --------------------------------------------------------------------------- #


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_empty_state_shows_hint_not_empty_table() -> None:
    _app()
    panel = ZnPanel()
    assert panel._closed_stack.currentIndex() == 0, "未计算时应显示提示页"
    placeholder = panel._closed_stack.widget(0)
    assert "尚未计算" in placeholder.text() and "计算" in placeholder.text()
    assert panel._open_stack.currentIndex() == 0

    panel._ku.setValue(2.0)
    panel._tu.setValue(2.0)
    panel._on_closed_calc()
    assert panel._closed_stack.currentIndex() == 1, "计算后应切到表格页"
    for row in range(3):
        for col in range(6):
            assert panel._closed_table.item(row, col) is not None, "三行数据缺格"
            assert panel._closed_table.item(row, col).text() != ""


def test_highlight_follows_selected_law() -> None:
    _app()
    panel = ZnPanel()
    panel._ku.setValue(2.0)
    panel._tu.setValue(2.0)
    panel._on_closed_calc()

    # 默认选中 P → 第 0 行高亮
    assert panel._closed_table.item(0, 0).font().bold()
    assert not panel._closed_table.item(2, 0).font().bold()

    panel._closed_mode.setCurrentText("PID")
    assert panel._closed_table.item(2, 0).font().bold()
    assert not panel._closed_table.item(0, 0).font().bold()


# --------------------------------------------------------------------------- #
# 3. 340 像素面板内不裁切
# --------------------------------------------------------------------------- #


def test_formula_block_fits_narrow_panel() -> None:
    """340 像素面板内公式完整显示、不被裁切，且不把最小高度顶上去。"""
    _app()
    from PySide6.QtWidgets import QLabel

    from serialpid.gui.zn_panel import formula_lines

    panel = ZnPanel()
    panel.resize(340, 620)
    panel.show()
    _app().processEvents()

    labels = [l for l in panel.findChildren(QLabel) if l.objectName() == "zn_formula"]
    assert len(labels) == 2, "两个标签页各一段公式"
    for label in labels:
        assert label.width() > 250, f"公式区宽度异常：{label.width()}"
        need = label.heightForWidth(label.width())
        assert need <= label.height() + 2, (
            f"公式在 {label.width()}px 内被裁切：需要 {need}px，实际 {label.height()}px"
        )
        # 公式行（[P]/[PI]/[PID] 与续行）都不超过可用宽度，避免拆成参差的多行；
        # 说明句与换算句是自然语言，允许折行。
        for intro, formulas in (
            ("先用纯比例作用增大 Kp 至出现等幅振荡，记录此时的 Ku 与振荡周期 Tu。", CLOSED_FORMULAS),
            ("对开环对象施加阶跃，由反应曲线切线法辨识 K、L、T。", OPEN_FORMULAS),
        ):
            for line in formula_lines(formulas, intro, GAIN_CONVERSION):
                if not (line.startswith("[") or line.startswith("    ")):
                    continue
                width = label.fontMetrics().horizontalAdvance(line)
                assert width <= label.width(), f"单行 {width}px 超过可用 {label.width()}px：{line}"

    # 最小高度不得明显高于改动前（Windows 默认字体下 311）。不能用固定 311：
    # 行高随平台字体走（ubuntu 320 / macos-latest 338），而真正的回归是换行标签
    # 把"整段公式文本高度"报成最小高度（会跳到 450+），所以给字体度量差异留
    # 50px 余量，超了才是最小高度又文本顶上去。
    min_h = panel.minimumSizeHint().height()
    assert min_h <= 360, f"面板最小高度涨到 {min_h}，会给左栏加负担"


# --------------------------------------------------------------------------- #
# 4. 三处口径一致（界面 / 算法注释 / 用户手册）
# --------------------------------------------------------------------------- #


def _read_doc(name: str) -> str:
    with open(os.path.join(DOCS, name), encoding="utf-8") as handle:
        return handle.read()


def test_three_places_agree() -> None:
    guide = _read_doc("user_guide.md")
    module_doc = zn_tuning.__doc__ or ""

    for text, label in ((guide, "用户手册"), (module_doc, "算法模块注释")):
        assert "÷" not in text, f"{label}仍出现除号"
        for literal in ("0.50 × Ku", "0.45 × Ku", "0.60 × Ku", "Tu / 1.2", "Tu / 2", "Tu / 8"):
            assert literal in text, f"{label}缺少或写法不一致：{literal}"
        for literal in ("T / (K × L)", "0.90 × T / (K × L)", "1.20 × T / (K × L)", "3.33 × L", "0.5 × L"):
            assert literal in text, f"{label}缺少或写法不一致：{literal}"
        assert "Kd = Kp × Td" in text, f"{label}的 Kd 换算写法不一致"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
