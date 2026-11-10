"""屏幕墙归属登记: wall_bind 的鉴权、幂等、冲突与落盘."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rdc.accounts.db import SCHEMA_VERSION
from rdc.accounts.store import AccountStore
from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope
from rdc.signaling.server import InMemorySignalingServer

WALL = "wall-owner-a"


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
    accounts: dict[str, str] | None = None,
    wall_binder=None,
) -> InMemorySignalingServer:
    deliveries: list[tuple[str, dict]] = []
    mapping = accounts or {}
    return InMemorySignalingServer(
        deliver=lambda device_id, envelope: deliveries.append((device_id, envelope)) or True,
        account_of=lambda device_id: mapping.get(device_id, ""),
        wall_binder=wall_binder,
    )


def _establish(server: InMemorySignalingServer, host: DeviceIdentity, controller: DeviceIdentity, session: str) -> None:
    """建立会话: controller 发 offer, host 回 answer, host 由此被记为被控端."""
    offer = build_envelope(
        controller,
        "offer",
        {
            "session_id": session,
            "candidates": [],
            "media_caps": {},
            "access_proof": "proof",
            "from_device_id": controller.device_id,
            "to_device_id": host.device_id,
        },
        session_id=session,
    )
    server.handle(offer, controller.device_id)
    answer = build_envelope(
        host,
        "answer",
        {
            "session_id": session,
            "chosen_candidate": {},
            "media_caps": {},
            "from_device_id": host.device_id,
            "to_device_id": controller.device_id,
        },
        session_id=session,
    )
    responses = server.handle(answer, host.device_id)
    assert not any(item.get("type") == "error" for item in responses)


def _bind(server: InMemorySignalingServer, identity: DeviceIdentity, session: str, wall_id: str = WALL) -> dict:
    return server.handle(
        build_envelope(
            identity,
            "wall_bind",
            {"wall_id": wall_id, "session_id": session},
            session_id=session,
        ),
        identity.device_id,
    )[0]


def test_host_bind_registers_owner_from_authenticated_device():
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"})
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")

    response = _bind(server, alice, "s1")
    assert response["type"] == "wall_bound"
    assert response["payload"]["wall_id"] == WALL
    assert response["payload"]["account_id"] == "acct-a"
    assert response["payload"]["bound_at"]
    assert server.wall_owner(WALL) == "acct-a"


def test_repeat_bind_is_idempotent_and_keeps_bound_at():
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"})
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")

    first = _bind(server, alice, "s1")
    second = _bind(server, alice, "s1")
    assert second["type"] == "wall_bound"
    assert second["payload"]["bound_at"] == first["payload"]["bound_at"]


def test_wall_bind_default_deny_paths():
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"})
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")

    # 未认证连接
    anonymous = server.handle(
        build_envelope(alice, "wall_bind", {"wall_id": WALL, "session_id": "s1"}, session_id="s1")
    )[0]
    assert anonymous["type"] == "error"
    assert anonymous["payload"]["code"] == "AUTH_FAILED"

    # 会话参与者但不是 host
    denied = _bind(server, bob, "s1")
    assert denied["type"] == "error"
    assert denied["payload"]["code"] == "AUTH_FAILED"

    # 不在会话中的设备
    carol = DeviceIdentity.generate()
    server.handle(
        build_envelope(
            carol,
            "register",
            {
                "device_id": carol.device_id,
                "public_key": carol.public_key_b64,
                "capabilities": ["desktop"],
                "protocol_version": 1,
            },
        )
    )
    outsider = server.handle(
        build_envelope(carol, "wall_bind", {"wall_id": WALL, "session_id": "s1"}, session_id="s1"),
        carol.device_id,
    )[0]
    assert outsider["payload"]["code"] == "AUTH_FAILED"

    # 设备未绑定账号 (account_of 返回空)
    unbound = _server({})
    _authenticate(unbound, alice)
    _authenticate(unbound, bob)
    _establish(unbound, alice, bob, "s2")
    no_account = _bind(unbound, alice, "s2")
    assert no_account["type"] == "error"
    assert no_account["payload"]["code"] == "AUTH_FAILED"

    # 签名被篡改
    tampered = build_envelope(alice, "wall_bind", {"wall_id": WALL, "session_id": "s1"}, session_id="s1")
    tampered["payload"]["wall_id"] = "other-wall"
    rejected = server.handle(tampered, alice.device_id)[0]
    assert rejected["payload"]["code"] == "AUTH_FAILED"
    assert server.wall_owner(WALL) is None


def test_conflict_between_accounts_is_rejected_without_overwrite():
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"})
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")
    first = _bind(server, alice, "s1")
    assert first["type"] == "wall_bound"

    # 第二个会话里 bob 成为 host, 对同一堵墙登记必须被明确拒绝
    _establish(server, bob, alice, "s2")
    conflict = _bind(server, bob, "s2")
    assert conflict["type"] == "error"
    assert conflict["payload"]["code"] == "WALL_OWNERSHIP_CONFLICT"
    assert server.wall_owner(WALL) == "acct-a"

    # 原账号仍可继续使用这堵墙
    preview = server.handle(
        build_envelope(alice, "wall_preview", {"wall_id": WALL, "cell": 0}, session_id="s1"),
        alice.device_id,
    )[0]
    assert preview["type"] == "wall_preview_granted"


def test_preview_positive_and_negative_after_bind():
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"})
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")
    assert _bind(server, alice, "s1")["type"] == "wall_bound"

    granted = server.handle(
        build_envelope(alice, "wall_preview", {"wall_id": WALL, "cell": 0}, session_id="s1"),
        alice.device_id,
    )[0]
    assert granted["type"] == "wall_preview_granted"

    denied = server.handle(
        build_envelope(bob, "wall_preview", {"wall_id": WALL, "cell": 0}, session_id="s1"),
        bob.device_id,
    )[0]
    assert denied["type"] == "error"
    assert denied["payload"]["code"] == "SCOPE_INSUFFICIENT"

    again = server.handle(
        build_envelope(alice, "wall_preview", {"wall_id": WALL, "cell": 0}, session_id="s1"),
        alice.device_id,
    )[0]
    assert again["type"] == "wall_preview_granted"


def test_bind_persists_in_account_store_and_migration_is_idempotent(tmp_path: Path):
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    server = _server({alice.device_id: "acct-a", bob.device_id: "acct-b"}, wall_binder=store.bind_wall)
    _authenticate(server, alice)
    _authenticate(server, bob)
    _establish(server, alice, bob, "s1")
    assert _bind(server, alice, "s1")["type"] == "wall_bound"
    assert store.wall_owner(WALL) == "acct-a"
    # 成功登记与幂等重放都写审计, 便于事后查询
    assert [event.event for event in store.audit].count("wall.bind") == 1
    store.close()

    reopened = AccountStore(path)
    try:
        assert reopened.wall_owner(WALL) == "acct-a"
    finally:
        reopened.close()

    # 模拟旧库(结构已含 walls, 版本被标为 2)重复升级: 不重建、不丢墙归属
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA user_version=2")
        connection.commit()
    finally:
        connection.close()
    upgraded = AccountStore(path)
    try:
        assert upgraded.wall_owner(WALL) == "acct-a"
        assert upgraded.database is not None
    finally:
        upgraded.close()
    connection = sqlite3.connect(str(path))
    try:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
    finally:
        connection.close()
