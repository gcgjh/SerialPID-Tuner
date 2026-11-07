"""PID 参数面板：参数输入、下发、ACK 回显。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import load_config
from ..protocol import PacketType, pack_pid_set, pack_sp_set


class ParamPanel(QWidget):
    def __init__(self, worker, config_path=None, parent=None) -> None:
        """``config_path``：覆盖默认配置文件路径（测试 / 便携运行用）。"""
        super().__init__(parent)
        self._worker = worker

        config, warning = load_config(config_path)
        limits = config["param_limits"]

        kp, ki, kd, sp = (limits["kp"], limits["ki"], limits["kd"], limits["sp"])
        self._kp = self._spin(kp["min"], kp["max"], kp["default"], kp["step"])
        self._ki = self._spin(ki["min"], ki["max"], ki["default"], ki["step"])
        self._kd = self._spin(kd["min"], kd["max"], kd["default"], kd["step"])
        self._sp = self._spin(sp["min"], sp["max"], sp["default"], sp["step"])

        form = QFormLayout()
        form.addRow("Kp", self._kp)
        form.addRow("Ki", self._ki)
        form.addRow("Kd", self._kd)
        form.addRow("SP", self._sp)

        self._apply_pid = QPushButton("下发 PID 参数")
        self._apply_sp = QPushButton("仅下发 SP")
        self._ack_label = QLabel("未收到 ACK")
        self._ack_label.setStyleSheet("color: #888;")

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._apply_pid)
        btn_row.addWidget(self._apply_sp)

        box = QGroupBox("PID 整定")
        box_layout = QVBoxLayout(box)
        box_layout.addLayout(form)
        box_layout.addLayout(btn_row)
        box_layout.addWidget(self._ack_label)

        outer = QVBoxLayout(self)
        outer.addWidget(box)

        self._apply_pid.clicked.connect(self._send_pid)
        self._apply_sp.clicked.connect(self._send_sp)

        if warning:
            self._ack_label.setText(f"配置：{warning}")
            self._ack_label.setStyleSheet("color: #e67e22;")
        box.setToolTip(
            "参数上下限来自配置文件（config.json），可放宽；"
            "下发前的数值有限性校验（NaN/Inf/float32 范围）不受影响"
        )

    @staticmethod
    def _spin(lo: float, hi: float, val: float, step: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(4)
        box.setSingleStep(step)
        box.setValue(val)
        return box

    def _send_pid(self) -> None:
        payload = pack_pid_set(self._kp.value(), self._ki.value(), self._kd.value(), self._sp.value())
        self._worker.send(PacketType.PID_SET, payload)

    def _send_sp(self) -> None:
        self._worker.send(PacketType.SP_SET, pack_sp_set(self._sp.value()))

    def current_sp(self) -> float:
        """当前界面上的设定值（供其它面板组 PID_SET 时复用）。"""
        return self._sp.value()

    def show_ack(self, kp: float, ki: float, kd: float, sp: float, result: int) -> None:
        if result == 0:
            self._ack_label.setText(f"ACK 已接受：Kp={kp:.3f} Ki={ki:.3f} Kd={kd:.3f} SP={sp:.2f}")
            self._ack_label.setStyleSheet("color: #2ecc71;")
        else:
            self._ack_label.setText("ACK 拒绝：参数越界")
            self._ack_label.setStyleSheet("color: #e74c3c;")
