# ==========================================================================================
# 文件        : build_linux.py
# 所属目录    : scripts/
# 文件用途    : 在 Linux 上一条命令构建 deb 安装包与 tar.gz 便携包
# 架构定位    : 构建脚本, 不进入运行时; 只在 Linux 主机上执行
# 核心功能    : PyInstaller 冻结 -> 便携目录与 tar.gz -> deb 目录树与 dpkg-deb -> sha256
# 职责边界    : 不修改源码; 不签名; 不安装; 不上传; 不写任何凭据
# ==========================================================================================

"""Linux 安装包与便携包构建.

用法:
    python3 scripts/build_linux.py --build-root /home/wuge/rdc-build
    python3 scripts/build_linux.py --build-root /home/wuge/rdc-build --skip-build

前置条件: 当前 Python 具备 PyInstaller 与 PySide6, 系统具备 dpkg-deb.
产物: <构建根>/artifacts/desktop/rdc-desktop-<版本>-linux-<arch>.deb
      <构建根>/artifacts/desktop/rdc-desktop-<版本>-linux-<arch>-portable.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

from _rdc_path import PROJECT_ROOT

APP_DIR_NAME = "rdc-desktop"
PACKAGE_NAME = "rdc-desktop"
INSTALL_DIR = Path("/opt") / APP_DIR_NAME
ICON_ROOT = PROJECT_ROOT / "packaging" / "linux" / "icons" / "hicolor"
DESKTOP_ENTRY = PROJECT_ROOT / "packaging" / "linux" / "rdc-desktop.desktop"
APP_ICON = ICON_ROOT / "256x256" / "apps" / "rdc-desktop.png"
PORTABLE_CONFIG = '{\n  "update_source": ""\n}\n'
DEB_ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
DEB_DEPENDS = (
    "libc6, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, "
    "libxcb-render-util0, libxcb-shape0, libxcb-xkb1, libxkbcommon0, libxkbcommon-x11-0, "
    "libegl1, libgl1, libfontconfig1, libglib2.0-0, libdbus-1-3"
)


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _version_info() -> tuple[str, str, str]:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from rdc.version import __version__, artifact_basename, platform_tag

    return __version__, artifact_basename(), platform_tag()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_pyinstaller(dist_dir: Path, work_dir: Path, spec_dir: Path, icon_path: Path | None) -> int:
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
        str(PROJECT_ROOT / "src"),
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
    ]
    if icon_path is not None:
        command.extend(["--icon", str(icon_path)])
    command.append(str(PROJECT_ROOT / "src" / "rdc" / "ui" / "__main__.py"))
    _emit("执行: " + " ".join(command))
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode


def _write_executable(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _portable_readme(version: str) -> str:
    return (
        f"RDC 远程桌面 便携版 {version} (Linux x86_64)\n"
        "============================================\n\n"
        "启动: ./rdc-desktop.sh, 或直接执行 ./rdc-desktop\n"
        "配置: data/config.json; update_source 可填清单地址或本地目录.\n"
        "日志: data/logs/rdc.log.\n"
        "卸载: 直接删除整个文件夹即可, 不写系统目录, 不依赖系统 Python.\n"
        "依赖: 系统需具备 Qt/X11 运行库 (Ubuntu 22.04 桌面版默认具备).\n"
        "说明: 本版本未签名; 若被安全软件拦截请核对 sha256 后放行.\n"
    )


def _assemble_portable(dist_app: Path, target: Path, version: str) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(dist_app, target)
    (target / "portable.marker").write_text("portable\n", encoding="utf-8", newline="\n")
    data_dir = target / "data"
    (data_dir / "update-source").mkdir(parents=True, exist_ok=True)
    (data_dir / "config.json").write_text(PORTABLE_CONFIG, encoding="utf-8", newline="\n")
    _write_executable(target / "rdc-desktop.sh", '#!/bin/sh\ncd "$(dirname "$0")" || exit 1\nexec ./rdc-desktop "$@"\n')
    (target / "使用说明.txt").write_text(_portable_readme(version), encoding="utf-8", newline="\n")


def _tar_portable(source_dir: Path, tar_path: Path, top_name: str) -> int:
    with tarfile.open(tar_path, "w:gz", compresslevel=6) as archive:
        archive.add(source_dir, arcname=top_name, recursive=True)
    return sum(1 for item in source_dir.rglob("*") if item.is_file())


def _deb_arch() -> str:
    machine = (platform.machine() or "").lower()
    return DEB_ARCHES.get(machine, machine or "amd64")


def _control_file(version: str, installed_kb: int, deb_arch: str) -> str:
    return (
        f"Package: {PACKAGE_NAME}\n"
        f"Version: {version}\n"
        "Section: net\n"
        "Priority: optional\n"
        f"Architecture: {deb_arch}\n"
        "Maintainer: RDC <rdc@example.invalid>\n"
        f"Installed-Size: {installed_kb}\n"
        f"Depends: {DEB_DEPENDS}\n"
        "Description: RDC 远程桌面\n"
        " 远程控制、屏幕共享与文件传输桌面端.\n"
    )


def _assemble_deb(dist_app: Path, deb_root: Path, version: str) -> None:
    if deb_root.exists():
        shutil.rmtree(deb_root)
    app_target = deb_root / INSTALL_DIR.relative_to("/")
    shutil.copytree(dist_app, app_target)
    (app_target / "rdc-desktop").chmod(0o755)

    _write_executable(
        deb_root / "usr" / "bin" / APP_DIR_NAME,
        f'#!/bin/sh\nexec {INSTALL_DIR / APP_DIR_NAME} "$@"\n',
    )
    desktop_target = deb_root / "usr" / "share" / "applications" / f"{APP_DIR_NAME}.desktop"
    desktop_target.parent.mkdir(parents=True, exist_ok=True)
    desktop_target.write_text(DESKTOP_ENTRY.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    for icon in sorted(ICON_ROOT.glob("*/apps/rdc-desktop.png")):
        size = icon.parent.parent.name
        target = deb_root / "usr" / "share" / "icons" / "hicolor" / size / "apps" / f"{APP_DIR_NAME}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon, target)

    installed_kb = sum(item.stat().st_size for item in app_target.rglob("*") if item.is_file()) // 1024
    control = deb_root / "DEBIAN"
    control.mkdir(parents=True, exist_ok=True)
    (control / "control").write_text(_control_file(version, installed_kb, _deb_arch()), encoding="utf-8", newline="\n")


def _build_deb(deb_root: Path, deb_path: Path) -> int:
    return subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(deb_root), str(deb_path)],
        check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Linux deb 与便携 tar.gz")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(Path.home() / "rdc-build")))
    parser.add_argument("--skip-build", action="store_true", help="复用现有 PyInstaller 产物, 只重打包")
    parser.add_argument("--icon", default=str(APP_ICON), help="可执行文件图标 (png), 传空字符串则不嵌入")
    parser.add_argument("--only", choices=("all", "portable", "deb"), default="all")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        _emit("Linux 包只能在 Linux 上构建")
        return 1
    version, basename, tag = _version_info()
    build_root = Path(args.build_root)
    stage = build_root / "build" / "desktop"
    dist_dir = stage / "dist"
    artifacts = build_root / "artifacts" / "desktop"
    dist_app = dist_dir / APP_DIR_NAME
    stage.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)

    if not args.skip_build:
        icon_path = Path(args.icon) if args.icon else None
        if icon_path is not None and not icon_path.is_file():
            _emit(f"未找到图标文件: {icon_path}")
            return 1
        code = _run_pyinstaller(dist_dir, stage / "work", stage / "spec", icon_path)
        if code != 0:
            _emit(f"PyInstaller 构建失败, 退出码 {code}")
            return 1
    if not (dist_app / APP_DIR_NAME).is_file():
        _emit(f"未找到冻结产物: {dist_app / APP_DIR_NAME}")
        return 1

    summary: dict[str, object] = {
        "version": version,
        "artifact": basename,
        "platform": tag,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "console": "无控制台 (PyInstaller --windowed)",
        "artifacts": {},
    }
    produced = summary["artifacts"]
    assert isinstance(produced, dict)

    if args.only in ("all", "portable"):
        portable_dir = artifacts / basename
        _assemble_portable(dist_app, portable_dir, version)
        tar_path = artifacts / f"{basename}-portable.tar.gz"
        files = _tar_portable(portable_dir, tar_path, basename)
        digest = _sha256(tar_path)
        (tar_path.parent / f"{tar_path.name}.sha256").write_text(
            f"{digest}  {tar_path.name}\n", encoding="ascii", newline="\n"
        )
        produced["portable"] = {
            "path": str(tar_path),
            "sha256": digest,
            "bytes": tar_path.stat().st_size,
            "files": files,
        }
        _emit(f"便携包: {tar_path}  {digest}")

    if args.only in ("all", "deb"):
        deb_path = artifacts / f"{basename}.deb"
        deb_root = stage / "deb" / f"{PACKAGE_NAME}_{version}_{_deb_arch()}"
        _assemble_deb(dist_app, deb_root, version)
        code = _build_deb(deb_root, deb_path)
        if code != 0:
            _emit(f"dpkg-deb 构建失败, 退出码 {code}")
            return 1
        digest = _sha256(deb_path)
        deb_path.with_suffix(".deb.sha256").write_text(f"{digest}  {deb_path.name}\n", encoding="ascii", newline="\n")
        produced["deb"] = {"path": str(deb_path), "sha256": digest, "bytes": deb_path.stat().st_size}
        _emit(f"安装包: {deb_path}  {digest}")

    (artifacts / "linux-build-info.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    _emit("")
    _emit(f"版本: {version} ({tag})")
    _emit(f"产物目录: {artifacts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
