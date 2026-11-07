# 小时级长稳压测（card38）

> 问题：挂机一夜后，内存会不会涨、界面会不会变卡？
> 答案不能靠"几分钟加速跑"推出来：加速跑验证的是**数据量封顶后是否退化**，
> 而真实挂机考验的是**定时器、事件循环、GC、线程池在几小时里的行为**。
> 这篇记录工具、口径与实测。

## 一、两种压测，别混用

| 目的 | 工具 | 时间尺度 | 判什么 |
| --- | --- | --- | --- |
| 数据管道 / 连接生命周期的等效载荷 | `tools/pressure_test.py`（见 [PRESSURE_TEST.md](PRESSURE_TEST.md)） | 秒级（几分钟跑完等效几小时的数据量） | 队列封顶后内存是否恒定、重连 N 次句柄是否累积 |
| **真实挂机** | `tools/soak_test.py`（本文） | **墙钟 4 h / 8 h** | 内存曲线斜率、句柄 / 线程漂移、单帧渲染耗时是否爬升、偶发异常 |

两者不能互相替代：前者能证明"队列不涨"，但证明不了"第 5 小时开始变卡"。

## 二、工具：`tools/soak_test.py`

### 负载（全部走真实界面路径）

- 通道切「内置模拟器」后点**连接** → 20 Hz 遥测经 `CommWorker` 线程、跨线程信号进主线程；
- 每 `--param-every` 秒改一次 Kp/Ki 并下发（PID_SET / SP_SET 交替），模拟真实调参节奏；
- 每 `--step-every` 秒触发一次阶跃，`--step-hold-s` 秒后显式 STOP；
- 每 `--reconnect-every` 秒断开、3 s 后重连；
- 帧日志环形缓冲（默认 2000 帧 ≈ 100 s 满载）与曲线队列（20000 点 ≈ 17 min 满载）全程跑满。

### 采样（CSV 一行一个时刻）

`t_s / event / rss_mb / traced_mb / handles / threads / render_ms / rtt_ms / fps /
rx_frames / tx_frames / bad_frames / trace_frames / plot_points / buffer_points / log_rows / connected / errors`

- 默认 10 s 一次（`--sample-every`）：高频采样本身会扰动结果；
- 事件时刻（param / step-start / step-stop / disconnect / reconnect / warmup-end / end）
  单独写行，便于把资源波动与事件对齐；
- 每行立即 flush：进程被杀也保留已有数据；
- `--tracemalloc` 额外记录 Python 已跟踪内存，用来区分"分配器行为"与"真泄漏"；
- `--tracemalloc-snapshots 60,1500` 在指定秒数取快照，跑完写 `<csv>.tracemalloc.txt`：
  首末已跟踪内存差、折合 MB/h，以及按 `lineno` 统计的净增长 Top 20 分配点；
  快照本身有约 2 倍记账开销，只用于对比，不进最终性能数字。

### 判定口径（脚本内置，报告逐项写明）

| 指标 | 阈值 | 依据 |
| --- | --- | --- |
| 内存 | 线性回归斜率 ≤ 5 MB/h **且** 首尾差 ≤ 20 MB → 平稳 | 斜率比首尾差更能排除单点抖动 |
| 句柄 | 窗口内 max−min ≤ 5 → 平稳 | 重连会短时 +1~2 个 socket 句柄，属正常 |
| 线程 | 窗口内 max−min ≤ 2 → 平稳 | 线程池收缩/扩张超过 2 条要按事件归因 |
| 渲染耗时 | 后半段均值 / 前半段均值 ≤ 1.2 → 未爬升 | 只看均价会漏掉缓慢爬升，按前后半对比 |
| 时长 | < 10 min 只算"跑通校验"，不给稳定性结论 | 前 17 分钟曲线队列仍在填充，判泄漏必然误报 |
| 预热 | 判定窗口跳过前 60 s | Qt 线程池、字体/位图缓存、socket 池在启动期建立 |

### 运行卫生

