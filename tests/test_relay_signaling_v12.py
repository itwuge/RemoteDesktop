"""信令服务 v1.2 语义: presence 在线表、登出踢下线、预览令牌与聚焦名额."""

from __future__ import annotations

import time
from dataclasses import replace

from rdc.accounts.service import AccountService
from rdc.core.ids import iso_after
from rdc.crypto.identity import DeviceIdentity
from rdc.relay.dataplane import RelayDataPlane, RelayGrant
from rdc.signaling.envelope import build_envelope, verify_envelope
from rdc.signaling.server import InMemorySignalingServer

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def _register(server: InMemorySignalingServer, identity: DeviceIdentity) -> str:
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
    assert responses[0]["type"] == "challenge"
    return str(responses[0]["payload"]["nonce"])


def _authenticate(server: InMemorySignalingServer, identity: DeviceIdentity) -> None:
    nonce = _register(server, identity)
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    auth = build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature})
    responses = server.handle(auth)
    assert len(responses) == 1
    assert responses[0]["type"] == "auth_ok"
    server.mark_online(identity.device_id)


def _offer(identity: DeviceIdentity, peer: DeviceIdentity, session_id: str) -> dict:
    return build_envelope(
        identity,
        "offer",
        {
            "session_id": session_id,
            "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
            "media_caps": {"codecs": ["h264"]},
            "access_proof": "cHJvb2Y=",
            "from_device_id": identity.device_id,
            "to_device_id": peer.device_id,
        },
        session_id=session_id,
    )


def _allocate(identity: DeviceIdentity, session_id: str, requested: int = 2760) -> dict:
    return build_envelope(
        identity,
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": requested, "est_duration_s": 3600},
        session_id=session_id,
    )


# --------------------------------------------------------------- presence 在线表


def test_presence_unknown_online_then_offline_after_90_seconds():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    # 未上报 presence 的旧客户端记为 unknown
    assert server.presence_for(alice.device_id)["presence_state"] == "unknown"

    presence = build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"})
    assert server.handle(presence, alice.device_id) == []

    info = server.presence_for(alice.device_id)
    assert info["presence_state"] == "online"
    assert info["online"] is True
    assert info["last_seen_at"]

    later = time.time() + 91
    expired = server.presence_for(alice.device_id, now=later)
    assert expired["presence_state"] == "offline"
    assert expired["online"] is False
    # 离线不改写最后上报时间, list_devices 仍能看到 last_seen_at
    assert expired["last_seen_at"] == info["last_seen_at"]


def test_presence_requires_authenticated_matching_device():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)

    unauthenticated = build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"})
    responses = server.handle(unauthenticated)
    assert responses[0]["type"] == "error"
    assert responses[0]["payload"]["code"] == "AUTH_FAILED"

    spoofed = build_envelope(alice, "presence", {"device_id": bob.device_id, "state": "online"})
    responses = server.handle(spoofed, alice.device_id)
    assert responses[0]["type"] == "error"
    assert server.presence_for(bob.device_id)["presence_state"] == "unknown"


def test_presence_snapshot_matches_single_device_query():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    server.handle(build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"}), alice.device_id)

    snapshot = server.presence_snapshot([alice.device_id, bob.device_id])
    assert snapshot[alice.device_id] == server.presence_for(alice.device_id)
    assert snapshot[bob.device_id]["presence_state"] == "unknown"


def test_disconnect_does_not_revoke_token_or_end_session():
    """契约 v1.2: 离线只影响在线状态."""
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    server.handle(_offer(alice, bob, SESSION_A), alice.device_id)
    server.handle(_allocate(alice, SESSION_A), alice.device_id)
    assert len(server.relay_grants) == 1

    assert server.mark_offline(alice.device_id) == []
    assert len(server.relay_grants) == 1
    assert server.session_stats()["active_sessions"] == 1
    assert server.presence_for(alice.device_id)["presence_state"] == "unknown"


# --------------------------------------------------------------- 路由与会话


def test_route_delivers_to_peer_connection_not_sender():
    delivered: list[tuple[str, dict]] = []
    server = InMemorySignalingServer(
        deliver=lambda device_id, envelope: delivered.append((device_id, envelope)) or True
    )
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)

    offer = _offer(alice, bob, SESSION_A)
    assert server.handle(offer, alice.device_id) == []
    assert delivered == [(bob.device_id, offer)]
    assert server.session_stats()["active_sessions"] == 1


def test_route_to_unreachable_peer_returns_peer_offline():
    server = InMemorySignalingServer(deliver=lambda device_id, envelope: False)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    responses = server.handle(_offer(alice, bob, SESSION_A), alice.device_id)
    assert responses[0]["payload"]["code"] == "PEER_OFFLINE"


