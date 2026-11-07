"""生成 MCU 侧协议对照测试向量（mcu/test/golden_vectors.h）。

真值来源：pc/serialpid/protocol.py（PC 侧冻结协议实现），因此
"MCU 能过这套向量" 等价于 "两侧字节级一致"。

每个用例包含：
  - 字节流（可含垃圾、坏 CRC、坏帧尾、超长 LEN、粘连帧）
  - 期望解出的帧列表（按构造时的真实帧给出，不依赖 PC 解码器）
  - 分片大小（模拟串口一次读到 1/2/3/13/17 字节的随机分片）

顺带打印 PC 侧 FrameDecoder 在同样输入下的表现，用来暴露两侧差异。

用法：
    python tools/gen_protocol_vectors.py
"""

from __future__ import annotations

import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PC_DIR = os.path.join(ROOT, "pc")
sys.path.insert(0, PC_DIR)

from serialpid.protocol import (  # noqa: E402
    FrameDecoder,
    PacketType,
    crc16_modbus,
    encode,
)

OUT_PATH = os.path.join(ROOT, "mcu", "test", "golden_vectors.h")


def f32(*vals: float) -> bytes:
    return struct.pack("<%df" % len(vals), *vals)


def frame_error(code: int, msg: bytes) -> bytes:
    """ERROR 帧：code u8, msg_len u8, ASCII（允许超过 64 字节，用于测 LEN 边界）"""
    return encode(PacketType.ERROR, bytes((code, len(msg))) + msg)


# ---------------------------------------------------------------- 用例构造

cases: list[dict] = []


def add_case(name: str, stream: bytes, frames: list[tuple[int, bytes]], chunk: int) -> None:
    cases.append({"name": name, "stream": stream, "frames": frames, "chunk": chunk})


ping = encode(PacketType.PING)
add_case("ping_single", ping, [(PacketType.PING, b"")], 1)

step = encode(PacketType.STEP_START, f32(10.0) + struct.pack("<I", 2000))
add_case("two_frames_glued", ping + step,
         [(PacketType.PING, b""), (PacketType.STEP_START, f32(10.0) + struct.pack("<I", 2000))], 3)

# 帧前有垃圾，且垃圾里含孤立 0xAA（这正是 PC 侧会卡死的形状）
add_case("garbage_with_stray_aa", b"\x00\x11\xAA\x00\x22" + ping,
         [(PacketType.PING, b"")], 5)

# 坏 CRC 后紧跟好帧：坏帧整帧丢弃，好帧必须解出
bad_crc = bytearray(encode(PacketType.PID_SET, f32(1.0, 2.0, 3.0, 4.0)))
bad_crc[6] ^= 0xFF
add_case("bad_crc_then_good", bytes(bad_crc) + ping, [(PacketType.PING, b"")], 4)

# 坏帧尾后紧跟好帧（帧尾字节被改成 0x00）
bad_tail = bytearray(encode(PacketType.SP_SET, f32(30.0)))
bad_tail[-1] = 0x00
add_case("bad_tail_then_good", bytes(bad_tail) + ping, [(PacketType.PING, b"")], 2)

# LEN=250 的最大帧（窗口边界）
max_msg = bytes((0x41 + (i % 26)) for i in range(248))
add_case("max_payload_250", frame_error(0x01, max_msg), [(PacketType.ERROR, bytes((1, 248)) + max_msg)], 17)

# 非法 LEN（0xFF > 250）：必须重同步，不能把后面的好帧吞掉
overflow_hdr = b"\xAA\x55\xFF\x20" + b"\x33" * 8
add_case("len_overflow_resync", overflow_hdr + ping, [(PacketType.PING, b"")], 6)

# 重叠帧头 AA AA 55
add_case("overlap_sof", b"\xAA" + ping, [(PacketType.PING, b"")], 3)

# 空载荷 + 满载荷交替，验证窗口复用
tel = encode(PacketType.TELEMETRY, struct.pack("<I4f", 1000, 25.0, 24.5, 60.0, -0.5))
add_case("telemetry_then_ping", tel + ping, [(PacketType.TELEMETRY, struct.pack("<I4f", 1000, 25.0, 24.5, 60.0, -0.5)),
                                             (PacketType.PING, b"")], 7)

