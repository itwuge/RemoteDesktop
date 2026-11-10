# ==========================================================================================
# 文件        : settings.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 设置读写、诊断信息与服务器连通性检测命令, 供界面层调用
# 架构定位    : 命令层; 只依赖 core 与 update 的只读工具, 不依赖界面
# 核心功能    : 读写本机 config.json; 汇总版本/更新源/设备码/最近连接; TCP 探测服务器
# 职责边界    : 不改已冻结命令签名; 不新建第二套存储; 不伪造可达状态
# ==========================================================================================

"""设置与诊断命令.

契约缺口: desktop-v1 只定义了 ``open_settings`` 与 ``apply_relay_policy``,
没有设置读写与服务器连通性检测命令. 本模块复用运行时既有的 config.json 存储,
不改契约正文; 缺口登记在 ``.team/records/契约变更记录-契约-202611-001-20261110.md`` 第十节.
"""

from __future__ import annotations

import re
import socket
import time
from typing import Any

from rdc.commands import api
from rdc.core import autostart, runtime
from rdc.core.errors import CONFIG_INVALID, RELAY_UNREACHABLE
from rdc.core.result import CommandResult
from rdc.update.source import default_source_value, parse_source
from rdc.version import __version__

QUALITY_PROFILES: tuple[str, ...] = ("720p30",)
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
MAX_DEVICE_NAME = 32
DEFAULT_TIMEOUT_S = 2.0
AUTOSTART_INITIALIZED_KEY = "autostart_initialized"

DEFAULT_SETTINGS: dict[str, Any] = {
    "relay_endpoint": "",
    "device_name": "",
    "quality_profile": QUALITY_PROFILES[0],
    "log_level": "INFO",
    "auto_start": False,
    "clipboard_auto_sync": True,
}

STATUS_TEXT = {
    "pending": "等待中",
    "done": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}

_ENDPOINT_PATTERN = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://)?(?P<host>[A-Za-z0-9._-]+):(?P<port>\d{1,5})(?:/\S*)?$"
)


