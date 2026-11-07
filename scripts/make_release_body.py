#!/usr/bin/env python3
"""生成 GitHub Release 附注（卡37）：三平台产物清单 + 自检摘要 + 发布报告。

为什么单独一个脚本：Release 页面的内容一旦手写，就会和 ``docs/releases/``
里的报告各自走样。这里只做"搬运 + 补表"：

* 正文主体直接嵌入 ``docs/releases/v<ver>.md``（test / selftest / 哈希的唯一真相）；
* 在前面补一张三平台下载表（文件、大小、SHA-256 前 12 位、自检结论）；
* 再补每平台的"首次运行"注意事项（Linux dialout / macOS Gatekeeper）。

用法（仓库根）::

    python scripts/make_release_body.py --version 0.2.1 --artifacts dist --out release-body.md

产物：``--out`` 指定的 Markdown；同时把 ``SHA256SUMS.txt`` 写进 ``--artifacts``，
方便用户下载后本地校验（Release 页面里也给出校验命令）。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
RELEASES_DIR = ROOT / "docs" / "releases"

#: 产物文件名 → 平台标签。顺序即下载表顺序（Windows 在最前，多数用户在此）
PLATFORMS = (
    ("windows", "Windows", "SerialPID-Tuner-v*-*.exe"),
    ("linux", "Linux", "SerialPID-Tuner-v*-linux-*"),
    ("macos", "macOS", "SerialPID-Tuner-v*-macos-*"),
)

FIRST_RUN = {
    "windows": "下载 `.exe` 双击即用，无需安装 Python。SmartScreen 提示时选「更多信息 → 仍要运行」。",
    "linux": (
        "解压后执行 `./SerialPID-Tuner/SerialPID-Tuner`；串口报权限不足时 "
        "`sudo usermod -aG dialout $USER` 后重新登录。缺 `libGL.so.1` 时 "
        "`sudo apt install -y libgl1 libegl1 libxkbcommon0 libdbus-1-3`。"
    ),
    "macos": (
        "解压后把 `.app` 拖进「应用程序」。首次打开需右键 →「打开」绕过 Gatekeeper"
        "（未签名），或在「系统设置 → 隐私与安全性」里点「仍要打开」。"
    ),
}


def _load_version_tool():
    path = ROOT / "tools" / "make_version_info.py"
    spec = importlib.util.spec_from_file_location("body_make_version_info", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERSION_TOOL = _load_version_tool()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024:.0f} KB"


def _platform_of(name: str) -> Optional[str]:
    lowered = name.lower()
    if lowered.endswith(".exe"):
        return "windows"
    if "-linux-" in lowered:
        return "linux"
    if "-macos-" in lowered:
        return "macos"
    return None


def collect_artifacts(artifacts_dir: Path, version: str) -> List[Dict[str, str]]:
    """按平台归类产物；忽略自检 JSON、校验文件与目录。"""
    order = {key: index for index, (key, _label, _pattern) in enumerate(PLATFORMS)}
    rows: List[Dict[str, str]] = []
    for entry in sorted(artifacts_dir.iterdir()):
        if not entry.is_file():
            continue
        platform = _platform_of(entry.name)
        if platform is None:
            continue
        if f"v{version}" not in entry.name and f"v{version}." not in entry.name:
            continue
        if entry.suffix == ".json":
            continue
        rows.append(
            {
                "platform": platform,
                "file": entry.name,
                "size": _human_size(entry.stat().st_size),
                "sha256": _sha256(entry),
                "path": str(entry),
            }
        )
    return sorted(rows, key=lambda row: (order.get(row["platform"], 99), row["file"]))


def read_selftest(artifacts_dir: Path) -> Dict[str, Dict[str, str]]:
    """读取各平台的自检结果（``selftest-<platform>.json``，兼容旧名 ``selftest-exe.json``）。"""
    found: Dict[str, Dict[str, str]] = {}
    for platform in ("windows", "linux", "macos"):
        for name in (f"selftest-{platform}.json", "selftest-exe.json"):
            path = artifacts_dir / name
            if not path.is_file():
                continue
            try:
                # utf-8-sig：Windows 侧工具写出的 JSON 常带 BOM
                report = json.loads(path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                found[platform] = {"summary": "自检结果无法解析", "status": "unknown"}
                break
            found[platform] = {
                "summary": str(report.get("summary", "?")),
                "status": str(report.get("status", "?")),
            }
            break
    return found


def _report_section(version: str) -> str:
    path = RELEASES_DIR / f"v{version}.md"
    if not path.is_file():
        return (
            "## 发布报告\n\n"
            f"> `docs/releases/v{version}.md` 尚未生成。发布页仍可用，"
            "但缺少测试结果与产物哈希的书面记录；补跑 `scripts/release.py` 后重打。\n"
        )
    return "## 发布报告\n\n" + path.read_text(encoding="utf-8").strip() + "\n"


def build_body(version: str, artifacts_dir: Path) -> str:
    rows = collect_artifacts(artifacts_dir, version)
    selftests = read_selftest(artifacts_dir)

    table = ["| 平台 | 文件 | 大小 | 启动自检 | SHA-256（前 12 位） |", "| --- | --- | --- | --- | --- |"]
    label_by_platform = {key: label for key, label, _pattern in PLATFORMS}
    for row in rows:
        platform = row["platform"]
        check = selftests.get(platform)
        verdict = f"{check['status']} · {check['summary']}" if check else "（未附自检 JSON）"
        table.append(
            f"| {label_by_platform.get(platform, platform)} | `{row['file']}` | {row['size']} "
            f"| {verdict} | `{row['sha256'][:12]}` |"
        )
    if not rows:
        table.append("| — | 未找到产物 | — | — | — |")

    present = sorted({row["platform"] for row in rows}, key=lambda p: [k for k, _, _ in PLATFORMS].index(p))
    first_run = ["## 首次运行", ""]
    for platform in present:
        first_run.append(f"- **{label_by_platform[platform]}**：{FIRST_RUN[platform]}")
    if not first_run[2:]:
        first_run.append("- 本次未找到可分发的产物，仅发布说明。")

    today = datetime.date.today().isoformat()
    return (
        f"# SerialPID-Tuner v{version}\n\n"
        f"> 由 `scripts/make_release_body.py` 于 {today} 生成；正文报告来自 "
        f"`docs/releases/v{version}.md`（请勿手改发布页的测试与哈希数字）。\n\n"
        "## 下载\n\n"
        + "\n".join(table)
        + "\n\n"
        "校验下载完整性（下载目录里执行）：\n\n"
        "```bash\n"
        "sha256sum -c SHA256SUMS.txt     # Linux\n"
        "shasum -a 256 -c SHA256SUMS.txt # macOS\n"
        "certutil -hashfile SerialPID-Tuner-v" + version + ".exe SHA256   # Windows\n"
        "```\n\n"
        + "\n".join(first_run)
        + "\n\n"
        "## 版本要求\n\n"
        "- 三平台产物都自带 Python 与 Qt，运行不需要另装解释器或依赖。\n"
        "- 源码运行要求 Python 3.10+（实测 3.13）。\n\n"
        + _report_section(version)
    )


def write_sha256sums(version: str, artifacts_dir: Path) -> Optional[Path]:
    rows = collect_artifacts(artifacts_dir, version)
    if not rows:
        return None
    path = artifacts_dir / "SHA256SUMS.txt"
    lines = [f"{row['sha256']}  {row['file']}" for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub Release 附注")
    parser.add_argument("--version", default=None, help="版本号（默认读 pc/serialpid/__init__.py）")
    parser.add_argument("--artifacts", default=str(ROOT / "dist"), help="产物目录")
    parser.add_argument("--out", default=None, help="输出 Markdown（默认打印到 stdout）")
    args = parser.parse_args(argv)

    version = (args.version or VERSION_TOOL.read_version()).strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"[ERR] 版本号格式不对：{version}")
        return 2
    artifacts_dir = Path(args.artifacts)
    if not artifacts_dir.is_dir():
        print(f"[ERR] 产物目录不存在：{artifacts_dir}")
        return 2

    body = build_body(version, artifacts_dir)
    sums = write_sha256sums(version, artifacts_dir)
    if sums is not None:
        print(f"[OK] 校验清单: {sums}")
    else:
        print("[WARN] 没有找到任何产物，发布页只会有说明文字")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8", newline="\n")
        print(f"[OK] Release 附注: {out}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
