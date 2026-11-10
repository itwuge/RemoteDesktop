# ==========================================================================================
# 文件        : messages.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 消息中心页: 审计事件按日期分组的卡片时间线、服务端未读与已读水位
# 架构定位    : 界面层, 只调用 rdc.commands.messages
# 核心功能    : 分类胶囊筛选、服务端 unread/read_until、全部已读、复制单条/全部
# 职责边界    : 不本地累计未读; 不自造事件; 不改写命令返回文案; 不提供无契约的导出
# ==========================================================================================

"""消息中心页."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import messages
from rdc.core.result import CommandResult
from rdc.ui.components import (
    CopyButton,
    EmptyState,
    MenuItem,
    chip,
    copy_to_clipboard,
    fade_in,
    open_menu,
    show_toast,
)
from rdc.ui.copy import error_text, result_text
from rdc.ui.pages.base import PageBase, row

FILTERS = (
    ("全部分类", ""),
    ("审批", "approval"),
    ("绑定", "binding"),
    ("会话", "session"),
    ("授权", "grant"),
    ("账号", "account"),
)

CATEGORY_TONES = {"session": "accent", "binding": "warn", "account": "ok"}

LOADING, EMPTY, FAILED, TIMELINE = range(4)


class MessagesPage(PageBase):
    """审计事件消息流, 按日期分组展示为时间线."""

    def __init__(self) -> None:
        super().__init__(
            "messages",
            "消息中心",
            "审批、绑定、授权与会话建立/结束都来自设备审计; 未读与已读水位以服务端返回为准.",
            scrollable=True,
        )
        self._items: list[dict[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._unread = 0
        self._read_until = ""
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        self.header_actions.addWidget(self.button("全部已读", self._on_mark_all_read, icon="circle-check"))
        self.header_actions.addWidget(self.button("刷新", self.refresh, primary=True, icon="refresh-cw"))

        summary = QFrame()
        summary.setObjectName("statusStrip")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(14, 9, 14, 9)
        summary_layout.setSpacing(8)
        self.unread_label = QLabel("未读 -")
        self.unread_label.setObjectName("badgeWarn")
        summary_layout.addWidget(self.unread_label)
        separator = QLabel("·")
        separator.setObjectName("cardMeta")
        summary_layout.addWidget(separator)
        self.watermark_label = QLabel("已读水位 未知")
        self.watermark_label.setObjectName("cardMeta")
        self.watermark_label.setToolTip("已读水位由服务端返回, 决定哪些事件算未读; 界面不做本地计数")
        summary_layout.addWidget(self.watermark_label)
        summary_layout.addStretch(1)
        hint = QLabel("未读口径与已读水位均由命令返回")
        hint.setObjectName("cardMeta")
        summary_layout.addWidget(hint)
        self.body.addWidget(summary)

        # 分类状态由隐藏的下拉框承载, 胶囊只负责切换与展示.
        self.filter_combo = QComboBox(self)
        for label, value in FILTERS:
            self.filter_combo.addItem(label, value)
        self.filter_combo.setVisible(False)
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 9, 12, 9)
        toolbar_layout.setSpacing(6)
        self.filter_group = QButtonGroup(toolbar)
        self.filter_group.setExclusive(True)
        self.filter_buttons: list[QPushButton] = []
        for index, (label, _value) in enumerate(FILTERS):
            pill = self.button(label, self._filter_handler(index))
            pill.setProperty("seg", True)
            pill.setCheckable(True)
            pill.setChecked(index == 0)
            self.filter_group.addButton(pill, index)
            self.filter_buttons.append(pill)
            toolbar_layout.addWidget(pill)
        toolbar_layout.addStretch(1)
        toolbar_layout.addWidget(self.button("复制全部", self._on_copy_all, icon="copy"))
        self.body.addWidget(toolbar)

        self.status_label = QLabel("未登录 · 请先在账号页登录")
        self.status_label.setObjectName("cardMeta")
        self.status_label.setWordWrap(True)
        self.body.addWidget(self.status_label)

        self.error_strip = QFrame()
        self.error_strip.setObjectName("statusStrip")
        error_layout = QHBoxLayout(self.error_strip)
        error_layout.setContentsMargins(14, 8, 14, 8)
        error_layout.setSpacing(8)
        self.error_label = QLabel("")
        self.error_label.setObjectName("badgeWarn")
        self.error_label.setWordWrap(True)
        error_layout.addWidget(self.error_label, 1)
        self.error_copy = CopyButton("", "复制错误原因")
        error_layout.addWidget(self.error_copy)
        self.error_strip.setVisible(False)
        self.body.addWidget(self.error_strip)

        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        self.feedback.setWordWrap(True)
        self.body.addWidget(self.feedback)

        self.content_stack = QStackedWidget()

        self.loading = QLabel("正在读取消息时间线…")
        self.loading.setObjectName("muted")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.setMinimumHeight(180)
        self.content_stack.addWidget(self.loading)

        self.empty = EmptyState(
            "inbox",
            "还没有消息",
            "登录后, 审批、绑定、授权与会话事件会从设备审计汇总到这里.",
        )
        self.content_stack.addWidget(self.empty)

        self.failed = EmptyState(
            "inbox",
            "无法读取消息",
            "命令层当前不可用或尚未登录. 请先在账号页完成登录, 再回到这里重试.",
            action_text="重试",
            action_icon="refresh-cw",
            action=self.refresh,
        )
        self.content_stack.addWidget(self.failed)

        self.timeline = QWidget()
        self.timeline_layout = QVBoxLayout(self.timeline)
        self.timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline_layout.setSpacing(12)
        self.timeline_layout.addStretch(1)
        self.content_stack.addWidget(self.timeline)
        self.body.addWidget(self.content_stack, 1)

        self.body.addLayout(row(self.muted("右键消息可复制; 未读与已读水位均由服务端返回, 本页不做本地累计.")))

    def _set_state(self, state: int) -> None:
        self.content_stack.setCurrentIndex(state)

    def _set_feedback(self, text: str, *, ok: bool = False) -> None:
        self.feedback.setText(text)
        self.feedback.setObjectName("badgeOk" if ok else "badgeWarn")
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)
        self.feedback.update()

    def _show_error(self, result: CommandResult) -> None:
        reason = error_text(result.error_code, result.message)
        self.error_label.setText(reason)
        self.error_copy.set_value(reason, "复制错误原因")
        self.error_strip.setVisible(True)

    # ---------------------------------------------------------------- 命令
    def _on_mark_all_read(self) -> None:
        if self._unread == 0:
            self._set_feedback("当前没有未读消息", ok=True)
            return
        result = messages.mark_all_read()
        self._set_feedback(result.message if result.ok else error_text(result.error_code, result.message), ok=result.ok)
        self.report("全部已读", result)
        if result.ok:
            self.refresh()

    def _on_copy_all(self) -> None:
        """复制全部按当前筛选顺序拼接, 内容与时间线可见卡片一致."""
        if not self._rows:
            self._set_feedback("当前没有可复制的消息")
            return
        text = "\n".join(self._row_text(item) for item in self._rows)
        if copy_to_clipboard(text):
            show_toast(self)
            self._set_feedback(f"已复制 {len(self._rows)} 条消息", ok=True)

    def _on_copy_one(self, item: dict[str, Any]) -> None:
        if copy_to_clipboard(self._row_text(item)):
            show_toast(self)
            self._set_feedback("已复制 1 条消息", ok=True)

    def _on_card_menu(self, card: QWidget, position: QPoint, item: dict[str, Any]) -> None:
        """时间线卡片右键: 复制这一条 / 复制全部."""
        items: list[MenuItem | None] = [
            MenuItem("复制", lambda: self._on_copy_one(item)),
            MenuItem("复制全部", self._on_copy_all, enabled=bool(self._rows)),
        ]
        open_menu(card, position, items)

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        self._set_state(LOADING)
        result = messages.list_messages()
        self.report("刷新消息", result)
        if not result.ok or result.data is None:
            self._items = []
            self._rows = []
            self._unread = 0
            self._read_until = ""
            self._update_unread()
            self._render_timeline([])
            reason = error_text(result.error_code, result.message)
            self.status_label.setText(reason)
            self._show_error(result)
            self.failed.desc_label.setText(f"{reason}. 请先在账号页完成登录, 或确认命令层与后端连接后重试.")
            self._set_state(FAILED)
            return

        self._items = [dict(item) for item in result.data.get("items", [])]
        self._unread = int(result.data.get("unread", 0))
        self._read_until = str(result.data.get("read_until") or "")
        self.error_strip.setVisible(False)
        self.status_label.setText(result.message)
        total = int(result.data.get("total", len(self._items)))
        watermark = _minute(self._read_until) if self._read_until else "暂无记录"
        self.watermark_label.setText(f"已读水位 {watermark} · 消息总数 {total} 条")
        self.watermark_label.setToolTip("已读水位与未读计数均来自服务端; 界面只按返回结果呈现")
        self._apply_filter()

    def _select_filter(self, index: int) -> None:
        self.filter_combo.setCurrentIndex(index)

    def _apply_filter(self, *_args: object) -> None:
        category = str(self.filter_combo.currentData() or "")
        rows = self._items if not category else [item for item in self._items if item.get("category") == category]
        self._rows = rows
        self._sync_pills()
        self._render_timeline(rows)
        self._update_unread()
        self._set_state(TIMELINE if rows else EMPTY)
        if not rows:
            if self._items:
                self.empty.title_label.setText("该分类下没有消息")
                self.empty.desc_label.setText("切换其他分类, 或等待新的会话与账号事件.")
            else:
                self.empty.title_label.setText("还没有消息")
                self.empty.desc_label.setText("登录后, 审批、绑定、授权与会话事件会从设备审计汇总到这里.")

    def _sync_pills(self) -> None:
        index = self.filter_combo.currentIndex()
        for position, pill in enumerate(self.filter_buttons):
            pill.blockSignals(True)
            pill.setChecked(position == index)
            pill.blockSignals(False)

    def _filter_handler(self, index: int) -> Callable[[], None]:
        """生成筛选回调, 避免闭包捕获循环变量."""

        def handler() -> None:
            self._select_filter(index)

        return handler

    def _render_timeline(self, rows: list[dict[str, Any]]) -> None:
        """按日期分组重建卡片时间线; 组内保持命令返回的倒序."""
        while self.timeline_layout.count():
            layout_item = self.timeline_layout.takeAt(0)
            if layout_item is None:
                break
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()
        if not rows:
            self.timeline_layout.addStretch(1)
            return

        current_day = ""
        for item in rows:
            day = str(item.get("ts") or "")[:10]
            if day != current_day:
                current_day = day
                heading = QLabel(_day_label(day))
                heading.setObjectName("cardMeta")
                self.timeline_layout.addWidget(heading)
            self.timeline_layout.addWidget(self._build_card(item))
        self.timeline_layout.addStretch(1)

    def _build_card(self, item: dict[str, Any]) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda position, target=card, message=item: self._on_card_menu(target, position, message)
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(7)
        title = QLabel(str(item.get("title") or "未知事件"))
        title.setObjectName("cardTitle")
        head.addWidget(title)
        category = str(item.get("category") or "")
        head.addWidget(chip(str(item.get("category_label") or "系统"), CATEGORY_TONES.get(category, "")))
        result = str(item.get("result") or "")
        if result:
            head.addWidget(chip(result_text(result), "ok" if result in ("ok", "success") else "danger"))
        if self._is_unread(item):
            head.addWidget(chip("未读", "warn"))
        head.addStretch(1)
        clock = QLabel(_clock(item.get("ts")))
        clock.setObjectName("cardMeta")
        head.addWidget(clock)
        head.addWidget(CopyButton(self._row_text(item), "复制这条消息"))
        layout.addLayout(head)

        device = str(item.get("device_name") or item.get("device_id") or "未知设备")
        actor = str(item.get("actor") or "")
        desc = f"设备 {device}" + (f" · 操作者 {actor}" if actor else "")
        meta = QLabel(desc)
        meta.setObjectName("muted")
        meta.setWordWrap(True)
        layout.addWidget(meta)
        card.setToolTip(
            f"{item.get('category_label', '')} · {item.get('title', '')}\n"
            f"设备: {device}\n"
            f"操作者: {actor or '未知'}\n"
            f"详情: {item.get('detail') or '无'}"
        )
        return card

    def _is_unread(self, item: dict[str, Any]) -> bool:
        ts = str(item.get("ts") or "")
        return not self._read_until or ts > self._read_until

    @staticmethod
    def _row_text(item: dict[str, Any]) -> str:
        device = str(item.get("device_name") or item.get("device_id") or "未知设备")
        return (
            f"[{_minute(item.get('ts'))}] {item.get('category_label', '')} · {item.get('title', '')}\n"
            f"设备: {device}\n结果: {result_text(item.get('result'))}\n详情: {item.get('detail') or '无'}"
        )

    def _update_unread(self) -> None:
        self.unread_label.setText(f"未读 {self._unread} 条")
        self.unread_label.setObjectName("badgeWarn" if self._unread else "badgeOk")
        self.unread_label.style().unpolish(self.unread_label)
        self.unread_label.style().polish(self.unread_label)
        self.unread_label.update()


def _day_label(day: str) -> str:
    """日期分组标题: 今天 / 昨天 / 具体日期."""
    value = QDate.fromString(day, "yyyy-MM-dd")
    if not value.isValid():
        return "日期未知"
    today = QDate.currentDate()
    if value == today:
        return "今天"
    if value == today.addDays(-1):
        return "昨天"
    return day


def _clock(value: object) -> str:
    """卡片右上角时间只显示时分; 缺失时显示「未知」."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return text[11:16]
    return text or "未知"


def _minute(value: object) -> str:
    """时间统一显示到分钟; 空值显示「未知」."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[0:10]} {text[11:16]}"
    return text or "未知"
