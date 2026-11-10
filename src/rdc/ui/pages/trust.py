# ==========================================================================================
# 文件        : trust.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 信任页: 受信设备清单、授信来源与时间、撤销信任
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 受信设备列表卡、在线统计、空/加载/失败态、设备编号复制、撤销信任二次确认
# 职责边界    : 不判定准入结果; 不改写命令返回; 不在界面记录审计内容
# ==========================================================================================

"""信任设备页."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from rdc.commands import accounts
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import (
    PLATFORM_GLYPH,
    CopyButton,
    EmptyState,
    chip,
    confirm,
    fade_in,
    metric_card,
)
from rdc.ui.copy import error_text, platform_text, trusted_source_text
from rdc.ui.pages.base import PageBase
from rdc.ui.theme import active

EMPTY_DESC = "新绑定设备默认需要逐次确认。把常用设备设为信任后，连接时不再需要连接码。"


class TrustRow(QFrame):
    """受信设备一行: 名称、设备编号、平台与授信信息、免确认直连与撤销按钮."""

    revoke_requested = Signal(str)

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__()
        self.device_id = str(item.get("device_id", ""))
        self.setObjectName("card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        platform_key = str(item.get("platform") or "").strip().lower()
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(PLATFORM_GLYPH.get(platform_key, "monitor-off"), active()["muted"], 20))
        glyph.setToolTip(f"平台: {platform_text(platform_key)}")
        layout.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name = QLabel(str(item.get("display_name") or "未命名设备"))
        name.setObjectName("cardTitle")
        name_row.addWidget(name)
        if _online(item):
            name_row.addWidget(chip("在线", "ok"))
        name_row.addStretch(1)
        info.addLayout(name_row)

        code_row = QHBoxLayout()
        code_row.setSpacing(4)
        code = QLabel(self.device_id or "-")
        code.setObjectName("mono")
        code.setProperty("class", "muted")
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code.setToolTip(f"设备编号 {self.device_id}")
        code_row.addWidget(code)
        code_row.addWidget(CopyButton(self.device_id, "复制设备编号"))
        code_row.addStretch(1)
        info.addLayout(code_row)

        meta = QLabel(
            f"{platform_text(platform_key)} · 来源 {trusted_source_text(item.get('trust_source'))} · "
            f"授信时间 {_minute(item.get('trusted_at'))}"
        )
        meta.setObjectName("cardMeta")
        meta.setToolTip("授信来源分为账号授信与设备授信; 授信时间取信任变更审计事件的时间")
        info.addWidget(meta)
        layout.addLayout(info, 1)

        self.toggle = QPushButton("免确认直连")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(True)
        self.toggle.setProperty("seg", True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setToolTip("免确认直连已开启; 关闭后该设备下次连接恢复逐次确认")
        self.toggle.clicked.connect(self._on_toggle)
        layout.addWidget(self.toggle)

        revoke = QPushButton("撤销信任")
        revoke.setProperty("ghost", True)
        revoke.setCursor(Qt.CursorShape.PointingHandCursor)
        icons.apply_icon(revoke, "shield-off", tone="danger", size=14)
        revoke.setToolTip(f"撤销设备 {self.device_id} 的信任, 撤销后立即生效")
        revoke.clicked.connect(lambda: self.revoke_requested.emit(self.device_id))
        layout.addWidget(revoke)

    def _on_toggle(self, checked: bool) -> None:
        """关闭免确认直连等同于撤销信任; 待确认或刷新前先复位显示."""
        if checked:
            return
        self.toggle.setChecked(True)
        self.revoke_requested.emit(self.device_id)


class TrustPage(PageBase):
    """信任管理."""

    def __init__(self) -> None:
        super().__init__(
            "trust",
            "信任设备",
            "受信设备连接时免逐次确认；撤销信任立即生效，变更写入审计并出现在消息中心。",
            scrollable=True,
        )
        self._trusted: list[dict[str, Any]] = []
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        nav = self.button("设备列表", lambda: self.navigate.emit("devices"), icon="monitor")
        nav.setProperty("ghost", True)
        self.header_actions.addWidget(nav)

        self.info_strip = QFrame()
        self.info_strip.setObjectName("statusStrip")
        info_layout = QHBoxLayout(self.info_strip)
        info_layout.setContentsMargins(14, 10, 14, 10)
        info_layout.setSpacing(9)
        info_icon = QLabel()
        info_icon.setPixmap(icons.pixmap("shield-check", active()["accent"], 18))
        info_layout.addWidget(info_icon, 0, Qt.AlignmentFlag.AlignTop)
        info_text = QVBoxLayout()
        info_text.setSpacing(2)
        self.info_count = QLabel("当前 0 台设备处于免确认直连")
        self.info_count.setObjectName("cardTitle")
        info_text.addWidget(self.info_count)
        info_note = QLabel("新绑定设备默认需要逐次确认；撤销信任后，该设备下次连接恢复逐次确认。")
        info_note.setObjectName("cardMeta")
        info_note.setWordWrap(True)
        info_text.addWidget(info_note)
        info_layout.addLayout(info_text, 1)
        self.body.addWidget(self.info_strip)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        trusted_card, self.trusted_value = metric_card("受信设备", "0")
        online_card, self.online_value = metric_card("其中在线", "0")
        instant_card, self.instant_value = metric_card("撤销立即生效", "立即")
        stats.addWidget(trusted_card, 1)
        stats.addWidget(online_card, 1)
        stats.addWidget(instant_card, 1)
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

        self.card = QFrame()
        self.card.setObjectName("card")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(18, 14, 18, 14)
        card_layout.setSpacing(9)
        head = QHBoxLayout()
        head.setSpacing(8)
        head_icon = QLabel()
        head_icon.setPixmap(icons.pixmap("shield-check", active()["accent"], 16))
        head.addWidget(head_icon)
        title = QLabel("受信设备")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        head_hint = QLabel("来源与授信时间按事件审计展示")
        head_hint.setObjectName("cardMeta")
        head.addWidget(head_hint)
        card_layout.addLayout(head)

        self.loading = QLabel("正在读取受信设备…")
        self.loading.setObjectName("muted")
        self.loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading.setMinimumHeight(120)
        self.loading.setVisible(False)
        card_layout.addWidget(self.loading)

        self.content = QVBoxLayout()
        self.content.setSpacing(8)
        card_layout.addLayout(self.content)

        self.empty = EmptyState(
            "shield",
            "还没有受信设备",
            EMPTY_DESC,
            action_text="去设备列表",
            action_icon="monitor",
            action=lambda: self.navigate.emit("devices"),
        )
        self.empty.setVisible(False)
        card_layout.addWidget(self.empty)
        card_layout.addWidget(self.muted("「来源」取自信任变更审计事件的来源字段, 按账号授信或设备授信展示."))

        self.body.addWidget(self.card)
        self.body.addStretch(1)

    def refresh(self) -> None:
        self.loading.setVisible(True)
        self.empty.setVisible(False)
        result = accounts.list_devices()
        self._fill(result)

    def _fill(self, result: CommandResult) -> None:
        while self.content.count():
            holder = self.content.takeAt(0)
            widget = holder.widget() if holder is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.loading.setVisible(False)
        if not result.ok or result.data is None:
            self._trusted = []
            self._sync_stats()
            reason = error_text(result.error_code, result.message)
            self.status_label.setText(reason)
            self.error_label.setText(reason)
            self.error_copy.set_value(reason, "复制错误原因")
            self.error_strip.setVisible(True)
            self.empty.setVisible(True)
            self.empty.title_label.setText("无法读取信任列表")
            self.empty.desc_label.setText(f"信任关系读取失败, 请稍后重试. 原因: {reason}.")
            return

        items = [dict(item) for item in result.data.get("items", [])]
        self._trusted = [item for item in items if item.get("trusted")]
        self.error_strip.setVisible(False)
        self.status_label.setText(result.message)
        self._sync_stats()
        self.empty.setVisible(not self._trusted)
        if self._trusted:
            self.empty.title_label.setText("还没有受信设备")
            self.empty.desc_label.setText(EMPTY_DESC)
        for item in self._trusted:
            row = TrustRow(item)
            row.revoke_requested.connect(self._on_revoke)
            self.content.addWidget(row)
        self.content.addStretch(1)

    def _sync_stats(self) -> None:
        """统计卡与提示条按当前受信数据同步; 撤销生效时间固定为立即."""
        total = len(self._trusted)
        online = sum(1 for item in self._trusted if _online(item))
        self.trusted_value.setText(str(total))
        self.online_value.setText(str(online))
        self.info_count.setText(f"当前 {total} 台设备处于免确认直连")

    def _on_revoke(self, device_id: str) -> None:
        if not confirm(
            self,
            "撤销信任",
            f"确认撤销设备 {device_id} 的信任？撤销后再次连接需要输入连接码.",
            "撤销信任",
        ):
            return
        result = accounts.set_trust_policy(device_id, True)
        self.report("撤销信任", result)
        if not result.ok:
            reason = error_text(result.error_code, result.message)
            self.error_label.setText(reason)
            self.error_copy.set_value(reason, "复制错误原因")
            self.error_strip.setVisible(True)
            return
        self.status_label.setText(f"已撤销信任: {device_id}")
        self.refresh()


def _online(item: dict[str, Any]) -> bool:
    """在线判定兼容列表返回的 online 与 presence_state 两种字段."""
    if item.get("online"):
        return True
    return str(item.get("presence_state") or "").strip().lower() == "online"


def _minute(value: object) -> str:
    """授信时间只显示到分钟; 空值显示「未知」."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[0:10]} {text[11:16]}"
    return text or "未知"
