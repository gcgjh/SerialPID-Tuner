# 任务分工与接口契约（主 Agent 统筹）

> 协作模式：主 Agent 分配任务、核对接口、验收产出；4 个 AI 按卡片交付。
> 所有跨模块接口以本文件与 `PROTOCOL.md` 为准；字节布局冻结，禁止私改。

## 依赖关系

```
AI-1 协议/通信层 ──┬──> AI-2 GUI 业务层 ──> AI-4 打包/文档
                   │            ▲
AI-3 MCU 固件 ─────┴── 协议对齐 ┘
AI-4 tools/fake_mcu.py 为 AI-2 提供离线闭环（TCP 传输，帧格式与串口一致）
```

## 固定接口约定

- PC 侧协议唯一实现：`pc/serialpid/protocol.py`（封包/解包/CRC/载荷辅助函数）。
- 通信层对外只暴露：帧类型 + 原始载荷字节；字段解析在 `model.py` / GUI 层完成。
- 传输层可替换：`SerialTransport`（实物）与 `TcpTransport`（模拟器/测试）实现同一接口。
- 线程模型：通信线程独占串口/socket；UI 线程只通过信号/队列接收帧，禁止跨线程直接画控件。

---

## AI-1 协议与通信层（PC）

**输入**：`docs/PROTOCOL.md`、`serialpid/protocol.py`（已冻结骨架）。

**产出**：

| 文件 | 职责 |
| --- | --- |
| `pc/serialpid/comm.py` | 通信线程：打开/关闭连接、端口枚举、收发调度、断流检测、线程安全写 |
| `pc/serialpid/model.py` | `PidParams`、`TelemetrySample` 数据结构；遥测环形缓冲 |
| `pc/serialpid/transport.py` | `Transport` 抽象 + `SerialTransport` + `TcpTransport` |
| `pc/tests/test_comm_loopback.py` | TCP 自收发闭环测试 |

**对外接口（GUI 只能依赖这些）**：

- `CommWorker`（QThread 或线程 + 回调）
  - `open(port: str, baud: int = 115200)` / `close()`
  - `send(pkt_type: int, payload: bytes = b"")`
  - 信号：`frame_received(int type, bytes payload)`、`connection_changed(bool connected, str detail)`、`error_occurred(str msg)`
- `list_ports() -> list[str]`
- 模型：`PidParams(kp, ki, kd, sp)`；`TelemetrySample(t_ms, sp, pv, out, err)`；环形缓冲 `append()` / `samples()` / `clear()`。

**验收标准**：

1. `test_protocol.py` 全过；TCP 自收发闭环通过。
2. 粘包/拆包/CRC 错帧/帧尾错帧均能重同步，不抛异常到 UI 层。
3. 断流 1 s 触发 `connection_changed(False, "timeout")`。
4. 不 import 任何 GUI 控件，可在无显示环境独立测试。

## AI-2 GUI 业务层（PC）

**输入**：AI-1 的 `comm.py` / `model.py` / `protocol.py`；AI-4 的 `fake_mcu.py`。

**产出**：

| 文件 | 职责 |
| --- | --- |
| `pc/serialpid/gui/main_window.py` | 主窗口：端口/地址选择、连接按钮、状态栏（连接态、遥测频率、告警） |
| `pc/serialpid/gui/plot_panel.py` | pyqtgraph 实时曲线：SP / PV / OUT；暂停/继续、清空、时间窗下拉、纵轴自动；内置 20000 点定长队列 |
| `pc/serialpid/gui/param_panel.py` | Kp/Ki/Kd/SP 输入与下发；`PID_ACK` 回显；越界拒绝提示 |
| `pc/serialpid/gui/log_panel.py` | 事件日志表；CSV 导出（`t_ms,SP,PV,OUT,err`） |
| `pc/serialpid/gui/step_panel.py` | 阶跃幅值/保持时长；开始/停止；`STEP_EVENT` 状态显示 |
| `pc/serialpid/main.py` | 组装入口，启动应用 |

**验收标准**：

1. 连 `fake_mcu` 可完成：连接 → 看曲线 → 改参数下发并收到 ACK → 触发阶跃 → 导出 CSV。
2. 断线后 UI 不卡死；重连后曲线缓冲不串数据。
3. 端口/波特率可在界面配置；曲线时间窗可在曲线面板下拉选择。
4. CSV 为 UTF-8（带 BOM，Excel 直开不乱码），首行表头。

## AI-3 MCU 固件（STM32）

**输入**：`docs/PROTOCOL.md`（含三个对齐向量）；PID 控制需求。

**产出**：

