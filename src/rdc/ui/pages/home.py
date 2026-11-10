# ==========================================================================================
# 文件        : home.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 首页 / 连接页: 本机设备码、被控启停、对端连接与最近会话
# 架构定位    : 界面层, 只调用 rdc.commands.api
# 核心功能    : 启动被控、刷新口令、发起连接并轮询进度、展示最近会话
# 职责边界    : 不生成口令; 不判定连接成功; 不写会话状态
# ==========================================================================================

"""首页 / 连接页."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api
from rdc.ui import icons
from rdc.ui.components import EmptyState, chip, fade_in
from rdc.ui.pages.base import PageBase, row
from rdc.ui.theme import active


class HomePage(PageBase):
    """连接与最近会话."""

    connection_started = Signal(str, str)
    session_opened = Signal(str, str)

    def __init__(self) -> None:
        super().__init__(
            "home",
            "首页 / 连接",
            "本机设备码与临时口令由命令层生成; 界面只收集参数并呈现返回结果.",
            scrollable=True,
        )
        self._task_id: str | None = None
        self._recent_items: list[dict[str, object]] = []
        self._build()
        self.body.setSpacing(12)
        self._apply_compact()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(600)
        self._poll_timer.timeout.connect(self._poll_progress)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _apply_compact(self) -> None:
        compact = self.width() < 1000
        if self.device_id_label.property("compact") == compact:
            return
        self.device_id_label.setProperty("compact", compact)
        style = self.device_id_label.style()
        style.unpolish(self.device_id_label)
        style.polish(self.device_id_label)
        self.device_id_label.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_compact()

    def _build(self) -> None:
        self.body.addWidget(self._build_hero())
        self.body.addWidget(self._build_connect_card())
        self.body.addWidget(self._build_recent_card(), 1)

    # ---------------------------------------------------------------- 本机
    def _build_hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("hero")
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(26, 22, 22, 22)
        layout.setSpacing(24)

        left = QVBoxLayout()
        left.setSpacing(6)
        eyebrow = QLabel("THIS DEVICE · 本机")
        eyebrow.setObjectName("eyebrow")
        left.addWidget(eyebrow)
        self.device_id_label = QLabel("-")
        self.device_id_label.setObjectName("heroCode")
        self.device_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        left.addWidget(self.device_id_label)
        left.addWidget(self.muted("把这串设备码发给对方, 配合一次性口令即可发起连接."))
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.host_chip = chip("被控未启动")
        chips.addWidget(self.host_chip)
        chips.addWidget(chip("端到端加密", "accent"))
        chips.addWidget(chip("P2P 优先"))
        chips.addStretch(1)
        left.addLayout(chips)
        left.addStretch(1)
        layout.addLayout(left, 1)

        panel = QFrame()
        panel.setObjectName("heroPanel")
        panel.setFixedWidth(360)
        side = QVBoxLayout(panel)
        side.setContentsMargins(18, 16, 18, 16)
        side.setSpacing(4)
        caption = QLabel("一次性口令")
        caption.setObjectName("heroLabel")
        side.addWidget(caption)
        self.access_code_label = QLabel("未启动")
        self.access_code_label.setObjectName("bigCode")
        side.addWidget(self.access_code_label)
        self.expires_label = QLabel("口令有效期至 -")
        self.expires_label.setObjectName("cardMeta")
        side.addWidget(self.expires_label)
        side.addSpacing(8)
        side.addLayout(
            row(
                self.button("启动被控", self._on_start_hosting, primary=True, icon="plug-zap"),
                self.button("停止被控", self._on_stop_hosting, icon="square"),
                self.button("刷新口令", self._on_refresh_code, icon="refresh-cw"),
            )
        )
        layout.addWidget(panel)
        return hero

    # ---------------------------------------------------------------- 连接
    def _build_connect_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap("link-2", active()["accent"], 18))
        head.addWidget(icon_label)
        title = QLabel("连接远程设备")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addWidget(chip("直连优先 · 失败自动降级中继"))
        head.addStretch(1)
        layout.addLayout(head)
        layout.addWidget(self.muted("输入对端设备码与对方给出的一次性口令; 连接进度以命令返回为准."))

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        peer_label = QLabel("对端设备码")
        peer_label.setObjectName("formLabel")
        code_label = QLabel("一次性口令")
        code_label.setObjectName("formLabel")
        grid.addWidget(peer_label, 0, 0)
        grid.addWidget(code_label, 0, 1)
        self.peer_input = QLineEdit()
        self.peer_input.setPlaceholderText("20 位设备码, 例如 ABCDEFGH23456789JKLM")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("6 位一次性口令")
        grid.addWidget(self.peer_input, 1, 0)
        grid.addWidget(self.code_input, 1, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.button("连接", self._on_connect, primary=True, icon="link-2"))
        actions.addWidget(self.button("取消连接", self._on_cancel, icon="x"))
        self.connect_status = QLabel("未连接")
        self.connect_status.setObjectName("muted")
        actions.addWidget(self.connect_status, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 4)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(220)
        self.progress_bar.setTextVisible(False)
        actions.addWidget(self.progress_bar)
        layout.addLayout(actions)
        return card

    # ---------------------------------------------------------------- 最近
    def _build_recent_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("最近会话")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.muted("双击重新进入会话窗"))
        layout.addLayout(head)

        self.recent_stack = QStackedWidget()
        self.recent_stack.addWidget(
            EmptyState(
                "session",
                "还没有最近会话",
                "建立连接后, 最近使用的设备会出现在这里. 双击列表项即可再次进入会话窗.",
            )
        )
        self.recent_list = QListWidget()
        self.recent_list.setMinimumHeight(118)
        self.recent_list.itemDoubleClicked.connect(self._on_recent_activated)
        self.recent_stack.addWidget(self.recent_list)
        layout.addWidget(self.recent_stack, 1)
        return card

    # ---------------------------------------------------------------- 命令
    def _on_start_hosting(self) -> None:
        result = api.start_hosting("本机")
        if result.ok and result.data:
            self.access_code_label.setText(str(result.data.get("access_code", "-")))
            self.expires_label.setText(f"口令有效期至 {result.data.get('expires_at', '-')}")
            self._set_host_state("被控已启动", "ok")
        self.report("启动被控", result)

    def _on_stop_hosting(self) -> None:
        result = api.stop_hosting()
        if result.ok:
            self.access_code_label.setText("未启动")
            self.expires_label.setText("口令有效期至 -")
            self._set_host_state("被控未启动", "")
        self.report("停止被控", result)

    def _on_refresh_code(self) -> None:
        result = api.refresh_access_code()
        if result.ok and result.data:
            self.access_code_label.setText(str(result.data.get("access_code", "-")))
            self.expires_label.setText(f"口令有效期至 {result.data.get('expires_at', '-')}")
            self._set_host_state("被控已启动", "ok")
        self.report("刷新口令", result)

    def _on_connect(self) -> None:
        peer = self.peer_input.text().strip()
        result = api.start_connection(peer, self.code_input.text().strip())
        if result.ok and result.data:
            self._task_id = str(result.data.get("task_id"))
            self.progress_bar.setRange(0, int(result.data.get("total", 4)))
            self.progress_bar.setValue(0)
            self.connect_status.setText(f"连接中 · 任务 {self._task_id}")
            self._poll_timer.start()
            self.connection_started.emit(peer, self._task_id)
        else:
            self.connect_status.setText(result.message)
        self.report("发起连接", result)

    def _on_recent_activated(self, item: QListWidgetItem) -> None:
        """双击最近会话进入会话窗; 无会话时空行不响应."""
        index = self.recent_list.row(item)
        if 0 <= index < len(self._recent_items):
            entry = self._recent_items[index]
            self.session_opened.emit(str(entry.get("session_id", "")), str(entry.get("peer_device_id", "")))

    def _on_cancel(self) -> None:
        if self._task_id is None:
            self.connect_status.setText("当前没有连接任务")
            return
        self._poll_timer.stop()
        self.report("取消连接", api.cancel_connection(self._task_id))

    def _poll_progress(self) -> None:
        if self._task_id is None:
            return
        result = api.get_connection_progress(self._task_id)
        if result.ok and result.data:
            self.progress_bar.setMaximum(int(result.data.get("total", 4)))
            self.progress_bar.setValue(int(result.data.get("completed", 0)))
            self.connect_status.setText(result.message)
            if result.data.get("status") in {"done", "failed", "canceled"}:
                self._poll_timer.stop()
        self.report("连接进度", result)

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        self.device_id_label.setText(api.get_service().identity.device_id)
        self.recent_list.clear()
        result = api.list_sessions()
        items = result.data.get("items", []) if result.ok and result.data else []
        self._recent_items = list(items)
        for item in items:
            entry = QListWidgetItem(
                f"{item.get('peer_device_id')} · {item.get('transport_kind')} · {item.get('quality_profile')}"
            )
            entry.setIcon(icons.icon("monitor", active()["muted"], 16))
            entry.setSizeHint(QSize(0, 44))
            self.recent_list.addItem(entry)
        self.recent_stack.setCurrentIndex(1 if items else 0)

    def _set_host_state(self, text: str, tone: str) -> None:
        self.host_chip.setText(text)
        if tone:
            self.host_chip.setProperty("tone", tone)
        else:
            self.host_chip.setProperty("tone", "")
        self.host_chip.style().unpolish(self.host_chip)
        self.host_chip.style().polish(self.host_chip)


def build_home() -> QWidget:
    """供装配处使用的最小工厂."""
    return HomePage()
