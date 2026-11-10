"""屏幕墙入站消息: wall_focus / wall_unfocus / wall_preview 与预览吊销推送."""

from __future__ import annotations

from datetime import UTC, datetime

from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope, verify_envelope
from rdc.signaling.server import InMemorySignalingServer


def _authenticate(server: InMemorySignalingServer, identity: DeviceIdentity) -> None:
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
    nonce = str(server.handle(register)[0]["payload"]["nonce"])
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    auth = build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature})
    assert server.handle(auth)[0]["type"] == "auth_ok"


def _server(
    deliveries: list[tuple[str, dict]] | None = None, accounts: dict[str, str] | None = None
) -> InMemorySignalingServer:
    sink = deliveries if deliveries is not None else []
    mapping = accounts or {}
    server = InMemorySignalingServer(
        deliver=lambda device_id, envelope: sink.append((device_id, envelope)) or True,
        account_of=lambda device_id: mapping.get(device_id, "acct-1"),
    )
    # 墙归属 acct-1: 未登记的墙一律按无权处理
    server.register_wall("wall-a", "acct-1")
    server.register_wall("wall-b", "acct-1")
    return server


def test_wall_focus_returns_token_bound_to_connection_identity():
    server = _server()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    focus = build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": 2, "account_id": "self-reported"})
    response = server.handle(focus, alice.device_id)[0]
    assert response["type"] == "wall_focus_granted"
    assert response["payload"]["wall_id"] == "wall-a"
    assert response["payload"]["cell"] == 2
    assert datetime.fromisoformat(response["payload"]["expires_at"]) > datetime.now(UTC)

    grant = server.resolve_relay_token(str(response["payload"]["focus_token"]))
    assert grant is not None
    assert grant.scope == "focus"
    assert grant.device_id == alice.device_id
    # 账号取自已认证连接的解析结果, 不采信 payload
    assert grant.account_id == "acct-1"


