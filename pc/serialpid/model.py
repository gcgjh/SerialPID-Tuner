"""上位机数据模型与遥测缓冲。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional


@dataclass
class PidParams:
    kp: float
    ki: float
    kd: float
    sp: float


@dataclass
class TelemetrySample:
    t_ms: int
    sp: float
    pv: float
    out: float
    err: float


class TelemetryBuffer:
    """定长遥测环形缓冲；线程追加由通信线程完成，读取在 UI 线程。"""

    def __init__(self, max_len: int = 20000) -> None:
        self._buf: Deque[TelemetrySample] = deque(maxlen=max_len)

    def append(self, sample: TelemetrySample) -> None:
        self._buf.append(sample)

    def samples(self) -> List[TelemetrySample]:
        return list(self._buf)

    def last(self) -> Optional[TelemetrySample]:
        return self._buf[-1] if self._buf else None

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)
