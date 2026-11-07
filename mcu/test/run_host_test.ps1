# 编译并运行 MCU 主机侧离线测试（无 HAL 依赖，MinGW / Linux gcc 均可）。
# 用法：在 mcu/test 目录下执行  pwsh ./run_host_test.ps1
$ErrorActionPreference = "Stop"

$TestDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $TestDir

$gcc = (Get-Command gcc -ErrorAction SilentlyContinue)
if (-not $gcc) {
    Write-Error "未找到 gcc，请先安装 MinGW 并加入 PATH"
    exit 2
}

$runners = @(
    @{ Name = "host_test";      Sources = @("host_test.c",     "../Src/pid_comm.c", "../Src/pid_calc.c") },
    @{ Name = "app_host_test";  Sources = @("app_host_test.c", "../Src/pid_comm.c", "../Src/pid_calc.c", "../Src/pid_app.c") }
)

$failed = 0
foreach ($r in $runners) {
    $exe = Join-Path $TestDir ($r.Name + ".exe")
    Write-Host "==> 编译 $($r.Name)"
    & gcc -std=c11 -Wall -Wextra -I../Inc -I. @($r.Sources) -o $exe
    if ($LASTEXITCODE -ne 0) { $failed++; continue }

    Write-Host "==> 运行 $($r.Name)"
    & $exe
    if ($LASTEXITCODE -ne 0) { $failed++ }
}

if ($failed -gt 0) {
    Write-Host "`n$failed 个测试运行器失败" -ForegroundColor Red
    exit 1
}
Write-Host "`n全部 MCU 主机测试通过" -ForegroundColor Green
