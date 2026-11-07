#!/usr/bin/env bash
# ============================================================
#  SerialPID-Tuner Linux / macOS 打包脚本（card37 跨平台）
#
#  产物（按平台不同）：
#    Linux : dist/SerialPID-Tuner-v<ver>-linux-<arch>.tar.gz
#            （内含 onedir 目录：可执行文件 + _internal + 桌面快捷方式 +
#              udev 规则示例 + 首次运行说明）
#            dist/SerialPID-Tuner-v<ver>-linux-<arch>/  （未打包目录，便于本地试跑）
#    macOS : dist/SerialPID-Tuner-v<ver>-macos-<arch>.zip
#            （内含 SerialPID-Tuner.app，图标为 assets/app.icns）
#
#  用法:
#    bash scripts/build.sh                # 正常打包
#    bash scripts/build.sh --check        # 只检查打包前条件（依赖 / 图标 / 版本）
#    bash scripts/build.sh --dry-run      # 打印将要执行的命令，不实际打包
#    bash scripts/build.sh --platform linux   # 在任意平台验证 Linux 分支（配合 --dry-run）
#
#  备注:
#  - 解释器优先级: SERIALPID_PYTHON > 仓库上一层 .venv > $PYTHON > python3；
#    实际使用的解释器路径会打印在第一行，便于排查环境问题；
#  - 依赖按 pc/requirements-dev.txt 里锁定的版本安装（不裸装）；
#  - 版本号取自 pc/serialpid/__init__.py 的 __version__（唯一真相）；
#  - 工作 / 输出目录放到临时目录（ASCII 路径），规避源码路径含非 ASCII
#    字符时 PyInstaller 持久化 PYZ 失败的问题；
#  - Windows 走 scripts/build.bat，本脚本不处理 Windows（PyInstaller 不能交叉打包，
#    Linux 包必须在 Linux 上打、macOS 包必须在 macOS 上打）。
#
#  为什么 Linux 用 onedir 而不是 onefile：onefile 每次启动都要把约 100 MB 解到
#  临时目录，冷启动 3-6 s；onedir 直接 exec 内部二进制的共享库，1 s 内起。
#  AppImage 作为二期（需要 linuxdeploy + FUSE），当前先给 tar.gz——解压即用，
#  不依赖 FUSE，服务器 / 无桌面环境也能跑。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"

DRY_RUN=0
CHECK_ONLY=0
FORCE_PLATFORM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --check) CHECK_ONLY=1 ;;
    --platform) FORCE_PLATFORM="${2:-}"; shift ;;
    --platform=*) FORCE_PLATFORM="${1#--platform=}" ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "[ERR] 未知参数: $1"; exit 2 ;;
  esac
  shift
done

# ---- 平台判定 -------------------------------------------------------------
UNAME_S="$(uname -s)"
case "$UNAME_S" in
  Darwin) PLATFORM="macos" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  *) PLATFORM="linux" ;;
esac
if [ -n "$FORCE_PLATFORM" ]; then
  case "$FORCE_PLATFORM" in
    linux|macos) PLATFORM="$FORCE_PLATFORM" ;;
    windows) echo "[ERR] Windows 请用 scripts/build.bat（PyInstaller 不能交叉打包）"; exit 2 ;;
    *) echo "[ERR] --platform 只支持 linux / macos"; exit 2 ;;
  esac
fi

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac

if [ "$PLATFORM" = "windows" ]; then
  echo "[ERR] Windows 请用 scripts/build.bat（PyInstaller 不能交叉打包）"
  exit 1
fi

# ---- 解释器 ---------------------------------------------------------------
VENV_PY="$ROOT_DIR/../.venv/bin/python"
if [ -n "${SERIALPID_PYTHON:-}" ] && [ -x "${SERIALPID_PYTHON}" ]; then
  PY="$SERIALPID_PYTHON"
elif [ -x "$VENV_PY" ]; then
  PY="$VENV_PY"
