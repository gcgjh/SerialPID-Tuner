#!/usr/bin/env bash
# ============================================================
#  SerialPID-Tuner Linux / macOS 打包脚本
#  产物: dist/SerialPID-Tuner（单文件、无控制台）
#  用法: bash scripts/build.sh
#  备注:
#  - 解释器优先级: 仓库上一层 .venv > $PYTHON > python3；
#    实际使用的解释器路径会打印在第一行，便于排查环境问题；
#  - 依赖已装好时跳过安装，避免每次构建重下 / 污染全局环境；
#  - 工作 / 输出目录放到临时目录（ASCII 路径），规避源码路径含非 ASCII
#    字符时 PyInstaller 持久化 PYZ 失败的问题；
#  - .ico 与 Windows 版本资源仅在 Windows 打包时生效；
#    macOS 如需 Dock 图标请另备 .icns。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"

VENV_PY="$ROOT_DIR/../.venv/bin/python"
if [ -x "$VENV_PY" ]; then
  PY="$VENV_PY"
elif [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="python3"
fi

echo "[环境] 使用解释器: $PY"
"$PY" -c 'import sys; print("       版本:", sys.version.split()[0], "|", sys.executable)'

if "$PY" -c 'import PyInstaller, PIL' >/dev/null 2>&1; then
  echo "[1/4] 依赖已就绪（PyInstaller / Pillow）"
else
  echo "[1/4] 缺少 PyInstaller / Pillow，安装运行 / 打包依赖..."
  "$PY" -m pip install -r pc/requirements.txt
  "$PY" -m pip install pyinstaller pillow
fi

WORK="${TMPDIR:-/tmp}/SerialPID-Tuner-build/work"
DIST="${TMPDIR:-/tmp}/SerialPID-Tuner-build/dist"

echo "[2/4] 生成应用图标 assets/app.ico ..."
"$PY" tools/make_icon.py

echo "[3/4] PyInstaller 打包（onefile / windowed / 隐藏控制台）..."
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
echo "[4/4] 拷贝产物到 dist/ ..."
cp "$DIST/SerialPID-Tuner" dist/SerialPID-Tuner

echo "打包完成: dist/SerialPID-Tuner"
