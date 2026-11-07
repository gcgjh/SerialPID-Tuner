# 文档索引

> 这份索引回答一个问题：**我想知道 X，该读哪个文件。** 按"使用者 → 协议 → 开发 →
> 测试 → 发布"的顺序排；每篇文档都只保留一个"主入口"，避免同一件事两个版本。

## 从这里开始

| 想做什么 | 读这篇 | 一句话 |
| --- | --- | --- |
| 装好、连上、看到第一根曲线 | [../README.md](../README.md) | 仓库门面：功能、安装、测试、目录结构 |
| 用这个上位机调 PID | [user_guide.md](user_guide.md) | 用户手册：五分钟上手、内置模拟器调参实战、界面速查、术语表、常见问题 |
| 想知道某功能写没写进文档 | [FEATURES.md](FEATURES.md) | 功能—文档对照清单：已写 / 缺失 / 描述过时，三份主文档逐项对齐 |
| 手上有板子，从零上电联调 | [bringup.md](bringup.md) | 八步自测、CubeMX 配置对照、故障排查树 |
| 接线怎么接、引脚怎么对 | [wiring.md](wiring.md) | USART1 接线表、电平注意事项、[接线示意图](images/wiring-usb-ttl.png) |

## 协议与接口

| 文档 | 说明 |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | **冻结的协议基准**：帧格式、CRC、命令码、载荷布局。改协议先改这里 |
| [PC_TOOLS_API.md](PC_TOOLS_API.md) | PC 侧函数级接口：协议 / 通信 / 模型 / 分析各层的公开函数 |

## 二次开发

| 文档 | 说明 |
| --- | --- |
| [development.md](development.md) | 分层与线程模型、打包脚本、发布流程、代码约定 |
| [ITERATION.md](ITERATION.md) | 迭代路线、每个版本的取舍与风险 |
| [BUGS.md](BUGS.md) | 已知问题与限制清单（含"为什么不修"） |
| [TASKS.md](TASKS.md) | 任务卡与验收口径的归档 |

## 测试与压测

| 文档 | 说明 |
| --- | --- |
| [TEST_REPORT.md](TEST_REPORT.md) | 当前验收测试报告（用例数、覆盖范围、结论） |
| [PRESSURE_TEST.md](PRESSURE_TEST.md) | **加速**压测口径：几分钟跑完等效数据量 / 重连次数，判数据管道与连接生命周期 |
| [SOAK_TEST.md](SOAK_TEST.md) | **小时级**挂机压测：真实墙钟 4 h / 8 h，看内存、句柄、线程、渲染耗时是否随时间漂移 |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | 发布前逐项检查清单（测试 → 自检 → 打包 → 校验 → 冒烟） |

## 发布与变更

| 文档 | 说明 |
| --- | --- |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本的变更记录（版本号来自 `pc/serialpid/__init__.py`） |
| [PACKAGING.md](PACKAGING.md) | 三平台打包与分发：产物形态、平台差异、流水线结构、验证边界、二期缺口 |
| [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) | 发布前逐项检查清单（测试 → 自检 → 打包 → 校验 → 冒烟） |
| [releases/](releases/) | 每版本一份发布报告（`scripts/release.py` 生成：commit / 测试 / 产物 SHA-256） |

## 文档资产（截图与示意图）

| 资产 | 来源 | 说明 |
| --- | --- | --- |
| [images/01-main-window.png](images/01-main-window.png) | `python tools/make_screenshots.py` | 主界面：曲线 + 阶跃指标；README 顶部与手册第 1 / 6 章引用 |
| [images/02-sim-panel.png](images/02-sim-panel.png) | 同上 | 内置模拟器 + 场景预设 / 仿真参数面板；README 顶部与手册第 3 / 4 / 9 章引用 |
| [images/03-port-diagnosis.png](images/03-port-diagnosis.png) | 同上 | 端口诊断对话框 |
| [images/04-selftest-banner.png](images/04-selftest-banner.png) | 同上 | 启动自检横幅（警告态，三段式文案） |
| [images/05-step-metrics.png](images/05-step-metrics.png) | 同上 | 阶跃指标区特写 |
| [images/06-statusbar.png](images/06-statusbar.png) | 同上 | 链路质量 / 坏帧状态栏特写 |
| [images/wiring-usb-ttl.png](images/wiring-usb-ttl.png) | `python tools/make_wiring_diagram.py` | USART1 接线示意图 |

## 维护规则

- **截图只有一条来源**：`python tools/make_screenshots.py` 一条命令重生成六张标准图
  （离屏渲染，显式加载系统中文字体，避免中文变方块）。界面改了就重跑，不要手工截图。
- **帮助与手册同源**：内置帮助（F1）不另写文案，运行时从 `user_guide.md` 抽取
  「五分钟上手 / 界面速查 / 常见问题」三节；改手册不必改帮助。
- **版本号只有一处**：`pc/serialpid/__init__.py` 的 `__version__`，README、CHANGELOG、
  Windows 版本资源、产物文件名都由它派生（`pc/tests/test_version_consistency.py` 会校验）。
- **新增文档**：在本索引对应分组登记一行；不登记的文档视为临时草稿。
