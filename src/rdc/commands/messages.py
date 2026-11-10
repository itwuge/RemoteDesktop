# ==========================================================================================
# 文件        : messages.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 消息中心的只读聚合、未读计数与全部已读命令
# 架构定位    : 命令层; 复用账号审计与运行时 config.json, 不新增存储
# 核心功能    : 汇总审批/绑定/会话/授权事件; 已读水位读写
# 职责边界    : 不改已冻结命令签名; 不伪造事件; 不直接读界面状态
# ==========================================================================================

"""消息中心命令.

契约缺口: desktop-v1 没有消息聚合、未读计数与「全部已读」命令, 也没有规定会话建立与结束的
审计事件类型与字段. 本模块只聚合既有 `get_audit_log` 返回的真实事件, 会话建立与结束由命令层
写入同一条审计时间线; 已读水位写入运行时 config.json 的 `messages_read_until` 键; 缺口登记在
契约变更记录第十节.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from rdc.accounts.store import _iso
from rdc.commands import accounts
from rdc.core import runtime
from rdc.core.errors import CONFIG_INVALID, CREDENTIAL_INVALID, TRANSFER_IO_ERROR
from rdc.core.logging import redact
from rdc.core.result import CommandResult

READ_CURSOR_KEY = "messages_read_until"
EXPORT_COLUMNS = ("时间", "类别", "事件", "标题", "设备", "设备码", "结果", "详情")
SESSION_EVENTS = frozenset({"session.start", "session.end"})
CATEGORY_LABELS = {
    "approval": "审批",
    "binding": "绑定",
    "session": "会话",
    "grant": "授权",
    "account": "账号",
    "system": "系统",
}
EVENT_META: dict[str, tuple[str, str]] = {
    "access.request": ("approval", "收到访问申请"),
    "access.approve": ("approval", "申请已批准"),
    "access.reject": ("approval", "申请已拒绝"),
    "device.bind": ("binding", "设备已绑定"),
    "device.unbind": ("binding", "设备已解绑"),
    "device.token_revoke": ("binding", "设备令牌已吊销"),
    "trust_policy.change": ("binding", "信任状态变更"),
    "access.passwordless": ("session", "免确认设备连接"),
    "session.start": ("session", "会话开始"),
    "session.end": ("session", "会话结束"),
    "session.kicked": ("session", "会话被踢出"),
    "grant.create": ("grant", "授权已签发"),
    "grant.revoke": ("grant", "授权已撤销"),
    "grant.transfer": ("grant", "授权已转授"),
    "account.register": ("account", "账号注册"),
    "account.login": ("account", "账号登录"),
    "account.logout": ("account", "账号登出"),
    "account.password_changed": ("account", "口令已修改"),
}


def list_messages(limit: int = 200) -> CommandResult:
    """汇总当前账号自有设备的审计事件, 附带未读计数; limit<=0 表示不截断."""
    devices = accounts.list_devices()
    if not devices.ok or devices.data is None:
        return devices
    account_id = accounts.get_service().session_account_id
    if not account_id:
        return CommandResult(ok=False, message="请先登录账号", error_code=CREDENTIAL_INVALID)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for device in devices.data.get("items", []):
        if not device.get("owner"):
            continue
        device_id = str(device.get("device_id", ""))
        display_name = str(device.get("display_name") or device_id)
        audit = accounts.get_audit_log(device_id)
        if not audit.ok or audit.data is None:
            continue
        for event in audit.data.get("items", []):
            _append(items, seen, _message(device_id, display_name, event))

    # 会话事件的被控端未必属于本账号, 按发起账号补入, 与绑定变化和免确认连接合成一条时间线
    for event in _session_events(account_id):
        peer_device_id = str(event.get("detail", {}).get("peer_device_id", ""))
        _append(items, seen, _message(peer_device_id, _device_name(peer_device_id), event))

    items.sort(key=lambda item: str(item["ts"]), reverse=True)
    cursor = _read_cursor(account_id)
    unread = sum(1 for item in items if not cursor or str(item["ts"]) > cursor)
    total = len(items)
    if limit > 0:
        items = items[:limit]
    return CommandResult(
        ok=True,
        message=f"共 {total} 条消息, 未读 {unread} 条",
        data={"items": items, "unread": unread, "total": total, "read_until": cursor},
    )


def mark_all_read() -> CommandResult:
    """把当前账号的已读水位推进到当前时刻; 只写本地 config.json."""
    listed = list_messages(limit=0)
    if not listed.ok or listed.data is None:
        return listed
    account_id = accounts.get_service().session_account_id
    if not account_id:
        return CommandResult(ok=False, message="请先登录账号", error_code=CREDENTIAL_INVALID)
    cursor = _iso(time.time())
    try:
        _write_cursor(account_id, cursor)
    except OSError as error:
        return CommandResult(ok=False, message=f"已读状态写入失败: {error}", error_code=CONFIG_INVALID)
    cleared = int(listed.data.get("unread", 0))
    return CommandResult(
        ok=True,
        message=f"已全部标记为已读, 清除 {cleared} 条未读",
        data={"read_until": cursor, "cleared": cleared},
    )


def export_messages(path: str, limit: int = 0) -> CommandResult:
    """把消息中心当前列表导出为 CSV; 只导出命令层已有字段, 不额外推断."""
    text = (path or "").strip()
    if not text:
        return CommandResult(ok=False, message="请先选择导出文件", error_code=CONFIG_INVALID)
    listed = list_messages(limit=limit)
    if not listed.ok or listed.data is None:
        return listed
    items = list(listed.data.get("items", []))
    target = Path(text).expanduser()
    try:
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(EXPORT_COLUMNS)
            for item in items:
                writer.writerow([_csv_cell(value) for value in _export_row(item)])
    except OSError as error:
        return CommandResult(ok=False, message=f"导出失败：{error}", error_code=TRANSFER_IO_ERROR)
    return CommandResult(
        ok=True,
        message=f"已导出 {len(items)} 条消息到 {target}",
        data={"path": str(target), "rows": len(items)},
    )


def _export_row(item: dict[str, Any]) -> tuple[str, ...]:
    """按固定列序取消息字段; detail 以 JSON 文本落一格, 不做二次翻译."""
    detail = item.get("detail")
    return (
        str(item.get("ts", "")),
        str(item.get("category_label", "")),
        str(item.get("event", "")),
        str(item.get("title", "")),
        str(item.get("device_name", "")),
        str(item.get("device_id", "")),
        str(item.get("result", "")),
        json.dumps(detail, ensure_ascii=False, sort_keys=True) if isinstance(detail, dict) else "",
    )


def _csv_cell(value: str) -> str:
    """表格软件会把 = + - @ 开头的单元格当公式执行, 统一加前导单引号."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _message(device_id: str, display_name: str, event: dict[str, Any]) -> dict[str, Any]:
    """把审计事件翻译成消息条目; 未知事件保留原始事件名, 不丢弃."""
    name = str(event.get("event", ""))
    category, title = EVENT_META.get(name, ("system", name or "未知事件"))
    detail = event.get("detail")
    ts = str(event.get("ts", ""))
    return {
        "message_id": f"{ts}|{name}|{device_id}|{event.get('actor', '')}",
        "ts": ts,
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, "系统"),
        "event": name,
        "title": title,
        "device_id": device_id,
        "device_name": display_name,
        "actor": str(event.get("actor", "")),
        "result": str(event.get("result", "")),
        "detail": redact(detail) if isinstance(detail, dict) else {},
    }


