# ==========================================================================================
# 文件        : e2e_common.py
# 所属目录    : scripts/
# 文件用途    : 端到端远程输入验证的公共装置: 帧收发、信令客户端、中继接入、AEAD 通道、Windows 取证
# 架构定位    : 验收装置, 复用产品信令/中继实现, 不修改产品代码
# 核心功能    : 4 字节长度帧、注册/挑战/认证、offer/answer 路由、relay_allocate、数据面握手、
#               ChaCha20-Poly1305 会话通道、前台窗口/剪贴板/窗口矩形等取证原语
# 职责边界    : 不承载产品业务逻辑; 不写 src; 不访问公网; 不伪造成功
# ==========================================================================================

"""端到端验证公共组件.

帧格式与信令、中继一致: 4 字节大端长度 + 负载. 信令帧是 JSON 包络,
数据面首个帧是明文握手 JSON, 之后是由本模块 AEAD 通道承载的密文.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import struct
import time
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

from poc.common.crypto import AeadSession, EphemeralKeyPair, derive_aead_session

from rdc.crypto.identity import (
    DeviceIdentity,
    b64decode,
    b64encode,
    device_id_from_public_key,
    verify_signature,
)
from rdc.signaling.envelope import build_envelope

MAX_FRAME_BYTES = 6 << 20
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class E2EFailure(Exception):
    """端到端装置失败, 携带可落盘的错误说明."""


# ================================ 帧收发 ================================


def send_packet(sock: socket.socket, payload: bytes) -> None:
    """发送一帧: 4 字节大端长度 + 负载."""
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise E2EFailure(f"帧长度非法: {len(payload)}")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def recv_packet(sock: socket.socket) -> bytes | None:
    """接收一帧; 对端正常关闭返回 None."""
    header = _recv_exactly(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise E2EFailure(f"帧长度非法: {length}")
    body = _recv_exactly(sock, length)
    if body is None:
        raise E2EFailure("帧体被截断")
    return body


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    """发送 JSON 帧."""
    send_packet(sock, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def recv_json(sock: socket.socket) -> dict[str, Any] | None:
    """接收 JSON 帧."""
    body = recv_packet(sock)
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise E2EFailure(f"对端帧不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise E2EFailure("对端帧必须是 JSON 对象")
    return value


# ================================ 信令客户端 ================================


class SignalingClient:
    """真实 TCP 信令客户端, 只实现本验证需要的最小流程."""

    def __init__(self, host: str, port: int, timeout_s: float = 20.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None
        self.identity: DeviceIdentity | None = None
        self.token = ""

    def connect(self) -> None:
        """建立 TCP 连接并设置超时."""
        self.sock = socket.create_connection((self.host, self.port), timeout=10.0)
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
            raise E2EFailure("信令连接未建立")
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
        challenge = self._expect("challenge")
        nonce = str(challenge["payload"]["nonce"])
        auth = build_envelope(
            identity,
            "auth",
            {
                "device_id": identity.device_id,
                "signature": identity.sign({"device_id": identity.device_id, "nonce": nonce}),
            },
        )
        send_json(self.sock, auth)
        ack = self._expect("auth_ok")
        self.token = str(ack["payload"]["token"])
        return ack["payload"]

    def send_envelope(
        self,
        msg_type: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> None:
        """按七字段包络发送一条消息."""
        if self.sock is None or self.identity is None:
            raise E2EFailure("信令未认证")
        send_json(self.sock, build_envelope(self.identity, msg_type, payload, session_id=session_id))

    def recv_until(self, msg_type: str) -> dict[str, Any]:
        """读取直到指定类型; 遇到 error 直接失败."""
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            frame = recv_json(self.sock) if self.sock is not None else None
            if frame is None:
                raise E2EFailure(f"等待 {msg_type} 时信令连接关闭")
            current = str(frame.get("type", ""))
            if current == "error":
                payload = frame.get("payload", {})
                raise E2EFailure(f"信令返回错误: {payload.get('code')} {payload.get('message')}")
            if current == msg_type:
                return frame
        raise E2EFailure(f"等待 {msg_type} 超时")

    def _expect(self, msg_type: str) -> dict[str, Any]:
        return self.recv_until(msg_type)


# ================================ 中继数据面 ================================


def relay_connect(host: str, port: int, relay_token: str, session_id: str, role: str) -> socket.socket:
    """接入中继数据面并完成明文握手, 返回已就绪的连接."""
    if role not in ("publisher", "subscriber"):
        raise E2EFailure(f"非法数据面角色: {role}")
    sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(30.0)
    send_json(
        sock,
        {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": relay_token,
            "session_id": session_id,
            "role": role,
        },
    )
    hello = recv_json(sock)
    if hello is None or hello.get("type") != "relay_ready":
        sock.close()
        detail = "连接关闭" if hello is None else f"{hello.get('type')} {hello.get('message', '')}"
        raise E2EFailure(f"中继数据面握手失败: {detail}")
    return sock


# ================================ AEAD 通道 ================================


class AeadChannel:
    """在已建立的数据面连接上收发 AEAD 密文帧."""

    def __init__(self, sock: socket.socket, session: AeadSession) -> None:
        self.sock = sock
        self.session = session
        self.sent_frames = 0
        self.recv_frames = 0

    def send(self, payload: dict[str, Any]) -> int:
        """加密并发送一条 JSON, 返回发送完成时刻(纳秒)."""
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ciphertext = self.session.encrypt(raw)
        send_packet(self.sock, ciphertext)
        self.sent_frames += 1
        return time.time_ns()

    def recv(self) -> dict[str, Any]:
        """接收并解密一条 JSON."""
        body = recv_packet(self.sock)
        if body is None:
            raise E2EFailure("AEAD 通道被对端关闭")
        try:
            plaintext = self.session.decrypt(body)
        except ValueError as exc:
            raise E2EFailure(f"AEAD 解密失败: {exc}") from exc
        self.recv_frames += 1
        try:
            value = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise E2EFailure(f"AEAD 明文不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise E2EFailure("AEAD 明文必须是 JSON 对象")
        return value


def controller_handshake(sock: socket.socket, identity: DeviceIdentity) -> AeadSession:
    """控制端发起 AEAD 握手, 并校验被控端身份签名."""
    pair = EphemeralKeyPair.generate()
    salt = b64encode(os.urandom(16))
    hello: dict[str, Any] = {
        "kind": "e2e_hello",
        "v": 1,
        "x25519": b64encode(pair.public_bytes),
        "salt": salt,
        "device_id": identity.device_id,
        "public_key": identity.public_key_b64,
    }
    hello["sig"] = identity.sign(hello)
    send_json(sock, hello)
    ack = recv_json(sock)
    if ack is None or ack.get("kind") != "e2e_hello_ack":
        raise E2EFailure("被控端 AEAD 握手应答缺失")
    unsigned = _without_signature(ack)
    public_key = str(ack.get("public_key", ""))
    if not verify_signature(public_key, unsigned, str(ack.get("sig", ""))):
        raise E2EFailure("被控端 AEAD 握手签名无效")
    if device_id_from_public_key(b64decode(public_key)) != str(ack.get("device_id", "")):
        raise E2EFailure("被控端 device_id 与公钥不一致")
    shared = pair.exchange(b64decode(str(ack["x25519"])))
    return derive_aead_session(shared, b64decode(salt), initiator=True)


def host_handshake(sock: socket.socket, identity: DeviceIdentity) -> AeadSession:
    """被控端应答 AEAD 握手, 并校验控制端身份签名."""
    hello = recv_json(sock)
    if hello is None or hello.get("kind") != "e2e_hello":
        raise E2EFailure("未收到控制端 AEAD 握手")
    unsigned = _without_signature(hello)
    public_key = str(hello.get("public_key", ""))
    if not verify_signature(public_key, unsigned, str(hello.get("sig", ""))):
        raise E2EFailure("控制端 AEAD 握手签名无效")
    if device_id_from_public_key(b64decode(public_key)) != str(hello.get("device_id", "")):
        raise E2EFailure("控制端 device_id 与公钥不一致")
    pair = EphemeralKeyPair.generate()
    ack: dict[str, Any] = {
        "kind": "e2e_hello_ack",
        "v": 1,
        "x25519": b64encode(pair.public_bytes),
        "device_id": identity.device_id,
        "public_key": identity.public_key_b64,
    }
    ack["sig"] = identity.sign(ack)
    send_json(sock, ack)
    shared = pair.exchange(b64decode(str(hello["x25519"])))
    return derive_aead_session(shared, b64decode(str(hello["salt"])), initiator=False)


def _without_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "sig"}


# ================================ 统计 ================================


def percentile_ms(values: list[float], ratio: float) -> float:
    """毫秒样本的百分位, 空列表返回 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


