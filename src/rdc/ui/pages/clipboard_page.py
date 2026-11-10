# ==========================================================================================
# 文件        : clipboard_page.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 剪贴板页: 两列布局的同步开关、当前内容、同步历史与安全边界
# 架构定位    : 界面层, 只调用 rdc.commands.api 与 rdc.commands.clipboard
# 核心功能    : 无会话空态、开关状态、脱敏预览、已同步/未同步判定、本页操作记录
# 职责边界    : 不读写业务状态; 不落盘明文; 不把未生效的结果显示成已同步
# ==========================================================================================

"""剪贴板页."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api, clipboard
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import CopyButton, EmptyState, chip, copy_to_clipboard, fade_in, show_toast
from rdc.ui.copy import message_text
from rdc.ui.pages.base import PageBase, row

COLUMNS = ("方向", "结果", "摘要", "时间")
ENTRY_LIMIT = 8

NO_SESSION, SESSION_FAILED, MAIN = range(3)

SECURITY_RULES = (
    "密码类内容不自动同步, 需在会话内确认.",
    "图片超过 8 兆字节时不自动传输, 改为提示手动发送.",
    "断开会话即清空对端映射, 历史仅留本机摘要.",
)


def _human_size(size: int) -> str:
    """字节数转可读文本; 与命令层口径一致, 界面不参与计算."""
    value = float(size)
    for unit in ("字节", "千字节", "兆字节"):
        if value < 1024 or unit == "兆字节":
            return f"{int(value)} {unit}" if unit == "字节" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} 兆字节"


def mask_preview(text: str) -> str:
    """脱敏预览: 只保留首尾少量字符与总长度, 不展示全文."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "（空文本）"
    length = len(collapsed)
    if length <= 8:
        body = f"{collapsed[:1]}…"
    elif length <= 16:
        body = f"{collapsed[:2]}…{collapsed[-2:]}"
    elif length <= 32:
        body = f"{collapsed[:3]}…{collapsed[-2:]}"
    else:
        body = f"{collapsed[:6]}…{collapsed[-4:]}"
    return f"{body}（共 {length} 字符）"


