# SerialPID-Tuner ESP32 build/flash wrapper (card45)
#
# Why this script exists:
#   The repo path contains non-ASCII characters (...\串口PID\SerialPID-Tuner).
#   ESP-IDF v6.0.1 on Windows has three hard failures with such paths:
#     1) build/toolchain/cflags response file mangles the picolibc.specs path
#        -> "cannot read spec file ... \x0aPID"
#     2) ninja cannot create object files under a non-ASCII output path
#     3) ccache uses std::filesystem path conversion -> "Illegal byte sequence"
#   junction / subst do not help: CMake resolves the real (non-ASCII) path.
#
#   So the build happens in an ASCII-only workspace (default C:\SerialPID-esp32).
#   Every run mirrors mcu/Src, mcu/Inc and mcu/esp32 there (byte-identical copies,
#   the repo stays the source of truth), runs idf.py, then copies sdkconfig and
#   bin/elf/map back into mcu/esp32/build.
#
# Usage (PowerShell 7):
#   .\mcu\esp32\build.ps1 build
#   .\mcu\esp32\build.ps1 flash -Port COM5
#   .\mcu\esp32\build.ps1 monitor -Port COM5
#   .\mcu\esp32\build.ps1 build -WorkDir D:\esp-work
#
# NOTE: this file is intentionally ASCII-only. PowerShell on this machine
#       decodes BOM-less UTF-8 scripts as ANSI, which mangles Chinese text here.
param(
    [ValidateSet('build', 'flash', 'monitor', 'clean', 'erase')]
    [string]$Action = 'build',

    [string]$Port = 'COM5',

    # ASCII-only workspace. Keep it out of OneDrive-synced folders.
    [string]$WorkDir = 'C:\SerialPID-esp32',

    # ccache works in the ASCII workspace; off by default to remove a variable.
    [switch]$UseCcache,

    [string]$IdfProfile = 'C:\Espressif\tools\Microsoft.v6.0.1.PowerShell_profile.ps1'
)

# Continue, not Stop: idf.py writes normal progress to stderr, and with Stop
# PowerShell turns every stderr line into a terminating NativeCommandError.
$ErrorActionPreference = 'Continue'

function Assert-AsciiPath {
    param([string]$Path, [string]$What)

    if ($Path -match '[^\x20-\x7E]') {
        throw "$What must be an ASCII-only path (got: $Path). ESP-IDF tooling cannot handle non-ASCII paths."
    }
}

function Sync-Tree {
    param([string]$From, [string]$To, [string[]]$Exclude = @())

    if (-not (Test-Path -LiteralPath $From)) {
        throw "source directory missing: $From"
    }
    # No /MIR: the workspace build/ dir is an incremental build directory,
    # mirroring would wipe it every run.
    $rcArgs = @($From, $To, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/R:1', '/W:1')
    if ($Exclude.Count -gt 0) {
        $rcArgs += '/XD'
        $rcArgs += $Exclude
    }
    # robocopy: 0..7 are success codes (1 = files copied), 8+ are failures
    $null = & robocopy @rcArgs
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed ($LASTEXITCODE): $From -> $To"
    }
    $global:LASTEXITCODE = 0
}

Assert-AsciiPath -Path $WorkDir -What 'WorkDir'

# $PSScriptRoot = <repo>\mcu\esp32 -> parent = mcu, grandparent = repo root
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$srcRoot  = Join-Path $repoRoot 'mcu'
$dstRoot  = Join-Path $WorkDir 'mcu'
$projSrc  = Join-Path $srcRoot 'esp32'
$projDst  = Join-Path $dstRoot 'esp32'

# ---- 1. mirror sources into the ASCII workspace ----
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

Sync-Tree -From (Join-Path $srcRoot 'Src') -To (Join-Path $dstRoot 'Src')
Sync-Tree -From (Join-Path $srcRoot 'Inc') -To (Join-Path $dstRoot 'Inc')
Sync-Tree -From $projSrc -To $projDst -Exclude @((Join-Path $projSrc 'build'), (Join-Path $projDst 'build'))

# Carry an existing sdkconfig over, but never overwrite the workspace one
# (that file holds the user's menuconfig edits).
$sdkRepo = Join-Path $projSrc 'sdkconfig'
$sdkWork = Join-Path $projDst 'sdkconfig'

if ((Test-Path -LiteralPath $sdkRepo) -and -not (Test-Path -LiteralPath $sdkWork)) {
    Copy-Item -LiteralPath $sdkRepo -Destination $sdkWork
}

Write-Host "[esp32] workspace: $projDst"

# ---- 2. load ESP-IDF then run idf.py ----
. $IdfProfile | Out-Null

$idfArgs = @('-C', $projDst)
if (-not $UseCcache) {
    $idfArgs += '--no-ccache'
}

switch ($Action) {
    'build'   { $idfArgs += 'build' }
    'flash'   { $idfArgs += @('-p', $Port, 'flash') }
    'monitor' { $idfArgs += @('-p', $Port, 'monitor') }
    'clean'   { $idfArgs += 'fullclean' }
    'erase'   { $idfArgs += @('-p', $Port, 'erase-flash') }
}

& idf.py @idfArgs
$code = $LASTEXITCODE

# ---- 3. copy config and artifacts back ----
if (($code -eq 0) -and (Test-Path -LiteralPath $sdkWork)) {
    Copy-Item -LiteralPath $sdkWork -Destination $sdkRepo -Force
}

if ($code -eq 0) {
    $binDir = Join-Path $projDst 'build'
    $outDir = Join-Path $projSrc 'build'

    if (Test-Path -LiteralPath $binDir) {
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null
        foreach ($name in @('serialpid_tuner_fw.bin', 'serialpid_tuner_fw.elf',
                            'serialpid_tuner_fw.map',
                            'bootloader\bootloader.bin',
                            'partition_table\partition-table.bin',
                            'flasher_args.json')) {
            $from = Join-Path $binDir $name
            if (Test-Path -LiteralPath $from) {
                Copy-Item -LiteralPath $from -Destination $outDir -Force
            }
        }
        Write-Host "[esp32] artifacts copied back to $outDir"
    }
}

exit $code
