# SerialPID-Tuner 二次开发文档

> 面向在 PC 侧扩展功能、新增协议包类型、接入新传输通道或改动固件接口的开发者。
> 协议字节布局以 [PROTOCOL.md](PROTOCOL.md) 为唯一基准；Python 封包 / 解包 /
> CSV / 预设的函数级说明见 [PC_TOOLS_API.md](PC_TOOLS_API.md)。

## 1. 目录结构与分层

```
SerialPID-Tuner/
├── pc/
│   ├── main.py                  # 薄启动器（PyInstaller / 脚本直跑入口）
│   ├── requirements.txt         # 运行依赖
│   ├── requirements-dev.txt    # 开发 / 打包 / 测试依赖
│   ├── tests/                  # 离线单测 + GUI 离屏冒烟
│   └── serialpid/
│       ├── protocol.py         # 纯协议层：帧编解码、CRC、载荷封包/解包
│       ├── comm.py             # 通信核心：接收线程、传输路由、回调注册
│       ├── comm_qt.py          # CommCore 的 Qt 适配（线程回调 → 队列信号）
│       ├── transport.py        # 传输抽象：SerialTransport / TcpTransport
│       ├── model.py             # TelemetrySample / PidParams / TelemetryBuffer
│       ├── step_test.py        # 阶跃响应分析 + 自动测试状态机（纯逻辑）
│       ├── zn_tuning.py        # Z-N 两套公式 + 反应曲线切线辨识（纯逻辑）
│       ├── csv_export.py        # 历史曲线 CSV 导出
│       ├── preset_manager.py    # PID 预设 JSON 管理
│       ├── main.py              # 模块入口（QApplication + 主窗口 + 深色主题）
│       └── gui/
│           ├── main_window.py  # 组装、连接管理、帧分发
│           ├── plot_panel.py    # 实时曲线
│           ├── param_panel.py   # 参数下发 / ACK 回显
│           ├── step_panel.py    # 阶跃命令 + 指标展示
│           ├── zn_panel.py      # Z-N 辅助整定面板
│           ├── log_panel.py      # 日志 / CSV 导出
│           └── theme.py         # 深色主题配色与 apply_dark()
├── mcu/                         # MCU 侧协议层 + PID 应用 + HAL 集成示例
├── tools/
│   ├── fake_mcu.py              # 离线 TCP 模拟器
│   └── make_icon.py             # 生成 assets/app.ico 与 app.png
├── scripts/
│   ├── build.bat / build.sh     # 一键打包
│   └── version_info.txt         # Windows 版本资源
├── assets/                      # 图标等随包资源
└── docs/
```

分层原则：

- **协议层 `protocol.py`** 不依赖 Qt / pyserial / numpy，所有 I/O 均为 `bytes`，
  可在离线脚本与单测中直接复用。
- **核心算法 `step_test.py` / `zn_tuning.py`** 同样不依赖 Qt / numpy，只做纯计算，
  便于离线单测与将来复用在命令行 / Web 服务端。
- **通信层**只负责收发与解帧，业务判定全部上抛给 UI 层。
- **GUI 层**只做展示与交互，不直接操作串口；跨线程只走 Qt 信号。

## 2. 线程模型

```
接收线程 (CommCore 内部)                UI 线程 (Qt 事件循环)
┌───────────────────────┐              ┌──────────────────────────┐
│ Transport.read 循环   │  on_frame    │ CommWorker.frameReceived  │
│ FrameDecoder.feed     │ ──────────▶  │   → MainWindow._on_frame  │
│ 解出完整帧 → 回调      │  队列信号    │ 解包 → 模型 → 面板更新     │
└───────────────────────┘              └──────────────────────────┘
            ▲                                   │
            │  send(pkt_type, payload)          │
            └──────────────────────────────────┘
```

约束：

- 接收线程里**禁止**直接创建控件、绘图、弹对话框或做长耗时解析；收到帧后只
  触发 `CommWorker` 信号，由 Qt 排队投递到 UI 线程。
