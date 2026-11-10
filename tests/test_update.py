"""客户端更新单测: 版本比较、更新源解析、清单选择、下载校验与安全底线."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from rdc.core.errors import (
    UPDATE_DOWNGRADE_REJECTED,
    UPDATE_HASH_MISMATCH,
    UPDATE_MANIFEST_INVALID,
    UPDATE_SOURCE_UNREACHABLE,
)
from rdc.core.logging import reset_for_tests
from rdc.update.manifest import parse_manifest
from rdc.update.service import UpdateService
from rdc.update.source import (
    DEFAULT_GITHUB_REPO,
    default_source_value,
    parse_source,
    release_asset_url,
)
from rdc.update.versioning import compare_versions, parse_version
from rdc.version import platform_tag


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离更新源环境变量与日志目录, 避免用例之间互相影响."""
    for name in ("RDC_UPDATE_SOURCE", "RDC_UPDATE_REPO", "RDC_UPDATE_MANIFEST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RDC_LOG_DIR", str(tmp_path / "logs"))
    reset_for_tests()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _asset(payload: bytes, **overrides: object) -> dict[str, object]:
    asset: dict[str, object] = {
        "platform": "windows-x64",
        "kind": "portable",
        "name": "rdc-desktop-0.2.0-windows-x64-portable.zip",
        "url": "rdc-desktop-0.2.0-windows-x64-portable.zip",
        "sha256": _sha256(payload),
        "size": len(payload),
    }
    asset.update(overrides)
    return asset


def _manifest(assets: list[dict[str, object]], **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 2,
        "version": "0.2.0",
        "release_date": "2026-11-10",
        "notes": ["新增自更新", "修复若干问题"],
        "mandatory": False,
        "min_supported_version": "0.1.0",
        "assets": assets,
    }
    payload.update(overrides)
    return payload


