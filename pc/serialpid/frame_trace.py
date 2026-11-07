# -*- coding: utf-8 -*-
"""帧级日志与回放（card28）：纯逻辑，无 Qt、无 IO。

用途是把"现场到底发生了一帧什么"留住：

* **环形缓冲**：通信线程把每一帧（方向 / 类型 / 载荷 / 相对时刻）塞进来，
  只保留最近 ``capacity`` 帧，内存有界；
* **导出**：文本给人看，JSONL 给机器读（一行一帧、可 diff、可 grep）；
* **回放**：读回 JSONL，按原始时序把 rx 帧重新喂给界面，曲线就能重现当时
  的样子。回放支持倍速与单步。

存储口径：
    * 只存帧本身与相对时间戳，**不存**端口号、绝对路径、设备序列号等可识别
      信息（导出的文件会给别人看）；
    * 载荷按十六进制原样保存，保证回放时逐字节一致；
    * 时间戳是相对会话起点的毫秒数（int），不是墙钟。

方向口径：``dir`` 永远是 ``rx`` / ``tx``（相对上位机：rx = 设备→上位机，
tx = 上位机→设备），另带一个 ``dir_label``（接收 / 发送）给人看。读回时额外
接受 ``up`` / ``down`` 等别名——``sim/recorder.py`` 录的帧流把 "上位机→引擎"
写作 ``up``，两套文件都读得进来，用户不必记哪个格式属于哪个工具。

线程模型：{append} 在通信线程调用，读取 / 导出在界面线程调用，共用一把锁。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from .protocol import Frame, PACKET_NAMES, describe_frame, parse_frame

DIR_RX = "rx"   # MCU -> PC
DIR_TX = "tx"   # PC -> MCU
_DIRECTIONS = (DIR_RX, DIR_TX)

#: 方向别名：接受 ``sim/recorder.py``（up/down）与常见写法的文件，一律折回 rx/tx
_DIR_ALIASES = {
    "rx": DIR_RX, "in": DIR_RX, "recv": DIR_RX, "down": DIR_RX, "下行": DIR_RX,
    "tx": DIR_TX, "out": DIR_TX, "send": DIR_TX, "up": DIR_TX, "上行": DIR_TX,
}

#: 导出时给人看的方向标签（列表列 / JSONL 的 dir_label 共用）
DIR_LABELS = {DIR_RX: "接收", DIR_TX: "发送"}

#: 环形缓冲默认容量（帧）。20 Hz 遥测下 2000 帧约 100 秒，
#: 内存量级 = 2000 × (20 字节载荷 + 固定开销) ≈ 0.2 MB，界面线程遍历无压力。
DEFAULT_CAPACITY = 2000


@dataclass(frozen=True)
class TraceEntry:
    """一帧的日志记录。"""

    t_ms: int
    direction: str
    pkt_type: int
    payload: bytes

    @property
    def name(self) -> str:
        return PACKET_NAMES.get(self.pkt_type, f"UNKNOWN(0x{self.pkt_type:02X})")

    def describe(self) -> str:
        return describe_frame(Frame(self.pkt_type, self.payload))


class FrameTrace:
    """有界帧日志：追加、快照、导出、读回。"""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._capacity = max(int(capacity), 1)
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._entries: List[TraceEntry] = []
        self._origin: Optional[float] = None
        self._dropped = 0

    # ---- 写入 -------------------------------------------------------------

    def reset(self) -> None:
        """清空（换通道 / 新会话时调用，相对时间从 0 重新起算）。"""
        with self._lock:
            self._entries.clear()
            self._origin = None
            self._dropped = 0

    def append(
        self,
        direction: str,
        pkt_type: int,
        payload: bytes = b"",
        t_ms: Optional[int] = None,
    ) -> Optional[TraceEntry]:
        """记录一帧；``t_ms`` 省略时按内部时钟算相对时刻。

        :raises ValueError: 方向不是 ``rx`` / ``tx``。
        """
        if direction not in _DIRECTIONS:
            raise ValueError(f"方向必须是 rx 或 tx，收到 {direction!r}")
        now = self._clock()
        with self._lock:
            if self._origin is None:
                self._origin = now
            stamp = (
                int(round((now - self._origin) * 1000.0))
                if t_ms is None
                else max(int(t_ms), 0)
            )
            entry = TraceEntry(
                t_ms=stamp,
                direction=direction,
                pkt_type=int(pkt_type),
                payload=bytes(payload),
            )
            self._entries.append(entry)
            overflow = len(self._entries) - self._capacity
            if overflow > 0:
                # 超出容量时丢最旧的：近期现场比远古历史更值钱
                del self._entries[:overflow]
                self._dropped += overflow
            return entry

    # ---- 读取 -------------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def dropped(self) -> int:
        """因超出容量被挤掉的帧数（导出时提示"更早的已滚掉"）。"""
        with self._lock:
            return self._dropped

    def snapshot(self) -> List[TraceEntry]:
        with self._lock:
            return list(self._entries)

    # ---- 导出 -------------------------------------------------------------

    def to_jsonl(self) -> str:
        """一行一帧的 JSONL；字段固定，便于 diff 与二次解析。

        每行 = 相对毫秒 ``t`` + 方向 ``dir``（rx/tx）+ 中文标签 ``dir_label``
        + 类型号 ``type`` + 类型名 ``name`` + 长度 ``len`` + 载荷 ``payload``
        （十六进制）。不含端口、路径、序列号。
        """
        lines = []
        for entry in self.snapshot():
            lines.append(json.dumps({
                "t": entry.t_ms,
                "dir": entry.direction,
                "dir_label": DIR_LABELS.get(entry.direction, entry.direction),
                "type": entry.pkt_type,
                "name": entry.name,
                "len": len(entry.payload),
                "payload": entry.payload.hex(),
            }, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + ("\n" if lines else "")

    def memory_estimate_bytes(self) -> int:
        """环形缓冲当前占用的粗略字节数（界面显示"上限是几万条"时的依据）。

        每个 ``TraceEntry`` 是 frozen dataclass：对象头 + 4 个字段 ≈ 120 B，
        再加载荷本身。20 Hz / 24 B 载荷下 2000 帧约 0.3 MB，2 万帧约 3 MB。
        """
        entries = self.snapshot()
        return sum(120 + len(e.payload) for e in entries)

    def to_text(self) -> str:
        """给人看的文本：相对时刻 + 方向 + 解析后的帧内容。"""
        entries = self.snapshot()
        head = ["# SerialPID-Tuner 帧日志", "# 列：相对时刻 方向 帧内容（解析失败时退回十六进制）"]
        if entries:
            head.insert(1, f"# 帧数 {len(entries)}，最早 t=+{entries[0].t_ms / 1000.0:.3f}s")
        else:
            head.insert(1, "# 帧数 0")
        body = [
            f"+{e.t_ms / 1000.0:8.3f}s {'←' if e.direction == DIR_RX else '→'} "
            f"{e.describe()}"
            for e in entries
        ]
        return "\n".join(head + body) + "\n"


def load_jsonl(text: str) -> List[TraceEntry]:
    """把 :meth:`FrameTrace.to_jsonl` 的产物读回成帧流。

    容错口径：空行跳过；字段缺失 / 类型非法 / 载荷十六进制不合法一律抛
    ``ValueError`` 并带行号——回放用的是真实现场数据，静默跳过会把"丢了
    一段"伪装成"当时就这样"。
    """
    entries: List[TraceEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"第 {number} 行不是合法 JSON：{exc}") from None
        if not isinstance(payload, dict):
            raise ValueError(f"第 {number} 行不是对象")
        direction = payload.get("dir")
        if direction not in _DIRECTIONS:
            raise ValueError(f"第 {number} 行 dir 非法：{direction!r}")
        pkt_type = payload.get("type")
        if not isinstance(pkt_type, int) or isinstance(pkt_type, bool) or not 0 <= pkt_type <= 0xFF:
            raise ValueError(f"第 {number} 行 type 非法：{pkt_type!r}")
        stamp = payload.get("t")
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
            raise ValueError(f"第 {number} 行 t 非法：{stamp!r}")
        raw = payload.get("payload", "")
        if not isinstance(raw, str):
            raise ValueError(f"第 {number} 行 payload 不是字符串")
        try:
            body = bytes.fromhex(raw)
        except ValueError as exc:
            raise ValueError(f"第 {number} 行 payload 不是合法十六进制：{exc}") from None
        declared = payload.get("len")
        if isinstance(declared, int) and declared != len(body):
            raise ValueError(
                f"第 {number} 行 len={declared} 与载荷实际 {len(body)} 字节不符"
            )
        entries.append(TraceEntry(stamp, direction, pkt_type, body))
    return entries


class ReplayCursor:
    """按原始时序放出帧流；支持倍速、单步、进度。

    用法（界面用 QTimer 按 20 ms 左右拉一次）::

        cursor = ReplayCursor(entries, speed=2.0)
        due = cursor.advance(20.0)      # 这 20 ms 内到点的帧
        ...  # 没有到点的帧时返回空列表
        one = cursor.step()             # 单步：强制放出下一帧

    时间基准取**第一条帧**的相对时刻，导出文件从中间截取也能正常回放。
    """

    def __init__(self, entries: Iterable[TraceEntry], speed: float = 1.0) -> None:
        self._entries: List[TraceEntry] = list(entries)
        self._base = self._entries[0].t_ms if self._entries else 0
        self._speed = 1.0
        self.set_speed(speed)
        self.reset()

    # ---- 参数与状态 -------------------------------------------------------

    def set_speed(self, speed: float) -> float:
        """倍速（0.25 ~ 20），返回实际生效值。"""
        value = float(speed)
        if value != value or value <= 0.0:  # NaN / 非正
            value = 1.0
        self._speed = max(0.25, min(20.0, value))
        return self._speed

    @property
    def speed(self) -> float:
        return self._speed

    def reset(self) -> None:
        self._position = 0
        self._elapsed_ms = 0.0

    @property
    def total(self) -> int:
        return len(self._entries)

    @property
    def position(self) -> int:
        return self._position

    @property
    def finished(self) -> bool:
        return self._position >= len(self._entries)

    @property
    def duration_ms(self) -> float:
        if not self._entries:
            return 0.0
        return float(self._entries[-1].t_ms - self._base)

    @property
    def elapsed_ms(self) -> float:
        return self._elapsed_ms

    def progress(self) -> float:
        return self._position / len(self._entries) if self._entries else 1.0

    # ---- 推进 -------------------------------------------------------------

    def advance(self, wall_dt_ms: float) -> List[TraceEntry]:
        """按墙上流逝的时间推进，返回本次到点（含之前积压）的全部帧。"""
        dt = max(float(wall_dt_ms), 0.0)
        self._elapsed_ms += dt * self._speed
        due: List[TraceEntry] = []
        total = len(self._entries)
        while self._position < total:
            entry = self._entries[self._position]
            if entry.t_ms - self._base > self._elapsed_ms:
                break
            due.append(entry)
            self._position += 1
        return due

    def step(self) -> List[TraceEntry]:
        """单步：强制放出下一帧（不管时间到没到）。"""
        if self.finished:
            return []
        entry = self._entries[self._position]
        self._position += 1
        # 让内部时钟追上这一帧，避免单步之后连续播放又倒回去等
        self._elapsed_ms = max(self._elapsed_ms, float(entry.t_ms - self._base))
        return [entry]


def rx_only(entries: Iterable[TraceEntry]) -> List[TraceEntry]:
    """回放用：只取 rx 帧（tx 是上位机自己发的，回放只会重复下发命令）。"""
    return [e for e in entries if e.direction == DIR_RX]


def summarize(entries: Iterable[TraceEntry]) -> Dict[str, int]:
    """按帧类型统计条数（导出对话框里给一行概览）。"""
    counts: Dict[str, int] = {}
    for entry in entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    return counts
