# 跨平台打包与分发（card37）

> 这份文档只讲"怎么把同一个仓库变成三个平台都能跑的东西"，以及每处平台差异
> 为什么这么处理。业务代码的分层不在本文范围（见
> [`development.md`](development.md)）。

## 一、为什么必须三平台各打各的

PyInstaller **不做交叉编译**：它把解释器、Qt 二进制、`.so` / `.dylib` / `.dll`
一起塞进产物，所以产物只能在目标操作系统上生成。Windows 打得通不代表 Linux 能跑——
两条最常见的差异是：

| 平台 | 缺什么就起不来 | 表现 |
| --- | --- | --- |
| Linux | `libGL.so.1` / `libEGL.so.1` / `libxkbcommon` | 启动即 `ImportError: libGL.so.1` 或静默退出 |
| macOS | 未签名 + Gatekeeper | 双击提示"无法打开，因为无法验证开发者" |
| Linux | 用户不在 `dialout` 组 | 程序能开，串口一连接就 Permission denied |

所以流水线里三个平台是三个 job，各自出各自的包；任何一份失败都必须看见红字。

## 二、产物形态

| 平台 | 形态 | 产物名 | 为什么 |
| --- | --- | --- | --- |
| Windows | onefile | `SerialPID-Tuner-v<ver>.exe` | 单文件分发最省事；Windows 冷启动解压成本可接受 |
| Linux | **onedir** + tar.gz | `SerialPID-Tuner-v<ver>-linux-<arch>.tar.gz` | onefile 每次启动要把约 100 MB 解到 `/tmp`（3–6 s）；onedir 1 s 内起。tar.gz 解压即用，不依赖 FUSE |
| macOS | **.app**（onedir）+ zip | `SerialPID-Tuner-v<ver>-macos-<arch>.zip` | `.app` 是系统认的目录束，Dock 图标 / 双击行为才对；zip 用 `ditto` 打，保留符号链接 |

Linux 包里额外带三样东西（`scripts/build.sh` 生成）：

- `SerialPID-Tuner.desktop`：桌面快捷方式（`Exec=SerialPID-Tuner/SerialPID-Tuner`）；
- `serialpid-udev.rules`：CH340 / CP210x / FTDI / Arduino 的放行规则；
- `首次运行.md`：dialout 命令、缺库补装、无显示器跑自检的写法。

macOS 包内带 `首次运行.md`（Gatekeeper 绕过方式）。

## 三、图标

`tools/make_icon.py` 一次生成三种格式，**Pillow 在任何平台上都能写 `.icns`**，
所以 macOS job 不需要 macOS 专有的 `iconutil`：

| 文件 | 来源画布 | 用途 |
| --- | --- | --- |
| `assets/app.ico` | 256px（内含 16–256 多档） | Windows exe |
| `assets/app.icns` | 1024px（macOS 的 512@2x 需要） | macOS `.app` |
| `assets/app.png` | 256px | Linux 图标 / 文档配图 |

`python tools/make_icon.py --check` 可在 CI 里快速确认三件都在（缺了退出码 1）。

## 四、行尾纪律（`build.bat` 必须 CRLF）

`cmd` 解析 `.bat` 依赖 CRLF；LF-only 的脚本会被拼成一条命令，表现是满屏

```
'L" >nul 2>nul' is not recognized as an internal or external command
'--add-data' is not recognized as an internal or external command
[ERR] 打包产物自检没有产出结果文件: \selftest-exe.json
```

（`%DIST%` 为空、参数被拆开，但脚本本身语法没错）。这类故障不看行尾永远查不出来，
本轮实测就踩到了：`scripts/build.bat` 被写盘工具存成 LF 后 Windows 打包直接崩。

对策是 `.gitattributes` 把规则写死，而不是靠 `core.autocrlf` 碰运气：

```
*.bat text eol=crlf
*.cmd text eol=crlf
*.sh  text eol=lf
```

`pc/tests/test_packaging.py::test_batch_files_use_crlf` 会守住这条：
`build.bat` 必须含 `\r\n`、`build.sh` 必须不含 `\r`。

## 五、流水线结构
`.github/workflows/ci.yml`：

```
pc-tests（Windows，硬门槛）
pc-tests-cross（ubuntu / macOS，首轮观测项）
pc-tests-long-run（长稳，允许失败）
mcu-host-tests / arm-cross-compile（固件侧）
package（矩阵：windows / ubuntu / macos）
  └─ 源码自检 → 平台打包脚本（内含产物自检）→ 产物新鲜度 → 上传 artifact
release（只在 push tag v* 触发，needs: package）
  └─ 下载三份 artifact → scripts/make_release_body.py → gh release create
```

