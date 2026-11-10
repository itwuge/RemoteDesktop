# ==========================================================================================
# 文件        : autostart.py
# 所属目录    : src/rdc/core/
# 文件用途    : 开机自启登记与真实状态读取; Windows 走 HKCU Run 键, Linux 走 XDG autostart
# 架构定位    : 基础设施层; 只做系统级登记, 不依赖界面与命令层
# 核心功能    : 读取真实自启状态、写入或删除自启项、生成自启命令行
# 职责边界    : 不弹窗; 不写日志; 不修改配置文件; 不猜测状态
# ==========================================================================================

"""开机自启登记.

Windows 选 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 而不是启动目录快捷方式:
1. Run 键是单个字符串值, 读取即真实状态; 启动目录要先解析 .lnk 才能拿到目标, 回读链路更长.
2. 卸载时 NSIS 只需一次 DeleteRegValue, 不依赖 Shell 链接解析.
3. 均为当前用户范围, 不需要管理员权限, 与安装包 RequestExecutionLevel user 一致.

Linux 按 XDG 写 ``~/.config/autostart/rdc-desktop.desktop`` (遵守 XDG_CONFIG_HOME),
文件存在且 Exec 非空即视为启用, 删除文件即关闭.

自启命令行统一带 ``--autostart`` 参数, 界面据此静默进入托盘, 不弹主窗口.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from rdc.core import runtime

AUTOSTART_FLAG = "--autostart"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE_NAME = "RDC 远程桌面"
LINUX_DESKTOP_NAME = "rdc-desktop.desktop"
LINUX_AUTOSTART_DIR = "autostart"


@dataclass(frozen=True)
class AutostartState:
    """一次真实读取的结果; enabled 只由系统登记项推导, 不读本地缓存."""

    supported: bool
    enabled: bool
    target: str
    command: str
    detail: str
    portable: bool = False


def platform_kind() -> str:
    """当前系统的自启实现类别: windows、linux 或 unsupported."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def launch_command(executable: Path | None = None) -> str:
    """自启命令行; 打包态取可执行文件, 开发态仅用于测试."""
    target = executable if executable is not None else Path(sys.executable)
    return f'"{target}" {AUTOSTART_FLAG}'


def linux_config_home(home: Path | None = None) -> Path:
    """XDG 配置根; 环境变量为绝对路径时优先, 否则退回 ~/.config."""
    configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if configured:
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate
    return (home if home is not None else Path.home()) / ".config"


def linux_desktop_path(home: Path | None = None) -> Path:
    """Linux 自启 desktop 文件路径."""
    return linux_config_home(home) / LINUX_AUTOSTART_DIR / LINUX_DESKTOP_NAME


def desktop_entry_text(command: str) -> str:
    """生成 XDG 自启 desktop 文件内容."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Version=1.5\n"
        "Name=RDC 远程桌面\n"
        "Comment=登录系统后静默启动并进入托盘\n"
        f"Exec={command}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def read_linux_entry(path: Path) -> str | None:
    """读取 desktop 文件的 Exec; 文件不存在、被禁用或缺少 Exec 时返回 None."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    command = ""
    enabled = True
    for line in text.splitlines():
        key, _, value = line.partition("=")
        name = key.strip()
        if name == "Exec":
            command = value.strip()
        elif name == "X-GNOME-Autostart-enabled":
            enabled = value.strip().lower() not in {"false", "0", "no"}
        elif name == "Hidden":
            enabled = value.strip().lower() not in {"true", "1", "yes"}
    if not enabled or not command:
        return None
    return command


def write_linux_entry(path: Path, command: str) -> None:
    """原子写入 desktop 文件; 目录不可写时抛 OSError 交给调用方呈现."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(desktop_entry_text(command), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def remove_linux_entry(path: Path) -> None:
    """删除 desktop 文件; 本来不存在时视为已关闭."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def read_windows_value(value_name: str = WINDOWS_VALUE_NAME, run_key: str = WINDOWS_RUN_KEY) -> str | None:
    """读取 HKCU Run 键下的自启命令行; 值或键不存在返回 None."""
    if sys.platform != "win32":
        raise OSError("当前平台不是 Windows")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return None
    return str(value)


def write_windows_value(command: str, value_name: str = WINDOWS_VALUE_NAME, run_key: str = WINDOWS_RUN_KEY) -> None:
    """写入 HKCU Run 键自启命令行; 失败抛 OSError."""
    if sys.platform != "win32":
        raise OSError("当前平台不是 Windows")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)


def delete_windows_value(value_name: str = WINDOWS_VALUE_NAME, run_key: str = WINDOWS_RUN_KEY) -> None:
    """删除 HKCU Run 键自启值; 值或键不存在视为已关闭."""
    if sys.platform != "win32":
        raise OSError("当前平台不是 Windows")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        return


def current_state(
    *,
    platform: str | None = None,
    home: Path | None = None,
    run_key: str = WINDOWS_RUN_KEY,
    value_name: str = WINDOWS_VALUE_NAME,
) -> AutostartState:
    """读取系统真实自启状态; 不读取也不信任 config.json 里的偏好."""
    kind = platform if platform is not None else platform_kind()
    portable = runtime.is_portable()
    if not runtime.is_frozen():
        return AutostartState(
            supported=False,
            enabled=False,
            target="",
            command="",
            detail="开发态不注册开机自启; 安装版默认开启, 便携版可手动开启",
            portable=portable,
        )
    if kind == "windows":
        target = f"HKCU\\{run_key}\\{value_name}"
        stored = read_windows_value(value_name, run_key)
        detail = f"已登记在 {target}" if stored else f"未登记在 {target}"
        return AutostartState(
            supported=True,
            enabled=bool(stored),
            target=target,
            command=stored or launch_command(),
            detail=detail,
            portable=portable,
        )
    if kind == "linux":
        path = linux_desktop_path(home)
        stored = read_linux_entry(path)
        detail = f"已写入 {path}" if stored else f"未写入 {path}"
        return AutostartState(
            supported=True,
            enabled=bool(stored),
            target=str(path),
            command=stored or launch_command(),
            detail=detail,
            portable=portable,
        )
    return AutostartState(
        supported=False,
        enabled=False,
        target="",
        command="",
        detail="当前系统不支持开机自启, 仅支持 Windows 与 Linux",
        portable=portable,
    )


def set_enabled(
    enabled: bool,
    *,
    platform: str | None = None,
    home: Path | None = None,
    run_key: str = WINDOWS_RUN_KEY,
    value_name: str = WINDOWS_VALUE_NAME,
) -> AutostartState:
    """写入或删除自启项并立即回读; 失败抛 OSError, 由命令层回读并报出原因."""
    if not runtime.is_frozen():
        raise OSError("开发态不支持注册开机自启")
    kind = platform if platform is not None else platform_kind()
    if kind == "windows":
        if enabled:
            write_windows_value(launch_command(), value_name, run_key)
        else:
            delete_windows_value(value_name, run_key)
        return current_state(platform=kind, home=home, run_key=run_key, value_name=value_name)
    if kind == "linux":
        path = linux_desktop_path(home)
        if enabled:
            write_linux_entry(path, launch_command())
        else:
            remove_linux_entry(path)
        return current_state(platform=kind, home=home, run_key=run_key, value_name=value_name)
    raise OSError("当前系统不支持开机自启, 仅支持 Windows 与 Linux")
