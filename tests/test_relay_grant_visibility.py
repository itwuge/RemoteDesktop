"""跨进程中继授予可见性: 信令进程签发的令牌必须能被中继进程解析.

两个场景都起独立进程共用一份临时账号库, 不复用单进程内存路径.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from rdc.accounts.db import AccountDatabase
from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope
from rdc.signaling.grants import RelayGrantStore

ROOT = Path(__file__).resolve().parents[1]
MAX_FRAME_BYTES = 1 << 20


# ------------------------------------------------------------------ 网络与进程工具


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"端口 {port} 未就绪")


def _connect(port: int) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=10.0)
    connection.settimeout(10.0)
    return connection


def _send_frame(connection: socket.socket, body: bytes) -> None:
    connection.sendall(struct.pack(">I", len(body)) + body)


def _send(connection: socket.socket, payload: dict) -> None:
    _send_frame(connection, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _recv_frame(connection: socket.socket, timeout: float = 10.0) -> bytes | None:
    connection.settimeout(timeout)
    header = _recv_exactly(connection, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ValueError(f"帧长度非法: {length}")
    return _recv_exactly(connection, length)


def _recv_exactly(connection: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = connection.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def _recv(connection: socket.socket, timeout: float = 10.0) -> dict | None:
    body = _recv_frame(connection, timeout)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _wait_eof(connection: socket.socket, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        connection.settimeout(max(0.05, min(deadline - time.time(), 0.5)))
        try:
            chunk = connection.recv(65536)
        except TimeoutError:
            continue
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        if not chunk:
            return True
    return False


def _config(path: Path, *, port: int, health: int, dataplane: bool, grant_ttl: int = 90) -> Path:
    path.write_text(
        "[server]\n"
        "bind = 127.0.0.1\n"
        f"port = {port}\n"
        "health_bind = 127.0.0.1\n"
        f"health_port = {health}\n"
        "max_connections = 32\n"
        "max_connections_per_ip = 16\n"
        "connect_rate_per_min = 0\n"
        "\n[relay]\n"
        f"endpoint = relay://127.0.0.1:{port}\n"
        "egress_mbps = 3\n"
        "overhead_ratio = 0.08\n"
        "max_sessions = 2\n"
        "degrade_profile = L0\n"
        f"dataplane_enabled = {'true' if dataplane else 'false'}\n"
        "min_session_kbps = 200\n"
        f"control_grant_ttl_s = {grant_ttl}\n"
        "\n[logging]\n"
        "level = ERROR\n",
        encoding="utf-8",
    )
    return path


def _spawn(config: Path, data_dir: Path) -> subprocess.Popen:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
    environment["RDC_DATA_DIR"] = str(data_dir)
    return subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config)],
        cwd=str(ROOT),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


# ------------------------------------------------------------------ 协议工具


def _authenticate(connection: socket.socket, identity: DeviceIdentity) -> None:
    _send(
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
    challenge = _recv(connection)
    assert challenge is not None and challenge["type"] == "challenge"
    nonce = str(challenge["payload"]["nonce"])
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    _send(connection, build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature}))
    reply = _recv(connection)
    assert reply is not None and reply["type"] == "auth_ok"


def _negotiate(
    controller: socket.socket, host: socket.socket, controller_id: DeviceIdentity, host_id: DeviceIdentity
) -> str:
    session_id = str(uuid.uuid4())
    _send(
        controller,
        build_envelope(
            controller_id,
            "offer",
            {
                "session_id": session_id,
                "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
                "media_caps": {"codecs": ["h264"]},
                "access_proof": "cHJvb2Y=",
                "from_device_id": controller_id.device_id,
                "to_device_id": host_id.device_id,
            },
            session_id=session_id,
        ),
    )
    offer = _recv(host)
    assert offer is not None and offer["type"] == "offer"
    _send(
        host,
        build_envelope(
            host_id,
            "answer",
            {
                "session_id": session_id,
                "chosen_candidate": {"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1},
                "media_caps": {"codecs": ["h264"]},
                "from_device_id": host_id.device_id,
                "to_device_id": controller_id.device_id,
            },
            session_id=session_id,
        ),
    )
    answer = _recv(controller)
    assert answer is not None and answer["type"] == "answer"
    return session_id


def _allocate(controller: socket.socket, identity: DeviceIdentity, session_id: str) -> dict:
    _send(
        controller,
        build_envelope(
            identity,
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 1200, "est_duration_s": 600},
            session_id=session_id,
        ),
    )
    reply = _recv(controller)
    assert reply is not None
    return reply


def _hello(port: int, relay_token: str, session_id: str, role: str) -> tuple[socket.socket, dict]:
    connection = _connect(port)
    _send(
        connection,
        {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": relay_token,
            "session_id": session_id,
            "role": role,
        },
    )
    reply = _recv(connection)
    assert reply is not None
    return connection, reply


# ------------------------------------------------------------------ 双进程集成


def test_signaling_process_grant_is_visible_to_relay_process(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    signaling_port, signaling_health = _free_port(), _free_port()
    relay_port, relay_health = _free_port(), _free_port()
    signaling = _spawn(
        _config(tmp_path / "signaling.conf", port=signaling_port, health=signaling_health, dataplane=False),
        data_dir,
    )
    relay = _spawn(
        _config(tmp_path / "relay.conf", port=relay_port, health=relay_health, dataplane=True),
        data_dir,
    )
    controller_socket = host_socket = publisher = subscriber = None
    try:
        _wait_port(signaling_port)
        _wait_port(relay_port)
        controller_id, host_id = DeviceIdentity.generate(), DeviceIdentity.generate()
        controller_socket, host_socket = _connect(signaling_port), _connect(signaling_port)
        _authenticate(controller_socket, controller_id)
        _authenticate(host_socket, host_id)
        session_id = _negotiate(controller_socket, host_socket, controller_id, host_id)
        granted = _allocate(controller_socket, controller_id, session_id)
        assert granted["type"] == "relay_allocated", granted
        relay_token = str(granted["payload"]["relay_token"])

        # 授予确实写进了共享库, 而不是只留在信令进程内存
        shared = RelayGrantStore(data_dir / "accounts.db")
        try:
            stored = shared.get(relay_token)
            assert stored is not None and stored["session_id"] == session_id
        finally:
            shared.close()

        # 中继进程用同一份库解析令牌, 数据面必须就绪并且双向转发
        publisher, ready = _hello(relay_port, relay_token, session_id, "publisher")
        assert ready["type"] == "relay_ready", ready
        subscriber, ready = _hello(relay_port, relay_token, session_id, "subscriber")
        assert ready["type"] == "relay_ready", ready
        _send_frame(publisher, b"cross-process")
        assert _recv_frame(subscriber) == b"cross-process"

        # 会话结束在信令进程删库, 中继进程复核后必须断开两端数据面
        _send(
            controller_socket,
            build_envelope(
                controller_id, "session_end", {"session_id": session_id, "reason": "验证结束"}, session_id=session_id
            ),
        )
        assert _wait_eof(subscriber)
        assert _wait_eof(publisher)
    finally:
        for connection in (controller_socket, host_socket, publisher, subscriber):
            if connection is not None:
                connection.close()
        _stop(signaling, relay)


def test_expired_grant_is_rejected_cross_process(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    signaling_port, signaling_health = _free_port(), _free_port()
    relay_port, relay_health = _free_port(), _free_port()
    signaling = _spawn(
        _config(
            tmp_path / "signaling.conf", port=signaling_port, health=signaling_health, dataplane=False, grant_ttl=1
        ),
        data_dir,
    )
    relay = _spawn(
        _config(tmp_path / "relay.conf", port=relay_port, health=relay_health, dataplane=True),
        data_dir,
    )
    controller_socket = host_socket = attempt = None
    try:
        _wait_port(signaling_port)
        _wait_port(relay_port)
        controller_id, host_id = DeviceIdentity.generate(), DeviceIdentity.generate()
        controller_socket, host_socket = _connect(signaling_port), _connect(signaling_port)
        _authenticate(controller_socket, controller_id)
        _authenticate(host_socket, host_id)
        session_id = _negotiate(controller_socket, host_socket, controller_id, host_id)
        granted = _allocate(controller_socket, controller_id, session_id)
        assert granted["type"] == "relay_allocated", granted
        relay_token = str(granted["payload"]["relay_token"])

        time.sleep(1.3)
        attempt, reply = _hello(relay_port, relay_token, session_id, "publisher")
        assert reply["type"] == "relay_error"
        assert reply["code"] == "TOKEN_INVALID"
    finally:
        for connection in (controller_socket, host_socket, attempt):
            if connection is not None:
                connection.close()
        _stop(signaling, relay)


# ------------------------------------------------------------------ 表本身


def _grant_record(session_id: str, device_id: str = "device-a", seconds: float = 60.0) -> dict:
    return {
        "session_id": session_id,
        "device_id": device_id,
        "budget_kbps": 1200,
        "expires_at": time.time() + seconds,
        "scope": "control",
        "wall_id": "",
        "cell": -1,
        "account_id": "account-a",
    }


def test_store_keeps_single_grant_per_session_under_concurrency(tmp_path):
    path = tmp_path / "accounts.db"
    first, second = RelayGrantStore(path), RelayGrantStore(path)
    barrier = threading.Barrier(2)

    def issue(store: RelayGrantStore, token: str) -> None:
        barrier.wait(timeout=5)
        store.put(token, _grant_record("session-1"))

    try:
        workers = [
            threading.Thread(target=issue, args=(first, "token-1")),
            threading.Thread(target=issue, args=(second, "token-2")),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert first.count() == 1
        resolvable = [token for token in ("token-1", "token-2") if first.get(token) is not None]
        assert len(resolvable) == 1
    finally:
        first.close()
        second.close()


def test_store_drops_expired_rows_on_read(tmp_path):
    path = tmp_path / "accounts.db"
    store = RelayGrantStore(path)
    try:
        store.put("short", _grant_record("session-2", seconds=0.05))
        time.sleep(0.1)
        assert store.get("short") is None
        assert store.count() == 0
    finally:
        store.close()


def test_existing_accounts_database_opens_after_grants_table_added(tmp_path):
    path = tmp_path / "accounts.db"
    AccountDatabase(path).close()
    with sqlite3.connect(str(path)) as raw:
        version_before = int(raw.execute("PRAGMA user_version").fetchone()[0])

    store = RelayGrantStore(path)
    try:
        assert store.count() == 0
    finally:
        store.close()

    reopened = AccountDatabase(path)
    reopened.close()
    with sqlite3.connect(str(path)) as raw:
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == version_before
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "devices" in tables and "relay_grants" in tables
