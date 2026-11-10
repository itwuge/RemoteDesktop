# ==========================================================================================
# 文件        : update.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 客户端更新命令函数, 对应契约变更记录-契约-202611-010
# 架构定位    : 命令层; 只转发到注入的 UpdateService, 统一返回 CommandResult
# 核心功能    : 检查更新、开始下载、查询进度、取消下载
# 职责边界    : 不弹窗; 不执行下载的安装包; 不改写 message
# ==========================================================================================

"""客户端更新命令.

契约依据: 契约变更记录-契约-202611-010. 新增命令入表前以该记录为准.
"""

from __future__ import annotations

from rdc.core.result import CommandResult
from rdc.update.service import UpdateService

_service: UpdateService | None = None


def configure(service: UpdateService) -> None:
    """由装配处注入更新后端."""
    global _service
    _service = service


def get_service() -> UpdateService:
    """返回当前更新后端; 首次调用时才读取环境变量, 保证运行时装配先生效."""
    global _service
    if _service is None:
        _service = UpdateService()
    return _service


def refresh_update_status() -> CommandResult:
    """读取更新源并返回当前版本与最新版本对比."""
    return get_service().refresh_update_status()


def start_update_download() -> CommandResult:
    """开始下载已检查到的更新包, 立即返回任务句柄."""
    return get_service().start_update_download()


def get_update_progress(task_id: str) -> CommandResult:
    """查询更新包下载进度与校验结果."""
    return get_service().get_update_progress(task_id)


def cancel_update_download(task_id: str) -> CommandResult:
    """取消更新包下载."""
    return get_service().cancel_update_download(task_id)
