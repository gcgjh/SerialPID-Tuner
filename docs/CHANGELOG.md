# 变更记录

本项目遵循语义化版本思路，格式参考 Keep a Changelog。

版本号的唯一来源是 `pc/serialpid/__init__.py` 的 `__version__`；本文件的最高版本、
`README.md` 的当前版本行与 `scripts/version_info.txt` 都由它派生，
`pc/tests/test_version_consistency.py` 负责在测试里守住这条。

## [Unreleased]

（暂无。下一轮改动先写在这里，发版时整体提升为 `## [X.Y.Z] - 日期`。）

## [0.2.0] - 2026-11-07

第四、五轮的仿真 / 端口诊断 / 启动自检接入，以及本轮的发布工程收口。
PC 测试基线 `281 passed`（含长稳档），MCU 双测试器与 ARM 交叉编译零告警。

### 新增

- **内置仿真设备面板**：`sim/plant.py`（一阶惯性植物）、`sim/engine.py`（协议会话、
  命令分发、阶跃状态机）、`gui/sim_panel.py`（7 参数 / 5 预设、默认折叠）。进程内即可
  闭环演示，不必再另起 `tools/fake_mcu.py` 的 TCP 模拟器。
- **串口端口诊断**：`port_scan.py` 提供三态枚举结果（有设备 / 无设备 / 枚举失败），
  带超时保护（驱动卡死时有上限），可生成一段能直接粘贴给开发者的诊断文本；界面端口
  下拉不再塞假 `COM1` 占位。
- **启动自检**：`selftest.py` 八项检查（Python 版本、pyserial、串口平台后端、Qt 平台
  插件、配置目录可写、日志目录可写、图标资源、协议帧往返），三态报告
  （pass / warn / fail）+ JSON 与文本日志落盘 + 无控制台 exe 的
  `--selftest --out <文件>` 出口；主窗口启动横幅只在失败 / 警告时显示，不占常态高度。
- **发布工程**（本轮）：
  - `__version__` 作为版本单一来源，`tools/make_version_info.py` 从它生成
    `scripts/version_info.txt`，打包脚本在打包前重新生成；
  - GitHub Actions 五档：PC 常规测试、PC 长稳档（允许失败但可见）、MCU 主机测试、
    ARM 交叉编译（`-Wall -Wextra` 零告警）、出包 + 产物新鲜度；
  - `scripts/check_artifact_freshness.py`：dist/ 里的 exe 早于任一实现源码即判失败
    （测试 / 桩 / 文档不计入，避免误报）；
  - `scripts/release.py`：测试 → 自检 → 打包 → 校验 → 冒烟 → 报告，一条命令串完，
    报告归档到 `docs/releases/v<版本>.md`（含 commit、测试结果、产物哈希）。

### 变更

- 打包依赖锁定版本：PyInstaller 6.22.3 / Pillow 12.0.0 / pytest 9.2.0；
  打包脚本改为 `pip install -r pc/requirements-dev.txt`，不再裸装。
- 产物名带版本号：`dist/SerialPID-Tuner-v0.2.0.exe`，同时保留稳定名副本
  `dist/SerialPID-Tuner.exe` 供文档引用。
- 版本号三处不一致（版本资源 1.0.0.0 / 本文件 0.1.0 / README v0.1）收敛为 `0.2.0`。

### 修复与文档一致性（card5 二轮复核，2026-11-05）

- 阶跃分析样本列表改为定长队列（上限 20000，与遥测缓冲 / 绘图队列一致），
  消除长跑无界增长；补对应回归用例。
- 文档—代码口径对齐：打包链路改为按 `scripts/build.bat` / `build.sh` 的
  PyInstaller 命令行参数记录（本项目不以 spec 作为构建入口）；断流超时统一为
  1.0s；撤回无实现支撑的 500ms 应答超时表述；修正失效的 `app_config.h` 引用为
  `pid_app.h` 配置宏。
- 功能列表删除未兑现承诺：链路质量统计、帧日志回放、曲线标注阶跃时刻
  （缺口见 `BUGS.md` B-05 / B-07）。

### 文档清理（card6 清除虚构的 spec 入口，2026-11-07）

- 五份文档（`TASKS.md` / `TEST_REPORT.md` / `development.md` / `BUGS.md` / 本文）
  共 8 处把不存在的 `SerialPID-Tuner.spec` 描述成构建入口、实测对象或工作区保留
  文件，现全部改写为「打包由 `scripts/build.bat` / `build.sh` 命令行参数完成，
  不以 spec 作为构建入口」；`TEST_REPORT.md` 的打包命令示例改为直接调用一键脚本。
