# ==========================================================================================
# 文件        : desktop.py
# 所属目录    : src/rdc/session/
# 文件用途    : 桌面端会话装配: 一条会话通道按类型分发给文件/剪贴板服务与文件收端
# 架构定位    : 装配层; 只负责接线, 不复制传输与校验逻辑
# 核心功能    : 帧复制订阅(避免多个服务互相抢帧)、服务绑定、收端泵、界面泵入口
# 职责边界    : 不建连; 不加密; 不写业务状态; 无会话时不建目录、不起线程、不写盘
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.session.channel; rdc.files; rdc.clipboard
# ==========================================================================================

"""桌面会话装配.

一条数据面上同时跑文件与剪贴板: 每个消费者拿一条只含自己消息类型的订阅队列,
因此界面刷新剪贴板状态不会顺带吞掉文件的 ack, 两个服务也不会互相偷帧.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rdc.clipboard.sync import ClipboardSyncService
from rdc.core.logging import log_event
from rdc.files.receiver import FileTransferReceiver
from rdc.files.service import FileTransferService
from rdc.session.channel import (
    CHANNEL_CLOSED,
    ChannelInfo,
    SessionChannel,
    SessionChannelError,
)

FILE_MESSAGE_TYPES: frozenset[str] = frozenset(
    {"file_offer", "file_accept", "file_chunk", "file_ack", "file_complete", "file_abort", "file_result"}
)
CLIPBOARD_MESSAGE_TYPES: frozenset[str] = frozenset({"clipboard_update", "clipboard_request", "clipboard_ack"})


class _FanoutView:
    """单个消费者的通道视图; ``close`` 只退订, 不关闭整条会话通道."""

    def __init__(self, owner: ChannelFanout, name: str, queue_size: int) -> None:
        self._owner = owner
        self._name = name
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._closed = False

    def open(self) -> None:
        self._owner.open()

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        self._owner.send(msg_type, payload)

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        if self._closed:
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        try:
            item = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty as exc:
            if self._closed or self._owner.closed:
                raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭") from exc
            return None
        if item is None:
            raise SessionChannelError(CHANNEL_CLOSED, "会话通道已关闭")
        msg_type, payload = item
        return str(msg_type), dict(payload)

    def offer(self, item: Any) -> None:
        """投递一帧或结束标记; 队列满时由调用方判定失败."""
        self._queue.put(item, timeout=1.0)

    def shutdown(self) -> None:
        """停止视图(不关闭底层通道), 让阻塞中的读取方立即返回."""
        if self._closed:
            return
        self._closed = True
        self._owner.unsubscribe(self)
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)

    def close(self) -> None:
        self.shutdown()

    def info(self) -> ChannelInfo:
        return self._owner.info()


class ChannelFanout:
    """一条会话通道 → 多条按类型过滤的订阅; 发送方向保持单条通道."""

    def __init__(self, channel: SessionChannel, *, queue_size: int = 1024) -> None:
        self._channel = channel
        self._queue_size = queue_size
        self._views: list[tuple[_FanoutView, frozenset[str]]] = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def subscribe(self, name: str, types: frozenset[str]) -> SessionChannel:
        view = _FanoutView(self, name, self._queue_size)
        with self._lock:
            self._views.append((view, types))
        return view

    def unsubscribe(self, view: _FanoutView) -> None:
        with self._lock:
            self._views = [(item, types) for item, types in self._views if item is not view]

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._read_loop, name="rdc-session-fanout", daemon=True)
        self._reader.start()

    def open(self) -> None:
        """通道在装配前已建立, 这里只保证读线程在跑."""
        self.start()

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        self._channel.send(msg_type, payload)

    def info(self) -> ChannelInfo:
        return self._channel.info()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            views = [view for view, _types in self._views]
            self._views = []
        for view in views:
            view.shutdown()
        self._channel.close()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                message = self._channel.receive(0.2)
                if message is None:
                    continue
                msg_type, payload = message
                with self._lock:
                    targets = [(view, types) for view, types in self._views if msg_type in types]
                for view, _types in targets:
                    try:
                        view.offer((msg_type, dict(payload)))
                    except queue.Full:
                        log_event("rdc.session.desktop", "fanout.queue_full", msg_type=msg_type)
                        self._closed.set()
                        return
        except SessionChannelError:
            pass
        finally:
            self._closed.set()
            with self._lock:
                views = [view for view, _types in self._views]
            for view in views:
                view.shutdown()


class DesktopSessionBinding:
    """桌面端会话装配.

    只在会话建立后构造: 绑定与界面共用的同一份服务实例, 并起一个收端线程处理
    对端发来的文件; 剪贴板由 ``pump`` 在界面线程推进, 与页面读取的是同一个服务.
    """

    def __init__(
        self,
        channel: SessionChannel,
        *,
        transfer_service: FileTransferService,
        clipboard_service: ClipboardSyncService,
        destination_root: Path | str,
        on_files_received: Callable[[dict[str, Any]], None] | None = None,
        on_clipboard_apply: Callable[[str, str], None] | None = None,
    ) -> None:
        self.transfer = transfer_service
        self.clipboard = clipboard_service
        self.fanout = ChannelFanout(channel)
        self._transfer_view = self.fanout.subscribe("transfer", FILE_MESSAGE_TYPES | {"error"})
        self._clipboard_view = self.fanout.subscribe("clipboard", CLIPBOARD_MESSAGE_TYPES | {"error"})
        self._receiver_view = self.fanout.subscribe("receiver", FILE_MESSAGE_TYPES)
        self.receiver = FileTransferReceiver(Path(destination_root), on_complete=on_files_received)
        self._stop = threading.Event()
        self._receiver_thread: threading.Thread | None = None
        self.transfer.bind_channel(self._transfer_view)
        self.clipboard.bind_channel(self._clipboard_view, on_apply=on_clipboard_apply)
        self.fanout.start()
        self._receiver_thread = threading.Thread(target=self._receive_loop, name="rdc-desktop-receiver", daemon=True)
        self._receiver_thread.start()
        info = self.fanout.info()
        log_event(
            "rdc.session.desktop",
            "desktop.bound",
            session_id=info.session_id,
            kind=info.kind,
            encrypted=info.encrypted,
            visible_to_relay=info.visible_to_relay,
        )

    # ------------------------------------------------------------------ 运行
    def _receive_loop(self) -> None:
        """收端线程: 只把对端发来的文件落盘并回执, 不碰发送任务状态."""
        while not self._stop.is_set():
            try:
                message = self._receiver_view.receive(0.2)
            except SessionChannelError:
                return
            if message is None:
                continue
            reply = self.receiver.handle(message[0], message[1])
            if reply is None:
                continue
            try:
                self._receiver_view.send(str(reply["type"]), dict(reply["payload"]))
            except SessionChannelError:
                return

    def pump(self) -> int:
        """界面线程周期调用; 与页面共用同一个剪贴板服务实例, 消费即处理不丢帧."""
        if self._stop.is_set():
            return 0
        return self.clipboard.poll_channel()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._receiver_thread is not None and self._receiver_thread.is_alive():
            self._receiver_thread.join(timeout=1.0)
        self._receiver_view.close()
        self.transfer.bind_channel(None)
        self.clipboard.bind_channel(None)
        self.fanout.close()
        log_event("rdc.session.desktop", "desktop.unbound")