- `CommWorker.send()` 可在 UI 线程调用，底层向传输通道写帧；发送路径不阻塞
  接收线程。
- `TelemetryBuffer` 由通信回调追加、UI 线程读取；当前实现为定长 deque，
  跨线程追加 / 读取的一致性由 GIL 与 deque 原子性兜底，不要在其中做复杂遍历。
- 传输对象作为参数传给接收线程，线程全程只操作本地引用，不回读共享字段；
  收尾关闭前做身份比较，仅当共享字段仍指向自己这份传输才关闭，避免旧线程
  退出时关掉重连后的新连接。
- 断流退出路径（空闲超时 / 读异常 / 写异常）必须三件事一起做：标记断开、
  通知上层、跳出循环；线程已退出时 `join` 立即返回，不白等超时上限。
- 连接状态由同一把锁保护；上层回调在锁外触发，避免回调反读状态造成死锁。
- 通信层**不做自动重连**：掉线必须显式可见，静默重连会掩盖真实硬件故障。

## 3. 关键接口

### 3.1 通信与传输

```python
from serialpid.comm_qt import CommWorker
from serialpid.transport import list_serial_ports

worker = CommWorker()
worker.open(kind, target, baud=115200)   # kind: "serial" / "tcp"
worker.close()
worker.send(pkt_type, payload=b"")
worker.connected                         # bool

worker.frameReceived.connect(on_frame)  # Signal(int, bytes)
worker.connectionChanged.connect(on_conn)  # Signal(bool, str)
worker.errorOccurred.connect(on_err)     # Signal(str)
```

- `open("tcp", "127.0.0.1:9000")` 走 `TcpTransport`；`open("serial", "COM3", 115200)`
  走 `SerialTransport`。
- `list_serial_ports() -> list[str]` 返回系统串口名；pyserial 缺失时返回空表。
- 传输抽象见 `transport.py` 的 `Transport` 基类；新增通道（蓝牙 / WebSocket 等）
  实现 `open / read / write / close / is_open` 后在 `comm.py` 路由表登记即可。

### 3.2 数据模型

```python
from serialpid.model import TelemetrySample, PidParams, TelemetryBuffer

TelemetrySample(t_ms: int, sp: float, pv: float, out: float, err: float)
PidParams(kp: float, ki: float, kd: float, sp: float)

buf = TelemetryBuffer(max_len=20000)
buf.append(sample)
buf.samples()        # list[TelemetrySample]
buf.last()           # TelemetrySample | None
buf.clear()
```

### 3.3 阶跃分析 `step_test.py`

```python
from serialpid.step_test import analyze_step, StepTester, StepMetrics, Sample

# 纯函数：给定曲线直接出指标
metrics: StepMetrics = analyze_step(
    times, sp, pv,
    band=0.02,          # 稳态带，相对 |ΔSP|；常用 0.01 / 0.02 / 0.05
    tail_ratio=0.2,     # 稳态值取尾部 20% 均值
    step_index=None,    # None 时按 SP 最大跳变自动定位
)
```

`StepMetrics` 主要字段：`step_index / step_t_ms / sp_initial / sp_final /
pv_initial / pv_final / delta_sp / direction / overshoot_pct / peak_value /
peak_t_ms / undershoot_pct / rise_time_ms / settling_time_ms / settled /
steady_state_error / steady_state_error_pct / note`；`summary()` 出单行可读
摘要，`to_dict()` 转字典。

`StepTester` 是自动测试状态机（IDLE → ARMED → RUNNING → CONFIRMING → DONE），
先静置再经回调自动跳变目标值，进入稳态带并保持确认窗口后出指标：

```python
tester = StepTester(
    setpoint_cb=link.set_setpoint,     # cb(sp: float) -> None，注入下发通路
    band=0.02,
    pre_settle_ms=1000.0,
    timeout_ms=30000.0,
    stable_window_ms=500.0,
    tail_ratio=0.2,
    on_metrics=panel.show_metrics,      # cb(metrics: StepMetrics) -> None
)
tester.begin(initial_sp=25.0, target_sp=50.0)
tester.on_sample(t_ms, sp, pv, out=0.0, err=0.0)
tester.stop()
tester.export_csv(path)                # UTF-8 BOM 表头 CSV
```

