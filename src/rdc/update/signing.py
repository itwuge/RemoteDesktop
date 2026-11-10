# ==========================================================================================
# 文件        : signing.py
# 所属目录    : src/rdc/update/
# 文件用途    : 更新清单 Ed25519 签名校验: 规范 JSON、内置公钥、中文拒绝原因
# 架构定位    : 更新模块的安全边界, 由 manifest.parse_manifest 统一调用
# 核心功能    : 校验 signature{alg,key_id,value}; 签名覆盖除 signature 外的全部字段
# 职责边界    : 不生成密钥; 不签名; 不发起网络请求; 不含任何私钥材料
# ==========================================================================================

"""更新清单签名校验.

签名格式(v2 清单顶层新增 ``signature`` 字段)::

    {
      "schema": 2,
      "version": "0.2.1",
      ...
      "signature": {"alg": "ed25519", "key_id": "e12dd98cb054069f", "value": "Base64 签名"}
    }

- 签名对象: 去掉 ``signature`` 后按字典序排序、无多余空白、不转义非必要字符的 UTF-8 JSON;
- ``key_id``: 公钥原始 32 字节 SHA-256 的十六进制前 16 位, 用于快速识别密钥;
- 公钥内置在客户端源码, 私钥只存在发布机, 见 ``docs/更新与发布.md``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from rdc.crypto.identity import b64decode, b64encode, canonical_json

SIGNATURE_ALG = "ed25519"
SIGNATURE_FIELD = "signature"

# 发布公钥: 只含公钥; 私钥不入库、不进包、不进镜像
PUBLIC_KEY_B64 = "4nj805+xRuL5hngRJ8bRetm3uxlh+z/DfktFxtPC+hk="
KEY_ID = "e12dd98cb054069f"


class ManifestSignatureError(ValueError):
    """签名缺失、算法不符、密钥不匹配或验签失败; 一律拒绝更新."""


@dataclass(frozen=True)
class TrustAnchor:
    """受信任的清单签名公钥; 测试可注入临时公钥, 产品只使用内置值."""

    public_key_b64: str
    key_id: str


BUILTIN_TRUST = TrustAnchor(public_key_b64=PUBLIC_KEY_B64, key_id=KEY_ID)


def key_id_for(public_key_raw: bytes) -> str:
    """由公钥原始字节计算 key_id."""
    return hashlib.sha256(public_key_raw).hexdigest()[:16]


def signing_bytes(payload: dict[str, object]) -> bytes:
    """返回参与签名的规范 JSON 字节串, signature 字段本身不参与签名."""
    body = {key: value for key, value in payload.items() if key != SIGNATURE_FIELD}
    return canonical_json(body)


def sign_payload(payload: dict[str, object], private_key: Ed25519PrivateKey, key_id: str) -> dict[str, object]:
    """用发布私钥为清单签名, 返回带 signature 字段的新字典; 仅发布脚本使用."""
    body = {key: value for key, value in payload.items() if key != SIGNATURE_FIELD}
    signature = private_key.sign(signing_bytes(body))
    signed = dict(body)
    signed[SIGNATURE_FIELD] = {
        "alg": SIGNATURE_ALG,
        "key_id": key_id,
        "value": b64encode(signature),
    }
    return signed


def verify_manifest_signature(payload: object, trust: TrustAnchor = BUILTIN_TRUST) -> None:
    """校验清单签名; 任一环节不通过都抛出 ManifestSignatureError."""
    if not isinstance(payload, dict):
        raise ManifestSignatureError("更新清单不是 JSON 对象, 无法验签")
    block = payload.get(SIGNATURE_FIELD)
    if not isinstance(block, dict):
        raise ManifestSignatureError("更新清单缺少 signature 字段, 拒绝更新")
    alg = str(block.get("alg", "")).strip().lower()
    if alg != SIGNATURE_ALG:
        raise ManifestSignatureError(f"更新清单签名算法不受支持: {alg or '空'}, 只接受 {SIGNATURE_ALG}")
    key_id = str(block.get("key_id", "")).strip()
    if key_id != trust.key_id:
        raise ManifestSignatureError(
            f"更新清单签名密钥不匹配: 清单 {key_id or '空'}, 客户端内置 {trust.key_id}, 拒绝更新"
        )
    value = str(block.get("value", "")).strip()
    if not value:
        raise ManifestSignatureError("更新清单签名为空, 拒绝更新")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(b64decode(trust.public_key_b64))
        public_key.verify(b64decode(value), signing_bytes(payload))
    except (InvalidSignature, ValueError) as error:
        raise ManifestSignatureError("更新清单签名校验失败: 内容与签名不符, 可能被替换或篡改") from error
