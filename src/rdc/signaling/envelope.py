# ==========================================================================================
# 文件        : envelope.py
# 所属目录    : src/rdc/signaling/
# 文件用途    : 七字段消息包络的构造、校验与验签, 对应 protocol-v1 第二节
# 架构定位    : 协议层, 只依赖 core 与 crypto
# 核心功能    : 构造包络; 校验字段、版本、时间窗口与类型; 验签
# 职责边界    : 不处理消息业务语义; 不做网络发送
# ==========================================================================================

"""七字段消息包络.

契约依据: docs/contracts/protocol-v1.md 第二节.
字段固定为 v, type, id, session_id, ts, payload, sig.
"""

from __future__ import annotations

import uuid
from typing import Any

from rdc.core.ids import is_within_window, new_message_id, now_iso
from rdc.crypto.identity import DeviceIdentity, b64decode, verify_signature
from rdc.signaling.messages import MESSAGE_TYPES, required_payload_fields

ENVELOPE_FIELDS = ("v", "type", "id", "session_id", "ts", "payload", "sig")
PROTOCOL_VERSION = 1


class ProtocolError(Exception):
    """协议校验失败, 携带可判定的错误码."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def build_envelope(
    identity: DeviceIdentity,
    msg_type: str,
    payload: dict[str, Any],
    session_id: str | None = None,
    msg_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """构造带签名的七字段包络.

    :param identity: 发送方设备身份
    :param msg_type: 消息类型, 必须在 24 类之内
    :param payload: 类型相关载荷
    :param session_id: 会话标识, 非会话消息传 None
    :param msg_id: 指定消息标识, 默认生成 UUIDv4
    :param timestamp: 指定时间戳, 默认当前时间
    :return: 七字段字典
    """
    if msg_type not in MESSAGE_TYPES:
        raise ProtocolError("UNKNOWN_TYPE", f"未知消息类型 {msg_type}")
    envelope: dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": msg_type,
        "id": msg_id or new_message_id(),
        "session_id": session_id,
        "ts": timestamp or now_iso(),
        "payload": payload,
    }
    envelope["sig"] = identity.sign(envelope)
    return envelope


def unsigned(envelope: dict[str, Any]) -> dict[str, Any]:
    """去掉 sig 字段, 得到签名对象."""
    return {key: value for key, value in envelope.items() if key != "sig"}


def validate_envelope(envelope: dict[str, Any]) -> None:
    """校验包络结构与字段.

    校验失败抛 ProtocolError; 通过返回 None.
    """
    if not isinstance(envelope, dict):
        raise ProtocolError("INVALID_MESSAGE", "包络必须是对象")
    for field_name in ENVELOPE_FIELDS:
        if field_name not in envelope:
            raise ProtocolError("MISSING_FIELD", f"缺少字段 {field_name}")

    version = envelope["v"]
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ProtocolError("PROTOCOL_VERSION_UNSUPPORTED", f"不支持的协议版本 {version}")

    msg_type = envelope["type"]
    if not isinstance(msg_type, str) or msg_type not in MESSAGE_TYPES:
        raise ProtocolError("UNKNOWN_TYPE", f"未知消息类型 {msg_type}")

    msg_id = envelope["id"]
    try:
        parsed = uuid.UUID(str(msg_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProtocolError("INVALID_MESSAGE", "id 不是合法 UUID") from exc
    if parsed.version != 4:
        raise ProtocolError("INVALID_MESSAGE", "id 必须是 UUIDv4")

    session_id = envelope["session_id"]
    if session_id is not None and not isinstance(session_id, str):
        raise ProtocolError("INVALID_MESSAGE", "session_id 必须是字符串或空")

    timestamp = envelope["ts"]
    if not isinstance(timestamp, str) or not is_within_window(timestamp):
        raise ProtocolError("INVALID_MESSAGE", "ts 缺失、不可解析或超出 5 分钟窗口")

    payload = envelope["payload"]
    if not isinstance(payload, dict):
        raise ProtocolError("INVALID_MESSAGE", "payload 必须是对象")
    missing = [name for name in required_payload_fields(msg_type) if name not in payload]
    if missing:
        raise ProtocolError("MISSING_FIELD", f"{msg_type} 缺少载荷字段 {','.join(missing)}")

    signature = envelope["sig"]
    if not isinstance(signature, str) or not signature:
        raise ProtocolError("INVALID_MESSAGE", "sig 必须是非空字符串")


def verify_envelope(envelope: dict[str, Any], public_key_b64: str) -> bool:
    """用指定公钥验证包络签名, 结构与字段不合法一律返回 False."""
    try:
        validate_envelope(envelope)
        return verify_signature(public_key_b64, unsigned(envelope), envelope["sig"])
    except ProtocolError:
        return False


def decode_signature(signature_b64: str) -> bytes:
    """解码签名, 供诊断使用."""
    return b64decode(signature_b64)