- 配置隔离：`SERIALPID_CONFIG` 指向输出目录旁的 `soak-config.json`，不碰用户真实配置；
- 睡眠：Windows 上 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`，
  结束（含异常路径）恢复；
- 异常：任何回调异常都写 `<csv>.errors.log` 并计数，**不中断长跑、也不吞掉**；
- 默认离屏（`--offscreen` 等价行为）：挂机窗口不会被人误点关闭；需要看真实窗口加 `--show`。

## 三、实测

环境：Windows 11 10.0.26200 · Python 3.13.3 · PySide6 6.9.3 · psutil 7.2.2 ·
解释器 `C:\Users\shehu\Desktop\串口PID\.venv\Scripts\python.exe`。

### 3.1 空跑基线（量脚本自身开销）

`--baseline --minutes 10 --sample-every 10`（`docs/soak/baseline-10min-20261107.csv`）：

| 指标 | 实测 | 说明 |
| --- | --- | --- |
| 内存 | 99.3 → 100.2 MB，斜率 1.64 MB/h | 只有窗口与采样，已经是"平稳"量级 |
| 句柄 | 310 → 278 | **首版误判为漂移**：全在启动几十秒里发生 |
| 线程 | 38 → 27 | 同上：Qt 线程池建完再回收 |
| 渲染耗时 | 0.20 → 0.17 ms（后/前 0.86） | 空跑时重画几乎不花钱 |

首版把上面两行读成"需要解释：句柄 / 线程"。3 分钟复核（`--warmup-s 60`）判定窗口内的
句柄 286→278、线程 28→25 仍在缓慢收敛，说明**启动期不止 60 s**——这类缓慢收敛在 4 h 档里
会被长时间平台期淹没，短跑里必须靠"短跑校验：不做稳定性判定"兜住。

> 结论 1：**采样与判定的成本可以忽略**（空跑斜率 1.64 MB/h、渲染 0.2 ms），
> 压测数字不是脚本自己制造的。

### 3.2 机制校验（6 分钟，所有负载各触发两次）

`--minutes 6 --param-every 30 --step-every 120 --reconnect-every 180`
（`docs/soak/smoke-6min-20261107.csv`，55 个采样点）：

| 指标 | 实测 |
| --- | --- |
| 内存 | 100.1 → 106.1 MB（期间曲线队列 7,027/20,000 点仍在填充） |
| 句柄 / 线程 | 314→305 / 40→36（启动期收敛） |
| 单帧渲染 | 均值 0.93 ms，最大 1.74 ms（预算 3 ms） |
| 帧日志 | 2,000 帧**顶满**（环形缓冲生效） |
| 收帧 / 发帧 / 坏帧 | 3,665 / 183 / 0 |
| 事件 | `start` → `param`×11 → `step-start`×2 → `step-stop`×2 → `disconnect`×1 → `reconnect`×1 → `end` |
| 脚本内捕获异常 | **0** |

事件链完整、两条缓冲都在跑、断线与重连都发生在预期时刻，`errors=0`：
说明**负载是在真实界面路径上施加的**，不是直接调内部函数凑出来的数字。

### 3.3 4 小时档 / 8 小时档

两档由 `scripts/soak_night.ps1` **顺序**跑（并行会互相抢 CPU，渲染耗时就不作数），
总耗时约 12 h。归档位置：

| 档 | CSV | 报告 |
| --- | --- | --- |
| 4 h（白天，窗口内可随时开 CSV 看） | `docs/soak/soak-4h-<启动时刻>.csv` | 同名 `.md`（跑完自动生成） |
| 8 h（过夜） | `docs/soak/soak-8h-<启动时刻>.csv` | 同名 `.md` |

判读顺序（也是报告"三件事"的顺序）：

1. **内存曲线是否平稳**：看 `rss_mb` 斜率与 `traced_mb`（开了 `--tracemalloc` 时）；
2. **渲染耗时是否爬升**：看 `render_ms` 前后半均值比，而不是单点最大值；
3. **有没有偶发异常**：`errors` 列是否非 0、是否生成 `<csv>.errors.log`；
   有异常就按下节流程登记 `docs/BUGS.md`，**不重试、不掩盖**。

> 一次完整记录落盘后，把 `.md` 的"结论"表与关键数字摘进本节即可；
> 原始 CSV 一律保留（体积约 10 万行/8 h，压缩后几百 KB）。

本轮已启动（2026-11-07 19:34，`scripts/soak_night.ps1` 顺序执行）：

| 档 | 文件 | 状态 |
| --- | --- | --- |
| 4 h | `docs/soak/soak-4h-20261107-1934.csv` | 已启动，边跑边写；同名 `.md` 由脚本收尾时生成 |
| 8 h | `docs/soak/soak-8h-<启动时刻>.csv` | 4 h 档退出码为 0 后自动接跑 |

启动后 7 分钟的中途读数（供对照，不作结论）：`rss` 100.3 → 108.2 MB（曲线队列 8,012/20,000
仍在填充）、句柄 307 稳定、线程 35–36、`fps` 20.3–20.7、`errors=0`、无 `.errors.log`。
期间 19:37 起跑了一次全量回归（约 45 s），与压测重叠——那几分钟的 `render_ms` 会偏高，
属已知干扰、已在 CSV 里留痕。

### 3.4 分代对比：70 秒冒烟"内存 120 MB/h"的定罪（card42）

[smoke-70s.md](soak/smoke-70s.md) 报的 RSS 99.0→103.2 MB（120.1 MB/h）、句柄漂移 7、
线程漂移 4，发生在**曲线队列仍在填充、Qt 线程池仍在收敛**的头 70 秒里。把同一负载拉长后，
斜率自己塌下来，不需要"修"任何对象。

4 h 正式跑（`docs/soak/soak-4h-20261107-1934.csv`，非事件行按 `t_s` 分段回归）：

| 窗口 | RSS 起→止 | 斜率 |
| --- | --- | --- |
| 0–60 s（启动） | 100.30 → 103.93 MB | +265.3 MB/h |
| 60–300 s | 104.96 → 107.36 MB | +23.7 MB/h |
| 300–900 s（队列填充） | 107.56 → 111.00 MB | +20.9 MB/h |
| 900–1900 s | 111.05 → 111.80 MB | +1.4 MB/h |
| **1030–1900 s（20000 点满载后）** | **111.70 → 111.80 MB** | **+0.37 MB/h** |

曲线与遥测缓冲在 `t=1030.7 s` 同时到 20000/20000，RSS 到 111.7 MB；之后 14.5 min 只涨 0.1 MB。
句柄 316→307、线程 41→35 是启动期**向下收敛**；稳态 307/35，重连瞬间临时 303↔309 句柄、
33↔38 线程，断开即回落（多出来的 5 条是 CommWorker + SimEngine + 线程池的工作线程及其内核句柄），
不跨轮累积。

tracemalloc 30 min 对照（`docs/soak/leak/leak-30min-20261107.csv` 与 `.tracemalloc.txt`，
71 次断线重连、89 次阶跃）：+60 s 已跟踪 25.89 MB → +1500 s 31.05 MB（+5.16 MB，+12.9 MB/h），
但缓冲在 1300 s 满后，1300–1800 s 斜率只有 **+2.6 MB/h**（1500–1800 s +2.0 MB/h）。
Top 20 增长点逐条都能落到容量上限内：

| 分配点 | 净增长 | 归因 |
| --- | --- | --- |
| `struct.unpack(fmt, payload)` | +2293 KiB / 94,718 块 | `TelemetrySample` 的 4 个 float（约 80k 个）+ 解析元组，被样本持有 |
| `TelemetrySample(...)` | +2074 KiB / 18,989 块 | `TelemetryBuffer(max_len=20000)` 满载 |
| `self.rows.append(dict(row))` | +319 KiB / 1,382 块 | 压测脚本自己的 838 行 CSV 记录 |
| `self._buf.append(sample)`（曲线环形缓冲） | +153 KiB / 297 块 | 20000 点定长环 |
| `tracemalloc` 快照 `traces` / `TraceEntry` | +143 KiB | 快照自身开销（只在对比时存在） |

前两项合计 4.37 MB，占 5.16 MB 总增长的 84.6%，正好是 20000 个样本 ×（对象 112 B + 4 个 float）；
它们到 20000 就停止增长。**结论：无罪**——不是"哪个对象持续涨多少 MB/h"的泄漏，
是 `TelemetryBuffer` 一次性的容量填充加分配器 arena 扩张；满载后 4 h 档 0.37 MB/h、30 min 末端 2.6 MB/h，
都在 5 MB/h 判据内。未做任何"手动 gc"式修补。

重连生命周期单独 50 次回归（`pc/tests/test_reconnect_resources.py`，50 轮连接→0.12 s 泵事件→断开）：
句柄 317→317、线程 39→39、Python 内存 0.00→0.14 MB、三个信号 receiver `(1,1,1)→(1,1,1)`，
每轮 `SimEngine` weakref 全部回收、无 `SimEngine` 线程残留。其余嫌疑逐条排除：
`FrameTrace` 容量 2000、`LinkStats` RTT `deque(maxlen=24)`、`SimTransport` 出站上限 256 KiB 都是有界的；
`CommCore.close()` 与 `SimEngine.stop()` 都 `join` 并用 `is_alive()` 真检查；`_connect_signals()` 只在
`MainWindow.__init__` 调一次，重连复用同一 worker——Qt 信号重复 connect 这条在架构上不成立。
所以本卡**没有泄漏点可修**，只补了这条守住"重连 50 次不增长"的回归用例。

## 四、偶发异常怎么登记

脚本不会让异常中断长跑（否则 8 小时就白跑了），但也**绝不吞掉**：

1. 异常写进 `<csv>.errors.log`，一行含时刻、位置标签、完整 traceback；
2. CSV 的 `errors` 列变成非 0，报告结论直接带出"有 N 次异常"；
3. 登记到 [BUGS.md](BUGS.md)：编号、现象、触发时刻（可从 CSV 精确到秒）、
   当时的负载事件（`param` / `step` / `reconnect`）、判定与责任方。

> 硬规矩：**不用重试掩盖**。偶发失败就是证据，重跑到"看起来通过"等于把证据删了。

## 五、复现命令

```powershell
# 机制校验：6 分钟，把所有负载都触发一遍（参数 30s、阶跃 120s、断线 180s）
python tools\soak_test.py --minutes 6 --param-every 30 --step-every 120 --reconnect-every 180

