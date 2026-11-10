"""设置与诊断命令: 默认值、持久化、校验、连通性探测与诊断汇总的单元测试."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from rdc.commands import api, settings
from rdc.commands.service import CommandService
from rdc.core import runtime
from rdc.core.errors import CONFIG_INVALID, RELAY_UNREACHABLE
from rdc.version import __version__


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把本机配置目录指向临时目录, 不碰真实用户配置."""
    monkeypatch.setenv("RDC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def service() -> CommandService:
    """每个用例一套干净的命令后端."""
    backend = CommandService()
    api.configure(backend)
    return backend


# --------------------------------------------------------------- 读取与写入


def test_defaults_when_config_missing(data_dir: Path) -> None:
    result = settings.get_settings()
    assert result.ok
    values = result.data["settings"]
    assert values == settings.DEFAULT_SETTINGS
    assert result.data["config_path"] == str(data_dir / runtime.CONFIG_FILE_NAME)


def test_save_roundtrip_and_keeps_unrelated_keys(data_dir: Path) -> None:
    (data_dir / runtime.CONFIG_FILE_NAME).write_text(json.dumps({"update_source": "local:/tmp/feed"}), encoding="utf-8")
    saved = settings.save_settings("192.168.37.132:27888", "书房主机", "720p30", "DEBUG", True)
    assert saved.ok, saved.message

    payload = json.loads((data_dir / runtime.CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    assert payload["relay_endpoint"] == "192.168.37.132:27888"
    assert payload["device_name"] == "书房主机"
    assert payload["log_level"] == "DEBUG"
    assert payload["auto_start"] is True
    assert payload["update_source"] == "local:/tmp/feed"

    reloaded = settings.get_settings().data["settings"]
    assert reloaded["device_name"] == "书房主机"
    assert reloaded["auto_start"] is True


def test_empty_endpoint_is_allowed(data_dir: Path) -> None:
    result = settings.save_settings("", "", "720p30", "INFO", False)
    assert result.ok
    assert result.data["settings"]["relay_endpoint"] == ""


def test_clipboard_auto_sync_default_is_persisted_and_applied(data_dir: Path) -> None:
    from rdc.clipboard.sync import ClipboardSyncService
    from rdc.commands import clipboard

    clipboard.configure(ClipboardSyncService(enabled=True))
    saved = settings.save_settings("", "主机", "720p30", "INFO", False, clipboard_auto_sync=False)
    assert saved.ok, saved.message

    payload = json.loads((data_dir / runtime.CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    assert payload["clipboard_auto_sync"] is False
    assert settings.get_settings().data["settings"]["clipboard_auto_sync"] is False
    assert clipboard.get_service().enabled is False


def test_clipboard_auto_sync_must_be_bool(data_dir: Path) -> None:
    result = settings.save_settings("", "主机", "720p30", "INFO", False, clipboard_auto_sync="yes")
    assert not result.ok
    assert result.error_code == CONFIG_INVALID
    assert "剪贴板同步默认值" in result.message


# --------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    "endpoint",
    ["192.168.37.132", "192.168.37.132:0", "192.168.37.132:70000", "host name:80", "http://:80"],
)
def test_invalid_endpoint_rejected(data_dir: Path, endpoint: str) -> None:
    result = settings.save_settings(endpoint, "主机", "720p30", "INFO", False)
    assert not result.ok
    assert result.error_code == CONFIG_INVALID
    assert not (data_dir / runtime.CONFIG_FILE_NAME).exists()


def test_invalid_device_name_and_enums_rejected(data_dir: Path) -> None:
    too_long = settings.save_settings("", "名" * (settings.MAX_DEVICE_NAME + 1), "720p30", "INFO", False)
    assert not too_long.ok and too_long.error_code == CONFIG_INVALID
    bad_level = settings.save_settings("", "主机", "720p30", "TRACE", False)
    assert not bad_level.ok and bad_level.error_code == CONFIG_INVALID
    bad_profile = settings.save_settings("", "主机", "1080p60", "INFO", False)
    assert not bad_profile.ok and bad_profile.error_code == CONFIG_INVALID


def test_write_failure_reports_reason(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = data_dir / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("RDC_DATA_DIR", str(blocker))
    result = settings.save_settings("", "主机", "720p30", "INFO", False)
    assert not result.ok
    assert result.error_code == CONFIG_INVALID
    assert "配置写入失败" in result.message


# --------------------------------------------------------------- 地址解析


def test_parse_endpoint_accepts_forms() -> None:
    assert settings.parse_endpoint("192.168.37.132:27888") == ("192.168.37.132", 27888)
    assert settings.parse_endpoint("relay://relay.example.com:443") == ("relay.example.com", 443)
    assert settings.parse_endpoint(" wss://relay.example.com:8443/path ") == ("relay.example.com", 8443)
    with pytest.raises(ValueError):
        settings.parse_endpoint("")


# --------------------------------------------------------------- 连通性


def test_check_server_reports_rtt_for_local_listener() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        result = settings.check_server(f"127.0.0.1:{port}", timeout_s=1.0)
    finally:
        server.close()
    assert result.ok, result.message
    assert result.message.startswith("中继可达，RTT ")
    assert result.data["reachable"] is True
    assert result.data["rtt_ms"] >= 0


def test_check_server_reports_unreachable() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    result = settings.check_server(f"127.0.0.1:{port}", timeout_s=0.5)
    assert not result.ok
    assert result.error_code == RELAY_UNREACHABLE
    assert result.message.startswith("不可达")
    assert result.data["reachable"] is False


def test_check_server_rejects_bad_address() -> None:
    result = settings.check_server("relay.example.com")
    assert not result.ok
    assert result.error_code == CONFIG_INVALID


# --------------------------------------------------------------- 诊断


def test_diagnostics_contains_version_source_and_device(data_dir: Path, service: CommandService) -> None:
    result = settings.get_diagnostics()
    assert result.ok
    data = result.data
    assert data["version"] == __version__
    assert data["update_source"].startswith("github:")
    assert len(data["device_id"]) == 20
    assert data["last_connection"] == "暂无连接记录"


def test_diagnostics_shows_pending_connection(data_dir: Path, service: CommandService) -> None:
    peer = "ABCDEFGHIJKLMNOPQRST"
    service.register_peer(peer, "对端")
    started = api.start_connection(peer, "123456")
    assert started.ok
    result = settings.get_diagnostics()
    assert "任务等待中" in result.data["last_connection"]
