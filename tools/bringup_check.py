"""真机首联自动核对：把 ``docs/bringup.md`` 第 3 节八步落成一条命令。

为什么要脚本：八步里有六步是"发一帧 / 等一帧 / 算间隔"，手工点串口工具既慢又
记不住数；脚本把所有原始帧、时间戳、遥测样本落盘，报告里的每个结论都能回溯。

用法::

    # 正常首联（板子已烧好固件，CH340 接好）
    python tools/bringup_check.py --port COM3 --seconds 60

    # 先做第 1 步 UART 回环（需要把 TX-RX 短接，做完记得拔掉短接）
    python tools/bringup_check.py --port COM3 --loopback

    # 第 8 步 NVM 掉电验证拆成两半（中间要人工断电）
    python tools/bringup_check.py --port COM3 --nvm-write     # 写 SP=50，等 5s 防抖
    ...拔掉 USB / 断 3.3V，等 2 秒，重新上电...
    python tools/bringup_check.py --port COM3 --nvm-verify    # 只读遥测，不下发任何参数

    # 没有真板子时用离线模拟器把脚本本身跑一遍（结果标注"非真机"）
    python tools/fake_mcu.py --port 19011
    python tools/bringup_check.py --tcp 127.0.0.1:19011 --seconds 10

产物（默认写到 ``docs/``）：

* ``bringup_frames_<时间>.jsonl``：每一帧原始字节与方向，逐条 flush，可 diff；
* ``bringup_telemetry_<时间>.csv``：遥测样本（t_ms / sp / pv / out / err / 收帧时刻）；
* ``bringup_report_<时间>.md``：每步 实测值 / 与预期差异 / 证据，含 PASS/FAIL 判定。

退出码：0 = 自动项全 PASS；1 = 有 FAIL；2 = 无可用串口 / 连不上（环境问题）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pc"))

from serialpid.protocol import (  # noqa: E402
    AckResult,
    Frame,
    FrameDecoder,
    PacketType,
    StepPhase,
    encode,
    parse_fw_version,
    pack_pid_set,
    pack_step_start,
    unpack_error,
    unpack_pid_ack,
    unpack_pong,
    unpack_step_event,
    unpack_telemetry,
)

GOLDEN_PING = bytes.fromhex("AA550001C07077777777")
GOLDEN_PID_SET = bytes.fromhex(
    "AA551010000000400000003FCDCCCC3D0000F041AA2977777777"
)

#: 遥测期望周期（ms）
TELEMETRY_PERIOD_MS = 50.0


# --------------------------------------------------------------------------- #
# 链路：串口 / TCP 两种，都只暴露 open / write / collect
# --------------------------------------------------------------------------- #


class Link:
    def __init__(self, port: Optional[str] = None, baud: int = 115200,
                 tcp: Optional[str] = None) -> None:
        self.port = port
        self.baud = baud
        self.tcp = tcp
        self._ser = None
        self._sock = None
        self._decoder = FrameDecoder()
        self._name = ""
        #: 原始帧留痕：{t, dir, type, raw}
        self.records: List[dict] = []

    def open(self) -> str:
        if self.tcp:
            import socket

            host, _, port_text = self.tcp.partition(":")
            self._sock = socket.create_connection((host or "127.0.0.1", int(port_text or 9000)),
                                                  timeout=2.0)
            self._sock.settimeout(0.05)
            self._name = f"tcp://{self.tcp}"
        else:
            import serial

            self._ser = serial.Serial(self.port, self.baud, timeout=0.05)
            self._name = f"{self.port} @ {self.baud} 8N1"
        return self._name

    def close(self) -> None:
        for obj in (self._ser, self._sock):
            if obj is not None:
                try:
                    obj.close()
                except OSError:
                    pass
        self._ser = self._sock = None

    def write(self, raw: bytes) -> None:
        if self._ser is not None:
            self._ser.write(raw)
        elif self._sock is not None:
            self._sock.sendall(raw)
        else:
            raise RuntimeError("链路未打开")
        self.records.append({"t": round(time.monotonic(), 3), "dir": "up",
                             "type": raw[3] if len(raw) > 3 else -1,
                             "raw": raw.hex()})

    def collect(self, seconds: float, until=None) -> List[Tuple[float, Frame]]:
        """收 ``seconds`` 秒的帧；``until(frames)`` 返回真可提前结束。"""
        frames: List[Tuple[float, Frame]] = []
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            chunk = b""
            try:
                if self._ser is not None:
                    chunk = self._ser.read(512)
                elif self._sock is not None:
                    chunk = self._sock.recv(4096)
            except OSError:
                chunk = b""
            if chunk:
                now = time.monotonic()
                for frame in self._decoder.feed(chunk):
                    frames.append((now, frame))
                    self.records.append({"t": round(now, 6), "dir": "down",
                                         "type": frame.type, "raw": encode(
                                             frame.type, frame.payload).hex()})
                if until is not None and until(frames):
                    break
            else:
                time.sleep(0.005)
        return frames

    def transact(self, raw: bytes, expect: Tuple[int, ...], timeout: float = 1.5):
        self.write(raw)
        frames = self.collect(timeout, until=lambda got: any(f.type in expect for _, f in got))
        return [frame for _, frame in frames if frame.type in expect], frames


def _find_ports() -> List[str]:
    try:
        from serial.tools import list_ports

        return [p.device for p in list_ports.comports()]
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# 八个步骤
# --------------------------------------------------------------------------- #


def _result(step: str, status: str, measured: str, diff: str, evidence: str) -> dict:
    return {"step": step, "status": status, "measured": measured,
            "diff": diff, "evidence": evidence}


def step1_loopback(link: Link) -> dict:
    sent = GOLDEN_PING
    frames, _all = link.transact(sent, (), timeout=1.0)
    echo = [f for _, f in _all if encode(f.type, f.payload) == sent]
    if echo:
        return _result("1 UART 回环", "PASS", f"原样收回 {sent.hex()}",
                       "无", "回环帧与发送帧逐字节相同")
    return _result("1 UART 回环", "FAIL", "未收到原样字节",
                   "TX-RX 未短接 / 未共地 / 端口或波特率不对",
                   f"1s 内收到 {len(_all)} 帧，无一与发送帧相同")


def step2_ping(link: Link) -> dict:
    pong_frames, _all = link.transact(GOLDEN_PING, (PacketType.PONG,), timeout=1.5)
    if not pong_frames:
        return _result("2 心跳 PONG", "FAIL", "1.5s 内未收到 PONG",
                       "固件未运行 / 波特率或接线错 / 中断没使能", "无 PONG 帧")
    payload = pong_frames[0].payload
    fw_ver, status = unpack_pong(payload)
    major, minor = parse_fw_version(fw_ver)
    expected_raw = encode(PacketType.PONG, bytes([0x02, 0x01]))
    measured = f"fw_ver=0x{fw_ver:02X}（v{major}.{minor}）status={status}"
    if fw_ver == 0x02:
        return _result("2 心跳 PONG", "PASS", measured,
                       "无" if status == 1 else "status 非 1（非 RUNNING），需核对固件状态机",
                       f"PONG 原始 {encode(PacketType.PONG, payload).hex()}")
    return _result("2 心跳 PONG", "WARN", measured,
                   f"固件版本与当前基线 PID_FW_VERSION=2（0x02）不一致，"
                   f"参考帧 {expected_raw.hex()}",
                   f"PONG 原始 {encode(PacketType.PONG, payload).hex()}")


def step3_pid_set(link: Link) -> dict:
    acks, _all = link.transact(GOLDEN_PID_SET, (PacketType.PID_ACK,), timeout=1.5)
    if not acks:
        return _result("3 参数下发 ACK", "FAIL", "未收到 PID_ACK",
                       "固件未跑到 pid_app_poll / send_frame 失败", "无 ACK")
    kp, ki, kd, sp, result = unpack_pid_ack(acks[0].payload)
    expect = (2.0, 0.5, 0.1, 30.0)
    diffs = [abs(a - b) for a, b in zip((kp, ki, kd, sp), expect)]
    echo_ok = max(diffs) <= 1e-6 and result == AckResult.ACCEPTED

    bad = encode(PacketType.PID_SET, pack_pid_set(1e9, 0.5, 0.1, 30.0))
    link.write(bad)
    bad_frames = link.collect(1.5, until=lambda got: any(
        f.type in (PacketType.PID_ACK, PacketType.ERROR) for _, f in got))
    rejected = any(f.type == PacketType.ERROR for _, f in bad_frames) or any(
        f.type == PacketType.PID_ACK and unpack_pid_ack(f.payload)[4] != 0
        for _, f in bad_frames)
    evidence = (f"ACK 回显 kp={kp:g} ki={ki:g} kd={kd:g} sp={sp:g} result={result}；"
                f"越界值 {'被拒' if rejected else '未被拒'}")
    if echo_ok and rejected:
        return _result("3 参数下发 ACK", "PASS", evidence, "无", evidence)
    if not rejected:
        return _result("3 参数下发 ACK", "FAIL", evidence,
                       "越界 Kp=1e9 未被拒绝（固件量程判据缺失或 ACK 未回）", evidence)
    return _result("3 参数下发 ACK", "FAIL", evidence,
                   f"回显与下发不符，最大差 {max(diffs):.3g}", evidence)


def _telemetry_stats(frames: List[Tuple[float, Frame]]) -> dict:
    samples = []
    for recv_t, frame in frames:
        if frame.type != PacketType.TELEMETRY:
            continue
        samples.append((recv_t, *unpack_telemetry(frame.payload)))
    if not samples:
        return {"count": 0}
    t_ms = [row[1] for row in samples]
    dt_fw = []
    for a, b in zip(t_ms, t_ms[1:]):
        delta = (b - a) & 0xFFFFFFFF
        if 0 < delta < 1000:
            dt_fw.append(delta)
    recv = [row[0] for row in samples]
    dt_pc = [(b - a) * 1000.0 for a, b in zip(recv, recv[1:])]
    pv = [row[3] for row in samples]
    out = [row[4] for row in samples]
    return {
        "count": len(samples),
        "fw_mean": statistics.fmean(dt_fw) if dt_fw else 0.0,
        "fw_max_jitter": (max(dt_fw) - min(dt_fw)) if dt_fw else 0.0,
        "pc_mean": statistics.fmean(dt_pc) if dt_pc else 0.0,
        "pc_max_jitter": (max(dt_pc) - min(dt_pc)) if dt_pc else 0.0,
        "pv_min": min(pv), "pv_max": max(pv),
        "pv_std": statistics.pstdev(pv) if len(pv) > 1 else 0.0,
        "out_min": min(out), "out_max": max(out),
        "t_mono": all(((b - a) & 0xFFFFFFFF) < 60000 for a, b in zip(t_ms, t_ms[1:])),
    }


def step4_telemetry(link: Link, seconds: float) -> dict:
    frames = link.collect(seconds)
    stats = _telemetry_stats(frames)
    if not stats["count"]:
        return _result("4 遥测节拍", "FAIL", "未收到遥测帧", "波特率/接线/固件未上报", "0 帧")
    measured = (f"{stats['count']} 帧 / {seconds:g}s；固件 t_ms 间隔均值 {stats['fw_mean']:.2f}ms "
                f"抖动 {stats['fw_max_jitter']:.1f}ms；PC 侧收帧间隔 {stats['pc_mean']:.1f}ms "
                f"抖动 {stats['pc_max_jitter']:.1f}ms")
    ok = abs(stats["fw_mean"] - TELEMETRY_PERIOD_MS) <= 2.5 and stats["fw_max_jitter"] <= 5.0
    diff = "无" if ok else "节拍偏离 50ms±2.5 或抖动 >5ms（查主循环阻塞 / 时基 / 波特率）"
    return _result("4 遥测节拍", "PASS" if ok else "FAIL", measured, diff,
                   f"t_ms 单调（含回绕）={stats['t_mono']}")


def step5_sensor(link: Link, seconds: float = 5.0) -> dict:
    frames = link.collect(seconds)
    stats = _telemetry_stats(frames)
    if not stats["count"]:
        return _result("5 反馈 PV", "FAIL", "无遥测", "先做第 4 步", "0 帧")
    measured = (f"PV {stats['pv_min']:.3f}..{stats['pv_max']:.3f}，"
                f"σ={stats['pv_std']:.3f}；OUT {stats['out_min']:.2f}..{stats['out_max']:.2f}")
    return _result("5 反馈 PV", "OBSERVED", measured,
                   "需人工改变被测量（捏热敏 / 挡光 / 移动），看 PV 方向与量程；静止噪声应 <1% FS",
                   measured)


def step6_actuator(link: Link, seconds: float = 3.0) -> dict:
    before = link.collect(1.0)
    stats_before = _telemetry_stats(before)
    pv = (stats_before.get("pv_max") or 20.0) if stats_before["count"] else 20.0
    link.write(encode(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, pv + 30.0)))
    link.collect(0.5)
    after = link.collect(seconds)
    stats_after = _telemetry_stats(after)
    if not stats_after["count"]:
        return _result("6 输出 OUT", "FAIL", "调整 SP 后无遥测", "先做第 4 步", "0 帧")
    delta = max(abs(stats_after["out_max"] - stats_after["out_min"]), 0.0)
    measured = (f"OUT {stats_after['out_min']:.2f}..{stats_after['out_max']:.2f}"
                f"（变化 {delta:.2f}）；PV {stats_after['pv_min']:.2f}..{stats_after['pv_max']:.2f}")
    ok = delta >= 5.0
    return _result("6 输出 OUT", "OBSERVED" if ok else "FAIL", measured,
                   "需示波器/万用表确认 PWM 引脚 1kHz 与占空比；OUT 无变化先查 HAL_TIM_PWM_Start",
                   measured)


def step7_step(link: Link, hold_ms: int = 5000) -> dict:
    before = link.collect(0.6)
    stats_before = _telemetry_stats(before)
    sp_before = 0.0
    for _, frame in before:
        if frame.type == PacketType.TELEMETRY:
            sp_before = unpack_telemetry(frame.payload)[1]
            break
    raw = encode(PacketType.STEP_START, pack_step_start(20.0, hold_ms))
    link.write(raw)
    frames = link.collect(hold_ms / 1000.0 + 4.0, until=lambda got: any(
        f.type == PacketType.STEP_EVENT and unpack_step_event(f.payload)[0] == StepPhase.DONE
        for _, f in got))
    events = [(t, *unpack_step_event(f.payload)) for t, f in frames
              if f.type == PacketType.STEP_EVENT]
    running = any(phase == StepPhase.RUNNING for _, phase, _ in events)
    done = any(phase == StepPhase.DONE for _, phase, _ in events)
    duration = (events[-1][0] - events[0][0]) * 1000.0 if len(events) >= 2 else 0.0
    stats = _telemetry_stats(frames)
    sp_values = [unpack_telemetry(f.payload)[1] for _, f in frames
                 if f.type == PacketType.TELEMETRY]
    sp_after = max(sp_values) if sp_values else 0.0
    measured = (f"事件 {[e[1] for e in events]}；SP {sp_before:.2f} → 峰值 {sp_after:.2f}；"
                f"RUNNING→DONE {duration:.0f}ms（设定保持 {hold_ms}ms）；"
                f"PV 峰值 {stats.get('pv_max', 0):.2f}")
    ok = running and done and abs(duration - hold_ms) <= 700
    return _result("7 闭环阶跃", "PASS" if ok else "FAIL", measured,
                   "无" if ok else "缺 RUNNING/DONE 或保持时长偏差 >700ms",
                   f"STEP_START 下发 {raw.hex()}")


def step8_nvm_write(link: Link) -> dict:
    link.write(encode(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, 50.0)))
    link.collect(0.6)
    frames = link.collect(5.5)
    errors = [unpack_error(f.payload) for _, f in frames if f.type == PacketType.ERROR]
    sp = 0.0
    for _, frame in frames:
        if frame.type == PacketType.TELEMETRY:
            sp = unpack_telemetry(frame.payload)[1]
    measured = f"SP 下发 50，等 5s 防抖后遥测 SP={sp:.2f}；ERROR 帧 {len(errors)} 条"
    ok = not errors and abs(sp - 50.0) <= 1e-3
    return _result("8a NVM 写入", "PASS" if ok else "FAIL", measured,
                   "无" if ok else "ERROR 0x04 擦写失败 / SP 未生效",
                   f"ERROR={errors}")


def step8_nvm_verify(link: Link) -> dict:
    frames = link.collect(2.0)
    sp = None
    for _, frame in frames:
        if frame.type == PacketType.TELEMETRY:
            sp = unpack_telemetry(frame.payload)[1]
            break
    if sp is None:
        return _result("8b NVM 读回", "FAIL", "上电后无遥测", "固件没启动 / 端口错", "0 帧")
    ok = abs(sp - 50.0) <= 1e-3
    return _result("8b NVM 读回", "PASS" if ok else "FAIL",
                   f"未下发任何参数，上电遥测 SP={sp:.2f}",
                   "无" if ok else "SP≠50：nvm_load 未挂 / 地址不对 / CRC 校验失败回默认",
                   "只读遥测，未发送任何帧")


# --------------------------------------------------------------------------- #
# 产物
# --------------------------------------------------------------------------- #


def write_frames(link: Link, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for record in link.records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_telemetry(link: Link, path: str) -> int:
    """把本次收到的遥测写成 CSV（列定义固定，便于后续 diff 基线）。"""
    rows = []
    for record in link.records:
        if record["dir"] != "down" or record["type"] != PacketType.TELEMETRY:
            continue
        raw = bytes.fromhex(record["raw"])
        frame = Frame(type=raw[3], payload=raw[4:-6])
        t_ms, sp, pv, out, err = unpack_telemetry(frame.payload)
        rows.append({"pc_time_s": f"{record['t']:.3f}", "t_ms": t_ms,
                     "sp": f"{sp:.4f}", "pv": f"{pv:.4f}", "out": f"{out:.4f}",
                     "err": f"{err:.4f}"})
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["pc_time_s", "t_ms", "sp", "pv",
                                                    "out", "err"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def write_report(results: List[dict], path: str, link_name: str, mode: str) -> None:
    lines = [
        "# 真机首联报告",
        "",
        f"- 链路：{link_name}",
        f"- 模式：{mode}",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 步骤 | 结论 | 实测值 | 与预期差异 / 下一步 | 证据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in results:
        lines.append(f"| {row['step']} | {row['status']} | {row['measured']} | "
                     f"{row['diff']} | {row['evidence']} |")
    lines += ["", "> OBSERVED = 数值已记录，判定需人工/仪器确认；FAIL 详见差异列。"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SerialPID-Tuner 真机首联核对")
    parser.add_argument("--port", help="串口，如 COM3；省略时自动找第一个")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--tcp", help="改用 TCP（host:port），仅用于无板子时自测脚本")
    parser.add_argument("--seconds", type=float, default=60.0, help="第 4 步统计时长")
    parser.add_argument("--loopback", action="store_true", help="执行第 1 步（TX-RX 已短接）")
    parser.add_argument("--nvm-write", action="store_true", help="只做第 8a 步（写 SP=50）")
    parser.add_argument("--nvm-verify", action="store_true", help="只做第 8b 步（读回 SP）")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "docs"))
    args = parser.parse_args(argv)

    port = args.port
    if not args.tcp and not port:
        ports = _find_ports()
        if not ports:
            print("未发现任何串口设备：板子/CH340 未枚举，或驱动未安装。")
            print("插入后先确认：设备管理器 > 端口(COM/LPT) 出现 USB-SERIAL CH340，")
            print("再运行：python tools/bringup_check.py --port COM3")
            return 2
        port = ports[0]
        print(f"未指定 --port，自动选择 {port}（可用：{ports}）")

    link = Link(port=port, baud=args.baud, tcp=args.tcp)
    try:
        name = link.open()
    except Exception as exc:  # noqa: BLE001 - 端口占用/不存在都属环境问题
        print(f"打开链路失败：{exc}")
        if not args.tcp:
            print("先在设备管理器确认「端口 (COM/LPT)」出现 USB-SERIAL CH340 (COMx)，"
                  "再 --port 对应端口；板子未枚举时不要据此判固件失败。")
        return 2

    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(args.out, exist_ok=True)
    frames_path = os.path.join(args.out, f"bringup_frames_{stamp}.jsonl")
    tele_path = os.path.join(args.out, f"bringup_telemetry_{stamp}.csv")
    report_path = os.path.join(args.out, f"bringup_report_{stamp}.md")

    results: List[dict] = []
    mode = "非真机（TCP 离线模拟器，仅验证脚本）" if args.tcp else "真机"
    try:
        if args.nvm_verify:
            results.append(step8_nvm_verify(link))
        elif args.nvm_write:
            results.append(step8_nvm_write(link))
        else:
            if args.loopback:
                results.append(step1_loopback(link))
            results.append(step2_ping(link))
            results.append(step3_pid_set(link))
            results.append(step4_telemetry(link, args.seconds))
            results.append(step5_sensor(link))
            results.append(step6_actuator(link))
            results.append(step7_step(link))
    finally:
        link.close()

    write_frames(link, frames_path)
    telemetry_rows = write_telemetry(link, tele_path)
    write_report(results, report_path, name, mode)

    print(f"\n链路：{name}（{mode}）")
    for row in results:
        print(f"[{row['status']:>8}] {row['step']}：{row['measured']}")
        if row["status"] == "FAIL":
            print(f"           下一步：{row['diff']}")
    print(f"\n帧留痕：{os.path.abspath(frames_path)}")
    print(f"遥测 CSV：{os.path.abspath(tele_path)}（{telemetry_rows} 行）")
    print(f"报告：{os.path.abspath(report_path)}")

    if any(row["status"] == "FAIL" for row in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
