"""通信核心：独立线程独占传输通道，对外以回调交付帧。

Qt 适配层在 GUI 侧把回调转成信号；本模块不依赖 PySide6，可在无显示环境单测。

回调约定：
- on_frame(type: int, payload: bytes)
- on_connect(connected: bool, detail: str)   detail 为 :mod:`connect 常量`之一，
  或主机名（连上时），界面用 :func:`describe_disconnect` 翻成人话
- on_error(message: str)

线程生命周期要点（断流 / 重连场景）：
- 传输对象作为参数传给线程，线程全程只操作自己那份本地引用，不读共享字段，
  从根上避免旧线程退出时关掉新连接。
- 空闲超时分支必须三件事一起做：标记断开、通知上层、跳出循环；只标记不退出
  会把线程变成永远空转的僵尸线程。
- 收尾关闭传输前做身份比较：共享字段仍指向自己这份才关闭，否则放过新连接。
- 连接状态的跨线程读写走同一把锁，避免超时边界上重复通知 / 误报。
- 本模块不做自动重连：掉线必须显式可见，静默重连会掩盖真实硬件故障。
  重连由界面按 :func:`reconnect_delay` 的退避节奏显式发起（card28）。

心跳（card28）：
- ``heartbeat_interval > 0`` 时，连上后按绝对时刻周期发 PING，收到 PONG 算往返时延；
- 判死判据是「连续 N 次 PING 无 PONG **且** 同窗口内没有收到任何帧」——
  对端忙到顾不上回 PONG 但仍在上报遥测时不该被判死；
- ``heartbeat_interval = 0`` 时行为与旧版一致，只靠空闲超时判死（库的默认值，
  GUI 显式打开心跳）。
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple

from .frame_trace import DIR_RX, DIR_TX, FrameTrace
from .link_stats import LinkStats
from .protocol import FrameDecoder, PacketType, encode
from .transport import (
    PeerClosed,
    SerialTransport,
    TcpTransport,
    Transport,
    TransportError,
)

Callback = Callable[..., None]

#: 空读时的节流切片（秒）。TCP 对端关闭后 read 会立即返回空字节，
#: 不节流会变成每秒几十万次的忙等；串口本身有读超时，此切片只是兜底。
_EMPTY_SLEEP = 0.02

# ---------------------------------------------------------------------------
# 断线原因（字符串是回调 detail 的取值，界面据此选文案）
# ---------------------------------------------------------------------------

DROP_TIMEOUT = "timeout"                  # 超过空闲窗口没有收到任何数据
DROP_HEARTBEAT = "heartbeat-timeout"      # 连续多次心跳无应答且无遥测
DROP_READ_ERROR = "read-error"            # 读异常（未归类的底层错误）
DROP_WRITE_ERROR = "write-error"          # 写失败
DROP_PEER_CLOSED = "peer-closed"          # 对端主动关闭（TCP 收到 FIN）
DROP_DEVICE_REMOVED = "device-removed"    # 设备被拔出 / 权限被收回
DROP_DRIVER_ERROR = "driver-error"        # 驱动上报的底层错误
DROP_CLOSED = "closed"                    # 用户主动断开

#: 断线原因 -> (现象, 下一步)。界面直接展示这两段，不再让用户去猜。
DISCONNECT_TEXT: Dict[str, Tuple[str, str]] = {
    DROP_TIMEOUT: (
        "超过判死窗口没有收到任何数据（含遥测）",
        "检查设备供电与接线；设备处于暂停 / 低功耗状态也会表现为这样",
    ),
    DROP_HEARTBEAT: (
        "连续多次心跳没有应答，且期间没有遥测",
        "确认设备仍在运行（供电 / 是否被复位）；USB 转串口可拔插一次后重连",
    ),
    DROP_PEER_CLOSED: (
        "对端主动关闭了连接",
        "模拟器或另一端的程序已退出；确认对端还在运行后重新连接",
    ),
    DROP_DEVICE_REMOVED: (
        "设备被拔出，或端口权限被系统收回",
        "插回 USB 转串口，等 1~2 秒让系统重新枚举，再点连接",
    ),
    DROP_DRIVER_ERROR: (
        "串口驱动报告了底层错误",
        "重装 USB 转串口驱动；换一个 USB 口或另一根线复测",
    ),
    DROP_WRITE_ERROR: (
        "写数据失败",
        "端口可能被别的程序抢占，或设备已掉线；关掉占用程序后重连",
    ),
    DROP_READ_ERROR: (
        "读数据异常",
        "先确认端口没被别的程序占用，再拔插设备复测",
    ),
    DROP_CLOSED: (
        "已主动断开",
        "无需处理",
    ),
}

#: 心跳参数合法范围（界面输入框与通信核心各守一道：核心不信任外部输入）
HEARTBEAT_INTERVAL_MIN_S = 0.2
HEARTBEAT_INTERVAL_MAX_S = 60.0
HEARTBEAT_MAX_MISSED_MIN = 1
HEARTBEAT_MAX_MISSED_MAX = 10
#: 关掉心跳时的空闲判死窗口（协议 §4.1 的 1.0 s，即 card28 之前的旧行为）
IDLE_TIMEOUT_NO_HEARTBEAT_S = 1.0
#: 心跳判死窗口在「间隔 × 次数」之外再留的余量，保证心跳先给出可解释的原因
HEARTBEAT_IDLE_MARGIN_S = 1.0


def clamp_heartbeat(interval_s: float, max_missed: int) -> Tuple[float, int]:
    """把心跳参数夹到合法范围；``interval_s <= 0`` 表示关闭（保留 0 而不是夹成 0.2）。"""
    try:
        interval = float(interval_s)
    except (TypeError, ValueError):
        interval = 0.0
    if interval != interval or interval < 0.0:  # NaN / 负数
        interval = 0.0
    if interval > 0.0:
        interval = min(max(interval, HEARTBEAT_INTERVAL_MIN_S), HEARTBEAT_INTERVAL_MAX_S)
    try:
        missed = int(max_missed)
    except (TypeError, ValueError):
        missed = HEARTBEAT_MAX_MISSED_MIN
    missed = min(max(missed, HEARTBEAT_MAX_MISSED_MIN), HEARTBEAT_MAX_MISSED_MAX)
    return interval, missed


#: 串口读写异常的关键字归类（pyserial 会把所有失败统一加
#: ``could not open port 'COMx':`` 前缀，所以只认 errno / 系统提示 / 类名）。
_REMOVED_WORDS = (
    "does not recognize", "not connected", "disconnected", "removed",
    "拒绝访问", "access is denied", "permissionerror", "permission denied",
    "winerror 5", "errno 13", "(13,", "被拔出", "已断开",
)
_DRIVER_WORDS = (
    "clearcomm", "驱动", "driver", "ioctl", "invalid handle", "句柄",
    "winerror 6", "(6,", "winerror 31", "(31,",
)

#: 自动重连退避节奏（秒）：一秒、两秒、五秒、十秒，之后每分钟一次。
#: 索引即「第几次重试 - 1」，超出列表长度按最后一项。
RECONNECT_DELAYS: Tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 60.0)
#: 自动重连最多尝试次数（到这个次数就明确放弃，不再无限重试）
RECONNECT_MAX_ATTEMPTS = 7


def classify_transport_error(exc: BaseException) -> str:
    """把串口 / 驱动的底层异常归类成 :data:`DISCONNECT_TEXT` 的键。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(word in text for word in _REMOVED_WORDS):
        return DROP_DEVICE_REMOVED
    if any(word in text for word in _DRIVER_WORDS):
        return DROP_DRIVER_ERROR
    return DROP_READ_ERROR


