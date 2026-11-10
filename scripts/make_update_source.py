# ==========================================================================================
# 文件        : make_update_source.py
# 所属目录    : scripts/
# 文件用途    : 生成本地离线更新源清单(单产物, v2 结构)
# 架构定位    : 本地开发与联调工具; 不进入运行时
# 核心功能    : 从真实产物读取 sha256; 写版本、资产地址、日期、说明、强制标记与最低版本
# 职责边界    : 不上传; 不签名; 不修改产物; 不访问外部服务
# ==========================================================================================

"""本地离线更新源生成.

用法:
    python scripts/make_update_source.py
    python scripts/make_update_source.py --mandatory --min-version 0.1.0

产物: <构建根>\\update-source\\update-manifest.json, 资产地址指向同目录下的便携包.
多平台完整发布请使用 scripts/publish_release.py; 本脚本只用于单产物离线演练.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
DEFAULT_NOTES = (
    "1. 新增客户端检查更新、下载与 SHA-256 校验链路.\n"
    "2. 设置页新增“检查更新”入口, 更新面板展示版本、说明、进度与校验结果.\n"
    "3. 校验失败拒绝安装, 不接受降级安装."
)


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _version_info() -> tuple[str, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import __version__, artifact_basename

    return __version__, artifact_basename()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_kind(name: str) -> str:
    return "portable" if "portable" in name.lower() else "installer"


def main(argv: list[str] | None = None) -> int:
    version, basename = _version_info()
    parser = argparse.ArgumentParser(description="生成本地离线更新源清单")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    parser.add_argument("--artifact", default="", help="更新包路径, 默认取便携 zip")
    parser.add_argument("--out", default="", help="清单输出目录")
    parser.add_argument("--version", default=version)
    parser.add_argument("--platform", default="", help="平台标识, 默认按产物名推断")
    parser.add_argument("--min-version", default="0.1.0")
    parser.add_argument("--release-date", default=date.today().isoformat())
    parser.add_argument("--notes", default=DEFAULT_NOTES)
    parser.add_argument("--mandatory", action="store_true")
    args = parser.parse_args(argv)

    build_root = Path(args.build_root)
    default_artifact = build_root / "artifacts" / "desktop" / f"{basename}-portable.zip"
    artifact = Path(args.artifact) if args.artifact else default_artifact
    if not artifact.is_file():
        _emit(f"未找到更新包: {artifact}, 请先执行 scripts/build_windows.py")
        return 1
    out_dir = Path(args.out) if args.out else build_root / "update-source"
    out_dir.mkdir(parents=True, exist_ok=True)

    platform = args.platform or _platform_from_name(artifact.name, basename)
    download_url = os.path.relpath(artifact, out_dir).replace("\\", "/")
    manifest = {
        "schema": 2,
        "version": args.version,
        "release_date": args.release_date,
        "notes": [line for line in args.notes.splitlines() if line.strip()],
        "mandatory": bool(args.mandatory),
        "min_supported_version": args.min_version,
        "assets": [
            {
                "platform": platform,
                "kind": _asset_kind(artifact.name),
                "name": artifact.name,
                "url": download_url,
                "sha256": _sha256(artifact),
                "size": artifact.stat().st_size,
            }
        ],
    }
    path = out_dir / "update-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    _emit(f"更新清单: {path}")
    _emit(f"版本: {manifest['version']} 最低可运行: {manifest['min_supported_version']} 强制: {manifest['mandatory']}")
    _emit(f"平台: {platform} 形态: {_asset_kind(artifact.name)} 下载地址: {download_url}")
    _emit(f"SHA-256: {manifest['assets'][0]['sha256']}")
    _emit(f"本地目录源: local:{out_dir}")
    _emit(f"HTTP 模拟: python -m http.server 8800 --directory {build_root}")
    _emit("HTTP 源: mirror:http://127.0.0.1:8800/update-source")
    return 0


def _platform_from_name(name: str, basename: str) -> str:
    """从产物名推断平台标识, 例如 rdc-desktop-0.2.0-windows-x64-portable.zip."""
    suffix = name[len(basename) :] if name.startswith(basename) else ""
    platform = suffix.lstrip("-").split("-setup")[0].split("-portable")[0]
    if platform and platform.startswith(("windows", "macos", "linux")):
        return platform
    from rdc.version import platform_tag

    return platform_tag()


if __name__ == "__main__":
    raise SystemExit(main())
