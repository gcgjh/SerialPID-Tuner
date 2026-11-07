# -*- coding: utf-8 -*-
"""帧日志限流：未知类型帧与解析异常共用一套按分类键聚合的记录器。

策略（card3 缺陷三 + 二轮卡8）：
    * 首次出现：记一行完整信息；
    * 之后：每满 ``summary_every`` 次，或距上次汇总满 ``summary_interval_s``
      秒，汇总一行；未触发阈值的事件不产生日志行；
    * 静默恢复：同一分类键超过 ``quiet_reset_s`` 秒没有再出现，下次出现时
      重新输出首行（带上前一段的累计），避免一条旧汇总行让人误以为错误
      一直没有停；
    * 分类键相互独立：未知帧按帧类型；解析异常按「帧类型 + 错误原因」，
      于是「遥测帧载荷 15、期望 16」与「心跳帧载荷 3、期望 2」各自聚合，
      不会被混成一条无意义的「解析异常累计一千次」。

本模块是纯逻辑（不依赖 Qt），GUI 只需把返回的行写进日志面板。
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional

SummaryFn = Callable[[int, int], str]


class _KeyedAggregator:
    """按分类键聚合的限流基类（未知帧与解析异常共用，避免两套规则各自演化）。

    每个键独立维护：累计总数、上次汇总时已记到的总数、上次汇总时刻、
    最近一次出现时刻。``note_key`` 决定是否产出一行文本，``flush`` 把尚未
    汇总的尾部计数收尾。
    """

    def __init__(
        self,
        summary_every: int = 100,
        summary_interval_s: float = 5.0,
        quiet_reset_s: float = 30.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.summary_every = max(int(summary_every), 1)
        self.summary_interval_s = max(float(summary_interval_s), 0.0)
        self.quiet_reset_s = max(float(quiet_reset_s), 0.0)
        self._clock = clock if clock is not None else time.monotonic
        self._state: Dict[object, Dict[str, object]] = {}

    def _entry(self, key: object, summary_fn: SummaryFn) -> Dict[str, object]:
        entry = self._state.get(key)
        if entry is None:
            entry = {
                "total": 0,
                "first": False,
                "last_total": 0,
                "last_time": 0.0,
                "seen_time": 0.0,
                "summary_fn": summary_fn,
            }
            self._state[key] = entry
        else:
            entry["summary_fn"] = summary_fn
        return entry

    def note_key(
        self, key: object, first_line: str, summary_fn: SummaryFn
    ) -> Optional[str]:
        """记录一次事件；需要写日志时返回日志行，否则返回 ``None``。

        ``first_line`` 是每次「重新开张」时使用的首行文本；
        ``summary_fn(total, delta)`` 返回汇总行文本。
        """
        now = self._clock()
        entry = self._entry(key, summary_fn)
        total = int(entry["total"]) + 1
        entry["total"] = total

        quiet = (
            bool(entry["first"])
            and self.quiet_reset_s > 0.0
            and now - float(entry["seen_time"]) >= self.quiet_reset_s
        )
        entry["seen_time"] = now

        if not bool(entry["first"]) or quiet:
            entry["first"] = True
            entry["last_total"] = total
            entry["last_time"] = now
            if quiet:
                # 静默后重新开张：首行带上上一段的累计，诊断信息不丢
                return f"{first_line}（此前累计 {total - 1} 次）"
            return first_line

        delta = total - int(entry["last_total"])
        elapsed = now - float(entry["last_time"])
        if delta >= self.summary_every or (
            self.summary_interval_s > 0.0 and elapsed >= self.summary_interval_s
        ):
            entry["last_total"] = total
            entry["last_time"] = now
            return summary_fn(total, delta)
        return None

    def flush(self) -> Optional[str]:
        """把尚未汇总的计数收尾成一行（断线 / 清空日志前调用）。"""
        now = self._clock()
        lines: list[str] = []
        for key in sorted(self._state.keys(), key=repr):
            entry = self._state[key]
            if not bool(entry["first"]):
                continue
            total = int(entry["total"])
            delta = total - int(entry["last_total"])
            if delta <= 0:
                continue
            entry["last_total"] = total
            entry["last_time"] = now
            fn = entry["summary_fn"]
            lines.append(fn(total, delta))
        return "\n".join(lines) if lines else None

    def reset(self) -> None:
        """清空全部聚合状态（清空日志 / 切换通道时调用）。"""
        self._state.clear()

    def stats(self) -> Dict[object, int]:
        """每个分类键的累计次数（调试 / 测试用）。"""
        return {k: int(entry["total"]) for k, entry in self._state.items()}


class UnknownFrameLogger(_KeyedAggregator):
    """未识别帧按类型聚合的记录器（线程模型：与调用方相同，主线程调用）。"""

    def note(self, pkt_type: int, payload_len: int) -> Optional[str]:
        """记录一帧未识别帧；需要写日志时返回日志行，否则返回 ``None``。"""
        return self.note_key(
            pkt_type,
            f"未处理帧 type=0x{pkt_type:02X} len={payload_len}",
            lambda total, delta: (
                f"未处理帧 type=0x{pkt_type:02X} 累计 {total} 次（最近 {delta} 次）"
            ),
        )

    def stats(self) -> Dict[object, int]:
        return super().stats()


class ParseErrorLogger(_KeyedAggregator):
    """解析异常按「帧类型 + 错误原因」聚合的记录器。

    与 :class:`UnknownFrameLogger` 共用同一套限流实现：坏帧雪崩时不会把
    连接事件、ACK 回显、阶跃事件顶出日志窗口，同时保留「从何时开始、
    连续多少条、错在哪个类型、期望多长实际多长」这些真正有用的信息。
    """

    def note(self, pkt_type: int, reason: str) -> Optional[str]:
        name = f"type=0x{pkt_type:02X}"
        return self.note_key(
            (pkt_type, reason),
            f"解析异常 {name} {reason}",
            lambda total, delta: (
                f"解析异常 {name} {reason}：累计 {total} 次（最近 {delta} 次）"
            ),
        )

    def stats(self) -> Dict[object, int]:
        return super().stats()
