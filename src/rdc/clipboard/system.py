# ==========================================================================================
# 文件        : system.py
# 所属目录    : src/rdc/clipboard/
# 文件用途    : 本机系统剪贴板读写, 供被控端把远端剪贴板落到真实桌面
# 架构定位    : 能力层的平台适配; 只被 session.host 调用, 不面向界面
# 核心功能    : Windows 走 user32; Linux 走 xclip; 读取失败返回空串而不是抛错
# 职责边界    : 只处理文本剪贴板; 不做脱敏、不做网络传输、不落盘
# 依赖关系    : 标准库 ctypes / subprocess; 运行时依赖 xclip (Linux)
# ==========================================================================================

"""系统剪贴板读写.

远端剪贴板同步是尽力而为: 读写失败返回空串或 False, 由调用方给出"未同步"提示,
不允许把失败伪装成成功.
"""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
from typing import Any

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def read_system_clipboard() -> str:
    """读取本机文本剪贴板; 无内容或读取失败返回空串."""
    if sys.platform.startswith("win"):
        return _read_windows()
    if sys.platform.startswith("linux"):
        return _read_linux()
    return ""


def write_system_clipboard(text: str) -> bool:
    """写入本机文本剪贴板; 失败返回 False."""
    if sys.platform.startswith("win"):
        return _write_windows(text)
    if sys.platform.startswith("linux"):
        return _write_linux(text)
    return False


def _bind_windows_prototypes(user32: Any, kernel32: Any) -> None:
    """声明 Win32 剪贴板接口的指针宽度; 缺省 c_int 会把 64 位句柄截断成野指针."""
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int


def _read_windows() -> str:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _bind_windows_prototypes(user32, kernel32)
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return str(ctypes.wstring_at(pointer))
        except OSError:
            return ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_windows(text: str) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _bind_windows_prototypes(user32, kernel32)
    encoded = (text + "\0").encode("utf-16-le")
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def _read_linux() -> str:
    if shutil.which("xclip") is None:
        return ""
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-o"],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def _write_linux(text: str) -> bool:
    if shutil.which("xclip") is None:
        return False
    try:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-i"],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
