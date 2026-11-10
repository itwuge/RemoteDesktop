"""剪贴板双向同步: 文本与图片、上限与截断、防环回与去重、日志不落明文."""

from __future__ import annotations

import base64
import binascii
import json

import pytest

from rdc.clipboard import protocol as clipboard_protocol
from rdc.clipboard.sync import (
    IMAGE_LIMIT_BYTES,
    TEXT_LIMIT_BYTES,
    ClipboardSyncService,
)
from rdc.core import errors
from rdc.core.logging import recent_events, reset_for_tests
from rdc.files.scope import SCOPE_INSUFFICIENT, SCOPE_INVALID, scope_covers
from rdc.signaling.messages import MESSAGE_TYPES


def _b64(payload: bytes) -> str:
    return base64.b64encode(payload).decode("ascii")


# ---------------------------------------------------------------- 双向与上限
def test_text_roundtrip_between_ends() -> None:
    """桌面推送, 安卓拉取; 内容与摘要一致."""
    service = ClipboardSyncService()
    pushed = service.push("desktop", "text", "你好, 远端")
    assert pushed.ok and pushed.data is not None
    assert pushed.data["applied"] is True

    pulled = service.pull("android")
    assert pulled.ok and pulled.data is not None
    assert pulled.data["text"] == "你好, 远端"
    assert pulled.data["digest"] == pushed.data["digest"]
    assert pulled.data["original_bytes"] == len("你好, 远端".encode())


