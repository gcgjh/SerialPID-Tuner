"""主窗口：连接管理 + 面板组装 + 帧分发。

布局要点（card16）：
    左栏是高密度控件区（参数 / 阶跃 / Z-N / 日志），纵向需求超过 1000px。
    1366×768 的现场办公本上，若让布局把最小高度一路顶上去，窗口会被撑到
    1167px，底部日志与状态栏全在屏外，而且拖不动缩不小。
    因此左栏整体放进 QScrollArea：窗口保持合理最小尺寸，内容按需滚动；
    日志另设最小高度（约 8 行），不再被拉伸系数挤成一行。
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut

from ..comm import (
    DROP_CLOSED,
    RECONNECT_MAX_ATTEMPTS,
    brief_disconnect,
    describe_disconnect_message,
    reconnect_delay,
)
from ..comm_qt import CommWorker
from ..config import load_config, save_config
from ..frame_log import ParseErrorLogger, UnknownFrameLogger
from ..messages import (
    EMPTY_PORT_TEXT,
    empty_port_message,
    explain_serial_error,
    join_names,
    parse_error_message,
    port_scan_failed_message,
    selftest_message,
    serial_open_message,
    UserMessage,
    worker_error_message,
)
from ..model import TelemetryBuffer, TelemetrySample
from ..port_scan import PortScanResult, diagnose, scan_ports
from ..protocol import (
    PacketType,
    error_line,
    pack_pid_set,
    payload_error_reason,
    unpack_error,
    unpack_pid_ack,
    unpack_pong,
    unpack_telemetry,
    unpack_step_event,
)
from .frame_log_dialog import FrameLogDialog
from .fault_panel import FaultPanel
from .link_settings import LinkSettingsDialog
from .link_status import LinkStatusWidget
from .log_panel import LogPanel
from .param_panel import ParamPanel
from .plot_panel import PlotPanel
from .sim_panel import SimPanel
from .step_panel import StepPanel
from .theme import COLORS
from .zn_panel import ZnPanel

# 左栏宽度：面板内部布局按这个宽度排布（改宽度时确认各面板不依赖固定间距）
LEFT_PANEL_WIDTH = 340
LEFT_PANEL_MIN_WIDTH = 300
LEFT_PANEL_MAX_WIDTH = 520
# 窗口最小尺寸：让内容去滚动，而不是把窗口撑出屏幕（1366×768 现场本可完整放下）
MIN_WINDOW_WIDTH = 960
MIN_WINDOW_HEIGHT = 640
# 恢复尺寸时的兜底上限：屏幕本身比最小窗口还小时（离屏渲染 / 极小平板），
# 不用屏幕尺寸当上限，改用这个理智上限，免得把记忆的尺寸夹成不可用。
MAX_WINDOW_WIDTH = 4096
MAX_WINDOW_HEIGHT = 2160
# 日志至少能看见这么多行，避免被拉伸系数吃成一行
LOG_MIN_LINES = 8

# ---- 通信默认参数（card28；可在 config.json 的 comm 段覆盖） ----
#: 心跳间隔：**默认关闭**（card32 定的口径）。低质量链路开了心跳可能频繁
#: 断开重连、干扰演示，所以现场要先确认"不开心跳也能连"，再按需打开。
#: 0 表示关闭心跳，退回纯空闲断流判定（与旧版行为完全一致）。
DEFAULT_HEARTBEAT_INTERVAL_S = 0.0
#: 连续多少次 PING 没等到 PONG 才判链路失效（且同窗口内没有任何遥测）
DEFAULT_HEARTBEAT_MAX_MISSED = 3
#: 心跳判死窗口之外再留 1 秒，作为"彻底静默"的兜底
HEARTBEAT_IDLE_MARGIN_S = 1.0
#: 帧日志环形缓冲容量（帧）
DEFAULT_TRACE_CAPACITY = 2000
#: 链路质量刷新周期（毫秒）：1 Hz 够看清趋势，也不跟 20 Hz 遥测抢主线程
LINK_STATUS_REFRESH_MS = 1000
#: 关掉心跳时的空闲判死窗口（与协议文档 §4.1 的 1.0 s 一致）
IDLE_TIMEOUT_NO_HEARTBEAT_S = 1.0
#: 仿真链路劣化默认值（card32 / card33；只作用于「内置模拟器」通道）
#: 三个开关默认全零是硬要求：正常演示路径必须零开销、直通。
DEFAULT_SIM_DEGRADATION = {
    "enabled": False,
    "drop_down": 0.0,
    "bad_crc_down": 0.0,
    "disconnect_after_s": 0.0,
}

#: 通道选项。前两个是真实通道，第三项是进程内仿真（card25）
CHANNEL_TCP = "TCP"
CHANNEL_SERIAL = "Serial"
CHANNEL_SIM = "内置模拟器"
#: 空/异常时的占位文案：绝不出现可被误认为真设备的假端口号
#: （文案与三段式提示共用 ``messages.EMPTY_PORT_TEXT`` 这一处定义）
PORT_EMPTY_TEXT = EMPTY_PORT_TEXT
PORT_ERROR_TEXT = "端口枚举失败（点「诊断」看原因）"
SIM_TARGET_TEXT = "进程内仿真 · 无需端口"


def _explain_serial_error(message: str) -> str:
    """把串口打开失败的一句话翻成"下一步该做什么"（规则表在 messages 模块）。

    保留这个薄壳是为了既有调用方与测试；真正的判定与文案只有一份实现。
    """
    return explain_serial_error(message)


def comm_settings(config: object = None) -> dict:
    """从配置的 ``comm`` 段读出心跳/判死参数；任何非法值都回默认。

    口径（card28 要求"心跳间隔与超时次数可配置"）：

    * ``heartbeat_interval_s``：心跳间隔，0 表示关闭心跳；
    * ``heartbeat_max_missed``：连续多少次没 PONG 才判死；
    * ``idle_timeout_s``：可选，彻底静默的兜底窗口；不给就按
      ``间隔 × 次数 + 1 s`` 推算，保证"心跳先给出可解释的原因"；
    * ``trace_capacity``：帧日志环形缓冲容量；
    * ``sim_degradation``：内置模拟器的链路劣化（card32：丢帧 / 坏帧比例）。

    心跳关闭时兜底窗口回落到协议文档 §4.1 的 1.0 s，行为与旧版一致。
    """

    def _number(raw: object, default: float, low: float, high: float) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return default
        if value != value or value < low or value > high:  # NaN / 越界
            return default
        return value

    interval = DEFAULT_HEARTBEAT_INTERVAL_S
    missed = DEFAULT_HEARTBEAT_MAX_MISSED
    capacity = DEFAULT_TRACE_CAPACITY
    idle_raw = None
    degradation = dict(DEFAULT_SIM_DEGRADATION)

    section = config.get("comm") if isinstance(config, dict) else None
    if isinstance(section, dict):
        interval = _number(section.get("heartbeat_interval_s"), interval, 0.0, 60.0)
        missed = int(_number(section.get("heartbeat_max_missed"), missed, 1.0, 10.0))
        capacity = int(_number(section.get("trace_capacity"), capacity, 100.0, 20000.0))
        if "idle_timeout_s" in section:
            idle_raw = section.get("idle_timeout_s")
        raw_deg = section.get("sim_degradation")
        if isinstance(raw_deg, dict):
            degradation = {
                "enabled": bool(raw_deg.get("enabled", False)),
                "drop_down": _number(raw_deg.get("drop_down"), 0.0, 0.0, 1.0),
                "bad_crc_down": _number(raw_deg.get("bad_crc_down"), 0.0, 0.0, 1.0),
                "disconnect_after_s": _number(
                    raw_deg.get("disconnect_after_s"), 0.0, 0.0, 600.0
                ),
            }

    if idle_raw is not None:
        idle = _number(idle_raw, IDLE_TIMEOUT_NO_HEARTBEAT_S, 0.2, 600.0)
    elif interval > 0.0:
        idle = interval * max(missed, 1) + HEARTBEAT_IDLE_MARGIN_S
    else:
        idle = IDLE_TIMEOUT_NO_HEARTBEAT_S

    return {
        "heartbeat_interval_s": interval,
        "heartbeat_max_missed": max(missed, 1),
        "idle_timeout_s": idle,
        "trace_capacity": max(capacity, 100),
        "sim_degradation": degradation,
    }


def _plant_kwargs(params: dict) -> dict:
    """面板参数键 -> ``PlantParams`` 字段名（映射表在 ``sim/params.py`` 一处维护）。"""
    from ..sim.params import plant_kwargs

    return plant_kwargs(params)


class _CheckBanner(QWidget):
    """启动自检横幅（card25 接线，卡22 提供结果）。

    三态：全通过 -> 隐藏（不占左栏）；有警告 -> 黄色一行；有失败 -> 红色，
    并可把完整诊断文本复制走（无控制台打包时用户报问题就靠这段）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._text = ""
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        row = QHBoxLayout()
        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._copy = QPushButton("复制诊断")
        self._copy.setVisible(False)
        self._copy.clicked.connect(self._copy_text)
        row.addWidget(self._label, stretch=1)
        row.addWidget(self._copy)
        box.addLayout(row)
        self.hide()

    def set_results(self, results) -> None:
        """接收卡22 的检查结果（支持对象或 dict 两种形态）。"""
        items = list(results or [])
        self._apply(items, "\n".join(_describe_check(r) for r in items))

    def set_report(self, report) -> None:
        """接收卡22 的 SelfTestReport：横幅只显示失败/警告，复制走完整报告。"""
        checks = list(getattr(report, "checks", []) or [])
        try:
            from ..selftest import format_report_text

            text = format_report_text(report)
        except Exception:  # noqa: BLE001 - 报告格式化失败不影响横幅本身
            text = "\n".join(_describe_check(r) for r in checks)
        self._apply(checks, text)

    def _apply(self, items, text: str) -> None:
        failures = [r for r in items if _check_level(r) == "fail"]
        warnings = [r for r in items if _check_level(r) == "warn"]
        if not items or (not failures and not warnings):
            self._text = ""
            self.hide()
            return
        self._text = text
        # 三段式（卡30）：横幅只说"现象 + 原因 + 下一步"，完整报告留在复制诊断里。
        message = selftest_message(
            [_describe_check(r) for r in failures],
            [_describe_check(r) for r in warnings],
        )
        head = f"现象：{message.phenomenon} · 原因：{message.cause}\n下一步：{message.next_step}"
        if failures:
            style = f"color: {COLORS['text']}; background: {COLORS['err']};"
        else:
            style = f"color: {COLORS['text']}; background: {COLORS['warn']};"
        self._label.setText(head)
        self._label.setStyleSheet(style + "padding: 4px 6px; border-radius: 4px;")
        self._copy.setVisible(True)
        self.show()

    def _copy_text(self) -> None:
        QApplication.clipboard().setText(self._text)

    def text(self) -> str:
        return self._text


