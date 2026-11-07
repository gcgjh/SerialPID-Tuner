# 硬件接线与示例网表

> 本文件描述上位机 ↔ MCU 的串口接线，以及 MCU 侧控制回路的预留接口。
> 协议物理层参数（115200 8N1、3.3V TTL）见 [PROTOCOL.md](PROTOCOL.md)；
> 固件集成示例见 `mcu/Src/main_example.c`。

## 1. 链路总览

```
┌──────────────┐   3.3V TTL UART    ┌──────────────────┐   USB   ┌────────────┐
│   STM32 MCU  │ ─────────────────▶ │  USB-TTL 转换器  │ ──────▶ │  PC 上位机  │
│  USART1      │ ◀───────────────── │  (CH340/CP2102/  │         │            │
│              │                     │   FT232)         │         │            │
└──────────────┘                     └──────────────────┘         └────────────┘
```

- 通信总线：UART，3.3V TTL 电平，115200 8N1，无流控。
- 上位机也可走 TCP 连接离线模拟器 `tools/fake_mcu.py`，该方式无需任何接线。

## 2. 串口接线表（USART1 示例）

下表为 STM32 典型 USART1 映射（F1/F4 常见 PA9=TX、PA10=RX）；**具体引脚以你的
CubeMX USART1 配置为准**，换用其它串口时对应替换即可。

| STM32 引脚 | 方向 | 接到 USB-TTL 引脚 | 说明 |
| --- | --- | --- | --- |
| USART1_TX（如 PA9） | 输出 → | TTL 转换器 **RXD** | MCU 发送 → 转换器接收，交叉连接 |
| USART1_RX（如 PA10） | 输入 ← | TTL 转换器 **TXD** | 转换器发送 → MCU 接收，交叉连接 |
| GND | 共地 | 转换器 **GND** | 必须连接，否则电平参考不一致、数据乱码 |
| 3.3V | 按需 | 转换器 **3V3 / VCC** | 仅当由 MCU 给转换器供电时接；不要把 5V 接进 MCU 引脚 |

```
STM32 PA9 (TX)  ────────▶  转换器 RXD
STM32 PA10(RX)  ◀────────  转换器 TXD
STM32 GND       ─────────  转换器 GND
```

### 2.1 电平与供电注意

- STM32 IO 为 **3.3V** TTL，禁止直连 RS232 口（±12V 电平），也不要直连
  5V TTL 串口器件，必要时加电平转换。
- 转换器若标注 5V/3.3V 可选，拨到 **3.3V** 一侧。
- 上位机与 MCU 必须**共地**；只接 TX/RX 不接 GND 是最常见的乱码原因之一。
- 执行器 / 传感器电源与 MCU 电源分开供电时，两地之间仍需连一根共地线。
- 上电顺序：先接好线再上电；确认 TX/RX 不接反，避免 MCU 接收脚被灌电流。

## 3. 控制回路预留接口

上位机只观测曲线、下发参数，**闭环 PID 运算在 MCU 内完成**。固件示例预留了
两路 BSP 接口（`main_example.c`）：

| 回路 | MCU 外设（示例） | 方向 | 接到对象 |
| --- | --- | --- | --- |
| 执行器输出 | TIM3 PWM（示例 CH1，ARR=999，1kHz） | MCU → 驱动电路 | 加热丝 / 电机 / 半桥 / 继电器等 |
| 被控量反馈 | ADC 采样 | 传感器 → MCU | 温度 / 转速 / 位置 / 液位等传感器 |

### 3.1 输出通路（`bsp_write_output`）

- 输入为 0–100% 的归一化输出，应用层已限幅；BSP 层再兜一层 0–100% 限幅。
- PWM 示例：`CCR = out_percent × 9.99`（ARR=999）。
- 驱动加热丝 / 半桥时，在 BSP 层处理极性、死区与安全上限；感性负载注意续流。

### 3.2 反馈通路（`bsp_read_pv`）

