"""AEAD 通道适配与会话装配: 只转协议、按类型分发、双向收发不互相丢帧."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rdc.clipboard.sync import ClipboardSyncService
from rdc.files.service import FileTransferService
from rdc.session.aead import AeadSessionChannel
from rdc.session.channel import LoopbackChannel, SessionChannelError
from rdc.session.desktop import DesktopSessionBinding


class _FakeLink:
    """最小 AEAD 链路替身: 队列进、列表出, 用于验证转接不改语义."""

    def __init__(self) -> None:
        self.inbox: queue.Queue[bytes | None] = queue.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send(self, payload: dict[str, Any]) -> int:
        self.sent.append(payload)
        return len(json.dumps(payload))

    def recv_raw(self) -> bytes:
        item = self.inbox.get(timeout=2.0)
        if item is None:
            raise OSError("对端已关闭")
        return item

    def close(self) -> None:
        self.closed = True
        self.inbox.put(None)


def _envelope(msg_type: str, payload: dict[str, Any], session_id: str = "s1") -> bytes:
    return json.dumps({"type": msg_type, "session_id": session_id, "payload": payload}).encode("utf-8")


def test_adapter_routes_session_plane_and_passes_control_through() -> None:
    link = _FakeLink()
    control: list[bytes | dict[str, Any]] = []
    channel = AeadSessionChannel(
        link,
        session_id="s1",
        role="controller",
        budget_kbps=8000,
        on_control=control.append,
    )
    channel.open()
    try:
        link.inbox.put(b"\x00\x02video-bytes")
        link.inbox.put(json.dumps({"kind": "ready", "w": 1280, "h": 720}).encode("utf-8"))
        link.inbox.put(_envelope("file_offer", {"transfer_id": "t1", "total_bytes": 3}))

        msg_type, payload = channel.receive(2.0) or ("", {})
        assert msg_type == "file_offer"
        assert payload["session_id"] == "s1"
        assert payload["transfer_id"] == "t1"
        assert control, "二进制与 kind 控制帧应交给上层"
        assert control[0] == b"\x00\x02video-bytes"
        assert control[1]["kind"] == "ready"

        channel.send("clipboard_ack", {"session_id": "s1", "kind": "text", "accepted": True})
        assert link.sent[-1] == {
            "type": "clipboard_ack",
            "session_id": "s1",
            "payload": {"session_id": "s1", "kind": "text", "accepted": True},
        }
        info = channel.info()
        assert info.encrypted is True
        assert info.visible_to_relay is False
        assert info.kind == "aead"
        assert info.budget_kbps == 8000
    finally:
        channel.close()


def test_adapter_closed_channel_raises() -> None:
    link = _FakeLink()
    channel = AeadSessionChannel(link, session_id="s1", role="controller")
    channel.open()
    channel.close()
    with pytest.raises(SessionChannelError):
        channel.receive(0.2)


def _wait_until(predicate: Callable[[], bool], pumps: tuple[Callable[[], Any], ...], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pump in pumps:
            pump()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_desktop_binding_shares_services_and_moves_both_directions(tmp_path: Path) -> None:
    left, right = LoopbackChannel.pair("s1", capacity=512)
    left.open()
    right.open()

    transfer = FileTransferService()
    clipboard = ClipboardSyncService()
    received: list[dict[str, Any]] = []
    applied: list[tuple[str, str]] = []
    binding = DesktopSessionBinding(
        left,
        transfer_service=transfer,
        clipboard_service=clipboard,
        destination_root=tmp_path / "inbox",
        on_files_received=received.append,
        on_clipboard_apply=lambda kind, content: applied.append((kind, content)),
    )
    peer_transfer = FileTransferService()
    peer_clipboard = ClipboardSyncService()
    peer_received: list[dict[str, Any]] = []
    peer_applied: list[tuple[str, str]] = []
    peer = DesktopSessionBinding(
        right,
        transfer_service=peer_transfer,
        clipboard_service=peer_clipboard,
        destination_root=tmp_path / "peer",
        on_files_received=peer_received.append,
        on_clipboard_apply=lambda kind, content: peer_applied.append((kind, content)),
    )
    try:
        # 装配只接线: 用的是传入的同一份服务实例, 且未收帧前不建接收目录
        assert binding.transfer is transfer
        assert binding.clipboard is clipboard
        assert not (tmp_path / "inbox").exists()

        assert transfer.start_transfer(str(_write(tmp_path / "a.bin", 64 * 1024)), str(tmp_path / "remote")).ok
        assert peer_transfer.start_transfer(str(_write(tmp_path / "b.bin", 32 * 1024)), str(tmp_path / "remote")).ok
        assert len(transfer.jobs) == 1 and len(peer_transfer.jobs) == 1, (transfer.jobs, peer_transfer.jobs)

        def _advance() -> None:
            for job in list(transfer.jobs):
                transfer.get_transfer_progress(job)
            for job in list(peer_transfer.jobs):
                peer_transfer.get_transfer_progress(job)
            binding.pump()
            peer.pump()

        done = _wait_until(lambda: len(received) == 1 and len(peer_received) == 1, (_advance,))
        assert done, (
            [transfer.get_transfer_progress(job).data for job in transfer.jobs],
            [peer_transfer.get_transfer_progress(job).data for job in peer_transfer.jobs],
            received,
            peer_received,
        )
        assert (tmp_path / "peer" / "a.bin").read_bytes() == (tmp_path / "a.bin").read_bytes()
        assert (tmp_path / "inbox" / "b.bin").read_bytes() == (tmp_path / "b.bin").read_bytes()
        assert received[0]["files"] == ["b.bin"]
        assert peer_received[0]["files"] == ["a.bin"]

        assert clipboard.push("desktop", "text", "桌面端文本").ok
        assert _wait_until(lambda: bool(peer_applied) and clipboard.confirmed is not None, (binding.pump, peer.pump))
        assert peer_applied[-1] == ("text", "桌面端文本")

        assert peer_clipboard.push("host", "text", "被控端文本").ok
        assert _wait_until(lambda: bool(applied) and peer_clipboard.confirmed is not None, (binding.pump, peer.pump))
        assert applied[-1] == ("text", "被控端文本")
    finally:
        binding.close()
        peer.close()


def test_binding_close_detaches_services(tmp_path: Path) -> None:
    left, right = LoopbackChannel.pair("s1")
    left.open()
    right.open()
    transfer = FileTransferService()
    clipboard = ClipboardSyncService()
    binding = DesktopSessionBinding(
        left,
        transfer_service=transfer,
        clipboard_service=clipboard,
        destination_root=tmp_path / "inbox",
    )
    binding.close()
    assert transfer.channel is None
    assert clipboard.channel is None
    right.close()


def _write(path: Path, size: int) -> Path:
    path.write_bytes(bytes(range(256)) * (size // 256) + bytes(size % 256))
    return path


def test_binding_no_session_leaves_no_threads_or_dirs(tmp_path: Path) -> None:
    """未装配(无会话)时服务保持未绑定, 也不应出现接收目录或装配线程."""
    service = FileTransferService()
    clipboard = ClipboardSyncService()
    assert service.channel is None
    assert clipboard.channel is None
    assert not (tmp_path / "inbox").exists()
    assert not any(
        thread.name.startswith("rdc-session") or thread.name.startswith("rdc-aead") for thread in threading.enumerate()
    )
