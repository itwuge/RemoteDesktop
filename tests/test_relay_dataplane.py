"""中继数据面: 密文转发与预算限速的单元测试."""

from __future__ import annotations

import contextlib
import json
import socket
import struct
import threading
import time

from rdc.relay.dataplane import DATA_MAX_FRAME_BYTES, RelayDataPlane, RelayGrant, TokenBucket
from rdc.relay.server import RelayConfig, RelayServer, recv_frame, send_frame
from rdc.signaling.server import RelayGrantRecord


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


# --------------------------------------------------------------- 令牌桶


def test_token_bucket_allows_burst_then_blocks():
    bucket = TokenBucket(rate_bps=1000.0, capacity_s=0.1)  # 容量 100 字节
    assert bucket.allow(100) is True
    assert bucket.allow(100) is False
    assert bucket.allow(99) is False


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(rate_bps=10_000.0, capacity_s=0.1)
    assert bucket.allow(1000) is True
    assert bucket.allow(1000) is False
    time.sleep(0.15)
    assert bucket.allow(1000) is True


# --------------------------------------------------------------- 握手识别


def test_parse_hello_recognizes_data_plane_only():
    plane = RelayDataPlane(lambda _token: None, max_sessions=1)
    hello = plane.parse_hello(json.dumps({"plane": "data", "v": 1}).encode("utf-8"))
    assert hello == {"plane": "data", "v": 1}
    assert plane.parse_hello(json.dumps({"v": 1, "type": "offer"}).encode("utf-8")) is None
    assert plane.parse_hello(b"not-json") is None


def test_attach_rejects_unknown_token():
    plane = RelayDataPlane(lambda _token: None, max_sessions=1)
    left, right = socket.socketpair()
    try:
        plane.attach(left, {"plane": "data", "v": 1, "relay_token": "bad", "session_id": "s", "role": "publisher"})
        response = json.loads(recv_frame(right).decode("utf-8"))
        assert response["type"] == "relay_error"
        assert response["code"] == "TOKEN_INVALID"
        assert plane.rejected_tokens == 1
    finally:
        right.close()


def test_attach_rejects_wrong_role_and_session_mismatch():
    grant = RelayGrant(session_id="s1", budget_kbps=1000, expires_at=time.time() + 60)
    plane = RelayDataPlane(lambda _token: grant, max_sessions=1)
    cases = (
        ({"plane": "data", "v": 1, "relay_token": "t", "session_id": "s1", "role": "observer"}, "role"),
        ({"plane": "data", "v": 1, "relay_token": "t", "session_id": "s2", "role": "publisher"}, "不匹配"),
        ({"plane": "data", "v": 2, "relay_token": "t", "session_id": "s1", "role": "publisher"}, "版本"),
    )
    for hello, expected in cases:
        left, right = socket.socketpair()
        try:
            plane.attach(left, hello)
            response = json.loads(recv_frame(right).decode("utf-8"))
            assert response["type"] == "relay_error"
            assert expected in response["message"] or expected == "role"
        finally:
            right.close()


def test_expired_grant_is_rejected():
    grant = RelayGrant(session_id="s1", budget_kbps=1000, expires_at=time.time() - 1)
    plane = RelayDataPlane(lambda _token: grant, max_sessions=1)
    left, right = socket.socketpair()
    try:
        plane.attach(left, {"plane": "data", "v": 1, "relay_token": "t", "session_id": "s1", "role": "publisher"})
        response = json.loads(recv_frame(right).decode("utf-8"))
        assert response["code"] == "TOKEN_INVALID"
    finally:
        right.close()


# --------------------------------------------------------------- 端到端转发


def _wait_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"端口 {port} 未就绪")


def _start_relay(max_sessions: int = 2) -> tuple[RelayServer, int]:
    data_port, health_port = _free_port(), _free_port()
    server = RelayServer(_config(data_port, health_port, max_sessions))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_port(data_port)
    return server, data_port


def _grant(server: RelayServer, session_id: str, budget_kbps: int = 100_000) -> str:
    token = f"token-{session_id}-{time.time_ns()}"
    server.signaling.relay_grants[token] = RelayGrantRecord(
        session_id=session_id,
        budget_kbps=budget_kbps,
        expires_at=time.time() + 60,
    )
    return token


def _connect(port: int, token: str, session_id: str, role: str) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    connection.settimeout(10.0)
    send_frame(
        connection,
        {"plane": "data", "v": 1, "type": "relay_hello", "relay_token": token, "session_id": session_id, "role": role},
    )
    ready = json.loads(recv_frame(connection).decode("utf-8"))
    assert ready["type"] == "relay_ready"
    assert ready["role"] == role
    return connection


