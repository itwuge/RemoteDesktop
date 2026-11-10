# ==========================================================================================
# 文件        : udp_direct.py
# 所属目录    : poc/transport/
# 文件用途    : PoC UDP 直连传输, 完成打洞握手、会话密钥协商与密文收发
# 架构定位    : PoC 传输层, 对应设计书 3.7 与协议契约 v1 的直连数据面
# 核心功能    : 三向打洞握手; X25519 + HKDF 会话密钥; ChaCha20-Poly1305 数据面
# 职责边界    : 只做传输与加解密; 不解析媒体, 不选画质档, 不做中继
# 运行环境    : Python 3.12; 标准库 socket 与 cryptography
# 依赖关系    : 引用 common/crypto.py; 被 tools/run_p0_02_smoke.py 引用
# ==========================================================================================

"""PoC UDP 直连传输模块."""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
from dataclasses import dataclass

from poc.common.crypto import AeadSession, DeviceIdentity, EphemeralKeyPair, derive_aead_session

MAGIC = b"RDCP"
TYPE_HELLO = b"H"
TYPE_ACK = b"A"
TYPE_CONFIRM = b"C"
TYPE_DATA = b"D"


@dataclass(frozen=True)
class HandshakeResult:
    """握手结果.

    :param session: 双向 AEAD 会话
    :type session: AeadSession
    :param peer_device_id: 对端设备标识
    :type peer_device_id: str
    :param cookie: 本次握手 Cookie
    :type cookie: bytes
    """

    session: AeadSession
    peer_device_id: str
    cookie: bytes


# ================================ DirectTransport ================================