class ClipboardPage(PageBase):
    """剪贴板双向同步面板."""

    def __init__(self) -> None:
        super().__init__(
            "clipboard",
            "剪贴板",
            "文本 256 千字节截断, 图片 4 兆字节上限; 内容只在内存中流转.",
            scrollable=True,
        )
        self._session: dict[str, Any] | None = None
        self._entries: list[dict[str, str]] = []
        self._last_digest: str | None = None
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        self.header_actions.addWidget(self.button("刷新", self.refresh, primary=True, icon="refresh-cw"))

        self.content_stack = QStackedWidget()

        self.no_session = EmptyState(
            "session",
            "需要先建立会话",
            "剪贴板双向同步只在已建立的会话内生效. 请先在首页发起连接, 建立会话后再回到这里.",
            action_text="去建立会话",
            action_icon="plug-zap",
            action=lambda: self.navigate.emit("home"),
        )
        self.content_stack.addWidget(self.no_session)

        self.session_failed = EmptyState(
            "session",
            "无法读取会话",
            "命令层当前不可用, 无法确认是否存在活动会话. 请检查后端连接后重试.",
            action_text="重试",
            action_icon="refresh-cw",
            action=self.refresh,
        )
        self.content_stack.addWidget(self.session_failed)

        main = QWidget()
        outer = QVBoxLayout(main)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        self.channel_banner = QLabel("未接入会话通道: 剪贴板仅在本机生效.")
        self.channel_banner.setObjectName("badgeWarn")
        self.channel_banner.setWordWrap(True)
        self.channel_banner.setVisible(False)
        outer.addWidget(self.channel_banner)

        columns = QHBoxLayout()
        columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(10)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        outer.addLayout(columns, 1)

        self.session_line = QLabel("活动会话 -")
        self.session_line.setWordWrap(True)
        self.last_sync_label = QLabel("尚无同步记录")
        self.last_sync_label.setWordWrap(True)
        self.kind_label = QLabel("文本 · 图片")
        self.history_count = QLabel(f"0 / {ENTRY_LIMIT}")

        self.sync_chip = chip("同步状态未知", "warn")
        switch_card, switch_layout = self._card("同步开关", self.sync_chip)
        self.toggle = QCheckBox("允许本机与对端双向同步（文本超过 256 千字节截断, 图片上限 4 兆字节）")
        self.toggle.toggled.connect(self._on_toggle)
        switch_layout.addWidget(self.toggle)
        switch_layout.addWidget(self._kv("会话", self.session_line))
        switch_layout.addWidget(self._kv("最近同步", self.last_sync_label))
        switch_layout.addWidget(self._kv("内容类型", self.kind_label))
        switch_layout.addWidget(self._kv("历史条数", self.history_count))

        self.send_button = self.button("发送本端文本", self._on_send_text, primary=True, icon="send")
        self.pull_button = self.button("拉取远端文本", self._on_pull_text, icon="download")
        switch_layout.addLayout(row(self.send_button, self.pull_button))

        status_box = QFrame()
        status_box.setObjectName("statusStrip")
        status_layout = QVBoxLayout(status_box)
        status_layout.setContentsMargins(12, 9, 12, 9)
        status_layout.setSpacing(4)
        self.sync_state = QLabel("未同步")
        self.sync_state.setObjectName("badgeWarn")
        self.status_detail = QLabel("尚无同步记录.")
        self.status_detail.setObjectName("muted")
        self.status_detail.setWordWrap(True)
        status_layout.addWidget(self.sync_state)
        status_layout.addWidget(self.status_detail)
        switch_layout.addWidget(status_box)
        switch_layout.addWidget(self.muted("开关关闭后, 发送与拉取都会被命令层拒绝."))
        left.addWidget(switch_card)

        preview_card, preview_layout = self._card("当前内容", chip("文本 · 已脱敏"))
        preview_box = QFrame()
        preview_box.setObjectName("statusStrip")
        preview_inner = QVBoxLayout(preview_box)
        preview_inner.setContentsMargins(12, 10, 12, 10)
        self.preview_label = QLabel("暂无内容.")
        self.preview_label.setObjectName("mono")
        self.preview_label.setWordWrap(True)
        preview_inner.addWidget(self.preview_label)
        preview_layout.addWidget(preview_box)

        self.digest_copy = CopyButton("", "复制内容摘要")
        self.preview_copy = QPushButton()
        self.preview_copy.setObjectName("copyBtn")
        self.preview_copy.setFixedSize(26, 26)
        self.preview_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preview_copy.setToolTip("复制脱敏预览")
        icons.apply_icon(self.preview_copy, "copy", tone="muted", size=14)
        self.preview_copy.clicked.connect(self._copy_preview)
        preview_layout.addLayout(row(self.digest_copy, self.preview_copy, self.muted("复制到本机剪贴板")))
        preview_layout.addWidget(
            self.muted("明文不落盘; 本页只保留本次运行内的脱敏预览, 历史内容需由命令层提供查询命令.")
        )
        left.addWidget(preview_card)
        left.addStretch(1)

        self.history_chip = chip("最近 0 条")
        history_card, history_layout = self._card("同步历史", self.history_chip)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        history_layout.addWidget(self.table, 1)

        self.entries_hint = QLabel("本页还没有执行过同步; 发送或拉取后在此显示脱敏结果.")
        self.entries_hint.setObjectName("muted")
        self.entries_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.entries_hint.setMinimumHeight(90)
        history_layout.addWidget(self.entries_hint, 1)

        self.copy_recent_button = self.button("复制最近一条", self._copy_recent, icon="copy")
        self.copy_recent_button.setEnabled(False)
        history_layout.addLayout(row(self.copy_recent_button))
        right.addWidget(history_card, 1)

        security_card, security_layout = self._card("安全边界")
        for rule in SECURITY_RULES:
            security_layout.addWidget(self.muted(f"· {rule}"))
        right.addWidget(security_card)

        self.content_stack.addWidget(main)
        self.body.addWidget(self.content_stack, 1)
        self.table.setVisible(False)
        self._set_state(NO_SESSION)

    @staticmethod
    def _card(title: str, badge: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        caption = QLabel(title)
        caption.setObjectName("cardTitle")
        head.addWidget(caption)
        head.addStretch(1)
        if badge is not None:
            head.addWidget(badge)
        layout.addLayout(head)
        return card, layout

    @staticmethod
    def _kv(label: str, value: QLabel) -> QWidget:
        """键值行: 左侧说明, 右侧取值."""
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        caption = QLabel(label)
        caption.setObjectName("cardMeta")
        layout.addWidget(caption)
        layout.addStretch(1)
        value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(value, 3)
        return host

    def _set_state(self, state: int) -> None:
        self.content_stack.setCurrentIndex(state)

    def _set_chip(self, text: str, ok: bool) -> None:
        self.sync_chip.setText(text)
        self.sync_chip.setProperty("tone", "ok" if ok else "warn")
        self.sync_chip.style().unpolish(self.sync_chip)
        self.sync_chip.style().polish(self.sync_chip)
        self.sync_chip.update()

    def _set_sync(self, ok: bool, detail: str) -> None:
        """统一更新「已同步 / 未同步」与说明; 失败不得显示成成功."""
        self.sync_state.setText("已同步" if ok else "未同步")
        self.sync_state.setObjectName("badgeOk" if ok else "badgeWarn")
        self.sync_state.style().unpolish(self.sync_state)
        self.sync_state.style().polish(self.sync_state)
        self.sync_state.update()
        self.status_detail.setText(detail)

    # ---------------------------------------------------------------- 会话
    def refresh(self) -> None:
        result = api.list_sessions()
        self.report("刷新会话", result)
        if not result.ok or result.data is None:
            self._session = None
            self._set_chip("会话未知", False)
            reason = message_text(result.error_code, result.message, ok=result.ok)
            self.session_failed.desc_label.setText(f"{reason}. 请确认命令层与后端连接后重试.")
            self._set_state(SESSION_FAILED)
            return
        items = [dict(item) for item in result.data.get("items", [])]
        if not items:
            self._session = None
            self._set_chip("未建立会话", False)
            self._set_state(NO_SESSION)
            return
        self._session = items[0]
        session_id = str(self._session.get("session_id", ""))
        peer = str(self._session.get("peer_device_id", ""))
        route = "中继" if self._session.get("transport_kind") == "relay" else "直连"
        self.session_line.setText(f"{session_id} · 对端 {peer or '-'} · {route}")
        self._set_state(MAIN)
        self._refresh_status()

    def _refresh_status(self) -> None:
        """读取开关与最近内容摘要; 不读取明文, 不覆盖最近一次命令的成败."""
        result = clipboard.get_clipboard_status()
        self.report("刷新剪贴板", result)
        if not result.ok or result.data is None:
            self._set_chip("开关未知", False)
            self._set_sync(False, message_text(result.error_code, result.message, ok=result.ok))
            return
        data = result.data
        self._refresh_channel(data)
        enabled = bool(data.get("enabled"))
        self.toggle.blockSignals(True)
        self.toggle.setChecked(enabled)
        self.toggle.blockSignals(False)
        self._set_chip("同步已开启" if enabled else "同步已关闭", enabled)
        self.send_button.setEnabled(enabled)
        self.pull_button.setEnabled(enabled)

        digest = str(data.get("digest") or "")
        if not enabled:
            self._set_sync(False, "剪贴板同步已关闭; 打开开关后才会同步.")
            self.preview_label.setText("暂无内容.")
            return
        if data.get("pending"):
            self._set_sync(False, "已发送, 等待对端确认; 确认前不视为已同步.")
            return
        if not digest:
            self._set_sync(False, "尚无同步内容; 发送本端文本或拉取远端文本后这里会显示脱敏预览.")
            self.preview_label.setText("暂无内容.")
            return
        size = _human_size(int(data.get("size_bytes", 0) or 0))
        origin = {"remote": "对端", "local": "本机"}.get(str(data.get("origin") or ""), "未知来源")
        kind = "文本" if data.get("kind") == "text" else "图片"
        detail = f"最近内容 {kind} {size} · 来源 {origin} · 摘要 {digest[:12]}…"
        self.digest_copy.set_value(digest, "复制内容摘要")
        if data.get("truncated"):
            detail += " · 已截断"
        self.status_detail.setText(detail)
        if digest != self._last_digest:
            self.preview_label.setText(f"最近内容不是由本页同步, 明文不在界面缓存（摘要 {digest[:12]}…）.")

    def _refresh_channel(self, data: dict[str, Any]) -> None:
        """通道提示条: 未接入或降级通道都要让用户看到真实状态."""
        mode = str(data.get("mode") or "local")
        if mode == "channel" and data.get("visible_to_relay"):
            text = "当前走信令降级通道：剪贴板内容对中继可见，未达端到端加密。"
            style = "badgeWarn"
        elif mode == "channel":
            text = "已接入会话通道：剪贴板内容发送到对端并等待确认。"
            style = "badgeOk"
        else:
            text = "未接入会话通道：剪贴板内容仅在本机生效, 不会同步到对端。"
            style = "badgeWarn"
        self.channel_banner.setText(text)
        self.channel_banner.setObjectName(style)
        self.channel_banner.style().unpolish(self.channel_banner)
        self.channel_banner.style().polish(self.channel_banner)
        self.channel_banner.update()
        self.channel_banner.setVisible(True)

    # ---------------------------------------------------------------- 命令
    def _on_toggle(self, enabled: bool) -> None:
        result = clipboard.set_clipboard_enabled(enabled)
        self.report("切换剪贴板", result)
        if not result.ok:
            self.toggle.blockSignals(True)
            self.toggle.setChecked(not enabled)
            self.toggle.blockSignals(False)
            self._set_sync(False, message_text(result.error_code, result.message, ok=result.ok))
            return
        self._set_chip("同步已开启" if enabled else "同步已关闭", enabled)
        self.send_button.setEnabled(enabled)
        self.pull_button.setEnabled(enabled)
        if enabled:
            self.status_detail.setText("剪贴板同步已开启; 可发送本端文本或拉取远端文本.")
        else:
            self._set_sync(False, "剪贴板同步已关闭.")
            self.preview_label.setText("暂无内容.")

    def _on_send_text(self) -> None:
        if self._session is None:
            self._set_sync(False, "需要先建立会话.")
            return
        text = QApplication.clipboard().text()
        if not text:
            self._set_sync(False, "本端剪贴板没有文本, 未发起同步.")
            return
        result = clipboard.push_clipboard("desktop", "text", text)
        self.report("剪贴板同步", result)
        applied = self._applied(result)
        if applied:
            truncated = bool(result.data.get("truncated")) if result.data else False
            preview = mask_preview(text) + ("（已截断）" if truncated else "")
            self._last_digest = self._digest(result)
            self.preview_label.setText(preview)
        else:
            preview = "未同步, 无新增内容."
        self._set_sync(applied, message_text(result.error_code, result.message, ok=result.ok))
        self._add_entry("本端 → 远端", applied, result, preview)

    def _on_pull_text(self) -> None:
        if self._session is None:
            self._set_sync(False, "需要先建立会话.")
            return
        result = clipboard.pull_clipboard("desktop")
        self.report("剪贴板同步", result)
        applied = self._applied(result)
        data = result.data or {}
        preview = "未同步, 无新增内容."
        if applied and data.get("kind") == "text" and "text" in data:
            text = str(data["text"])
            QApplication.clipboard().setText(text)
            preview = mask_preview(text) + ("（已截断）" if data.get("truncated") else "")
            self._last_digest = self._digest(result)
            self.preview_label.setText(preview)
            self._set_sync(True, message_text(result.error_code, result.message, ok=result.ok))
        elif applied:
            self.preview_label.setText("远端最新内容是图片, 本页只展示文本预览.")
            self._set_sync(True, message_text(result.error_code, result.message, ok=result.ok))
        else:
            self._set_sync(False, message_text(result.error_code, result.message, ok=result.ok))
        self._add_entry("远端 → 本端", applied, result, preview)

    @staticmethod
    def _applied(result: CommandResult) -> bool:
        return bool(result.ok and result.data and result.data.get("applied"))

    @staticmethod
    def _digest(result: CommandResult) -> str | None:
        digest = str(result.data.get("digest") or "") if result.data else ""
        return digest or None

    def _add_entry(self, direction: str, applied: bool, result: CommandResult, preview: str) -> None:
        """记录本页最近一次操作; 只存摘要与脱敏预览, 不落盘."""
        data = result.data or {}
        digest = str(data.get("digest") or "")
        kind = data.get("kind")
        kind_text = "文本" if kind == "text" else ("图片" if kind == "image" else "-")
        size_text = _human_size(int(data.get("size_bytes", 0) or 0)) if data else "-"
        now = datetime.now()
        summary = f"{kind_text} · {size_text}"
        if digest:
            summary += f" · 摘要 {digest[:12]}…"
        entry = {
            "direction": direction,
            "status": "已同步" if applied else "未同步",
            "summary": summary,
            "time": now.strftime("%H:%M"),
            "preview": preview,
        }
        self._entries.insert(0, entry)
        del self._entries[ENTRY_LIMIT:]
        self.last_sync_label.setText(f"{now.strftime('%H:%M:%S')} · {direction}")
        self._render_entries()

    def _copy_preview(self) -> None:
        """复制脱敏预览; 明文不进入剪贴板之外的本页缓存."""
        text = self.preview_label.text()
        if text and text != "暂无内容." and copy_to_clipboard(text):
            show_toast(self)

    def _copy_recent(self) -> None:
        """复制最近一条同步记录的方向、摘要、时间与脱敏预览."""
        if not self._entries:
            self.status_detail.setText("本页还没有同步记录, 无可复制内容.")
            return
        entry = self._entries[0]
        text = (
            f"{entry['direction']} · {entry['status']}\n"
            f"摘要: {entry['summary']}\n"
            f"时间: {entry['time']}\n"
            f"预览: {entry['preview']}"
        )
        if copy_to_clipboard(text):
            show_toast(self)

    def _render_entries(self) -> None:
        self.table.setRowCount(len(self._entries))
        for index, entry in enumerate(self._entries):
            for column, key in enumerate(("direction", "status", "summary", "time")):
                cell = QTableWidgetItem(entry[key])
                if key == "summary":
                    cell.setToolTip(entry["preview"])
                self.table.setItem(index, column, cell)
        count = len(self._entries)
        has_rows = bool(count)
        self.history_count.setText(f"{count} / {ENTRY_LIMIT}")
        self.history_chip.setText(f"最近 {count} 条")
        self.copy_recent_button.setEnabled(has_rows)
        self.table.setVisible(has_rows)
        self.entries_hint.setVisible(not has_rows)
