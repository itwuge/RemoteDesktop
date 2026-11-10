# ==========================================================================================
# 文件        : smoke.ps1
# 所属目录    : scripts/
# 文件用途    : 一条命令执行 V1 冒烟, 转发退出码
# 架构定位    : 验收入口
# 核心功能    : 设置构建根与 Python 路径, 调用 scripts/smoke.py
# 职责边界    : 不改网络与服务; 不重启机器; 不掩盖失败
# ==========================================================================================

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:RDC_BUILD_ROOT = "D:\RDC-Build"
$env:RDC_LOG_DIR = "D:\RDC-Build\logs\rdc"
$env:QT_QPA_PLATFORM = "offscreen"

$pylibs = "D:\RDC-Build\tools\pylibs"
if (Test-Path $pylibs) {
    $env:PYTHONPATH = "$pylibs;$root\src;$root\scripts"
} else {
    $env:PYTHONPATH = "$root\src;$root\scripts"
}

$python = "python"
& $python scripts\smoke.py
exit $LASTEXITCODE
