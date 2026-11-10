"""文件传输走会话通道: 落盘、校验、续传、背压、超时与拒绝路径."""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from rdc.core import errors
from rdc.files.digest import tree_digest
from rdc.files.model import CHUNK_SIZE
from rdc.files.receiver import FileTransferReceiver
from rdc.files.service import FileTransferService
from rdc.session.channel import ChannelInfo, LoopbackChannel, SessionChannelError


class _CountingChannel:
    """包一层统计真正发出的分块字节, 用来证明续传没有重发."""

    def __init__(self, inner: LoopbackChannel) -> None:
        self.inner = inner
        self.chunk_bytes = 0

    def open(self) -> None:
        self.inner.open()

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if msg_type == "file_chunk":
            self.chunk_bytes += int(payload.get("length", 0))
        self.inner.send(msg_type, payload)

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        return self.inner.receive(timeout)

    def close(self) -> None:
        self.inner.close()

    def info(self) -> ChannelInfo:
        return self.inner.info()


def _make_file(path: Path, size: int) -> str:
    payload = (bytes(range(256)) * (size // 256 + 1))[:size]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _serve(receiver: FileTransferReceiver, channel: LoopbackChannel) -> int:
    handled = 0
    for _ in range(1024):
        try:
            message = channel.receive(0)
        except SessionChannelError:
            break
        if message is None:
            break
        reply = receiver.handle(message[0], message[1])
        if reply is not None:
            try:
                channel.send(reply["type"], reply["payload"])
            except SessionChannelError:
                break
        handled += 1
    return handled


def _run(
    service: FileTransferService,
    receiver: FileTransferReceiver,
    channel: LoopbackChannel,
    job_id: str,
    limit: int = 2000,
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(limit):
        last = service.get_transfer_progress(job_id).data or {}
        if last.get("state") in {"completed", "failed", "canceled"}:
            return last
        _serve(receiver, channel)
    return last


def _bind(
    tmp_path: Path, allow_local_staging: bool = False
) -> tuple[FileTransferService, FileTransferReceiver, LoopbackChannel, LoopbackChannel]:
    sender, receiver_channel = LoopbackChannel.pair("session-file", capacity=4096)
    service = FileTransferService(
        channel=sender,
        allow_local_staging=allow_local_staging,
        poll_budget_bytes=16 * 1024 * 1024,
    )
    receiver = FileTransferReceiver(tmp_path / "inbox")
    return service, receiver, sender, receiver_channel


def test_single_file_lands_on_receiver_with_matching_digest(tmp_path: Path) -> None:
    """发送端 completed 必须以收端 file_result 为准, 收端文件逐字节一致."""
    source = tmp_path / "payload.bin"
    digest = _make_file(source, CHUNK_SIZE * 3 + 123)
    service, receiver, _sender, receiver_channel = _bind(tmp_path)
    started = service.start_transfer(str(source), str(tmp_path / "remote"))
    assert started.ok and started.data is not None
    job_id = str(started.data["job_id"])

    final = _run(service, receiver, receiver_channel, job_id)

    assert final["state"] == "completed", final
    assert final["digest"] == tree_digest([(source.name, source)])
    assert final["sent_bytes"] == source.stat().st_size
    assert final["acked_bytes"] == source.stat().st_size
    assert hashlib.sha256((tmp_path / "inbox" / source.name).read_bytes()).hexdigest() == digest
    assert not (tmp_path / "inbox" / f"{source.name}.part").exists()


def test_resume_from_receiver_part_file_avoids_retransmit(tmp_path: Path) -> None:
    """收端已有 .part 时按其大小续传, 只发剩余块, 最终哈希一致."""
    source = tmp_path / "resume.bin"
    digest = _make_file(source, 700_000)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "resume.bin.part").write_bytes(source.read_bytes()[:200_000])

    sender, receiver_channel = LoopbackChannel.pair("session-file", capacity=4096)
    counting = _CountingChannel(sender)
    service = FileTransferService(channel=counting, allow_local_staging=False, poll_budget_bytes=16 * 1024 * 1024)
    receiver = FileTransferReceiver(inbox)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "remote")).data["job_id"])

    final = _run(service, receiver, receiver_channel, job_id)

    assert final["state"] == "completed"
    assert counting.chunk_bytes == 500_000
    assert final["digest"] == tree_digest([(source.name, source)])
    assert hashlib.sha256((inbox / "resume.bin").read_bytes()).hexdigest() == digest


