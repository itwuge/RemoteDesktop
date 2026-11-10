# Unified quality gate entry. Rules live in pyproject.toml and scripts/gate.py.
# Usage: powershell -File scripts/gate.ps1 [-SkipTests]
# Exit code: 0 all green, 1 any step failed.

param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$pylibs = "D:\RDC-Build\tools\pylibs"
if (Test-Path $pylibs) {
    $env:PYTHONPATH = "$pylibs;$root\src;$root\scripts;$root"
} else {
    $env:PYTHONPATH = "$root\src;$root\scripts;$root"
}

$arguments = @()
if ($SkipTests) {
    $arguments += "--skip-tests"
}

& python scripts\gate.py @arguments
exit $LASTEXITCODE
