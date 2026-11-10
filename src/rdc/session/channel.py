# ==========================================================================================
# 文件        : channel.py
# 所属目录    : src/rdc/session/
# 文件用途    : 会话通道抽象: 环路实现与真实 TCP 中继数据面实现, 供文件与剪贴板共用
# 架构定位    : 业务引擎与传输层之间的可替换接口; 不绑定中继分支
# 核心功能    : 有界收发、背压错误、可见性声明、relay_hello/relay_ready 握手
# 职责边界    : 不解析文件与剪贴板语义; 不加密; 不落盘; 不重试业务消息
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.relay.server 的 send_frame; rdc.core.logging
# ==========================================================================================

"""会话通道.

上层只依赖 ``SessionChannel`` 协议, 因此未来把信令降级通道换成正式数据面 AEAD 通道时,
文件与剪贴板引擎不需要改动. 当前 ``RelayFrameChannel`` 明确声明
``encrypted=False, visible_to_relay=True``, 由界面与日志提示"未达端到端加密".
"""

from __future__ import annotations

import contextlib
import json
import queue
import socket
import ssl
import struct
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from rdc.core.logging import log_event
from rdc.relay.dataplane import DATA_MAX_FRAME_BYTES
from rdc.relay.server import send_frame

CHANNEL_BACKPRESSURE = "CHANNEL_BACKPRESSURE"
CHANNEL_TIMEOUT = "CHANNEL_TIMEOUT"
CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
CHANNEL_AUTH_FAILED = "CHANNEL_AUTH_FAILED"
CHANNEL_CLOSED = "CHANNEL_CLOSED"


