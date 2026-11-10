#!/usr/bin/env bash
# ==========================================================================================
# 文件        : build_macos.sh
# 所属目录    : scripts/
# 文件用途    : 在 macOS 上构建 dmg 安装包与 zip 便携包
# 架构定位    : 构建脚本, 只在 macOS 主机执行, 不进入运行时
# 核心功能    : PyInstaller 冻结 -> .app -> 可选签名 -> dmg / 便携 zip -> sha256
# 职责边界    : 不做公证; 没有证书时不伪造签名; 不安装; 不上传
# ==========================================================================================
#
# 用法:
#   bash scripts/build_macos.sh [构建根]
#   CODESIGN_IDENTITY="Developer ID Application: 名称 (TEAMID)" bash scripts/build_macos.sh
#
# 前置:
#   1. Xcode Command Line Tools: xcode-select --install
#   2. Python 3.12, 且已安装 PySide6==6.11.2 与 PyInstaller==6.10.0
#   3. 可选: Apple Developer ID 证书; 未提供时产物为未签名版本
#
# 产物:
#   <构建根>/artifacts/desktop/rdc-desktop-<版本>-macos-<arch>.dmg
#   <构建根>/artifacts/desktop/rdc-desktop-<版本>-macos-<arch>-portable.zip
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${1:-${RDC_BUILD_ROOT:-$HOME/rdc-build}}"
PYTHON="${PYTHON:-python3}"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "macOS 包只能在 macOS 上构建"
  exit 1
fi

read -r VERSION BASENAME <<<"$("$PYTHON" - "$ROOT" <<'PY'
import sys

sys.path.insert(0, sys.argv[1] + "/src")
from rdc.version import __version__, artifact_basename

print(__version__, artifact_basename())
PY
)"

STAGE="$BUILD_ROOT/build/desktop"
DIST="$STAGE/dist"
ARTIFACTS="$BUILD_ROOT/artifacts/desktop"
mkdir -p "$DIST" "$ARTIFACTS"

echo "== 冻结应用 ($VERSION / $BASENAME) =="
"$PYTHON" -m PyInstaller --noconfirm --clean --windowed --onedir --noupx \
  --name rdc-desktop \
  --icon "$ROOT/assets/app-icon/rdc.icns" \
  --paths "$ROOT/src" \
  --distpath "$DIST" \
  --workpath "$STAGE/work" \
  --specpath "$STAGE/spec" \
  "$ROOT/src/rdc/ui/__main__.py"

APP="$DIST/rdc-desktop.app"
if [ ! -d "$APP" ]; then
  echo "未找到 .app 产物: $APP"
  exit 1
fi

echo "== 签名 =="
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo "签名完成: $CODESIGN_IDENTITY"
else
  echo "未提供 CODESIGN_IDENTITY: 产物保持未签名, 首次打开需右键选择「打开」"
fi

echo "== 便携包 =="
PORTABLE_DIR="$ARTIFACTS/$BASENAME"
rm -rf "$PORTABLE_DIR"
mkdir -p "$PORTABLE_DIR"
cp -R "$APP" "$PORTABLE_DIR/rdc-desktop.app"
MACOS_DIR="$PORTABLE_DIR/rdc-desktop.app/Contents/MacOS"
printf 'portable\n' > "$MACOS_DIR/portable.marker"
mkdir -p "$MACOS_DIR/data/update-source"
printf '{\n  "update_source": ""\n}\n' > "$MACOS_DIR/data/config.json"
{
  echo "RDC 远程桌面 便携版 $VERSION (macOS)"
  echo "===================================="
  echo ""
  echo "启动: 双击 rdc-desktop.app"
  echo "配置: rdc-desktop.app/Contents/MacOS/data/config.json"
  echo "日志: rdc-desktop.app/Contents/MacOS/data/logs/rdc.log"
  echo "卸载: 删除整个文件夹即可, 不写系统目录."
  echo "说明: 未签名版本首次打开需右键选择「打开」."
} > "$PORTABLE_DIR/使用说明.txt"
ZIP="$ARTIFACTS/$BASENAME-portable.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$PORTABLE_DIR" "$ZIP"

echo "== 安装包 =="
DMG="$ARTIFACTS/$BASENAME.dmg"
DMG_STAGE="$STAGE/dmg"
rm -rf "$DMG_STAGE" "$DMG"
mkdir -p "$DMG_STAGE"
cp -R "$APP" "$DMG_STAGE/rdc-desktop.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create -volname "RDC 远程桌面 $VERSION" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG"

for item in "$DMG" "$ZIP"; do
  shasum -a 256 "$item" | awk '{print $1"  "$2}' > "$item.sha256"
  echo "$(basename "$item"): $(cat "$item.sha256")"
done

echo ""
echo "版本: $VERSION"
echo "dmg: $DMG"
echo "zip: $ZIP"
if [ -z "${CODESIGN_IDENTITY:-}" ]; then
  echo "签名状态: 未签名 (无 Apple Developer ID 证书)"
fi
