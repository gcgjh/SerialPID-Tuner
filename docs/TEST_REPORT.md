# 验收测试报告（v0.1 离线闭环）

- 报告日期：2026-11-05
- 验收对象：PC 上位机（协议 / 通信 / GUI / 算法工具）+ MCU 固件三模块
  （`pid_comm` / `pid_calc` / `pid_app`）+ 离线模拟器
- 验收方式：主机侧自动化测试（Python pytest + C gcc 运行器），无实物硬件接入

## 1. 测试环境

| 项 | 版本 / 说明 |
| --- | --- |
| 操作系统 | Windows（本机，pwsh 7） |
| Python | 3.13.16 |
| pyserial | 3.5 |
| numpy | 2.5.4 |
| PySide6 | 6.12.0 |
| pyqtgraph | 0.14.0 |
| pytest | 9.2.0 |
| 主机 C 编译器 | MinGW gcc 13.1.0（x86_64-win32-seh-rev1） |
| 交叉编译器（备选，未用于本报告） | arm-none-eabi-gcc 15.3.1 |
| 协议基准 | `docs/PROTOCOL.md` v1.0（冻结） |

## 2. PC 侧测试

命令：

```powershell
cd pc
python -m pytest tests/ -q
```

结果：**99 passed, 52 subtests passed**（耗时约 1.7s，0 失败 / 0 跳过）。

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_protocol.py` | CRC16-Modbus 标准向量（`123456789`→0x4B37 等）；组帧逐字节比对；载荷封解包；单帧解析；`FrameDecoder` 粘包 / 拆包 / 坏 CRC / 坏帧尾 / 超长帧重同步；CSV 导出；预设 JSON 读写 |
| `test_comm_loopback.py` | TCP 自收发闭环：连接、下发帧、回包解析、线程安全写、断流路径 |
| `test_integration_fake_mcu.py` | 对接 `tools/fake_mcu.py` 模拟器的全链路：连接 → 心跳 → 遥测上报 → `PID_SET` → `PID_ACK` 回显 → 阶跃命令 → 事件帧 |
| `test_step_test.py` | 超调量 / 峰值时间 / 上升时间 / 调节时间 / 稳态误差；单调无超调、永不收敛、下降阶跃；阶跃状态机跳转与完成；CSV 导出 |
| `test_zn_tuning.py` | 闭环临界振荡法、开环反应曲线法已知值比对；非法输入拒绝；反应曲线切线辨识 |
| `test_gui_smoke.py` | Qt 离屏冒烟：主窗口与各面板可构造、信号接线不报错、参数回调路径可走通 |

## 3. MCU 主机侧测试

编译标志：`-std=c11 -Wall -Wextra`，零告警。两个运行器均编译运行通过：

```powershell
pwsh mcu\test\run_host_test.ps1
```

### 3.1 `host_test`（协议层 + PID 算法层）

- **CRC**：4 组向量全部通过（空串→0xFFFF；`[02 10 00]`→0xC0DD；`00..0F`→0xE7B4；
  `123456789`→0x4B37）。
- **解析状态机**：11 个用例，覆盖单帧、双帧粘连、垃圾字节中夹带残留帧头、
  坏 CRC 后好帧、坏帧尾后好帧、250 字节最大载荷、LEN 溢出重同步、帧头重叠、
  遥测 + 心跳混流、空流、4096 字节 fuzz 混杂流（40 帧随机穿插垃圾字节）。
- **组帧真值**：PONG / PID_ACK / TELEMETRY / STEP_EVENT / ERROR 共 5 组，
  逐字节与 PC 冻结协议实现产出的黄金向量比对一致。
- **PID 算法**：输出全程有限；大偏差阶段输出顶到上限（抗饱和路径被覆盖）；
  闭环后 PV 向设定值靠近；最终输出在限幅内。

### 3.2 `app_host_test`（应用层 mock IO 冒烟）

注入内存 TX 缓冲、模拟一阶植物、回调存输出，走通：

- `PING` → `PONG`（类型 / 载荷 / 状态枚举正确）；
- `PID_SET`(2.0 / 0.5 / 0.1 / 30.0) → `PID_ACK` result=0，回显 Kp/Ki/Kd/SP；
- `STEP_START`(幅值 5，保持 1000ms) → `STEP_EVENT`(RUNNING)；
- 节拍推进 1.5s：保持结束回 `STEP_EVENT`(DONE)，期间持续上报遥测，阶跃结束退出开环；
- NaN Kp 参数 → `PID_ACK` BAD_PARAM，原参数不被改写。

### 3.3 打包产物验证（PyInstaller）

环境：PyInstaller 6.22.3、Pillow 13.0.0；不使用 spec 文件，onefile / windowed /
隐藏控制台 / 图标 / Windows 版本资源 / 附加数据目录 / 模块搜索路径等参数全部内联
在 `scripts/build.bat`（Linux / macOS 为 `scripts/build.sh`）。

- 命令：仓库根执行 `scripts\build.bat`
- 结果：构建成功，产物 `dist/SerialPID-Tuner.exe`，约 **52.5 MB**。
- 工作区副产物：命令行构建会在仓库根自动生成同名 `SerialPID-Tuner.spec`。它是
  PyInstaller 顺手写出的副产物，**不是**构建入口、也不入库（`.gitignore` 已忽略
  `*.spec`）；构建入口只有一键脚本。
- 启动冒烟：双击式拉起单文件 exe，进程存活 7 秒未崩溃，随后结束测试进程。
- 打包告警（`warn-SerialPID-Tuner.txt`）缺失项均为可选依赖：POSIX 专属模块
  （`fcntl`/`termios`/`pwd` 等，Windows 不适用）、`scipy`/`matplotlib`/`OpenGL`
  （pyqtgraph 可选后端，本程序走默认 2D 画布，未使用）；`serialpid` 自有模块无缺失。

## 4. 离线全链路结论

PC 上位机 ↔ TCP 模拟器（帧格式与串口一致）闭环在集成测试中通过：
连接、心跳、实时遥测、参数下发与回显确认、阶跃命令与事件、曲线指标计算、
CSV 导出路径均有自动化用例覆盖。MCU 三模块在主机侧按冻结协议与 PC 参考实现
行为一致，具备接实物前的离线验收基础。

## 5. 未覆盖项（验收缺口）

| 缺口 | 说明 | 计划 |
| --- | --- | --- |
| 实物硬件联调 | 当前未枚举到串口板卡；固件未经真机编译、烧录、上电实测 | v0.3 |
| 真机电气与时序 | UART 中断接收、ORE 溢出恢复、PWM / 传感器采样、长时间运行稳定性未在真机测量 | v0.3 |
| 交叉编译验证 | `main_example.c` / NVM 示例绑定 HAL 与 CubeMX 句柄，主机不编译；arm-none-eabi-gcc 可编译性待工程化后验证 | v0.3 |
| 压力与边界 | 高频遥测、长时运行内存增长、断线重连恢复未做长时间压力测试 | v0.2 / v0.3 |
| 打包产物实测 | 本机已实际出包并启动冒烟通过（见 3.3）；干净系统（无 Python 环境）首次运行、杀毒误报、长时驻留尚未验证 | v0.3 发布前 |

## 6. 二轮独立复验（card5 文档—代码一致性复核，2026-11-05）

复核原则：未实现的功能不写成已有；测试未跑绿不记“已修复”；每条结论可指到
具体实现文件 / 代码行 / 实测输出。本轮以磁盘现状为准重新取证，不沿用首轮结论。

### 6.1 复跑命令与结果

| 项 | 命令 | 实测结果 |
| --- | --- | --- |
| PC 全量回归 | `cd pc; python -m pytest tests/ -q` | **147 passed, 52 subtests passed**，约 3.60s，0 失败 / 0 跳过 |
| 断流线程生命周期专项 | `python -m pytest tests/test_comm_disconnect.py -v` | **3 passed**，约 1.78s |
| MCU 主机侧 | `pwsh mcu/test/run_host_test.ps1` | 两个运行器 `ALL HOST TESTS PASSED` / `ALL APP HOST TESTS PASSED`，零告警 |

### 6.2 本轮代码改动与回归证据

- **F-09 阶跃分析样本定长**：`pc/serialpid/gui/step_panel.py` 的 `_samples` 由
  无界 `List` 改为 `deque(maxlen=MAX_SAMPLES=20000)`，与 `TelemetryBuffer`、
  绘图队列上限一致。新增回归 `tests/test_ui_defects.py::test_step_panel_samples_bounded`：
  喂入 `MAX_SAMPLES+3` 点后长度恒为上限、最旧 3 点被淘汰、清空后归零。
- **断流空转复核（未复现）**：F-05 修复在位（`comm.py` `_run` 超时 / 读异常 /
  写异常路径均走 `_drop` 后 `return`；空读 20ms 节流；收尾身份比较；状态加锁）。
  断流专项 3 用例观测：超时掉线后接收线程退出、进程 CPU < 0.2s、5 次重连无
  线程增长、旧线程收尾不误关新连接。本轮未复现忙等空转。
- 复核其余内存结构：绘图队列 `plot_panel.py` 为 `deque(maxlen=20000)`；
  遥测缓冲 `model.py` 上限 20000；日志面板 `setMaximumBlockCount(2000)`，
  均已有界，未发现其它无界列表。

### 6.3 文档口径修正（不改运行行为）

- **打包口径**：出包链路为 `scripts/build.bat` / `scripts/build.sh` 的 PyInstaller
  命令行参数；本项目不以 spec 作为构建入口（文档所称的 `SerialPID-Tuner.spec`
  并非入库的构建文件，参数全部内联在脚本里，避免两处维护）。命令行构建会在工作
  目录自动落一份同名 `*.spec`，属构建副产物、不入库。早期设想的
  `tools/pack_release.py` 未落地，文档不再记为已有。
- **断流超时对齐**：`comm.py` 默认 `idle_timeout=1.0`，与 `PROTOCOL.md`、
  `TASKS.md` 的 1.0s 口径一致；未发现 1.5s 残留。
- **撤回未实现承诺**：原“PC 等待 `PID_ACK`/`PONG` 超时 500ms”无对应定时器
  实现，已从协议文档撤回，记为已知缺口 B-07；README 删除“链路统计”“帧日志
  回放”“曲线标注阶跃时刻”等未兑现表述。
- **失效引用修正**：固件周期配置原引用不存在的 `app_config.h`，改为
  `mcu/Inc/pid_app.h` 的 `PID_TELEMETRY_PERIOD_MS` 等宏。

### 6.4 二轮仍未覆盖

真机长时驻留、干净系统首次运行、杀毒误报、实物电气与时序测量仍排 v0.3，
见第 5 节缺口表与 `BUGS.md` P-01 ~ P-04。

## 7. 复现方式

```powershell
# PC 测试
cd pc
python -m pytest tests/ -q

# MCU 主机测试（需 gcc 在 PATH）
pwsh mcu\test\run_host_test.ps1

# 手动跑模拟器全流程（GUI）
python tools\fake_mcu.py --port 9000
cd pc; python -m serialpid.main
```
