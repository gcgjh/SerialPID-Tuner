"""主窗口：连接管理 + 面板组装 + 帧分发。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)
from PySide6.QtCore import Qt

from ..comm_qt import CommWorker
from ..frame_log import ParseErrorLogger, UnknownFrameLogger
from ..model import TelemetryBuffer, TelemetrySample
from ..protocol import (
    PacketType,
    pack_pid_set,
    payload_error_reason,
    unpack_error,
    unpack_pid_ack,
    unpack_pong,
    unpack_telemetry,
    unpack_step_event,
)
from ..transport import list_serial_ports
from .log_panel import LogPanel
from .param_panel import ParamPanel
from .plot_panel import PlotPanel
from .step_panel import StepPanel
from .zn_panel import ZnPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SerialPID-Tuner")
        self.resize(1280, 800)

        self._worker = CommWorker()
        self._buffer = TelemetryBuffer()

        self._parse_errors = 0
        self._parse_stat = QLabel("解析异常 0")
        self._unknown_logger = UnknownFrameLogger()
        # 坏帧限流（卡8）：按「帧类型 + 错误原因」聚合，与未知帧共用限流实现。
        # 状态栏累计计数与该聚合器互不耦合：清空日志只清聚合状态，不清计数。
        self._parse_logger = ParseErrorLogger()

        self._plot = PlotPanel()
        self._param = ParamPanel(self._worker)
        self._step = StepPanel(self._worker)
        self._zn = ZnPanel()
        self._log = LogPanel(self._buffer)

        self._build_topbar()
        self._build_layout()
        self._connect_signals()

        self._log.log("就绪：选择 TCP 模拟器或串口后点击连接")

    # ---- 界面 ----

    def _build_topbar(self) -> None:
        self._transport = QComboBox()
        self._transport.addItems(["TCP", "Serial"])

        self._target = QComboBox()
        self._target.setEditable(True)
        self._target.setMinimumWidth(160)
        self._target.setEditText("127.0.0.1:9000")

        self._baud = QComboBox()
        self._baud.addItems(["115200", "9600", "38400", "57600"])

        self._refresh = QPushButton("刷新端口")
        self._connect_btn = QPushButton("连接")
        self._state_label = QLabel("未连接")
        self._state_label.setStyleSheet("color: #888;")

        bar = QHBoxLayout()
        bar.addWidget(QLabel("通道"))
        bar.addWidget(self._transport)
        bar.addWidget(QLabel("目标"))
        bar.addWidget(self._target)
        bar.addWidget(QLabel("波特率"))
        bar.addWidget(self._baud)
        bar.addWidget(self._refresh)
        bar.addWidget(self._connect_btn)
        bar.addWidget(self._state_label, stretch=1)

        self._topbar = QWidget()
        self._topbar.setLayout(bar)
        self._refresh_ports()

    def _build_layout(self) -> None:
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.addWidget(self._param)
        left_layout.addWidget(self._step)
        left_layout.addWidget(self._zn)
        left_layout.addWidget(self._log, stretch=1)
        left.setFixedWidth(340)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(self._plot)
        splitter.setStretchFactor(1, 1)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._topbar)
        layout.addWidget(splitter, stretch=1)
        self.setCentralWidget(central)

        self.statusBar().showMessage("采样 0 条")
        self.statusBar().addPermanentWidget(self._parse_stat)

    def _connect_signals(self) -> None:
        self._connect_btn.clicked.connect(self._toggle_connection)
        self._refresh.clicked.connect(self._refresh_ports)
        self._transport.currentTextChanged.connect(self._on_transport_changed)

        self._worker.frameReceived.connect(self._on_frame)
        self._worker.connectionChanged.connect(self._on_connection)
        self._worker.errorOccurred.connect(lambda msg: self._log.log(f"错误: {msg}"))
        self._zn.applied.connect(self._on_zn_applied)

        self._log.clear_buffer_signal().clicked.connect(self._clear_records)
        # “清空日志”只重置聚合状态，不动状态栏累计计数（那是本机开机以来的量）
        self._log.clear_log_signal().clicked.connect(self._reset_log_aggregators)

    # ---- 连接管理 ----

    def _refresh_ports(self) -> None:
        if self._transport.currentText() == "Serial":
            ports = list_serial_ports()
            self._target.clear()
            self._target.addItems(ports or ["COM1"])
        else:
            self._target.clear()
            self._target.setEditText("127.0.0.1:9000")

    def _on_transport_changed(self, _text: str) -> None:
        self._refresh_ports()

    def _toggle_connection(self) -> None:
        if self._worker.connected:
            self._worker.close()
            return

        kind = "tcp" if self._transport.currentText() == "TCP" else "serial"
        target = self._target.currentText().strip()
        baud = int(self._baud.currentText())
        ok = self._worker.open(kind, target, baud)
        if ok:
            self._log.log(f"已连接 {kind}://{target}")
        else:
            self._log.log(f"连接失败 {target}")

    def _on_connection(self, connected: bool, detail: str) -> None:
        if connected:
            # 新会话：绘图时间基准从 0 重新起算（card11）。
            # 板子复位 / 重新上电后时间戳从 0 重新计数，若沿用上一场的零点，
            # 横轴会立刻跳到负数区间，时间窗名存实亡。
            # 边界：这里只重置绘图时间轴；主窗口遥测缓冲与阶跃分析仍保留上一会话
            # 样本（导出 / 统计会看到两段 t_ms），需要一并清零时走 _clear_records。
            self._plot.restart_timeline("连接建立，时间轴从 0 起算")
            self._connect_btn.setText("断开")
            self._state_label.setText("已连接")
            self._state_label.setStyleSheet("color: #2ecc71;")
        else:
            self._connect_btn.setText("连接")
            self._state_label.setText(f"未连接（{detail}）")
            self._state_label.setStyleSheet("color: #e74c3c;")
            self._log.log(f"连接状态: {detail}")

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
            self.statusBar().showMessage(
                f"采样 {len(self._buffer)} 条 | SP={sp:.2f} PV={pv:.2f} OUT={out:.2f} err={err:.2f}"
            )
        elif pkt_type == PacketType.PID_ACK:
            kp, ki, kd, sp, result = unpack_pid_ack(payload)
            self._param.show_ack(kp, ki, kd, sp, result)
            self._log.log(f"PID_ACK result={result} Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")
        elif pkt_type == PacketType.PONG:
            fw_ver, status = unpack_pong(payload)
            self._log.log(f"PONG 固件版本={fw_ver >> 4}.{fw_ver & 0x0F} 状态={status}")
        elif pkt_type == PacketType.STEP_EVENT:
            phase, t_ms = unpack_step_event(payload)
            self._step.show_phase(phase)
            self._log.log(f"STEP_EVENT phase={phase} t={t_ms}ms")
        elif pkt_type == PacketType.ERROR:
            code, msg = unpack_error(payload)
            self._log.log(f"ERROR code={code} {msg}")
        else:
            line = self._unknown_logger.note(pkt_type, len(payload))
            if line:
                self._log.log(line)

    def _record_parse_error(self, pkt_type: int, reason: str) -> None:
        """记一条解析异常（不会中断后续帧处理）。

        两件事分开做（卡8）：
        * 状态栏 ``解析异常 N`` 逐条累加，回答「有没有问题、共多少次」；
        * 日志按「帧类型 + 错误原因」聚合限流，回答「什么问题、什么时候、
          连续多少条」——坏帧雪崩时不再一行一条刷满 2000 行窗口。
        """
        self._parse_errors += 1
        self._parse_stat.setText(f"解析异常 {self._parse_errors}")
        line = self._parse_logger.note(pkt_type, reason)
        if line is not None:
            self._log.log(line)

    def _reset_log_aggregators(self) -> None:
        """清空日志后的聚合状态归零；状态栏累计计数刻意保留。"""
        self._parse_logger.reset()
        self._unknown_logger.reset()

    def _clear_records(self) -> None:
        self._buffer.clear()
        self._plot.clear()
        self._step.clear_samples()
        self.statusBar().showMessage("采样 0 条")
        self._log.log("已清空曲线与缓冲")

    def _on_zn_applied(self, kp: float, ki: float, kd: float) -> None:
        """Z-N 面板选定参数后，沿用当前 SP 组 PID_SET 下发。"""
        sp = self._param.current_sp()
        self._worker.send(PacketType.PID_SET, pack_pid_set(kp, ki, kd, sp))
        self._log.log(f"Z-N 下发 Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._worker.close()
        super().closeEvent(event)
