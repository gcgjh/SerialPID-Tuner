"""SerialPID-Tuner 冻结串口协议编解码层（PC 侧）。

协议基准：``docs/PROTOCOL.md``（冻结 v1.0，任何人不得私改字节布局）。

帧布局（N = LEN = PAYLOAD 字节数，0 <= N <= 250）::

    0     1     2    3     4 ... 3+N     4+N     5+N    6+N ... 9+N
    +-----+-----+----+----+-----------+-------+-------+------------+
    |0xAA |0x55 |LEN |TYPE|  PAYLOAD  | CRC_LO| CRC_HI| 0x77 x 4   |
    +-----+-----+----+----+-----------+-------+-------+------------+

* 多字节整数小端序；浮点 IEEE-754 单精度小端。
* CRC16-Modbus：poly 0xA001，init 0xFFFF，输入输出不额外异或；
  校验范围 = ``LEN + TYPE + PAYLOAD``（不含帧头、不含帧尾），以小端附加。

本模块是纯协议实现：不依赖 Qt、pyserial 或任何第三方库，
所有对外输入输出都是 ``bytes``，供通信层、GUI、模拟器与 MCU 对齐测试共用。

约定（PC 下发方向才校验，接收方向一律宽容）：
    ``pack_pid_set`` / ``pack_sp_set`` / ``pack_step_start`` 拒绝 NaN、inf
    以及超出 float32 表示范围的数值，避免把不可控参数下发到固件；
    ``unpack_*`` 只校验载荷长度，不对数值做有限性检查（固件异常值不应
    导致上位机解析线程崩溃）。
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# 帧常量（与 PROTOCOL.md 第 2 节一致）
# ---------------------------------------------------------------------------

SOF0: int = 0xAA
SOF1: int = 0x55
EOF: bytes = b"\x77\x77\x77\x77"

MAX_PAYLOAD: int = 250          # LEN 允许的最大值（协议硬上限）
FRAME_OVERHEAD: int = 10        # SOF2 + LEN1 + TYPE1 + CRC2 + EOF4
MAX_FRAME_SIZE: int = FRAME_OVERHEAD + MAX_PAYLOAD  # 260

_TAIL: int = 4                  # EOF 长度


class PacketType:
    """包类型定义（冻结，禁止重编号）。"""

    PING = 0x01        # PC->MCU 空载荷，链路心跳
    PONG = 0x02        # MCU->PC fw_ver u8, status u8
    PID_SET = 0x10     # PC->MCU Kp,Ki,Kd,SP (4 x f32)
    PID_ACK = 0x11     # MCU->PC 回显 Kp,Ki,Kd,SP,result u8
    SP_SET = 0x12      # PC->MCU SP f32
    TELEMETRY = 0x20   # MCU->PC t_ms u32, SP,PV,OUT,err f32
    STEP_START = 0x30  # PC->MCU amplitude f32, hold_ms u32
    STEP_STOP = 0x31   # PC->MCU 空载荷
    STEP_EVENT = 0x32  # MCU->PC phase u8, t_ms u32
    ERROR = 0x70       # MCU->PC code u8, msg_len u8, ASCII/UTF-8
    RESET = 0x71       # PC->MCU 空载荷，软复位 MCU


class PongStatus:
    """PONG.status 取值（冻结）。"""

    IDLE = 0
    RUNNING = 1
    STEPPING = 2


class AckResult:
    """PID_ACK.result 取值（冻结）。"""

    ACCEPTED = 0
    REJECTED = 1


class StepPhase:
    """STEP_EVENT.phase 取值（冻结）。"""

    IDLE = 0
    RUNNING = 1
    DONE = 2


PACKET_NAMES: Dict[int, str] = {
    PacketType.PING: "PING",
    PacketType.PONG: "PONG",
    PacketType.PID_SET: "PID_SET",
    PacketType.PID_ACK: "PID_ACK",
    PacketType.SP_SET: "SP_SET",
    PacketType.TELEMETRY: "TELEMETRY",
    PacketType.STEP_START: "STEP_START",
    PacketType.STEP_STOP: "STEP_STOP",
    PacketType.STEP_EVENT: "STEP_EVENT",
    PacketType.ERROR: "ERROR",
    PacketType.RESET: "RESET",
}

STATUS_NAMES: Dict[int, str] = {
    PongStatus.IDLE: "空闲",
    PongStatus.RUNNING: "运行",
    PongStatus.STEPPING: "阶跃中",
}

PHASE_NAMES: Dict[int, str] = {
    StepPhase.IDLE: "空闲",
    StepPhase.RUNNING: "运行中",
    StepPhase.DONE: "完成",
}

RESULT_NAMES: Dict[int, str] = {
    AckResult.ACCEPTED: "已接受",
    AckResult.REJECTED: "越界拒绝",
}

# 各类型固定载荷的 struct 格式（小端）；ERROR 为可变长，单独处理。
PAYLOAD_FMT: Dict[int, str] = {
    PacketType.PONG: "<BB",
    PacketType.PID_SET: "<4f",
    PacketType.PID_ACK: "<4fB",
    PacketType.SP_SET: "<f",
    PacketType.TELEMETRY: "<I4f",
    PacketType.STEP_START: "<fI",
    PacketType.STEP_EVENT: "<BI",
}

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ProtocolError(ValueError):
    """协议层异常基类（继承 ValueError，兼容旧代码的 except ValueError）。"""


class FrameFormatError(ProtocolError):
    """帧结构非法：帧头、长度、帧尾不匹配。"""


class CrcError(ProtocolError):
    """CRC 校验失败。"""


class PayloadError(ProtocolError):
    """载荷长度或内容与包类型不匹配。"""


# ---------------------------------------------------------------------------
# CRC16-Modbus
# ---------------------------------------------------------------------------


def _build_crc_table() -> Tuple[int, ...]:
    table: List[int] = []
    for value in range(256):
        crc = value
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
        table.append(crc & 0xFFFF)
    return tuple(table)


_CRC_TABLE: Tuple[int, ...] = _build_crc_table()


def crc16_modbus(data: Union[bytes, bytearray, memoryview]) -> int:
    """CRC16-Modbus：poly 0xA001，init 0xFFFF，返回 u16。

    标准校验向量：``crc16_modbus(b"123456789") == 0x4B37``。
    """
    crc = 0xFFFF
    table = _CRC_TABLE
    for byte in bytes(data):
        crc = (crc >> 8) ^ table[(crc ^ byte) & 0xFF]
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# 数值校验辅助
# ---------------------------------------------------------------------------

_F32_MAX = 3.4028234663852886e38


def _finite_f32(value: float, field_name: str) -> float:
    """校验数值可安全表示为 float32；不可表示则抛出 ValueError。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是数值，收到 {value!r}") from None
    if not math.isfinite(number):
        raise ValueError(f"{field_name} 必须是有限数值，收到 {value!r}")
    if abs(number) > _F32_MAX:
        raise ValueError(f"{field_name} 超出 float32 表示范围：{number!r}")
    return number


