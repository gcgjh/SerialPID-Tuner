# STM32H750VBT6 HAL 工程（SerialPID 真机首联用）

本目录是 `docs/bringup.md` §3 八步所需的**可烧录 HAL 工程**。它把仓库里零 HAL 依赖的
`pid_app` / `pid_calc` / `pid_comm` 与 `mcu/Src/main_example.c` 接到真实的 STM32H750VBT6 上。

## 1. 工程来源与版本（不要以为是 CubeMX 生成）

| 组件 | 来源 | 版本 |
|---|---|---|
| STM32H7xx HAL Driver | 本机 H750VB 开发板例程 `S:\单片机\STM32H750VBT6\...\HALLIB` | HAL V1.3.0 |
| CMSIS Core / 器件头 | 同上 + Keil `STM32H7xx_DFP 4.1.3` 的 `cmsis_gcc.h` | CMSIS 5.x |
| 启动文件 | `tools/gen_startup.py` 按 `stm32h750xx.h` 的 `IRQn_Type` 自动生成 | GCC 语法 |
| 链接脚本 | 本目录 `Core/Startup/STM32H750VBTx_FLASH.ld` | Flash 128 KB / AXI RAM 512 KB |

选本机例程而不是在线下载，是因为本机到 GitHub 的实际速率约 18 KB/s，完整 H7 包
（数百 MB）不可行；`S:\STM32CubeH7-1.13.0.zip` 是残缺文件，不要当可用包。

## 2. 板级口径（改接线只改这里）

| 功能 | 引脚 | 配置 |
|---|---|---|
| 串口 | PA9 / PA10 | USART1 AF7，115200 8N1，NVIC 抢占 3 |
| PWM 输出 | PB1 | TIM3_CH4 AF2，PSC=199 / ARR=999 → 1 kHz |
| 模拟输入 | PC0 | ADC1_IN10，12 位，单次软件触发，PCLK2/4 = 25 MHz |
| LED | PA1 | 推挽输出，高电平点亮 |

时钟：HSE 25 MHz → PLL1 M=5 / N=160 / P=2 → SYSCLK 400 MHz，HCLK 200 MHz，
APB1/APB2 100 MHz（定时器 200 MHz），APB4 50 MHz，VOS1，Flash latency 2。

> PWM 通道是 `main.h` 里的 `PID_EXAMPLE_PWM_CHANNEL`（本板 = `TIM_CHANNEL_4`）。
> `main_example.c` 默认 `TIM_CHANNEL_1`，仅影响通用模板；本工程已按板子覆盖。

## 3. 编译

```powershell
cd mcu\h7_board
pwsh -File .\build.ps1            # 仿真对象固件（默认，安全，先验协议/串口）
pwsh -File .\build.ps1 -RealIO    # 真实 IO 固件（TIM3 PWM + ADC1）
```

产物在 `build\`：`SerialPID-H750.elf / .hex / .bin / .map`。

- 仿真模式（`PID_DEMO_SIMULATED_PLANT=1`）：PV 由一阶惯性模型产生（K=0.45，T=8 s），
  OUT 只写内存，**不驱动 PB1**。用于首次烧录后验证 UART/协议/遥测，不会让执行器动。
- 真实 IO 模式（`PID_DEMO_SIMULATED_PLANT=0`）：OUT 写 TIM3_CH4，PV 读 ADC1_IN10。
  上真实执行器前，按 `docs/bringup.md` 第 1 步先开环确认方向与量程。

## 4. 烧录

```powershell
pwsh -File .\flash.ps1                  # 自动判断 DAPLink / J-Link
pwsh -File .\flash.ps1 -Interface jlink
```

脚本先用 OpenOCD（`interface/cmsis-dap.cfg` 或 `interface/jlink.cfg` + `target/stm32h7x.cfg`）
`program ... verify reset exit`，失败再退到 pyOCD（target `stm32h750xx`）。
**没有探针时脚本以退出码 2 结束并说明原因**，不会假装烧成功。

已验证环境：`arm-none-eabi-gcc 15.3.1`、`xpack-openocd 0.12.0`、`pyocd`（`S:\language environment\py\Scripts`）。

## 5. 烧完怎么确认跑起来了

1. 串口 `COM3`（CH340，`VID_1A86&PID_8011`）115200 8N1；
2. 发 PING `AA550001C07077777777`，应回 PONG（`fw_ver=0x02`，`status=1`）；
3. 用仓库脚本直接跑八步：

```powershell
cd ..\..
.\.venv\Scripts\python.exe tools\bringup_check.py --port COM3 --seconds 10 --out out\bringup_h750
```

## 6. 已知边界（不要当成已解决）

- **NVM 未挂**：H750 内部 Flash 只有 128 KB 且单扇区，擦除会连代码一起擦。
  `PID_Example_Init()` 里 `nvm_load/nvm_save` 仍为 `NULL`，八步第 8 步在本工程上
  **硬件条件不具备**；要做得外挂 QSPI/W25Q 或在 RAM 里做掉电即失的参数。
  `docs/bringup.md` 里 F103/F407/H743 的 NVM 地址对 H750 都不适用。
- **ADC 量程**：本工程用 12 位（4095），开发板例程用 16 位；`main_example.c` 默认
  `PID_EXAMPLE_PV_SCALE=100/4095`，接真实传感器必须按你的调理电路重算。
- **HAL 是 V1.3.0**（本机例程自带），不是 ST 最新 1.11.x；寄存器差异不影响本工程用到的
  UART/TIM/ADC/GPIO/RCC，但后续用新外设要留意。
- **烧录探针**：WCH-Link（VID_1A86&PID_8011）也能当 CMSIS-DAP 用；普通 CH340 只有串口时
  无法 SWD 烧录。`flash.ps1` 会按 BusReportedDeviceDesc 区分 WCH-Link 与普通 CH340。

## 7. 本次烧录记录（2026-11-07，实测）

- 探针：WCH-Link（VID_1A86&PID_8011，BusReported `WCH-Link`，CMSIS-DAP FW 2.0.0）；
  OpenOCD 识别 `Cortex-M7 r1p1`，SWD DPIDR `0x6ba02477`。
- 固件：SIM 模式（`PID_DEMO_SIMULATED_PLANT=1`），Flash 16,900 B / 128 KB，RAM 17,520 B。
- 烧录：`pyocd flash -t stm32h750xx` 成功；随后 OpenOCD `verify_image` 回读校验
  `verified 16900 bytes`，与 ELF 一致。
- 运行：COM3 115200 连续 3 s 收到 62 帧 TELEMETRY（约 20.6 帧/s），`t_ms` 递增，
  SP/PV/OUT 均为 0（仿真对象未给阶跃，符合预期）。
- **未通过项**：发 PING（原始黄金帧 `AA550001C07077777777`）没有 PONG。
  SWD 读回 `USART1->CR1 = 0x0000812D`（UE/RE/TE/RXNEIE 全开）、`CR3 = 1`（EIE 开），
  说明 MCU 侧接收已使能；但 PA10 没有收到任何字节。下一步要查：
  WCH-Link/CH340 的 TXD 是否真的接到 PA10、两端 GND 是否共地。
  在 RX 通之前，八步第 2 步（PING/PONG）不能判定通过。