def test_relay_allocation_is_capped_and_revoked_on_session_end():
    server = InMemorySignalingServer(max_sessions=1)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)

    server.handle(_offer(alice, bob, SESSION_A), alice.device_id)
    first = server.handle(_allocate(alice, SESSION_A), alice.device_id)
    assert first[0]["type"] == "relay_allocated"
    token = str(first[0]["payload"]["relay_token"])
    assert first[0]["payload"]["budget_kbps"] == 2760

    full = server.handle(_allocate(alice, SESSION_B), alice.device_id)
    assert full[0]["payload"]["code"] == "RELAY_FULL"

    end = build_envelope(
        alice,
        "session_end",
        {"session_id": SESSION_A, "reason": "用户结束会话"},
        session_id=SESSION_A,
    )
    server.handle(end, alice.device_id)
    assert server.resolve_relay_token(token) is None

    retry = server.handle(_allocate(alice, SESSION_B), alice.device_id)
    assert retry[0]["type"] == "relay_allocated"


def test_reallocation_replaces_previous_token():
    server = InMemorySignalingServer(max_sessions=1)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    server.handle(_offer(alice, bob, SESSION_A), alice.device_id)

    first = str(server.handle(_allocate(alice, SESSION_A), alice.device_id)[0]["payload"]["relay_token"])
    second = str(server.handle(_allocate(alice, SESSION_A), alice.device_id)[0]["payload"]["relay_token"])
    assert first != second
    assert server.resolve_relay_token(first) is None
    assert server.resolve_relay_token(second) is not None


def test_expired_challenge_is_rejected():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    nonce = _register(server, alice)
    server.challenges[alice.device_id] = (nonce, iso_after(-1))
    signature = alice.sign({"device_id": alice.device_id, "nonce": nonce})
    auth = build_envelope(alice, "auth", {"device_id": alice.device_id, "signature": signature})
    responses = server.handle(auth)
    assert responses[0]["payload"]["code"] == "AUTH_FAILED"
    # 过期挑战在入口即被清理, 不会留下可重放的认证凭据
    assert alice.device_id not in server.challenges


# --------------------------------------------------------------- 登出与吊销