- ADC 原始值先做物理量标定（线性换算或查表）。
- 建议加一阶低通滤波，抑制采样噪声；断线 / 超量程时返回 `NaN`，应用层会
  保持上次输出。
- 传感器量程要覆盖工作区间，否则阶跃指标与稳态误差会被削顶失真。

### 3.3 仿真对象（无硬件联调）

`main_example.c` 中 `PID_DEMO_SIMULATED_PLANT=1` 时，PV 由一阶惯性仿真对象
（K=0.45，T=8s）产生，可在没有执行器 / 传感器时跑通上位机联调；接真实硬件
时把宏置 0，并替换两路 BSP 实现。

## 4. 建议物料（BOM）

| 类别 | 物料 | 备注 |
| --- | --- | --- |
| 主控 | STM32 开发板（F103 / F4 等） | 按固件工程与外设资源选择 |
| 串口转换器 | CH340 / CP2102 / FT232，3.3V 电平 | 带 3.3V 电平选择更佳 |
| 连线 | 杜邦线 / 排针若干 | TX-RX 交叉、GND 共地 |
| 被控对象 | 加热体 / 电机 / 水泵 / 温度箱等 | 按整定目标选择 |
| 驱动 | MOS 管 / 半桥 / 继电器 / 固态继电器 | 与执行器电压电流匹配 |
| 反馈传感器 | 温度 / 转速 / 位置 / 液位传感器 | 输出经调理后可接 ADC |
| 电源 | MCU 电源 + 执行器电源 | 功率回路与逻辑回路分开，共地 |

## 5. 接线检查清单

- [ ] USART1 波特率 115200、8 数据位、无校验、1 停止位，与上位机一致。
- [ ] TX ↔ RX 交叉，RX ↔ TX 交叉。
- [ ] GND 已连接（MCU、转换器、执行器电源三地共地）。
- [ ] IO 电平为 3.3V，未直连 RS232 / 5V 器件。
- [ ] 执行器驱动与传感器调理已接好，量程覆盖工作区间。
- [ ] 固件接收中断已开启；ORE 溢出错误回调里 Abort + 重挂接收（见示例
      `HAL_UART_ErrorCallback`），避免“能发不能收”。
- [ ] 先用模拟器（TCP 127.0.0.1:9000）确认上位机链路正常，再接实物串口。
- [ ] 改过 HAL 集成示例后跑一次 `scripts/verify_mcu_build.ps1`（见第 7 节），
      确认示例仍能编过、且没有引入告警。

## 6. 常见接线问题

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| 连接后完全无数据 | TX/RX 接反；GND 未接；串口号 / 波特率错误 | 交叉互换 TX/RX；补共地；核对端口与波特率 |
| 收到乱码字节 | 波特率不一致；电平不匹配；共地不良；线过长 | 统一 115200；换 3.3V 转换器；补共地；缩短杜邦线 |
| 能下发、收不到回包 | 固件未开接收中断；接收被 ORE 打断；RX 未接 | 检查中断与错误回调；核对 RX 接线 |
| 曲线削顶 / 饱和 | 传感器量程不足或输出限幅 | 标定量程；检查执行器与驱动上限 |
| 模拟器正常、实物不通 | 只验了 TCP，串口硬件链路未通 | 按第 5 节清单逐项排查 |

## 7. HAL 集成示例：符号清单与编译验证

两份示例（`mcu/Src/main_example.c`、`mcu/Src/pid_nvm_stm32_example.c`）**不是**库的一部分，
它们是"照抄进 CubeMX 工程"的接线图。也正因如此，它们以前不在仓库的编译验证范围里——
一旦 HAL 宏改名、函数签名变化，用户照抄时会卡在编译错误上，还以为是自己的工程配置坏了。
本节把这件事闭合：示例进入每次回归的 ARM 编译验证，并把"需要哪些 HAL 符号"列清楚。

### 7.1 一份示例、两种编译环境

