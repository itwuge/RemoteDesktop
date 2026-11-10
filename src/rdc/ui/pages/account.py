# ==========================================================================================
# 文件        : account.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 登录 / 注册页: 账号口令收集、登录、注册、刷新会话令牌
# 架构定位    : 界面层, 只调用 rdc.commands.accounts
# 核心功能    : 登录与注册切换、令牌掩码显示、失败文案原样呈现
# 职责边界    : 不保存口令; 不显示令牌全文; 不写日志
# ==========================================================================================

"""登录 / 注册页."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import accounts
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import Illustration, chip, fade_in
from rdc.ui.pages.base import PageBase
from rdc.ui.theme import active


def _mask(token: str) -> str:
    """令牌只显示首尾各 4 位."""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


class AccountPage(PageBase):
    """账号入口."""

    def __init__(self) -> None:
        super().__init__(
            "account",
            "登录 / 注册",
            "一账号多设备; 多账号可经授权控制同一设备; 同账号免确认默认开启.",
        )
        self._device_token: str | None = None
        self._build()
        self.body.setSpacing(12)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        columns = QHBoxLayout()
        columns.setSpacing(16)
        columns.addWidget(self._build_brand_panel(), 5)
        columns.addWidget(self._build_form_panel(), 6)
        self.body.addLayout(columns, 1)

    # ---------------------------------------------------------------- 左侧
    def _build_brand_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("hero")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(8)
        eyebrow = QLabel("SECURE ACCESS · 账号体系")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)
        title = QLabel("一台设备, 一个身份,\n把控制权留在自己手里.")
        title.setObjectName("pageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)
        layout.addSpacing(2)
        layout.addWidget(self.muted("登录后设备列表、屏幕墙与最近会话才会同步; 所有凭据只在命令调用中传递."))
        layout.addSpacing(6)
        illustration = Illustration("shield", 230, 150)
        layout.addWidget(illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(4)
        for glyph, text in (
            ("lock", "口令不落盘, 不写日志"),
            ("monitor", "一账号可绑定多台设备"),
            ("shield-check", "同账号免确认可逐台关闭"),
        ):
            row = QHBoxLayout()
            row.setSpacing(9)
            icon_label = QLabel()
            icon_label.setPixmap(icons.pixmap(glyph, active()["accent"], 16))
            row.addWidget(icon_label)
            caption = QLabel(text)
            caption.setObjectName("muted")
            row.addWidget(caption, 1)
            layout.addLayout(row)
        layout.addStretch(1)
        return panel

    # ---------------------------------------------------------------- 右侧
    def _build_form_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("card")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("账号入口")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(chip("V1 · 本地命令层", "accent"))
        layout.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.tabs.addTab(self._build_login(), "登录")
        self.tabs.addTab(self._build_register(), "注册")
        layout.addWidget(self.tabs, 0)

        status_card = QFrame()
        status_card.setObjectName("statusStrip")
        status_layout = QVBoxLayout(status_card)
        status_layout.setContentsMargins(14, 10, 14, 10)
        status_layout.setSpacing(4)
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        key_icon = QLabel()
        key_icon.setPixmap(icons.pixmap("key-round", active()["muted"], 15))
        token_row.addWidget(key_icon)
        self.token_label = QLabel("设备令牌: -")
        self.token_label.setObjectName("muted")
        token_row.addWidget(self.token_label, 1)
        self.status_label = QLabel("未登录")
        self.status_label.setObjectName("badgeWarn")
        token_row.addWidget(self.status_label)
        status_layout.addLayout(token_row)
        status_layout.addWidget(self.muted("口令只在本次调用中传递, 不落盘、不写日志; 令牌仅显示掩码."))
        layout.addWidget(status_card)
        layout.addStretch(1)
        return panel

    def _build_login(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 14, 4, 6)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.login_account = QLineEdit()
        self.login_account.setPlaceholderText("账号, 3 至 32 位")
        self.login_password = QLineEdit()
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_password.setPlaceholderText("口令, 至少 8 位")
        form.addRow("账号", self.login_account)
        form.addRow("口令", self.login_password)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.button("登录", self._on_login, primary=True, icon="log-in"))
        buttons.addWidget(self.button("刷新会话令牌", self._on_refresh_token, icon="key-round"))
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _build_register(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 14, 4, 6)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.register_account = QLineEdit()
        self.register_account.setPlaceholderText("账号, 3 至 32 位")
        self.register_password = QLineEdit()
        self.register_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_password.setPlaceholderText("口令, 至少 8 位")
        self.register_confirm = QLineEdit()
        self.register_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.register_confirm.setPlaceholderText("再次输入口令")
        form.addRow("账号", self.register_account)
        form.addRow("口令", self.register_password)
        form.addRow("确认口令", self.register_confirm)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.button("注册并登录", self._on_register, primary=True, icon="user-plus"))
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addWidget(self.muted("注册成功后自动登录; 任何一步失败都原样显示命令返回文案."))
        layout.addStretch(1)
        return page

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
            return
        registered = accounts.register_account(account, password)
        self.report("注册账号", registered)
        if not registered.ok:
            self.status_label.setText(registered.message)
            return
        result = accounts.login_account(account, password)
        self._apply_login(result)
        self.report("注册后登录", result)

    def _on_refresh_token(self) -> None:
        if self._device_token is None:
            self.status_label.setText("尚未登录, 无设备令牌")
            return
        result = accounts.refresh_session_token(self._device_token)
        if result.ok and result.data:
            self.status_label.setText(f"会话令牌已刷新, 有效期至 {result.data.get('expires_at', '-')}")
        else:
            self.status_label.setText(result.message)
        self.report("刷新会话令牌", result)

    def _apply_login(self, result: CommandResult) -> None:
        if not result.ok or result.data is None:
            self.status_label.setText(result.message)
            return
        self._device_token = str(result.data.get("device_token", ""))
        self.token_label.setText(f"设备令牌: {_mask(self._device_token)}")
        self.status_label.setText(f"已登录 · 会话令牌有效期至 {result.data.get('expires_at', '-')}")
