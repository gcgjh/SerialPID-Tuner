# PC 工具模块接口（protocol / csv_export / preset_manager / config / frame_log）

> 面向 GUI（AI-2）、模拟器（AI-4）与打包脚本的接线说明。
> 协议字节布局以 `docs/PROTOCOL.md` 为唯一基准，本文件只说明 **Python API**。

## 1. `pc/serialpid/protocol.py`

纯协议层：不依赖 Qt / pyserial，所有 I/O 均为 `bytes`。
通信层 `comm.py` 只需 `FrameDecoder` + `encode`，其余为载荷辅助函数。

### 帧与常量

| 名称 | 说明 |
| --- | --- |
| `SOF0 / SOF1 / EOF` | `0xAA` / `0x55` / `b"\x77\x77\x77\x77"` |
| `MAX_PAYLOAD` | 250（LEN 上限） |
| `FRAME_OVERHEAD` | 10（帧头2 + LEN1 + TYPE1 + CRC2 + 帧尾4） |
| `PacketType` | PING/PONG/PID_SET/PID_ACK/SP_SET/TELEMETRY/STEP_START/STEP_STOP/STEP_EVENT/ERROR/RESET |
| `AckResult` | `ACCEPTED=0` / `REJECTED=1` |
| `StepPhase` | `IDLE=0` / `RUNNING=1` / `DONE=2` |
| `PongStatus` | `IDLE=0` / `RUNNING=1` / `STEPPING=2` |

### 封包 / 解包

```python
encode(pkt_type, payload=b"") -> bytes            # 完整帧
parse_frame(data) -> Frame                        # 严格解析单帧，失败抛异常

pack_ping() / pack_step_stop() / pack_reset() -> bytes        # 空载荷
pack_pid_set(kp, ki, kd, sp) -> bytes            # 16B
pack_pid_ack(kp, ki, kd, sp, result) -> bytes    # 17B
pack_sp_set(sp) -> bytes                         # 4B
pack_telemetry(t_ms, sp, pv, out, err) -> bytes   # 20B，模拟器用
pack_step_start(amplitude, hold_ms) -> bytes     # 8B
pack_step_event(phase, t_ms) -> bytes            # 5B
pack_error(code, message) -> bytes               # 2 + UTF-8 消息（≤248B）
pack_pong(fw_ver, status) -> bytes               # 2B
pack_fw_version(major, minor) -> int              # 高4位主/低4位次

unpack_pid_set / unpack_pid_ack / unpack_sp_set / unpack_telemetry /
unpack_pong / unpack_step_start / unpack_step_event / unpack_error
```

- PC 下发方向（`pack_pid_set` / `pack_sp_set` / `pack_step_start`）会拒绝
  `NaN`、`inf` 和超出 float32 范围的数值，抛 `ValueError`——GUI 应当捕获并提示
  “参数非法”，不要把异常抛进 Qt 事件循环。
- 接收方向只校验载荷长度（`PayloadError`，`ValueError` 子类），**不**校验数值有限性；
  MCU 上报 `NaN` 时仍能解出，由 GUI 决定是否丢弃该点。

### 流式解包

```python
dec = FrameDecoder()
frames = dec.feed(chunk)        # 0..N 帧，任何垃圾字节都不会抛异常
dec.pending()                   # 缓冲中未成帧字节数
dec.reset(clear_stats=False)    # 重连/换口必须调用，防止上一路残帧串数据
dec.stats.as_dict()             # bytes_in/frames/dropped_bytes/crc_errors/eof_errors/oversize_errors
```

容错规则与 MCU `pid_comm.c: parser_drain()` 逐条一致：
帧头失配丢 1 字节；帧尾按**本帧自身偏移**校验，失配丢 1 字节；CRC 失配丢整帧；
`LEN > 250` 按伪帧头丢 1 字节；未知 TYPE 原样保留（不丢弃、不解析）。

### 日志

```python
describe_frame(frame) -> str    # "TELEMETRY t=1000ms SP=25.000 PV=24.500 OUT=60.000 err=-0.500"
```

## 2. `pc/serialpid/csv_export.py`

```python
export_csv(samples, path, *, fields=("t_ms","SP","PV","OUT","err"),
           metadata=None, comment_meta=False, derive_elapsed=False,
           encoding="utf-8-sig", delimiter=",", float_fmt="{:.6f}",
           atomic=True) -> int
```

- 首行固定为表头（默认列顺序即 `t_ms,SP,PV,OUT,err`），满足 TASKS.md 验收要求。
- 默认 `utf-8-sig`：Excel 双击直开不乱码；Origin/pandas 若不想要 BOM 传 `encoding="utf-8"`。
- `t_ms` 写成整数；其余列 6 位小数；`NaN/Inf` 写成空单元格。
- `derive_elapsed=True` 追加 `elapsed_s`（自动处理 u32 回绕），可直接当 Origin 的 X 轴。
- `atomic=True`：先写临时文件再替换；导出中途失败时原文件保持不变，不留临时文件。
- 样本支持 `TelemetrySample` 对象、字典（`sp/pv` 或 `SP/PV` 皆可）、
  `(t_ms, SP, PV, OUT, err)` 元组。

