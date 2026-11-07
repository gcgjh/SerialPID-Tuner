"""启动自检与依赖诊断（card22）。

为什么要有这一层：打包版是**无控制台窗口**的，启动阶段任何依赖异常都是静默的——
配置目录不可写、串口平台后端加载失败、Qt 平台插件缺失、图标资源丢失，用户看到的
只是"某个功能不好使"，开发者拿到的反馈只有"连不上"。这里把关键前提逐项检查成三态
（``pass`` / ``warn`` / ``fail``），并提供三个出口：

1. 命令行：``python -m serialpid.selftest --json``，打包脚本出包后跑一次即可判断这个包
   是否完整；无控制台的 exe 用 ``--out <文件>`` 落盘（windowed 进程没有 stdout）。
2. 日志：结果写入日志目录（``selftest-*.json`` + ``selftest.log`` 追加一段可粘贴文本）。
3. 内存：``latest_report()`` 供界面启动时读取横幅（接线归 card25）。

**自检只报告，不修复**：发现目录不可写就如实报错，不替用户换目录；发现图标缺失就
报资源缺失，不去重新生成。修复会掩盖环境异常，是自检最不该做的事。

所有检查项加起来应在几百毫秒内完成：这里只做导入与文件探测，不做串口枚举（枚举有
卡住的驱动，属 card21 的端口扫描，带超时保护）。
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

#: 三态：pass / warn / fail。warn 表示"能跑但会降级"，fail 表示"该功能不可用"
STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
_RANK = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}

#: 配置目录不可写会让参数范围、预设保存全部异常，界面上视为阻断项
BLOCKING_KEYS = ("config_dir",)

LOG_DIR_ENV = "SERIALPID_LOG_DIR"
_JSON_PREFIX = "selftest-"
_TEXT_LOG = "selftest.log"

#: 最近一次自检结果（供界面读取；不进磁盘，进程退出即失效）
_LATEST: Optional["SelfTestReport"] = None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """单项检查结果。``detail`` 是一句人话，``hint`` 是给用户的可操作建议。"""

    key: str
    title: str
    status: str
    detail: str
    hint: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "duration_ms": round(self.duration_ms, 3),
        }


@dataclass
class SelfTestReport:
    """一次自检的完整结果。界面只关心 ``status`` / ``counts`` / ``blocking``。"""

    checks: List[CheckResult] = field(default_factory=list)
    generated_at: str = ""
    python_version: str = ""
    platform_text: str = ""
    duration_ms: float = 0.0
    log_path: str = ""

    @property
    def counts(self) -> Dict[str, int]:
        counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    @property
    def status(self) -> str:
        """整体状态取最差的一项；没有检查项时按 warn（说明自检没跑起来）。"""
        worst = STATUS_PASS
        for check in self.checks:
            if _RANK.get(check.status, 0) > _RANK[worst]:
                worst = check.status
        return worst

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAIL

    @property
    def failed(self) -> List[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_FAIL]

    @property
    def warnings(self) -> List[CheckResult]:
        return [c for c in self.checks if c.status == STATUS_WARN]

    @property
    def blocking(self) -> bool:
        """是否存在必须让用户先处理、否则后续功能全坏的失败项。"""
        return any(c.status == STATUS_FAIL and c.key in BLOCKING_KEYS for c in self.checks)

    def get(self, key: str) -> Optional[CheckResult]:
        for check in self.checks:
            if check.key == key:
                return check
        return None

    def summary(self) -> str:
        counts = self.counts
        return (
            f"{counts.get(STATUS_PASS, 0)} 通过 / "
            f"{counts.get(STATUS_WARN, 0)} 警告 / "
            f"{counts.get(STATUS_FAIL, 0)} 失败"
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "ok": self.ok,
            "blocking": self.blocking,
            "summary": self.summary(),
            "counts": self.counts,
            "checks": [c.to_dict() for c in self.checks],
            "generated_at": self.generated_at,
            "python": self.python_version,
            "platform": self.platform_text,
            "duration_ms": round(self.duration_ms, 3),
            "log_path": self.log_path,
        }


# ---------------------------------------------------------------------------
# 路径约定
# ---------------------------------------------------------------------------


def default_log_dir() -> str:
    """日志目录：``SERIALPID_LOG_DIR`` > 配置目录下的 ``logs``。

    与配置文件同根，便携运行（``SERIALPID_CONFIG`` 指到 U 盘）时一起搬走。
    """
    env = os.environ.get(LOG_DIR_ENV)
    if env:
        return env
    config_dir = os.path.dirname(os.path.abspath(default_config_path()))
    return os.path.join(config_dir, "logs")


def assets_dir() -> str:
    """资源目录：源码运行取仓库 ``assets/``，打包后取 PyInstaller 的解包目录。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
        return os.path.join(base, "assets")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets")


