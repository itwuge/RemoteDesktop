# ==========================================================================================
# 文件        : aead.py
# 所属目录    : src/rdc/session/
# 文件用途    : 把已建立的 AEAD 数据面通道适配成 ``SessionChannel`` 协议
# 架构定位    : 协议转接层; 不承载分块、校验、重传等业务逻辑, 通道仍可替换
# 核心功能    : 业务消息包成 ``{type, session_id, payload}``; 读线程把会话面消息入有界队列,
#               视频与输入等控制帧原样交给上层回调
# 职责边界    : 不加密(加密在底层通道); 不解析文件/剪贴板语义; 不落盘; 不重试
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.session.channel 的协议与错误码
# ==========================================================================================

"""AEAD 通道到会话通道的适配.

上层(文件、剪贴板)只认 ``SessionChannel``; 底层可以是脚本里的 ``AeadChannel``,
也可以是后续正式数据面实现, 只要提供 ``send(dict)`` 与 ``recv_raw()``.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
from collections.abc import Callable
from typing import Any, Protocol

from rdc.core.logging import log_event
from rdc.session.channel import (
    CHANNEL_CLOSED,
    SESSION_PLANE_TYPES,
    ChannelInfo,
    SessionChannelError,
)

# 会话面消息之外, ``error`` 也由通道回给业务层处理(剪贴板读空、offer 被拒等)
ROUTED_TYPES: frozenset[str] = SESSION_PLANE_TYPES | {"error"}
ControlItem = bytes | dict[str, Any]


class AeadLink(Protocol):
    """适配层需要的最小底层能力, 与具体加密实现解耦."""

    def send(self, payload: dict[str, Any]) -> object: ...

    def recv_raw(self) -> bytes: ...


class AeadSessionChannel:
    """在 AEAD 数据面上实现 ``SessionChannel``.

    读线程是唯一的读取方: 会话面消息进入 ``_queue``, 视频/输入/控制帧通过
    ``on_control`` 回调交给界面侧, 避免两个线程抢同一个 socket.
    """

    def __init__(
        self,
        link: AeadLink,
        *,
        session_id: str,
        role: str,
        budget_kbps: int = 0,
        on_control: Callable[[ControlItem], None] | None = None,
        queue_size: int = 512,
    ) -> None:
        self._link = link
        self._session_id = session_id
        self._role = role
        self._budget_kbps = budget_kbps
        self._on_control = on_control
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._closed = threading.Event()
        self._opened = False
        self._reader: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def open(self) -> None:
        if self._opened:
            return
        self._opened = True
        self._reader = threading.Thread(target=self._read_loop, name="rdc-aead-read", daemon=True)
        self._reader.start()
        log_event(
            "rdc.session.aead",
            "channel.opened",
            session_id=self._session_id,
            role=self._role,
            encrypted=True,
            visible_to_relay=False,
        )

    def close(self) -> None:
        if self._closed.is_set() and self._reader is None:
            return
        self._closed.set()
        closer = getattr(self._link, "close", None)
        if callable(closer):
            with contextlib.suppress(OSError, RuntimeError):
                closer()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        log_event("rdc.session.aead", "channel.closed", session_id=self._session_id)

    # ------------------------------------------------------------------ 收发
    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        if self._closed.is_set():
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        message = {
            "type": msg_type,
            "session_id": self._session_id,
            "payload": dict(payload),
        }
        try:
            self._link.send(message)
        except (OSError, RuntimeError, ValueError, EOFError) as exc:
            self._closed.set()
            raise SessionChannelError(CHANNEL_CLOSED, f"会话通道写入失败: {exc}") from exc

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

    def info(self) -> ChannelInfo:
        return ChannelInfo(
            kind="aead",
            session_id=self._session_id,
            role=self._role,
            encrypted=True,
            visible_to_relay=False,
            budget_kbps=self._budget_kbps,
        )

    # ------------------------------------------------------------------ 读线程
    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                raw = self._link.recv_raw()
                routed = self._route(raw)
                if routed is None:
                    continue
                try:
                    self._queue.put(routed, timeout=1.0)
                except queue.Full:
                    # 业务层长期不取: 关闭通道让调用方看到明确失败, 不无界堆积
                    log_event("rdc.session.aead", "channel.queue_full", session_id=self._session_id)
                    break
        except (OSError, RuntimeError, ValueError, EOFError):
            pass
        finally:
            self._closed.set()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)

    def _route(self, raw: bytes) -> tuple[str, dict[str, Any]] | None:
        """会话面消息返回结构化结果; 其余帧交给控制回调并返回 None."""
        if raw[:1] == b"{":
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(message, dict):
                return None
            msg_type = str(message.get("type") or "")
            payload = message.get("payload")
            if msg_type in ROUTED_TYPES and isinstance(payload, dict):
                merged = dict(payload)
                merged.setdefault("session_id", str(message.get("session_id") or self._session_id))
                return msg_type, merged
            self._deliver(message)
            return None
        self._deliver(raw)
        return None

    def _deliver(self, item: ControlItem) -> None:
        handler = self._on_control
        if handler is None:
            return
        try:
            handler(item)
        except (OSError, RuntimeError, ValueError):
            self._closed.set()
