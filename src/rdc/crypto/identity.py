# ==========================================================================================
# 文件        : identity.py
# 所属目录    : src/rdc/crypto/
# 文件用途    : 设备身份、规范 JSON、签名与验签, 供信令协议层使用
# 架构定位    : 能力层, 只依赖标准库与 cryptography
# 核心功能    : Ed25519 密钥对; device_id 由公钥摘要生成; 规范 JSON 签名与验签
# 职责边界    : 不做网络; 不做会话密钥派生; 不读写密钥库文件
# ==========================================================================================

"""设备身份与签名.

口径与 protocol-v1 第二节一致:
- 规范 JSON: UTF-8、键名按字典序、无多余空白、不转义非必要字符;
- device_id 取公钥 SHA-256 的 Base32 前 20 字符;
- 签名对象为不含 sig 的规范 JSON.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical_json(payload: dict) -> bytes:
    """把字典序列化为规范 JSON 字节串."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def device_id_from_public_key(public_key: bytes) -> str:
    """由 Ed25519 公钥原始字节生成 device_id."""
    digest = hashlib.sha256(public_key).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return encoded[:20]


def b64encode(raw: bytes) -> str:
    """标准 Base64 编码."""
    return base64.b64encode(raw).decode("ascii")


def b64decode(text: str) -> bytes:
    """标准 Base64 解码, 非法输入抛 ValueError."""
    return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass
class DeviceIdentity:
    """设备身份, 持有 Ed25519 私钥."""

    private_key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> DeviceIdentity:
        """生成新的设备身份."""
        return cls(Ed25519PrivateKey.generate())

    @property
    def public_key_raw(self) -> bytes:
        """返回公钥原始字节."""
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def public_key_b64(self) -> str:
        """返回公钥 Base64, 用于注册与验签."""
        return b64encode(self.public_key_raw)

    @property
    def device_id(self) -> str:
        """返回由公钥摘要生成的设备标识."""
        return device_id_from_public_key(self.public_key_raw)

    def sign(self, payload: dict) -> str:
        """对不含 sig 的载荷签名, 返回 Base64 签名."""
        signature = self.private_key.sign(canonical_json(payload))
        return b64encode(signature)


def verify_signature(public_key_b64: str, payload: dict, signature_b64: str) -> bool:
    """用 Base64 公钥验证载荷签名."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(b64decode(public_key_b64))
        public_key.verify(b64decode(signature_b64), canonical_json(payload))
        return True
    except (InvalidSignature, ValueError):
        return False