| 文件 | 职责 |
| --- | --- |
| `mcu/Inc/pid_comm.h` / `mcu/Src/pid_comm.c` | 帧解析/封包、CRC16-Modbus、环形缓冲、命令分发钩子 |
| `mcu/Inc/pid_calc.h` / `mcu/Src/pid_calc.c` | 位置式 / 增量式 PID：输出限幅、抗积分饱和、无扰切换 |
| `mcu/Src/pid_app.c` + `mcu/Inc/pid_app.h` | 帧分发、10ms 控制节拍、50ms 遥测、阶跃辨识、参数延迟落 Flash；控制 / 遥测周期、阶跃上限等可配置宏（`PID_CTRL_PERIOD_MS` 等） |
| `mcu/Src/main_example.c` | HAL 接线示例：UART 中断、时基、PWM 占位、可选仿真植物（主机不编译） |
| 构建脚本（CMakeLists / PlatformIO `platformio.ini`） | 待硬件选型拍板后补；芯片型号待确认后锁定 |

**验收标准**：

1. CRC 自测：`crc16_modbus("123456789") == 0x4B37`；三个协议向量解析/封包一致。
2. PID 离线仿真（Python 对照）：限幅内无积分饱和漂移；饱和退出后不反向深超调。
3. 收到 `PING` 回 `PONG`；收到 `PID_SET` 回 `PID_ACK`；周期上报 `TELEMETRY`。
4. 软复位命令 `0x71` 可在不拔线情况下重启固件。
5. **待确认**：具体 MCU 型号、晶振、串口引脚、执行器（PWM/继电器）与传感器接口，实物接入前按预留接口实现。

## AI-4 文档 / 打包 / 工具

**输入**：AI-1/2/3 全部产出。

**产出**：

| 文件 | 职责 |
| --- | --- |
| `README.md` | 功能概览、安装、连接步骤、目录结构、协议链接、开发指南、截图位 |
| `pc/requirements.txt` | 运行与开发依赖锁定 |
| `tools/fake_mcu.py` | 离线模拟器：TCP 监听，响应 PING/PID_SET/SP_SET，周期发 TELEMETRY，支持阶跃 |
| `scripts/build.bat` / `build.sh` | 一键打包：PyInstaller 命令行参数直接构建 onefile/windowed 产物，带版本资源与图标 |
| `docs/CHANGELOG.md` | 版本变更记录 |

**验收标准**：

1. 干净虚拟环境按 README 安装成功并启动。
2. `fake_mcu` 可被 GUI 通过 TCP 连接，全流程可演示。
3. 打包产物在无 Python 环境的机器上可启动连接。
4. 协议变更同步更新 `PROTOCOL.md` 与 CHANGELOG。

---

## 主 Agent 验收记录（持续更新）

| 日期 | 事项 | 状态 | 备注 |
| --- | --- | --- | --- |
| 2026-11-05 | 协议基准 v1.0 冻结，PC 协议层单测通过 | ✅ | 三个测试向量见 PROTOCOL.md 第 5 节 |
| 2026-11-05 | PC 通信层 / GUI / 模拟器 / 算法工具联调 | ✅ | `pytest tests/ -q`：99 passed, 52 subtests passed |
| 2026-11-05 | MCU 协议层 / PID 算法层 / 应用层主机侧测试 | ✅ | `host_test` + `app_host_test` 全部通过，`-Wextra` 零告警 |
| 2026-11-05 | 打包链路出包与启动冒烟 | ✅ | `dist/SerialPID-Tuner.exe` 约 52.5 MB，拉起 7 秒未崩溃；干净环境复测留 v0.3 |
| 2026-11-05 | 交付文档：README / CHANGELOG / TEST_REPORT / BUGS / wiring | ✅ | 见仓库根 `README.md` 与 `docs/` |
| 2026-11-05 | 二轮文档—代码一致性复核 | ✅ | `pytest tests/ -q`：147 passed, 52 subtests；断流专项 3 passed；MCU 两运行器通过（见 TEST_REPORT 第 6 节） |
| 2026-11-05 | MCU 具体型号 / 传感器 / 执行器待硬件确认 | ⏳ | 当前未枚举到串口设备；实物联调排 v0.3 |

> 实现与早期任务卡文件名的偏差：MCU 实际拆为 `pid_comm` / `pid_calc` / `pid_app`
> 三模块；打包由 `scripts/build.bat` / `build.sh` 的 PyInstaller 命令行参数完成
> （本项目不以 spec 作为构建入口），替代早期设想的 `tools/pack_release.py`。
> 接口契约与验收标准仍按本文件第 1–4 节执行。