def parse_endpoint(value: str) -> tuple[str, int]:
    """解析服务器地址为 (host, port); 不合法时抛 ValueError 并给出中文原因."""
    text = (value or "").strip()
    if not text:
        raise ValueError("请填写服务器地址，格式 host:port")
    match = _ENDPOINT_PATTERN.match(text)
    if match is None:
        raise ValueError("地址格式应为 host:port，例如 192.168.37.132:27888")
    port = int(match.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    return match.group("host"), port


def get_settings() -> CommandResult:
    """读取本机设置; 缺失项按默认值补齐, 不写文件."""
    stored = runtime.load_config()
    settings = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in stored:
            settings[key] = stored[key]
    settings["log_level"] = str(settings["log_level"]).upper()
    settings["auto_start"] = bool(settings["auto_start"])
    settings["clipboard_auto_sync"] = bool(settings["clipboard_auto_sync"])
    return CommandResult(
        ok=True,
        message="设置已加载",
        data={"settings": settings, "config_path": str(runtime.config_path())},
    )


def save_settings(
    relay_endpoint: str,
    device_name: str,
    quality_profile: str,
    log_level: str,
    auto_start: bool,
    clipboard_auto_sync: bool = True,
) -> CommandResult:
    """校验并写入本机设置; 失败返回 CONFIG_INVALID 与具体原因."""
    endpoint = (relay_endpoint or "").strip()
    if endpoint:
        try:
            parse_endpoint(endpoint)
        except ValueError as error:
            return CommandResult(ok=False, message=str(error), error_code=CONFIG_INVALID)

    name = (device_name or "").strip()
    if len(name) > MAX_DEVICE_NAME:
        return CommandResult(
            ok=False,
            message=f"设备显示名最长 {MAX_DEVICE_NAME} 个字符",
            error_code=CONFIG_INVALID,
        )
    if any(ord(char) < 32 for char in name):
        return CommandResult(ok=False, message="设备显示名不能包含控制字符", error_code=CONFIG_INVALID)

    level = (log_level or "").upper()
    if level not in LOG_LEVELS:
        return CommandResult(ok=False, message="日志级别不在允许范围内", error_code=CONFIG_INVALID)
    if quality_profile not in QUALITY_PROFILES:
        return CommandResult(ok=False, message="中继画质档位不在允许范围内", error_code=CONFIG_INVALID)
    if not isinstance(clipboard_auto_sync, bool):
        return CommandResult(ok=False, message="剪贴板同步默认值必须是布尔开关", error_code=CONFIG_INVALID)

    settings = {
        "relay_endpoint": endpoint,
        "device_name": name,
        "quality_profile": quality_profile,
        "log_level": level,
        "auto_start": bool(auto_start),
        "clipboard_auto_sync": clipboard_auto_sync,
    }
    merged = runtime.load_config()
    merged.update(settings)
    try:
        runtime.save_config(merged)
    except OSError as error:
        return CommandResult(ok=False, message=f"配置写入失败：{error}", error_code=CONFIG_INVALID)

    from rdc.commands import clipboard as clipboard_commands

    clipboard_commands.set_clipboard_enabled(clipboard_auto_sync)
    return CommandResult(
        ok=True,
        message="设置已保存",
        data={"settings": settings, "config_path": str(runtime.config_path())},
    )


def get_diagnostics() -> CommandResult:
    """汇总诊断信息; 只读, 不发起网络."""
    settings = get_settings()
    values: dict[str, Any] = settings.data["settings"] if settings.data else dict(DEFAULT_SETTINGS)
    source_value = default_source_value()
    try:
        source_label = parse_source(source_value).label
    except ValueError:
        source_label = source_value
    service = api.get_service()
    return CommandResult(
        ok=True,
        message="诊断信息已刷新",
        data={
            "version": __version__,
            "update_source": source_label,
            "device_id": service.identity.device_id,
            "last_connection": _last_connection_text(service),
            "relay_endpoint": values["relay_endpoint"],
            "log_level": values["log_level"],
            "config_path": str(runtime.config_path()),
        },
    )


def check_server(endpoint: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> CommandResult:
    """对中继/信令服务器做一次 TCP 连通性检测, 返回中文结论与 RTT."""
    try:
        host, port = parse_endpoint(endpoint)
    except ValueError as error:
        return CommandResult(ok=False, message=f"不可达：{error}", error_code=CONFIG_INVALID)
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            rtt_ms = int((time.perf_counter() - started) * 1000)
    except TimeoutError:
        return CommandResult(
            ok=False,
            message="不可达：连接超时",
            error_code=RELAY_UNREACHABLE,
            data={"host": host, "port": port, "rtt_ms": None, "reachable": False},
        )
    except socket.gaierror:
        return CommandResult(
            ok=False,
            message="不可达：地址无法解析",
            error_code=RELAY_UNREACHABLE,
            data={"host": host, "port": port, "rtt_ms": None, "reachable": False},
        )
    except ConnectionRefusedError:
        return CommandResult(
            ok=False,
            message="不可达：连接被拒绝",
            error_code=RELAY_UNREACHABLE,
            data={"host": host, "port": port, "rtt_ms": None, "reachable": False},
        )
    except OSError as error:
        return CommandResult(
            ok=False,
            message=f"不可达：{error}",
            error_code=RELAY_UNREACHABLE,
            data={"host": host, "port": port, "rtt_ms": None, "reachable": False},
        )
    return CommandResult(
        ok=True,
        message=f"中继可达，RTT {rtt_ms}ms",
        data={"host": host, "port": port, "rtt_ms": rtt_ms, "reachable": True},
    )


def get_autostart_status() -> CommandResult:
    """读取系统真实开机自启状态; 读取失败直接返回失败, 不用本地缓存兜底."""
    try:
        state = autostart.current_state()
    except OSError as error:
        return CommandResult(ok=False, message=f"读取开机自启状态失败：{error}")
    return CommandResult(ok=True, message=state.detail, data=_autostart_payload(state))


def set_autostart_enabled(enabled: bool) -> CommandResult:
    """立即写入或删除开机自启项, 成功后回读校验; 失败回读实际状态并报出原因."""
    if not isinstance(enabled, bool):
        return CommandResult(ok=False, message="开机自启开关必须是布尔值", error_code=CONFIG_INVALID)
    try:
        state = autostart.set_enabled(enabled)
    except OSError as error:
        actual, actual_text = _read_autostart_quietly()
        data = _autostart_payload(actual) if actual is not None else {}
        data["requested"] = enabled
        return CommandResult(
            ok=False,
            message=f"开机自启设置失败：{error}；回读当前状态：{actual_text}",
            data=data,
        )
    if state.enabled != enabled:
        requested = "开启" if enabled else "关闭"
        actual_text = "开启" if state.enabled else "关闭"
        data = _autostart_payload(state)
        data["requested"] = enabled
        return CommandResult(
            ok=False,
            message=f"开机自启设置未生效：请求{requested}，回读为{actual_text}；{state.detail}",
            data=data,
        )
    message = (
        "已开启开机自启：登录系统后静默启动并进入托盘" if state.enabled else "已关闭开机自启：下次登录系统不会自动启动"
    )
    data = _autostart_payload(state)
    data["requested"] = enabled
    return CommandResult(ok=True, message=message, data=data)


def ensure_autostart_default() -> CommandResult:
    """首次启动写入安装版默认自启; 便携版默认关闭, 由设置页手动开启."""
    config = runtime.load_config()
    if bool(config.get(AUTOSTART_INITIALIZED_KEY)):
        return CommandResult(ok=True, message="开机自启已完成首次初始化")
    if not runtime.is_frozen():
        return CommandResult(ok=True, message="开发态不注册开机自启")
    if runtime.is_portable():
        return _mark_autostart_initialized(config, "便携版默认不开启开机自启，可在设置页手动开启")
    try:
        state = autostart.current_state()
        if not state.enabled:
            state = autostart.set_enabled(True)
    except OSError as error:
        return CommandResult(ok=False, message=f"安装版首次启动写入开机自启失败：{error}")
    if not state.enabled:
        return CommandResult(ok=False, message="安装版首次启动写入开机自启失败：回读未发现自启项")
    return _mark_autostart_initialized(config, "安装版已默认开启开机自启")


def _mark_autostart_initialized(config: dict[str, Any], message: str) -> CommandResult:
    """记录首次初始化标记; 标记写不进去时如实报错, 下次启动会重试."""
    merged = dict(config)
    merged[AUTOSTART_INITIALIZED_KEY] = True
    try:
        runtime.save_config(merged)
    except OSError as error:
        return CommandResult(ok=False, message=f"{message}；初始化标记写入失败：{error}")
    return CommandResult(ok=True, message=message)


def _read_autostart_quietly() -> tuple[autostart.AutostartState | None, str]:
    """失败路径的回读; 回读自身失败时只返回原因文本."""
    try:
        state = autostart.current_state()
    except OSError as error:
        return None, f"读取失败（{error}）"
    return state, state.detail


def _autostart_payload(state: autostart.AutostartState) -> dict[str, Any]:
    """命令返回 data: 只读字段, 界面原样呈现."""
    return {
        "enabled": state.enabled,
        "supported": state.supported,
        "target": state.target,
        "command": state.command,
        "portable": state.portable,
        "detail": state.detail,
    }


def _last_connection_text(service: Any) -> str:
    """按会话优先、任务次之的顺序给最近一次连接结果."""
    if service.sessions:
        session = next(reversed(service.sessions.values()))
        return f"已连接 · {session.transport_kind} 传输 · 画质 {session.quality_profile}"
    if service.tasks:
        task = next(reversed(service.tasks.values()))
        state = STATUS_TEXT.get(task.status, task.status)
        return f"任务{state} · 已完成 {task.completed}/{task.total}"
    return "暂无连接记录"
