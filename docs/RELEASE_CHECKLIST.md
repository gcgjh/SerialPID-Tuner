# 发布前检查清单（SerialPID-Tuner v0.2）

> 用途：打包 / 发布前逐项执行并勾选，任一项不通过不得发布。
> 每项都给出可执行命令或明确判据，避免"看一遍觉得没问题"。
> 最近一次完整执行记录见文末。

## 1. 版本单一来源

- [ ] **版本资源与包内版本一致**
  - 命令（仓库根）：`..\.venv\Scripts\python.exe tools\make_version_info.py --check`
  - 判据：`[OK] 版本资源与包内版本一致：0.2.0.0`，退出码 0
- [ ] **四处版本口径一致**
  - 命令（`pc` 目录）：`..\..\.venv\Scripts\python.exe -m pytest tests\test_version_consistency.py -q`
  - 判据：包内 `__version__` = `scripts/version_info.txt` = `CHANGELOG` 最高版本 = README 当前版本行
- [ ] **tag 与版本一致**（发版时）
  - 命令：`git tag -l "v0.2.0"`，且 tag 指向发布提交

## 2. 测试基线

- [ ] **常规回归全过**
  - 命令（`pc` 目录）：`..\..\.venv\Scripts\python.exe -m pytest tests -q`
  - 判据：`0 failed`；`skip` 只来自长稳门控（`-rs` 可核对原因）；`52 subtests` 全过
- [ ] **长稳全过、无 xfail**
  - 命令：`..\..\.venv\Scripts\python.exe -m pytest tests\test_long_run.py --long-run -q`
  - 判据：`0 failed` / `0 xfailed`；时间轴回绕与复位两条必须是正式通过用例
- [ ] **MCU 主机测试**
  - 命令：`pwsh mcu\test\run_host_test.ps1`
  - 判据：`host_test` 与 `app_host_test` 均 `exit 0`
- [ ] **ARM 交叉编译 + HAL 集成示例**
  - 命令：`pwsh scripts\verify_mcu_build.ps1`
  - 判据：`pid_comm` / `pid_calc` / `pid_app` / `main_example` / `pid_nvm_stm32_example`
    全部编译通过，`-Wall -Wextra` 零告警

## 3. 产物与版本资源

- [ ] **版本化产物存在，稳定名副本同时更新**
  - 命令：`Get-ChildItem dist\SerialPID-Tuner* | Select-Object Name,LastWriteTime,Length`
  - 判据：`SerialPID-Tuner-v0.2.0.exe` 与 `SerialPID-Tuner.exe` 同一次构建（时间戳相差秒级）
- [ ] **版本资源与脚本一致**
  - 命令：
    `$e=(Get-Item dist\SerialPID-Tuner-v0.2.0.exe).VersionInfo; $e.FileVersion; $e.ProductVersion; $e.LegalCopyright`
  - 判据：`0.2.0.0` / `0.2.0.0` / `MIT License`
- [ ] **图标已嵌入**：构建日志出现 `Copying icon to EXE`
- [ ] **打包版可启动**：`Start-Process dist\SerialPID-Tuner-v0.2.0.exe`，进程存活 ≥ 7 秒、无崩溃退出
- [ ] **出包后自检**：`dist\selftest-exe.json` 存在且 `status != fail`

## 4. 产物新鲜度与发布报告

- [ ] **exe 不落后于实现源码**
  - 命令：`..\.venv\Scripts\python.exe scripts\check_artifact_freshness.py --require`
  - 判据：退出码 0，输出 `[OK] ... 晚于最新源码`
  - 反证（可选）：故意 `touch` 一个 `pc/serialpid/*.py` 后再跑，必须 `[STALE]` 退出码 1
  - 判据只认 `pc/main.py`、`pc/serialpid/**/*.py`、`mcu/Src/*.c`、`mcu/Inc/*.h`；
    测试 / 桩 / 文档 / 脚本改动不触发（避免天天误报）
- [ ] **一键发布跑通**
  - 命令：`..\.venv\Scripts\python.exe scripts\release.py`
  - 判据：打印 `[完成] 发布报告：docs/releases/v0.2.0.md`，退出码 0
- [ ] **发布报告已归档**
  - 判据：`docs/releases/v0.2.0.md` 含版本、commit、测试结果、自检结果、体积、SHA-256、
    新鲜度与冒烟结论
  - 记录 SHA-256，供下载后核对

## 5. 持续集成

- [ ] `.github/workflows/ci.yml` 五个 job 全绿：PC 常规 / PC 长稳（允许失败但要可见）/
      MCU 主机 / ARM 交叉编译 / 出包与产物新鲜度
- [ ] CI 出包 job 的产物可下载，且 `check_artifact_freshness.py --require` 通过
- [ ] CI 只出包不发布：没有自动 tag、没有自动上传 release

## 6. 许可证与文档口径

- [ ] `LICENSE` 存在且为 MIT 全文（Copyright 年份 / 作者在位）
- [ ] `README.md` 许可章节引用 `LICENSE`，正文与版本资源声明一致
- [ ] `README.md` / `docs/user_guide.md` 的虚拟环境路径与实际一致
      （本机为仓库上一层 `.venv`，干净克隆按文档自建）
- [ ] `docs/PROTOCOL.md` 的 CRC / 长度口径与 `pc/serialpid/protocol.py` 一致
- [ ] 文档与实现无"文档说有问题、测试显示全绿"的错位：
      `docs/BUGS.md` 每条遗留缺陷都有编号（F-xx / B-xx）、状态与测试结果一致
- [ ] `docs/CHANGELOG.md` 的版本标题与当前版本一致，本轮行为变更已记录

## 7. 仓库卫生

- [ ] **工作区干净**
  - 命令：`git status --short`
  - 判据：无未提交修改、无未跟踪的应入库文件（本地临时脚本、日志、缓存必须清掉；
    `dist/` 与 `docs/releases/` 已被忽略 / 是发布产物，不计为脏）
- [ ] **提交清单无构建产物 / 环境 / 缓存 / 可执行文件**
  - 命令：`git ls-files | Select-String "\.venv|__pycache__|\.pytest_cache|^dist/|^build/|\.exe$|\.log$|\.spec$"`
  - 判据：零命中
- [ ] **构建副产物 `SerialPID-Tuner.spec` 未入库**（构建后删除，不作为构建入口）
- [ ] **干净克隆可复现测试**
  - 命令：`git clone <repo> <临时目录>` → 按 README 建 venv、`pip install -r pc\requirements-dev.txt`
    → `cd pc; python -m pytest tests -q`
  - 判据：全部通过，且克隆内无 `dist/`

---

## 最近一次执行记录

> 每轮发布前追加，不覆盖历史。

- **2026-11-07 第五轮（card26 发布工程）**：版本收敛为 `0.2.0`
  （`__version__` / 版本资源 / CHANGELOG / README 四处一致，并由
  `test_version_consistency.py` 守护）；积压改动按模块拆分提交并打 `v0.2.0` tag；
  新增五档 CI（PC 常规 / 长稳 / MCU 主机 / ARM 交叉编译 / 出包 + 新鲜度）；
  打包依赖锁定 PyInstaller 6.22.3 / Pillow 12.0.0 / pytest 9.2.0；产物改为
  `dist\SerialPID-Tuner-v0.2.0.exe` + 稳定名副本；新增 `scripts/release.py`
  一键发布与 `docs/releases/v0.2.0.md` 报告归档。详见该报告。
