#!/usr/bin/env python3
"""产物新鲜度检查：dist/ 里的 exe 必须晚于所有实现源码（card26）。

为什么单列一个脚本：exe 落后源码这件事，肉眼比对时间戳靠不住——改了三个文件、
只打包一次、然后再改一个，看起来"刚打过包"其实已经落后。把它做成可执行判据后，
有三处会调用同一套逻辑：

- ``scripts/release.py``：出包后必须通过（``--require``）；
- GitHub Actions：出包 job 里跑一次（``--require``）；
- 人工排查：``python scripts/check_artifact_freshness.py``。

判据只认**实现文件**：``pc/main.py``、``pc/serialpid/**/*.py``、``mcu/Src/**/*.c``、
``mcu/Inc/**/*.h``。测试、桩、文档、脚本改动都不触发重打包（改测试不需要重出包，
把它们算进来只会天天误报）。

退出码：0 通过或跳过；1 产物落后；2 要求产物但 dist/ 里没有。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 参与新鲜度判断的实现文件（相对仓库根）。测试 / 桩 / 文档 / 构建脚本刻意不计入。
SOURCE_GLOBS: Sequence[str] = (
    "pc/main.py",
    "pc/serialpid/*.py",
    "pc/serialpid/gui/*.py",
    "pc/serialpid/sim/*.py",
    "mcu/Src/*.c",
    "mcu/Inc/*.h",
)

#: 构建副产物目录，永远不计入源码新鲜度
_EXCLUDED_PARTS = ("__pycache__", "tests", "test", "stubs")


def source_files(root: Path = REPO_ROOT) -> List[Path]:
    files: List[Path] = []
    for pattern in SOURCE_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in _EXCLUDED_PARTS for part in relative.parts):
                continue
            files.append(path)
    return sorted(files)


def newest(files: Iterable[Path]) -> Tuple[Path, float]:
    items = [(path, path.stat().st_mtime) for path in files]
    if not items:
        raise SystemExit("[ERR] 没有找到任何实现源码，检查脚本的 glob 配置有问题")
    return max(items, key=lambda item: item[1])


def artifacts(dist_dir: Path, pattern: str = "SerialPID-Tuner*") -> List[Path]:
    """dist/ 里可发布的产物（按平台形态：exe / 无扩展名可执行 / zip / tar.gz）。

    ``.json``（自检结果）、``.md``、``.sha256`` 是旁证不是产物，不能参与新鲜度比较，
    否则"自检文件刚写出来"就能把落后的包判成新鲜的。
    """
    if not dist_dir.is_dir():
        return []
    result: List[Path] = []
    for path in dist_dir.glob(pattern):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith((".json", ".md", ".sha256", ".txt")):
            continue
        if (
            path.suffix in (".exe", "")
            or path.suffix.startswith(".v")
            or name.endswith((".zip", ".tar.gz", ".appimage"))
        ):
            result.append(path)
    return sorted(result)


def _stamp(path: Path) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def check(dist_dir: Path, pattern: str = "SerialPID-Tuner*") -> Tuple[int, List[str]]:
    """返回 (退出码, 输出行)。纯函数式，方便被发布脚本直接调用。"""
    lines: List[str] = []
    exe_files = artifacts(dist_dir, pattern)
    if not exe_files:
        lines.append(f"[SKIP] {dist_dir} 里没有产物（未打包，或 CI 只做源码构建）")
        return 0, lines

    newest_source, newest_mtime = newest(source_files())
    lines.append(f"最新实现源码：{newest_source.relative_to(REPO_ROOT)}（{_stamp(newest_source)}）")

    stale: List[Path] = []
    for exe in exe_files:
        exe_mtime = exe.stat().st_mtime
        lag = exe_mtime - newest_mtime
        if lag >= 0:
            lines.append(f"[OK] {exe.name}：{_stamp(exe)}，晚于最新源码 {lag / 60:.1f} 分钟")
        else:
            stale.append(exe)
            lines.append(f"[STALE] {exe.name}：{_stamp(exe)}，比最新源码早 {-lag / 60:.1f} 分钟")

    if stale:
        newer = [
            path
            for path in source_files()
            if path.stat().st_mtime > max(exe.stat().st_mtime for exe in stale)
        ]
        lines.append(f"以下 {len(newer)} 个文件在产物之后被改过（最多列 10 个）：")
        for path in newer[:10]:
            lines.append(f"  - {path.relative_to(REPO_ROOT)}（{_stamp(path)}）")
        lines.append("处理：重新执行 scripts/build.bat（或 scripts/release.py），不要改时间戳凑数")
        return 1, lines

    return 0, lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查 dist/ 产物是否晚于实现源码")
    parser.add_argument("--dist-dir", default=str(REPO_ROOT / "dist"), help="产物目录（默认仓库 dist/）")
    parser.add_argument("--pattern", default="SerialPID-Tuner*", help="产物文件名通配（默认 SerialPID-Tuner*）")
    parser.add_argument("--require", action="store_true", help="没有产物时判失败（发布流程用）")
    args = parser.parse_args(argv)

    dist_dir = Path(args.dist_dir)
    code, lines = check(dist_dir, args.pattern)
    if code == 0 and args.require and not artifacts(dist_dir, args.pattern):
        lines.append("[ERR] --require：要求 dist/ 里存在产物，实际没有")
        code = 2

    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
