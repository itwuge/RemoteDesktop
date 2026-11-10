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

import rdc.relay.server as relay_module
from rdc.core.errors import AUTH_FAILED
from rdc.crypto.identity import DeviceIdentity
from rdc.relay.server import MAX_FRAME_BYTES, RelayConfig, RelayServer, load_config, recv_frame, send_frame
from rdc.signaling.envelope import build_envelope

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "relay" / "relay.conf.example"
AUTH_SESSION = "33333333-3333-4333-8333-333333333333"


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


def test_example_config_is_valid_and_budget_matches_formula(tmp_path):
    path = tmp_path / "relay.conf"
    path.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false"),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.port == 8750
    assert config.health_bind == "127.0.0.1"
    assert config.degrade_profile in ("L0", "L1", "L2", "L3")
    # 上线默认值: 域名与端口已脱敏, 3 Mbps 出口, 1 路 1080p30(高运动回落 720p30)
    assert config.relay_endpoint == "relay://<RELAY-DOMAIN>:8750"
    assert config.egress_mbps == 3
    assert config.max_sessions == 1
    # 线上实测已含封装膨胀, 不再二次扣 8%: 3 Mbps × (1 − 0.0) ÷ 1 = 3000 kbps
    assert config.overhead_ratio == 0.0
    assert config.budget_kbps == 3000
    # 固定档: 1920x1080 @ 30fps, 目标 2800 kbps 必须落在单路预算内
    assert config.target_kbps == 2800
    assert (config.profile_width, config.profile_height, config.profile_fps) == (1920, 1080, 30)
    # 预览上限按实测 720p 读正文 392 kbps 上调到 400, 不再硬编码 300
    assert config.preview_max_kbps == 400


def test_budget_follows_relay_policy_not_hardcoded():
    assert _config(egress_mbps=3, max_sessions=1).budget_kbps == 2760
    assert _config(egress_mbps=10, max_sessions=2).budget_kbps == 4600
    assert _config(egress_mbps=20, max_sessions=1, overhead_ratio=0.0).budget_kbps == 20000


def test_dataplane_can_be_disabled_for_standalone_signaling():
    """独立信令部署: 关闭数据面后仍保留信令、预算与健康检查."""
    server = RelayServer(_config(dataplane_enabled=False, overhead_ratio=0.0))
    assert server.dataplane is None
    assert server.config.budget_kbps == 3000


def test_load_config_reads_dataplane_flag(tmp_path):
    path = tmp_path / "relay.conf"
    text = (
        EXAMPLE.read_text(encoding="utf-8")
        .replace("degrade_profile = L0", "degrade_profile = L0\ndataplane_enabled = false")
        .replace("tls_enabled = true", "tls_enabled = false")
    )
    path.write_text(text, encoding="utf-8")
    assert load_config(path).dataplane_enabled is False


def test_load_config_reads_extra_ports(tmp_path):
    """额外监听端口从 [server] extra_ports 读取, 逗号分隔并去重; 缺省为空."""
    template = EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false")
    path = tmp_path / "relay-extra.conf"
    path.write_text(template.replace("port = 8750", "port = 8750\nextra_ports = 443, 8443, 443"), encoding="utf-8")
    assert load_config(path).extra_ports == (443, 8443)
    plain = tmp_path / "relay-plain.conf"
    plain.write_text(template, encoding="utf-8")
    assert load_config(plain).extra_ports == ()


