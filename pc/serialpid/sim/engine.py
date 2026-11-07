"""内置仿真引擎：一条线程按 20 Hz 跑"固件"侧，帧格式与实物串口完全一致。

这是**唯一**的 MCU 替身实现：协议会话、命令分发、阶跃状态机、PID 与对象推进
都在这里；``tools/fake_mcu.py`` 只是它的 TCP 薄壳（卡27 消除两套实现的分叉）。

职责边界（cards_sim 卡23 / 卡27）：

* 引擎 = MCU 固件行为的替身：收命令、算 PID、推对象、发遥测；
* 被控对象**不写死**，通过构造参数注入（``sim/plant.py`` 由卡24 提供，
  引擎只要求 ``step(out, dt_s) -> 测量值`` 与 ``reset()`` 两个方法）；
* 链路劣化（``sim/degradation.py``）与录制回放（``sim/recorder.py``）包在传输层，
  引擎不感知，也不为它们付额外开销；
* 支持手动步进模式（不起线程，一拍一拍推进），给测试与教学用。

时序：每拍先排空并解析收到的命令帧 → 再推进一步对象 → 最后组遥测帧投进
输出队列；节拍按绝对时刻排、分段睡眠，不忙等也不漂移。
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Callable, List, Optional, Protocol, Tuple

from ..protocol import (
    FrameDecoder,
    PacketType,
    PongStatus,
    StepPhase,
    encode,
    pack_pid_ack,
    pack_pong,
    pack_step_event,
    pack_telemetry,
    unpack_pid_set,
    unpack_sp_set,
    unpack_step_start,
)
from .transport import SimTransport

logger = logging.getLogger("serialpid.sim")

#: 默认控制/遥测周期（20 Hz）
DEFAULT_PERIOD_S = 0.05

#: 执行量限幅（与固件、独立模拟器同口径）
OUT_MIN, OUT_MAX = 0.0, 100.0
#: 积分限幅，防饱和
INTEGRAL_LIMIT = 50.0
#: 单拍 dt 上限，防止线程被调度延迟后一步跨太大
MAX_DT_S = 0.2
#: 控制周期允许范围
MIN_PERIOD_S, MAX_PERIOD_S = 0.01, 1.0

#: 初始状态（与 tools/fake_mcu.py 保持一致）
DEFAULT_KP, DEFAULT_KI, DEFAULT_KD = 2.0, 0.5, 0.1
DEFAULT_SP, DEFAULT_PV = 25.0, 20.0


class Plant(Protocol):
    """被控对象协议：吃执行量，吐测量值（与 ``sim/plant.py`` 同一签名）。"""

    def step(self, out: float, dt_s: Optional[float] = None) -> float: ...

    def reset(self) -> None: ...


class LagPlant:
    """兜底一阶惯性对象，仅在未注入对象时使用。

    正式模型见 ``sim/plant.py``（二阶 + 纯延迟 + 噪声 + 量化）；这里的兜底
    只保证引擎能独立跑起来，不承担演示整定效果的责任。
    """

    def __init__(self, tau_s: float = 2.0, gain: float = 1.0, initial: float = DEFAULT_PV) -> None:
        self.tau_s = max(1e-3, float(tau_s))
        self.gain = float(gain)
        self.initial = float(initial)
        self._y = float(initial)
        self.last_out = 0.0

    def step(self, out: float, dt_s: Optional[float] = None) -> float:
        dt = max(0.0, min(float(DEFAULT_PERIOD_S if dt_s is None else dt_s), MAX_DT_S))
        u = max(OUT_MIN, min(OUT_MAX, float(out)))
        self.last_out = u
        self._y += dt * (self.gain * u - self._y) / self.tau_s
        return self._y

    def reset(self) -> None:
        self._y = self.initial
        self.last_out = 0.0


class SimEngine:
    """固件替身：命令分发 + PID + 对象推进 + 遥测上行。

    用法（GUI 接线见卡25）::

        transport = SimTransport()
        engine = SimEngine(transport=transport, plant=plant)
        core.open("sim", "", transport=transport)   # 通信核心只认 Transport
        engine.start()
        ...
        engine.stop()

    单元测试可直接调用 :meth:`tick` / :meth:`handle_frame` 驱动，不起线程。
    """

    def __init__(
        self,
        transport: Optional[SimTransport] = None,
        plant: Optional[Plant] = None,
        period_s: float = DEFAULT_PERIOD_S,
        fw_version: int = 0x10,
        clock: Callable[[], float] = time.monotonic,
        on_event: Optional[Callable[[str, dict], None]] = None,
        manual: bool = False,
    ) -> None:
        self.transport = transport if transport is not None else SimTransport()
        self._plant = plant if plant is not None else LagPlant()
        self._period_s = self._clamp_period(period_s)
        self._fw_version = int(fw_version) & 0xFF
        self._clock = clock
        #: 事件钩子（薄壳用它打印原格式的运维日志）；``None`` 时零回调开销
        self._on_event = on_event
        self._manual = bool(manual)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._decoder = FrameDecoder()
        self._reset_state()

    # ---- 生命周期 -------------------------------------------------------- #

    def start(self) -> None:
        """启动引擎；每次 start 都是新会话（不继承上一次的连接状态）。

        手动模式（``manual=True``）只打开通道、清状态，不起线程，靠 :meth:`step`
        一拍一拍推进——测试与教学需要"停在第 N 拍看看"的能力。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._reset_state()
            if not self.transport.is_open():
                self.transport.open()
            self._stop.clear()
            if self._manual:
                return
            self._thread = threading.Thread(
                target=self._run, name="SimEngine", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> bool:
        """停止线程并关闭通道；返回线程是否在时限内退出。"""
        self._stop.set()
        thread = self._thread
        with self._lock:
            self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)
            joined = not thread.is_alive()
        else:
            joined = True
        self.transport.close()
        if not joined:
            logger.warning("仿真引擎线程在 %.1fs 内未退出", timeout)
        return joined

    def is_running(self) -> bool:
        if self._manual:
            return self.transport.is_open() and not self._stop.is_set()
        thread = self._thread
        return thread is not None and thread.is_alive()

    def __enter__(self) -> "SimEngine":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # ---- 线程主体 -------------------------------------------------------- #

    def _run(self) -> None:
        last = self._clock()
        next_at = last
        # Windows 上 Condition/Event 的带超时等待会被 15.6 ms 系统定时器量化
        # （实测 wait(0.05) 返回 62 ms），直接整段等会把 20 Hz 拖成 16 Hz。
        # 用 time.sleep 分段睡（3.13 的实现是高精度定时器），每段醒来顺手
        # 非阻塞取一次命令，ACK 延迟不超过一个切片，节拍仍按绝对时刻排。
        slice_s = min(0.01, self._period_s)
        while not self._stop.is_set():
            now = self._clock()
            remaining = next_at - now
            if remaining > 0:
                time.sleep(min(remaining, slice_s))
                while True:
                    data = self._take_inbound(0.0)
                    if not data:
                        break
                    self.feed(data)
                continue
            dt = now - last
            last = now
            next_at = now + self._period_s
            try:
                self.tick(dt)
            except Exception:  # noqa: BLE001 - 单拍异常不能打死线程
                logger.exception("仿真引擎单拍异常")
        logger.debug("仿真引擎线程退出")

    # ---- 单拍 ------------------------------------------------------------ #

    def tick(self, dt: float) -> Optional[bytes]:
        """推进一拍，返回本拍发出的遥测帧（未发则 None）。

        dt 为上一拍到现在的时间；为 0 或异常值时本拍不推进。
        """
        self._drain_inbound()
        dt = float(dt)
        if not (0.0 < dt <= MAX_DT_S):
            return None

        with self._lock:
            self._maybe_end_step()
            sp = self.sp
            err = sp - self.pv
            self._integral += err * dt
            self._integral = max(-INTEGRAL_LIMIT, min(INTEGRAL_LIMIT, self._integral))
            deriv = (err - self._last_err) / dt
            self._last_err = err
            out = self.kp * err + self.ki * self._integral + self.kd * deriv
            out = max(OUT_MIN, min(OUT_MAX, out))

            # 执行量喂给对象，拿回测量值（噪声/量化由对象负责）
            try:
                pv = float(self._plant.step(out, dt))
            except Exception:  # noqa: BLE001 - 对象异常降级为保持上一拍
                logger.exception("被控对象推进异常，保持上一拍测量值")
                pv = self.pv
            if pv != pv or pv in (float("inf"), float("-inf")):
                logger.warning("被控对象输出非有限值 %r，保持上一拍", pv)
                pv = self.pv
            self.out = float(getattr(self._plant, "last_out", out))
            self.pv = pv
            self._t_ms = (self._t_ms + int(round(dt * 1000))) & 0xFFFFFFFF

            payload = pack_telemetry(self._t_ms, sp, pv, self.out, err)
            raw = encode(PacketType.TELEMETRY, payload)

        self.transport.emit(raw)
        return raw

    def step(self, count: int = 1, dt_s: Optional[float] = None) -> List[bytes]:
        """手动步进：不依赖线程，一拍一拍推进，返回期间发出的遥测帧。

        ``dt_s`` 省略时按当前控制周期；这是测试、教学与扫参脚本的统一入口，
        与线程模式共用同一套 PID/对象/组帧逻辑，不存在"离线和在线两套算法"。
        """
        dt = self._period_s if dt_s is None else float(dt_s)
        out: List[bytes] = []
        for _ in range(max(0, int(count))):
            raw = self.tick(dt)
            if raw is not None:
                out.append(raw)
        return out

    def _take_inbound(self, timeout: float = 0.0) -> bytes:
        """取上行字节；传输层没有队列接口时返回空（薄壳自己 ``feed``）。"""
        take = getattr(self.transport, "take_inbound", None)
        return take(timeout) if take is not None else b""

    def _drain_inbound(self) -> None:
        while True:
            chunk = self._take_inbound(0.0)
            if not chunk:
                return
            self.feed(chunk)

    def feed(self, data: bytes) -> List[bytes]:
        """把收到的字节喂给帧解码器，逐帧分发，返回所有应答帧。"""
        frames: List[bytes] = []
        for frame in self._decoder.feed(data):
            frames.extend(self.handle_frame(frame.type, frame.payload))
        return frames

    # ---- 命令分发（与 tools/fake_mcu.py 语义一致）------------------------- #

    def handle_frame(self, pkt_type: int, payload: bytes) -> List[bytes]:
        """处理一条命令，返回并写出应答帧；载荷异常只记日志、不崩。"""
        replies: List[Tuple[int, bytes]] = []
        event: Optional[Tuple[str, dict]] = None
        try:
            with self._lock:
                if pkt_type == PacketType.PING:
                    replies.append((PacketType.PONG, pack_pong(self._fw_version, self._status())))
                elif pkt_type == PacketType.PID_SET:
                    kp, ki, kd, sp = unpack_pid_set(payload)
                    self.kp, self.ki, self.kd, self.sp = kp, ki, kd, sp
                    self._integral = 0.0
                    self._last_err = sp - self.pv
                    replies.append((PacketType.PID_ACK, pack_pid_ack(kp, ki, kd, sp, 0)))
                    event = ("PID_SET", {"kp": kp, "ki": ki, "kd": kd, "sp": sp})
                elif pkt_type == PacketType.SP_SET:
                    self.sp = unpack_sp_set(payload)
                    event = ("SP_SET", {"sp": self.sp})
                elif pkt_type == PacketType.STEP_START:
                    amplitude, hold_ms = unpack_step_start(payload)
                    self._begin_step(amplitude, hold_ms)
                    replies.append(
                        (PacketType.STEP_EVENT, pack_step_event(StepPhase.RUNNING, self._t_ms))
                    )
                    event = ("STEP_START", {"amplitude": amplitude, "hold_ms": hold_ms, "sp": self.sp})
                elif pkt_type == PacketType.STEP_STOP:
                    self._revert_step()
                    replies.append(
                        (PacketType.STEP_EVENT, pack_step_event(StepPhase.IDLE, self._t_ms))
                    )
                    event = ("STEP_STOP", {})
                elif pkt_type == PacketType.RESET:
                    self._reset_state()
                    replies.append((PacketType.PONG, pack_pong(self._fw_version, self._status())))
                    event = ("RESET", {})
                else:
                    logger.debug("忽略未知包类型 0x%02X", pkt_type)
        except (struct.error, ValueError) as exc:
            logger.warning("载荷解析失败 type=0x%02X: %s", pkt_type, exc)
            return []

        if event is not None:
            self._notify(*event)
        frames = [encode(kind, body) for kind, body in replies]
        for raw in frames:
            self.transport.emit(raw)
        return frames

    def _notify(self, name: str, fields: dict) -> None:
        """调用事件钩子；钩子异常只记日志，不影响协议应答。"""
        hook = self._on_event
        if hook is None:
            return
        try:
            hook(name, fields)
        except Exception:  # noqa: BLE001 - 运维打印失败不该改变协议行为
            logger.exception("命令钩子异常（%s）", name)

    # ---- 阶跃 ------------------------------------------------------------ #

    def _begin_step(self, amplitude: float, hold_ms: int) -> None:
        self._step_baseline = self.sp
        self.sp = self.sp + amplitude
        if hold_ms > 0:
            self._step_deadline = self._clock() + hold_ms / 1000.0
        else:
            self._step_deadline = None  # 0 表示一直保持，由 STEP_STOP 结束

    def _maybe_end_step(self) -> None:
        if self._step_baseline is None or self._step_deadline is None:
            return
        if self._clock() >= self._step_deadline:
            self._revert_step()
            self.transport.emit(
                encode(PacketType.STEP_EVENT, pack_step_event(StepPhase.DONE, self._t_ms))
            )
            self._notify("STEP_DONE", {"t_ms": self._t_ms})

    def _revert_step(self) -> None:
        if self._step_baseline is not None:
            self.sp = self._step_baseline
        self._step_baseline = None
        self._step_deadline = None

    def _status(self) -> int:
        return PongStatus.STEPPING if self._step_baseline is not None else PongStatus.RUNNING

    # ---- 状态与运行期调参 ------------------------------------------------ #

    def _reset_state(self) -> None:
        """清控制器/阶跃/时间戳与解码器，并复位对象（不动对象参数）。"""
        with self._lock:
            self.kp, self.ki, self.kd = DEFAULT_KP, DEFAULT_KI, DEFAULT_KD
            self.sp, self.pv = DEFAULT_SP, DEFAULT_PV
            self.out = 0.0
            self._integral = 0.0
            self._last_err = 0.0
            self._t_ms = 0
            self._step_baseline: Optional[float] = None
            self._step_deadline: Optional[float] = None
            self._decoder = FrameDecoder()
        try:
            self._plant.reset()
        except Exception:  # noqa: BLE001 - 对象没有 reset 也能用
            logger.debug("被控对象无 reset()，跳过")

    def reset(self) -> None:
        """软复位（等同收到 RESET 帧，但不发应答）。"""
        self._reset_state()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "kp": self.kp, "ki": self.ki, "kd": self.kd,
                "sp": self.sp, "pv": self.pv, "out": self.out,
                "t_ms": self._t_ms,
                "status": self._status(),
                "step_baseline": self._step_baseline,
                "period_s": self._period_s,
                "manual": self._manual,
            }

    @property
    def plant(self) -> Plant:
        return self._plant

    def set_period(self, period_s: float) -> float:
        """改控制周期（运行期生效，下一拍即按新周期排）。"""
        with self._lock:
            self._period_s = self._clamp_period(period_s)
            set_dt = getattr(self._plant, "set_dt", None)
            if callable(set_dt):
                set_dt(self._period_s)
            return self._period_s

    def update_plant(self, **params: float) -> dict:
        """运行期改对象参数：优先 ``configure()`` / ``set_params()``，否则同名赋值。

        返回本次提交的参数名与值（对象侧的告警写进日志）。
        """
        with self._lock:
            for name in ("configure", "set_params"):
                fn = getattr(self._plant, name, None)
                if not callable(fn):
                    continue
                warnings = fn(**params)
                if isinstance(warnings, list) and warnings:
                    for text in warnings:
                        logger.warning("对象参数警告：%s", text)
                return dict(params)
            applied: dict = {}
            for key, value in params.items():
                if hasattr(self._plant, key):
                    setattr(self._plant, key, value)
                    applied[key] = value
                else:
                    logger.warning("对象无参数 %s，已忽略", key)
            return applied

    @staticmethod
    def _clamp_period(period_s: float) -> float:
        return max(MIN_PERIOD_S, min(MAX_PERIOD_S, float(period_s)))


def describe(engine: SimEngine) -> Tuple[str, str]:
    """返回 (通道说明, 状态说明)，供界面只读显示。"""
    snap = engine.snapshot()
    state = "阶跃中" if snap["status"] == PongStatus.STEPPING else "运行"
    return (
        f"内置模拟器 · {snap['period_s'] * 1000:.0f} ms",
        f"{state} SP={snap['sp']:.2f} PV={snap['pv']:.2f} OUT={snap['out']:.2f}",
    )
