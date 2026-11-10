"""中继吊销回归测试: 会话结束断开数据面, 账号令牌吊销对在线连接立即生效.

两个缺陷均由独立验证客户端实测确认（证据目录 D:/RDC-Build/evidence/security/20261110-193306）:
1. 会话结束后已建立的数据面连接不断开, 仍在双向转发;
2. 账号库置 revoked_at 后, 同一条在线连接仍能拿到 relay_allocated.

因此这里的用例按「先红后绿」编写: 修复前必须失败, 修复后必须通过.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from rdc.accounts.db import AccountDatabase
from rdc.core.errors import TOKEN_REVOKED
from rdc.crypto.identity import DeviceIdentity
from rdc.relay.revocation import AccountRevocationProbe
from rdc.relay.server import RelayConfig, RelayServer, recv_frame, send_frame
from rdc.signaling.envelope import build_envelope

# --------------------------------------------------------------- 基础工具


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _config(port: int, health_port: int, max_sessions: int = 2) -> RelayConfig:
    return RelayConfig(
        bind="127.0.0.1",
        port=port,
        health_bind="127.0.0.1",
        health_port=health_port,
        relay_endpoint=f"relay://127.0.0.1:{port}",
        egress_mbps=3,
        overhead_ratio=0.08,
        max_sessions=max_sessions,
        degrade_profile="L0",
    )


def _wait_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"端口 {port} 未就绪")


def _start_relay(probe: AccountRevocationProbe | None = None) -> tuple[RelayServer, int]:
    data_port, health_port = _free_port(), _free_port()
    server = RelayServer(_config(data_port, health_port), revocation_probe=probe)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _wait_port(data_port)
    return server, data_port


def _open(port: int) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    connection.settimeout(5.0)
    return connection


def _recv_json(connection: socket.socket) -> dict:
    return json.loads(recv_frame(connection).decode("utf-8"))


def _register(connection: socket.socket, identity: DeviceIdentity) -> dict:
    send_frame(
        connection,
        build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["desktop"],
                "protocol_version": 1,
            },
        ),
    )
    return _recv_json(connection)


def _auth_request(connection: socket.socket, identity: DeviceIdentity, challenge: dict) -> None:
    nonce = str(challenge["payload"]["nonce"])
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    send_frame(connection, build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature}))


def _auth(connection: socket.socket, identity: DeviceIdentity) -> dict:
    """完整认证并返回 auth_ok; 认证被拒时原样返回服务端响应, 由用例断言."""
    challenge = _register(connection, identity)
    assert challenge["type"] == "challenge"
    _auth_request(connection, identity, challenge)
    return _recv_json(connection)


def _negotiate(alice: socket.socket, bob: socket.socket, alice_id: DeviceIdentity, bob_id: DeviceIdentity) -> str:
    session_id = str(uuid.uuid4())
    send_frame(
        alice,
        build_envelope(
            alice_id,
            "offer",
            {
                "session_id": session_id,
                "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
                "media_caps": {"codecs": ["h264"]},
                "access_proof": "cHJvb2Y=",
                "from_device_id": alice_id.device_id,
                "to_device_id": bob_id.device_id,
            },
            session_id=session_id,
        ),
    )
    assert _recv_json(bob)["type"] == "offer"
    send_frame(
        bob,
        build_envelope(
            bob_id,
            "answer",
            {
                "session_id": session_id,
                "chosen_candidate": {"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1},
                "media_caps": {"codecs": ["h264"]},
                "from_device_id": bob_id.device_id,
                "to_device_id": alice_id.device_id,
            },
            session_id=session_id,
        ),
    )
    assert _recv_json(alice)["type"] == "answer"
    return session_id


def _allocate(connection: socket.socket, identity: DeviceIdentity, session_id: str) -> dict:
    send_frame(
        connection,
        build_envelope(
            identity,
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 1200, "est_duration_s": 600},
            session_id=session_id,
        ),
    )
    return _recv_json(connection)


def _data_connect(port: int, token: str, session_id: str, role: str) -> socket.socket:
    connection = _open(port)
    send_frame(
        connection,
        {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": token,
            "session_id": session_id,
            "role": role,
        },
    )
    ready = _recv_json(connection)
    assert ready["type"] == "relay_ready", ready
    connection.settimeout(10.0)
    return connection


def _send_raw(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(len(payload).to_bytes(4, "big") + payload)


def _wait_eof(connection: socket.socket, timeout: float = 3.0) -> bool:
    """等待连接被服务端断开; 期间收到任何数据都判定为仍在转发, 返回 False."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        connection.settimeout(max(0.05, min(deadline - time.time(), 0.5)))
        try:
            chunk = connection.recv(65536)
        except TimeoutError:
            continue
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        return not chunk
    return False


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------- 账号库夹具


