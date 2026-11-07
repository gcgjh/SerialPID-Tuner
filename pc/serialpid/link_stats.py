# -*- coding: utf-8 -*-
"""链路质量统计（card28）：纯逻辑，无 Qt、无 IO，可在无显示环境单测。

答的是三个问题，都直接影响调参判断：

* **收到的数据完整吗**——收发帧数、坏帧数、按时间推断的丢帧数；
* **链路有多快**——窗口内的帧率与有效字节率；
* **对端反应有多快**——心跳往返时延的均值 / 最大 / 抖动。

口径约定（界面必须照这个口径说话）：

* 丢帧数是**推断值**：按设备时间戳（u32 毫秒）相邻间隔与控制周期估算，
  间隔超过 1.5 个周期才计入，展示时要标「约」；
* 时间戳负跳变分两种：回绕（同一会话继续，按模运算自然处理）与
  设备复位（跳到 0 重新计数），复位**不计丢帧**，否则每次烧录都会报
  "丢了六十多亿帧"；
* 坏帧由解包器统计，CRC 错 / 帧尾错 / 超长各算一帧，同一批字节里重复
  计到的部分由调用方按差值喂入，本模块只累加。

线程模型：通信线程写入、界面线程读快照，所有状态由一把锁保护。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

#: u32 毫秒计数器掩码；设备上电起算，约 49.7 天回绕一次
U32_MASK = 0xFFFFFFFF
U32_SPAN = 1 << 32
#: 相邻时间戳差值超过半圈，判为设备复位（时间戳从 0 重新计数）而不是继续推进
RESET_JUMP = 1 << 31

#: 丢帧判据：间隔超过 1.5 个控制周期才算丢帧，避免把抖动当成丢包
LOST_GAP_FACTOR = 1.5
#: 估周期时只采纳不超过 1.2 个周期的样本，免得把"丢了三帧"的间隔当成新周期
PERIOD_MAX_FACTOR = 1.2
#: 周期估计的平滑系数（越小越不敏感）
PERIOD_BLEND = 0.2

QUALITY_OK = "ok"
QUALITY_WARN = "warn"
QUALITY_BAD = "bad"
QUALITY_IDLE = "idle"

#: 质量阈值：坏帧占比超 5% 或丢帧占比超 20% 判红
BAD_RATIO_BAD = 0.05
LOST_RATIO_BAD = 0.20
#: 丢帧占比超 5%、或时延抖动超 50 ms 判黄
LOST_RATIO_WARN = 0.05
JITTER_WARN_MS = 50.0
#: 帧率跌到名义值的这个比例以下判黄（名义值取窗口内见过的最高帧率）
FPS_DROP_RATIO = 0.7
#: RTT 均值/抖动的平滑系数
RTT_BLEND = 0.2


@dataclass(frozen=True)
class LinkSnapshot:
    """某一时刻的链路快照（不可变，可跨线程传出）。"""

    rx_frames: int = 0
    tx_frames: int = 0
    bad_frames: int = 0
    lost_frames: int = 0
    fps: float = 0.0
    bytes_per_s: float = 0.0
    rtt_last_ms: Optional[float] = None
    rtt_avg_ms: Optional[float] = None
    rtt_max_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    #: 连续多少次 PING 还没等到 PONG（0 表示心跳正常）
    heartbeat_misses: int = 0
    uptime_s: float = 0.0
    quality: str = QUALITY_IDLE
    quality_reason: str = "未连接"

    @property
    def bad_ratio(self) -> float:
        """坏帧占「收到 + 坏」的比例。"""
        total = self.rx_frames + self.bad_frames
        return (self.bad_frames / total) if total else 0.0

    @property
    def lost_ratio(self) -> float:
        """推断丢帧占「收到 + 丢」的比例。"""
        total = self.rx_frames + self.lost_frames
        return (self.lost_frames / total) if total else 0.0

    def summary(self) -> str:
        """状态栏那一行：够短，但三个关键数字都在。"""
        if self.quality == QUALITY_IDLE:
            return "未连接"
        parts: List[str] = []
        if self.fps > 0:
            parts.append(f"{self.fps:.1f} 帧/s")
        if self.rtt_avg_ms is not None:
            parts.append(f"{self.rtt_avg_ms:.0f} ms")
        parts.append(f"约丢 {self.lost_frames}")
        return " · ".join(parts)

    def details(self) -> str:
        """悬停详情：多行文本，把统计口径也写清楚。"""
        rtt = "—" if self.rtt_avg_ms is None else (
            f"均值 {self.rtt_avg_ms:.1f} ms / 最大 {self.rtt_max_ms:.1f} ms / "
            f"抖动 {0.0 if self.jitter_ms is None else self.jitter_ms:.1f} ms"
        )
        return "\n".join([
            f"链路质量：{_QUALITY_TEXT.get(self.quality, self.quality)}"
            f"（{self.quality_reason}）",
            f"帧：收 {self.rx_frames} / 发 {self.tx_frames} / "
            f"坏 {self.bad_frames}（{self.bad_ratio * 100:.1f}%）/ "
            f"约丢 {self.lost_frames}（{self.lost_ratio * 100:.1f}%）",
            f"速率：{self.fps:.1f} 帧/s，{self.bytes_per_s / 1024.0:.1f} KiB/s",
            f"往返时延：{rtt}"
            + (f"；连续 {self.heartbeat_misses} 次心跳无应答"
               if self.heartbeat_misses > 0 else ""),
            f"会话时长：{self.uptime_s:.0f} s",
            "",
            "口径：丢帧按设备时间戳间隔与控制周期推算，是推断出的约数不是精确值；"
            "设备复位（时间戳归零）不计丢帧。",
        ])


_QUALITY_TEXT = {
    QUALITY_OK: "良好",
    QUALITY_WARN: "偏差",
    QUALITY_BAD: "差",
    QUALITY_IDLE: "未连接",
}


class LinkStats:
    """链路统计累加器（线程安全）。

    通信线程调 ``note_*``，界面线程调 :meth:`snapshot`，两者共用一把锁。
    """

    def __init__(
        self,
        window_s: float = 3.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._window_s = max(float(window_s), 0.5)
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()
        self._window: Deque[Tuple[float, int]] = deque()
        self.reset()

    # ---- 写入（通信线程） -------------------------------------------------

    def reset(self, now: Optional[float] = None) -> None:
        """新会话开始时清零（断开重连后重新计）。"""
        moment = self._clock() if now is None else float(now)
        with self._lock:
            self._started = moment
            self._rx = 0
            self._tx = 0
            self._bad = 0
            self._lost = 0
            self._window.clear()
            self._rtt_last: Optional[float] = None
            self._rtt_avg: Optional[float] = None
            self._rtt_max: Optional[float] = None
            self._jitter: Optional[float] = None
            self._heartbeat_misses = 0
            self._last_t_ms: Optional[int] = None
            self._period_ms: Optional[float] = None
            self._peak_fps = 0.0

    def note_rx(self, nbytes: int = 0) -> None:
        """收到一帧（已通过 CRC 与帧尾校验）。"""
        with self._lock:
            self._rx += 1
            self._window.append((self._clock(), max(int(nbytes), 0)))
            self._prune()

    def note_tx(self, nbytes: int = 0) -> None:
        """发出（入队）一帧。"""
        with self._lock:
            self._tx += 1

    def note_bad(self, count: int = 1) -> None:
        """坏帧：CRC 错 / 帧尾错 / 超长，调用方按解包器统计的差值喂入。"""
        if count <= 0:
            return
        with self._lock:
            self._bad += int(count)

    def note_ping(self) -> int:
        """发了一次 PING，返回当前"连续多少次还没等到 PONG"。"""
        with self._lock:
            self._heartbeat_misses += 1
            return self._heartbeat_misses

    def note_pong(self) -> None:
        """收到一次 PONG（即使没有在途 PING，也说明对端活着）。"""
        with self._lock:
            self._heartbeat_misses = 0

    def note_heartbeat_gap(self, misses: int) -> None:
        """判死时把欠账记进快照，断线详情里能看到"几次没应答"。"""
        with self._lock:
            self._heartbeat_misses = max(int(misses), self._heartbeat_misses)

    def note_rtt(self, rtt_ms: float) -> None:
        """一次心跳往返时延（毫秒）。抖动取相对上一次均值的偏差。

        能算出 RTT 就说明这一轮 PING/PONG 走通了，欠账一并清零。
        """
        value = float(rtt_ms)
        if value < 0.0:
            return
        with self._lock:
            self._heartbeat_misses = 0
            self._rtt_last = value
            if self._rtt_avg is None:
                self._rtt_avg = value
                self._jitter = 0.0
            else:
                deviation = abs(value - self._rtt_avg)
                self._jitter = (
                    deviation if self._jitter is None
                    else (1.0 - RTT_BLEND) * self._jitter + RTT_BLEND * deviation
                )
                self._rtt_avg = (1.0 - RTT_BLEND) * self._rtt_avg + RTT_BLEND * value
            self._rtt_max = value if self._rtt_max is None else max(self._rtt_max, value)

    def note_telemetry(self, t_ms: int) -> int:
        """喂一帧遥测的设备时间戳，返回本次**推断**出的丢帧数。"""
        raw = int(t_ms) & U32_MASK
        with self._lock:
            if self._last_t_ms is None:
                self._last_t_ms = raw
                return 0
            delta = (raw - self._last_t_ms) & U32_MASK
            if delta >= RESET_JUMP:
                # 设备复位 / 重新上电：时间戳从 0 重新计数，是会话重启不是丢帧。
                # 周期估计保留（固件配置通常没变），下一帧起按新基准继续推。
                self._last_t_ms = raw
                return 0
            self._last_t_ms = raw

            if delta == 0:
                return 0
            if self._period_ms is None:
                self._period_ms = float(delta)
                return 0

            period = self._period_ms
            if delta <= period * PERIOD_MAX_FACTOR:
                self._period_ms = (1.0 - PERIOD_BLEND) * period + PERIOD_BLEND * float(delta)
                return 0
            if delta <= period * LOST_GAP_FACTOR:
                return 0

            missing = int(round(delta / period)) - 1
            if missing <= 0:
                return 0
            self._lost += missing
            return missing

    # ---- 读取（界面线程） -------------------------------------------------

    def snapshot(self, connected: bool = True) -> LinkSnapshot:
        """生成快照并给出绿 / 黄 / 红判定。"""
        with self._lock:
            now = self._clock()
            self._prune(now)
            entries = len(self._window)
            uptime = max(0.0, now - self._started)
            span = max(min(self._window_s, uptime), 0.2)
            fps = entries / span if entries else 0.0
            byte_rate = sum(n for _, n in self._window) / span if entries else 0.0
            self._peak_fps = max(self._peak_fps, fps)

            snap = LinkSnapshot(
                rx_frames=self._rx,
                tx_frames=self._tx,
                bad_frames=self._bad,
                lost_frames=self._lost,
                fps=fps,
                bytes_per_s=byte_rate,
                rtt_last_ms=self._rtt_last,
                rtt_avg_ms=self._rtt_avg,
                rtt_max_ms=self._rtt_max,
                jitter_ms=self._jitter,
                heartbeat_misses=self._heartbeat_misses,
                uptime_s=uptime,
            )
            if not connected:
                return _replace(snap, quality=QUALITY_IDLE, quality_reason="未连接")
            level, reason = self._judge(snap)
            return _replace(snap, quality=level, quality_reason=reason)

    def _judge(self, snap: LinkSnapshot) -> Tuple[str, str]:
        if snap.bad_ratio > BAD_RATIO_BAD:
            return QUALITY_BAD, f"坏帧占 {snap.bad_ratio * 100:.0f}%，多半是波特率或线序不对"
        if snap.lost_ratio > LOST_RATIO_BAD:
            return QUALITY_BAD, f"约丢 {snap.lost_ratio * 100:.0f}% 帧，曲线是残缺的"
        if snap.bad_frames > 0:
            return QUALITY_WARN, f"收到 {snap.bad_frames} 个坏帧（CRC / 帧尾 / 超长）"
        if snap.lost_ratio > LOST_RATIO_WARN:
            return QUALITY_WARN, f"约丢 {snap.lost_ratio * 100:.0f}% 帧"
        if snap.heartbeat_misses > 0:
            # 有遥测却没 PONG：链路还活着，但固件可能没实现心跳应答，需要提示
            return QUALITY_WARN, f"连续 {snap.heartbeat_misses} 次心跳无应答（仍有遥测，未判死）"
        if snap.jitter_ms is not None and snap.jitter_ms > JITTER_WARN_MS:
            return QUALITY_WARN, f"往返时延抖动 {snap.jitter_ms:.0f} ms，链路不稳"
        if self._peak_fps > 0 and snap.fps < self._peak_fps * FPS_DROP_RATIO:
            return QUALITY_WARN, f"帧率从 {self._peak_fps:.0f} 掉到 {snap.fps:.0f} 帧/s"
        return QUALITY_OK, "收发正常"

    # ---- 内部 -------------------------------------------------------------

    def _prune(self, now: Optional[float] = None) -> None:
        moment = self._clock() if now is None else now
        horizon = moment - self._window_s
        while self._window and self._window[0][0] < horizon:
            self._window.popleft()


def _replace(snap: LinkSnapshot, **changes: object) -> LinkSnapshot:
    """``dataclasses.replace`` 的薄包装：只改质量字段，统计值原样带走。"""
    values = {
        "rx_frames": snap.rx_frames,
        "tx_frames": snap.tx_frames,
        "bad_frames": snap.bad_frames,
        "lost_frames": snap.lost_frames,
        "fps": snap.fps,
        "bytes_per_s": snap.bytes_per_s,
        "rtt_last_ms": snap.rtt_last_ms,
        "rtt_avg_ms": snap.rtt_avg_ms,
        "rtt_max_ms": snap.rtt_max_ms,
        "jitter_ms": snap.jitter_ms,
        "heartbeat_misses": snap.heartbeat_misses,
        "uptime_s": snap.uptime_s,
        "quality": snap.quality,
        "quality_reason": snap.quality_reason,
    }
    values.update(changes)
    return LinkSnapshot(**values)  # type: ignore[arg-type]
