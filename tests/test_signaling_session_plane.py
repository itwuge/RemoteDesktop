"""会话数据面 10 类消息的校验与信令降级转发, 对应 protocol-v1 v1.6 3.1 节."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest

from rdc.core.errors import (
    CHUNK_CHECKSUM_MISMATCH,
    CLIPBOARD_DIGEST_MISMATCH,
    CLIPBOARD_TOO_LARGE,
    CLIPBOARD_UNSUPPORTED_KIND,
    FILE_CHUNK_INVALID,
    FILE_OFFER_REJECTED,
    SCOPE_INSUFFICIENT,
    TRANSFER_HASH_MISMATCH,
    TRANSFER_NOT_FOUND,
)
from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope
from rdc.signaling.server import PRESENCE_OFFLINE, InMemorySignalingServer, PresenceRecord, RelayGrantRecord
from rdc.signaling.session_plane import SessionPlaneTracker

SESSION = "33333333-3333-4333-8333-333333333333"
TRANSFER = "transfer-1"
TEXT = "剪贴板内容"
TEXT_BYTES = TEXT.encode("utf-8")
TEXT_DIGEST = hashlib.sha256(TEXT_BYTES).hexdigest()


def _offer_payload(size: int = 10, chunk_size: int = 4, **overrides) -> dict:
    payload = {
        "session_id": SESSION,
        "transfer_id": TRANSFER,
        "items": [{"relative": "a.txt", "size": size, "sha256": "a" * 64}],
        "total_bytes": size,
        "chunk_size": chunk_size,
        "digest_alg": "sha256",
        "digest": "b" * 64,
    }
    payload.update(overrides)
    return payload


def _accept_payload(**overrides) -> dict:
    payload = {"session_id": SESSION, "transfer_id": TRANSFER, "accepted_items": ["a.txt"]}
    payload.update(overrides)
    return payload


def _chunk_payload(raw: bytes = b"abcd", offset: int = 0, **overrides) -> dict:
    payload = {
        "session_id": SESSION,
        "transfer_id": TRANSFER,
        "file_index": 0,
        "offset": offset,
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(raw).decode("ascii"),
    }
    payload.update(overrides)
    return payload


def _clipboard_payload(content: str = TEXT, **overrides) -> dict:
    payload = {
        "session_id": SESSION,
        "kind": "text",
        "size_bytes": len(content.encode("utf-8")),
        "digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }
    payload.update(overrides)
    return payload


def _accepted_tracker() -> SessionPlaneTracker:
    tracker = SessionPlaneTracker()
    assert tracker.check("file_offer", _offer_payload()).ok
    assert tracker.check("file_accept", _accept_payload()).ok
    return tracker


# --------------------------------------------------------------- 文件传输语义


def test_file_flow_happy_path_and_duplicate_chunk_is_idempotent():
    tracker = _accepted_tracker()
    first = tracker.check("file_chunk", _chunk_payload(b"abcd", 0))
    assert first.ok
    assert tracker.check("file_chunk", _chunk_payload(b"abcd", 0)).suppress
    assert tracker.check(
        "file_ack", {"session_id": SESSION, "transfer_id": TRANSFER, "file_index": 0, "acked_bytes": 4}
    ).ok
    assert tracker.check("file_chunk", _chunk_payload(b"efghij", 4)).ok
    assert tracker.check(
        "file_complete", {"session_id": SESSION, "transfer_id": TRANSFER, "digest": "b" * 64}
    ).code == (TRANSFER_HASH_MISMATCH)
    tracker.check("file_ack", {"session_id": SESSION, "transfer_id": TRANSFER, "file_index": 0, "acked_bytes": 10})
    assert tracker.check("file_complete", {"session_id": SESSION, "transfer_id": TRANSFER, "digest": "b" * 64}).ok
    after_complete = tracker.check("file_chunk", _chunk_payload(b"zz", 10))
    assert after_complete.code == FILE_CHUNK_INVALID
    assert tracker.snapshot()["duplicate_chunks"] == 1


def test_file_chunk_rejects_offset_and_checksum():
    tracker = _accepted_tracker()
    assert tracker.check("file_chunk", _chunk_payload(b"abcd", 2)).code == FILE_CHUNK_INVALID
    bad_digest = _chunk_payload(b"abcd", 0, sha256="c" * 64)
    assert tracker.check("file_chunk", bad_digest).code == CHUNK_CHECKSUM_MISMATCH


def test_file_chunk_rejects_inflight_over_4_mib():
    tracker = SessionPlaneTracker()
    files = [{"relative": f"f{index}", "size": 256 * 1024, "sha256": "a" * 64} for index in range(17)]
    offer = _offer_payload(items=files, total_bytes=17 * 256 * 1024, chunk_size=256 * 1024)
    assert tracker.check("file_offer", offer).ok
    assert tracker.check("file_accept", _accept_payload(accepted_items=[item["relative"] for item in files])).ok
    raw = b"x" * (256 * 1024)
    for index in range(16):
        chunk = _chunk_payload(raw, 0, file_index=index)
        assert tracker.check("file_chunk", chunk).ok
    overflow = tracker.check("file_chunk", _chunk_payload(raw, 0, file_index=16))
    assert overflow.code == FILE_CHUNK_INVALID


def test_file_ack_cannot_roll_back_or_exceed_sent():
    tracker = _accepted_tracker()
    assert tracker.check("file_chunk", _chunk_payload(b"abcd", 0)).ok
    ack = {"session_id": SESSION, "transfer_id": TRANSFER, "file_index": 0, "acked_bytes": 4}
    assert tracker.check("file_ack", ack).ok
    assert tracker.check("file_ack", {**ack, "acked_bytes": 2}).code == FILE_CHUNK_INVALID
    assert tracker.check("file_ack", {**ack, "acked_bytes": 8}).code == FILE_CHUNK_INVALID


@pytest.mark.parametrize(
    "payload",
    [
        _offer_payload(items=[]),
        _offer_payload(total_bytes=99),
        _offer_payload(chunk_size=0),
        _offer_payload(items=[{"relative": "a.txt", "size": 4 * 1024**3 + 1, "sha256": "a" * 64}]),
        _offer_payload(digest_alg="md5"),
        _offer_payload(
            items=[{"relative": f"f{index}", "size": 0, "sha256": "a" * 64} for index in range(4097)],
            total_bytes=0,
        ),
    ],
)
def test_file_offer_rejects_out_of_contract_payloads(payload):
    assert SessionPlaneTracker().check("file_offer", payload).code == FILE_OFFER_REJECTED


def test_messages_before_offer_return_transfer_not_found():
    tracker = SessionPlaneTracker()
    assert tracker.check("file_accept", _accept_payload()).code == TRANSFER_NOT_FOUND
    assert tracker.check("file_chunk", _chunk_payload()).code == TRANSFER_NOT_FOUND


def test_file_abort_is_terminal_and_idempotent():
    tracker = _accepted_tracker()
    abort = {"session_id": SESSION, "transfer_id": TRANSFER, "reason": "用户取消"}
    assert tracker.check("file_abort", abort).ok
    assert tracker.check("file_chunk", _chunk_payload()).code == FILE_CHUNK_INVALID


def test_file_accept_resume_starts_from_received_point():
    """file_accept 带回续传偏移后, 块必须从该偏移开始."""
    tracker = SessionPlaneTracker()
    assert tracker.check("file_offer", _offer_payload(size=10)).ok
    resume = [{"relative": "a.txt", "received_bytes": 4}]
    assert tracker.check("file_accept", _accept_payload(resume=resume)).ok
    assert tracker.check("file_chunk", _chunk_payload(b"ef", 0)).code == FILE_CHUNK_INVALID
    assert tracker.check("file_chunk", _chunk_payload(b"ef", 4)).ok


def test_file_accept_rejects_invalid_resume():
    tracker = SessionPlaneTracker()
    assert tracker.check("file_offer", _offer_payload(size=10)).ok
    too_far = [{"relative": "a.txt", "received_bytes": 11}]
    assert tracker.check("file_accept", _accept_payload(resume=too_far)).code == FILE_OFFER_REJECTED
    unknown = [{"relative": "b.txt", "received_bytes": 1}]
    assert tracker.check("file_accept", _accept_payload(resume=unknown)).code == FILE_OFFER_REJECTED


def test_file_result_confirms_or_fails_completed_transfer():
    """file_result 只有完成之后才被接受; verified=true 与摘要一致后进入最终态."""
    tracker = _accepted_tracker()
    assert tracker.check("file_chunk", _chunk_payload(b"abcdefghij", 0)).ok
    ack = {"session_id": SESSION, "transfer_id": TRANSFER, "file_index": 0, "acked_bytes": 10}
    assert tracker.check("file_ack", ack).ok
    complete = {"session_id": SESSION, "transfer_id": TRANSFER, "digest": "b" * 64}
    assert tracker.check("file_complete", complete).ok
    mismatch = {**complete, "verified": True, "digest": "c" * 64}
    assert tracker.check("file_result", mismatch).code == TRANSFER_HASH_MISMATCH
    result = {"session_id": SESSION, "transfer_id": TRANSFER, "verified": True, "digest": "b" * 64}
    assert tracker.check("file_result", result).ok
    assert tracker.check("file_result", result).suppress


def test_file_result_failure_requires_error_code():
    tracker = _accepted_tracker()
    assert tracker.check("file_chunk", _chunk_payload(b"abcdefghij", 0)).ok
    ack = {"session_id": SESSION, "transfer_id": TRANSFER, "file_index": 0, "acked_bytes": 10}
    assert tracker.check("file_ack", ack).ok
    assert tracker.check("file_complete", {**ack, "digest": "b" * 64}).ok
    failed = {"session_id": SESSION, "transfer_id": TRANSFER, "verified": False, "digest": ""}
    assert tracker.check("file_result", failed).code == TRANSFER_HASH_MISMATCH
    assert tracker.check("file_result", {**failed, "error_code": "TRANSFER_HASH_MISMATCH"}).ok


# --------------------------------------------------------------- 剪贴板语义


def test_clipboard_update_dedup_and_digest_mismatch():
    tracker = SessionPlaneTracker()
    assert tracker.check("clipboard_update", _clipboard_payload()).ok
    assert tracker.check("clipboard_update", _clipboard_payload()).suppress
    assert tracker.check("clipboard_update", _clipboard_payload(digest="d" * 64)).code == CLIPBOARD_DIGEST_MISMATCH
    assert tracker.check("clipboard_update", _clipboard_payload(kind="rtf")).code == CLIPBOARD_UNSUPPORTED_KIND
    assert tracker.check("clipboard_request", {"session_id": SESSION, "kind": "image"}).ok
    oversize = _clipboard_payload("a" * (256 * 1024 + 1))
    assert tracker.check("clipboard_update", oversize).code == CLIPBOARD_TOO_LARGE


def test_clipboard_ack_requires_boolean_applied():
    tracker = SessionPlaneTracker()
    ack = {"session_id": SESSION, "kind": "text", "digest": TEXT_DIGEST, "applied": "no"}
    assert tracker.check("clipboard_ack", ack).code == CLIPBOARD_DIGEST_MISMATCH
    assert tracker.check("clipboard_ack", {**ack, "applied": False}).ok


# --------------------------------------------------------------- 信令降级转发


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


def _pair(server: InMemorySignalingServer) -> tuple[DeviceIdentity, DeviceIdentity]:
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    _authenticate(server, alice)
    _authenticate(server, bob)
    offer = build_envelope(
        alice,
        "offer",
        {
            "session_id": SESSION,
            "candidates": [],
            "media_caps": {},
            "access_proof": "cHJvb2Y=",
            "from_device_id": alice.device_id,
            "to_device_id": bob.device_id,
        },
        session_id=SESSION,
    )
    assert server.handle(offer, alice.device_id) == [] or server.routed[-1].msg_type == "offer"
    answer = build_envelope(
        bob,
        "answer",
        {
            "session_id": SESSION,
            "chosen_candidate": {"kind": "host", "address": "127.0.0.1", "port": 5001, "priority": 1},
            "media_caps": {"codecs": ["h264"]},
            "from_device_id": bob.device_id,
            "to_device_id": alice.device_id,
        },
        session_id=SESSION,
    )
    assert server.handle(answer, bob.device_id) == [] or server.routed[-1].msg_type == "answer"
    return alice, bob


def test_session_plane_messages_route_to_peer_and_dedup():
    deliveries: list[tuple[str, dict]] = []
    server = InMemorySignalingServer(
        deliver=lambda device_id, envelope: deliveries.append((device_id, envelope)) or True
    )
    alice, bob = _pair(server)

    offer = build_envelope(alice, "file_offer", _offer_payload(), session_id=SESSION)
    assert server.handle(offer, alice.device_id) == []
    assert deliveries[-1][0] == bob.device_id
    assert deliveries[-1][1]["type"] == "file_offer"

    accept = build_envelope(bob, "file_accept", _accept_payload(), session_id=SESSION)
    assert server.handle(accept, bob.device_id) == []
    assert deliveries[-1][0] == alice.device_id

    chunk = build_envelope(alice, "file_chunk", _chunk_payload(), session_id=SESSION)
    assert server.handle(chunk, alice.device_id) == []
    assert deliveries[-1][1]["type"] == "file_chunk"
    count = len(deliveries)
    assert server.handle(chunk, alice.device_id) == []
    assert len(deliveries) == count

    update = build_envelope(alice, "clipboard_update", _clipboard_payload(), session_id=SESSION)
    assert server.handle(update, alice.device_id) == []
    assert deliveries[-1][1]["type"] == "clipboard_update"
    count = len(deliveries)
    assert server.handle(update, alice.device_id) == []
    assert len(deliveries) == count
    assert server.session_plane.snapshot()["duplicate_chunks"] == 1


def test_session_plane_rejects_view_scope_and_unpaired_peer():
    server = InMemorySignalingServer()
    alice, bob = _pair(server)
    server.relay_grants["view-token"] = RelayGrantRecord(
        session_id=SESSION,
        budget_kbps=1380,
        expires_at=time.time() + 60,
        device_id=alice.device_id,
        scope="view",
    )
    offer = build_envelope(alice, "file_offer", _offer_payload(), session_id=SESSION)
    responses = server.handle(offer, alice.device_id)
    assert responses[0]["payload"]["code"] == SCOPE_INSUFFICIENT

    other = "44444444-4444-4444-8444-444444444444"
    lonely = build_envelope(
        bob,
        "clipboard_request",
        {"session_id": other, "kind": "text"},
        session_id=other,
    )
    responses = server.handle(lonely, bob.device_id)
    assert responses[0]["payload"]["code"] == "PEER_OFFLINE"


def test_presence_sink_receives_online_then_offline_once():
    events: list[tuple[str, str]] = []
    server = InMemorySignalingServer(presence_sink=lambda device_id, state: events.append((device_id, state)))
    alice = DeviceIdentity.generate()
    _authenticate(server, alice)
    presence = build_envelope(alice, "presence", {"device_id": alice.device_id, "state": "online"})
    assert server.handle(presence, alice.device_id) == []
    assert server.handle(presence, alice.device_id) == []
    assert events == [(alice.device_id, "online")]

    server.presence[alice.device_id] = PresenceRecord(last_seen_at=time.time() - 91, state_hint="online")
    heartbeat = build_envelope(alice, "heartbeat", {"session_id": SESSION, "seq": 1}, session_id=SESSION)
    assert server.handle(heartbeat, alice.device_id) == []
    assert events[-1] == (alice.device_id, PRESENCE_OFFLINE)
    assert server.handle(heartbeat, alice.device_id) == []
    assert events.count((alice.device_id, PRESENCE_OFFLINE)) == 1
