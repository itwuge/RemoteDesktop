"""桌面界面离屏冒烟: 每页可构造、可切换、无后端显示未连接、关键交互不崩."""

from __future__ import annotations

import json
import re
import socket
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from rdc.accounts.service import AccountService
from rdc.clipboard.sync import ClipboardSyncService
from rdc.commands import accounts, api, clipboard, transfer, wall
from rdc.commands import settings as settings_commands
from rdc.commands.service import CommandService
from rdc.core.result import CommandResult
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
    """十个页面逐一进入, 无异常且堆栈指向正确页面."""
    for key in (
        "home",
        "devices",
        "wall",
        "session",
        "transfer",
        "clipboard",
        "settings",
        "account",
        "approvals",
        "messages",
    ):
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


def test_session_failure_text_has_no_bare_error_codes(window: MainWindow) -> None:
    """会话失败时界面只给中文原因, 不出现 CODE / [CODE] / 【CODE】."""
    window.switch_page("session")
    page = window.session_page
    page.set_session("missing-session")
    page._on_status()
    texts = (
        page.status_line.text(),
        page.recovery_label.text(),
        page.transfer_notice.text(),
        page._info.get("error", ""),
    )
    for text in texts:
        assert not re.search(r"[\[【]\s*[A-Z][A-Z0-9_]+\s*[\]】]", text), text
        assert not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text), text


def test_unconnected_states_are_explicit(window: MainWindow) -> None:
    """无后端时每个业务页都给出明确的未连接 / 未登录提示."""
    window.switch_page("home")
    assert "未连接" in window.home.connect_status.text()
    window.switch_page("session")
    assert "未连接" in window.session_page.picture_label.text()
    window.switch_page("transfer")
    assert window.transfer_page.no_session.title_label.text() == "需要先建立会话"
    window.switch_page("clipboard")
    assert window.clipboard_page.no_session.title_label.text() == "需要先建立会话"
    window.switch_page("devices")
    assert "登录" in window.devices.status_label.text()
    window.switch_page("approvals")
    assert "登录" in window.approvals.status_label.text()
    window.switch_page("messages")
    assert "登录" in window.messages.status_label.text()
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
    assert "设备编号" in window.approvals.feedback.text()
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


def test_settings_page_saves_reports_and_probes(
    window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置页: 非法地址就地提示, 保存写入同一份 config.json, 探测结果中文呈现."""
    monkeypatch.setenv("RDC_DATA_DIR", str(tmp_path))
    window.switch_page("settings")
    page = window.settings

    page.endpoint_edit.setText("bad-address")
    assert "host:port" in page.endpoint_hint.text()
    page._on_save()
    assert "host:port" in page.save_status.text()
    assert not (tmp_path / "config.json").exists()

    page.endpoint_edit.setText("192.168.37.132:27888")
    page.name_edit.setText("冒烟主机")
    page._on_save()
    assert page.save_status.text() == "设置已保存"
    payload = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert payload["relay_endpoint"] == "192.168.37.132:27888"
    assert payload["device_name"] == "冒烟主机"

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    page.endpoint_edit.setText(f"127.0.0.1:{server.getsockname()[1]}")
    try:
        page._on_probe()
        for _ in range(60):
            QApplication.processEvents()
            if page._probe_worker is None:
                break
            time.sleep(0.05)
    finally:
        server.close()
    assert "中继可达" in page.probe_status.text()


def _autostart_data(enabled: bool) -> dict[str, object]:
    return {
        "enabled": enabled,
        "supported": True,
        "portable": False,
        "target": "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\RDC 远程桌面",
        "command": '"C:\\RDC\\rdc-desktop.exe" --autostart',
        "detail": "已登记" if enabled else "未登记",
    }


def test_settings_autostart_switch_reads_system_state(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    """开关初值来自系统读取; 点击后立即调用命令并刷新为真实状态."""
    window.switch_page("settings")
    page = window.settings
    enabled_state = {"value": False}
    monkeypatch.setattr(
        settings_commands,
        "get_autostart_status",
        lambda: CommandResult(ok=True, message="已登记", data=_autostart_data(enabled_state["value"])),
    )

    def apply(enabled: bool) -> CommandResult:
        enabled_state["value"] = enabled
        return CommandResult(ok=True, message="已开启开机自启", data=_autostart_data(enabled))

    monkeypatch.setattr(settings_commands, "set_autostart_enabled", apply)
    page._refresh_autostart()
    assert page.autostart_check.isEnabled()
    assert not page.autostart_check.isChecked()

    page.autostart_check.click()
    assert page.autostart_check.isChecked()
    assert "已开启" in page.autostart_status.text()
    assert any(action == "开机自启" for action, _ in window.command_log)


def test_settings_autostart_failure_reports_reason(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    """写入失败时保留原因, 开关按回读值复位."""
    window.switch_page("settings")
    page = window.settings
    monkeypatch.setattr(
        settings_commands,
        "get_autostart_status",
        lambda: CommandResult(ok=True, message="未登记", data=_autostart_data(False)),
    )
    monkeypatch.setattr(
        settings_commands,
        "set_autostart_enabled",
        lambda enabled: CommandResult(ok=False, message="开机自启设置失败：拒绝访问；回读当前状态：未登记"),
    )
    page._refresh_autostart()
    page.autostart_check.click()
    assert "拒绝访问" in page.autostart_status.text()
    assert not page.autostart_check.isChecked()


def test_close_to_tray_hides_instead_of_quitting(window: MainWindow) -> None:
    """托盘模式下关闭主窗口只隐藏, 不退出进程."""
    window.set_close_to_tray(True)
    window.show()
    window.close()
    assert window.isHidden()


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
