# ==========================================================================================
# 文件        : remote_protocol.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 服务端账号中心与客户端共用的协议常量、包络与设备身份声明
# 架构定位    : 账号中心线协议层, 服务端与客户端都不重复实现同一份常量
# 核心功能    : /v1 路径、统一包络、错误码、设备挑战签名载荷
# 职责边界    : 不做网络; 不读写存储; 不生成用户面长句
# ==========================================================================================

"""跨端账号中心协议常量.

契约依据: .team/records/契约变更记录-契约-202611-018-20261110.md 第六部分.
HTTP 复用现有 TLS 监听端口, 不新增端口.
"""

from __future__ import annotations

from typing import Any

API_PREFIX = "/v1"

# 写操作幂等键有效期与缓存条数上限
IDEMPOTENCY_TTL_SECONDS = 600.0
IDEMPOTENCY_MAX_ENTRIES = 512

# 设备身份挑战有效期
CHALLENGE_TTL_SECONDS = 300.0

# 服务端确认的账号令牌有效期与本地一致
REMOTE_DEVICE_TOKEN_TTL_SECONDS = 30 * 24 * 3600
REMOTE_SESSION_TOKEN_TTL_SECONDS = 15 * 60

CODE_OK = 0
CODE_ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
CODE_CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
CODE_SESSION_REVOKED = "TOKEN_REVOKED"
CODE_SESSION_EXPIRED = "TOKEN_EXPIRED"
CODE_SESSION_INVALID = "TOKEN_INVALID"
CODE_DEVICE_BOUND_OTHER = "DEVICE_BOUND_OTHER"
CODE_CHALLENGE_INVALID = "CHALLENGE_INVALID"
CODE_PERMISSION_DENIED = "PERMISSION_DENIED"
CODE_DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
CODE_IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
CODE_BAD_REQUEST = "BAD_REQUEST"
CODE_STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"

MAX_BODY_BYTES = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024

# 设备平台枚举: 只接受明确来源, 未知一律 unknown, 不按设备名或显示名猜测
DEVICE_PLATFORMS = frozenset({"windows", "linux", "macos", "android", "unknown"})


def normalize_platform(value: Any) -> str:
    """把调用方自报平台收敛到契约枚举; 缺失或不认识时返回 unknown."""
    platform = str(value or "").strip().lower()
    return platform if platform in DEVICE_PLATFORMS else "unknown"


def envelope_ok(data: dict[str, Any] | None = None, message: str = "ok") -> dict[str, Any]:
    """构造成功包络."""
    return {"code": CODE_OK, "message": message, "data": data if data is not None else {}}


def envelope_fail(code: str, message: str) -> dict[str, Any]:
    """构造失败包络; 错误码只在调用方需要分支时给."""
    return {"code": code, "message": message, "data": {}}


def attestation_payload(account: str, device_id: str, nonce: str) -> dict[str, str]:
    """设备身份声明签名载荷; 客户端与服务端必须逐字段一致."""
    return {"account": account, "device_id": device_id, "nonce": nonce}
