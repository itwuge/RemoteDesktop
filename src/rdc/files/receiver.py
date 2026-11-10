# ==========================================================================================
# 文件        : receiver.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件传输收端: 校验 offer、落盘 chunk、逐块与整树哈希、回传 file_result
# 架构定位    : 可被 host 脚本直接 import 的纯业务入口, 不依赖界面与命令层
# 核心功能    : 断点续传偏移、路径穿越防护、分块校验、整体校验、失败清理
# 职责边界    : 不建连; 不读系统剪贴板; 不打印明文; 不决定传输调度
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.files.digest; rdc.core.errors
# ==========================================================================================

"""文件传输收端.

用法: 由会话循环把通道消息交给 ``handle``, 有返回值时原样回发::

    reply = receiver.handle(msg_type, payload)
    if reply:
        channel.send(reply["type"], reply["payload"])

收端以 ``<目标>/.part`` 落盘, 只有整树摘要一致才改名为正式文件并回 ``file_result``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdc.core.errors import (
    CHUNK_CHECKSUM_MISMATCH,
    FILE_CHUNK_INVALID,
    FILE_OFFER_REJECTED,
    TRANSFER_HASH_MISMATCH,
    TRANSFER_IO_ERROR,
)
from rdc.core.logging import log_event
from rdc.files.digest import tree_digest
from rdc.files.model import CHUNK_SIZE

HEX_DIGITS = set("0123456789abcdef")
MAX_ITEMS = 4096


@dataclass
class _IncomingItem:
    relative: str
    size: int
    sha256: str
    received: int = 0

    def part(self, root: Path) -> Path:
        return root / f"{self.relative}.part"

    def final(self, root: Path) -> Path:
        return root / self.relative


@dataclass
class _Incoming:
    session_id: str
    transfer_id: str
    chunk_size: int
    digest: str
    items: list[_IncomingItem]
    accepted: bool = False
    completed: bool = False
    started_at: float = 0.0
    errors: int = 0

    @property
    def received_bytes(self) -> int:
        return sum(item.received for item in self.items)


@dataclass
class FileTransferReceiver:
    """收端状态机; 所有方法都不抛异常, 失败通过 file_abort/file_result 表达."""

    destination_root: Path
    on_complete: Callable[[dict[str, Any]], None] | None = None
    transfers: dict[str, _Incoming] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.destination_root = Path(self.destination_root)

    # ------------------------------------------------------------------ 入口
    def handle(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条会话消息; 返回待回发消息或 None."""
        session_id = str(payload.get("session_id") or "")
        transfer_id = str(payload.get("transfer_id") or "")
        if not session_id or not transfer_id:
            return None
        if msg_type == "file_offer":
            return self._on_offer(session_id, transfer_id, payload)
        if msg_type == "file_chunk":
            return self._on_chunk(session_id, transfer_id, payload)
        if msg_type == "file_complete":
            return self._on_complete(session_id, transfer_id, payload)
        if msg_type == "file_abort":
            self._on_abort(transfer_id)
        return None

    def snapshot(self) -> dict[str, Any]:
        """运行状态摘要; 不含路径与摘要明文."""
        return {
            "transfers": len(self.transfers),
            "received_bytes": sum(state.received_bytes for state in self.transfers.values()),
            "completed": sum(1 for state in self.transfers.values() if state.completed),
        }

    # ------------------------------------------------------------------ offer
    def _on_offer(self, session_id: str, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.transfers.get(transfer_id)
        if state is not None and not state.completed:
            return self._accept(state)
        parsed = self._parse_offer(payload)
        if isinstance(parsed, str):
            return self._abort(session_id, transfer_id, FILE_OFFER_REJECTED, parsed)
        chunk_size, digest, items = parsed
        for item in items:
            if item.size == 0:
                part = item.part(self.destination_root)
                part.parent.mkdir(parents=True, exist_ok=True)
                part.touch(exist_ok=True)
            else:
                part = item.part(self.destination_root)
                if part.exists() and part.is_file():
                    item.received = min(item.size, part.stat().st_size)
        state = _Incoming(
            session_id=session_id,
            transfer_id=transfer_id,
            chunk_size=chunk_size,
            digest=digest,
            items=items,
            accepted=True,
        )
        self.transfers[transfer_id] = state
        log_event(
            "files.receive",
            "transfer.offer_accepted",
            transfer_id=transfer_id,
            files=len(items),
            resume_bytes=state.received_bytes,
        )
        return self._accept(state)

    def _parse_offer(self, payload: dict[str, Any]) -> tuple[int, str, list[_IncomingItem]] | str:
        chunk_size = payload.get("chunk_size")
        if not isinstance(chunk_size, int) or not 0 < chunk_size <= CHUNK_SIZE:
            return "分块大小超出 1..256 KiB"
        if payload.get("digest_alg") != "sha256":
            return "不支持的摘要算法"
        digest = str(payload.get("digest") or "")
        if not _is_hex_digest(digest):
            return "整体摘要格式非法"
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > MAX_ITEMS:
            return "条目数量非法"
        items: list[_IncomingItem] = []
        seen: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                return "条目格式非法"
            relative = str(raw.get("relative") or "")
            safe = _safe_relative(relative)
            if safe is None:
                return f"相对路径非法: {relative[:64]}"
            if safe in seen:
                return "条目相对路径重复"
            seen.add(safe)
            size = raw.get("size")
            sha256 = str(raw.get("sha256") or "").lower()
            if not isinstance(size, int) or size < 0:
                return "条目大小非法"
            if not _is_hex_digest(sha256):
                return "条目摘要格式非法"
            items.append(_IncomingItem(relative=safe, size=size, sha256=sha256))
        total = payload.get("total_bytes")
        if total != sum(item.size for item in items):
            return "总字节数与条目不一致"
        return chunk_size, digest, items

    def _accept(self, state: _Incoming) -> dict[str, Any]:
        offsets = [
            {"relative": item.relative, "received_bytes": item.received} for item in state.items if item.received > 0
        ]
        return self._reply(
            "file_accept",
            session_id=state.session_id,
            transfer_id=state.transfer_id,
            accepted_items=[item.relative for item in state.items],
            resume=offsets,
        )

    # ------------------------------------------------------------------ chunk
    def _on_chunk(self, session_id: str, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.transfers.get(transfer_id)
        if state is None or state.completed:
            return self._abort(session_id, transfer_id, FILE_CHUNK_INVALID, "任务不存在或已完成")
        file_index = payload.get("file_index")
        if not isinstance(file_index, int) or not 0 <= file_index < len(state.items):
            return self._abort(session_id, transfer_id, FILE_CHUNK_INVALID, "文件序号非法")
        item = state.items[file_index]
        offset = payload.get("offset")
        length = payload.get("length")
        if not isinstance(offset, int) or offset != item.received:
            return self._abort(session_id, transfer_id, FILE_CHUNK_INVALID, "分块偏移与已收字节不一致")
        raw = _decode_base64(str(payload.get("data") or ""))
        if raw is None or not isinstance(length, int) or length != len(raw):
            return self._abort(session_id, transfer_id, FILE_CHUNK_INVALID, "分块内容无法解码或长度不符")
        if hashlib.sha256(raw).hexdigest() != str(payload.get("sha256") or "").lower():
            state.errors += 1
            return self._abort(session_id, transfer_id, CHUNK_CHECKSUM_MISMATCH, "分块摘要不一致")
        if offset + len(raw) > item.size:
            return self._abort(session_id, transfer_id, FILE_CHUNK_INVALID, "分块超出文件大小")
        part = item.part(self.destination_root)
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            with part.open("r+b" if part.exists() else "wb") as handle:
                handle.seek(offset)
                handle.write(raw)
                handle.flush()
        except OSError:
            return self._abort(session_id, transfer_id, TRANSFER_IO_ERROR, "写入接收文件失败")
        item.received += len(raw)
        return self._reply(
            "file_ack",
            session_id=session_id,
            transfer_id=transfer_id,
            file_index=file_index,
            acked_bytes=item.received,
        )

    # ------------------------------------------------------------------ complete
    def _on_complete(self, session_id: str, transfer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.transfers.get(transfer_id)
        if state is None or state.completed:
            return self._result(session_id, transfer_id, False, "", TRANSFER_IO_ERROR, "任务不存在或已完成")
        if any(item.received != item.size for item in state.items):
            return self._result(session_id, transfer_id, False, "", TRANSFER_IO_ERROR, "仍有文件未传完")
        entries = [(item.relative, item.part(self.destination_root)) for item in state.items]
        try:
            actual = tree_digest(entries)
        except OSError:
            return self._result(session_id, transfer_id, False, "", TRANSFER_IO_ERROR, "读取接收文件失败")
        if actual != state.digest or actual != str(payload.get("digest") or "").lower():
            self._discard(state)
            return self._result(
                session_id,
                transfer_id,
                False,
                actual,
                TRANSFER_HASH_MISMATCH,
                "整体哈希不一致, 已清理接收分块",
            )
        try:
            self._promote(state)
        except OSError:
            return self._result(session_id, transfer_id, False, actual, TRANSFER_IO_ERROR, "接收文件落盘失败")
        state.completed = True
        self.transfers.pop(transfer_id, None)
        log_event("files.receive", "transfer.completed", transfer_id=transfer_id, files=len(state.items))
        if self.on_complete is not None:
            self.on_complete(
                {
                    "transfer_id": transfer_id,
                    "files": [item.relative for item in state.items],
                    "digest": actual,
                }
            )
        return self._result(session_id, transfer_id, True, actual)

    def _promote(self, state: _Incoming) -> None:
        for item in state.items:
            part = item.part(self.destination_root)
            final = item.final(self.destination_root)
            final.parent.mkdir(parents=True, exist_ok=True)
            with part.open("r+b") as handle:
                os.fsync(handle.fileno())
            part.replace(final)

    def _discard(self, state: _Incoming) -> None:
        for item in state.items:
            part = item.part(self.destination_root)
            try:
                part.unlink(missing_ok=True)
            except OSError:
                continue

    def _on_abort(self, transfer_id: str) -> None:
        state = self.transfers.pop(transfer_id, None)
        if state is not None:
            self._discard(state)

    # ------------------------------------------------------------------ 回包
    def _abort(self, session_id: str, transfer_id: str, code: str, message: str) -> dict[str, Any]:
        state = self.transfers.pop(transfer_id, None)
        if state is not None:
            self._discard(state)
        log_event("files.receive", "transfer.aborted", level="WARNING", code=code, reason=message)
        return self._reply(
            "file_abort",
            session_id=session_id,
            transfer_id=transfer_id,
            error_code=code,
            reason=message,
        )

    @staticmethod
    def _result(
        session_id: str,
        transfer_id: str,
        verified: bool,
        digest: str,
        error_code: str = "",
        message: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "transfer_id": transfer_id,
            "verified": verified,
            "digest": digest,
        }
        if error_code:
            payload["error_code"] = error_code
        if message:
            payload["message"] = message
        return {"type": "file_result", "payload": payload}

    @staticmethod
    def _reply(msg_type: str, **payload: Any) -> dict[str, Any]:
        return {"type": msg_type, "payload": payload}


def _is_hex_digest(value: str) -> bool:
    return len(value) == 64 and set(value.lower()) <= HEX_DIGITS


def _safe_relative(relative: str) -> str | None:
    """拒绝绝对路径、盘符与 .. 段; 返回规范化 posix 相对路径."""
    text = relative.replace("\\", "/")
    if not text or ":" in text.split("/", 1)[0]:
        return None
    parts = [part for part in text.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    return "/".join(parts)


def _decode_base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