def _new_accounts_db(path: Path) -> None:
    database = AccountDatabase(path)
    database.close()


def _seed_device(path: Path, device_id: str) -> str:
    """写入一台已绑定账号的设备与一条未吊销的设备令牌, 返回账号 ID."""
    account_id = f"acct-{uuid.uuid4().hex[:12]}"
    with contextlib.closing(sqlite3.connect(str(path))) as connection:
        connection.execute(
            "INSERT INTO devices (device_id, display_name, owner_account_id, bound_at, require_confirm)"
            " VALUES (?, ?, ?, ?, 1)",
            (device_id, "回归测试设备", account_id, "2026-11-10T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO tokens (token_hash, kind, account_id, device_id, issued_at, expires_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (f"hash-{uuid.uuid4().hex}", "device", account_id, device_id, time.time(), time.time() + 86400),
        )
        connection.commit()
    return account_id


def _revoke_account(path: Path, account_id: str) -> None:
    """模拟账号命令层/运维脚本吊销: 直接把令牌置 revoked_at 并提交."""
    with contextlib.closing(sqlite3.connect(str(path))) as connection:
        connection.execute(
            "UPDATE tokens SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
            (time.time(), account_id),
        )
        connection.commit()


def _issue_new_device_token(path: Path, device_id: str, account_id: str) -> None:
    """模拟吊销后重新登录: 签发一条更新的未吊销设备令牌."""
    with contextlib.closing(sqlite3.connect(str(path))) as connection:
        connection.execute(
            "INSERT INTO tokens (token_hash, kind, account_id, device_id, issued_at, expires_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                f"hash-{uuid.uuid4().hex}",
                "device",
                account_id,
                device_id,
                time.time() + 1.0,
                time.time() + 86400,
            ),
        )
        connection.commit()


# --------------------------------------------------------------- 吊销探测


def test_revocation_probe_sees_external_commit_without_ttl_wait(tmp_path: Path) -> None:
    """吊销写入必须立刻可见: TTL 设成 60 秒也不依赖超时轮询."""
    path = tmp_path / "accounts.db"
    _new_accounts_db(path)
    device_id = f"dev-{uuid.uuid4().hex}"
    account_id = _seed_device(path, device_id)
    probe = AccountRevocationProbe(path, ttl_s=60.0)
    try:
        assert probe.is_revoked(device_id) is False
        _revoke_account(path, account_id)
        assert probe.is_revoked(device_id) is True
        # 重新登录签发新令牌后自动恢复
        _issue_new_device_token(path, device_id, account_id)
        assert probe.is_revoked(device_id) is False
        assert probe.snapshot()["available"] is True
    finally:
        probe.close()


# --------------------------------------------------------------- 缺陷 1