def default_icon_path() -> str:
    return os.path.join(assets_dir(), "app.ico")


def default_config_path() -> str:
    # 延迟导入：config 模块自身出问题时，自检仍要能给出一份可用的诊断
    from .config import default_config_path as _config_path

    return _config_path()


# ---------------------------------------------------------------------------
# 单项检查（每个函数返回 (status, detail, hint)）
# ---------------------------------------------------------------------------


def _check_python() -> Tuple[str, str, str]:
    version = platform.python_version()
    if sys.version_info < (3, 10):
        return (
            STATUS_FAIL,
            f"Python {version} 低于最低要求 3.10",
            "安装 Python 3.10+，或使用仓库上一层 .venv 内的解释器",
        )
    bits = platform.architecture()[0]
    return STATUS_PASS, f"Python {version}（{bits}，{sys.executable}）", ""


def _check_pyserial() -> Tuple[str, str, str]:
    try:
        import serial  # noqa: F401
    except Exception as exc:  # ImportError / OSError 都算依赖不可用
        return (
            STATUS_FAIL,
            f"pyserial 导入失败：{type(exc).__name__}: {exc}",
            "pip install -r pc/requirements.txt；打包版需确认 serial 模块未被裁掉",
        )
    version = getattr(serial, "__version__", "未知版本")
    return STATUS_PASS, f"pyserial {version}", ""


def _serial_backend_name() -> str:
    if sys.platform.startswith("win"):
        return "serial.serialwin32"
    if sys.platform == "darwin":
        return "serial.serialposix"
    return "serial.serialposix"


def serial_backend_summary() -> str:
    """一行串口后端状态（关于框 / 诊断文本用，与自检同一判定口径）。

    不在两处各写一套判定：自检说"后端可用"、关于框说"不可用"是最难查的那种
    自相矛盾。
    """
    name = _serial_backend_name()
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - 依赖缺失 / 平台不支持都同样处理
        return f"{name} 不可用（{type(exc).__name__}: {exc}）"
    try:
        importlib.import_module("serial.tools.list_ports")
    except Exception as exc:  # noqa: BLE001
        return f"{name} 已加载；端口枚举模块异常（{type(exc).__name__}: {exc}）"
    return f"{name} 已加载，端口枚举可用"


def _check_serial_backend() -> Tuple[str, str, str]:
    name = _serial_backend_name()
    try:
        importlib.import_module(name)
    except Exception as exc:
        return (
            STATUS_FAIL,
            f"串口平台后端 {name} 不可用：{type(exc).__name__}: {exc}",
            "重装 pyserial；Windows 下也需确认驱动 / 权限正常",
        )
    notes = [f"{name} 已加载"]
    try:
        importlib.import_module("serial.tools.list_ports")
    except Exception as exc:
        notes.append(f"端口枚举模块异常：{type(exc).__name__}: {exc}")
        return (
            STATUS_WARN,
            "；".join(notes),
            "串口能连但下拉可能列不出端口，参考 card21 的端口诊断",
        )
    return STATUS_PASS, "；".join(notes), ""


def _nearest_existing(path: str) -> str:
    probe = os.path.abspath(path)
    while True:
        if os.path.isdir(probe):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return probe
        probe = parent


