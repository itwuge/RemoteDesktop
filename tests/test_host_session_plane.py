"""host 会话面分流: 文件与剪贴板消息只走接收端, 输入消息原样放行."""

from __future__ import annotations

from typing import Any

import rdc_host_linux as host


class _FakeChannel:
    """只记录发送内容, 不碰网络."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


class _FakeClipboard:
    """记录 applied 次数, 与真实接收端接口一致."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, str]] = []
        self.suppressed = 0

    def handle(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if msg_type != "clipboard_update":
            return None
        self.applied.append((str(payload.get("kind", "")), str(payload.get("content", ""))))
        return {"type": "clipboard_ack", "payload": {"session_id": payload["session_id"]}}


def _session() -> host.HostSession:
    session = host.HostSession.__new__(host.HostSession)
    session.session_id = "s-1"
    return session


def test_input_message_is_not_consumed(tmp_path: Any) -> None:
    channel = _FakeChannel()
    handled = _session()._handle_session_plane(
        channel,
        {"kind": "mouse", "action": "move", "x": 1, "y": 2},
        host.FileTransferReceiver(tmp_path),
        _FakeClipboard(),
    )
    assert handled is False
    assert channel.sent == []


def test_clipboard_update_is_routed_and_acked(tmp_path: Any) -> None:
    channel = _FakeChannel()
    clipboard = _FakeClipboard()
    message = {"type": "clipboard_update", "payload": {"kind": "text", "content": "hello"}}
    handled = _session()._handle_session_plane(channel, message, host.FileTransferReceiver(tmp_path), clipboard)
    assert handled is True
    assert clipboard.applied == [("text", "hello")]
    # session_id 缺失时由会话补进 payload, 回包也带会话号
    assert channel.sent == [{"type": "clipboard_ack", "session_id": "s-1", "payload": {"session_id": "s-1"}}]


def test_file_chunk_without_offer_is_rejected(tmp_path: Any) -> None:
    channel = _FakeChannel()
    message = {"type": "file_chunk", "session_id": "s-1", "payload": {"transfer_id": "t-1", "index": 0, "data": ""}}
    handled = _session()._handle_session_plane(channel, message, host.FileTransferReceiver(tmp_path), _FakeClipboard())
    assert handled is True
    assert channel.sent and channel.sent[0]["type"] == "file_abort"
    assert channel.sent[0]["payload"]["error_code"]
