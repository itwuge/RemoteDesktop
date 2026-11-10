# ==========================================================================================
# 文件        : input_probe.py
# 所属目录    : scripts/
# 文件用途    : 冒烟与基准共用的 Windows 键鼠注入装置, 只用于本机取证
# 架构定位    : 测试装置, 不进入产品路径
# 核心功能    : SendInput 键盘/鼠标注入; 复制与保存组合键; 百分位统计
# 职责边界    : 不连网; 不解码; 不实现远程协议; 不产生用户可见文案
# ==========================================================================================

"""Windows 输入注入装置."""

from __future__ import annotations

import contextlib
import ctypes
import time


class _KeybdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeybdInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


def percentile(samples: list[float], ratio: float) -> float:
    """百分位, 空列表返回 0.

    :param samples: 样本
    :param ratio: 0 到 1 的分位
    :return: 分位值, 保留 3 位
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


class Injector:
    """Windows SendInput 封装; 只用于冒烟与基准取证."""

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_WHEEL = 0x0800
    VK_CONTROL = 0x11
    VK_A = 0x41
    VK_C = 0x43
    VK_S = 0x53

    def __init__(self) -> None:
        self.user32 = ctypes.windll.user32
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)

    def _send_key(self, vk: int, scan: int, flags: int) -> int:
        item = _Input(type=1, u=_InputUnion(ki=_KeybdInput(vk, scan, flags, 0, None)))
        return self.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input))

    def type_text(self, text: str, delay_s: float = 0.02) -> list[int]:
        """逐字符注入 Unicode 并返回每个字符的发送时刻(纳秒)."""
        sent_at: list[int] = []
        for char in text:
            sent_at.append(time.time_ns())
            self._send_key(0, ord(char), self.KEYEVENTF_UNICODE)
            self._send_key(0, ord(char), self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP)
            time.sleep(delay_s)
        return sent_at

    def type_char(self, char: str) -> tuple[int, int]:
        """注入单个字符, 返回 (开始时刻, 注入完成时刻), 单位纳秒, 取自性能计数器."""
        started_ns = time.perf_counter_ns()
        self._send_key(0, ord(char), self.KEYEVENTF_UNICODE)
        self._send_key(0, ord(char), self.KEYEVENTF_UNICODE | self.KEYEVENTF_KEYUP)
        return started_ns, time.perf_counter_ns()

    def _chord(self, virtual_key: int) -> None:
        """Ctrl + 指定键的组合键; 逐事件留间隔, 避免高负载下修饰键先被吞掉."""
        self._send_key(self.VK_CONTROL, 0, 0)
        time.sleep(0.03)
        self._send_key(virtual_key, 0, 0)
        time.sleep(0.03)
        self._send_key(virtual_key, 0, self.KEYEVENTF_KEYUP)
        time.sleep(0.03)
        self._send_key(self.VK_CONTROL, 0, self.KEYEVENTF_KEYUP)
        time.sleep(0.2)

    def select_all(self) -> None:
        """Ctrl+A 组合键."""
        self._chord(self.VK_A)

    def copy(self) -> None:
        """Ctrl+C 组合键."""
        self._chord(self.VK_C)

    def save(self) -> None:
        """Ctrl+S 组合键."""
        self._chord(self.VK_S)

    def move_to(self, x: int, y: int) -> None:
        self.user32.SetCursorPos(x, y)

    def click(self, x: int, y: int, right: bool = False) -> None:
        self.move_to(x, y)
        down = self.MOUSEEVENTF_RIGHTDOWN if right else self.MOUSEEVENTF_LEFTDOWN
        up = self.MOUSEEVENTF_RIGHTUP if right else self.MOUSEEVENTF_LEFTUP
        self.user32.mouse_event(down, 0, 0, 0, 0)
        self.user32.mouse_event(up, 0, 0, 0, 0)

    def move_relative(self, dx: int, dy: int) -> None:
        self.user32.mouse_event(self.MOUSEEVENTF_MOVE, dx, dy, 0, 0)

    def wheel(self, delta: int = -120) -> None:
        self.user32.mouse_event(self.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