#: 断线原因 -> 状态栏那一行用的短标签（详情走 :func:`describe_disconnect`）
DISCONNECT_BRIEF: Dict[str, str] = {
    DROP_TIMEOUT: "等待数据超时",
    DROP_HEARTBEAT: "心跳超时",
    DROP_PEER_CLOSED: "对端已关闭连接",
    DROP_DEVICE_REMOVED: "设备已拔出",
    DROP_DRIVER_ERROR: "驱动异常",
    DROP_WRITE_ERROR: "写入失败",
    DROP_READ_ERROR: "读取异常",
    DROP_CLOSED: "已断开",
}


#: 断线原因 -> 机制解释。给日志的「原因：」段用：现象说"怎么了"，这里说
#: "为什么"，下一步说"该做什么"。用户贴日志时这三行就够定位。
DISCONNECT_CAUSE: Dict[str, str] = {
    DROP_TIMEOUT: "判死窗口内一帧数据都没收到：设备没在发、波特率不对，或线接触不良",
    DROP_HEARTBEAT: "心跳请求发出去了但没收到应答，且这段时间也没有遥测：设备死机、被复位，或固件没实现 PONG",
    DROP_PEER_CLOSED: "对端（模拟器 / fake_mcu）把套接字关了",
    DROP_DEVICE_REMOVED: "系统报告端口消失：USB 被拔出，或驱动句柄被关闭",
    DROP_DRIVER_ERROR: "串口驱动 / 系统接口返回了错误，读写通道已不可用",
    DROP_WRITE_ERROR: "写串口时抛异常：端口被别的程序抢占，或设备已掉线",
    DROP_READ_ERROR: "读串口时抛异常：端口被别的程序关闭，或设备掉线",
    DROP_CLOSED: "用户点了「断开」",
}


