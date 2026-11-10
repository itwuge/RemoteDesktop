# ==========================================================================================
# 文件        : audit.py
# 所属目录    : src/rdc/wall/
# 文件用途    : 屏幕墙审计事件存储, 对应契约 v1.5 第八节
# 架构定位    : 业务层内部存储, 只被屏幕墙命令使用
# 核心功能    : 8 类固定事件、关闭后 30 天保留、单次最多返回最近 1000 条
# 职责边界    : 不生成用户文案; 不打印; 不记录预览令牌与设备令牌明文
# ==========================================================================================

"""屏幕墙审计.

契约依据: docs/contracts/desktop-v1.md v1.5 §3.7. 关闭屏幕墙不删除历史, 保留期内可读.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

WALL_AUDIT_RETENTION_SECONDS = 30 * 24 * 3600
WALL_AUDIT_MAX_ITEMS = 1000
WALL_AUDIT_EVENTS = frozenset(
    {
        "wall.start",
        "wall.layout",
        "wall.focus",
        "wall.focus.stop",
        "wall.close",
        "wall.degrade",
        "wall.preview_token.issue",
        "wall.preview_token.reject",
    }
)


def iso(epoch: float) -> str:
    """把 epoch 秒转为带时区的 ISO 8601 字符串."""
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class WallAuditEvent:
    """屏幕墙审计事件; 六字段与契约 46 返回项一致."""

    ts: str
    event: str
    actor: str
    target: str
    result: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约规定的六字段."""
        return {
            "ts": self.ts,
            "event": self.event,
            "actor": self.actor,
            "target": self.target,
            "result": self.result,
            "detail": dict(self.detail),
        }


class WallAuditLog:
    """屏幕墙审计内存实现; 接入持久层时保持字段与保留策略不变."""

    def __init__(self) -> None:
        self.items: list[WallAuditEvent] = []

    def append(self, wall_id: str, event: str, actor: str, result: str = "ok", **detail: Any) -> None:
        """追加审计; 只接受契约固定事件集合."""
        if event not in WALL_AUDIT_EVENTS:
            raise ValueError("非契约屏幕墙审计事件")
        self.items.append(
            WallAuditEvent(
                ts=iso(time.time()),
                event=event,
                actor=actor,
                target=wall_id,
                result=result,
                detail=detail,
            )
        )

    def query(self, wall_id: str, since: str = "") -> list[dict[str, Any]]:
        """按墙读取审计; since 为空表示全部, 单次最多最近 1000 条, 时间升序."""
        matched = [item for item in self.items if item.target == wall_id]
        if since:
            matched = [item for item in matched if item.ts >= since]
        return [item.to_dict() for item in matched[-WALL_AUDIT_MAX_ITEMS:]]
