#Requires -Version 7
<#
.SYNOPSIS
  STM32H750VBT6 烧录脚本：优先 OpenOCD（DAPLink/J-Link），其次 pyOCD。

.DESCRIPTION
  探针必须先在设备管理器/PnP 里枚举出来，否则本脚本只会报"没有探针"，不会去猜。
    DAPLink : USB VID_0D28 / VID_1209（CMSIS-DAP）
    J-Link  : USB VID_1366
    ST-Link : USB VID_0483（OpenOCD 也能用，但 hla 接口需自行改）
  固件默认烧 build\SerialPID-H750.elf（构建脚本同时产出 hex/bin）。

.PARAMETER Interface
  cmsis-dap（默认，DAPLink）或 jlink。不确定就让脚本自动判。

.PARAMETER File
  要烧的固件，默认 build\SerialPID-H750.elf。

.EXAMPLE
  pwsh -File .\flash.ps1
  pwsh -File .\flash.ps1 -Interface jlink
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'cmsis-dap', 'jlink')]
    [string]$Interface = 'auto',
    [string]$File = 'build\SerialPID-H750.elf'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath $File)) {
    throw "固件不存在: $File（先运行 .\build.ps1）"
}
$firmware = (Resolve-Path -LiteralPath $File).Path

# ---- 探针枚举（用 PnP，不靠猜 COM 口）----
$probes = Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -match 'VID_0D28|VID_1209|VID_1366|VID_0483|VID_1A86&PID_8011'
}
if (-not $probes) {
    Write-Host '当前没有枚举到调试探针（VID_0D28/1209/1366/0483/WCH-Link 全无）。' -ForegroundColor Red
    Write-Host '请先插上 DAPLink / J-Link，确认设备管理器出现 CMSIS-DAP 或 J-Link 设备，再重跑本脚本。'
    Write-Host '若只有 CH340(COM3) 而没有探针，说明 SWD 调试器还没接上，烧录无法进行。'
    exit 2
}
$probes | Select-Object Class, FriendlyName, InstanceId | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

if ($Interface -eq 'auto') {
    $hasJlink = ($probes.InstanceId -match 'VID_1366').Count -gt 0
    $hasDap = ($probes.InstanceId -match 'VID_0D28|VID_1209|VID_1A86&PID_8011').Count -gt 0
    if ($hasJlink) { $Interface = 'jlink' }
    elseif ($hasDap) { $Interface = 'cmsis-dap' }
    else { throw '检测到探针但无法判断类型（非 DAPLink/J-Link），请显式指定 -Interface' }
}

# ---- OpenOCD ----
$openocd = (Get-Command openocd -ErrorAction SilentlyContinue)?.Source
if (-not $openocd) {
    $candidate = 'S:\openocd\xpack-openocd-0.12.0-7-win32-x64\xpack-openocd-0.12.0-7\bin\openocd.exe'
    if (Test-Path -LiteralPath $candidate) { $openocd = $candidate }
}
if ($openocd) {
    $ifaceCfg = if ($Interface -eq 'jlink') { 'interface/jlink.cfg' } else { 'interface/cmsis-dap.cfg' }
    Write-Host ("OpenOCD: {0}  interface={1}" -f $openocd, $Interface)
    & $openocd -f $ifaceCfg -f 'target/stm32h7x.cfg' `
        -c 'adapter speed 1000' `
        -c "program `"$firmware`" verify reset exit"
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'FLASH OK (OpenOCD)' -ForegroundColor Green
        exit 0
    }
    Write-Host "OpenOCD 失败（exit=$LASTEXITCODE），尝试 pyOCD…" -ForegroundColor Yellow
}

# ---- pyOCD 兜底 ----
$pyocd = (Get-Command pyocd -ErrorAction SilentlyContinue)?.Source
if (-not $pyocd) {
    $candidate = 'S:\language environment\py\Scripts\pyocd.exe'
    if (Test-Path -LiteralPath $candidate) { $pyocd = $candidate }
}
if (-not $pyocd) { throw 'OpenOCD 与 pyOCD 都不可用，无法烧录' }

Write-Host ("pyOCD: {0}" -f $pyocd)
& $pyocd flash -t stm32h750xx --erase sector $firmware
if ($LASTEXITCODE -ne 0) { throw "pyOCD 烧录失败（exit=$LASTEXITCODE）" }
& $pyocd reset -t stm32h750xx
Write-Host 'FLASH OK (pyOCD)' -ForegroundColor Green
