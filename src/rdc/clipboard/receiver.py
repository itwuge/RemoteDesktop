# ==========================================================================================
# 文件        : receiver.py
# 所属目录    : src/rdc/clipboard/
# 文件用途    : 剪贴板收端: 校验 clipboard_update、去重、回 ack、应答 clipboard_request
# 架构定位    : 可被 host 脚本直接 import 的纯业务入口, 与界面和系统剪贴板解耦
# 核心功能    : 文本/图片大小与摘要校验、同摘要去重、最近内容缓存
# 职责边界    : 不建连; 不读写系统剪贴板; 不落盘; 不记录明文
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.clipboard.sync 的上限常量; rdc.core.errors
# ==========================================================================================

"""剪贴板收端."""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdc.clipboard.sync import CLIPBOARD_KINDS, IMAGE_LIMIT_BYTES, TEXT_LIMIT_BYTES
from rdc.core.errors import CLIPBOARD_DIGEST_MISMATCH, CLIPBOARD_EMPTY, CLIPBOARD_TOO_LARGE
from rdc.core.logging import log_event


@dataclass
class ClipboardReceiver:
    """收端状态机; 返回待回发消息, 由会话循环发送."""

    on_apply: Callable[[str, str], None] | None = None
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_digest: str = ""
    last_kind: str = ""
    applied: int = 0
    suppressed: int = 0

    def handle(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条会话消息; 失败通过 error 帧表达."""
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            return None
        if msg_type == "clipboard_update":
            return self._on_update(session_id, payload)
        if msg_type == "clipboard_request":
            return self._on_request(session_id, payload)
        return None

    def set_local(self, kind: str, content: str) -> None:
        """记录本端系统剪贴板内容, 供对端 clipboard_request 应答."""
        raw = _raw_content(kind, content)
        if raw is None:
            return
        digest = hashlib.sha256(raw).hexdigest()
        self.latest[kind] = {"content": content, "digest": digest, "size_bytes": len(raw)}
        self.last_digest = digest
        self.last_kind = kind

    def _on_update(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or "")
        content = str(payload.get("content") or "")
        digest = str(payload.get("digest") or "")
        if kind not in CLIPBOARD_KINDS:
            return _error(session_id, "CLIPBOARD_UNSUPPORTED_KIND", "只支持文本与图片两种剪贴板内容")
        raw = _raw_content(kind, content)
        if raw is None:
            return _error(session_id, "CLIPBOARD_DECODE_ERROR", "剪贴板内容无法解码")
        limit = TEXT_LIMIT_BYTES if kind == "text" else IMAGE_LIMIT_BYTES
        if len(raw) > limit:
            return _error(session_id, CLIPBOARD_TOO_LARGE, "剪贴板内容超过大小上限")
        if len(raw) != payload.get("size_bytes"):
            return _error(session_id, CLIPBOARD_DIGEST_MISMATCH, "剪贴板长度与声明不一致")
        actual = hashlib.sha256(raw).hexdigest()
        if actual != digest:
            return _error(session_id, CLIPBOARD_DIGEST_MISMATCH, "剪贴板摘要不一致")
        if digest == self.last_digest:
            self.suppressed += 1
            log_event("clipboard.receive", "clipboard.duplicate", kind=kind, size_bytes=len(raw))
            return _ack(session_id, kind, digest, applied=False)
        self.latest[kind] = {"content": content, "digest": digest, "size_bytes": len(raw)}
        self.last_digest = digest
        self.last_kind = kind
        self.applied += 1
        log_event("clipboard.receive", "clipboard.applied", kind=kind, size_bytes=len(raw), digest=digest)
        if self.on_apply is not None:
            self.on_apply(kind, content)
        return _ack(session_id, kind, digest, applied=True)

    def _on_request(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        kind = str(payload.get("kind") or self.last_kind or "text")
        item = self.latest.get(kind)
        if item is None:
            return _error(session_id, CLIPBOARD_EMPTY, "对端剪贴板暂无内容")
        return {
            "type": "clipboard_update",
            "payload": {
                "session_id": session_id,
                "kind": kind,
                "content": item["content"],
                "size_bytes": item["size_bytes"],
                "digest": item["digest"],
            },
        }


def _raw_content(kind: str, content: str) -> bytes | None:
    if kind == "text":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        return None


def _ack(session_id: str, kind: str, digest: str, *, applied: bool) -> dict[str, Any]:
    return {
        "type": "clipboard_ack",
        "payload": {
            "session_id": session_id,
            "kind": kind,
            "digest": digest,
            "applied": applied,
        },
    }


def _error(session_id: str, code: str, message: str) -> dict[str, Any]:
    return {"type": "error", "payload": {"session_id": session_id, "code": code, "message": message}}
