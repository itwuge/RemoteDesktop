# ==========================================================================================
# 文件        : build_windows.py
# 所属目录    : scripts/
# 文件用途    : 一条命令构建 Windows 便携包: PyInstaller 无控制台产物 + zip + sha256
# 架构定位    : 构建脚本, 不进入运行时; 不进仓库产物统一落 D 盘构建根
# 核心功能    : 读取版本单一来源; 生成无控制台 exe; 便携目录; 便携 zip; sha256; 构建信息
# 职责边界    : 不修改源码; 不签名; 不上传; 不安装系统组件
# ==========================================================================================

"""Windows 便携包构建.

用法:
    python scripts/build_windows.py
    python scripts/build_windows.py --build-root D:\\RDC-Build

前置条件: 当前 Python 需同时具备 PyInstaller 与 PySide6.
产物目录: <构建根>\\artifacts\\desktop\\.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
APP_DIR_NAME = "rdc-desktop"
APP_ICON = ROOT / "assets" / "app-icon" / "rdc.ico"
PORTABLE_FOLDER_NAME = "RDC 远程桌面"
PORTABLE_CONFIG = '{\n  "update_source": ""\n}\n'


def _version_info() -> tuple[str, str, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import __version__, artifact_basename, platform_tag

    return __version__, artifact_basename(), platform_tag()


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = version.split("-", 1)[0].split(".")
    numbers = [int(item) for item in parts[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2], 0


def _version_file(version: str) -> str:
    major, minor, patch, build = _version_tuple(version)
    return (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers=({major}, {minor}, {patch}, {build}),\n"
        f"    prodvers=({major}, {minor}, {patch}, {build}),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('080404b0', [\n"
        "        StringStruct('CompanyName', 'RDC'),\n"
        "        StringStruct('FileDescription', 'RDC 远程桌面'),\n"
        f"        StringStruct('FileVersion', '{version}'),\n"
        "        StringStruct('InternalName', 'rdc-desktop'),\n"
        "        StringStruct('OriginalFilename', 'rdc-desktop.exe'),\n"
        "        StringStruct('ProductName', 'RDC 远程桌面'),\n"
        f"        StringStruct('ProductVersion', '{version}')\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
        "  ]\n"
        ")\n"
    )


def _run_pyinstaller(
    version: str, dist_dir: Path, work_dir: Path, spec_dir: Path, version_file: Path, icon_path: Path | None
) -> int:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--noupx",
        "--name",
        APP_DIR_NAME,
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--version-file",
        str(version_file),
        "--add-data",
        f"{ROOT / 'src' / 'rdc' / 'ui' / 'assets'}{os.pathsep}rdc{os.sep}ui{os.sep}assets",
        "--add-data",
        f"{ROOT / 'deploy' / 'relay' / 'ca.crt'}{os.pathsep}rdc{os.sep}ui{os.sep}assets",
        "--add-data",
        f"{ROOT / 'deploy' / 'relay' / 'ca.crt'}{os.pathsep}rdc{os.sep}assets",
    ]
    if icon_path is not None:
        command.extend(["--icon", str(icon_path)])
    command.append(str(ROOT / "src" / "rdc" / "ui" / "__main__.py"))
    _emit("执行: " + " ".join(command))
    result = subprocess.run(command, cwd=ROOT, check=False)
    return result.returncode


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _write_portable_assets(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "portable.marker").write_text("portable\n", encoding="utf-8", newline="\n")
    data_dir = target / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(PORTABLE_CONFIG, encoding="utf-8", newline="\n")
    (data_dir / "update-source").mkdir(parents=True, exist_ok=True)
    (target / "使用说明.txt").write_text(_portable_readme(), encoding="utf-8", newline="\n")


def _portable_readme() -> str:
    return (
        "RDC 远程桌面 便携版\n"
        "==================\n\n"
        "解压后得到「RDC 远程桌面」文件夹, 安装版默认也装到同名目录\n"
        "(%LOCALAPPDATA%\\Programs\\RDC 远程桌面). 两者同名, 请解压到与安装版不同的父目录再并列使用.\n"
        "启动: 双击 rdc-desktop.exe, 不会出现黑色控制台窗口.\n"
        "配置: data\\config.json; update_source 可填本地 manifest.json 路径或本地 HTTP 地址.\n"
        "日志: data\\logs\\rdc.log.\n"
        "开机自启: 便携版默认关闭; 在设置页手动开启后会写入当前用户 Run 键, 删除文件夹前请先关闭.\n"
        "卸载: 直接删除整个文件夹即可, 默认不写注册表, 不依赖系统 Python.\n"
        "说明: 本版本为内部 V1 构建, 未签名; 若被安全软件拦截请核对 sha256 后放行.\n"
    )


def _zip_portable(source_dir: Path, zip_path: Path, top_name: str) -> int:
    """把便携目录压成 zip, 顶层目录统一为 PORTABLE_FOLDER_NAME, 与安装目录同名."""
    files = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, f"{top_name}/{path.relative_to(source_dir).as_posix()}")
            files += 1
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Windows 便携包与校验文件")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    parser.add_argument("--skip-build", action="store_true", help="复用现有 PyInstaller 产物, 只重打包")
    parser.add_argument("--icon", default=str(APP_ICON), help="可执行文件图标 (.ico), 传空字符串则不嵌入")
    args = parser.parse_args(argv)

    version, basename, platform = _version_info()
    if platform != "windows-x64":
        _emit(f"当前平台为 {platform}, 产物按本机平台命名; 目标环境为 windows-x64")
    build_root = Path(args.build_root)
    stage = build_root / "build" / "desktop"
    dist_dir = stage / "dist"
    work_dir = stage / "work"
    spec_dir = stage / "spec"
    artifacts = build_root / "artifacts" / "desktop"
    stage.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    exe_path = dist_dir / APP_DIR_NAME / f"{APP_DIR_NAME}.exe"
    if not args.skip_build:
        version_file = stage / "version_info.txt"
        version_file.write_text(_version_file(version), encoding="utf-8", newline="\n")
        icon_path = Path(args.icon) if args.icon else None
        if icon_path is not None and not icon_path.is_file():
            _emit(f"未找到图标文件: {icon_path}")
            return 1
        code = _run_pyinstaller(version, dist_dir, work_dir, spec_dir, version_file, icon_path)
        if code != 0:
            _emit(f"PyInstaller 构建失败, 退出码 {code}")
            return 1
    if not exe_path.is_file():
        _emit(f"未找到可执行文件: {exe_path}")
        return 1

    portable_dir = artifacts / basename
    if portable_dir.exists():
        shutil.rmtree(portable_dir)
    shutil.copytree(dist_dir / APP_DIR_NAME, portable_dir)
    _write_portable_assets(portable_dir)

    zip_path = artifacts / f"{basename}-portable.zip"
    files = _zip_portable(portable_dir, zip_path, PORTABLE_FOLDER_NAME)
    digest = _sha256(zip_path)
    checksum_path = zip_path.with_suffix(".zip.sha256")
    checksum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii", newline="\n")

    info = {
        "version": version,
        "artifact": basename,
        "platform": platform,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "exe": str(portable_dir / f"{APP_DIR_NAME}.exe"),
        "portable_dir": str(portable_dir),
        "portable_zip": str(zip_path),
        "sha256": digest,
        "zip_bytes": zip_path.stat().st_size,
        "zip_files": files,
        "console": "无控制台 (PyInstaller --windowed)",
    }
    (artifacts / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _emit("")
    _emit(f"版本: {version} ({platform})")
    _emit(f"便携目录: {portable_dir}")
    _emit(f"便携 zip: {zip_path}")
    _emit(f"校验文件: {checksum_path}")
    _emit(f"SHA-256: {digest}")
    _emit(f"zip 大小: {info['zip_bytes']} 字节, 文件数 {files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
