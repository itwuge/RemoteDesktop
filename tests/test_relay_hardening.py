"""中继服务加固: 配置项、连接准入、健康指标与证书到期读取."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from rdc.relay.server import ConnectionGuard, RelayConfig, RelayServer, load_config, parse_ip_networks
from rdc.relay.tls import certificate_days_left, certificate_not_after_epoch

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "relay" / "relay.conf.example"


def _config(**overrides) -> RelayConfig:
    values = {
        "bind": "127.0.0.1",
        "port": 0,
        "health_bind": "127.0.0.1",
        "health_port": 0,
        "relay_endpoint": "relay://127.0.0.1:8750",
        "egress_mbps": 3,
        "overhead_ratio": 0.08,
        "max_sessions": 2,
        "degrade_profile": "L0",
    }
    values.update(overrides)
    return RelayConfig(**values)


def _rewrite(tmp_path: Path, key: str, value: str) -> Path:
    """按配置键替换一行, 其余保持上线样例原样."""
    lines = []
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        name = line.split("=", 1)[0].strip()
        lines.append(f"{name} = {value}" if name == key and "=" in line and not line.lstrip().startswith("#") else line)
    path = tmp_path / f"{key}.conf"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cert_dir() -> Path:
    explicit = os.environ.get("RDC_TLS_CERT_DIR")
    if explicit:
        return Path(explicit)
    build = os.environ.get("RDC_BUILD_ROOT")
    if build:
        return Path(build) / "certs" / "relay"
    return Path(r"D:\RDC-Build\certs\relay")


# --------------------------------------------------------------- 配置


def test_example_config_exposes_hardening_defaults():
    """样例配置给出三道上限、最低预算与日志级别, 与代码默认值一致."""
    config = load_config(EXAMPLE)
    assert config.max_connections == 64
    assert config.max_connections_per_ip == 8
    assert config.connect_rate_per_min == 60
    assert config.min_session_kbps == 200
    assert config.log_level == "INFO"
    assert config.allow_ips == ""
    assert config.deny_ips == ""


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("max_connections", "0", "max_connections"),
        ("max_connections_per_ip", "0", "max_connections_per_ip"),
        ("max_connections_per_ip", "128", "不得大于"),
        ("connect_rate_per_min", "-1", "不得为负数"),
        ("min_session_kbps", "0", "min_session_kbps"),
        ("level", "TRACE", "日志级别"),
        ("deny_ips", "10.0.0.0/33", "deny_ips"),
        ("allow_ips", "not-an-ip", "allow_ips"),
    ],
)
def test_load_config_rejects_bad_hardening_values(tmp_path, key, value, fragment):
    with pytest.raises(ValueError, match=fragment):
        load_config(_rewrite(tmp_path, key, value))


def test_parse_ip_networks_accepts_plain_and_cidr():
    networks = parse_ip_networks(" 10.0.0.1, 192.168.0.0/16 ,", "allow_ips")
    assert len(networks) == 2
    assert parse_ip_networks("", "allow_ips") == ()


# --------------------------------------------------------------- 连接准入


def test_allow_and_deny_lists_are_enforced():
    """deny 优先; allow 非空时未列出的来源一律拒绝."""
    guard = ConnectionGuard(_config(allow_ips="10.0.0.0/24", deny_ips="10.0.0.9"))
    assert guard.admit("10.0.0.1") == (True, "")
    guard.release("10.0.0.1")
    assert guard.admit("10.0.0.9") == (False, "denied")
    assert guard.admit("192.168.1.1") == (False, "denied")
    assert guard.snapshot()["rejected"]["denied"] == 2


def test_connection_rate_is_limited_per_ip():
    guard = ConnectionGuard(_config(connect_rate_per_min=2))
    assert guard.admit("10.1.1.1")[0] is True
    assert guard.admit("10.1.1.1")[0] is True
    assert guard.admit("10.1.1.1") == (False, "rate_limited")
    # 另一个来源不受影响
    assert guard.admit("10.1.1.2")[0] is True


def test_total_and_per_ip_connection_limits():
    guard = ConnectionGuard(_config(max_connections=2, max_connections_per_ip=1, connect_rate_per_min=0))
    assert guard.admit("10.2.2.1")[0] is True
    assert guard.admit("10.2.2.1") == (False, "too_many")
    assert guard.admit("10.2.2.2")[0] is True
    assert guard.admit("10.2.2.3") == (False, "too_many")
    guard.release("10.2.2.1")
    assert guard.admit("10.2.2.3")[0] is True


# --------------------------------------------------------------- 健康指标


def test_healthz_reports_limits_presence_and_focus():
    server = RelayServer(_config(egress_mbps=3, max_sessions=2))
    health = server._build_health_server()
    thread = threading.Thread(target=health.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = health.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["min_session_kbps"] == 200
        assert body["connections"]["max"] == 64
        assert body["connections"]["per_ip_max"] == 8
        assert body["connections"]["active"] == 0
        assert body["signaling"]["online_devices"] == 0
        assert body["signaling"]["connected_devices"] == 0
        assert body["focus"]["focused"] is False
        assert body["tls"]["enabled"] is False
        assert body["dataplane"]["rejected_sessions"] == 0
        assert body["uptime_s"] >= 0
    finally:
        health.shutdown()
        health.server_close()
        thread.join(timeout=5)


def test_certificate_expiry_is_readable():
    """证书到期时间可被健康检查读出; 本地无测试证书时跳过, 不伪造通过."""
    directory = _cert_dir()
    cert = directory / "server.crt"
    if not cert.is_file():
        pytest.skip(f"本地测试证书缺失: {directory}")
    expires_at = certificate_not_after_epoch(str(cert))
    assert expires_at is not None
    assert expires_at > time.time()
    days = certificate_days_left(str(cert))
    assert days is not None
    assert days >= 0
    assert certificate_not_after_epoch(str(directory / "missing.crt")) is None
