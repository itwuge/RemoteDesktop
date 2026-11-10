# ==========================================================================================
# 文件        : sync.py
# 所属目录    : src/rdc/clipboard/
# 文件用途    : 剪贴板双向同步: 文本与图片、大小上限、截断提示、防环回与去重
# 架构定位    : 业务层, 只依赖 core; 内容仅存内存, 日志只记摘要与长度
# 核心功能    : UTF-8 统一编码、图片 base64 校验、来源标记、重复推送抑制
# 职责边界    : 不读写系统剪贴板（由界面层收集后传入）; 不落盘明文; 不建连
# ==========================================================================================

"""剪贴板同步.

契约依据: docs/contracts/desktop-v1.md v1.4 第 37 至 40 条.

上限: 文本 256 KiB（UTF-8 字节, 超限截断并提示）, 图片 4 MiB（超限拒绝并提示）.
防环回: 记录最近一次来源与摘要; 同来源同摘要不再回写, 跨来源同摘要视为重复推送.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rdc.core.errors import (
    CLIPBOARD_DECODE_ERROR,
    CLIPBOARD_DISABLED,
    CLIPBOARD_EMPTY,
    CLIPBOARD_TOO_LARGE,
    CLIPBOARD_UNSUPPORTED_KIND,
)
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.files.scope import SCOPE_INVALID, SCOPE_LEVELS, scope_rejection
from rdc.session.channel import SessionChannelError

if TYPE_CHECKING:
    from rdc.clipboard.receiver import ClipboardReceiver
    from rdc.session.channel import SessionChannel

TEXT_LIMIT_BYTES = 256 * 1024
IMAGE_LIMIT_BYTES = 4 * 1024 * 1024
CLIPBOARD_KINDS = ("text", "image")


def _human_size(size: int) -> str:
    """字节数转可读文本."""
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024 or unit == "MiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} MiB"


@dataclass
class ClipboardSyncService:
    """剪贴板同步内存实现; 双向由 origin / target 区分."""

    enabled: bool = True
    scope: str = "control"
    latest: dict[str, Any] | None = None
    last_digest: str | None = None
    last_origin: str | None = None
    pushes: int = 0
    suppressed: int = 0
    history: list[str] = field(default_factory=list)
    history_limit: int = 64
    channel: SessionChannel | None = None
    receiver: ClipboardReceiver | None = None
    pending: dict[str, Any] | None = None
    confirmed: dict[str, Any] | None = None
    last_error: str = ""

    # ---------------------------------------------------------------- 通道绑定
    def bind_channel(
        self,
        channel: SessionChannel | None,
        *,
        on_apply: Callable[[str, str], None] | None = None,
    ) -> None:
        """绑定或解绑会话通道; 通道实现可替换, 业务逻辑不感知中继分支.

        ``on_apply`` 由装配处提供(通常写系统剪贴板), 与接收端一一对应.
        """
        from rdc.clipboard.receiver import ClipboardReceiver

        if self.channel is not None and self.channel is not channel:
            self.channel.close()
        self.channel = channel
        self.receiver = ClipboardReceiver(on_apply=on_apply) if channel is not None else None
        self.pending = None
        self.confirmed = None
        self.last_error = ""
        info = channel.info() if channel is not None else None
        log_event(
            "clipboard.sync",
            "clipboard.channel_bind",
            bound=channel is not None,
            kind=info.kind if info else "",
            visible_to_relay=info.visible_to_relay if info else False,
            encrypted=info.encrypted if info else False,
        )

    def poll_channel(self) -> int:
        """消费通道消息并回写收发状态; 返回处理条数, 界面刷新时调用."""
        if self.channel is None or self.receiver is None:
            return 0
        processed = 0
        for _ in range(64):
            try:
                message = self.channel.receive(0.0)
            except SessionChannelError as exc:
                self.last_error = _channel_error_text(exc)
                return processed
            if message is None:
                return processed
            processed += 1
            msg_type, payload = message
            if msg_type == "clipboard_ack":
                self._on_ack(payload)
                continue
            if msg_type == "error":
                self.pending = None
                self.last_error = f"{payload.get('code', '')}: {payload.get('message', '')}"
                continue
            reply = self.receiver.handle(msg_type, payload)
            if reply is not None:
                try:
                    self.channel.send(str(reply["type"]), dict(reply["payload"]))
                except SessionChannelError as exc:
                    self.last_error = _channel_error_text(exc)
                    return processed
            if (
                msg_type == "clipboard_update"
                and reply is not None
                and reply["type"] == "clipboard_ack"
                # 双向同时推送时, 对端内容先到不能覆盖本端待确认的推送状态
                and (self.pending is None or self.pending.get("action") != "push")
            ):
                self._confirm(payload, applied=bool(reply["payload"].get("applied")))
        return processed

    def _on_ack(self, payload: dict[str, Any]) -> None:
        """对端确认: 只有 applied 与摘要都对得上才算已同步."""
        if self.pending is None or self.pending.get("action") != "push":
            return
        digest = str(payload.get("digest") or "")
        if digest and digest != self.pending.get("digest"):
            self.last_error = "对端确认的摘要与本端发送不一致"
            return
        self._confirm(payload, applied=bool(payload.get("applied")))

    def _confirm(self, payload: dict[str, Any], *, applied: bool) -> None:
        pending = self.pending or {}
        kind = str(payload.get("kind") or pending.get("kind") or "")
        digest = str(payload.get("digest") or pending.get("digest") or "")
        self.confirmed = {
            "action": pending.get("action", "push"),
            "kind": kind,
            "digest": digest,
            "size_bytes": int(payload.get("size_bytes") or pending.get("size_bytes") or 0),
            "applied": applied,
        }
        self.pending = None
        if pending.get("action") == "pull" and self.receiver is not None:
            item = self.receiver.latest.get(kind)
            if item is not None:
                self.latest = {
                    "kind": kind,
                    "digest": item["digest"],
                    "size_bytes": item["size_bytes"],
                    "original_bytes": item["size_bytes"],
                    "truncated": False,
                    "origin": "remote",
                    **({"text": item["content"]} if kind == "text" else {"image_b64": item["content"]}),
                }
                self.last_digest = item["digest"]
                self.last_origin = "remote"
        log_event(
            "clipboard.sync",
            "clipboard.confirmed",
            action=self.confirmed["action"],
            kind=kind,
            digest=digest,
            applied=applied,
        )

    def _channel_info(self) -> Any:
        return None if self.channel is None else self.channel.info()

    def set_scope(self, scope: str) -> CommandResult:
        """绑定当前会话授权范围; 剪贴板统一要求 control, view 一律拒绝."""
        if scope not in SCOPE_LEVELS:
            return CommandResult(ok=False, message="授权范围无效, 请重新选择", error_code=SCOPE_INVALID)
        self.scope = scope
        log_event("clipboard.sync", "clipboard.scope", scope=scope)
        return CommandResult(ok=True, message=f"授权范围已设置为 {scope}", data={"scope": scope})

    def _scope_gate(self, action: str) -> CommandResult | None:
        """按当前授权范围判定; 拒绝时留结构化日志."""
        denied = scope_rejection(self.scope, action)
        if denied is not None:
            log_event(
                "clipboard.sync",
                "clipboard.scope_rejected",
                level="WARNING",
                scope=self.scope,
                action=action,
            )
        return denied

    def set_enabled(self, enabled: bool) -> CommandResult:
        """开启或关闭同步; 关闭后双向命令一律返回 CLIPBOARD_DISABLED."""
        denied = self._scope_gate("剪贴板")
        if denied is not None:
            return denied
        self.enabled = enabled
        log_event("clipboard.sync", "clipboard.toggled", enabled=enabled)
        return CommandResult(
            ok=True,
            message="剪贴板同步已开启" if enabled else "剪贴板同步已关闭",
            data={"enabled": self.enabled},
        )

    def push(self, origin: str, kind: str, content: str) -> CommandResult:
        """本端内容上行; 文本为原文, 图片为 PNG 的 base64."""
        denied = self._scope_gate("剪贴板")
        if denied is not None:
            return denied
        if not self.enabled:
            return CommandResult(ok=False, message="剪贴板同步已关闭", error_code=CLIPBOARD_DISABLED)
        if kind not in CLIPBOARD_KINDS:
            return CommandResult(
                ok=False, message="只支持文本与图片两种剪贴板内容", error_code=CLIPBOARD_UNSUPPORTED_KIND
            )

        if kind == "text":
            raw = content.encode("utf-8")
            original_bytes = len(raw)
            truncated = original_bytes > TEXT_LIMIT_BYTES
            text = raw[:TEXT_LIMIT_BYTES].decode("utf-8", errors="ignore") if truncated else content
            # 截断可能切到多字节字符: 以实际下发文本重算长度与摘要, 保证对端校验一致
            raw = text.encode("utf-8")
            payload: dict[str, Any] = {"text": text}
            limit = TEXT_LIMIT_BYTES
        else:
            try:
                raw = base64.b64decode(content, validate=True)
            except (binascii.Error, ValueError):
                return CommandResult(ok=False, message="图片内容不是合法的 base64", error_code=CLIPBOARD_DECODE_ERROR)
            original_bytes = len(raw)
            truncated = False
            limit = IMAGE_LIMIT_BYTES
            if original_bytes > limit:
                return CommandResult(
                    ok=False,
                    message=f"图片超过 {_human_size(limit)} 上限, 已取消本次同步",
                    error_code=CLIPBOARD_TOO_LARGE,
                    data={"kind": kind, "size_bytes": original_bytes, "limit_bytes": limit},
                )
            payload = {"image_b64": content}

        digest = hashlib.sha256(raw).hexdigest()
        size = len(raw)
        notice = _truncate_notice(truncated, limit)
        duplicated = self.latest is not None and self.latest["kind"] == kind and digest == self.last_digest
        if duplicated:
            self.suppressed += 1
            log_event(
                "clipboard.sync",
                "clipboard.duplicate",
                kind=kind,
                size_bytes=size,
                digest=digest,
                origin=origin,
                loop=origin == self.last_origin,
            )
            return CommandResult(
                ok=True,
                message=f"内容未变化, 已抑制重复同步{notice}",
                data={
                    "applied": False,
                    "kind": kind,
                    "size_bytes": size,
                    "digest": digest,
                    "truncated": truncated,
                    "reason": "loop" if origin == self.last_origin else "duplicate",
                },
            )

        self.latest = {
            "kind": kind,
            "digest": digest,
            "size_bytes": size,
            "original_bytes": original_bytes,
            "truncated": truncated,
            "origin": origin,
            **payload,
        }
        self.last_digest = digest
        self.last_origin = origin
        self.pushes += 1
        self.history.append(digest)
        if len(self.history) > self.history_limit:
            del self.history[: -self.history_limit]
        log_event(
            "clipboard.sync",
            "clipboard.pushed",
            kind=kind,
            size_bytes=size,
            digest=digest,
            truncated=truncated,
            origin=origin,
        )
        data: dict[str, Any] = {
            "applied": True,
            "kind": kind,
            "size_bytes": size,
            "original_bytes": original_bytes,
            "digest": digest,
            "truncated": truncated,
            "limit_bytes": limit,
        }
        if self.channel is None:
            return CommandResult(
                ok=True,
                message=f"剪贴板已同步（{'文本' if kind == 'text' else '图片'} {_human_size(size)}）{notice}",
                data=data,
            )
        info = self.channel.info()
        outgoing = str(payload["text"]) if kind == "text" else content
        try:
            self.channel.send(
                "clipboard_update",
                {
                    "session_id": info.session_id,
                    "kind": kind,
                    "content": outgoing,
                    "size_bytes": size,
                    "digest": digest,
                },
            )
        except SessionChannelError as exc:
            self.last_error = _channel_error_text(exc)
            log_event(
                "clipboard.sync",
                "clipboard.send_failed",
                level="WARNING",
                kind=kind,
                size_bytes=size,
                digest=digest,
                code=exc.code,
            )
            return CommandResult(
                ok=False,
                message=f"剪贴板发送失败: {exc.message}",
                error_code=exc.code,
                data={**data, "applied": False, "pending": False},
            )
        self.pending = {"action": "push", "kind": kind, "digest": digest, "size_bytes": size}
        self.confirmed = None
        return CommandResult(
            ok=True,
            message=f"剪贴板已发送, 等待对端确认（{'文本' if kind == 'text' else '图片'} {_human_size(size)}）{notice}",
            data={**data, "pending": True},
        )

    def pull(self, target: str) -> CommandResult:
        """对端拉取最近内容; 来源端自己拉取时跳过以避免环回."""
        denied = self._scope_gate("剪贴板")
        if denied is not None:
            return denied
        if not self.enabled:
            return CommandResult(ok=False, message="剪贴板同步已关闭", error_code=CLIPBOARD_DISABLED)
        if self.channel is not None:
            info = self.channel.info()
            try:
                self.channel.send(
                    "clipboard_request",
                    {"session_id": info.session_id, "kind": self.latest["kind"] if self.latest else ""},
                )
            except SessionChannelError as exc:
                self.last_error = _channel_error_text(exc)
                return CommandResult(
                    ok=False,
                    message=f"拉取请求发送失败: {exc.message}",
                    error_code=exc.code,
                    data={"applied": False, "pending": False, "mode": "channel"},
                )
            kind = str(self.latest["kind"]) if self.latest else ""
            self.pending = {"action": "pull", "kind": kind, "digest": "", "size_bytes": 0}
            self.confirmed = None
            return CommandResult(
                ok=True,
                message="已向对端请求剪贴板, 等待回传; 回传并写入本端前不视为已同步.",
                data={"applied": False, "pending": True, "mode": "channel", "kind": kind or None},
            )
        if self.latest is None:
            return CommandResult(ok=False, message="剪贴板暂无内容", error_code=CLIPBOARD_EMPTY)
        latest = self.latest
        if latest["origin"] == target:
            return CommandResult(
                ok=True,
                message="内容来自本端, 已跳过回写以避免环回",
                data={
                    "applied": False,
                    "kind": latest["kind"],
                    "digest": latest["digest"],
                    "size_bytes": latest["size_bytes"],
                    "reason": "loop",
                },
            )
        data: dict[str, Any] = {
            "applied": True,
            "kind": latest["kind"],
            "digest": latest["digest"],
            "size_bytes": latest["size_bytes"],
            "original_bytes": latest["original_bytes"],
            "truncated": latest["truncated"],
            "origin": latest["origin"],
        }
        data.update({"text": latest["text"]} if latest["kind"] == "text" else {"image_b64": latest["image_b64"]})
        log_event(
            "clipboard.sync",
            "clipboard.pulled",
            kind=latest["kind"],
            size_bytes=latest["size_bytes"],
            digest=latest["digest"],
            target=target,
        )
        return CommandResult(
            ok=True,
            message=f"已获取剪贴板{'文本' if latest['kind'] == 'text' else '图片'}",
            data=data,
        )

    def status(self) -> CommandResult:
        """返回同步状态; 不含内容明文; view 授权同样拒绝."""
        denied = self._scope_gate("剪贴板")
        if denied is not None:
            return denied
        latest = self.latest
        info = self._channel_info()
        pending = self.pending or {}
        confirmed = self.confirmed or {}
        return CommandResult(
            ok=True,
            message="剪贴板同步已开启" if self.enabled else "剪贴板同步已关闭",
            data={
                "enabled": self.enabled,
                "kind": latest["kind"] if latest else None,
                "size_bytes": latest["size_bytes"] if latest else 0,
                "original_bytes": latest["original_bytes"] if latest else 0,
                "digest": latest["digest"] if latest else None,
                "origin": latest["origin"] if latest else None,
                "truncated": latest["truncated"] if latest else False,
                "pushes": self.pushes,
                "suppressed": self.suppressed,
                "text_limit_bytes": TEXT_LIMIT_BYTES,
                "image_limit_bytes": IMAGE_LIMIT_BYTES,
                "mode": "channel" if info is not None else "local",
                "bound": info is not None,
                "channel_kind": info.kind if info is not None else "",
                "encrypted": info.encrypted if info is not None else False,
                "visible_to_relay": info.visible_to_relay if info is not None else False,
                "pending": bool(pending),
                "pending_action": pending.get("action"),
                "pending_kind": pending.get("kind"),
                "pending_digest": pending.get("digest") or None,
                "confirmed": bool(confirmed),
                "confirmed_action": confirmed.get("action"),
                "confirmed_digest": confirmed.get("digest") or None,
                "confirmed_applied": confirmed.get("applied"),
                "last_error": self.last_error,
            },
        )


def _channel_error_text(exc: SessionChannelError) -> str:
    """通道异常转一行状态文本; 界面只显示结论, 不打印载荷."""
    return f"{exc.code}: {exc.message}"


def _truncate_notice(truncated: bool, limit: int) -> str:
    """统一截断提示文案."""
    if not truncated:
        return ""
    return f"（内容超过 {_human_size(limit)} 上限, 已截断）"
