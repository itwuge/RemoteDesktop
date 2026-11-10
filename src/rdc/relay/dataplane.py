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

握手帧字段: plane="data"、v=1、type="relay_hello"、relay_token、session_id、role、account_id（仅预览令牌需要）。
role 取 publisher（被控端上行, 受预算限速）或 subscriber（控制端下行）。
"""

from __future__ import annotations

import json
import select
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
# 对端未接入时最多等这么久再丢帧: 覆盖"订阅端先到、发布端几毫秒后到"的常见时序,
# 同时保证对端始终不出现时不会把发送方长期挂住
PEER_WAIT_SECONDS = 2.0
# 收帧循环内的吊销复核间隔: 与账号库探测缓存同频, 每帧只做一次内存判断, 不额外读盘
REVOCATION_CHECK_INTERVAL_S = 0.5


@dataclass(frozen=True)
class RelayGrant:
    """一次中继预算授予, 由信令服务在 relay_allocate 时签发.

    :param session_id: 绑定的会话
    :param budget_kbps: 授予的预算, 单位 kbps
    :param expires_at: 过期时刻, epoch 秒
    :param scope: control 正式会话 / view 屏幕墙预览(只允许订阅)
    :param account_id: 签发时绑定的账号, 预览令牌握手时必须一致
    :param wall_id: 绑定的屏幕墙, 仅预览令牌使用
    :param device_id: 申请方设备, 用于运行期复核账号令牌是否已吊销
    """

    session_id: str
    budget_kbps: int
    expires_at: float
    scope: str = "control"
    account_id: str = ""
    wall_id: str = ""
    device_id: str = ""


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
    budget_exceeded: int = 0
    peer_waiting: bool = False
    # 1 秒滚动窗口, 用于发现"实际线上码率超过目标档"的越界
    window_started: float = field(default_factory=time.monotonic)
    window_bytes: int = 0
    last_exceed_log: float = 0.0


@dataclass
class _Endpoint:
    sock: socket.socket
    role: str
    token: str = ""
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
    :param target_kbps: 固定档目标码率, 0 表示不做越界统计
    :param token_active: 运行期复核令牌是否仍然有效; 为空时只看授予记录是否存在
    """

    def __init__(
        self,
        resolve_grant: Callable[[str], RelayGrant | None],
        max_sessions: int,
        target_kbps: int = 0,
        token_active: Callable[[str], bool] | None = None,
    ) -> None:
        self._resolve = resolve_grant
        self._max_sessions = max_sessions
        self._target_kbps = target_kbps
        self._token_active = token_active or (lambda token: resolve_grant(token) is not None)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self.rejected_tokens = 0
        self.rejected_sessions = 0
        # 被会话结束或令牌吊销强制断开的计数, 供健康检查与证据核对
        self.revoked_tokens = 0
        self.revoked_sessions = 0
        # 累计计数: 会话断开后仍保留, 供健康检查与证据核对
        self._totals = {
            "bytes_forwarded": 0,
            "frames_forwarded": 0,
            "frames_dropped": 0,
            "budget_exceeded": 0,
        }

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

        endpoint = _Endpoint(sock=connection, role=role, token=str(hello.get("relay_token") or ""))
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
            paired = session.publisher is not None and session.subscriber is not None

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
        if paired:
            # 配对信号放到 relay_ready 之后: 唤醒等待方时对端必然已经能收到数据帧
            session.ready.set()
        # 不在此处等对端: 等待放在收帧循环里, 未配对时按需短等并丢帧, 不阻塞首帧读取也不断链
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
        if not self._token_active(token):
            # 授予还在但账号令牌已吊销: 与无效令牌同样拒绝, 不给吊销后的连接开口子
            self.rejected_tokens += 1
            return "relay_token 已失效"
        if grant.session_id != session_id:
            self.rejected_tokens += 1
            return "relay_token 与会话不匹配"
        if grant.scope == "view":
            # 预览令牌只读: 只能订阅, 不能作为发布端推流
            if role != "subscriber":
                self.rejected_tokens += 1
                return "预览令牌只允许订阅方向"
            # 预览令牌绑定账号: 数据面必须用同一账号出示, 账号不符直接拒绝
            if not grant.account_id or str(hello.get("account_id") or "") != grant.account_id:
                self.rejected_tokens += 1
                return "预览令牌与账号不匹配"
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
        """循环读帧并转发给对端; 发布方向按预算限速, 超预算丢帧不缓冲.

        对端尚未接入时不丢连接: 首帧短暂等待配对(有上限), 仍未配对就丢弃该帧并继续读下一帧,
        既不缓冲也不断开已通过令牌校验的连接.
        """
        waited_peer = False
        reported_unpaired = False
        next_check = time.monotonic() + REVOCATION_CHECK_INTERVAL_S
        while not self._closed.is_set():
            now = time.monotonic()
            if now >= next_check:
                next_check = now + REVOCATION_CHECK_INTERVAL_S
                if not self._token_active(endpoint.token):
                    # 会话结束或令牌吊销: 立即断开连接, 不再转发任何帧
                    self.revoke_token(endpoint.token, "授权已失效")
                    return
            if session.expires_at and time.time() > session.expires_at:
                # 授予到期即断开, 不给过期令牌留下长期转发通道
                return
            # 空闲连接也要按固定间隔醒来复核授权: 跨进程吊销不会有人关闭本端套接字
            readable, _, _ = select.select([connection], [], [], REVOCATION_CHECK_INTERVAL_S)
            if not readable:
                continue
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
            if peer is None and not waited_peer and not session.ready.is_set():
                # 只等第一次: 对端始终不出现时后续帧直接丢弃, 不把发送方反复挂住
                waited_peer = True
                session.ready.wait(PEER_WAIT_SECONDS)
                peer = session.counter(endpoint)
            if peer is None:
                # 对端未配对: 丢帧计数并继续读, 绝不因此断开已通过令牌校验的连接
                session.stats.frames_dropped += 1
                with self._lock:
                    self._totals["frames_dropped"] += 1
                if not reported_unpaired:
                    reported_unpaired = True
                    log_event(
                        "relay.dataplane",
                        "frame.dropped_unpaired",
                        level="WARNING",
                        session_id=session.session_id,
                        role=endpoint.role,
                    )
                continue
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
            self._check_budget(session, endpoint.role, len(payload))

    def _check_budget(self, session: _Session, role: str, size: int) -> None:
        """按 1 秒窗口统计发布方向线上码率, 超过目标档时计数并限频记日志."""
        if role != "publisher" or self._target_kbps <= 0:
            return
        stats = session.stats
        now = time.monotonic()
        elapsed = now - stats.window_started
        if elapsed >= 1.0:
            measured_kbps = int(stats.window_bytes * 8 / 1000 / elapsed)
            if measured_kbps > self._target_kbps:
                stats.budget_exceeded += 1
                with self._lock:
                    self._totals["budget_exceeded"] += 1
                if now - stats.last_exceed_log >= 10.0:
                    stats.last_exceed_log = now
                    log_event(
                        "relay.dataplane",
                        "budget.exceeded",
                        level="WARNING",
                        session_id=session.session_id,
                        measured_kbps=measured_kbps,
                        target_kbps=self._target_kbps,
                        budget_kbps=session.budget_kbps,
                    )
            stats.window_started = now
            stats.window_bytes = 0
        stats.window_bytes += size

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
    def revoke_token(self, token: str, reason: str = "授权已撤销") -> int:
        """立即断开使用该令牌的数据面连接, 并停止后续转发; 返回断开的端点数.

        会话结束、授权回收、令牌吊销都会走到这里。先摘除端点再关闭套接字,
        保证已经进入转发路径的帧最多只发一帧, 不会在断开后继续双向转发。
        """
        if not token:
            return 0
        dropped: list[tuple[_Session, _Endpoint]] = []
        emptied: list[_Session] = []
        with self._lock:
            for session in list(self._sessions.values()):
                endpoints = (session.publisher, session.subscriber)
                for endpoint in endpoints:
                    if endpoint is None or endpoint.token != token:
                        continue
                    setattr(session, endpoint.role, None)
                    dropped.append((session, endpoint))
                if session.publisher is None and session.subscriber is None and session not in emptied:
                    emptied.append(session)
            for session in emptied:
                self._sessions.pop(session.session_id, None)
            if dropped:
                self.revoked_tokens += 1
                self.revoked_sessions += len(emptied)
        for session, endpoint in dropped:
            with suppress(OSError):
                endpoint.sock.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                endpoint.sock.close()
            session.ready.set()
        if dropped:
            log_event(
                "relay.dataplane",
                "session.revoked",
                level="WARNING",
                reason=reason,
                session_ids=[session.session_id for session, _ in dropped],
                roles=[endpoint.role for _, endpoint in dropped],
                frames_forwarded=sum(session.stats.frames_forwarded for session, _ in dropped),
                bytes_forwarded=sum(session.stats.bytes_forwarded for session, _ in dropped),
                sessions_closed=len(emptied),
            )
        return len(dropped)

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
            "revoked_tokens": self.revoked_tokens,
            "revoked_sessions": self.revoked_sessions,
            "bytes_forwarded": sum(item.stats.bytes_forwarded for item in sessions),
            "frames_forwarded": sum(item.stats.frames_forwarded for item in sessions),
            "frames_dropped": sum(item.stats.frames_dropped for item in sessions),
            "budget_exceeded": sum(item.stats.budget_exceeded for item in sessions),
            "total_bytes_forwarded": totals["bytes_forwarded"],
            "total_frames_forwarded": totals["frames_forwarded"],
            "total_frames_dropped": totals["frames_dropped"],
            "total_budget_exceeded": totals["budget_exceeded"],
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
