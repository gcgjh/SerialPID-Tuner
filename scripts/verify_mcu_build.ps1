# =============================================================================
# MCU 固件 ARM 编译验证（card15）
#
# 为什么有这个脚本：核心三模块（pid_comm / pid_calc / pid_app）早就验证过能编，
# 但两份 HAL 集成示例（main_example.c / pid_nvm_stm32_example.c）从来没被编译过，
# 于是"示例里一个改名的 HAL 宏"能让用户排查一整天。这个脚本把两份示例也拉进每次
# 回归：桩环境下三族各编一遍，机器上若有真实 STM32Cube 包再对着真实 HAL 编一遍。
#
# 用法（仓库根目录）：
#   pwsh scripts/verify_mcu_build.ps1
#   pwsh scripts/verify_mcu_build.ps1 -SkipRealHal          # 只用桩，不找 Cube 包
#   pwsh scripts/verify_mcu_build.ps1 -OutDir C:\tmp\objs   # 指定中间产物目录
#
# 判定标准：每一项都必须 exit 0 且**零告警**（-Wall -Wextra）。
# 任一不符 → 退出码 1，方便挂到 CI / 发布前检查清单里。
#
# 真实 HAL 的来源：环境变量 SERIALPID_CUBE_REPO 指定的目录，或
# %USERPROFILE%\STM32Cube\Repository（CubeMX 的默认仓库）。找不到就自动跳过，
# 不判失败——桩验证是硬门槛，真实 HAL 是加分项。
# =============================================================================

param(
    [string]$OutDir = "",
    [switch]$SkipRealHal
)

$ErrorActionPreference = "Stop"

# 脚本放在 scripts/ 下，仓库根是上一级
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $RepoRoot
try {

$compiler = Get-Command arm-none-eabi-gcc -ErrorAction SilentlyContinue
if (-not $compiler) {
    Write-Host "找不到 arm-none-eabi-gcc（Arm GNU Toolchain），请先装好并加入 PATH" -ForegroundColor Red
    exit 2
}

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path ([IO.Path]::GetTempPath()) "serialpid_mcu_build"
}
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
}

$script:Total = 0
$script:Failed = 0
$script:Warned = 0

function Invoke-OneBuild {
    param(
        [string]$Label,
        [string]$Source,
        [string[]]$FlagList
    )

    $obj = Join-Path $OutDir (([IO.Path]::GetFileNameWithoutExtension($Source)) + ".o")
    $raw = & arm-none-eabi-gcc @FlagList -c $Source -o $obj 2>&1
    $exitCode = $LASTEXITCODE
    $lines = @($raw | ForEach-Object { "$_" })
    # 只统计"我们自己的源码"报出的告警：vendor 头文件的告警不该让本仓背锅
    $ourWarnings = @($lines | Where-Object { $_ -match "warning:" -and $_ -match "mcu[\\/]Src" })
    $errors = @($lines | Where-Object { $_ -match "error:" })

    $script:Total++
    if ($exitCode -ne 0) { $script:Failed++ }
    if ($ourWarnings.Count -gt 0) { $script:Warned++ }

    $status = if ($exitCode -eq 0 -and $ourWarnings.Count -eq 0) { "PASS" } else { "FAIL" }
    "{0,-4} {1,-42} exit={2} warn={3}" -f $status, $Label, $exitCode, $ourWarnings.Count

    foreach ($l in @($errors | Select-Object -First 3)) { "        " + $l.Trim() }
    foreach ($l in @($ourWarnings | Select-Object -First 3)) { "        " + $l.Trim() }
}

# ---------------------------------------------------------------- 1. 核心三模块
Write-Host "== 核心模块（cortex-m4，无 HAL 依赖）==" -ForegroundColor Cyan
foreach ($src in @("mcu/Src/pid_comm.c", "mcu/Src/pid_calc.c", "mcu/Src/pid_app.c")) {
    Invoke-OneBuild ("core " + [IO.Path]::GetFileName($src)) $src @(
        "-mcpu=cortex-m4", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
        "-I", "mcu/Inc"
    )
}

# ---------------------------------------------------------------- 2. 示例 × 三族（桩）
Write-Host "== HAL 集成示例（mcu/stubs 桩环境，F1/F4/H7 各一遍）==" -ForegroundColor Cyan
$families = @(
    @{ Id = 1; Cpu = "cortex-m3"; Name = "F1" },
    @{ Id = 4; Cpu = "cortex-m4"; Name = "F4" },
    @{ Id = 7; Cpu = "cortex-m7"; Name = "H7" }
)
$exampleSources = @("mcu/Src/main_example.c", "mcu/Src/pid_nvm_stm32_example.c")
foreach ($fam in $families) {
    foreach ($src in $exampleSources) {
        Invoke-OneBuild ("stub " + $fam.Name + " " + [IO.Path]::GetFileName($src)) $src @(
            "-mcpu=$($fam.Cpu)", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
            "-I", "mcu/Inc", "-I", "mcu/stubs",
            "-DPID_HAL_STUB=1", "-DPID_HAL_STUB_FAMILY=$($fam.Id)"
        )
    }
}
# TIM6 时基是可选分支（默认走 SysTick），单独编一次保证这条分支不腐坏
Invoke-OneBuild "stub F1 main_example (PID_EXAMPLE_USE_TIM6=1)" "mcu/Src/main_example.c" @(
    "-mcpu=cortex-m3", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
    "-I", "mcu/Inc", "-I", "mcu/stubs",
    "-DPID_HAL_STUB=1", "-DPID_HAL_STUB_FAMILY=1", "-DPID_EXAMPLE_USE_TIM6=1"
)

