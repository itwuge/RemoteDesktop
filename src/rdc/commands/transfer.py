# ==========================================================================================
# 文件        : transfer.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 文件传输命令函数, 对应契约 v1.4 第 32 至 36 条与 v1.6 第 48、49 条
# 架构定位    : 命令层对外接口; 界面层只调本模块; 实现委托 rdc.files
# 核心功能    : 开始、查询进度、暂停、继续、取消、绑定授权范围、进程重启后恢复
# 职责边界    : 不写业务判断; 不弹窗; 不打印; 不直接碰界面控件; 不绕过 scope 门禁
# ==========================================================================================

"""文件传输命令.

契约依据: docs/contracts/desktop-v1.md v1.4 第 32 至 36 条、v1.6 第 48 与 49 条。
实现只绑 ``rdc.files``, 不再保留旧的 ``rdc.transfer`` 双轨。
"""

from __future__ import annotations

from rdc.core.result import CommandResult
from rdc.files.service import FileTransferService, default_destination_root

_service = FileTransferService(allow_local_staging=False)


def configure(service: FileTransferService, scope: str = "control") -> None:
    """由装配处注入传输后端, 并把当前会话的授权范围写入服务门禁."""
    global _service
    _service = service
    service.set_scope(scope)


def get_service() -> FileTransferService:
    """返回当前传输后端, 供测试与界面轮询断言."""
    return _service


def default_destination() -> str:
    """返回默认接收目录."""
    return str(default_destination_root())


def get_transfer_channel_status() -> CommandResult:
    """查询文件传输当前使用的会话通道; 降级通道会明确标注对中继可见 (契约第 50 条)."""
    return _service.channel_status()


def set_transfer_scope(scope: str) -> CommandResult:
    """绑定当前会话的授权范围 (契约第 49 条); 取值非法返回 SCOPE_INVALID."""
    return _service.set_scope(scope)


def recover_transfers(destination_dir: str = "") -> CommandResult:
    """扫描断点日志, 把进程重启前中断的任务恢复为可续传状态 (契约第 48 条)."""
    target = destination_dir or default_destination()
    results = _service.recover_pending(target)
    if len(results) == 1 and not results[0].ok:
        return results[0]
    items = [result.data for result in results if result.ok and result.data]
    if not items:
        return CommandResult(
            ok=True,
            message="没有待恢复的中断任务",
            data={"destination": target, "recovered": 0, "items": []},
        )
    return CommandResult(
        ok=True,
        message=f"已恢复 {len(items)} 个中断任务, 可逐个继续传输",
        data={"destination": target, "recovered": len(items), "items": items},
    )


def start_transfer(
    source_path: str,
    destination_dir: str = "",
    rate_limit_kbps: int = 0,
    link_kbps: int = 0,
) -> CommandResult:
    """开始传输文件或目录; 目标目录为空时使用默认接收目录."""
    return _service.start_transfer(
        source_path,
        destination_dir or default_destination(),
        rate_limit_kbps=rate_limit_kbps,
        link_kbps=link_kbps,
    )


def get_transfer_progress(job_id: str) -> CommandResult:
    """查询传输进度并推进内存实现."""
    return _service.get_transfer_progress(job_id)


def pause_transfer(job_id: str) -> CommandResult:
    """暂停传输."""
    return _service.pause_transfer(job_id)


def resume_transfer(job_id: str) -> CommandResult:
    """续传."""
    return _service.resume_transfer(job_id)


def cancel_transfer(job_id: str) -> CommandResult:
    """取消传输."""
    return _service.cancel_transfer(job_id)
