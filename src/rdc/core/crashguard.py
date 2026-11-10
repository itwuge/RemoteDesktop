# ==========================================================================================
# 文件        : crashguard.py
# 所属目录    : src/rdc/core/
# 文件用途    : 无控制台运行时的防静默退出: 标准流补位、异常落盘、可见提示与存活心跳
# 架构定位    : 基础设施层, 只依赖标准库; 在包导入期即可安装, 早于 PySide6 等第三方库
# 核心功能    : 补齐 stdout/stderr/stdin; 未捕获异常写 crash.log 与 rdc.log; 弹窗或系统对话框提示
# 职责边界    : 不执行业务逻辑; 不吞掉异常; 不写令牌; 不阻塞主流程
# ==========================================================================================

"""无控制台防静默退出.

pythonw 与 PyInstaller ``--windowed`` 下 ``sys.stdout``/``sys.stderr``/``sys.stdin`` 都是 None:
- 任何库写 stderr 都会抛 ``AttributeError``;
- 默认 ``sys.excepthook`` 也写不出堆栈, 进程直接退出, 用户什么也看不到.

本模块在包导入时补位标准流, 并提供:
- ``install_exception_hooks``: 主线程、子线程、unraisable 异常统一落盘并提示;
- ``enable_faulthandler``: 原生崩溃(段错误/abort)写 fault.log;
- ``log_session_start`` / 心跳: 记录启动与存活时间, 便于判断静默消失的时点.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, TextIO, cast

if TYPE_CHECKING:
    from PySide6.QtCore import QObject

CRASH_FILE_NAME = "crash.log"
FAULT_FILE_NAME = "fault.log"
STDIO_FILE_PREFIX = "stdio"

_STARTED = time.monotonic()
_EXIT_REGISTERED = False
_FAULT_HANDLE: TextIO | None = None
_STDIO_LOCK = threading.Lock()


def uptime_seconds() -> float:
    """进程已存活秒数, 单调时钟, 不受系统时间调整影响."""
    return time.monotonic() - _STARTED


def _log_dir() -> Path:
    """日志目录: RDC_LOG_DIR > RDC_BUILD_ROOT/logs/rdc > 可执行文件旁的 data/logs > ./logs."""
    explicit = os.environ.get("RDC_LOG_DIR", "").strip()
    if explicit:
        return Path(explicit)
    build_root = os.environ.get("RDC_BUILD_ROOT", "").strip()
    if build_root:
        return Path(build_root) / "logs" / "rdc"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data" / "logs"
    return Path("logs")


def _writable_dir() -> Path:
    """返回可写目录; 只读安装位置退到系统临时目录, 保证仍有落盘证据."""
    directory = _log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return directory
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "rdc-logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class _StreamToFile:
    """把写入落到 stdio-<pid>.log 的文本流; 任何写入失败都不抛给调用方."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.encoding = "utf-8"
        self.errors = "replace"
        self.closed = False

    @property
    def path(self) -> Path:
        return _writable_dir() / f"{STDIO_FILE_PREFIX}-{self.name}-{os.getpid()}.log"

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("无控制台运行, 不提供文件描述符")

    def flush(self) -> None:
        return

    def close(self) -> None:
        return

    def write(self, text: str) -> int:
        if not text:
            return 0
        with _STDIO_LOCK:
            try:
                with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(text)
            except OSError:
                return len(text)
        return len(text)


class _NullReader:
    """stdin 补位: 读取立即返回空, 避免交互式库因 stdin 为 None 抛错."""

    encoding = "utf-8"
    errors = "replace"
    closed = False

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("无控制台运行, 不提供文件描述符")

    def flush(self) -> None:
        return

    def close(self) -> None:
        return

    def read(self, size: int = -1) -> str:
        return ""

    def readline(self, size: int = -1) -> str:
        return ""


def ensure_stdio() -> bool:
    """把 None 的标准流换成安全实现; 返回是否发生了补位(即无控制台模式)."""
    changed = False
    if sys.stdout is None:
        sys.stdout = cast(TextIO, _StreamToFile("stdout"))
        changed = True
    if sys.stderr is None:
        sys.stderr = cast(TextIO, _StreamToFile("stderr"))
        changed = True
    if sys.stdin is None:
        sys.stdin = cast(TextIO, _NullReader())
        changed = True
    return changed


def _append_crash_record(event: str, content: str) -> Path:
    """直接追加崩溃记录, 不依赖 logging 是否已初始化."""
    path = _writable_dir() / CRASH_FILE_NAME
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    header = f"[{stamp}] pid={os.getpid()} uptime={uptime_seconds():.1f}s event={event}\n"
    with _STDIO_LOCK:
        try:
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(header)
                handle.write(content.rstrip() + "\n\n")
        except OSError:
            fallback = Path(tempfile.gettempdir()) / "rdc-logs" / CRASH_FILE_NAME
            try:
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with fallback.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(header)
                    handle.write(content.rstrip() + "\n\n")
            except OSError:
                return path
            return fallback
    return path