流式记录：

```python
with CsvWriter(path, append=False, flush_every=20) as w:
    w.write(sample)
```

## 3. `pc/serialpid/preset_manager.py`

默认存储：`%APPDATA%/SerialPID-Tuner/presets.json`（其它系统 `~/.config/serialpid-tuner/`）。

```json
{
  "schema": "serialpid.presets",
  "version": 1,
  "updated_at": "2026-11-05T22:20:00+08:00",
  "presets": [
    {"name": "中速响应", "kp": 2.0, "ki": 0.1, "kd": 0.02,
     "sp": 30.0, "note": "…", "updated_at": "…"}
  ]
}
```

```python
mgr = PresetManager()                 # strict=False：坏文件自动备份为 *.corrupt-*.json 后用默认值继续
mgr.list() / mgr.names() / mgr.get("中速响应")
mgr.add("快调", kp=3.0, ki=0.3, kd=0.05, sp=45.0, note="…")
mgr.update("快调", kp=3.5, sp=None)   # sp=None 表示清除设定值
mgr.rename("快调", "快调2") / mgr.delete("快调2")
mgr.export_to(path) / mgr.import_from(path, overwrite=False) -> ImportResult
mgr.restore_defaults(clear=False)     # 不覆盖同名用户预设，默认项自动加“(默认)”后缀
```

- 名称不区分大小写唯一，1..32 字符；增益必须有限且 `|v| <= 1e6`；`sp` 可为 `None`。
- 所有修改先落盘成功再改内存；写盘为临时文件 + `os.replace`，不会留下半截 JSON。
- `mgr.warning` 记录坏文件跳过/备份等非致命问题，GUI 可在状态栏提示。
- 异常：`PresetError` / `PresetNotFoundError` / `PresetExistsError` / `InvalidPresetError`。

## 4. 接线注意

- GUI 导出功能请直接调用 `csv_export.export_csv` / `CsvWriter`。
  目前 `gui/log_panel.py::_export_csv` 与 `step_test.py::export_csv` 各自手写了 CSV，
  表头大小写、`t_ms` 格式、小数位互不一致，建议统一替换，避免同一份数据三种格式。
- 预设 UI（v0.2）直接使用 `PresetManager`；当前 GUI 尚未接线。

## 5. 载荷长度预检（card3 缺陷一，`protocol.py`）

界面层分发前调用，畸形载荷不许进入解析流程：

```python
from serialpid.protocol import payload_error_reason, expected_payload_len

reason = payload_error_reason(pkt_type, payload)  # 不通过 -> 原因字符串；通过/不适用 -> None
```

- 定长类型数值全部来自 `PAYLOAD_FMT`（与 PROTOCOL.md §4 载荷定义同源），
  不会在界面层另抄一张表导致静默丢正常帧；
- `ERROR` 可变长：额外校验“声明消息长度 msg_len vs 载荷实际长度”，
  不匹配按截断处理，越界读在预检就被挡住；
- 未知类型不预检（§3.5 原样保留）。
- `main_window._on_frame` 已在解析前预检 + 外层 try/except 兜底，
  状态栏常驻显示“解析异常 N”；任何畸形输入不会让主线程事件循环停更。

## 6. 应用配置（card3 缺陷二，`config.py`）

```python
from serialpid.config import load_config, save_config, default_config_path

config, warning = load_config()          # 缺失自动建默认文件；损坏备份后回退默认
config["param_limits"]["kp"]["max"] = 500.0
save_config(config)
```

- 默认路径：`%APPDATA%/SerialPID-Tuner/config.json`（其它系统 `~/.config/serialpid-tuner/`），
  可用环境变量 `SERIALPID_CONFIG` 覆盖（便携运行 / 测试注入）；
- `param_limits.kp/ki/kd/sp` 提供 `min/max/step/default`，界面按此生成输入框上下限；
- 放宽范围**不会**关闭 `pack_pid_set` 的数值有限性校验（NaN/Inf/float32 防线保留）；
- 字段非法 / 文件损坏时该项回落默认值并给出 warning（GUI 在参数面板提示）。

## 7. 未识别帧日志聚合（card3 缺陷三，`frame_log.py`）

```python
from serialpid.frame_log import UnknownFrameLogger

logger = UnknownFrameLogger(summary_every=100, summary_interval_s=5.0)
line = logger.note(pkt_type, len(payload))   # 需要写日志时返回行，否则 None
logger.flush() / logger.reset() / logger.stats()
```

首次出现记一行完整信息，之后每满 100 次或每满 5 秒汇总一行；
1000 个未知帧约产生 10 行（<15 行验收）。`main_window` 已接线。
