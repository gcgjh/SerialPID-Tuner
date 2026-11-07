# 卡35 验收记录 · 三条对话框与全界面文案终审

> 记录时间：2026-11-07　｜　分支 `master`　｜　跑法见文末「一条命令复核」

本卡要的是"用户真的能用"：首次引导、F1 帮助、关于框、截图脚本四条路径走得通，
且界面上所有会给用户看的失败提示都按 **现象 / 原因 / 下一步** 三段式说清楚。
下面每一条都配上**怎么验的**与**证据在哪**；自动走查覆盖不到的，单列在
「仍需人工点一遍」。

---

## 一、结论

| 验收项（卡35 六、验收八条） | 结论 | 怎么验的 | 证据 |
| --- | --- | --- | --- |
| 四条交互路径走通并留记录 | 通过 | `tools/walkthrough_35.py` 离屏走查 43 项全 OK | `docs/images/acceptance-35/report.json` + 7 张现场图 |
| 全界面文案统一三段式（六处点名） | 通过 | 逐处改造 + 断言"三行齐全" | 见第二节表；`pc/tests/test_messages.py`、`test_link_ui.py` |
| 引导只弹一次、跳过被记住 | 通过 | 跳过后回到配置、重建实例、重建主窗口都不再弹 | `onboarding-1..4`；`test_onboarding.py` 3 条 |
| 帮助与手册同源 | 通过 | 来源标签指向 `docs/user_guide.md`，正文三节都在 | `help-f1.png` |
| 关于框诊断可复制、含串口后端 | 通过 | 复制到剪贴板后逐行比对；串口后端与自检同一函数 | `about.png`；`test_about_dialog.py` 4 条 |
| 截图重跑幂等、中文正常 | 通过 | 同参跑两次：文件名一致、像素漂移 ≤0.021%、尺寸一致；两个汉字墨迹不同 | `test_screenshots.py` 2 条 |
| 畸形 markdown 容错 | 通过 | 坏链接降级、未闭合代码块自动补；不崩、内容不丢 | `help-malformed.png`；`test_help_dialog.py` 3 条 |
| 全量测试不回归 | 通过 | **525 passed / 6 skipped / 52 subtests / 42.24 s**（改动前基线 447 passed） | 本机实测 |

走查还顺手抓到两个**真人一眼就看得见、单测看不出来**的问题，已一并修好：

1. **向导三屏配图全都不显示**。`_build_page` 拼的是 `docs_dir()/02-sim-panel.png`，
   而截图实际在 `docs/images/` 下——文件不存在，代码静默跳过配图，界面只剩
   一大片空白。修成 `docs_dir()/"images"/image`（回退旧布局），现在三屏都能
   看到示意截图（对照 `onboarding-1..3.png`）。
2. **关于框把已装好的 PySide6 报成「未安装」**。本机 Qt 的发行名是
   `PySide6_Essentials`，`importlib.metadata.version("PySide6")` 直接抛
   `PackageNotFoundError`。诊断信息里出现"能跑却说没装"是硬伤，改成先查别名表
   （`PySide6-Essentials` / `PySide6-Addons` / `PySide6_Essentials`），再退回
   `import 模块读 __version__`，全失败才算未安装。修后 6.9.3 / 0.14.0 / 3.5 / 2.4.6。

---

## 二、点名必改的六处文案（改前 → 改后）

三段式的写法统一为：`现象 / 原因 / 下一步` 三行，日志面板整块打印，只有一行高的
状态栏打短句、完整版挂 tooltip。