def _send_raw(connection: socket.socket, payload: bytes) -> None:
    """数据面帧直接发原始密文字节, 不走 JSON 封装."""
    connection.sendall(struct.pack(">I", len(payload)) + payload)


def test_ciphertext_is_forwarded_byte_exact_both_ways():
    server, port = _start_relay()
    publisher = subscriber = None
    try:
        token = _grant(server, "session-1")
        errors: list[BaseException] = []

        def run_subscriber() -> None:
            nonlocal subscriber
            try:
                subscriber = _connect(port, token, "session-1", "subscriber")
            except BaseException as exc:  # 线程内异常必须带回主线程
                errors.append(exc)

        thread = threading.Thread(target=run_subscriber, daemon=True)
        thread.start()
        publisher = _connect(port, token, "session-1", "publisher")
        thread.join(timeout=5)
        assert not errors and subscriber is not None

        frames = [bytes([index]) * 1024 for index in range(8)]
        for frame in frames:
            _send_raw(publisher, frame)
        for expected in frames:
            assert recv_frame(subscriber) == expected

        # 反向: 订阅方发给发布方同样逐字节一致
        back = b"control-plane-ciphertext"
        _send_raw(subscriber, back)
        assert recv_frame(publisher) == back
    finally:
        for sock in (publisher, subscriber):
            if sock is not None:
                sock.close()
        server.stop()


def test_publisher_over_budget_drops_frames_and_counts():
    server, port = _start_relay()
    publisher = subscriber = None
    try:
        # 8 kbps 预算, 桶容量 0.5 秒 = 500 字节, 发送 2 KiB 帧必然超预算
        token = _grant(server, "session-slow", budget_kbps=8)
        errors: list[BaseException] = []

        def run_subscriber() -> None:
            nonlocal subscriber
            try:
                subscriber = _connect(port, token, "session-slow", "subscriber")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_subscriber, daemon=True)
        thread.start()
        publisher = _connect(port, token, "session-slow", "publisher")
        thread.join(timeout=5)
        assert not errors and subscriber is not None

        for index in range(10):
            _send_raw(publisher, bytes(2048) + index.to_bytes(2, "big"))
            time.sleep(0.01)

        snapshot = server.dataplane.snapshot()
        assert snapshot["frames_dropped"] > 0
        assert snapshot["frames_forwarded"] < 10
        assert snapshot["bytes_forwarded"] <= 10 * 2050
    finally:
        for sock in (publisher, subscriber):
            if sock is not None:
                sock.close()
        server.stop()


def test_session_limit_returns_relay_full():
    server, port = _start_relay(max_sessions=1)
    first = second = None
    try:
        first = _connect(port, _grant(server, "session-a"), "session-a", "publisher")
        connection = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        connection.settimeout(5.0)
        send_frame(
            connection,
            {
                "plane": "data",
                "v": 1,
                "type": "relay_hello",
                "relay_token": _grant(server, "session-b"),
                "session_id": "session-b",
                "role": "publisher",
            },
        )
        response = json.loads(recv_frame(connection).decode("utf-8"))
        assert response["type"] == "relay_error"
        assert response["code"] == "RELAY_FULL"
        connection.close()
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        server.stop()


def test_oversized_data_frame_is_rejected():
    server, port = _start_relay()
    publisher = subscriber = None
    try:
        token = _grant(server, "session-big")
        errors: list[BaseException] = []

        def run_subscriber() -> None:
            nonlocal subscriber
            try:
                subscriber = _connect(port, token, "session-big", "subscriber")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_subscriber, daemon=True)
        thread.start()
        publisher = _connect(port, token, "session-big", "publisher")
        thread.join(timeout=5)
        assert not errors

        header = (DATA_MAX_FRAME_BYTES + 1).to_bytes(4, "big")
        publisher.sendall(header + b"\x00")
        # 中继断开而不是缓冲超大帧: 对端读到 EOF 或被重置
        publisher.settimeout(3.0)
        with contextlib.suppress(ConnectionResetError):
            assert publisher.recv(1) == b""
    finally:
        for sock in (publisher, subscriber):
            if sock is not None:
                sock.close()
        server.stop()


def test_healthz_exposes_dataplane_counters():
    server, _port = _start_relay()
    try:
        body = server.dataplane.snapshot()
        for key in (
            "sessions",
            "max_sessions",
            "rejected_tokens",
            "bytes_forwarded",
            "frames_forwarded",
            "frames_dropped",
        ):
            assert key in body
    finally:
        server.stop()
