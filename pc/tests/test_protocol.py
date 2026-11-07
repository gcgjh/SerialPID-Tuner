# -*- coding: utf-8 -*-
"""协议层 + 工具模块离线单测（无 Qt、无串口、无第三方依赖）。

运行方式（任选）::

    cd pc && python -m unittest discover -s tests -v
    cd pc && python tests/test_protocol.py
    cd pc && python -m pytest tests/test_protocol.py -v      # 装了 pytest 也行

覆盖：
    * CRC16-Modbus 标准向量与 PROTOCOL.md §5 三个对齐向量；
    * 每种包类型的封包/解包往返、载荷长度与数值校验；
    * 粘包、拆包、帧头/帧尾/CRC 错帧、超长 LEN、未知 TYPE、缓冲上限；
    * CSV 导出（表头/编码/BOM/回绕/NaN/原子写回滚）；
    * 预设管理（增删改查、持久化、损坏文件恢复、导入导出）。
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from serialpid import csv_export, preset_manager  # noqa: E402
from serialpid.csv_export import CsvExportError, CsvWriter, export_csv  # noqa: E402
from serialpid.preset_manager import (  # noqa: E402
    InvalidPresetError,
    Preset,
    PresetExistsError,
    PresetManager,
    PresetNotFoundError,
)
from serialpid.protocol import (  # noqa: E402
    AckResult,
    CrcError,
    EOF,
    FRAME_OVERHEAD,
    Frame,
    FrameDecoder,
    FrameFormatError,
    MAX_PAYLOAD,
    PacketType,
    PayloadError,
    StepPhase,
    crc16_modbus,
    describe_frame,
    encode,
    parse_fw_version,
    parse_frame,
    pack_error,
    pack_fw_version,
    pack_pid_ack,
    pack_pid_set,
    pack_pong,
    pack_sp_set,
    pack_step_event,
    pack_step_start,
    pack_telemetry,
    unpack_error,
    unpack_pid_ack,
    unpack_pid_set,
    unpack_pong,
    unpack_sp_set,
    unpack_step_event,
    unpack_step_start,
    unpack_telemetry,
)

# PROTOCOL.md §5 对齐向量（冻结，双方必须逐字节复现）
VEC_PING = "AA 55 00 01 C0 70 77 77 77 77"
VEC_TELEMETRY = (
    "AA 55 14 20 E8 03 00 00 00 00 C8 41 00 00 C4 41 00 00 70 42 "
    "00 00 00 BF 64 90 77 77 77 77"
)
VEC_PID_SET = (
    "AA 55 10 10 00 00 00 40 00 00 00 3F CD CC CC 3D 00 00 F0 41 "
    "AA 29 77 77 77 77"
)


def hex_to_bytes(text: str) -> bytes:
    return bytes.fromhex(text)


class TestCrc(unittest.TestCase):
    def test_standard_check_value(self) -> None:
        # CRC-16/MODBUS 标准校验值，PROTOCOL.md §2.2
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_empty_input_is_init_value(self) -> None:
        self.assertEqual(crc16_modbus(b""), 0xFFFF)

    def test_accepts_bytearray_and_memoryview(self) -> None:
        self.assertEqual(crc16_modbus(bytearray(b"123456789")), 0x4B37)
        self.assertEqual(crc16_modbus(memoryview(b"123456789")), 0x4B37)

    def test_all_vectors_crc(self) -> None:
        # 帧内 CRC 按小端解码后与文档向量字节一致
        self.assertEqual(crc16_modbus(hex_to_bytes(VEC_PING)[2:4]), 0x70C0)
        self.assertEqual(
            crc16_modbus(hex_to_bytes(VEC_TELEMETRY)[2:4 + 20]), 0x9064
        )
        self.assertEqual(
            crc16_modbus(hex_to_bytes(VEC_PID_SET)[2:4 + 16]), 0x29AA
        )
        # 字节序回归：帧里存的是 LE，文档 §5.2/§5.3 的整数值标注按 BE 读数，
        # 两者矛盾时以帧内字节 + 算法为准（已上报主 Agent 待修文档）。
        for vector, value in (
            (VEC_PING, 0x70C0),
            (VEC_TELEMETRY, 0x9064),
            (VEC_PID_SET, 0x29AA),
        ):
            raw = hex_to_bytes(vector)
            n = raw[2]
            self.assertEqual(raw[4 + n:6 + n], struct.pack("<H", value))



class TestEncode(unittest.TestCase):
    def test_spec_vector_ping(self) -> None:
        self.assertEqual(encode(PacketType.PING).hex(" ").upper(), VEC_PING)

    def test_spec_vector_telemetry(self) -> None:
        frame = encode(
            PacketType.TELEMETRY, pack_telemetry(1000, 25.0, 24.5, 60.0, -0.5)
        )
        self.assertEqual(frame.hex(" ").upper(), VEC_TELEMETRY)

    def test_spec_vector_pid_set(self) -> None:
        frame = encode(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, 30.0))
        self.assertEqual(frame.hex(" ").upper(), VEC_PID_SET)

    def test_layout_fields(self) -> None:
        frame = encode(PacketType.SP_SET, struct.pack("<f", 12.5))
        self.assertEqual(frame[0], 0xAA)
        self.assertEqual(frame[1], 0x55)
        self.assertEqual(frame[2], 4)             # LEN
        self.assertEqual(frame[3], PacketType.SP_SET)
        self.assertEqual(frame[-4:], EOF)
        self.assertEqual(len(frame), FRAME_OVERHEAD + 4)

    def test_max_payload_boundary(self) -> None:
        encode(PacketType.PID_SET, b"\x00" * MAX_PAYLOAD)   # 250 合法
        with self.assertRaises(ValueError):
            encode(PacketType.PID_SET, b"\x00" * (MAX_PAYLOAD + 1))

    def test_invalid_type(self) -> None:
        with self.assertRaises(ValueError):
            encode(-1)
        with self.assertRaises(ValueError):
            encode(256)

    def test_payload_type_check(self) -> None:
        with self.assertRaises(TypeError):
            encode(PacketType.PING, "not-bytes")  # type: ignore[arg-type]


class TestPayloads(unittest.TestCase):
    def test_pid_set_roundtrip(self) -> None:
        payload = pack_pid_set(2.0, 0.5, 0.1, 30.0)
        self.assertEqual(len(payload), 16)
        kp, ki, kd, sp = unpack_pid_set(payload)
        self.assertAlmostEqual(kp, 2.0, places=6)
        self.assertAlmostEqual(ki, 0.5, places=6)
        self.assertAlmostEqual(kd, 0.1, places=5)   # float32 精度
        self.assertAlmostEqual(sp, 30.0, places=6)

    def test_pid_set_rejects_non_finite(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf"), 1e39):
            with self.assertRaises(ValueError):
                pack_pid_set(bad, 0.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            pack_pid_set(0.0, 0.0, 0.0, float("nan"))

    def test_pid_set_rejects_non_numeric(self) -> None:
        with self.assertRaises(ValueError):
            pack_pid_set("abc", 0.0, 0.0, 0.0)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            pack_pid_set(None, 0.0, 0.0, 0.0)  # type: ignore[arg-type]

    def test_sp_set_roundtrip(self) -> None:
        self.assertEqual(len(pack_sp_set(25.0)), 4)
        self.assertAlmostEqual(unpack_sp_set(pack_sp_set(25.0)), 25.0)
        with self.assertRaises(ValueError):
            pack_sp_set(float("nan"))

    def test_pid_ack_roundtrip(self) -> None:
        payload = pack_pid_ack(2.0, 0.5, 0.1, 30.0, AckResult.ACCEPTED)
        self.assertEqual(len(payload), 17)
        kp, ki, kd, sp, result = unpack_pid_ack(payload)
        self.assertEqual(result, AckResult.ACCEPTED)
        self.assertAlmostEqual(kp, 2.0)
        with self.assertRaises(ValueError):
            pack_pid_ack(1.0, 0.0, 0.0, 0.0, 2)

    def test_pong_and_version_nibble(self) -> None:
        payload = pack_pong(pack_fw_version(1, 0), 2)
        self.assertEqual(payload, b"\x10\x02")
        self.assertEqual(unpack_pong(payload), (0x10, 2))
        self.assertEqual(parse_fw_version(0x12), (1, 2))
        with self.assertRaises(ValueError):
            pack_pong(0x10, 3)
        with self.assertRaises(ValueError):
            pack_fw_version(16, 0)

    def test_telemetry_roundtrip_spec_values(self) -> None:
        payload = pack_telemetry(1000, 25.0, 24.5, 60.0, -0.5)
        self.assertEqual(len(payload), 20)
        fields = unpack_telemetry(payload)
        self.assertEqual(fields, (1000, 25.0, 24.5, 60.0, -0.5))

    def test_telemetry_allows_nan_from_firmware(self) -> None:
        # 固件 0/0 等异常不应导致解码失败
        payload = pack_telemetry(1, float("nan"), 1.0, 1.0, 1.0)
        t_ms, sp, _, _, _ = unpack_telemetry(payload)
        self.assertEqual(t_ms, 1)
        self.assertTrue(math.isnan(sp))

    def test_telemetry_t_ms_range(self) -> None:
        pack_telemetry(0xFFFFFFFF, 0.0, 0.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            pack_telemetry(-1, 0.0, 0.0, 0.0, 0.0)
        with self.assertRaises(ValueError):
            pack_telemetry(1 << 32, 0.0, 0.0, 0.0, 0.0)

    def test_step_start_roundtrip(self) -> None:
        payload = pack_step_start(10.0, 2000)
        self.assertEqual(len(payload), 8)
        self.assertEqual(unpack_step_start(payload), (10.0, 2000))
        with self.assertRaises(ValueError):
            pack_step_start(float("inf"), 0)
        with self.assertRaises(ValueError):
            pack_step_start(1.0, -1)

    def test_step_event_roundtrip(self) -> None:
        payload = pack_step_event(StepPhase.DONE, 12345)
        self.assertEqual(len(payload), 5)
        self.assertEqual(unpack_step_event(payload), (2, 12345))
        with self.assertRaises(ValueError):
            pack_step_event(9, 0)

    def test_error_roundtrip_ascii_and_chinese(self) -> None:
        payload = pack_error(3, "over temperature")
        self.assertEqual(unpack_error(payload), (3, "over temperature"))
        payload = pack_error(7, "传感器断线")
        self.assertEqual(unpack_error(payload), (7, "传感器断线"))
        # msg_len 以字节计（中文 5 字 = 15 字节）
        self.assertEqual(payload[1], len("传感器断线".encode("utf-8")))

    def test_error_too_long(self) -> None:
        with self.assertRaises(ValueError):
            pack_error(1, "a" * (MAX_PAYLOAD - 1))

    def test_error_bad_msg_len(self) -> None:
        with self.assertRaises(PayloadError):
            unpack_error(b"\x01\x05ab")
        with self.assertRaises(PayloadError):
            unpack_error(b"\x01")

    def test_length_mismatch_raises_payload_error(self) -> None:
        cases = [
            (unpack_pid_set, b"\x00" * 12),
            (unpack_pid_ack, b"\x00" * 16),
            (unpack_sp_set, b"\x00" * 8),
            (unpack_pong, b"\x00" * 1),
            (unpack_step_start, b"\x00" * 4),
            (unpack_step_event, b"\x00" * 4),
        ]
        for func, payload in cases:
            with self.subTest(func=func.__name__):
                with self.assertRaises(PayloadError):
                    func(payload)
        self.assertTrue(issubclass(PayloadError, ValueError))

    def test_empty_payload_types(self) -> None:
        self.assertEqual(encode(PacketType.PING).hex(" ").upper(), VEC_PING)
        self.assertEqual(len(encode(PacketType.STEP_STOP)), FRAME_OVERHEAD)
        self.assertEqual(len(encode(PacketType.RESET)), FRAME_OVERHEAD)


class TestParseFrame(unittest.TestCase):
    def test_parse_spec_vectors(self) -> None:
        frame = parse_frame(hex_to_bytes(VEC_TELEMETRY))
        self.assertEqual(frame.type, PacketType.TELEMETRY)
        self.assertEqual(unpack_telemetry(frame.payload), (1000, 25.0, 24.5, 60.0, -0.5))

    def test_errors(self) -> None:
        good = hex_to_bytes(VEC_PING)
        with self.assertRaises(FrameFormatError):
            parse_frame(good[:5])
        with self.assertRaises(FrameFormatError):
            parse_frame(b"\xaa\x00" + good[2:])
        with self.assertRaises(FrameFormatError):
            parse_frame(good[:-1] + b"\x00")
        with self.assertRaises(FrameFormatError):
            parse_frame(good + b"\x00")
        bad_crc = bytearray(good)
        bad_crc[4] ^= 0xFF
        with self.assertRaises(CrcError):
            parse_frame(bytes(bad_crc))

    def test_frame_to_bytes(self) -> None:
        frame = Frame(PacketType.SP_SET, pack_sp_set(18.5))
        self.assertEqual(parse_frame(frame.to_bytes()), frame)


class TestDecoder(unittest.TestCase):
    def test_byte_by_byte(self) -> None:
        frame = encode(
            PacketType.TELEMETRY, pack_telemetry(1000, 25.0, 24.5, 60.0, -0.5)
        )
        decoder = FrameDecoder()
        frames = []
        for byte in frame:
            frames.extend(decoder.feed(bytes((byte,))))
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            unpack_telemetry(frames[0].payload), (1000, 25.0, 24.5, 60.0, -0.5)
        )

    def test_split_at_every_offset(self) -> None:
        for frame in (
            encode(PacketType.PING),
            encode(PacketType.PID_SET, pack_pid_set(1.0, 0.2, 0.05, 40.0)),
        ):
            for cut in range(1, len(frame)):
                decoder = FrameDecoder()
                frames = decoder.feed(frame[:cut]) + decoder.feed(frame[cut:])
                with self.subTest(cut=cut, pkt=frame[3]):
                    self.assertEqual(len(frames), 1)
                    self.assertEqual(frames[0].payload, frame[4:4 + frame[2]])

    def test_two_frames_one_chunk(self) -> None:
        f1 = encode(PacketType.PING)
        f2 = encode(PacketType.STEP_START, pack_step_start(10.0, 2000))
        decoder = FrameDecoder()
        frames = decoder.feed(f1 + f2)
        self.assertEqual([f.type for f in frames], [PacketType.PING, PacketType.STEP_START])

    def test_random_chunking(self) -> None:
        source = b"".join(
            encode(PacketType.TELEMETRY, pack_telemetry(i, 25.0, 24.0, 50.0, 1.0))
            for i in range(50)
        )
        decoder = FrameDecoder()
        frames = []
        rng = random.Random(20261105)
        index = 0
        while index < len(source):
            step = rng.randint(1, 40)
            frames.extend(decoder.feed(source[index:index + step]))
            index += step
        self.assertEqual(len(frames), 50)
        self.assertEqual([unpack_telemetry(f.payload)[0] for f in frames], list(range(50)))
        self.assertEqual(decoder.pending(), 0)
        self.assertEqual(decoder.stats.frames, 50)
        self.assertEqual(decoder.stats.crc_errors, 0)
        self.assertEqual(decoder.stats.eof_errors, 0)

    def test_aa_byte_not_followed_by_55_does_not_stall(self) -> None:
        # 旧实现会卡死在 AA 后跟非 55 的字节上
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"\xaa\x00\x01\x02"), [])
        frames = decoder.feed(encode(PacketType.PING))
        self.assertEqual(len(frames), 1)

    def test_partial_header_preserved(self) -> None:
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"\xaa"), [])
        self.assertEqual(decoder.pending(), 1)
        frame = encode(PacketType.PING)
        frames = decoder.feed(frame[1:])
        self.assertEqual(len(frames), 1)

    def test_garbage_prefix(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(b"\x00\x11\x22\x33" + encode(PacketType.PING))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.stats.dropped_bytes, 4)

    def test_garbage_between_frames(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(
            encode(PacketType.PING) + b"\xde\xad\xbe\xef" + encode(PacketType.PING)
        )
        self.assertEqual(len(frames), 2)

    def test_bad_crc_drops_frame_and_resyncs(self) -> None:
        bad = bytearray(encode(PacketType.PING))
        bad[4] ^= 0xFF  # 破坏 CRC，帧尾仍正常 -> 按 §3.4 丢整帧
        decoder = FrameDecoder()
        frames = decoder.feed(bytes(bad))
        self.assertEqual(frames, [])
        self.assertEqual(decoder.stats.crc_errors, 1)
        self.assertEqual(decoder.pending(), 0)
        frames = decoder.feed(encode(PacketType.PING))
        self.assertEqual(len(frames), 1)

    def test_bad_crc_then_good_frame_same_chunk(self) -> None:
        bad = bytearray(encode(PacketType.TELEMETRY, pack_telemetry(1, 1, 1, 1, 1)))
        bad[8] ^= 0xFF
        decoder = FrameDecoder()
        frames = decoder.feed(bytes(bad) + encode(PacketType.PING))
        self.assertEqual([f.type for f in frames], [PacketType.PING])
        self.assertEqual(decoder.stats.crc_errors, 1)

    def test_tail_mismatch_uses_frame_boundary(self) -> None:
        # 伪帧头 + 伪造帧尾，随后才是真帧：必须只丢字节，不能吞掉真帧
        decoder = FrameDecoder()
        garbage = b"\xaa\x55\x00\x01\x12\x34" + b"\x00" * 4
        frames = decoder.feed(garbage + encode(PacketType.PING))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.stats.eof_errors, 1)

    def test_tail_check_not_buffer_tail(self) -> None:
        # 帧后还有尾随垃圾时，旧实现会用缓冲区末 4 字节当帧尾而丢帧
        decoder = FrameDecoder()
        frames = decoder.feed(encode(PacketType.PING) + b"\x00\x00\x00")
        self.assertEqual(len(frames), 1)

    def test_oversize_len_resync(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(b"\xaa\x55\xff\x10" + b"\x00" * 20 + encode(PacketType.PING))
        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.stats.oversize_errors, 1)

    def test_unknown_type_is_preserved(self) -> None:
        decoder = FrameDecoder()
        frames = decoder.feed(encode(0x55, b"\x01\x02\x03"))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].type, 0x55)
        self.assertEqual(frames[0].payload, b"\x01\x02\x03")
        self.assertEqual(frames[0].name, "UNKNOWN(0x55)")

    def test_truncated_frame_completed_later(self) -> None:
        frame = encode(PacketType.PID_SET, pack_pid_set(1.0, 0.1, 0.01, 20.0))
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(frame[:-3]), [])
        frames = decoder.feed(frame[-3:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, frame[4:20])

    def test_reset_discards_partial_frame(self) -> None:
        frame = encode(PacketType.PING)
        decoder = FrameDecoder()
        decoder.feed(frame[:6])
        decoder.reset()
        frames = decoder.feed(frame[6:])
        self.assertEqual(frames, [])
        self.assertEqual(decoder.pending(), 0)
        decoder.reset(clear_stats=True)
        self.assertEqual(decoder.stats.bytes_in, 0)

    def test_stats(self) -> None:
        decoder = FrameDecoder()
        decoder.feed(encode(PacketType.PING))
        decoder.feed(encode(PacketType.PING))
        self.assertEqual(decoder.stats.frames, 2)
        self.assertEqual(decoder.stats.bytes_in, 20)
        self.assertEqual(decoder.stats.as_dict()["frames"], 2)

    def test_no_exception_on_random_noise(self) -> None:
        rng = random.Random(7)
        decoder = FrameDecoder()
        chunks = [rng.randbytes(rng.randint(1, 200)) for _ in range(30)]
        for chunk in chunks:
            decoder.feed(chunk)  # 不抛异常即可
        self.assertEqual(decoder.stats.bytes_in, sum(len(c) for c in chunks))
        decoder.reset()
        frames = decoder.feed(encode(PacketType.PING))
        self.assertEqual(len(frames), 1)

    def test_buffer_bounded(self) -> None:
        decoder = FrameDecoder()
        decoder.feed(b"\x00" * 10000 + b"\xaa\x55\x00")
        self.assertLessEqual(decoder.pending(), 3)

    def test_accepts_memoryview_and_bytearray(self) -> None:
        frame = encode(PacketType.PING)
        decoder = FrameDecoder()
        self.assertEqual(len(decoder.feed(memoryview(frame))), 1)
        self.assertEqual(len(decoder.feed(bytearray(frame))), 1)
        self.assertEqual(decoder.feed(b""), [])

    def test_describe_all_packet_types(self) -> None:
        samples = [
            Frame(PacketType.PING, b""),
            Frame(PacketType.PONG, pack_pong(0x10, 1)),
            Frame(PacketType.PID_SET, pack_pid_set(2.0, 0.5, 0.1, 30.0)),
            Frame(PacketType.PID_ACK, pack_pid_ack(2.0, 0.5, 0.1, 30.0, 1)),
            Frame(PacketType.SP_SET, pack_sp_set(30.0)),
            Frame(PacketType.TELEMETRY, pack_telemetry(1, 2, 3, 4, -1)),
            Frame(PacketType.STEP_START, pack_step_start(5.0, 1000)),
            Frame(PacketType.STEP_STOP, b""),
            Frame(PacketType.STEP_EVENT, pack_step_event(2, 100)),
            Frame(PacketType.ERROR, pack_error(1, "fault")),
            Frame(PacketType.RESET, b""),
            Frame(0x55, b"\x01"),
        ]
        for frame in samples:
            with self.subTest(pkt=frame.type):
                self.assertTrue(describe_frame(frame))
        # 载荷异常时退回十六进制，不抛异常
        self.assertIn("载荷异常", describe_frame(Frame(PacketType.TELEMETRY, b"\x00")))


class TestCsvExport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path(self, name: str = "run.csv") -> str:
        return os.path.join(self.dir, name)

    def _read(self, path: str) -> list:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.reader(handle))

    def test_header_is_first_line_and_spec_columns(self) -> None:
        path = self._path()
        rows = export_csv([], path)
        self.assertEqual(rows, 0)
        table = self._read(path)
        self.assertEqual(table[0], ["t_ms", "SP", "PV", "OUT", "err"])

    def test_utf8_bom_for_excel(self) -> None:
        path = self._path()
        export_csv([(1000, 25.0, 24.5, 60.0, -0.5)], path)
        with open(path, "rb") as handle:
            head = handle.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf")

    def test_plain_utf8_without_bom(self) -> None:
        path = self._path("origin.csv")
        export_csv([(0, 1.0, 1.0, 1.0, 0.0)], path, encoding="utf-8")
        with open(path, "rb") as handle:
            self.assertFalse(handle.read(3).startswith(b"\xef\xbb\xbf"))

    def test_objects_dicts_and_tuples(self) -> None:
        obj = SimpleNamespace(t_ms=1000, sp=25.0, pv=24.5, out=60.0, err=-0.5)
        upper = SimpleNamespace(t_ms=1500, SP=26.0, PV=25.0, OUT=61.0, err=1.0)
        mapping = {"t_ms": 2000, "SP": 27.0, "PV": 26.0, "OUT": 62.0, "err": 1.0}
        path = self._path()
        rows = export_csv([obj, upper, mapping, (2500, 28.0, 27.0, 63.0, 1.0)], path)
        self.assertEqual(rows, 4)
        table = self._read(path)
        self.assertEqual(table[1], ["1000", "25.000000", "24.500000", "60.000000", "-0.500000"])
        self.assertEqual(table[2], ["1500", "26.000000", "25.000000", "61.000000", "1.000000"])
        self.assertEqual(table[4], ["2500", "28.000000", "27.000000", "63.000000", "1.000000"])

    def test_derive_elapsed_handles_u32_wrap(self) -> None:
        path = self._path()
        export_csv(
            [(0xFFFFFF00, 1.0, 1.0, 1.0, 0.0), (0x00000100, 1.0, 1.0, 1.0, 0.0)],
            path,
            derive_elapsed=True,
        )
        table = self._read(path)
        self.assertEqual(table[0], ["t_ms", "elapsed_s", "SP", "PV", "OUT", "err"])
        self.assertEqual(table[1][1], "0.000000")
        self.assertEqual(table[2][1], "0.512000")   # 256 + 256 = 512 ms

    def test_nan_written_as_empty(self) -> None:
        path = self._path()
        export_csv([(0, float("nan"), 1.0, float("inf"), 0.0)], path)
        self.assertEqual(self._read(path)[1], ["0", "", "1.000000", "", "0.000000"])

    def test_metadata_as_comments(self) -> None:
        path = self._path()
        export_csv(
            [(0, 1.0, 1.0, 1.0, 0.0)],
            path,
            metadata={"port": "COM3", "baud": 115200, "Kp": 2.0},
            comment_meta=True,
        )
        table = self._read(path)
        self.assertEqual(table[0], ["# port=COM3"])
        self.assertEqual(table[1], ["# baud=115200"])
        self.assertEqual(table[3], ["t_ms", "SP", "PV", "OUT", "err"])

    def test_metadata_ignored_by_default(self) -> None:
        path = self._path()
        export_csv([(0, 1.0, 1.0, 1.0, 0.0)], path, metadata={"port": "COM3"})
        self.assertEqual(self._read(path)[0], ["t_ms", "SP", "PV", "OUT", "err"])

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(CsvExportError):
            export_csv([], self._path(), fields=("t_ms", "Kp"))

    def test_missing_field_reports_row(self) -> None:
        path = self._path()
        with self.assertRaises(CsvExportError) as ctx:
            export_csv([(0, 1.0, 1.0, 1.0, 0.0), SimpleNamespace(t_ms=5)], path)
        self.assertIn("第 2 条", str(ctx.exception))

    def test_failed_export_keeps_previous_file(self) -> None:
        path = self._path()
        export_csv([(0, 1.0, 1.0, 1.0, 0.0)], path)
        with open(path, "rb") as handle:
            before = handle.read()
        with self.assertRaises(CsvExportError):
            export_csv([SimpleNamespace(t_ms=1)], path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".serialpid-")]
        self.assertEqual(leftovers, [])

    def test_custom_float_format_and_delimiter(self) -> None:
        path = self._path("semi.csv")
        export_csv([(0, 1.234567, 2.0, 3.0, -1.0)], path, float_fmt="{:.3f}")
        table = self._read(path)
        self.assertEqual(table[1][1], "1.235")

    def test_empty_directory_is_created(self) -> None:
        path = os.path.join(self.dir, "nested", "deep", "run.csv")
        self.assertEqual(export_csv([(0, 1.0, 1.0, 1.0, 0.0)], path), 1)
        self.assertTrue(os.path.exists(path))

    def test_non_atomic_mode(self) -> None:
        path = self._path()
        self.assertEqual(export_csv([(0, 1.0, 1.0, 1.0, 0.0)], path, atomic=False), 1)
        self.assertEqual(len(self._read(path)), 2)

    def test_stream_writer(self) -> None:
        path = self._path("stream.csv")
        with CsvWriter(path, flush_every=1) as writer:
            for i in range(3):
                writer.write((i * 50, 25.0, 24.0 + i * 0.1, 50.0, 1.0))
        self.assertEqual(writer.rows, 3)
        table = self._read(path)
        self.assertEqual(len(table), 4)
        self.assertEqual(table[0], ["t_ms", "SP", "PV", "OUT", "err"])

    def test_stream_writer_append_mode(self) -> None:
        path = self._path("append.csv")
        with CsvWriter(path) as writer:
            writer.write((0, 1.0, 1.0, 1.0, 0.0))
        with CsvWriter(path, append=True) as writer:
            writer.write((50, 2.0, 2.0, 2.0, 0.0))
        table = self._read(path)
        self.assertEqual(len(table), 3)
        self.assertEqual(table[0], ["t_ms", "SP", "PV", "OUT", "err"])
        self.assertEqual(table[2][0], "50")

    def test_stream_writer_derive_elapsed(self) -> None:
        path = self._path("stream_elapsed.csv")
        with CsvWriter(path, derive_elapsed=True) as writer:
            writer.write((0xFFFFFF00, 1.0, 1.0, 1.0, 0.0))
            writer.write((0x00000100, 1.0, 1.0, 1.0, 0.0))
        table = self._read(path)
        self.assertEqual(table[2][1], "0.512000")


class TestPresetManager(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path(self, name: str = "presets.json") -> str:
        return os.path.join(self.dir, name)

    def test_first_run_creates_defaults(self) -> None:
        manager = PresetManager(self._path())
        self.assertEqual(len(manager), 3)
        self.assertTrue(os.path.exists(manager.path))
        self.assertIn("保守起步", manager.names())

    def test_list_returns_copies(self) -> None:
        manager = PresetManager(self._path())
        preset = manager.list()[0]
        preset.kp = 999.0
        self.assertNotEqual(manager.list()[0].kp, 999.0)

    def test_add_get_duplicate(self) -> None:
        manager = PresetManager(self._path())
        manager.add("快调", 3.0, 0.3, 0.05, 45.0, "测试")
        got = manager.get("快调")
        self.assertAlmostEqual(got.kp, 3.0)
        self.assertAlmostEqual(got.sp, 45.0)
        with self.assertRaises(PresetExistsError):
            manager.add("快调", 1.0, 0.0, 0.0)

    def test_case_insensitive_uniqueness(self) -> None:
        manager = PresetManager(self._path())
        manager.add("Loop", 1.0, 0.0, 0.0)
        with self.assertRaises(PresetExistsError):
            manager.add("loop", 2.0, 0.0, 0.0)
        self.assertIn("LOOP", manager)

    def test_update_partial_and_clear_sp(self) -> None:
        manager = PresetManager(self._path())
        manager.add("调参", 1.0, 0.1, 0.0, 30.0)
        updated = manager.update("调参", kp=2.5, note="改 Kp")
        self.assertAlmostEqual(updated.kp, 2.5)
        self.assertEqual(updated.note, "改 Kp")
        cleared = manager.update("调参", sp=None)
        self.assertIsNone(cleared.sp)
        self.assertAlmostEqual(cleared.kp, 2.5)

    def test_update_missing(self) -> None:
        manager = PresetManager(self._path())
        with self.assertRaises(PresetNotFoundError):
            manager.update("不存在", kp=1.0)

    def test_delete_and_rename(self) -> None:
        manager = PresetManager(self._path())
        manager.add("A", 1.0, 0.0, 0.0)
        manager.rename("A", "B")
        self.assertIn("B", manager)
        self.assertNotIn("A", manager)
        manager.delete("B")
        self.assertNotIn("B", manager)
        with self.assertRaises(PresetNotFoundError):
            manager.delete("B")

    def test_rename_conflict(self) -> None:
        manager = PresetManager(self._path())
        manager.add("A", 1.0, 0.0, 0.0)
        manager.add("C", 2.0, 0.0, 0.0)
        with self.assertRaises(PresetExistsError):
            manager.rename("A", "C")
        # 仅改大小写允许
        manager.rename("A", "a")
        self.assertEqual(manager.get("a").name, "a")

    def test_persistence_across_instances(self) -> None:
        manager = PresetManager(self._path())
        manager.add("持久", 4.0, 0.4, 0.04, 50.0, "跨实例")
        del manager
        again = PresetManager(self._path())
        preset = again.get("持久")
        self.assertAlmostEqual(preset.kp, 4.0)
        self.assertEqual(preset.note, "跨实例")

    def test_save_with_overwrite_false(self) -> None:
        manager = PresetManager(self._path())
        with self.assertRaises(PresetExistsError):
            manager.save(Preset("保守起步", 1.0, 0.0, 0.0), overwrite=False)
        manager.save(Preset("新", 1.0, 0.0, 0.0))
        self.assertIn("新", manager)

    def test_validation(self) -> None:
        manager = PresetManager(self._path())
        with self.assertRaises(InvalidPresetError):
            manager.add("坏", float("nan"), 0.0, 0.0)
        with self.assertRaises(InvalidPresetError):
            manager.add("坏", 1e9, 0.0, 0.0)
        with self.assertRaises(InvalidPresetError):
            manager.add("", 1.0, 0.0, 0.0)
        with self.assertRaises(InvalidPresetError):
            manager.add("x" * 33, 1.0, 0.0, 0.0)
        with self.assertRaises(InvalidPresetError):
            manager.add("坏", 1.0, 0.0, 0.0, note="n" * 201)

    def test_corrupt_file_strict_raises(self) -> None:
        path = self._path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(preset_manager.PresetError):
            PresetManager(path, strict=True)

    def test_corrupt_file_recovery_backs_up(self) -> None:
        path = self._path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        manager = PresetManager(path)
        self.assertIsNotNone(manager.warning)
        self.assertEqual(len(manager), 3)
        backups = [n for n in os.listdir(self.dir) if ".corrupt-" in n]
        self.assertEqual(len(backups), 1)
        # 坏文件内容仍在备份里，没有被悄悄丢弃
        with open(os.path.join(self.dir, backups[0]), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")

    def test_invalid_entries_are_skipped(self) -> None:
        path = self._path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema": "serialpid.presets",
                    "version": 1,
                    "presets": [
                        {"name": "好", "kp": 1.0, "ki": 0.1, "kd": 0.0},
                        {"name": "坏", "kp": "abc", "ki": 0.1, "kd": 0.0},
                        {"kp": 1.0, "ki": 0.1, "kd": 0.0},
                    ],
                },
                handle,
            )
        manager = PresetManager(path)
        self.assertEqual(manager.names(), ["好"])
        self.assertIn("2 条", manager.warning or "")

    def test_alien_schema_raises(self) -> None:
        path = self._path()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema": "other", "presets": []}, handle)
        with self.assertRaises(preset_manager.PresetError):
            PresetManager(path, strict=True)

    def test_export_and_import(self) -> None:
        source = PresetManager(self._path("source.json"))
        source.add("导出项", 2.0, 0.2, 0.02, 33.0, "从 source 导出")
        exported = self._path("exported.json")
        self.assertEqual(source.export_to(exported), len(source))

        target = PresetManager(self._path("target.json"), create_defaults=False)
        result = target.import_from(exported, overwrite=False)
        self.assertEqual(result.added, len(source))
        self.assertEqual(result.updated, 0)
        self.assertIn("导出项", target)

        again = target.import_from(exported, overwrite=True)
        self.assertEqual(again.updated, len(source))
        self.assertEqual(again.added, 0)

    def test_export_file_is_valid_schema(self) -> None:
        manager = PresetManager(self._path())
        exported = self._path("dump.json")
        manager.export_to(exported)
        with open(exported, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["schema"], "serialpid.presets")
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["presets"]), len(manager))
        for item in data["presets"]:
            for key in ("name", "kp", "ki", "kd", "sp", "note", "updated_at"):
                self.assertIn(key, item)

    def test_no_temp_files_left(self) -> None:
        manager = PresetManager(self._path())
        manager.add("X", 1.0, 0.0, 0.0)
        leftovers = [n for n in os.listdir(self.dir) if ".tmp-" in n]
        self.assertEqual(leftovers, [])

    def test_restore_defaults_avoids_overwriting_user_preset(self) -> None:
        manager = PresetManager(self._path("user.json"), create_defaults=False)
        manager.add("保守起步", 9.0, 0.9, 0.0)
        manager.restore_defaults()
        self.assertAlmostEqual(manager.get("保守起步").kp, 9.0)
        self.assertIn("保守起步 (默认)", manager.names())

    def test_empty_file_creates_defaults(self) -> None:
        path = self._path("empty.json")
        with open(path, "w", encoding="utf-8"):
            pass
        manager = PresetManager(path)
        self.assertEqual(len(manager), 3)
        self.assertTrue(manager.warning)


if __name__ == "__main__":
    unittest.main(verbosity=2)
