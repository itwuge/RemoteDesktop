# ==========================================================================================
# 文件        : session_plane.py
# 所属目录    : src/rdc/signaling/
# 文件用途    : 会话数据面 9 类消息(file_*/clipboard_*)的语义校验与去重, 对应 protocol-v1 v1.3 3.1 节
# 架构定位    : 协议层校验器, 不做网络收发, 不解析或记录消息内容
# 核心功能    : 必填字段之外的类型/大小上限/顺序状态机/块幂等/剪贴板去重与摘要校验
# 职责边界    : 不落盘; 不打印内容与摘要; 不替代端到端会话密钥解密
# ==========================================================================================

"""会话数据面消息校验.

契约依据: docs/contracts/protocol-v1.md 第三节 v1.3 与 3.1 节.
- 单块 ≤ 256 KiB, 未确认在途 ≤ 4 MiB, 单文件 ≤ 4 GiB, 单次条目 ≤ 4096.
- 顺序: file_offer → file_accept → (file_chunk → file_ack)* → file_complete; file_abort 任意阶段终止.
- 重复块幂等; acked_bytes 只增不减; 剪贴板同 digest 去重, 摘要不一致丢弃.

契约把 9 类消息划归会话数据面, 中继数据面按不透明密文转发. 本模块供信令侧
降级转发通道与联调入口做边界校验, 不读取内容落日志, 摘要只用于内存比对.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from typing import Any

from rdc.core.errors import (
    CHUNK_CHECKSUM_MISMATCH,
    CLIPBOARD_DECODE_ERROR,
    CLIPBOARD_DIGEST_MISMATCH,
    CLIPBOARD_EMPTY,
    CLIPBOARD_TOO_LARGE,
    CLIPBOARD_UNSUPPORTED_KIND,
    FILE_CHUNK_INVALID,
    FILE_OFFER_REJECTED,
    TRANSFER_HASH_MISMATCH,
    TRANSFER_NOT_FOUND,
)

FILE_MAX_ITEMS = 4096
FILE_MAX_BYTES = 4 * 1024**3
CHUNK_MAX_BYTES = 256 * 1024
INFLIGHT_MAX_BYTES = 4 * 1024 * 1024
CLIPBOARD_TEXT_MAX_BYTES = 256 * 1024
CLIPBOARD_IMAGE_MAX_BYTES = 4 * 1024 * 1024
DIGEST_ALGORITHM = "sha256"
DIGEST_HEX_LEN = 64
CLIPBOARD_KINDS = ("text", "image")


@dataclass(frozen=True)
class Validation:
    """一次校验结果; code 为空表示通过, suppress 表示重复消息不再转发."""

    code: str = ""
    message: str = ""
    suppress: bool = False

    @property
    def ok(self) -> bool:
        return not self.code


PASS = Validation()


@dataclass
class TransferState:
    """单次文件传输的协议状态, 只保存相对路径、字节数与摘要, 不保存文件内容."""

    session_id: str
    transfer_id: str
    items: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    chunk_size: int = CHUNK_MAX_BYTES
    digest_alg: str = DIGEST_ALGORITHM
    state: str = "offered"
    accepted: list[str] = field(default_factory=list)
    acked: dict[int, int] = field(default_factory=dict)
    sent: dict[int, int] = field(default_factory=dict)
    seen: dict[tuple[int, int], str] = field(default_factory=dict)


class SessionPlaneTracker:
    """按会话维度维护文件传输与剪贴板去重状态."""

    def __init__(self) -> None:
        self._transfers: dict[tuple[str, str], TransferState] = {}
        self._clipboard: dict[tuple[str, str], str] = {}
        self.accepted_chunks = 0
        self.duplicate_chunks = 0
        self.suppressed_updates = 0

    def check(self, msg_type: str, payload: dict[str, Any]) -> Validation:
        """校验一条会话数据面消息, 通过时返回 PASS."""
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return Validation(FILE_CHUNK_INVALID, "缺少 session_id")
        handler = getattr(self, f"_check_{msg_type}", None)
        if handler is None:
            return Validation(FILE_CHUNK_INVALID, f"未支持的会话数据面类型 {msg_type}")
        return handler(session_id, payload)

    def forget_session(self, session_id: str) -> None:
        """会话结束时清理全部传输与剪贴板状态."""
        for key in [key for key in self._transfers if key[0] == session_id]:
            self._transfers.pop(key, None)
        for key in [key for key in self._clipboard if key[0] == session_id]:
            self._clipboard.pop(key, None)

    def snapshot(self) -> dict[str, int]:
        """返回可公开的计数, 不含路径、内容与摘要."""
        return {
            "transfers": len(self._transfers),
            "accepted_chunks": self.accepted_chunks,
            "duplicate_chunks": self.duplicate_chunks,
            "suppressed_updates": self.suppressed_updates,
        }

    # ------------------------------------------------------------------ 文件传输
    def _check_file_offer(self, session_id: str, payload: dict[str, Any]) -> Validation:
        transfer_id = _text(payload, "transfer_id")
        items = payload.get("items")
        if not transfer_id:
            return Validation(FILE_OFFER_REJECTED, "缺少 transfer_id")
        if not isinstance(items, list) or not items:
            return Validation(FILE_OFFER_REJECTED, "items 必须是非空列表")
        if len(items) > FILE_MAX_ITEMS:
            return Validation(FILE_OFFER_REJECTED, "单次条目超过 4096")
        sizes: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                return Validation(FILE_OFFER_REJECTED, "items 每项必须是对象")
            relative = item.get("relative")
            size = item.get("size")
            if not isinstance(relative, str) or not relative:
                return Validation(FILE_OFFER_REJECTED, "relative 必须是非空字符串")
            if relative in sizes:
                return Validation(FILE_OFFER_REJECTED, "relative 不得重复")
            size_value = _as_int(size)
            if size_value is None or size_value < 0 or size_value > FILE_MAX_BYTES:
                return Validation(FILE_OFFER_REJECTED, "单文件大小超出 0..4 GiB")
            if not _is_hex(item.get("sha256")):
                return Validation(FILE_OFFER_REJECTED, "sha256 必须是 64 位十六进制")
            sizes[relative] = size_value
        total = _as_int(payload.get("total_bytes"))
        if total is None or total < 0 or total != sum(sizes.values()):
            return Validation(FILE_OFFER_REJECTED, "total_bytes 必须等于各文件大小之和")
        chunk_size = _as_int(payload.get("chunk_size"))
        if chunk_size is None or not 1 <= chunk_size <= CHUNK_MAX_BYTES:
            return Validation(FILE_OFFER_REJECTED, "chunk_size 必须在 1..256 KiB")
        if payload.get("digest_alg") != DIGEST_ALGORITHM:
            return Validation(FILE_OFFER_REJECTED, "digest_alg 只支持 sha256")
        if "resume" in payload and not _valid_resume(payload["resume"], sizes):
            return Validation(FILE_OFFER_REJECTED, "resume 与 items 不一致")

        key = (session_id, transfer_id)
        existing = self._transfers.get(key)
        if existing is not None and existing.state != "aborted":
            if existing.items == sizes and existing.total_bytes == total and existing.chunk_size == chunk_size:
                return Validation(suppress=True)
            return Validation(FILE_OFFER_REJECTED, "同一 transfer_id 的条目不一致")
        self._transfers[key] = TransferState(
            session_id=session_id,
            transfer_id=transfer_id,
            items=sizes,
            total_bytes=total,
            chunk_size=chunk_size,
        )
        return PASS

    def _check_file_accept(self, session_id: str, payload: dict[str, Any]) -> Validation:
        state = self._transfer(session_id, payload)
        if state is None:
            return Validation(TRANSFER_NOT_FOUND, "传输不存在")
        if state.state != "offered":
            return Validation(FILE_OFFER_REJECTED, "当前状态不接受 file_accept")
        accepted = payload.get("accepted_items")
        if not isinstance(accepted, list) or not accepted:
            return Validation(FILE_OFFER_REJECTED, "accepted_items 必须是非空列表")
        chosen: list[str] = []
        for relative in accepted:
            if not isinstance(relative, str) or relative not in state.items or relative in chosen:
                return Validation(FILE_OFFER_REJECTED, "accepted_items 必须是 items 的非重复子集")
            chosen.append(relative)
        state.accepted = chosen
        state.state = "accepted"
        for index in range(len(chosen)):
            state.acked[index] = 0
            state.sent[index] = 0
        return PASS

    def _check_file_chunk(self, session_id: str, payload: dict[str, Any]) -> Validation:
        state = self._transfer(session_id, payload)
        if state is None:
            return Validation(TRANSFER_NOT_FOUND, "传输不存在")
        if state.state in ("completed", "aborted"):
            return Validation(FILE_CHUNK_INVALID, "传输已结束, 不接受文件块")
        if state.state != "accepted":
            return Validation(FILE_CHUNK_INVALID, "尚未接受传输")
        file_index = _as_int(payload.get("file_index"))
        offset = _as_int(payload.get("offset"))
        length = _as_int(payload.get("length"))
        if file_index is None or not 0 <= file_index < len(state.accepted):
            return Validation(FILE_CHUNK_INVALID, "file_index 越界")
        if offset is None or offset < 0 or length is None or not 1 <= length <= CHUNK_MAX_BYTES:
            return Validation(FILE_CHUNK_INVALID, "offset 或 length 不合法")
        raw = _decode_b64(payload.get("data"))
        if raw is None or len(raw) != length:
            return Validation(FILE_CHUNK_INVALID, "data 与 length 不一致")
        digest = payload.get("sha256")
        if not _is_hex(digest):
            return Validation(FILE_CHUNK_INVALID, "sha256 必须是 64 位十六进制")
        if hashlib.sha256(raw).hexdigest() != digest:
            return Validation(CHUNK_CHECKSUM_MISMATCH, "块摘要与内容不一致")

        relative = state.accepted[file_index]
        size = state.items[relative]
        if offset + length > size:
            return Validation(FILE_CHUNK_INVALID, "块越过文件末尾")
        key = (file_index, offset)
        if key in state.seen:
            if state.seen[key] == digest:
                self.duplicate_chunks += 1
                return Validation(suppress=True)
            return Validation(FILE_CHUNK_INVALID, "同一位置出现不同摘要的块")
        if offset != state.acked.get(file_index, 0):
            return Validation(FILE_CHUNK_INVALID, "offset 必须等于该文件已确认字节")
        inflight = sum(state.sent.values()) - sum(state.acked.values())
        if inflight + length > INFLIGHT_MAX_BYTES:
            return Validation(FILE_CHUNK_INVALID, "未确认在途超过 4 MiB")
        state.sent[file_index] = state.sent.get(file_index, 0) + length
        state.seen[key] = digest
        self.accepted_chunks += 1
        return PASS

    def _check_file_ack(self, session_id: str, payload: dict[str, Any]) -> Validation:
        state = self._transfer(session_id, payload)
        if state is None:
            return Validation(TRANSFER_NOT_FOUND, "传输不存在")
        if state.state != "accepted":
            return Validation(FILE_CHUNK_INVALID, "当前状态不接受 file_ack")
        file_index = _as_int(payload.get("file_index"))
        acked_bytes = _as_int(payload.get("acked_bytes"))
        if file_index is None or not 0 <= file_index < len(state.accepted):
            return Validation(FILE_CHUNK_INVALID, "file_index 越界")
        size = state.items[state.accepted[file_index]]
        if acked_bytes is None or acked_bytes < 0 or acked_bytes > size:
            return Validation(FILE_CHUNK_INVALID, "acked_bytes 超出文件范围")
        if acked_bytes < state.acked.get(file_index, 0):
            return Validation(FILE_CHUNK_INVALID, "acked_bytes 不得回退")
        if acked_bytes > state.sent.get(file_index, 0):
            return Validation(FILE_CHUNK_INVALID, "acked_bytes 超过已发送字节")
        state.acked[file_index] = acked_bytes
        return PASS

    def _check_file_complete(self, session_id: str, payload: dict[str, Any]) -> Validation:
        state = self._transfer(session_id, payload)
        if state is None:
            return Validation(TRANSFER_NOT_FOUND, "传输不存在")
        if state.state != "accepted":
            return Validation(FILE_CHUNK_INVALID, "当前状态不接受 file_complete")
        for file_index, relative in enumerate(state.accepted):
            if state.acked.get(file_index, 0) != state.items[relative]:
                return Validation(TRANSFER_HASH_MISMATCH, "仍有文件块未确认")
        if not _is_hex(payload.get("digest")):
            return Validation(TRANSFER_HASH_MISMATCH, "整体摘要必须是 64 位十六进制")
        state.state = "completed"
        return PASS

    def _check_file_abort(self, session_id: str, payload: dict[str, Any]) -> Validation:
        state = self._transfer(session_id, payload)
        if state is None:
            return Validation(TRANSFER_NOT_FOUND, "传输不存在")
        state.state = "aborted"
        return PASS

    # ------------------------------------------------------------------ 剪贴板
    def _check_clipboard_update(self, session_id: str, payload: dict[str, Any]) -> Validation:
        kind = payload.get("kind")
        if kind not in CLIPBOARD_KINDS:
            return Validation(CLIPBOARD_UNSUPPORTED_KIND, "kind 只支持 text 或 image")
        size = _as_int(payload.get("size_bytes"))
        if size is None or size <= 0:
            return Validation(CLIPBOARD_EMPTY, "size_bytes 必须大于 0")
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return Validation(CLIPBOARD_EMPTY, "content 不得为空")
        if kind == "text":
            raw = content.encode("utf-8")
            if len(raw) > CLIPBOARD_TEXT_MAX_BYTES:
                return Validation(CLIPBOARD_TOO_LARGE, "文本剪贴板超过 256 KiB")
        else:
            decoded = _decode_b64(content)
            if decoded is None:
                return Validation(CLIPBOARD_DECODE_ERROR, "图片剪贴板必须是合法 Base64")
            raw = decoded
            if len(raw) > CLIPBOARD_IMAGE_MAX_BYTES:
                return Validation(CLIPBOARD_TOO_LARGE, "图片剪贴板超过 4 MiB")
        if len(raw) != size:
            return Validation(CLIPBOARD_DIGEST_MISMATCH, "size_bytes 与内容长度不一致")
        digest = payload.get("digest")
        if not _is_hex(digest):
            return Validation(CLIPBOARD_DIGEST_MISMATCH, "digest 必须是 64 位十六进制")
        if hashlib.sha256(raw).hexdigest() != digest:
            return Validation(CLIPBOARD_DIGEST_MISMATCH, "摘要与内容不一致")
        key = (session_id, kind)
        if self._clipboard.get(key) == digest:
            self.suppressed_updates += 1
            return Validation(suppress=True)
        self._clipboard[key] = digest
        return PASS

    def _check_clipboard_request(self, session_id: str, payload: dict[str, Any]) -> Validation:
        if payload.get("kind") not in CLIPBOARD_KINDS:
            return Validation(CLIPBOARD_UNSUPPORTED_KIND, "kind 只支持 text 或 image")
        return PASS

    def _check_clipboard_ack(self, session_id: str, payload: dict[str, Any]) -> Validation:
        if payload.get("kind") not in CLIPBOARD_KINDS:
            return Validation(CLIPBOARD_UNSUPPORTED_KIND, "kind 只支持 text 或 image")
        if not _is_hex(payload.get("digest")):
            return Validation(CLIPBOARD_DIGEST_MISMATCH, "digest 必须是 64 位十六进制")
        if not isinstance(payload.get("applied"), bool):
            return Validation(CLIPBOARD_DIGEST_MISMATCH, "applied 必须是布尔值")
        return PASS

    # ------------------------------------------------------------------ 内部工具
    def _transfer(self, session_id: str, payload: dict[str, Any]) -> TransferState | None:
        transfer_id = _text(payload, "transfer_id")
        if not transfer_id:
            return None
        return self._transfers.get((session_id, transfer_id))


def _text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == DIGEST_HEX_LEN and all(char in "0123456789abcdef" for char in value)


def _decode_b64(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _valid_resume(value: Any, sizes: dict[str, int]) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        relative = item.get("relative")
        received = _as_int(item.get("received_bytes"))
        if not isinstance(relative, str) or relative not in sizes:
            return False
        if received is None or not 0 <= received <= sizes[relative]:
            return False
    return True
