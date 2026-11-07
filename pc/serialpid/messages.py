# -*- coding: utf-8 -*-
"""用户可见文案：所有错误提示统一成「现象 / 原因 / 下一步」三段。

背景（卡30）：界面里原来混着三种口吻——有的只说"连接失败"，有的直接抛
``解析异常 #3``，有的把 pyserial 的英文原文贴进日志。用户看完知道"坏了"，
但不知道"坏在哪、接下来该干什么"。

约定：

* **现象**：用户眼前发生了什么，用他能看到的对象描述（哪个端口、哪种数据）；
* **原因**：最可能的一到两个原因，不堆砌所有可能性；
* **下一步**：一条能直接照做的动作，最多两条。

本模块是纯文案与纯逻辑（不依赖 Qt），GUI 只负责把结果写进日志、状态栏或横幅。
技术细节（帧类型、字节长度、异常类名）保留在诊断通道里，但不作为用户提示的
主语——``_detail`` 字段就是给"复制诊断"用的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class UserMessage:
    """一条三段式提示。``detail`` 是可选的技术细节，只在诊断文本里出现。"""

    phenomenon: str
    cause: str
    next_step: str
    detail: str = ""

    def as_block(self) -> str:
        """多行形态（日志面板、诊断文本、横幅）。"""
        return (
            f"现象：{self.phenomenon}\n"
            f"原因：{self.cause}\n"
            f"下一步：{self.next_step}"
        )

    def as_line(self) -> str:
        """单行形态（状态栏、下拉占位项等只有一行的位置）。"""
        return (
            f"现象：{self.phenomenon}｜原因：{self.cause}｜下一步：{self.next_step}"
        )


# ---------------------------------------------------------------------------
# 串口打开失败：pyserial / WinError 的报错文本 -> 能照做的下一步
# ---------------------------------------------------------------------------
#: 关键字 -> (原因, 下一步)。pyserial 对所有失败都加
#: ``could not open port 'COMx':`` 前缀，前缀不能当判据；只认 errno、
#: 中文系统提示与异常类名（与卡25 的判定口径保持一致）。
SERIAL_OPEN_RULES = (
    (
        ("拒绝访问", "access is denied", "permissionerror", "permission denied",
         "errno 13", "(13,", "winerror 5"),
        "端口被占用或权限不足：多半是别的串口工具（串口助手 / 另一个本程序窗口）还开着",
        "关掉占用程序后重试；仍失败可尝试以管理员身份运行。",
    ),
    (
        ("busy", "占线", "没有发挥作用", "无法访问", "errno 16", "(16,", "winerror 31", "(31,"),
        "端口被其他程序独占或设备异常",
        "关闭占用该 COM 口的程序；USB 转串口拔插一次、必要时重装驱动后再连。",
    ),
    (
        ("找不到", "file not found", "filenotfounderror", "errno 2", "(2,", "winerror 2"),
        "端口不存在：设备可能被拔掉或换了口",
        "点「刷新端口」重新枚举后再连。",
    ),
)

#: 端口为空时给用户看的固定说法（下拉占位项与日志共用同一句）
EMPTY_PORT_TEXT = "未发现串口设备（USB 未插 / 驱动未装？）"


def serial_open_message(target: str, message: str) -> UserMessage:
    """串口打开失败：把库的原文翻成三段式；认不出时如实转述原因。"""
    text = (message or "").lower()
    for keywords, cause, action in SERIAL_OPEN_RULES:
        if any(word in text for word in keywords):
            return UserMessage(
                phenomenon=f"打不开串口 {target or '（未选端口）'}",
                cause=cause,
                next_step=action,
                detail=message or "",
            )
    return UserMessage(
        phenomenon=f"打不开串口 {target or '（未选端口）'}",
        cause=f"系统返回：{message or '未知原因'}",
        next_step="点「诊断端口」复制诊断文本；确认端口号与波特率后重试。",
        detail=message or "",
    )


def explain_serial_error(message: str) -> str:
    """一段话版本的串口失败说明（保留给日志与既有调用方）。

    认不出原因的报错原样返回——不臆造原因，比一句"未知错误"更有用。
    """
    text = (message or "").lower()
    for keywords, cause, action in SERIAL_OPEN_RULES:
        if any(word in text for word in keywords):
            return f"{cause}；{action}"
    return message or "未知原因"


# ---------------------------------------------------------------------------
# 端口为空 / 枚举失败
# ---------------------------------------------------------------------------


def empty_port_message() -> UserMessage:
    return UserMessage(
        phenomenon="没有可连接的串口，连接按钮不会发出任何帧",
        cause="系统没有枚举到串口设备：USB 线没插稳、USB 转串口驱动没装，或设备被别的程序占着",
        next_step="插好线后点「刷新端口」；没有硬件时把通道切到「内置模拟器」，不用端口也能跑通全流程。",
    )


def port_scan_failed_message(error: str = "") -> UserMessage:
    return UserMessage(
        phenomenon="串口枚举失败，下拉框里只有一个不可选中的说明项",
        cause=error or "枚举过程抛出异常（驱动或系统接口异常）",
        next_step="点「诊断端口」复制诊断文本；仍失败可切到「内置模拟器」继续调参。",
        detail=error,
    )


# ---------------------------------------------------------------------------
# 坏帧 / 解析异常
# ---------------------------------------------------------------------------

_FRAME_NAMES = {
    0x20: "遥测帧",
    0x21: "参数确认帧",
    0x22: "心跳应答帧",
    0x23: "阶跃事件帧",
    0x24: "错误帧",
}


def frame_name(pkt_type: int) -> str:
    """帧类型 -> 用户能看懂的名字（未登记的类型如实显示十六进制）。"""
    return _FRAME_NAMES.get(int(pkt_type), f"类型 0x{int(pkt_type):02X} 帧")


def parse_error_message(pkt_type: int, reason: str, count: int = 1) -> UserMessage:
    """坏帧提示：现象说清"收到几帧什么样的数据"，原因给最常见三条，下一步给动作。"""
    name = frame_name(pkt_type)
    return UserMessage(
        phenomenon=f"收到 {count} 帧格式不对的{name}（{reason}）",
        cause="帧长度或字段与协议对不上：常见于两侧波特率不一致、固件版本与上位机不匹配，或线路干扰",
        next_step="核对串口 115200 8N1 与协议 v1.0；换一根短 USB 线；持续出现就导出帧日志反馈给开发者。",
        detail=reason,
    )


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


def selftest_message(
    failures: Iterable[str] = (), warnings: Iterable[str] = ()
) -> UserMessage:
    """自检结果 -> 三段式；``failures`` / ``warnings`` 是"名称：说明"短句。"""
    failed = [item for item in failures if item]
    warned = [item for item in warnings if item]
    if failed:
        phenomenon = f"启动自检 {len(failed)} 项失败" + (
            f"、{len(warned)} 项警告" if warned else ""
        )
        cause = "；".join(failed[:3]) + (" 等" if len(failed) > 3 else "")
        severe = True
    else:
        phenomenon = f"启动自检 {len(warned)} 项警告"
        cause = "；".join(warned[:3]) + (" 等" if len(warned) > 3 else "")
        severe = False
    return UserMessage(
        phenomenon=phenomenon,
        cause=cause or "自检没有给出具体说明",
        next_step=(
            "点「复制诊断」把完整报告发给开发者；自检只报告不修复，程序仍可继续使用。"
            if not severe
            else "点「复制诊断」把完整报告发给开发者；先修好失败项再开始调参，避免把环境问题当成控制问题。"
        ),
    )


# ---------------------------------------------------------------------------
# 心跳超时（card28 新增判死原因）
# ---------------------------------------------------------------------------


def heartbeat_timeout_message(missed: int = 0, limit: int = 0) -> UserMessage:
    """心跳判死：必须说清"连了几次没应答"和"可以放宽"，否则用户只会怀疑程序坏了。

    ``missed`` / ``limit`` 未知时（0）退化成不带数字的说法，绝不编一个次数。
    """
    missed = max(int(missed or 0), 0)
    limit = max(int(limit or 0), 0)
    if missed and limit:
        phenomenon = f"对端连续 {missed} 次（容忍上限 {limit} 次）未应答心跳，已断开"
        next_step = (
            "检查设备供电与接线；固件只是忙、心跳回得慢时，"
            "可在「工具 → 链路设置…」里把「容忍次数」调大后重连。"
        )
    elif missed:
        phenomenon = f"对端连续 {missed} 次未应答心跳，已断开"
        next_step = (
            "检查设备供电与接线；固件心跳回得慢时，"
            "可在「工具 → 链路设置…」里把「容忍次数」调大后重连。"
        )
    else:
        phenomenon = "对端连续多次未应答心跳，已断开"
        next_step = (
            "检查设备供电与接线；固件心跳回得慢时，"
            "可在「工具 → 链路设置…」里把「容忍次数」调大后重连。"
        )
    return UserMessage(
        phenomenon=phenomenon,
        cause="心跳请求发出去了但没收到应答，且这段时间也没有遥测：设备死机、被复位，或固件没实现 PONG",
        next_step=next_step,
    )


# ---------------------------------------------------------------------------
# 后台线程回报的原始错误 / 预设文件操作失败
# ---------------------------------------------------------------------------


def worker_error_message(text: str) -> UserMessage:
    """接收线程回报的错误原文 -> 三段式；原文留在 ``detail`` 里给诊断用。

    线程只知道自己撞上了什么（``读取异常: ...`` / ``心跳发送失败: ...``），
    不知道用户该干什么。翻译成"这一层坏了会怎样 + 下一步"，比贴一句
    ``错误: ...`` 有用。
    """
    raw = str(text or "").strip()
    lowered = raw.lower()
    if "心跳" in raw or "ping" in lowered:
        return UserMessage(
            phenomenon="心跳帧没发出去，链路质量判断会失真",
            cause=raw or "写心跳时抛出异常",
            next_step="观察是否连续出现；反复出现就断开重连，并点「诊断端口」复制诊断文本反馈。",
            detail=raw,
        )
    if "写" in raw or "write" in lowered:
        return UserMessage(
            phenomenon="数据没有写进串口，这次下发/指令可能没生效",
            cause=raw or "写串口时抛出异常",
            next_step="先看设备是否仍在线；断开重连后重新下发一次，仍失败就换 USB 口或线复测。",
            detail=raw,
        )
    if "帧" in raw and "回调" in raw:
        return UserMessage(
            phenomenon="某一帧的数据没能交给界面，曲线上可能缺一个点",
            cause=raw or "帧处理回调抛出异常",
            next_step="偶发一次可忽略（下一帧会补上）；连续出现请导出帧日志反馈给开发者。",
            detail=raw,
        )
    if "读" in raw or "read" in lowered:
        return UserMessage(
            phenomenon="读取串口数据时出错，这一小段遥测可能缺失",
            cause=raw or "读串口时抛出异常",
            next_step="偶发一次可继续观察；连续出现请断开重连，或点「诊断端口」复制诊断文本。",
            detail=raw,
        )
    return UserMessage(
        phenomenon="后台线程报告了一个错误，链路可能已受影响",
        cause=raw or "线程回报错误但没有给出说明",
        next_step="偶发一次可继续观察；重复出现请点「诊断端口」复制诊断文本，或断开后重连。",
        detail=raw,
    )


#: 预设文件操作 -> (现象, 下一步)；原因由调用方带入具体异常文本
_PRESET_ACTIONS = {
    "保存": ("预设没有存下来", "换一个名字，或确认预设目录可写后重试。"),
    "加载": ("预设没有加载进来", "确认这个预设还在列表里；列表被别的窗口改过就点「刷新」。"),
    "删除": ("预设没有删掉", "确认预设文件没被别的程序占用，再重试一次。"),
    "重命名": ("预设没有改成新名字", "确认新名字不与已有预设重名，再重试一次。"),
}


def preset_error_message(action: str, error: str) -> UserMessage:
    """参数预设的增删改查失败：一句话形态够用（面板底部状态行只有一行）。"""
    phenomenon, next_step = _PRESET_ACTIONS.get(
        action, ("预设操作没有完成", "重试一次；仍失败请点「复制诊断」反馈给开发者。")
    )
    return UserMessage(
        phenomenon=phenomenon,
        cause=error or "预设文件读写失败（原因未知）",
        next_step=next_step,
        detail=error or "",
    )


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def join_names(items: Iterable[str], limit: int = 3) -> str:
    """把若干短句拼成"甲；乙；丙 等"，用于横幅一行显示。"""
    values = [str(item) for item in items if item]
    if not values:
        return ""
    head = "；".join(values[:limit])
    return head + (" 等" if len(values) > limit else "")


def optional_detail(message: Optional[UserMessage]) -> str:
    """诊断用的一行原文；没有原文时返回空串。"""
    return message.detail if message is not None else ""


__all__ = [
    "EMPTY_PORT_TEXT",
    "SERIAL_OPEN_RULES",
    "UserMessage",
    "empty_port_message",
    "explain_serial_error",
    "frame_name",
    "heartbeat_timeout_message",
    "join_names",
    "optional_detail",
    "parse_error_message",
    "port_scan_failed_message",
    "preset_error_message",
    "selftest_message",
    "serial_open_message",
    "worker_error_message",
]