def test_session_end_closes_established_dataplane_and_stops_forwarding() -> None:
    """会话结束后数据面连接必须立即断开, 且不再转发任何帧."""
    server, port = _start_relay()
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    alice_sock = bob_sock = publisher = subscriber = None
    try:
        alice_sock, bob_sock = _open(port), _open(port)
        assert _auth(alice_sock, alice)["type"] == "auth_ok"
        assert _auth(bob_sock, bob)["type"] == "auth_ok"

        session_id = _negotiate(alice_sock, bob_sock, alice, bob)
        allocated = _allocate(alice_sock, alice, session_id)
        assert allocated["type"] == "relay_allocated", allocated
        token = str(allocated["payload"]["relay_token"])

        holder: dict[str, socket.socket] = {}
        errors: list[BaseException] = []

        def run_subscriber() -> None:
            try:
                holder["sock"] = _data_connect(port, token, session_id, "subscriber")
            except BaseException as exc:  # 线程内异常必须带回主线程
                errors.append(exc)

        thread = threading.Thread(target=run_subscriber, daemon=True)
        thread.start()
        publisher = _data_connect(port, token, session_id, "publisher")
        thread.join(timeout=5)
        assert not errors and "sock" in holder
        subscriber = holder["sock"]

        # 会话结束前: 数据面确实在双向转发
        _send_raw(publisher, b"probe-before-end")
        assert recv_frame(subscriber) == b"probe-before-end"
        # 转发计数由发送线程在 sendall 之后累加, 这里等它落账
        assert _wait_until(lambda: server.dataplane.snapshot()["frames_forwarded"] == 1)

        send_frame(
            alice_sock,
            build_envelope(
                alice,
                "session_end",
                {
                    "session_id": session_id,
                    "from_device_id": alice.device_id,
                    "to_device_id": bob.device_id,
                    "reason": "user_end",
                },
                session_id=session_id,
            ),
        )
        assert _recv_json(bob_sock)["type"] == "session_end"

        # 会话结束后: 两端都必须被立即断开, 且不再有任何帧被转发
        assert _wait_eof(subscriber), "会话结束后订阅端数据面连接未断开"
        assert _wait_eof(publisher), "会话结束后发布端数据面连接未断开"
        with contextlib.suppress(OSError):
            _send_raw(publisher, b"probe-after-end")

        snapshot = server.dataplane.snapshot()
        assert snapshot["sessions"] == 0
        assert snapshot["frames_forwarded"] == 0
        assert snapshot["revoked_tokens"] >= 1
        assert snapshot["revoked_sessions"] >= 1
        assert server.signaling.resolve_relay_token(token) is None
    finally:
        for sock in (alice_sock, bob_sock, publisher, subscriber):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        server.stop()


# --------------------------------------------------------------- 缺陷 2


def test_account_token_revocation_rejects_online_allocation(tmp_path: Path) -> None:
    """账号令牌吊销后: 在线连接立即失效, 新的中继申请被拒并回明确错误码."""
    path = tmp_path / "accounts.db"
    _new_accounts_db(path)
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    account_id = _seed_device(path, alice.device_id)
    probe = AccountRevocationProbe(path)
    server, port = _start_relay(probe)
    alice_sock = bob_sock = late = None
    try:
        alice_sock, bob_sock = _open(port), _open(port)
        assert _auth(alice_sock, alice)["type"] == "auth_ok"
        assert _auth(bob_sock, bob)["type"] == "auth_ok"

        session_id = _negotiate(alice_sock, bob_sock, alice, bob)
        allocated = _allocate(alice_sock, alice, session_id)
        assert allocated["type"] == "relay_allocated", allocated
        token = str(allocated["payload"]["relay_token"])
        assert server.signaling.resolve_relay_token(token) is not None

        # 吊销写库后不等待任何缓存过期, 同一条在线连接的下一次申请必须被拒
        _revoke_account(path, account_id)
        rejected = _allocate(alice_sock, alice, session_id)
        assert rejected["type"] == "error", rejected
        assert rejected["payload"]["code"] == TOKEN_REVOKED
        assert _wait_eof(alice_sock), "吊销后在线连接未被断开"

        # 已发出的授予必须失效: 旧令牌不能再接入数据面
        assert server.signaling.resolve_relay_token(token) is None
        assert all(grant.device_id != alice.device_id for grant in server.signaling.relay_grants.values())

        # 新连接也不能再完成认证
        late = _open(port)
        challenge = _register(late, alice)
        assert challenge["type"] == "challenge"
        _auth_request(late, alice, challenge)
        denied = _recv_json(late)
        assert denied["type"] == "error", denied
        assert denied["payload"]["code"] == TOKEN_REVOKED
        assert _wait_eof(late), "吊销设备的重新认证连接未被断开"
    finally:
        for sock in (alice_sock, bob_sock, late):
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        probe.close()
        server.stop()
