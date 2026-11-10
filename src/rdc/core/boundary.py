# ==========================================================================================
# 文件        : boundary.py
# 所属目录    : src/rdc/core/
# 文件用途    : 统一异常捕获与错误映射, 对外入口只返回 CommandResult
# 架构定位    : 基础设施层
# 核心功能    : AppError 携带契约错误码与原样 message; 未预期异常带追踪号与堆栈落日志
# 职责边界    : 不改写用户可见文案; 不吞异常; 不把堆栈返回给界面
# ==========================================================================================

"""异常边界.

规则:
1. 禁止裸 except 与吞异常; 只有本模块允许捕获 Exception, 用于转换为统一返回.
2. 契约明确失败用 AppError, message 必须原样返回.
3. 未预期异常写堆栈日志并返回带追踪号的文案, 不暴露内部细节.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from rdc.core import logging as rdc_logging
from rdc.core.result import CommandResult

T = TypeVar("T")


class AppError(Exception):
    """可预期失败: 携带契约错误码与原样用户文案."""

    def __init__(self, message: str, error_code: str | None = None, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.data = data


def new_trace_id() -> str:
    """生成短追踪号, 供日志与用户反馈对齐."""
    return uuid.uuid4().hex[:12]


def guarded(operation: str, func: Callable[..., CommandResult], *args: Any, **kwargs: Any) -> CommandResult:
    """执行命令函数并统一转换异常.

    :param operation: 操作名, 用于日志 event
    :param func: 命令函数, 返回 CommandResult
    :return: 始终返回 CommandResult, 不向上抛异常
    """
    trace_id = new_trace_id()
    try:
        return func(*args, **kwargs)
    except AppError as error:
        rdc_logging.log_event(
            "core.boundary",
            "command.rejected",
            level="WARNING",
            error_code=error.error_code,
            trace_id=trace_id,
            operation=operation,
            reason=error.message,
        )
        return CommandResult(ok=False, message=error.message, error_code=error.error_code, data=error.data)
    except Exception as error:  # 边界处唯一允许的宽捕获, 见模块规则 1
        logger = rdc_logging.get_logger("core.boundary")
        logger.exception(
            "command.unhandled",
            extra={
                "rdc_fields": {
                    "trace_id": trace_id,
                    "operation": operation,
                    "error_code": None,
                    "exception_type": type(error).__name__,
                }
            },
        )
        return CommandResult(ok=False, message=f"内部错误，请提供追踪号 {trace_id}")