def _uint(value: int, field_name: str, bits: int = 8) -> int:
    """校验无符号整数范围。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是整数，收到 {value!r}") from None
    if number < 0 or number > (1 << bits) - 1:
        raise ValueError(f"{field_name} 超出 u{bits} 范围：{number}")
    return number


# ---------------------------------------------------------------------------
# 封包
# ---------------------------------------------------------------------------


def encode(pkt_type: int, payload: bytes = b"") -> bytes:
    """按冻结布局封包，返回完整帧字节。

    :raises ValueError: TYPE 越界或载荷超过 250 字节。
    """
    pkt_type = _uint(pkt_type, "包类型")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload 必须是 bytes")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload 超过 {MAX_PAYLOAD} 字节：{len(payload)}")
    head = bytes((SOF0, SOF1, len(payload), pkt_type))
    crc = crc16_modbus(head[2:] + payload)  # 校验 LEN + TYPE + PAYLOAD
    return head + payload + struct.pack("<H", crc) + EOF


# ---- 载荷打包（PC -> MCU 方向）--------------------------------------------


def pack_pid_set(kp: float, ki: float, kd: float, sp: float) -> bytes:
    """PID_SET 载荷：Kp, Ki, Kd, SP 四个 f32。"""
    return struct.pack(
        "<4f",
        _finite_f32(kp, "Kp"),
        _finite_f32(ki, "Ki"),
        _finite_f32(kd, "Kd"),
        _finite_f32(sp, "SP"),
    )


def pack_sp_set(sp: float) -> bytes:
    """SP_SET 载荷：单个 f32 设定值。"""
    return struct.pack("<f", _finite_f32(sp, "SP"))


def pack_step_start(amplitude: float, hold_ms: int) -> bytes:
    """STEP_START 载荷：幅值 f32 + 保持时长 u32（毫秒）。"""
    return struct.pack("<fI", _finite_f32(amplitude, "amplitude"), _uint(hold_ms, "hold_ms", 32))


def pack_step_stop() -> bytes:
    """STEP_STOP 载荷：空。"""
    return b""


def pack_reset() -> bytes:
    """RESET 载荷：空。"""
    return b""


def pack_ping() -> bytes:
    """PING 载荷：空。"""
    return b""


def pack_pong(fw_ver: int, status: int) -> bytes:
    """PONG 载荷：fw_ver u8 + status u8（MCU 侧实现与模拟器共用）。"""
    if status not in (PongStatus.IDLE, PongStatus.RUNNING, PongStatus.STEPPING):
        raise ValueError(f"status 非法：{status}")
    return struct.pack("<BB", _uint(fw_ver, "fw_ver"), status)


def pack_telemetry(t_ms: int, sp: float, pv: float, out: float, err: float) -> bytes:
    """TELEMETRY 载荷：t_ms u32 + SP/PV/OUT/err 四个 f32。

    仅用于模拟器与离线测试（MCU 固件按同名打包）。这里不校验数值有限性，
    以便模拟器复现固件除零等异常工况时仍能照原样上报。
    """
    return struct.pack(
        "<I4f",
        _uint(t_ms, "t_ms", 32),
        float(sp),
        float(pv),
        float(out),
        float(err),
    )


def pack_pid_ack(kp: float, ki: float, kd: float, sp: float, result: int) -> bytes:
    """PID_ACK 载荷：回显四个 f32 + result u8（0=接受，1=越界拒绝）。"""
    if result not in (AckResult.ACCEPTED, AckResult.REJECTED):
        raise ValueError(f"result 非法：{result}")
    return struct.pack(
        "<4fB",
        _finite_f32(kp, "Kp"),
        _finite_f32(ki, "Ki"),
        _finite_f32(kd, "Kd"),
        _finite_f32(sp, "SP"),
        result,
    )


def pack_step_event(phase: int, t_ms: int) -> bytes:
    """STEP_EVENT 载荷：phase u8 + t_ms u32。"""
    if phase not in (StepPhase.IDLE, StepPhase.RUNNING, StepPhase.DONE):
        raise ValueError(f"phase 非法：{phase}")
    return struct.pack("<BI", phase, _uint(t_ms, "t_ms", 32))


def pack_error(code: int, message: str) -> bytes:
    """ERROR 载荷：code u8 + msg_len u8 + msg（UTF-8，无结束符）。

    ``msg`` 编码后最多 248 字节（保证总载荷 <= 250）。
    """
    if not isinstance(message, str):
        raise TypeError("message 必须是 str")
    raw = message.encode("utf-8")
    if len(raw) > MAX_PAYLOAD - 2:
        raise ValueError(f"错误消息过长：{len(raw)} 字节（上限 {MAX_PAYLOAD - 2}）")
    return bytes((_uint(code, "code"), len(raw))) + raw


def pack_fw_version(major: int, minor: int) -> int:
    """把主/次版本号压成 PONG.fw_ver：高 4 位主版本，低 4 位次版本。"""
    if not 0 <= int(major) <= 15 or not 0 <= int(minor) <= 15:
        raise ValueError("主/次版本号必须在 0..15 之间")
    return (int(major) << 4) | int(minor)


def parse_fw_version(fw_ver: int) -> Tuple[int, int]:
    """从 PONG.fw_ver 解出 (主版本, 次版本)。"""
    value = _uint(fw_ver, "fw_ver")
    return (value >> 4) & 0x0F, value & 0x0F


# ---------------------------------------------------------------------------
# 解包（长度严格校验，数值不做有限性校验）
# ---------------------------------------------------------------------------


def _expect_len(payload: bytes, fmt: str, name: str) -> Tuple:
    expected = struct.calcsize(fmt)
    if len(payload) != expected:
        raise PayloadError(
            f"{name} 载荷长度应为 {expected} 字节，实际 {len(payload)} 字节"
        )
    return struct.unpack(fmt, bytes(payload))


def unpack_pid_set(payload: bytes) -> Tuple[float, float, float, float]:
    return _expect_len(payload, "<4f", "PID_SET")  # type: ignore[return-value]


def unpack_pid_ack(payload: bytes) -> Tuple[float, float, float, float, int]:
    return _expect_len(payload, "<4fB", "PID_ACK")  # type: ignore[return-value]


def unpack_sp_set(payload: bytes) -> float:
    return _expect_len(payload, "<f", "SP_SET")[0]


def unpack_telemetry(payload: bytes) -> Tuple[int, float, float, float, float]:
    return _expect_len(payload, "<I4f", "TELEMETRY")  # type: ignore[return-value]


def unpack_pong(payload: bytes) -> Tuple[int, int]:
    return _expect_len(payload, "<BB", "PONG")  # type: ignore[return-value]


def unpack_step_start(payload: bytes) -> Tuple[float, int]:
    return _expect_len(payload, "<fI", "STEP_START")  # type: ignore[return-value]


def unpack_step_event(payload: bytes) -> Tuple[int, int]:
    return _expect_len(payload, "<BI", "STEP_EVENT")  # type: ignore[return-value]


def unpack_error(payload: bytes) -> Tuple[int, str]:
    """ERROR 载荷 -> (code, message)。消息按 UTF-8 解码，非法字节替换为 U+FFFD。"""
    raw = bytes(payload)
    if len(raw) < 2:
        raise PayloadError(f"ERROR 载荷至少 2 字节，实际 {len(raw)} 字节")
    code = raw[0]
    msg_len = raw[1]
    if 2 + msg_len != len(raw):
        raise PayloadError(
            f"ERROR 声明消息长度 {msg_len}，实际 {len(raw) - 2} 字节"
        )
    return code, raw[2:].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 载荷长度预检（界面层分发前调用，畸形载荷不许进入解析流程）
# ---------------------------------------------------------------------------


# 无载荷的帧类型（LEN 段为 0）
EMPTY_PAYLOAD_TYPES: FrozenSet[int] = frozenset(
    {PacketType.PING, PacketType.STEP_STOP, PacketType.RESET}
)


def expected_payload_len(pkt_type: int) -> Optional[int]:
    """按帧类型返回协议规定的载荷长度（PROTOCOL.md §4 逐字节对应）。

    可变长（ERROR）与未知类型返回 ``None``（未知帧保留、不预检，见 §3.5）。
    定长类型的数值全部来自 :data:`PAYLOAD_FMT`，与 §4 载荷定义同源，
    避免界面层再抄一份表导致静默丢正常帧。
    """
    fmt = PAYLOAD_FMT.get(pkt_type)
    if fmt is not None:
        return struct.calcsize(fmt)
    if pkt_type in EMPTY_PAYLOAD_TYPES:
        return 0
    return None


def payload_error_reason(
    pkt_type: int, payload: Union[bytes, bytearray]
) -> Optional[str]:
    """解析前的长度预检：不通过时返回原因字符串，通过 / 不适用时返回 ``None``。

    * 定长类型：长度必须与载荷定义一致，否则丢弃、不进入解析流程；
    * ERROR（可变长）：额外校验“声明的消息长度 msg_len”与“载荷实际长度”
      是否匹配，不匹配按截断处理（``unpack_error`` 的越界读在此被挡住）；
    * 未知类型：原样保留，不预检（协议 §3.5）。
    """
    expected = expected_payload_len(pkt_type)
    if expected is not None:
        if len(payload) != expected:
            name = PACKET_NAMES.get(pkt_type, f"0x{pkt_type:02X}")
            return f"{name} 载荷应为 {expected} 字节，实际 {len(payload)} 字节"
        return None
    if pkt_type == PacketType.ERROR:
        if len(payload) < 2:
            return f"ERROR 载荷至少 2 字节，实际 {len(payload)} 字节"
        msg_len = payload[1]
        if 2 + msg_len != len(payload):
            return (
                f"ERROR 声明消息长度 {msg_len} 字节，实际载荷 {len(payload) - 2} 字节"
                "（按截断处理）"
            )
    return None


# ---------------------------------------------------------------------------
# 帧对象
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """一个已通过 CRC 与帧尾校验的完整帧。"""

    type: int
    payload: bytes

    def to_bytes(self) -> bytes:
        """重新封包（模拟器回显 / 测试辅助，非日志回放功能）。"""
        return encode(self.type, self.payload)

    @property
    def name(self) -> str:
        return PACKET_NAMES.get(self.type, f"UNKNOWN(0x{self.type:02X})")

    def __repr__(self) -> str:  # pragma: no cover - 仅调试显示
        return f"Frame(type=0x{self.type:02X} {self.name}, len={len(self.payload)})"


@dataclass
class DecoderStats:
    """解包统计（链路质量展示 / 告警用）。"""

    bytes_in: int = 0
    frames: int = 0
    dropped_bytes: int = 0
    crc_errors: int = 0
    eof_errors: int = 0
    oversize_errors: int = 0

    def reset(self) -> None:
        self.bytes_in = 0
        self.frames = 0
        self.dropped_bytes = 0
        self.crc_errors = 0
        self.eof_errors = 0
        self.oversize_errors = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "bytes_in": self.bytes_in,
            "frames": self.frames,
            "dropped_bytes": self.dropped_bytes,
            "crc_errors": self.crc_errors,
            "eof_errors": self.eof_errors,
            "oversize_errors": self.oversize_errors,
        }


# ---------------------------------------------------------------------------
# 流式解包状态机
# ---------------------------------------------------------------------------


class FrameDecoder:
    """流式解包器：喂入串口原始字节，吐出完整帧。

    特性（与 PROTOCOL.md 第 3 节对齐）：
      * 粘包：一次 ``feed`` 可返回多帧；
      * 拆包：半帧留在缓冲，下次 ``feed`` 继续拼；
      * 帧头失配：逐字节丢弃后重新扫描 ``AA 55``；``AA`` 后跟非 ``55``
        不会卡死（会丢弃 1 字节继续扫描）；
      * 帧尾失配：丢弃当前首字节后重扫（§3.3）；
      * CRC 失配：丢弃整帧并重同步，错误载荷绝不交给上层（§3.4）；
      * LEN > 250：按超长帧丢弃首字节并计数（§3.6）；
      * 未知 TYPE：原样保留在 ``Frame`` 中，不丢弃（§3.5）；
      * 任何输入都不会抛出异常，失败只体现在统计计数上。

    线程模型：非线程安全，只允许通信线程单线程调用。
    """

    def __init__(self, max_buffer: int = MAX_FRAME_SIZE * 4) -> None:
        self._buf = bytearray()
        self._max_buffer = max(int(max_buffer), MAX_FRAME_SIZE)
        self.stats = DecoderStats()

    # -- 对外 ---------------------------------------------------------------

    def feed(self, data: Union[bytes, bytearray, memoryview]) -> List[Frame]:
        """喂入任意字节块，返回本次新解出的全部帧（可能为空）。"""
        if not data:
            return []
        chunk = bytes(data)
        self._buf.extend(chunk)
        self.stats.bytes_in += len(chunk)
        frames: List[Frame] = []
        buf = self._buf

        while True:
            start = buf.find(b"\xAA\x55")
            if start < 0:
                # 没有帧头：保留末尾可能是半个帧头的 0xAA
                keep = 1 if buf and buf[-1] == SOF0 else 0
                drop = len(buf) - keep
                if drop > 0:
                    del buf[:drop]
                    self.stats.dropped_bytes += drop
                break

            if start > 0:
                del buf[:start]
                self.stats.dropped_bytes += start

            if len(buf) < 4:  # 还差 LEN / TYPE
                break

            payload_len = buf[2]
            if payload_len > MAX_PAYLOAD:
                # LEN 上限 250；超出说明是伪帧头，丢 1 字节重扫
                del buf[0]
                self.stats.dropped_bytes += 1
                self.stats.oversize_errors += 1
                continue

            total = FRAME_OVERHEAD + payload_len
            if len(buf) < total:
                break  # 半帧，等后续字节

            # 先验帧尾（便宜且能快速否掉伪帧头），再验 CRC
            if bytes(buf[total - _TAIL: total]) != EOF:
                del buf[0]
                self.stats.dropped_bytes += 1
                self.stats.eof_errors += 1
                continue

            crc_calc = crc16_modbus(buf[2: 4 + payload_len])
            crc_rx = buf[4 + payload_len] | (buf[5 + payload_len] << 8)
            if crc_calc != crc_rx:
                del buf[:total]
                self.stats.dropped_bytes += total
                self.stats.crc_errors += 1
                continue

            frames.append(
                Frame(type=buf[3], payload=bytes(buf[4: 4 + payload_len]))
            )
            self.stats.frames += 1
            del buf[:total]

        # 兜底：理论上等待中的半帧最多 260 字节，异常情况下防止缓冲无界增长
        if len(buf) > self._max_buffer:
            excess = len(buf) - self._max_buffer
            del buf[:excess]
            self.stats.dropped_bytes += excess

        return frames

    def pending(self) -> int:
        """缓冲中尚未成帧的字节数。"""
        return len(self._buf)

    def reset(self, clear_stats: bool = False) -> None:
        """清空缓冲（重连/换口时必须调用，避免上一路残帧串数据）。"""
        self._buf.clear()
        if clear_stats:
            self.stats.reset()


# ---------------------------------------------------------------------------
# 单帧严格解析（测试向量核对 / 离线解析用）
# ---------------------------------------------------------------------------


def parse_frame(data: Union[bytes, bytearray, memoryview]) -> Frame:
    """解析**恰好一个**完整帧；结构或 CRC 不符抛出异常。"""
    raw = bytes(data)
    if len(raw) < FRAME_OVERHEAD:
        raise FrameFormatError(
            f"帧长度不足：{len(raw)} 字节，最少 {FRAME_OVERHEAD} 字节"
        )
    if raw[0] != SOF0 or raw[1] != SOF1:
        raise FrameFormatError(
            f"帧头错误：期望 AA 55，实际 {raw[0]:02X} {raw[1]:02X}"
        )
    payload_len = raw[2]
    if payload_len > MAX_PAYLOAD:
        raise FrameFormatError(f"LEN 超限：{payload_len} > {MAX_PAYLOAD}")
    total = FRAME_OVERHEAD + payload_len
    if len(raw) != total:
        raise FrameFormatError(f"帧长度不符：声明 {total} 字节，实际 {len(raw)} 字节")
    if raw[total - _TAIL: total] != EOF:
        raise FrameFormatError("帧尾错误：期望 77 77 77 77")
    crc_rx = raw[4 + payload_len] | (raw[5 + payload_len] << 8)
    crc_calc = crc16_modbus(raw[2: 4 + payload_len])
    if crc_calc != crc_rx:
        raise CrcError(f"CRC 校验失败：计算 0x{crc_calc:04X}，帧内 0x{crc_rx:04X}")
    return Frame(type=raw[3], payload=raw[4: 4 + payload_len])


# ---------------------------------------------------------------------------
# 载荷解码与日志描述（调试/模拟器/测试用；GUI 业务解析请走 model.py）
# ---------------------------------------------------------------------------


def unpack_payload(pkt_type: int, payload: bytes) -> object:
    """按 TYPE 解出载荷元组；空载荷类型返回 ``None``。

    :raises PayloadError: 载荷长度与类型不匹配或类型未知。
    """
    if pkt_type in (PacketType.PING, PacketType.STEP_STOP, PacketType.RESET):
        if payload:
            raise PayloadError(f"{PACKET_NAMES[pkt_type]} 应为空载荷，实际 {len(payload)} 字节")
        return None
    unpackers = {
        PacketType.PONG: unpack_pong,
        PacketType.PID_SET: unpack_pid_set,
        PacketType.PID_ACK: unpack_pid_ack,
        PacketType.SP_SET: unpack_sp_set,
        PacketType.TELEMETRY: unpack_telemetry,
        PacketType.STEP_START: unpack_step_start,
        PacketType.STEP_EVENT: unpack_step_event,
        PacketType.ERROR: unpack_error,
    }
    unpacker = unpackers.get(pkt_type)
    if unpacker is None:
        raise PayloadError(f"未知包类型 0x{pkt_type:02X}，无法解析载荷")
    return unpacker(payload)


def describe_frame(frame: Frame) -> str:
    """把帧渲染成单行日志文本（解析失败时退回十六进制）。"""
    name = frame.name
    try:
        if frame.type == PacketType.TELEMETRY:
            t_ms, sp, pv, out, err = unpack_telemetry(frame.payload)
            return f"{name} t={t_ms}ms SP={sp:.3f} PV={pv:.3f} OUT={out:.3f} err={err:+.3f}"
        if frame.type == PacketType.PID_ACK:
            kp, ki, kd, sp, result = unpack_pid_ack(frame.payload)
            return (
                f"{name} Kp={kp:g} Ki={ki:g} Kd={kd:g} SP={sp:g} "
                f"result={RESULT_NAMES.get(result, result)}"
            )
        if frame.type == PacketType.PONG:
            fw_ver, status = unpack_pong(frame.payload)
            major, minor = parse_fw_version(fw_ver)
            return (
                f"{name} fw=v{major}.{minor} "
                f"status={STATUS_NAMES.get(status, status)}"
            )
        if frame.type == PacketType.STEP_EVENT:
            phase, t_ms = unpack_step_event(frame.payload)
            return f"{name} phase={PHASE_NAMES.get(phase, phase)} t={t_ms}ms"
        if frame.type == PacketType.ERROR:
            code, message = unpack_error(frame.payload)
            return f"{name} code={code} msg={message}"
        if frame.type == PacketType.STEP_START:
            amplitude, hold_ms = unpack_step_start(frame.payload)
            return f"{name} amplitude={amplitude:g} hold={hold_ms}ms"
        if frame.type == PacketType.SP_SET:
            return f"{name} SP={unpack_sp_set(frame.payload):g}"
        if frame.type == PacketType.PID_SET:
            kp, ki, kd, sp = unpack_pid_set(frame.payload)
            return f"{name} Kp={kp:g} Ki={ki:g} Kd={kd:g} SP={sp:g}"
    except PayloadError as exc:
        return f"{name} 载荷异常：{exc} | {frame.payload.hex(' ')}"
    return f"{name}" if not frame.payload else f"{name} {frame.payload.hex(' ')}"
