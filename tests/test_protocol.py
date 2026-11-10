"""protocol-v1 单测: 七字段包络、31 类消息、签名、时间窗口与服务端路由."""

from __future__ import annotations

import copy

import pytest

from rdc.core.errors import AUTH_FAILED, PEER_OFFLINE
from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import (
    ENVELOPE_FIELDS,
    ProtocolError,
    build_envelope,
    validate_envelope,
    verify_envelope,
)
from rdc.signaling.messages import MESSAGE_TYPES, REQUIRED_PAYLOAD
from rdc.signaling.server import InMemorySignalingServer

SESSION_ID = "8a7b6c5d-4e3f-2a1b-9c8d-7e6f5a4b3c2d"

SAMPLE_PAYLOADS: dict[str, dict] = {
    "register": {
        "device_id": "A" * 20,
        "public_key": "cHVibGlj",
        "capabilities": ["desktop"],
        "protocol_version": 1,
    },
    "challenge": {"nonce": "bm9uY2U=", "expires_at": "2026-11-10T10:05:00+08:00"},
    "auth": {"device_id": "A" * 20, "signature": "c2ln"},
    "auth_ok": {
        "token": "dG9rZW4=",
        "expires_at": "2026-11-10T10:05:00+08:00",
        "relay_endpoint": "relay://127.0.0.1:7000",
    },
    "offer": {
        "session_id": SESSION_ID,
        "candidates": [{"kind": "host", "address": "192.168.31.80", "port": 5000, "priority": 1}],
        "media_caps": {"codecs": ["h264"]},
        "access_proof": "cHJvb2Y=",
    },
    "answer": {
        "session_id": SESSION_ID,
        "chosen_candidate": {"kind": "host", "address": "192.168.31.81", "port": 5000, "priority": 1},
        "media_caps": {"codecs": ["h264"]},
    },
    "candidate": {
        "session_id": SESSION_ID,
        "kind": "srflx",
        "address": "203.0.113.5",
        "port": 5001,
        "priority": 2,
    },
    "relay_allocate": {"session_id": SESSION_ID, "requested_kbps": 5000, "est_duration_s": 60},
    "relay_allocated": {
        "session_id": SESSION_ID,
        "relay_token": "cmVsYXk=",
        "endpoint": "relay://127.0.0.1:7000",
        "budget_kbps": 2760,
        "quality_profile": "L0",
    },
    "session_start": {"session_id": SESSION_ID, "transport_kind": "direct", "quality_profile": "1080p30"},
    "session_end": {"session_id": SESSION_ID, "reason": "用户结束会话"},
    "display_update": {"session_id": SESSION_ID, "width": 1080, "height": 1920, "rotation": 90},
    "heartbeat": {"session_id": SESSION_ID, "seq": 1},
    "presence": {"device_id": "A" * 20, "state": "online"},
    "file_offer": {
        "session_id": SESSION_ID,
        "transfer_id": "t-1",
        "items": [{"relative": "a.bin", "size": 2048, "sha256": "0" * 64}],
        "total_bytes": 2048,
        "chunk_size": 262144,
        "digest_alg": "sha256",
    },
    "file_accept": {"session_id": SESSION_ID, "transfer_id": "t-1", "accepted_items": ["a.bin"]},
    "file_chunk": {
        "session_id": SESSION_ID,
        "transfer_id": "t-1",
        "file_index": 0,
        "offset": 0,
        "length": 6,
        "sha256": "0" * 64,
        "data": "Y2h1bms=",
    },
    "file_ack": {"session_id": SESSION_ID, "transfer_id": "t-1", "file_index": 0, "acked_bytes": 6},
    "file_complete": {"session_id": SESSION_ID, "transfer_id": "t-1", "digest": "0" * 64},
    "file_abort": {"session_id": SESSION_ID, "transfer_id": "t-1", "reason": "用户取消"},
    "clipboard_update": {
        "session_id": SESSION_ID,
        "kind": "text",
        "size_bytes": 6,
        "digest": "0" * 64,
        "content": "aGVsbG8=",
    },
    "clipboard_request": {"session_id": SESSION_ID, "kind": "text"},
    "clipboard_ack": {"session_id": SESSION_ID, "kind": "text", "digest": "0" * 64, "applied": True},
    "wall_focus": {"wall_id": "wall-1", "cell": 2},
    "wall_focus_granted": {
        "wall_id": "wall-1",
        "cell": 2,
        "focus_token": "f" * 32,
        "expires_at": "2026-11-10T12:00:00Z",
    },
    "wall_unfocus": {"wall_id": "wall-1"},
    "wall_unfocused": {"wall_id": "wall-1", "released": True},
    "wall_preview": {"wall_id": "wall-1", "cell": 3},
    "wall_preview_granted": {
        "wall_id": "wall-1",
        "cell": 3,
        "preview_token": "p" * 32,
        "expires_at": "2026-11-10T12:00:00Z",
    },
    "wall_preview_revoked": {"wall_id": "wall-1", "cell": 3, "reason": "墙已关闭"},
    "error": {"code": "PEER_OFFLINE", "message": "对端设备不在线"},
}


