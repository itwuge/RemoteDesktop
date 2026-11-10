"""客户端更新: 版本比较、更新源、更新清单与下载校验."""

from rdc.update.manifest import ManifestAsset, UpdateManifest, parse_manifest, resolve_download_url
from rdc.update.service import UpdateService
from rdc.update.source import (
    DEFAULT_GITHUB_REPO,
    UpdateSource,
    default_source_value,
    manifest_asset_name,
    parse_source,
    release_asset_url,
)
from rdc.update.versioning import SemVer, compare_versions, parse_version

__all__ = [
    "DEFAULT_GITHUB_REPO",
    "ManifestAsset",
    "SemVer",
    "UpdateManifest",
    "UpdateService",
    "UpdateSource",
    "compare_versions",
    "default_source_value",
    "manifest_asset_name",
    "parse_manifest",
    "parse_source",
    "parse_version",
    "release_asset_url",
    "resolve_download_url",
]
