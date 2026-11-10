# ==========================================================================================
# 文件        : source.py
# 所属目录    : src/rdc/update/
# 文件用途    : 解析更新源配置, 统一 GitHub Releases、本地目录与自建镜像三种实现
# 架构定位    : 更新模块的配置层, 只做字符串解析, 不发起网络请求
# 核心功能    : 默认指向 GitHub 公开仓库; 支持仓库、清单文件名与 Release 资产的配置化
# 职责边界    : 不读取令牌; 不下载; 不区分新旧版本
# ==========================================================================================

"""更新源解析.

配置形态(均可通过环境变量或 config.json 切换, 改源不改代码):
- ``github:owner/repo``        默认; 读取 Release 资产里的清单, 公开仓库无需令牌
- ``github:owner/repo@v0.2.0`` 固定到某个 tag, 不再跟随 latest
- ``mirror:https://host/base`` 自建镜像; 清单地址为 ``<base>/<清单文件名>``
- ``local:D:\\dir``            本地目录; 清单地址为 ``<dir>/<清单文件名>``
- 直接给出 http(s) 清单地址或本地 json 文件路径也兼容

环境变量:
- ``RDC_UPDATE_SOURCE`` 完整更新源配置, 优先级最高
- ``RDC_UPDATE_REPO``   仅指定 GitHub 仓库, 与 ``github:`` 等价
- ``RDC_UPDATE_MANIFEST`` 清单资产名, 默认 update-manifest.json
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

DEFAULT_GITHUB_REPO = "itwuge/RemoteDesktop"
DEFAULT_MANIFEST_ASSET = "update-manifest.json"

KIND_GITHUB = "github"
KIND_MIRROR = "mirror"
KIND_LOCAL = "local"

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_url(url: str) -> bool:
    """判断地址是否指向本机回环; 本机发布演练允许 http, 其它 http 一律视为不安全."""
    host = (urlparse(url).hostname or "").strip().lower()
    return host in LOOPBACK_HOSTS or host.endswith(".localhost")


def is_insecure_http_url(url: str) -> bool:
    """非回环的 http 地址视为不安全: 生产只允许 https、本地目录或 file."""
    return urlparse(url).scheme == "http" and not is_loopback_url(url)


@dataclass(frozen=True)
class UpdateSource:
    """解析后的更新源; manifest_url 可直接读取."""

    kind: str
    location: str
    manifest_url: str
    tag: str = ""

    @property
    def label(self) -> str:
        """给界面与日志用的短标识, 不含令牌等敏感信息."""
        if self.kind == KIND_GITHUB:
            suffix = f"@{self.tag}" if self.tag else ""
            return f"github:{self.location}{suffix}"
        return f"{self.kind}:{self.location}"


def manifest_asset_name() -> str:
    """清单资产名; 可用 RDC_UPDATE_MANIFEST 覆盖."""
    configured = os.environ.get("RDC_UPDATE_MANIFEST", "").strip()
    return configured or DEFAULT_MANIFEST_ASSET


def default_source_value() -> str:
    """默认更新源: 环境变量 > GitHub 仓库变量 > 项目仓库."""
    explicit = os.environ.get("RDC_UPDATE_SOURCE", "").strip()
    if explicit:
        return explicit
    repo = os.environ.get("RDC_UPDATE_REPO", "").strip()
    if repo:
        return f"github:{repo}"
    return f"github:{DEFAULT_GITHUB_REPO}"


def release_asset_url(repo: str, tag: str, asset_name: str) -> str:
    """GitHub Release 资产直链, 公开仓库可直接下载, 无需令牌."""
    slug = _normalize_repo(repo)
    if tag:
        return f"https://github.com/{slug}/releases/download/{tag}/{asset_name}"
    return f"https://github.com/{slug}/releases/latest/download/{asset_name}"


def parse_source(value: str | None) -> UpdateSource:
    """把配置字符串解析为更新源; 无法识别的一律按本地目录处理."""
    text = (value or "").strip() or f"github:{DEFAULT_GITHUB_REPO}"
    lowered = text.lower()
    if lowered.startswith("github:"):
        return _parse_github(text[len("github:") :])
    if lowered.startswith("mirror:"):
        return _parse_mirror(text[len("mirror:") :])
    if lowered.startswith("local:"):
        return _parse_local(text[len("local:") :])

    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.lower() in {"github.com", "www.github.com"}:
            return _parse_github_url(parsed)
        base = text.rstrip("/")
        if base.lower().endswith(".json"):
            return UpdateSource(KIND_MIRROR, text, text)
        return UpdateSource(KIND_MIRROR, base, f"{base}/{manifest_asset_name()}")
    if parsed.scheme == "file":
        return _parse_local(url2pathname(parsed.path))
    return _parse_local(text)


def _normalize_repo(repo: str) -> str:
    slug = repo.strip().strip("/")
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    if not _REPO_PATTERN.match(slug):
        raise ValueError(f"GitHub 仓库应为 owner/repo 形式: {repo}")
    return slug


def _parse_github(body: str) -> UpdateSource:
    repo_part, _, tag = body.strip().partition("@")
    slug = _normalize_repo(repo_part)
    return UpdateSource(KIND_GITHUB, slug, release_asset_url(slug, tag, manifest_asset_name()), tag)


def _parse_github_url(parsed: object) -> UpdateSource:
    path = str(getattr(parsed, "path", "")).strip("/")
    parts = [item for item in path.split("/") if item]
    if len(parts) < 2:
        raise ValueError(f"GitHub 更新源缺少 owner/repo: {getattr(parsed, 'geturl', lambda: '')()}")
    slug = _normalize_repo("/".join(parts[:2]))
    tag = ""
    if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "download":
        tag = parts[4]
    return UpdateSource(KIND_GITHUB, slug, release_asset_url(slug, tag, manifest_asset_name()), tag)


def _parse_mirror(body: str) -> UpdateSource:
    base = body.strip().rstrip("/")
    if not base:
        raise ValueError("mirror 更新源缺少地址")
    if base.lower().endswith(".json"):
        return UpdateSource(KIND_MIRROR, base, base)
    return UpdateSource(KIND_MIRROR, base, f"{base}/{manifest_asset_name()}")


def _parse_local(body: str) -> UpdateSource:
    raw = body.strip()
    if not raw:
        raise ValueError("local 更新源缺少目录")
    target = Path(raw).expanduser()
    if target.suffix.lower() == ".json":
        return UpdateSource(KIND_LOCAL, str(target.parent), str(target.resolve()))
    return UpdateSource(KIND_LOCAL, str(target), str((target / manifest_asset_name()).resolve()))
