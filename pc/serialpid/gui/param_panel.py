"""PID 参数面板：参数输入、下发、ACK 回显、预设管理。

预设（``preset_manager``）接在面板内部，不经主窗口：
    * 保存 / 加载 / 管理三件事收进一个「预设…」菜单按钮（卡20），行内只留
      「下拉框 + 菜单按钮」，面板高度不增加；原先四个控件挤一行时下拉只剩
      半截文字、三个按钮各约 40 px，误点"保存"会覆盖正在调的那组；
    * **加载只填界面输入框，绝不自动下发**——板子上可能正跑着一组能用的
      参数，直接写进去会让现场设备失控，这是安全边界不是交互偏好；
    * 预设文件与 ``config.json`` 同目录（含 ``SERIALPID_CONFIG`` 覆盖与便携
      运行），不另建一套路径规则。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional, Tuple

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import load_config, default_config_path
from ..preset_manager import (
    NAME_MAX_LEN,
    NOTE_MAX_LEN,
    Preset,
    PresetError,
    PresetManager,
    default_presets_path,
)
from ..protocol import PacketType, pack_pid_set, pack_sp_set

#: 预设默认名里的“对象名”。当前产品只支持单对象（PID 回路），
#: 将来引入多对象时这里改成配置项即可。
OBJECT_NAME = "PID"
_PRESETS_FILE_NAME = "presets.json"


def presets_path_for(config_path: Optional[str] = None) -> str:
    """按配置模块的规则推导预设文件路径（两者始终同目录）。

    优先级与 ``config.default_config_path`` 一致：
    显式 ``config_path`` > ``SERIALPID_CONFIG`` > 平台默认目录。
    这样便携运行（环境变量指到 U 盘）时配置与预设一起搬走，不会一半留在
    ``%APPDATA%``；测试注入临时目录时也不会污染真实用户数据。
    """
    base = config_path if config_path is not None else os.environ.get("SERIALPID_CONFIG")
    if base:
        folder = os.path.dirname(os.path.abspath(os.fspath(base)))
        return os.path.join(folder, _PRESETS_FILE_NAME)
    return default_presets_path()


class _PresetSaveDialog(QDialog):
    """保存预设：名称 + 可选备注，一次填完。"""

    def __init__(self, default_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("保存预设")

        self._name = QLineEdit(default_name, self)
        self._name.setMaxLength(NAME_MAX_LEN)
        self._note = QLineEdit(self)
        self._note.setMaxLength(NOTE_MAX_LEN)
        self._note.setPlaceholderText("可选备注：这组参数用在什么对象、当时的现象")

        form = QFormLayout()
        form.addRow("名称", self._name)
        form.addRow("备注", self._note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self._name.selectAll()

    def values(self) -> Tuple[str, str]:
        return self._name.text().strip(), self._note.text().strip()


class _PresetManageDialog(QDialog):
    """管理预设：删除与重命名（删除走面板的二次确认）。"""

    def __init__(self, panel: "ParamPanel", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理预设")
        self.resize(380, 260)
        self._panel = panel

        self._list = QListWidget(self)
        self._rename_btn = QPushButton("重命名", self)
        self._delete_btn = QPushButton("删除", self)
        self._close_btn = QPushButton("关闭", self)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._rename_btn)
        btn_row.addWidget(self._delete_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._close_btn)

        path_label = QLabel(f"文件：{panel.presets_path}", self)
        path_label.setWordWrap(True)
        path_label.setStyleSheet("color: #888;")

        layout = QVBoxLayout(self)
        layout.addWidget(path_label)
        layout.addWidget(self._list, 1)
        layout.addLayout(btn_row)

        self._rename_btn.clicked.connect(self._rename_selected)
        self._delete_btn.clicked.connect(self._delete_selected)
        self._close_btn.clicked.connect(self.accept)
        self._list.itemSelectionChanged.connect(self._sync_buttons)

        self._reload()

    def _reload(self) -> None:
        self._list.clear()
        self._list.addItems(self._panel.preset_names())
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_selection = self._list.currentItem() is not None
        self._rename_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    def _current_name(self) -> Optional[str]:
        item = self._list.currentItem()
        return item.text() if item is not None else None

    def _rename_selected(self) -> None:
        name = self._current_name()
        if not name:
            return
        new_name, ok = QInputDialog.getText(
            self, "重命名预设", f"「{name}」的新名称：", QLineEdit.Normal, name
        )
        if ok and new_name.strip() and new_name.strip() != name:
            self._panel.rename_preset(name, new_name)
            self._reload()

    def _delete_selected(self) -> None:
        name = self._current_name()
        if not name:
            return
        if self._panel.delete_preset(name):
            self._reload()


class ParamPanel(QWidget):
    def __init__(
        self,
        worker,
        config_path=None,
        presets_path=None,
        parent=None,
    ) -> None:
        """``config_path`` / ``presets_path``：覆盖默认路径（测试 / 便携运行用）。"""
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
        # 输入框横向撑满表单（卡20）：默认策略按建议宽度摆放，340 px 面板里
        # 右侧会空出约 100 px，看着像"还有控件没显示"。
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Kp", self._kp)
        form.addRow("Ki", self._ki)
        form.addRow("Kd", self._kd)
        form.addRow("SP", self._sp)

        # ---- 预设（卡13 接入 / 卡20 排布）：一行控件，高度不增 ----
        self._presets = PresetManager(
            presets_path if presets_path is not None else presets_path_for(config_path)
        )
        self._loaded_name: Optional[str] = None

        self._preset_box = QComboBox(self)
        self._preset_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        # 不按最长条目定宽：面板只有 340 px，优先让名字在行内显示完整
        self._preset_box.setMinimumContentsLength(8)
        self._preset_box.setToolTip("选择一组已保存的预设；只填界面，不会自动下发")

        # 三个动作收进一个菜单按钮（卡20 方案二）：
        #   原来"下拉 + 保存 + 加载 + 管理"挤一行，下拉被压到只剩前半截，
        #   三个按钮各约 40 px 紧邻，误点"保存"会覆盖正在调的那组参数。
        #   收进菜单后行内只需"下拉 + 一个按钮"，面板高度不增加。
        self._preset_menu_btn = QPushButton("预设…", self)
        self._preset_menu_btn.setToolTip("保存当前参数 / 加载选中预设 / 管理（删除、重命名）")
        self._preset_menu_btn.setMaximumWidth(80)
        preset_menu = QMenu(self._preset_menu_btn)
        self._preset_action_save = preset_menu.addAction("保存当前参数…")
        self._preset_action_load = preset_menu.addAction("加载选中预设（只填界面）")
        self._preset_action_manage = preset_menu.addAction("管理预设（删除 / 重命名）…")
        self._preset_menu_btn.setMenu(preset_menu)

        preset_row = QHBoxLayout()
        preset_row.addWidget(self._preset_box, 1)
        preset_row.addWidget(self._preset_menu_btn)

        self._preset_status = QLabel("", self)
        self._preset_status.setWordWrap(True)
        self._preset_status.setStyleSheet("color: #888;")

        self._apply_pid = QPushButton("下发 PID 参数")
        self._apply_pid.setToolTip("把界面上的 Kp/Ki/Kd/SP 打包下发")
        self._apply_sp = QPushButton("仅下发 SP")
        self._apply_sp.setToolTip("只把设定值下发到板子，不动 PID 增益")
        self._ack_label = QLabel("未下发")
        self._ack_label.setStyleSheet("color: #888;")

        # 两个下发按钮等宽（卡20）：等拉伸，不用固定宽度——换字体 / 换语言也不会不齐
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._apply_pid, 1)
        btn_row.addWidget(self._apply_sp, 1)

        box = QGroupBox("PID 整定")
        box_layout = QVBoxLayout(box)
        box_layout.addLayout(form)
        box_layout.addLayout(preset_row)
        box_layout.addWidget(self._preset_status)
        box_layout.addLayout(btn_row)
        box_layout.addWidget(self._ack_label)

        outer = QVBoxLayout(self)
        outer.addWidget(box)

        self._apply_pid.clicked.connect(self._send_pid)
        self._apply_sp.clicked.connect(self._send_sp)
        self._preset_action_save.triggered.connect(lambda *_: self._save_preset())
        self._preset_action_load.triggered.connect(lambda *_: self._load_preset())
        self._preset_action_manage.triggered.connect(lambda *_: self._manage_presets())

        if warning:
            self._ack_label.setText(f"配置：{warning}")
            self._ack_label.setStyleSheet("color: #e67e22;")
        box.setToolTip(
            "参数上下限来自配置文件（config.json），可放宽；"
            "下发前的数值有限性校验（NaN/Inf/float32 范围）不受影响"
        )

        self.refresh_presets()
        if self._presets.warning:
            self._set_status(f"预设：{self._presets.warning}", warn=True)
        else:
            # 「已保存 N 组」挪进下拉的 tooltip（卡20）：状态行只留"当前在用什么"
            self._set_status("未加载预设")

    # ---- 通用 ----

    @staticmethod
    def _spin(lo: float, hi: float, val: float, step: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(lo, hi)
        box.setDecimals(4)
        box.setSingleStep(step)
        box.setValue(val)
        return box

    @staticmethod
    def _default_preset_name() -> str:
        """默认名：对象名 + 日期时间，用户可直接改。"""
        return f"{OBJECT_NAME}-{datetime.now():%Y%m%d-%H%M}"

    @property
    def presets_path(self) -> str:
        return self._presets.path

    def preset_names(self) -> List[str]:
        return self._presets.names()

    def refresh_presets(self, select: Optional[str] = None) -> None:
        """重建下拉框；``select`` 指定选中项（不存在则忽略）。

        显示文本只放名称（卡20）：长文本会把下拉压成半截，「已保存 N 组」
        这类计数信息挪到 tooltip；空列表给占位「未选择预设」而不是「（无预设）」，
        避免用户以为选错了什么。
        """
        names = self._presets.names()
        self._preset_box.blockSignals(True)
        self._preset_box.clear()
        if names:
            self._preset_box.addItems(names)
        else:
            self._preset_box.addItem("未选择预设")
        self._preset_box.blockSignals(False)
        self._preset_box.setToolTip(
            f"已保存 {len(names)} 组预设；选择后走「预设…→加载选中预设」只填界面，不会自动下发"
        )
        self._preset_action_load.setEnabled(bool(names))
        self._preset_action_manage.setEnabled(bool(names))
        if select:
            index = self._preset_box.findText(select)
            if index >= 0:
                self._preset_box.setCurrentIndex(index)

    def _set_status(self, text: str, *, warn: bool = False, error: bool = False) -> None:
        self._preset_status.setText(text)
        if error:
            self._preset_status.setStyleSheet("color: #e74c3c;")
        elif warn:
            self._preset_status.setStyleSheet("color: #e67e22;")
        else:
            self._preset_status.setStyleSheet("color: #888;")

    # ---- 保存 ----

    def _save_preset(self) -> None:
        result = self._ask_preset_save(self._default_preset_name())
        if result is None:
            return
        name, note = result
        try:
            self.store_preset(name, note)
        except PresetError as exc:
            self._set_status(f"保存失败：{exc}", error=True)

    def _ask_preset_save(self, default_name: str) -> Optional[Tuple[str, str]]:
        """弹命名对话框；测试可替换此方法跳过 GUI 交互。"""
        dialog = _PresetSaveDialog(default_name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.values()

    def _confirm_overwrite(self, name: str) -> bool:
        answer = QMessageBox.question(
            self,
            "覆盖预设",
            f"预设「{name}」已存在，是否覆盖？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def store_preset(self, name: str, note: str = "") -> Optional[Preset]:
        """把当前界面数值存成预设；同名时先确认覆盖。取消返回 None。"""
        if name.strip().casefold() in {n.casefold() for n in self._presets.names()}:
            if not self._confirm_overwrite(name.strip()):
                self._set_status(f"已取消保存：{name.strip()}")
                return None
        preset = Preset(
            name=name,
            kp=self._kp.value(),
            ki=self._ki.value(),
            kd=self._kd.value(),
            sp=self._sp.value(),
            note=note,
        )
        saved = self._presets.save(preset)
        self.refresh_presets(select=saved.name)
        self._loaded_name = saved.name
        self._set_status(
            f"已保存预设：{saved.name}"
            f"（Kp={saved.kp:g} Ki={saved.ki:g} Kd={saved.kd:g} SP={saved.sp:g}）"
        )
        return saved

    # ---- 加载（只填界面，不下发）----

    def apply_preset(self, preset: Preset) -> List[str]:
        """把预设数值填进输入框，返回超出当前范围被按边界填入的说明。"""
        clamped: List[str] = []
        targets = (
            (self._kp, "Kp", preset.kp),
            (self._ki, "Ki", preset.ki),
            (self._kd, "Kd", preset.kd),
            (self._sp, "SP", preset.sp),
        )
        for box, label, value in targets:
            if value is None:
                continue
            low, high = box.minimum(), box.maximum()
            if value < low or value > high:
                filled = min(max(value, low), high)
                box.setValue(filled)
                clamped.append(
                    f"{label} {value:g} 超出当前范围 [{low:g}, {high:g}]，"
                    f"已按边界 {filled:g} 填入"
                )
            else:
                box.setValue(value)
        return clamped

    def _load_preset(self) -> None:
        name = self._preset_box.currentText().strip()
        if not name or name not in self._presets:
            return
        try:
            preset = self._presets.get(name)
        except PresetError as exc:
            self._set_status(f"加载失败：{exc}", error=True)
            return

        clamped = self.apply_preset(preset)
        self._loaded_name = preset.name

        head = f"当前预设：{preset.name}（已填入界面，未下发）"
        extras: List[str] = []
        if preset.sp is None:
            extras.append("该预设未固定 SP，沿用当前设定值")
        extras.extend(clamped)
        if clamped:
            self._set_status("；".join([head] + extras), warn=True)
        elif extras:
            self._set_status("；".join([head] + extras))
        else:
            self._set_status(head)

    # ---- 管理 ----

    def _manage_presets(self) -> None:
        dialog = _PresetManageDialog(self, self)
        dialog.exec()
        self.refresh_presets(select=self._loaded_name)
        if self._loaded_name and self._loaded_name.strip().casefold() not in {
            n.casefold() for n in self._presets.names()
        }:
            self._loaded_name = None
            self._set_status("当前预设已被删除，界面数值保持不变", warn=True)

    def _confirm_delete(self, name: str) -> bool:
        answer = QMessageBox.question(
            self,
            "删除预设",
            f"确定删除预设「{name}」？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def delete_preset(self, name: str) -> bool:
        """删除预设（默认二次确认，不可撤销）。返回是否真的删掉。"""
        if not self._confirm_delete(name):
            return False
        try:
            self._presets.delete(name)
        except PresetError as exc:
            self._set_status(f"删除失败：{exc}", error=True)
            return False
        if self._loaded_name and self._loaded_name.strip().casefold() == name.strip().casefold():
            self._loaded_name = None
            self._set_status(f"已删除预设：{name}（界面数值保持不变，未下发）", warn=True)
        else:
            self._set_status(f"已删除预设：{name}", warn=True)
        self.refresh_presets(select=self._loaded_name)
        return True

    def rename_preset(self, old_name: str, new_name: str) -> bool:
        try:
            preset = self._presets.rename(old_name, new_name)
        except PresetError as exc:
            self._set_status(f"重命名失败：{exc}", error=True)
            return False
        if self._loaded_name and self._loaded_name.strip().casefold() == old_name.strip().casefold():
            self._loaded_name = preset.name
            self.refresh_presets(select=preset.name)
            self._set_status(f"当前预设已重命名为：{preset.name}")
            return True
        self.refresh_presets(select=preset.name)
        self._set_status(f"已重命名为：{preset.name}")
        return True

    # ---- 下发 ----

    def _send_pid(self) -> None:
        payload = pack_pid_set(self._kp.value(), self._ki.value(), self._kd.value(), self._sp.value())
        self._worker.send(PacketType.PID_SET, payload)
        self._mark_sent("PID 参数")

    def _send_sp(self) -> None:
        self._worker.send(PacketType.SP_SET, pack_sp_set(self._sp.value()))
        self._mark_sent("设定值 SP")

    def _mark_sent(self, what: str) -> None:
        """下发后更新状态行（卡20）：没连上就明说"没发出去"，不用"等待 ACK"糊弄。

        颜色与含义一一对应：橙＝没发出去，灰＝已发等待应答，绿＝ACK 接受（带回显），
        红＝ACK 拒绝。
        """
        connected = bool(getattr(self._worker, "connected", False))
        if connected:
            self._ack_label.setText(f"{what} 已下发，等待 ACK…")
            self._ack_label.setStyleSheet("color: #888;")
        else:
            self._ack_label.setText(f"未连接：{what} 未下发")
            self._ack_label.setStyleSheet("color: #e67e22;")

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