def test_message_type_count_and_payload_map() -> None:
    """恰好 31 类消息(v1.2 presence、v1.3 传输与剪贴板、v1.4 屏幕墙), 每类都有必填字段声明."""
    assert len(MESSAGE_TYPES) == 31
    assert set(REQUIRED_PAYLOAD) == set(MESSAGE_TYPES)
    assert set(ENVELOPE_FIELDS) == {"v", "type", "id", "session_id", "ts", "payload", "sig"}


@pytest.mark.parametrize("msg_type", sorted(MESSAGE_TYPES))
def test_all_24_types_build_and_verify(msg_type: str) -> None:
    """24 类消息都能构造七字段包络并通过校验与验签."""
    identity = DeviceIdentity.generate()
    envelope = build_envelope(identity, msg_type, SAMPLE_PAYLOADS[msg_type], session_id=SESSION_ID)
    assert set(envelope) == set(ENVELOPE_FIELDS)
    validate_envelope(envelope)
    assert verify_envelope(envelope, identity.public_key_b64) is True


def test_tampered_payload_fails_verification() -> None:
    """改动载荷后签名失效."""
    identity = DeviceIdentity.generate()
    envelope = build_envelope(identity, "heartbeat", {"session_id": SESSION_ID, "seq": 1})
    tampered = copy.deepcopy(envelope)
    tampered["payload"]["seq"] = 2
    assert verify_envelope(tampered, identity.public_key_b64) is False


def test_missing_envelope_field_rejected() -> None:
    """缺任一字段即不合法."""
    identity = DeviceIdentity.generate()
    envelope = build_envelope(identity, "heartbeat", {"session_id": SESSION_ID, "seq": 1})
    del envelope["sig"]
    with pytest.raises(ProtocolError) as raised:
        validate_envelope(envelope)
    assert raised.value.code == "MISSING_FIELD"


def test_missing_payload_field_rejected() -> None:
    """类型必填载荷缺失被拒绝."""
    identity = DeviceIdentity.generate()
    envelope = build_envelope(identity, "heartbeat", {"session_id": SESSION_ID})
    with pytest.raises(ProtocolError) as raised:
        validate_envelope(envelope)
    assert raised.value.code == "MISSING_FIELD"


@pytest.mark.parametrize(
    "msg_type",
    [
        "file_offer",
        "file_accept",
        "file_chunk",
        "file_ack",
        "file_complete",
        "file_abort",
        "clipboard_update",
        "clipboard_request",
        "clipboard_ack",
    ],
)
def test_v13_new_types_reject_missing_required_field(msg_type: str) -> None:
    """v1.3 新增 9 类消息: 缺任一必填载荷字段即拒绝且验签为假."""
    identity = DeviceIdentity.generate()
    payload = dict(SAMPLE_PAYLOADS[msg_type])
    del payload[REQUIRED_PAYLOAD[msg_type][0]]
    envelope = build_envelope(identity, msg_type, payload, session_id=SESSION_ID)
    with pytest.raises(ProtocolError) as raised:
        validate_envelope(envelope)
    assert raised.value.code == "MISSING_FIELD"
    assert msg_type in raised.value.message
    assert verify_envelope(envelope, identity.public_key_b64) is False


def test_unknown_message_type_rejected() -> None:
    """不在 24 类协议内的类型不得构造."""
    identity = DeviceIdentity.generate()
    with pytest.raises(ProtocolError) as raised:
        build_envelope(identity, "file_pause", {}, session_id=SESSION_ID)
    assert raised.value.code == "UNKNOWN_TYPE"


