"""卡34：真机首联脚本的纯逻辑部分（不碰串口）。

验收口径：
* 遥测统计按固件 ``t_ms`` 差分算周期/抖动，不被 PC 侧 USB 批量上报带偏；
* 遥测 CSV 固定六列，数值与帧载荷一致，可直接当回归基线；
* 第 3 步对"越界值被拒"和"越界值被接受"分别判 PASS/FAIL，判据不能只看有没有 ACK。
"""

from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

import bringup_check  # noqa: E402
from serialpid.protocol import (  # noqa: E402
    AckResult,
    Frame,
    PacketType,
    encode,
    pack_error,
    pack_pid_ack,
    pack_pid_set,
    pack_telemetry,
)


def _telemetry_frames(count: int, step_ms: int = 50):
    return [
        (index * 0.05, Frame(PacketType.TELEMETRY,
                             pack_telemetry(index * step_ms, 30.0, 25.0, 50.0, 5.0)))
        for index in range(count)
    ]


def test_telemetry_stats_uses_firmware_timeline():
    stats = bringup_check._telemetry_stats(_telemetry_frames(10))

    assert stats["count"] == 10
    assert stats["fw_mean"] == 50.0
    assert stats["fw_max_jitter"] == 0.0
    assert stats["t_mono"] is True
    assert stats["pv_min"] == stats["pv_max"] == 25.0


def test_write_telemetry_has_fixed_six_columns(tmp_path):
    link = bringup_check.Link()
    link.records = [
        {
            "t": 1.0 + index * 0.05,
            "dir": "down",
            "type": PacketType.TELEMETRY,
            "raw": encode(PacketType.TELEMETRY,
                          pack_telemetry(index * 50, 30.0, 25.0, 50.0, 5.0)).hex(),
        }
        for index in range(3)
    ]
    path = tmp_path / "telemetry.csv"
    rows = bringup_check.write_telemetry(link, str(path))

    assert rows == 3
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["pc_time_s", "t_ms", "sp", "pv", "out", "err"]
        first = next(reader)
    assert first["t_ms"] == "0"
    assert first["sp"] == "30.0000"
    assert first["err"] == "5.0000"


class _StubLink:
    """只实现第 3 步用到的 write / collect，``reject`` 控制是否回 ERROR。"""

    def __init__(self, reject: bool) -> None:
        self.reject = reject
        self.sent = []

    def write(self, raw: bytes) -> None:
        self.sent.append(raw)

    def transact(self, raw: bytes, expect, timeout: float = 1.5):
        self.write(raw)
        frames = self.collect(timeout)
        return [frame for _, frame in frames if frame.type in expect], frames

    def collect(self, seconds, until=None):
        last = self.sent[-1]
        frame_type = last[3]
        if frame_type == PacketType.PID_SET:
            from serialpid.protocol import unpack_pid_set

            kp, ki, kd, sp = unpack_pid_set(last[4:-6])
            if kp > 1e6 and self.reject:
                return [(0.0, Frame(PacketType.ERROR, pack_error(0x03, "Kp out of range")))]
            result = (AckResult.REJECTED if (kp > 1e6 and self.reject)
                      else AckResult.ACCEPTED)
            return [(0.0, Frame(PacketType.PID_ACK, pack_pid_ack(kp, ki, kd, sp, result)))]
        return []


def test_step3_pass_only_when_valid_echo_and_invalid_rejected():
    result = bringup_check.step3_pid_set(_StubLink(reject=True))

    assert result["status"] == "PASS"
    assert "被拒" in result["measured"]


def test_step3_fails_when_out_of_range_is_accepted():
    result = bringup_check.step3_pid_set(_StubLink(reject=False))

    assert result["status"] == "FAIL"
    assert "未被拒" in result["diff"]
