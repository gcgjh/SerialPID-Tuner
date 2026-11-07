"""card12 交叉验证：界面导出路径与 csv_export 模块路径必须字节级一致。

这条用例的价值：以后谁再在界面里写第三套 CSV 实现，或把精度/编码/列序
改到只有其中一条路径，测试立刻报出来。
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from serialpid import csv_export  # noqa: E402
from serialpid.csv_export import export_csv, export_telemetry  # noqa: E402
from serialpid.model import TelemetryBuffer, TelemetrySample  # noqa: E402

SAMPLES = [
    # t_ms, sp, pv, out, err —— 覆盖负数、无法精确二进制表示的尾数、整数、u32 上限附近
    (1000, 25.0, 24.5, 60.0, -0.5),
    (1050, 25.0, 25.1234567, 61.2345678, -0.1234567),
    (1100, 30.0, 30.0, 0.0, 0.0),
    (1150, 25.5, 25.4999999, 99.9999999, -0.0000001),
    (4294967295, 88.0, -12.3456789, 123.456789, -100.8024693),
]


def _buffer() -> TelemetryBuffer:
    buf = TelemetryBuffer()
    for t, sp, pv, out, err in SAMPLES:
        buf.append(TelemetrySample(t, sp, pv, out, err))
    return buf


def test_ui_and_module_byte_identical() -> None:
    """界面路径 vs 模块路径（元组）vs 模块路径（对象）→ 三个文件字节完全相同。"""
    from PySide6.QtWidgets import QApplication

    from serialpid.gui.log_panel import LogPanel

    app = QApplication.instance() or QApplication([])  # noqa: F841
    panel = LogPanel(_buffer())

    with tempfile.TemporaryDirectory() as d:
        ui_path = os.path.join(d, "ui.csv")
        mod_path = os.path.join(d, "module.csv")
        obj_path = os.path.join(d, "module_obj.csv")

        rows = panel.export_to(ui_path)
        assert rows == len(SAMPLES)
        assert export_csv(SAMPLES, mod_path) == len(SAMPLES)
        assert export_telemetry(_buffer().samples(), obj_path) == len(SAMPLES)

        data_ui = open(ui_path, "rb").read()
        data_mod = open(mod_path, "rb").read()
        data_obj = open(obj_path, "rb").read()

    assert data_ui == data_mod, "界面导出与模块导出字节不一致"
    assert data_ui == data_obj, "样本对象与元组两条路径字节不一致"


def test_export_format_contract() -> None:
    """BOM、表头、六位小数、CRLF、列数 —— 与 Excel 实际表现核对过的定稿格式。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fmt.csv")
        export_telemetry(_buffer().samples(), path)
        raw = open(path, "rb").read()

    assert raw.startswith(b"\xef\xbb\xbf"), "缺少 UTF-8 BOM，Excel 双击会乱码"
    assert raw.count(b"\r\n") == len(SAMPLES) + 1, "换行必须是 CRLF（表头+数据行）"

    text = raw.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0] == "t_ms,SP,PV,OUT,err", f"表头不符：{lines[0]!r}"
    assert lines[-1] == ""  # 文件以换行结尾

    for line in lines[1:-1]:
        cells = line.split(",")
        assert len(cells) == 5, f"列数不是 5：{line!r}"
        # t_ms 整数、其余六位小数
        assert cells[0].lstrip("-").isdigit(), f"t_ms 非整数：{cells[0]!r}"
        for cell in cells[1:]:
            assert len(cell.split(".")[1]) == 6, f"小数位不是 6：{cell!r}"

    # 关键对照：旧界面实现是四位小数（99 字节），这里必须是六位
    assert lines[1] == "1000,25.000000,24.500000,60.000000,-0.500000"
    assert "24.5000," not in text


def test_no_second_implementation_in_gui() -> None:
    """界面代码里不允许出现 csv 模块导入或 writer/writerow 调用（防第三次分叉）。

    只看 AST 里的真实代码，不看文档字符串 —— 文档里提到"不要写 csv.writer"
    正是要保留的警示。
    """
    import ast

    from serialpid.gui import log_panel

    tree = ast.parse(inspect.getsource(log_panel))
    module_imports: set[str] = set()
    call_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute):
            call_attrs.add(node.attr)

    assert "csv" not in module_imports, "log_panel 又直接 import csv 了"
    assert not ({"writer", "writerow"} & call_attrs), "log_panel 又出现了内联 CSV 写入"


def test_export_error_is_logged_not_raised() -> None:
    """导出失败（非法路径）应记日志并返回 0，不能让界面回调崩掉。"""
    from PySide6.QtWidgets import QApplication

    from serialpid.gui.log_panel import LogPanel

    app = QApplication.instance() or QApplication([])  # noqa: F841
    panel = LogPanel(_buffer())
    with tempfile.TemporaryDirectory() as d:
        blocker = os.path.join(d, "blocker")   # 用文件充当父目录，制造真实写失败
        open(blocker, "w").close()
        bad_path = os.path.join(blocker, "x.csv")
        assert panel.export_to(bad_path) == 0
    assert "导出失败" in panel._view.toPlainText()


def test_module_docstring_states_contract() -> None:
    """格式契约写在模块文档里（用户/后继者不必猜精度）。"""
    doc = csv_export.__doc__ or ""
    assert "六位小数" in doc and "UTF-8" in doc and "t_ms,SP,PV,OUT,err" in doc