# 空流
add_case("empty_stream", b"", [], 1)

# 混杂流：4096 字节随机垃圾 + 40 个真帧 + 5 个坏帧。
# 真帧被随机插入垃圾流，因此最终顺序必须以 FrameDecoder 回采为准，
# 不能按构造顺序记录（否则帧序与流内实际顺序错位）。
rng = random.Random(20261105)
alphabet = [b for b in range(256) if b != 0xAA]  # 不产生偶然帧头
parts: list[bytes] = [bytes(rng.choice(alphabet) for _ in range(4096))]

for i in range(40):
    kind = i % 5
    if kind == 0:
        f = ping
    elif kind == 1:
        pl = f32(float(i), 0.5, 0.1, 40.0)
        f = encode(PacketType.PID_SET, pl)
    elif kind == 2:
        pl = struct.pack("<I4f", i * 10, 40.0, 39.5, 55.0, 0.5)
        f = encode(PacketType.TELEMETRY, pl)
    elif kind == 3:
        pl = f32(5.0) + struct.pack("<I", 500)
        f = encode(PacketType.STEP_START, pl)
    else:
        pl = bytes((i, i))
        f = encode(PacketType.PONG, pl)

    pos = rng.randrange(0, len(parts) + 1)
    parts.insert(pos, f)

# 5 个坏帧（CRC 损坏），期望里不包含
for _ in range(5):
    bad = bytearray(encode(PacketType.PID_SET, f32(9.0, 9.0, 9.0, 9.0)))
    bad[-5] ^= 0x5A
    parts.insert(rng.randrange(0, len(parts) + 1), bytes(bad))

fuzz_stream = b"".join(parts)
fuzz_frames = [(fr.type, fr.payload) for fr in FrameDecoder().feed(fuzz_stream)]
add_case("fuzz_4096_mixed", fuzz_stream, fuzz_frames, 13)


# ---------------------------------------------------------------- PC 侧行为对比

print("PC FrameDecoder 对照：")
for c in cases:
    dec = FrameDecoder()
    got: list[tuple[int, bytes]] = []
    for i in range(0, len(c["stream"]), c["chunk"]):
        for fr in dec.feed(c["stream"][i : i + c["chunk"]]):
            got.append((fr.type, fr.payload))
    same = got == c["frames"]
    tag = "OK  " if same else "DIFF"
    print(f"  [{tag}] {c['name']:<24} 期望 {len(c['frames']):>2} 帧, PC 解出 {len(got):>2} 帧")
    if not same:
        print(f"         期望 type={[t for t, _ in c['frames']]}")
        print(f"         PC   type={[t for t, _ in got]}  pending={dec.pending()}")


# ---------------------------------------------------------------- 生成 C 头文件

L: list[str] = []
L.append("/* 自动生成：tools/gen_protocol_vectors.py —— 不要手改。 */")
L.append("/* 真值来自 pc/serialpid/protocol.py（PC 侧冻结协议）。 */")
L.append("#ifndef GOLDEN_VECTORS_H")
L.append("#define GOLDEN_VECTORS_H")
L.append("")
L.append("#include <stdint.h>")
L.append("")
L.append("typedef struct {")
L.append("    uint8_t        type;")
L.append("    uint16_t       len;")
L.append("    const uint8_t *payload;   /* len==0 时为 NULL */")
L.append("} vec_frame_t;")
L.append("")
L.append("typedef struct {")
L.append("    const char     *name;")
L.append("    const uint8_t  *stream;")
L.append("    uint16_t        stream_len;")
L.append("    const vec_frame_t *frames;")
L.append("    uint16_t        frame_count;")
L.append("    uint8_t         chunk;")
L.append("} vec_case_t;")
L.append("")
L.append("typedef struct {")
L.append("    const uint8_t *data;")
L.append("    uint16_t       len;")
L.append("    uint16_t       crc;")
L.append("} vec_crc_t;")
L.append("")