def test_version_and_timestamp_window() -> None:
    """版本不是 1 或时间戳超窗都被拒绝."""
    identity = DeviceIdentity.generate()
    envelope = build_envelope(identity, "heartbeat", {"session_id": SESSION_ID, "seq": 1})
    envelope["v"] = 2
    with pytest.raises(ProtocolError) as raised:
        validate_envelope(envelope)
    assert raised.value.code == "PROTOCOL_VERSION_UNSUPPORTED"

    old = build_envelope(
        identity,
        "heartbeat",
        {"session_id": SESSION_ID, "seq": 1},
        timestamp="2020-01-01T00:00:00+08:00",
    )
    with pytest.raises(ProtocolError) as raised:
        validate_envelope(old)
    assert raised.value.code == "INVALID_MESSAGE"


def _register(server: InMemorySignalingServer, identity: DeviceIdentity) -> str:
    """完成注册并返回挑战 nonce."""
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
    responses = server.handle(register)
    assert len(responses) == 1
    challenge = responses[0]
    assert challenge["type"] == "challenge"
    assert verify_envelope(challenge, server.identity.public_key_b64) is True
    return str(challenge["payload"]["nonce"])


def _authenticate(server: InMemorySignalingServer, identity: DeviceIdentity) -> None:
    """完成注册、认证并断言 auth_ok."""
    nonce = _register(server, identity)
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    auth = build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature})
    responses = server.handle(auth)
    assert len(responses) == 1
    assert responses[0]["type"] == "auth_ok"
    assert responses[0]["payload"]["relay_endpoint"] == server.relay_endpoint


def test_register_auth_and_route() -> None:
    """两台设备注册认证后, offer 被转发到对端."""
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    assert set(server.devices) == {alice.device_id, bob.device_id}

    offer = build_envelope(
        alice,
        "offer",
        {
            "session_id": SESSION_ID,
            "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
            "media_caps": {"codecs": ["h264"]},
            "access_proof": "cHJvb2Y=",
            "from_device_id": alice.device_id,
            "to_device_id": bob.device_id,
        },
        session_id=SESSION_ID,
    )
    responses = server.handle(offer)
    assert responses == [offer]
    assert server.routed[-1].to_device_id == bob.device_id
    assert server.routed[-1].msg_type == "offer"


def test_route_to_offline_peer_returns_error() -> None:
    """对端未注册返回 PEER_OFFLINE."""
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    offer = build_envelope(
        alice,
        "offer",
        {
            "session_id": SESSION_ID,
            "candidates": [],
            "media_caps": {},
            "access_proof": "cHJvb2Y=",
            "from_device_id": alice.device_id,
            "to_device_id": "B" * 20,
        },
        session_id=SESSION_ID,
    )
    responses = server.handle(offer)
    assert len(responses) == 1
    assert responses[0]["type"] == "error"
    assert responses[0]["payload"]["code"] == PEER_OFFLINE


def test_register_with_wrong_device_id_returns_auth_failed() -> None:
    """device_id 与公钥不一致返回 AUTH_FAILED."""
    server = InMemorySignalingServer()
    identity = DeviceIdentity.generate()
    register = build_envelope(
        identity,
        "register",
        {
            "device_id": "C" * 20,
            "public_key": identity.public_key_b64,
            "capabilities": [],
            "protocol_version": 1,
        },
    )
    responses = server.handle(register)
    assert responses[0]["payload"]["code"] == AUTH_FAILED


def test_relay_allocate_budget_and_profile() -> None:
    """中继预算由出口带宽计算, 档位与预算绑定."""
    server = InMemorySignalingServer(egress_mbps=3, overhead_ratio=0.08, max_sessions=1)
    identity = DeviceIdentity.generate()
    request = build_envelope(
        identity,
        "relay_allocate",
        {"session_id": SESSION_ID, "requested_kbps": 5000, "est_duration_s": 60},
        session_id=SESSION_ID,
    )
    responses = server.handle(request)
    assert len(responses) == 1
    allocation = responses[0]
    assert allocation["type"] == "relay_allocated"
    assert allocation["payload"]["budget_kbps"] == 2760
    assert allocation["payload"]["quality_profile"] == "L0"