# 空跑基线：量脚本自身开销（只开窗口 + 采样，不连链路）
python tools\soak_test.py --baseline --minutes 10

# 白天 4 小时档
python tools\soak_test.py --hours 4 --out docs/soak/soak-4h.csv

# 过夜 8 小时档（跑完自动写报告）
python tools\soak_test.py --hours 8 --out docs/soak/soak-8h.csv

# 两档顺序跑（4h 后接 8h，约 12 小时；不要并行，两个实例互相抢 CPU）
.\scripts\soak_night.ps1
```

## 六、怎么读 CSV

任何能画折线的工具都行（Excel / Origin / `python -c` + matplotlib）。三条曲线最重要：

1. `rss_mb` 与 `traced_mb` 一起看：两条都涨才是泄漏；只有 RSS 涨多半是分配器缓存；
2. `render_ms`：后半段应落在前半段的 1.2 倍以内；
3. `handles` / `threads`：只应在 `disconnect` 事件附近小幅波动，事件过后回到基线。

## 七、限制

- 离屏渲染与真实窗口的合成路径不同，渲染耗时只能当作**相对趋势**，不能当绝对帧预算；
- 同机并行跑 pytest / PyInstaller 会抬高 `render_ms`，报告中已注明测量时的并发情况；
- 真实串口拔插、USB 掉电重枚举未纳入（没有硬件），仍属真机待验项；
- 曲线队列 20000 点约 17 分钟才满载，因此 < 20 分钟的跑不判"满载后是否退化"。
