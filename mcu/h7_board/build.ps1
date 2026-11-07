#Requires -Version 7
<#
.SYNOPSIS
  STM32H750VBT6 HAL 工程构建脚本（arm-none-eabi-gcc，免 make）。

.PARAMETER RealIO
  编真实 IO 固件（TIM3 PWM + ADC1）。默认编仿真对象固件（只跑 UART/协议）。

.PARAMETER Clean
  先删 build 目录再全量编译。

.EXAMPLE
  pwsh -File .\build.ps1
  pwsh -File .\build.ps1 -RealIO
#>
[CmdletBinding()]
param(
    [switch]$RealIO,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

# ---- 工具链定位（优先 PATH，回落到本机已验证安装位置）----
$gcc = (Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue)?.Source
if (-not $gcc) {
    $fallback = 'S:\arm_gcc\arm-gnu-toolchain-15.3.rel1-mingw-w64-i686-arm-none-eabi\bin\arm-none-eabi-gcc.exe'
    if (Test-Path -LiteralPath $fallback) { $gcc = $fallback }
}
if (-not $gcc) { throw 'arm-none-eabi-gcc 不在 PATH，也未找到本机安装位置' }
$toolDir = Split-Path -Parent $gcc
$objcopy = Join-Path $toolDir 'arm-none-eabi-objcopy.exe'
$size = Join-Path $toolDir 'arm-none-eabi-size.exe'
foreach ($t in @($objcopy, $size)) {
    if (-not (Test-Path -LiteralPath $t)) { throw "缺少工具: $t" }
}

$build = Join-Path $root 'build'
if ($Clean -and (Test-Path -LiteralPath $build)) {
    Remove-Item -LiteralPath $build -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $build | Out-Null

$sim = if ($RealIO) { 0 } else { 1 }
$common = @(
    '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard',
    '-O2', '-Wall', '-Wextra', '-std=gnu11', '-Wno-sign-compare', '-Wno-unused-parameter', '-Wno-header-guard',
    '-ffunction-sections', '-fdata-sections', '-fno-common'
)
$defs = @('-DUSE_HAL_DRIVER', '-DSTM32H750xx', "-DPID_DEMO_SIMULATED_PLANT=$sim")
$incs = @(
    '-ICore/Inc',
    '-IDrivers/STM32H7xx_HAL_Driver/Inc',
    '-IDrivers/CMSIS/Include',
    '-I../Inc'
)

$sources = @(
    'Core/Src/board.c',
    'Core/Src/stm32h7xx_it.c',
    '../Src/main_example.c',
    '../Src/pid_app.c',
    '../Src/pid_calc.c',
    '../Src/pid_comm.c',
    'Drivers/CMSIS/Include/system_stm32h7xx.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_cortex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_gpio.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_dma_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_tim.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_tim_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_adc.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_adc_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr_ex.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash.c',
    'Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash_ex.c'
)

$objects = @()
$warnings = 0
foreach ($src in $sources) {
    if (-not (Test-Path -LiteralPath $src)) { throw "源文件不存在: $src" }
    $obj = Join-Path $build (([IO.Path]::GetFileNameWithoutExtension($src)) + '.o')
    $gccArgs = @('-c') + $common + $defs + $incs + @($src, '-o', $obj)
    $out = & $gcc @gccArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | ForEach-Object { Write-Host $_ }
        throw "编译失败: $src"
    }
    if ($out) { $warnings++; $out | ForEach-Object { Write-Host $_ } }
    $objects += $obj
}

# 启动文件
$startup = 'Core/Startup/startup_stm32h750xx.s'
$startupObj = Join-Path $build 'startup_stm32h750xx.o'
$out = & $gcc @('-c') @($common + $defs + $incs + @($startup, '-o', $startupObj)) 2>&1
if ($LASTEXITCODE -ne 0) {
    $out | ForEach-Object { Write-Host $_ }
    throw "汇编失败: $startup"
}
$objects += $startupObj

# 链接
$elf = Join-Path $build 'SerialPID-H750.elf'
$linkFlags = @(
    '-mcpu=cortex-m7', '-mthumb', '-mfpu=fpv5-d16', '-mfloat-abi=hard',
    '-TCore/Startup/STM32H750VBTx_FLASH.ld',
    '-Wl,--gc-sections',
    "-Wl,-Map=$build\SerialPID-H750.map,--print-memory-usage",
    '-specs=nano.specs', '-specs=nosys.specs'
)
$out = & $gcc @objects @linkFlags '-o' $elf 2>&1
if ($LASTEXITCODE -ne 0) {
    $out | ForEach-Object { Write-Host $_ }
    throw '链接失败'
}
$out | ForEach-Object { Write-Host $_ }

# hex / bin
$hex = Join-Path $build 'SerialPID-H750.hex'
$bin = Join-Path $build 'SerialPID-H750.bin'
& $objcopy '-O' 'ihex' $elf $hex
if ($LASTEXITCODE -ne 0) { throw 'objcopy hex 失败' }
& $objcopy '-O' 'binary' $elf $bin
if ($LASTEXITCODE -ne 0) { throw 'objcopy bin 失败' }

& $size $elf
if ($LASTEXITCODE -ne 0) { throw 'size 失败' }

Write-Host ''
Write-Host ("BUILD OK  mode={0}  warnings={1}" -f $(if ($RealIO) { 'REAL_IO' } else { 'SIMULATED_PLANT' }), $warnings)
Write-Host ("ELF : {0}" -f $elf)
Write-Host ("HEX : {0}" -f $hex)
Write-Host ("BIN : {0}" -f $bin)
