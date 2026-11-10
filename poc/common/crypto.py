# ==========================================================================================
# 文件        : crypto.py
# 所属目录    : poc/common/
# 文件用途    : 提供 PoC 所需的设备身份、规范 JSON 签名、X25519 会话密钥与 AEAD 加解密
# 架构定位    : PoC 公共基础层, 仅被 PoC 信令与 UDP 直连模块引用
# 核心功能    : Ed25519 签名与验签; 规范 JSON; X25519 + HKDF-SHA256; ChaCha20-Poly1305
# 职责边界    : 只做密码学原语与数据结构; 不发网络包, 不读配置, 不写日志
# 运行环境    : Python 3.12; Windows 11 与 Ubuntu 22.04 均可运行
# 依赖关系    : 下游 signaling.py 与 udp_direct.py 引用本模块
# ==========================================================================================

"""PoC 密码学与签名公共模块."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENVELOPE_VERSION = 1
RECV_REPLAY_WINDOW = 64
RECV_REPLAY_MASK = (1 << RECV_REPLAY_WINDOW) - 1
HKDF_INFO_PREFIX = b"rdc-poc-v1"


def base64_encode(data: bytes) -> str:
    """把字节编码为 Base64 文本.

    :param data: 待编码字节
    :type data: bytes
    :return: Base64 文本
    :rtype: str
    """
    return base64.b64encode(data).decode("ascii")


def base64_decode(text: str) -> bytes:
    """把 Base64 文本解码为字节.

    :param text: Base64 文本
    :type text: str
    :return: 解码后的字节
    :rtype: bytes
    :raises ValueError: 文本不是合法 Base64 时抛出
    """
    return base64.b64decode(text.encode("ascii"), validate=True)


def canonical_json(fields: dict[str, Any]) -> bytes:
    """按签名字口径生成规范 JSON 字节.

    规则: UTF-8, 键名按字典序, 无多余空白, 不转义非必要字符.

    :param fields: 参与签名的字段
    :type fields: dict
    :return: 规范 JSON 字节
    :rtype: bytes
    """
    return json.dumps(
        fields,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def now_iso() -> str:
    """返回带时区的 ISO 8601 当前时间.

    :return: 形如 2026-11-10T10:00:00+08:00 的文本
    :rtype: str
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def device_id_from_public_key(public_key: bytes) -> str:
    """由 Ed25519 公钥生成设备标识.

    取 SHA-256 摘要的 Base32 文本前 20 字符, 与设计书 3.8 一致.

    :param public_key: 32 字节 Ed25519 公钥
    :type public_key: bytes
    :return: 20 字符设备标识
    :rtype: str
    """
    digest = hashlib.sha256(public_key).digest()
    return base64.b32encode(digest).decode("ascii")[:20]


# ================================ DeviceIdentity ================================


