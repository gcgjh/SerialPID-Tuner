@echo off
rem ============================================================
rem  SerialPID-Tuner Windows 一键打包脚本
rem  产物: dist\SerialPID-Tuner.exe（单文件、无控制台、带图标与版本信息）
rem  用法: 双击本脚本，或在仓库根目录执行 scripts\build.bat
rem
rem  说明:
rem  - 解释器优先级: 仓库上一层 .venv 内的 python.exe > PATH 中的 python；
rem    实际使用的解释器路径会在第一行打印，便于排查环境问题；
rem  - 依赖已装好时跳过安装，避免每次构建重下 / 污染全局环境；
rem  - 工作 / 输出目录放到系统临时目录（ASCII 路径），规避部分环境下
rem    仓库路径含中文时 PyInstaller 持久化 PYZ 失败的问题；
rem  - 打包完成后再把 exe 拷回仓库 dist\ 目录。
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

set "VENV_PY=%~dp0..\..\.venv\Scripts\python.exe"
set "PY="
if exist "%VENV_PY%" set "PY=%VENV_PY%"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [ERR] 未找到项目环境 "%~dp0..\..\.venv\Scripts\python.exe"，且 PATH 中也没有 python
    echo       请先安装 Python 3.10+，或在仓库上一层创建 .venv（见 README 快速开始）
    exit /b 1
  )
  set "PY=python"
)
echo [环境] 使用解释器: %PY%
"%PY%" -c "import sys; print('       版本:', sys.version.split()[0], '|', sys.executable)"

"%PY%" -c "import PyInstaller, PIL" >nul 2>nul
if errorlevel 1 (
  echo [1/4] 缺少 PyInstaller / Pillow，安装运行 / 打包依赖...
  "%PY%" -m pip install -r pc\requirements.txt
  "%PY%" -m pip install pyinstaller pillow
) else (
  echo [1/4] 依赖已就绪（PyInstaller / Pillow）
)

echo [2/4] 生成应用图标 assets\app.ico ...
"%PY%" tools\make_icon.py
if errorlevel 1 exit /b 1

set "WORK=%TEMP%\SerialPID-Tuner-build\work"
set "DIST=%TEMP%\SerialPID-Tuner-build\dist"

echo [3/4] PyInstaller 打包（onefile / windowed / 隐藏控制台）...
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
echo [4/4] 拷贝产物到 dist\ ...
copy /Y "%DIST%\SerialPID-Tuner.exe" dist\SerialPID-Tuner.exe
if errorlevel 1 exit /b 1

echo 打包完成: dist\SerialPID-Tuner.exe
endlocal
