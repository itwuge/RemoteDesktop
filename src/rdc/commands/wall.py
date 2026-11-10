# ==========================================================================================
# 文件        : wall.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 屏幕墙命令函数, 对应契约 v1.3 第 28 至 31 条与 v1.5 第 45 至 47 条
# 架构定位    : 命令层; 预算计算委托 rdc.wall.budget, 审计与预览令牌委托 rdc.wall
# 核心功能    : 1/4/9/16 布局、聚焦升级与退回、审计读取、预览令牌; 4K60 同时最多 1 路
# 职责边界    : 不渲染; 不解码; 不接网络; 不绕过预算; 不写令牌明文
# ==========================================================================================

"""屏幕墙命令.

契约依据: docs/contracts/desktop-v1.md v1.3 第四节与 v1.5 §3.7.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdc.core.errors import PERMISSION_DENIED, PREVIEW_TOKEN_INVALID, WALL_BUDGET_EXCEEDED, WALL_NOT_FOUND
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.wall.audit import WALL_AUDIT_RETENTION_SECONDS, WallAuditLog, iso
from rdc.wall.budget import (
    DEFAULT_DECODE_BUDGET_UNITS,
    FOCUS_UNITS_4K60,
    MAX_CONCURRENT_4K60,
    PREVIEW_PROFILE,
    WALL_LAYOUTS,
    focus_cell,
    plan_wall,
)
from rdc.wall.preview import PREVIEW_SCOPE, PreviewTokenStore


@dataclass
class WallRecord:
    """一块屏幕墙的内存状态."""

    wall_id: str
    plan: dict[str, Any]
    budget_units: int = DEFAULT_DECODE_BUDGET_UNITS
    focused_cell: int | None = None


@dataclass
class WallService:
    """屏幕墙命令后端; 只维护布局、预算、审计与预览令牌, 不持有真实会话."""

    budget_units: int = DEFAULT_DECODE_BUDGET_UNITS
    account_id: str | None = None
    actor: str = "local"
    walls: dict[str, WallRecord] = field(default_factory=dict)
    archived: dict[str, float] = field(default_factory=dict)
    archived_devices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    audit: WallAuditLog = field(default_factory=WallAuditLog)
    preview_tokens: PreviewTokenStore = field(default_factory=PreviewTokenStore)
    audit_guard: Callable[[str], bool] | None = None

    def start_wall(self, layout: int) -> CommandResult:
        """创建屏幕墙; 布局非法或预算不足返回失败."""
        if layout not in WALL_LAYOUTS:
            return CommandResult(ok=False, message="布局只能为 1、4、9、16 路")
        try:
            plan = plan_wall(layout, self.budget_units)
        except ValueError as error:
            return CommandResult(ok=False, message=str(error), error_code=WALL_BUDGET_EXCEEDED)
        record = WallRecord(wall_id=f"wall-{uuid.uuid4()}", plan=plan, budget_units=self.budget_units)
        self.walls[record.wall_id] = record
        self._audit(record.wall_id, "wall.start", layout=layout, cells=len(plan["cells"]))
        self._emit_degrade(record, "start_wall")
        return CommandResult(
            ok=True,
            message=f"屏幕墙已创建, {len(plan['cells'])} 格",
            data=self._payload(record),
        )

    def set_wall_layout(self, wall_id: str, layout: int) -> CommandResult:
        """切换布局; 已聚焦格仍在新布局内时保持聚焦, 越界则取消聚焦."""
        record = self.walls.get(wall_id)
        if record is None:
            return CommandResult(ok=False, message="屏幕墙不存在", error_code=WALL_NOT_FOUND)
        if layout not in WALL_LAYOUTS:
            return CommandResult(ok=False, message="布局只能为 1、4、9、16 路")
        try:
            record.plan = plan_wall(layout, record.budget_units)
        except ValueError as error:
            return CommandResult(ok=False, message=str(error), error_code=WALL_BUDGET_EXCEEDED)
        if record.focused_cell is not None:
            if record.focused_cell >= len(record.plan["cells"]):
                record.focused_cell = None
            else:
                try:
                    focus_cell(record.plan, record.focused_cell, record.budget_units)
                except ValueError:
                    record.focused_cell = None
        self._audit(wall_id, "wall.layout", layout=layout, degraded=record.plan["degraded"])
        self._emit_degrade(record, "set_wall_layout")
        return CommandResult(ok=True, message="布局已切换", data=self._payload(record))

    def focus_wall_cell(self, wall_id: str, cell: int) -> CommandResult:
        """聚焦某格升高清; 全服务同一时刻只允许一格聚焦, 保证 4K60 并发不超过 1."""
        record = self.walls.get(wall_id)
        if record is None:
            return CommandResult(ok=False, message="屏幕墙不存在", error_code=WALL_NOT_FOUND)
        if cell < 0 or cell >= len(record.plan["cells"]):
            return CommandResult(ok=False, message="格号超出当前布局", error_code=WALL_BUDGET_EXCEEDED)
        if any(other.wall_id != wall_id and other.focused_cell is not None for other in self.walls.values()):
            return CommandResult(
                ok=False,
                message="已有一块屏幕墙处于聚焦档, 请先退回预览再聚焦",
                error_code=WALL_BUDGET_EXCEEDED,
            )
        try:
            focus_cell(record.plan, cell, record.budget_units)
        except ValueError as error:
            return CommandResult(ok=False, message=str(error), error_code=WALL_BUDGET_EXCEEDED)
        record.focused_cell = cell
        record.plan["concurrent_4k60"] = min(MAX_CONCURRENT_4K60, 1)
        self._audit(wall_id, "wall.focus", cell=cell, degraded=record.plan["degraded"])
        self._emit_degrade(record, "focus_wall_cell")
        return CommandResult(
            ok=True,
            message="已升级为高清会话, 其余未聚焦格按下标暂停" if record.plan["degraded"] else "已升级为高清会话",
            data=self._payload(record) | {"session_id": f"session-{wall_id}-{cell}", "quality_profile": "auto"},
        )

    def stop_wall_focus(self, wall_id: str) -> CommandResult:
        """退回全预览, 释放 4K60 名额 (契约第 45 条)."""
        record = self.walls.get(wall_id)
        if record is None:
            return CommandResult(ok=False, message="屏幕墙不存在", error_code=WALL_NOT_FOUND)
        previous = record.focused_cell
        record.plan = plan_wall(record.plan["layout"], record.budget_units)
        record.focused_cell = None
        self._audit(wall_id, "wall.focus.stop", cell=previous)
        self._emit_degrade(record, "stop_wall_focus")
        return CommandResult(
            ok=True,
            message="已退回全预览, 4K60 名额已释放",
            data=self._payload(record) | {"focused_cell": None, "quality_profile": PREVIEW_PROFILE["name"]},
        )

    def close_wall(self, wall_id: str) -> CommandResult:
        """关闭屏幕墙; 保留审计历史供 46 读取, 并吊销该墙预览令牌."""
        if wall_id not in self.walls:
            return CommandResult(ok=False, message="屏幕墙不存在", error_code=WALL_NOT_FOUND)
        devices = tuple(self.devices_of(wall_id))
        del self.walls[wall_id]
        self.archived[wall_id] = time.time()
        self.archived_devices[wall_id] = devices
        revoked = self.preview_tokens.revoke_for_wall(wall_id)
        self._audit(wall_id, "wall.close", revoked_preview_tokens=revoked)
        return CommandResult(ok=True, message="屏幕墙已关闭", data={"wall_id": wall_id, "status": "closed"})

    def devices_of(self, wall_id: str) -> list[str]:
        """返回墙上已绑定设备; 已关闭的墙取关闭时快照, 供权限判定."""
        record = self.walls.get(wall_id)
        if record is None:
            return list(self.archived_devices.get(wall_id, ()))
        return [str(cell["device_id"]) for cell in record.plan["cells"] if cell.get("device_id")]

    def get_wall_audit_log(self, wall_id: str, since: str = "") -> CommandResult:
        """读取屏幕墙审计; 已关闭但保留期内的墙仍可读 (契约第 46 条)."""
        if not self._audit_authorized(wall_id):
            return CommandResult(ok=False, message="无权查看该屏幕墙审计", error_code=PERMISSION_DENIED)
        if wall_id not in self.walls:
            closed_at = self.archived.get(wall_id)
            if closed_at is None or time.time() - closed_at > WALL_AUDIT_RETENTION_SECONDS:
                return CommandResult(ok=False, message="屏幕墙不存在或已超出审计保留期", error_code=WALL_NOT_FOUND)
        items = self.audit.query(wall_id, since)
        return CommandResult(ok=True, message=f"共 {len(items)} 条审计", data={"wall_id": wall_id, "items": items})

    def _audit_authorized(self, wall_id: str) -> bool:
        """审计读取身份判定: 显式守卫优先, 否则以当前登录账号对该墙全部设备做 full 判定."""
        if self.audit_guard is not None:
            return self.audit_guard(wall_id)
        devices = self.devices_of(wall_id)
        if not devices:
            return False
        from rdc.commands import accounts as accounts_commands

        service = accounts_commands.get_service()
        return all(service.can_manage_device(device_id) for device_id in devices)

    def issue_preview_token(self, wall_id: str, cell: int) -> CommandResult:
        """签发 60 秒预览令牌; 绑定墙、格、账号与设备 (契约第 47 条)."""
        record = self.walls.get(wall_id)
        if record is None:
            return CommandResult(ok=False, message="屏幕墙不存在", error_code=WALL_NOT_FOUND)
        if not self.account_id:
            return CommandResult(ok=False, message="预览令牌需登录账号", error_code=PERMISSION_DENIED)
        if cell < 0 or cell >= len(record.plan["cells"]):
            return CommandResult(ok=False, message="格号超出当前布局", error_code=WALL_BUDGET_EXCEEDED)
        device_id = record.plan["cells"][cell].get("device_id")
        if not device_id:
            return CommandResult(ok=False, message="该格未绑定设备, 不能签发预览令牌", error_code=PERMISSION_DENIED)
        token, token_record = self.preview_tokens.issue(wall_id, cell, self.account_id, device_id)
        self._audit(
            wall_id,
            "wall.preview_token.issue",
            cell=cell,
            account_id=self.account_id,
            device_id=device_id,
            expires_at=token_record.expires_at,
        )
        return CommandResult(
            ok=True,
            message="预览令牌已签发, 60 秒内有效",
            data={
                "token": token,
                "wall_id": wall_id,
                "cell": cell,
                "scope": PREVIEW_SCOPE,
                "expires_at": iso(token_record.expires_at),
                "single_use": False,
            },
        )

    def validate_preview_token(self, token: str, wall_id: str, cell: int, account_id: str) -> CommandResult:
        """预览流接入侧校验令牌; 失败返回 PREVIEW_TOKEN_INVALID 并落审计."""
        record, reason = self.preview_tokens.validate(token, wall_id, cell, account_id)
        wall = self.walls.get(wall_id)
        if record is not None:
            if wall is None:
                record, reason = None, "屏幕墙已关闭"
            elif cell < 0 or cell >= len(wall.plan["cells"]):
                record, reason = None, "格已不存在"
            elif wall.plan["cells"][cell].get("device_id") != record.device_id:
                record, reason = None, "格设备已变更"
        if record is None:
            self._audit(
                wall_id,
                "wall.preview_token.reject",
                result="deny",
                reason=reason,
                cell=cell,
                account_id=account_id,
            )
            return CommandResult(
                ok=False,
                message=f"预览令牌无效: {reason}, 请重新获取",
                error_code=PREVIEW_TOKEN_INVALID,
            )
        return CommandResult(
            ok=True,
            message="预览令牌有效",
            data={
                "wall_id": wall_id,
                "cell": cell,
                "account_id": account_id,
                "device_id": record.device_id,
                "scope": record.scope,
                "expires_at": iso(record.expires_at),
            },
        )

    def _audit(self, wall_id: str, event: str, result: str = "ok", **detail: Any) -> None:
        """追加屏幕墙审计; 事件集合由 WallAuditLog 校验."""
        self.audit.append(wall_id, event, self.actor, result, **detail)

    def _emit_degrade(self, record: WallRecord, action: str) -> None:
        """降级结果写入结构化日志与屏幕墙审计 wall.degrade; 未降级不写."""
        if not record.plan["degraded"]:
            return
        self._audit(
            record.wall_id,
            "wall.degrade",
            action=action,
            layout=record.plan["layout"],
            degrade_steps=list(record.plan["degrade_steps"]),
            units_used=record.plan["units_used"],
            units_budget=record.plan["units_budget"],
        )
        log_event(
            "wall",
            "wall.degrade",
            wall_id=record.wall_id,
            action=action,
            layout=record.plan["layout"],
            degrade_steps=list(record.plan["degrade_steps"]),
            units_used=record.plan["units_used"],
            units_budget=record.plan["units_budget"],
        )

    def _payload(self, record: WallRecord) -> dict[str, Any]:
        """统一返回体; 带硬约束字段便于界面与验收断言."""
        return {
            "wall_id": record.wall_id,
            "layout": record.plan["layout"],
            "cells": record.plan["cells"],
            "focused_cell": record.focused_cell,
            "degraded": record.plan["degraded"],
            "degrade_steps": record.plan["degrade_steps"],
            "units_used": record.plan["units_used"],
            "units_budget": record.plan["units_budget"],
            "concurrent_4k60": record.plan["concurrent_4k60"],
            "max_concurrent_4k60": MAX_CONCURRENT_4K60,
            "focus_units": FOCUS_UNITS_4K60,
        }


_service = WallService()


def configure(service: WallService) -> None:
    """由装配处注入屏幕墙后端."""
    global _service
    _service = service


def get_service() -> WallService:
    """返回当前屏幕墙后端."""
    return _service


def start_wall(layout: int) -> CommandResult:
    """创建屏幕墙."""
    return _service.start_wall(layout)


def set_wall_layout(wall_id: str, layout: int) -> CommandResult:
    """切换布局."""
    return _service.set_wall_layout(wall_id, layout)


def focus_wall_cell(wall_id: str, cell: int) -> CommandResult:
    """聚焦升级某格."""
    return _service.focus_wall_cell(wall_id, cell)


def close_wall(wall_id: str) -> CommandResult:
    """关闭屏幕墙."""
    return _service.close_wall(wall_id)


def stop_wall_focus(wall_id: str) -> CommandResult:
    """退回全预览 (契约第 45 条)."""
    return _service.stop_wall_focus(wall_id)


def get_wall_audit_log(wall_id: str, since: str = "") -> CommandResult:
    """读取屏幕墙审计 (契约第 46 条)."""
    return _service.get_wall_audit_log(wall_id, since)


def issue_preview_token(wall_id: str, cell: int) -> CommandResult:
    """签发预览令牌 (契约第 47 条)."""
    return _service.issue_preview_token(wall_id, cell)
