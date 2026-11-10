# ==========================================================================================
# 文件        : account.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 账号页: 未登录(登录/注册)与已登录(账号总览)两种状态
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 登录注册、登录态恢复、账号总览、登录与安全、令牌掩码、退出登录
# 职责边界    : 不保存口令; 不显示令牌全文; 不写日志
# ==========================================================================================

"""账号页."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QMouseEvent, QPainter, QPaintEvent, QPen, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import accounts, api
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import CopyButton, Illustration, chip, confirm, fade_in
from rdc.ui.copy import error_text
from rdc.ui.pages.base import PageBase
from rdc.ui.theme import active


def _mask(token: str) -> str:
    """令牌只显示首尾各 4 位."""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def _minute(value: object) -> str:
    """时间显示到分钟; 空值显示「未知」."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[0:10]} {text[11:16]}"
    return text or "未知"


def _remaining(value: object) -> str:
    """会话剩余有效期; 空值表示本机会话不限时."""
    text = str(value or "").strip()
    if not text:
        return "本机会话不限时"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "有效期未知"
    if moment.tzinfo is None:
        moment = moment.astimezone()
    seconds = int((moment - datetime.now(moment.tzinfo)).total_seconds())
    if seconds <= 0:
        return "已过期"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"剩余约 {hours} 小时 {minutes} 分"
    if hours:
        return f"剩余约 {hours} 小时"
    return f"剩余约 {minutes} 分"


LOGIN_NOTE = "登录态以密文保存在本机，退出登录即删除；界面只提交账号与口令，不参与口令摘要与令牌签发。"
REGISTER_NOTE = "注册成功后自动登录。口令摘要、令牌密文与设备密钥均由命令层落库，界面不再参与。"
SIGNED_OUT_SUBTITLE = "登录或注册后可查看账号标识、当前设备、已绑定设备与登录状态。"
SIGNED_IN_SUBTITLE = "账号标识、当前设备、已绑定设备与登录状态；退出登录会吊销本机令牌并清空登录态。"


class _Avatar(QWidget):
    """账号首字母头像: 颜色取自主题令牌, 切换深浅色时随重绘更新."""

    def __init__(self, side: int = 56) -> None:
        super().__init__()
        self._text = "?"
        self.setFixedSize(side, side)

    def set_text(self, text: str) -> None:
        self._text = text.strip()[:1].upper() or "?"
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        painter.setBrush(QColor(token["accent_soft"]))
        painter.setPen(QPen(QColor(token["accent_border"]), 1.0))
        painter.drawEllipse(rect)
        font = QFont(self.font())
        font.setPointSizeF(17.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(token["accent_strong"]))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