def test_wall_focus_is_global_and_same_wall_can_replace():
    server = _server()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)

    first = server.handle(build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": 0}), alice.device_id)[0]
    assert first["type"] == "wall_focus_granted"
    old_token = str(first["payload"]["focus_token"])

    exceeded = server.handle(build_envelope(bob, "wall_focus", {"wall_id": "wall-b", "cell": 0}), bob.device_id)[0]
    assert exceeded["type"] == "error"
    assert exceeded["payload"]["code"] == "WALL_BUDGET_EXCEEDED"
    assert server.resolve_relay_token(old_token) is not None

    replaced = server.handle(build_envelope(bob, "wall_focus", {"wall_id": "wall-a", "cell": 1}), bob.device_id)[0]
    assert replaced["type"] == "wall_focus_granted"
    assert server.resolve_relay_token(old_token) is None
    assert server.focus_stats()["cell"] == 1


def test_wall_unfocus_releases_and_is_idempotent():
    server = _server()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    server.handle(build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": 0}), alice.device_id)

    released = server.handle(build_envelope(alice, "wall_unfocus", {"wall_id": "wall-a"}), alice.device_id)[0]
    assert released["type"] == "wall_unfocused"
    assert released["payload"]["released"] is True

    again = server.handle(build_envelope(alice, "wall_unfocus", {"wall_id": "wall-a"}), alice.device_id)[0]
    assert again["type"] == "wall_unfocused"
    assert again["payload"]["released"] is False


def test_wall_preview_reissue_revokes_old_token_and_pushes_notice():
    deliveries: list[tuple[str, dict]] = []
    server = _server(deliveries)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)

    first = server.handle(build_envelope(alice, "wall_preview", {"wall_id": "wall-a", "cell": 3}), alice.device_id)[0]
    assert first["type"] == "wall_preview_granted"
    old_token = str(first["payload"]["preview_token"])
    grant = server.resolve_relay_token(old_token)
    assert grant is not None
    assert grant.scope == "view"
    assert grant.account_id == "acct-1"
    assert grant.device_id == alice.device_id

    second = server.handle(build_envelope(bob, "wall_preview", {"wall_id": "wall-a", "cell": 3}), bob.device_id)[0]
    assert second["type"] == "wall_preview_granted"
    new_token = str(second["payload"]["preview_token"])
    assert server.resolve_relay_token(old_token) is None
    assert server.resolve_relay_token(new_token) is not None

    pushes = [
        envelope
        for device_id, envelope in deliveries
        if device_id == alice.device_id and envelope["type"] == "wall_preview_revoked"
    ]
    assert len(pushes) == 1
    assert pushes[0]["payload"] == {
        "wall_id": "wall-a",
        "cell": 3,
        "reason": "重新签发同格预览令牌",
    }
    assert verify_envelope(pushes[0], server.identity.public_key_b64) is True


def test_wall_messages_require_authenticated_connection_and_valid_signature():
    server = _server()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    focus = build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": 0})

    anonymous = server.handle(focus)[0]
    assert anonymous["type"] == "error"
    assert anonymous["payload"]["code"] == "AUTH_FAILED"

    tampered = build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": 0})
    tampered["payload"]["cell"] = 9
    rejected = server.handle(tampered, alice.device_id)[0]
    assert rejected["type"] == "error"
    assert rejected["payload"]["code"] == "AUTH_FAILED"


def test_wall_target_validation_rejects_bad_cell():
    server = _server()
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    response = server.handle(build_envelope(alice, "wall_focus", {"wall_id": "wall-a", "cell": "2"}), alice.device_id)[
        0
    ]
    assert response["type"] == "error"
    assert response["payload"]["code"] == "INVALID_MESSAGE"


def test_wall_preview_and_focus_reject_other_account_wall():
    """墙归属按账号校验: 其它账号既不能预览, 也不能聚焦或抢占他人墙."""
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server(accounts={alice.device_id: "acct-a", bob.device_id: "acct-b"})
    server.register_wall("other-users-wall", "acct-a")
    _authenticate(server, alice)
    _authenticate(server, bob)

    owned = server.handle(
        build_envelope(alice, "wall_preview", {"wall_id": "other-users-wall", "cell": 0}),
        alice.device_id,
    )[0]
    assert owned["type"] == "wall_preview_granted"

    preview = server.handle(
        build_envelope(bob, "wall_preview", {"wall_id": "other-users-wall", "cell": 0}),
        bob.device_id,
    )[0]
    assert preview["type"] == "error"
    assert preview["payload"]["code"] == "SCOPE_INSUFFICIENT"

    hijack = server.handle(
        build_envelope(bob, "wall_focus", {"wall_id": "other-users-wall", "cell": 1}),
        bob.device_id,
    )[0]
    assert hijack["type"] == "error"
    assert hijack["payload"]["code"] == "SCOPE_INSUFFICIENT"


def test_wall_unfocus_only_releases_own_focus_token():
    """聚焦令牌只能由持有人释放, 其它账号 unfocus 不得生效."""
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server(accounts={alice.device_id: "acct-a", bob.device_id: "acct-b"})
    server.register_wall("other-users-wall", "acct-a")
    _authenticate(server, alice)
    _authenticate(server, bob)

    focus = server.handle(
        build_envelope(alice, "wall_focus", {"wall_id": "other-users-wall", "cell": 0}),
        alice.device_id,
    )[0]
    assert focus["type"] == "wall_focus_granted"
    token = str(focus["payload"]["focus_token"])

    denied = server.handle(
        build_envelope(bob, "wall_unfocus", {"wall_id": "other-users-wall"}),
        bob.device_id,
    )[0]
    assert denied["type"] == "error"
    assert denied["payload"]["code"] == "SCOPE_INSUFFICIENT"
    assert server.resolve_relay_token(token) is not None

    released = server.handle(
        build_envelope(alice, "wall_unfocus", {"wall_id": "other-users-wall"}),
        alice.device_id,
    )[0]
    assert released["payload"]["released"] is True
    assert server.resolve_relay_token(token) is None


def test_wall_without_owner_is_denied_by_default():
    """未建立归属的墙不签发任何令牌, 避免注册即可操作任意墙."""
    alice = DeviceIdentity.generate()
    server = _server(accounts={alice.device_id: "acct-a"})
    _authenticate(server, alice)
    response = server.handle(
        build_envelope(alice, "wall_preview", {"wall_id": "unregistered-wall", "cell": 0}),
        alice.device_id,
    )[0]
    assert response["type"] == "error"
    assert response["payload"]["code"] == "SCOPE_INSUFFICIENT"
