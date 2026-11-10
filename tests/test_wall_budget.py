# ==========================================================================================
# 文件        : test_wall_budget.py
# 所属目录    : tests/
# 文件用途    : 屏幕墙布局、解码预算与降级顺序的契约测试
# 架构定位    : 单元测试
# 核心功能    : 覆盖 1/4/9/16 布局、预算超限降级、4K60 唯一性、命令层返回
# 职责边界    : 不渲染; 不解码; 不接网络
# ==========================================================================================

"""屏幕墙测试."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from rdc.accounts.service import AccountService
from rdc.commands import accounts as account_commands
from rdc.commands.wall import WallService
from rdc.core.errors import (
    PERMISSION_DENIED,
    PREVIEW_TOKEN_INVALID,
    WALL_BUDGET_EXCEEDED,
    WALL_NOT_FOUND,
)
from rdc.core.logging import recent_events, reset_for_tests
from rdc.wall.audit import WALL_AUDIT_RETENTION_SECONDS
from rdc.wall.budget import (
    DEFAULT_DECODE_BUDGET_UNITS,
    MAX_CONCURRENT_4K60,
    focus_cell,
    plan_wall,
)

WALL_ACCOUNT = "alice@example.com"
WALL_DEVICE = "AAAABBBBCCCCDDDDEEEE"


def _wall_with_device(layout: int = 4) -> tuple[WallService, str]:
    """构造带一格已绑定设备的屏幕墙."""
    service = WallService(account_id=WALL_ACCOUNT)
    wall_id = service.start_wall(layout).data["wall_id"]
    service.walls[wall_id].plan["cells"][0]["device_id"] = WALL_DEVICE
    return service, wall_id


@pytest.mark.parametrize("layout", [1, 4, 9])
def test_layouts_within_budget(layout: int) -> None:
    """预算内的布局不降级."""
    plan = plan_wall(layout)
    assert len(plan["cells"]) == layout
    assert plan["degraded"] is False
    assert plan["concurrent_4k60"] == 0


def test_layout_16_exceeds_budget_and_reduces_cells() -> None:
    """16 路超过 12 单位预算, 先减格."""
    plan = plan_wall(16, DEFAULT_DECODE_BUDGET_UNITS)
    assert plan["degraded"] is True
    assert "reduce_cells" in plan["degrade_steps"]
    assert len(plan["cells"]) <= DEFAULT_DECODE_BUDGET_UNITS


def test_invalid_layout_rejected() -> None:
    """非 1/4/9/16 布局被拒."""
    with pytest.raises(ValueError):
        plan_wall(6)


def test_focus_only_one_4k60() -> None:
    """聚焦后 4K60 路数恒为 1, 未聚焦格按预算暂停."""
    plan = plan_wall(9, DEFAULT_DECODE_BUDGET_UNITS)
    focused = focus_cell(plan, 0, DEFAULT_DECODE_BUDGET_UNITS)
    assert focused["concurrent_4k60"] == MAX_CONCURRENT_4K60 == 1
    assert focused["cells"][0]["state"] == "focus"
    previewing = [cell for cell in focused["cells"] if cell["cell"] != 0 and cell["state"] == "preview"]
    assert len(previewing) <= DEFAULT_DECODE_BUDGET_UNITS - 4
    assert focused["units_used"] <= DEFAULT_DECODE_BUDGET_UNITS


def test_focus_out_of_range() -> None:
    """越界格号被拒."""
    plan = plan_wall(4)
    with pytest.raises(ValueError):
        focus_cell(plan, 5)


def test_command_layer_round_trip() -> None:
    """命令层: 创建、切布局、聚焦、关闭."""
    service = WallService()
    started = service.start_wall(4)
    assert started.ok and started.data["layout"] == 4
    wall_id = started.data["wall_id"]

    relayout = service.set_wall_layout(wall_id, 9)
    assert relayout.ok and len(relayout.data["cells"]) == 9

    focused = service.focus_wall_cell(wall_id, 2)
    assert focused.ok and focused.data["quality_profile"] == "auto"
    assert focused.data["concurrent_4k60"] == 1

    closed = service.close_wall(wall_id)
    assert closed.ok and closed.data["status"] == "closed"
    assert service.focus_wall_cell(wall_id, 0).error_code == WALL_NOT_FOUND


def test_wall_invalid_layout_and_cell() -> None:
    """命令层校验布局与格号."""
    service = WallService()
    assert not service.start_wall(6).ok
    wall_id = service.start_wall(4).data["wall_id"]
    assert service.focus_wall_cell(wall_id, 9).error_code == WALL_BUDGET_EXCEEDED
    assert service.close_wall("wall-missing").error_code == WALL_NOT_FOUND


def test_focus_after_layout_shrink_clears_focus() -> None:
    """聚焦格在新布局越界时清空聚焦标记, 不抛异常."""
    service = WallService()
    wall_id = service.start_wall(9).data["wall_id"]
    service.focus_wall_cell(wall_id, 8)
    shrunk = service.set_wall_layout(wall_id, 4)
    assert shrunk.ok
    assert service.walls[wall_id].focused_cell is None


def test_refocus_switches_single_focus() -> None:
    """同一块屏幕墙换格聚焦时旧聚焦格退回预览, 始终只有一格聚焦."""
    service = WallService()
    wall_id = service.start_wall(9).data["wall_id"]
    assert service.focus_wall_cell(wall_id, 0).ok
    again = service.focus_wall_cell(wall_id, 5)
    assert again.ok
    focused = [cell for cell in again.data["cells"] if cell["state"] == "focus"]
    assert [cell["cell"] for cell in focused] == [5]
    assert again.data["concurrent_4k60"] == 1


def test_focus_rejected_when_budget_below_4k60() -> None:
    """预算不足 4 单位时拒绝聚焦, 不得超出预算解码 4K60."""
    service = WallService(budget_units=3)
    wall_id = service.start_wall(1).data["wall_id"]
    result = service.focus_wall_cell(wall_id, 0)
    assert not result.ok and result.error_code == WALL_BUDGET_EXCEEDED
    assert service.walls[wall_id].focused_cell is None


def test_focus_blocked_when_other_wall_focused() -> None:
    """跨屏幕墙也只允许 1 路 4K60 聚焦, 关闭后释放名额."""
    service = WallService()
    first = service.start_wall(4).data["wall_id"]
    second = service.start_wall(4).data["wall_id"]
    assert service.focus_wall_cell(first, 0).ok
    blocked = service.focus_wall_cell(second, 1)
    assert not blocked.ok and blocked.error_code == WALL_BUDGET_EXCEEDED
    assert service.close_wall(first).ok
    assert service.focus_wall_cell(second, 1).ok


def test_relayout_preserves_focus_in_range() -> None:
    """缩小布局时聚焦格仍在范围内则保持聚焦, 越界才取消."""
    service = WallService()
    wall_id = service.start_wall(9).data["wall_id"]
    service.focus_wall_cell(wall_id, 2)
    kept = service.set_wall_layout(wall_id, 4)
    assert kept.ok
    assert service.walls[wall_id].focused_cell == 2
    assert kept.data["cells"][2]["state"] == "focus"
    assert kept.data["concurrent_4k60"] == 1

    shrunk = service.set_wall_layout(wall_id, 1)
    assert shrunk.ok
    assert service.walls[wall_id].focused_cell is None
    assert shrunk.data["concurrent_4k60"] == 0


def test_degrade_writes_audit_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """降级结果写入结构化审计事件 wall.degrade."""
    monkeypatch.setenv("RDC_LOG_DIR", str(tmp_path))
    reset_for_tests()
    try:
        WallService().start_wall(16)
        events = [item["event"] for item in recent_events()]
        assert "wall.degrade" in events
    finally:
        reset_for_tests()


def test_stop_focus_returns_to_preview() -> None:
    """退回全预览释放 4K60 名额并写入固定审计事件."""
    service = WallService()
    wall_id = service.start_wall(9).data["wall_id"]
    assert service.focus_wall_cell(wall_id, 3).ok
    stopped = service.stop_wall_focus(wall_id)
    assert stopped.ok
    assert stopped.data["focused_cell"] is None
    assert stopped.data["quality_profile"] == "preview_480p15"
    assert stopped.data["concurrent_4k60"] == 0
    assert service.walls[wall_id].focused_cell is None
    assert all(cell["state"] == "preview" for cell in stopped.data["cells"])
    assert service.stop_wall_focus("missing-wall").error_code == WALL_NOT_FOUND


def test_wall_audit_events_and_closed_retention() -> None:
    """8 类固定事件可读; 关闭后 30 天内可读, 超期返回 WALL_NOT_FOUND."""
    service = WallService(audit_guard=lambda wall_id: True)
    wall_id = service.start_wall(16).data["wall_id"]
    service.set_wall_layout(wall_id, 4)
    service.focus_wall_cell(wall_id, 0)
    service.stop_wall_focus(wall_id)
    service.close_wall(wall_id)

    log = service.get_wall_audit_log(wall_id)
    assert log.ok
    names = {item["event"] for item in log.data["items"]}
    assert {"wall.start", "wall.layout", "wall.focus", "wall.focus.stop", "wall.close", "wall.degrade"} <= names
    for item in log.data["items"]:
        assert set(item) == {"ts", "event", "actor", "target", "result", "detail"}

    service.archived[wall_id] = time.time() - WALL_AUDIT_RETENTION_SECONDS - 1
    assert service.get_wall_audit_log(wall_id).error_code == WALL_NOT_FOUND
    assert service.get_wall_audit_log("never-existed").error_code == WALL_NOT_FOUND


def test_wall_audit_returns_latest_1000() -> None:
    """单次读取最多返回最近 1000 条."""
    service = WallService(audit_guard=lambda wall_id: True)
    wall_id = service.start_wall(4).data["wall_id"]
    for _ in range(1004):
        service.set_wall_layout(wall_id, 4)
    items = service.get_wall_audit_log(wall_id).data["items"]
    assert len(items) == 1000
    assert items[-1]["event"] == "wall.layout"


def test_wall_audit_permission_denied() -> None:
    """审计读取守卫拒绝时返回 PERMISSION_DENIED."""
    service = WallService(audit_guard=lambda wall_id: False)
    wall_id = service.start_wall(4).data["wall_id"]
    result = service.get_wall_audit_log(wall_id)
    assert not result.ok and result.error_code == PERMISSION_DENIED


def test_wall_audit_denied_without_authenticated_account() -> None:
    """未接线账号或未登录时默认拒绝, 不得当作放行."""
    account_commands.configure(AccountService())
    service = WallService(account_id=WALL_ACCOUNT)
    wall_id = service.start_wall(4).data["wall_id"]
    service.walls[wall_id].plan["cells"][0]["device_id"] = WALL_DEVICE
    assert service.get_wall_audit_log(wall_id).error_code == PERMISSION_DENIED


def test_wall_audit_allowed_for_device_owner() -> None:
    """设备所有者登录后经账号身份判定可读审计."""
    account_service = AccountService()
    account_service.register_device(WALL_DEVICE)
    assert account_service.register_account(WALL_ACCOUNT, "password123").ok
    assert account_service.login_account(WALL_ACCOUNT, "password123").ok
    assert account_service.bind_device(WALL_DEVICE, "123456").ok
    account_commands.configure(account_service)
    service = WallService(account_id=WALL_ACCOUNT)
    wall_id = service.start_wall(4).data["wall_id"]
    service.walls[wall_id].plan["cells"][0]["device_id"] = WALL_DEVICE
    assert service.get_wall_audit_log(wall_id).ok


def test_preview_token_issue_validate_and_revoke_paths() -> None:
    """预览令牌 60 秒绑定墙+格+账号+设备; 重签、过期、设备变更与关闭墙都立即失效."""
    service, wall_id = _wall_with_device()
    service.audit_guard = lambda wall_id: True
    no_account = WallService()
    wall_without_account = no_account.start_wall(4).data["wall_id"]
    assert no_account.issue_preview_token(wall_without_account, 0).error_code == PERMISSION_DENIED
    assert service.issue_preview_token(wall_id, 1).error_code == PERMISSION_DENIED
    assert service.issue_preview_token(wall_id, 99).error_code == WALL_BUDGET_EXCEEDED

    first = service.issue_preview_token(wall_id, 0)
    assert first.ok and first.data["scope"] == "view" and first.data["single_use"] is False
    token = first.data["token"]
    assert service.validate_preview_token(token, wall_id, 0, WALL_ACCOUNT).ok
    assert service.validate_preview_token("forged", wall_id, 0, WALL_ACCOUNT).error_code == PREVIEW_TOKEN_INVALID
    assert service.validate_preview_token(token, wall_id, 1, WALL_ACCOUNT).error_code == PREVIEW_TOKEN_INVALID
    assert service.validate_preview_token(token, wall_id, 0, "other@example.com").error_code == PREVIEW_TOKEN_INVALID

    second = service.issue_preview_token(wall_id, 0)
    assert second.ok
    assert service.validate_preview_token(token, wall_id, 0, WALL_ACCOUNT).error_code == PREVIEW_TOKEN_INVALID
    assert service.validate_preview_token(second.data["token"], wall_id, 0, WALL_ACCOUNT).ok

    record = service.preview_tokens.lookup(second.data["token"])
    assert record is not None
    record.expires_at = time.time() - 1
    expired = service.validate_preview_token(second.data["token"], wall_id, 0, WALL_ACCOUNT)
    assert expired.error_code == PREVIEW_TOKEN_INVALID

    third = service.issue_preview_token(wall_id, 0)
    service.walls[wall_id].plan["cells"][0]["device_id"] = "ZZZZZZZZZZZZZZZZZZZZ"
    changed = service.validate_preview_token(third.data["token"], wall_id, 0, WALL_ACCOUNT)
    assert changed.error_code == PREVIEW_TOKEN_INVALID

    service.walls[wall_id].plan["cells"][0]["device_id"] = WALL_DEVICE
    fourth = service.issue_preview_token(wall_id, 0)
    assert fourth.ok
    assert service.close_wall(wall_id).ok
    closed = service.validate_preview_token(fourth.data["token"], wall_id, 0, WALL_ACCOUNT)
    assert closed.error_code == PREVIEW_TOKEN_INVALID

    items = service.get_wall_audit_log(wall_id).data["items"]
    names = [item["event"] for item in items]
    assert "wall.preview_token.issue" in names and "wall.preview_token.reject" in names
    assert fourth.data["token"] not in str([item["detail"] for item in items])