def test_load_config_reads_open_ports(tmp_path):
    """open_ports 从 [server] 读取, 与 extra_ports 独立表达; 缺省为空."""
    template = EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false")
    path = tmp_path / "relay-open.conf"
    path.write_text(
        template.replace("port = 8750", "port = 8750\nextra_ports = 443, 8443\nopen_ports = 443"),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.extra_ports == (443, 8443)
    assert config.open_ports == (443,)
    plain = tmp_path / "relay-open-plain.conf"
    plain.write_text(template, encoding="utf-8")
    assert load_config(plain).open_ports == ()


def test_load_config_rejects_bad_extra_ports(tmp_path):
    """非法端口值直接拒绝, 不静默丢弃."""
    template = EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false")
    for value in ("abc", "0", "70000"):
        path = tmp_path / f"relay-{value}.conf"
        path.write_text(template.replace("port = 8750", f"port = 8750\nextra_ports = {value}"), encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(path)


def test_load_config_rejects_bad_values(tmp_path):
    template = EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false")
    for bad, fragment in (
        ("degrade_profile=L9", "degrade_profile"),
        ("max_sessions=0", "max_sessions"),
        ("overhead_ratio=1.5", "overhead_ratio"),
        ("egress_mbps=0", "egress_mbps"),
        ("port=70000", "端口"),
        ("target_kbps=5000", "超过单会话预算"),
        ("profile_fps=0", "profile"),
        ("max_session_kbps=0", "max_session_kbps"),
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


def _auth_connection(server: RelayServer, identity: DeviceIdentity) -> None:
    """走真实网络边界完成 register → challenge → auth."""
    register = build_envelope(
        identity,
        "register",
        {
            "device_id": identity.device_id,
            "public_key": identity.public_key_b64,
            "capabilities": ["desktop"],
            "protocol_version": 1,
        },
    )
    challenge = server._process(json.dumps(register).encode("utf-8"), None)[0]
    assert challenge["type"] == "challenge"
    nonce = str(challenge["payload"]["nonce"])
    auth = build_envelope(
        identity,
        "auth",
        {
            "device_id": identity.device_id,
            "signature": identity.sign({"device_id": identity.device_id, "nonce": nonce}),
        },
    )
    responses = server._process(json.dumps(auth).encode("utf-8"), None)
    assert responses[0]["type"] == "auth_ok"


def test_unauthenticated_connection_cannot_get_relay_allocated():
    """A1 回归: 未认证连接自报公钥不能通过验签拿到控制令牌.

    旧实现 _public_key 会回退到 payload.public_key, 本用例在旧代码下会拿到
    relay_allocated; 修复后必须返回鉴权类错误且不产生任何授予.
    """
    server = RelayServer(_config(overhead_ratio=0.0))
    attacker = DeviceIdentity.generate()
    forged = build_envelope(
        attacker,
        "relay_allocate",
        {
            "session_id": AUTH_SESSION,
            "requested_kbps": 2900,
            "est_duration_s": 60,
            "public_key": attacker.public_key_b64,
        },
        session_id=AUTH_SESSION,
    )
    responses = server._process(json.dumps(forged).encode("utf-8"), None)
    assert all(item.get("type") != "relay_allocated" for item in responses)
    assert responses[0]["type"] == "error"
    assert responses[0]["payload"]["code"] == AUTH_FAILED
    assert server.signaling.relay_grants == {}


def test_public_ipv6_peer_still_fails_auth_and_is_audited(monkeypatch):
    """开放端口放行来源后, 认证失败仍被拒并写 auth.failed 审计, 不因来源放宽而跳过."""
    server = RelayServer(_config(allow_ips="127.0.0.1/32", open_ports=(443,)))
    peer = "2409:8a00:1234:5678::1"
    assert server.guard.admit(peer, 443) == (True, "")
    server.guard.release(peer)
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        relay_module,
        "log_event",
        lambda _module, event, **fields: events.append((event, fields)),
    )
    stranger = DeviceIdentity.generate()
    frame = build_envelope(stranger, "auth", {"device_id": stranger.device_id, "signature": "x"})
    client, server_sock = socket.socketpair()
    try:
        client.settimeout(5.0)
        send_frame(client, frame)
        thread = threading.Thread(target=server._handle_connection, args=(server_sock, peer), daemon=True)
        thread.start()
        # 认证失败必须回错误帧; 读到之后关闭客户端让服务端收尾
        assert recv_frame(client) is not None
        client.close()
        thread.join(5)
    finally:
        server_sock.close()

    failed = [fields for event, fields in events if event == "auth.failed"]
    assert failed and failed[0]["error_code"] == AUTH_FAILED
    assert failed[0]["peer_ip"] == peer


def test_relay_allocate_requires_session_membership():
    """已认证但不是会话成员的设备不能申请中继预算."""
    server = RelayServer(_config(overhead_ratio=0.0))
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    outsider = DeviceIdentity.generate()
    for identity in (alice, bob, outsider):
        _auth_connection(server, identity)
    offer = build_envelope(
        alice,
        "offer",
        {
            "session_id": AUTH_SESSION,
            "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
            "media_caps": {"codecs": ["h264"]},
            "access_proof": "cHJvb2Y=",
            "from_device_id": alice.device_id,
            "to_device_id": bob.device_id,
        },
        session_id=AUTH_SESSION,
    )
    server._process(json.dumps(offer).encode("utf-8"), alice.device_id)
    assert AUTH_SESSION in server.signaling.sessions

    request = build_envelope(
        outsider,
        "relay_allocate",
        {"session_id": AUTH_SESSION, "requested_kbps": 2900, "est_duration_s": 60},
        session_id=AUTH_SESSION,
    )
    responses = server._process(json.dumps(request).encode("utf-8"), outsider.device_id)
    assert responses[0]["type"] == "error"
    assert responses[0]["payload"]["code"] == AUTH_FAILED
    assert server.signaling.relay_grants == {}


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


def test_healthz_omits_placeholder_relay_endpoint():
    server = RelayServer(_config(relay_endpoint="relay://<RELAY-DOMAIN>:8750"))
    health = server._build_health_server()
    thread = threading.Thread(target=health.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = health.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        assert "relay_endpoint" not in body
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
        allow_disable_tls=False,
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


def test_example_config_enables_tls_by_default():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "\ntls_enabled = true" in text
    assert "\ntls_enabled = false" not in text


def test_install_config_refuses_plaintext_without_explicit_flag(tmp_path):
    unsafe = tmp_path / "unsafe.conf"
    unsafe.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false"),
        encoding="utf-8",
    )
    deployer = _deployer(tmp_path, config_source=str(unsafe), force_config=True)
    with pytest.raises(Exception, match="tls_enabled=false"):
        deployer._install_config()
    assert not Path(deployer.args.config).exists()


def test_install_config_allows_plaintext_only_with_explicit_flag(tmp_path):
    unsafe = tmp_path / "unsafe.conf"
    unsafe.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("tls_enabled = true", "tls_enabled = false"),
        encoding="utf-8",
    )
    deployer = _deployer(tmp_path, config_source=str(unsafe), force_config=True, allow_disable_tls=True)
    deployer._install_config()
    assert "tls_enabled = false" in Path(deployer.args.config).read_text(encoding="utf-8")


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
    # 服务端保留来源 IP 用于取证, 客户端侧脱敏不在此单元
    assert "RDC_LOG_REDACT_IP=0" in unit
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
