# ==========================================================================================
# 文件        : linux_x11.py
# 所属目录    : src/rdc/input/
# 文件用途    : Linux (X11) 键鼠注入端口实现, 供被控端命令层装配使用
# 架构定位    : 能力层的平台适配, 只实现 rdc.contracts.ports 定义的 InputInjector
# 核心功能    : InputEvent → xdotool 命令; 只放行契约允许的按键名、修饰键与鼠标按钮
# 职责边界    : 不改系统输入配置; 不落盘; 不直接面向界面; 注入失败只计数不中断会话
# 依赖关系    : rdc.contracts.ports; 运行时依赖 xdotool 可执行文件
# ==========================================================================================

"""Linux X11 输入注入端口.

事件字段校验放在端口内: 控制端下发的按键名、修饰键与按钮不在白名单内时静默忽略,
与脚本层原行为一致, 避免异常输入中断会话.
"""

from __future__ import annotations

import os
import subprocess

from rdc.contracts.ports import InputEvent

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
WHEEL_BUTTONS = frozenset({4, 5})


class XdotoolInputInjector:
    """用 xdotool 注入键鼠事件; 失败只记一次计数, 不中断会话."""

    def __init__(self, display: str, timeout_s: float = 3.0) -> None:
        self._env = {**os.environ, "DISPLAY": display}
        self._timeout_s = timeout_s
        self.failed = 0

    def inject(self, event: InputEvent) -> None:
        """注入一条输入事件; 不在白名单内的事件直接忽略."""
        command = self.command_for(event)
        if command is None:
            return
        try:
            subprocess.run(
                ["xdotool", *command],
                env=self._env,
                timeout=self._timeout_s,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            self.failed += 1

    def command_for(self, event: InputEvent) -> list[str] | None:
        """把事件翻译成 xdotool 参数; 非法事件返回 None, 便于单测覆盖."""
        if event.kind == "key":
            return self._key_command(event)
        if event.kind == "mouse":
            return self._mouse_command(event)
        return None

    @staticmethod
    def _key_command(event: InputEvent) -> list[str] | None:
        if event.action == "char":
            if 0 < len(event.text) <= MAX_TEXT_LEN:
                return ["type", "--clearmodifiers", "--delay", "0", "--", event.text]
            return None
        if event.action == "key":
            if event.key in KEY_NAMES:
                return ["key", "--clearmodifiers", event.key]
            return None
        if event.action == "combo":
            if event.key and all(item in MODIFIER_NAMES for item in event.mods):
                return ["key", "--clearmodifiers", "+".join([*event.mods, event.key])]
            return None
        return None

    @staticmethod
    def _mouse_command(event: InputEvent) -> list[str] | None:
        if event.action == "move":
            return ["mousemove", str(event.x), str(event.y)]
        if event.action == "click" and event.button in CLICK_BUTTONS:
            return ["mousemove", str(event.x), str(event.y), "click", str(event.button)]
        if event.action == "wheel" and event.button in WHEEL_BUTTONS:
            return ["mousemove", str(event.x), str(event.y), "click", str(event.button)]
        return None
