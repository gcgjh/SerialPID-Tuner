"""帧流录制与回放（现场证据留存 / 离线复现）。

两件事：

1. **录制**：把一次会话的双向帧流按相对时间存成 JSONL，一行一帧；
2. **回放**：按原时序把下行帧重新投给上位机，支持倍速与单步。

JSONL 只存帧字节、类型、方向与相对时间戳——不存绝对路径、设备序列号，
也不存原始文本日志，因此可以放心贴进 issue。逐行 JSON 还能直接 ``git diff``。

回放有两条落地路径：

* :class:`ReplayTransport` —— 进程内通道，``CommCore.open("replay", path, transport=...)``
  即可把当时的帧序列灌进界面；
* ``tools/replay_frames.py`` —— TCP 服务，界面按老办法连 ``127.0.0.1:port`` 看回放，
  不需要改界面通道列表。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..transport import Transport, TransportError
from .frames import is_frame, split_frames
from .transport import SimTransport

#: 单条记录的字段：相对毫秒、方向、类型、长度、帧字节（hex）
RECORD_VERSION = 1


@dataclass
class FrameRecord:
    """一条录制记录。"""

    t_ms: int
    direction: str  # "up"（上位机→引擎）或 "down"（引擎→上位机）
    raw: bytes

    @property
    def type(self) -> Optional[int]:
        return self.raw[3] if is_frame(self.raw) else None

    def to_json(self) -> Dict[str, object]:
        return {
            "t_ms": self.t_ms,
            "dir": self.direction,
            "type": self.type,
            "len": len(self.raw),
            "raw": self.raw.hex(),
        }

    @classmethod
    def from_json(cls, payload: Dict[str, object]) -> "FrameRecord":
        direction = str(payload.get("dir", "down"))
        if direction not in ("up", "down"):
            raise ValueError(f"方向非法：{direction!r}")
        raw = bytes.fromhex(str(payload["raw"]))
        if len(raw) > 260:
            raise ValueError("帧长度超限")
        return cls(t_ms=int(payload["t_ms"]), direction=direction, raw=raw)


class FrameRecorder:
    """把帧写成 JSONL；线程安全，写一条刷一条（断电也不丢已记录的帧）。"""

    def __init__(self, path: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._handle = open(path, "w", encoding="utf-8")
        self.records = 0
        self.skipped = 0

    def record(self, direction: str, raw: bytes) -> None:
        """记一帧；非帧字节（噪声/半帧）跳过并计数。"""
        if not is_frame(raw):
            with self._lock:
                self.skipped += 1
            return
        stamp = int(round((self._clock() - self._t0) * 1000))
        line = json.dumps(FrameRecord(stamp, direction, bytes(raw)).to_json(), ensure_ascii=False)
        with self._lock:
            if self._handle is None:
                return
            self._handle.write(line + "\n")
            self._handle.flush()
            self.records += 1

    def record_chunk(self, direction: str, data: bytes) -> None:
        for chunk in split_frames(data):
            self.record(direction, chunk)

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()

    def __enter__(self) -> "FrameRecorder":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class RecordingTransport(Transport):
    """录制层：包在真通道外面，原样透传的同时把双向帧写进 JSONL。"""

    def __init__(self, inner: Optional[SimTransport] = None, recorder: Optional[FrameRecorder] = None) -> None:
        if recorder is None:
            raise ValueError("RecordingTransport 需要 FrameRecorder")
        self.inner = inner if inner is not None else SimTransport()
        self.recorder = recorder
        self.chunks_up = 0
        self.chunks_down = 0

    def open(self, target: str = "sim", baud: int = 115200) -> None:
        self.inner.open(target, baud)

    def read(self, n: int = 1024) -> bytes:
        return self.inner.read(n)

    def write(self, data: bytes) -> None:
        self.chunks_up += 1
        self.recorder.record_chunk("up", data)
        self.inner.write(data)

    def close(self) -> None:
        self.inner.close()

    def is_open(self) -> bool:
        return self.inner.is_open()

    def emit(self, data: bytes) -> bool:
        self.chunks_down += 1
        self.recorder.record_chunk("down", data)
        return self.inner.emit(data)

    def take_inbound(self, timeout: float = 0.0) -> bytes:
        take = getattr(self.inner, "take_inbound", None)
        return take(timeout) if take is not None else b""

    @property
    def pending_out_bytes(self) -> int:
        return getattr(self.inner, "pending_out_bytes", 0)


def load_records(path: str) -> Tuple[List[FrameRecord], List[str]]:
    """读 JSONL；坏行跳过并记警告，不因为一行坏掉整份录音。"""
    records: List[FrameRecord] = []
    warnings: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                record = FrameRecord.from_json(payload)
            except (ValueError, KeyError, TypeError) as exc:
                warnings.append(f"第 {number} 行无法解析：{exc}")
                continue
            records.append(record)
    return records, warnings


class FrameReplay:
    """按原时序排队回放（纯逻辑，不依赖线程），供 Transport 与 TCP 工具共用。

    时间口径：以录制里第一条帧为 0，``speed=2`` 表示两倍速（时间轴缩短一半）。
    单步（:meth:`next_frame`）会把后续帧的排期整体前移，保证"点一下走一帧"之后
    继续播放时不会突然追赶。
    """

    def __init__(
        self,
        path: str,
        speed: float = 1.0,
        loop: bool = False,
        direction: str = "down",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        records, self.warnings = load_records(path)
        self.path = path
        self._clock = clock
        self._frames = [r for r in records if r.direction == direction]
        if not self._frames:
            raise ValueError(f"录制里没有 {direction} 方向的帧：{path}")
        self._t0_ms = self._frames[0].t_ms
        self._speed = max(0.01, float(speed))
        self._loop = bool(loop)
        self._index = 0
        self._started_at = clock()
        self._paused = False
        self._pause_at: Optional[float] = None
        self.yielded = 0

    # ---- 状态 ------------------------------------------------------------ #

    @property
    def total(self) -> int:
        return len(self._frames)

    @property
    def position(self) -> int:
        return self._index

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def paused(self) -> bool:
        return self._paused

    def set_speed(self, speed: float) -> float:
        new_speed = max(0.01, float(speed))
        # 把"已经走过的虚拟时间"按新倍速折算回起点，避免当前帧突然跳期
        virtual_elapsed = (self._clock() - self._started_at) * self._speed
        self._started_at = self._clock() - virtual_elapsed / new_speed
        self._speed = new_speed
        return self._speed

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._pause_at = self._clock()

    def resume(self) -> None:
        if self._paused:
            assert self._pause_at is not None
            self._started_at += self._clock() - self._pause_at
            self._pause_at = None
            self._paused = False

    def seek(self, index: int = 0) -> None:
        self._index = max(0, min(int(index), self.total))
        anchor = 0 if self._index >= self.total else (
            self._frames[self._index].t_ms - self._t0_ms
        )
        self._started_at = self._clock() - anchor / 1000.0 / self._speed
        self.yielded = 0

    def remaining_s(self) -> float:
        """按当前倍速还剩多少秒（用于界面进度提示）。"""
        if self._index >= self.total:
            return 0.0
        last = self._frames[-1].t_ms - self._t0_ms
        current = self._frames[self._index].t_ms - self._t0_ms
        return max(0.0, (last - current) / 1000.0 / self._speed)

    # ---- 取帧 ------------------------------------------------------------ #

    def due_frames(self, now: Optional[float] = None) -> List[bytes]:
        """取出所有已到点的帧（不暂停时）。"""
        if self._paused:
            return []
        time_now = self._clock() if now is None else float(now)
        out: List[bytes] = []
        while self._index < self.total:
            frame = self._frames[self._index]
            due = self._started_at + (frame.t_ms - self._t0_ms) / 1000.0 / self._speed
            if due > time_now:
                break
            out.append(frame.raw)
            self._index += 1
            self.yielded += 1
        self._maybe_loop(time_now)
        return out

    def next_frame(self) -> Optional[bytes]:
        """单步：无视排期立刻返回下一帧，并把后续排期前移（不追赶）。"""
        if self._index >= self.total:
            if not self._loop:
                return None
            self.seek(0)
        frame = self._frames[self._index]
        self._index += 1
        self.yielded += 1
        now = self._clock()
        if self._index < self.total:
            following = self._frames[self._index]
            gap = (following.t_ms - frame.t_ms) / 1000.0 / self._speed
            self._started_at = now + gap - (following.t_ms - self._t0_ms) / 1000.0 / self._speed
        return frame.raw

    def _maybe_loop(self, now: float) -> None:
        if self._loop and self._index >= self.total:
            self._started_at = now
            self._index = 0


class ReplayTransport(Transport):
    """进程内回放通道：读侧交给 CommCore，写侧忽略（回放只是播，不是收命令）。"""

    def __init__(
        self,
        path: str,
        speed: float = 1.0,
        loop: bool = False,
        direction: str = "down",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.replay = FrameReplay(path, speed=speed, loop=loop, direction=direction, clock=clock)
        self.inner = SimTransport()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.closed = False

    # ---- Transport ------------------------------------------------------- #

    def open(self, target: str = "replay", baud: int = 115200) -> None:
        self.inner.open("replay")
        self.replay.seek(0)
        self.closed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._pump_loop, args=(self._stop,), name="FrameReplay", daemon=True
        )
        self._thread.start()

    def read(self, n: int = 1024) -> bytes:
        return self.inner.read(n)

    def write(self, data: bytes) -> None:
        # 回放期间上位机仍可能发心跳/命令：不投给任何人，也不报错——
        # 报错会让通信线程误判"链路坏了"，与回放目的相反。
        return None

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        self.inner.close()
        self.closed = True

    def is_open(self) -> bool:
        return self.inner.is_open() and not self.closed

    # ---- 播放控制 -------------------------------------------------------- #

    def set_speed(self, speed: float) -> float:
        return self.replay.set_speed(speed)

    def pause(self) -> None:
        self.replay.pause()

    def resume(self) -> None:
        self.replay.resume()

    def step(self, count: int = 1) -> int:
        """单步推进 ``count`` 帧，立即投递；返回实际投递帧数。"""
        self.replay.pause()
        delivered = 0
        for _ in range(max(0, int(count))):
            raw = self.replay.next_frame()
            if raw is None:
                break
            if self.inner.emit(raw):
                delivered += 1
        return delivered

    def statistics(self) -> Dict[str, object]:
        return {
            "total": self.replay.total,
            "position": self.replay.position,
            "speed": self.replay.speed,
            "paused": self.replay.paused,
            "warnings": len(self.replay.warnings),
            **self.inner.statistics(),
        }

    def _pump_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            for raw in self.replay.due_frames():
                if not self.inner.emit(raw):
                    break
            time.sleep(0.005)


__all__ = [
    "FrameRecord",
    "FrameRecorder",
    "RecordingTransport",
    "FrameReplay",
    "ReplayTransport",
    "load_records",
]
