# V1 performance benchmark entry. Measurement logic lives in scripts/bench.py.
# Usage: powershell -File scripts/bench.ps1 [-Duration 8] [-Fps 30] [-Bitrate 4000000] [-SkipInput]
# Exit code: 0 measurement completed, 1 measurement failed.

param(
    [double]$Duration = 8.0,
    [int]$Fps = 30,
    [int]$Bitrate = 4000000,
    [switch]$SkipInput
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:RDC_BUILD_ROOT = "D:\RDC-Build"
$env:PYTHONPATH = "$root\src;$root\scripts;$root"

$arguments = @("--duration", "$Duration", "--fps", "$Fps", "--bitrate", "$Bitrate")
if ($SkipInput) {
    $arguments += "--skip-input"
}

& python scripts\bench.py @arguments
exit $LASTEXITCODE
