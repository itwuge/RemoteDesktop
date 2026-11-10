# ==========================================================================================
# 文件        : clipboard.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 剪贴板同步命令函数, 对应契约 v1.4 第 37 至 40 条
# 架构定位    : 命令层对外接口; 界面层只调本模块
# 核心功能    : 推送、拉取、开关、状态
# 职责边界    : 不读写系统剪贴板; 不打印内容; 不弹窗
# ==========================================================================================

"""剪贴板同步命令."""

from __future__ import annotations

from rdc.clipboard.sync import ClipboardSyncService
from rdc.core.result import CommandResult

_service = ClipboardSyncService()


def configure(service: ClipboardSyncService) -> None:
    """由装配处注入剪贴板后端."""
    global _service
    _service = service


def get_service() -> ClipboardSyncService:
    """返回当前剪贴板后端."""
    return _service


def set_clipboard_enabled(enabled: bool) -> CommandResult:
    """开启或关闭双向同步."""
    return _service.set_enabled(enabled)


def push_clipboard(origin: str, kind: str, content: str) -> CommandResult:
    """推送本端剪贴板; 文本为原文, 图片为 base64."""
    return _service.push(origin, kind, content)


def pull_clipboard(target: str) -> CommandResult:
    """拉取对端剪贴板内容."""
    return _service.pull(target)


def get_clipboard_status() -> CommandResult:
    """查看同步状态（不含内容明文）."""
    return _service.status()
