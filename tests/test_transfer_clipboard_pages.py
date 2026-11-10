"""文件传输页与剪贴板页: 会话门禁、真实落盘传输、失败重试与未同步判定."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from rdc.accounts.service import AccountService
from rdc.clipboard.sync import ClipboardSyncService
from rdc.commands import accounts, api, clipboard, transfer, wall
from rdc.commands.service import CommandService
from rdc.files import FileTransferService
from rdc.session.channel import ChannelInfo, LoopbackChannel
from rdc.ui.app import MainWindow
from rdc.ui.pages.clipboard_page import mask_preview


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


def _open_page(window: MainWindow, key: str) -> None:
    window.switch_page(key)


def test_pages_require_session(window: MainWindow) -> None:
    """没有活动会话时两个页面都显示「需要先建立会话」且不发命令."""
    _open_page(window, "transfer")
    page = window.transfer_page
    assert page.content_stack.currentIndex() == 0
    assert page.no_session.title_label.text() == "需要先建立会话"
    assert "首页发起连接" in page.no_session.desc_label.text()
    assert not page.start_button.isEnabled()

    _open_page(window, "clipboard")
    panel = window.clipboard_page
    assert panel.content_stack.currentIndex() == 0
    assert panel.no_session.title_label.text() == "需要先建立会话"
    assert "首页发起连接" in panel.no_session.desc_label.text()


def test_transfer_page_really_copies_file(window: MainWindow, tmp_path: Path) -> None:
    """有会话时界面发起传输, 轮询到完成并核对目标文件逐字节一致."""
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "transfer")
    page = window.transfer_page
    assert page.content_stack.currentIndex() == 2

    source = tmp_path / "payload.bin"
    source.write_bytes(bytes(range(256)) * 9000)
    page.source_input.setText(str(source))
    page.destination_input.setText(str(tmp_path / "inbox"))
    page._on_start()

    for _ in range(40):
        page._poll_jobs()
        if all(job.get("state") == "completed" for job in page._jobs.values()):
            break
    assert [job.get("state") for job in page._jobs.values()] == ["completed"]
    assert (tmp_path / "inbox" / "payload.bin").read_bytes() == source.read_bytes()
    assert page.progress_bar.value() == 100
    assert "已完成 1" in page.counts_label.text()
    assert page.table.item(0, 0).text() == "已完成"
    assert any(action == "开始传输" for action, _ in window.command_log)


def test_transfer_failure_reason_and_retry(window: MainWindow, tmp_path: Path) -> None:
    """传输中断显示命令层中文原因; 重试仍失败时如实提示, 源恢复后重试可完成."""
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "transfer")
    page = window.transfer_page

    source = tmp_path / "big.bin"
    payload = b"x" * (3 * 1024 * 1024)
    source.write_bytes(payload)
    page.source_input.setText(str(source))
    page.destination_input.setText(str(tmp_path / "inbox"))
    page._on_start()
    page._poll_jobs()
    source.unlink()
    page._poll_jobs()

    assert next(iter(page._jobs.values())).get("state") == "failed"
    assert page.notice.text() == "传输过程中读写失败"
    assert "传输过程中读写失败" in page.table.item(0, 6).text()
    assert "失败 1" in page.counts_label.text()

    page._on_retry()
    assert page.notice.text() == "源文件不存在"
    assert len(page._jobs) == 1

    source.write_bytes(payload)
    page._on_retry()
    assert "已开始传输" in page.notice.text()
    for _ in range(40):
        page._poll_jobs()
        if all(job.get("state") == "completed" for job in page._jobs.values()):
            break
    assert (tmp_path / "inbox" / "big.bin").read_bytes() == payload
    assert [job.get("state") for job in page._jobs.values()] == ["failed", "completed"]


def test_transfer_pause_resume_cancel(window: MainWindow, tmp_path: Path) -> None:
    """暂停后计时器停止, 继续恢复轮询, 取消把任务置为已取消."""
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "transfer")
    page = window.transfer_page

    source = tmp_path / "long.bin"
    source.write_bytes(b"y" * (4 * 1024 * 1024))
    page.source_input.setText(str(source))
    page.destination_input.setText(str(tmp_path / "inbox"))
    page._on_start()
    page._poll_jobs()

    page._on_pause()
    assert next(iter(page._jobs.values())).get("state") == "paused"
    assert not page._timer.isActive()
    assert "已暂停" in page.detail_line.text()

    page._on_resume()
    assert next(iter(page._jobs.values())).get("state") == "running"
    assert page._timer.isActive()

    page._on_cancel()
    assert next(iter(page._jobs.values())).get("state") == "canceled"
    assert not page._timer.isActive()
    assert page.table.item(0, 0).text() == "已取消"


def test_clipboard_page_push_pull_and_mask(window: MainWindow) -> None:
    """发送与拉取都走命令层; 预览脱敏, 拉取后写回系统剪贴板."""
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "clipboard")
    page = window.clipboard_page
    assert page.content_stack.currentIndex() == 2

    secret = "来自桌面端的机密文本-1234567890"
    QApplication.clipboard().setText(secret)
    page._on_send_text()
    assert page.sync_state.text() == "已同步"
    assert page.sync_chip.text() == "同步已开启"
    assert secret not in page.preview_label.text()
    assert "…" in page.preview_label.text()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "已同步"

    clipboard.push_clipboard("android", "text", "安卓端发来的文本内容")
    page._on_pull_text()
    assert QApplication.clipboard().text() == "安卓端发来的文本内容"
    assert page.sync_state.text() == "已同步"
    assert "安卓端发来的文本内容" not in page.preview_label.text()
    assert page.table.rowCount() == 2


def test_clipboard_page_unsynced_cases(window: MainWindow) -> None:
    """环回拉取与开关关闭都显示「未同步」, 不伪装成功."""
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "clipboard")
    page = window.clipboard_page

    QApplication.clipboard().setText("本端独有文本 abcdefghij")
    page._on_send_text()
    page._on_pull_text()
    assert page.sync_state.text() == "未同步"
    assert "环回" in page.status_detail.text()
    assert page.table.item(0, 1).text() == "未同步"

    page.toggle.setChecked(False)
    assert page.sync_chip.text() == "同步已关闭"
    page._on_send_text()
    assert page.sync_state.text() == "未同步"
    assert "已关闭" in page.status_detail.text()
    assert not page.send_button.isEnabled()


def test_mask_preview_hides_short_text() -> None:
    """短文本只保留首尾各一两个字符, 不因长度短而泄露全文."""
    assert mask_preview("验证码 884213") == "验证…13（共 10 字符）"
    token = "token=SECRET-9F2A-1234567890"
    masked = mask_preview(token)
    assert "SECRET" not in masked
    assert masked == "tok…90（共 28 字符）"


def test_pages_follow_session_lifecycle(window: MainWindow) -> None:
    """会话建立后进入主界面, 会话结束后回到「需要先建立会话」."""
    service = api.get_service()
    session = service.add_session("ABCDEFGHIJKLMNOPQRST")
    for key in ("transfer", "clipboard"):
        _open_page(window, key)
        assert window.stack.currentWidget().content_stack.currentIndex() == 2

    service.stop_session(session.session_id)
    for key in ("transfer", "clipboard"):
        _open_page(window, key)
        assert window.stack.currentWidget().content_stack.currentIndex() == 0


class _RelayVisibleChannel:
    """测试替身: 声明「对中继可见、未加密」的信令降级通道, 不承载真实数据."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def open(self) -> None:
        return None

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        self.sent.append((msg_type, payload))

    def receive(self, timeout: float = 0.0) -> tuple[str, dict[str, Any]] | None:
        return None

    def close(self) -> None:
        return None

    def info(self) -> ChannelInfo:
        return ChannelInfo(
            kind="relay-signaling",
            session_id="session-test",
            role="publisher",
            encrypted=False,
            visible_to_relay=True,
            budget_kbps=8000,
        )


