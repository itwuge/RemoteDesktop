# ==========================================================================================
# 文件        : devices.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 设备列表页: 真实命令驱动的设备卡片、筛选搜索、信任切换与在线轮询
# 架构定位    : 界面层, 只调用 rdc.commands.accounts 与 rdc.commands.api
# 核心功能    : 加载/空/失败/卡片四态、15 秒在线轮询、右键菜单、设备编号复制
# 职责边界    : 不判定权限; 不生成临时口令; 不缓存令牌; 不改写命令返回文案
# ==========================================================================================

"""设备列表页."""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import accounts, api
from rdc.core.result import CommandResult
from rdc.ui.components import (
    CopyButton,
    DeviceCard,
    EmptyState,
    confirm,
    fade_in,
    metric_card,
)
from rdc.ui.copy import error_text
from rdc.ui.pages.base import PageBase, row

DEVICE_ID_PATTERN = re.compile(r"^[A-Z2-7]{20}$")
ACCESS_CODE_PATTERN = re.compile(r"^\d{6}$")

LOADING, EMPTY, FAILED, CARDS = range(4)

POLL_INTERVAL_MS = 15_000
FILTERS = (("全部", "all"), ("在线", "online"), ("离线", "offline"))
GRID_COLUMNS = 2


class DevicesPage(PageBase):
    """设备卡片、筛选搜索与信任切换."""

    connection_started = Signal(str, str)

    def __init__(self) -> None:
        super().__init__(
            "devices",
            "设备列表",
            "同一账号可绑定多台设备；受信设备连接时免连接码，可随时撤销信任。",
            scrollable=True,
        )
        self._items: list[dict[str, Any]] = []
        self._items_by_id: dict[str, dict[str, Any]] = {}
        self._selected_id: str | None = None
        self._filter = "all"
        self._current_id = ""
        self._pending_unbind: str | None = None
        self._cards: dict[str, DeviceCard] = {}
        self._build()
        self.body.setSpacing(12)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._on_poll)
        self._watched: QObject | None = None

    # ---------------------------------------------------------------- 生命周期
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)
        top = self.window()
        if top is not self and self._watched is not top:
            top.installEventFilter(self)
            self._watched = top
        self._sync_poll()

    def hideEvent(self, event: QHideEvent) -> None:
        self._poll_timer.stop()
        super().hideEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """窗口失焦即停轮询, 回到前台且页面可见时恢复."""
        if event.type() in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate):
            self._sync_poll()
        return super().eventFilter(watched, event)

    def _sync_poll(self) -> None:
        """只在页面可见且窗口处于前台时开启 15 秒轮询."""
        active = self.isVisible() and self.window().isActiveWindow()
        if active and not self._poll_timer.isActive():
            self._poll_timer.start()
        elif not active:
            self._poll_timer.stop()

    def _on_poll(self) -> None:
        if not self.isVisible() or not self.window().isActiveWindow():
            self._poll_timer.stop()
            return
        result = accounts.list_devices()
        self._fill(result, quiet=True)
        self.report("刷新设备", result)

    def _build(self) -> None:
        self.header_actions.addWidget(self.button("绑定设备", self._open_bind_dialog, primary=True, icon="plus"))

        stats = QHBoxLayout()
        stats.setSpacing(10)
        total_card, self.total_value = metric_card("全部设备", "0")
        online_card, self.online_value = metric_card("在线", "0")
        trusted_card, self.trusted_value = metric_card("受信", "0")
        for card in (total_card, online_card, trusted_card):
            stats.addWidget(card, 1)
        self.body.addLayout(stats)

        self.status_label = QLabel("未登录 · 请先在账号页登录")
        self.status_label.setObjectName("muted")
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

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 9, 12, 9)
        toolbar_layout.setSpacing(6)
        self.filter_group = QButtonGroup(toolbar)
        self.filter_group.setExclusive(True)
        self.filter_buttons: list[QPushButton] = []
        for index, (label, _value) in enumerate(FILTERS):
            pill = QPushButton(label)
            pill.setProperty("seg", True)
            pill.setCheckable(True)
            pill.setChecked(index == 0)
            self.filter_group.addButton(pill, index)
            self.filter_buttons.append(pill)
            toolbar_layout.addWidget(pill)
        self.filter_group.idClicked.connect(self._on_filter_clicked)
        toolbar_layout.addStretch(1)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索设备名或设备编号")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._render_cards)
        toolbar_layout.addWidget(self.search_input, 1)
        self.body.addWidget(toolbar)

        self.content_stack = QStackedWidget()

        self.loading = QLabel("正在读取设备列表…")
        self.loading.setObjectName("muted")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.setMinimumHeight(180)
        self.content_stack.addWidget(self.loading)

        self.empty = EmptyState(
            "devices",
            "还没有绑定设备",
            "登录账号后输入设备编号与临时连接码, 即可把远端的电脑、笔记本或手机加入设备列表.",
            action_text="去绑定第一台设备",
            action_icon="plus",
            action=self._focus_bind,
        )
        self.content_stack.addWidget(self.empty)

        self.failed = EmptyState(
            "devices",
            "无法读取设备列表",
            "命令层当前不可用或尚未登录. 请先在账号页完成登录, 再回到这里重试.",
            action_text="重试",
            action_icon="refresh-cw",
            action=self.refresh,
        )
        self.content_stack.addWidget(self.failed)

        self.cards_host = QWidget()
        self.cards_host.setObjectName("scrollBody")
        self.cards_grid = QGridLayout(self.cards_host)
        self.cards_grid.setContentsMargins(0, 0, 8, 0)
        self.cards_grid.setHorizontalSpacing(12)
        self.cards_grid.setVerticalSpacing(12)
        self.cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.content_stack.addWidget(self.cards_host)
        self.body.addWidget(self.content_stack, 1)

        self.bind_hint = QLabel("")
        self.bind_hint.setObjectName("muted")
        self.bind_hint.setWordWrap(True)

        self.trust_button = self.button("信任此设备", self._on_toggle_trust, icon="shield-check")
        self.unbind_button = self.button("解绑选中设备", self._on_unbind, icon="trash")
        self.body.addLayout(
            row(
                self.trust_button,
                self.unbind_button,
                self.bind_hint,
            )
        )

        self.unbind_confirm = QFrame()
        self.unbind_confirm.setObjectName("statusStrip")
        confirm_layout = QHBoxLayout(self.unbind_confirm)
        confirm_layout.setContentsMargins(14, 8, 14, 8)
        confirm_layout.setSpacing(8)
        self.unbind_confirm_label = QLabel("")
        self.unbind_confirm_label.setObjectName("muted")
        self.unbind_confirm_label.setWordWrap(True)
        confirm_layout.addWidget(self.unbind_confirm_label, 1)
        confirm_layout.addWidget(self.button("确认解绑", self._on_confirm_unbind, primary=True, icon="trash"))
        confirm_layout.addWidget(self.button("取消", self._cancel_unbind, icon="x"))
        self.unbind_confirm.setVisible(False)
        self.body.addWidget(self.unbind_confirm)

        self._build_bind_dialog()
        self.body.addStretch(1)

    def _build_bind_dialog(self) -> None:
        """绑定设备对话框: 设备编号与一次性口令, 确认后复用 _on_bind."""
        dialog = QDialog(self)
        dialog.setWindowTitle("绑定设备")
        dialog.setMinimumWidth(420)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setSpacing(8)
        self.bind_device_input = QLineEdit()
        self.bind_device_input.setPlaceholderText("20 位设备编号, 大写字母与数字 2-7")
        self.bind_device_input.setMaxLength(20)
        self.bind_code_input = QLineEdit()
        self.bind_code_input.setPlaceholderText("6 位一次性口令")
        self.bind_code_input.setMaxLength(6)
        form.addRow("设备编号", self.bind_device_input)
        form.addRow("一次性口令", self.bind_code_input)
        layout.addLayout(form)
        self.bind_dialog_hint = QLabel("")
        self.bind_dialog_hint.setObjectName("badgeWarn")
        self.bind_dialog_hint.setWordWrap(True)
        layout.addWidget(self.bind_dialog_hint)
        buttons = QDialogButtonBox()
        accept = buttons.addButton("绑定", QDialogButtonBox.ButtonRole.AcceptRole)
        accept.setProperty("primary", True)
        accept.setDefault(True)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept_bind)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        self.bind_dialog = dialog

    def _open_bind_dialog(self) -> None:
        self.bind_device_input.clear()
        self.bind_code_input.clear()
        self.bind_dialog_hint.clear()
        self.bind_dialog.exec()

    def _accept_bind(self) -> None:
        if self._on_bind():
            self.bind_dialog.accept()

    def _on_filter_clicked(self, index: int) -> None:
        self._filter = FILTERS[index][1]
        self._render_cards()

    def _focus_bind(self) -> None:
        self._open_bind_dialog()

    def _set_hint(self, text: str, *, ok: bool = False) -> None:
        """就地提示绑定/开关结果; 文案不改写命令返回."""
        for label in (self.bind_hint, self.bind_dialog_hint):
            label.setText(text)
            label.setObjectName("badgeOk" if ok else "badgeWarn")
            label.style().unpolish(label)
            label.style().polish(label)
            label.update()

    def _show_error(self, result: CommandResult) -> None:
        """错误条只显示中文原因, 并提供一键复制."""
        reason = error_text(result.error_code, result.message)
        self.error_label.setText(reason)
        self.error_copy.set_value(reason, "复制错误原因")
        self.error_strip.setVisible(True)

    def _clear_error(self) -> None:
        self.error_strip.setVisible(False)

    def _set_state(self, state: int) -> None:
        self.content_stack.setCurrentIndex(state)

    # ---------------------------------------------------------------- 命令
    def _on_bind(self) -> bool:
        device_id = self.bind_device_input.text().strip().upper()
        access_code = self.bind_code_input.text().strip()
        self.bind_device_input.setText(device_id)
        if not DEVICE_ID_PATTERN.fullmatch(device_id):
            self._set_hint("设备编号应为 20 位大写字母或数字, 且只含 2-7")
            self.bind_device_input.setFocus()
            return False
        if not ACCESS_CODE_PATTERN.fullmatch(access_code):
            self._set_hint("一次性口令应为 6 位数字")
            self.bind_code_input.setFocus()
            return False
        result = accounts.bind_device(device_id, access_code)
        self.report("绑定设备", result)
        if not result.ok:
            self._set_hint(error_text(result.error_code, result.message))
            self._show_error(result)
            return False
        self.bind_device_input.clear()
        self.bind_code_input.clear()
        self._set_hint(f"绑定成功: {device_id}", ok=True)
        self.refresh()
        return True

    def _on_unbind(self) -> None:
        device_id = self._selected_device()
        if device_id is None:
            self._set_hint("请先选择一台设备")
            return
        self._pending_unbind = device_id
        self.unbind_confirm_label.setText(f"确认解绑 {device_id}？解绑会吊销该设备全部令牌并踢出在线会话，且不可撤销。")
        self.unbind_confirm.setVisible(True)

    def _on_confirm_unbind(self) -> None:
        device_id = self._pending_unbind
        if device_id is None:
            return
        result = accounts.unbind_device(device_id)
        self.report("解绑设备", result)
        if not result.ok:
            self._show_error(result)
        self._cancel_unbind()
        self._set_hint(error_text(result.error_code, result.message), ok=result.ok)
        self.refresh()

    def _cancel_unbind(self) -> None:
        self._pending_unbind = None
        self.unbind_confirm.setVisible(False)

    def _on_toggle_trust(self) -> None:
        device_id = self._selected_device()
        if device_id is None:
            self._set_hint("请先选择一台设备")
            return
        current = self._items_by_id.get(device_id, {})
        # 目标信任状态与当前 require_confirm 相反: 当前需确认则本次设为受信
        self._apply_trust(device_id, bool(current.get("require_confirm", True)))

    def _apply_trust(self, device_id: str, trusted: bool) -> None:
        """切换信任前二次确认; 结果以回读的 require_confirm 为准."""
        if trusted:
            prompt = f"确认把设备 {device_id} 设为受信？受信后同账号连接时免连接码, 并记入审计."
        else:
            prompt = f"确认撤销设备 {device_id} 的信任？撤销后再次连接需要输入连接码."
        # 页面不可见(无头测试与自动化)时没有可交互的确认入口, 不弹模态阻塞
        if self.isVisible() and not confirm(
            self, "信任设置", prompt, "确认设置" if trusted else "取消信任", danger=not trusted
        ):
            return
        result = accounts.set_trust_policy(device_id, not trusted)
        self.report("切换信任", result)
        if not result.ok:
            self._set_hint(error_text(result.error_code, result.message))
            self._show_error(result)
            return
        self.refresh()
        stored = bool(self._items_by_id.get(device_id, {}).get("require_confirm", True))
        if stored == trusted:
            expected = "受信" if trusted else "需连接码"
            actual = "受信" if not stored else "需连接码"
            self._set_hint(f"信任状态写入未生效: 期望 {expected}, 回读 {actual}")
            return
        text = "已信任此设备, 同账号连接免连接码并记入审计" if trusted else "已撤销信任, 再次连接需要输入连接码"
        self._set_hint(f"{text}（已落盘并回读确认）", ok=True)

    def _on_connect(self, device_id: str) -> None:
        """从设备卡连接: 受信设备直接交由命令层判定免码, 非受信先收集连接码."""
        device = self._items_by_id.get(device_id, {})
        if not device:
            self._set_hint("设备不存在, 请刷新后重试")
            return
        if device_id == self._current_id:
            self._set_hint("不能连接本机")
            return
        code = ""
        if not bool(device.get("trusted")):
            asked = self._ask_access_code(device_id)
            if asked is None:
                return
            code = asked
        result = api.start_connection(device_id, code)
        self.report("连接设备", result)
        if not result.ok:
            self._set_hint(error_text(result.error_code, result.message))
            self._show_error(result)
            return
        task_id = str((result.data or {}).get("task_id", ""))
        self._set_hint(f"已发起连接: {device_id}", ok=True)
        self.connection_started.emit(device_id, task_id)

    def _ask_access_code(self, device_id: str) -> str | None:
        """非受信设备连接码对话框; 连接码只提交不缓存."""
        dialog = QDialog(self)
        dialog.setWindowTitle("输入连接码")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(QLabel(f"连接 {device_id} 需要对方给出的 6 位连接码."))
        editor = QLineEdit()
        editor.setPlaceholderText("6 位连接码")
        editor.setMaxLength(6)
        layout.addWidget(editor)
        buttons = QDialogButtonBox()
        accept = buttons.addButton("连接", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        accept.setProperty("primary", True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        code = editor.text().strip()
        return code if ACCESS_CODE_PATTERN.fullmatch(code) else None

    def _request_remove(self, device_id: str) -> None:
        """设备卡「删除」: 二次确认后解绑, 不卸载对端."""
        if not confirm(
            self,
            "删除设备",
            f"确认从列表删除设备 {device_id}？只会解绑并移出列表，不会卸载对端，且不可撤销。",
            "删除",
        ):
            return
        result = accounts.unbind_device(device_id)
        self.report("删除设备", result)
        self._set_hint(error_text(result.error_code, result.message), ok=result.ok)
        if not result.ok:
            self._show_error(result)
        self.refresh()

    def _selected_device(self) -> str | None:
        return self._selected_id

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        self._set_state(LOADING)
        current = accounts.get_account_state()
        self._current_id = str((current.data or {}).get("device_id") or "") if current.ok else ""
        result = accounts.list_devices()
        self._fill(result)

    def _fill(self, result: CommandResult, *, quiet: bool = False) -> None:
        if not result.ok or result.data is None:
            self._items = []
            self._items_by_id = {}
            self._selected_id = None
            self._cancel_unbind()
            self._render_cards()
            self._update_stats()
            reason = error_text(result.error_code, result.message)
            self.status_label.setText(reason)
            self._show_error(result)
            self.failed.desc_label.setText(f"{reason}. 请先在账号页完成登录, 或确认后端可用后重试.")
            if not quiet:
                self._set_state(FAILED)
            return

        items = result.data.get("items", [])
        self._items = [dict(item) for item in items]
        self._items_by_id = {str(item.get("device_id", "")): item for item in self._items}
        if self._selected_id not in self._items_by_id:
            self._selected_id = None
        self._clear_error()
        self._render_cards()
        self._update_stats()
        self.status_label.setText(result.message)
        if not quiet:
            self._set_state(CARDS if self._items else EMPTY)

    def _visible_items(self) -> list[dict[str, Any]]:
        mode = self._filter
        query = self.search_input.text().strip().lower()
        items = []
        for item in self._items:
            if mode == "online" and not item.get("online"):
                continue
            if mode == "offline" and item.get("online"):
                continue
            if query:
                haystack = f"{item.get('display_name', '')} {item.get('device_id', '')}".lower()
                if query not in haystack:
                    continue
            items.append(item)
        items.sort(key=lambda item: (not bool(item.get("online")), str(item.get("display_name") or "")))
        return items

    def _render_cards(self) -> None:
        while self.cards_grid.count():
            layout_item = self.cards_grid.takeAt(0)
            widget = layout_item.widget() if layout_item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._cards.clear()
        row_index = 0
        groups = (("在线", True), ("离线", False))
        for title, online in groups:
            group = [item for item in self._visible_items() if bool(item.get("online")) is online]
            header = QLabel(f"{title} {len(group)} 台")
            header.setObjectName("sectionTitle")
            self.cards_grid.addWidget(header, row_index, 0, 1, GRID_COLUMNS)
            row_index += 1
            for index, device in enumerate(group):
                card = DeviceCard(device, current=str(device.get("device_id", "")) == self._current_id)
                card.clicked.connect(self._select_device)
                card.connect_requested.connect(self._on_connect)
                card.trust_requested.connect(self._apply_trust)
                card.remove_requested.connect(self._request_remove)
                card.set_selected(card.device_id == self._selected_id)
                self.cards_grid.addWidget(card, row_index + index // GRID_COLUMNS, index % GRID_COLUMNS)
                self._cards[card.device_id] = card
            row_index += (len(group) + GRID_COLUMNS - 1) // GRID_COLUMNS
        for column in range(GRID_COLUMNS):
            self.cards_grid.setColumnStretch(column, 1)
        self._sync_trust_button()

    def _update_stats(self) -> None:
        self.total_value.setText(str(len(self._items)))
        self.online_value.setText(str(sum(1 for item in self._items if item.get("online"))))
        self.trusted_value.setText(str(sum(1 for item in self._items if item.get("trusted"))))

    def _select_device(self, device_id: str) -> None:
        self._selected_id = device_id
        for key, card in self._cards.items():
            card.set_selected(key == device_id)
        self._sync_trust_button()
        if device_id:
            self.status_label.setText(f"已选择 {device_id}")

    def _sync_trust_button(self) -> None:
        """按当前选中设备同步按钮可用性: 未选中时两个动作置灰."""
        device = self._items_by_id.get(self._selected_id or "", {})
        selected = bool(device)
        trusted = selected and not bool(device.get("require_confirm", True))
        self.trust_button.setText("取消信任" if trusted else "信任此设备")
        self.trust_button.setEnabled(selected)
        self.unbind_button.setEnabled(selected)
