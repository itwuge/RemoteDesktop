"""无控制台防静默退出单测: 标准流补位、异常落盘、会话日志与心跳."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import TracebackType

import pytest

from rdc.core import crashguard
from rdc.core.logging import reset_for_tests


@pytest.fixture(autouse=True)
def isolated_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离日志目录与 logging 单例, 避免用例之间互相影响."""
    monkeypatch.setenv("RDC_LOG_DIR", str(tmp_path / "logs"))
    reset_for_tests()


def _log_events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _event_names(path: Path) -> list[str]:
    return [str(item.get("event", "")) for item in _log_events(path)]


def _boom() -> None:
    raise RuntimeError("probe-crash")


class _Hooks:
    """临时替换全局异常钩子, 退出时恢复原值."""

    def __init__(self) -> None:
        self.excepthook = sys.excepthook
        self.thread_hook = threading.excepthook
        self.unraisablehook = sys.unraisablehook

    def restore(self) -> None:
        sys.excepthook = self.excepthook
        threading.excepthook = self.thread_hook
        sys.unraisablehook = self.unraisablehook


def test_ensure_stdio_replaces_missing_streams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "stdin", None)

    assert crashguard.ensure_stdio() is True
    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdin is not None
    assert sys.stdout.write("probe-stdio\n") > 0
    assert sys.stderr.write("probe-stderr\n") > 0
    assert sys.stdin.read() == ""
    assert sys.stdin.readline() == ""
    assert crashguard.ensure_stdio() is False
    stdio = sorted((tmp_path / "logs").glob("stdio-*.log"))
    assert {item.name.split("-")[1] for item in stdio} == {"stdout", "stderr"}


def test_uptime_is_monotonic() -> None:
    first = crashguard.uptime_seconds()
    second = crashguard.uptime_seconds()
    assert first >= 0
    assert second >= first


def test_main_unhandled_exception_writes_traceback(tmp_path: Path) -> None:
    hooks = _Hooks()
    try:
        crashguard.install_exception_hooks(notify=False)
        try:
            _boom()
        except RuntimeError as error:
            traceback_type: TracebackType | None = error.__traceback__
            sys.excepthook(type(error), error, traceback_type)
    finally:
        hooks.restore()

    text = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert "event=app.unhandled_exception" in text
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: probe-crash" in text
    assert "app.unhandled_exception" in _event_names(tmp_path / "logs" / "rdc.log")


def test_thread_unhandled_exception_writes_traceback(tmp_path: Path) -> None:
    hooks = _Hooks()
    try:
        crashguard.install_exception_hooks(notify=False)
        worker = threading.Thread(target=_boom)
        worker.start()
        worker.join()
    finally:
        hooks.restore()

    text = (tmp_path / "logs" / "crash.log").read_text(encoding="utf-8")
    assert "event=app.thread_exception" in text
    assert "RuntimeError: probe-crash" in text


def test_session_start_and_exit_logged(tmp_path: Path) -> None:
    crashguard.log_session_start(version="0.0.0-test")
    crashguard.log_session_exit()

    names = _event_names(tmp_path / "logs" / "rdc.log")
    assert "app.start" in names
    assert "app.exit" in names
    start = next(item for item in _log_events(tmp_path / "logs" / "rdc.log") if item.get("event") == "app.start")
    assert start["version"] == "0.0.0-test"
    assert start["pid"] > 0


def test_heartbeat_writes_alive_event(tmp_path: Path) -> None:
    crashguard.heartbeat_once()

    names = _event_names(tmp_path / "logs" / "rdc.log")
    assert names.count("app.alive") == 1