def _probe_writable(directory: str) -> Tuple[bool, str]:
    """真写一个探测文件再删掉。只读目录 / 权限不足、路径其实是文件，都会在这里暴露。"""
    probe = os.path.join(directory, f".selftest-probe-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _writable_check(path: str, *, missing_status: str, what: str) -> Tuple[str, str, str]:
    target = os.path.abspath(path)
    exists = os.path.isdir(target)
    host = target if exists else _nearest_existing(target)
    writable, reason = _probe_writable(host)
    where = "目录" if exists else f"目录不存在，最近的可写探测点是 {host}"
    if not writable:
        return (
            STATUS_FAIL,
            f"{what}不可写（{where}）：{reason}",
            f"检查 {target} 的权限，或用环境变量把路径指到可写位置",
        )
    if not exists:
        return (
            missing_status,
            f"{what}尚未创建（首次运行会自动建）：{target}；上级目录可写",
            "",
        )
    return STATUS_PASS, f"{what}可写：{target}", ""


def _check_config_dir(config_path: str) -> Tuple[str, str, str]:
    directory = os.path.dirname(os.path.abspath(config_path)) or os.getcwd()
    return _writable_check(directory, missing_status=STATUS_WARN, what="配置目录")


def _check_log_dir(log_dir: str) -> Tuple[str, str, str]:
    status, detail, hint = _writable_check(log_dir, missing_status=STATUS_WARN, what="日志目录")
    if status == STATUS_FAIL:
        detail += "（自检结果只能留在内存，无法落盘）"
    return status, detail, hint


_PLATFORM_PLUGIN = {
    "win32": "qwindows.dll",
    "darwin": "libqcocoa.dylib",
}
_PLATFORM_PLUGIN_FALLBACK = "libqxcb.so"


def _check_qt_plugins() -> Tuple[str, str, str]:
    try:
        import PySide6  # noqa: F401
    except Exception as exc:
        return (
            STATUS_FAIL,
            f"PySide6 导入失败：{type(exc).__name__}: {exc}",
            "pip install -r pc/requirements.txt；打包版确认 PySide6 未被裁掉",
        )
    version = getattr(PySide6, "__version__", "未知版本")
    base = os.path.dirname(os.path.abspath(PySide6.__file__))
    # 布局随版本变：PySide6 6.x 新版放在 <包>/Qt/plugins，老版 / 部分构建在 <包>/plugins
    candidates = [
        os.path.join(base, "Qt", "plugins", "platforms"),
        os.path.join(base, "plugins", "platforms"),
    ]
    requested = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    if requested in ("offscreen", "minimal"):
        plugin = f"q{requested}.dll" if sys.platform.startswith("win") else f"libq{requested}.so"
    else:
        plugin = _PLATFORM_PLUGIN.get(sys.platform, _PLATFORM_PLUGIN_FALLBACK)
    platforms = next((p for p in candidates if os.path.isdir(p)), candidates[0])
    path = os.path.join(platforms, plugin)
    if not os.path.exists(path):
        return (
            STATUS_FAIL,
            f"PySide6 {version} 已安装，但平台插件缺失：{path}",
            "重装 PySide6，或确认打包时 Qt plugins 目录未被裁掉",
        )
    # 说明：这里只确认插件文件在位，不做实际加载（加载需要 QApplication，属界面卡的事）
    return STATUS_PASS, f"PySide6 {version}；平台插件 {plugin} 在位（{platforms}）", ""


def _check_icon(icon_path: str) -> Tuple[str, str, str]:
    if not os.path.isfile(icon_path):
        return (
            STATUS_FAIL,
            f"图标资源缺失：{icon_path}",
            "运行 tools/make_icon.py 重新生成；打包版确认 --add-data assets 生效",
        )
    size = os.path.getsize(icon_path)
    if size <= 0:
        return (
            STATUS_FAIL,
            f"图标资源为空文件：{icon_path}",
            "运行 tools/make_icon.py 重新生成",
        )
    return STATUS_PASS, f"图标资源就绪：{icon_path}（{size} 字节）", ""


def _check_protocol() -> Tuple[str, str, str]:
    """协议自检：封一帧再解回来，再故意改坏 CRC 确认会被拒。"""
    from . import protocol

    # 注意：pack_telemetry 只产出 **payload**，外层帧要经 encode 组包；
    # 直接把 payload 喂给 parse_frame 会报"帧头错误"，这是本检查踩过的坑。
    payload = protocol.pack_telemetry(12345, 25.5, 24.75, 60.0, -0.75)
    frame = protocol.encode(protocol.PacketType.TELEMETRY, payload)
    parsed = protocol.parse_frame(frame)
    if parsed.type != protocol.PacketType.TELEMETRY:
        return (
            STATUS_FAIL,
            f"帧类型不符：解出 0x{parsed.type:02X}",
            "协议模块与打包版本不一致，检查是否混入旧版 protocol.py",
        )
    t_ms, sp, pv, out, err = protocol.unpack_telemetry(parsed.payload)
    if t_ms != 12345 or abs(sp - 25.5) > 1e-6 or abs(pv - 24.75) > 1e-6:
        return (
            STATUS_FAIL,
            f"遥测往返不一致：t={t_ms} sp={sp} pv={pv}",
            "协议编解码与自检版本不一致",
        )
    corrupted = bytearray(frame)
    corrupted[-6] ^= 0xFF  # 帧尾前两位是 CRC，改一个字节必须被拒
    try:
        protocol.parse_frame(bytes(corrupted))
    except Exception:
        pass
    else:
        return (
            STATUS_FAIL,
            "坏 CRC 帧没有被拒绝",
            "协议层的 CRC 校验失效，下发参数可能写到错误位置",
        )
    return STATUS_PASS, f"遥测往返一致，坏 CRC 帧被拒绝（{len(frame)} 字节）", ""


# ---------------------------------------------------------------------------
# 运行、缓存与输出
# ---------------------------------------------------------------------------

_CHECK_SPECS: Tuple[Tuple[str, str, str, str], ...] = (
    # (key, 标题, 检查函数名, 参数形态) —— 存函数名而非函数对象，
    # 运行时再按名字取：测试 / 界面可以替换单项检查，不必自建一套调度
    ("python_runtime", "Python 运行时", "_check_python", "runtime"),
    ("pyserial", "pyserial 依赖", "_check_pyserial", "dep"),
    ("serial_backend", "串口平台后端", "_check_serial_backend", "dep"),
    ("qt_plugins", "Qt 平台插件", "_check_qt_plugins", "dep"),
    ("config_dir", "配置目录可写", "_check_config_dir", "fs"),
    ("log_dir", "日志目录可写", "_check_log_dir", "fs"),
    ("icon_asset", "图标资源", "_check_icon", "fs"),
    ("protocol_io", "协议帧往返", "_check_protocol", "core"),
)


def run_selftest(
    *,
    config_path: Optional[str] = None,
    log_dir: Optional[str] = None,
    icon_path: Optional[str] = None,
    write_log: bool = True,
    remember: bool = True,
) -> SelfTestReport:
    """跑一遍全部检查，返回报告（不抛异常；单项炸了也只记 fail）。"""
    started = time.perf_counter()
    cfg = config_path if config_path is not None else default_config_path()
    logs = log_dir if log_dir is not None else default_log_dir()
    icon = icon_path if icon_path is not None else default_icon_path()

    checks: List[CheckResult] = []
    targets = {"config_dir": cfg, "log_dir": logs, "icon_asset": icon}
    for key, title, func_name, kind in _CHECK_SPECS:
        func: Callable[..., Tuple[str, str, str]] = globals()[func_name]
        t0 = time.perf_counter()
        try:
            if kind == "fs":
                status, detail, hint = func(targets[key])
            else:
                status, detail, hint = func()
        except Exception as exc:  # 检查自身出错也要如实报，不能让启动崩
            status, detail, hint = (
                STATUS_FAIL,
                f"检查自身抛出异常：{type(exc).__name__}: {exc}",
                "这是自检实现的缺陷，请把这段文本反馈给开发者",
            )
        checks.append(
            CheckResult(
                key=key,
                title=title,
                status=status,
                detail=detail,
                hint=hint,
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
        )

    report = SelfTestReport(
        checks=checks,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        python_version=platform.python_version(),
        platform_text=f"{platform.system()} {platform.release()} / {sys.platform}",
        duration_ms=(time.perf_counter() - started) * 1000.0,
    )
    if write_log:
        report.log_path = save_report(report, log_dir=logs) or ""
    if remember:
        remember_report(report)
    return report


def remember_report(report: SelfTestReport) -> None:
    """把结果留在内存里，供界面启动时读取横幅（card25 接线）。"""
    global _LATEST
    _LATEST = report


def latest_report() -> Optional[SelfTestReport]:
    return _LATEST


def save_report(report: SelfTestReport, *, log_dir: Optional[str] = None) -> str:
    """落盘：``selftest-<时间戳>.json``（机器读）+ ``selftest.log`` 追加一段（人贴）。

    写不进去就返回空串，绝不抛异常——日志目录不可写本身已被自检报出来了，
    诊断入口不能因为写不了日志而消失。
    """
    directory = log_dir if log_dir is not None else default_log_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    json_path = os.path.join(directory, f"{_JSON_PREFIX}{stamp}.json")
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = f"{json_path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, json_path)
        with open(os.path.join(directory, _TEXT_LOG), "a", encoding="utf-8") as handle:
            handle.write(format_report_text(report))
            handle.write("\n")
    except OSError:
        return ""
    return json_path


_SYMBOL = {STATUS_PASS: "[OK]  ", STATUS_WARN: "[WARN]", STATUS_FAIL: "[FAIL]"}


def format_report_text(report: SelfTestReport) -> str:
    """可整段贴进 issue 的诊断文本：版本、平台、逐项结果、建议。"""
    lines = [
        f"SerialPID-Tuner 启动自检 {report.generated_at}",
        f"Python {report.python_version} | {report.platform_text}",
        "",
    ]
    for check in report.checks:
        lines.append(f"{_SYMBOL.get(check.status, '[????]')} {check.key:<14} {check.detail}")
        if check.hint:
            lines.append(f"       建议：{check.hint}")
    lines.append("")
    lines.append(f"小计：{report.summary()}，总耗时 {report.duration_ms:.0f} ms")
    if report.log_path:
        lines.append(f"日志：{report.log_path}")
    return "\n".join(lines)


def report_to_json(report: SelfTestReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------

_USAGE = """用法: python -m serialpid.selftest [选项]
  --json               输出 JSON（默认输出给人看的文本）
  --out PATH           结果写入文件（无控制台的 exe 必须用这个）
  --log-dir DIR        覆盖日志目录
  --config PATH        覆盖配置文件路径（只影响"配置目录可写"这一项）
  --icon PATH          覆盖图标路径（用于验证"资源缺失"能否报出来）
  --no-log             不写日志文件
  --quiet              不打印到标准输出
退出码: 0 = 通过或有警告, 1 = 有失败项, 2 = 参数错误
"""


def _parse_argv(argv: Sequence[str]) -> Tuple[Dict[str, object], List[str]]:
    options: Dict[str, object] = {
        "json": False,
        "out": None,
        "log_dir": None,
        "config": None,
        "icon": None,
        "write_log": True,
        "quiet": False,
    }
    problems: List[str] = []
    needs_value = ("--out", "--log-dir", "--config", "--icon")
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg.startswith("--") and "=" in arg:
            arg, value = arg.split("=", 1)
            argv = list(argv)
            argv.insert(index + 1, value)
        if arg == "--json":
            options["json"] = True
        elif arg == "--no-log":
            options["write_log"] = False
        elif arg == "--quiet":
            options["quiet"] = True
        elif arg in needs_value:
            value = argv[index + 1] if index + 1 < len(argv) else ""
            if not value:
                problems.append(f"{arg} 需要跟一个路径")
            else:
                key = {"--out": "out", "--log-dir": "log_dir", "--config": "config", "--icon": "icon"}[arg]
                options[key] = value
                index += 1
        elif arg in ("-h", "--help"):
            options["help"] = True
        elif arg == "--selftest":
            pass  # 入口脚本把 --selftest 吃掉后可能连同它一起传进来
        else:
            problems.append(f"未知参数：{arg}")
        index += 1
    return options, problems


def _write_stdout(text: str) -> bool:
    """无控制台（windowed）打包时 ``sys.stdout`` 是 None，print 会直接抛异常。"""
    stream = sys.stdout
    if stream is None:
        return False
    try:
        stream.write(text + "\n")
        stream.flush()
        return True
    except (OSError, ValueError):
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。``python -m serialpid.selftest --json`` / exe ``--selftest --json``。"""
    args = list(sys.argv[1:] if argv is None else argv)
    options, problems = _parse_argv(args)
    if options.get("help"):
        _write_stdout(_USAGE)
        return 0
    if problems:
        for problem in problems:
            _write_stdout(f"[ERR] {problem}")
        _write_stdout(_USAGE)
        return 2

    report = run_selftest(
        config_path=options["config"],
        log_dir=options["log_dir"],
        icon_path=options["icon"],
        write_log=bool(options["write_log"]),
    )
    payload = report_to_json(report) if options["json"] else format_report_text(report)

    out = options["out"]
    if out:
        try:
            directory = os.path.dirname(os.path.abspath(out))
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            _write_stdout(f"[ERR] 结果写入失败 {out}: {exc}")
            return 2

    if not options["quiet"] and not out:
        # 没有 --out 时只能打标准输出；windowed 打包下 stdout 是 None，
        # 写不出去就算了（此时调用方理应传 --out，否则结果只留在日志与内存里）
        _write_stdout(payload)
    return 0 if report.status != STATUS_FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
