"""桌面界面离屏冒烟: 每页可构造、可切换、无后端显示未连接、关键交互不崩."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from rdc.accounts.service import AccountService
from rdc.clipboard.sync import ClipboardSyncService
from rdc.commands import accounts, api, clipboard, transfer, wall
from rdc.commands.service import CommandService
from rdc.files import FileTransferService
from rdc.ui.app import MainWindow
from rdc.ui.theme import stylesheet


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """离屏 QApplication, 整个模块共用一个."""
    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    return application


@pytest.fixture
def window(qt_app: QApplication) -> MainWindow:
    """每个用例一套干净的命令后端."""
    api.configure(CommandService())
    accounts.configure(AccountService())
    clipboard.configure(ClipboardSyncService())
    transfer.configure(FileTransferService())
    wall.configure(wall.WallService())
    main = MainWindow()
    yield main
    main.close()


def test_every_page_constructs_and_switches(window: MainWindow) -> None:
    """七个页面逐一进入, 无异常且堆栈指向正确页面."""
    for key in ("home", "devices", "wall", "session", "settings", "account", "approvals"):
        window.switch_page(key)
        current = window.stack.currentWidget()
        assert current is not None
        assert current.page_key == key
    assert window.command_log is not None


def test_connect_enters_session_and_back(window: MainWindow) -> None:
    """连接发起后进入会话窗显示连接中, 返回按钮回到连接页, 不留死页面."""
    window.switch_page("home")
    api.get_service().register_peer("ABCDEFGHIJKLMNOPQRST", "对端")
    window.home.peer_input.setText("ABCDEFGHIJKLMNOPQRST")
    window.home.code_input.setText("123456")
    window.home._on_connect()

    assert window.stack.currentWidget() is window.session_page
    assert "连接中" in window.session_page.status_line.text()

    window.session_page._on_back()
    assert window.stack.currentWidget() is window.home


def test_unconnected_states_are_explicit(window: MainWindow) -> None:
    """无后端时每个业务页都给出明确的未连接 / 未登录提示."""
    window.switch_page("home")
    assert "未连接" in window.home.connect_status.text()
    window.switch_page("session")
    assert "未连接" in window.session_page.picture_label.text()
    window.switch_page("devices")
    assert "登录" in window.devices.status_label.text()
    window.switch_page("approvals")
    assert "登录" in window.approvals.status_label.text()
    window.switch_page("account")
    assert "登录" in window.account.status_label.text()
    window.switch_page("wall")
    assert "未创建" in window.wall.budget_label.text()


def test_empty_actions_return_contract_errors(window: MainWindow) -> None:
    """空参数动作走命令层返回错误码, 界面只呈现不改写."""
    window.home._on_connect()
    assert window.home.connect_status.text()
    window.devices._on_bind()
    assert window.devices.status_label.text()
    window.approvals._on_request()
    assert window.approvals.status_label.text() == "申请理由必填"
    window.settings.relay_egress.setText("abc")
    window.settings._on_apply_relay()
    assert "正整数" in window.settings.relay_status.text()
    window.session_page._on_status()
    assert "未连接" in window.session_page.transfer_notice.text()


def test_wall_page_renders_and_focuses(window: MainWindow) -> None:
    """屏幕墙四格渲染、点选聚焦后 4K60 并发为 1."""
    window.switch_page("wall")
    window.wall._on_layout(4)
    assert window.wall.grid.count() == 4
    button = window.wall.grid.itemAt(0).widget()
    assert button is not None and "格 1" in button.text()
    window.wall._on_focus(0)
    assert "4K60 并发 1/1" in window.wall.budget_label.text()


def test_session_transfer_drag_and_progress(window: MainWindow, tmp_path: Path) -> None:
    """会话页拖拽真实文件到落点, 轮询推进到完成并校验目标文件."""
    window.switch_page("session")
    page = window.session_page
    page.handle_dropped_paths([str(tmp_path / "unused.bin")])
    assert "未连接" in page.transfer_notice.text()

    page.set_session("session-smoke")
    source = tmp_path / "drag.bin"
    source.write_bytes(bytes(range(256)) * 40)
    page.destination_input.setText(str(tmp_path / "inbox"))
    page.handle_dropped_paths([str(source)])
    for _ in range(20):
        page._poll_transfer()
        if page._transfer_state == "completed":
            break
    assert page._transfer_state == "completed"
    assert page.transfer_bar.value() == 100
    assert (tmp_path / "inbox" / "drag.bin").read_bytes() == source.read_bytes()
    assert any(action == "传输进度" for action, _ in window.command_log)


def test_session_clipboard_roundtrip(window: MainWindow) -> None:
    """会话页发送本端文本后, 对端内容可拉回本端系统剪贴板."""
    window.switch_page("session")
    page = window.session_page
    page.set_session("session-smoke")
    QApplication.clipboard().setText("来自桌面端的文本")
    page._on_send_text()
    assert "已同步" in page.clipboard_status.text()

    clipboard.push_clipboard("android", "text", "来自安卓端的文本")
    page._on_pull_text()
    assert QApplication.clipboard().text() == "来自安卓端的文本"
    assert any(action == "剪贴板同步" for action, _ in window.command_log)


def test_theme_toggle_and_login_flow(window: MainWindow) -> None:
    """深浅色切换不崩; 注册并登录后设备令牌只显示掩码."""
    window.toggle_theme()
    assert window.theme_button.text() == "切换深色"
    window.switch_page("account")
    window.account.register_account.setText("smoke-user")
    window.account.register_password.setText("pass-123456")
    window.account.register_confirm.setText("pass-123456")
    window.account._on_register()
    assert "已登录" in window.account.status_label.text()
    assert "…" in window.account.token_label.text()
    assert stylesheet(True) != stylesheet(False)
