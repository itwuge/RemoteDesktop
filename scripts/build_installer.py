# ==========================================================================================
# 文件        : build_installer.py
# 所属目录    : scripts/
# 文件用途    : 调用 NSIS 生成 Windows 安装包与 sha256 校验文件
# 架构定位    : 构建脚本; 复用 build_windows.py 的 PyInstaller 产物
# 核心功能    : 参数化版本号与输出路径; 校验 makensis; 生成 setup.exe 与安装信息
# 职责边界    : 不修改源码; 不签名; 不执行安装; 不上传
# ==========================================================================================

"""Windows 安装包构建.

用法:
    python scripts/build_installer.py
    python scripts/build_installer.py --build-root D:\\RDC-Build

前置条件: 已安装 NSIS (makensis), 且已执行 scripts/build_windows.py 生成 dist 产物.
产物: <构建根>\\artifacts\\desktop\\<产物名>-setup.exe 与 .sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
NSI_SCRIPT = ROOT / "packaging" / "windows" / "rdc-installer.nsi"
APP_ICON = ROOT / "assets" / "app-icon" / "rdc.ico"
MAKENSIS_CANDIDATES = (
    Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
    Path(r"C:\Program Files\NSIS\makensis.exe"),
)


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _version_info() -> tuple[str, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import __version__, artifact_basename

    return __version__, artifact_basename()


def _find_makensis() -> Path | None:
    configured = os.environ.get("NSIS_MAKENSIS")
    if configured and Path(configured).is_file():
        return Path(configured)
    for candidate in MAKENSIS_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Windows 安装包")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    parser.add_argument("--source-dir", default="", help="PyInstaller dist 目录, 默认取构建根下 dist/rdc-desktop")
    parser.add_argument("--icon", default=str(APP_ICON), help="安装包与快捷方式图标 (.ico)")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        _emit("安装包只能在 Windows 上构建")
        return 1
    makensis = _find_makensis()
    if makensis is None:
        _emit("未找到 makensis, 请安装 NSIS 3.x 或设置 NSIS_MAKENSIS 指向 makensis.exe")
        return 1

    version, basename = _version_info()
    build_root = Path(args.build_root)
    source_dir = Path(args.source_dir) if args.source_dir else build_root / "build" / "desktop" / "dist" / "rdc-desktop"
    if not (source_dir / "rdc-desktop.exe").is_file():
        _emit(f"未找到无控制台产物: {source_dir}\\rdc-desktop.exe, 请先执行 scripts/build_windows.py")
        return 1

    artifacts = build_root / "artifacts" / "desktop"
    artifacts.mkdir(parents=True, exist_ok=True)
    out_file = artifacts / f"{basename}-setup.exe"
    command = [
        str(makensis),
        "/V2",
        f"/DVERSION={version}",
        f"/DSOURCE_DIR={source_dir}",
        f"/DOUT_FILE={out_file}",
    ]
    icon_path = Path(args.icon) if args.icon else None
    if icon_path is not None and not icon_path.is_file():
        _emit(f"未找到图标文件: {icon_path}")
        return 1
    if icon_path is not None:
        command.append(f"/DICON_FILE={icon_path}")
    command.append(str(NSI_SCRIPT))
    _emit("执行: " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        _emit(f"makensis 构建失败, 退出码 {result.returncode}")
        return 1
    if not out_file.is_file():
        _emit(f"未生成安装包: {out_file}")
        return 1

    digest = _sha256(out_file)
    checksum_path = out_file.with_suffix(".exe.sha256")
    checksum_path.write_text(f"{digest}  {out_file.name}\n", encoding="ascii", newline="\n")
    info = {
        "version": version,
        "artifact": basename,
        "installer": str(out_file),
        "sha256": digest,
        "bytes": out_file.stat().st_size,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "makensis": str(makensis),
        "source_dir": str(source_dir),
        "install_scope": "per-machine (Program Files 或用户选择目录)",
        "shortcuts": ["开始菜单", "桌面"],
        "uninstall": "控制面板卸载 或 安装目录 Uninstall.exe",
        "console": "无控制台 (PyInstaller --windowed)",
    }
    (artifacts / "installer-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _emit("")
    _emit(f"安装包: {out_file}")
    _emit(f"校验文件: {checksum_path}")
    _emit(f"SHA-256: {digest}")
    _emit(f"大小: {info['bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
