# -*- coding: utf-8 -*-
"""卡30 统一文案层：三段式格式、串口失败归类、坏帧说法、自检文案。

这些断言只碰纯字符串，不需要 Qt——文案层就是为此不依赖 Qt 的。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from serialpid.messages import (  # noqa: E402
    EMPTY_PORT_TEXT,
    UserMessage,
    empty_port_message,
    explain_serial_error,
    frame_name,
    heartbeat_timeout_message,
    join_names,
    optional_detail,
    parse_error_message,
    port_scan_failed_message,
    preset_error_message,
    selftest_message,
    serial_open_message,
    worker_error_message,
)


def test_user_message_two_shapes():
    message = UserMessage("打不开串口 COM3", "端口被占用", "关掉串口助手再试")
    block = message.as_block()
    assert block.splitlines() == [
        "现象：打不开串口 COM3",
        "原因：端口被占用",
        "下一步：关掉串口助手再试",
    ]
    line = message.as_line()
    assert line.startswith("现象：打不开串口 COM3｜原因：端口被占用｜下一步：")
    assert "\n" not in line


def test_serial_open_message_classifies_by_errno_not_library_prefix():
    """pyserial 给所有失败都加同一句前缀，分类必须看 errno / 中文提示。"""
    occupied = serial_open_message(
        "COM3", "could not open port 'COM3': PermissionError(13, '拒绝访问。', None, 5)"
    )
    assert "占用" in occupied.cause or "权限" in occupied.cause
    assert "关掉" in occupied.next_step

    missing = serial_open_message(
        "COM7", "could not open port 'COM7': FileNotFoundError(2, '系统找不到指定的文件。')"
    )
    assert "不存在" in missing.cause

    unknown = serial_open_message("COM9", "SomethingOddHappened")
    assert "SomethingOddHappened" in unknown.cause
    assert unknown.detail == "SomethingOddHappened"


def test_explain_serial_error_keeps_unknown_text_verbatim():
    assert explain_serial_error("SomeVendorError") == "SomeVendorError"
    assert explain_serial_error("") == "未知原因"


def test_empty_port_message_is_three_part_and_offers_simulator():
    message = empty_port_message()
    assert message.as_block().count("\n") == 2
    assert "内置模拟器" in message.next_step
    assert "未发现串口设备" in EMPTY_PORT_TEXT


def test_port_scan_failed_message_carries_error_detail():
    message = port_scan_failed_message("serial.tools.list_ports 抛异常")
    assert "枚举失败" in message.phenomenon
    assert message.cause == "serial.tools.list_ports 抛异常"
    assert message.detail == "serial.tools.list_ports 抛异常"


def test_frame_name_maps_known_types_and_shows_hex_for_unknown():
    assert frame_name(0x20) == "遥测帧"
    assert frame_name(0x23) == "阶跃事件帧"
    assert "0x7F" in frame_name(0x7F)


def test_parse_error_message_says_how_many_and_which_frames():
    message = parse_error_message(0x20, "载荷长度不足", 3)
    assert message.phenomenon == "收到 3 帧格式不对的遥测帧（载荷长度不足）"
    assert "波特率" in message.cause
    assert "115200" in message.next_step
    # 不给用户看"解析异常 #3"这种内部编号：现象里只有数量 + 帧名 + 原因
    assert "解析异常" not in message.as_block()


def test_selftest_message_prefers_failures_over_warnings():
    message = selftest_message(
        failures=["配置目录：不可写", "图标资源：缺失", "pyserial：未安装", "协议：往返不一致"],
        warnings=["日志目录：不存在"],
    )
    assert message.phenomenon.startswith("启动自检 4 项失败")
    assert "1 项警告" in message.phenomenon
    assert "等" in message.cause  # 超过三条只列前三条
    assert "先修好失败项" in message.next_step

    only_warn = selftest_message(warnings=["图标资源：缺失"])
    assert only_warn.phenomenon == "启动自检 1 项警告"
    assert "自检只报告不修复" in only_warn.next_step


def test_join_names_and_optional_detail():
    assert join_names(["甲", "乙", "丙", "丁"]) == "甲；乙；丙 等"
    assert join_names([]) == ""
    assert optional_detail(UserMessage("a", "b", "c", detail="raw")) == "raw"
    assert optional_detail(None) == ""


# ---------------------------------------------------------------- 卡35 新增


def test_heartbeat_timeout_message_counts_misses_and_points_at_setting():
    """心跳判死必须带次数；次数未知时如实退化，绝不编一个数字。"""
    message = heartbeat_timeout_message(3, 3)
    assert "连续 3 次" in message.phenomenon
    assert "容忍上限 3 次" in message.phenomenon
    assert "链路设置" in message.next_step
    assert "PONG" in message.cause

    only_missed = heartbeat_timeout_message(5, 0)
    assert "连续 5 次" in only_missed.phenomenon
    assert "容忍上限" not in only_missed.phenomenon

    unknown = heartbeat_timeout_message()
    assert "多次" in unknown.phenomenon
    assert "0" not in unknown.phenomenon        # 不编次数
    assert unknown.as_block().count("\n") == 2  # 仍是三行


def test_worker_error_message_classifies_and_keeps_raw_text():
    read = worker_error_message("读取异常: ClearCommError failed (拒绝访问)")
    assert "读" in read.phenomenon
    assert "读取异常" in read.cause
    assert read.detail == "读取异常: ClearCommError failed (拒绝访问)"

    write = worker_error_message("写入异常: 设备未就绪")
    assert "下发" in write.phenomenon or "没生效" in write.phenomenon

    heartbeat = worker_error_message("心跳发送失败: 端口已关闭")
    assert "心跳" in heartbeat.phenomenon

    unknown = worker_error_message("")
    assert unknown.cause            # 空原文也要给一句可读的原因
    assert "错误" in unknown.phenomenon


def test_preset_error_message_covers_four_actions_and_is_single_line():
    for action in ("保存", "加载", "删除", "重命名"):
        message = preset_error_message(action, "Errno 13 权限不足")
        line = message.as_line()
        assert "\n" not in line
        assert "权限不足" in line           # 原因原样转述
        assert line.startswith("现象：")
        assert "下一步：" in line
    assert preset_error_message("删除", "x").phenomenon == "预设没有删掉"
    # 未知动作也能给出一句人话，而不是 KeyError
    assert preset_error_message("未知动作", "boom").phenomenon
