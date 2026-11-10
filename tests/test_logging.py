"""结构化日志、脱敏与诊断包测试."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from rdc.core.debug import debug_enabled, dlog, export_diagnostics
from rdc.core.logging import log_event, recent_events, reset_for_tests

FIXED_FIELDS = {"timestamp", "level", "session_id", "trace_id", "module", "event", "duration_ms", "error_code"}


@pytest.fixture(autouse=True)
def clean_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立日志目录, 且不污染其他测试."""
    monkeypatch.setenv("RDC_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("RDC_DEBUG", raising=False)
    monkeypatch.delenv("RDC_DEBUG_MODULES", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


def _flush_and_read(directory: Path) -> str:
    """刷新 handler 后读取日志文件."""
    for handler in logging.getLogger("rdc").handlers:
        handler.flush()
    return (directory / "logs" / "rdc.log").read_text(encoding="utf-8")


def test_fixed_fields_and_json_line(tmp_path: Path) -> None:
    """落盘为 JSON 行且固定字段齐全."""
    log_event(
        "smoke",
        "smoke.step",
        session_id="session-1",
        trace_id="trace-1",
        duration_ms=12.5,
        error_code="DEVICE_NOT_FOUND",
        step="connect",
    )
    payload = json.loads(_flush_and_read(tmp_path).splitlines()[-1])
    assert set(payload) >= FIXED_FIELDS
    assert payload["module"] == "rdc.smoke"
    assert payload["event"] == "smoke.step"
    assert payload["session_id"] == "session-1"
    assert payload["step"] == "connect"


def test_sensitive_fields_never_plaintext(tmp_path: Path) -> None:
    """口令与令牌落盘前替换为 ***, 明文不出现在文件里."""
    log_event("smoke", "login", password="Passw0rd!", device_token="rdc_deadbeefdeadbeef", note="ok")
    raw = _flush_and_read(tmp_path)
    assert "Passw0rd!" not in raw
    assert "deadbeefdeadbeef" not in raw
    payload = json.loads(raw.splitlines()[-1])
    assert payload["password"] == "***"
    assert payload["device_token"] == "***"


def test_recent_events_and_trace(tmp_path: Path) -> None:
    """最近事件环形缓冲可读, 且不含敏感明文."""
    log_event("smoke", "one", token="secret-token-value")
    events = recent_events()
    assert events[-1]["event"] == "one"
    assert events[-1]["token"] == "***"


def test_diagnostics_pack_has_no_credentials(tmp_path: Path) -> None:
    """诊断包包含环境与指标, 但不含口令令牌."""
    target = tmp_path / "diag.json"
    result = export_diagnostics({"password": "P@ss-123", "metrics": {"p50": 24}}, target)
    assert result["ok"] is True
    text = target.read_text(encoding="utf-8")
    assert "P@ss-123" not in text
    payload = json.loads(text)
    assert payload["extra"]["password"] == "***"
    assert payload["extra"]["metrics"]["p50"] == 24
    assert "recent_events" in payload


def test_debug_switch_defaults_off() -> None:
    """调试默认关闭; 开启后可总控或按模块分级."""
    assert debug_enabled("rdc.net") is False
    dlog("rdc.net", "should.not.appear")
    assert all(event["event"] != "should.not.appear" for event in recent_events())