# ---------------------------------------------------------------- 2b. 真实 IO 分支
# main_example.c 里 PWM + ADC 那半边代码只在 PID_DEMO_SIMULATED_PLANT=0 时参与编译。
# 不单独编一次，就等于"上板用的那份代码"从来没进过编译验证——这正是卡29 要补的洞。
Write-Host "== 真实 IO 分支（PWM + ADC，PID_DEMO_SIMULATED_PLANT=0）==" -ForegroundColor Cyan
foreach ($fam in $families) {
    Invoke-OneBuild ("stub " + $fam.Name + " main_example (REAL IO)") "mcu/Src/main_example.c" @(
        "-mcpu=$($fam.Cpu)", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
        "-I", "mcu/Inc", "-I", "mcu/stubs",
        "-DPID_HAL_STUB=1", "-DPID_HAL_STUB_FAMILY=$($fam.Id)",
        "-DPID_DEMO_SIMULATED_PLANT=0"
    )
}

# ---------------------------------------------------------------- 3. 真实 HAL 交叉验证
if (-not $SkipRealHal) {
    $repoCandidates = @()
    if ($env:SERIALPID_CUBE_REPO) { $repoCandidates += $env:SERIALPID_CUBE_REPO }
    $repoCandidates += (Join-Path $env:USERPROFILE "STM32Cube\Repository")

    function Find-CubePackage {
        param([string]$Family)
        $best = $null
        $bestVer = $null
        foreach ($root in $script:repoCandidates) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            foreach ($dir in (Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue)) {
                if ($dir.Name -match ("^STM32Cube_FW_" + $Family + "_V(\d+\.\d+\.\d+)$")) {
                    $ver = [version]$Matches[1]
                    if (($null -eq $bestVer) -or ($ver -gt $bestVer)) {
                        $bestVer = $ver
                        $best = $dir.FullName
                    }
                }
            }
        }
        return $best
    }

    $realTargets = @(
        @{ Family = "F1"; Cpu = "cortex-m3"; Device = "STM32F103xB"; Conf = "mcu/stubs/real_hal/f1"; HalDir = "STM32F1xx_HAL_Driver"; DevDir = "STM32F1xx" },
        @{ Family = "F4"; Cpu = "cortex-m4"; Device = "STM32F407xx"; Conf = "mcu/stubs/real_hal/f4"; HalDir = "STM32F4xx_HAL_Driver"; DevDir = "STM32F4xx" }
    )

    $anyReal = $false
    foreach ($t in $realTargets) {
        $pkg = Find-CubePackage $t.Family
        if (-not $pkg) {
            Write-Host ("-- 未找到 STM32Cube_FW_" + $t.Family + "，跳过真实 HAL 交叉验证（不算失败）") -ForegroundColor Yellow
            continue
        }
        if (-not $anyReal) {
            Write-Host "== 真实 HAL 交叉验证（核对桩签名是否与官方头文件一致）==" -ForegroundColor Cyan
            $anyReal = $true
        }
        $halInc = Join-Path $pkg ("Drivers\" + $t.HalDir + "\Inc")
        $devInc = Join-Path $pkg ("Drivers\CMSIS\Device\ST\" + $t.DevDir + "\Include")
        $coreInc = Join-Path $pkg "Drivers\CMSIS\Include"
        if (-not (Test-Path -LiteralPath $halInc)) {
            Write-Host ("-- " + (Split-Path -Leaf $pkg) + " 缺少 HAL 头目录，跳过") -ForegroundColor Yellow
            continue
        }
        foreach ($src in $exampleSources) {
            Invoke-OneBuild ("real " + $t.Family + " " + [IO.Path]::GetFileName($src)) $src @(
                "-mcpu=$($t.Cpu)", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
                "-D$($t.Device)", "-DUSE_HAL_DRIVER", "-DPID_HAL_STUB=0",
                "-I", $t.Conf, "-I", $halInc, "-I", $devInc, "-I", $coreInc, "-I", "mcu/Inc"
            )
        }
        if ($t.Family -eq "F4") {
            # 可选 TIM6 时基在 F4 上真有这个定时器，除了桩也要对真实 HAL 编一次
            Invoke-OneBuild "real F4 main_example (PID_EXAMPLE_USE_TIM6=1)" "mcu/Src/main_example.c" @(
                "-mcpu=$($t.Cpu)", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
                "-D$($t.Device)", "-DUSE_HAL_DRIVER", "-DPID_HAL_STUB=0", "-DPID_EXAMPLE_USE_TIM6=1",
                "-I", $t.Conf, "-I", $halInc, "-I", $devInc, "-I", $coreInc, "-I", "mcu/Inc"
            )
        }
        # 真实 IO 分支对真实 HAL 编一次：核对 ADC/PWM 的函数签名与通道宏是否真的存在
        Invoke-OneBuild ("real " + $t.Family + " main_example (REAL IO)") "mcu/Src/main_example.c" @(
            "-mcpu=$($t.Cpu)", "-mthumb", "-std=c11", "-O2", "-Wall", "-Wextra",
            "-D$($t.Device)", "-DUSE_HAL_DRIVER", "-DPID_HAL_STUB=0", "-DPID_DEMO_SIMULATED_PLANT=0",
            "-I", $t.Conf, "-I", $halInc, "-I", $devInc, "-I", $coreInc, "-I", "mcu/Inc"
        )
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ""
if ($script:Failed -gt 0 -or $script:Warned -gt 0) {
    Write-Host ("编译验证失败：共 " + $script:Total + " 项，" + $script:Failed + " 项失败，" + $script:Warned + " 项带告警") -ForegroundColor Red
    exit 1
}
Write-Host ("编译验证通过：共 " + $script:Total + " 项，全部零告警") -ForegroundColor Green
exit 0

}
finally {
    Pop-Location
}
