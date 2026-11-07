# ESP32 移植：SerialPID 协议的第二条真机路线

这块工程让 `pid_comm / pid_calc / pid_app` 三层核心（**一行未改**）跑在 ESP32 上。
和 H750（SWD 板）路线**并存**，不是替代关系：哪块板在手就用哪条，见
`docs/bringup.md` 第 9 节的并列状态表。

- 目标芯片：ESP32-D0WD-V3（双核 240 MHz，4 MB flash，板载 CH340）
- SDK：ESP-IDF v6.0.1
- 串口：UART0 / GPIO1(TX) GPIO3(RX) / 115200 8N1 —— 就是板上那颗 CH340 的口
- 协议版本：`PID_FW_VERSION=2`（对应仓库 v0.2.x，规则见 `docs/PROTOCOL.md` §6）

## 1. 目录结构

```
mcu/esp32/
├─ CMakeLists.txt                     # ESP-IDF 工程根
├─ sdkconfig.defaults                 # 只写与默认不同的项（4MB flash / 1kHz tick / 日志压到 ERROR）
├─ build.ps1                          # 构建/烧录封装（ASCII 工作区，见第 6 节）
├─ components/serialpid_core/
│  └─ CMakeLists.txt                  # 把 ../../../Src 的三个 .c 编成组件，头文件路径 ../../../Inc
└─ main/
   ├─ CMakeLists.txt
   ├─ main.c                          # app_main + UART 接收任务 + 10ms 控制任务
   ├─ board_esp32.c / board_esp32.h   # 板级六回调
   └─ （build/ 是本机构建产物，已 gitignore）
```

核心模块**不复制**到 ESP32 目录：组件直接引用 `mcu/Src/*.c` 与 `mcu/Inc`，
保证三条硬件路线看到的是同一份代码。改核心只需改一处。

## 2. 板级六回调：STM32 ↔ ESP32 对照

| `pid_app_io_t` | STM32（`mcu/Src/main_example.c`） | ESP32（`main/board_esp32.c`） |
| --- | --- | --- |
| `write_output(float)` | `TIM3_CH4` PWM，CCR = 占空比 | LEDC timer0 / 1 kHz / 10 bit，GPIO2（板载 LED） |
| `read_pv()` | `ADC1_IN10` 单次转换 + 轮询 | 默认一阶惯性仿真（K=0.45 / T=8 s）；`BOARD_SIM_PLANT=0` 时读 `adc_oneshot`（GPIO34 / ADC1_CH6，line_fitting 校准，失败退回裸码映射） |
| `send_frame(buf, len)` | `HAL_UART_Transmit(..., 20ms)` | `uart_write_bytes` + `uart_wait_tx_done(20ms)` |
| `nvm_load(pid_nvm_t*)` | `pid_nvm_stm32_example.c` 扇区擦写 + CRC | NVS blob（namespace `pids` / key `params`），长度 + magic + CRC 三重校验 |
| `nvm_save(const pid_nvm_t*)` | 同上 | `nvs_set_blob` + `nvs_commit` |
| `system_reset()` | `NVIC_SystemReset()` | `esp_restart()` |

`pid_nvm_t` 仍是 28 字节同一布局、同一个 `PID_NVM_MAGIC (0x50494431)`、
同一份 `pid_crc16_modbus`（前 16 字节），**和 STM32 版口径完全一致**：
把同一份参数从 STM32 搬到 ESP32 或反过来，CRC 口径不需要重新定义。
校验逻辑仍在 `pid_app.c` 里（核心层），板级只负责"把 28B 原样读出来/写进去"，
所以 ESP32 侧看到长度不符或 magic/CRC 不对时，`pid_app` 会当空参数、绝不带坏参数跑闭环。

## 3. 接收模型差异（移植文档必须讲清的那一点）

