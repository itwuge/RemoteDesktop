# ==========================================================================================
# 文件        : ids.py
# 所属目录    : src/rdc/core/
# 文件用途    : 标识与时间工具, 供命令层与协议层共用
# 架构定位    : 基础设施层
# 核心功能    : 生成 UUIDv4; 生成带时区 ISO 8601 时间; 校验时间窗口
# 职责边界    : 不做签名; 不做业务校验
# ==========================================================================================

"""标识与时间工具."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta


def new_message_id() -> str:
    """生成 UUIDv4 字符串, 用作消息唯一标识."""
    return str(uuid.uuid4())


def new_task_id() -> str:
    """生成任务标识, 与消息标识区分命名."""
    return f"task-{uuid.uuid4()}"


def new_session_id() -> str:
    """生成会话标识."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """返回本地时区的 ISO 8601 时间, 精确到秒, 带偏移."""
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def iso_after(seconds: int) -> str:
    """返回当前时间之后若干秒的 ISO 8601 时间."""
    moment = datetime.now(UTC).astimezone() + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


def is_within_window(timestamp: str, window_seconds: int = 300) -> bool:
    """判断时间戳是否落在允许窗口内.

    :param timestamp: ISO 8601 字符串
    :param window_seconds: 允许的前后偏差秒数
    :return: 在窗口内返回 True, 解析失败返回 False
    """
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    delta = abs((datetime.now(UTC) - moment.astimezone(UTC)).total_seconds())
    return delta <= window_seconds
