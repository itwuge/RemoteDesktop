# ==========================================================================================
# 文件        : e2e_common.py
# 所属目录    : scripts/
# 文件用途    : 端到端验证的公共装置; 协议、加解密、握手与数据面全部转发产品实现
# 架构定位    : 薄壳。实现已收编到 src/rdc/session 与 src/rdc/clipboard, 这里只保留
#               验收专用的等待/取证原语(窗口截图、剪贴板、JSON 证据), 不再自带第二套协议
# 核心功能    : 产品实现的统一导出; 旧的信令客户端参数兼容; Windows 取证原语
# 职责边界    : 不重写帧格式、AEAD、握手、中继接入; 不承载产品业务逻辑; 不访问公网
# ==========================================================================================

"""端到端验证公共组件.

协议实现只有一份: ``rdc.session.wire`` / ``crypto`` / ``handshake`` / ``dataplane`` / ``control``.
本模块保留旧名字做导入兼容, 另含验收专用的 Windows 取证与等待原语.
"""

from __future__ import annotations

import ctypes
import json
import time
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

from rdc.clipboard.system import read_system_clipboard as _read_system_clipboard
from rdc.clipboard.system import write_system_clipboard as _write_system_clipboard
from rdc.session.control import SignalingClient as _ProductSignalingClient
from rdc.session.crypto import HANDSHAKE_VERSION, AeadSession, EphemeralKeyPair, derive_aead_session
from rdc.session.dataplane import AeadChannel, DataPlaneError, relay_connect, tls_connect
from rdc.session.handshake import (
    ACCESS_FAILURE_LIMIT,
    ACCESS_MAX_BACKOFF_S,
    ACCESS_NONCE_CACHE,
    ACCESS_PROOF_ALG,
    AccessCodeGuard,
    HandshakeError,
    access_proof_message,
    build_access_proof,
    controller_handshake,
    host_handshake,
    verify_access_proof,
)
from rdc.session.wire import MAX_FRAME_BYTES, WireError, recv_json, recv_packet, send_json, send_packet
from rdc.session.wire import recv_exactly as _recv_exactly

E2EFailure = HandshakeError
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

__all__ = [
    "ACCESS_FAILURE_LIMIT",
    "ACCESS_MAX_BACKOFF_S",
    "ACCESS_NONCE_CACHE",
    "ACCESS_PROOF_ALG",
    "HANDSHAKE_VERSION",
    "MAX_FRAME_BYTES",
    "AccessCodeGuard",
    "AeadChannel",
    "AeadSession",
    "AeadSessionLink",
    "DataPlaneError",
    "E2EFailure",
    "EphemeralKeyPair",
    "HandshakeError",
    "SignalingClient",
    "WireError",
    "_recv_exactly",
    "access_proof_message",
    "build_access_proof",
    "controller_handshake",
    "derive_aead_session",
    "find_window_by_title",
    "force_foreground",
    "grab_window",
    "host_handshake",
    "percentile_ms",
    "read_clipboard_text",
    "recv_json",
    "recv_packet",
    "relay_connect",
    "send_json",
    "send_packet",
    "tail_text",
    "tls_connect",
    "verify_access_proof",
    "wait_json_file",
    "window_rect",
    "write_clipboard_text",
    "write_json",
]


class SignalingClient(_ProductSignalingClient):
    """信令客户端兼容壳: 支持旧的位置参数与证书指纹参数, 实现全部来自产品代码."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 20.0,
        use_tls: bool = False,
        ca_file: str | PurePath = "",
        ca_sha256: str = "",
    ) -> None:
        super().__init__(host, port, timeout_s, use_tls=use_tls, ca_file=str(ca_file or ""), sni="")
        self.ca_sha256 = ca_sha256

    def _expect(self, msg_type: str) -> dict[str, Any]:
        """兼容旧名字: 等待指定类型应答."""
        return self.recv_until(msg_type)


class AeadSessionLink:
    """把产品 AeadChannel 适配成 ``AeadLink``; 只是方法转发, 不含协议逻辑."""

    def __init__(self, channel: AeadChannel) -> None:
        self.channel = channel

    def send(self, payload: dict[str, Any]) -> int:
        return self.channel.send(payload)

    def recv_raw(self) -> bytes:
        return self.channel.recv_raw()

    def close(self) -> None:
        self.channel.close()


# ================================ 计时与等待 ================================


def percentile_ms(values: list[float], ratio: float) -> float:
    """毫秒样本的百分位, 空列表返回 0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


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


# ================================ 桌面取证原语 ================================


def read_clipboard_text() -> str | None:
    """读取系统剪贴板文本; 无内容返回 None."""
    text = _read_system_clipboard()
    return text or None


def write_clipboard_text(text: str) -> None:
    """写回系统剪贴板文本; 产品实现内部已做失败兜底."""
    _write_system_clipboard(text)


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
