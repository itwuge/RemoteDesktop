# ==========================================================================================
# 文件        : test_session_audit.py
# 所属目录    : tests/
# 文件用途    : 会话建立与结束审计测试
# 架构定位    : 命令层与账号审计的集成测试
# 核心功能    : 事件字段完整、失败原因、去重、脱敏与消息中心时间线
# 职责边界    : 不接网络; 全部内存或临时目录执行
# ==========================================================================================

"""会话审计测试: 建立与结束各一条、字段完整、脱敏、重连不重复记账."""

from __future__ import annotations

from pathlib import Path

import pytest

from rdc.accounts.service import AccountService
from rdc.commands import accounts, api, messages
from rdc.commands.service import CommandService

ACCOUNT = "session@example.com"
PASSWORD = "correct-horse-1"
PEER = "AAAABBBBCCCCDDDDEEEE"


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CommandService:
    """独立数据目录、已登录账号与一个对端设备."""
    monkeypatch.setenv("RDC_DATA_DIR", str(tmp_path))
    instance = CommandService()
    instance.register_peer(PEER, "被控工作站")
    api.configure(instance)
    account_service = AccountService()
    account_service.register_device(PEER, "被控工作站")
    accounts.configure(account_service)
    assert accounts.register_account(ACCOUNT, PASSWORD).ok
    assert accounts.login_account(ACCOUNT, PASSWORD).ok
    return instance


def _events(name: str) -> list:
    """取当前账号库中指定事件名的审计记录."""
    return [item for item in accounts.get_service().store.audit if item.event == name]


def test_session_start_records_full_fields(service: CommandService) -> None:
    """会话建立产生一条审计, 含发起设备、被控端、传输方式与画质档."""
    session = service.add_session(PEER, transport_kind="relay", quality_profile="720p30")

    events = _events("session.start")
    assert len(events) == 1
    event = events[0]
    assert event.target == PEER
    assert event.actor == accounts.get_service().session_account_id
    assert event.result == "ok"
    assert event.ts
    detail = event.detail
    assert detail["session_id"] == session.session_id
    assert detail["from_device_id"] == service.identity.device_id
    assert detail["peer_device_id"] == PEER
    assert detail["device_id"] == PEER
    assert detail["transport_kind"] == "relay"
    assert detail["quality_profile"] == "720p30"
    assert detail["account_id"] == accounts.get_service().session_account_id
    assert detail["established"] is True


def test_session_end_records_normal_reason(service: CommandService) -> None:
    """正常断开产生一条结束审计, 字段与建立事件一致."""
    session = service.add_session(PEER, transport_kind="direct", quality_profile="auto")
    assert service.stop_session(session.session_id).ok

    events = _events("session.end")
    assert len(events) == 1
    detail = events[0].detail
    assert detail["session_id"] == session.session_id
    assert detail["peer_device_id"] == PEER
    assert detail["transport_kind"] == "direct"
    assert detail["quality_profile"] == session.quality_profile
    assert detail["reason"] == "normal"
    assert detail["established"] is True
    assert events[0].result == "ok"

    # 已关闭的会话不再记账
    assert service.stop_session(session.session_id).ok is False
    assert len(_events("session.end")) == 1


def test_canceled_connection_records_end_once(service: CommandService) -> None:
    """连接任务取消写一条结束审计, 重复取消不再记账."""
    started = service.start_connection(PEER, "123456")
    assert started.ok and started.data is not None
    task_id = str(started.data["task_id"])

    assert service.cancel_connection(task_id).ok
    events = _events("session.end")
    assert len(events) == 1
    assert events[0].detail["reason"] == "canceled"
    assert events[0].detail["established"] is False
    assert events[0].detail["attempt_id"] == task_id

    assert service.cancel_connection(task_id).ok is False
    assert len(_events("session.end")) == 1


def test_failed_connection_records_timeout_reason(service: CommandService) -> None:
    """连接超时写一条失败审计, 与成功会话不重复."""

    def timeout_connector(device_id: str, access_code: str, progress: object) -> object:
        raise TimeoutError("建连超时")

    started = service.start_connection(PEER, "123456")
    assert started.ok and started.data is not None
    task = service.tasks[str(started.data["task_id"])]
    service.connector = timeout_connector
    service._execute_connection(task, PEER, "123456")

    events = _events("session.end")
    assert len(events) == 1
    assert events[0].result == "fail"
    assert events[0].detail["reason"] == "timeout"
    assert events[0].detail["session_id"] == task.session_id
    assert _events("session.start") == []


def test_reconnect_records_one_start_per_session(service: CommandService) -> None:
    """断线重连产生新的会话对, 同一会话重复登记不重复记账."""
    first = service.add_session(PEER)
    service.add_session(PEER, session_id=first.session_id)
    assert len(_events("session.start")) == 1

    service.stop_session(first.session_id)
    second = service.add_session(PEER)
    service.stop_session(second.session_id)
    started = {item.detail["session_id"] for item in _events("session.start")}
    ended = {item.detail["session_id"] for item in _events("session.end")}
    assert started == {first.session_id, second.session_id}
    assert ended == started
    assert len(_events("session.end")) == 2


def test_audit_never_contains_sensitive_values(service: CommandService) -> None:
    """连接码、令牌与口令一律不落审计; 统一脱敏规则覆盖新增字段."""
    session = service.add_session(PEER)
    service.stop_session(session.session_id)
    service.start_connection(PEER, "123456")

    details = [item.detail for item in accounts.get_service().store.audit]
    blob = str(details)
    assert "123456" not in blob
    assert "correct-horse-1" not in blob
    assert all("access_code" not in detail for detail in details)

    store = accounts.get_service().store
    store.append_audit(
        "session.start",
        "acc-test",
        PEER,
        "ok",
        session_id="session-test",
        access_code="654321",
        token="rdc_plain_token_value",
        password="correct-horse-1",
    )
    sanitized = store.audit[-1].detail
    assert sanitized["access_code"] == "***"
    assert sanitized["token"] == "***"
    assert sanitized["password"] == "***"
    assert sanitized["session_id"] == "session-test"


def test_session_events_join_message_timeline(service: CommandService) -> None:
    """会话事件进入消息中心同一时间线, 未读口径与全部已读一致."""
    session = service.add_session(PEER, transport_kind="relay", quality_profile="720p30")
    service.stop_session(session.session_id)

    listed = messages.list_messages()
    assert listed.ok and listed.data is not None
    items = listed.data["items"]
    names = {item["event"] for item in items}
    assert {"session.start", "session.end"} <= names
    for item in items:
        assert item["category"] == "session"
        assert item["device_id"] == PEER
        assert item["device_name"] == "被控工作站"
        assert item["actor"] == accounts.get_service().session_account_id
    assert listed.data["unread"] == listed.data["total"] >= 2

    read = messages.mark_all_read()
    assert read.ok and read.data is not None
    assert read.data["cleared"] == listed.data["unread"]
    again = messages.list_messages()
    assert again.ok and again.data is not None
    assert again.data["unread"] == 0