def _describe_plant(params: dict) -> str:
    """一行对象摘要（连接成功日志里用，让用户知道当前跑的是什么对象）。"""
    return (
        f"增益 {params.get('gain', 1.0):g} · "
        f"τ1 {params.get('tau1', 0):g}s · τ2 {params.get('tau2', 0):g}s · "
        f"延迟 {int(params.get('delay', 0))} 拍"
    )


def _check_level(result) -> str:
    """自检条目的级别：通过 / 警告 / 失败，兼容对象与字典。"""
    if isinstance(result, dict):
        status = result.get("status") or result.get("level") or "ok"
    else:
        status = getattr(result, "status", getattr(result, "level", "ok"))
    text = str(status).lower()
    if text in ("fail", "failed", "error", "失败"):
        return "fail"
    if text in ("warn", "warning", "警告"):
        return "warn"
    return "ok"


def _describe_check(result) -> str:
    """一行：名称 + 说明（字段名兼容 enum/dict）。"""
    if isinstance(result, dict):
        name = result.get("name") or result.get("title") or "检查项"
        detail = result.get("detail") or result.get("message") or ""
    else:
        name = getattr(result, "name", getattr(result, "title", "检查项"))
        detail = getattr(result, "detail", getattr(result, "message", ""))
    return f"{name}：{detail}" if detail else str(name)


