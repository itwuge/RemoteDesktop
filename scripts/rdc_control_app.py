# ==========================================================================================
# 文件        : rdc_control_app.py
# 所属目录    : scripts/
# 文件用途    : Windows 控制端图形界面: 填地址/设备 ID/连接码 → 连接 Ubuntu 被控端 →
#               显示远端桌面画面, 并把本机键盘鼠标事件转发过去
# 架构定位    : 反向链路 (Windows 控制 Ubuntu) 的控制端入口; 复用产品信令/中继/加密实现
# 核心功能    : 连接/断开按钮; 连接中/已连接/断开/失败原因四态提示; H.264 解码渲染;
#               键盘(字符/功能键/组合键)与鼠标(移动/点击/滚轮)转发
# 职责边界    : 连接码只保存在输入框内存中, 不写文件不写日志; 不伪造连接状态
# ==========================================================================================

"""Windows 控制端.

用法:
    python scripts/rdc_control_app.py
    python scripts/rdc_control_app.py --selftest RDC-TYPING-TEST --evidence <目录>

自测模式: 自动连接, 收到首帧后保存截图, 逐字符发送标记文本, 再保存截图后退出.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import logging
import os
import queue
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import av
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    controller_handshake,
    relay_connect,
)
from rdc.crypto.identity import DeviceIdentity, b64encode

DEFAULT_ADDRESS = "192.168.37.132"
DEFAULT_PORT = "27888"
APP_ID = "rdc-control"
LOGGER = logging.getLogger(APP_ID)
REQUEST_KBPS = 8000
STATE_COLORS = {
    "idle": "background:#eef1f4;color:#3b4652;",
    "connecting": "background:#fff4d6;color:#7a5200;",
    "connected": "background:#dff5e6;color:#0b6b34;",
    "closed": "background:#eef1f4;color:#3b4652;",
    "failed": "background:#fde4e1;color:#a01b12;",
}
KEY_NAMES = {
    Qt.Key_Return: "Return",
    Qt.Key_Enter: "Return",
    Qt.Key_Backspace: "BackSpace",
    Qt.Key_Tab: "Tab",
    Qt.Key_Escape: "Escape",
    Qt.Key_Delete: "Delete",
    Qt.Key_Left: "Left",
    Qt.Key_Right: "Right",
    Qt.Key_Up: "Up",
    Qt.Key_Down: "Down",
    Qt.Key_Home: "Home",
    Qt.Key_End: "End",
    Qt.Key_PageUp: "Page_Up",
    Qt.Key_PageDown: "Page_Down",
    Qt.Key_Space: "space",
}
MODIFIERS = (
    (Qt.ControlModifier, "ctrl"),
    (Qt.ShiftModifier, "shift"),
    (Qt.AltModifier, "alt"),
    (Qt.MetaModifier, "super"),
)


def _portable_root() -> Path | None:
    """便携包根目录: 环境变量优先, 其次可执行文件旁的 portable.marker."""
    override = os.environ.get("RDC_PORTABLE_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "portable.marker").is_file():
            return exe_dir
    return None


def _app_paths() -> tuple[Path, Path]:
    """返回 (配置目录, 日志目录).

    便携包: 跟随包目录, 整个目录拷走即带走配置与日志;
    系统安装: 配置 ~/.config/rdc-control, 日志 ~/.local/state/rdc-control, 卸载即清空.
    """
    root = _portable_root()
    if root is not None:
        return root / "config", root / "logs"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return config_home / APP_ID, state_home / APP_ID


def _setup_logging(log_dir: Path) -> None:
    """初始化文件日志; 只记连接事件, 不记录连接码与设备 ID."""
    LOGGER.setLevel(logging.INFO)
    if LOGGER.handlers:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / f"{APP_ID}.log", encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def _frame_to_image(frame: Any, expected: tuple[int, int]) -> QImage | None:
    """把解码帧转成 QImage; 数据损坏或尺寸不符时返回 None, 由调用方丢弃该帧.

    解码后的帧缓冲按 32 字节对齐, 行尾带填充, numpy 视图因此不是 C 连续,
    直接交给 QImage 会抛 BufferError. 这里先转成连续的 BGRA 字节, 行宽取
    宽度*4, 任意分辨率(包括宽度不是 32 字节对齐)都能正常渲染.
    """
    try:
        bitmap = frame.to_ndarray(format="bgra")
    except (av.error.FFmpegError, ValueError):
        return None
    if bitmap.ndim != 3 or bitmap.shape[2] != 4:
        return None
    height, width = int(bitmap.shape[0]), int(bitmap.shape[1])
    if width <= 0 or height <= 0:
        return None
    expected_width, expected_height = expected
    if expected_width > 0 and expected_height > 0 and (width, height) != (expected_width, expected_height):
        return None
    contiguous = np.ascontiguousarray(bitmap)
    image = QImage(contiguous.tobytes(), width, height, width * 4, QImage.Format_ARGB32)
    if image.isNull():
        return None
    return image.copy()


def _explain(error: BaseException) -> str:
    """把网络异常转成用户看得懂的原因."""
    if isinstance(error, ConnectionRefusedError):
        return "被控端拒绝了连接: 请确认 Ubuntu 侧已启动被控端, 地址与端口填写正确"
    if isinstance(error, TimeoutError):
        return "连接超时: 请检查地址是否可达, 以及 Ubuntu 侧是否仍在运行"
    if isinstance(error, socket.gaierror):
        return "地址无法解析: 请检查 IP 或主机名"
    if isinstance(error, E2EFailure):
        text = str(error)
        if "PEER_OFFLINE" in text:
            return "设备 ID 不正确, 或该被控端不在线: 请核对终端里的设备 ID"
        if "等待 answer 超时" in text:
            return "被控端没有应答: 请确认被控端仍在运行, 且连接码对应的会话未被占用"
        if "AEAD 通道被对端关闭" in text or "连接已关闭" in text:
            return "与 Ubuntu 的连接已断开: 被控端可能已退出或网络中断"
        return text
    return f"{type(error).__name__}: {error}"


class ConnectWorker(QThread):
    """连接、解码与输入上行的承载线程."""

    status_changed = Signal(str, str)
    frame_ready = Signal(QImage)
    remote_ready = Signal(int, int)

    def __init__(self, address: str, port: int, device_id: str, access_code: str) -> None:
        super().__init__()
        self.address = address
        self.port = port
        self.device_id = device_id
        self.access_code = access_code
        self.session_id = f"rdc-{int(time.time())}-{secrets.token_hex(3)}"
        self.stop_event = threading.Event()
        self.outbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.write_lock = threading.Lock()
        self.channel: AeadChannel | None = None
        self.sock: socket.socket | None = None
        self.client: SignalingClient | None = None
        self.frames = 0
        self.failed_reason = ""
        self.established = False

    # ------------------------------------------------------------------ 线程主体
    def run(self) -> None:
        try:
            self._connect()
        except (OSError, ValueError, RuntimeError, E2EFailure) as error:
            self.failed_reason = _explain(error)
            if not self.stop_event.is_set():
                if self.established:
                    self.status_changed.emit("closed", f"已断开: {self.failed_reason}")
                else:
                    self.status_changed.emit("failed", f"连接失败: {self.failed_reason}")
        finally:
            self._cleanup()
            if self.stop_event.is_set() and not self.failed_reason:
                self.status_changed.emit("closed", "已断开")

    def _connect(self) -> None:
        self.status_changed.emit("connecting", f"正在连接 {self.address}:{self.port} …")
        identity = DeviceIdentity.generate()
        self.client = SignalingClient(self.address, self.port, timeout_s=20.0)
        self.client.connect()
        self.client.register_and_auth(identity, "windows-controller")
        threading.Thread(target=self._keepalive_loop, daemon=True, name="control-keepalive").start()
        self.status_changed.emit("connecting", "已连上被控端, 正在校验连接码 …")
        self.client.send_envelope(
            "offer",
            {
                "session_id": self.session_id,
                "from_device_id": identity.device_id,
                "to_device_id": self.device_id,
                "candidates": [{"kind": "relay", "address": self.address, "port": self.port, "priority": 1}],
                "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
                "access_proof": b64encode(self.access_code.encode("utf-8")),
            },
            session_id=self.session_id,
        )
        answer = self.client.recv_until("answer")
        payload = answer.get("payload", {})
        if not payload.get("accepted"):
            reason = str(payload.get("reason") or "被控端拒绝了本次连接")
            raise E2EFailure(reason)
        peer = str(payload.get("from_device_id") or self.device_id)
        self.status_changed.emit("connecting", "连接码正确, 正在建立画面通道 …")
        self.client.send_envelope(
            "session_start",
            {
                "session_id": self.session_id,
                "from_device_id": identity.device_id,
                "to_device_id": peer,
                "transport_kind": "relay",
                "quality_profile": "L1",
            },
            session_id=self.session_id,
        )
        self.client.send_envelope(
            "relay_allocate",
            {"session_id": self.session_id, "requested_kbps": REQUEST_KBPS, "est_duration_s": 7200},
            session_id=self.session_id,
        )
        allocated = self.client.recv_until("relay_allocated")
        token = str(allocated.get("payload", {}).get("relay_token", ""))
        self.sock = relay_connect(self.address, self.port, token, self.session_id, "subscriber")
        self.sock.settimeout(300.0)
        session = controller_handshake(self.sock, identity)
        channel = AeadChannel(self.sock, session)
        self.channel = channel
        sender = threading.Thread(target=self._send_loop, daemon=True, name="control-sender")
        sender.start()
        self.established = True
        self.status_changed.emit("connected", "已连接, 正在等待 Ubuntu 桌面画面 …")
        self._receive_loop(channel)

    def _keepalive_loop(self) -> None:
        """每 10 秒发一次心跳, 避免信令连接空闲超时导致会话被回收."""
        seq = 0
        while not self.stop_event.wait(10.0):
            seq += 1
            if self.client is None:
                return
            with contextlib.suppress(OSError, E2EFailure):
                self.client.send_envelope("heartbeat", {"session_id": "", "seq": seq})

    def _receive_loop(self, channel: AeadChannel) -> None:
        decoder = av.CodecContext.create("h264", "r")
        decoder.open()
        while not self.stop_event.is_set():
            message = channel.recv()
            kind = str(message.get("kind", ""))
            if kind == "video":
                self._on_video(decoder, message)
            elif kind == "ready":
                self.remote_ready.emit(int(message.get("w", 0)), int(message.get("h", 0)))
            elif kind == "error":
                raise E2EFailure(str(message.get("reason") or "被控端返回错误"))

    def _on_video(self, decoder: av.CodecContext, message: dict[str, Any]) -> None:
        try:
            raw = base64.b64decode(str(message.get("data", "")).encode("ascii"), validate=True)
            frames = decoder.decode(av.Packet(raw))
        except (av.error.FFmpegError, ValueError):
            return
        expected = (int(message.get("w") or 0), int(message.get("h") or 0))
        for frame in frames:
            image = _frame_to_image(frame, expected)
            if image is None:
                continue
            self.frames += 1
            self.frame_ready.emit(image)

    # ------------------------------------------------------------------ 发送
    def send(self, message: dict[str, Any]) -> None:
        """排队一条输入消息, 由发送线程写入通道."""
        self.outbound.put(message)

    def _send_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            with self.write_lock:
                if self.channel is None:
                    continue
                with contextlib.suppress(OSError, E2EFailure):
                    self.channel.send(message)

    def request_stop(self) -> None:
        """主动断开: 先通知被控端, 再关掉数据面让接收线程退出."""
        self.stop_event.set()
        with self.write_lock:
            if self.channel is not None:
                with contextlib.suppress(OSError, E2EFailure):
                    self.channel.send({"kind": "bye"})
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)

    def _cleanup(self) -> None:
        self.stop_event.set()
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
        if self.client is not None:
            self.client.close()


class ControllerWindow(QMainWindow):
    """主窗口: 顶部连接表单, 中部远端画面, 底部状态条."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("RDC 控制端 · 远程操作 Ubuntu")
        self.resize(1280, 820)
        self.worker: ConnectWorker | None = None
        self.remote_size = (0, 0)
        self.last_move_sent = 0.0
        self.selftest_text = ""
        self.evidence_dir: Path | None = None
        self.selftest_done: Callable[[bool, str], None] | None = None

        central = QWidget(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)
        root.addLayout(self._build_form())

        self.video = QLabel("填写地址、设备 ID 与连接码后, 点击「连接」", central)
        self.video.setAlignment(Qt.AlignCenter)
        self.video.setStyleSheet("background:#0f1318;color:#94a3b0;font-size:16px;")
        self.video.setMinimumSize(720, 450)
        self.video.setFocusPolicy(Qt.StrongFocus)
        self.video.setMouseTracking(True)
        self.video.installEventFilter(self)
        root.addWidget(self.video, 1)

        self.status = QLabel("未连接", central)
        self.status.setStyleSheet(f"padding:7px;font-size:13px;{STATE_COLORS['idle']}")
        root.addWidget(self.status)
        self.setCentralWidget(central)

    def _build_form(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("服务器地址"))
        self.address = QLineEdit(DEFAULT_ADDRESS)
        self.address.setMaximumWidth(160)
        row.addWidget(self.address)
        row.addWidget(QLabel("端口"))
        self.port = QLineEdit(DEFAULT_PORT)
        self.port.setMaximumWidth(70)
        row.addWidget(self.port)
        row.addSpacing(10)
        row.addWidget(QLabel("设备 ID"))
        self.device = QLineEdit()
        self.device.setPlaceholderText("被控端启动时打印的设备 ID")
        self.device.setMinimumWidth(200)
        row.addWidget(self.device)
        row.addWidget(QLabel("连接码"))
        self.code = QLineEdit()
        self.code.setPlaceholderText("6 位数字")
        self.code.setMaximumWidth(100)
        self.code.returnPressed.connect(self._on_connect)
        row.addWidget(self.code)
        row.addSpacing(10)
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._on_connect)
        row.addWidget(self.connect_button)
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._on_disconnect)
        row.addWidget(self.disconnect_button)
        row.addStretch(1)
        return row

    # ------------------------------------------------------------------ 连接控制
    def _set_status(self, kind: str, text: str) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"padding:7px;font-size:13px;{STATE_COLORS.get(kind, STATE_COLORS['idle'])}")

    def set_status_text(self, kind: str, text: str) -> None:
        """供一键入口在连接尚未建立时显示启动阶段状态."""
        self._set_status(kind, text)

    def start_session(self, address: str, port: int, device_id: str, access_code: str) -> None:
        """一键入口: 预填参数并立即连接; 连接码以掩码显示, 不裸露在界面上."""
        self.address.setText(address)
        self.port.setText(str(port))
        self.device.setText(device_id)
        self.code.setText(access_code)
        self.code.setEchoMode(QLineEdit.Password)
        self._on_connect()

    def _on_connect(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        address = self.address.text().strip()
        try:
            port = int(self.port.text().strip())
        except ValueError:
            self._set_status("failed", "端口必须是数字, 例如 27888")
            return
        device_id = self.device.text().strip()
        code = self.code.text().strip()
        if not address:
            self._set_status("failed", "请填写 Ubuntu 被控端的地址")
            return
        if not device_id or not code:
            self._set_status("failed", "请填写被控端打印的设备 ID 与连接码")
            return
        self.worker = ConnectWorker(address, port, device_id, code)
        LOGGER.info("发起连接 %s:%s", address, port)
        self.worker.status_changed.connect(self._on_status)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.remote_ready.connect(self._on_remote_ready)
        self.worker.finished.connect(self._on_worker_finished)
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.video.setText("正在连接 …")
        self.worker.start()
        self.video.setFocus()

    def _on_disconnect(self) -> None:
        if self.worker is None:
            return
        self._set_status("closed", "正在断开 …")
        self.worker.request_stop()

    def _on_status(self, kind: str, text: str) -> None:
        self._set_status(kind, text)
        LOGGER.info("状态 %s: %s", kind, text)
        if kind in {"closed", "failed"}:
            self.connect_button.setEnabled(True)
            self.disconnect_button.setEnabled(False)
            if self.selftest_text and self.worker is not None and self.worker.frames == 0:
                if self.selftest_done is not None:
                    self.selftest_done(False, text)
                QTimer.singleShot(200, QApplication.instance().quit)

    def _on_remote_ready(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self.remote_size = (width, height)

    def _on_worker_finished(self) -> None:
        """线程真正收尾后释放引用, 保证下次点「连接」不会被旧线程挡住."""
        if self.sender() is self.worker:
            self.worker = None

    def _on_frame(self, image: QImage) -> None:
        self.remote_size = (image.width(), image.height())
        pixmap = QPixmap.fromImage(image)
        self.video.setPixmap(
            pixmap.scaled(
                self.video.size() * (self.devicePixelRatioF() or 1),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        if self.worker is not None and self.worker.frames == 1:
            self._set_status("connected", f"已连接 · 远端画面 {image.width()}x{image.height()} · 可直接操作")
            if self.selftest_text:
                QTimer.singleShot(600, self._run_selftest_typing)

    # ------------------------------------------------------------------ 输入转发
    def _map_to_remote(self, position: QPointF) -> tuple[int, int] | None:
        """把窗口坐标换算成远端像素坐标, 黑边区域返回 None."""
        if self.remote_size == (0, 0):
            return None
        pixmap = self.video.pixmap()
        if pixmap is None or pixmap.isNull():
            return None
        width = pixmap.width()
        height = pixmap.height()
        if width <= 0 or height <= 0:
            return None
        offset_x = (self.video.width() - width) / 2.0
        offset_y = (self.video.height() - height) / 2.0
        x = int((position.x() - offset_x) / width * self.remote_size[0])
        y = int((position.y() - offset_y) / height * self.remote_size[1])
        if 0 <= x < self.remote_size[0] and 0 <= y < self.remote_size[1]:
            return x, y
        return None

    def _active(self) -> bool:
        return self.worker is not None and self.worker.isRunning() and not self.worker.stop_event.is_set()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.video or not self._active():
            return super().eventFilter(watched, event)
        if event.type() == QEvent.MouseButtonPress:
            self._on_mouse_press(event)
        elif event.type() == QEvent.MouseMove:
            self._on_mouse_move(event)
        elif event.type() == QEvent.Wheel:
            self._on_wheel(event)
        elif event.type() == QEvent.KeyPress:
            self._on_key_press(event)
        else:
            return super().eventFilter(watched, event)
        return True

    def _on_mouse_press(self, event: Any) -> None:
        point = self._map_to_remote(event.position())
        if point is None or self.worker is None:
            return
        button = {Qt.LeftButton: 1, Qt.MiddleButton: 2, Qt.RightButton: 3}.get(event.button(), 0)
        if button:
            self.worker.send({"kind": "mouse", "action": "click", "x": point[0], "y": point[1], "button": button})
        self.video.setFocus()

    def _on_mouse_move(self, event: Any) -> None:
        now = time.perf_counter()
        if now - self.last_move_sent < 0.04:
            return
        self.last_move_sent = now
        point = self._map_to_remote(event.position())
        if point is not None and self.worker is not None:
            self.worker.send({"kind": "mouse", "action": "move", "x": point[0], "y": point[1]})

    def _on_wheel(self, event: Any) -> None:
        point = self._map_to_remote(event.position())
        if point is None or self.worker is None:
            return
        button = 4 if event.angleDelta().y() > 0 else 5
        self.worker.send({"kind": "mouse", "action": "wheel", "x": point[0], "y": point[1], "button": button})

    def _on_key_press(self, event: Any) -> None:
        if self.worker is None:
            return
        mods = [name for flag, name in MODIFIERS if event.modifiers() & flag]
        text = event.text()
        if len(text) == 1 and text.isprintable():
            self.worker.send({"kind": "key", "action": "char", "text": text})
            return
        name = KEY_NAMES.get(event.key(), "")
        if not name and Qt.Key_A <= event.key() <= Qt.Key_Z:
            name = chr(event.key()).lower()
        if not name:
            return
        if mods:
            self.worker.send({"kind": "key", "action": "combo", "mods": mods, "key": name})
        else:
            self.worker.send({"kind": "key", "action": "key", "key": name})

    # ------------------------------------------------------------------ 自测
    def _run_selftest_typing(self) -> None:
        if self.worker is None:
            return
        for char in self.selftest_text:
            self.worker.send({"kind": "key", "action": "char", "text": char})
            time.sleep(0.03)
        QTimer.singleShot(1500, self._finish_selftest)

    def _finish_selftest(self) -> None:
        ok = self.worker is not None and self.worker.frames > 0
        reason = f"收到 {self.worker.frames if self.worker else 0} 帧画面" if ok else "未收到画面"
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            if self.worker is not None:
                frame = self.video.grab()
                frame.save(str(self.evidence_dir / "selftest_after_typing.png"), "PNG")
        if self.selftest_done is not None:
            self.selftest_done(ok, reason)
        self._on_disconnect()
        QTimer.singleShot(400, QApplication.instance().quit)

    def closeEvent(self, event: QEvent) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(3000)
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    """启动控制端界面; 带 --selftest 时自动跑一次连接与输入验证."""
    parser = argparse.ArgumentParser(description="Windows 控制端")
    parser.add_argument("--selftest", default="", help="自测标记文本, 非空则自动连接并输入")
    parser.add_argument("--evidence", default="", help="自测截图输出目录")
    parser.add_argument("--address", default="", help="预填被控端地址")
    parser.add_argument("--device-id", default="", help="预填设备 ID")
    parser.add_argument("--access-code", default="", help="预填连接码")
    arguments = parser.parse_args(argv)
    if arguments.selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _, log_dir = _app_paths()
    _setup_logging(log_dir)
    LOGGER.info("控制端启动: %s", "便携包" if _portable_root() else "系统安装")
    application = QApplication([sys.argv[0]])
    window = ControllerWindow()
    if arguments.address:
        window.address.setText(arguments.address)
    if arguments.device_id:
        window.device.setText(arguments.device_id)
    if arguments.access_code:
        window.code.setText(arguments.access_code)
    if arguments.selftest:
        window.selftest_text = arguments.selftest
        window.evidence_dir = Path(arguments.evidence) if arguments.evidence else None
        window.selftest_done = _record_selftest
        window.show()
        QTimer.singleShot(200, window._on_connect)
    else:
        window.show()
    return application.exec()


def _record_selftest(ok: bool, reason: str) -> None:
    sys.stdout.write(f"自测结果: {'通过' if ok else '失败'} · {reason}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
