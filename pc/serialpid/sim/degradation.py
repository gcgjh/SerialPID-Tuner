"""链路劣化层：把"正常"的仿真通道包一层，按需注入五类现场故障。

卡27 的出发点：仿真器能跑不等于能验。现在它只跑理想工况——不丢帧、不坏帧、
不断线，而这三件事恰恰是现场最常见的。本层放在**引擎与传输之间**：

    SimEngine ── emit/write ──> DegradedTransport ──> SimTransport ──> 上位机

* 默认 ``LinkConfig(enabled=False)`` 时是零开销直通（不加锁、不起线程、不取随机数）；
* 打开后支持：按概率丢帧（上下行分别设）、按概率伪造坏 CRC 帧、固定延迟 + 随机抖动、
  运行 N 秒后强制断线、周期性插入随机字节噪声；
* 所有参数运行期可改（:meth:`DegradedTransport.apply`），开关一关立即回到直通；
* 随机数用固定种子，同一种子连续跑十次，丢/坏/断的计数与产出帧序列完全一致。

口径说明：延迟与抖动只作用于**下行**（引擎→上位机），因为上行由引擎每拍主动取空，
再加延迟只是把命令晚一拍生效，观察不到"链路延迟"。丢帧/坏 CRC 上下行都支持。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..transport import Transport, TransportError
from .frames import corrupt_crc, is_frame, make_noise, split_frames
from .transport import SimTransport

#: 泵线程（延迟/噪声/断线检查）的调度粒度
PUMP_INTERVAL_S = 0.005


@dataclass
class LinkConfig:
    """劣化参数。全 0 + ``enabled=False`` 表示直通。"""

    enabled: bool = False
    drop_up: float = 0.0            # 上行（上位机→引擎）每帧丢弃概率
    drop_down: float = 0.0          # 下行（引擎→上位机）每帧丢弃概率
    bad_crc_up: float = 0.0         # 上行伪造坏 CRC 概率
    bad_crc_down: float = 0.0       # 下行伪造坏 CRC 概率
    delay_ms: float = 0.0           # 下行固定延迟（毫秒）
    jitter_ms: float = 0.0          # 下行延迟抖动（±，均匀分布）
    disconnect_after_s: float = 0.0  # 打开后多少秒强制断线；0 = 不断
    noise_period_s: float = 0.0     # 每隔多少秒插入一段随机字节；0 = 不插
    noise_burst_bytes: int = 8      # 每次噪声长度（字节）
    seed: int = 20261107

    def sanitized(self) -> "LinkConfig":
        """夹回合法范围（概率 0~1、时间非负），返回新对象不改原值。"""
        def prob(value: float) -> float:
            return min(1.0, max(0.0, float(value)))

        def nonneg(value: float) -> float:
            return max(0.0, float(value))

        return LinkConfig(
            enabled=bool(self.enabled),
            drop_up=prob(self.drop_up),
            drop_down=prob(self.drop_down),
            bad_crc_up=prob(self.bad_crc_up),
            bad_crc_down=prob(self.bad_crc_down),
            delay_ms=nonneg(self.delay_ms),
            jitter_ms=nonneg(self.jitter_ms),
            disconnect_after_s=nonneg(self.disconnect_after_s),
            noise_period_s=nonneg(self.noise_period_s),
            noise_burst_bytes=max(0, int(self.noise_burst_bytes)),
            seed=int(self.seed),
        )

    def active(self) -> bool:
        """是否真的会改变字节流（用于决定要不要起泵线程）。"""
        return self.enabled and (
            self.drop_up > 0 or self.drop_down > 0
            or self.bad_crc_up > 0 or self.bad_crc_down > 0
            or self.delay_ms > 0 or self.jitter_ms > 0
            or self.disconnect_after_s > 0 or self.noise_period_s > 0
        )

    def describe(self) -> str:
        """一行中文说明，供界面/日志展示。"""
        if not self.enabled:
            return "链路劣化：关闭（直通）"
        parts = []
        if self.drop_up or self.drop_down:
            parts.append(f"丢帧 上行{self.drop_up:.0%}/下行{self.drop_down:.0%}")
        if self.bad_crc_up or self.bad_crc_down:
            parts.append(f"坏CRC 上行{self.bad_crc_up:.0%}/下行{self.bad_crc_down:.0%}")
        if self.delay_ms or self.jitter_ms:
            parts.append(f"延迟 {self.delay_ms:.0f}±{self.jitter_ms:.0f} ms")
        if self.disconnect_after_s:
            parts.append(f"{self.disconnect_after_s:g}s 后断线")
        if self.noise_period_s and self.noise_burst_bytes:
            parts.append(f"每 {self.noise_period_s:g}s 插 {self.noise_burst_bytes}B 噪声")
        return "链路劣化：" + ("、".join(parts) if parts else "无有效项")


@dataclass
class FaultStats:
    """劣化层计数（界面观测与测试断言用）。"""

    frames_up: int = 0
    frames_down: int = 0
    dropped_up: int = 0
    dropped_down: int = 0
    bad_crc_up: int = 0
    bad_crc_down: int = 0
    delayed: int = 0
    noise_bytes: int = 0
    forced_close: int = 0
    passthrough_chunks: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "frames_up": self.frames_up,
            "frames_down": self.frames_down,
            "dropped_up": self.dropped_up,
            "dropped_down": self.dropped_down,
            "bad_crc_up": self.bad_crc_up,
            "bad_crc_down": self.bad_crc_down,
            "delayed": self.delayed,
            "noise_bytes": self.noise_bytes,
            "forced_close": self.forced_close,
            "passthrough_chunks": self.passthrough_chunks,
        }


class DegradedTransport(Transport):
    """包在真传输外面的劣化层（对上仍是一个 Transport）。

    参数:
        inner: 被包装的通道（``SimTransport``）。
        config: 劣化参数；``None`` 视为关闭。
        clock: 时间源，测试可注入假时钟。
    """

    def __init__(
        self,
        inner: Optional[SimTransport] = None,
        config: Optional[LinkConfig] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner if inner is not None else SimTransport()
        self._config = (config or LinkConfig()).sanitized()
        self._clock = clock
        self._lock = threading.RLock()
        self._rng = random.Random(self._config.seed)
        self._pending: List[Tuple[float, bytes]] = []
        self._opened_at: Optional[float] = None
        self._forced = False
        self._noise_next: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.counters = FaultStats()

    # ---- 配置 ------------------------------------------------------------ #

    @property
    def config(self) -> LinkConfig:
        return self._config

    def apply(self, config: LinkConfig) -> LinkConfig:
        """运行期替换整份参数；开关变化即时生效。"""
        with self._lock:
            self._config = config.sanitized()
            self._rng = random.Random(self._config.seed)
            self._pending.clear()
            self._reset_schedule()
            if self._forced:
                # 已断开的链路不会因为关掉劣化就复活，需重新 open
                return self._config
            self._sync_pump()
            return self._config

    # ---- Transport 接口 -------------------------------------------------- #

    def open(self, target: str = "sim", baud: int = 115200) -> None:
        with self._lock:
            self.inner.open(target, baud)
            self._forced = False
            self._opened_at = self._clock()
            self._pending.clear()
            self._rng = random.Random(self._config.seed)
            self.counters = FaultStats()
            self._reset_schedule()
            self._sync_pump()

    def read(self, n: int = 1024) -> bytes:
        self.pump()
        if self._forced:
            return b""
        return self.inner.read(n)

    def write(self, data: bytes) -> None:
        self._check_disconnect()
        if self._forced:
            raise TransportError("链路已断开（故障注入：强制断线）")
        payload = bytes(data)
        if not self._config.active():
            self.counters.passthrough_chunks += 1
            self.inner.write(payload)
            return
        for chunk in split_frames(payload):
            if self._forced:
                raise TransportError("链路已断开（故障注入：强制断线）")
            with self._lock:
                if is_frame(chunk):
                    self.counters.frames_up += 1
                    if self._rng.random() < self._config.drop_up:
                        self.counters.dropped_up += 1
                        continue
                    if self._rng.random() < self._config.bad_crc_up:
                        chunk = corrupt_crc(chunk)
                        self.counters.bad_crc_up += 1
            self.inner.write(chunk)

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._thread = None
            self._pending.clear()
        if thread is not None:
            thread.join(timeout=1.0)
        self.inner.close()

    def is_open(self) -> bool:
        return (not self._forced) and self.inner.is_open()

    # ---- 引擎侧（与 SimTransport 同接口） -------------------------------- #

    def emit(self, data: bytes) -> bool:
        """引擎 -> 上位机。按配置丢/坏/延迟后投进内层缓冲。"""
        self._check_disconnect()
        if self._forced:
            return False
        payload = bytes(data)
        if not self._config.active():
            self.counters.passthrough_chunks += 1
            return self.inner.emit(payload)

        delivered = False
        for chunk in split_frames(payload):
            if self._forced:
                return False
            with self._lock:
                if is_frame(chunk):
                    self.counters.frames_down += 1
                    if self._rng.random() < self._config.drop_down:
                        self.counters.dropped_down += 1
                        continue
                    if self._rng.random() < self._config.bad_crc_down:
                        chunk = corrupt_crc(chunk)
                        self.counters.bad_crc_down += 1
                due = self._due_time(chunk)
            if due <= self._clock():
                delivered = self.inner.emit(chunk) or delivered
            else:
                with self._lock:
                    self._pending.append((due, chunk))
                    self.counters.delayed += 1
        return delivered or bool(self._pending)

    def take_inbound(self, timeout: float = 0.0) -> bytes:
        """引擎侧取上行字节（劣化已在 ``write`` 里做完，这里直通）。"""
        take = getattr(self.inner, "take_inbound", None)
        if take is None:
            return b""
        return take(timeout)

    @property
    def pending_out_bytes(self) -> int:
        return getattr(self.inner, "pending_out_bytes", 0) + sum(
            len(chunk) for _, chunk in self._pending
        )

    def statistics(self) -> Dict[str, object]:
        base = {}
        stats = getattr(self.inner, "statistics", None)
        if callable(stats):
            base = dict(stats())
        base.update(self.counters.as_dict())
        base["link"] = self._config.describe()
        return base

    # ---- 泵：延迟出队 / 周期噪声 / 断线检查 ------------------------------ #

    def pump(self, now: Optional[float] = None) -> int:
        """把到点的下行帧投递出去，必要时插噪声；返回本次投递的帧数。"""
        time_now = self._clock() if now is None else float(now)
        self._check_disconnect()
        delivered = 0
        with self._lock:
            due: List[bytes] = []
            keep: List[Tuple[float, bytes]] = []
            for stamp, chunk in self._pending:
                if stamp <= time_now:
                    due.append(chunk)
                else:
                    keep.append((stamp, chunk))
            self._pending = keep
        for chunk in due:
            if self._forced:
                break
            if self.inner.emit(chunk):
                delivered += 1
        self._maybe_noise(time_now)
        return delivered

    def _maybe_noise(self, now: float) -> None:
        if self._forced or not self._config.enabled:
            return
        if self._config.noise_period_s <= 0 or self._config.noise_burst_bytes <= 0:
            return
        if self._noise_next is None:
            with self._lock:
                self._noise_next = now + self._config.noise_period_s
            return
        if now < self._noise_next:
            return
        with self._lock:
            self._noise_next = now + self._config.noise_period_s
            noise = make_noise(self._rng, self._config.noise_burst_bytes)
            self.counters.noise_bytes += len(noise)
        self.inner.emit(noise)

    # ---- 内部 ------------------------------------------------------------ #

    def _due_time(self, chunk: bytes) -> float:
        now = self._clock()
        if not is_frame(chunk):
            return now
        delay = self._config.delay_ms
        jitter = self._config.jitter_ms
        if delay <= 0 and jitter <= 0:
            return now
        offset = delay
        if jitter > 0:
            offset += self._rng.uniform(-jitter, jitter)
        return now + max(0.0, offset) / 1000.0

    def _reset_schedule(self) -> None:
        now = self._clock()
        self._opened_at = now
        self._noise_next = now + self._config.noise_period_s if self._config.noise_period_s > 0 else None

    def _check_disconnect(self) -> None:
        if self._forced or self._opened_at is None:
            return
        limit = self._config.disconnect_after_s
        if not self._config.enabled or limit <= 0:
            return
        if self._clock() - self._opened_at < limit:
            return
        with self._lock:
            if self._forced:
                return
            self._forced = True
            self.counters.forced_close += 1
            self._pending.clear()
        try:
            self.inner.close()
        except Exception:  # noqa: BLE001 - 断线注入不该把调用方带崩
            pass

    def _needs_pump(self) -> bool:
        cfg = self._config
        return cfg.active() and (
            cfg.delay_ms > 0 or cfg.jitter_ms > 0
            or cfg.noise_period_s > 0 or cfg.disconnect_after_s > 0
        )

    def _sync_pump(self) -> None:
        """需要泵就起线程，不需要就让它自行退出（在 ``_lock`` 内调用，不 join）。"""
        if self._needs_pump() and not self._forced:
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._pump_loop, args=(self._stop,), name="SimLinkPump", daemon=True
                )
                self._thread.start()
        else:
            self._stop.set()
            self._thread = None

    def _pump_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.pump()
            except Exception:  # noqa: BLE001 - 泵线程单次异常不能退出
                pass
            time.sleep(PUMP_INTERVAL_S)


__all__ = ["LinkConfig", "FaultStats", "DegradedTransport"]
