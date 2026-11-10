# ==========================================================================================
# 文件        : windows_sendinput.py
# 所属目录    : src/rdc/input/
# 文件用途    : Windows 键鼠注入端口实现, 供被控端命令层装配使用
# 架构定位    : 能力层的平台适配, 只实现 rdc.contracts.ports 定义的 InputInjector
# 核心功能    : InputEvent → SendInput; 只放行契约允许的按键名、修饰键与鼠标动作
# 职责边界    : 不改系统输入配置、不落盘、不面向界面; 非法事件静默忽略不中断会话
# 依赖关系    : rdc.contracts.ports; 运行时依赖 Windows user32
# ==========================================================================================

"""Windows SendInput 输入注入端口.

事件字段校验与 Linux 端口保持一致: 白名单外的按键名、修饰键与按钮一律忽略,
避免异常输入打断会话. 中文等非 ASCII 字符走 KEYEVENTF_UNICODE 直接注入 unicode 码点.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import time

from rdc.contracts.ports import InputEvent, PortError
from rdc.core.errors import INPUT_PERMISSION_DENIED

KEY_NAMES = frozenset(
    {
        "Return",
        "BackSpace",
        "Tab",
        "Escape",
        "Delete",
        "Left",
        "Right",
        "Up",
        "Down",
        "Home",
        "End",
        "Page_Up",
        "Page_Down",
        "space",
    }
)
MODIFIER_NAMES = frozenset({"ctrl", "shift", "alt", "super"})
MAX_TEXT_LEN = 4
CLICK_BUTTONS = frozenset({1, 2, 3})

VK_CODES = {
    "Return": 0x0D,
    "BackSpace": 0x08,
    "Tab": 0x09,
    "Escape": 0x1B,
    "Delete": 0x2E,
    "Left": 0x25,
    "Right": 0x27,
    "Up": 0x26,
    "Down": 0x28,
    "Home": 0x24,
    "End": 0x23,
    "Page_Up": 0x21,
    "Page_Down": 0x22,
    "space": 0x20,
    # 字母与数字的虚拟键码: 组合键(ctrl+c/ctrl+s 等)依赖它们
    **{chr(0x61 + index): 0x41 + index for index in range(26)},
    **{chr(0x41 + index): 0x41 + index for index in range(26)},
    **{chr(0x30 + index): 0x30 + index for index in range(10)},
}
MODIFIER_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "super": 0x5B}
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001
WM_CHAR = 0x0102
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
# 控制台/终端窗口从控制台输入缓冲区取字, 直接投递 WM_CHAR 会被丢弃, 必须走 SendInput
CONSOLE_CLASSES = frozenset({"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"})


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


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _GuiThreadInfo(ctypes.Structure):
    """GetGUIThreadInfo 的入参结构, 用于取当前焦点控件窗口句柄."""

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", _Rect),
    ]


class SendInputInjector:
    """用 SendInput 注入键鼠事件; 失败只记一次计数, 不中断会话."""

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise PortError(INPUT_PERMISSION_DENIED, "SendInput 仅在 Windows 可用")
        self._user32 = ctypes.windll.user32
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        self.failed = 0

    def inject(self, event: InputEvent) -> None:
        """注入一条输入事件; 不在白名单内的事件直接忽略."""
        if event.kind == "key":
            self._inject_key(event)
        elif event.kind == "mouse":
            self._inject_mouse(event)

    def _inject_key(self, event: InputEvent) -> None:
        if event.action == "char":
            if 0 < len(event.text) <= MAX_TEXT_LEN:
                self.type_text(event.text)
            return
        if event.action == "key":
            virtual_key = VK_CODES.get(event.key)
            if virtual_key is not None:
                self._tap(virtual_key)
            return
        if event.action == "combo" and event.key in VK_CODES and all(item in MODIFIER_NAMES for item in event.mods):
            self._combo([MODIFIER_VK[item] for item in event.mods], VK_CODES[event.key])

    def _inject_mouse(self, event: InputEvent) -> None:
        if event.action == "move":
            self.move_to(event.x, event.y)
        elif event.action == "click" and event.button in CLICK_BUTTONS:
            self.click(event.x, event.y, event.button)
        elif event.action == "wheel":
            self.move_to(event.x, event.y)
            self.wheel(-event.button * WHEEL_DELTA)

    # ------------------------------------------------------------------ 注入原语
    def type_text(self, text: str, delay_s: float = 0.0) -> None:
        """逐字符注入文本, 优先走焦点窗口的 WM_CHAR.

        SendInput 的 KEYEVENTF_UNICODE 走 VK_PACKET 通道, Qt 一类按 VK_PACKET
        推导按键状态的窗口会把下一个字符误判成上一键的自动重复, 典型表现是
        "RDC-LINUX2WIN-2026-11-10" 落成 "RDC--INUX2WIN--026--1--0" (每个 '-'
        之后的字符被替换成 '-'). 因此文本字符直接投递 WM_CHAR 到焦点控件;
        没有焦点窗口或投递被系统拒绝时回落到 SendInput unicode. 控制台类窗口
        只从控制台输入缓冲区取字, WM_CHAR 会被丢弃, 这类窗口直接走 SendInput.
        """
        target = self._focused_window()
        console = bool(target) and self._window_class(target) in CONSOLE_CLASSES
        for char in text:
            if console or not target or not self._post_char(target, char):
                self._send_unicode(char)
            if delay_s > 0:
                time.sleep(delay_s)

    def _window_class(self, hwnd: int) -> str:
        """取窗口类名, 用于区分控制台与普通 GUI 窗口."""
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def _focused_window(self) -> int:
        """取前台线程的焦点控件句柄; 取不到时退回前台窗口, 都没有返回 0."""
        foreground = int(self._user32.GetForegroundWindow())
        if not foreground:
            return 0
        thread_id = self._user32.GetWindowThreadProcessId(foreground, None)
        info = _GuiThreadInfo(cbSize=ctypes.sizeof(_GuiThreadInfo))
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return foreground
        focus = int(info.hwndFocus or 0)
        active = int(info.hwndActive or 0)
        return focus or active or foreground

    def _post_char(self, target: int, char: str) -> bool:
        """把单个字符作为 WM_CHAR 投递给焦点控件; 非 BMP 字符拆成代理对."""
        point = 0x0D if char == "\n" else ord(char)
        codes = [point]
        if point > 0xFFFF:
            encoded = char.encode("utf-16-le")
            codes = [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]
        return all(self._user32.PostMessageW(target, WM_CHAR, code, 1) for code in codes)

    def _send_unicode(self, char: str) -> None:
        """SendInput 的 unicode 回落路径: 同一字符先按下再抬起."""
        self._send_key(0, ord(char), KEYEVENTF_UNICODE)
        self._send_key(0, ord(char), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)

    def move_to(self, x: int, y: int) -> None:
        """把光标移到屏幕绝对坐标."""
        self._user32.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, button: int = 1) -> None:
        """在指定坐标点击一次; button 取 1 左键、2 右键、3 中键."""
        self.move_to(x, y)
        down, up = _button_flags(button)
        self._user32.mouse_event(down, 0, 0, 0, 0)
        self._user32.mouse_event(up, 0, 0, 0, 0)

    def wheel(self, delta: int) -> None:
        """滚轮滚动一格, delta 为 Windows 的 120 的整数倍."""
        self._user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)

    def _tap(self, virtual_key: int) -> None:
        self._send_key(virtual_key, 0, 0)
        self._send_key(virtual_key, 0, KEYEVENTF_KEYUP)

    def _combo(self, modifiers: list[int], virtual_key: int) -> None:
        """按住修饰键再敲目标键; 逐事件留间隔, 避免高负载下修饰键被吞."""
        for modifier in modifiers:
            self._send_key(modifier, 0, 0)
            time.sleep(0.01)
        self._send_key(virtual_key, 0, 0)
        time.sleep(0.01)
        self._send_key(virtual_key, 0, KEYEVENTF_KEYUP)
        for modifier in reversed(modifiers):
            time.sleep(0.01)
            self._send_key(modifier, 0, KEYEVENTF_KEYUP)

    def _send_key(self, virtual_key: int, scan: int, flags: int) -> None:
        item = _Input(type=INPUT_KEYBOARD, u=_InputUnion(ki=_KeybdInput(virtual_key, scan, flags, 0, None)))
        if self._user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_Input)) == 0:
            self.failed += 1


def _button_flags(button: int) -> tuple[int, int]:
    """鼠标按钮到按下/抬起事件标志."""
    if button == 2:
        return MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    if button == 3:
        return MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    return MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
