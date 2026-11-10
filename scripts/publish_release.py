# ==========================================================================================
# 文件        : publish_release.py
# 所属目录    : scripts/
# 文件用途    : 一条命令完成发布演练: 打 tag -> 归集产物 -> 生成 Release 清单 -> 回读校验
# 架构定位    : 发布侧工具; 只操作本地构建根, 不接触任何远端
# 核心功能    : 生成 GitHub Releases 目录结构与清单资产; 输出 gh/git 命令但绝不执行
# 职责边界    : 不创建仓库; 不推送; 不上传; 不写入任何令牌
# ==========================================================================================

"""发布演练.

用法:
    python scripts/publish_release.py
    python scripts/publish_release.py --version 0.2.0 --artifacts D:\\RDC-Build\\artifacts\\desktop

本地演练产物(<out> 默认 <构建根>\\release-sim):
    <out>\\<tag>\\                                     待上传资产集合
    <out>\\github\\<owner>\\<repo>\\releases\\download\\<tag>\\   模拟 Release 资产地址
    <out>\\github\\<owner>\\<repo>\\releases\\latest\\download\\  模拟 latest 直链
    <out>\\release-report.json                          回读校验报告

真实发布只输出命令, 不执行: 建仓库、打 tag、上传资产均由用户确认后手动完成.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
DEFAULT_NOTES = (
    "1. 客户端自更新改为读取 GitHub Releases 清单资产.\n"
    "2. 支持 GitHub 公开仓库、自建镜像与本地目录三种更新源.\n"
    "3. 更新包必须通过 SHA-256 校验, 校验失败拒绝安装, 不接受降级安装."
)

# 顺序重要: 先匹配更长的后缀
SUFFIX_KINDS = (
    ("-portable.app.zip", "portable"),
    ("-portable.tar.gz", "portable"),
    ("-portable.zip", "portable"),
    ("-setup.exe", "installer"),
    ("-installer.exe", "installer"),
    (".dmg", "installer"),
    (".deb", "installer"),
    (".AppImage", "portable"),
)
PLATFORM_PATTERN = re.compile(r"^(?P<platform>[a-z]+-[a-z0-9_]+)(?P<tail>.+)$")
GATEKEEPER_HINT = "未签名未公证: 首次打开需右键选择「打开」, 或执行 xattr -d com.apple.quarantine rdc-desktop.app"


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_info() -> tuple[str, str]:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import __version__, platform_tag

    return __version__, platform_tag()


def _default_repo() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.update.source import DEFAULT_GITHUB_REPO

    return os.environ.get("RDC_UPDATE_REPO", "").strip() or DEFAULT_GITHUB_REPO


def classify(path: Path, version: str) -> tuple[str, str] | None:
    """按产物命名规范识别 (平台, 形态); 不符合规范返回 None."""
    prefix = f"rdc-desktop-{version}-"
    if not path.name.startswith(prefix):
        return None
    rest = path.name[len(prefix) :]
    matched = PLATFORM_PATTERN.match(rest)
    if matched is None:
        return None
    platform = matched.group("platform")
    tail = matched.group("tail")
    for suffix, kind in SUFFIX_KINDS:
        if tail.endswith(suffix):
            return platform, kind
    return None


def collect_artifacts(directories: list[Path], explicit: list[Path], version: str) -> list[tuple[Path, str, str]]:
    """收集产物并按 (平台, 形态) 去重; 同键重复直接失败."""
    candidates: list[Path] = list(explicit)
    for directory in directories:
        if directory.is_dir():
            candidates.extend(sorted(item for item in directory.iterdir() if item.is_file()))
    selected: dict[tuple[str, str], Path] = {}
    for path in candidates:
        if not path.is_file():
            continue
        classified = classify(path, version)
        if classified is None:
            continue
        key = classified
        if key in selected:
            raise ValueError(f"同一平台与形态出现多个产物: {selected[key].name} / {path.name}")
        selected[key] = path
    return [(path, platform, kind) for (platform, kind), path in sorted(selected.items())]


def build_manifest(
    *,
    version: str,
    tag: str,
    artifacts: list[tuple[Path, str, str]],
    asset_url_base: str,
    release_date: str,
    notes: list[str],
    mandatory: bool,
    min_version: str,
    platforms: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """构造 v2 清单; asset_url_base 为空时使用资产相对名, 便于本地目录源."""
    assets = []
    for path, platform, kind in artifacts:
        url = f"{asset_url_base.rstrip('/')}/{path.name}" if asset_url_base else path.name
        assets.append(
            {
                "platform": platform,
                "kind": kind,
                "name": path.name,
                "url": url,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    manifest: dict[str, object] = {
        "schema": 2,
        "version": version,
        "release_date": release_date,
        "notes": notes,
        "mandatory": mandatory,
        "min_supported_version": min_version,
        "tag": tag,
        "assets": assets,
    }
    if platforms:
        manifest["platforms"] = platforms
    if any(str(asset["platform"]).startswith("macos-") for asset in assets):
        manifest["darwin"] = darwin_section(assets)
    return manifest


def materialize(source: Path, target: Path) -> None:
    """优先硬链接, 跨卷时退回复制, 避免演练重复占用磁盘."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")


