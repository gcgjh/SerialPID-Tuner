# SerialPID-Tuner

开源串口 PID 调试上位机 + 配套 STM32 固件：PySide6 + pyqtgraph + pySerial 上位机，
经二进制串口协议与 MCU 通信，支持实时曲线、PID 参数下发与回显、阶跃响应测试、
Ziegler-Nichols 辅助整定、CSV 曲线导出；MCU 侧提供零 HAL 依赖的协议层 / PID 算法层 /
应用层，以及 STM32 HAL 集成示例。

> 协议字节布局已冻结，见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)（v1.0）。
> 当前为 **v0.1 离线闭环验收版**：PC 全链路 + MCU 主机侧离线测试通过；
> 实物烧录联调列入 v0.3。

## 功能

- **稳定通信**：串口（115200 8N1）/ TCP 双传输抽象；粘包、拆包、错帧、坏 CRC、
  超长帧自动重同步；空闲超时 1.0 s 判定断流并回调链路状态。
- **实时绘图**：SP / PV / OUT 同屏滚动，误差随行记录。
- **参数下发**：Kp / Ki / Kd / SP 经 `PID_SET` 下发，固件回 `PID_ACK` 回显并确认；
  NaN / Inf / 越界参数拒绝并回错误码。
- **阶跃测试**：上位机配置幅值与保持时长，自动辨识超调量、峰值 / 峰值时间、
  10%–90% 上升时间、调节时间、稳态误差。
- **Z-N 辅助整定**：临界振荡法 / 反应曲线法两套经典公式，纯 PC 侧计算，
  选定后经既有 `PID_SET` 帧下发。
- **日志导出**：遥测曲线导出 CSV（UTF-8 带 BOM，Excel 直开）；日志面板在内存保留最近事件记录。
- **离线模拟器**：无硬件时用 TCP 模拟器（一阶惯性植物 + 简易 PID）跑通全流程。
- **深色主题**：内置统一配色的深色界面，曲线颜色与面板配色集中在 `gui/theme.py`。

## 快速开始

### 源码运行（开发 / 调试）

要求 Python 3.10+（开发环境实测 3.13）。

本机开发环境建在**仓库的上一层**（`串口PID/.venv`，依赖已装好），从仓库根直接激活：

```powershell
# 仓库根目录
..\.venv\Scripts\activate          # Windows；Linux/macOS: source ../.venv/bin/activate
```

干净克隆或想自建环境时，虚拟环境位置不限，建好激活后安装依赖即可：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r pc\requirements.txt
```

启动上位机（在 pc/ 目录下以模块方式运行）：

```powershell
cd pc
python -m serialpid.main
```

### 离线体验（无需硬件）

```powershell
# 终端 1：启动 TCP 模拟器（默认 127.0.0.1:9000）
python tools\fake_mcu.py --port 9000

# 终端 2：启动上位机，连接方式选 TCP，地址 127.0.0.1:9000
cd pc
python -m serialpid.main
```

### 打包发行版

Windows：

```powershell
scripts\build.bat
# 产物：dist\SerialPID-Tuner.exe（单文件、无控制台、带图标与版本信息）
```

Linux / macOS：

```bash
bash scripts/build.sh
# 产物：dist/SerialPID-Tuner
```

打包依赖见 [`pc/requirements-dev.txt`](pc/requirements-dev.txt)（追加 pyinstaller / pillow / pytest）。

构建脚本会优先使用项目环境（`../.venv`）里的解释器，找不到才回退 `PATH` 中的 `python`，
并在日志第一行打印实际使用的解释器路径；依赖已装好时跳过安装，不污染全局环境。

## 测试

### PC 侧离线测试

```powershell
cd pc
python -m pytest tests/ -q
```

覆盖：协议编解码与 CRC 标准向量、TCP 自收发闭环、阶跃分析、Z-N 整定、
模拟器集成闭环、GUI 离屏冒烟。

### MCU 侧主机测试（无需开发板 / 无需 HAL）

协议层与 PID 算法层零 HAL 依赖，可在主机用 gcc 直接编译运行，真值来自 PC 侧冻结协议：

```powershell
# Windows（MinGW gcc）
pwsh mcu\test\run_host_test.ps1

