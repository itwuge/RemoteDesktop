"""直连媒体回归: accept 首帧分流、HostSession 注入、pending 竞态与回落中继."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

import pytest

from rdc.connect.policy import TRANSPORT_DIRECT_IPV6, assert_direct_ipv6
from rdc.crypto.identity import DeviceIdentity
from rdc.relay.server import RelayConfig, RelayServer, _peek_direct, send_frame
from rdc.session import host as host_module
from rdc.session import remote as remote_module


class _FakeRelay:
    """只记录直连登记, 不启动真实服务."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, Any]] = []
        self.unregistered: list[str] = []

    def register_direct(self, session_id: str, handler: Any) -> None:
        self.registered.append((session_id, handler))

    def unregister_direct(self, session_id: str) -> None:
        self.unregistered.append(session_id)


class _FakeSocket:
    """只记录关闭与超时设置."""

    def __init__(self) -> None:
        self.closed = False
        self.timeout: float | None = None

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    """替代 HostSession, 记录被注入的直连 socket."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.direct = kwargs.get("direct")
        self.started = False

    def start(self) -> None:
        self.started = True


def _config() -> RelayConfig:
    return RelayConfig(
        bind="127.0.0.1",
        port=0,
        health_bind="127.0.0.1",
        health_port=0,
        relay_endpoint="relay://127.0.0.1:8750",
        egress_mbps=3,
        overhead_ratio=0.08,
        max_sessions=1,
        degrade_profile="L0",
    )


def _service(monkeypatch: pytest.MonkeyPatch, relay: _FakeRelay) -> host_module.HostService:
    service = host_module.HostService(host_module.HostConfig(port=27888), DeviceIdentity.generate())
    service.server = relay  # 类型: RelayServer, 测试里只用到登记接口
    monkeypatch.setattr(service.guard, "accept", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(service, "_start_punch_targets", lambda candidates: None)
    monkeypatch.setattr(service, "_send", lambda *args, **kwargs: None)
    return service


# --------------------------------------------------------------- 首帧分流


def test_peek_direct_recognizes_only_e2e_hello() -> None:
    hello = {"kind": "e2e_hello", "session_id": "s-1", "device_id": "d-1"}
    assert _peek_direct(json.dumps(hello).encode("utf-8")) == hello
    # 信令包络与数据面 relay_hello 都不得命中直连分流
    assert _peek_direct(json.dumps({"type": "relay_allocate", "payload": {}}).encode("utf-8")) is None
    assert _peek_direct(json.dumps({"kind": "relay_hello", "token": "t"}).encode("utf-8")) is None
    assert _peek_direct(b"\x00\x00\x00\x00") is None


def test_relay_accept_hands_e2e_hello_to_registered_handler() -> None:
    server = RelayServer(_config())
    seen: dict[str, Any] = {}

    def handler(connection: socket.socket, hello: dict[str, Any]) -> bool:
        seen["hello"] = hello
        seen["sock"] = connection
        return True

    server.register_direct("s-1", handler)
    client, server_sock = socket.socketpair()
    try:
        send_frame(client, {"kind": "e2e_hello", "session_id": "s-1", "device_id": "d-1", "sig": "x"})
        thread = threading.Thread(
            target=server._handle_connection,
            args=(server_sock, "127.0.0.1"),
            daemon=True,
        )
        thread.start()
        thread.join(5)
        assert seen["hello"]["session_id"] == "s-1"
        assert seen["sock"] is server_sock
        # 接管成功: 服务端不再关闭 socket, 所有权已转移给会话
        assert server_sock.fileno() != -1
    finally:
        client.close()
        server_sock.close()


def test_relay_accept_closes_unregistered_direct_hello() -> None:
    server = RelayServer(_config())
    client, server_sock = socket.socketpair()
    try:
        send_frame(client, {"kind": "e2e_hello", "session_id": "unknown", "device_id": "d-1"})
        thread = threading.Thread(
            target=server._handle_connection,
            args=(server_sock, "127.0.0.1"),
            daemon=True,
        )
        thread.start()
        thread.join(5)
        # 未登记会话: 服务端关闭连接, 不把 socket 交出去
        assert server_sock.fileno() == -1
    finally:
        client.close()
        server_sock.close()


# --------------------------------------------------------------- 会话创建竞态


def test_pending_and_registration_written_before_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _FakeRelay()
    service = _service(monkeypatch, relay)
    monkeypatch.setattr(
        service,
        "_answer_candidates",
        lambda: (host_module.Candidate(kind="host", family=6, address="240e::1", port=27888),),
    )
    observed: dict[str, Any] = {}

    def fake_answer(*args: Any, **kwargs: Any) -> None:
        observed["pending"] = dict(service.pending or {})
        observed["registered"] = list(relay.registered)

    monkeypatch.setattr(service, "_answer", fake_answer)
    service._on_offer(
        {
            "session_id": "s-1",
            "from_device_id": "peer-1",
            "access_auth": {},
            "media_caps": {},
        }
    )
    # answer 下发时 pending 与直连接管必须已经就位, 否则早到的直连会被当成未知会话
    assert observed["pending"]["session_id"] == "s-1"
    assert observed["pending"]["direct_possible"] is True
    assert observed["registered"][0][0] == "s-1"


def test_allocated_prefers_ready_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _FakeRelay()
    service = _service(monkeypatch, relay)
    service.pending = {
        "session_id": "s-1",
        "peer": "peer-1",
        "frame_version": host_module.VERSION_V2,
        "transport": "direct",
        "direct_possible": True,
    }
    service._direct_event.set()
    sock = _FakeSocket()
    service._direct_ready = (sock, object())  # 占位 AEAD 会话
    created: dict[str, Any] = {}

    def fake_session(*args: Any, **kwargs: Any) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)
        created["session"] = session
        return session

    monkeypatch.setattr(host_module, "HostSession", fake_session)
    service._on_allocated({"session_id": "s-1", "relay_token": "token-1"})
    assert created["session"].direct is not None
    assert created["session"].started is True
    assert sock.closed is False
    assert relay.unregistered == ["s-1"]


def test_allocated_falls_back_to_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _FakeRelay()
    service = _service(monkeypatch, relay)
    service.pending = {
        "session_id": "s-1",
        "peer": "peer-1",
        "frame_version": host_module.VERSION_V2,
        "transport": "relay",
        "direct_possible": True,
    }
    service._direct_event.set()
    created: dict[str, Any] = {}

    def fake_session(*args: Any, **kwargs: Any) -> _FakeSession:
        session = _FakeSession(*args, **kwargs)
        created["session"] = session
        return session

    monkeypatch.setattr(host_module, "HostSession", fake_session)
    service._on_allocated({"session_id": "s-1", "relay_token": "token-1"})
    assert created["session"].direct is None
    assert created["session"].started is True
    assert relay.unregistered == ["s-1"]


# --------------------------------------------------------------- 控制端直连


def _remote() -> remote_module.RemoteSession:
    return remote_module.RemoteSession(
        remote_module.RemoteConfig(host="127.0.0.1", port=27888, device_id="PEER", access_code="")
    )


def test_direct_candidates_keep_only_ipv6_host() -> None:
    payload = {
        "candidates": [
            {"kind": "host", "family": 4, "address": "192.168.1.2", "port": 27888},
            {"kind": "host", "family": 6, "address": "fe80::1", "port": 27888},
            {"kind": "host", "family": 6, "address": "240e:3a1::5", "port": 27888},
            {"kind": "srflx", "family": 6, "address": "240e:3a1::9", "port": 4000},
        ]
    }
    items = _remote()._direct_candidates(payload)
    # 链路本地与 v4 不能做 TCP 直连媒体, 只留全局 IPv6 host
    assert [item.address for item in items] == ["240e:3a1::5"]


def test_connect_direct_uses_reachable_ipv6_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _remote()
    sock = _FakeSocket()
    monkeypatch.setattr(remote_module.socket, "create_connection", lambda *args, **kwargs: sock)
    monkeypatch.setattr(remote_module, "controller_handshake", lambda *args, **kwargs: object())
    result = session._connect_direct(
        {"candidates": [{"kind": "host", "family": 6, "address": "240e:3a1::5", "port": 27888}]}
    )
    assert result is not None
    assert result[0] is sock
    assert result[2].address == "240e:3a1::5"
    assert sock.closed is False


def test_connect_direct_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _remote()

    def unreachable(*args: Any, **kwargs: Any) -> _FakeSocket:
        raise OSError("unreachable")

    monkeypatch.setattr(remote_module.socket, "create_connection", unreachable)
    result = session._connect_direct(
        {"candidates": [{"kind": "host", "family": 6, "address": "240e:3a1::5", "port": 27888}]}
    )
    assert result is None
    # 直连失败时保持默认中继口径, 由调用方继续 relay_connect
    assert session.transport_kind == "relay"


def test_connect_direct_tries_candidates_until_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """多监听端口展开出的多个 v6 host 候选逐个尝试, 第一个不通就换下一个."""
    session = _remote()
    attempts: list[tuple[str, int]] = []
    sock = _FakeSocket()

    def create_connection(address: tuple[str, int], timeout: float | None = None) -> _FakeSocket:
        attempts.append(address)
        if address[1] == 27888:
            raise OSError("unreachable")
        return sock

    monkeypatch.setattr(remote_module.socket, "create_connection", create_connection)
    monkeypatch.setattr(remote_module, "controller_handshake", lambda *args, **kwargs: object())
    result = session._connect_direct(
        {
            "candidates": [
                {"kind": "host", "family": 6, "address": "240e:3a1::5", "port": 27888, "priority": 100},
                {"kind": "host", "family": 6, "address": "240e:3a1::5", "port": 443, "priority": 99},
            ]
        }
    )
    assert attempts == [("240e:3a1::5", 27888), ("240e:3a1::5", 443)]
    assert result is not None
    assert result[2].port == 443
    assert sock.closed is False


def test_connect_direct_without_candidates_returns_none() -> None:
    session = _remote()
    assert session._connect_direct({"candidates": []}) is None
    assert session.transport_kind == "relay"


def test_assert_direct_ipv6_accepts_any_global_prefix() -> None:
    def verdict(address: str) -> bool:
        path = {
            "transport": TRANSPORT_DIRECT_IPV6,
            "peer_address": address,
            "peer_family": 6,
            "relay_used": False,
        }
        return bool(assert_direct_ipv6(path)["peer_address_global_v6"])

    # 不再硬编码 240e: 任何全局 IPv6 前缀都算通过, ULA 与链路本地不通过
    assert verdict("240e:3a1::5") is True
    assert verdict("2408:8000::1") is True
    assert verdict("fc00::1") is False
    assert verdict("fe80::1") is False