def _default_signing_key() -> str:
    """签名私钥路径: 只从环境变量读取, 私钥不写进仓库与产物."""
    return os.environ.get("RDC_UPDATE_SIGNING_KEY", "").strip()


def sign_manifest(manifest: dict[str, object], key_path: str) -> dict[str, object]:
    """用发布私钥给清单签名; key_id 必须与客户端内置公钥一致, 否则拒绝出包."""
    sys.path.insert(0, str(ROOT / "src"))
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from rdc.crypto.identity import b64encode
    from rdc.update.signing import KEY_ID, PUBLIC_KEY_B64, key_id_for, sign_payload

    if not key_path:
        raise ValueError("未配置签名私钥: 请把 RDC_UPDATE_SIGNING_KEY 设为私钥 PEM 路径")
    path = Path(key_path).expanduser()
    if not path.is_file():
        raise ValueError(f"签名私钥不存在: {path}")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("签名私钥必须是 Ed25519 PEM (PKCS8)")
    raw_public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = key_id_for(raw_public)
    if key_id != KEY_ID or b64encode(raw_public) != PUBLIC_KEY_B64:
        raise ValueError(f"签名私钥与客户端内置公钥不匹配: 私钥 key_id {key_id}, 内置 {KEY_ID}")
    return sign_payload(manifest, key, key_id)


def verify_release(manifest_path: Path) -> dict[str, object]:
    """回读清单, 先验签再逐个核对同目录资产的 sha256 与大小."""
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.update.manifest import parse_manifest
    from rdc.update.signing import ManifestSignatureError, verify_manifest_signature

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        verify_manifest_signature(raw)
    except ManifestSignatureError as error:
        return {
            "manifest": str(manifest_path),
            "signature_ok": False,
            "error": str(error),
            "assets": [],
            "passed": False,
        }
    checked = []
    for asset in raw.get("assets", []):
        platform = str(asset.get("platform", ""))
        kind = str(asset.get("kind", ""))
        parsed = parse_manifest(raw, source=str(manifest_path), platform=platform, preferred_kind=kind)
        path = manifest_path.parent / parsed.asset_name
        digest = _sha256(path) if path.is_file() else ""
        size = path.stat().st_size if path.is_file() else -1
        checked.append(
            {
                "platform": platform,
                "kind": kind,
                "name": parsed.asset_name,
                "exists": path.is_file(),
                "sha256_match": digest == parsed.sha256,
                "size_match": size == int(asset.get("size", -1)),
            }
        )
    return {
        "manifest": str(manifest_path),
        "signature_ok": True,
        "assets": checked,
        "passed": all(item["sha256_match"] for item in checked),
    }


