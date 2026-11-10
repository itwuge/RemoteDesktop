# ==========================================================================================
# 文件        : tray.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 托盘图标装配; 开机自启时静默进入托盘, 不弹主窗口
# 架构定位    : 界面层装配件; 只依赖主窗口与图标资源
# 核心功能    : 托盘菜单、双击打开主界面、退出时走主窗口关闭流程
# 职责边界    : 不写业务逻辑; 不读写配置; 不直接调用命令层
# ==========================================================================================

"""托盘装配."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from rdc.ui import icons
from rdc.ui.main_window import MainWindow

__all__ = ["TrayController", "tray_available"]


def tray_available() -> bool:
    """当前桌面会话是否提供系统托盘."""
    return QSystemTrayIcon.isSystemTrayAvailable()


class TrayController(QObject):
    """托盘图标与菜单."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window
        self.icon = QSystemTrayIcon(icons.app_icon(), self)
        self.icon.setToolTip("RDC 远程桌面")
        menu = QMenu()
        open_action = QAction("打开主界面", menu)
        open_action.triggered.connect(self._window.show_main)
        menu.addAction(open_action)
        menu.addSeparator()
        quit_action = QAction("退出 RDC", menu)
        quit_action.triggered.connect(self._window.quit_from_tray)
        menu.addAction(quit_action)
        self.icon.setContextMenu(menu)
        self.icon.activated.connect(self._on_activated)
        self._menu = menu

    def show(self) -> None:
        """显示托盘图标; 主窗口保持隐藏."""
        self.icon.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._window.show_main()
