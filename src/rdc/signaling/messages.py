# ==========================================================================================
# 文件        : messages.py
# 所属目录    : src/rdc/signaling/
# 文件用途    : 34 类消息类型与必填载荷字段, 对应 protocol-v1 第三节
# 架构定位    : 协议层
# 核心功能    : 声明消息类型集合、必填字段、方向与路由分类
# 职责边界    : 不构造包络; 不实现服务端; 不定义额外字段语义
# ==========================================================================================

"""34 类消息.

契约依据: docs/contracts/protocol-v1.md 第三节(v1.2 追加 presence, v1.3 追加
文件传输 6 类与剪贴板 3 类, v1.4 追加屏幕墙聚焦与预览 7 类, v1.5 追加屏幕墙归属
登记 `wall_bind`/`wall_bound`, v1.6 追加 `file_result` 整文件校验回执).
文件与剪贴板消息走会话数据面, 屏幕墙消息经信令服务签发凭证或登记归属.
"""

from __future__ import annotations

# 34 类消息, 顺序与契约表一致
MESSAGE_TYPES: frozenset[str] = frozenset(
    {
        "register",
        "challenge",
        "auth",
        "auth_ok",
        "offer",
        "answer",
        "candidate",
        "relay_allocate",
        "relay_allocated",
        "session_start",
        "session_end",
        "display_update",
        "heartbeat",
        "presence",
        "error",
        "file_offer",
        "file_accept",
        "file_chunk",
        "file_ack",
        "file_complete",
        "file_abort",
        "file_result",
        "clipboard_update",
        "clipboard_request",
        "clipboard_ack",
        "wall_focus",
        "wall_focus_granted",
        "wall_unfocus",
        "wall_unfocused",
        "wall_preview",
        "wall_preview_granted",
        "wall_preview_revoked",
        "wall_bind",
        "wall_bound",
    }
)

# 各类型必填 payload 字段, 与契约表逐条对应
REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    "register": ("device_id", "public_key", "capabilities", "protocol_version"),
    "challenge": ("nonce", "expires_at"),
    "auth": ("device_id", "signature"),
    "auth_ok": ("token", "expires_at", "relay_endpoint"),
    "offer": ("session_id", "candidates", "media_caps", "access_proof"),
    "answer": ("session_id", "chosen_candidate", "media_caps"),
    "candidate": ("session_id", "kind", "address", "port", "priority"),
    "relay_allocate": ("session_id", "requested_kbps", "est_duration_s"),
    "relay_allocated": ("session_id", "relay_token", "endpoint", "budget_kbps", "quality_profile"),
    "session_start": ("session_id", "transport_kind", "quality_profile"),
    "session_end": ("session_id", "reason"),
    "display_update": ("session_id", "width", "height", "rotation"),
    "heartbeat": ("session_id", "seq"),
    "presence": ("device_id", "state"),
    "error": ("code", "message"),
    "file_offer": (
        "session_id",
        "transfer_id",
        "items",
        "total_bytes",
        "chunk_size",
        "digest_alg",
        "digest",
    ),
    "file_accept": ("session_id", "transfer_id", "accepted_items"),
    "file_chunk": ("session_id", "transfer_id", "file_index", "offset", "length", "sha256", "data"),
    "file_ack": ("session_id", "transfer_id", "file_index", "acked_bytes"),
    "file_complete": ("session_id", "transfer_id", "digest"),
    "file_abort": ("session_id", "transfer_id", "reason"),
    "file_result": ("session_id", "transfer_id", "verified", "digest"),
    "clipboard_update": ("session_id", "kind", "size_bytes", "digest", "content"),
    "clipboard_request": ("session_id", "kind"),
    "clipboard_ack": ("session_id", "kind", "digest", "applied"),
    "wall_focus": ("wall_id", "cell"),
    "wall_focus_granted": ("wall_id", "cell", "focus_token", "expires_at"),
    "wall_unfocus": ("wall_id",),
    "wall_unfocused": ("wall_id", "released"),
    "wall_preview": ("wall_id", "cell"),
    "wall_preview_granted": ("wall_id", "cell", "preview_token", "expires_at"),
    "wall_preview_revoked": ("wall_id", "cell", "reason"),
    "wall_bind": ("wall_id", "session_id"),
    "wall_bound": ("wall_id", "account_id", "bound_at"),
}

# 需要服务端转发给对端的类型
ROUTED_TYPES: frozenset[str] = frozenset(
    {"offer", "answer", "candidate", "session_start", "session_end", "display_update"}
)

# 会话数据面 10 类消息: 契约 v1.5 规定走会话数据面, 信令侧只做边界校验与降级转发
SESSION_PLANE_TYPES: frozenset[str] = frozenset(
    {
        "file_offer",
        "file_accept",
        "file_chunk",
        "file_ack",
        "file_complete",
        "file_abort",
        "file_result",
        "clipboard_update",
        "clipboard_request",
        "clipboard_ack",
    }
)

# 只能由服务端发出的类型
SERVER_TYPES: frozenset[str] = frozenset({"challenge", "auth_ok", "relay_allocated", "wall_bound"})


def required_payload_fields(msg_type: str) -> tuple[str, ...]:
    """返回指定类型的必填载荷字段, 未知类型返回空元组."""
    return REQUIRED_PAYLOAD.get(msg_type, ())