def _show_qt_message(message: str) -> bool:
    """主线程且已有 QApplication 时用 Qt 弹窗; 失败返回 False 由系统对话框兜底."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        return False
    app = QApplication.instance()
    if app is None:
        return False
    from PySide6.QtCore import QThread

    if QThread.currentThread() is not app.thread():
        return False
    try:
        QMessageBox.critical(None, "RDC 远程桌面", message)
    except RuntimeError:
        return False
    return True


def _show_native_message(message: str) -> bool:
    """不依赖 Qt 的系统对话框; 无控制台时也能让用户看到错误."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        flags = 0x00000010 | 0x00040000  # MB_ICONERROR | MB_TOPMOST
        user32.MessageBoxW(None, message, "RDC 远程桌面", flags)
    except (AttributeError, OSError):
        return False
    return True


def notify_crash(error: BaseException, *, detail: str = "") -> None:
    """给用户可见提示: 优先 Qt 弹窗, 否则系统对话框, 再退 stderr."""
    path = _writable_dir() / CRASH_FILE_NAME
    message = f"程序遇到未处理的错误: {error}\n\n详细堆栈已写入:\n{path}"
    if detail:
        message = f"{message}\n\n{detail}"
    if _show_qt_message(message):
        return
    if _show_native_message(message):
        return
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (AttributeError, OSError, ValueError):
        return


def _log_with_logging(
    event: str,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    """尽量同时写结构化日志; logging 未初始化时补一次初始化."""
    try:
        from rdc.core.logging import configure

        configure()
    except OSError:
        return
    logger = logging.getLogger("rdc")
    logger.error(event, exc_info=(exc_type, exc_value, exc_tb))


def _record(
    event: str,
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
    *,
    notify: bool,
) -> None:
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    _append_crash_record(event, text)
    _log_with_logging(event, exc_type, exc_value, exc_tb)
    if notify:
        notify_crash(exc_value)


def _handle_unhandled(exc_type: type[BaseException], exc_value: BaseException, exc_tb: TracebackType | None) -> None:
    _record("app.unhandled_exception", exc_type, exc_value, exc_tb, notify=True)


def _handle_thread_unhandled(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None:
        return
    # 子线程异常不会结束进程, 但仍需落盘并提示, 避免"功能坏了却没记录"
    _record("app.thread_exception", args.exc_type, args.exc_value, args.exc_traceback, notify=True)


def _handle_unraisable(args: object) -> None:
    exc_value = getattr(args, "exc_value", None)
    if not isinstance(exc_value, BaseException):
        return
    _record("app.unraisable_exception", type(exc_value), exc_value, exc_value.__traceback__, notify=False)


def install_exception_hooks(*, notify: bool = True) -> None:
    """安装未捕获异常钩子; 幂等, 可重复调用. notify=False 只落盘不弹窗(供自动化验证)."""
    if notify:
        sys.excepthook = _handle_unhandled
        threading.excepthook = _handle_thread_unhandled
    else:
        sys.excepthook = _handle_unhandled_notifyless
        threading.excepthook = _handle_thread_unhandled_notifyless
    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = cast(Any, _handle_unraisable)


def _handle_unhandled_notifyless(
    exc_type: type[BaseException], exc_value: BaseException, exc_tb: TracebackType | None
) -> None:
    _record("app.unhandled_exception", exc_type, exc_value, exc_tb, notify=False)


def _handle_thread_unhandled_notifyless(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is None:
        return
    _record("app.thread_exception", args.exc_type, args.exc_value, args.exc_traceback, notify=False)


def enable_faulthandler() -> Path:
    """把原生崩溃堆栈写入 fault.log; 返回日志路径."""
    global _FAULT_HANDLE
    path = _writable_dir() / FAULT_FILE_NAME
    try:
        _FAULT_HANDLE = path.open("a", encoding="utf-8", buffering=1)
        import faulthandler

        faulthandler.enable(file=_FAULT_HANDLE, all_threads=True)
    except OSError:
        return path
    return path


def log_session_start(**fields: object) -> None:
    """记录启动环境, 便于对照崩溃记录与存活时长."""
    global _EXIT_REGISTERED
    try:
        from rdc.core.logging import log_event
    except ImportError:
        return
    if not _EXIT_REGISTERED:
        atexit.register(log_session_exit)
        _EXIT_REGISTERED = True
    payload: dict[str, Any] = {
        "uptime_s": 0.0,
        "pid": os.getpid(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "log_dir": str(_writable_dir()),
    }
    payload.update(fields)
    log_event("app", "app.start", **payload)


def log_session_exit() -> None:
    """进程退出时记录运行时长; 静默消失时这条记录会缺失, 便于定位."""
    try:
        from rdc.core.logging import log_event
    except ImportError:
        return
    log_event("app", "app.exit", uptime_s=round(uptime_seconds(), 2), pid=os.getpid())


def start_heartbeat(app: QObject, interval_ms: int = 10000) -> QObject:
    """用 QTimer 定时写 app.alive, 记录最后存活时点."""
    from PySide6.QtCore import QTimer

    timer = QTimer(app)
    timer.setInterval(interval_ms)
    timer.timeout.connect(heartbeat_once)
    timer.start()
    return timer


def heartbeat_once() -> None:
    """写一条存活心跳, 供定时器与单测共用."""
    try:
        from rdc.core.logging import log_event
    except ImportError:
        return
    log_event("app", "app.alive", uptime_s=round(uptime_seconds(), 2), pid=os.getpid())
