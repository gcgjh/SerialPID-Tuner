"""ESP32 真机首联：只跑「前三步 + 遥测节拍」，判据与 ``bringup_check`` 同源。

为什么单独一个入口而不是给 ``bringup_check.py`` 加参数：
    ESP32 板上没有外接 USB-TTL 探针（整条链路就是板上 CH340），
    第 1 步「UART 回环」的物理前提（TX↔RX 短接）在开发板上做不了；
    对 ESP32 来说"链路通"的可观测证据就是**能收到遥测帧**。
    其余判定全部直接 import 自 ``bringup_check``，不复制第二份口径。

    H750 路线的八步仍在 ``tools/bringup_check.py``，两条路线的判据不混。

用法::

    python tools/esp32_bringup.py --port COM5 --seconds 20

产物（默认写 ``docs/``）：

* ``esp32_frames_<时间>.jsonl``    每帧原始字节（上/下行）
* ``esp32_telemetry_<时间>.csv``   遥测样本
* ``esp32_report_<时间>.md``       四步结论 + 证据

退出码：0 = 全 PASS；1 = 有 FAIL；2 = 端口打不开。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

import bringup_check as bc  # noqa: E402  同目录脚本，复用 Link / 步骤 / 落盘
from serialpid.protocol import (  # noqa: E402
    PacketType, pack_reset, pack_sp_set, unpack_telemetry,
)


def step3_pid_set(link: bc.Link):
    """参数下发 ACK：回显一致 + result=接受；越界拒绝单独一行（见下）。

    为什么拆两行：
        ``bringup_check.step3_pid_set`` 把「越界值必须被拒」和「ACK 回显一致」
        合并成一个 PASS/FAIL。协议 §5 确实要求越界回 ``result=1``，但核心
        ``pid_app.c`` 只拒绝 NaN/Inf（见 app_handle_pid_set），不校验量程——
        STM32 与 ESP32 两条路线同源，行为一样。卡 45 明确「不改三层核心代码」，
        所以这里把「ACK 口径」照常判 PASS/FAIL，把「越界拒绝」如实标为已知缺口，
        而不是让它把整步拖成 FAIL、也不是悄悄放过。
    """
    acks, _all = link.transact(bc.GOLDEN_PID_SET, (PacketType.PID_ACK,), timeout=1.5)
    if not acks:
        row = bc._result("3 参数下发 ACK", "FAIL", "未收到 PID_ACK",
                         "固件未跑到 pid_app_poll / send_frame 失败", "无 ACK")
        return row, None

    kp, ki, kd, sp, result = bc.unpack_pid_ack(acks[0].payload)
    expect = (2.0, 0.5, 0.1, 30.0)
    diffs = [abs(a - b) for a, b in zip((kp, ki, kd, sp), expect)]
    echo_ok = max(diffs) <= 1e-6 and result == bc.AckResult.ACCEPTED

    row = bc._result(
        "3 参数下发 ACK", "PASS" if echo_ok else "FAIL",
        f"ACK 回显 kp={kp:g} ki={ki:g} kd={kd:g} sp={sp:g} result={result}",
        "无" if echo_ok else f"回显与下发不符，最大差 {max(diffs):.3g}",
        "PID_SET 16B → PID_ACK 17B，四参回显一致",
    )

    bad = bc.encode(PacketType.PID_SET, bc.pack_pid_set(1e9, 0.5, 0.1, 30.0))
    link.write(bad)
    bad_frames = link.collect(1.5, until=lambda got: any(
        f.type in (PacketType.PID_ACK, PacketType.ERROR) for _, f in got))
    rejected = any(f.type == PacketType.ERROR for _, f in bad_frames) or any(
        f.type == PacketType.PID_ACK and bc.unpack_pid_ack(f.payload)[4] != 0
        for _, f in bad_frames)

    gap = None
    if not rejected:
        gap = bc._result(
            "3b 越界拒绝（协议 §5）", "KNOWN-GAP",
            "Kp=1e9 被接受（result=0）",
            "核心 pid_app 只拒绝 NaN/Inf，未校验量程；两条硬件路线同源，"
            "修核心不属本卡范围（卡 45 约束：不改三层核心代码）",
            "PROTOCOL.md 0x11 要求 result=1=越界拒绝；pid_app.c app_handle_pid_set 仅做 finite 校验",
        )
    else:
        gap = bc._result("3b 越界拒绝（协议 §5）", "PASS",
                         "Kp=1e9 被拒", "无", "收到 ERROR 或 ACK result!=0")
    return row, gap


def step1_link(link: bc.Link, seconds: float = 3.0) -> dict:
    """ESP32 版第 1 步：打开端口后能否收到遥测（链路 + 波特率 + 固件在跑）。"""
    frames = link.collect(seconds)
    tele = [f for _, f in frames if f.type == PacketType.TELEMETRY]
    if not tele:
        return bc._result(
            "1 链路通路（ESP32 口径）", "FAIL",
            f"{seconds:g}s 内 0 帧遥测（总帧 {len(frames)}）",
            "端口/波特率不对，或固件没跑起来；先确认设备管理器里是 CH340 的那个 COM 口",
            "有原始字节但解不出遥测帧时，多为波特率或帧头被日志干扰",
        )
    return bc._result(
        "1 链路通路（ESP32 口径）", "PASS",
        f"{seconds:g}s 内收到 {len(tele)} 帧遥测（总帧 {len(frames)}）",
        "无",
        "整条链路只有板上 CH340，收到遥测即证明 TX 通路正确；"
        "ESP32 无外接 USB-TTL 探针，物理 TX-RX 短接回环不适用",
    )


def _read_sp(link: bc.Link, seconds: float = 1.5) -> Optional[float]:
    for _, frame in link.collect(seconds):
        if frame.type == PacketType.TELEMETRY:
            return unpack_telemetry(frame.payload)[1]
    return None



def step5_nvm(link: bc.Link) -> dict:
    """参数快照落 NVS 并能跨复位读回（ESP32 侧对应验收第 5 项）。

    可观测手段：协议没有「读回当前参数」的指令，但遥测帧里带 SP。
    所以下一条 SP_SET，等过 PID_NVM_SAVE_DELAY_MS（5 s）让核心去调 nvm_save，
    再 DTR 复位，看复位后遥测里的 SP 是不是还在——能读回就说明
    magic / CRC 校验通过（nvm_valid 不过就会退回默认参数）。
    """
    sp_before = _read_sp(link)
    if sp_before is None:
        return bc._result("5 NVS 参数快照", "FAIL", "无遥测，读不到 SP", "先做第 4 步", "0 帧")

    target = 42.5 if abs(sp_before - 42.5) > 0.5 else 37.25
    link.write(bc.encode(PacketType.SP_SET, pack_sp_set(target)))
    # 等核心的延迟保存窗口（5 s）过去，再复位
    link.collect(bc.NVM_SAVE_DELAY_S if hasattr(bc, "NVM_SAVE_DELAY_S") else 6.5)

    # 复位走协议软复位（0x71 RESET → bsp_system_reset → esp_restart）。
    # 比 DTR 可靠：本板自动下载电路失效，DTR/RTS 组合有概率把芯片打进下载模式。
    link.write(bc.encode(PacketType.RESET, pack_reset()))

    # 判复位是否真的发生：t_ms 是开机毫秒计数，复位后必须回到 0 附近。
    # 不加这条，复位没生效时 SP 读回原值会给出假 PASS。
    fresh = None
    for _t, frame in link.collect(3.0):
        if frame.type == PacketType.TELEMETRY and unpack_telemetry(frame.payload)[0] < 3000:
            fresh = frame
            break
    if fresh is None:
        return bc._result("5 NVS 参数快照", "FAIL", "复位后 t_ms 未归零",
                          "软复位没生效（bsp_system_reset 未接 esp_restart？）",
                          "3 s 内没有 t_ms<3000 的遥测帧，无法证明是重启后的读数")

    sp_after = unpack_telemetry(fresh.payload)[1]
    t_after = unpack_telemetry(fresh.payload)[0]

    ok = abs(sp_after - target) <= 0.01

    # 还原现场，别把板子留在 42.5
    link.write(bc.encode(PacketType.SP_SET, pack_sp_set(sp_before)))

    return bc._result(
        "5 NVS 参数快照", "PASS" if ok else "FAIL",
        f"SP {sp_before:.2f} → 写 {target:.2f} → 复位（t_ms={t_after}）→ 读回 {sp_after:.2f}",
        "无" if ok else "复位后参数没恢复：nvm_save 未落盘或 nvm_valid 校验未过（magic/CRC）",
        "pid_nvm_t 28 B（magic 0x50494431 + 四 float + mode + crc16/modbus 前 16 B）由核心计算，"
        "ESP32 侧只把整块 blob 存进 NVS namespace pids/key params",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ESP32 真机首联（前三步 + 遥测节拍）")
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=20.0, help="第 4 步遥测统计时长")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "docs"))
    parser.add_argument("--nvs", action="store_true",
                        help="追加第 5 步：参数快照写 NVS 并跨复位读回（会复位一次板子）")
    args = parser.parse_args(argv)

    link = bc.Link(port=args.port, baud=args.baud)
    try:
        name = link.open()
    except Exception as exc:  # noqa: BLE001 - 端口占用/不存在都属环境问题
        print(f"打开 {args.port} 失败：{exc}")
        print("先确认设备管理器里出现 USB-SERIAL CH340 (COMx)，且上位机界面没有占用该端口。")
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.out, exist_ok=True)
    frames_path = os.path.join(args.out, f"esp32_frames_{stamp}.jsonl")
    tele_path = os.path.join(args.out, f"esp32_telemetry_{stamp}.csv")
    report_path = os.path.join(args.out, f"esp32_report_{stamp}.md")

    results: List[dict] = []
    try:
        results.append(step1_link(link))
        results.append(bc.step2_ping(link))
        row_ack, row_gap = step3_pid_set(link)
        results.append(row_ack)
        if row_gap is not None:
            results.append(row_gap)
        results.append(bc.step4_telemetry(link, args.seconds))
        if args.nvs:
            results.append(step5_nvm(link))
    finally:
        link.close()

    bc.write_frames(link, frames_path)
    rows = bc.write_telemetry(link, tele_path)
    bc.write_report(results, report_path, name, "ESP32 真机（ESP-IDF 固件，无外接探针）")

    print(f"\n链路：{name}")
    for row in results:
        print(f"[{row['status']:>8}] {row['step']}：{row['measured']}")
        if row["status"] == "FAIL":
            print(f"           下一步：{row['diff']}")
    print(f"\n帧留痕：{os.path.abspath(frames_path)}")
    print(f"遥测 CSV：{os.path.abspath(tele_path)}（{rows} 行）")
    print(f"报告：{os.path.abspath(report_path)}")

    return 1 if any(row["status"] == "FAIL" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
