# ==========================================================================================
# 文件        : manifest.py
# 所属目录    : src/rdc/update/
# 文件用途    : 更新清单解析、字段校验、平台产物选择与下载地址解析
# 架构定位    : 更新模块的数据层, 不发起网络请求
# 核心功能    : 兼容 v1 单产物与 v2 多平台产物; 校验版本、sha256、日期、说明、强制标记
# 职责边界    : 不下载; 不比较新旧; 不生成用户文案以外的内容
# ==========================================================================================

"""更新清单.

v2 清单按平台列出 Release 资产, 客户端只取当前平台与当前形态(安装/便携)的产物:

    {
      "schema": 2,
      "version": "0.2.0",
      "release_date": "2026-11-10",
      "notes": ["..."],
      "mandatory": false,
      "min_supported_version": "0.1.0",
      "assets": [
        {"platform": "windows-x64", "kind": "installer", "name": "...-setup.exe",
         "url": "...", "sha256": "...", "size": 35519250}
      ]
    }

v1 单产物清单(顶层 download_url + sha256)继续兼容, 便于旧清单与本地演练.
下载地址允许 http、https、file 三种协议或本地路径, 其余协议一律拒绝.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from rdc.update.versioning import parse_version
from rdc.version import platform_tag

ALLOWED_SCHEMES = {"http", "https", "file"}
COMMON_FIELDS = ("version", "release_date", "notes", "mandatory", "min_supported_version")
FLAT_FIELDS = (*COMMON_FIELDS, "download_url", "sha256")
ASSET_FIELDS = ("platform", "kind", "name", "url", "sha256")
ASSET_KINDS = {"installer", "portable"}

_PLATFORM_ALIASES = {
    "windows-amd64": "windows-x64",
    "windows-x86_64": "windows-x64",
    "macos-x86_64": "macos-x64",
    "macos-aarch64": "macos-arm64",
    "linux-amd64": "linux-x64",
    "linux-aarch64": "linux-arm64",
}


@dataclass(frozen=True)
class ManifestAsset:
    """一条平台产物记录; url 已解析为可直接访问的地址."""

    platform: str
    kind: str
    name: str
    url: str
    sha256: str
    size: int = 0


@dataclass(frozen=True)
class UpdateManifest:
    """一条更新清单; download_url 与 sha256 是当前平台选中产物的值."""

    version: str
    download_url: str
    sha256: str
    release_date: str
    notes: str
    mandatory: bool
    min_supported_version: str
    source: str
    schema: int = 1
    asset_platform: str = "any"
    asset_kind: str = ""
    asset_name: str = ""
    assets: tuple[ManifestAsset, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """转换为可序列化字典; source 只用于诊断, 不参与更新判定."""
        return {
            "schema": self.schema,
            "version": self.version,
            "download_url": self.download_url,
            "sha256": self.sha256,
            "release_date": self.release_date,
            "notes": self.notes,
            "mandatory": self.mandatory,
            "min_supported_version": self.min_supported_version,
            "source": self.source,
            "asset_platform": self.asset_platform,
            "asset_kind": self.asset_kind,
            "asset_name": self.asset_name,
            "assets": [
                {
                    "platform": asset.platform,
                    "kind": asset.kind,
                    "name": asset.name,
                    "url": asset.url,
                    "sha256": asset.sha256,
                    "size": asset.size,
                }
                for asset in self.assets
            ],
        }


def _require_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"更新清单字段 {field} 必须是非空字符串")
    return value.strip()


def _parse_sha256(value: str, field: str = "sha256") -> str:
    digest = value.strip().lower()
    if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
        raise ValueError(f"更新清单字段 {field} 必须是 64 位十六进制")
    return digest


def _parse_notes(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        lines = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        text = "\n".join(lines)
    else:
        raise ValueError("更新清单字段 notes 必须是字符串或字符串数组")
    if not text:
        raise ValueError("更新清单字段 notes 不能为空")
    return text


def _parse_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"更新清单字段 release_date 不是合法日期: {value}") from error
    return value


def _parse_mandatory(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("更新清单字段 mandatory 必须是布尔值")
    return value


def _safe_name(resolved_url: str, raw_url: str) -> str:
    resolved_path = urlparse(resolved_url).path.replace("\\", "/")
    name = posixpath.basename(resolved_path) or posixpath.basename(raw_url.replace("\\", "/"))
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("更新清单字段 download_url 缺少合法文件名")
    return name


def resolve_download_url(source: str, download_url: str) -> str:
    """把清单中的下载地址解析为可直接访问的地址."""
    parsed = urlparse(download_url)
    if parsed.scheme:
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ValueError(f"不支持的下载协议: {parsed.scheme}")
        return download_url

    source_parsed = urlparse(source)
    if source_parsed.scheme in {"http", "https", "file"}:
        return urljoin(source, download_url)

    if Path(download_url).is_absolute():
        return str(Path(download_url))
    return str((Path(source).parent / download_url).resolve())


def normalize_platform(value: str) -> str:
    """统一平台标识写法, 例如 windows-amd64 与 windows-x64 等价."""
    lowered = value.strip().lower().replace("_", "-")
    return _PLATFORM_ALIASES.get(lowered, lowered)


def _parse_asset(payload: dict[str, object], source: str) -> ManifestAsset:
    missing = [field for field in ASSET_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"更新清单 assets 条目缺少字段: {', '.join(missing)}")
    platform = _require_text(payload, "platform")
    kind = _require_text(payload, "kind")
    if kind not in ASSET_KINDS:
        raise ValueError(f"更新清单 assets 条目 kind 只允许 {', '.join(sorted(ASSET_KINDS))}: {kind}")
    raw_url = _require_text(payload, "url")
    resolved = resolve_download_url(source, raw_url)
    declared = str(payload.get("name") or "").strip()
    name = declared or _safe_name(resolved, raw_url)
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"更新清单 assets 条目 name 不合法: {name}")
    size = payload.get("size", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("更新清单 assets 条目 size 必须是非负整数")
    return ManifestAsset(
        platform=platform,
        kind=kind,
        name=name,
        url=resolved,
        sha256=_parse_sha256(_require_text(payload, "sha256"), "assets.sha256"),
        size=size,
    )


def _select_asset(assets: tuple[ManifestAsset, ...], platform: str, preferred_kind: str) -> ManifestAsset:
    target = normalize_platform(platform)
    if preferred_kind:
        for asset in assets:
            if normalize_platform(asset.platform) == target and asset.kind == preferred_kind:
                return asset
    for asset in assets:
        if normalize_platform(asset.platform) == target:
            return asset
    available = ", ".join(sorted({normalize_platform(asset.platform) for asset in assets}))
    raise ValueError(f"更新清单缺少平台 {target} 的产物; 现有: {available}")


def parse_manifest(
    payload: object,
    *,
    source: str,
    platform: str | None = None,
    preferred_kind: str = "",
) -> UpdateManifest:
    """校验并构造更新清单; 任一字段不合法抛出 ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("更新清单必须是 JSON 对象")

    common_missing = [field for field in COMMON_FIELDS if field not in payload]
    if common_missing:
        raise ValueError(f"更新清单缺少字段: {', '.join(common_missing)}")

    version = _require_text(payload, "version")
    parse_version(version)
    min_supported_version = _require_text(payload, "min_supported_version")
    parse_version(min_supported_version)
    release_date = _parse_date(_require_text(payload, "release_date"))
    mandatory = _parse_mandatory(payload.get("mandatory"))
    notes = _parse_notes(payload.get("notes"))

    raw_assets = payload.get("assets")
    if raw_assets is None:
        return _parse_flat(
            payload,
            source=source,
            version=version,
            release_date=release_date,
            notes=notes,
            mandatory=mandatory,
            min_supported_version=min_supported_version,
        )
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ValueError("更新清单字段 assets 必须是非空数组")
    assets = tuple(_parse_asset(item, source) for item in raw_assets if isinstance(item, dict))
    if len(assets) != len(raw_assets):
        raise ValueError("更新清单 assets 每个条目都必须是 JSON 对象")
    selected = _select_asset(assets, platform or platform_tag(), preferred_kind)
    return UpdateManifest(
        version=version,
        download_url=selected.url,
        sha256=selected.sha256,
        release_date=release_date,
        notes=notes,
        mandatory=mandatory,
        min_supported_version=min_supported_version,
        source=source,
        schema=2,
        asset_platform=selected.platform,
        asset_kind=selected.kind,
        asset_name=selected.name,
        assets=assets,
    )


def _parse_flat(
    payload: dict[str, object],
    *,
    source: str,
    version: str,
    release_date: str,
    notes: str,
    mandatory: bool,
    min_supported_version: str,
) -> UpdateManifest:
    """解析 v1 单产物清单, 兼容本地演练与旧清单."""
    missing = [field for field in ("download_url", "sha256") if field not in payload]
    if missing:
        raise ValueError(f"更新清单缺少字段: {', '.join(missing)}")
    raw_url = _require_text(payload, "download_url")
    resolved_url = resolve_download_url(source, raw_url)
    name = _safe_name(resolved_url, raw_url)
    return UpdateManifest(
        version=version,
        download_url=resolved_url,
        sha256=_parse_sha256(_require_text(payload, "sha256")),
        release_date=release_date,
        notes=notes,
        mandatory=mandatory,
        min_supported_version=min_supported_version,
        source=source,
        schema=1,
        asset_platform="any",
        asset_kind="",
        asset_name=name,
        assets=(),
    )