# 或手动编译
cd mcu\test
gcc -std=c11 -Wall -Wextra -I../Inc -I. host_test.c     ../Src/pid_comm.c ../Src/pid_calc.c     -o host_test.exe
gcc -std=c11 -Wall -Wextra -I../Inc -I. app_host_test.c ../Src/pid_comm.c ../Src/pid_calc.c ../Src/pid_app.c -o app_host_test.exe
.\host_test.exe
.\app_host_test.exe
```

黄金向量由 PC 协议实现生成，改动协议后重新生成：

```powershell
python tools\gen_protocol_vectors.py
```

## 仓库结构

```
SerialPID-Tuner/
├── LICENSE                      # MIT 许可证
├── pc/                          # 上位机
│   ├── main.py                  # 薄启动器（PyInstaller 入口）
│   ├── requirements.txt         # 运行依赖
│   ├── requirements-dev.txt      # 开发 / 打包 / 测试依赖
│   ├── tests/                    # 离线单测 + GUI 离屏冒烟 + 模拟器集成
│   └── serialpid/
│       ├── protocol.py           # 冻结协议编解码 / CRC / 载荷封解包
│       ├── transport.py          # Serial / TCP 传输抽象
│       ├── comm.py              # 通信线程核心
│       ├── comm_qt.py            # Qt 信号适配
│       ├── model.py              # 参数 / 采样 / 环形缓冲
│       ├── step_test.py          # 阶跃响应分析 + 自动测试状态机
│       ├── zn_tuning.py          # Z-N 整定公式与反应曲线辨识
│       ├── csv_export.py         # CSV 导出
│       ├── config.py             # 参数输入范围配置（config.json 读写与回落）
│       ├── frame_log.py           # 未知类型帧聚合计数日志
│       ├── preset_manager.py     # PID 预设 JSON 管理
│       └── gui/                  # 主窗口 / 曲线 / 参数 / 阶跃 / Z-N / 日志面板
├── mcu/                         # 固件（协议层 / 算法层 / 应用层 + HAL 示例）
│   ├── Inc/  Src/
│   │   ├── pid_comm.*            # 协议解析 / 组帧 / CRC / 环形缓冲（无 HAL）
│   │   ├── pid_calc.*            # 位置式 / 增量式 PID、抗饱和、微分先行（无 HAL）
│   │   ├── pid_app.*             # 帧分发 / 控制节拍 / 遥测 / 阶跃 / 掉电保存（无 HAL）
│   │   ├── main_example.c        # STM32 HAL 接线示例（UART 中断 + 时基 + PWM 占位）
│   │   └── pid_nvm_stm32_example.c  # Flash 参数保存示例（F1/F4/H7 分支）
│   └── test/                     # 主机侧 C 测试运行器 + 黄金向量
├── tools/
│   ├── fake_mcu.py               # 离线 TCP 模拟器
│   ├── gen_protocol_vectors.py   # 从 PC 协议生成 MCU 黄金向量
│   └── make_icon.py              # 生成应用图标
├── scripts/                      # 一键打包脚本与 Windows 版本资源
├── assets/                       # 图标等随包资源
└── docs/
    ├── PROTOCOL.md               # 冻结协议基准 v1.0
    ├── user_guide.md             # 上位机使用手册
    ├── development.md            # 二次开发与分层 / 线程模型
    ├── PC_TOOLS_API.md           # PC 侧函数级接口说明
    ├── wiring.md                 # 硬件接线与 IO 回调映射
    ├── ITERATION.md              # 迭代路线与风险
    ├── TEST_REPORT.md            # 验收测试报告
    ├── BUGS.md                   # 已知问题与限制清单
    └── CHANGELOG.md              # 变更记录
```

## MCU 移植要点

- 三个核心模块（`pid_comm` / `pid_calc` / `pid_app`）**不依赖 HAL / CMSIS / RTOS**，
  硬件差异全部经 `pid_app_io_t` 回调注入：`write_output` / `read_pv` / `send_frame` /
  `nvm_load` / `nvm_save` / `system_reset`。
- 接收路径：UART 中断（或 DMA + IDLE）只把字节推入环形缓冲；解析、控制、组帧、发送
  全部在主循环，中断内不阻塞、不打印、不等锁。
- 时间基准由调用方以毫秒传入 `pid_app_tick(app, now_ms)`；内部节拍用无符号减法，
  32 位回绕安全。
- 具体芯片型号、传感器接口、执行器（PWM / 继电器 / 半桥）与引脚分配待硬件确认，
  示例中以预留接口和仿真植物占位，见 [`docs/wiring.md`](docs/wiring.md)。

## 路线图

- **v0.1 基础闭环**：协议冻结、PC 通信 / GUI / 模拟器、MCU 协议与算法主机测试 —— 当前。
- **v0.2 调参增强**：预设保存 / 加载接入界面、曲线交互增强、链路质量统计、错误日志落盘。
- **v0.3 发布版**：实物烧录联调、打包验收、截图 / 接线图完善、Release 发布。

详见 [`docs/ITERATION.md`](docs/ITERATION.md)。

## 许可

本项目采用 **MIT License**，全文见仓库根目录 [`LICENSE`](LICENSE)；
Windows 版本资源（`scripts/version_info.txt`）中的 `LegalCopyright` 与之一致。

第三方依赖（PySide6 / pyqtgraph / pySerial / numpy 等）分别遵循其自身许可证。
