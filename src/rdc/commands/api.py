# ==========================================================================================
# 文件        : api.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 13 个命令函数的唯一调用面, 签名与 desktop-v1 第三节逐条一致
# 架构定位    : 命令层对外接口, 界面层只调用本模块
# 核心功能    : 转发到注入的 CommandService; 提供 configure 供装配处替换实现
# 职责边界    : 不写业务判断; 不弹窗; 不打印; 不构造网络与采集器
# ==========================================================================================

"""13 个命令函数.

契约依据: docs/contracts/desktop-v1.md 第三节. 签名不得改动.
界面层只导入本模块, 不直接导入 service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rdc.commands.service import CommandService
from rdc.core.result import CommandResult
from rdc.crypto.identity_store import load_or_create_identity

# 应用默认使用真实连接策略与持久设备身份; 单测会通过 configure() 换成 stub 实例
_service = CommandService(connect_mode="real", identity=load_or_create_identity())


def configure(service: CommandService) -> None:
    """由装配处注入命令后端."""
    global _service
    _service = service


def get_service() -> CommandService:
    """返回当前命令后端, 供界面轮询与测试断言使用."""
    return _service


def start_connection(device_id: str, access_code: str) -> CommandResult:
    """发起连接, 立即返回任务句柄."""
    return _service.start_connection(device_id, access_code)


def get_connection_progress(task_id: str) -> CommandResult:
    """查询连接任务进度."""
    return _service.get_connection_progress(task_id)


def cancel_connection(task_id: str) -> CommandResult:
    """取消连接任务."""
    return _service.cancel_connection(task_id)


def stop_session(session_id: str) -> CommandResult:
    """停止已建立的会话."""
    return _service.stop_session(session_id)


def get_session_status(session_id: str) -> CommandResult:
    """查询会话状态."""
    return _service.get_session_status(session_id)


def list_sessions() -> CommandResult:
    """列出当前会话摘要."""
    return _service.list_sessions()


def apply_quality_profile(session_id: str, profile: str) -> CommandResult:
    """切换画质档位."""
    return _service.apply_quality_profile(session_id, profile)


def get_session_metrics(session_id: str) -> CommandResult:
    """返回会话指标快照."""
    return _service.get_session_metrics(session_id)


def start_hosting(display_name: str) -> CommandResult:
    """启动被控并生成一次性口令."""
    return _service.start_hosting(display_name)


def stop_hosting() -> CommandResult:
    """停止被控."""
    return _service.stop_hosting()


def refresh_access_code() -> CommandResult:
    """刷新一次性口令."""
    return _service.refresh_access_code()


def open_settings() -> CommandResult:
    """打开设置页."""
    return _service.open_settings()


def apply_relay_policy(endpoint: str, egress_mbps: int, degrade_profile: str) -> CommandResult:
    """应用中继策略."""
    return _service.apply_relay_policy(endpoint, egress_mbps, degrade_profile)


# ------------------------------------------------------------------------------------------
# 以下三个函数不是契约命令, 只解决"界面如何拿到已解码画面与如何上行输入"这一缺口:
# 契约第三节没有取帧与发送输入的入口, 按约束只在此登记缺口, 不改契约正文.
# 界面仍只收集参数、调用命令层、呈现结果; 解码在 rdc.media.h264 内完成, 不进界面.
# ------------------------------------------------------------------------------------------
def watch_remote_frames(session_id: str, sink: Callable[[Any], None]) -> bool:
    """登记远端画面回调; 回调参数为 rdc.media.h264.DecodedFrame."""
    return _service.watch_remote_frames(session_id, sink)


def send_remote_input(session_id: str, message: dict[str, Any]) -> bool:
    """把一条键鼠消息交给真实会话上行."""
    return _service.send_remote_input(session_id, message)


def get_remote_stats(session_id: str) -> dict[str, Any]:
    """返回真实会话的连接与帧统计."""
    return _service.get_remote_stats(session_id)


def request_remote_keyframe(session_id: str) -> CommandResult:
    """请求被控端立即产生关键帧; 不是契约命令, 供会话页右键菜单使用."""
    return _service.request_remote_keyframe(session_id)


def get_hosting_info() -> dict[str, Any]:
    """返回本机被控信息快照; 不是契约命令, 供会话页复制连接信息."""
    return _service.get_hosting_info()