elif [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
else
  PY="python3"
fi

# 注意：变量后面紧跟全角字符时必须用 ${VAR}。macOS 自带的是 bash 3.2，
# 在该版本里 `$ARCH（` 会把全角括号的字节当成变量名的一部分，
# 配合 set -u 直接报 "ARCH?: unbound variable"（2026-11-07 出包 macos 就红在这）。
echo "[1/8] 平台: ${PLATFORM}-${ARCH}（${UNAME_S}）"
echo "       解释器: $PY"
"$PY" -c 'import sys; print("       版本:", sys.version.split()[0], "|", sys.executable)'

# ---- 依赖 ----------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  echo "[2/8] （dry-run 跳过依赖检查与安装）"
elif "$PY" -c 'import PyInstaller, PIL' >/dev/null 2>&1; then
  echo "[2/8] 依赖已就绪（PyInstaller / Pillow）"
else
  if [ "$CHECK_ONLY" = "1" ]; then
    echo "[ERR] 缺少 PyInstaller / Pillow：pip install -r pc/requirements-dev.txt"
    exit 1
  fi
  echo "[2/8] 缺少 PyInstaller / Pillow，按 requirements-dev.txt 安装锁定版本..."
  "$PY" -m pip install -r pc/requirements-dev.txt
fi

# ---- 版本 / 图标 ----------------------------------------------------------
echo "[3/8] 生成版本资源 scripts/version_info.txt ..."
if [ "$DRY_RUN" = "0" ]; then
  "$PY" tools/make_version_info.py
fi
VER="$("$PY" tools/make_version_info.py --version)"
echo "       版本: $VER"

echo "[4/8] 生成图标（ico / png / icns）..."
if [ "$DRY_RUN" = "0" ]; then
  "$PY" tools/make_icon.py
fi
for f in assets/app.ico assets/app.png assets/app.icns; do
  if [ ! -f "$f" ] && [ "$DRY_RUN" = "0" ]; then
    echo "[ERR] 图标缺失: ${f}（跑 python tools/make_icon.py）"
    exit 1
  fi
done

# ---- 目标路径 ------------------------------------------------------------
SHORT="SerialPID-Tuner"
WORK="${TMPDIR:-/tmp}/SerialPID-Tuner-build/work"
PYDIST="${TMPDIR:-/tmp}/SerialPID-Tuner-build/dist"
mkdir -p dist

BUNDLE_EXTRA=()
if [ "$PLATFORM" = "linux" ]; then
  ARTIFACT="dist/${SHORT}-v${VER}-linux-${ARCH}.tar.gz"
  PAYLOAD="dist/${SHORT}-v${VER}-linux-${ARCH}"
  ICON_ARG="assets/app.png"
else
  ARTIFACT="dist/${SHORT}-v${VER}-macos-${ARCH}.zip"
  PAYLOAD="dist/${SHORT}-v${VER}-macos-${ARCH}"
  ICON_ARG="assets/app.icns"
  # 必须拆成两个数组元素。写成单个含空格的字符串 "--osx-bundle-identifier
  # com.serialpid.tuner" 时，PyInstaller 的 argparse 会把整串当成一个未知选项，
  # 连后面的脚本名一起报 "unrecognized arguments"（2026-11-07 出包 macos）。
  BUNDLE_EXTRA=(--osx-bundle-identifier com.serialpid.tuner)
fi

echo "[5/8] PyInstaller 打包（onedir / windowed）..."
PYI_CMD=(
  "$PY" -m PyInstaller --noconfirm --clean --windowed --onedir
  ${BUNDLE_EXTRA[@]+"${BUNDLE_EXTRA[@]}"}
  --name "$SHORT"
  --paths pc
  --icon "$ICON_ARG"
  --add-data "assets:assets"
  --workpath "$WORK" --distpath "$PYDIST"
  --exclude-module torch --exclude-module torchvision --exclude-module cv2
  --exclude-module tensorflow --exclude-module transformers --exclude-module scipy
  --exclude-module matplotlib --exclude-module pygame --exclude-module onnxruntime
  --exclude-module sympy --exclude-module pandas --exclude-module PIL
  --hidden-import PySide6.QtCore
  --hidden-import PySide6.QtGui
  --hidden-import PySide6.QtWidgets
  pc/main.py
)

if [ "$DRY_RUN" = "1" ]; then
  echo "       [dry-run] ${PYI_CMD[*]}"
  echo "       [dry-run] 产物: $ARTIFACT"
  echo "       [dry-run] 自检（源码 + 打包产物，各一次）"
  exit 0
fi

"${PYI_CMD[@]}"

# ---- 落盘为可分发包 ------------------------------------------------------
echo "[6/8] 组装分发包: $PAYLOAD ..."
rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD"

if [ "$PLATFORM" = "linux" ]; then
  cp -a "$PYDIST/$SHORT" "$PAYLOAD/$SHORT"
  cat > "$PAYLOAD/${SHORT}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SerialPID-Tuner
Comment=串口 PID 调试上位机
Exec=$SHORT/$SHORT %F
Icon=$SHORT-icon
Terminal=false
Categories=Development;Engineering;
EOF
  cp assets/app.png "$PAYLOAD/${SHORT}-icon.png"
  cat > "$PAYLOAD/serialpid-udev.rules" <<'EOF'
# /etc/udev/rules.d/99-serialpid.rules
# 让当前用户免 sudo 访问常见 USB 转串口（CH340 / CP210x / FTDI / CDC-ACM）。
# 装法：sudo cp serialpid-udev.rules /etc/udev/rules.d/99-serialpid.rules
#       sudo udevadm control --reload-rules && sudo udevadm trigger
#       sudo usermod -aG dialout "$USER"   # 重新登录生效
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", MODE="0666"
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", MODE="0666"
EOF
  cat > "$PAYLOAD/首次运行.md" <<'EOF'
# 首次运行（Linux）

1. 解压后直接执行 `./SerialPID-Tuner/SerialPID-Tuner`（不要单独拷可执行文件出去，
   onedir 的 `_internal/` 必须跟它同目录）。
2. 串口打不开、报"权限不足"时，先把自己加入 dialout 组再重新登录：

       sudo usermod -aG dialout "$USER"

   还不行就装随包的 udev 规则（见 `serialpid-udev.rules` 头两行注释）。
3. 缺少 `libGL.so.1` / `libEGL.so.1` 时（最小化安装的发行版常见）：

       sudo apt install -y libgl1 libegl1 libxkbcommon0 libdbus-1-3   # Debian/Ubuntu
       sudo dnf install -y mesa-libGL mesa-libEGL                     # Fedora

4. 无显示器 / 服务器上跑自检：`./SerialPID-Tuner/SerialPID-Tuner --selftest --json`
EOF
else
  cp -R "$PYDIST/$SHORT.app" "$PAYLOAD/$SHORT.app"
  cat > "$PAYLOAD/首次运行.md" <<'EOF'
# 首次运行（macOS）

1. 解压后把 `SerialPID-Tuner.app` 拖进「应用程序」。
2. 首次打开会被 Gatekeeper 拦下（未签名）：右键点图标选「打开」，或在
   「系统设置 → 隐私与安全性」里点「仍要打开」。之后可正常双击。
3. 只想验证程序完整性，可在终端跑：
   `SerialPID-Tuner.app/Contents/MacOS/SerialPID-Tuner --selftest --json`
4. 串口设备若被系统拒绝，确认驱动（CH340 / CP210x）已安装，并在
   「隐私与安全性 → 允许配件连接」里放行。
EOF
fi

echo "[7/8] 出包后自检（源码 + 打包产物）..."
SELF_SRC="${TMPDIR:-/tmp}/SerialPID-Tuner-build/selftest-src.json"
SELF_EXE="${TMPDIR:-/tmp}/SerialPID-Tuner-build/selftest-exe.json"
"$PY" pc/main.py --selftest --json --out "$SELF_SRC"
if [ "$PLATFORM" = "linux" ]; then
  BIN="$PAYLOAD/$SHORT/$SHORT"
else
  BIN="$PAYLOAD/$SHORT.app/Contents/MacOS/$SHORT"
fi
"$BIN" --selftest --json --out "$SELF_EXE"
mkdir -p dist
cp "$SELF_SRC" dist/selftest-src.json
cp "$SELF_EXE" "dist/selftest-${PLATFORM}.json"
cp "$SELF_EXE" dist/selftest-exe.json
echo "       自检通过: dist/selftest-src.json / dist/selftest-${PLATFORM}.json"

echo "[8/8] 打归档: $ARTIFACT ..."
rm -f "$ARTIFACT"
if [ "$PLATFORM" = "linux" ]; then
  tar -czf "$ARTIFACT" -C "$(dirname "$PAYLOAD")" "$(basename "$PAYLOAD")"
else
  # .app 是目录束：用 ditto 保留符号链接与资源分叉（zip 命令会破坏签名结构）。
  # 连 .app 与首次运行说明一起装进顶层目录，解压后是一份可直接拖走的文件夹。
  if command -v ditto >/dev/null 2>&1; then
    ditto -c -k --sequesterRsrc --keepParent "$PAYLOAD" "$ARTIFACT"
  else
    ( cd "$(dirname "$PAYLOAD")" && zip -qry "../../$ARTIFACT" "$(basename "$PAYLOAD")" )
  fi
fi
if command -v sha256sum >/dev/null 2>&1; then
  SUM="$(sha256sum "$ARTIFACT" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  SUM="$(shasum -a 256 "$ARTIFACT" | cut -d' ' -f1)"
else
  SUM="$("$PY" -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$ARTIFACT")"
fi
SIZE="$(du -h "$ARTIFACT" | cut -f1)"
echo "$SUM  $(basename "$ARTIFACT")" > "${ARTIFACT}.sha256"
echo "打包完成: ${ARTIFACT}（${SIZE}）"
echo "SHA-256: $SUM"
echo "未打包目录: ${PAYLOAD}（本地试跑用，不入库）"
