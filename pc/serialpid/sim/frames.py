"""帧级小工具：切割字节流、伪造坏 CRC、读帧类型。

链路劣化层与录制层都要按"帧"而不是"字节块"做决定（丢一帧、坏一帧的 CRC），
所以把切帧逻辑收敛到这里一份，避免两处各写一套扫描规则。

只做**无损**切割：认得出的完整帧逐帧返回，认不出的字节与不完整的尾段原样
作为最后一项返回，交给上层自己决定（劣化层原样透传，录制层跳过）。
"""

from __future__ import annotations

import struct
from typing import List, Optional

HEADER = b"\xaa\x55"
EOF = b"\x77\x77\x77\x77"
FRAME_OVERHEAD = 10
MAX_PAYLOAD = 250


def is_frame(chunk: bytes) -> bool:
    """``chunk`` 是否恰为一个完整帧（帧头、LEN 自洽、帧尾齐全）。"""
    if len(chunk) < FRAME_OVERHEAD or chunk[:2] != HEADER:
        return False
    length = chunk[2]
    total = FRAME_OVERHEAD + length
    return len(chunk) == total and chunk[-4:] == EOF


def frame_type(chunk: bytes) -> Optional[int]:
    """帧类型字节；不是完整帧返回 ``None``。"""
    return chunk[3] if is_frame(chunk) else None


def split_frames(data: bytes) -> List[bytes]:
    """把一段字节切成帧；不完整/无法识别的部分原样附在末尾。

    典型输入是 ``CommCore.send`` 写出的一帧，或噪声与半帧混在一起的字节块。
    输出保证 ``b"".join(result) == data``，调用方可以放心透传。
    """
    raw = bytes(data)
    frames: List[bytes] = []
    index = 0
    while index < len(raw):
        start = raw.find(HEADER, index)
        if start < 0:
            frames.append(raw[index:])
            break
        if start > index:
            frames.append(raw[index:start])
        remaining = raw[start:]
        if len(remaining) < 3:
            frames.append(remaining)
            break
        total = FRAME_OVERHEAD + remaining[2]
        chunk = remaining[:total]
        if len(chunk) < total:
            frames.append(remaining)
            break
        frames.append(chunk)
        index = start + total
    return frames


def corrupt_crc(raw: bytes) -> bytes:
    """把帧内 CRC 低字节翻一位，制造"帧结构对、校验错"的坏帧。

    只动 CRC 域，不动载荷：上位机解包器会按 CRC 失败丢弃整帧，正好模拟
    "线上被干扰坏掉"的现象。不是完整帧时原样返回。
    """
    if not is_frame(raw):
        return raw
    pos = -6  # EOF 4 字节 + CRC 2 字节
    return raw[:pos] + bytes((raw[pos] ^ 0x01,)) + raw[pos + 1:]


def make_noise(rng, count: int) -> bytes:
    """生成一段随机字节噪声；刻意避开 ``AA 55``，保证能被解包器识别为噪声。"""
    out = bytearray()
    while len(out) < count:
        value = rng.randint(0, 0xFF)
        if value == 0xAA and (not out or out[-1] == 0x55):
            value = 0x00
        out.append(value)
    return bytes(out)


def summarize(raw: bytes) -> str:
    """一行可读摘要（日志/报告用）。"""
    pkt = frame_type(raw)
    if pkt is None:
        return f"raw {len(raw)}B"
    return f"type=0x{pkt:02X} len={len(raw)}B"


__all__ = [
    "HEADER",
    "EOF",
    "FRAME_OVERHEAD",
    "MAX_PAYLOAD",
    "is_frame",
    "frame_type",
    "split_frames",
    "corrupt_crc",
    "make_noise",
    "summarize",
]
