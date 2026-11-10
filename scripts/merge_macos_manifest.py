# ==========================================================================================
# 文件        : merge_macos_manifest.py
# 所属目录    : scripts/
# 文件用途    : 把 macOS 两架构产物合并进 Release 的 update-manifest.json
# 架构定位    : 发布侧工具; CI 与本地都可执行; 不接触凭据
# 核心功能    : 归集 macos-arm64 / macos-x64 的 dmg 与 zip; 合并 assets; 生成 darwin 分组
# 职责边界    : 不下载资产本体; 不上传; 不清除其它平台条目
# ==========================================================================================

"""合并 macOS 产物进入更新清单.

用法:
    python scripts/merge_macos_manifest.py --tag v0.2.2 --assets ./artifacts/desktop
    python scripts/merge_macos_manifest.py --tag v0.2.2 --assets ./out \
        --manifest https://github.com/itwuge/RemoteDesktop/releases/download/v0.2.2/update-manifest.json

清单缺失时输出 <out> 指定的 fragment 文件, 只包含 macOS 条目, 供后续合并.
"""

from __future__ import annotations

import argparse
import http.client
import json
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

from publish_release import _emit, _sha256, classify, darwin_section

DEFAULT_REPO = "itwuge/RemoteDesktop"
DEFAULT_OUT = "update-manifest.json"
FRAGMENT_OUT = "update-manifest-macos.json"


def _version_from_src() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from rdc.version import __version__

    return __version__


def _http_get(url: str, timeout: float = 30.0) -> bytes | None:
    """按 http/https 取回内容, 手动跟随 3xx; 失败返回 None."""
    current = url
    for _ in range(5):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname, parts.port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
        try:
            target = parts.path or "/"
            if parts.query:
                target = f"{target}?{parts.query}"
            connection.request(
                "GET", target, headers={"User-Agent": "rdc-release-merge", "Accept": "application/octet-stream"}
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    return None
                current = location
                continue
            if response.status != 200:
                return None
            return response.read()
        except (OSError, http.client.HTTPException):
            return None
        finally:
            connection.close()
    return None


def _load_manifest(location: str) -> dict[str, object] | None:
    if not location:
        return None
    if location.startswith(("http://", "https://")):
        payload = _http_get(location)
        if payload is None:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    path = Path(location)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _macos_assets(directory: Path, version: str, url_base: str) -> list[dict[str, object]]:
    assets: list[dict[str, object]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        classified = classify(path, version)
        if classified is None or not classified[0].startswith("macos-"):
            continue
        platform, kind = classified
        assets.append(
            {
                "platform": platform,
                "kind": kind,
                "name": path.name,
                "url": f"{url_base.rstrip('/')}/{path.name}",
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return assets


def _platform_map(assets: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    kinds: dict[str, set[str]] = {}
    for asset in assets:
        kinds.setdefault(str(asset["platform"]), set()).add(str(asset["kind"]))
    return {platform: {"status": "available", "kinds": sorted(values)} for platform, values in sorted(kinds.items())}


def _merge_assets(existing: list[dict[str, object]], additions: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for asset in existing + additions:
        merged[(str(asset.get("platform", "")), str(asset.get("kind", "")))] = asset
    return [merged[key] for key in sorted(merged)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="合并 macOS 产物到更新清单")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--assets", required=True, help="包含 macOS 产物的目录")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--manifest", default="", help="现有清单地址或路径, 为空则只出 fragment")
    parser.add_argument("--out", default="", help="完整清单输出路径, 默认 update-manifest.json")
    parser.add_argument("--fragment-out", default=FRAGMENT_OUT)
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)

    version = args.version or _version_from_src()
    asset_dir = Path(args.assets)
    if not asset_dir.is_dir():
        _emit(f"产物目录不存在: {asset_dir}")
        return 1
    url_base = f"https://github.com/{args.repo}/releases/download/{args.tag}"
    macos_assets = _macos_assets(asset_dir, version, url_base)
    platforms = sorted({str(item["platform"]) for item in macos_assets})
    if not macos_assets:
        _emit(f"未在 {asset_dir} 找到 {version} 的 macOS 产物")
        return 1
    _emit(f"macOS 产物 {len(macos_assets)} 个, 平台: {', '.join(platforms)}")

    existing = _load_manifest(args.manifest)
    if existing is None:
        fragment_platforms = _platform_map(macos_assets)
        fragment = {
            "schema": 2,
            "version": version,
            "tag": args.tag,
            "fragment": True,
            "assets": macos_assets,
            "platforms": fragment_platforms,
            "darwin": darwin_section(macos_assets),
        }
        out_path = Path(args.fragment_out)
        out_path.write_text(json.dumps(fragment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        _emit(f"未取到现有清单, 已输出 macOS fragment: {out_path.resolve()}")
        return 0

    raw_assets = existing.get("assets")
    current = [item for item in raw_assets if isinstance(item, dict)] if isinstance(raw_assets, list) else []
    merged = _merge_assets(current, macos_assets)
    existing["assets"] = merged
    raw_platforms = existing.get("platforms")
    platform_map = dict(raw_platforms) if isinstance(raw_platforms, dict) else {}
    for platform in platforms:
        kinds = sorted(str(item["kind"]) for item in merged if item.get("platform") == platform)
        platform_map[platform] = {"status": "available", "kinds": kinds}
    existing["platforms"] = platform_map
    existing["darwin"] = darwin_section(merged)
    out_path = Path(args.out or DEFAULT_OUT)
    out_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    _emit(f"合并完成: {out_path.resolve()} 共 {len(merged)} 个资产")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
