# ==========================================================================================
# 文件        : control.py
# 所属目录    : src/rdc/session/
# 文件用途    : 信令面客户端: 注册、挑战应答认证、发送包络、按类型等待应答
# 架构定位    : 信令传输层; 被控端上架与控制端发起连接共用
# 核心功能    : TCP 或 TLS 1.3 连接; register/challenge/auth; offer/answer/relay_allocate
# 职责边界    : 只做信令收发与错误翻译; 不建数据面、不解析媒体
# 运行环境    : Windows 与 Ubuntu, Python 3.12
# 依赖关系    : rdc.crypto.identity 与 rdc.signaling.envelope 的包络规则
# ==========================================================================================

"""信令客户端.

信令服务与中继服务同端口同进程: 先注册认证, 再用七字段包络交换 offer/answer 与
relay_allocate. 错误统一翻译成 ``SignalingError``, 带上服务端错误码便于界面给中文提示.
"""

from __future__ import annotations

import socket
import time
from typing import Any

from rdc.core.errors import AUTH_FAILED, PEER_OFFLINE, PROTOCOL_VERSION_UNSUPPORTED, SIGNALING_UNREACHABLE
from rdc.crypto.identity import DeviceIdentity
from rdc.session.dataplane import DEFAULT_TIMEOUT_S, tls_connect
from rdc.session.wire import recv_json, send_json
from rdc.signaling.envelope import build_envelope


class SignalingError(RuntimeError):
    """信令失败, 携带服务端错误码与中文消息."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SignalingClient:
    """最小可用的真实信令客户端."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 20.0,
        *,
        use_tls: bool = False,
        ca_file: str = "",
        sni: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.use_tls = use_tls
        self.ca_file = ca_file
        self.sni = sni
        self.sock: socket.socket | None = None
        self.identity: DeviceIdentity | None = None
        self.token = ""
        self.tls_info: dict[str, Any] = {}

    def connect(self) -> None:
        """建立 TCP 或 TLS 1.3 连接并设置超时."""
        try:
            if self.use_tls:
                self.sock, self.tls_info = tls_connect(self.host, self.port, self.ca_file, self.sni)
            else:
                self.sock = socket.create_connection((self.host, self.port), timeout=DEFAULT_TIMEOUT_S)
        except (OSError, RuntimeError) as exc:
            raise SignalingError(SIGNALING_UNREACHABLE, f"信令服务不可达: {exc}") from exc
        self.sock.settimeout(self.timeout_s)

    def close(self) -> None:
        """关闭连接, 可重复调用."""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def register_and_auth(self, identity: DeviceIdentity, display_name: str) -> dict[str, Any]:
        """注册、应答挑战并认证, 返回 auth_ok 载荷."""
        if self.sock is None:
            raise SignalingError(SIGNALING_UNREACHABLE, "信令连接未建立")
        self.identity = identity
        register = build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["desktop", "control", "clipboard", "file_transfer"],
                "protocol_version": 1,
                "display_name": display_name,
            },
        )
        send_json(self.sock, register)
        challenge = self.recv_until("challenge")
        nonce = str(challenge.get("payload", {}).get("nonce", ""))
        signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
        auth = build_envelope(
            identity,
            "auth",
            {"device_id": identity.device_id, "signature": signature},
        )
        send_json(self.sock, auth)
        ack = self.recv_until("auth_ok")
        self.token = str(ack.get("payload", {}).get("token", ""))
        return dict(ack.get("payload", {}))

    def send_envelope(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> None:
        """按七字段包络发送一条消息."""
        if self.sock is None or self.identity is None:
            raise SignalingError(SIGNALING_UNREACHABLE, "信令未认证")
        send_json(self.sock, build_envelope(self.identity, msg_type, payload, session_id=session_id))

    def recv_until(self, msg_type: str, timeout_s: float | None = None) -> dict[str, Any]:
        """读取直到指定类型; 遇到 error 直接失败."""
        deadline = time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)
        while time.monotonic() < deadline:
            frame = recv_json(self.sock) if self.sock is not None else None
            if frame is None:
                raise SignalingError(SIGNALING_UNREACHABLE, f"等待 {msg_type} 时信令连接关闭")
            current = str(frame.get("type", ""))
            if current == "error":
                payload = frame.get("payload", {})
                code = str(payload.get("code") or PROTOCOL_VERSION_UNSUPPORTED)
                message = str(payload.get("message") or "信令返回错误")
                raise SignalingError(_friendly_code(code, message), message)
            if current == msg_type:
                return frame
        raise SignalingError(PEER_OFFLINE, f"等待 {msg_type} 超时")

    def request(
        self,
        msg_type: str,
        payload: dict[str, Any],
        reply_type: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """发一条信令并等待指定类型应答."""
        self.send_envelope(msg_type, payload, session_id=session_id)
        return self.recv_until(reply_type)

    def request_offer(self, target_device_id: str, payload: dict[str, Any], session_id: str) -> dict[str, Any]:
        """向目标设备发起 offer 并等待 answer."""
        self.send_envelope("offer", {"to": target_device_id, **payload}, session_id=session_id)
        answer = self.recv_until("answer")
        if str(answer.get("session_id", "")) != session_id:
            raise SignalingError(PROTOCOL_VERSION_UNSUPPORTED, "answer 的会话号与本次会话不一致")
        return dict(answer.get("payload", {}))


def _friendly_code(code: str, message: str) -> str:
    """把服务端错误码映射成本端错误码; 未知错误保留原码."""
    if code == PROTOCOL_VERSION_UNSUPPORTED and "认证" in message:
        return AUTH_FAILED
    return code