def _disconnect_pair(reason: str) -> Tuple[str, str]:
    """(现象, 下一步)：已知原因查表，未知原因给"未归类"的诚实说法。"""
    known = DISCONNECT_TEXT.get(reason)
    if known is not None:
        return known
    return (
        f"链路断开（原因未归类：{reason or '未知'}）",
        "把这一行日志连同端口诊断文本一起贴给开发者",
    )


def describe_disconnect_message(
    reason: str, *, missed: Optional[int] = None, limit: Optional[int] = None
) -> "UserMessage":
    """断线原因的完整三段式说法（现象 / 原因 / 下一步）。

    ``missed`` / ``limit`` 只对心跳判死有意义；未知原因不编原因，只说"未归类"。
    """
    from .messages import UserMessage

    if reason == DROP_HEARTBEAT and (missed or limit):
        from .messages import heartbeat_timeout_message

        return heartbeat_timeout_message(int(missed or 0), int(limit or 0))
    phenomenon, next_step = _disconnect_pair(reason)
    cause = DISCONNECT_CAUSE.get(
        reason, f"原因未归类：{reason or '未知'}（把这条原因连同日志贴给开发者）"
    )
    return UserMessage(phenomenon=phenomenon, cause=cause, next_step=next_step)


def describe_disconnect(
    reason: str, *, missed: Optional[int] = None, limit: Optional[int] = None
) -> Tuple[str, str]:
    """断线原因 -> (现象, 下一步)；未知原因给出"未归类"的诚实说法。

    ``missed`` / ``limit`` 只对心跳判死有意义：说清"连了几次没应答"和"容忍上限
    可以放宽"。需要带"原因"的完整版本用 :func:`describe_disconnect_message`。
    """
    if reason == DROP_HEARTBEAT and (missed or limit):
        from .messages import heartbeat_timeout_message

        message = heartbeat_timeout_message(int(missed or 0), int(limit or 0))
        return message.phenomenon, message.next_step
    return _disconnect_pair(reason)


def brief_disconnect(reason: str) -> str:
    """状态栏用的短标签；没归类的原因就原样显示，别假装认识它。"""
    return DISCONNECT_BRIEF.get(reason) or (reason or "未知")


def reconnect_delay(attempt: int) -> float:
    """第 ``attempt`` 次重试（从 1 起）之前要等多少秒。"""
    index = max(int(attempt) - 1, 0)
    if index >= len(RECONNECT_DELAYS):
        return RECONNECT_DELAYS[-1]
    return RECONNECT_DELAYS[index]


