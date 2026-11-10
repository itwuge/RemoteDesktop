# ==========================================================================================
# 文件        : budget.py
# 所属目录    : src/rdc/wall/
# 文件用途    : 屏幕墙布局、解码预算与降级顺序, 对应契约 v1.3 第五节硬约束
# 架构定位    : 业务层纯函数, 不持有状态, 便于单测与界面预演
# 核心功能    : 1/4/9/16 布局; 预览档与聚焦档; 预算单位换算; 三级降级
# 职责边界    : 不做渲染; 不开会话; 不读配置; 不写日志
# ==========================================================================================

"""屏幕墙预算.

硬约束: 任何时刻 4K60 解码路数不得大于 1; 严禁 N 路 4K60 同时解码.
超限降级顺序: 减格 → 预览降 360p10 → 暂停未聚焦格, 每次只做一级.
"""

from __future__ import annotations

from typing import Any

WALL_LAYOUTS = (1, 4, 9, 16)
DEFAULT_DECODE_BUDGET_UNITS = 12
PREVIEW_UNITS = 1
FOCUS_UNITS_4K60 = 4
MAX_CONCURRENT_4K60 = 1

PREVIEW_PROFILE = {"name": "preview_480p15", "width": 854, "height": 480, "fps": 15, "kbps": 300}
PREVIEW_PROFILE_DEGRADED = {"name": "preview_360p10", "width": 640, "height": 360, "fps": 10, "kbps": 180}
FOCUS_PROFILE = {"name": "focus", "width": None, "height": None, "fps": None, "kbps": None}

DEGRADE_STEPS = ("reduce_cells", "lower_preview", "pause_unfocused")


def _cell(index: int, profile: dict[str, Any], state: str) -> dict[str, Any]:
    """构造一格描述."""
    return {
        "cell": index,
        "device_id": None,
        "preview_profile": profile["name"],
        "preview_fps": profile["fps"],
        "preview_kbps": profile["kbps"],
        "state": state,
    }


def plan_wall(
    layout: int,
    budget_units: int = DEFAULT_DECODE_BUDGET_UNITS,
    cells: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按布局与预算生成屏幕墙计划.

    :param layout: 1、4、9、16
    :param budget_units: 解码预算单位, 预览格 1 单位, 4K60 聚焦格 4 单位
    :param cells: 外部给定的格数据（仅补 device_id）, 缺省全部为空
    :return: 含 layout、cells、degraded、degrade_steps、units_used、units_budget
    :raises ValueError: 布局或预算非法
    """
    if layout not in WALL_LAYOUTS:
        raise ValueError("layout 只能为 1、4、9、16")
    if budget_units <= 0:
        raise ValueError("budget_units 必须为正数")

    degrade_steps: list[str] = []
    shown = layout
    if layout * PREVIEW_UNITS > budget_units:
        shown = max(1, budget_units)
        degrade_steps.append("reduce_cells")

    profile = PREVIEW_PROFILE
    if shown * PREVIEW_UNITS > budget_units:  # 第二级: 预览降档
        profile = PREVIEW_PROFILE_DEGRADED
        degrade_steps.append("lower_preview")

    built = []
    for index in range(shown):
        cell = _cell(index, profile, "preview")
        if cells and index < len(cells):
            cell["device_id"] = cells[index].get("device_id")
        built.append(cell)

    return {
        "layout": layout,
        "cells": built,
        "degraded": bool(degrade_steps),
        "degrade_steps": degrade_steps,
        "units_used": shown * PREVIEW_UNITS,
        "units_budget": budget_units,
        "concurrent_4k60": 0,
        "max_concurrent_4k60": MAX_CONCURRENT_4K60,
    }


def focus_cell(
    plan: dict[str, Any],
    cell: int,
    budget_units: int = DEFAULT_DECODE_BUDGET_UNITS,
) -> dict[str, Any]:
    """聚焦某格: 其余格保持预览, 预算不足按下标从大到小暂停未聚焦格.

    :raises ValueError: 格号越界或预算不足以承载聚焦格
    """
    cells = plan["cells"]
    if cell < 0 or cell >= len(cells):
        raise ValueError("cell 超出当前布局范围")
    if budget_units < FOCUS_UNITS_4K60:
        raise ValueError("解码预算不足以承载 4K60 聚焦格")

    for item in cells:
        if item["state"] == "focus" and item["cell"] != cell:
            item["state"] = "preview"
            item["preview_profile"] = PREVIEW_PROFILE["name"]
            item["preview_fps"] = PREVIEW_PROFILE["fps"]
            item["preview_kbps"] = PREVIEW_PROFILE["kbps"]

    selected = cells[cell]
    selected["state"] = "focus"
    selected["preview_profile"] = FOCUS_PROFILE["name"]
    selected["preview_fps"] = None
    selected["preview_kbps"] = None

    degrade_steps: list[str] = []
    remaining_budget = budget_units - FOCUS_UNITS_4K60
    active_previews = [item for item in cells if item["cell"] != cell and item["state"] == "preview"]
    while len(active_previews) > remaining_budget:
        victim = max(active_previews, key=lambda entry: entry["cell"])
        victim["state"] = "paused"
        victim["preview_profile"] = PREVIEW_PROFILE_DEGRADED["name"]
        victim["preview_fps"] = 0
        active_previews.remove(victim)
        if "pause_unfocused" not in degrade_steps:
            degrade_steps.append("pause_unfocused")

    plan["degraded"] = bool(plan["degraded"] or degrade_steps)
    plan["degrade_steps"] = list(dict.fromkeys(list(plan["degrade_steps"]) + degrade_steps))
    plan["units_used"] = FOCUS_UNITS_4K60 + sum(1 for item in cells if item["state"] == "preview")
    plan["concurrent_4k60"] = MAX_CONCURRENT_4K60
    return plan
