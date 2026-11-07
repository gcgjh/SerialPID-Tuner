@echo off
rem ============================================================
rem  SerialPID-Tuner Windows 一键打包脚本
rem  产物: dist\SerialPID-Tuner-v<版本>.exe（单文件、无控制台、带图标与版本信息）
rem        dist\SerialPID-Tuner.exe（稳定名副本，供文档 / 旧链接引用）
rem  用法: 双击本脚本，或在仓库根目录执行 scripts\build.bat
rem
rem  说明:
rem  - 解释器优先级: 仓库上一层 .venv 内的 python.exe > PATH 中的 python；
rem    实际使用的解释器路径会在第一行打印，便于排查环境问题；
rem  - 依赖按 pc\requirements-dev.txt 里锁定的版本安装（不裸装），保证换机器 / 换时间
rem    打出来的包可复现；
rem  - 版本号取自 pc\serialpid\__init__.py 的 __version__（唯一真相），
rem    scripts\version_info.txt 由 tools\make_version_info.py 在打包前重新生成；
rem  - 工作 / 输出目录放到系统临时目录（ASCII 路径），规避部分环境下
rem    仓库路径含中文时 PyInstaller 持久化 PYZ 失败的问题；
rem  - 打包完成后再把 exe 拷回仓库 dist\ 目录。
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

set "VENV_PY=%~dp0..\..\.venv\Scripts\python.exe"
set "PY="
rem 外部可指定解释器：release.py 的干净 worktree 构建与 CI 复用已有环境都靠它
if defined SERIALPID_PYTHON if exist "%SERIALPID_PYTHON%" set "PY=%SERIALPID_PYTHON%"
if not defined PY if exist "%VENV_PY%" set "PY=%VENV_PY%"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERR] 未找到项目环境 "%~dp0..\..\.venv\Scripts\python.exe"，且 PATH 中也没有 python
    echo       请先安装 Python 3.10+，或在仓库上一层创建 .venv（见 README 快速开始）
    exit /b 1
  )
  set "PY=python"
)
echo [1/7] 使用解释器: %PY%
"%PY%" -c "import sys; print('       版本:', sys.version.split()[0], '|', sys.executable)"

"%PY%" -c "import PyInstaller, PIL" >nul 2>nul
if errorlevel 1 (
  echo [2/7] 缺少 PyInstaller / Pillow，按 requirements-dev.txt 安装锁定版本...
  "%PY%" -m pip install -r pc\requirements-dev.txt
) else (
  echo [2/7] 依赖已就绪（PyInstaller / Pillow）
)

echo [3/7] 生成版本资源 scripts\version_info.txt ...
"%PY%" tools\make_version_info.py
if errorlevel 1 exit /b 1
set "VERFILE=%TEMP%\serialpid-version.txt"
"%PY%" tools\make_version_info.py --version > "%VERFILE%"
set "VER="
set /p VER=<"%VERFILE%"
if not defined VER (
  echo [ERR] 读取版本号失败
  exit /b 1
)
echo       版本: %VER%

echo [4/7] 生成应用图标 assets\app.ico ...
"%PY%" tools\make_icon.py
if errorlevel 1 exit /b 1

set "WORK=%TEMP%\SerialPID-Tuner-build\work"
set "DIST=%TEMP%\SerialPID-Tuner-build\dist"

echo [5/7] PyInstaller 打包（onefile / windowed / 隐藏控制台）...
"%PY%" -m PyInstaller --noconfirm --clean --windowed --onefile ^
  --name SerialPID-Tuner ^
  --paths pc ^
  --icon assets\app.ico ^
  --version-file scripts\version_info.txt ^
  --add-data "assets;assets" ^
  --workpath "%WORK%" --distpath "%DIST%" ^
  --exclude-module torch --exclude-module torchvision --exclude-module cv2 ^
  --exclude-module tensorflow --exclude-module transformers --exclude-module scipy ^
  --exclude-module matplotlib --exclude-module pygame --exclude-module onnxruntime ^
  --exclude-module sympy --exclude-module pandas --exclude-module PIL ^
  --hidden-import PySide6.QtCore ^
  --hidden-import PySide6.QtGui ^
  --hidden-import PySide6.QtWidgets ^
  pc\main.py
if errorlevel 1 (
  echo [ERR] PyInstaller 打包失败
  exit /b 1
)

if not exist dist mkdir dist
echo [6/7] 拷贝产物到 dist\（版本化 + 稳定名副本）...
copy /Y "%DIST%\SerialPID-Tuner.exe" "dist\SerialPID-Tuner-v%VER%.exe"
if errorlevel 1 exit /b 1
copy /Y "%DIST%\SerialPID-Tuner.exe" "dist\SerialPID-Tuner.exe"
if errorlevel 1 exit /b 1


echo [7/7] 出包后自检 - card22，无控制台 exe 用 --selftest 落盘 JSON ...
rem 源码环境：确认依赖、配置/日志目录、图标、协议往返都正常
"%PY%" pc\main.py --selftest --json --out "%DIST%\selftest-src.json"
if errorlevel 1 (
  echo [ERR] 源码环境自检有失败项，明细见 %DIST%\selftest-src.json
  copy /Y "%DIST%\selftest-src.json" dist\selftest-src.json >nul
  exit /b 1
)
rem 打包产物：windowed 进程没有 stdout，必须 --out；cmd 跑 GUI 程序用 start /wait 才等得到退出码
start /wait "" "%DIST%\SerialPID-Tuner.exe" --selftest --json --out "%DIST%\selftest-exe.json"
if errorlevel 1 (
  echo [ERR] 打包产物自检有失败项，明细见 %DIST%\selftest-exe.json
  copy /Y "%DIST%\selftest-exe.json" dist\selftest-exe.json >nul
  exit /b 1
)
if not exist "%DIST%\selftest-exe.json" (
  echo [ERR] 打包产物自检没有产出结果文件: %DIST%\selftest-exe.json
  exit /b 1
)
copy /Y "%DIST%\selftest-src.json" dist\selftest-src.json >nul
copy /Y "%DIST%\selftest-exe.json" dist\selftest-exe.json >nul
echo 自检通过: dist\selftest-src.json / dist\selftest-exe.json

echo 打包完成: dist\SerialPID-Tuner-v%VER%.exe（稳定名副本 dist\SerialPID-Tuner.exe）
endlocal