| 环境 | 头文件来源 | 怎么触发 |
| --- | --- | --- |
| 你的 CubeMX 工程 | CubeMX 生成的 `main.h`（内含真实 HAL） | 什么都不用定义，粘进去即可 |
| 本仓库回归 | `mcu/stubs/stm32_hal_stub.h`（**只有声明与类型，没有实现**） | 脚本传 `-DPID_HAL_STUB=1` |

文件头里有三态宏 `PID_HAL_STUB`：不定义=自动探测（有 `main.h` 就用真实的，没有就用桩），
`1`=强制桩，`0`=强制真实 HAL。桩环境**只用于编译**，不能链接、不能运行——它刻意不提供任何
函数体，就是为了避免"示例看起来跑通了"的错觉。

### 7.2 逐项核对清单（对着你的 CubeMX 工程勾）

| 符号 | 类别 | 示例中的用途 |
| --- | --- | --- |
| `USART1` | 实例宏 | 判断 `huart->Instance` 是哪个串口 |
| `UART_HandleTypeDef` / `TIM_HandleTypeDef` | 类型 | 外设句柄 |
| `HAL_UART_Transmit` | 函数 | BSP 发帧（轮询，20ms 超时） |
| `HAL_UART_Receive_IT` | 函数 | 挂 1 字节接收中断 |
| `HAL_UART_AbortReceive_IT` | 函数 | 错误回调里打断接收状态机后重挂 |
| `__HAL_UART_CLEAR_OREFLAG` / `NEFLAG` / `FEFLAG` | 宏 | 清溢出/噪声/帧错误标志 |
| `HAL_GetTick` | 函数 | **默认毫秒时基**（SysTick） |
| `TIM6` + `HAL_TIM_Base_Start_IT` | 实例宏 / 函数 | 仅 `PID_EXAMPLE_USE_TIM6=1` 时需要 |
| `HAL_FLASH_Unlock` / `Lock` | 函数 | NVM 擦写临界区 |
| `HAL_FLASHEx_Erase` + `FLASH_EraseInitTypeDef` | 函数 / 类型 | 擦页（F1）或擦扇区（F4/H7） |
| `HAL_FLASH_Program` | 函数 | 写半字 / 字 / 32 字节 flashword |
| `HAL_FLASH_Unlock` 相关 `FLASH_FLAG_*` | 宏 | 擦之前清错误标志 |
| `MX_USART1_UART_Init` / `MX_GPIO_Init` / `MX_TIM6_Init` / `MX_TIM3_Init` / `SystemClock_Config` / `Error_Handler` | 函数 | CubeMX 生成的初始化入口（示例只调用，不定义） |

> 说明：`MX_TIM6_Init` 只有在你启用 TIM6 时基时才需要；默认走 SysTick 的版本不需要它。

### 7.3 三族差异（示例里的分支就是按这张表选的）

| 项目 | F1 / F0 / L0 | F4 / F3 | H7 |
| --- | --- | --- | --- |
| 编程单位宏 | `FLASH_TYPEPROGRAM_HALFWORD` | `FLASH_TYPEPROGRAM_WORD` | `FLASH_TYPEPROGRAM_FLASHWORD` |
| 擦除方式 | `FLASH_TYPEERASE_PAGES`（页） | `FLASH_TYPEERASE_SECTORS`（扇区）+ `FLASH_VOLTAGE_RANGE_3` | `FLASH_TYPEERASE_SECTORS` + `Banks` |
| 写保护错误标志 | `FLASH_FLAG_WRPERR`（寄存器位叫 `SR_WRPRTERR`，HAL 宏不叫 `WRPRTERR`） | `FLASH_FLAG_WRPERR` | `FLASH_FLAG_ALL_ERRORS` |
| 示例默认存放区 | `0x0801FC00`（末页 1KB） | `0x080E0000`（Sector 11） | `0x081E0000`（Sector 7 Bank2） |
| 1ms 硬件定时器 | F103C8 只有 TIM1~TIM4，**没有 TIM6** | TIM6 可用 | TIM6 可用 |

