"""中继 TLS 1.3 配置校验与接入测试.

证书相关用例在 D:\\RDC-Build\\certs\\relay 缺失时自动跳过, 不因环境缺证书而失败。
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from rdc.connect import relay as relay_module
from rdc.connect.relay import RelayUnavailable, relay_ca_path
from rdc.relay.server import RelayConfig, RelayServer, load_config, recv_frame, send_frame
from rdc.relay.tls import build_client_context, build_server_context

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "relay" / "relay.conf.example"
CERT_FILES = ("ca.crt", "server.crt", "server.key", "client.crt", "client.key", "wrong-ca.crt")


def _cert_dir() -> Path:
    explicit = os.environ.get("RDC_TLS_CERT_DIR")
    if explicit:
        return Path(explicit)
    build = os.environ.get("RDC_BUILD_ROOT")
    if build:
        return Path(build) / "certs" / "relay"
    return Path(r"D:\RDC-Build\certs\relay")


def _require_certs() -> Path:
    directory = _cert_dir()
    if not all((directory / name).is_file() for name in CERT_FILES):
        pytest.skip(f"本地测试证书缺失: {directory}; 按 deploy/relay/README.md 生成后重跑")
    return directory


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config(**overrides) -> RelayConfig:
    values = {
        "bind": "127.0.0.1",
        "port": 0,
        "health_bind": "127.0.0.1",
        "health_port": 0,
        "relay_endpoint": "relay://127.0.0.1:8750",
        "egress_mbps": 3,
        "overhead_ratio": 0.08,
        "max_sessions": 1,
        "degrade_profile": "L0",
    }
    values.update(overrides)
    return RelayConfig(**values)


def _write_config(
    tmp_path: Path,
    *,
    tls_enabled: str = "false",
    cert_file: str = "",
    key_file: str = "",
    ca_file: str = "",
    require_client_cert: str = "false",
) -> Path:
    """以样例为基底重写 [tls] 段, 其余配置保持上线默认值."""
    base = EXAMPLE.read_text(encoding="utf-8").split("[tls]")[0].rstrip()
    text = "\n".join(
        [
            base,
            "",
            "[tls]",
            f"tls_enabled = {tls_enabled}",
            f"cert_file = {cert_file}",
            f"key_file = {key_file}",
            f"ca_file = {ca_file}",
            f"require_client_cert = {require_client_cert}",
            "",
        ]
    )
    path = tmp_path / "relay-tls.conf"
    path.write_text(text, encoding="utf-8")
    return path


def _touch_cert(tmp_path: Path) -> tuple[Path, Path, Path]:
    cert = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    ca = tmp_path / "ca.crt"
    for path in (cert, key, ca):
        path.write_text("placeholder\n", encoding="utf-8")
    return cert, key, ca


# --------------------------------------------------------------- 配置校验


def test_tls_disabled_still_supports_plaintext_mode(tmp_path):
    path = _write_config(tmp_path, tls_enabled="false")
    config = load_config(path)
    assert config.tls_enabled is False
    assert config.require_client_cert is False
    assert RelayServer(config).tls_context is None


def test_tls_section_is_parsed(tmp_path):
    cert, key, ca = _touch_cert(tmp_path)
    path = _write_config(
        tmp_path,
        tls_enabled="true",
        cert_file=str(cert),
        key_file=str(key),
        ca_file=str(ca),
        require_client_cert="true",
    )
    config = load_config(path)
    assert config.tls_enabled is True
    assert config.require_client_cert is True
    assert config.cert_file == str(cert)
    assert config.key_file == str(key)
    assert config.ca_file == str(ca)


def test_tls_enabled_without_cert_or_key_fails(tmp_path):
    path = _write_config(tmp_path, tls_enabled="true")
    with pytest.raises(ValueError, match="cert_file"):
        load_config(path)


def test_tls_enabled_with_missing_files_fails(tmp_path):
    path = _write_config(
        tmp_path,
        tls_enabled="true",
        cert_file=str(tmp_path / "absent.crt"),
        key_file=str(tmp_path / "absent.key"),
    )
    with pytest.raises(ValueError, match="不存在"):
        load_config(path)


def test_require_client_cert_without_ca_fails(tmp_path):
    cert, key, _ = _touch_cert(tmp_path)
    path = _write_config(
        tmp_path,
        tls_enabled="true",
        cert_file=str(cert),
        key_file=str(key),
        require_client_cert="true",
    )
    with pytest.raises(ValueError, match="ca_file"):
        load_config(path)


def test_require_client_cert_requires_tls_enabled(tmp_path):
    path = _write_config(tmp_path, tls_enabled="false", require_client_cert="true")
    with pytest.raises(ValueError, match="tls_enabled"):
        load_config(path)


def test_invalid_tls_boolean_is_rejected(tmp_path):
    path = _write_config(tmp_path, tls_enabled="maybe")
    with pytest.raises(ValueError, match="布尔值"):
        load_config(path)


def test_server_context_rejects_unreadable_cert(tmp_path):
    cert, key, _ = _touch_cert(tmp_path)
    with pytest.raises(ValueError, match="加载服务端证书失败"):
        build_server_context(cert_file=str(cert), key_file=str(key))


def test_server_context_requires_ca_for_mutual_tls():
    with pytest.raises(ValueError, match="ca_file"):
        build_server_context(cert_file="a.crt", key_file="a.key", require_client_cert=True)


# --------------------------------------------------------------- TLS 1.3 接入


def _wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"中继端口 {port} 未就绪")


def _start_server(config: RelayConfig) -> tuple[RelayServer, threading.Thread, int]:
    port = _free_port()
    config = replace(config, port=port, health_port=_free_port())
    server = RelayServer(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_port(port)
    return server, thread, port


def _connect_tls(port: int, context: ssl.SSLContext) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    raw.settimeout(5)
    return context.wrap_socket(raw, server_hostname="localhost")


def test_tls13_minimum_version_and_unidirectional_access():
    directory = _require_certs()
    server, thread, port = _start_server(
        _config(
            tls_enabled=True,
            cert_file=str(directory / "server.crt"),
            key_file=str(directory / "server.key"),
        )
    )
    try:
        assert server.tls_context is not None
        assert server.tls_context.minimum_version == ssl.TLSVersion.TLSv1_3
        assert server.tls_context.verify_mode == ssl.CERT_NONE
        context = build_client_context(ca_file=str(directory / "ca.crt"))
        connection = _connect_tls(port, context)
        try:
            assert connection.version() == "TLSv1.3"
            send_frame(connection, {"not": "an-envelope"})
            response = json.loads(recv_frame(connection).decode("utf-8"))
        finally:
            connection.close()
        # 帧格式未变: 非法包络仍回签名 error 包络
        assert response["type"] == "error"
    finally:
        server.stop()
        thread.join(timeout=10)


def test_mutual_tls_rejects_client_without_certificate():
    directory = _require_certs()
    server, thread, port = _start_server(
        _config(
            tls_enabled=True,
            cert_file=str(directory / "server.crt"),
            key_file=str(directory / "server.key"),
            ca_file=str(directory / "ca.crt"),
            require_client_cert=True,
        )
    )
    try:
        assert server.tls_context is not None
        assert server.tls_context.verify_mode == ssl.CERT_REQUIRED
        context = build_client_context(ca_file=str(directory / "ca.crt"))
        rejected = False
        try:
            connection = _connect_tls(port, context)
            try:
                send_frame(connection, {"plane": "data", "v": 1, "type": "relay_hello"})
                try:
                    rejected = recv_frame(connection) is None
                except (OSError, ValueError):
                    rejected = True
            finally:
                connection.close()
        except (ssl.SSLError, OSError):
            rejected = True
        assert rejected
    finally:
        server.stop()
        thread.join(timeout=10)


def test_relay_ca_resolves_from_bundled_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """冻结包内 assets/ca.crt 应作为信任锚命中, 不依赖开发树路径."""
    bundle = tmp_path / "bundle"
    assets = bundle / "assets"
    assets.mkdir(parents=True)
    ca_file = assets / "ca.crt"
    ca_file.write_text("dummy-ca", encoding="utf-8")
    monkeypatch.delenv("RDC_RELAY_CA", raising=False)
    monkeypatch.setattr(relay_module.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(relay_module.sys, "frozen", True, raising=False)

    assert relay_ca_path() == ca_file


def test_relay_ca_reports_chinese_reason_when_all_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """环境变量与包内资源都不存在时, 给中文原因且不返回空信任锚."""
    monkeypatch.delenv("RDC_RELAY_CA", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(relay_module, "bundled_ca_roots", lambda: (empty,))
    monkeypatch.setattr(relay_module, "default_ca_path", lambda: empty / "dev" / "ca.crt")

    with pytest.raises(RelayUnavailable) as excinfo:
        relay_ca_path()
    message = str(excinfo.value)
    assert "缺少中继信任锚" in message
    assert "RDC_RELAY_CA" in message


def test_relay_ca_env_var_wins_over_bundled_assets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """显式配置的 RDC_RELAY_CA 优先于包内资源."""
    override = tmp_path / "custom" / "ca.crt"
    override.parent.mkdir()
    override.write_text("custom-ca", encoding="utf-8")
    bundled = tmp_path / "bundle" / "assets" / "ca.crt"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("dummy-ca", encoding="utf-8")
    monkeypatch.setenv("RDC_RELAY_CA", str(override))
    monkeypatch.setattr(relay_module.sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)

    assert relay_ca_path() == override


def test_mutual_tls_accepts_client_with_valid_certificate():
    directory = _require_certs()
    server, thread, port = _start_server(
        _config(
            tls_enabled=True,
            cert_file=str(directory / "server.crt"),
            key_file=str(directory / "server.key"),
            ca_file=str(directory / "ca.crt"),
            require_client_cert=True,
        )
    )
    try:
        context = build_client_context(
            ca_file=str(directory / "ca.crt"),
            cert_file=str(directory / "client.crt"),
            key_file=str(directory / "client.key"),
        )
        connection = _connect_tls(port, context)
        try:
            assert connection.version() == "TLSv1.3"
            send_frame(connection, {"not": "an-envelope"})
            response = json.loads(recv_frame(connection).decode("utf-8"))
        finally:
            connection.close()
        assert response["type"] == "error"
    finally:
        server.stop()
        thread.join(timeout=10)