# ================================ 等待与文件 ================================


def wait_json_file(path: Path, predicate: Callable[[dict[str, Any]], bool], timeout_s: float) -> dict[str, Any]:
    """等待一个 JSON 文件出现并满足条件; 超时抛错, 错误里带最后状态."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.05)
            continue
        last = payload
        if isinstance(payload, dict) and predicate(payload):
            return payload
        time.sleep(0.05)
    raise E2EFailure(f"等待 {path.name} 超时, 最后状态: {last!r}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON 证据."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def tail_text(path: Path, limit: int = 4000) -> str:
    """读取日志尾部, 文件不存在返回空串."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


# ================================ Windows 取证原语 ================================


def read_clipboard_text() -> str | None:
    """读取系统剪贴板文本; 无内容或占用时返回 None."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(pointer)
    finally:
        user32.CloseClipboard()


def write_clipboard_text(text: str) -> None:
    """写回系统剪贴板文本, 失败时保持原状."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    payload = text.encode("utf-16-le") + b"\x00\x00"
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(pointer)
    if not user32.OpenClipboard(None):
        return
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def find_window_by_title(keyword: str) -> int | None:
    """按标题关键字查找可见顶层窗口句柄."""
    user32 = ctypes.windll.user32
    matches: list[int] = []

    def visit(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if keyword in buffer.value:
            matches.append(int(hwnd))
        return True

    callback = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(callback(visit), 0)
    return matches[0] if matches else None


def force_foreground(hwnd: int) -> bool:
    """把窗口切到前台; 拿不到焦点返回 False, 调用方不得继续注入."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.ShowWindow(hwnd, 9)
    user32.AllowSetForegroundWindow(0xFFFFFFFF)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return True
    foreground = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    current_thread = kernel32.GetCurrentThreadId()
    if foreground_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    if foreground_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


class WindowRect(ctypes.Structure):
    """Win32 RECT."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """返回窗口屏幕矩形 (left, top, right, bottom); 失败返回 None."""
    rect = WindowRect()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def grab_window(hwnd: int, target: PurePath) -> bool:
    """截取窗口区域并落盘 PNG; 失败返回 False."""
    from PIL import ImageGrab

    bounds = window_rect(hwnd)
    if bounds is None:
        return False
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        return False
    image = ImageGrab.grab(bbox=(left, top, right, bottom))
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    image.save(str(target), "PNG")
    return Path(target).is_file() and Path(target).stat().st_size > 1024