class DeviceIdentity:
    """PoC 设备身份, 持有 Ed25519 密钥对并负责消息签名与验签.

    工作流程:
        1. 调用 create() 生成密钥对与设备标识.
        2. 用 make_envelope() 组装七字段包络并签名.
        3. 对端用 verify_envelope() 用公钥验签.

    实例属性清单:
        private_key (Ed25519PrivateKey): 本机私钥, 只在本进程内使用.
        public_key_bytes (bytes): 32 字节公钥, 可上传信令服务.
        device_id (str): 公钥摘要生成的设备标识.

    构造参数:
        :param private_key: Ed25519 私钥
        :type private_key: Ed25519PrivateKey

    运行约束:
        单进程内使用, 不跨进程共享私钥.
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self.private_key = private_key
        self.public_key_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.device_id = device_id_from_public_key(self.public_key_bytes)

    @classmethod
    def create(cls) -> "DeviceIdentity":
        """生成新的设备身份.

        :return: 新的设备身份实例
        :rtype: DeviceIdentity
        """
        return cls(Ed25519PrivateKey.generate())

    def sign(self, data: bytes) -> bytes:
        """用本机私钥签名.

        :param data: 待签名字节
        :type data: bytes
        :return: 64 字节签名
        :rtype: bytes
        """
        return self.private_key.sign(data)

    def make_envelope(
        self,
        message_type: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """组装七字段包络并附加签名.

        :param message_type: 消息类型, 取值见协议契约
        :type message_type: str
        :param payload: 消息体
        :type payload: dict
        :param session_id: 会话标识, 会话相关消息必填
        :type session_id: str 或 None
        :return: 含 sig 字段的包络
        :rtype: dict
        """
        envelope: dict[str, Any] = {
            "v": ENVELOPE_VERSION,
            "type": message_type,
            "id": str(uuid.uuid4()),
            "session_id": session_id or "",
            "ts": now_iso(),
            "payload": payload,
        }
        envelope["sig"] = base64_encode(self.sign(canonical_json(envelope)))
        return envelope


def verify_envelope(public_key_b64: str, envelope: dict[str, Any]) -> bool:
    """用设备公钥验证包络签名.

    :param public_key_b64: Base64 编码的 Ed25519 公钥
    :type public_key_b64: str
    :param envelope: 含 sig 字段的包络
    :type envelope: dict
    :return: 签名有效返回 True
    :rtype: bool
    :raises ValueError: 包络或公钥格式非法时抛出
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = base64_decode(str(envelope["sig"]))
    fields = {key: value for key, value in envelope.items() if key != "sig"}
    public_key = Ed25519PublicKey.from_public_bytes(base64_decode(public_key_b64))
    try:
        public_key.verify(signature, canonical_json(fields))
    except Exception:
        return False
    return True


# ================================ EphemeralKeyPair ================================


class EphemeralKeyPair:
    """X25519 临时密钥对, 用于会话密钥协商.

    实例属性清单:
        private_key (X25519PrivateKey): 临时私钥.
        public_bytes (bytes): 32 字节临时公钥.

    构造参数:
        :param private_key: X25519 私钥
        :type private_key: X25519PrivateKey

    运行约束:
        每次会话新建, 用完即弃, 不落盘.
    """

    def __init__(self, private_key: X25519PrivateKey) -> None:
        self.private_key = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @classmethod
    def generate(cls) -> "EphemeralKeyPair":
        """生成临时密钥对.

        :return: 新的临时密钥对
        :rtype: EphemeralKeyPair
        """
        return cls(X25519PrivateKey.generate())

    def exchange(self, peer_public_key: bytes) -> bytes:
        """与对端临时公钥协商共享密钥.

        :param peer_public_key: 对端 32 字节 X25519 公钥
        :type peer_public_key: bytes
        :return: 32 字节共享密钥
        :rtype: bytes
        """
        peer_key = X25519PublicKey.from_public_bytes(peer_public_key)
        return self.private_key.exchange(peer_key)


# ================================ AeadSession ================================


