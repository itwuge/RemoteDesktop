# ==========================================================================================
# 文件        : messages_view.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 服务端消息列表的字段口径, 与桌面端 list_messages 保持一致
# 架构定位    : 账号中心内部视图层; 只把审计事件翻译成界面可直接消费的消息项
# 核心功能    : 事件分类与标题映射、消息项字段生成、已读水位比较
# 职责边界    : 不读本地配置; 不写存储; 不伪造事件
# ==========================================================================================

"""服务端消息视图.

字段口径与 `src/rdc/commands/messages.py` 的 `_message` 完全一致; 安卓取消息与桌面端
共用同一套事件名、分类与字段, 未读由服务端账号级水位计算, 不使用本地水位计数.
"""

from __future__ import annotations

from typing import Any

from rdc.core.logging import redact

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
    "device.bound": ("binding", "设备已绑定"),
    "device.unbind": ("binding", "设备已解绑"),
    "device.token_revoke": ("binding", "设备令牌已吊销"),
    "trust_policy.change": ("binding", "信任状态变更"),
    "trust.change": ("binding", "信任状态变更"),
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


def build_message(device_id: str, display_name: str, event: dict[str, Any]) -> dict[str, Any]:
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
