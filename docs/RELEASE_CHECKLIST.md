# 发布前检查清单（SerialPID-Tuner v0.1）

> 用途：打包 / 发布前逐项执行并勾选，任一项不通过不得发布。
> 每项都给出可执行命令或明确判据，避免"看一遍觉得没问题"。
> 最近一次完整执行记录见文末。

## 1. 测试基线

- [ ] **常规回归全过**
  - 命令（仓库根）：`cd pc` 后 `..\..\.venv\Scripts\python.exe -m pytest tests -q`
  - 判据：`0 failed`；`skip` 只来自长稳门控（`-rs` 可核对原因）；`52 subtests` 全过
- [ ] **长稳全过、无 xfail**
  - 命令：`cd pc` 后 `..\..\.venv\Scripts\python.exe -m pytest tests/test_long_run.py --long-run -q`
  - 判据：`0 failed` / `0 xfailed`；时间轴回绕与复位两条必须是正式通过用例
- [ ] **MCU 主机测试**
  - 命令：`pwsh mcu\test\run_host_test.ps1`
  - 判据：`host_test` 与 `app_host_test` 均 `exit 0`
- [ ] **ARM 交叉编译 + HAL 集成示例**
  - 命令：`pwsh scripts\verify_mcu_build.ps1`
  - 判据：`pid_comm` / `pid_calc` / `pid_app` / `main_example` / `pid_nvm_stm32_example`
    全部编译通过，`-Wall -Wextra` 零告警

## 2. 产物与版本资源

- [ ] **产物时间戳晚于源码最后改动**
  - 命令：`Get-Item dist\SerialPID-Tuner.exe | Select-Object LastWriteTime`
  - 判据：该时间晚于仓库内除 `dist/` 与缓存目录外全部文件的最后修改时间。
    若并行修改了源码，必须重新打包而不是靠改时间戳。
- [ ] **版本资源与脚本一致**
  - 命令：`$e=(Get-Item dist\SerialPID-Tuner.exe).VersionInfo; $e.FileVersion; $e.ProductVersion; $e.LegalCopyright`
  - 判据：`1.0.0.0` / `1.0.0.0` / `MIT License`，与 `scripts/version_info.txt` 一致
- [ ] **图标已嵌入**
  - 判据：构建日志出现 `Copying icon to EXE`
- [ ] **打包版可启动**
  - 命令：`Start-Process dist\SerialPID-Tuner.exe`
  - 判据：进程存活 ≥ 7 秒、无崩溃退出

## 3. 许可证与文档口径

- [ ] `LICENSE` 存在且为 MIT 全文（Copyright 年份 / 作者在位）
- [ ] `README.md` 许可章节引用 `LICENSE`，正文与版本资源声明一致
- [ ] `README.md` / `docs/user_guide.md` 的虚拟环境路径与实际一致
      （本机为仓库上一层 `.venv`，干净克隆按文档自建）
- [ ] `docs/PROTOCOL.md` 的 CRC / 长度口径与 `pc/serialpid/protocol.py` 一致
- [ ] 文档与实现无"文档说有问题、测试显示全绿"的错位：
      `docs/BUGS.md` 每条遗留缺陷都有编号（F-xx / B-xx）、状态与测试结果一致

## 4. 仓库卫生

- [ ] **工作区干净**
  - 命令：`git status --short`
  - 判据：无未提交修改、无未跟踪的应入库文件（本地临时脚本、日志、缓存必须清掉）
- [ ] **提交清单无构建产物 / 环境 / 缓存 / 可执行文件**
  - 命令：`git ls-files | Select-String "\.venv|__pycache__|\.pytest_cache|^dist/|^build/|\.exe$|\.log$|\.spec$"`
  - 判据：零命中
- [ ] **构建副产物 `SerialPID-Tuner.spec` 未入库**（构建后删除，不作为构建入口）
- [ ] **干净克隆可复现测试**
  - 命令：`git clone <repo> <临时目录>` → 按 README 建 venv、`pip install -r pc\requirements.txt`
    → `cd pc; python -m pytest tests -q`
  - 判据：全部通过，且克隆内无 `dist/`

## 5. 遗留缺陷与发布说明

- [ ] `docs/BUGS.md` 未关闭项均带编号、影响、责任方与目标版本
- [ ] `docs/CHANGELOG.md` 记录本轮行为变更（如 CSV 精度统一、时间轴回绕/复位、
      预设管理接入），并写明对已有导出文件对比的影响
- [ ] 未验证项在文档中明确标注（例如未用真机验证的 HAL 分支），不写成"全部验证通过"

---

## 最近一次执行记录

> 每轮发布前追加，不覆盖历史。

- **2026-11-07 第三轮（卡14 执行）**：常规 `166 passed / 6 skipped / 52 subtests`；
  长稳 `--long-run` `6 passed / 0 xfailed`；MCU 双测试器 `exit 0`；
  ARM 编译（含 HAL 示例）零告警；`dist\SerialPID-Tuner.exe` 时间戳晚于全部源码；
  `git status` 干净；提交 `见 git log`。
