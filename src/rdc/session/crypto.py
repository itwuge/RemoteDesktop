# ==========================================================================================
# 文件        : crypto.py
# 所属目录    : src/rdc/session/
# 文件用途    : 会话密钥派生与 AEAD 双向加解密的产品实现
# 架构定位    : 协议基础层; 被数据面通道、被控端与控制端共用
# 核心功能    : X25519 临时密钥交换; HKDF-SHA256 派生; ChaCha20-Poly1305 与 64 位滑动窗口
# 职责边界    : 只做密码学原语; 不发网络包、不读配置、不写日志、不落盘
# 运行环境    : Windows 与 Ubuntu, Python 3.12
# 依赖关系    : 只依赖 cryptography 与标准库; 派生参数与 hs=2 契约保持一致
# ==========================================================================================

"""会话加密原语.

接收侧按 64 位滑动窗口登记计数器: 计数器重复即拒, 落后窗口视为越界断链,
窗口内乱序仍可接受, 以适配 UDP 传输.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RECV_REPLAY_WINDOW = 64
RECV_REPLAY_MASK = (1 << RECV_REPLAY_WINDOW) - 1
HKDF_INFO_PREFIX = b"rdc-poc-v1"
HANDSHAKE_VERSION = 2


class EphemeralKeyPair:
    """X25519 临时密钥对, 每次会话新建、用完即弃、不落盘."""

    def __init__(self, private_key: X25519PrivateKey) -> None:
        self.private_key = private_key
        self.public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @classmethod
    def generate(cls) -> EphemeralKeyPair:
        """生成临时密钥对."""
        return cls(X25519PrivateKey.generate())

    def exchange(self, peer_public_key: bytes) -> bytes:
        """与对端临时公钥协商共享密钥."""
        return self.private_key.exchange(X25519PublicKey.from_public_bytes(peer_public_key))


class AeadSession:
    """ChaCha20-Poly1305 双向会话加解密器, 接收侧带 64 位滑动窗口去重."""

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
        """加密一段载荷, 输出 4 字节前缀 + 8 字节计数器 + 密文."""
        counter = self.send_counter.to_bytes(8, "big")
        ciphertext = self._send_cipher.encrypt(self._send_prefix + counter, payload, None)
        self.send_counter += 1
        return self._send_prefix + counter + ciphertext

    def decrypt(self, data: bytes) -> bytes:
        """解密并拒绝重放与越界计数器; 认证失败抛异常."""
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
        plaintext = self._recv_cipher.decrypt(prefix + counter, data[12:], None)
        self._commit_counter(counter_value)
        self.recv_counter += 1
        return plaintext

    def _check_counter(self, counter_value: int) -> None:
        """在任何认证动作之前检查计数器是否已接收或已滑出窗口."""
        if self._recv_highest < 0 or counter_value > self._recv_highest:
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


def derive_aead_session(shared_secret: bytes, salt: bytes, initiator: bool, *, context: bytes = b"") -> AeadSession:
    """由共享密钥派生双向 AEAD 会话; context 把会话号与双方设备标识绑进 HKDF 的 info 与 salt."""
    info = HKDF_INFO_PREFIX if not context else HKDF_INFO_PREFIX + b"|" + context
    effective_salt = salt if not context else salt + b"|" + context
    material = HKDF(algorithm=hashes.SHA256(), length=64, salt=effective_salt, info=info).derive(shared_secret)
    key_a = material[:32]
    key_b = material[32:]
    if initiator:
        return AeadSession(send_key=key_a, recv_key=key_b)
    return AeadSession(send_key=key_b, recv_key=key_a)