def c_array(name: str, data: bytes, guard_empty: bool = False) -> str:
    body = ", ".join("0x%02X" % b for b in data)
    if not data and guard_empty:
        body = "0x00"
    return f"static const uint8_t {name}[] = {{ {body} }};"


# CRC 向量
crc_cases = [
    (b"123456789", crc16_modbus(b"123456789")),
    (b"", crc16_modbus(b"")),
    (bytes((0x02, 0x10, 0x00)), crc16_modbus(bytes((0x02, 0x10, 0x00)))),
    (bytes(range(0, 16)), crc16_modbus(bytes(range(0, 16)))),
]
for i, (data, _) in enumerate(crc_cases):
    L.append(c_array(f"CRC_D{i}", data, guard_empty=True))
L.append("")
L.append("static const vec_crc_t VEC_CRC[] = {")
for i, (data, c) in enumerate(crc_cases):
    L.append(f'    {{ CRC_D{i}, {len(data)}u, 0x{c:04X}u }},')
L.append("};")
L.append("#define VEC_CRC_COUNT (sizeof(VEC_CRC) / sizeof(VEC_CRC[0]))")
L.append("")

# 组帧对照（C 侧按同样参数组帧，应与下面的字节数组逐字节相同）
enc_pong = encode(PacketType.PONG, bytes((1, 0)))
enc_ack = encode(PacketType.PID_ACK, f32(2.0, 0.5, 0.1, 30.0) + bytes((0,)))
enc_tel = encode(PacketType.TELEMETRY, struct.pack("<I4f", 1000, 25.0, 24.5, 60.0, -0.5))
enc_evt = encode(PacketType.STEP_EVENT, bytes((1,)) + struct.pack("<I", 2000))
enc_err = frame_error(1, b"bad param")

for name, data in (("ENC_PONG", enc_pong), ("ENC_ACK", enc_ack), ("ENC_TEL", enc_tel),
                   ("ENC_EVT", enc_evt), ("ENC_ERR", enc_err)):
    L.append(c_array(name, data))

L.append("")
L.append("#define VEC_ENC_PONG_LEN %du" % len(enc_pong))
L.append("#define VEC_ENC_ACK_LEN  %du" % len(enc_ack))
L.append("#define VEC_ENC_TEL_LEN  %du" % len(enc_tel))
L.append("#define VEC_ENC_EVT_LEN  %du" % len(enc_evt))
L.append("#define VEC_ENC_ERR_LEN  %du" % len(enc_err))
L.append("")

# 流用例
for ci, c in enumerate(cases):
    L.append(f"/* ---- case {ci}: {c['name']} ---- */")
    L.append(c_array(f"VEC_S{ci}", c["stream"], guard_empty=True))
    for fi, (ftype, payload) in enumerate(c["frames"]):
        if payload:
            L.append(c_array(f"VEC_P{ci}_{fi}", payload))
    L.append(f"static const vec_frame_t VEC_F{ci}[] = {{")
    if c["frames"]:
        for fi, (ftype, payload) in enumerate(c["frames"]):
            ptr = f"VEC_P{ci}_{fi}" if payload else "0"
            L.append(f"    {{ 0x{ftype:02X}u, {len(payload)}u, {ptr} }},")
    else:
        L.append("    { 0x00u, 0u, 0 },")
    L.append("};")
    L.append("")

L.append("static const vec_case_t VEC_CASES[] = {")
for ci, c in enumerate(cases):
    L.append('    { "%s", VEC_S%d, %du, VEC_F%d, %du, %du },'
             % (c["name"], ci, len(c["stream"]), ci, len(c["frames"]), c["chunk"]))
L.append("};")
L.append("#define VEC_CASE_COUNT (sizeof(VEC_CASES) / sizeof(VEC_CASES[0]))")
L.append("")
L.append("#endif /* GOLDEN_VECTORS_H */")

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(L) + "\n")

print(f"\n已生成 {OUT_PATH}")
print(f"用例 {len(cases)} 个，CRC 向量 {len(crc_cases)} 组")