要点：

- `package` 每份产物打完都跑一次 `--selftest`（源码环境 + 打包产物各一次），
  把"缺依赖 / 缺 Qt 插件"提前变成 CI 日志里的失败项；
- 产物新鲜度（`scripts/check_artifact_freshness.py --require`）在三个平台都跑，
  判据新增了 `.tar.gz` / `.zip` / `.AppImage` 形态，避免只认 `.exe` 导致 Linux
  永远"跳过"；
- Release 正文不手写：`scripts/make_release_body.py` 读
  `docs/releases/v<ver>.md`（测试 / 自检 / 哈希的唯一真相），前面补三平台下载表
  与首次运行说明，同时生成 `SHA256SUMS.txt`。

## 六、发布动作（人来按）

CI 只负责"打得出、验得过"，**发不发由人定**（仓库纪律：不自动 tag、不自动发版）：

```bash
# 1) 确认变更日志与版本号
#    pc/serialpid/__init__.py 的 __version__ 是唯一来源
# 2) 本机出一份 Windows 包并归档报告
..\.venv\Scripts\python.exe scripts\release.py --clean-tree --allow-dirty
# 3) 人工复核后打 tag（推送才会触发 CI 的 package + release）
git tag -a v0.2.1 -m "SerialPID-Tuner v0.2.1"
git push origin v0.2.1
```

推 tag 之前请确认 `docs/releases/v<ver>.md` 已入库——Release 页直接引用它，
缺了会在页面上留一行"报告未生成"。

## 七、验证边界（重要，别把计划当证据）

本仓库开发机是 Windows，且**没有配置 git remote**。因此：

| 项 | 本机可验证 | 证据 |
| --- | --- | --- |
| Windows 打包 / 自检 / 冒烟 / 新鲜度 / 版本资源 | ✅ | `scripts/release.py` 全流程 + `docs/releases/v0.2.1.md` |
| 三平台分支命令是否正确 | ✅ 静态 | `bash scripts/build.sh --dry-run --platform linux\|macos` 打出完整 PyInstaller 命令行 |
| build.sh 语法 | ✅ | `bash -n scripts/build.sh` |
| 工作流结构（矩阵、tag 触发、release 权限与上传清单） | ✅ 静态 | YAML 解析 + `pc/tests/test_packaging.py` |
| Release 附注生成（下载表 / 自检摘要 / 报告拼接 / SHA256SUMS） | ✅ | 同上的单测（用假产物） |
| `.icns` 真能生成并被读回 | ✅ | `make_icon.py` + 单测（Pillow 回读尺寸） |
| Linux / macOS **真实出包并启动** | ❌ 未验证 | 需要 push 后由 CI 首次运行给出；本地无 Linux/macOS 环境 |
| Release 页真实挂载 | ❌ 未验证 | 需要 remote + tag push |

**首次在 CI 上跑需要盯的三件事**：

1. `package / linux`：`libGL` 等系统库是否缺失（已在 job 里 `apt install`，若仍失败看日志里的 `.so` 名）；
2. `package / macos`：`.app` 是否被 `ditto` 正确打包（解压后能否双击）；
3. `pc-tests-cross`：跨平台用例里是否残留 Windows 假设（COM 排序、字体、路径分隔符）。
   红了先判断是"测试假设"还是"实现缺陷"，修干净后把该 job 的
   `continue-on-error` 摘掉转成硬门槛。

## 八、已知缺口（刻意分期）

- **AppImage**（Linux 免解压单文件）：需要 `linuxdeploy` + FUSE，二期再做；
  当前 tar.gz 已能用，服务器 / 无桌面环境也不受影响。
- **macOS 签名与公证**：需要 Apple Developer 证书与 `codesign` / `notarytool`，
  二期再做；当前文档给的是右键绕过 Gatekeeper 的手工路径。
- **`.dmg`**：二期；当前给 zip，用户自己拖进「应用程序」。
- **arm64 macOS 原生包**：CI 的 `macos-latest` 目前是 arm64 runner，产物名带架构
  （`-macos-arm64`）；Intel 机器可跑 Rosetta，若要原生 x86_64 包需再加一个 runner 或
  指定 `macos-13`，属二期。
