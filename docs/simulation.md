# 内置仿真生态（卡27）

本轮把"内置模拟器"与"TCP 离线模拟器"收敛成**一套实现**，并在传输层补上了
链路劣化、录制回放；预设扩到 8 个，对象参数会记住，引擎可以手动步进。

## 1. 一套实现，两个入口

```
               ┌──────────────────────── tools/fake_mcu.py（薄壳）
               │   只做：监听 TCP / feed(bytes) / 引擎 emit 的帧写回 socket / 打印日志
               │
  命令帧 ──►  SimEngine（pc/serialpid/sim/engine.py）──► 遥测帧
               │   唯一实现：协议会话、命令分发、PID、阶跃状态机、对象推进
               │
               └──────────────────────── 进程内入口（界面「内置模拟」）
```

* `tools/fake_mcu.py` 的 CLI（`--host` 默认 `127.0.0.1`、`--port` 默认 `9000`）与
  启动/断开/各类命令的输出文案**完全不变**，既有集成测试仍按子进程拉起它。
* 两个入口的应答帧逐字节一致，由 `pc/tests/test_fake_mcu_thin.py` 锁死：
  同一条命令序列分别喂给薄壳与引擎，比较拼接后的**原始字节**；并检查薄壳源码里
  不再出现 PID 算式/对象模型（防止再长回第二份实现）。

## 2. 八个场景预设（每个对应一类真实对象）

界面下拉框切预设时整组替换 7 个参数（6 个对象参数 + 控制周期），不会"只换两个、
其余串味"。鼠标停在预设名上会显示同一条说明。

| 预设 | 对象增益 | τ1 / τ2 (s) | 延迟(拍) | 噪声 σ | 量化 | 对应什么真实对象 |
|---|---|---|---|---|---|---|
| 快速小惯性 | 1.0 | 0.3 / 0.8 | 0 | 0 | 0 | 小热容探头、小功率加热片：几十毫秒就跟着动，曲线几乎不超调 |
| 慢速大惯性热对象 | 1.0 | 12 / 45 | 0 | 0 | 0 | 大功率加热炉、热水箱：十几秒才见反应，整定周期长，容易等不及 |
| 大延迟对象 | 1.0 | 2 / 5 | 10 | 0 | 0 | 长管道、远距离测温：延迟半秒左右，与惯性同量级 |
| 带噪声传感器 | 1.0 | 1.2 / 3 | 0 | 1.2 | 0.05 | 廉价热电偶加长线干扰：曲线有毛刺，读数最小分辨约 0.05 |
| 大增益易振荡 | 8.0 | 0.8 / 2.5 | 6 | 0 | 0 | 高放大倍数执行器（小加热器 + 大功率驱动）：Kp 略大就等幅振荡 |
| 执行器饱和 | 30.0 | 3 / 8 | 1 | 0 | 0 | 大惯性对象配小执行器：输出很快顶到 0 或 100，积分饱和后回不来 |
| 粗糙量化 | 1.0 | 1.2 / 3 | 0 | 0.8 | 0.5 | 8 位 ADC / 0.5 度分辨率传感器：曲线呈明显阶梯，微分项被放大 |
| 纯延迟主导 | 1.0 | 0.3 / 0.8 | 30 | 0 | 0 | 传送带、气动输送：一秒半纯滞后，惯性可忽略，先空走一段才看到反应 |

原来的 5 个预设名称与数值全部保留；「接近临界振荡」按卡27 改名为
「大增益易振荡」（同一物理现象，只改显示名）。

## 3. 仿真参数会记住

* 路径：`sim_state.json`，与 `config.json` **同目录**（`SERIALPID_CONFIG` 指哪跟哪，
  便携运行口径不变）。单独一份文件，是因为 `config.json` 只认自己的键，把仿真参数
  塞进去会在每次开窗时被丢掉。
* 行为：改参数 → 200 ms 防抖落盘（原子写：临时文件 + `os.replace`）；
  启动时读回；文件损坏先备份为 `sim_state.json.corrupt-<时间戳>.json` 再用默认；
  越界值夹回范围并留下警告，绝不因为状态文件坏掉阻塞启动。
* 想恢复默认：删掉 `sim_state.json` 即可。

## 4. 手动步进（不起线程）

```python
from serialpid.sim.engine import SimEngine
from serialpid.sim.plant import Plant, PlantParams

engine = SimEngine(manual=True, plant=Plant(PlantParams()))
engine.start()          # 只开通道、清状态，不起线程
frames = engine.step(10)                 # 推进 10 拍，返回期间的遥测帧
frames += engine.step(1, dt_s=0.05)      # 也可指定本拍 dt
engine.stop()
```