def test_kick_device_ends_sessions_and_revokes_tokens():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    server.handle(build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"}), alice.device_id)
    server.handle(_offer(alice, bob, SESSION_A), alice.device_id)
    token = str(server.handle(_allocate(alice, SESSION_A), alice.device_id)[0]["payload"]["relay_token"])

    counts = server.kick_device(alice.device_id)
    assert counts == {"kicked_sessions": 1, "revoked_tokens": 1}
    assert server.resolve_relay_token(token) is None
    assert server.presence_for(alice.device_id)["presence_state"] == "unknown"
    assert server.session_stats()["connected_devices"] == 1
    # 对端令牌与会话状态不因另一台设备登出而受影响
    assert server.session_stats()["active_sessions"] == 0


# --------------------------------------------------------------- 预览令牌


def test_preview_grant_is_view_only_and_reissue_revokes_old():
    server = InMemorySignalingServer()
    first = server.issue_preview_grant("wall-1", 3, "account-1", "device-1")
    grant = server.resolve_relay_token(first)
    assert grant is not None
    assert grant.scope == "view"
    assert grant.wall_id == "wall-1"
    assert grant.cell == 3
    assert grant.budget_kbps <= 300

    second = server.issue_preview_grant("wall-1", 3, "account-1", "device-1")
    assert server.resolve_relay_token(first) is None
    assert server.resolve_relay_token(second) is not None
    assert server.revoke_preview_grant(second) is True
    assert server.revoke_preview_grant(second) is False


def test_preview_grant_expires_and_purges():
    server = InMemorySignalingServer()
    token = server.issue_preview_grant("wall-1", 0, "account-1", "device-1", ttl_s=1)
    server.relay_grants[token] = replace(server.relay_grants[token], expires_at=time.time() - 1)
    assert server.resolve_relay_token(token) is None
    assert server.revoke_wall_grants("wall-1") == 0


def test_dataplane_rejects_preview_token_for_publisher():
    grant = RelayGrant(session_id=SESSION_A, budget_kbps=300, expires_at=time.time() + 60, scope="view")
    plane = RelayDataPlane(lambda _token: grant, max_sessions=2)
    hello = {
        "plane": "data",
        "v": 1,
        "type": "relay_hello",
        "session_id": SESSION_A,
        "relay_token": "token-value",
        "role": "publisher",
    }
    assert plane._validate(hello) == "预览令牌只允许订阅方向"
    hello["role"] = "subscriber"
    assert plane._validate(hello) is grant


# --------------------------------------------------------------- 聚焦名额


def test_focus_quota_allows_only_one_wall():
    server = InMemorySignalingServer()
    token, error = server.issue_focus_grant("wall-a", 0, "account-1", "device-1")
    assert error == ""
    assert server.focus_stats()["wall_id"] == "wall-a"

    other, error = server.issue_focus_grant("wall-b", 0, "account-1", "device-1")
    assert other == ""
    assert error == "WALL_BUDGET_EXCEEDED"
    assert server.stop_focus("wall-b") is False
    assert server.stop_focus("wall-a") is True
    assert server.resolve_relay_token(token) is None

    next_token, error = server.issue_focus_grant("wall-b", 0, "account-1", "device-1")
    assert error == ""
    assert server.focus_stats() == {
        "focused": True,
        "wall_id": "wall-b",
        "cell": 0,
        "account_id": "account-1",
    }
    assert server.resolve_relay_token(next_token) is not None


def test_focus_quota_is_released_on_expiry():
    server = InMemorySignalingServer()
    token, error = server.issue_focus_grant("wall-a", 0, "account-1", "device-1", ttl_s=1)
    assert error == ""
    server.relay_grants[token] = replace(server.relay_grants[token], expires_at=time.time() - 1)
    assert server.focus_stats()["focused"] is False
    assert server.resolve_relay_token(token) is None


def test_auth_ok_envelope_is_signed_by_server():
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    nonce = _register(server, alice)
    auth = build_envelope(
        alice,
        "auth",
        {"device_id": alice.device_id, "signature": alice.sign({"device_id": alice.device_id, "nonce": nonce})},
    )
    response = server.handle(auth)[0]
    assert verify_envelope(response, server.identity.public_key_b64) is True


def test_presence_sink_feeds_accounts_list_devices():
    """组装后在线状态从信令在线表落到账号服务, list_devices 不再停在 unknown."""
    account_service = AccountService()
    alice = DeviceIdentity.generate()
    account_service.register_device(alice.device_id)
    assert account_service.register_account("alice@example.com", "password123").ok
    assert account_service.login_account("alice@example.com", "password123").ok
    assert account_service.bind_device(alice.device_id, "123456").ok
    server = InMemorySignalingServer(presence_sink=account_service.record_presence)

    _authenticate(server, alice)
    server.handle(
        build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"}),
        alice.device_id,
    )
    item = next(
        entry for entry in account_service.list_devices().data["items"] if entry["device_id"] == alice.device_id
    )
    assert item["presence_state"] == "online"
    assert item["online"] is True

    # 契约 v1.2: 90 秒未心跳才判离线; 用过期时间戳触发一次清理, 不等待真实时钟
    server.presence[alice.device_id] = replace(server.presence[alice.device_id], last_seen_at=time.time() - 91)
    server.handle(
        build_envelope(alice, "heartbeat", {"session_id": SESSION_A, "seq": 1}, session_id=SESSION_A),
        alice.device_id,
    )
    item = next(
        entry for entry in account_service.list_devices().data["items"] if entry["device_id"] == alice.device_id
    )
    assert item["presence_state"] == "offline"
    assert item["online"] is False


def test_two_devices_same_session_keep_their_own_relay_tokens():
    """令牌绑「设备 + 会话」: 第二台设备申请预算不得吊销第一台的令牌."""
    server = InMemorySignalingServer()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    server.handle(_offer(alice, bob, SESSION_A), alice.device_id)

    alice_response = server.handle(_allocate(alice, SESSION_A, 1380), alice.device_id)[0]
    bob_response = server.handle(_allocate(bob, SESSION_A, 1380), bob.device_id)[0]
    assert alice_response["type"] == "relay_allocated"
    assert bob_response["type"] == "relay_allocated"
    alice_token = str(alice_response["payload"]["relay_token"])
    bob_token = str(bob_response["payload"]["relay_token"])
    assert alice_token != bob_token

    alice_grant = server.resolve_relay_token(alice_token)
    bob_grant = server.resolve_relay_token(bob_token)
    assert alice_grant is not None and alice_grant.device_id == alice.device_id
    assert bob_grant is not None and bob_grant.device_id == bob.device_id

    # 同设备重取只吊销自己的旧令牌, 不能影响同会话对端
    again = server.handle(_allocate(alice, SESSION_A, 1380), alice.device_id)[0]
    new_alice_token = str(again["payload"]["relay_token"])
    assert server.resolve_relay_token(alice_token) is None
    assert server.resolve_relay_token(new_alice_token) is not None
    assert server.resolve_relay_token(bob_token) is not None
