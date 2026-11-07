# -*- coding: utf-8 -*-
"""跨平台打包与分发（card37）的静态与可执行校验。

能在这里验的只有"不需要 Linux/macOS 机器"的部分：工作流结构、打包分支命令、
图标三件、Release 附注生成、产物判据、文档口径。真实出包与 Release 挂载必须
由 CI 首跑给出——这条边界写在 ``docs/PACKAGING.md`` 第七节，不要用本文件的
绿色冒充"三平台都跑过"。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "tools"
SCRIPTS = REPO / "scripts"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

yaml = pytest.importorskip("yaml", reason="工作流结构校验需要 PyYAML")
PIL = pytest.importorskip("PIL", reason="图标校验需要 Pillow")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _version() -> str:
    sys.path.insert(0, str(REPO / "pc"))
    from serialpid import __version__

    return __version__


def _bash() -> str | None:
    """找一个能用的 bash：CI 上有，开发机（Windows）可能只有 Git 自带的。"""
    candidates = [
        shutil.which("bash"),
        r"S:\git\Git\Git\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# 工作流结构
# ---------------------------------------------------------------------------


def test_workflow_declares_three_platform_package_matrix():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = data["jobs"]
    include = jobs["package"]["strategy"]["matrix"]["include"]
    platforms = [entry["platform"] for entry in include]
    runners = {entry["platform"]: entry["os"] for entry in include}
    assert platforms == ["windows", "linux", "macos"]
    assert runners == {
        "windows": "windows-latest",
        "linux": "ubuntu-latest",
        "macos": "macos-latest",
    }, "三个平台必须各自跑在对应 runner 上（PyInstaller 不能交叉打包）"


def test_package_job_runs_selftest_and_freshness_on_every_platform():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["package"]["steps"]
    text = json.dumps(steps, ensure_ascii=False)
    assert "--selftest" in text, "出包后必须跑启动自检"
    assert "check_artifact_freshness.py --require" in text, "三个平台都要验产物新鲜度"
    # 平台分支：Windows 走 bat，其余走 sh；两条都在
    conditions = [step.get("if", "") for step in steps]
    assert any("Windows" in cond and "build.bat" in json.dumps(steps, ensure_ascii=False) for cond in conditions)


def test_package_upload_includes_archives_and_platform_selftest():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["package"]["steps"]
    upload = next(
        step
        for step in steps
        if step.get("uses", "").startswith("actions/upload-artifact")
    )
    path = upload["with"]["path"]
    assert "dist/SerialPID-Tuner-v*.tar.gz" in path
    assert "dist/SerialPID-Tuner-v*.zip" in path
    assert "dist/selftest-${{ matrix.platform }}.json" in path
    # artifact 名要带平台与版本号（卡43 第一步的核对项）
    assert upload["with"]["name"] == (
        "SerialPID-Tuner-${{ steps.ver.outputs.version }}-${{ matrix.platform }}"
    )
    assert any(
        step.get("id") == "ver" and "make_version_info.py --version" in step.get("run", "")
        for step in steps
    ), "artifact 名里的版本号必须取自包内版本，不能写死"


def test_package_job_is_a_hard_gate_with_freshness_guard():
    """矩阵 job 不许设成可跳过；构建前要清 dist，产物判据在 CI 里也要跑。"""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    package = data["jobs"]["package"]
    assert not package.get("continue-on-error"), "出包矩阵失败不能设为可跳过"
    steps = package["steps"]
    names = [step.get("name", "") for step in steps]
    assert any("构建前清空 dist" in name for name in names), "构建前必须清掉旧产物"
    assert any("校验自检结果" in name for name in names), "selftest JSON 必须校验后再上传"
    body = json.dumps(steps, ensure_ascii=False)
    assert "check_artifact_freshness.py --require" in body
    assert "%s.json" % "selftest-${{ matrix.platform }}" in body


def test_release_job_only_on_tags_and_can_write_contents():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    release = data["jobs"]["release"]
    assert release["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert release["needs"] == ["package"]
    assert release["permissions"]["contents"] == "write"
    body = json.dumps(release["steps"], ensure_ascii=False)
    assert "make_release_body.py" in body, "Release 正文必须由脚本从发布报告生成"
    assert "gh release create" in body


def test_branch_push_does_not_publish_release():
    """分支推送不许建 Release：on.push 的 tags 过滤与 job 的 if 必须同时存在。"""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 把裸 ``on`` 解析成布尔 True，取值要兼容两种写法
    triggers = data.get("on") if data.get("on") is not None else data.get(True)
    assert triggers["push"]["tags"] == ["v*"]
    assert data["jobs"]["release"]["if"].startswith("startsWith(github.ref, 'refs/tags/v')")


# ---------------------------------------------------------------------------
# 打包脚本
# ---------------------------------------------------------------------------


def test_build_sh_covers_linux_and_macos_forms():
    text = (SCRIPTS / "build.sh").read_text(encoding="utf-8")
    assert "--onedir" in text, "Linux/macOS 用 onedir（onefile 冷启动 3-6s）"
    assert "-linux-" in text and "-macos-" in text
    assert "assets/app.icns" in text, "macOS 必须用 .icns"
    assert "osx-bundle-identifier" in text
    assert "dialout" in text, "Linux 首次运行说明必须给 dialout 命令"
    assert "serialpid-udev.rules" in text
    assert "Windows 请用 scripts/build.bat" in text, "Windows 必须拒绝走 sh（不能交叉打包）"


def test_build_sh_syntax_is_valid():
    bash = _bash()
    if not bash:
        pytest.skip("本机没有 bash，语法检查留给 CI")
    result = subprocess.run([bash, "-n", str(SCRIPTS / "build.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_build_sh_dry_run_prints_platform_command(platform):
    bash = _bash()
    if not bash:
        pytest.skip("本机没有 bash，dry-run 留给 CI")
    version = _version()
    env = dict(os.environ)
    env["SERIALPID_PYTHON"] = sys.executable.replace("\\", "/")
    result = subprocess.run(
        [bash, "scripts/build.sh", "--dry-run", f"--platform={platform}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout
    assert "--onedir" in out
    if platform == "linux":
        assert f"SerialPID-Tuner-v{version}-linux-" in out
        assert "--icon assets/app.png" in out
    else:
        assert f"SerialPID-Tuner-v{version}-macos-" in out
        assert "--icon assets/app.icns" in out
        assert "--osx-bundle-identifier com.serialpid.tuner" in out


def test_build_sh_refuses_windows(capsys):
    bash = _bash()
    if not bash:
        pytest.skip("本机没有 bash")
    result = subprocess.run(
        [bash, "scripts/build.sh", "--platform", "windows"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "build.bat" in result.stdout


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------


def test_make_icon_emits_ico_png_and_icns(tmp_path):
    module = _load(TOOLS / "make_icon.py", "card37_make_icon")
    assert module.main(["--out-dir", str(tmp_path)]) == 0

    from PIL import Image

    ico = tmp_path / "app.ico"
    png = tmp_path / "app.png"
    icns = tmp_path / "app.icns"
    for path in (ico, png, icns):
        assert path.is_file(), f"{path.name} 没生成"

    with Image.open(ico) as image:
        assert image.format == "ICO"
        assert image.size == (256, 256)
    with Image.open(png) as image:
        assert image.format == "PNG"
    with Image.open(icns) as image:
        assert image.format == "ICNS"
        assert image.size == (1024, 1024), "macOS 512@2x 需要 1024 画布"
        # Pillow 会把打包进去的各档尺寸列在 info 里
        assert len(image.info.get("sizes", [])) >= 4

    # --check 在文件齐全时必须通过、缺一个时必须失败
    assert module.main(["--check", "--out-dir", str(tmp_path)]) == 0
    icns.unlink()
    assert module.main(["--check", "--out-dir", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# Release 附注
# ---------------------------------------------------------------------------


def _fake_artifacts(tmp_path: Path, version: str) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / f"SerialPID-Tuner-v{version}.exe").write_bytes(b"windows-binary")
    (dist / f"SerialPID-Tuner-v{version}-linux-x86_64.tar.gz").write_bytes(b"linux-archive")
    (dist / f"SerialPID-Tuner-v{version}-macos-arm64.zip").write_bytes(b"macos-archive")
    (dist / "selftest-linux.json").write_text(
        json.dumps({"status": "pass", "summary": "8 通过 / 0 警告 / 0 失败"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (dist / "selftest-macos.json").write_text(
        json.dumps({"status": "pass", "summary": "7 通过 / 1 警告 / 0 失败"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return dist


def test_release_body_lists_three_platforms_in_order(tmp_path):
    module = _load(SCRIPTS / "make_release_body.py", "card37_release_body")
    version = _version()
    dist = _fake_artifacts(tmp_path, version)
    out = tmp_path / "body.md"
    assert module.main(["--version", version, "--artifacts", str(dist), "--out", str(out)]) == 0

    body = out.read_text(encoding="utf-8")
    assert body.startswith(f"# SerialPID-Tuner v{version}")
    rows = [line for line in body.splitlines() if line.startswith("| ") and "SerialPID-Tuner-v" in line]
    assert [row.split("|")[1].strip() for row in rows] == ["Windows", "Linux", "macOS"]
    assert "pass · 8 通过" in body and "pass · 7 通过" in body
    for name in (".exe", "-linux-x86_64.tar.gz", "-macos-arm64.zip"):
        assert name in body
    assert "SHA256SUMS.txt" in body
    assert "sudo usermod -aG dialout" in body, "Linux 首次运行必须给权限命令"
    assert "Gatekeeper" in body
    assert "## 发布报告" in body, "正文要拼上 docs/releases 的报告"


def test_release_body_writes_sha256sums_that_verify(tmp_path):
    module = _load(SCRIPTS / "make_release_body.py", "card37_release_body_sums")
    version = _version()
    dist = _fake_artifacts(tmp_path, version)
    assert module.main(["--version", version, "--artifacts", str(dist), "--out", str(tmp_path / "b.md")]) == 0

    sums = (dist / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(sums) == 3
    for line in sums:
        digest, name = line.split("  ", 1)
        target = dist / name
        assert target.is_file()
        actual = module._sha256(target)
        assert digest == actual, f"{name} 的哈希行与实际不符"


def test_release_body_without_artifacts_says_so_explicitly(tmp_path):
    module = _load(SCRIPTS / "make_release_body.py", "card37_release_body_empty")
    dist = tmp_path / "dist"
    dist.mkdir()
    out = tmp_path / "b.md"
    capsys_code = module.main(["--version", _version(), "--artifacts", str(dist), "--out", str(out)])
    assert capsys_code == 0
    body = out.read_text(encoding="utf-8")
    assert "未找到产物" in body
    assert not (dist / "SHA256SUMS.txt").exists()


# ---------------------------------------------------------------------------
# 产物判据与文档口径
# ---------------------------------------------------------------------------


def test_freshness_accepts_archives_and_ignores_sidecars(tmp_path):
    module = _load(SCRIPTS / "check_artifact_freshness.py", "card37_freshness")
    for name in (
        "SerialPID-Tuner-v0.2.1.exe",
        "SerialPID-Tuner-v0.2.1-linux-x86_64.tar.gz",
        "SerialPID-Tuner-v0.2.1-macos-arm64.zip",
    ):
        (tmp_path / name).write_bytes(b"x")
    for name in ("selftest-exe.json", "v0.2.1.md", "x.tar.gz.sha256", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")

    names = sorted(path.name for path in module.artifacts(tmp_path))
    assert names == [
        "SerialPID-Tuner-v0.2.1-linux-x86_64.tar.gz",
        "SerialPID-Tuner-v0.2.1-macos-arm64.zip",
        "SerialPID-Tuner-v0.2.1.exe",
    ], "自检 JSON / 报告 / 校验文件不是产物，不能参与新鲜度比较"


def test_docs_carry_three_platform_and_linux_permission_instructions():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    guide = (REPO / "docs" / "user_guide.md").read_text(encoding="utf-8")
    packaging = (REPO / "docs" / "PACKAGING.md").read_text(encoding="utf-8")

    for text, label in ((readme, "README"), (guide, "用户手册")):
        assert "dialout" in text, f"{label} 缺 Linux 串口权限命令"
        assert "udev" in text, f"{label} 缺 udev 规则示例"
        assert "Gatekeeper" in text, f"{label} 缺 macOS 首次打开说明"
        assert "linux-x86_64.tar.gz" in text, f"{label} 缺 Linux 下载物命名"
        assert "macos" in text and ".zip" in text, f"{label} 缺 macOS 下载物命名"

    assert "验证边界" in packaging
    assert "未验证" in packaging, "必须写清楚哪些是 CI 才能验的"
    assert "AppImage" in packaging and "二期" in packaging


def test_icon_assets_are_committed():
    for name in ("app.ico", "app.png", "app.icns"):
        path = REPO / "assets" / name
        assert path.is_file(), f"assets/{name} 应随仓库入库（CI 也要用）"


def test_batch_files_use_crlf_and_shell_scripts_use_lf():
    """cmd 解析 LF-only 的 .bat 会把多行拼成一条（参数被拆、%DIST% 为空）。"""
    bat = (SCRIPTS / "build.bat").read_bytes()
    assert b"\r\n" in bat, "build.bat 必须是 CRLF，否则 cmd 解析会碎片化"
    assert bat.count(b"\n") == bat.count(b"\r\n"), "build.bat 不能混入裸 LF"

    sh = (SCRIPTS / "build.sh").read_bytes()
    assert b"\r" not in sh, "build.sh 必须是 LF，否则 bash 报 bad interpreter"

    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat text eol=crlf" in attributes
    assert "*.sh  text eol=lf" in attributes
