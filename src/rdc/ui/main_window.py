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

from rdc.commands import accounts, api
from rdc.commands import update as update_commands
from rdc.commands.service import CommandService
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import BrandMark, StatusDot, copy_to_clipboard, show_toast
from rdc.ui.copy import error_text
from rdc.ui.pages import (
    AccountPage,
    ApprovalsPage,
    ClipboardPage,
    DevicesPage,
    HomePage,
    MessagesPage,
    SessionPage,
    SettingsPage,
    TransferPage,
    TrustPage,
    UpdatePage,
    WallPage,
)
from rdc.ui.pages.base import PageBase
from rdc.ui.theme import apply_theme
from rdc.ui.update_panel import UpdateCheckWorker
from rdc.version import __version__

NAV_GROUPS = (
    (
        "工作台",
        (
            ("home", "首页", "house"),
            ("devices", "设备", "monitor"),
            ("messages", "消息", "activity"),
            ("account", "账号", "user-round"),
        ),
    ),
    (
        "空间",
        (
            ("wall", "屏幕墙", "layout-grid"),
            ("session", "会话窗口", "screen-share"),
            ("transfer", "文件传输", "file-up"),
            ("clipboard", "剪贴板", "clipboard"),
            ("approvals", "申请与审批", "shield-check"),
        ),
    ),
    (
        "账号与系统",
        (
            ("trust", "信任设备", "shield-check"),
            ("settings", "设置", "settings"),
            ("update", "更新面板", "download"),
        ),
    ),
)

NAV_ITEMS = tuple(item for _, items in NAV_GROUPS for item in items)

# 页头眉标与 ui-v2 桌面原型逐页对齐; 首页与设备额外追加登录态
PAGE_EYEBROWS = {
    "home": "首页",
    "devices": "设备",
    "trust": "信任设备 · 免确认直连",
    "messages": "消息 · 审计时间线",
    "account": "账号与安全",
    "settings": "设置 · 本机与服务端",
    "update": "更新面板 · 客户端升级",
    "wall": "屏幕墙 · 多设备监看",
    "transfer": "文件传输 · 会话内通道",
    "clipboard": "剪贴板 · 双向同步",
    "approvals": "申请与审批 · 临时授权",
    "session": "会话窗口 · 远程控制",
}
LOGIN_AWARE_EYEBROWS = frozenset({"home", "devices"})


