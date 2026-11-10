"""desktop-v1 命令层单测: 签名、返回结构、错误码与状态流转."""

from __future__ import annotations

import inspect
import re
import typing

import pytest

from rdc.commands import api
from rdc.commands.service import CommandService
from rdc.core.errors import (
    ACCESS_CODE_INVALID,
    DEVICE_NOT_FOUND,
    HOSTING_ALREADY_ACTIVE,
    HOSTING_NOT_ACTIVE,
    POLICY_INVALID,
    PROFILE_UNSUPPORTED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    TASK_NOT_FOUND,
)
from rdc.core.result import CommandResult

PEER_ID = "A" * 20


EXPECTED_SIGNATURES = {
    "start_connection": ["device_id", "access_code"],
    "get_connection_progress": ["task_id"],
    "cancel_connection": ["task_id"],
    "stop_session": ["session_id"],
    "get_session_status": ["session_id"],
    "list_sessions": [],
    "apply_quality_profile": ["session_id", "profile"],
    "get_session_metrics": ["session_id"],
    "start_hosting": ["display_name"],
    "stop_hosting": [],
    "refresh_access_code": [],
    "open_settings": [],
    "apply_relay_policy": ["endpoint", "egress_mbps", "degrade_profile"],
}


@pytest.fixture()
def service() -> CommandService:
    """每个用例一个干净后端, 并登记一个对端设备."""
    instance = CommandService()
    instance.register_peer(PEER_ID, "测试对端")
    api.configure(instance)
    return instance


def test_thirteen_command_signatures_frozen() -> None:
    """13 个命令函数存在, 参数名与返回类型符合契约."""
    for name, expected in EXPECTED_SIGNATURES.items():
        function = getattr(api, name)
        signature = inspect.signature(function)
        assert list(signature.parameters) == expected, name
        assert typing.get_type_hints(function).get("return") is CommandResult, name
        for parameter in signature.parameters.values():
            assert parameter.default is inspect.Parameter.empty, f"{name}.{parameter.name} 不应有默认值"


def test_command_result_shape() -> None:
    """CommandResult 五字段固定, 空可选字段不输出."""
    result = CommandResult(ok=True, message="ok")
    assert result.to_dict() == {"ok": True, "message": "ok"}
    result = CommandResult(ok=False, message="bad", error_code="X", data={"a": 1})
    assert result.to_dict() == {"ok": False, "message": "bad", "error_code": "X", "data": {"a": 1}}


def test_start_connection_rejects_unknown_device(service: CommandService) -> None:
    """未登记设备返回 DEVICE_NOT_FOUND."""
    result = api.start_connection("B" * 20, "123456")
    assert result.ok is False
    assert result.error_code == DEVICE_NOT_FOUND


def test_start_connection_rejects_bad_code(service: CommandService) -> None:
    """口令非 6 位数字返回 ACCESS_CODE_INVALID."""
    result = api.start_connection(PEER_ID, "12345")
    assert result.ok is False
    assert result.error_code == ACCESS_CODE_INVALID


def test_connection_task_lifecycle(service: CommandService) -> None:
    """连接任务返回句柄, 进度可查, 可取消."""
    started = api.start_connection(PEER_ID, "123456")
    assert started.ok is True
    assert started.data is not None
    task_id = str(started.data["task_id"])
    assert started.data["total"] == 4
    assert task_id in started.message

    progress = api.get_connection_progress(task_id)
    assert progress.ok is True
    assert progress.data == {"completed": 0, "total": 4, "status": "pending", "cancelable": True}

    canceled = api.cancel_connection(task_id)
    assert canceled.ok is True
    assert api.get_connection_progress(task_id).data["status"] == "canceled"
    assert api.cancel_connection(task_id).ok is False
    assert api.get_connection_progress("missing").error_code == TASK_NOT_FOUND


def test_session_busy_when_one_session_exists(service: CommandService) -> None:
    """已有一个会话时第二个连接返回 SESSION_BUSY."""
    service.add_session(PEER_ID)
    result = api.start_connection(PEER_ID, "123456")
    assert result.error_code == SESSION_BUSY


def test_session_commands(service: CommandService) -> None:
    """会话状态、列表、档位、指标与停止."""
    session = service.add_session(PEER_ID, transport_kind="direct", quality_profile="auto")
    status = api.get_session_status(session.session_id)
    assert status.ok is True
    assert status.data == {
        "state": "connected",
        "transport_kind": "direct",
        "quality_profile": "1080p60",
        "peer_device_id": PEER_ID,
    }
    assert len(api.list_sessions().data["items"]) == 1

    applied = api.apply_quality_profile(session.session_id, "1080p30")
    assert applied.ok is True
    assert applied.data["quality_profile"] == "1080p30"

    # 契约 v1.1: 直连四档合法, 中继专用档位在直连会话被拒
    upgraded = api.apply_quality_profile(session.session_id, "4K60")
    assert upgraded.ok is True
    assert upgraded.data["quality_profile"] == "4K60"

    unsupported = api.apply_quality_profile(session.session_id, "720p30")
    assert unsupported.error_code == PROFILE_UNSUPPORTED

    metrics = api.get_session_metrics(session.session_id)
    assert metrics.ok is True
    assert {
        "p50_ms",
        "p95_ms",
        "fps",
        "bitrate_kbps",
        "dropped_frames",
        "ssim",
        "quality_profile",
        "gpu_percent",
        "rss_mb",
        "switches_used",
        "switches_limit",
        "quality_events",
    } == set(metrics.data)

    stopped = api.stop_session(session.session_id)
    assert stopped.ok is True
    assert stopped.data["status"] == "closed"
    assert api.get_session_status(session.session_id).error_code == SESSION_NOT_FOUND


def test_hosting_lifecycle(service: CommandService) -> None:
    """被控启停与口令刷新."""
    started = api.start_hosting("本机")
    assert started.ok is True
    assert re.fullmatch(r"\d{6}", str(started.data["access_code"]))
    assert started.data["expires_at"]
    assert api.start_hosting("本机").error_code == HOSTING_ALREADY_ACTIVE

    refreshed = api.refresh_access_code()
    assert refreshed.ok is True
    assert re.fullmatch(r"\d{6}", str(refreshed.data["access_code"]))

    assert api.stop_hosting().ok is True
    assert api.stop_hosting().ok is False
    assert api.refresh_access_code().error_code == HOSTING_NOT_ACTIVE


def test_settings_and_relay_policy(service: CommandService) -> None:
    """设置页与中继预算计算."""
    assert api.open_settings().data == {"section": "general"}

    applied = api.apply_relay_policy("relay://127.0.0.1:7000", 3, "L0")
    assert applied.ok is True
    assert applied.data["policy"]["budget_kbps"] == 2760

    assert api.apply_relay_policy("", 3, "L0").error_code == POLICY_INVALID
    assert api.apply_relay_policy("relay://x", 0, "L0").error_code == POLICY_INVALID
    assert api.apply_relay_policy("relay://x", 3, "L9").error_code == POLICY_INVALID
