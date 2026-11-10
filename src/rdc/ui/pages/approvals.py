# ==========================================================================================
# 文件        : approvals.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 申请与审批页: 待我审批 / 我发起的、批准限时或一次性、拒绝、发起申请
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 方向切换、列表呈现、审批动作、申请表单校验
# 职责边界    : 不判定权限; 不自造令牌; 不改写返回文案
# ==========================================================================================

"""申请与审批页."""

from __future__ import annotations

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
)

from rdc.commands import accounts
from rdc.core.result import CommandResult
from rdc.ui.components import EmptyState, fade_in
from rdc.ui.pages.base import PageBase, row

SCOPE_LABELS = {"view": "只看画面", "control": "可控制", "full": "完整权限"}
TTL_30_MINUTES = 1800


class ApprovalsPage(PageBase):
    """申请与审批."""

    def __init__(self) -> None:
        super().__init__(
            "approvals",
            "申请与审批",
            "批准可生成 30 分钟限时或一次性授权; 拒绝会写入设备审计.",
            scrollable=True,
        )
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        self.direction = QComboBox()
        self.direction.addItem("待我审批", "incoming")
        self.direction.addItem("我发起的", "outgoing")
        self.direction.currentIndexChanged.connect(self.refresh)
        self.direction.setMinimumWidth(130)
        self.header_actions.addWidget(self.direction)
        self.header_actions.addWidget(self.button("刷新", self.refresh, primary=True, icon="refresh-cw"))

        self.status_label = QLabel("未登录 · 请先登录")
        self.status_label.setObjectName("muted")
        self.body.addWidget(self.status_label)

        self.content_stack = QStackedWidget()
        self.empty = EmptyState(
            "inbox",
            "没有待处理的申请",
            "当其他账号申请控制你的设备时, 会在这里出现; 批准后生成限时或一次性授权.",
            action_text="发起一条申请",
            action_icon="send",
            action=lambda: self.request_device.setFocus(),
        )
        self.content_stack.addWidget(self.empty)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["申请", "设备", "申请人", "范围", "理由", "状态"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(210)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.content_stack.addWidget(self.table)
        self.body.addWidget(self.content_stack, 1)

        self.body.addLayout(
            row(
                self.button("批准 30 分钟", lambda: self._decide("approve"), primary=True, icon="check"),
                self.button("批准一次性", lambda: self._decide("approve_once"), icon="circle-check"),
                self.button("拒绝", lambda: self._decide("reject"), icon="x"),
                self.muted("撤回未实现: v1 无对应命令, 此入口不提供."),
            )
        )

        request_box, request_layout = self.form_section("发起申请")
        self.request_device = QLineEdit()
        self.request_device.setPlaceholderText("20 位设备码")
        self.request_scope = QComboBox()
        for value, label in SCOPE_LABELS.items():
            self.request_scope.addItem(label, value)
        self.request_reason = QLineEdit()
        self.request_reason.setPlaceholderText("申请理由, 必填")
        request_layout.addRow("设备码", self.request_device)
        request_layout.addRow("权限范围", self.request_scope)
        request_layout.addRow("理由", self.request_reason)
        request_layout.addRow(row(self.button("提交申请", self._on_request, primary=True, icon="send")))
        self.body.addWidget(request_box)
        self.table.setEnabled(False)

    # ---------------------------------------------------------------- 命令
    def _decide(self, action: str) -> None:
        request_id = self._selected_request()
        if request_id is None:
            self.status_label.setText("请先选择一条申请")
            return
        if action == "reject":
            result = accounts.reject_access(request_id, "在申请与审批页拒绝")
        else:
            result = accounts.approve_access(request_id, TTL_30_MINUTES, action == "approve_once")
        self.status_label.setText(result.message)
        self.report("审批申请", result)
        if result.ok:
            self.refresh()

    def _on_request(self) -> None:
        reason = self.request_reason.text().strip()
        if not reason:
            self.status_label.setText("申请理由必填")
            return
        result = accounts.request_access(
            self.request_device.text().strip(),
            str(self.request_scope.currentData()),
            reason,
        )
        self.status_label.setText(result.message)
        self.report("发起申请", result)
        if result.ok:
            self.refresh()

    def _selected_request(self) -> str | None:
        row_index = self.table.currentRow()
        if row_index < 0:
            return None
        item = self.table.item(row_index, 0)
        return item.text() if item is not None else None

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        result = accounts.list_access_requests(str(self.direction.currentData()))
        self._fill(result)
        self.report("刷新申请", result)

    def _fill(self, result: CommandResult) -> None:
        items = result.data.get("items", []) if result.ok and result.data else []
        self.table.setRowCount(len(items))
        for index, item in enumerate(items):
            values = [
                str(item.get("request_id", "")),
                str(item.get("device_id", "")),
                str(item.get("requester", "")),
                SCOPE_LABELS.get(str(item.get("scope")), str(item.get("scope"))),
                str(item.get("reason", "")),
                _status_text(str(item.get("status", ""))),
            ]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()
        self.table.setEnabled(bool(items))
        self.content_stack.setCurrentIndex(1 if items else 0)
        if not items:
            incoming = str(self.direction.currentData()) == "incoming"
            self.empty.title_label.setText("没有待处理的申请" if incoming else "还没有发起过申请")
            self.empty.desc_label.setText(
                "当其他账号申请控制你的设备时, 会在这里出现; 批准后生成限时或一次性授权."
                if incoming
                else "在下方填写设备码、权限范围与理由, 提交后可在「我发起的」中查看状态."
            )
        self.status_label.setText(result.message if result.ok else f"{result.message} · 未登录")


def _status_text(status: str) -> str:
    """申请状态中文化."""
    return {
        "pending": "待审批",
        "approved": "已批准",
        "rejected": "已拒绝",
        "expired": "已过期",
    }.get(status, status)
