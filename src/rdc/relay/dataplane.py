# ==========================================================================================
# 文件        : dataplane.py
# 所属目录    : src/rdc/relay/
# 文件用途    : 中继数据面最小实现: 按协议 v1 第六节转发端到端 AEAD 密文与最少元数据
# 架构定位    : 中继服务层的数据面, 只做会话配对、密文转发与预算限速
# 核心功能    : relay_token 校验; 按 session_id 配对发布方与订阅方; 令牌桶按预算限速丢帧
# 职责边界    : 不解析屏幕、输入与剪贴板; 不解密; 不落盘; 不改系统网络配置
# 运行环境    : Ubuntu 20.04/22.04 x86_64, Python 3.8 及以上
# 依赖关系    : rdc.relay.server 的帧收发; rdc.signaling.server 的 relay_token 授予记录
# ==========================================================================================

"""中继数据面.

帧格式与信令一致: 4 字节大端无符号长度 + 负载.
首个帧是明文握手的 JSON, 之后每帧都是不透明密文, 中继只转发不解析.

握手帧字段: plane="data"、v=1、type="relay_hello"、relay_token、session_id、role。
role 取 publisher（被控端上行, 受预算限速）或 subscriber（控制端下行）。
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from rdc.core.logging import log_event
from rdc.relay.server import send_frame

# 数据面单帧上限 6 MiB: 剪贴板 image ≤ 4 MiB, Base64 后约 5.34 MiB, 再加包络与 AEAD 开销;
# 超过按协议错误断开, 不缓冲, 防止内存被撑爆
DATA_MAX_FRAME_BYTES = 6 << 20
ROLES = ("publisher", "subscriber")


@dataclass(frozen=True)
class RelayGrant:
    """一次中继预算授予, 由信令服务在 relay_allocate 时签发.

    :param session_id: 绑定的会话
    :param budget_kbps: 授予的预算, 单位 kbps
    :param expires_at: 过期时刻, epoch 秒
    :param scope: control 正式会话 / view 屏幕墙预览(只允许订阅)
    """

    session_id: str
    budget_kbps: int
    expires_at: float
    scope: str = "control"


class TokenBucket:
    """按字节计量的令牌桶, 用于预算限速.

    :param rate_bps: 每秒允许转发的字节数
    :param capacity_s: 桶容量对应的秒数; 越小越严格, 越大越允许突发
    """

    def __init__(self, rate_bps: float, capacity_s: float = 0.5) -> None:
        self.rate_bps = max(1.0, rate_bps)
        self.capacity = max(1.0, self.rate_bps * capacity_s)
        self._tokens = self.capacity
        self._stamp = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, size: int) -> bool:
        """判断是否放行 size 字节, 放行则扣减令牌."""
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate_bps)
            self._stamp = now
            if size > self.capacity:
                # 单帧大于桶容量时按帧长扩容, 否则永远放不过大帧
                self.capacity = float(size)
                self._tokens = self.capacity
            if self._tokens < size:
                return False
            self._tokens -= size
            return True


@dataclass
class SessionStats:
    """单会话转发计数, 供健康检查与证据输出."""

    bytes_forwarded: int = 0
    frames_forwarded: int = 0
    frames_dropped: int = 0
    peer_waiting: bool = False


@dataclass
class _Endpoint:
    sock: socket.socket
    role: str
    write_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _Session:
    session_id: str
    budget_kbps: int
    expires_at: float = 0.0
    publisher: _Endpoint | None = None
    subscriber: _Endpoint | None = None
    bucket: TokenBucket | None = None
    stats: SessionStats = field(default_factory=SessionStats)
    ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def counter(self, endpoint: _Endpoint) -> _Endpoint | None:
        return self.subscriber if endpoint.role == "publisher" else self.publisher


class RelayDataPlane:
    """数据面: 接收已握手的连接, 按会话配对后双向转发密文.

    :param resolve_grant: 由 relay_token 取授予记录, 返回 None 表示令牌无效或过期
    :param max_sessions: 允许的并发数据会话上限
    """

    def __init__(self, resolve_grant: Callable[[str], RelayGrant | None], max_sessions: int) -> None:
        self._resolve = resolve_grant
        self._max_sessions = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self.rejected_tokens = 0
        self.rejected_sessions = 0
        # 累计计数: 会话断开后仍保留, 供健康检查与证据核对
        self._totals = {"bytes_forwarded": 0, "frames_forwarded": 0, "frames_dropped": 0}

    # ------------------------------------------------------------------ 握手
    def parse_hello(self, payload: bytes) -> dict[str, Any] | None:
        """识别数据面握手帧; 不是数据面握手返回 None."""
        try:
            hello = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(hello, dict) or hello.get("plane") != "data":
            return None
        return hello

    def attach(self, connection: socket.socket, hello: dict[str, Any]) -> None:
        """校验握手并把连接接入会话; 失败回 error 帧后关闭."""
        grant = self._validate(hello)
        if isinstance(grant, str):
            self._reject(connection, "TOKEN_INVALID", grant)
            return
        session_id = str(hello["session_id"])
        role = str(hello["role"])

        endpoint = _Endpoint(sock=connection, role=role)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                if len(self._sessions) >= self._max_sessions:
                    self.rejected_sessions += 1
                    self._reject(connection, "RELAY_FULL", "中继会话已满")
                    return
                session = _Session(session_id=session_id, budget_kbps=grant.budget_kbps, expires_at=grant.expires_at)
                session.bucket = TokenBucket(grant.budget_kbps * 1000 / 8)
                self._sessions[session_id] = session
            if getattr(session, role) is not None:
                self._reject(connection, "AUTH_FAILED", f"该会话已有 {role}")
                return
            setattr(session, role, endpoint)
            if session.publisher is not None and session.subscriber is not None:
                session.ready.set()

        log_event(
            "relay.dataplane",
            "session.attached",
            session_id=session_id,
            role=role,
            budget_kbps=session.budget_kbps,
        )

        send_frame(
            connection,
            {
                "plane": "data",
                "v": 1,
                "type": "relay_ready",
                "session_id": session_id,
                "role": role,
                "budget_kbps": session.budget_kbps,
            },
        )
        session.ready.wait(timeout=30.0)
        try:
            self._pump(connection, endpoint, session)
        finally:
            self._detach(session, endpoint)

    # ------------------------------------------------------------------ 内部
    def _validate(self, hello: dict[str, Any]) -> RelayGrant | str:
        token = str(hello.get("relay_token") or "")
        session_id = str(hello.get("session_id") or "")
        role = str(hello.get("role") or "")
        if hello.get("v") != 1:
            return "协议版本不兼容"
        if role not in ROLES:
            return "role 必须是 publisher 或 subscriber"
        if not token or not session_id:
            return "缺少 relay_token 或 session_id"
        grant = self._resolve(token)
        if grant is None or grant.expires_at <= time.time():
            self.rejected_tokens += 1
            return "relay_token 无效或已过期"
        if grant.session_id != session_id:
            self.rejected_tokens += 1
            return "relay_token 与会话不匹配"
        if grant.scope == "view" and role != "subscriber":
            # 预览令牌只读: 只能订阅, 不能作为发布端推流
            self.rejected_tokens += 1
            return "预览令牌只允许订阅方向"
        return grant

    def _reject(self, connection: socket.socket, code: str, message: str) -> None:
        log_event("relay.dataplane", "session.rejected", level="WARNING", error_code=code, reason=message)
        try:
            send_frame(connection, {"plane": "data", "v": 1, "type": "relay_error", "code": code, "message": message})
        except OSError:
            pass
        finally:
            with suppress(OSError):
                connection.close()

    def _pump(self, connection: socket.socket, endpoint: _Endpoint, session: _Session) -> None:
        """循环读帧并转发给对端; 发布方向按预算限速, 超预算丢帧不缓冲."""
        while not self._closed.is_set():
            if session.expires_at and time.time() > session.expires_at:
                # 授予到期即断开, 不给过期令牌留下长期转发通道
                return
            try:
                payload = _recv_frame_limited(connection)
            except (OSError, ValueError):
                return
            if payload is None:
                return
            if endpoint.role == "publisher" and session.bucket is not None and not session.bucket.allow(len(payload)):
                session.stats.frames_dropped += 1
                with self._lock:
                    self._totals["frames_dropped"] += 1
                continue
            peer = session.counter(endpoint)
            if peer is None:
                return
            try:
                with peer.write_lock:
                    peer.sock.sendall(struct.pack(">I", len(payload)) + payload)
            except OSError:
                return
            session.stats.bytes_forwarded += len(payload)
            session.stats.frames_forwarded += 1
            with self._lock:
                self._totals["bytes_forwarded"] += len(payload)
                self._totals["frames_forwarded"] += 1

    def _detach(self, session: _Session, endpoint: _Endpoint) -> None:
        empty = False
        with self._lock:
            if getattr(session, endpoint.role) is endpoint:
                setattr(session, endpoint.role, None)
            if session.publisher is None and session.subscriber is None:
                self._sessions.pop(session.session_id, None)
                empty = True
        session.ready.set()
        log_event(
            "relay.dataplane",
            "session.detached",
            session_id=session.session_id,
            role=endpoint.role,
            frames_forwarded=session.stats.frames_forwarded,
            frames_dropped=session.stats.frames_dropped,
            bytes_forwarded=session.stats.bytes_forwarded,
            session_closed=empty,
        )

    # ------------------------------------------------------------------ 观测与生命周期
    def snapshot(self) -> dict[str, Any]:
        """返回可公开的统计, 不含令牌与内容."""
        with self._lock:
            sessions = list(self._sessions.values())
            totals = dict(self._totals)
        return {
            "sessions": len(sessions),
            "max_sessions": self._max_sessions,
            "active_publishers": sum(1 for item in sessions if item.publisher is not None),
            "active_subscribers": sum(1 for item in sessions if item.subscriber is not None),
            "rejected_tokens": self.rejected_tokens,
            "rejected_sessions": self.rejected_sessions,
            "bytes_forwarded": sum(item.stats.bytes_forwarded for item in sessions),
            "frames_forwarded": sum(item.stats.frames_forwarded for item in sessions),
            "frames_dropped": sum(item.stats.frames_dropped for item in sessions),
            "total_bytes_forwarded": totals["bytes_forwarded"],
            "total_frames_forwarded": totals["frames_forwarded"],
            "total_frames_dropped": totals["frames_dropped"],
        }

    def close(self) -> None:
        """关闭数据面: 停止转发并断开所有会话."""
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.ready.set()
            for endpoint in (session.publisher, session.subscriber):
                if endpoint is not None:
                    with suppress(OSError):
                        endpoint.sock.close()


def _recv_frame_limited(connection: socket.socket) -> bytes | None:
    """读一个数据面帧, 长度超过 64 KiB 直接判为非法."""
    header = _recv_exactly(connection, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > DATA_MAX_FRAME_BYTES:
        raise ValueError(f"数据帧长度非法: {length}")
    payload = _recv_exactly(connection, length)
    if payload is None:
        raise ValueError("数据帧负载不完整")
    return payload


def _recv_exactly(connection: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = connection.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)
