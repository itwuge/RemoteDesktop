# ==========================================================================================
# 文件        : main_window.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 主窗口: 侧栏导航、页面堆栈、命令结果统一呈现、主题切换
# 架构定位    : 界面层装配处; 唯一依赖是 rdc.commands 与各页面
# 核心功能    : 页面注册与切换、结果日志、深浅色切换、无后端时的未连接状态
# 职责边界    : 不写业务判断; 不直接调用网络、文件与解密; 不改写 message
# ==========================================================================================

"""主窗口装配."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QSize, Qt, QTime
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api
from rdc.commands import update as update_commands
from rdc.commands.service import CommandService
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import BrandMark, StatusDot
from rdc.ui.pages import (
    AccountPage,
    ApprovalsPage,
    DevicesPage,
    HomePage,
    SessionPage,
    SettingsPage,
    WallPage,
)
from rdc.ui.pages.base import PageBase
from rdc.ui.theme import apply_theme
from rdc.ui.update_panel import UpdateCheckWorker

NAV_ITEMS = (
    ("home", "首页 / 连接", "house"),
    ("devices", "设备列表", "monitor"),
    ("wall", "屏幕墙", "layout-grid"),
    ("session", "会话窗口", "screen-share"),
    ("settings", "设置", "settings"),
    ("account", "登录 / 注册", "user-round"),
    ("approvals", "申请与审批", "shield-check"),
)


class MainWindow(QMainWindow):
    """桌面主窗口."""

    def __init__(self, service: CommandService | None = None) -> None:
        super().__init__()
        if service is not None:
            api.configure(service)
        self.command_log: list[tuple[str, CommandResult]] = []
        self._light = False
        self._log_count = 0
        self._update_worker: UpdateCheckWorker | None = None
        self.setWindowTitle("RDC 远程桌面 · V1")
        self.resize(1280, 840)
        self.setMinimumSize(1080, 720)
        self.setWindowIcon(icons.app_icon())
        self._pages: dict[str, PageBase] = {}
        self._nav_buttons: dict[str, QPushButton] = {}
        self._build()
        self.switch_page("home")

    # ---------------------------------------------------------------- 装配
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        self._register_pages()
        right.addWidget(self.stack, 1)
        right.addWidget(self._build_log_panel())
        layout.addLayout(right, 1)
        self.setCentralWidget(central)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage("就绪 · 本地模式 · 未连接")
        self._status_dot = StatusDot("faint")
        self.statusBar().addPermanentWidget(self._status_dot)
        version = QLabel("RDC V1.0")
        version.setObjectName("cardMeta")
        self.statusBar().addPermanentWidget(version)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        brand_row.addWidget(BrandMark(40))
        caption = QVBoxLayout()
        caption.setSpacing(0)
        brand = QLabel("RDC 远程桌面")
        brand.setObjectName("brand")
        brand_sub = QLabel("V1 · 安全远程控制")
        brand_sub.setObjectName("brandSub")
        caption.addWidget(brand)
        caption.addWidget(brand_sub)
        brand_row.addLayout(caption)
        brand_row.addStretch(1)
        side.addLayout(brand_row)
        side.addSpacing(20)

        nav_section = QLabel("工作台")
        nav_section.setObjectName("navSection")
        side.addWidget(nav_section)
        side.addSpacing(4)
        for key, label, glyph in NAV_ITEMS:
            button = self._nav_button(key, label, glyph)
            self._nav_buttons[key] = button
            side.addWidget(button)
        side.addStretch(1)

        self.theme_button = QPushButton("切换浅色")
        self.theme_button.setProperty("nav", True)
        self.theme_button.setMinimumHeight(38)
        self.theme_button.clicked.connect(self.toggle_theme)
        icons.apply_icon(self.theme_button, "sun", tone="muted", size=17)
        side.addWidget(self.theme_button)
        side.addSpacing(6)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        foot.addWidget(StatusDot("warn", 8))
        foot_label = QLabel("本地界面 · 未连接后端")
        foot_label.setObjectName("sidebarFoot")
        foot.addWidget(foot_label)
        foot.addStretch(1)
        side.addLayout(foot)
        side.addWidget(self._muted("界面层不承载业务逻辑"))
        return sidebar

    def _build_log_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.log_toggle = QPushButton("命令返回 · 运行日志 (0)")
        self.log_toggle.setObjectName("logToggle")
        self.log_toggle.setProperty("link", False)
        icons.apply_icon(self.log_toggle, "chevron-up", tone="muted", size=14)
        self.log_toggle.clicked.connect(self._toggle_log)
        layout.addWidget(self.log_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(104)
        self.log_view.setVisible(False)
        self.log_view.setPlaceholderText("每次命令调用在此显示 message 与 error_code, 不改写文案")
        layout.addWidget(self.log_view)
        return panel

    def _register_pages(self) -> None:
        pages: tuple[PageBase, ...] = (
            HomePage(),
            DevicesPage(),
            WallPage(),
            SessionPage(),
            SettingsPage(),
            AccountPage(),
            ApprovalsPage(),
        )
        for page in pages:
            page.set_sink(self._on_result)
            page.navigate.connect(self.switch_page)
            self._pages[page.page_key] = page
            self.stack.addWidget(page)
        self.home = self._pages["home"]
        self.devices = self._pages["devices"]
        self.wall = self._pages["wall"]
        self.session_page = cast(SessionPage, self._pages["session"])
        self.settings = self._pages["settings"]
        self.account = self._pages["account"]
        self.approvals = self._pages["approvals"]

        home = cast(HomePage, self.home)
        home.connection_started.connect(self._on_connection_started)
        home.session_opened.connect(self._on_session_opened)

    def _on_connection_started(self, peer: str, task_id: str) -> None:
        """连接任务发起后进入会话窗, 先显示连接中过渡态."""
        self.switch_page("session")
        self.session_page.show_connecting(peer, task_id)

    def _on_session_opened(self, session_id: str, peer: str) -> None:
        """最近会话双击进入会话窗."""
        self.switch_page("session")
        self.session_page.open_session(session_id, peer)

    def _nav_button(self, key: str, label: str, glyph: str = "") -> QPushButton:
        button = QPushButton(label)
        button.setProperty("nav", True)
        button.setCheckable(True)
        button.setMinimumHeight(38)
        button.setIconSize(QSize(18, 18))
        if glyph:
            icons.apply_icon(button, glyph, size=18)
        if key != "__theme__":
            button.clicked.connect(lambda checked=False, target=key: self.switch_page(target))
        return button

    @staticmethod
    def _muted(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    # ---------------------------------------------------------------- 交互
    def switch_page(self, key: str) -> None:
        """切换页面并触达一次刷新; 未知页面回到首页."""
        page = self._pages.get(key)
        if page is None:
            page = self._pages["home"]
            key = "home"
        for nav_key, button in self._nav_buttons.items():
            button.setChecked(nav_key == key)
        self.stack.setCurrentWidget(page)
        page.refresh()

    def toggle_theme(self) -> None:
        """深浅色切换; 纯视图状态."""
        self._light = not self._light
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, self._light)
        self.theme_button.setText("切换深色" if self._light else "切换浅色")
        icons.apply_icon(self.theme_button, "moon" if self._light else "sun", tone="muted", size=17)
        icons.refresh_icons(self, self._light)
        self.setWindowIcon(icons.app_icon(self._light))
        for widget in self.findChildren(QWidget):
            widget.update()

    def _toggle_log(self) -> None:
        """展开或收起命令返回日志, 不改变日志内容."""
        visible = not self.log_view.isVisible()
        self.log_view.setVisible(visible)
        icons.apply_icon(self.log_toggle, "chevron-down" if visible else "chevron-up", tone="muted", size=14)

    def start_silent_update_check(self) -> None:
        """启动后静默检查更新; 失败只落日志, 不阻塞启动也不弹错误."""
        if self._update_worker is not None and self._update_worker.isRunning():
            return
        worker = UpdateCheckWorker(update_commands.get_service(), self)
        worker.checked.connect(self._on_silent_update_checked)
        worker.finished.connect(self._clear_update_worker)
        self._update_worker = worker
        worker.start()

    def _on_silent_update_checked(self, result: object) -> None:
        if not isinstance(result, CommandResult) or not result.ok or not result.data:
            return
        if not result.data.get("available"):
            return
        latest = str(result.data.get("latest_version", ""))
        self.statusBar().showMessage(f"发现新版本 {latest}, 可在设置中检查更新", 10000)

    def _clear_update_worker(self) -> None:
        self._update_worker = None

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭前等待静默检查结束, 避免销毁运行中的 QThread."""
        if self._update_worker is not None and self._update_worker.isRunning():
            self._update_worker.wait(2000)
        super().closeEvent(event)

    def _on_result(self, action: str, result: CommandResult) -> None:
        """统一呈现命令结果: 原样 message + 错误码."""
        self.command_log.append((action, result))
        self._log_count += 1
        stamp = QTime.currentTime().toString("HH:mm:ss")
        suffix = f" [{result.error_code}]" if result.error_code else ""
        self.log_view.appendPlainText(f"[{stamp}] {action}: {result.message}{suffix}")
        self.log_toggle.setText(f"命令返回 · 运行日志 ({self._log_count})")
        self.statusBar().showMessage(f"{action}: {result.message}{suffix}", 8000)
