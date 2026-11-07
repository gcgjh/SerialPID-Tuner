#!/usr/bin/env bash
# ============================================================
#  SerialPID-Tuner Linux / macOS 打包脚本
#  产物: dist/SerialPID-Tuner-v<版本>（单文件、无控制台）
#        dist/SerialPID-Tuner（稳定名副本，供文档 / 旧链接引用）
#  用法: bash scripts/build.sh
#  备注:
#  - 解释器优先级: 仓库上一层 .venv > $PYTHON > python3；
#    实际使用的解释器路径会打印在第一行，便于排查环境问题；
#  - 依赖按 pc/requirements-dev.txt 里锁定的版本安装（不裸装）；
#  - 版本号取自 pc/serialpid/__init__.py 的 __version__（唯一真相）；
#  - 工作 / 输出目录放到临时目录（ASCII 路径），规避源码路径含非 ASCII
#    字符时 PyInstaller 持久化 PYZ 失败的问题；
#  - .ico 与 Windows 版本资源仅在 Windows 打包时生效；
#    macOS 如需 Dock 图标请另备 .icns。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"

VENV_PY="$ROOT_DIR/../.venv/bin/python"
# 外部可指定解释器：release.py 的干净 worktree 构建与 CI 复用已有环境都靠它
if [ -n "${SERIALPID_PYTHON:-}" ] && [ -x "${SERIALPID_PYTHON}" ]; then
  PY="$SERIALPID_PYTHON"
elif [ -x "$VENV_PY" ]; then
  PY="$VENV_PY"
elif [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="python3"
fi

echo "[1/6] 使用解释器: $PY"
"$PY" -c 'import sys; print("       版本:", sys.version.split()[0], "|", sys.executable)'

if "$PY" -c 'import PyInstaller, PIL' >/dev/null 2>&1; then
  echo "[2/6] 依赖已就绪（PyInstaller / Pillow）"
else
  echo "[2/6] 缺少 PyInstaller / Pillow，按 requirements-dev.txt 安装锁定版本..."
  "$PY" -m pip install -r pc/requirements-dev.txt
fi

echo "[3/6] 生成版本资源 scripts/version_info.txt ..."
"$PY" tools/make_version_info.py
VER="$("$PY" tools/make_version_info.py --version)"
echo "       版本: $VER"

WORK="${TMPDIR:-/tmp}/SerialPID-Tuner-build/work"
DIST="${TMPDIR:-/tmp}/SerialPID-Tuner-build/dist"

echo "[4/6] 生成应用图标 assets/app.ico ..."
"$PY" tools/make_icon.py

echo "[5/6] PyInstaller 打包（onefile / windowed / 隐藏控制台）..."
"$PY" -m PyInstaller --noconfirm --clean --windowed --onefile \
  --name SerialPID-Tuner \
  --paths pc \
  --icon assets/app.ico \
  --add-data "assets:assets" \
  --workpath "$WORK" --distpath "$DIST" \
  --exclude-module torch --exclude-module torchvision --exclude-module cv2 \
  --exclude-module tensorflow --exclude-module transformers --exclude-module scipy \
  --exclude-module matplotlib --exclude-module pygame --exclude-module onnxruntime \
  --exclude-module sympy --exclude-module pandas --exclude-module PIL \
  --hidden-import PySide6.QtCore \
  --hidden-import PySide6.QtGui \
  --hidden-import PySide6.QtWidgets \
  pc/main.py

mkdir -p dist
cp "$DIST/SerialPID-Tuner" "dist/SerialPID-Tuner-v$VER"
cp "$DIST/SerialPID-Tuner" dist/SerialPID-Tuner


echo "[6/6] 出包后自检（card22）..."
"$PY" pc/main.py --selftest --json --out "$DIST/selftest-src.json" || {
  echo "[ERR] 源码环境自检有失败项，明细见 $DIST/selftest-src.json"
  cp "$DIST/selftest-src.json" dist/ 2>/dev/null || true
  exit 1
}
"$DIST/SerialPID-Tuner" --selftest --json --out "$DIST/selftest-exe.json" || {
  echo "[ERR] 打包产物自检有失败项，明细见 $DIST/selftest-exe.json"
  cp "$DIST/selftest-exe.json" dist/ 2>/dev/null || true
  exit 1
}
cp "$DIST/selftest-src.json" dist/selftest-src.json
cp "$DIST/selftest-exe.json" dist/selftest-exe.json
echo "自检通过: dist/selftest-src.json / dist/selftest-exe.json"
echo "打包完成: dist/SerialPID-Tuner-v$VER（稳定名副本 dist/SerialPID-Tuner）"
