"""中继服务与部署脚本的离线可校验测试."""

from __future__ import annotations

import argparse
import importlib.util
import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from rdc.relay.server import MAX_FRAME_BYTES, RelayConfig, RelayServer, load_config, recv_frame, send_frame

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
        "max_sessions": 1,
        "degrade_profile": "L0",
    }
    values.update(overrides)
    return RelayConfig(**values)


def _load_deploy_module():
    path = ROOT / "deploy" / "relay" / "deploy.py"
    spec = importlib.util.spec_from_file_location("rdc_deploy_tool", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- 配置与预算


def test_example_config_is_valid_and_budget_matches_formula():
    config = load_config(EXAMPLE)
    assert config.port == 8750
    assert config.health_bind == "127.0.0.1"
    assert config.degrade_profile in ("L0", "L1", "L2", "L3")
    # 上线默认值: 域名与端口已脱敏, 3 Mbps 出口, 2 路并发
    assert config.relay_endpoint == "relay://<RELAY-DOMAIN>:8750"
    assert config.egress_mbps == 3
    assert config.max_sessions == 2
    # 3 Mbps × (1 − 0.08) ÷ 2 = 1380 kbps
    assert config.budget_kbps == 1380


def test_budget_follows_relay_policy_not_hardcoded():
    assert _config(egress_mbps=3, max_sessions=1).budget_kbps == 2760
    assert _config(egress_mbps=10, max_sessions=2).budget_kbps == 4600
    assert _config(egress_mbps=20, max_sessions=1, overhead_ratio=0.0).budget_kbps == 20000


def test_load_config_rejects_bad_values(tmp_path):
    template = EXAMPLE.read_text(encoding="utf-8")
    for bad, fragment in (
        ("degrade_profile=L9", "degrade_profile"),
        ("max_sessions=0", "max_sessions"),
        ("overhead_ratio=1.5", "overhead_ratio"),
        ("egress_mbps=0", "egress_mbps"),
        ("port=70000", "端口"),
    ):
        key, value = bad.split("=")
        lines = []
        for line in template.splitlines():
            name = line.split("=", 1)[0].strip()
            lines.append(f"{name} = {value}" if name == key and "=" in line else line)
        path = tmp_path / f"{key}.conf"
        path.write_text("\n".join(lines), encoding="utf-8")
        with pytest.raises(ValueError, match=fragment):
            load_config(path)
    with pytest.raises(ValueError, match="不可读"):
        load_config(tmp_path / "missing.conf")


# --------------------------------------------------------------- 帧收发


def test_frame_roundtrip_uses_length_prefix():
    left, right = socket.socketpair()
    try:
        send_frame(left, {"type": "ping", "text": "远程"})
        assert recv_frame(right) == json.dumps(
            {"type": "ping", "text": "远程"}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    finally:
        left.close()
        right.close()


def test_frame_rejects_illegal_length():
    reader, writer = socket.socketpair()
    try:
        for header in (b"\x00\x00\x00\x00", b"\x00\x10\x00\x01"):
            writer.sendall(header)
            if int.from_bytes(header, "big") > MAX_FRAME_BYTES:
                with pytest.raises(ValueError, match="帧长度非法"):
                    recv_frame(reader)
            else:
                with pytest.raises(ValueError, match="帧长度非法"):
                    recv_frame(reader)
    finally:
        reader.close()
        writer.close()


def test_frame_returns_none_when_peer_closed():
    reader, writer = socket.socketpair()
    writer.close()
    try:
        assert recv_frame(reader) is None
    finally:
        reader.close()


# --------------------------------------------------------------- 服务行为


def test_invalid_payload_returns_signed_error_envelope():
    server = RelayServer(_config())
    responses = server._process(b"not-json")
    assert len(responses) == 1
    envelope = responses[0]
    assert envelope["type"] == "error"
    assert envelope["payload"]["message"]


def test_invalid_envelope_shape_is_rejected_without_crash():
    server = RelayServer(_config())
    responses = server._process(json.dumps({"only": "one"}).encode("utf-8"))
    assert responses[0]["type"] == "error"


def test_healthz_reports_budget_and_profile():
    server = RelayServer(_config(egress_mbps=5, max_sessions=2, degrade_profile="L1"))
    health = server._build_health_server()
    thread = threading.Thread(target=health.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = health.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert body["status"] == "ok"
        assert body["service"] == "rdc-relay"
        assert body["budget_kbps"] == 2300
        assert body["degrade_profile"] == "L1"
        assert body["devices"] == 0
    finally:
        health.shutdown()
        health.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------- 部署脚本


def _deployer(tmp_path: Path, **overrides):
    module = _load_deploy_module()
    args = argparse.Namespace(
        dry_run=False,
        allow_unsupported=False,
        force_config=False,
        no_restart=False,
        user="rdc",
        group="rdc",
        prefix=str(tmp_path / "opt"),
        logdir=str(tmp_path / "log"),
        config=str(tmp_path / "etc" / "relay.conf"),
        config_source=str(EXAMPLE),
        unit=str(tmp_path / "etc" / "rdc-relay.service"),
        python="/usr/bin/python3",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return module.Deployer(args)


def test_write_is_idempotent_on_unchanged_content(tmp_path):
    deployer = _deployer(tmp_path)
    target = tmp_path / "etc" / "relay.conf"
    assert deployer._write(target, "[server]\nport=8750\n", 0o640) is True
    assert target.read_text(encoding="utf-8") == "[server]\nport=8750\n"
    assert deployer._write(target, "[server]\nport=8750\n", 0o640) is False


def test_existing_config_is_not_overwritten_unless_forced(tmp_path):
    deployer = _deployer(tmp_path)
    config = Path(deployer.args.config)
    config.parent.mkdir(parents=True)
    config.write_text("手工修改过的配置\n", encoding="utf-8")

    deployer._install_config()
    assert config.read_text(encoding="utf-8") == "手工修改过的配置\n"

    forced = _deployer(tmp_path, force_config=True)
    forced._install_config()
    assert "egress_mbps" in config.read_text(encoding="utf-8")


def test_unit_template_renders_paths_and_no_hardcoded_credentials(tmp_path):
    deployer = _deployer(tmp_path)
    calls = []
    deployer._act = lambda message, command: calls.append((message, command))
    deployer._install_unit()
    unit = Path(deployer.args.unit).read_text(encoding="utf-8")
    assert "@" not in unit.replace("@system-service", "")
    assert str(tmp_path / "opt") in unit
    assert "User=rdc" in unit
    assert ("systemctl daemon-reload", ["systemctl", "daemon-reload"]) in calls
    for secret in ("password", "passwd", "token", "secret"):
        assert secret not in unit.lower()


def test_dry_run_touches_nothing(tmp_path):
    deployer = _deployer(tmp_path, dry_run=True)
    assert deployer.run() == 0
    assert not (tmp_path / "etc").exists()
    assert not (tmp_path / "opt").exists()
    assert not (tmp_path / "log").exists()


def test_tree_hash_ignores_bytecode(tmp_path):
    deployer = _deployer(tmp_path)
    app = tmp_path / "app"
    (app / "pkg").mkdir(parents=True)
    (app / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = deployer._tree_hash(app)
    (app / "pkg" / "__pycache__").mkdir()
    (app / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00")
    assert deployer._tree_hash(app) == before
    (app / "pkg" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert deployer._tree_hash(app) != before
