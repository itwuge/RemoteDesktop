# ==========================================================================================
# 文件        : test_accounts_persistence.py
# 所属目录    : tests/
# 文件用途    : 账号与授权落盘的回归测试
# 架构定位    : 单元测试
# 核心功能    : 重启后可登录、令牌有效期内可用、重复注册拒绝、损坏与版本不匹配的处理
# 职责边界    : 不接网络; 不依赖外部服务; 数据库文件全部落在临时目录
# ==========================================================================================

"""账号落盘测试."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rdc.accounts.db import SCHEMA_VERSION, AccountStoreError
from rdc.accounts.service import AccountService
from rdc.accounts.store import AccountStore
from rdc.commands import accounts as account_commands
from rdc.core.errors import TOKEN_REVOKED

ACCOUNT = "persist@example.com"
PASSWORD = "correct-horse-1"
DEVICE = "AAAABBBBCCCCDDDDEEEE"


def open_service(path: Path) -> tuple[AccountService, AccountStore]:
    """按给定库文件构造服务; 返回服务与存储便于显式关闭."""
    store = AccountStore(path)
    return AccountService(store=store), store


def test_account_survives_reopen(tmp_path: Path) -> None:
    """注册后关闭存储再打开, 账号仍在且能登录."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    created = service.register_account(ACCOUNT, PASSWORD)
    assert created.ok, created.message
    account_id = created.data["account_id"]
    store.close()

    reopened, reopened_store = open_service(path)
    found = reopened.store.find_account_by_name(ACCOUNT)
    assert found is not None and found.account_id == account_id
    login = reopened.login_account(ACCOUNT, PASSWORD)
    assert login.ok, login.message
    assert login.data["account_id"] == account_id
    reopened_store.close()


def test_device_token_usable_across_reopen(tmp_path: Path) -> None:
    """设备令牌在有效期内跨进程重启仍可换会话令牌."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    assert service.register_account(ACCOUNT, PASSWORD).ok
    login = service.login_account(ACCOUNT, PASSWORD)
    assert login.ok, login.message
    device_token = login.data["device_token"]
    store.close()

    reopened, reopened_store = open_service(path)
    refreshed = reopened.refresh_session_token(device_token)
    assert refreshed.ok, refreshed.message
    assert refreshed.data["session_token"]
    reopened_store.close()


def test_revoked_token_still_rejected_after_reopen(tmp_path: Path) -> None:
    """吊销状态落盘, 重启后不会复活."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    assert service.register_account(ACCOUNT, PASSWORD).ok
    login = service.login_account(ACCOUNT, PASSWORD)
    assert login.ok, login.message
    service.store.revoke_account_tokens(login.data["account_id"])
    store.close()

    reopened, reopened_store = open_service(path)
    refreshed = reopened.refresh_session_token(login.data["device_token"])
    assert not refreshed.ok and refreshed.error_code == TOKEN_REVOKED
    reopened_store.close()


def test_duplicate_name_rejected_after_reopen(tmp_path: Path) -> None:
    """重启后重复注册同名账号仍被拒绝, 文案保持不变."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    assert service.register_account(ACCOUNT, PASSWORD).ok
    store.close()

    reopened, reopened_store = open_service(path)
    duplicate = reopened.register_account(ACCOUNT, PASSWORD)
    assert not duplicate.ok
    assert duplicate.message == "账号已存在, 请直接登录"
    reopened_store.close()


def test_bound_device_and_trust_policy_survive_reopen(tmp_path: Path) -> None:
    """绑定关系与免确认开关随账号一起落盘."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    service.register_device(DEVICE, "设备1")
    assert service.register_account(ACCOUNT, PASSWORD).ok
    login = service.login_account(ACCOUNT, PASSWORD)
    assert login.ok, login.message
    assert service.bind_device(DEVICE, "123456").ok
    assert service.set_trust_policy(DEVICE, True).ok
    store.close()

    reopened, reopened_store = open_service(path)
    device = reopened.store.devices.get(DEVICE)
    assert device is not None
    assert device.owner_account_id == login.data["account_id"]
    assert device.require_confirm is True
    reopened_store.close()


def test_password_not_written_in_plain_text(tmp_path: Path) -> None:
    """库文件不含口令明文, 只存盐与派生值."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    assert service.register_account(ACCOUNT, PASSWORD).ok
    store.close()

    assert PASSWORD.encode("utf-8") not in path.read_bytes()
    connection = sqlite3.connect(str(path))
    try:
        row = connection.execute("SELECT salt, password_hash FROM accounts").fetchone()
    finally:
        connection.close()
    assert row is not None
    assert len(row[0]) == 16
    assert len(row[1]) == 32


def test_wal_enabled(tmp_path: Path) -> None:
    """库文件启用 WAL 日志模式."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    store.close()
    connection = sqlite3.connect(str(path))
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert str(mode).lower() == "wal"
    assert path.is_file()


def test_corrupt_database_reports_clear_error(tmp_path: Path) -> None:
    """损坏文件给出明确错误而不是崩溃."""
    path = tmp_path / "accounts.db"
    path.write_bytes(b"this is not a sqlite database" * 64)
    with pytest.raises(AccountStoreError) as captured:
        AccountStore(path)
    assert str(path) in str(captured.value)


def test_version_mismatch_reports_clear_error(tmp_path: Path) -> None:
    """库文件版本高于当前程序时给出明确错误."""
    path = tmp_path / "accounts.db"
    store = AccountStore(path)
    store.close()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(AccountStoreError) as captured:
        AccountStore(path)
    assert "版本不兼容" in str(captured.value)


def test_closed_database_returns_failure_result(tmp_path: Path) -> None:
    """运行期库不可写时返回失败结果, 不抛异常."""
    path = tmp_path / "accounts.db"
    service, store = open_service(path)
    store.close()
    result = service.register_account(ACCOUNT, PASSWORD)
    assert not result.ok
    assert "账号数据暂不可用" in result.message


def test_command_layer_login_after_restart(tmp_path: Path) -> None:
    """命令层按落盘库装配后, 重启仍可登录."""
    path = tmp_path / "accounts.db"
    assert account_commands.configure_persistent_store(path) is None
    try:
        registered = account_commands.register_account(ACCOUNT, PASSWORD)
        assert registered.ok, registered.message
        account_commands.get_service().store.close()

        assert account_commands.configure_persistent_store(path) is None
        login = account_commands.login_account(ACCOUNT, PASSWORD)
        assert login.ok, login.message
    finally:
        account_commands.get_service().store.close()
        account_commands.configure(AccountService())