线程模式与手动模式共用同一条 `tick()` 路径（同一套 PID/对象/组帧逻辑），
不存在"离线算法"和"在线算法"两套。`dt_s=0` 或超过 0.2 s 时不推进、不发帧。

## 5. 批量扫参：`tools/sweep_params.py`

```bash
python tools/sweep_params.py                                   # Kp 1..100 取 20 点，Ki 0..2 取 5 点
python tools/sweep_params.py --kp 1:100:20 --ki 0:2:5 --preset 大延迟对象
python tools/sweep_params.py --kp 1:200:30 --gain 1 --tau1 2 --tau2 5 --delay 10 \
                             --kd 0.1 --sp 25 --ticks 600 --out big_delay.csv
```

* 每个组合跑一次闭环阶跃（`SimEngine.step`，不启线程、不做字符串拼接），
  算 **超调 % / 调节时间 s / 稳态误差 / 过零次数 / 末段波动**；
* 产物：`*.csv`（每组合一行，字段固定）+ `*_summary.md`（每个 Ki 一行，
  含 **临界 Kp（开始振荡）** 列；网格里没振荡就写"未振荡（网格上限内）"）。
* `oscillating` 判据：误差过零 ≥ 2 次，且后半段峰峰值 > 2 倍容差带。

## 6. 录制与回放：`serialpid.sim.recorder` + `tools/replay_frames.py`

录制（传输层包装，引擎不感知）：

```python
recorder = FrameRecorder("frames.jsonl")
transport = RecordingTransport(SimTransport(), recorder)
engine = SimEngine(transport=transport)
```

每行一条 JSON：`{"t_ms": 相对毫秒, "dir": "up|down", "type": 帧类型, "len": 载荷长度,
"raw": 整帧十六进制}`。只存帧与相对时间，**不含绝对路径、端口号、序列号**。

回放（把录到的帧按原时序灌回界面）：

```bash
python tools/replay_frames.py --file frames.jsonl --port 9100          # 界面连 127.0.0.1:9100
python tools/replay_frames.py --file frames.jsonl --speed 2 --loop     # 两倍速循环
```

程序内也可用 `ReplayTransport` / `FrameReplay`：`due_frames()` 批量取、`next_frame()`
单步取、`set_speed()` 运行期改倍速、`seek()` 跳到第几帧。默认只播下行帧
（当时的命令不该再灌回设备）。

## 7. 链路劣化层：`serialpid.sim.degradation`

包在**传输层**外面，引擎与界面都不感知；**默认全关，关闭时零开销直通**。

```python
from serialpid.sim.degradation import DegradedTransport, LinkConfig

cfg = LinkConfig(
    enabled=True,
    drop_down=0.3,        # 下行按概率丢整帧
    drop_up=0.2,          # 上行同理
    bad_crc_down=0.1,     # 伪造坏 CRC（只改 CRC 域，帧长不变）
    delay_ms=120, jitter_ms=40,   # 固定延迟 + 随机抖动
    disconnect_after_s=5,         # 跑 N 秒后强制断线
    noise_period_s=1.0, noise_burst_bytes=16,  # 周期性突发噪声
    seed=20261107,
)
transport = DegradedTransport(SimTransport(), cfg)
transport.apply(new_cfg)   # 运行期改参，立即生效
transport.statistics()     # 丢弃/坏帧/延迟/噪声字节计数
```

同一种子下同一命令序列连跑十次，计数与产出字节完全一致（有专门用例）。

## 8. 测试

| 文件 | 覆盖 |
|---|---|
| `pc/tests/test_fake_mcu_thin.py` | 两入口逐字节一致、薄壳无第二份实现、CLI 不变、TCP 返回同字节 PONG |
| `pc/tests/test_sim_fault_injection.py` | 丢帧（上下行）/坏 CRC/延迟抖动/强制断线/突发噪声、十次一致、默认关直通 |
| `pc/tests/test_sim_record_replay.py` | JSONL 字段与无敏感路径、回放帧序列一致、倍速、单步、坏行跳过 |
| `pc/tests/test_sim_presets.py` | 8 预设齐全、参数在范围、响应形态两两不同、真实对象说明 |
| `pc/tests/test_sim_persist_manual.py` | 重启保留、损坏备份、越界夹取、手动步进不起线程且字节可复现 |
| `pc/tests/test_sweep_params.py` | 网格解析、指标算法、CSV/汇总产物、临界 Kp 标注 |