- `.gitignore` 删除「spec 保留入库、故不忽略」的失效注释，改为按构建副产物忽略
  `*.spec`：PyInstaller 命令行构建会在工作目录自动生成同名 spec，不入库。

### 第三轮修复与接入（card11–card15，2026-11-07）

- 绘图时间轴：32 位毫秒回绕按 `2^32` 补偿继续；会话中途复位判为新会话重开；
  连接建立 / 检测到复位 / 手动清空统一走 `restart_timeline()`。长稳中
  「回绕」「复位重连」两条由预期失败转为正式通过。
- CSV 导出收敛为唯一实现：界面按钮改调 `csv_export` 正式模块，精度统一为
  六位小数；同一批样本界面导出与模块导出字节级一致。历史四位小数文件对比时
  需注意精度差异。
- 预设管理接入参数面板：保存 / 加载 / 删除 / 重命名；加载只填界面、不自动下发。
- HAL 集成示例与 NVM 示例纳入编译验证：最小桩环境 + 真实 HAL 头交叉核对，
  `scripts/verify_mcu_build.ps1` 共 15 项零告警；接线文档补充 HAL 符号清单。

### 计划中（v0.3）

- 真机联调：烧录、上板自测序列、故障排查树（card29）。
- 通信可靠性：主动心跳、链路质量统计、ERROR 码中文映射、断线原因分类（card28）。
- 仿真生态：故障注入、录制回放、批量扫参（card27）。
- 用户可见面：截图素材、内置帮助、关于对话框（card30）。

## [0.1.0] - 2026-11-05

首个离线闭环验收版本：PC 上位机全链路跑通，MCU 协议层 / 算法层 / 应用层通过
主机侧离线测试，协议基准冻结。

### 新增

- **协议层**：帧头 `0xAA 0x55`、帧尾 `0x77×4`、CRC16-Modbus、小端 float32；
  11 类包类型冻结定义；粘包 / 拆包 / 坏 CRC / 坏帧尾 / 超长帧重同步。
- **PC 通信**：Serial / TCP 双传输抽象、通信线程、断流检测、线程安全发送队列、
  Qt 信号适配。
- **PC 上位机**：SP/PV/OUT 实时曲线、参数下发 + ACK 回显、阶跃命令与指标展示、
  Z-N 辅助整定面板、日志与 CSV 导出、深色主题、PID 预设 JSON 管理。
- **离线模拟器**：`tools/fake_mcu.py`，TCP 一阶惯性植物 + 简易 PID，响应心跳 /
  参数下发 / 设定值 / 阶跃 / 复位。
- **MCU 固件**：
  - `pid_comm`：CRC、小端读写、512 字节环形缓冲、解析状态机、组帧与便捷封装；
  - `pid_calc`：位置式 / 增量式 PID、微分先行、抗积分饱和、积分分离、NaN/Inf 守卫、
    无扰切换；
  - `pid_app`：帧分发、10ms 控制节拍、50ms 遥测、阶跃辨识、参数延迟落 Flash、
    IO 回调注入；
  - `main_example.c` HAL 接线示例（UART 中断 / 1ms 时基 / PWM 占位 / 可选仿真植物）；
  - `pid_nvm_stm32_example.c` Flash 保存示例（F1 / F4 / H7 分支）。
- **MCU 主机测试**：CRC 标准向量、11 组解析用例（含 4096 字节 fuzz 混杂流）、
  5 组组帧真值比对、PID 限幅 / 有限性 / 闭环方向检查；应用层 mock IO 冒烟测试。
- **工程化**：PyInstaller 打包脚本（Windows `.bat` / Linux `.sh`）、版本资源、
  图标生成脚本、协议向量生成脚本。

### 修复（验收阶段对齐）

- 修正 MCU `PONG.status`：由位掩码改为与 PC 冻结枚举一致的取值
  （0 空闲 / 1 运行 / 2 阶跃中）；NVM 故障改走 ERROR 帧上报，不再占用状态位。
- 修正 `STEP_EVENT.phase` 语义：开始→运行中(1)、保持结束→完成(2)、中止 / 手动
  停止→空闲(0)，与 PC `StepPhase` 对齐。
- 修正黄金向量 fuzz 用例帧序：真值帧改为由 PC 解码器按流内顺序回采，
  不再按构造顺序记录，避免随机插入后顺序错位。
- 移除 `pid_app.c` 未使用的静态函数，消除 `-Wextra` 告警。

### 已知限制

- 未接实物板卡：MCU 固件未经真机编译 / 烧录 / 联调；芯片型号、传感器、执行器
  与引脚分配待硬件确认。详见 [`BUGS.md`](BUGS.md)。