def _append(items: list[dict[str, Any]], seen: set[str], item: dict[str, Any]) -> None:
    """按 message_id 去重后追加, 避免同一事件既按设备又按账号被重复计入."""
    message_id = str(item["message_id"])
    if message_id in seen:
        return
    seen.add(message_id)
    items.append(item)


def _session_events(account_id: str) -> list[dict[str, Any]]:
    """按发起账号取会话建立与结束事件; 被控端不属于本账号时也能在消息中心看到."""
    store = accounts.get_service().store
    return [
        event.to_dict()
        for event in store.audit
        if event.event in SESSION_EVENTS and str(event.detail.get("account_id", "")) == account_id
    ]


def _device_name(device_id: str) -> str:
    """设备显示名; 设备未登记时退化为设备码."""
    device = accounts.get_service().store.devices.get(device_id)
    return device.display_name if device is not None else device_id


def _read_cursor(account_id: str) -> str:
    """读取该账号的已读水位; 配置缺失或形状不对时按从未读取处理."""
    cursors = runtime.load_config().get(READ_CURSOR_KEY)
    if not isinstance(cursors, dict):
        return ""
    value = cursors.get(account_id)
    return value if isinstance(value, str) else ""


def _write_cursor(account_id: str, cursor: str) -> None:
    """写入已读水位并保留其他配置键; 目录不可写时抛 OSError."""
    payload = runtime.load_config()
    cursors = payload.get(READ_CURSOR_KEY)
    merged = dict(cursors) if isinstance(cursors, dict) else {}
    merged[account_id] = cursor
    payload[READ_CURSOR_KEY] = merged
    runtime.save_config(payload)