def _clamp_int(value: object, low: int, high: int, fallback: int) -> int:
    """把配置里读出的数值夹到 [low, high]；非法值用 fallback。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return max(low, min(high, fallback))
    if high < low:  # 屏幕比最小窗口还小：至少守住下限
        return low
    return max(low, min(high, number))


class _LeftScroll(QScrollArea):
    """左栏滚动容器：内容宽度钉死等于视口宽度。

    QScrollArea 在 widgetResizable 下只会把内容缩到 ``minimumSizeHint().width()``，
    而左栏里 Z-N 面板的建议宽度有 500 多、阶跃面板最小宽度 354，内容会被排成
    358 宽——比 326 的视口宽，右侧直接被裁掉。这里按视口宽度强制内容宽度，
    面板内部的横向压缩交给各自的布局处理。
    """

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        widget = self.widget()
        if widget is not None and widget.width() != self.viewport().width():
            widget.setFixedWidth(self.viewport().width())


class MainWindow(QMainWindow):
    def __init__(self, *, show_onboarding: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("SerialPID-Tuner")
        self.resize(1280, 800)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self._wants_onboarding = bool(show_onboarding)

        try:
            _cfg, _cfg_warning = load_config()
        except Exception:  # noqa: BLE001 - 配置坏了也要能打开界面
            _cfg = {}
        self._comm_settings = comm_settings(_cfg if isinstance(_cfg, dict) else {})
        # 心跳默认关闭（card32）：现场先确认"不开也能连"，再按需打开。
        # 参数可在「工具 → 链路设置…」里改，改完立即作用在当前连接上。
        self._comm_degradation = dict(self._comm_settings["sim_degradation"])
        self._worker = CommWorker(
            heartbeat_interval=float(self._comm_settings["heartbeat_interval_s"]),
            heartbeat_max_missed=int(self._comm_settings["heartbeat_max_missed"]),
            idle_timeout=float(self._comm_settings["idle_timeout_s"]),
            trace_capacity=int(self._comm_settings["trace_capacity"]),
        )
        self._buffer = TelemetryBuffer()

        self._parse_errors = 0
        #: 解码层坏帧（CRC / 帧尾）上次提示时的累计值与时刻——坏帧会以每秒好几条
        #: 的速度来，提示必须限流，不然整块日志只剩这一件事（card33）。
        self._wire_bad_logged = 0
        self._wire_bad_log_at = 0.0
        self._parse_stat = QLabel("")
        self._parse_stat.setStyleSheet("color: #e74c3c;")
        self._parse_stat.hide()  # 为 0 时不占状态栏；大于 0 才出现（card16）
        self._unknown_logger = UnknownFrameLogger()
        # 坏帧限流（卡8）：按「帧类型 + 错误原因」聚合，与未知帧共用限流实现。
        # 状态栏累计计数与该聚合器互不耦合：清空日志只清聚合状态，不清计数。
        self._parse_logger = ParseErrorLogger()
        # 已解释过的坏帧类别：每一类只给一次三段式说明，避免坏帧雪崩时刷屏。
        self._explained_parse: set[tuple[int, str]] = set()

        # 采样率（顶部信息标签用）：用设备时间戳算，不依赖墙钟
        self._last_t_ms: int | None = None
        self._rate_hz = 0.0

        self._left_width = LEFT_PANEL_WIDTH

        self._plot = PlotPanel()
        self._param = ParamPanel(self._worker)
        self._step = StepPanel(self._worker)
        self._zn = ZnPanel()
        self._sim = SimPanel()
        # 故障注入（card33）：三个滑杆默认全零 = 直通；初值跟配置走，避免
        # "界面写着 0%、链路却在丢帧"这种自相矛盾。
        self._fault = FaultPanel()
        self._fault.set_params(
            drop_pct=float(self._comm_degradation.get("drop_down", 0.0)) * 100.0,
            bad_pct=float(self._comm_degradation.get("bad_crc_down", 0.0)) * 100.0,
            cut_s=float(self._comm_degradation.get("disconnect_after_s", 0.0)),
            emit=False,
        )
        self._log = LogPanel(self._buffer)

        # 端口扫描结果（三态：ok / empty / error）与仿真引擎句柄（card25）
        self._port_scan: PortScanResult | None = None
        self._sim_engine = None
        #: 仿真通道的劣化包装层（card32；关闭劣化时保持 None，走零开销直通）
        self._sim_degraded = None
        self._last_open_error = ""
        #: 最近一次线程错误的规范化说法（三段式），供"复制诊断"取原文用
        self._last_worker_error: UserMessage | None = None
        #: 最近一次断线的三段式说法（状态栏短标签之外的完整解释）
        self._last_disconnect_message: UserMessage | None = None
        self._check_banner = _CheckBanner()

        # ---- 链路质量与自动重连（card28） ----
        # 状态栏常驻指示：1 Hz 拉一次快照，够看清趋势又不会跟 20 Hz 遥测抢主线程
        self._link_status = LinkStatusWidget()
        #: 故障断开的原因（card32）：有值时 1 Hz 刷新不再把红灯的"链路断"
        #: 覆盖成灰的"未连接"，直到重新连上（或用户主动断开）才清掉。
        self._link_fault: str | None = None
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(LINK_STATUS_REFRESH_MS)
        # 自动重连状态：默认关闭；开启后按退避重试并显示"第几次"
        self._retry_attempt = 0
        self._reconnect_args: tuple[str, str, int] | None = None
        self._open_in_progress = False
        self._user_disconnected = False
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        # 帧日志对话框（延迟创建；菜单打开时构造）
        self._frame_log_dialog = None
        # 上一次 PONG 的状态值：心跳每秒一次，不该每秒往日志里刷一行
        self._last_pong_status: int | None = None
        self._pong_logged = False

        self._build_menu()
        self._build_topbar()
        self._build_layout()
        self._connect_signals()
        self._restore_ui_state()
        self._run_startup_checks()
        self._refresh_ports()

        self._log.log("就绪：选择 TCP / 串口 / 内置模拟器后点击连接")
        if self._wants_onboarding:
            # 等窗口先画出来再弹，否则向导会挡在一片未布局的空白上（卡30）
            QTimer.singleShot(0, self.maybe_show_onboarding)

    # ---- 菜单与内置帮助（卡30） ----

    def _build_menu(self) -> None:
        """「帮助」菜单：使用帮助 / 关于。F1 与菜单项走同一条路径。"""
        help_menu = self.menuBar().addMenu("帮助")
        help_action = QAction("使用帮助（F1）", self)
        help_action.triggered.connect(self._show_help)
        about_action = QAction("关于 SerialPID-Tuner", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(help_action)
        help_menu.addAction(about_action)

        # 「工具」菜单：链路设置与帧级日志回放（card28 / card32）。单独放在菜单里，
        # 因为它们是"连接前配置"与"事后排查"用的，不该常驻顶部工具栏跟连接操作抢位置。
        tools_menu = self.menuBar().addMenu("工具")
        link_settings_action = QAction("链路设置…（心跳 / 统计）", self)
        link_settings_action.triggered.connect(self._show_link_settings)
        tools_menu.addAction(link_settings_action)
        self._link_settings_action = link_settings_action
        frame_log_action = QAction("帧日志 / 回放…", self)
        frame_log_action.triggered.connect(self._show_frame_log)
        tools_menu.addAction(frame_log_action)
        # 持有引用：菜单里的 QAction 也怕被回收（与 F1 快捷键同理）
        self._frame_log_action = frame_log_action
        # card33：单独一条「回放帧记录」——远程协助时对方只发来一个 JSONL，
        # 不该逼他先去学"帧日志对话框"在哪。
        replay_action = QAction("回放帧记录…", self)
        replay_action.triggered.connect(self._replay_from_file)
        tools_menu.addAction(replay_action)
        self._replay_action = replay_action

        shortcut = QShortcut(
            QKeySequence(QKeySequence.StandardKey.HelpContents), self
        )
        shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        shortcut.activated.connect(self._show_help)
        # 持有引用，避免局部变量被回收后快捷键失效
        self._help_shortcut = shortcut

    def build_help_dialog(self):
        """构造帮助对话框（不 exec）：测试与截图脚本都走这个入口。"""
        from .help_dialog import HelpDialog

        return HelpDialog(self)

    def build_about_dialog(self):
        """构造关于对话框（不 exec）。"""
        from .about_dialog import AboutDialog

        return AboutDialog(self)

    def _show_help(self) -> None:
        dialog = self.build_help_dialog()
        self._help_dialog = dialog
        dialog.exec()

    def _show_about(self) -> None:
        dialog = self.build_about_dialog()
        self._about_dialog = dialog
        dialog.exec()

    def maybe_show_onboarding(self) -> bool:
        """首次运行（无配置文件）弹三屏向导；看过就直接返回 False。"""
        from .onboarding import OnboardingDialog, onboarding_done

        if onboarding_done():
            return False
        dialog = OnboardingDialog(self)
        self._onboarding_dialog = dialog
        dialog.exec()
        return True

    # ---- 界面 ----

    def _build_topbar(self) -> None:
        self._transport = QComboBox()
        self._transport.addItems([CHANNEL_TCP, CHANNEL_SERIAL, CHANNEL_SIM])

        self._target = QComboBox()
        self._target.setEditable(True)
        self._target.setMinimumWidth(180)
        self._target.setEditText("127.0.0.1:9000")

        self._baud = QComboBox()
        self._baud.addItems(["115200", "9600", "38400", "57600"])

        self._refresh = QPushButton("刷新端口")
        self._diag = QPushButton("诊断端口")
        self._diag.setToolTip("复制一段可粘贴给开发者的端口诊断文本")
        self._connect_btn = QPushButton("连接")
        self._state_label = QLabel("未连接")
        self._state_label.setStyleSheet("color: #888;")
        # 顶部中段原来挂着一个占满剩余宽度的状态标签（等于留白），换成采样信息：
        # 谁连上、多少条、什么速率，一眼能看到，不用盯着状态栏。
        self._rate_label = QLabel("采样 0 条")
        self._rate_label.setStyleSheet("color: #888;")
        # 自动重连（card28）：默认关闭。静默重连会把"设备已经掉线"伪装成
        # "一直连不上"，调试时必须先看见掉线；演示 / 长时间记录时才值得开。
        self._auto_reconnect = QCheckBox("断线重连")
        self._auto_reconnect.setChecked(False)
        self._auto_reconnect.setToolTip(
            "开启后掉线按 1/2/5/10 秒退避重试（之后每分钟一次），日志里会写明第几次。\n"
            "默认关闭：静默重连会掩盖真实硬件故障，调试时要能看见掉线。"
        )

        bar = QHBoxLayout()
        bar.addWidget(QLabel("通道"))
        bar.addWidget(self._transport)
        bar.addWidget(QLabel("目标"))
        bar.addWidget(self._target)
        bar.addWidget(QLabel("波特率"))
        bar.addWidget(self._baud)
        bar.addWidget(self._refresh)
        bar.addWidget(self._diag)
        bar.addWidget(self._connect_btn)
        bar.addWidget(self._auto_reconnect)
        bar.addStretch(1)
        bar.addWidget(self._rate_label)
        bar.addWidget(self._state_label)

        self._topbar = QWidget()
        self._topbar.setLayout(bar)
        self._refresh_ports()

    def _build_layout(self) -> None:
        # ---- 左栏：控件竖排，整体交给滚动区托管 ----
        content = QWidget()
        left_layout = QVBoxLayout(content)
        left_layout.setContentsMargins(2, 2, 2, 2)
        left_layout.setSpacing(4)
        # 左栏宽度是硬预算：面板自身的建议宽度可能超过 340（Z-N 面板建议 520），
        # 这里关掉布局的最小尺寸约束，让内容按视口宽度排布、纵向滚动，
        # 而不是把滚动区内容撑宽后被右边缘裁掉。
        left_layout.setSizeConstraint(QLayout.SetNoConstraint)
        content.setMinimumWidth(0)
        left_layout.addWidget(self._param)
        left_layout.addWidget(self._step)
        left_layout.addWidget(self._zn)
        # 仿真对象参数在左侧参数区下方、默认折叠（card25）：展开与否由用户决定，
        # 折叠时只占一行标题，不会把左栏再撑高。
        left_layout.addWidget(self._sim)
        # 故障注入紧贴仿真面板：它只在仿真通道上有意义，放在一起用户才连得上
        left_layout.addWidget(self._fault)
        left_layout.addStretch(0)  # 控件高度够时不要被强行拉长

        self._log.setMinimumHeight(self._log_min_height())

        scroll = _LeftScroll()
        scroll.setWidgetResizable(True)  # 内容宽度跟随视口
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(content)
        self._left_scroll = scroll

        # 日志钉在左栏底部，不进滚动区：参数类控件长得再多，日志也始终可见
        # （至少 LOG_MIN_LINES 行）。这是卡16 方案①（左栏可滚）+ 日志常驻的组合：
        # 屏幕矮时让*参数面板*去滚动，而不是把日志一起推出屏幕。
        left_col = QWidget()
        col_box = QVBoxLayout(left_col)
        col_box.setContentsMargins(0, 0, 0, 0)
        col_box.setSpacing(4)
        col_box.addWidget(self._check_banner)  # 自检横幅：全通过时自己隐藏，不占高度
        col_box.addWidget(scroll, stretch=1)
        col_box.addWidget(self._log)
        left_col.setFixedWidth(self._left_width)
        self._left_col = left_col

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_col)
        splitter.addWidget(self._plot)
        splitter.setStretchFactor(1, 1)
        # 左栏固定宽度，别让分割条把它拉宽
        splitter.setCollapsible(0, False)
        self._splitter = splitter

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._topbar)
        layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        self.statusBar().showMessage("采样 0 条")
        # 链路质量指示放在解析异常计数左边：先看"链路好不好"，再看"坏了多少帧"
        self.statusBar().addPermanentWidget(self._link_status)
        self.statusBar().addPermanentWidget(self._parse_stat)

    def _log_min_height(self) -> int:
        """日志面板最小高度：足够显示 LOG_MIN_LINES 行 + 按钮行与边距。

        边距/按钮行高度不写死数字，用当前字体行高推算后再留余量，
        换主题或换字体时不会又掉回"一行日志"。
        """
        line = self._log._view.fontMetrics().lineSpacing()
        return LOG_MIN_LINES * max(line, 12) + 90

    def _apply_left_width(self, width: int) -> None:
        self._left_width = width
        if hasattr(self, "_left_col"):
            self._left_col.setFixedWidth(width)
        if hasattr(self, "_left_scroll"):
            self._left_scroll.setFixedWidth(width)

    # ---- 界面状态持久化（尺寸记忆） ----

    def _restore_ui_state(self) -> None:
        """从配置文件恢复窗口尺寸与左栏宽度；任何异常都退回默认，不阻塞启动。"""
        try:
            cfg, _warning = load_config()
        except Exception:  # noqa: BLE001 - 配置坏了也要能打开界面
            return
        ui = cfg.get("ui") if isinstance(cfg, dict) else None
        if not isinstance(ui, dict):
            ui = {}

        screen = QApplication.primaryScreen()
        avail_w, avail_h = MAX_WINDOW_WIDTH, MAX_WINDOW_HEIGHT
        if screen is not None:
            geo = screen.availableGeometry()
            # 屏幕放得下最小窗口才拿它当上限；比最小窗口还小的屏幕（离屏渲染、
            # 极小分辨率）用理智上限，否则记忆的尺寸会被夹成"最小尺寸"。
            if geo.width() >= MIN_WINDOW_WIDTH:
                avail_w = geo.width()
            if geo.height() >= MIN_WINDOW_HEIGHT:
                avail_h = geo.height()

        width = _clamp_int(ui.get("window_width"), MIN_WINDOW_WIDTH, avail_w, 1280)
        height = _clamp_int(ui.get("window_height"), MIN_WINDOW_HEIGHT, avail_h, 800)
        left_w = _clamp_int(ui.get("left_panel_width"),
                            LEFT_PANEL_MIN_WIDTH, LEFT_PANEL_MAX_WIDTH, LEFT_PANEL_WIDTH)
        self._apply_left_width(left_w)
        self.resize(width, height)

    def _save_ui_state(self) -> None:
        try:
            cfg, _warning = load_config()
            if not isinstance(cfg, dict):
                return
            ui = cfg.get("ui")
            if not isinstance(ui, dict):
                ui = {}
            ui.update({
                "window_width": self.width(),
                "window_height": self.height(),
                "left_panel_width": self._left_width,
            })
            cfg["ui"] = ui
            save_config(cfg)
        except Exception:  # noqa: BLE001 - 存不下就算了，不能因此崩在关闭流程里
            pass

    def _connect_signals(self) -> None:
        self._connect_btn.clicked.connect(self._toggle_connection)
        self._refresh.clicked.connect(self._refresh_ports)
        self._diag.clicked.connect(self._show_port_diagnostics)
        self._transport.currentTextChanged.connect(self._on_transport_changed)

        self._worker.frameReceived.connect(self._on_frame)
        self._worker.connectionChanged.connect(self._on_connection)
        self._worker.errorOccurred.connect(self._on_worker_error)
        self._zn.applied.connect(self._on_zn_applied)
        self._sim.paramsChanged.connect(self._on_sim_params)
        self._sim.presetChanged.connect(self._on_sim_preset)
        self._sim.expandedChanged.connect(self._on_sim_expanded)
        # 故障注入（card33）：滑杆一动就折算成 LinkConfig 并作用到当前仿真链路
        self._fault.configChanged.connect(self._on_fault_config)
        self._fault.expandedChanged.connect(self._on_sim_expanded)

        self._log.clear_buffer_signal().clicked.connect(self._clear_records)
        # “清空日志”只重置聚合状态，不动状态栏累计计数（那是本机开机以来的量）
        self._log.clear_log_signal().clicked.connect(self._reset_log_aggregators)

        # ---- 链路质量与自动重连（card28） ----
        self._stats_timer.timeout.connect(self._refresh_link_status)
        self._stats_timer.start()
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)
        self._auto_reconnect.toggled.connect(self._on_auto_reconnect_toggled)

    def _on_worker_error(self, msg: str) -> None:
        self._last_open_error = msg
        # 卡35：线程只报原始异常（"读取异常: ..."），界面负责翻成三段式，
        # 原文留在诊断里；这样日志里不会出现"错误: xxx"这种只有结论没有下文的行。
        self._log.log(worker_error_message(msg).as_block())
        self._last_worker_error = worker_error_message(msg)

    # ---- 连接管理 ----

    def _refresh_ports(self) -> None:
        """按当前通道刷新「目标」框。

        Serial 走 :mod:`serialpid.port_scan` 的三态结果：
        有设备 -> 逐项列出（设备名 · 描述 · 厂商，data 存裸设备名，如 COM3）；
        无设备 / 枚举失败 -> 一条**不可选中**的说明项，绝不塞假 ``COM1``。
        """
        channel = self._transport.currentText()
        self._refresh.setEnabled(channel != CHANNEL_SIM)
        self._diag.setVisible(channel != CHANNEL_SIM)
        self._apply_channel_state(channel)

        if channel == CHANNEL_SIM:
            return
        if channel == CHANNEL_TCP:
            self._target.setEditable(True)
            self._target.setEnabled(True)
            self._target.clear()
            self._target.setEditText("127.0.0.1:9000")
            return

        # ---- Serial ----
        self._target.setEditable(False)
        self._target.setEnabled(True)
        try:
            result = scan_ports()
        except Exception as exc:  # noqa: BLE001 - 枚举炸了也不能留空下拉框
            from ..port_scan import PortScanResult

            result = PortScanResult(
                status="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        self._port_scan = result
        self._target.clear()
        if result.ok:
            for port in result.ports:
                self._target.addItem(port.display_name, port.device)
                self._target.setItemData(
                    self._target.count() - 1, port.detail_line(), Qt.ToolTipRole
                )
            self._target.setCurrentIndex(0)
            self._diag.setEnabled(True)
        else:
            text = PORT_EMPTY_TEXT if result.is_empty else PORT_ERROR_TEXT
            self._target.addItem(text)
            self._target.setItemData(0, result.hint, Qt.ToolTipRole)
            # 说明项不可选：置灰 + 清空可选状态，用户无法把它当成设备去连
            model = self._target.model()
            if model is not None:
                item = model.item(0)
                if item is not None:
                    item.setEnabled(False)
            self._diag.setEnabled(True)
            # 端口为空 / 枚举失败是用户最容易卡住的一步：日志直接给三段式，
            # 而不是一行"端口：未发现串口设备；请检查…"（卡30）。
            message = (
                empty_port_message()
                if result.is_empty
                else port_scan_failed_message(f"{result.summary}；{result.hint}")
            )
            self._log.log(message.as_block())

    def _apply_channel_state(self, channel: str) -> None:
        """通道决定「目标 / 波特率」能不能填：仿真通道两者都置灰并给说明。"""
        if channel == CHANNEL_SIM:
            self._target.setEditable(False)
            self._target.clear()
            self._target.addItem(SIM_TARGET_TEXT)
            self._target.setEnabled(False)
            self._baud.setEnabled(False)
        else:
            self._target.setEnabled(True)
            self._baud.setEnabled(True)

    def _on_transport_changed(self, text: str) -> None:
        # 切通道等于换物理链路：先把旧连接断开、把仿真引擎停掉，
        # 否则引擎会在后台空转、旧连接还会撑着一个"已连接"的假状态。
        if self._worker.connected:
            before = self._state_label.text()
            # 通道切换是"用户要求换链路"，不该被自动重连又拉回旧通道
            self._reconnect_timer.stop()
            self._retry_attempt = 0
            self._worker.close()
            if self._state_label.text() == before:
                # 换通道是用户主动行为：先说清"为什么断开"，再按主动断开的文案收口
                self._log.log("通道切换，已自动断开")
                self._on_connection(False, DROP_CLOSED)
        self._stop_sim_engine()
        self._refresh_ports()
        if text == CHANNEL_SIM:
            # 面板在参数区最下方且默认折叠：切到仿真通道时把左栏滚到它那里，
            # 用户不用自己翻也知道"参数就在这儿"。
            QTimer.singleShot(0, self._scroll_left_to_bottom)

    def _toggle_connection(self) -> None:
        if self._worker.connected:
            before = self._state_label.text()
            # 用户主动断开：取消可能挂着的自动重连，并标记"这次不是故障"
            self._user_disconnected = True
            self._reconnect_timer.stop()
            self._retry_attempt = 0
            self._worker.close()
            self._stop_sim_engine()
            # 显式关闭时通信核心在等线程退出，接收线程的 finally 可能先把
            # _connected 清掉，导致核心不再补发"断开"通知（close 里的
            # was_connected 竞态）。界面自己收口，别让按钮停在"断开"上。
            if self._state_label.text() == before:
                self._on_connection(False, DROP_CLOSED)
            return

        channel = self._transport.currentText()
        if channel == CHANNEL_SIM:
            self._connect_sim()
            return

        kind = "tcp" if channel == CHANNEL_TCP else "serial"
        target = self._serial_target() if kind == "serial" else self._target.currentText().strip()
        if not target:
            scan = self._port_scan
            message = empty_port_message()
            hint = getattr(scan, "hint", "") if scan is not None else ""
            if hint and hint not in message.next_step:
                message = UserMessage(
                    phenomenon=message.phenomenon,
                    cause=message.cause,
                    next_step=f"{message.next_step}（{hint}）",
                )
            self._log.log(message.as_block())
            return
        baud = int(self._baud.currentText())
        self._open_in_progress = True
        try:
            ok = self._worker.open(kind, target, baud)
        finally:
            self._open_in_progress = False
        if ok:
            self._remember_connection(kind, target, baud)
            self._log.log(f"已连接 {kind}://{target}")
        else:
            reason = self._last_open_error or "未知原因"
            self._log.log(serial_open_message(target, reason).as_block())

    def _remember_connection(self, kind: str, target: str, baud: int) -> None:
        """记住最后一次成功打开的通道参数：自动重连要用它原样重试。"""
        self._reconnect_args = (kind, target, baud)
        self._retry_attempt = 0
        self._user_disconnected = False
        self._reconnect_timer.stop()

    def _serial_target(self) -> str:
        """当前选中的裸设备名（COM3），而不是下拉里那行带描述的显示文本。

        空/异常态下占位项是**不可选中**的说明行，此时返回空串——绝不能把
        "未发现串口设备…"当端口号去打开。
        """
        data = self._target.currentData()
        if isinstance(data, str) and data:
            return data
        if self._target.isEditable():  # 手打 TEXT_FORMAT 时的兜底
            return self._target.currentText().strip().split(" · ")[0].strip()
        return ""

    def build_port_diagnostics_dialog(self) -> tuple[QDialog, str]:
        """构造端口诊断对话框（不 exec）：文案来自 port_scan.diagnose 唯一实现。"""
        text = diagnose(self._port_scan)
        dlg = QDialog(self)
        dlg.setWindowTitle("端口诊断")
        dlg.resize(560, 380)
        box = QVBoxLayout(dlg)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        box.addWidget(view)
        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton("复制到剪贴板")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(text))
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        box.addLayout(row)
        return dlg, text

    def _show_port_diagnostics(self) -> None:
        """把端口诊断文本弹出来，支持一键复制。"""
        dlg, _text = self.build_port_diagnostics_dialog()
        self._log.log("已生成端口诊断文本（对话框可复制）")
        dlg.exec()

    def _on_connection(self, connected: bool, detail: str) -> None:
        if connected:
            # 新会话：绘图时间基准从 0 重新起算（card11）。
            # 板子复位 / 重新上电后时间戳从 0 重新计数，若沿用上一场的零点，
            # 横轴会立刻跳到负数区间，时间窗名存实亡。
            # 边界：这里只重置绘图时间轴；主窗口遥测缓冲与阶跃分析仍保留上一会话
            # 样本（导出 / 统计会看到两段 t_ms），需要一并清零时走 _clear_records。
            self._plot.restart_timeline("连接建立，时间轴从 0 起算")
            # 新会话的设备时间戳从 0 重新计数，采样率基准跟着重来
            self._last_t_ms = None
            self._rate_hz = 0.0
            self._rate_label.setText(f"采样 {len(self._buffer)} 条")
            self._connect_btn.setText("断开")
            self._state_label.setText("已连接")
            self._state_label.setStyleSheet(f"color: {COLORS['ok']};")
            # 新连接：重连计数归零、心跳日志状态复位、质量灯立刻刷一次
            self._retry_attempt = 0
            self._reconnect_timer.stop()
            self._user_disconnected = False
            self._last_pong_status = None
            self._pong_logged = False
            # 新会话：解码层坏帧提示重新开始计数（上一场的数字不该继续吓人）
            self._wire_bad_logged = 0
            self._wire_bad_log_at = 0.0
            # 连上了就不再是"链路断"：清掉故障标记，让徽章回到实时质量
            self._link_fault = None
            self._refresh_link_status()
        else:
            self._connect_btn.setText("连接")
            brief = brief_disconnect(detail)
            self._state_label.setText(f"未连接（{brief}）")
            self._state_label.setStyleSheet(f"color: {COLORS['err']};")
            # 六类断线原因中文化（card28）：现象 + 原因 + 下一步三行一起进日志，
            # 用户不用去翻协议文档或凭 "timeout" 猜发生了什么。
            # 心跳判死再补上"连了几次没应答 / 容忍上限几次"（卡35）。
            message = describe_disconnect_message(
                detail,
                missed=self._heartbeat_misses(),
                limit=int(self._comm_settings["heartbeat_max_missed"]),
            )
            self._log.log(f"连接状态：{brief}")
            self._log.log(message.as_block())
            self._last_disconnect_message = message
            # 断开的这一刻最该做的是把证据留住：帧日志还在内存里，能导就导（card33）
            try:
                frames = len(self._worker.trace)
            except Exception:  # noqa: BLE001 - 取不到帧数不该挡住断线提示
                frames = 0
            self._log.log(
                "帧日志可从「工具 → 帧日志 / 回放」导出（JSONL 能直接发给别人），"
                "或「工具 → 回放帧记录」重演"
                + (f"；当前缓冲 {frames} 帧还没保存" if frames else "")
            )
            # card32：故障断开保持红灯 + 原因；用户主动断开则回到"未连接"
            if detail == DROP_CLOSED:
                self._link_fault = None
                self._refresh_link_status()
            else:
                self._link_fault = brief
                try:
                    fault_details = self._worker.stats_snapshot().details()
                except Exception:  # noqa: BLE001 - 统计取不到也要把原因显示出来
                    fault_details = ""
                self._link_status.show_fault(brief, fault_details)
            # 打开失败（端口被占 / 拔了）已经由 _toggle_connection 说明过，
            # 那时不能自动重连——否则会对着一个不存在的端口无限重试。
            if not self._open_in_progress:
                self._schedule_reconnect(detail)

    # ---- 链路质量与自动重连（card28） ----

    def _refresh_link_status(self) -> None:
        """1 Hz 把链路快照刷到状态栏（不随 20 Hz 遥测再开一路信号）。"""
        if self._link_fault and not self._worker.connected:
            # 故障断开：红灯 + 原因保持住，统计只是"最后一次会话"的参考值
            try:
                details = self._worker.stats_snapshot().details()
            except Exception:  # noqa: BLE001 - 统计出问题也不能把红灯弄丢
                details = ""
            self._link_status.show_fault(self._link_fault, details)
            return
        try:
            snapshot = self._worker.stats_snapshot()
        except Exception as exc:  # noqa: BLE001 - 统计出问题不能拖垮状态栏
            self._link_status.setToolTip(f"链路统计不可用：{exc}")
            return
        self._link_status.update_snapshot(snapshot)
        # CRC / 帧尾级坏帧在解码层就被丢掉，走不到 _on_frame 的载荷预检，
        # 所以状态栏的「坏帧 N」要在这里补上这部分（card33：注入坏帧后
        # 用户必须能在界面上看见计数，而不是只有一句"日志里刷过去了"）。
        try:
            wire_bad = int(getattr(snapshot, "bad_frames", 0))
        except Exception:  # noqa: BLE001 - 统计字段缺失不该影响质量灯
            wire_bad = 0
        if wire_bad > 0:
            total_bad = self._parse_errors + wire_bad
            self._parse_stat.setText(f"坏帧 {total_bad}")
            reason = getattr(snapshot, "quality_reason", "") or ""
            self._parse_stat.setToolTip(
                f"{reason}\n其中解码层坏帧 {wire_bad} 个、载荷层坏帧 {self._parse_errors} 个。"
                "常见于波特率不一致、线序/干扰、固件与上位机版本不匹配。"
            )
            self._parse_stat.show()
            now = time.monotonic()
            if wire_bad > self._wire_bad_logged and now - self._wire_bad_log_at >= 10.0:
                self._wire_bad_logged = wire_bad
                self._wire_bad_log_at = now
                self._log.log(
                    UserMessage(
                        phenomenon=f"链路上出现坏帧（已累计 {wire_bad} 个，状态栏可看总数）",
                        cause=(
                            "CRC 校验或帧尾对不上：波特率 / 线序不一致、线路干扰，"
                            "或固件与上位机版本不匹配都会这样"
                        ),
                        next_step=(
                            "先核对 115200 8N1 与接线；换短一点的线和共地；"
                            "基线稳下来后导出帧日志，用来定位是哪类帧先坏"
                        ),
                    ).as_block()
                )

    # ---- 链路设置：心跳开关 / 统计清零（card32） ----

    def build_link_settings_dialog(self) -> LinkSettingsDialog:
        """构造链路设置对话框（不 exec）：测试与截图脚本都走这个入口。"""
        dialog = LinkSettingsDialog(
            heartbeat_interval=float(self._comm_settings["heartbeat_interval_s"]),
            heartbeat_max_missed=int(self._comm_settings["heartbeat_max_missed"]),
            parent=self,
        )
        dialog.heartbeatChanged.connect(self._on_heartbeat_changed)
        dialog.statsReset.connect(self._on_stats_reset)
        self._link_settings_dialog = dialog
        return dialog

    def _show_link_settings(self) -> None:
        self.build_link_settings_dialog().exec()

    def _on_heartbeat_changed(self, interval_s: float, max_missed: int) -> None:
        """改心跳参数：立刻作用在当前连接上（不需要断开重连），并写回配置。"""
        interval, missed = self._worker.set_heartbeat(interval_s, max_missed)
        self._comm_settings["heartbeat_interval_s"] = interval
        self._comm_settings["heartbeat_max_missed"] = missed
        self._comm_settings["idle_timeout_s"] = self._worker.idle_timeout
        if interval > 0.0:
            self._log.log(
                f"心跳已开启：每 {interval:g} s 发一次 PING，连续 {missed} 次无应答"
                f"且同窗口内没有遥测才判死（判死窗口 {self._worker.idle_timeout:.1f} s）"
            )
        else:
            self._log.log(
                "心跳已关闭：回到空闲 "
                f"{IDLE_TIMEOUT_NO_HEARTBEAT_S:.1f} s 判死（与旧版行为一致）"
            )
        self._persist_comm_settings()
        self._refresh_link_status()

    def _on_stats_reset(self) -> None:
        self._worker.reset_stats()
        self._log.log(
            "链路统计已清零：收发帧数 / 坏帧 / 约丢帧 / RTT 从 0 重新计（连接没有断开）"
        )
        self._refresh_link_status()

    def _on_fault_config(self, config) -> None:
        """故障注入滑杆（card33）-> ``LinkConfig``：立刻作用于当前仿真链路。"""
        self._comm_degradation = {
            "enabled": bool(getattr(config, "enabled", False)),
            "drop_down": min(max(float(getattr(config, "drop_down", 0.0)), 0.0), 1.0),
            "bad_crc_down": min(max(float(getattr(config, "bad_crc_down", 0.0)), 0.0), 1.0),
            "disconnect_after_s": max(
                float(getattr(config, "disconnect_after_s", 0.0)), 0.0
            ),
        }
        applied = self._apply_degradation()
        text = getattr(config, "describe", None)
        describe = text() if callable(text) else "故障注入参数已更新"
        if not applied and self._comm_degradation["enabled"]:
            describe += "（连上内置模拟器后生效）"
        self._log.log(describe)
        self._persist_comm_settings()
        self._refresh_link_status()

    def _apply_degradation(self) -> bool:
        """把当前劣化参数推给已建好的劣化层；没建过就返回 False。"""
        if self._sim_degraded is None:
            return False
        try:
            from ..sim.degradation import LinkConfig

            values = dict(self._comm_degradation)
            self._sim_degraded.apply(
                LinkConfig(
                    enabled=bool(values.get("enabled", False)),
                    drop_down=float(values.get("drop_down", 0.0)),
                    bad_crc_down=float(values.get("bad_crc_down", 0.0)),
                    disconnect_after_s=float(values.get("disconnect_after_s", 0.0)),
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 劣化层出错不能拖垮界面
            self._log.log(f"劣化参数没有应用成功：{exc}")
            return False

    def _persist_comm_settings(self) -> None:
        """把 comm 段写回 config.json（心跳三项 + 仿真劣化）。

        ``idle_timeout_s`` 一并删掉：它是由「间隔 × 次数」推导的，留着手工值会让
        改了间隔之后判死窗口不跟着变——这正是"改了没用"的经典来源。
        """
        try:
            cfg, _warning = load_config()
            if not isinstance(cfg, dict):
                return
            comm = cfg.get("comm")
            if not isinstance(comm, dict):
                comm = {}
            comm.update({
                "heartbeat_interval_s": float(self._comm_settings["heartbeat_interval_s"]),
                "heartbeat_max_missed": int(self._comm_settings["heartbeat_max_missed"]),
                "trace_capacity": int(self._comm_settings["trace_capacity"]),
                "sim_degradation": dict(self._comm_degradation),
            })
            comm.pop("idle_timeout_s", None)
            cfg["comm"] = comm
            save_config(cfg)
        except Exception as exc:  # noqa: BLE001 - 存不下就算了，本次改动仍已生效
            self._log.log(f"链路设置写入 config.json 失败：{exc}（本次设置已生效）")

    def _heartbeat_misses(self) -> int:
        """心跳累计欠账次数（取自链路统计）；读不到就返回 0，文案退化成不带数字。"""
        try:
            return max(int(self._worker.stats_snapshot().heartbeat_misses), 0)
        except Exception:  # noqa: BLE001 - 统计不可用时不能连带断线提示一起坏
            return 0

    def _on_auto_reconnect_toggled(self, checked: bool) -> None:
        """切开关时的说明：默认关，是为了让掉线先被看见。"""
        if checked:
            self._log.log(
                f"自动重连已开启：掉线后按 1/2/5/10 秒退避重试，最多 {RECONNECT_MAX_ATTEMPTS} 次"
            )
        else:
            self._reconnect_timer.stop()
            self._retry_attempt = 0
            self._log.log("自动重连已关闭：掉线后需要手动点「连接」")

    def _schedule_reconnect(self, reason: str) -> bool:
        """掉线后安排下一次重试；返回是否真的排上了。

        只有三个条件同时成立才排：用户打开了开关、手里有可重试的通道参数、
        这次断开不是用户自己要求的。
        """
        if not self._auto_reconnect.isChecked():
            return False
        if self._reconnect_args is None or self._user_disconnected:
            return False
        if reason == DROP_CLOSED:
            return False
        self._retry_attempt += 1
        if self._retry_attempt > RECONNECT_MAX_ATTEMPTS:
            self._state_label.setText("未连接（自动重连已放弃）")
            self._log.log(
                f"自动重连放弃：已尝试 {RECONNECT_MAX_ATTEMPTS} 次仍未成功，"
                "请检查 USB 线 / 供电 / 端口是否被占用"
            )
            return False
        delay = reconnect_delay(self._retry_attempt)
        self._reconnect_timer.start(int(round(delay * 1000)))
        target = self._reconnect_args[1]
        self._state_label.setText(
            f"未连接（自动重连第 {self._retry_attempt} 次，{delay:.0f} s 后）"
        )
        self._log.log(
            f"自动重连：第 {self._retry_attempt} 次将在 {delay:.0f} s 后重试 {target}"
        )
        return True

    def _attempt_reconnect(self) -> None:
        """定时器到点：按记住的通道参数重开一次。"""
        if not self._auto_reconnect.isChecked() or self._reconnect_args is None:
            return
        if self._worker.connected:
            return
        kind, target, baud = self._reconnect_args
        self._open_in_progress = True
        try:
            ok = self._worker.open(kind, target, baud)
        finally:
            self._open_in_progress = False
        if ok:
            self._log.log(f"自动重连成功（第 {self._retry_attempt} 次）：{kind}://{target}")
            self._retry_attempt = 0
            return
        self._log.log(
            f"自动重连第 {self._retry_attempt} 次失败：{self._last_open_error or '未知原因'}"
        )
        # 重开失败也要继续按退避排下一次，直到放弃上限
        self._schedule_reconnect("reopen-failed")

    # ---- 帧日志与回放（card28） ----

    def build_frame_log_dialog(self) -> FrameLogDialog:
        """构造帧日志对话框（不 exec）：测试与截图脚本都走这个入口。"""
        dialog = FrameLogDialog(self, self._worker.trace)
        dialog.framePlayed.connect(self._on_frame)
        dialog.replayStarted.connect(self._on_replay_started)
        dialog.replayFinished.connect(self._on_replay_finished)
        return dialog

    def _show_frame_log(self) -> None:
        dialog = self.build_frame_log_dialog()
        self._frame_log_dialog = dialog
        dialog.exec()

    def replay_from_file(self, path: str = "") -> bool:
        """离线回放（card33）：选 JSONL 后直接按原时序驱动界面。

        与「帧日志 / 回放…」的区别：这条入口不先摊开列表，选完就播，适合
        "别人发来一份帧流，我想立刻看现场"。回放是只读重演，不打开任何真实
        通道，也不会下发命令（``_on_replay_started`` 会先断开实时连接）。
        """
        if not path:
            path, _selected = QFileDialog.getOpenFileName(
                self, "回放帧记录", "", "JSONL (*.jsonl);;所有文件 (*)"
            )
        if not path:
            return False
        dialog = self.build_frame_log_dialog()
        self._frame_log_dialog = dialog
        if not dialog.load_file(path):
            dialog.show()
            return False
        dialog.toggle_play()
        dialog.show()
        return True

    def _replay_from_file(self) -> None:
        self.replay_from_file()

    def _on_replay_started(self) -> None:
        """回放前把现场清干净：实时连接断掉，曲线时间轴从 0 重来。"""
        if self._worker.connected:
            self._user_disconnected = True
            self._reconnect_timer.stop()
            self._retry_attempt = 0
            self._worker.close()
            self._stop_sim_engine()
            self._log.log("回放前已断开实时连接（避免回放帧与实时帧混在同一时间轴）")
        self._clear_records()
        self._plot.restart_timeline("回放导入的帧流")
        self._log.log("开始回放帧日志：曲线按原始时序重建")

    def _on_replay_finished(self) -> None:
        self._log.log("回放结束")

    # ---- 启动自检（card25 接线，卡22 提供结果） ----

    def _run_startup_checks(self) -> None:
        """跑一次启动自检并交给横幅；自检模块缺席时保持横幅隐藏，不阻塞启动。"""
        try:
            from ..selftest import run_selftest  # 卡22
        except ImportError:
            self._check_banner.hide()
            return
        try:
            report = run_selftest()
        except Exception as exc:  # noqa: BLE001 - 自检本身不能挡住程序启动
            self._log.log(f"启动自检执行失败：{type(exc).__name__}: {exc}")
            self._check_banner.hide()
            return
        self._check_banner.set_report(report)
        for check in list(getattr(report, "failed", [])) + list(getattr(report, "warnings", [])):
            self._log.log(f"自检：{_describe_check(check)}")

    # ---- 内置模拟器（card25，引擎来自 card23） ----

    def _current_sim_params(self) -> dict:
        return dict(self._sim.params())

    def _connect_sim(self) -> None:
        """创建引擎 + 进程内通道，交给通信核心（核心只认传输接口）。"""
        try:
            from ..sim.engine import SimEngine
            from ..sim.plant import Plant, PlantParams
            from ..sim.transport import SimTransport
        except ImportError as exc:
            self._log.log(
                UserMessage(
                    phenomenon="内置模拟器没有启动",
                    cause=f"仿真引擎模块缺失或导入失败（{exc}）",
                    next_step="改用「TCP」通道配合 tools/fake_mcu.py，或重新安装完整版本。",
                ).as_block()
            )
            self._state_label.setText("未连接（内置模拟器模块缺失）")
            self._state_label.setStyleSheet(f"color: {COLORS['err']};")
            return

        params = self._current_sim_params()
        transport = SimTransport()
        degraded = None
        # card32 / card33：仿真通道始终套一层劣化包装，但默认 enabled=False——
        # 不丢字节、不起后台线程（零开销直通），滑杆一动就能立刻作用，用户
        # 不必为了试故障而断开重连。
        try:
            from ..sim.degradation import DegradedTransport, LinkConfig

            degraded = DegradedTransport(
                transport,
                LinkConfig(
                    enabled=bool(self._comm_degradation.get("enabled", False)),
                    drop_down=float(self._comm_degradation.get("drop_down", 0.0)),
                    bad_crc_down=float(self._comm_degradation.get("bad_crc_down", 0.0)),
                    disconnect_after_s=float(
                        self._comm_degradation.get("disconnect_after_s", 0.0)
                    ),
                ),
            )
            transport = degraded
        except Exception as exc:  # noqa: BLE001 - 劣化层缺失不该挡住仿真通道
            self._log.log(f"劣化层没有加载成功，按直通连接：{exc}")
            degraded = None
        plant = Plant(PlantParams(**_plant_kwargs(params)))
        period_s = float(params.get("period_ms", 50)) / 1000.0
        engine = SimEngine(transport=transport, plant=plant, period_s=period_s)

        # 先让通信核心接管通道（它会 open + 起接收线程），再起引擎：
        # 反过来的话两边都会 open 一次，引擎刚写的首帧会被 open 的清理冲掉。
        ok = self._worker.open("sim", SIM_TARGET_TEXT, 0, transport=transport)
        if not ok:
            reason = self._last_open_error or "未知原因"
            self._log.log(
                UserMessage(
                    phenomenon="内置模拟器没有启动，曲线不会动",
                    cause=f"通信核心没有接管进程内仿真通道（{reason}）",
                    next_step="先点「断开」再重新点「连接」；仍失败改用「TCP」通道跑 tools/fake_mcu.py。",
                ).as_block()
            )
            self._state_label.setText("未连接（仿真通道打开失败）")
            self._state_label.setStyleSheet(f"color: {COLORS['err']};")
            return
        self._sim_engine = engine
        self._sim_degraded = degraded
        engine.start()
        self._apply_sim_params(params)
        self._log.log(
            f"已连接内置模拟器（进程内，无需外部进程）；"
            f"周期 {period_s * 1000:.0f} ms，对象 {_describe_plant(params)}"
            + (
                f"；链路劣化已开启（下行丢帧 "
                f"{float(self._comm_degradation.get('drop_down', 0.0)) * 100:.0f}%、坏帧 "
                f"{float(self._comm_degradation.get('bad_crc_down', 0.0)) * 100:.0f}%"
                + (
                    f"、{float(self._comm_degradation['disconnect_after_s']):g} s 后强制断线"
                    if float(self._comm_degradation.get("disconnect_after_s", 0.0)) > 0.0
                    else ""
                )
                + "）"
                if degraded is not None and self._comm_degradation.get("enabled")
                else ""
            )
        )

    def _stop_sim_engine(self) -> None:
        engine = self._sim_engine
        self._sim_engine = None
        # 劣化包装层随通道一起失效：下次连接按当时的设置重新决定包不包
        self._sim_degraded = None
        if engine is None:
            return
        try:
            engine.stop()
        except Exception as exc:  # noqa: BLE001 - 停引擎失败不该让界面崩
            self._log.log(f"停止仿真引擎失败：{exc}")

    def _apply_sim_params(self, params: dict) -> None:
        """把面板快照推给引擎：对象参数走 update_plant，控制周期走 set_period。"""
        engine = self._sim_engine
        if engine is None:
            return
        kwargs = _plant_kwargs(params)
        update = getattr(engine, "update_plant", None)
        try:
            if callable(update):
                update(**kwargs)
            else:  # 引擎形态变化时的兜底：直接改对象参数
                plant = getattr(engine, "plant", None)
                if plant is not None:
                    plant.set_params(**kwargs)
        except ValueError as exc:
            self._log.log(f"仿真参数被拒绝：{exc}")
            return
        period_ms = params.get("period_ms")
        setter = getattr(engine, "set_period", None)
        if period_ms and callable(setter):
            setter(float(period_ms) / 1000.0)

    def _scroll_left_to_bottom(self) -> None:
        """把左栏滚到底：仿真面板是滚动内容的最后一块，滚到底才看得全。"""
        try:
            bar = self._left_scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:  # 延迟调用时窗口可能已经销毁
            pass

    def _on_sim_expanded(self, expanded: bool) -> None:
        if expanded:
            # 展开后布局尺寸要等下一轮事件循环才更新，延迟一次再滚
            QTimer.singleShot(0, self._scroll_left_to_bottom)

    def _on_sim_params(self, params: dict) -> None:
        """面板改值 -> 引擎实时生效（参数是纯 dict 快照，跨线程安全）。"""
        self._apply_sim_params(params)

    def _on_sim_preset(self, name: str) -> None:
        # 时间常数跨数量级跳变时，旧的对象内部状态会让曲线"跳"一下；
        # 切预设时重置对象状态并说明（card25 风险项）。
        if self._sim_engine is None:
            return
        reset = getattr(self._sim_engine, "reset", None)
        if callable(reset):
            reset()
        self._log.log(f"已切换仿真场景「{name}」，对象状态已重置")

    # ---- 帧分发 ----

    def _on_frame(self, pkt_type: int, payload: bytes) -> None:
        """帧分发槽（主线程事件循环）。

        兜底结构（card3 缺陷一）：
          1) 解析前按帧类型做长度预检，长度不符记一条并丢弃，不进入解析流程；
          2) 整个分发函数外层包 try/except，意外异常写日志并计数；
          3) 状态栏常驻显示累计解析异常次数。
        任何畸形输入都不允许让事件循环噎住——否则曲线静默停更。
        """
        try:
            reason = payload_error_reason(pkt_type, payload)
            if reason is not None:
                self._record_parse_error(pkt_type, reason)
                return
            self._dispatch_frame(pkt_type, payload)
        except Exception as exc:  # noqa: BLE001 - 槽函数兜底，见 card3 缺陷一
            self._record_parse_error(pkt_type, f"{type(exc).__name__}: {exc}")

    def _dispatch_frame(self, pkt_type: int, payload: bytes) -> None:
        if pkt_type == PacketType.TELEMETRY:
            t_ms, sp, pv, out, err = unpack_telemetry(payload)
            sample = TelemetrySample(t_ms, sp, pv, out, err)
            self._buffer.append(sample)
            self._plot.append(sample)
            self._step.append_sample(sample)
            self._update_rate(t_ms)
            self.statusBar().showMessage(
                f"采样 {len(self._buffer)} 条 | SP={sp:.2f} PV={pv:.2f} OUT={out:.2f} err={err:.2f}"
            )
        elif pkt_type == PacketType.PID_ACK:
            kp, ki, kd, sp, result = unpack_pid_ack(payload)
            self._param.show_ack(kp, ki, kd, sp, result)
            self._log.log(f"PID_ACK result={result} Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")
        elif pkt_type == PacketType.PONG:
            fw_ver, status = unpack_pong(payload)
            # 心跳每秒一次，PONG 就不能每秒刷一行日志：只记"第一次通"和
            # "状态变了"两次，往返时延看状态栏的 RTT 数值（card28）。
            first = not self._pong_logged
            if first or status != self._last_pong_status:
                suffix = "（心跳已通）" if first else ""
                self._log.log(
                    f"PONG 固件版本={fw_ver >> 4}.{fw_ver & 0x0F} 状态={status}{suffix}"
                )
            self._pong_logged = True
            self._last_pong_status = status
        elif pkt_type == PacketType.STEP_EVENT:
            phase, t_ms = unpack_step_event(payload)
            self._step.show_phase(phase)
            self._log.log(f"STEP_EVENT phase={phase} t={t_ms}ms")
        elif pkt_type == PacketType.ERROR:
            code, msg = unpack_error(payload)
            # ERROR 码中文化（card28）：数字 + 含义 + 固件原话 + 建议动作
            self._log.log(error_line(code, msg))
        else:
            line = self._unknown_logger.note(pkt_type, len(payload))
            if line:
                self._log.log(line)

    def _update_rate(self, t_ms: int) -> None:
        """按设备时间戳估算采样率，刷新顶部信息标签。

        用 t_ms 而不是墙钟：固件节拍是多少就显示多少，不受 UI 线程卡顿影响。
        设备复位后时间戳回绕成小值（dt<=0），此时保留上一段速率不显示负数。
        """
        if self._last_t_ms is not None:
            dt = t_ms - self._last_t_ms
            if dt > 0:
                self._rate_hz = 1000.0 / dt
        self._last_t_ms = t_ms
        if self._rate_hz > 0.0:
            self._rate_label.setText(f"采样 {len(self._buffer)} 条 · {self._rate_hz:.1f} Hz")
        else:
            self._rate_label.setText(f"采样 {len(self._buffer)} 条")

    def _record_parse_error(self, pkt_type: int, reason: str) -> None:
        """记一条解析异常（不会中断后续帧处理）。

        两件事分开做（卡8）：
        * 状态栏 ``解析异常 N`` 逐条累加，回答「有没有问题、共多少次」；
        * 日志按「帧类型 + 错误原因」聚合限流，回答「什么问题、什么时候、
          连续多少条」——坏帧雪崩时不再一行一条刷满 2000 行窗口。
        """
        self._parse_errors += 1
        # 0 不显示：状态栏常驻一个"坏帧 0"只会让人以为出过问题（card16）
        # 文案（卡30）：用"坏帧"而不是"解析异常 #3"，悬停给出三段式原因与下一步。
        self._parse_stat.setText(f"坏帧 {self._parse_errors}")
        self._parse_stat.setToolTip(
            parse_error_message(pkt_type, reason, self._parse_errors).as_line()
        )
        self._parse_stat.show()
        line = self._parse_logger.note(pkt_type, reason)
        if line is not None:
            self._log.log(line)
        key = (int(pkt_type), reason)
        if key not in self._explained_parse:
            self._explained_parse.add(key)
            self._log.log(parse_error_message(pkt_type, reason, 1).as_block())

    def _reset_log_aggregators(self) -> None:
        """清空日志后的聚合状态归零；状态栏累计计数刻意保留。"""
        self._parse_logger.reset()
        self._unknown_logger.reset()
        self._explained_parse.clear()

    def _clear_records(self) -> None:
        self._buffer.clear()
        self._plot.clear()
        self._step.clear_samples()
        self._last_t_ms = None
        self._rate_hz = 0.0
        self._rate_label.setText("采样 0 条")
        self.statusBar().showMessage("采样 0 条")
        self._log.log("已清空曲线与缓冲")

    def _on_zn_applied(self, kp: float, ki: float, kd: float) -> None:
        """Z-N 面板选定参数后，沿用当前 SP 组 PID_SET 下发。"""
        sp = self._param.current_sp()
        self._worker.send(PacketType.PID_SET, pack_pid_set(kp, ki, kd, sp))
        self._log.log(f"Z-N 下发 Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._save_ui_state()
        # 关窗口时把两个定时器停掉：心跳统计和自动重连都不该在退出流程里再触发
        self._stats_timer.stop()
        self._reconnect_timer.stop()
        self._worker.close()
        super().closeEvent(event)
