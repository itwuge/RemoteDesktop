"""剪贴板通道模式: 双向同步、对端确认前不算已同步、去重、拉取、开关与解绑."""

from __future__ import annotations

import base64
import hashlib

from rdc.clipboard.receiver import ClipboardReceiver
from rdc.clipboard.sync import ClipboardSyncService
from rdc.core.errors import CLIPBOARD_DISABLED
from rdc.session.channel import LoopbackChannel


def _pair() -> tuple[ClipboardSyncService, ClipboardSyncService]:
    left, right = LoopbackChannel.pair("session-clip")
    desktop = ClipboardSyncService()
    android = ClipboardSyncService()
    desktop.bind_channel(left)
    android.bind_channel(right)
    return desktop, android


def _status(service: ClipboardSyncService) -> dict[str, object]:
    data = service.status().data
    assert data is not None
    return data


# ---------------------------------------------------------------- 绑定与状态
def test_bind_channel_reports_visibility() -> None:
    """绑定后 mode=channel; 环路不可见中继, 真实中继由通道自报."""
    desktop, _ = _pair()
    data = _status(desktop)
    assert data["mode"] == "channel"
    assert data["bound"] is True
    assert data["visible_to_relay"] is False


def test_unbound_service_stays_local() -> None:
    """未绑定通道时保持内存模式且不发送."""
    service = ClipboardSyncService()
    pushed = service.push("desktop", "text", "仅本机")
    assert pushed.ok and pushed.data is not None
    assert pushed.data.get("pending") in (None, False)
    assert _status(service)["mode"] == "local"


# ---------------------------------------------------------------- 双向
def test_push_requires_peer_ack() -> None:
    """发送后只是待确认, 对端回 ack 后才算已同步."""
    desktop, android = _pair()
    pushed = desktop.push("desktop", "text", "双向同步内容")
    assert pushed.ok and pushed.data is not None
    assert pushed.data["pending"] is True

    before = _status(desktop)
    assert before["pending"] is True
    assert before["confirmed"] is False

    assert android.poll_channel() == 1
    assert _status(android)["confirmed_action"] == "push"
    assert android.receiver is not None
    assert android.receiver.latest["text"]["content"] == "双向同步内容"

    assert desktop.poll_channel() == 1
    after = _status(desktop)
    assert after["pending"] is False
    assert after["confirmed"] is True
    assert after["confirmed_applied"] is True
    assert after["confirmed_digest"] == pushed.data["digest"]


def test_bidirectional_roundtrip() -> None:
    """两端互相推送, 各自收到对方内容."""
    desktop, android = _pair()
    assert desktop.push("desktop", "text", "来自桌面").ok
    assert android.poll_channel() == 1
    assert desktop.poll_channel() == 1

    assert android.push("android", "text", "来自安卓").ok
    assert desktop.poll_channel() == 1
    assert android.poll_channel() == 1

    assert desktop.receiver is not None and android.receiver is not None
    assert desktop.receiver.latest["text"]["content"] == "来自安卓"
    assert android.receiver.latest["text"]["content"] == "来自桌面"


def test_duplicate_push_is_suppressed() -> None:
    """同内容重复推送不再发第二帧."""
    desktop, android = _pair()
    assert desktop.push("desktop", "text", "重复内容").ok
    second = desktop.push("desktop", "text", "重复内容")
    assert second.ok and second.data is not None
    assert second.data["applied"] is False
    assert second.data["reason"] in {"duplicate", "loop"}
    assert android.poll_channel() == 1
    assert android.receiver is not None
    assert android.receiver.applied == 1


def test_image_push_is_acked() -> None:
    """图片按 base64 下发, 收端解码后回 ack."""
    desktop, android = _pair()
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16).decode("ascii")
    pushed = desktop.push("desktop", "image", payload)
    assert pushed.ok and pushed.data is not None
    assert android.poll_channel() == 1
    assert desktop.poll_channel() == 1
    assert _status(desktop)["confirmed"] is True
    assert android.receiver is not None
    assert android.receiver.latest["image"]["size_bytes"] > 0


# ---------------------------------------------------------------- 拉取
def test_pull_gets_peer_content() -> None:
    """拉取向对端发 request, 回传后本地按远端内容记录."""
    desktop, android = _pair()
    assert android.receiver is not None
    android.receiver.set_local("text", "远端已有内容")

    pulled = desktop.pull("desktop")
    assert pulled.ok and pulled.data is not None
    assert pulled.data["pending"] is True

    assert android.poll_channel() == 1
    assert desktop.poll_channel() == 1
    data = _status(desktop)
    assert data["confirmed_action"] == "pull"
    assert data["digest"] == hashlib.sha256("远端已有内容".encode()).hexdigest()
    assert desktop.latest is not None
    assert desktop.latest["text"] == "远端已有内容"
    assert desktop.latest["origin"] == "remote"


def test_pull_without_peer_content_marks_error() -> None:
    """对端没有内容时回错误帧, 发送端记录明确原因且不伪装成功."""
    desktop, android = _pair()
    assert desktop.pull("desktop").ok
    assert android.poll_channel() == 1
    assert desktop.poll_channel() == 1
    data = _status(desktop)
    assert data["pending"] is False
    assert data["confirmed"] is False
    assert "CLIPBOARD_EMPTY" in str(data["last_error"])


# ---------------------------------------------------------------- 开关与脱敏
def test_disabled_service_sends_nothing() -> None:
    """关闭同步后 push 直接拒绝, 通道无帧."""
    desktop, android = _pair()
    assert desktop.set_enabled(False).ok
    result = desktop.push("desktop", "text", "不应发送")
    assert result.ok is False
    assert result.error_code == CLIPBOARD_DISABLED
    assert android.poll_channel() == 0


def test_status_never_contains_plaintext() -> None:
    """状态只给摘要与长度, 不回显正文."""
    desktop, _ = _pair()
    assert desktop.push("desktop", "text", "敏感明文内容").ok
    data = _status(desktop)
    assert "text" not in data
    assert "image_b64" not in data
    assert data["digest"]


# ---------------------------------------------------------------- 收端校验
def test_receiver_rejects_digest_mismatch() -> None:
    """摘要不符时回 error, 不写入 latest."""
    receiver = ClipboardReceiver()
    reply = receiver.handle(
        "clipboard_update",
        {"session_id": "s", "kind": "text", "content": "abc", "size_bytes": 3, "digest": "0" * 64},
    )
    assert reply is not None
    assert reply["type"] == "error"
    assert receiver.latest == {}


def test_receiver_deduplicates_same_digest() -> None:
    """同摘要重复到达只应用一次, 第二次回 applied=false."""
    receiver = ClipboardReceiver()
    digest = hashlib.sha256(b"abc").hexdigest()
    payload = {"session_id": "s", "kind": "text", "content": "abc", "size_bytes": 3, "digest": digest}
    first = receiver.handle("clipboard_update", dict(payload))
    second = receiver.handle("clipboard_update", dict(payload))
    assert first is not None and first["payload"]["applied"] is True
    assert second is not None and second["payload"]["applied"] is False
    assert receiver.applied == 1
    assert receiver.suppressed == 1
