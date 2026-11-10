# ==========================================================================================
# 文件        : handshake.py
# 所属目录    : src/rdc/session/
# 文件用途    : 连接码挑战应答与 hs=2 数据面握手
# 架构定位    : 协议基础层; 被控端与控制端共用, 保证两端握手规则只有一份
# 核心功能    : 连接码 HMAC 挑战应答; 失败退避与换码; hello/ack 会话绑定签名与 HKDF 上下文
# 职责边界    : 只做握手; 不建连接、不启服务、不注入输入、不落盘
# 运行环境    : Windows 与 Ubuntu, Python 3.12
# 依赖关系    : rdc.session.crypto 与 rdc.crypto.identity; 线格式走 rdc.session.wire
# ==========================================================================================

"""连接码挑战应答与 hs=2 握手.

hs=2 要求 hello 与 ack 都携带 ``session_id`` 与双方 ``peer_device_id``, 两者同时进入
签名校验与 HKDF 的 info/salt, 任何一项不匹配即握手失败; 旧握手一律拒绝.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import time
from base64 import b64decode, b64encode
from collections import deque
from collections.abc import Callable
from typing import Any

from rdc.crypto.identity import DeviceIdentity, device_id_from_public_key, verify_signature
from rdc.session.crypto import HANDSHAKE_VERSION, AeadSession, EphemeralKeyPair, derive_aead_session
from rdc.session.wire import recv_json, send_json

ACCESS_PROOF_ALG = "hmac-sha256-v1"
ACCESS_FAILURE_LIMIT = 5
ACCESS_MAX_BACKOFF_S = 30.0
ACCESS_NONCE_CACHE = 256
# 账号准入函数判定为免码的路径: 同账号受信设备与生效授权; 申请制与拒绝不放行
TRUSTED_NO_CODE_ROUTES = frozenset({"passwordless", "grant"})


class HandshakeError(RuntimeError):
    """握手失败, 消息为可直接展示的中文原因."""


def access_proof_message(session_id: str, from_device_id: str, to_device_id: str, nonce_b64: str) -> bytes:
    """生成参与 HMAC 的规范消息: 算法、会话号、双方设备标识与一次性随机数."""
    return f"{ACCESS_PROOF_ALG}|{session_id}|{from_device_id}|{to_device_id}|{nonce_b64}".encode()


def build_access_proof(
    code: str,
    session_id: str,
    from_device_id: str,
    to_device_id: str,
    nonce: bytes | None = None,
) -> dict[str, str]:
    """用连接码对本次会话做挑战应答, 不在信令里传输连接码本身."""
    nonce_b64 = b64encode(os.urandom(16) if nonce is None else nonce).decode("ascii")
    message = access_proof_message(session_id, from_device_id, to_device_id, nonce_b64)
    response = hmac.new(code.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return {"alg": ACCESS_PROOF_ALG, "nonce": nonce_b64, "response": response}


def verify_access_proof(
    code: str,
    session_id: str,
    from_device_id: str,
    to_device_id: str,
    proof: Any,
) -> bool:
    """校验连接码挑战应答; 算法不符、字段缺失或摘要不一致均返回 False."""
    if not isinstance(proof, dict):
        return False
    if str(proof.get("alg", "")) != ACCESS_PROOF_ALG:
        return False
    nonce_b64 = str(proof.get("nonce", ""))
    response = str(proof.get("response", ""))
    if not nonce_b64 or not response:
        return False
    message = access_proof_message(session_id, from_device_id, to_device_id, nonce_b64)
    expected = hmac.new(code.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, response)


class AccessCodeGuard:
    """连接码尝试计数、指数退避、连续失败作废换码与随机数去重."""

    def __init__(
        self,
        rotate: Callable[[], str],
        *,
        limit: int = ACCESS_FAILURE_LIMIT,
        max_backoff_s: float = ACCESS_MAX_BACKOFF_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rotate = rotate
        self.limit = limit
        self.max_backoff_s = max_backoff_s
        self.clock = clock
        self.code = ""
        self.failures = 0
        self.locked_until = 0.0
        self.seen_nonces: deque[str] = deque(maxlen=ACCESS_NONCE_CACHE)

    def set_code(self, code: str) -> None:
        """登记当前生效的连接码."""
        self.code = code

    def lock_remaining(self) -> float:
        """返回剩余冷却秒数, 0 表示不在冷却期."""
        return max(0.0, self.locked_until - self.clock())

    def accept(self, proof: Any, session_id: str, from_device_id: str, to_device_id: str) -> tuple[bool, str]:
        """校验一次 offer 的挑战应答, 返回 (是否通过, 拒绝原因).

        未携带任何挑战应答时按「同账号受信设备免连接码」请求处理, 只认账号
        准入函数的判定; 免掉的仅是连接码, 设备签名、hs=2 握手与 AEAD 校验
        仍由后续握手执行. 未受信、跨账号或无法判定一律拒绝, 由控制端改用
        6 位连接码重新发起并走完整挑战应答.
        """
        remaining = self.lock_remaining()
        if remaining > 0:
            return False, f"连接码尝试过于频繁, 请 {int(remaining) + 1} 秒后重试"
        if proof is None or proof == "":
            return self._accept_trusted(from_device_id, session_id)
        nonce = str(proof.get("nonce", "")) if isinstance(proof, dict) else ""
        if not verify_access_proof(self.code, session_id, from_device_id, to_device_id, proof):
            return False, self._record_failure()
        if not nonce or nonce in self.seen_nonces:
            return False, "重复的连接请求, 已忽略"
        self.seen_nonces.append(nonce)
        self.failures = 0
        self.locked_until = 0.0
        return True, ""

    def _accept_trusted(self, from_device_id: str, session_id: str) -> tuple[bool, str]:
        """按账号准入函数判定是否允许免连接码.

        只认 ``resolve_connection_access`` 的返回结果, 不采信控制端自述的同账号
        声明; 判定失败或路径不在免码白名单时拒绝, 由控制端改用 6 位连接码.
        """
        from rdc.commands.accounts import resolve_connection_access

        try:
            result = resolve_connection_access(from_device_id, "control", session_id=session_id)
        except (OSError, RuntimeError, ValueError, KeyError):
            return False, "账号准入判定失败, 请改用 6 位连接码"
        if not result.ok:
            return False, str(result.message or "账号准入判定失败, 请改用 6 位连接码")
        if str((result.data or {}).get("route", "")) not in TRUSTED_NO_CODE_ROUTES:
            return False, "该设备未受信, 请改用 6 位连接码"
        self.failures = 0
        self.locked_until = 0.0
        return True, ""

    def _record_failure(self) -> str:
        """登记一次失败: 达到上限即换码, 否则按次数指数退避."""
        self.failures += 1
        if self.failures >= self.limit:
            self.code = self.rotate()
            self.failures = 0
            self.locked_until = 0.0
            self.seen_nonces.clear()
            return f"连接码连续错误 {self.limit} 次, 已作废并更换新连接码"
        backoff = min(2.0**self.failures, self.max_backoff_s)
        self.locked_until = self.clock() + backoff
        return f"连接码不正确, 已拒绝 ({self.failures}/{self.limit})"


def handshake_context(session_id: str, controller_device_id: str, host_device_id: str) -> bytes:
    """会话绑定上下文: 会话号 + 控制端标识 + 被控端标识, 顺序固定."""
    return f"hs{HANDSHAKE_VERSION}|{session_id}|{controller_device_id}|{host_device_id}".encode()


def _without_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "sig"}


def controller_handshake(
    sock: socket.socket,
    identity: DeviceIdentity,
    session_id: str,
    expected_peer_device_id: str,
) -> AeadSession:
    """控制端发起 hs=2 握手, 校验被控端身份与会话绑定."""
    if not session_id or not expected_peer_device_id:
        raise HandshakeError("控制端握手缺少会话号或目标设备标识")
    pair = EphemeralKeyPair.generate()
    salt = b64encode(os.urandom(16)).decode("ascii")
    hello: dict[str, Any] = {
        "kind": "e2e_hello",
        "v": 1,
        "hs": HANDSHAKE_VERSION,
        "x25519": b64encode(pair.public_bytes).decode("ascii"),
        "salt": salt,
        "device_id": identity.device_id,
        "public_key": identity.public_key_b64,
        "session_id": session_id,
        "peer_device_id": expected_peer_device_id,
    }
    hello["sig"] = identity.sign(hello)
    send_json(sock, hello)
    ack = recv_json(sock)
    if ack is None or ack.get("kind") != "e2e_hello_ack":
        raise HandshakeError("被控端 AEAD 握手应答缺失")
    if ack.get("hs") != HANDSHAKE_VERSION:
        raise HandshakeError("被控端 AEAD 握手未声明会话绑定")
    if str(ack.get("session_id", "")) != session_id:
        raise HandshakeError("被控端 AEAD 握手会话号不匹配")
    if str(ack.get("peer_device_id", "")) != identity.device_id:
        raise HandshakeError("被控端 AEAD 握手未回指本控制端")
    public_key = str(ack.get("public_key", ""))
    if not verify_signature(public_key, _without_signature(ack), str(ack.get("sig", ""))):
        raise HandshakeError("被控端 AEAD 握手签名无效")
    if device_id_from_public_key(b64decode(public_key)) != str(ack.get("device_id", "")):
        raise HandshakeError("被控端 device_id 与公钥不一致")
    if str(ack.get("device_id", "")) != expected_peer_device_id:
        raise HandshakeError("被控端 device_id 与本次会话期望的对端不一致")
    shared = pair.exchange(b64decode(str(ack["x25519"])))
    context = handshake_context(session_id, identity.device_id, expected_peer_device_id)
    return derive_aead_session(shared, b64decode(salt), initiator=True, context=context)


def host_handshake(
    sock: socket.socket,
    identity: DeviceIdentity,
    session_id: str,
    expected_peer_device_id: str,
    hello: dict[str, Any] | None = None,
) -> AeadSession:
    """被控端应答 hs=2 握手, 校验控制端身份与会话绑定; 旧握手直接拒绝.

    ``hello`` 为首帧分流时已读出的 ``e2e_hello``: 直连媒体连接由中继服务先按
    长度前缀读出首帧, 再交给这里, 避免重复读取同一条 socket.
    """
    if not session_id or not expected_peer_device_id:
        raise HandshakeError("被控端握手缺少会话号或对端设备标识")
    if hello is None:
        hello = recv_json(sock)
    if hello is None or hello.get("kind") != "e2e_hello":
        raise HandshakeError("未收到控制端 AEAD 握手")
    public_key = str(hello.get("public_key", ""))
    if not verify_signature(public_key, _without_signature(hello), str(hello.get("sig", ""))):
        raise HandshakeError("控制端 AEAD 握手签名无效")
    if device_id_from_public_key(b64decode(public_key)) != str(hello.get("device_id", "")):
        raise HandshakeError("控制端 device_id 与公钥不一致")
    if str(hello.get("device_id", "")) != expected_peer_device_id:
        raise HandshakeError("控制端 device_id 与本次会话允许的对端不一致")
    if hello.get("hs") != HANDSHAKE_VERSION:
        raise HandshakeError("控制端 AEAD 握手未声明会话绑定, 已拒绝")
    if str(hello.get("session_id", "")) != session_id:
        raise HandshakeError("控制端 AEAD 握手会话号不匹配")
    if str(hello.get("peer_device_id", "")) != identity.device_id:
        raise HandshakeError("控制端 AEAD 握手未指向本被控端")
    pair = EphemeralKeyPair.generate()
    ack: dict[str, Any] = {
        "kind": "e2e_hello_ack",
        "v": 1,
        "hs": HANDSHAKE_VERSION,
        "x25519": b64encode(pair.public_bytes).decode("ascii"),
        "device_id": identity.device_id,
        "public_key": identity.public_key_b64,
        "session_id": session_id,
        "peer_device_id": expected_peer_device_id,
    }
    ack["sig"] = identity.sign(ack)
    send_json(sock, ack)
    shared = pair.exchange(b64decode(str(hello["x25519"])))
    context = handshake_context(session_id, expected_peer_device_id, identity.device_id)
    return derive_aead_session(shared, b64decode(str(hello["salt"])), initiator=False, context=context)