class _LinkTile(QFrame):
    """可点击的跳转磁贴: 图标、标题与一行说明, 复用卡片悬停样式."""

    clicked = Signal()

    def __init__(self, glyph: str, title: str, detail: str = "") -> None:
        super().__init__()
        self.setObjectName("deviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap(glyph, active()["accent"], 18))
        layout.addWidget(icon_label)
        column = QVBoxLayout()
        column.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        column.addWidget(title_label)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("cardMeta")
        column.addWidget(self.detail_label)
        layout.addLayout(column, 1)

    def set_detail(self, text: str) -> None:
        self.detail_label.setText(text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


class AccountPage(PageBase):
    """账号入口与已登录总览."""

    def __init__(self) -> None:
        super().__init__(
            "account",
            "账号",
            SIGNED_IN_SUBTITLE,
        )
        self.subtitle_label.setWordWrap(False)
        self._device_token: str | None = None
        self._account_id = ""
        self._device_id = ""
        self._expires_at = ""
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        self.state_stack = QStackedWidget()
        self.state_stack.addWidget(self._build_auth_view())
        self.state_stack.addWidget(self._build_overview())
        self.body.addWidget(self.state_stack, 1)
        self.body.addWidget(self._build_error_strip())

    def _build_auth_view(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        columns = QHBoxLayout()
        columns.setSpacing(13)
        columns.setAlignment(Qt.AlignmentFlag.AlignTop)
        for panel in (self._build_brand_panel(), self._build_form_panel()):
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            columns.addWidget(panel, 1)
        outer.addLayout(columns)
        outer.addStretch(1)
        return page

    def _build_error_strip(self) -> QFrame:
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
        return self.error_strip

    # ---------------------------------------------------------------- 左侧
    def _build_brand_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("authIntro")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        eyebrow = QLabel("登录")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)
        title = QLabel("登录后，设备与信任关系\n跟着账号走")
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        intro = QLabel(
            "同一账号在不同系统的客户端登录后，可在设备列表看到彼此；受信设备连接免确认，会话与信任变更写入审计时间线。"
        )
        intro.setObjectName("authIntroText")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addSpacing(2)
        layout.addWidget(Illustration("auth", 240, 160), 0, Qt.AlignmentFlag.AlignHCenter)
        points = QVBoxLayout()
        points.setSpacing(7)
        for glyph, text in (
            ("monitor", "跨端设备列表与在线状态"),
            ("shield-check", "受信设备免连接码直连"),
            ("activity", "会话与信任变更留痕"),
        ):
            caption_row = QHBoxLayout()
            caption_row.setSpacing(7)
            icon_label = QLabel()
            icon_label.setPixmap(icons.pixmap(glyph, active()["accent"], 13))
            caption_row.addWidget(icon_label)
            caption = QLabel(text)
            caption.setObjectName("authPoint")
            caption_row.addWidget(caption, 1)
            points.addLayout(caption_row)
        layout.addLayout(points)
        return panel

    # ---------------------------------------------------------------- 右侧
    def _build_form_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("authCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(0)
        layout.addWidget(self._build_signed_out())
        return panel

    def _build_signed_out(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        tabs = QFrame()
        tabs.setObjectName("authTabs")
        tabs_layout = QHBoxLayout(tabs)
        tabs_layout.setContentsMargins(3, 3, 3, 3)
        tabs_layout.setSpacing(2)
        self.login_tab = QPushButton("登录")
        self.register_tab = QPushButton("注册")
        for button in (self.login_tab, self.register_tab):
            button.setProperty("authSeg", True)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setFixedHeight(30)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            tabs_layout.addWidget(button, 1)
        self.login_tab.setChecked(True)
        self.login_tab.clicked.connect(lambda: self._set_auth_mode("login"))
        self.register_tab.clicked.connect(lambda: self._set_auth_mode("register"))
        layout.addWidget(tabs)

        layout.addWidget(self._build_auth_form())

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.token_label = QLabel("设备令牌：未签发")
        self.token_label.setObjectName("authNote")
        self.token_label.setToolTip("令牌只显示首尾各 4 位，不提供复制，避免泄露")
        foot.addWidget(self.token_label, 1)
        self.status_label = QLabel("未登录")
        self.status_label.setObjectName("authStateWarn")
        foot.addWidget(self.status_label)
        layout.addLayout(foot)
        return page

    def _build_auth_form(self) -> QWidget:
        """登录与注册共用一套单列表单, 注册时展开确认口令."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        account_field, self.login_account = self._field("账号", "邮箱或账号名", hint="3 至 64 位")
        self.login_account.setToolTip("账号为 3 至 64 位字母、数字或 _ . @ -")
        self.register_account = self.login_account
        password_field, self.login_password = self._field("口令", "请输入口令", password=True, hint="至少 8 位")
        self.register_password = self.login_password
        self.confirm_field, self.register_confirm = self._field("确认口令", "再次输入口令", password=True)
        self.confirm_field.setVisible(False)

        layout.addWidget(account_field)
        layout.addWidget(password_field)
        layout.addWidget(self.confirm_field)

        self.submit_button = self.button("登录", self._on_submit, primary=True)
        self.submit_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.submit_button)

        self.auth_note = QLabel(LOGIN_NOTE)
        self.auth_note.setObjectName("authNote")
        self.auth_note.setWordWrap(True)
        layout.addWidget(self.auth_note)
        return page

    @staticmethod
    def _field(caption: str, placeholder: str, *, password: bool = False, hint: str = "") -> tuple[QWidget, QLineEdit]:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(5)
        label_row = QHBoxLayout()
        label_row.setSpacing(6)
        label = QLabel(caption)
        label.setObjectName("authFieldLabel")
        label_row.addWidget(label)
        label_row.addStretch(1)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("authFieldHint")
            label_row.addWidget(hint_label)
        column.addLayout(label_row)
        edit = QLineEdit()
        edit.setObjectName("authInput")
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        column.addWidget(edit)
        return box, edit

    def _set_auth_mode(self, mode: str) -> None:
        register = mode == "register"
        self.confirm_field.setVisible(register)
        self.submit_button.setText("注册并登录" if register else "登录")
        if register:
            icons.apply_icon(self.submit_button, "user-plus", tone="white", size=15)
        else:
            self.submit_button.setIcon(QIcon())
        self.auth_note.setText(REGISTER_NOTE if register else LOGIN_NOTE)

    def _on_submit(self) -> None:
        if self.register_tab.isChecked():
            self._on_register()
        else:
            self._on_login()

    # ---------------------------------------------------------------- 已登录总览
    def _build_overview(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_account_hero())

        columns = QHBoxLayout()
        columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_profile_card())
        left.addWidget(self._build_links_card())
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_security_card())
        right.addWidget(self._build_logout_card())
        right.addStretch(1)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        layout.addLayout(columns, 1)
        return page

    def _build_account_hero(self) -> QFrame:
        hero = QFrame()
        hero.setObjectName("hero")
        layout = QHBoxLayout(hero)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        self.avatar = _Avatar(56)
        layout.addWidget(self.avatar)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.hero_name = QLabel("已登录账号")
        self.hero_name.setObjectName("pageTitle")
        info.addWidget(self.hero_name)
        id_line = QHBoxLayout()
        id_line.setSpacing(6)
        id_line.addWidget(self._meta("账号标识"))
        self.hero_account_id = QLabel("-")
        self.hero_account_id.setObjectName("mono")
        self.hero_account_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        id_line.addWidget(self.hero_account_id)
        self.hero_account_copy = CopyButton("", "复制账号标识")
        id_line.addWidget(self.hero_account_copy)
        id_line.addWidget(self._meta("·"))
        self.hero_created = self._meta("注册于 未知")
        id_line.addWidget(self.hero_created)
        id_line.addStretch(1)
        info.addLayout(id_line)
        layout.addLayout(info, 1)

        side = QVBoxLayout()
        side.setSpacing(5)
        badge_line = QHBoxLayout()
        badge_line.addStretch(1)
        self.hero_badge = chip("已登录", "ok")
        badge_line.addWidget(self.hero_badge)
        side.addLayout(badge_line)
        self.hero_session = self._meta("会话有效期 未知")
        self.hero_session.setAlignment(Qt.AlignmentFlag.AlignRight)
        side.addWidget(self.hero_session)
        trust_button = self.button("信任设备管理", self._on_open_trust, icon="shield-check")
        trust_button.setProperty("ghost", True)
        side.addWidget(trust_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(side)
        return hero

    def _build_profile_card(self) -> QFrame:
        card, body, head = self._card("user-round", "账号信息")
        head.addWidget(chip("登录有效", "ok"))

        account_row, self.account_id_label = self._kv_row("账号标识")
        self.account_id_label.setObjectName("mono")
        self.account_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.account_copy = CopyButton("", "复制账号标识")
        account_row.addWidget(self.account_copy)
        body.addLayout(account_row)

        created_row, self.created_label = self._kv_row("注册时间")
        body.addLayout(created_row)

        device_row, self.device_id_label = self._kv_row("当前设备")
        self.device_id_label.setObjectName("mono")
        self.device_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        device_row.addWidget(chip("本机", "accent"))
        self.device_copy = CopyButton("", "复制设备编号")
        device_row.addWidget(self.device_copy)
        body.addLayout(device_row)

        bound_row, self.bound_label = self._kv_row("已绑定设备")
        self.bound_meta = self._meta("（在线 0 · 受信 0）")
        bound_row.addWidget(self.bound_meta)
        body.addLayout(bound_row)

        state_row, self.login_state_label = self._kv_row("登录状态")
        self.login_state_label.setObjectName("badgeOk")
        self.login_state_label.setText("已登录")
        body.addLayout(state_row)
        body.addWidget(self._hint("数据来源：本机登录态与登录时间；已绑定与受信数量来自设备列表。"))
        return card

    def _build_links_card(self) -> QFrame:
        card, body, head = self._card("monitor", "设备与信任")
        manage = self.button("管理设备", self._on_open_devices, icon="chevron-right")
        manage.setProperty("link", True)
        head.addWidget(manage)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.devices_tile = _LinkTile("monitor", "设备列表", "0 台已绑定 · 0 台在线")
        self.devices_tile.clicked.connect(self._on_open_devices)
        tiles.addWidget(self.devices_tile, 1)
        self.trust_tile = _LinkTile("shield-check", "信任设备", "0 台免确认直连")
        self.trust_tile.clicked.connect(self._on_open_trust)
        tiles.addWidget(self.trust_tile, 1)
        body.addLayout(tiles)
        body.addWidget(self._hint("受信设备连接时免确认；可在设备列表逐台设置或撤销信任。"))
        return card

    def _build_security_card(self) -> QFrame:
        card, body, head = self._card("lock", "登录与安全")
        head.addWidget(self._meta("本机登录态"))

        token_row, self.token_state_label = self._kv_row("设备令牌")
        self.token_state_label.setText("已签发 · 界面不读取原文")
        body.addLayout(token_row)

        session_row, self.session_left_label = self._kv_row("会话令牌")
        self.session_left_label.setText("有效 · 本机会话不限时")
        body.addLayout(session_row)

        storage_row, storage_value = self._kv_row("登录态存储")
        storage_value.setText("本机密文仓")
        body.addLayout(storage_row)
        body.addWidget(self._hint("界面不持有设备令牌，令牌刷新由数据层完成，这里只展示返回的有效期。"))
        return card

    def _build_logout_card(self) -> QFrame:
        card, body, _head = self._card("arrow-left", "退出登录")
        body.addWidget(
            self._hint("退出登录将吊销本机设备令牌与会话令牌，断开关联会话，并清空本机登录态；已绑定设备不会被解绑。")
        )
        zone = QFrame()
        zone.setObjectName("statusStrip")
        zone_layout = QHBoxLayout(zone)
        zone_layout.setContentsMargins(14, 10, 14, 10)
        zone_layout.setSpacing(10)
        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        text_column.addWidget(QLabel("确认后立即生效"))
        text_column.addWidget(self._meta("需要再次输入口令登录，才能继续查看设备与消息。"))
        zone_layout.addLayout(text_column, 1)
        zone_layout.addWidget(self.button("退出登录", self._on_logout, icon="log-out", tone="danger"))
        body.addWidget(zone)
        return card

    @staticmethod
    def _card(glyph: str, title: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        body = QVBoxLayout(card)
        body.setContentsMargins(18, 14, 18, 14)
        body.setSpacing(9)
        head = QHBoxLayout()
        head.setSpacing(8)
        icon_label = QLabel()
        icon_label.setPixmap(icons.pixmap(glyph, active()["accent"], 16))
        head.addWidget(icon_label)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        head.addWidget(title_label)
        head.addStretch(1)
        body.addLayout(head)
        return card, body, head

    @staticmethod
    def _kv_row(caption: str) -> tuple[QHBoxLayout, QLabel]:
        line = QHBoxLayout()
        line.setSpacing(8)
        key = QLabel(caption)
        key.setObjectName("formLabel")
        key.setMinimumWidth(84)
        line.addWidget(key)
        value = QLabel("-")
        line.addWidget(value, 1)
        return line, value

    @staticmethod
    def _meta(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("cardMeta")
        return label

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("cardMeta")
        label.setWordWrap(True)
        return label

    def _on_open_devices(self) -> None:
        self.navigate.emit("devices")

    def _on_open_trust(self) -> None:
        self.navigate.emit("trust")

    # ---------------------------------------------------------------- 状态
    def refresh(self) -> None:
        state = accounts.get_account_state()
        self.report("读取登录态", state)
        if not state.ok or state.data is None:
            self._show_signed_out()
            self.status_label.setText(error_text(state.error_code, state.message))
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)
            return
        if not bool(state.data.get("logged_in")):
            self._show_signed_out()
            self.status_label.setText("未登录")
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)
            return
        data = state.data
        self._account_id = str(data.get("account_id") or "")
        self._device_id = str(data.get("device_id") or api.get_service().identity.device_id)
        name = str(data.get("account_name") or "已登录账号")
        expires = data.get("expires_at") or self._expires_at
        expires_text = _minute(expires) if expires else "本机会话不限时"
        created = _minute(data.get("created_at"))
        self._show_signed_in()
        self.avatar.set_text(name or self._account_id)
        self.hero_name.setText(name)
        self.hero_account_id.setText(self._account_id or "-")
        self.hero_account_copy.set_value(self._account_id, "复制账号标识")
        self.hero_created.setText(f"注册于 {created}")
        self.hero_session.setText(f"会话有效期至 {expires_text}")
        self.account_id_label.setText(self._account_id or "-")
        self.account_copy.set_value(self._account_id, "复制账号标识")
        self.created_label.setText(created)
        self.device_id_label.setText(self._device_id or "-")
        self.device_copy.set_value(self._device_id, "复制设备编号")
        self.token_state_label.setText("已签发 · 界面不读取原文")
        self.session_left_label.setText(f"有效 · {_remaining(expires)}")
        self.status_label.setText(f"已登录 · 到期 {expires_text}")
        self.status_label.setObjectName("authStateOk")
        self._refresh_style(self.status_label)
        self._load_device_stats()

    def _load_device_stats(self) -> None:
        listed = accounts.list_devices()
        if not listed.ok or listed.data is None:
            self.bound_label.setText("暂不可用")
            self.bound_meta.setText("")
            self.devices_tile.set_detail("设备统计暂不可用")
            self.trust_tile.set_detail("设备统计暂不可用")
            return
        items = list(listed.data.get("items", []))
        online = sum(1 for item in items if item.get("online"))
        trusted = sum(1 for item in items if item.get("trusted"))
        self.bound_label.setText(f"{len(items)} 台")
        self.bound_meta.setText(f"（在线 {online} · 受信 {trusted}）")
        self.devices_tile.set_detail(f"{len(items)} 台已绑定 · {online} 台在线")
        self.trust_tile.set_detail(f"{trusted} 台免确认直连")

    def _show_signed_out(self) -> None:
        self.state_stack.setCurrentIndex(0)
        self.subtitle_label.setText(SIGNED_OUT_SUBTITLE)
        self.token_label.setText("设备令牌：未签发")

    def _show_signed_in(self) -> None:
        self.state_stack.setCurrentIndex(1)
        self.subtitle_label.setText(SIGNED_IN_SUBTITLE)

    @staticmethod
    def _refresh_style(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _show_error(self, result: CommandResult) -> None:
        reason = error_text(result.error_code, result.message)
        self.error_label.setText(reason)
        self.error_copy.set_value(reason, "复制错误原因")
        self.error_strip.setVisible(True)

    def _clear_error(self) -> None:
        self.error_strip.setVisible(False)

    # ---------------------------------------------------------------- 命令
    def _on_login(self) -> None:
        result = accounts.login_account(self.login_account.text().strip(), self.login_password.text())
        self._apply_login(result)
        self.report("登录", result)

    def _on_register(self) -> None:
        account = self.register_account.text().strip()
        password = self.register_password.text()
        if password != self.register_confirm.text():
            self.status_label.setText("两次输入的口令不一致")
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)
            return
        registered = accounts.register_account(account, password)
        self.report("注册账号", registered)
        if not registered.ok:
            self._show_error(registered)
            self.status_label.setText(error_text(registered.error_code, registered.message))
            return
        result = accounts.login_account(account, password)
        self._apply_login(result)
        self.report("注册后登录", result)

    def _on_logout(self) -> None:
        if not confirm(
            self, "退出登录", "确认退出当前账号？退出后设备列表、信任与消息需要重新登录才能查看。", "退出登录"
        ):
            return
        result = accounts.logout_account()
        self.report("退出登录", result)
        if not result.ok:
            self._show_error(result)
            return
        self._device_token = None
        self._account_id = ""
        self._device_id = ""
        self._clear_error()
        self.status_label.setText("已退出登录")
        self.status_label.setObjectName("authStateWarn")
        self._refresh_style(self.status_label)
        self.refresh()

    def _on_refresh_token(self) -> None:
        if self._device_token is None:
            state = accounts.get_account_state()
            logged_in = state.ok and bool((state.data or {}).get("logged_in"))
            self.status_label.setText("请重新登录后再刷新会话令牌" if logged_in else "尚未登录，无设备令牌")
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)
            return
        result = accounts.refresh_session_token(self._device_token)
        self.report("刷新会话令牌", result)
        if result.ok and result.data:
            self._device_token = str(result.data.get("device_token") or self._device_token)
            masked = _mask(self._device_token)
            self.token_label.setText(f"设备令牌：{masked}")
            self.token_state_label.setText("已签发 · 界面不读取原文")
            self.session_left_label.setText(f"有效 · {_remaining(result.data.get('expires_at'))}")
            self.status_label.setText(f"会话令牌已刷新，有效期至 {_minute(result.data.get('expires_at'))}")
            self.status_label.setObjectName("authStateOk")
            self._refresh_style(self.status_label)
        else:
            self._show_error(result)
            self.status_label.setText(error_text(result.error_code, result.message))
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)

    def _apply_login(self, result: CommandResult) -> None:
        if not result.ok or result.data is None:
            self._show_error(result)
            self.status_label.setText(error_text(result.error_code, result.message))
            self.status_label.setObjectName("authStateWarn")
            self._refresh_style(self.status_label)
            return
        self._device_token = str(result.data.get("device_token", ""))
        self._expires_at = str(result.data.get("expires_at") or "")
        self.token_label.setText(f"设备令牌：{_mask(self._device_token)}")
        self._clear_error()
        self.login_password.clear()
        self.register_password.clear()
        self.register_confirm.clear()
        self.refresh()
