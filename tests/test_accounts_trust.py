# ==========================================================================================
# 文件        : test_accounts_trust.py
# 所属目录    : tests/
# 文件用途    : 设备信任默认值与免确认连接审计的回归测试
# 架构定位    : 单元测试
# 核心功能    : 默认需确认、显式信任免确认、撤销信任、旧库迁移、免确认连接进消息中心
# 职责边界    : 不接网络; 不依赖外部服务; 落盘文件全部在临时目录
# ==========================================================================================

"""设备信任与免确认审计测试."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rdc.accounts.db import SCHEMA_VERSION
from rdc.accounts.service import AccountService
from rdc.accounts.store import AccountStore, DeviceRecord
from rdc.commands import accounts, messages

ACCOUNT = "trust@example.com"
PASSWORD = "correct-horse-1"
DEVICE = "AAAABBBBCCCCDDDDEEEE"


def make_service() -> AccountService:
    """内存后端 + 已登记的一台设备."""
    service = AccountService()
    service.register_device(DEVICE, "工作站")
    return service


def login_and_bind(service: AccountService) -> str:
    """注册并登录后绑定设备, 返回账号 ID."""
    assert service.register_account(ACCOUNT, PASSWORD).ok
    data = service.login_account(ACCOUNT, PASSWORD).data
    assert data is not None
    assert service.bind_device(DEVICE, "123456").ok
    return str(data["account_id"])


def test_device_record_defaults_to_confirm() -> None:
    """设备记录默认需要逐次确认."""
    assert DeviceRecord(device_id=DEVICE, display_name="工作站").require_confirm is True


def test_new_device_requires_confirm_by_default() -> None:
    """新绑定设备默认 route=approval, 不再免确认直连."""
    service = make_service()
    login_and_bind(service)
    device = service.store.devices[DEVICE]
    assert device.require_confirm is True
    route = service.resolve_access(DEVICE)
    assert route["route"] == "approval"


def test_explicit_trust_allows_passwordless_and_audits() -> None:
    """显式信任后可免确认, 且产生一条含账号、设备与时间的审计."""
    service = make_service()
    account_id = login_and_bind(service)
    assert service.set_trust_policy(DEVICE, False).ok

    route = service.resolve_access(DEVICE)
    assert route["route"] == "passwordless"

    events = [item for item in service.store.audit if item.event == "access.passwordless"]
    assert len(events) == 1
    event = events[0]
    assert event.actor == account_id
    assert event.target == DEVICE
    assert event.ts
    assert event.detail["device_name"] == "工作站"


def test_revoke_trust_returns_to_confirm() -> None:
    """撤销信任后立即回到需确认, 且不再新增免确认审计."""
    service = make_service()
    login_and_bind(service)
    assert service.set_trust_policy(DEVICE, False).ok
    assert service.resolve_access(DEVICE)["route"] == "passwordless"

    assert service.set_trust_policy(DEVICE, True).ok
    assert service.resolve_access(DEVICE)["route"] == "approval"
    assert len([item for item in service.store.audit if item.event == "access.passwordless"]) == 1


def test_rebind_resets_trust() -> None:
    """解绑后重新绑定回到需确认, 信任不跨绑定保留."""
    service = make_service()
    login_and_bind(service)
    assert service.set_trust_policy(DEVICE, False).ok
    assert service.unbind_device(DEVICE).ok
    assert service.bind_device(DEVICE, "123456").ok
    assert service.store.devices[DEVICE].require_confirm is True
    assert service.resolve_access(DEVICE)["route"] == "approval"


def test_new_database_column_defaults_to_confirm(tmp_path: Path) -> None:
    """库结构默认值为 1, 绕过代码直接插入的设备也需确认."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    store.close()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("INSERT INTO devices (device_id, display_name) VALUES ('raw-device', '直插设备')")
        connection.commit()
        value = connection.execute("SELECT require_confirm FROM devices WHERE device_id = 'raw-device'").fetchone()[0]
    finally:
        connection.close()
    assert int(value) == 1


def test_legacy_v1_database_migrates_and_tightens(tmp_path: Path) -> None:
    """旧库按 user_version 升级: 数据不丢, 免确认统一收紧为需确认."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    service = AccountService(store=store)
    service.register_device(DEVICE, "工作站")
    account_id = login_and_bind(service)
    assert service.set_trust_policy(DEVICE, False).ok
    audit_before = len(store.audit)
    store.close()

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA user_version=1")
        connection.commit()
    finally:
        connection.close()

    reopened_store = AccountStore(path)
    reopened = AccountService(store=reopened_store)
    try:
        assert reopened_store.devices[DEVICE].require_confirm is True
        assert account_id in reopened_store.accounts
        assert len(reopened_store.audit) == audit_before
        assert reopened.login_account(ACCOUNT, PASSWORD).ok
        assert reopened.resolve_access(DEVICE)["route"] == "approval"
    finally:
        reopened_store.close()

    connection = sqlite3.connect(str(path))
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    assert int(version) == SCHEMA_VERSION


def test_passwordless_connection_visible_in_message_center(tmp_path: Path) -> None:
    """免确认连接写入审计并可跨重启在消息中心看到."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    service = AccountService(store=store)
    service.register_device(DEVICE, "工作站")
    accounts.configure(service)
    try:
        account_id = login_and_bind(service)
        assert service.set_trust_policy(DEVICE, False).ok
        assert service.resolve_access(DEVICE)["route"] == "passwordless"
        store.close()

        reopened_store = AccountStore(path)
        reopened = AccountService(store=reopened_store)
        accounts.configure(reopened)
        try:
            assert reopened.login_account(ACCOUNT, PASSWORD).ok
            listed = messages.list_messages()
            assert listed.ok and listed.data is not None
            hits = [item for item in listed.data["items"] if item["event"] == "access.passwordless"]
            assert len(hits) == 1
            hit = hits[0]
            assert hit["actor"] == account_id
            assert hit["device_id"] == DEVICE
            assert hit["title"] == "免确认设备连接"
            assert hit["category"] == "session"
            assert hit["ts"]
        finally:
            reopened_store.close()
    finally:
        accounts.configure(AccountService())
