# ==========================================================================================
# 文件        : dnd.py
# 所属目录    : src/rdc/files/
# 文件用途    : 拖拽触发传输的入口适配: 会话校验、路径归一、失败反馈
# 架构定位    : 业务层, 只依赖 service 与 protocol; 界面只调用本入口
# 核心功能    : 未连接时拒绝并给出明确文案; 多路径逐个开始传输; 汇总成功与失败
# 职责边界    : 不做分块/校验/限速（交给 service）; 不弹窗; 不直接碰界面控件
# ==========================================================================================

"""拖拽触发.

契约依据: docs/contracts/desktop-v1.md v1.4 3.5 节「拖拽传输」:
界面把本地文件路径交给 ``start_transfer``, 界面层不得做分块、校验或限速;
未建立会话时不得开始传输, 必须给出明确提示。

协议侧: protocol-v1 没有文件传输消息, 详见 ``rdc.files.protocol``; 本模块只在
本地暂存并返回 ``protocol_ready=false``, 缺口只记录不改契约。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rdc.core.errors import TRANSFER_IO_ERROR
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.files.protocol import wire_plan
from rdc.files.scope import scope_rejection
from rdc.files.service import FileTransferService, human_size

CONNECTED_STATE = "connected"
NO_SESSION_MESSAGE = "未建立会话, 无法开始传输, 请先连接远端"


def handle_drop(
    service: FileTransferService,
    *,
    session_id: str | None,
    session_state: str,
    paths: Iterable[str],
    destination_dir: str,
    rate_limit_kbps: int = 0,
    link_kbps: int = 0,
    scope: str = "control",
) -> CommandResult:
    """处理一次拖入: 校验会话与路径, 逐个开始传输并汇总反馈.

    :param service: 传输后端
    :param session_id: 当前会话标识, None 或空格表示未建立
    :param session_state: 会话状态, 仅 ``connected`` 允许传输
    :param paths: 界面从拖拽事件取得的本地路径
    :param destination_dir: 远端接收目录（当前实现为目标端本地暂存目录）
    :param rate_limit_kbps: 用户限速, 0 表示用满可用带宽
    :param link_kbps: 链路带宽, 0 表示不做低优先级收敛
    :param scope: 会话授权范围, view 一律拒绝
    :return: 汇总结果; ``data`` 含 started、failed、job_ids、protocol_ready 与缺口
    """
    if not session_id or session_state != CONNECTED_STATE:
        log_event("files.dnd", "dnd.rejected", level="WARNING", reason="no_session")
        return CommandResult(
            ok=False,
            message=NO_SESSION_MESSAGE,
            data={"protocol_ready": False, "started": [], "failed": [], "job_ids": []},
        )

    denied = scope_rejection(scope, "拖拽传输")
    if denied is not None:
        log_event("files.dnd", "dnd.rejected", level="WARNING", reason="scope", scope=scope)
        return CommandResult(
            ok=False,
            message=denied.message,
            error_code=denied.error_code,
            data={"protocol_ready": False, "started": [], "failed": [], "job_ids": []},
        )

    candidates, rejected = _normalize_paths(paths)
    if not candidates:
        message = "拖入内容不含可用的本地文件, 已忽略"
        if rejected:
            message = f"{message}（{len(rejected)} 项不是本地文件）"
        log_event("files.dnd", "dnd.empty", level="WARNING", rejected_count=len(rejected))
        return CommandResult(
            ok=False,
            message=message,
            error_code=TRANSFER_IO_ERROR if rejected else None,
            data={"protocol_ready": False, "started": [], "failed": rejected, "job_ids": []},
        )

    job_ids: list[str] = []
    started: list[str] = []
    failures: list[dict[str, Any]] = []
    total_bytes = 0
    error_code: str | None = None
    for path in candidates:
        result = service.start_transfer(
            path,
            destination_dir,
            rate_limit_kbps=rate_limit_kbps,
            link_kbps=link_kbps,
            scope=scope,
        )
        if result.ok and result.data is not None:
            job_ids.append(str(result.data["job_id"]))
            started.append(path)
            total_bytes += int(result.data.get("total_bytes", 0))
        else:
            failures.append({"path": path, "message": result.message, "error_code": result.error_code})
            if error_code is None:
                error_code = result.error_code
    failures.extend({"path": path, "message": "不是本地文件", "error_code": TRANSFER_IO_ERROR} for path in rejected)

    plan = wire_plan(len(started), total_bytes)
    log_event(
        "files.dnd",
        "dnd.handled",
        started_count=len(started),
        failed_count=len(failures),
        total_bytes=total_bytes,
        protocol_ready=plan["protocol_ready"],
    )
    data = {
        "protocol_ready": plan["protocol_ready"],
        "missing_messages": plan["missing_messages"],
        "started": started,
        "failed": failures,
        "job_ids": job_ids,
        "total_bytes": total_bytes,
    }
    if not started:
        first = failures[0]["message"] if failures else "拖入内容无法传输"
        return CommandResult(ok=False, message=str(first), error_code=error_code, data=data)
    if failures:
        return CommandResult(
            ok=True,
            message=f"已开始 {len(started)} 个传输, {len(failures)} 项被拒绝, 详情见列表",
            data=data,
        )
    return CommandResult(
        ok=True,
        message=f"已开始 {len(started)} 个传输, 共 {human_size(total_bytes)}",
        data=data,
    )


def _normalize_paths(paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """去空白、去重, 拆分为本地路径与非本地/不存在的项目."""
    candidates: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        if "://" in value:
            rejected.append(value)
            continue
        if not Path(value).exists():
            rejected.append(value)
            continue
        candidates.append(value)
    return candidates, rejected