def test_tampered_chunk_fails_with_explicit_reason(tmp_path: Path) -> None:
    """分块被篡改时收端中止, 发送端拿到明确错误码而不是卡住."""
    source = tmp_path / "tamper.bin"
    _make_file(source, CHUNK_SIZE + 100)
    service, receiver, _sender, receiver_channel = _bind(tmp_path)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "remote")).data["job_id"])

    last: dict[str, Any] = {}
    for _ in range(400):
        last = service.get_transfer_progress(job_id).data or {}
        if last.get("state") in {"completed", "failed"}:
            break
        for _ in range(64):
            try:
                message = receiver_channel.receive(0)
            except SessionChannelError:
                break
            if message is None:
                break
            msg_type, payload = message
            if msg_type == "file_chunk":
                raw = bytearray(base64.b64decode(payload["data"]))
                raw[0] ^= 0xFF
                payload = {**payload, "data": base64.b64encode(bytes(raw)).decode("ascii")}
            reply = receiver.handle(msg_type, payload)
            if reply is not None:
                try:
                    receiver_channel.send(reply["type"], reply["payload"])
                except SessionChannelError:
                    break

    assert last["state"] == "failed"
    assert last["error_code"] == errors.CHUNK_CHECKSUM_MISMATCH
    assert not (tmp_path / "inbox" / source.name).exists()


def test_inflight_window_never_exceeds_four_mebibytes(tmp_path: Path) -> None:
    """收端不确认时发送端停在 4 MiB 窗口, 不无界堆积."""
    source = tmp_path / "window.bin"
    _make_file(source, 8 * 1024 * 1024)
    service, _receiver, _sender, receiver_channel = _bind(tmp_path)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "remote")).data["job_id"])

    last: dict[str, Any] = {}
    for _ in range(200):
        last = service.get_transfer_progress(job_id).data or {}
        for _ in range(64):
            try:
                message = receiver_channel.receive(0)
            except SessionChannelError:
                break
            if message is None:
                break
            if message[0] == "file_offer":
                receiver_channel.send(
                    "file_accept",
                    {
                        "session_id": "session-file",
                        "transfer_id": message[1]["transfer_id"],
                        "accepted_items": [item["relative"] for item in message[1]["items"]],
                        "resume": [],
                    },
                )

    assert last["state"] == "running"
    assert int(last["sent_bytes"]) - int(last["acked_bytes"]) <= 4 * 1024 * 1024
    assert service.cancel_transfer(job_id).ok


def test_missing_ack_fails_with_timeout(tmp_path: Path) -> None:
    """对端只接受不确认时按超时失败, 不悬挂."""
    source = tmp_path / "timeout.bin"
    _make_file(source, CHUNK_SIZE)
    sender, receiver_channel = LoopbackChannel.pair("session-file", capacity=64)
    service = FileTransferService(channel=sender, allow_local_staging=False, ack_timeout_s=0.05)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "remote")).data["job_id"])

    for _ in range(20):
        service.get_transfer_progress(job_id)
        try:
            message = receiver_channel.receive(0)
        except SessionChannelError:
            break
        if message is None:
            continue
        if message[0] == "file_offer":
            receiver_channel.send(
                "file_accept",
                {
                    "session_id": "session-file",
                    "transfer_id": message[1]["transfer_id"],
                    "accepted_items": [item["relative"] for item in message[1]["items"]],
                    "resume": [],
                },
            )
    time.sleep(0.2)
    final = service.get_transfer_progress(job_id).data or {}
    assert final["state"] == "failed"
    assert final["error_code"] == errors.SESSION_CHANNEL_TIMEOUT


def test_without_channel_refuses_silent_local_copy(tmp_path: Path) -> None:
    """没有通道时不得静默本机复制, 返回明确错误码."""
    source = tmp_path / "local.bin"
    _make_file(source, 1024)
    service = FileTransferService(allow_local_staging=False)
    result = service.start_transfer(str(source), str(tmp_path / "inbox"))
    assert not result.ok
    assert result.error_code == errors.SESSION_CHANNEL_UNAVAILABLE
    assert not (tmp_path / "inbox" / source.name).exists()