class SessionChannelError(RuntimeError):
    """通道错误: 带契约错误码与中文原因, 上层直接映射为任务失败原因."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# 文件与剪贴板共用的数据面消息类型; 会话循环只分流这些, 媒体与输入仍走各自分支
SESSION_PLANE_TYPES: frozenset[str] = frozenset(
    {
        "file_offer",
        "file_accept",
        "file_chunk",
        "file_ack",
        "file_complete",
        "file_abort",
        "file_result",
        "clipboard_update",
        "clipboard_request",
        "clipboard_ack",
    }
)


@dataclass(frozen=True)
class ChannelInfo:
    """通道能力声明, 供界面提示与发送端预算收敛."""

    kind: str
    session_id: str
    role: str
    encrypted: bool
    visible_to_relay: bool
    budget_kbps: int = 0


class SessionChannel(Protocol):
    """会话通道协议: 一帧一条会话消息, 不阻塞界面线程."""

    def open(self) -> None: ...

    def send(self, msg_type: str, payload: dict[str, Any]) -> None: ...

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None: ...

    def close(self) -> None: ...

    def info(self) -> ChannelInfo: ...


def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            return None if not chunks else bytes(chunks)
        chunks.extend(chunk)
    return bytes(chunks)


def recv_frame(sock: socket.socket, max_bytes: int = DATA_MAX_FRAME_BYTES) -> bytes | None:
    """读取一个 4 字节长度前缀帧; 超长直接拒绝, 避免内存被撑爆."""
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    if len(header) != 4:
        raise OSError("帧头不完整")
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > max_bytes:
        raise ValueError("帧长度非法")
    body = _recv_exact(sock, length)
    if body is None:
        raise OSError("帧体不完整")
    return body


class LoopbackChannel:
    """同进程配对通道, 用于单测与不依赖网络的联调; 容量满时返回背压错误."""

    def __init__(self, session_id: str, role: str, inbox: queue.Queue[Any], capacity: int) -> None:
        self._session_id = session_id
        self._role = role
        self._inbox = inbox
        self._capacity = capacity
        self._peer: LoopbackChannel | None = None
        self._closed = False
        self._peer_closed = False
        self._opened = False

    @classmethod
    def pair(cls, session_id: str, capacity: int = 64) -> tuple[LoopbackChannel, LoopbackChannel]:
        """构造一对互通的通道; capacity 为单方向可积压帧数."""
        left_inbox: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        right_inbox: queue.Queue[Any] = queue.Queue(maxsize=capacity)
        left = cls(session_id, "left", left_inbox, capacity)
        right = cls(session_id, "right", right_inbox, capacity)
        left._peer = right
        right._peer = left
        return left, right

    def open(self) -> None:
        self._opened = True

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if self._closed or self._peer_closed or self._peer is None:
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        try:
            self._peer._inbox.put_nowait((msg_type, dict(payload)))
        except queue.Full as exc:
            raise SessionChannelError(CHANNEL_BACKPRESSURE, "对端接收窗口已满") from exc

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        try:
            item = self._inbox.get(timeout=timeout) if timeout > 0 else self._inbox.get_nowait()
        except queue.Empty as exc:
            if self._closed or self._peer_closed:
                raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭") from exc
            return None
        msg_type, payload = item
        return str(msg_type), dict(payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._peer is not None:
            self._peer._peer_closed = True

    def info(self) -> ChannelInfo:
        return ChannelInfo(
            kind="loopback",
            session_id=self._session_id,
            role=self._role,
            encrypted=False,
            visible_to_relay=False,
        )


class RelayFrameChannel:
    """真实 TCP 中继数据面通道: 明文握手 + 不透明帧转发, 不提供端到端加密."""

    def __init__(
        self,
        host: str,
        port: int,
        relay_token: str,
        session_id: str,
        role: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        read_queue: int = 512,
        connect_timeout_s: float = 5.0,
        write_timeout_s: float = 0.5,
    ) -> None:
        self._host = host
        self._port = port
        self._relay_token = relay_token
        self._session_id = session_id
        self._role = role
        self._ssl_context = ssl_context
        self._connect_timeout_s = connect_timeout_s
        self._write_timeout_s = write_timeout_s
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=read_queue)
        self._sock: socket.socket | None = None
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._opened = False
        self._reader: threading.Thread | None = None
        self._budget_kbps = 0

    # ------------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self._opened:
            return
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout_s)
        except OSError as exc:
            raise SessionChannelError(CHANNEL_UNAVAILABLE, f"中继数据面不可达: {exc}") from exc
        if self._ssl_context is not None:
            try:
                sock = self._ssl_context.wrap_socket(sock, server_hostname=self._host)
            except ssl.SSLError as exc:
                sock.close()
                raise SessionChannelError(CHANNEL_AUTH_FAILED, f"数据面 TLS 握手失败: {exc}") from exc
        self._sock = sock
        sock.settimeout(self._connect_timeout_s)
        try:
            send_frame(
                sock,
                {
                    "plane": "data",
                    "v": 1,
                    "type": "relay_hello",
                    "relay_token": self._relay_token,
                    "session_id": self._session_id,
                    "role": self._role,
                },
            )
            raw = recv_frame(sock)
            if raw is None:
                raise SessionChannelError(CHANNEL_AUTH_FAILED, "中继在握手阶段断开")
            ready = json.loads(raw.decode("utf-8"))
            if not isinstance(ready, dict):
                raise SessionChannelError(CHANNEL_AUTH_FAILED, "数据面握手响应格式非法")
            if ready.get("type") != "relay_ready":
                code = str(ready.get("code") or CHANNEL_AUTH_FAILED)
                raise SessionChannelError(code, str(ready.get("message") or "数据面握手被拒绝"))
            self._budget_kbps = int(ready.get("budget_kbps", 0) or 0)
        except (OSError, ValueError) as exc:
            sock.close()
            raise SessionChannelError(CHANNEL_UNAVAILABLE, f"数据面握手失败: {exc}") from exc
        sock.settimeout(None)
        self._opened = True
        self._reader = threading.Thread(target=self._read_loop, name="rdc-channel-read", daemon=True)
        self._reader.start()
        log_event(
            "rdc.session.channel",
            "channel.opened",
            kind="relay",
            session_id=self._session_id,
            role=self._role,
            encrypted=False,
            visible_to_relay=True,
            budget_kbps=self._budget_kbps,
        )

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if self._sock is None or self._closed.is_set():
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        body = json.dumps(
            {"type": msg_type, "session_id": self._session_id, "payload": payload},
            ensure_ascii=False,
        ).encode("utf-8")
        if len(body) > DATA_MAX_FRAME_BYTES:
            raise SessionChannelError("CHANNEL_FRAME_TOO_LARGE", "单帧超过数据面上限")
        with self._write_lock:
            self._sock.settimeout(self._write_timeout_s)
            try:
                self._sock.sendall(_frame(body))
            except TimeoutError as exc:
                raise SessionChannelError(CHANNEL_BACKPRESSURE, "数据面写入超时, 对端窗口已满") from exc
            except OSError as exc:
                self._closed.set()
                raise SessionChannelError(CHANNEL_CLOSED, f"会话通道写入失败: {exc}") from exc
            finally:
                if self._sock is not None:
                    self._sock.settimeout(None)

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        try:
            item = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty as exc:
            if self._closed.is_set():
                raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭") from exc
            return None
        if item is None:
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        msg_type, payload = item
        return str(msg_type), dict(payload)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._write_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        log_event("rdc.session.channel", "channel.closed", session_id=self._session_id)

    def info(self) -> ChannelInfo:
        return ChannelInfo(
            kind="relay",
            session_id=self._session_id,
            role=self._role,
            encrypted=False,
            visible_to_relay=True,
            budget_kbps=self._budget_kbps,
        )

    # ------------------------------------------------------------------ 读线程
    def _read_loop(self) -> None:
        sock = self._sock
        try:
            while not self._closed.is_set() and sock is not None:
                raw = recv_frame(sock)
                if raw is None:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                msg_type = str(message.get("type") or "")
                payload = message.get("payload")
                if not msg_type or not isinstance(payload, dict):
                    continue
                try:
                    self._queue.put((msg_type, payload), timeout=1.0)
                except queue.Full:
                    # 上层长期不取: 关闭通道让调用方看到明确失败, 不无界堆积
                    self._closed.set()
                    break
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)


def connect_relay_channel(
    host: str,
    port: int,
    relay_token: str,
    session_id: str,
    role: str,
    **options: Any,
) -> RelayFrameChannel:
    """构造并完成握手的真实 TCP 通道, 供装配处一行接线."""
    channel = RelayFrameChannel(host, port, relay_token, session_id, role, **options)
    channel.open()
    return channel