class CommCore:
    def __init__(
        self,
        on_frame: Optional[Callback] = None,
        on_connect: Optional[Callback] = None,
        on_error: Optional[Callback] = None,
        idle_timeout: float = 1.0,
        heartbeat_interval: float = 0.0,
        heartbeat_max_missed: int = 3,
        stats: Optional[LinkStats] = None,
        trace: Optional[FrameTrace] = None,
    ) -> None:
        self._on_frame = on_frame or (lambda t, p: None)
        self._on_connect = on_connect or (lambda ok, detail: None)
        self._on_error = on_error or (lambda msg: None)
        # 心跳参数可在运行期改（界面开关），所以不能只在构造时算一次：
        # 一把小锁保护这三个值，接收线程每轮取一次快照，改完下一轮即生效。
        self._settings_lock = threading.Lock()
        self._heartbeat_interval, self._heartbeat_max_missed = clamp_heartbeat(
            heartbeat_interval, heartbeat_max_missed
        )
        # 初始空闲窗口尊重显式传入（既有测试用 0.0 关闭空闲判定），
        # 之后一旦调用 set_heartbeat 就按心跳参数重算。
        self._idle_timeout = float(idle_timeout)

        #: 链路统计与帧日志由外部按需注入，便于界面与测试共用同一份
        self.stats = stats if stats is not None else LinkStats()
        self.trace = trace if trace is not None else FrameTrace()

        self._lock = threading.Lock()
        self._transport: Optional[Transport] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._out: "queue.Queue[bytes]" = queue.Queue()
        self._connected = False
        self._last_rx = 0.0

    # ---- 对外接口 ----

    def open(
        self,
        kind: str,
        target: str,
        baud: int = 115200,
        transport: Optional[Transport] = None,
    ) -> bool:
        """kind: 'serial' / 'tcp' / 'sim'。成功启动接收线程返回 True。

        ``transport`` 给定时直接用它，不再按 kind 现造：内置仿真（``sim``）等
        进程内通道由此接入，本模块仍然只面对 ``Transport`` 接口。
        """
        if self.connected:
            self.close()

        tr = transport
        if tr is None:
            tr = SerialTransport() if kind == "serial" else TcpTransport()
        try:
            tr.open(target, baud)
        except TransportError as exc:
            self._on_error(str(exc))
            self._on_connect(False, str(exc))
            return False

        # 每次连接一个独立的停止信号：不能复用同一个 Event 再 clear()，
        # 旧线程还活着的时候 clear 等于把它"复活"，它会在已关闭的旧传输上再读，
        # 读超时后走 _drop 把刚建立的新连接标记成断开（2026-11-07 macos CI：
        # test_drop_then_reconnect_keeps_new_connection「新连接被旧线程收尾误关」）。
        stop = threading.Event()
        self._stop = stop
        self.stats.reset()
        self.trace.reset()
        with self._lock:
            self._transport = tr
            self._connected = True
            self._last_rx = time.monotonic()
        # 传输对象与停止信号都按参数传入线程，线程不再回读共享字段
        self._thread = threading.Thread(target=self._run, args=(tr, stop), daemon=True)
        self._thread.start()
        self._on_connect(True, target)
        return True

    def close(self) -> None:
        """显式关闭：置停止标志、等待线程退出、关闭共享传输。

        线程已退出时 ``join`` 立即返回，不会白等满超时上限。
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)

        with self._lock:
            self._thread = None
            if self._transport is not None:
                try:
                    self._transport.close()
                except OSError:
                    pass
                self._transport = None
            was_connected = self._connected
            self._connected = False

        if was_connected:
            self._on_connect(False, DROP_CLOSED)

    def send(self, pkt_type: int, payload: bytes = b"") -> None:
        """线程安全入队；实际写由接收线程完成。"""
        raw = encode(pkt_type, payload)
        self._out.put(raw)
        self.stats.note_tx(len(raw))
        self.trace.append(DIR_TX, pkt_type, payload)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def heartbeat_interval(self) -> float:
        with self._settings_lock:
            return self._heartbeat_interval

    @property
    def heartbeat_max_missed(self) -> int:
        with self._settings_lock:
            return self._heartbeat_max_missed

    @property
    def idle_timeout(self) -> float:
        with self._settings_lock:
            return self._idle_timeout

    def set_heartbeat(
        self,
        interval_s: float,
        max_missed: Optional[int] = None,
        *,
        idle_timeout: Optional[float] = None,
    ) -> Tuple[float, int]:
        """运行期改心跳参数：下一轮接收循环即生效，**不需要断开重连**。

        判决口径随参数一起收敛：
        * ``interval_s > 0``：判死窗口 = ``间隔 × 次数 + 1 s``（心跳先给出原因）；
        * ``interval_s == 0``：回到旧版的空闲超时 1.0 s——"不开心跳"必须与旧版
          行为完全一致，不能留着上次心跳算出来的 7 s 窗口；
        * ``max_missed=None`` 表示只改间隔，沿用当前次数。
        """
        current_missed = self._heartbeat_max_missed if max_missed is None else max_missed
        interval, missed = clamp_heartbeat(interval_s, current_missed)
        with self._settings_lock:
            self._heartbeat_interval = interval
            self._heartbeat_max_missed = missed
            if idle_timeout is None:
                self._idle_timeout = (
                    interval * missed + HEARTBEAT_IDLE_MARGIN_S
                    if interval > 0.0
                    else IDLE_TIMEOUT_NO_HEARTBEAT_S
                )
            else:
                self._idle_timeout = float(idle_timeout)
        return interval, missed

    def reset_stats(self) -> None:
        """清空链路统计（帧计数 / 坏帧 / 丢帧 / RTT / 会话时长）。

        不动帧日志、不动连接状态：用户要的是"从现在起重新数"，不是断线。
        """
        self.stats.reset()

    def _settings(self) -> Tuple[float, int, float]:
        """接收线程用的参数快照（一次拿全，避免三个字段各自漂移）。"""
        with self._settings_lock:
            return self._heartbeat_interval, self._heartbeat_max_missed, self._idle_timeout

    def stats_snapshot(self):
        """链路质量快照（界面线程调用；未连接时质量字段为"未连接"）。"""
        return self.stats.snapshot(connected=self.connected)

    # ---- 线程主体 ----

    def _write(self, tr: Transport, pkt_type: int, payload: bytes = b"") -> None:
        """由接收线程直接写一帧（心跳走这里，不走队列，免得被读阻塞拖慢）。"""
        raw = encode(pkt_type, payload)
        tr.write(raw)
        self.stats.note_tx(len(raw))
        self.trace.append(DIR_TX, pkt_type, payload)

    def _run(self, tr: Transport, stop: threading.Event) -> None:
        dec = FrameDecoder()
        last_rx = self._last_rx
        empty_streak = 0
        # 空读硬上限：按超时窗口折算，与空闲超时互为兜底判据；
        # idle_timeout <= 0 表示关闭空闲超时，此判据一并停用。
        interval, missed_limit, idle_timeout = self._settings()
        heartbeat_on = interval > 0.0
        # 空读硬上限：按超时窗口折算，与空闲超时互为兜底判据；
        # idle_timeout <= 0 表示关闭空闲超时，此判据一并停用。
        max_empty = (
            max(1, int(idle_timeout / _EMPTY_SLEEP) + 2)
            if idle_timeout > 0
            else None
        )
        #: 已发出但还没等到 PONG 的 PING 时刻（FIFO；PING 无序号，只能按先后配）
        pending_pings: Deque[float] = deque()
        missed = 0
        next_ping_at = time.monotonic() + interval if heartbeat_on else 0.0
        #: 心跳判死窗口：N 次间隔；这段时间内连遥测都没有才判死
        hb_window = interval * missed_limit
        #: 已喂进解包器、但还没计入统计的坏帧数（解包器统计是全量累计值）
        seen_bad = 0

        try:
            while not stop.is_set():
                now = time.monotonic()

                # ---- 参数可能在运行期被界面改过：每轮取一次快照 ----
                live_interval, live_missed, live_idle = self._settings()
                if (live_interval, live_missed) != (interval, missed_limit):
                    interval, missed_limit = live_interval, live_missed
                    heartbeat_on = interval > 0.0
                    hb_window = interval * missed_limit
                    # 换参数等于换心跳节奏：在途 PING 的账作废，欠账清零重数
                    pending_pings.clear()
                    missed = 0
                    next_ping_at = now + interval if heartbeat_on else 0.0
                if live_idle != idle_timeout:
                    idle_timeout = live_idle
                    max_empty = (
                        max(1, int(idle_timeout / _EMPTY_SLEEP) + 2)
                        if idle_timeout > 0
                        else None
                    )

                # ---- 心跳：按绝对时刻发，不靠"睡满间隔"（会累积漂移） ----
                if heartbeat_on and now >= next_ping_at:
                    try:
                        self._write(tr, PacketType.PING)
                    except (OSError, TransportError) as exc:
                        self._on_error(f"心跳发送失败: {exc}")
                        self._drop(DROP_WRITE_ERROR, tr)
                        return
                    pending_pings.append(time.monotonic())
                    missed += 1
                    if len(pending_pings) > missed_limit * 2:
                        pending_pings.popleft()
                    next_ping_at = max(next_ping_at + interval, now + interval * 0.5)
                    self.stats.note_ping()

                try:
                    data = tr.read(1024)
                except PeerClosed as exc:
                    self._on_error(f"对端关闭: {exc}")
                    self._drop(DROP_PEER_CLOSED, tr)
                    return
                except OSError as exc:
                    reason = classify_transport_error(exc)
                    self._on_error(f"读取异常: {exc}")
                    self._drop(reason, tr)
                    return

                if data:
                    empty_streak = 0
                    last_rx = time.monotonic()
                    got_pong = False
                    for frame in dec.feed(data):
                        try:
                            got_pong = self._handle_frame(frame) or got_pong
                        except Exception as exc:  # noqa: BLE001 - 回调异常不能打死线程
                            self._on_error(f"帧回调异常: {exc}")
                    # 解包器统计是全量值，这里只把"新增的坏帧"计入链路质量
                    bad_total = (
                        dec.stats.crc_errors + dec.stats.eof_errors
                        + dec.stats.oversize_errors
                    )
                    if bad_total > seen_bad:
                        self.stats.note_bad(bad_total - seen_bad)
                        seen_bad = bad_total

                    # PONG 是把心跳账本结清的唯一事件：算 RTT、清欠账
                    if got_pong:
                        self._settle_heartbeat(pending_pings)
                        missed = 0
                else:
                    empty_streak += 1
                    if idle_timeout > 0:
                        idle = time.monotonic() - last_rx > idle_timeout
                        if idle or (max_empty is not None and empty_streak >= max_empty):
                            self._drop(DROP_TIMEOUT, tr)
                            return
                    # 节流：避免对端关闭后 read 立即返回造成忙等占满一个核
                    time.sleep(_EMPTY_SLEEP)

                # ---- 心跳判死：既没 PONG 也没数据，才认为链路失效 ----
                if heartbeat_on and missed >= missed_limit:
                    silent_for = time.monotonic() - last_rx
                    if silent_for >= hb_window:
                        self.stats.note_heartbeat_gap(missed)
                        self._drop(DROP_HEARTBEAT, tr)
                        return

                # 排空发送队列
                while True:
                    try:
                        raw = self._out.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        tr.write(raw)
                    except (OSError, TransportError) as exc:
                        self._on_error(f"写入异常: {exc}")
                        self._drop(DROP_WRITE_ERROR, tr)
                        return
        finally:
            # 身份比较：共享字段仍指向自己这份传输才关闭并复位状态；
            # 若已被重连替换成新传输，则放过，绝不关掉新连接、也不清新连接状态。
            with self._lock:
                if self._transport is tr:
                    try:
                        tr.close()
                    except OSError:
                        pass
                    self._transport = None
                    self._connected = False

    def _handle_frame(self, frame) -> bool:
        """接收线程内的单帧处理：统计、帧日志、心跳账本，最后交给上层。

        返回该帧是否为 PONG（心跳账本由调用方在整批处理完后结清）。
        """
        self.stats.note_rx(len(frame.payload) + 10)
        self.trace.append(DIR_RX, frame.type, frame.payload)
        is_pong = frame.type == PacketType.PONG
        if is_pong:
            self.stats.note_pong()
        if frame.type == PacketType.TELEMETRY and len(frame.payload) == 20:
            # 只有标准 20 字节遥测才有 u32 时间戳可用来推断丢帧
            self.stats.note_telemetry(int.from_bytes(frame.payload[:4], "little"))
        self._on_frame(frame.type, frame.payload)
        return is_pong

    def _settle_heartbeat(self, pending: Deque[float]) -> None:
        """收到 PONG：用最早那笔未结账的 PING 算往返时延，并清空欠账。"""
        if not pending:
            return
        now = time.monotonic()
        rtt_ms = max((now - pending[0]) * 1000.0, 0.0)
        self.stats.note_rtt(rtt_ms)
        pending.clear()

    def _drop(self, reason: str, tr: Transport) -> None:
        """标记断开并通知上层；线程随后自行退出（不自动重连）。

        重复掉线 / 边界竞态下只通知一次；且只有"当前这份传输"才有资格把会话
        标记成断开——旧线程收尾时新连接可能已经接上，不能让它把新连接按死。
        """
        with self._lock:
            if self._transport is not tr:
                return
            if not self._connected:
                return
            self._connected = False
        self._on_connect(False, reason)
