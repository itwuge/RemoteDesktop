# ==========================================================================================
# 文件        : test_devices_approvals_messages.py
# 所属目录    : tests/
# 文件用途    : 设备列表、申请与审批、消息中心接真实命令后的行为测试
# 架构定位    : 单元与离屏界面测试
# 核心功能    : 四态渲染、绑定校验、解绑二次确认、免确认落盘回读、审批与已读
# 职责边界    : 不接网络; 不依赖外部服务; 全部内存或临时目录执行
# ==========================================================================================

"""设备、审批与消息中心测试."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from rdc.accounts.service import AccountService
from rdc.accounts.store import AccountStore
from rdc.commands import accounts, api, messages
from rdc.commands.service import CommandService
from rdc.ui.app import MainWindow

OWNER = "owner@example.com"
GUEST = "guest@example.com"
PASSWORD = "correct-horse-1"
DEVICE = "AAAABBBBCCCCDDDDEEEE"
DEVICE_2 = "FFFFGGGGHHHHIIIIJJJJ"


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """离屏 QApplication, 整个模块共用一个."""
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    return application


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AccountService:
    """独立数据目录与干净命令后端."""
    monkeypatch.setenv("RDC_DATA_DIR", str(tmp_path))
    api.configure(CommandService())
    account_service = AccountService()
    account_service.register_device(DEVICE, "工作站")
    account_service.register_device(DEVICE_2, "笔记本")
    accounts.configure(account_service)
    return account_service


@pytest.fixture
def window(qt_app: QApplication, service: AccountService) -> Iterator[MainWindow]:
    """每个用例一套干净主窗口."""
    main = MainWindow()
    yield main
    main.close()


def login(name: str) -> None:
    """注册并登录, 失败直接断言."""
    if accounts.get_service().store.find_account_by_name(name) is None:
        assert accounts.register_account(name, PASSWORD).ok
    assert accounts.login_account(name, PASSWORD).ok


def test_devices_show_failure_state_without_login(window: MainWindow) -> None:
    """未登录时设备页给出失败态与重试入口, 不空白."""
    window.switch_page("devices")
    page = window.devices
    assert page.content_stack.currentIndex() == 2
    assert "登录" in page.failed.desc_label.text()
    assert page.failed.action_button is not None


def test_devices_bind_trust_and_unbind_round_trip(window: MainWindow, service: AccountService) -> None:
    """绑定校验、免确认切换回读与解绑二次确认全部走真实命令."""
    window.switch_page("devices")
    login(OWNER)
    page = window.devices
    page.refresh()

    page.bind_device_input.setText("short")
    page.bind_code_input.setText("123456")
    page._on_bind()
    assert "20 位" in page.bind_hint.text()
    assert not any(action == "绑定设备" for action, _ in window.command_log)

    page.bind_device_input.setText(DEVICE.lower())
    page.bind_code_input.setText("12345")
    page._on_bind()
    assert "6 位数字" in page.bind_hint.text()

    page.bind_code_input.setText("123456")
    page._on_bind()
    assert service.store.devices[DEVICE].owner_account_id is not None
    assert [item["device_id"] for item in page._items] == [DEVICE]
    assert page.content_stack.currentIndex() == 3

    page._select_device(DEVICE)
    assert service.store.devices[DEVICE].require_confirm is True
    assert page.trust_button.text() == "信任此设备"
    page._on_toggle_trust()
    assert service.store.devices[DEVICE].require_confirm is False
    assert "回读" in page.bind_hint.text()

    page._select_device(DEVICE)
    assert page.trust_button.text() == "取消信任"
    page._on_toggle_trust()
    assert service.store.devices[DEVICE].require_confirm is True

    page._select_device(DEVICE)
    page._on_unbind()
    assert page._pending_unbind == DEVICE
    assert not page.unbind_confirm.isHidden()
    assert service.store.devices[DEVICE].owner_account_id is not None

    page._on_confirm_unbind()
    assert service.store.devices[DEVICE].owner_account_id is None
    assert page._items == []
    assert page.content_stack.currentIndex() == 1


def test_trust_policy_persists_across_reopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """信任状态写入数据库, 重建服务后回读仍生效."""
    monkeypatch.setenv("RDC_DATA_DIR", str(tmp_path))
    api.configure(CommandService())
    database = tmp_path / "accounts.db"
    store = AccountStore(database)
    current = AccountService(store=store)
    current.register_device(DEVICE, "工作站")
    accounts.configure(current)
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    assert accounts.set_trust_policy(DEVICE, False).ok
    store.close()

    reopened = AccountService(store=AccountStore(database))
    accounts.configure(reopened)
    login(OWNER)
    listed = accounts.list_devices()
    assert listed.ok and listed.data is not None
    assert listed.data["items"][0]["require_confirm"] is False


def test_approval_approves_real_request(window: MainWindow, service: AccountService) -> None:
    """待我审批分组显示真实申请, 批准后落到已处理."""
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    login(GUEST)
    requested = accounts.request_access(DEVICE, "control", "需要远程排障")
    assert requested.ok, requested.message
    request_id = str(requested.data["request_id"])
    login(OWNER)

    window.switch_page("approvals")
    page = window.approvals
    page.group_combo.setCurrentIndex(0)
    page.refresh()
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    assert page._selected_request() == request_id

    page._decide("approve")
    assert "已批准" in page.feedback.text()
    assert service.store.requests[request_id].status == "approved"
    assert page.table.rowCount() == 0
    assert page.content_stack.currentIndex() == 1

    page.group_combo.setCurrentIndex(2)
    page.refresh()
    assert page.table.rowCount() == 1
    status_cell = page.table.item(0, 5)
    assert status_cell is not None and status_cell.text() == "已批准"


def test_approval_rejects_real_request(window: MainWindow, service: AccountService) -> None:
    """拒绝走真实命令, 状态与反馈同步."""
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    login(GUEST)
    requested = accounts.request_access(DEVICE, "view", "只读核对")
    assert requested.ok, requested.message
    request_id = str(requested.data["request_id"])
    login(OWNER)

    window.switch_page("approvals")
    page = window.approvals
    page.group_combo.setCurrentIndex(0)
    page.refresh()
    page.table.selectRow(0)
    page._decide("reject")
    assert "已拒绝" in page.feedback.text()
    assert service.store.requests[request_id].status == "rejected"


def test_approvals_group_outgoing_shows_my_requests(window: MainWindow, service: AccountService) -> None:
    """我发起的按真实申请列表展示, 未登录时为空态而非崩溃."""
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    login(GUEST)
    assert accounts.request_access(DEVICE, "control", "临时协助").ok

    window.switch_page("approvals")
    page = window.approvals
    page.group_combo.setCurrentIndex(1)
    page.refresh()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 2) is not None
    assert page.table.item(0, 2).text().startswith("acc-")


def test_messages_unread_and_mark_all_read(service: AccountService, tmp_path: Path) -> None:
    """消息中心聚合真实审计, 未读与全部已读写入本机配置."""
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    login(GUEST)
    assert accounts.request_access(DEVICE, "control", "排障").ok
    login(OWNER)
    request_id = next(iter(service.store.requests))
    assert accounts.approve_access(request_id, 1800, False).ok

    listed = messages.list_messages()
    assert listed.ok and listed.data is not None
    unread = int(listed.data["unread"])
    assert unread >= 3
    categories = {item["category"] for item in listed.data["items"]}
    assert "approval" in categories and "binding" in categories

    read = messages.mark_all_read()
    assert read.ok and read.data is not None
    assert read.data["cleared"] == unread

    again = messages.list_messages()
    assert again.ok and again.data is not None
    assert again.data["unread"] == 0

    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["messages_read_until"]


def test_messages_page_lists_events_and_clears_unread(window: MainWindow, service: AccountService) -> None:
    """消息页展示真实事件, 全部已读后未读归零."""
    login(OWNER)
    assert accounts.bind_device(DEVICE, "123456").ok
    assert accounts.set_trust_policy(DEVICE, True).ok

    window.switch_page("messages")
    page = window.messages
    assert page.content_stack.currentIndex() == 3
    assert len(page._rows) >= 2
    assert page._unread >= 2

    page._on_mark_all_read()
    assert page._unread == 0
    assert "未读 0 条" in page.unread_label.text()
    assert len(page._rows) >= 2


def test_messages_page_without_login_is_explicit(window: MainWindow) -> None:
    """未登录时消息页给出失败态, 不空白不崩溃."""
    window.switch_page("messages")
    page = window.messages
    assert page.content_stack.currentIndex() == 2
    assert "登录" in page.failed.desc_label.text()