| # | 位置 | 改前 | 改后 |
| --- | --- | --- | --- |
| 1 | 串口连接失败三分<br>`messages.serial_open_message`（`pc/serialpid/messages.py:78`） | 一段话混在一起说"打不开" | 按 pyserial 报错分**被占用 / 忙 / 不存在**三类，每类各自给原因与下一步（`Access is denied` → "端口被别的程序占着"，`could not open port` → "盒子没插好或驱动没装"） |
| 2 | 解析异常计数<br>`messages.parse_error_message`（`messages.py:149`） | 逐帧刷"解析异常 #N: …" | 聚合为"收到 **N** 帧格式不对的 XXX 帧"+ 原因（固件版本/线序/波特率）+ 下一步（看「端口诊断」或换 115200） |
| 3 | 自检失败横幅<br>`messages.selftest_message`（`messages.py:165`） | 只报"自检未通过" | 横幅一行现象 + 原因（哪一项失败）+ 下一步（点「复制诊断信息」发给开发） |
| 4 | 端口空状态<br>`messages.empty_port_message`（`messages.py:114`） | 下拉框空白，无解释 | "没有可用串口"+ 三态区分（未插设备 / 驱动未装 / 枚举失败）+ 下一步（换 USB 口、用「内置模拟器」先跑） |
| 5 | 心跳超时带 N 次<br>`comm.describe_disconnect_message`（`pc/serialpid/comm.py:199`）+ `messages.heartbeat_timeout_message`（`messages.py:197`） | "连续多次心跳没有应答"——没有次数、也没说怎么放宽 | "连续 **N** 次（判死阈值 N 次）心跳没有应答，判定已断开"+ 原因（判死窗口内没收到任何数据）+ 下一步（查线/换口，或在**工具 → 链路设置…** 里把「容忍次数」调大） |
| 6 | 回放文件损坏告知跳过行数<br>`frame_log_dialog.load_file`（`pc/serialpid/gui/frame_log_dialog.py:406`） | 遇到坏行直接报"文件格式错误"、整份不回放 | `load_jsonl_lenient` 跳过坏行："文件里有 **N** 行无法解析，已跳过（其余 M 帧照常回放）"+ 原因（手改/断电写坏）+ 下一步（需要原始数据就重导一份）；文件名后加"· 跳过 N 行坏记录" |

同批补上的还有三处同类泄漏（不在点名清单里，但都是"贴一行原始异常"）：

- `main_window._on_worker_error`（`pc/serialpid/gui/main_window.py:733`）：心跳发送失败 /
  写失败 / 帧回调异常 / 读异常四类走 `messages.worker_error_message`，原文进 `detail`。
- `param_panel` 四处保存/加载/删除/重命名失败：走 `messages.preset_error_message`，
  状态栏短句 + tooltip 完整版。
- 断线日志：原来是两行（现象/下一步），补上"原因"一行，三行齐全。

**判定为不改的**（理由写在代码边或测试里，供后来者复核）：

- `sim_panel.load_warning`（"仿真参数读取失败：…"）：只在对象上暴露、没有任何展示方，
  设计意图是"读不到就用默认值、不打扰用户"。
- `frame_log.py` 的 `f"解析异常 {name} {reason}"`：日志聚合用的计数行，不是给用户的
  结论；`test_long_run.py` 断言它恰好出现 1 次，属长稳压测的观测口径。
- `config.py` / `csv_export.py` / `protocol.py` / `preset_manager.py` / `port_scan.py`
  / `transport.py` / `step_test.py` / `zn_tuning.py` 里的异常文本：不直接进界面。
- `step_panel` 的「样本不足」「分析失败」：指标区只有一行高，正文是短句，完整三段式
  在 tooltip 里（`pc/serialpid/gui/step_panel.py:250`、`:267`），已达标。

---

## 三、四条路径的逐项结果（`tools/walkthrough_35.py`）

一条命令跑完四条路径，失败不中断，结果落 `report.json`：

```
合计 43 项，失败 0 项
```

| 路径 | 项数 | 关键断言 | 现场图 |
| --- | --- | --- | --- |
| 首次引导 | 9 | 三屏各自能翻；末屏按钮变「完成」；点「跳过」后 `onboarding_done` 落盘；**重建实例与重建主窗口都不再弹** | `onboarding-1.png` `onboarding-2.png` `onboarding-3.png` `onboarding-4-main-window.png` |
| F1 帮助 | 9 | 真按 `F1` 键弹出（不是只检查快捷键绑没绑）；内容来自 `docs/user_guide.md`；三节齐全；来源标签指向手册；坏 markdown 不崩 | `help-f1.png` `help-malformed.png` |
| 关于框 | 10 | 版本号取自包属性；**含「串口后端」行且与 `selftest.serial_backend_summary()` 完全一致**；四个依赖都有真实版本；诊断整段可复制、以「SerialPID-Tuner 诊断信息」开头 | `about.png` |
| 截图脚本 | 15 | 两次 `capture_all` 文件名一致；逐图像素漂移 <0.2%（实测 0.0000%–0.0210%）；尺寸稳定；加载到中文字体；两个汉字墨迹不同（不是豆腐块） | 六张正式截图 `docs/images/0*.png` |

### 截图幂等实测（1280×860）

| 文件 | 像素漂移 | 尺寸 |
| --- | --- | --- |
| `01-main-window.png` | 0.0069% | 1280×860 |
| `02-sim-panel.png` | 0.0039% | 1280×860 |
| `03-port-diagnosis.png` | 0.0210% | 840×570 |
| `04-selftest-banner.png` | 0.0040% | 1280×860 |
| `05-step-metrics.png` | 0.0000% | 644×736 |
| `06-statusbar.png` | 0.0000% | 1240×98 |