def test_text_truncation_reports_notice() -> None:
    """超长文本按 UTF-8 字节截断, message 必须含「已截断」."""
    service = ClipboardSyncService()
    text = "汉" * (TEXT_LIMIT_BYTES // 3 + 10)
    result = service.push("desktop", "text", text)
    assert result.ok and result.data is not None
    assert result.data["truncated"] is True
    assert result.data["original_bytes"] > TEXT_LIMIT_BYTES
    assert result.data["size_bytes"] <= TEXT_LIMIT_BYTES
    assert "已截断" in result.message


def test_image_roundtrip() -> None:
    """图片以 base64 双向同步, 拉取端拿到同一载荷."""
    service = ClipboardSyncService()
    payload = _b64(b"png-bytes")
    assert service.push("desktop", "image", payload).ok
    pulled = service.pull("android")
    assert pulled.ok and pulled.data is not None
    assert pulled.data["image_b64"] == payload
    assert pulled.data["kind"] == "image"


def test_image_oversize_rejected_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片超过 4 MiB 上限返回 CLIPBOARD_TOO_LARGE, 不截断."""
    monkeypatch.setattr("rdc.clipboard.sync.IMAGE_LIMIT_BYTES", 16)
    service = ClipboardSyncService()
    result = service.push("desktop", "image", _b64(b"x" * 32))
    assert not result.ok
    assert result.error_code == errors.CLIPBOARD_TOO_LARGE
    assert "上限" in result.message
    assert service.pull("android").error_code == errors.CLIPBOARD_EMPTY
    assert IMAGE_LIMIT_BYTES > 0


def test_image_decode_error_and_unsupported_kind() -> None:
    """非法 base64 与不支持的类型返回契约错误码."""
    service = ClipboardSyncService()
    assert service.push("desktop", "image", "not-base64!!").error_code == errors.CLIPBOARD_DECODE_ERROR
    assert service.push("desktop", "audio", "x").error_code == errors.CLIPBOARD_UNSUPPORTED_KIND
    assert service.pull("android").error_code == errors.CLIPBOARD_EMPTY
    with pytest.raises(binascii.Error):
        base64.b64decode("not-base64!!", validate=True)


def test_disabled_switch_blocks_both_directions() -> None:
    """关闭后推送与拉取一律返回 CLIPBOARD_DISABLED."""
    service = ClipboardSyncService()
    assert service.push("desktop", "text", "x").ok
    off = service.set_enabled(False)
    assert off.ok and off.data["enabled"] is False
    assert service.push("desktop", "text", "y").error_code == errors.CLIPBOARD_DISABLED
    assert service.pull("android").error_code == errors.CLIPBOARD_DISABLED
    assert service.set_enabled(True).ok


# ---------------------------------------------------------------- 环回与去重
def test_pull_from_origin_is_skipped() -> None:
    """来源端自己拉取被跳过, applied=false 且 reason=loop."""
    service = ClipboardSyncService()
    service.push("desktop", "text", "loop-check")
    loop = service.pull("desktop")
    assert loop.ok and loop.data is not None
    assert loop.data["applied"] is False
    assert loop.data["reason"] == "loop"
    assert "环回" in loop.message


def test_remote_echo_is_suppressed_as_duplicate() -> None:
    """对端拉取后回推同一内容, 摘要相同且来源不同, 视为重复推送."""
    service = ClipboardSyncService()
    service.push("desktop", "text", "echo")
    assert service.pull("android").ok
    echo = service.push("android", "text", "echo")
    assert echo.ok and echo.data is not None
    assert echo.data["applied"] is False
    assert echo.data["reason"] == "duplicate"
    assert service.status().data["suppressed"] == 1


def test_same_bytes_different_kind_not_deduplicated() -> None:
    """文本与图片内容字节相同也不算重复, 避免跨类型误抑制."""
    service = ClipboardSyncService()
    raw = b"same-bytes"
    assert service.push("desktop", "text", raw.decode()).ok
    same_as_image = service.push("desktop", "image", _b64(raw))
    assert same_as_image.ok and same_as_image.data is not None
    assert same_as_image.data["applied"] is True


# ---------------------------------------------------------------- 授权范围
def test_view_scope_rejects_all_clipboard_commands() -> None:
    """view 授权一律拒绝: 推送、拉取、开关与状态都不放行, 也不留存内容."""
    service = ClipboardSyncService()
    assert service.set_scope("view").ok

    push = service.push("desktop", "text", "view-should-not-sync")
    assert not push.ok and push.error_code == SCOPE_INSUFFICIENT
    assert "control" in push.message
    assert service.latest is None

    for result in (service.pull("android"), service.status(), service.set_enabled(False)):
        assert not result.ok
        assert result.error_code == SCOPE_INSUFFICIENT
    assert service.enabled is True


def test_control_and_full_scope_allow_clipboard() -> None:
    """control 与 full 均放行; full 是 control 的超集."""
    assert scope_covers("full", "control") is True
    assert scope_covers("view", "control") is False

    control = ClipboardSyncService(scope="control")
    assert control.push("desktop", "text", "control-ok").ok
    full = ClipboardSyncService(scope="full")
    pushed = full.push("desktop", "text", "full-ok")
    assert pushed.ok
    assert full.pull("android").data["text"] == "full-ok"


def test_invalid_scope_returns_scope_invalid() -> None:
    """非法 scope 取值返回 SCOPE_INVALID, 不改变当前范围."""
    service = ClipboardSyncService()
    denied = service.set_scope("admin")
    assert not denied.ok and denied.error_code == SCOPE_INVALID
    assert service.scope == "control"
    assert service.push("desktop", "text", "still-control").ok


# ---------------------------------------------------------------- 日志红线
def test_clipboard_logs_never_contain_content() -> None:
    """文本与图片内容、base64 均不得出现在日志事件里."""
    reset_for_tests()
    service = ClipboardSyncService()
    secret_text = "SECRET-CLIPBOARD-TEXT"
    secret_image = _b64(b"SECRET-IMAGE-BYTES")
    service.push("desktop", "text", secret_text)
    service.push("desktop", "image", secret_image)
    service.pull("android")

    dumped = json.dumps(recent_events(200), ensure_ascii=False)
    assert secret_text not in dumped
    assert "SECRET-IMAGE-BYTES" not in dumped
    assert "clipboard.pushed" in dumped
    assert service.status().data["digest"] is not None
    assert "text" not in service.status().data


def test_protocol_messages_are_defined() -> None:
    """protocol-v1 v1.3 已定义 3 类剪贴板消息, 缺口清单为空."""
    missing = clipboard_protocol.missing_protocol_messages()
    assert missing == {}
    assert {"clipboard_update", "clipboard_request", "clipboard_ack"} <= MESSAGE_TYPES
    assert clipboard_protocol.protocol_ready() is True