def test_transfer_page_warns_relay_visible_channel(window: MainWindow) -> None:
    """绑定降级通道后, 页面必须直说「对中继可见、未达端到端加密」."""
    service = FileTransferService(allow_local_staging=False)
    service.bind_channel(_RelayVisibleChannel())
    transfer.configure(service)
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "transfer")
    page = window.transfer_page
    assert not page.channel_banner.isHidden()
    assert "对中继可见" in page.channel_banner.text()
    assert "未达端到端加密" in page.channel_banner.text()
    assert page.start_button.isEnabled()


def test_clipboard_page_pending_until_peer_ack(window: MainWindow) -> None:
    """通道模式下发送只算待确认, 对端 ack 前不得显示已同步."""
    service = ClipboardSyncService()
    left, _right = LoopbackChannel.pair("session-clip")
    service.bind_channel(left)
    clipboard.configure(service)
    api.get_service().add_session("ABCDEFGHIJKLMNOPQRST")
    _open_page(window, "clipboard")
    page = window.clipboard_page

    assert service.push("desktop", "text", "等待对端确认的文本").ok
    page._refresh_status()
    assert page.sync_state.text() == "未同步"
    assert "等待对端确认" in page.status_detail.text()
    assert "等待对端确认的文本" not in page.status_detail.text()