阈值取 0.2%（0.002）：日志时间戳每秒都在变，逐字节相同永远不可能；但再放宽就抓不住
"字体回退成方块 / 整屏错位"这类真事故。中文是否正常另用"不同汉字墨迹必须不同"判定
（只看文件大小抓不到——方块图照样几千字节）。

---

## 四、测试覆盖（本卡新增 20 条用例，全部随全量一起跑）

| 文件 | 新增用例 | 守住什么 |
| --- | --- | --- |
| `pc/tests/test_messages.py` | `test_heartbeat_timeout_message_counts_misses_and_points_at_setting`、`test_worker_error_message_classifies_and_keeps_raw_text`、`test_preset_error_message_covers_four_actions_and_is_single_line` | 三段式三行齐全；心跳带 N 次且指向「容忍次数」；原始异常不被吞 |
| `pc/tests/test_about_dialog.py` | `test_dependency_version_reports_missing_package_honestly`、`test_dependency_version_survives_distribution_name_mismatch`、`test_diagnostic_text_includes_serial_backend_status`、`test_serial_backend_summary_names_module_when_backend_missing`、`test_about_dialog_shows_serial_backend_row` | 版本真实读取；关于框与自检同源，不允许两处各写一套判定 |
| `pc/tests/test_help_dialog.py` | `test_normalize_markdown_closes_unclosed_code_fence`、`test_normalize_markdown_drops_dangerous_links_but_keeps_label`、`test_help_dialog_survives_malformed_markdown`、`test_help_dialog_reopen_has_clean_scroll_and_selection`、`test_f1_key_really_opens_the_help_dialog` | 畸形 markdown 不崩且内容不丢；重开滚动条回顶、无残留选区；**F1 真按真弹** |
| `pc/tests/test_link_ui.py` | `test_disconnect_log_is_full_three_part_with_cause`、`test_heartbeat_timeout_logs_miss_count_and_setting_hint`、`test_worker_error_is_three_part_not_raw_prefix` | 断线日志三行齐全；心跳日志带次数与设置入口；线程异常不再贴原始前缀 |
| `pc/tests/test_onboarding.py` | `test_each_page_shows_its_illustration`、`test_skip_is_remembered_across_restart_in_portable_mode`、`test_maybe_show_onboarding_pops_once_then_stays_quiet` | 三屏配图真的贴上；跳过写进配置且重启不再弹；`maybe_show_onboarding` 只弹一次 |
| `pc/tests/test_screenshots.py` | `test_capture_all_is_idempotent_on_rerun`、`test_cjk_glyphs_render_as_real_glyphs_not_tofu` | 截图重跑稳定；中文不是方块 |

---

## 五、仍需人工点一遍（脚本覆盖不到的部分）

脚本是离屏渲染 + 合成按键，能证明"逻辑上会弹、内容对、图能出"，但证明不了真实窗口
时序。**下列 6 条请在真机上走一遍**（约 3 分钟）：

1. 冷启动（先把 `%APPDATA%\SerialPID-Tuner\config.json` 改名）：向导自动弹出 →
   三屏翻页时 Esc / 点右上关闭按钮是否也算"看过"（不应二次弹）。
2. 向导打开时按 Alt-Tab 切走再切回：窗口是否仍然模态、焦点是否回到向导。
3. 主窗口按 `F1`：帮助是否立即置前（不闪到主窗口后面）、Esc 是否只关帮助。
4. 帮助窗口拉到最大 / 缩到最小：长代码块是否出现横向滚动条而不是撑破窗口。
5. 关于框「复制诊断信息」：粘到记事本里，中文与路径是否完整（非乱码）。
6. 真实串口拔出：手动把 USB 拔掉，日志三行是否齐全、心跳那行是否带具体次数；
   再插回重连，状态是否恢复「已连接」且不重复刷错误。

---

## 六、一条命令复核

```powershell
# 四条路径（约 60-90 秒，文本界面不弹窗）
cd C:\Users\shehu\Desktop\串口PID\SerialPID-Tuner
$env:QT_QPA_PLATFORM="offscreen"
C:\Users\shehu\Desktop\串口PID\.venv\Scripts\python.exe tools\walkthrough_35.py
# → 合计 43 项，失败 0 项；退出码 0

# 全量测试
cd pc
C:\Users\shehu\Desktop\串口PID\.venv\Scripts\python.exe -m pytest tests -q
# → 525 passed, 6 skipped, 52 subtests passed
```

证据目录 `docs/images/acceptance-35/`：`about.png`、`help-f1.png`、`help-malformed.png`、
`onboarding-1..3.png`、`onboarding-4-main-window.png`、`report.json`（43 条 step/ok/detail）。