样本不足 / 检测不到阶跃时，`metrics` 为 `None`，原因记录在 `tester.last_error`，
不会把异常抛进 Qt 事件循环。

### 3.4 Z-N 整定 `zn_tuning.py`

```python
from serialpid.zn_tuning import (
    zn_closed_loop, zn_open_loop, estimate_reaction_curve,
    ZNParams, ReactionCurve,
)

closed = zn_closed_loop(ku=2.0, tu=2.0)   # {"P": ZNParams, "PI": ..., "PID": ...}
opened = zn_open_loop(k=1.0, l=0.5, t=2.0)

curve: ReactionCurve = estimate_reaction_curve(times, sp, pv,
                                               step_index=None, smooth_win=3)
# curve.k / curve.l(s) / curve.t(s) / curve.step_t_ms / curve.inflection_t_ms / curve.note
```

- 输入非正（`ku / tu / k / l / t <= 0`）抛 `ValueError`，面板应捕获并提示。
- `ZNParams`：`mode / kp / ki / kd / ti / td / source`；`ti / td` 为 `None`
  表示该控制律不含此环节。
- 换算：`Ki = Kp / Ti`，`Kd = Kp · Td`，Ti / Td 单位为秒。

### 3.5 GUI 面板接口

| 面板 | 公共接口 | 说明 |
| --- | --- | --- |
| `PlotPanel` | `append(sample: TelemetrySample)` / `clear()` | 实时曲线追加与清空 |
| `ParamPanel(worker)` | `current_sp() -> float` | 取当前界面 SP，供其它面板组 `PID_SET` |
| `StepPanel(worker)` | `show_phase(phase: int)` / `append_sample(sample)` / `clear_samples()` | 命令路径 + 离线指标缓存 |
| `ZnPanel()` | 信号 `applied = Signal(float, float, float)` | 下发所选 `(Kp, Ki, Kd)`，SP 由主窗口从参数面板取 |
| `LogPanel(buffer)` | `log(message: str)` / `clear_buffer_signal()` | 日志与清空曲线按钮 |

主窗口帧分发在 `MainWindow._on_frame`：解包 `TELEMETRY` 后依次入缓冲、喂曲线、
喂阶跃面板；`PID_ACK` 回显到参数面板；`STEP_EVENT` 更新阶跃阶段；`ERROR` 入日志。

### 3.6 主题

`gui/theme.py` 集中维护 `COLORS` 配色与 `apply_dark(app)`。`serialpid.main`
在建窗前调用 `apply_dark(app)`；新增面板请从 `COLORS` 取色，不要硬编码十六进制。

## 4. 新增一个协议包类型

以新增 `0x40 MOTOR_STATUS` 为例：

1. **协议层 `protocol.py`**
   - `PacketType` 加 `MOTOR_STATUS = 0x40`；
   - 写 `pack_motor_status(...)` / `unpack_motor_status(...)`，遵循小端 /
     float32 小端约定；下发方向的封包拒绝 `NaN / inf`；
   - 在 `describe_frame` 里补可读描述。
2. **协议向量**：`tests/test_protocol.py` 加一条组帧 → 解包往返用例，并在
   `mcu/test/golden_vectors.h` 补同一字节向量，保证两侧逐字节一致。
3. **MCU 侧**：`pid_comm.h` 枚举加项，`pid_comm.c` 补编码 / 解码，`pid_app.c`
   补业务处理与上报。
4. **PC 链路**：`MainWindow._on_frame` 加分支解包 → 构造模型 / 调面板；
   若需要下发命令，在对应面板调用 `worker.send(PacketType.XXX, payload)`。
5. **文档**：在 [PROTOCOL.md](PROTOCOL.md) 帧表与载荷定义中补一行。

## 5. 测试

