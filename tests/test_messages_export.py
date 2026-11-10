"""消息导出命令: CSV 列序、BOM、公式注入防护与失败分支."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from rdc.commands import messages
from rdc.core.errors import CONFIG_INVALID, TRANSFER_IO_ERROR
from rdc.core.result import CommandResult

ITEMS = [
    {
        "ts": "2026-11-11T00:00:00+08:00",
        "category_label": "会话",
        "event": "session.start",
        "title": "会话开始",
        "device_name": "客厅电脑",
        "device_id": "ABCDEFGHIJKLMNOPQRST",
        "result": "ok",
        "detail": {"b": 2, "a": 1},
    },
    {
        "ts": "2026-11-11T00:01:00+08:00",
        "category_label": "审批",
        "event": "access.request",
        "title": '=HYPERLINK("http://x")',
        "device_name": "书房电脑",
        "device_id": "TSRQPONMLKJIHGFEDCBA",
        "result": "deny",
        "detail": {},
    },
]


def _stub(monkeypatch: pytest.MonkeyPatch, items: list[dict]) -> None:
    monkeypatch.setattr(
        messages,
        "list_messages",
        lambda limit=0: CommandResult(ok=True, message="ok", data={"items": items, "unread": 0, "total": len(items)}),
    )


def test_export_writes_utf8_bom_csv_with_fixed_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, ITEMS)
    target = tmp_path / "messages.csv"
    result = messages.export_messages(str(target))
    assert result.ok, result.message
    assert result.data == {"path": str(target), "rows": 2}
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")

    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == list(messages.EXPORT_COLUMNS)
    assert rows[1][1:3] == ["会话", "session.start"]
    assert rows[1][7] == '{"a": 1, "b": 2}'
    assert rows[2][3] == '\'=HYPERLINK("http://x")'


def test_export_rejects_empty_path(tmp_path: Path) -> None:
    result = messages.export_messages("  ")
    assert not result.ok
    assert result.error_code == CONFIG_INVALID


def test_export_propagates_list_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failure = CommandResult(ok=False, message="请先登录账号", error_code=CONFIG_INVALID)
    monkeypatch.setattr(messages, "list_messages", lambda limit=0: failure)
    result = messages.export_messages(str(tmp_path / "out.csv"))
    assert result is failure


def test_export_reports_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, ITEMS)
    result = messages.export_messages(str(tmp_path / "missing" / "out.csv"))
    assert not result.ok
    assert result.error_code == TRANSFER_IO_ERROR
    assert result.message.startswith("导出失败")