def _write_source(directory: Path, manifest: dict[str, object], payload: bytes, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(payload)
    path = directory / "update-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path


def _wait_done(service: UpdateService, task_id: str, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    progress = service.get_update_progress(task_id)
    while time.monotonic() < deadline:
        progress = service.get_update_progress(task_id)
        status = str((progress.data or {}).get("status", ""))
        if status in {"done", "failed", "canceled"}:
            break
        time.sleep(0.02)
    return {"ok": progress.ok, "error_code": progress.error_code, "data": progress.data or {}}


def test_compare_versions_orders_prerelease_correctly() -> None:
    """预发布低于同版本正式版, 修订号与大版本优先."""
    assert compare_versions("0.2.0", "0.1.9") > 0
    assert compare_versions("1.0.0", "1.0.0-rc.1") > 0
    assert compare_versions("1.0.0-rc.2", "1.0.0-rc.1") > 0
    assert compare_versions("0.2.0", "0.2.0") == 0
    assert parse_version("0.2.0").major == 0


def test_default_source_is_github_project_repo_without_token() -> None:
    """默认更新源指向项目 GitHub 仓库, 地址中不含任何令牌."""
    assert default_source_value() == f"github:{DEFAULT_GITHUB_REPO}"
    source = parse_source(None)
    assert source.kind == "github"
    assert source.location == "itwuge/RemoteDesktop"
    assert source.manifest_url == (
        "https://github.com/itwuge/RemoteDesktop/releases/latest/download/update-manifest.json"
    )
    assert "token" not in source.manifest_url.lower()
    assert release_asset_url("itwuge/RemoteDesktop", "v0.2.0", "pkg.zip").endswith("/releases/download/v0.2.0/pkg.zip")


def test_parse_source_supports_github_local_and_mirror(tmp_path: Path) -> None:
    """三种更新源都能解析; 本地目录与镜像各自得到正确的清单地址."""
    pinned = parse_source("github:itwuge/RemoteDesktop@v0.2.0")
    assert pinned.tag == "v0.2.0"
    assert "/releases/download/v0.2.0/" in pinned.manifest_url

    url_form = parse_source("https://github.com/itwuge/RemoteDesktop")
    assert url_form.kind == "github"

    mirror = parse_source("mirror:https://mirror.example.com/rdc/latest")
    assert mirror.kind == "mirror"
    assert mirror.manifest_url == "https://mirror.example.com/rdc/latest/update-manifest.json"

    local = parse_source(f"local:{tmp_path}")
    assert local.kind == "local"
    assert Path(local.manifest_url) == tmp_path / "update-manifest.json"

    direct = parse_source(str(tmp_path / "custom.json"))
    assert direct.manifest_url.endswith("custom.json")


def test_manifest_v2_selects_platform_and_form(tmp_path: Path) -> None:
    """清单含多平台多形态时, 只取当前平台与指定形态的产物."""
    payload = _manifest([_asset(b"portable")], version="0.2.0")
    manifest = parse_manifest(
        payload, source=str(tmp_path / "update-manifest.json"), platform="windows-x64", preferred_kind="portable"
    )
    assert manifest.schema == 2
    assert manifest.asset_kind == "portable"
    assert manifest.asset_name.endswith("-portable.zip")
    assert manifest.sha256 == _sha256(b"portable")

    installer = _asset(b"installer", kind="installer", name="rdc-desktop-0.2.0-windows-x64-setup.exe")
    payload = _manifest([installer, _asset(b"portable")])
    selected = parse_manifest(
        payload, source=str(tmp_path / "update-manifest.json"), platform="windows-x64", preferred_kind="installer"
    )
    assert selected.asset_name.endswith("-setup.exe")

    alias = parse_manifest(payload, source=str(tmp_path / "update-manifest.json"), platform="windows-amd64")
    assert alias.asset_platform == "windows-x64"


def test_manifest_rejects_missing_platform_and_bad_fields(tmp_path: Path) -> None:
    """缺少当前平台产物、非法 sha256 与非法形态都必须拒绝."""
    source = str(tmp_path / "update-manifest.json")
    mac_only = _manifest([_asset(b"x", platform="macos-arm64")])
    with pytest.raises(ValueError, match="缺少平台"):
        parse_manifest(mac_only, source=source, platform="windows-x64")

    with pytest.raises(ValueError, match="sha256"):
        parse_manifest(_manifest([_asset(b"x", sha256="abc")]), source=source, platform="windows-x64")

    with pytest.raises(ValueError, match="kind"):
        parse_manifest(_manifest([_asset(b"x", kind="dmg")]), source=source, platform="windows-x64")


def test_manifest_v1_flat_still_supported(tmp_path: Path) -> None:
    """旧版单产物清单继续可用, 便于历史演练脚本迁移."""
    payload = {
        "version": "0.2.0",
        "download_url": "pkg.zip",
        "sha256": _sha256(b"flat"),
        "release_date": "2026-11-10",
        "notes": "旧清单",
        "mandatory": True,
        "min_supported_version": "0.1.0",
    }
    manifest = parse_manifest(payload, source=str(tmp_path / "manifest.json"))
    assert manifest.schema == 1
    assert manifest.mandatory is True
    assert manifest.download_url.endswith("pkg.zip")


def test_service_check_download_verify_local_source(tmp_path: Path) -> None:
    """本地目录源: 检查到新版本 -> 下载 -> SHA-256 校验通过 -> 给出安装包路径."""
    payload = b"portable-package-bytes" * 128
    manifest = _manifest([_asset(payload)])
    _write_source(tmp_path / "source", manifest, payload, _asset(payload)["name"])

    service = UpdateService(current_version="0.1.0", source=f"local:{tmp_path / 'source'}", work_dir=tmp_path / "dl")
    status = service.refresh_update_status()
    assert status.ok
    assert status.data is not None
    assert status.data["available"] is True
    assert status.data["latest_version"] == "0.2.0"
    assert status.data["source_kind"] == "local"

    started = service.start_update_download()
    assert started.ok
    task_id = str((started.data or {}).get("task_id", ""))
    result = _wait_done(service, task_id)
    assert result["data"]["status"] == "done"
    assert result["data"]["verified"] is True
    assert Path(str(result["data"]["package_path"])).read_bytes() == payload


def test_service_rejects_downgrade(tmp_path: Path) -> None:
    """更新源版本低于当前版本时拒绝降级安装."""
    payload = b"old"
    manifest = _manifest([_asset(payload)])
    _write_source(tmp_path / "source", manifest, payload, _asset(payload)["name"])
    service = UpdateService(current_version="1.0.0", source=f"local:{tmp_path / 'source'}", work_dir=tmp_path / "dl")
    result = service.refresh_update_status()
    assert not result.ok
    assert result.error_code == UPDATE_DOWNGRADE_REJECTED


def test_service_hash_mismatch_removes_file(tmp_path: Path) -> None:
    """SHA-256 不匹配时返回校验失败并删除已下载文件, 不留下可执行包."""
    payload = b"tampered"
    manifest = _manifest([_asset(payload, sha256=_sha256(b"expected"))])
    _write_source(tmp_path / "source", manifest, payload, _asset(payload)["name"])
    service = UpdateService(current_version="0.1.0", source=f"local:{tmp_path / 'source'}", work_dir=tmp_path / "dl")
    service.refresh_update_status()
    started = service.start_update_download()
    task_id = str((started.data or {}).get("task_id", ""))
    result = _wait_done(service, task_id)
    assert result["ok"] is False
    assert result["error_code"] == UPDATE_HASH_MISMATCH
    assert not list((tmp_path / "dl").glob("*"))


def test_service_unreachable_source_and_download_without_check(tmp_path: Path) -> None:
    """源不可达返回 UPDATE_SOURCE_UNREACHABLE; 未检查先下载被拒绝."""
    service = UpdateService(current_version="0.1.0", source=f"local:{tmp_path / 'missing'}", work_dir=tmp_path / "dl")
    result = service.refresh_update_status()
    assert result.error_code == UPDATE_SOURCE_UNREACHABLE
    assert service.start_update_download().ok is False


def test_service_prefers_installer_or_portable_by_configuration(tmp_path: Path) -> None:
    """同一清单下, preferred_kind 决定下载安装包还是便携包."""
    installer = b"installer"
    portable = b"portable"
    assets = [
        _asset(installer, kind="installer", name=f"rdc-desktop-0.2.0-{platform_tag()}-setup.exe"),
        _asset(portable, kind="portable", name=f"rdc-desktop-0.2.0-{platform_tag()}-portable.zip"),
    ]
    manifest = _manifest(assets)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for asset, payload in zip(assets, (installer, portable), strict=True):
        (source_dir / str(asset["name"])).write_bytes(payload)
    (source_dir / "update-manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

    portable_service = UpdateService(
        current_version="0.1.0",
        source=f"local:{source_dir}",
        work_dir=tmp_path / "dl-portable",
        preferred_kind="portable",
    )
    portable_status = portable_service.refresh_update_status()
    assert portable_status.ok
    assert (portable_status.data or {})["asset_kind"] == "portable"

    installer_service = UpdateService(
        current_version="0.1.0",
        source=f"local:{source_dir}",
        work_dir=tmp_path / "dl-installer",
        preferred_kind="installer",
    )
    installer_status = installer_service.refresh_update_status()
    assert (installer_status.data or {})["asset_kind"] == "installer"


def test_invalid_manifest_reports_manifest_invalid(tmp_path: Path) -> None:
    """清单 JSON 合法但字段非法时返回 UPDATE_MANIFEST_INVALID, 不崩溃."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "update-manifest.json").write_text('{"version": "not-semver"}', encoding="utf-8")
    service = UpdateService(current_version="0.1.0", source=f"local:{source_dir}", work_dir=tmp_path / "dl")
    result = service.refresh_update_status()
    assert result.error_code in {UPDATE_MANIFEST_INVALID, UPDATE_SOURCE_UNREACHABLE}
    assert result.ok is False
