#!/usr/bin/env python3
"""从包内版本号生成 PyInstaller 的 Windows 版本资源文件（版本单一来源）。

为什么要有这个脚本：``scripts/version_info.txt`` 是一份 ``VSVersionInfo`` 常量表，
里面有两处版本号（``filevers`` 与两个 ``StringStruct`` 版本字段）。手写维护就意味着
"包内版本改了、exe 属性没改"，用户报 bug 时报的是文件属性里的旧版本号，无法定位。

现在的口径：

- 唯一真相：``pc/serialpid/__init__.py`` 的 ``__version__``；
- 本脚本把它渲染成 ``scripts/version_info.txt``（PyInstaller ``--version-file`` 读取）；
- 打包脚本（``scripts/build.bat`` / ``build.sh``）在打包前调用本脚本重新生成一次，
  所以 exe 属性永远跟着包内版本走；
- ``--check`` 模式供 CI 与测试用：磁盘上的版本资源与包内版本不一致时退出码 1。

用法：

    python tools/make_version_info.py            # 重新生成 scripts/version_info.txt
    python tools/make_version_info.py --check    # 只校验，不写文件
    python tools/make_version_info.py --version  # 只打印版本号（供打包脚本取用）

注意：``__version__`` 必须是 ``X.Y.Z`` 三段语义化版本；第四段固定补 ``0``
（Windows 的 ``FileVersion`` 是四段结构）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_PATH = REPO_ROOT / "pc" / "serialpid" / "__init__.py"
VERSION_INFO_PATH = REPO_ROOT / "scripts" / "version_info.txt"

_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

_HEADER = """\
# PyInstaller Windows 版本资源（--version-file 读取）。
# 由 tools/make_version_info.py 从 pc/serialpid/__init__.py 的 __version__ 生成，请勿手改；
# 打包脚本（scripts/build.bat / build.sh）在调用 PyInstaller 前会重新生成一次。
"""


def read_version(init_path: Path = INIT_PATH) -> str:
    """读出包内 ``__version__``；读不到直接抛错，不允许回落到任何默认值。"""
    text = init_path.read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if not match:
        raise ValueError(f"{init_path} 里没有 __version__ 定义")
    return match.group(1).strip()


def version_tuple(version: str) -> Tuple[int, int, int, int]:
    """``X.Y.Z`` → 四段整数元组（Windows 版本资源用，第四段固定 0）。"""
    match = _SEMVER_RE.match(version)
    if not match:
        raise ValueError(f"版本号必须是 X.Y.Z 三段数字：{version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch, 0)


def dotted_quad(version: str) -> str:
    return ".".join(str(part) for part in version_tuple(version))


def render(version: str) -> str:
    """渲染完整的 ``version_info.txt`` 内容（带结尾换行）。"""
    filevers = version_tuple(version)
    quad = dotted_quad(version)
    tuple_text = "(" + ", ".join(str(part) for part in filevers) + ")"
    return (
        _HEADER
        + "\n"
        + "VSVersionInfo(\n"
        + "  ffi=FixedFileInfo(\n"
        + f"    filevers={tuple_text},\n"
        + f"    prodvers={tuple_text},\n"
        + "    mask=0x3F,\n"
        + "    flags=0x0,\n"
        + "    OS=0x40004,\n"
        + "    fileType=0x1,\n"
        + "    subtype=0x0,\n"
        + "    date=(0, 0)\n"
        + "  ),\n"
        + "  kids=[\n"
        + "    StringFileInfo(\n"
        + "      [\n"
        + "      StringTable(\n"
        + "        '080404B0',\n"
        + "        [StringStruct('CompanyName', 'SerialPID-Tuner'),\n"
        + "         StringStruct('FileDescription', 'Serial PID Tuner (serial PID commissioning tool)'),\n"
        + f"         StringStruct('FileVersion', '{quad}'),\n"
        + "         StringStruct('InternalName', 'SerialPID-Tuner'),\n"
        + "         StringStruct('OriginalFilename', 'SerialPID-Tuner.exe'),\n"
        + "         StringStruct('ProductName', 'SerialPID-Tuner'),\n"
        + f"         StringStruct('ProductVersion', '{quad}'),\n"
        + "         StringStruct('LegalCopyright', 'MIT License'),\n"
        + "         StringStruct('Comments', 'Step test, Ziegler-Nichols assist, real-time telemetry.')])\n"
        + "      ]),\n"
        + "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
        + "  ]\n"
        + ")\n"
    )


_TRIPLE_RE = re.compile(r"filevers=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)")
_FILE_VERSION_RE = re.compile(r"StringStruct\('FileVersion',\s*'([^']+)'\)")
_PRODUCT_VERSION_RE = re.compile(r"StringStruct\('ProductVersion',\s*'([^']+)'\)")


def parse_version_info(text: str) -> Dict[str, str]:
    """从版本资源文本里抽出版本字段（供测试 / --check 使用）。"""
    fields: Dict[str, str] = {}
    triple = _TRIPLE_RE.search(text)
    if triple:
        fields["filevers"] = ".".join(triple.groups())
    for key, pattern in (("FileVersion", _FILE_VERSION_RE), ("ProductVersion", _PRODUCT_VERSION_RE)):
        match = pattern.search(text)
        if match:
            fields[key] = match.group(1)
    return fields


def _normalise(text: str) -> str:
    """比对时统一换行，避免 CRLF/LF 差异制造假失败。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="从包内 __version__ 生成 PyInstaller 版本资源")
    parser.add_argument("--check", action="store_true", help="只校验磁盘文件是否与包内版本一致")
    parser.add_argument("--version", action="store_true", help="只打印版本号")
    parser.add_argument("--init", default=str(INIT_PATH), help="包 __init__.py 路径（默认仓库内）")
    parser.add_argument("--out", default=str(VERSION_INFO_PATH), help="版本资源输出路径")
    args = parser.parse_args(argv)

    try:
        version = read_version(Path(args.init))
        quad = dotted_quad(version)
    except ValueError as exc:
        print(f"[ERR] {exc}")
        return 2

    if args.version:
        print(version)
        return 0

    rendered = render(version)
    out_path = Path(args.out)

    if args.check:
        if not out_path.exists():
            print(f"[ERR] 版本资源不存在：{out_path}")
            return 1
        current = out_path.read_text(encoding="utf-8")
        if _normalise(current) != _normalise(rendered):
            parsed = parse_version_info(current)
            print(
                f"[ERR] 版本资源与包内版本不一致：包内 {quad} / 文件 "
                f"{parsed.get('FileVersion', '未知')}（{out_path}）"
            )
            print("      执行 python tools/make_version_info.py 重新生成")
            return 1
        print(f"[OK] 版本资源与包内版本一致：{quad}")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 只在内容确实变化时写盘：避免每次打包都把版本资源时间戳刷新一遍，
    # 让"这个文件到底改没改"在 git diff 里仍然可读。
    if out_path.exists() and _normalise(out_path.read_text(encoding="utf-8")) == _normalise(rendered):
        print(f"[OK] 版本资源已是最新：{out_path}（{quad}）")
        return 0
    out_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"[OK] 已生成 {out_path}（{quad}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