| | STM32（H750 路线） | ESP32（本工程） |
| --- | --- | --- |
| 触发源 | `HAL_UART_RxCpltCallback`，每字节一次中断 | UART 驱动 ISR 把 FIFO 数据折叠成 `UART_DATA` 事件 |
| 谁搬字节 | 中断里 `pid_app_on_rx_byte`（一次 1 字节入环） | 接收任务 `xQueueReceive` 后 `uart_read_bytes` 批量取，`pid_app_on_rx_block` 整段入环 |
| 溢出处理 | `HAL_UART_ErrorCallback` 清 ORE + Abort + 重挂 | `UART_FIFO_OVF` / `UART_BUFFER_FULL` 事件：`uart_flush_input` + `xQueueReset` |
| 共同约束 | 中断/事件里**只入环**，不解析、不打印、不阻塞；环是单生产者单消费者无锁结构 | 同左 |

两种形态的等价性：`pid_app_on_rx_block(app, buf, n)` 就是把 `n` 个字节连续
`pid_app_on_rx_byte` 一遍（核心层这么实现），所以解帧行为逐字节相同。
差异只在于"谁在什么上下文里把字节搬进环"。

生产者在接收任务、消费者在控制任务，两者都钉在 **core 1**：同核上二者不会真正
并行，环的 head/tail 各自只有一个写者，无锁结构成立。**如果以后把接收任务挪到
core 0，必须给环加临界区**——这是本工程唯一一处"看起来能改、其实不能随便改"的地方。

## 4. 任务模型与时基

| 任务 | 优先级 | 栈 | 周期 | 说明 |
| --- | --- | --- | --- | --- |
| `pid_rx` | 12 | 4096 | 事件驱动 | 收字节任务高于控制任务：不能让 10 ms 控制把 UART 拖出溢出 |
| `pid_ctrl` | 10 | 6144 | `vTaskDelayUntil` 10 ms | 每圈 `pid_app_poll` + `pid_app_tick(now_ms)`；50 ms 遥测与 5 s 落盘由 `pid_app_tick` 内部按时间戳决定 |

- 时基：`esp_timer_get_time()/1000`（64 位微秒 → 32 位毫秒），回绕由 `pid_app` 的无符号减法处理。
- `CONFIG_FREERTOS_HZ=1000`：100 Hz tick 会把 10 ms 周期量化成整 tick 而看不出抖动，
  1 kHz 才能量出真实偏差。
- 不用双核隔离方案：先把抖动实测出来，超出预期再考虑，这是任务卡明确的要求。

抖动实测数据与"是否值得绑核/提优先级"的结论见 `docs/bringup.md` §9.4。

## 5. 引脚与默认口径

| 功能 | 引脚 | 参数 |
| --- | --- | --- |
| 协议串口 | GPIO1 (TX) / GPIO3 (RX) | 115200 8N1，UART0（板上 CH340） |
| 输出 PWM | GPIO2（板载 LED，高电平点亮） | LEDC 1 kHz / 10 bit，0–100 % 占空比 |
| 反馈 ADC | GPIO34（ADC1_CH6，只读引脚） | 12 bit / 12 dB 衰减 / 0–3.1 V ↔ 0–100 % |
| 参数存储 | — | NVS namespace `pids`，key `params`，28 B blob |

LEDC 选 GPIO2 是为了"不接线也能看见输出"；接真实执行器时改 `BOARD_LEDC_GPIO` 与
`BOARD_LEDC_FREQ_HZ` 即可，`write_output` 其余逻辑不用动。

## 6. 构建：路径必须是 ASCII（本机踩过的坑）

本仓库路径含中文（`…\串口PID\SerialPID-Tuner`），ESP-IDF v6.0.1 在 Windows 上
对非 ASCII 路径有**三处硬失败**，都不是配置能绕开的：

1. `build/toolchain/cflags` 响应文件里的 `picolibc.specs` 路径被拆成非法字节
   → `cannot read spec file 'C:/Users/shehu/Desktop/\x0aPID/...': Invalid argument`