```powershell
# 安装开发依赖（含 pytest / pyinstaller / pillow）
pip install -r pc\requirements-dev.txt

cd pc
pytest tests -q
```

- `test_protocol.py`：编解码、CRC 标准向量、往返一致性。
- `test_step_test.py`：上升 / 下降沿、超调 / 上升时间 / 调节时间 / 不收敛、
  状态机跳变、CSV 导出。
- `test_zn_tuning.py`：闭环 / 开环已知公式值、非法输入、反应曲线辨识。
- `test_comm_loopback.py` / `test_integration_fake_mcu.py`：传输回环与模拟器联调。
- `test_gui_smoke.py`：离屏装配主窗口、喂合成遥测、触发 Z-N 计算与阶跃分析、
  清空通路。无显示器时设 `QT_QPA_PLATFORM=offscreen` 运行：

  ```powershell
  $env:QT_QPA_PLATFORM = "offscreen"
  python tests\test_gui_smoke.py
  ```

## 6. 打包内部结构

- **入口**：`pc/main.py` 是薄启动器，把 `pc/` 加入 `sys.path` 后转发到
  `serialpid.main`。`serialpid/main.py` 使用相对导入，不能直接当脚本入口。
- **模块路径**：PyInstaller 用 `--paths pc` 定位 `serialpid` 包。
- **随包资源**：`--add-data` 把 `assets/` 打进产物；Windows 数据分隔符为 `;`，
  Linux / macOS 为 `:`。
- **图标**：`tools/make_icon.py` 用 Pillow 生成多尺寸 `assets/app.ico` 与
  `assets/app.png`；打包前脚本会先调用一次。
- **版本资源**：`scripts/version_info.txt` 是 PyInstaller `--version-file` 读取的
  VSVersionInfo；版本号改动后请与 README、包内版本同步。
- **窗口模式**：`--windowed --onefile`，最终用户双击运行不弹控制台。
- **临时目录**：`--workpath` / `--distpath` 指向系统临时目录（ASCII 路径），
  打包完成后再把产物拷回仓库 `dist/`。部分环境下仓库路径含中文 / 非 ASCII 字符时，
  PyInstaller 持久化 PYZ 会失败（`FileNotFoundError: PYZ-00.pyz`），把中间产物
  放到 ASCII 临时目录可规避。
- **排除重依赖**：`--exclude-module` 排除 torch / torchvision / cv2 / tensorflow /
  transformers / scipy / matplotlib / pygame / onnxruntime / sympy / pandas /
  PIL 等运行时未使用的包；本程序运行时只需要 PySide6、pyqtgraph、pyserial、numpy
  与标准库。PIL 仅在构建期由 `make_icon.py` 使用，不进运行时产物。

一键脚本：

```powershell
scripts\build.bat          # Windows
bash scripts/build.sh      # Linux / macOS
```

产物在 `dist/`。`.gitignore` 忽略 `build/`、`dist/`、`*.exe`、`*.pkg`。
本项目**不使用 PyInstaller spec 作为构建入口**：onefile / windowed / 图标 / 版本资源 /
附加数据 / 模块搜索路径等参数全部内联在 `scripts/build.bat` / `build.sh` 里，避免脚本
与 spec 两处维护；命令行构建时 PyInstaller 会在工作目录自动落一份同名 `*.spec`，
属构建副产物（已在忽略规则内），不是需要维护的构建文件。

## 7. 已知边界与后续可做项

- 预设管理 `PresetManager` 已落地，但预设 UI 尚未接入主窗口（见
  [PC_TOOLS_API.md](PC_TOOLS_API.md) 第 4 节）。
- `log_panel.py` 与 `step_test.py` 目前各自手写 CSV，表头 / 小数位口径待统一到
  `csv_export.py`。
- 反应曲线自动辨识对噪声敏感，必要时把 `step_index` 显式传入，或增大
  `smooth_win`。
- 打包时若 PySide6 / pyqtgraph 出现缺插件，用 `--collect-submodules pyqtgraph`
  或对应 hidden import 补充；以本机 PyInstaller 实际版本提示为准。
