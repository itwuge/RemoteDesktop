"""远程桌面控制软件核心包."""

from rdc.core.crashguard import ensure_stdio, install_exception_hooks
from rdc.version import __version__

__all__ = ["__version__"]

# pythonw 与 PyInstaller --windowed 下标准流为 None: 先补位再装异常钩子,
# 否则之后任何导入期异常(例如缺依赖)都会静默退出, 用户与日志都拿不到线索.
if ensure_stdio():
    install_exception_hooks()