2. ninja 在含中文的输出路径上建 `.obj` → `No such file or directory`（中文被吃成 `PID`）
3. ccache 用 `std::filesystem` 转换路径 → `Illegal byte sequence`

`subst` 虚拟盘和目录联接（junction）都无效：CMake 会把路径 `REAL_PATH` 还原成中文真实路径。
**对策**：`build.ps1` 把 `mcu/Src`、`mcu/Inc`、`mcu/esp32` 镜像到一个纯 ASCII 工作区
（默认 `C:\SerialPID-esp32`）再编译，成品拷回 `mcu/esp32/build/`。仓库仍是唯一源码。

```powershell
# 编译（第一次会全量编，约 3–5 分钟）
.\mcu\esp32\build.ps1 build

# 烧录 / 监视（烧录前必须手动进下载模式，见第 7 节）
.\mcu\esp32\build.ps1 flash -Port COM5
.\mcu\esp32\build.ps1 monitor -Port COM5
```

想直接用 `idf.py` 也可以，前提是把仓库放在纯 ASCII 路径下（例如 `C:\SerialPID-Tuner`）：
`idf.py -C mcu/esp32 build`。

## 7. 烧录：本机这块板必须手动按 BOOT

这块 ESP32 的 CH340 DTR/RTS 自动下载电路不工作，直接 `idf.py flash`（默认
`--before=default-reset`）实测报 `Failed to connect to ESP32: No serial data received`。
流程：

1. **按住 BOOT（IO0）不松手**，同时**按一下 RST/EN 再松开**（BOOT 保持按住）；
2. 确认板子已进下载模式后，用 esptool 直连烧（**必须 `--before=no-reset`**，别再让 DTR 复位一次，
   否则刚进的下载模式会被打断）：

```powershell
. "C:\Espressif\tools\Microsoft.v6.0.1.PowerShell_profile.ps1" | Out-Null
Set-Location C:\SerialPID-esp32\mcu\esp32\build
esptool --chip esp32 -p COM5 -b 460800 --before=no-reset --after=hard-reset write-flash `
  --flash-mode dio --flash-freq 40m --flash-size 4MB 0x1000 bootloader\bootloader.bin `
  0x8000 partition_table\partition-table.bin 0x10000 serialpid_tuner_fw.bin
```

3. 看到三段 `Hash of data verified` 后松开 BOOT；若此时串口一片静默，说明芯片还停在下载模式
   （BOOT 没松开），按一下 RST 即可正常启动。

烧完别用 `build.ps1 flash` 复核：它走的是 `default-reset`，在这块板上一半概率连不上。

串口还被上位机（SerialPID-Tuner 界面）占用时烧不进去：先断开界面里的"连接"，
然后跑 `python tools/esp32_bringup.py --port COM5 --seconds 20`（加 `--nvs` 多做一步参数快照复验），
不经过 GUI。

## 8. 已验证 / 未验证（诚实边界）

- 已验证：三层核心在 xtensa GCC 15.2.0（`-Wall -Wextra -Werror`）下**零告警**编过；
  固件链接通过（app 分区剩余 81%）；COM5 上烧录成功（三段 Hash verified）；PING/PONG、
  参数下发 ACK、50 ms 遥测（均值 50.00 ms / 抖动 0.0 ms）、控制环 10 ms 节拍（2000 圈最大间隔 10 ms）、
  参数快照写 NVS 并跨软复位读回，实测记录见 `docs/bringup.md` §9.4–§9.5。
  入口：`python tools/esp32_bringup.py --port COM5 --seconds 20 --nvs`。
- 未验证：真实传感器/执行器闭环（第 5–6 步要 `BOARD_SIM_PLANT=0` 且接线）、第 7 步阶跃响应、
  断电（拔电）后的 NVS 保留（本轮只验了软复位 `esp_restart`）、WiFi/BT 同时开启时的节拍抖动（本固件不开射频）。
  真实 ADC 通道与 LEDC 波形都还没上仪器量过。
