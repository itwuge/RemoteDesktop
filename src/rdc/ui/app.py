# ==========================================================================================
# 文件        : app.py
# 所属目录    : src/rdc/ui/
# 文件用途    : PySide6 桌面端入口; 装配运行时目录、主题、崩溃日志并打开主窗口
# 架构定位    : 界面层入口, 只做装配与显示
# 核心功能    : python -m rdc.ui 的启动实现; 标准流补位、异常落盘、存活心跳、静默更新检查
# 职责边界    : 不写业务逻辑; 不直接连接网络; 不执行安装包
# ==========================================================================================

"""桌面端入口."""

from __future__ import annotations

import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from rdc.commands import accounts as accounts_commands
from rdc.core import crashguard, runtime
from rdc.core.logging import configure as configure_logging
from rdc.ui import icons
from rdc.ui.main_window import MainWindow
from rdc.ui.theme import apply_theme
from rdc.version import __version__

__all__ = ["MainWindow", "main"]


def _claim_taskbar_identity() -> None:
    """声明进程身份, 让 Windows 任务栏使用窗口图标而不是 pythonw 的默认图标."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RDC.RemoteDesktop")
    except (AttributeError, OSError):
        return


def main(argv: list[str] | None = None) -> int:
    """启动桌面界面."""
    _claim_taskbar_identity()
    arguments = sys.argv if argv is None else argv
    runtime.bootstrap_runtime()
    configure_logging()
    crashguard.ensure_stdio()
    crashguard.install_exception_hooks()
    fault_log = crashguard.enable_faulthandler()
    crashguard.log_session_start(version=__version__, fault_log=str(fault_log))
    storage_warning = accounts_commands.configure_persistent_store()

    app = QApplication.instance()
    if app is None or not isinstance(app, QApplication):
        app = QApplication(arguments)
    app.setStyle("Fusion")
    app.setApplicationName("RDC 远程桌面")
    app.setWindowIcon(icons.app_icon())
    apply_theme(app)
    window = MainWindow()
    window.show()
    if storage_warning:
        QMessageBox.warning(window, "RDC 远程桌面", storage_warning)
    crashguard.start_heartbeat(app)
    QTimer.singleShot(800, window.start_silent_update_check)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
