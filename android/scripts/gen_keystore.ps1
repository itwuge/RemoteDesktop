# ==========================================================================================
# 文件        : gen_keystore.ps1
# 所属目录    : android/scripts/
# 文件用途    : 生成侧载 APK 用的发布签名库, 并写出仓库外的 D:\RDC-Build\secrets\keystore.properties
# 架构定位    : 构建与发布工具, 不属于产品运行时代码
# 核心功能    : PKCS12 + RSA 3072 + 有效期 25 年; 口令经环境变量传给 keytool, 不写进脚本与仓库
# 职责边界    : 只生成密钥与签名配置; 不构建 APK, 不安装, 不上传
# 运行环境    : Windows PowerShell 5.1+ 且 keytool 在 PATH 中 (JDK 17)
# 依赖关系    : 被 android/app/build.gradle.kts 读取的仓库外 keystore.properties
# ==========================================================================================

[CmdletBinding()]
param(
    [string]$Alias = 'rdc-release',
    [string]$CommonName = 'RDC Remote Desktop',
    [string]$KeystorePath = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

$keytool = Get-Command keytool -ErrorAction SilentlyContinue
if (-not $keytool) {
    Fail '未找到 keytool。请安装 JDK 17 并把 <JDK>\bin 加入 PATH 后重试；本次未生成任何文件。'
}

if (-not $env:RDC_BUILD_ROOT) {
    Fail '未设置 RDC_BUILD_ROOT。请先执行 $env:RDC_BUILD_ROOT = ''D:\RDC-Build'' 后重试。'
}

if ([string]::IsNullOrWhiteSpace($KeystorePath)) {
    $KeystorePath = Join-Path $env:RDC_BUILD_ROOT 'temp\android\rdc-release.p12'
}

$keystoreDir = Split-Path -Parent $KeystorePath
if (-not (Test-Path $keystoreDir)) {
    New-Item -ItemType Directory -Path $keystoreDir -Force | Out-Null
}

if (Test-Path $KeystorePath) {
    Fail "签名库已存在: $KeystorePath。覆盖会使已安装版本无法升级，请先备份并确认后再手动删除。"
}

# 口令来源: 环境变量优先, 否则交互输入; 两者都不落盘到脚本或命令行
$plainPassword = $env:RDC_KEYSTORE_PASSWORD
if ([string]::IsNullOrWhiteSpace($plainPassword)) {
    $secure = Read-Host -AsSecureString '请输入签名库口令 (至少 6 位)'
    $plainPassword = [System.Net.NetworkCredential]::new('', $secure).Password
}
if ([string]::IsNullOrWhiteSpace($plainPassword) -or $plainPassword.Length -lt 6) {
    Fail '口令为空或少于 6 位, 未生成签名库。'
}

$env:RDC_KS_PASS = $plainPassword
$previousErrorAction = $ErrorActionPreference
try {
    # keytool 把进度写到 stderr; Stop 模式会把它当异常, 这里临时放行并按退出码判定
    $ErrorActionPreference = 'Continue'
    & keytool -genkeypair `
        -keystore $KeystorePath `
        -storetype PKCS12 `
        -alias $Alias `
        -keyalg RSA `
        -keysize 3072 `
        -validity 9125 `
        -dname "CN=$CommonName, OU=RDC, O=RDC, L=Local, ST=Local, C=CN" `
        -storepass:env RDC_KS_PASS `
        -keypass:env RDC_KS_PASS `
        -noprompt 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Fail "keytool 失败, 退出码 $LASTEXITCODE; 未写出 keystore.properties。"
    }
} finally {
    $ErrorActionPreference = $previousErrorAction
    Remove-Item Env:\RDC_KS_PASS -ErrorAction SilentlyContinue
}

$propertiesPath = Join-Path $env:RDC_BUILD_ROOT 'secrets\keystore.properties'
$propertiesDir = Split-Path -Parent $propertiesPath
if (-not (Test-Path $propertiesDir)) {
    New-Item -ItemType Directory -Path $propertiesDir -Force | Out-Null
}
$escapedPath = $KeystorePath.Replace('\', '/')
$lines = @(
    '# 由 android/scripts/gen_keystore.ps1 生成; 含口令, 不入库、不进日志、不进构建摘要',
    "storeFile=$escapedPath",
    "storePassword=$plainPassword",
    "keyAlias=$Alias",
    "keyPassword=$plainPassword"
)
Set-Content -Path $propertiesPath -Value $lines -Encoding UTF8

Write-Host "签名库已生成: $KeystorePath"
Write-Host "签名配置已写出: $propertiesPath (仓库外, 仅当前用户可读)"
Write-Host '请勿把签名配置或 .p12 提交到版本库; 丢失签名库将导致无法升级安装。'
