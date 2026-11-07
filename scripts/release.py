#!/usr/bin/env python3
"""一键发布：测试 → 自检 → 打包 → 校验 → 冒烟 → 报告（card26）。

这条链的意义是"别人拿到的"和"你验过的"是同一个东西。五个环节缺一不可：

1. **测试**：全量 PC 测试（长稳档不在其中，它太慢；要跑就单独跑）；
2. **自检**：源码环境跑一次启动自检，把依赖 / 目录 / 图标 / 协议往返都过一遍；
3. **打包**：调用 ``scripts/build.bat``（Windows）/ ``build.sh``（其它平台），
   打包脚本自己还会跑一遍"打包产物自检"；
4. **校验**：产物新鲜度（exe 晚于实现源码，``--require`` 强制要求）、体积区间、
   Windows 版本资源与包内版本一致；
5. **冒烟**：拉起产物三秒，进程得活着——无控制台窗口的 exe 直接崩在启动阶段时，
   光看自检是看不出来的，因为自检根本不加载 Qt 界面。

最后输出一份报告到 ``docs/releases/v<版本>.md``：版本、commit、测试结果、
自检结果、产物名 / 体积 / SHA-256、新鲜度与冒烟证据。**报告要入库**——
它是"这个包是怎么验出来的"的唯一书面记录。

用法（仓库根，用项目环境解释器）：

    ..\\.venv\\Scripts\\python.exe scripts\\release.py
    ..\\.venv\\Scripts\\python.exe scripts\\release.py --reuse-artifact   # 只重生成报告

刻意**不做**的事：不自动 git tag、不自动上传 / 发布。tag 与分发由人确认变更日志后
再做——自动发出去的包比没有包更麻烦。
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
PC_DIR = ROOT / "pc"
DIST_DIR = ROOT / "dist"
RELEASES_DIR = ROOT / "docs" / "releases"

#: 体积合理区间（字节）：55 MB 量级正常，明显偏小说明依赖没打进去，偏大说明混入了重依赖
MIN_ARTIFACT_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_BYTES = 200 * 1024 * 1024

#: 冒烟存活时间（秒）
SMOKE_SECONDS = 3


class StepError(RuntimeError):
    """某一步失败：带上这一步的输出尾巴，报告里能直接看原因。"""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


def _load_version_tool():
    path = ROOT / "tools" / "make_version_info.py"
    spec = importlib.util.spec_from_file_location("release_make_version_info", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERSION_TOOL = _load_version_tool()


def _run(
    cmd: Sequence[str],
    *,
    cwd: Path = ROOT,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 1800,
) -> Tuple[int, str]:
    """跑一条命令，返回 (退出码, 合并输出)。输出不截断，失败时整体进报告。"""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    process = subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return process.returncode, process.stdout or ""


def _tail(text: str, lines: int = 25) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _say(line: str) -> None:
    print(line, flush=True)


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------


def step_version() -> str:
    version = VERSION_TOOL.read_version()
    _say(f"[1/6] 版本：{version}（唯一来源 pc/serialpid/__init__.py）")

    code, output = _run([sys.executable, "tools/make_version_info.py", "--check"])
    if code != 0:
        raise StepError("Windows 版本资源与包内版本不一致", output)
    _say(_tail(output, 3))
    return version


def step_git(allow_dirty: bool) -> Dict[str, str]:
    """记录 commit 与工作区状态。发布必须来自已提交的源码，除非显式 --allow-dirty。"""
    code, commit = _run(["git", "rev-parse", "--short", "HEAD"])
    if code != 0:
        raise StepError("读取 git commit 失败", commit)
    commit = commit.strip()

    code, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip() if code == 0 else "(未知)"

    code, status = _run(["git", "status", "--porcelain"])
    dirty = [
        line[3:].strip().strip('"').replace("\\", "/")
        for line in status.splitlines()
        if line.strip() and not _is_release_own_file(line[3:].strip())
    ]
    if dirty and not allow_dirty:
        raise StepError(
            "工作区有未提交改动，发布必须来自已提交的源码（确认无影响后可加 --allow-dirty）",
            "\n".join(dirty),
        )
    _say(f"[2/6] git：{branch} @ {commit}，工作区{'干净' if not dirty else f'有 {len(dirty)} 项改动（已放行）'}")
    return {"commit": commit, "branch": branch, "dirty": dirty}


def _is_release_own_file(path: str) -> bool:
    """发布脚本自己的输出（dist/ 产物与 docs/releases/ 报告）不算脏。"""
    normalised = path.strip().strip('"').replace("\\", "/")
    return normalised.startswith("dist/") or normalised.startswith("docs/releases/")


def step_tests(repo_dir: Path = ROOT) -> Dict[str, str]:
    _say("[3/6] 全量 PC 测试 ...")
    started = time.time()
    code, output = _run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=repo_dir / "pc",
        env={"QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"},
        timeout=3600,
    )
    elapsed = time.time() - started
    if code != 0:
        raise StepError("PC 测试未通过", output)

    summary = ""
    for line in output.splitlines():
        if " passed" in line or " failed" in line:
            summary = line.strip()
    failed = re.search(r"(\d+) failed", summary)
    if failed and int(failed.group(1)) > 0:
        raise StepError("PC 测试有失败用例", output)
    _say(f"      {summary or '（未解析到 pytest 汇总行）'}（{elapsed:.1f}s）")
    return {"summary": summary or "(未解析)", "elapsed": f"{elapsed:.1f}", "output": output}


def step_selftest(tmp_dir: Path, repo_dir: Path = ROOT) -> Dict[str, str]:
    _say("[4/6] 源码环境启动自检 ...")
    out_path = tmp_dir / "selftest-src.json"
    code, output = _run(
        [sys.executable, "pc/main.py", "--selftest", "--json", "--out", str(out_path)],
        cwd=repo_dir,
    )
    if code != 0 or not out_path.exists():
        raise StepError("源码环境启动自检有失败项", output + f"\n{out_path} 不存在")
    report = json.loads(out_path.read_text(encoding="utf-8"))
    _say(f"      {report.get('summary', '?')}")
    return {"summary": str(report.get("summary", "?")), "json": report}


def step_build(version: str, repo_dir: Path = ROOT) -> Path:
    """在 ``repo_dir`` 里调用平台打包脚本，返回版本化产物路径。

    ``repo_dir`` 可能是一份干净 worktree（``--clean-tree``）：那时产物落在
    worktree 的 ``dist/``，由调用方拷回主仓库。
    """
    _say(f"[5/6] 打包（源码目录 {repo_dir}）...")
    env = {"SERIALPID_PYTHON": sys.executable}
    if sys.platform.startswith("win"):
        code, output = _run(["cmd", "/c", "scripts\\build.bat"], cwd=repo_dir, env=env, timeout=2400)
    else:
        code, output = _run(["bash", "scripts/build.sh"], cwd=repo_dir, env=env, timeout=2400)
    if code != 0:
        raise StepError("打包脚本失败", output)
    for line in output.strip().splitlines()[-6:]:
        _say(f"      {line}")

    dist = repo_dir / "dist"
    versioned = dist / f"SerialPID-Tuner-v{version}.exe"
    stable = dist / "SerialPID-Tuner.exe"
    if sys.platform.startswith("win"):
        if not versioned.exists():
            raise StepError(f"打包脚本没有产出 {versioned.name}", output)
        if not stable.exists():
            raise StepError(f"打包脚本没有留下稳定名副本 {stable.name}", output)
    else:
        versioned = dist / f"SerialPID-Tuner-v{version}"
        if not versioned.exists():
            raise StepError(f"打包脚本没有产出 {versioned.name}", output)

    if repo_dir != ROOT:
        DIST_DIR.mkdir(parents=True, exist_ok=True)
        for source in (versioned, dist / ("SerialPID-Tuner.exe" if sys.platform.startswith("win") else "SerialPID-Tuner")):
            if not source.exists():
                continue
            target = DIST_DIR / source.name
            if target.exists():
                target.unlink()
            shutil.copy2(source, target)
        for json_name in ("selftest-src.json", "selftest-exe.json"):
            source = dist / json_name
            if source.exists():
                shutil.copy2(source, DIST_DIR / json_name)
        versioned = DIST_DIR / versioned.name
    return versioned


def step_freshness(artifact: Path, repo_dir: Path = ROOT) -> Dict[str, str]:
    _say("[6/6] 校验产物 ...")
    code, output = _run(
        [sys.executable, "scripts/check_artifact_freshness.py", "--require"],
        cwd=repo_dir,
    )
    lines = output.strip().splitlines()
    if code != 0:
        raise StepError("产物新鲜度检查未通过", output)
    for line in lines:
        _say(f"      {line}")
    fresh = next((line for line in lines if line.startswith("[OK]")), "")

    size = artifact.stat().st_size
    if not MIN_ARTIFACT_BYTES <= size <= MAX_ARTIFACT_BYTES:
        raise StepError(
            f"产物体积 {size / 1024 / 1024:.1f} MB 不在合理区间 "
            f"({MIN_ARTIFACT_BYTES / 1024 / 1024:.0f}–{MAX_ARTIFACT_BYTES / 1024 / 1024:.0f} MB)",
            "",
        )
    _say(f"      体积 {size / 1024 / 1024:.1f} MB，哈希计算中 ...")

    version_info = _windows_version_info(artifact)
    if sys.platform.startswith("win"):
        expected = VERSION_TOOL.dotted_quad(VERSION_TOOL.read_version())
        for key in ("FileVersion", "ProductVersion"):
            value = version_info.get(key)
            if value != expected:
                raise StepError(f"exe 版本资源 {key}={value}，期望 {expected}", "")
        _say(f"      版本资源 FileVersion={version_info.get('FileVersion')} "
             f"ProductVersion={version_info.get('ProductVersion')}")
    return {"freshness": fresh, "size_mb": f"{size / 1024 / 1024:.1f}", "version_info": version_info}


def _windows_version_info(artifact: Path) -> Dict[str, str]:
    """读 exe 的版本资源。非 Windows 平台直接返回空（没有可读的文件属性）。"""
    if not sys.platform.startswith("win"):
        return {}
    script = (
        f"$v=(Get-Item -LiteralPath '{artifact}').VersionInfo; "
        "@{FileVersion=$v.FileVersion; ProductVersion=$v.ProductVersion; "
        "LegalCopyright=$v.LegalCopyright} | ConvertTo-Json -Compress"
    )
    code, output = _run(["powershell", "-NoProfile", "-Command", script], timeout=120)
    if code != 0:
        raise StepError("读取 exe 版本资源失败", output)
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError as exc:
        raise StepError(f"版本资源不是合法 JSON：{exc}", output)


def step_smoke(artifact: Path) -> str:
    """拉起产物，等三秒确认存活。windowed 程序崩溃时不会打印任何东西，只能看进程。"""
    _say(f"      冒烟：启动 {artifact.name} 并等待 {SMOKE_SECONDS}s ...")
    process = subprocess.Popen([str(artifact)])
    try:
        time.sleep(SMOKE_SECONDS)
        if process.poll() is not None:
            raise StepError(f"产物启动后 {SMOKE_SECONDS}s 内退出，退出码 {process.returncode}")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    return f"启动后存活 {SMOKE_SECONDS}s，无提前退出"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add_clean_worktree(tmp_dir: Path) -> Path:
    """从 HEAD 拉一份干净 worktree 作为构建源。

    共享仓库里多个任务并行时，工作区随时处于"改了一半"的状态——直接在主树上
    打包，产物会混进别人未提交的半成品，测试结果也不可信。用 HEAD 的干净副本
    构建，产物与提交一一对应，"验过的"就是"发出去的"。
    """
    path = tmp_dir / "src"
    code, output = _run(["git", "worktree", "add", "--detach", "--force", str(path), "HEAD"])
    if code != 0:
        raise StepError("创建干净 worktree 失败", output)
    return path


_STABLE_COPY = "SerialPID-Tuner.exe"


def _stable_copy_path(versioned: Path) -> Optional[Path]:
    if versioned.suffix == ".exe":
        return DIST_DIR / _STABLE_COPY
    return DIST_DIR / "SerialPID-Tuner"


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _render_report(
    *,
    version: str,
    git: Dict[str, str],
    tests: Dict[str, str],
    selftest: Dict[str, str],
    artifact: Path,
    hash_hex: str,
    fresh: Dict[str, str],
    smoke: str,
    build_source: str,
    clean: bool,
    started_at: datetime.datetime,
    elapsed: float,
) -> str:
    stable = _stable_copy_path(artifact)
    stable_text = f"`{stable.relative_to(ROOT).as_posix()}`（与版本化产物同一次构建，供文档引用）"
    version_info = fresh.get("version_info") or {}
    version_lines = (
        f"- FileVersion / ProductVersion：{version_info.get('FileVersion', '-')} / "
        f"{version_info.get('ProductVersion', '-')}\n"
        + (f"- LegalCopyright：{version_info.get('LegalCopyright')}\n" if version_info else "")
    )
    dirty = list(git.get("dirty") or [])
    dirty_note = (
        f"工作区有 {len(dirty)} 项未提交改动（见文末「工作区状态」）" if dirty else "工作区干净"
    )
    dirty_section = ""
    if dirty:
        reason = (
            "它们属于其它并行任务，未进入本次产物" if clean else "已在 --allow-dirty 下放行，可能进入本次产物"
        )
        dirty_section = (
            f"\n## 工作区状态\n\n报告生成时工作区有 {len(dirty)} 项未提交改动，{reason}：\n\n"
            + "\n".join(f"- `{path}`" for path in dirty)
            + "\n"
        )
    tests_note = (
        "\n- 说明：测试在干净 worktree 中运行，只覆盖**已提交**用例；工作区里属于其它并行任务、尚未提交的用例不在其中。"
        if clean
        else ""
    )
    return (
        f"# SerialPID-Tuner v{version} 发布报告\n\n"
        f"> 本文件由 `scripts/release.py` 生成，请随版本入库；**不要手改**。\n\n"
        f"## 结论\n\n"
        f"- 版本：**v{version}**（唯一来源 `pc/serialpid/__init__.py`）\n"
        f"- 源码：`{git['branch']}` @ `{git['commit']}`；{dirty_note}\n"
        f"- 构建源：{build_source}\n"
        f"- 生成时间：{started_at.strftime('%Y-%m-%d %H:%M:%S')}（耗时 {elapsed:.1f}s）\n"
        f"- 解释器：Python {platform.python_version()}（{sys.executable}）\n"
        f"- 平台：{platform.platform()}\n\n"
        f"## 测试\n\n"
        f"- PC 全量测试：`{tests['summary']}`（{tests['elapsed']}s，`QT_QPA_PLATFORM=offscreen`）{tests_note}\n"
        f"- 长稳档：未纳入本次发布（另跑 `pytest tests/test_long_run.py --long-run`）\n\n"
        f"## 启动自检（源码环境）\n\n"
        f"- 结果：{selftest['summary']}\n\n"
        f"## 产物\n\n"
        f"- 版本化产物：`{artifact.relative_to(ROOT).as_posix()}`\n"
        f"- 稳定名副本：{stable_text}\n"
        f"- 体积：{fresh['size_mb']} MB\n"
        f"- SHA-256：`{hash_hex}`\n"
        f"{version_lines}\n"
        f"## 校验\n\n"
        f"- 产物新鲜度：{fresh['freshness']}\n"
        f"- 冒烟：{smoke}\n"
        f"- 打包产物自检：由 `scripts/build.bat` / `build.sh` 在出包时执行，"
        f"结果见 `dist/selftest-exe.json`\n\n"
        f"## 未覆盖项\n\n"
        f"- 真机联调未执行（本报告只覆盖 PC 侧离线链路）。\n"
        f"- 自动重连、链路质量统计等 card28 功能不在本版本内。\n"
        f"{dirty_section}"
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SerialPID-Tuner 一键发布（测试→自检→打包→校验→冒烟→报告）")
    parser.add_argument("--allow-dirty", action="store_true", help="允许工作区有未提交改动（排障用，正式发版不要加）")
    parser.add_argument("--clean-tree", action="store_true",
                        help="从 HEAD 的干净 worktree 构建（工作区里别人未提交的改动不进入产物）")
    parser.add_argument("--reuse-artifact", action="store_true", help="跳过打包，用 dist/ 现有产物重生成报告（排障用）")
    parser.add_argument("--report-dir", default=str(RELEASES_DIR), help="报告归档目录（默认 docs/releases/）")
    args = parser.parse_args(argv)

    started_at = datetime.datetime.now()
    started = time.time()
    tmp_dir = Path(tempfile.mkdtemp(prefix="serialpid-release-"))
    out_dir = tmp_dir / "out"
    out_dir.mkdir()
    worktree: Optional[Path] = None

    try:
        version = step_version()
        git = step_git(args.allow_dirty)

        source_dir = ROOT
        if args.clean_tree:
            worktree = _add_clean_worktree(tmp_dir)
            source_dir = worktree
            _say(f"[构建源] HEAD 的干净 worktree：{worktree}")

        tests = step_tests(source_dir)
        selftest = step_selftest(out_dir, source_dir)

        if args.reuse_artifact:
            artifact = DIST_DIR / f"SerialPID-Tuner-v{version}.exe"
            if not artifact.exists():
                raise StepError(f"--reuse-artifact 指定的产物不存在：{artifact}", "")
            _say(f"[5/6] 跳过打包，复用 {artifact.name}")
            if worktree is not None:
                # worktree 是新 checkout 的，源文件 mtime 全被刷成"现在"，和既有产物比时间戳没有意义
                raise StepError(
                    "--reuse-artifact 不能与 --clean-tree 同时使用（worktree 的 checkout 会刷新源文件时间，新鲜度无法比对）",
                    "",
                )
        else:
            artifact = step_build(version, source_dir)

        fresh = step_freshness(artifact, source_dir)
        hash_hex = _sha256(artifact)
        smoke = step_smoke(artifact)

        report = _render_report(
            version=version,
            git=git,
            tests=tests,
            selftest=selftest,
            artifact=artifact,
            hash_hex=hash_hex,
            fresh=fresh,
            smoke=smoke,
            build_source=(
                "HEAD 的干净 worktree（工作区未提交改动不进入产物）"
                if worktree is not None
                else "当前工作区"
            ),
            clean=args.clean_tree,
            started_at=started_at,
            elapsed=time.time() - started,
        )
        report_dir = Path(args.report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"v{version}.md"
        report_path.write_text(report, encoding="utf-8", newline="\n")

        _say("")
        _say(f"[完成] 发布报告：{report_path.relative_to(ROOT).as_posix()}")
        _say(f"       产物：{artifact.relative_to(ROOT).as_posix()}（{fresh['size_mb']} MB）")
        _say(f"       SHA-256：{hash_hex}")
        _say("       下一步由人确认变更日志后打 tag（脚本不自动 tag / 不自动发布）")
        return 0
    except StepError as exc:
        _say("")
        _say(f"[失败] {exc}")
        if exc.output:
            _say("---- 相关输出（末尾 25 行）----")
            _say(_tail(exc.output))
        return 1
    finally:
        if worktree is not None:
            _run(["git", "worktree", "remove", "--force", str(worktree)])
            _run(["git", "worktree", "prune"])
        # 临时目录里只有自检 JSON 与可能的 worktree 残留，直接清掉
        for child in out_dir.glob("*"):
            try:
                child.unlink()
            except OSError:
                pass
        for directory in (out_dir, tmp_dir):
            try:
                directory.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