def main(argv: list[str] | None = None) -> int:
    version, platform = _version_info()
    parser = argparse.ArgumentParser(description="发布演练: tag -> 产物 -> 清单 -> 回读校验")
    parser.add_argument("--version", default=version)
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    parser.add_argument("--artifacts", default="", help="产物目录, 默认 <构建根>/artifacts/desktop")
    parser.add_argument("--artifact", action="append", default=[], help="显式指定产物, 可重复")
    parser.add_argument("--out", default="", help="演练输出根, 默认 <构建根>/release-sim")
    parser.add_argument("--tag", default="", help="发布 tag, 默认 v<version>")
    parser.add_argument("--repo", default="", help="GitHub 仓库 owner/repo, 默认取 RDC_UPDATE_REPO 或项目仓库")
    parser.add_argument("--manifest-name", default="", help="清单资产名, 默认 update-manifest.json")
    parser.add_argument("--asset-url-base", default="", help="资产 URL 前缀; 默认 GitHub Release 直链")
    parser.add_argument("--relative-asset-urls", action="store_true", help="清单使用资产相对名, 供离线目录源")
    parser.add_argument("--release-date", default=date.today().isoformat())
    parser.add_argument("--min-version", default="0.1.0")
    parser.add_argument("--notes-file", default="", help="更新说明文件, 每行一条")
    parser.add_argument("--signing-key", default="", help="Ed25519 私钥 PEM 路径, 默认取 RDC_UPDATE_SIGNING_KEY")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument(
        "--platform-unavailable",
        action="append",
        default=[],
        help="平台=原因, 在清单中标注该平台未提供产物, 可重复",
    )
    args = parser.parse_args(argv)

    if args.version != version:
        _emit(f"版本不一致: 参数 {args.version}, 代码版本 {version}; 请先修改 src/rdc/version.py")
        return 1

    repo = args.repo.strip() or _default_repo()
    tag = args.tag.strip() or f"v{args.version}"
    manifest_name = args.manifest_name.strip() or "update-manifest.json"
    build_root = Path(args.build_root)
    out_root = Path(args.out) if args.out else build_root / "release-sim"
    artifact_dirs = [Path(args.artifacts)] if args.artifacts else [build_root / "artifacts" / "desktop"]
    explicit = [Path(item) for item in args.artifact]

    try:
        artifacts = collect_artifacts(artifact_dirs, explicit, args.version)
    except ValueError as error:
        _emit(f"产物归集失败: {error}")
        return 1
    if not artifacts:
        _emit("没有找到符合命名规范的产物; 先执行 scripts/build_windows.py")
        return 1

    default_asset_base = f"https://github.com/{repo}/releases/download/{tag}"
    asset_url_base = "" if args.relative_asset_urls else (args.asset_url_base or default_asset_base)
    notes = _read_notes(args.notes_file) or list(DEFAULT_NOTES.splitlines())
    platforms = _platform_entries(artifacts, args.platform_unavailable)
    manifest = build_manifest(
        version=args.version,
        tag=tag,
        artifacts=artifacts,
        asset_url_base=asset_url_base,
        release_date=args.release_date,
        notes=notes,
        mandatory=bool(args.mandatory),
        min_version=args.min_version,
        platforms=platforms,
    )

    try:
        manifest = sign_manifest(manifest, args.signing_key.strip() or _default_signing_key())
    except ValueError as error:
        _emit(f"清单签名失败, 不出包: {error}")
        return 1

    staging = out_root / tag
    repo_root = out_root / "github" / repo / "releases"
    locations = [staging, repo_root / "download" / tag, repo_root / "latest" / "download"]
    for directory in locations:
        directory.mkdir(parents=True, exist_ok=True)
        for path, _, _ in artifacts:
            materialize(path, directory / path.name)
        write_text(directory / manifest_name, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    verifications = [verify_release(directory / manifest_name) for directory in locations]
    passed = all(bool(item["passed"]) for item in verifications)
    report = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": args.version,
        "platform_build_host": platform,
        "tag": tag,
        "repo": repo,
        "manifest_name": manifest_name,
        "asset_url_base": asset_url_base,
        "signature": {
            "alg": "ed25519",
            "key_id": str(manifest.get("signature", {}).get("key_id", "")),
            "signed": True,
        },
        "remote_actions_executed": False,
        "artifacts": [
            {"name": path.name, "platform": item_platform, "kind": kind, "size": path.stat().st_size}
            for path, item_platform, kind in artifacts
        ],
        "locations": [str(item) for item in locations],
        "verification": verifications,
        "passed": passed,
    }
    write_text(out_root / "release-report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    for path, item_platform, kind in artifacts:
        _emit(f"[产物] {item_platform:14s} {kind:9s} {path.name} ({path.stat().st_size} 字节)")
    for directory in locations:
        _emit(f"[清单] {directory / manifest_name}")
    for item in verifications:
        state = "通过" if item["passed"] else "失败"
        _emit(f"[回读] {item['manifest']}: {state} (验签 {'通过' if item['signature_ok'] else '失败'})")
    _emit("")
    names = " ".join(path.name for path, _, _ in artifacts)
    _emit("远端命令(仅打印, 未执行):")
    _emit(f"  git tag {tag} && git push origin {tag}")
    _emit(f'  gh release create {tag} {names} --repo {repo} --title "{tag}"')
    _emit(f"  gh release upload {tag} {manifest_name} --repo {repo}")
    _emit(f"仓库前置: https://github.com/{repo} 需已存在且为公开仓库; 私有仓库会把令牌带进客户端, 不接受.")
    _emit(f"演练输出: {out_root}")
    return 0 if passed else 1


def darwin_section(assets: list[dict[str, object]]) -> dict[str, object]:
    """按架构汇总 macOS 资产, 供清单 darwin 字段使用."""
    architectures: dict[str, dict[str, object]] = {}
    for asset in assets:
        platform = str(asset.get("platform", ""))
        kind = str(asset.get("kind", ""))
        if not platform.startswith("macos-"):
            continue
        arch = platform.split("-", 1)[1]
        entry = architectures.setdefault(arch, {"platform": platform, "assets": {}})
        slots = entry["assets"]
        if isinstance(slots, dict):
            slots[kind] = {
                "name": asset["name"],
                "url": asset["url"],
                "sha256": asset["sha256"],
                "size": asset["size"],
            }
    return {"signed": False, "notarized": False, "gatekeeper": GATEKEEPER_HINT, "architectures": architectures}


def _read_notes(path: str) -> list[str]:
    if not path:
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _platform_entries(artifacts: list[tuple[Path, str, str]], unavailable: list[str]) -> dict[str, dict[str, object]]:
    """汇总清单的 platforms 字段: 有产物的平台标 available, 其余按 平台=原因 标注."""
    entries: dict[str, dict[str, object]] = {}
    for _, platform, kind in artifacts:
        entry = entries.setdefault(platform, {"status": "available", "kinds": []})
        kinds = entry["kinds"]
        if isinstance(kinds, list) and kind not in kinds:
            kinds.append(kind)
    for item in unavailable:
        name, _, reason = item.partition("=")
        label = name.strip()
        if not label:
            continue
        entries[label] = {"status": "unavailable", "reason": reason.strip() or "未提供该平台产物"}
    return entries


if __name__ == "__main__":
    raise SystemExit(main())
