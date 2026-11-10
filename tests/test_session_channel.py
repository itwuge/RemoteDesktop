"""会话通道: 环路收发、背压与真实 TCP 中继数据面握手."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from rdc.relay.dataplane import RelayDataPlane, RelayGrant
from rdc.session.channel import (
    CHANNEL_BACKPRESSURE,
    LoopbackChannel,
    RelayFrameChannel,
    SessionChannelError,
    recv_frame,
)


def test_loopback_delivers_and_reports_backpressure() -> None:
    """环路通道按帧投递; 容量满时返回背压而不是无限堆积."""
    left, right = LoopbackChannel.pair("session-1", capacity=1)
    left.send("clipboard_update", {"session_id": "session-1", "content": "x"})
    assert right.receive(0) == ("clipboard_update", {"session_id": "session-1", "content": "x"})
    left.send("clipboard_update", {"session_id": "session-1", "content": "y"})
    with pytest.raises(SessionChannelError) as failure:
        left.send("clipboard_update", {"session_id": "session-1", "content": "z"})
    assert failure.value.code == CHANNEL_BACKPRESSURE
    assert right.receive(0) == ("clipboard_update", {"session_id": "session-1", "content": "y"})


class RelayHarness:
    """本机 TCP 中继: 真实 relay_hello 握手 + RelayDataPlane 转发."""

    def __init__(self) -> None:
        self.session_id = "session-e2e"
        self.token = "token-e2e"
        self.grant = RelayGrant(session_id=self.session_id, budget_kbps=100_000, expires_at=time.time() + 120)
        self.plane = RelayDataPlane(self._resolve, max_sessions=4)
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = int(self.server.getsockname()[1])
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self._accept = threading.Thread(target=self._accept_loop, name="relay-harness", daemon=True)
        self._accept.start()

    def _resolve(self, token: str) -> RelayGrant | None:
        return self.grant if token == self.token else None

    def _accept_loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        raw = recv_frame(conn)
        if raw is None:
            conn.close()
            return
        hello = self.plane.parse_hello(raw)
        if hello is None:
            conn.close()
            return
        self.plane.attach(conn, hello)

    def connect(self, role: str) -> RelayFrameChannel:
        channel = RelayFrameChannel("127.0.0.1", self.port, self.token, self.session_id, role)
        channel.open()
        return channel

    def close(self) -> None:
        self.stop.set()
        self.server.close()


@pytest.fixture()
def relay() -> RelayHarness:
    harness = RelayHarness()
    try:
        yield harness
    finally:
        harness.close()


def test_relay_channel_handshake_and_round_trip(relay: RelayHarness) -> None:
    """两个真实 TCP 通道经 RelayDataPlane 配对后双向投递."""
    receiver = relay.connect("publisher")
    sender = relay.connect("subscriber")
    try:
        sender.send("file_offer", {"session_id": relay.session_id, "transfer_id": "t-1", "value": 1})
        assert _wait_message(receiver) == (
            "file_offer",
            {"session_id": relay.session_id, "transfer_id": "t-1", "value": 1},
        )
        receiver.send("file_accept", {"session_id": relay.session_id, "transfer_id": "t-1", "value": 2})
        assert _wait_message(sender) == (
            "file_accept",
            {"session_id": relay.session_id, "transfer_id": "t-1", "value": 2},
        )
        assert sender.info().visible_to_relay is True
        assert sender.info().encrypted is False
    finally:
        sender.close()
        receiver.close()


def test_relay_channel_rejects_bad_token() -> None:
    """令牌不匹配时握手失败, 错误码明确."""
    harness = RelayHarness()
    try:
        channel = RelayFrameChannel("127.0.0.1", harness.port, "wrong", harness.session_id, "publisher")
        with pytest.raises(SessionChannelError) as failure:
            channel.open()
        assert failure.value.code in {"CHANNEL_AUTH_FAILED", "TOKEN_INVALID"}
    finally:
        harness.close()


def _wait_message(channel: RelayFrameChannel, timeout: float = 3.0) -> tuple[str, dict[str, object]] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = channel.receive(0.05)
        if message is not None:
            return message
    return None