class MainWindow(QMainWindow):
    """桌面主窗口."""

    def __init__(self, service: CommandService | None = None) -> None:
        super().__init__()
        if service is not None:
            api.configure(service)
        self.command_log: list[tuple[str, CommandResult]] = []
        self._light = False
        self._log_count = 0
        self._close_to_tray = False
        self._force_close = False
        self._update_worker: UpdateCheckWorker | None = None
        self.setWindowTitle("RDC 远程桌面")
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
        version = QLabel(f"RDC 远程桌面 {__version__}")
        version.setObjectName("cardMeta")
        self.statusBar().addPermanentWidget(version)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(216)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(11, 15, 11, 14)
        side.setSpacing(2)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(8, 2, 8, 2)
        brand_row.setSpacing(9)
        brand_row.addWidget(BrandMark(30))
        caption = QVBoxLayout()
        caption.setSpacing(0)
        brand = QLabel("RDC 远程桌面")
        brand.setObjectName("brand")
        brand_sub = QLabel("安全远程控制")
        brand_sub.setObjectName("brandSub")
        caption.addWidget(brand)
        caption.addWidget(brand_sub)
        brand_row.addLayout(caption)
        brand_row.addStretch(1)
        side.addLayout(brand_row)
        side.addSpacing(14)

        for index, (group, items) in enumerate(NAV_GROUPS):
            if index:
                side.addSpacing(10)
            nav_section = QLabel(group)
            nav_section.setObjectName("navSection")
            nav_section.setContentsMargins(9, 0, 9, 5)
            side.addWidget(nav_section)
            for key, label, glyph in items:
                button = self._nav_button(key, label, glyph)
                self._nav_buttons[key] = button
                side.addWidget(button)
        side.addStretch(1)

        self.theme_button = QPushButton("切换浅色")
        self.theme_button.setProperty("nav", True)
        self.theme_button.setMinimumHeight(34)
        self.theme_button.clicked.connect(self.toggle_theme)
        icons.apply_icon(self.theme_button, "sun", tone="muted", size=17)
        side.addWidget(self.theme_button)
        side.addSpacing(6)

        foot = QVBoxLayout()
        foot.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self._foot_dot = StatusDot("faint", 8)
        self._foot_name = QLabel("未登录")
        self._foot_name.setObjectName("sidebarFoot")
        name_row.addWidget(self._foot_dot)
        name_row.addWidget(self._foot_name)
        name_row.addStretch(1)
        foot.addLayout(name_row)
        self._foot_id = QLabel("未登录")
        self._foot_id.setObjectName("cardMeta")
        self._foot_id.setContentsMargins(16, 0, 0, 0)
        foot.addWidget(self._foot_id)
        side.addLayout(foot)
        return sidebar

    def _build_log_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("logPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(6)
        self.log_toggle = QPushButton("命令返回 · 运行日志 (0)")
        self.log_toggle.setObjectName("logToggle")
        self.log_toggle.setProperty("link", False)
        icons.apply_icon(self.log_toggle, "chevron-up", tone="muted", size=14)
        self.log_toggle.clicked.connect(self._toggle_log)
        toggle_row.addWidget(self.log_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_copy = QPushButton("复制日志")
        self.log_copy.setObjectName("logToggle")
        icons.apply_icon(self.log_copy, "copy", tone="muted", size=14)
        self.log_copy.setToolTip("复制当前运行日志全文")
        self.log_copy.clicked.connect(self._copy_log)
        toggle_row.addWidget(self.log_copy, 0, Qt.AlignmentFlag.AlignLeft)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(104)
        self.log_view.setVisible(False)
        self.log_view.setPlaceholderText("每次命令调用在此显示中文原因; 错误码保留在结构化字段与日志中")
        layout.addWidget(self.log_view)
        return panel

    def _register_pages(self) -> None:
        pages: tuple[PageBase, ...] = (
            HomePage(),
            DevicesPage(),
            TrustPage(),
            WallPage(),
            SessionPage(),
            TransferPage(),
            ClipboardPage(),
            SettingsPage(),
            AccountPage(),
            ApprovalsPage(),
            MessagesPage(),
            UpdatePage(),
        )
        for page in pages:
            page.set_sink(self._on_result)
            page.navigate.connect(self.switch_page)
            self._pages[page.page_key] = page
            self.stack.addWidget(page)
        self.home = self._pages["home"]
        self.devices = self._pages["devices"]
        self.trust = self._pages["trust"]
        self.wall = self._pages["wall"]
        self.session_page = cast(SessionPage, self._pages["session"])
        self.transfer_page = cast(TransferPage, self._pages["transfer"])
        self.clipboard_page = cast(ClipboardPage, self._pages["clipboard"])
        self.settings = self._pages["settings"]
        self.account = self._pages["account"]
        self.approvals = self._pages["approvals"]
        self.messages = self._pages["messages"]

        home = cast(HomePage, self.home)
        home.connection_started.connect(self._on_connection_started)
        home.session_opened.connect(self._on_session_opened)
        cast(DevicesPage, self._pages["devices"]).connection_started.connect(self._on_connection_started)

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
        button.setMinimumHeight(34)
        button.setIconSize(QSize(15, 15))
        if glyph:
            icons.apply_icon(button, glyph, size=15)
        if key != "__theme__":
            button.clicked.connect(lambda checked=False, target=key: self.switch_page(target))
        return button

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
        state = accounts.get_account_state()
        data = state.data or {}
        logged_in = state.ok and bool(data.get("logged_in"))
        account_name = str(data.get("account_name") or "")
        account_id = str(data.get("account_id") or "")
        self._foot_name.setText(account_name or ("已登录" if logged_in else "未登录"))
        if logged_in and account_id:
            short_id = account_id if len(account_id) <= 18 else f"{account_id[:18]}…"
            self._foot_id.setText(f"已登录 · {short_id}")
            self._foot_id.setToolTip(account_id)
        else:
            self._foot_id.setText("已登录" if logged_in else "未登录")
            self._foot_id.setToolTip("")
        self._foot_dot.set_color("ok" if logged_in else "faint")
        eyebrow = PAGE_EYEBROWS.get(key, "")
        if key in LOGIN_AWARE_EYEBROWS:
            eyebrow = f"{eyebrow} · {'已登录' if logged_in else '未登录'}"
        page.set_eyebrow(eyebrow)
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

    def _copy_log(self) -> None:
        """复制日志全文; 错误码只随日志进入剪贴板, 不进入界面正文."""
        text = self.log_view.toPlainText()
        if not text.strip():
            self.statusBar().showMessage("日志为空, 暂无可复制内容", 3000)
            return
        if copy_to_clipboard(text):
            show_toast(self)
            self.statusBar().showMessage("已复制运行日志", 3000)

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
        self.statusBar().showMessage(f"发现新版本 {latest}, 可在更新面板中检查更新", 10000)

    def _clear_update_worker(self) -> None:
        self._update_worker = None

    def set_close_to_tray(self, enabled: bool) -> None:
        """开机自启进入托盘后, 关窗口只隐藏, 不退出进程."""
        self._close_to_tray = enabled

    def show_main(self) -> None:
        """从托盘打开主界面."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_from_tray(self) -> None:
        """托盘菜单退出: 走正常关闭流程后结束进程."""
        self._force_close = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭前等待静默检查结束, 避免销毁运行中的 QThread."""
        if self._close_to_tray and not self._force_close:
            event.ignore()
            self.hide()
            return
        for page in self._pages.values():
            page.shutdown()
        if self._update_worker is not None and self._update_worker.isRunning():
            self._update_worker.wait(2000)
        super().closeEvent(event)

    def _on_result(self, action: str, result: CommandResult) -> None:
        """统一呈现命令结果: 界面只显示中文原因, 错误码留在结构化字段与日志."""
        self.command_log.append((action, result))
        self._log_count += 1
        stamp = QTime.currentTime().toString("HH:mm:ss")
        reason = error_text(result.error_code, result.message)
        self.log_view.appendPlainText(f"[{stamp}] {action}: {reason}")
        self.log_toggle.setText(f"命令返回 · 运行日志 ({self._log_count})")
        self.statusBar().showMessage(f"{action}: {reason}", 8000)