class DirectTransport:
    """UDP 直连传输端点, 负责打洞握手与密文收发.

    工作流程:
        1. bind() 绑定本地 UDP 端口并暴露候选地址.
        2. 主动方 handshake_active(), 被动方 handshake_passive() 完成三向握手.
        3. send()/recv() 收发 AEAD 密文, 明文不落网络.

    实例属性清单:
        identity (DeviceIdentity): 本机设备身份, 用于握手签名.
        sock (socket.socket): 已绑定的 UDP 套接字.
        session (AeadSession 或 None): 握手完成后的会话.
        peer_address (tuple 或 None): 对端地址.

    构造参数:
        :param identity: 本机设备身份
        :type identity: DeviceIdentity

    运行约束:
        单进程; 一次会话一个套接字; 超时以秒计.
    """

    def __init__(self, identity: DeviceIdentity) -> None:
        self.identity = identity
        self.sock: socket.socket | None = None
        self.session: AeadSession | None = None
        self.peer_address: tuple[str, int] | None = None

    def bind(self, host: str = "0.0.0.0", port: int = 0) -> tuple[str, int]:
        """绑定本地 UDP 端口.

        :param host: 绑定地址
        :type host: str
        :param port: 绑定端口, 0 表示系统分配
        :type port: int
        :return: 实际绑定地址
        :rtype: tuple
        """
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(3.0)
        address = self.sock.getsockname()
        return str(address[0]), int(address[1])

    def close(self) -> None:
        """关闭套接字.

        :return: 无
        :rtype: None
        """
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def handshake_active(self, peer: tuple[str, int], timeout: float = 3.0) -> HandshakeResult:
        """主动发起打洞握手.

        :param peer: 对端 UDP 地址
        :type peer: tuple
        :param timeout: 单步超时秒数
        :type timeout: float
        :return: 握手结果
        :rtype: HandshakeResult
        :raises TimeoutError: 对端无响应时抛出
        """
        assert self.sock is not None
        self.sock.settimeout(timeout)
        cookie = os.urandom(16)
        ephemeral = EphemeralKeyPair.generate()
        hello = MAGIC + TYPE_HELLO + cookie + ephemeral.public_bytes + self.identity.public_key_bytes
        hello += self.identity.sign(b"rdc-poc-hello" + cookie + ephemeral.public_bytes)
        self.sock.sendto(hello, peer)

        data, address = self.sock.recvfrom(4096)
        if not data.startswith(MAGIC + TYPE_ACK) or len(data) < 5 + 16 + 32 + 32 + 64:
            raise ValueError("ACK 格式非法")
        peer_cookie = data[5:21]
        peer_ephemeral = data[21:53]
        peer_public = data[53:85]
        peer_signature = data[85:149]
        self._verify_peer("ack", peer_cookie, peer_ephemeral, peer_public, peer_signature)
        if peer_cookie != cookie:
            raise ValueError("ACK Cookie 与请求不一致")

        shared = ephemeral.exchange(peer_ephemeral)
        session = derive_aead_session(shared, cookie, initiator=True)
        confirm = MAGIC + TYPE_CONFIRM + self._confirm_tag(shared)
        self.sock.sendto(confirm, address)
        self.session = session
        self.peer_address = address
        return HandshakeResult(session=session, peer_device_id=_device_id(peer_public), cookie=cookie)

    def handshake_passive(self, timeout: float = 5.0) -> HandshakeResult:
        """被动等待并完成打洞握手.

        :param timeout: 总超时秒数
        :type timeout: float
        :return: 握手结果
        :rtype: HandshakeResult
        :raises TimeoutError: 未收到握手包时抛出
        """
        assert self.sock is not None
        self.sock.settimeout(timeout)
        data, address = self.sock.recvfrom(4096)
        if not data.startswith(MAGIC + TYPE_HELLO) or len(data) < 5 + 16 + 32 + 32 + 64:
            raise ValueError("HELLO 格式非法")
        cookie = data[5:21]
        peer_ephemeral = data[21:53]
        peer_public = data[53:85]
        peer_signature = data[85:149]
        self._verify_peer("hello", cookie, peer_ephemeral, peer_public, peer_signature)

        ephemeral = EphemeralKeyPair.generate()
        ack = MAGIC + TYPE_ACK + cookie + ephemeral.public_bytes + self.identity.public_key_bytes
        ack += self.identity.sign(b"rdc-poc-ack" + cookie + ephemeral.public_bytes)
        self.sock.sendto(ack, address)

        shared = ephemeral.exchange(peer_ephemeral)
        confirm, _ = self.sock.recvfrom(4096)
        if confirm[:5] != MAGIC + TYPE_CONFIRM or len(confirm) != 5 + 32:
            raise ValueError("CONFIRM 格式非法")
        expected = self._confirm_tag(shared)
        if not hmac.compare_digest(confirm[5:], expected):
            raise ValueError("CONFIRM 校验失败")

        session = derive_aead_session(shared, cookie, initiator=False)
        self.session = session
        self.peer_address = address
        return HandshakeResult(session=session, peer_device_id=_device_id(peer_public), cookie=cookie)

    def send(self, payload: bytes) -> int:
        """加密并发送一段载荷.

        :param payload: 明文载荷
        :type payload: bytes
        :return: 发出的字节数
        :rtype: int
        """
        if self.sock is None or self.session is None or self.peer_address is None:
            raise RuntimeError("尚未完成握手")
        datagram = MAGIC + TYPE_DATA + self.session.encrypt(payload)
        self.sock.sendto(datagram, self.peer_address)
        return len(datagram)

    def recv(self, timeout: float = 3.0) -> tuple[bytes, bytes]:
        """接收并解密一段载荷.

        :param timeout: 超时秒数
        :type timeout: float
        :return: 明文载荷与原始数据报
        :rtype: tuple
        :raises TimeoutError: 超时未收到数据时抛出
        """
        if self.sock is None or self.session is None:
            raise RuntimeError("尚未完成握手")
        self.sock.settimeout(timeout)
        datagram, _ = self.sock.recvfrom(65535)
        if datagram[:5] != MAGIC + TYPE_DATA:
            raise ValueError("DATA 格式非法")
        return self.session.decrypt(datagram[5:]), datagram

    def _verify_peer(self, label: str, cookie: bytes, ephemeral: bytes, public_key: bytes, signature: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(public_key)
        key.verify(signature, f"rdc-poc-{label}".encode("ascii") + cookie + ephemeral)

    @staticmethod
    def _confirm_tag(shared: bytes) -> bytes:
        return hmac.new(shared, b"rdc-poc-confirm", hashlib.sha256).digest()


def _device_id(public_key: bytes) -> str:
    import base64

    return base64.b32encode(hashlib.sha256(public_key).digest()).decode("ascii")[:20]