def test_path_traversal_rejected(tmp_path: Path) -> None:
    """收端拒绝 .. 路径, 不在目标目录外创建文件."""
    receiver = FileTransferReceiver(tmp_path / "inbox")
    digest = "0" * 64
    reply = receiver.handle(
        "file_offer",
        {
            "session_id": "s",
            "transfer_id": "t",
            "items": [{"relative": "../evil.bin", "size": 4, "sha256": digest}],
            "total_bytes": 4,
            "chunk_size": CHUNK_SIZE,
            "digest_alg": "sha256",
            "digest": digest,
        },
    )
    assert reply is not None and reply["type"] == "file_abort"
    assert reply["payload"]["error_code"] == errors.FILE_OFFER_REJECTED
    assert not (tmp_path / "evil.bin").exists()
    assert not (tmp_path / "inbox" / "evil.bin").exists()


def _serve_holding_result(receiver: FileTransferReceiver, channel: LoopbackChannel) -> dict[str, Any] | None:
    """应答 offer/chunk, 但把 file_result 扣下不发给发送端, 用来验证完成判定."""
    held: dict[str, Any] | None = None
    for _ in range(1024):
        message = channel.receive(0)
        if message is None:
            break
        reply = receiver.handle(message[0], message[1])
        if reply is None:
            continue
        if reply["type"] == "file_result":
            held = reply
            continue
        channel.send(reply["type"], reply["payload"])
    return held


def test_sender_waits_for_file_result_before_completed(tmp_path: Path) -> None:
    """收端回执未到前停在 verifying; 回执失败时明确失败而不是一直挂着."""
    source = tmp_path / "wait.bin"
    _make_file(source, 4096)
    service, receiver, _sender, receiver_channel = _bind(tmp_path)
    started = service.start_transfer(str(source), str(tmp_path / "remote"))
    assert started.data is not None
    job_id = str(started.data["job_id"])

    held: dict[str, Any] | None = None
    progress: dict[str, Any] = {}
    for _ in range(2000):
        held = _serve_holding_result(receiver, receiver_channel) or held
        progress = service.get_transfer_progress(job_id).data or {}
        if progress.get("phase") == "verifying" and held is not None:
            break

    assert progress.get("state") == "running"
    assert progress.get("phase") == "verifying"
    assert held is not None and held["payload"]["verified"] is True

    state = service.remote_states[job_id]
    receiver_channel.send(
        "file_result",
        {
            "session_id": state.session_id,
            "transfer_id": state.transfer_id,
            "verified": False,
            "digest": "",
            "error_code": errors.TRANSFER_HASH_MISMATCH,
            "error_message": "整文件摘要不一致",
        },
    )
    final = service.get_transfer_progress(job_id).data or {}
    assert final["state"] == "failed"
    assert final["error_code"] == errors.TRANSFER_HASH_MISMATCH


def test_empty_file_completes(tmp_path: Path) -> None:
    """零字节文件也要在收端建出并计入整体摘要."""
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    service, receiver, _sender, receiver_channel = _bind(tmp_path)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "remote")).data["job_id"])
    final = _run(service, receiver, receiver_channel, job_id)
    assert final["state"] == "completed", (final.get("error_code"), final.get("error_message"))
    assert (tmp_path / "inbox" / "empty.txt").exists()


@pytest.mark.parametrize("resume", [0, 1])
def test_sender_soft_snapshot_fields(tmp_path: Path, resume: int) -> None:
    """进度快照带 remote/phase/acked_bytes, 便于界面显示真实阶段."""
    source = tmp_path / "snap.bin"
    _make_file(source, CHUNK_SIZE)
    service, _receiver, _sender, _receiver_channel = _bind(tmp_path)
    started = service.start_transfer(str(source), str(tmp_path / "remote"), rate_limit_kbps=resume)
    assert started.data is not None
    assert started.data["remote"] is True
    assert started.data["phase"] == "hashing"
    assert started.data["acked_bytes"] == 0
