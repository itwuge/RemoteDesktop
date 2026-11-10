# ==========================================================================================
# 文件        : approvals.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 申请与审批页: 待我审批 / 我发起的 / 已处理 三组, 真实审批与申请
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 胶囊分组、批准限时或一次性、拒绝、发起申请与就地校验
# 职责边界    : 不判定权限; 不自造令牌; 不改写命令返回文案
# ==========================================================================================

"""申请与审批页."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import accounts
from rdc.core.result import CommandResult
from rdc.ui.components import CopyButton, EmptyState, chip, fade_in
from rdc.ui.copy import error_text, message_text
from rdc.ui.pages.base import PageBase, row

SCOPE_LABELS = {"view": "只看画面", "control": "可控制", "full": "完整权限"}
STATUS_LABELS = {
    "pending": "待审批",
    "approved": "已批准",
    "rejected": "已拒绝",
    "expired": "已过期",
}
GROUPS = (
    ("待我审批", "incoming"),
    ("我发起的", "outgoing"),
    ("已处理", "decided"),
)
APPROVAL_RULES = (
    "受信设备免审批；撤销信任后立即失效。",
    "一次性授权在会话断开后作废。",
    "拒绝需填写理由，理由会回传给申请方。",
)
TTL_30_MINUTES = 1800
DEVICE_ID_PATTERN = re.compile(r"^[A-Z2-7]{20}$")

LOADING, EMPTY, FAILED, TABLE = range(4)


class ApprovalsPage(PageBase):
    """申请与审批."""

    def __init__(self) -> None:
        super().__init__(
            "approvals",
            "申请与审批",
            "批准可生成 30 分钟限时或一次性授权; 拒绝会写入设备审计.",
            scrollable=True,
        )
        self._incoming: list[dict[str, Any]] = []
        self._outgoing: list[dict[str, Any]] = []
        self._rows: list[dict[str, Any]] = []
        self._row_items: dict[str, dict[str, Any]] = {}
        self._request_mode = False
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        # 分组状态仍由隐藏的下拉框承载, 胶囊标签只负责切换与展示.
        self.group_combo = QComboBox(self)
        for label, value in GROUPS:
            self.group_combo.addItem(label, value)
        self.group_combo.setVisible(False)
        self.group_combo.currentIndexChanged.connect(self._on_group_changed)

        self.header_actions.addWidget(self.button("发起申请", self._reveal_request_form, primary=True, icon="send"))
        self.header_actions.addWidget(self.button("刷新", self.refresh, icon="refresh-cw"))

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 9, 12, 9)
        toolbar_layout.setSpacing(8)
        self.group_pills = QButtonGroup(toolbar)
        self.group_pills.setExclusive(True)
        self.group_buttons: list[QPushButton] = []
        for index, (label, _value) in enumerate(GROUPS):
            pill = self.button(label, self._group_handler(index))
            pill.setProperty("seg", True)
            pill.setCheckable(True)
            pill.setChecked(index == 0)
            self.group_pills.addButton(pill, index)
            self.group_buttons.append(pill)
            toolbar_layout.addWidget(pill)
        self.status_label = QLabel("未登录 · 请先登录")
        self.status_label.setObjectName("cardMeta")
        self.status_label.setWordWrap(True)
        toolbar_layout.addWidget(self.status_label, 1)
        self.body.addWidget(toolbar)

        self.feedback = QLabel("")
        self.feedback.setObjectName("muted")
        self.feedback.setWordWrap(True)
        self.body.addWidget(self.feedback)

        self.content_stack = QStackedWidget()

        self.loading = QLabel("正在读取申请…")
        self.loading.setObjectName("muted")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.setMinimumHeight(180)
        self.content_stack.addWidget(self.loading)

        self.empty = EmptyState(
            "inbox",
            "没有待处理的申请",
            "当其他账号申请控制你的设备时, 会在这里出现; 批准后生成限时或一次性授权.",
            action_text="发起申请",
            action_icon="send",
            action=self._reveal_request_form,
        )
        self.content_stack.addWidget(self.empty)

        self.failed = EmptyState(
            "inbox",
            "无法读取申请列表",
            "命令层当前不可用或尚未登录. 请先在账号页完成登录, 再回到这里重试.",
            action_text="重试",
            action_icon="refresh-cw",
            action=self.refresh,
        )
        self.content_stack.addWidget(self.failed)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["申请", "设备", "申请人", "范围", "理由", "状态", "时间"])
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in (0, 1, 2, 3, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(210)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.content_stack.addWidget(self.table)
        self.body.addWidget(self.content_stack, 1)

        self.approve_button = self.button("批准 30 分钟", lambda: self._decide("approve"), primary=True, icon="check")
        self.approve_once_button = self.button("批准一次性", lambda: self._decide("approve_once"), icon="circle-check")
        self.reject_button = self.button("拒绝", lambda: self._decide("reject"), icon="x")
        self.request_copy = CopyButton("", "复制申请详情: 申请号、设备编号与账号")
        self.body.addLayout(
            row(
                self.approve_button,
                self.approve_once_button,
                self.reject_button,
                self.request_copy,
                self.muted("撤回未实现: v1 无对应命令, 此入口不提供."),
            )
        )

        self.bottom_area = self._build_bottom_area()
        self.bottom_area.setVisible(False)
        self.body.addWidget(self.bottom_area)
        self._update_actions()

    def _build_bottom_area(self) -> QWidget:
        """表格下方两列: 左侧发起授权申请表单, 右侧审批规则."""
        area = QWidget()
        columns = QHBoxLayout(area)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(12)

        request_card, request_layout = self._card("发起授权申请", "走服务端审批")
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.request_device = QLineEdit()
        self.request_device.setPlaceholderText("20 位设备编号, 大写字母与数字 2-7")
        self.request_device.setMaxLength(20)
        self.request_scope = QComboBox()
        for value, label in SCOPE_LABELS.items():
            self.request_scope.addItem(label, value)
        self.request_reason = QLineEdit()
        self.request_reason.setPlaceholderText("申请理由, 必填")
        form.addRow("设备编号", self.request_device)
        form.addRow("权限范围", self.request_scope)
        form.addRow("理由", self.request_reason)
        request_layout.addLayout(form)
        request_layout.addLayout(row(self.button("提交申请", self._on_request, primary=True, icon="send")))
        columns.addWidget(request_card, 3)

        rules_card, rules_layout = self._card("审批规则")
        for rule in APPROVAL_RULES:
            item = QLabel(f"· {rule}")
            item.setObjectName("muted")
            item.setWordWrap(True)
            rules_layout.addWidget(item)
        rules_layout.addStretch(1)
        columns.addWidget(rules_card, 2)
        return area

    def _card(self, title: str, badge: str = "") -> tuple[QFrame, QVBoxLayout]:
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
        if badge:
            head.addWidget(chip(badge))
        layout.addLayout(head)
        return card, layout

    def _group_handler(self, index: int) -> Callable[[], None]:
        """生成分组切换回调, 避免闭包捕获循环变量."""

        def handler() -> None:
            self._select_group(index)

        return handler

    def _select_group(self, index: int) -> None:
        self.group_combo.setCurrentIndex(index)

    def _on_group_changed(self, index: int) -> None:
        self._request_mode = False
        for position, button in enumerate(self.group_buttons):
            button.setChecked(position == index)
        self.refresh()

    def _reveal_request_form(self) -> None:
        """空态或页头的「发起申请」: 切回正常态并展开下方表单."""
        self._request_mode = True
        self._set_state(TABLE)
        self.request_device.setFocus()

    def _set_feedback(self, text: str, *, ok: bool = False) -> None:
        """就地反馈; 成功与失败用不同色调, 文案不改写命令返回."""
        self.feedback.setText(text)
        self.feedback.setObjectName("badgeOk" if ok else "badgeWarn")
        self.feedback.style().unpolish(self.feedback)
        self.feedback.style().polish(self.feedback)
        self.feedback.update()

    def _set_state(self, state: int) -> None:
        self.content_stack.setCurrentIndex(state)
        self.bottom_area.setVisible(state == TABLE)

    def _current_group(self) -> str:
        return str(self.group_combo.currentData())

    # ---------------------------------------------------------------- 命令
    def _decide(self, action: str) -> None:
        request_id = self._selected_request()
        if request_id is None:
            self._set_feedback("请先选择一条待审批的申请")
            return
        if self._current_group() != "incoming":
            self._set_feedback("只有「待我审批」中的申请可以处理")
            return
        if action == "reject":
            result = accounts.reject_access(request_id, "在申请与审批页拒绝")
        else:
            result = accounts.approve_access(request_id, TTL_30_MINUTES, action == "approve_once")
        self._set_feedback(message_text(result.error_code, result.message, ok=result.ok), ok=result.ok)
        self.report("审批申请", result)
        if result.ok:
            self.refresh()

    def _on_request(self) -> None:
        device_id = self.request_device.text().strip().upper()
        self.request_device.setText(device_id)
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            self._set_feedback("设备编号应为 20 位大写字母或数字, 且只含 2-7")
            self.request_device.setFocus()
            return
        reason = self.request_reason.text().strip()
        if not reason:
            self._set_feedback("申请理由必填")
            self.request_reason.setFocus()
            return
        result = accounts.request_access(device_id, str(self.request_scope.currentData()), reason)
        self._set_feedback(message_text(result.error_code, result.message, ok=result.ok), ok=result.ok)
        self.report("发起申请", result)
        if result.ok:
            self.request_reason.clear()
            self.refresh()

    def _selected_request(self) -> str | None:
        row_index = self.table.currentRow()
        if row_index < 0:
            return None
        item = self.table.item(row_index, 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        self._set_state(LOADING)
        incoming = accounts.list_access_requests("incoming")
        outgoing = accounts.list_access_requests("outgoing")
        failure = incoming if not incoming.ok else (None if outgoing.ok else outgoing)
        if failure is not None:
            self._incoming = []
            self._outgoing = []
            self._render_rows([])
            reason = error_text(failure.error_code, failure.message)
            self.status_label.setText(reason)
            self.failed.desc_label.setText(f"{reason}. 请先在账号页完成登录, 或确认命令层与后端连接后重试.")
            self._set_state(FAILED)
            self.report("刷新申请", failure)
            return

        self._incoming = _items_of(incoming)
        self._outgoing = _items_of(outgoing)
        self._render_rows(self._rows_for_group())
        pending = sum(1 for item in self._incoming if item.get("status") == "pending")
        self.group_buttons[0].setText(f"待我审批 {pending}")
        self.status_label.setText(
            f"本组 {len(self._rows)} 条 · 待我审批 {pending} 条 · 我发起 {len(self._outgoing)} 条"
        )
        self._set_state(TABLE if self._rows or self._request_mode else EMPTY)
        if not self._rows and not self._request_mode:
            self._fill_empty()
        self.report("刷新申请", incoming if self._current_group() != "outgoing" else outgoing)

    def _rows_for_group(self) -> list[dict[str, Any]]:
        group = self._current_group()
        if group == "incoming":
            rows = [item for item in self._incoming if item.get("status") == "pending"]
        elif group == "outgoing":
            rows = list(self._outgoing)
        else:
            merged = self._incoming + self._outgoing
            seen: set[str] = set()
            rows = []
            for item in merged:
                request_id = str(item.get("request_id", ""))
                if item.get("status") == "pending" or request_id in seen:
                    continue
                seen.add(request_id)
                rows.append(item)
        rows.sort(key=lambda item: str(item.get("decided_at") or item.get("created_at") or ""), reverse=True)
        return rows

    def _render_rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._row_items = {}
        self.table.setRowCount(len(rows))
        for index, item in enumerate(rows):
            request_id = str(item.get("request_id", ""))
            self._row_items[request_id] = dict(item)
            requester = str(item.get("requester", ""))
            device_id = str(item.get("device_id", ""))
            values = [
                _short_id(request_id),
                _short_device(device_id),
                _short_account(requester),
                SCOPE_LABELS.get(str(item.get("scope")), "未知范围"),
                str(item.get("reason", "")),
                STATUS_LABELS.get(str(item.get("status", "")), "未知状态"),
                _short_time(str(item.get("decided_at") or item.get("created_at") or "-")),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, request_id)
                    cell.setToolTip(request_id)
                elif column == 1:
                    cell.setToolTip(device_id)
                elif column == 2:
                    cell.setToolTip(requester)
                elif column == 4:
                    cell.setToolTip(str(item.get("reason", "")))
                elif column == 6:
                    cell.setToolTip(str(item.get("decided_at") or item.get("created_at") or "-"))
                self.table.setItem(index, column, cell)
        self._update_actions()

    def _update_actions(self) -> None:
        enabled = self._current_group() == "incoming" and self._selected_request() is not None
        for button in (self.approve_button, self.approve_once_button, self.reject_button):
            button.setEnabled(enabled)
        request = self._row_items.get(self._selected_request() or "")
        if request is None:
            self.request_copy.set_value("")
            return
        summary = (
            f"申请号 {request.get('request_id') or '未知'}\n"
            f"设备编号 {request.get('device_id') or '未知'}\n"
            f"申请人 {request.get('requester') or '未知'}\n"
            f"范围 {SCOPE_LABELS.get(str(request.get('scope')), '未知范围')}\n"
            f"理由 {request.get('reason') or '无'}\n"
            f"状态 {STATUS_LABELS.get(str(request.get('status')), '未知状态')}"
        )
        self.request_copy.set_value(summary, "复制申请详情: 申请号、设备编号与账号")

    def _fill_empty(self) -> None:
        group = self._current_group()
        if group == "incoming":
            self.empty.title_label.setText("没有待处理的申请")
            self.empty.desc_label.setText("当其他账号申请控制你的设备时, 会在这里出现; 批准后生成限时或一次性授权.")
        elif group == "outgoing":
            self.empty.title_label.setText("还没有发起过申请")
            self.empty.desc_label.setText(
                "点「发起申请」填写设备编号、权限范围与理由, 提交后可在「我发起的」中查看状态."
            )
        else:
            self.empty.title_label.setText("还没有处理记录")
            self.empty.desc_label.setText("批准或拒绝过的申请会出现在这里, 并保留审批时间与状态.")


def _items_of(result: CommandResult) -> list[dict[str, Any]]:
    if not result.ok or result.data is None:
        return []
    return [dict(item) for item in result.data.get("items", [])]


def _short_id(request_id: str) -> str:
    """申请号只显示可辨认的短标识, 完整值放在 tooltip."""
    text = request_id.removeprefix("req-")
    return text[:8] or "-"


def _short_account(account_id: str) -> str:
    """账号标识过长时中段省略; 完整值放 tooltip."""
    if len(account_id) <= 16:
        return account_id or "-"
    return f"{account_id[:8]}…{account_id[-4:]}"


def _short_device(device_id: str) -> str:
    """设备码在表格里只显示首尾各 4 位, 完整值放 tooltip."""
    if len(device_id) <= 12:
        return device_id or "-"
    return f"{device_id[:4]}…{device_id[-4:]}"


def _short_time(value: str) -> str:
    """ISO 时间压缩成 月-日 时:分, 只用于表格展示; 完整值放 tooltip."""
    if len(value) >= 16 and value[4] == "-" and value[10] == "T":
        return f"{value[5:10]} {value[11:16]}"
    return value