分支判据按"擦除单位"选族（`FLASH_TYPEERASE_PAGES` → F1；`FLASH_TYPEERASE_SECTORS` +
`FLASH_TYPEPROGRAM_WORD` → F4；`FLASH_TYPEPROGRAM_FLASHWORD` → H7）。**不要**用
`FLASH_TYPEPROGRAM_HALFWORD` 当判据：F4 也定义了这个宏，会把 F4 误判成 F1，编不过。

### 7.4 验证矩阵（本轮实测，脚本共 15 项）

| 构建项 | 编译器参数 | 结果 |
| --- | --- | --- |
| 核心三模块 `pid_comm` / `pid_calc` / `pid_app` | `cortex-m4 -mthumb -O2 -Wall -Wextra` | 通过，零告警 |
| `main_example.c` × 桩 F1/F4/H7 | `cortex-m3/m4/m7 -O2 -Wall -Wextra` | 通过，零告警 |
| `pid_nvm_stm32_example.c` × 桩 F1/F4/H7 | 同上 | 通过，零告警 |
| `main_example.c`（`PID_EXAMPLE_USE_TIM6=1` 分支） | `cortex-m3` 桩 | 通过，零告警 |
| `main_example.c` / `pid_nvm_stm32_example.c` | **真实 F1 HAL**（STM32Cube_FW_F1_V1.8.7，`STM32F103xB`） | 通过，零告警 |
| `main_example.c` / `pid_nvm_stm32_example.c` | **真实 F4 HAL**（STM32Cube_FW_F4_V1.28.3，`STM32F407xx`） | 通过，零告警 |
| `main_example.c`（`PID_EXAMPLE_USE_TIM6=1` 分支） | 真实 F4 HAL | 通过，零告警 |
| 真实 H7 HAL | 本机无 STM32Cube_FW_H7 包 | **未验证**：只有桩级别的语法/类型检查 |
| 上板实跑（擦写 Flash、UART 中断、PWM） | — | **未验证**：无实物板，属硬件待验项 |

**诚实边界**：桩验证能拦住语法、类型、宏改名、函数签名漂移；拦不住"型号相关的运行时行为"——
Flash 扇区大小、电压范围、时序、中断优先级这些仍要上板确认。H7 分支的 API 用法按公开头文件写，
但本机没有 H7 的 Cube 包，没有做过真实 HAL 对照，属于"桩验证、未上板、未对真实头文件"的三重空白。

### 7.5 这一轮编译验证抓出来的问题（已修）

| 问题 | 后果 | 修法 |
| --- | --- | --- |
| F4 也定义 `FLASH_TYPEPROGRAM_HALFWORD`，示例按该宏选分支 | **F4 走到 F1 分支**，`FLASH_FLAG_PGERR` / `FLASH_TYPEERASE_PAGES` 在 F4 未定义 → 真实 F4 HAL 下编不过 | 分支判据改为按擦除单位（见 7.3） |
| 示例写死 TIM6 做毫秒时基 | **F103C8（示例点名的芯片）没有 TIM6** → 目标芯片上编不过 | 默认改用 SysTick（`HAL_GetTick`），TIM6 变成可选开关 |
| F1 侧写了 `FLASH_FLAG_WRPRTERR` | 真实 F1 HAL 的宏叫 `FLASH_FLAG_WRPERR` → 编不过 | 统一改成 `FLASH_FLAG_WRPERR` |

### 7.6 怎么跑

```powershell
# 仓库根目录；需要 Arm GNU Toolchain（arm-none-eabi-gcc）在 PATH 里
pwsh scripts/verify_mcu_build.ps1

# 机器上没有 STM32Cube 包时（只用桩，仍然严格执行零告警）
pwsh scripts/verify_mcu_build.ps1 -SkipRealHal
```

脚本会：编译核心三模块 → 两份示例 × 三族（桩）→ TIM6 可选分支 → 找到 Cube 包再对真实 HAL 编一遍；
任何一项非零退出或**本仓源码**出现告警即返回 1（vendor 头文件自身的告警不记账）。

