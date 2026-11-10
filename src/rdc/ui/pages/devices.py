# ==========================================================================================
# 文件        : devices.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 设备列表页: 卡片式设备、归属、在线状态、免确认开关、绑定与解绑
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 刷新设备、切换免确认、绑定 / 解绑
# 职责边界    : 不判定权限; 不生成临时口令; 不缓存令牌
# ==========================================================================================

"""设备列表页."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QStackedWidget,
    QWidget,
)

from rdc.commands import accounts
from rdc.core.result import CommandResult
from rdc.ui.components import DeviceCard, EmptyState, fade_in, metric_card
from rdc.ui.pages.base import PageBase, row


class DevicesPage(PageBase):
    """设备卡片与免确认开关."""

    def __init__(self) -> None:
        super().__init__(
            "devices",
            "设备列表",
            "同一账号可绑定多台设备; 免确认开关按设备生效, 关闭后同账号连接也需审批.",
            scrollable=True,
        )
        self._items: list[dict[str, Any]] = []
        self._items_by_id: dict[str, dict[str, Any]] = {}
        self._selected_id: str | None = None
        self._cards: dict[str, DeviceCard] = {}
        self._page = 0
        self._page_size = 9
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        self.header_actions.addWidget(self.button("刷新", self.refresh, primary=True, icon="refresh-cw"))

        stats = QHBoxLayout()
        stats.setSpacing(10)
        total_card, self.total_value = metric_card("已绑定设备", "0")
        online_card, self.online_value = metric_card("在线设备", "0")
        pending_card, self.pending_value = metric_card("需审批设备", "0")
        stats.addWidget(total_card)
        stats.addWidget(online_card)
        stats.addWidget(pending_card)
        stats.addStretch(1)
        self.body.addLayout(stats)

        self.status_label = QLabel("未登录 · 请先在登录 / 注册页登录")
        self.status_label.setObjectName("muted")

        self.content_stack = QStackedWidget()
        self.empty = EmptyState(
            "devices",
            "还没有绑定设备",
            "登录账号后输入设备码与临时连接码, 即可把远端的电脑、笔记本或手机加入设备列表.",
            action_text="去绑定第一台设备",
            action_icon="plus",
            action=self._focus_bind,
        )
        self.content_stack.addWidget(self.empty)

        self.cards_host = QWidget()
        self.cards_host.setObjectName("scrollBody")
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 8, 0)
        self.cards_grid.setHorizontalSpacing(12)
        self.cards_grid.setVerticalSpacing(12)
        self.cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_stack.addWidget(self.cards_host)
        self.body.addWidget(self.content_stack, 1)

        self.pager = QWidget()
        pager_layout = QHBoxLayout(self.pager)
        pager_layout.setContentsMargins(0, 0, 0, 0)
        pager_layout.setSpacing(8)
        self.page_label = QLabel("")
        self.page_label.setObjectName("muted")
        pager_layout.addWidget(self.page_label)
        pager_layout.addStretch(1)
        self.prev_page = self.button("上一页", self._prev_page)
        self.next_page = self.button("下一页", self._next_page)
        pager_layout.addWidget(self.prev_page)
        pager_layout.addWidget(self.next_page)
        self.pager.setVisible(False)
        self.body.addWidget(self.pager)

        self.body.addLayout(
            row(
                self.button("切换免确认", self._on_toggle_trust, icon="shield-check"),
                self.button("解绑选中设备", self._on_unbind, icon="trash"),
                self.status_label,
            )
        )

        bind_box, bind_layout = self.form_section("绑定新设备")
        self.bind_device_input = QLineEdit()
        self.bind_device_input.setPlaceholderText("20 位设备码")
        self.bind_code_input = QLineEdit()
        self.bind_code_input.setPlaceholderText("6 位临时连接码")
        bind_layout.addRow("设备码", self.bind_device_input)
        bind_layout.addRow("临时连接码", self.bind_code_input)
        bind_layout.addRow(row(self.button("绑定", self._on_bind, primary=True, icon="plus")))
        self.body.addWidget(bind_box)
        self.body.addStretch(1)

    def _focus_bind(self) -> None:
        self.bind_device_input.setFocus()

    # ---------------------------------------------------------------- 命令
    def _on_bind(self) -> None:
        result = accounts.bind_device(self.bind_device_input.text().strip(), self.bind_code_input.text().strip())
        self.report("绑定设备", result)
        if result.ok:
            self.refresh()

    def _on_unbind(self) -> None:
        device_id = self._selected_device()
        if device_id is None:
            self.status_label.setText("请先选择一台设备")
            return
        result = accounts.unbind_device(device_id)
        self.report("解绑设备", result)
        self.refresh()

    def _on_toggle_trust(self) -> None:
        device_id = self._selected_device()
        if device_id is None:
            self.status_label.setText("请先选择一台设备")
            return
        current = self._items_by_id.get(device_id, {})
        require_confirm = bool(current.get("require_confirm"))
        self.report("切换免确认", accounts.set_trust_policy(device_id, require_confirm))
        self.refresh()

    def _selected_device(self) -> str | None:
        return self._selected_id

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        result = accounts.list_devices()
        self._fill(result)
        self.report("刷新设备", result)

    def _fill(self, result: CommandResult) -> None:
        items = result.data.get("items", []) if result.ok and result.data else []
        self._items = [dict(item) for item in items]
        self._items_by_id = {str(item.get("device_id", "")): item for item in self._items}
        if self._selected_id not in self._items_by_id:
            self._selected_id = None
        self._page = 0
        self._render_cards()
        self._update_stats()
        self.status_label.setText(result.message)
        self.content_stack.setCurrentIndex(1 if self._items else 0)
        if not self._items:
            self.empty.title_label.setText("还没有绑定设备")
            self.empty.desc_label.setText(
                "登录账号后输入设备码与临时连接码, 即可把远端的电脑、笔记本或手机加入设备列表."
                if result.ok
                else "当前未登录或无法读取设备列表. 请先在登录 / 注册页完成登录, 再回到这里刷新."
            )

    def _render_cards(self) -> None:
        while self.cards_grid.count():
            layout_item = self.cards_grid.takeAt(0)
            widget = layout_item.widget() if layout_item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()
        columns = 3
        total = len(self._items)
        pages = max(1, (total + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, pages - 1))
        start = self._page * self._page_size
        for index, device in enumerate(self._items[start : start + self._page_size]):
            card = DeviceCard(device)
            card.clicked.connect(self._select_device)
            card.set_selected(card.device_id == self._selected_id)
            self.cards_grid.addWidget(card, index // columns, index % columns)
            self._cards[card.device_id] = card
        for column in range(columns):
            self.cards_grid.setColumnStretch(column, 1)
        self._update_pager(total, pages)

    def _update_pager(self, total: int, pages: int) -> None:
        self.pager.setVisible(total > self._page_size)
        if total > self._page_size:
            self.page_label.setText(f"第 {self._page + 1} / {pages} 页 · 共 {total} 台")
        self.prev_page.setEnabled(self._page > 0)
        self.next_page.setEnabled(self._page < pages - 1)

    def _prev_page(self) -> None:
        if self._page > 0:
            self._page -= 1
            self._render_cards()

    def _next_page(self) -> None:
        pages = max(1, (len(self._items) + self._page_size - 1) // self._page_size)
        if self._page < pages - 1:
            self._page += 1
            self._render_cards()

    def _update_stats(self) -> None:
        self.total_value.setText(str(len(self._items)))
        self.online_value.setText(str(sum(1 for item in self._items if item.get("online"))))
        self.pending_value.setText(str(sum(1 for item in self._items if item.get("require_confirm"))))

    def _select_device(self, device_id: str) -> None:
        self._selected_id = device_id
        for key, card in self._cards.items():
            card.set_selected(key == device_id)
        if device_id:
            self.status_label.setText(f"已选择 {device_id}")
