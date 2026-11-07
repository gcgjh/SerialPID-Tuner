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

- [ ] `.github/workflows/ci.yml` 七个 job 全绿：PC 常规（Windows 硬门槛）/
      PC 跨平台观测（ubuntu + macOS）/ PC 长稳（允许失败但要可见）/ MCU 主机 /
      ARM 交叉编译 / 三平台出包矩阵 / Release 分发页（仅 tag 触发）
- [ ] CI 出包 job 的产物可下载，且 `check_artifact_freshness.py --require` 通过
- [ ] **三平台各出一份**：`SerialPID-Tuner-v<ver>.exe` / `-linux-<arch>.tar.gz` /
      `-macos-<arch>.zip`，三份都带 `selftest-<platform>.json`
- [ ] CI 只出包不发布：分支推送不建 Release；只有 `push tag v*` 才走 release job

## 5b. 跨平台打包（card37）

- [ ] **图标三件齐全**：`python tools/make_icon.py --check` 输出 ico / png / icns
- [ ] **Linux 分支命令正确**（任意平台可查）：`bash scripts/build.sh --dry-run --platform linux`
      打出的命令含 `--onedir`、`--icon assets/app.png`、产物名 `-linux-<arch>.tar.gz`
- [ ] **macOS 分支命令正确**：同上 `--platform macos`，含 `--icon assets/app.icns` 与
      `--osx-bundle-identifier`，产物名 `-macos-<arch>.zip`
- [ ] **Linux 包内包含**：可执行 + `_internal/` + `.desktop` + `serialpid-udev.rules` +
      `首次运行.md`
- [ ] **Linux 串口权限文档在位**：`README.md`「下载与安装」与
      `docs/user_guide.md` 2.1 都有 `usermod -aG dialout` 与 udev 规则示例
- [ ] **macOS Gatekeeper 说明在位**：README / 手册 / Release 附注都写"右键 → 打开"
- [ ] **Release 附注可生成**：`python scripts/make_release_body.py --version <ver> --artifacts dist --out body.md`
      生成的表格含三平台文件名、大小、自检摘要、SHA-256 前 12 位，并附 `SHA256SUMS.txt`
- [ ] **首页下载说明与产物命名一致**：README 表格里的文件名 = CI 实际上传的文件名

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

> 多个任务并行推进同一工作区时，"工作区干净"这条做不到（别人正在写盘）。
> 此时用 `python scripts/release.py --clean-tree --allow-dirty`：从 HEAD 拉一份干净
> worktree 跑测试、自检、打包与新鲜度检查，产物只来自已提交源码；报告会列出
> 未进入产物的改动清单，便于事后核对。

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
  `test_version_consistency.py` 守护）；积压改动按模块拆分提交（9 个提交，历史按
  模块可读）；新增五档 CI（PC 常规 / 长稳 / MCU 主机 / ARM 交叉编译 / 出包 + 新鲜度）；
  打包依赖锁定 PyInstaller 6.22.3 / Pillow 12.0.0 / pytest 9.2.0；产物改为
  `dist\SerialPID-Tuner-v0.2.0.exe` + 稳定名副本；新增 `scripts/release.py`
  一键发布与 `docs/releases/v0.2.0.md` 报告归档。详见该报告。
  - **实测**：`release.py --clean-tree` 全流程通过（73.7s）——
    已提交用例 `171 passed / 6 skipped / 52 subtests`、源码自检 `8 通过 / 0 警告 / 0 失败`、
    产物 52.6 MB、FileVersion/ProductVersion `0.2.0.0`、
    新鲜度 `[OK] 晚于最新源码 1.1 分钟`、冒烟存活 3s。
  - **产物**：`dist\SerialPID-Tuner-v0.2.0.exe`，
    SHA-256 `1fee8b5730e44c9dc810576631e97635a6c9440d6caea87c99f455aabbba5b11`；
    tag `v0.2.0` 打在被打包的那个提交 `897dc73` 上。
  - **口径说明**：本轮发布与 card27/28/29/30 在同一工作区并行执行，报告生成时工作区
    有 68 项他卡未提交改动。产物只来自 HEAD（已提交源码），这些改动未进入产物——
    它们的功能会在各自卡片提交后由后续版本打包。工作区当前全量用例为
    `280 passed / 6 skipped / 52 subtests`（含未提交的新用例），报告里的 171 项
    是 HEAD 上已提交的那部分。

- **2026-11-07 第六轮（card37 跨平台打包与分发）**：CI 由"Windows 单线"扩成三平台——
  新增 `package` 矩阵（windows / ubuntu / macos 各打各的包，产物按平台命名）与
  `release` job（仅 `push tag v*` 触发，汇总三份产物 + 发布报告挂 Release）；
  `build.sh` 重写为 Linux onedir tar.gz / macOS .app zip 两条分支，带 `--check` /
  `--dry-run` / `--platform` 便于在任意平台验证分支命令；`tools/make_icon.py` 增出
  `assets/app.icns`（Pillow 跨平台可写，macOS job 不必等 `iconutil`）；
  新增 `scripts/make_release_body.py`（下载表 + 自检摘要 + `SHA256SUMS.txt`）；
  `check_artifact_freshness.py` 的产物判据补 `.tar.gz` / `.zip` / `.AppImage`；
  README 与手册补三平台下载说明与 Linux `dialout` / udev 权限、macOS Gatekeeper 路径。
  - **本机可验证部分**：Windows 全流程仍走 `release.py`（产物与 v0.2.1 行为一致）；
    Linux/macOS 分支以 `bash -n` + `--dry-run` 验证命令正确；工作流以 YAML 解析 +
    `pc/tests/test_packaging.py` 验证矩阵与 release 触发条件；附注生成用假产物跑通。
  - **未验证（需 CI 首跑）**：Linux / macOS 真实出包与启动、Release 页真实挂载。
    本机无 Linux/macOS 环境、仓库无 remote，无法给出运行证据——详见
    [`PACKAGING.md`](PACKAGING.md) 第七节。