class AeadSession:
    """基于 ChaCha20-Poly1305 的双向会话加解密器.

    工作流程:
        1. 由协商出的密钥分别创建发送与接收 cipher.
        2. 每次加密使用 4 字节随机前缀加 8 字节单调计数器组成 12 字节 nonce.
        3. 接收端按 64 位滑动窗口登记计数器: 同一密文重复到达直接拒绝,
           落后窗口的计数器视为越界并断链; 窗口内乱序仍可容忍, 适配 UDP 传输.

    实例属性清单:
        send_key (bytes): 本端发送密钥.
        recv_key (bytes): 本端接收密钥.
        send_counter (int): 发送计数器, 只增不减.
        recv_counter (int): 已接受的接收帧数量.

    构造参数:
        :param send_key: 32 字节发送密钥
        :type send_key: bytes
        :param recv_key: 32 字节接收密钥
        :type recv_key: bytes

    运行约束:
        单会话单方向使用, 计数器不得回绕.
    """

    def __init__(self, send_key: bytes, recv_key: bytes) -> None:
        self.send_key = send_key
        self.recv_key = recv_key
        self.send_counter = 0
        self.recv_counter = 0
        self._send_cipher = ChaCha20Poly1305(send_key)
        self._recv_cipher = ChaCha20Poly1305(recv_key)
        self._send_prefix = os.urandom(4)
        self._recv_prefix: bytes | None = None
        self._recv_highest = -1
        self._recv_bitmap = 0

    def encrypt(self, payload: bytes) -> bytes:
        """加密一段载荷, 输出前缀加计数器加密文.

        :param payload: 明文载荷
        :type payload: bytes
        :return: 4 字节前缀 + 8 字节计数器 + nonce 之后的密文
        :rtype: bytes
        """
        counter = self.send_counter.to_bytes(8, "big")
        nonce = self._send_prefix + counter
        ciphertext = self._send_cipher.encrypt(nonce, payload, None)
        self.send_counter += 1
        return self._send_prefix + counter + ciphertext

    def decrypt(self, data: bytes) -> bytes:
        """解密一段载荷, 并拒绝重放与越界计数器.

        :param data: encrypt() 输出的字节
        :type data: bytes
        :return: 明文载荷
        :rtype: bytes
        :raises ValueError: 长度不足、前缀变化、重放、计数器越界或认证失败时抛出
        """
        if len(data) < 12:
            raise ValueError("密文长度不足")
        prefix = data[:4]
        counter = data[4:12]
        if self._recv_prefix is None:
            self._recv_prefix = prefix
        elif prefix != self._recv_prefix:
            raise ValueError("nonce 前缀与首包不一致")
        counter_value = int.from_bytes(counter, "big")
        self._check_counter(counter_value)
        nonce = prefix + counter
        plaintext = self._recv_cipher.decrypt(nonce, data[12:], None)
        self._commit_counter(counter_value)
        self.recv_counter += 1
        return plaintext

    def _check_counter(self, counter_value: int) -> None:
        """在任何认证动作之前检查计数器是否已接收或已滑出窗口."""
        if self._recv_highest < 0:
            return
        if counter_value > self._recv_highest:
            return
        distance = self._recv_highest - counter_value
        if distance >= RECV_REPLAY_WINDOW:
            raise ValueError("计数器落后接收窗口, 已断开")
        if (self._recv_bitmap >> distance) & 1:
            raise ValueError("检测到重放的密文帧")

    def _commit_counter(self, counter_value: int) -> None:
        """认证通过后登记计数器, 供后续帧去重."""
        if self._recv_highest < 0:
            self._recv_highest = counter_value
            self._recv_bitmap = 1
            return
        if counter_value > self._recv_highest:
            shift = counter_value - self._recv_highest
            self._recv_bitmap = (self._recv_bitmap << shift) & RECV_REPLAY_MASK
            self._recv_bitmap |= 1
            self._recv_highest = counter_value
            return
        self._recv_bitmap |= 1 << (self._recv_highest - counter_value)


def derive_aead_session(
    shared_secret: bytes,
    salt: bytes,
    initiator: bool,
    *,
    context: bytes = b"",
) -> AeadSession:
    """由共享密钥派生双向 AEAD 会话.

    ``context`` 用于把会话号与双方设备标识绑进派生过程; 为空时保持原派生结果,
    仅供只有单一会话概念的历史 PoC 路径使用.

    :param shared_secret: X25519 协商结果
    :type shared_secret: bytes
    :param salt: 握手 Cookie, 用作 HKDF salt
    :type salt: bytes
    :param initiator: 本端是否为主动发起方
    :type initiator: bool
    :param context: 会话绑定上下文, 为空表示不绑定
    :type context: bytes
    :return: 双向会话加解密器
    :rtype: AeadSession
    """
    info = HKDF_INFO_PREFIX if not context else HKDF_INFO_PREFIX + b"|" + context
    effective_salt = salt if not context else salt + b"|" + context
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=effective_salt,
        info=info,
    ).derive(shared_secret)
    key_a = material[:32]
    key_b = material[32:]
    if initiator:
        return AeadSession(send_key=key_a, recv_key=key_b)
    return AeadSession(send_key=key_b, recv_key=key_a)
