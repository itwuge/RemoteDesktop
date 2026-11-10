# ==========================================================================================
# 文件        : start_desktop.ps1
# 所属目录    : scripts/
# 文件用途    : 桌面端本地安装与启动入口; 幂等准备虚拟环境后拉起主窗口
# 架构定位    : 安装与启动脚本, 不进入业务代码
# 核心功能    : 建 venv、按 requirements-desktop.txt 装依赖、设置 PYTHONPATH、写启动日志
# 职责边界    : 不改 src/ 业务逻辑; 不改契约; 不重启机器; 不碰 ECS
# ==========================================================================================

param(
    [switch]$NoStart,
    [switch]$Reinstall
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$buildRoot = if ($env:RDC_BUILD_ROOT) { $env:RDC_BUILD_ROOT } else { "D:\RDC-Build" }
$venv = Join-Path $buildRoot "venv"
$python = Join-Path $venv "Scripts\python.exe"
$requirements = Join-Path $root "requirements-desktop.txt"
$logDir = Join-Path $root "logs"
$null = New-Item -ItemType Directory -Force -Path $logDir

function Write-Step([string]$text) {
    Write-Host "[RDC] $text"
}

# ---------------------------------------------------------------- 1. 虚拟环境
if ($Reinstall -and (Test-Path $venv)) {
    Write-Step "按参数移除旧虚拟环境: $venv"
    Remove-Item -Recurse -Force $venv
}
if (-not (Test-Path $python)) {
    Write-Step "创建虚拟环境: $venv"
    $basePython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $basePython) {
        throw "未找到 Python 3.12, 请先安装并加入 PATH"
    }
    & $basePython -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "虚拟环境创建失败"
    }
}

# ---------------------------------------------------------------- 2. 运行依赖
$probe = "import PySide6, cryptography; print(PySide6.__version__)"
& $python -c $probe 2>$null | Out-Null
if ($LASTEXITCODE -ne 0 -or $Reinstall) {
    Write-Step "安装运行依赖: $requirements"
    & $python -m pip install --disables-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "运行依赖安装失败"
    }
}
$versions = & $python -c "import PySide6, cryptography; print('PySide6=' + PySide6.__version__, 'cryptography=' + cryptography.__version__)"
Write-Step "依赖就绪: $versions"

# ---------------------------------------------------------------- 3. 启动
$env:RDC_BUILD_ROOT = $buildRoot
$env:PYTHONPATH = Join-Path $root "src"

if ($NoStart) {
    Write-Step "已完成安装检查, 未启动界面 (-NoStart)"
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$launchInfo = Join-Path $logDir "desktop-$stamp.launch.txt"
$launcher = Join-Path $root "启动桌面端.vbs"
if (-not (Test-Path $launcher)) {
    throw "未找到无控制台启动入口: $launcher"
}

Start-Process -FilePath "wscript.exe" -ArgumentList "`"$launcher`"" -WorkingDirectory $root

@(
    "started_at=$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
    "launcher=$launcher"
    "pythonpath=$($env:PYTHONPATH)"
    "log_dir=$logDir"
) | Set-Content -Path $launchInfo -Encoding UTF8

Write-Step "界面已通过无控制台入口启动"
Write-Step "启动记录: $launchInfo"
Write-Step "运行日志: $(Join-Path $logDir 'rdc.log') 与 crash.log"
