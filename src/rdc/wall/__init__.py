# ==========================================================================================
# 文件        : __init__.py
# 所属目录    : src/rdc/wall/
# 文件用途    : 屏幕墙包入口
# 架构定位    : 业务层
# 核心功能    : 导出预算函数与常量
# 职责边界    : 不做初始化副作用
# ==========================================================================================

"""屏幕墙预算."""

from rdc.wall.budget import (
    DEFAULT_DECODE_BUDGET_UNITS,
    MAX_CONCURRENT_4K60,
    WALL_LAYOUTS,
    focus_cell,
    plan_wall,
)

__all__ = [
    "DEFAULT_DECODE_BUDGET_UNITS",
    "MAX_CONCURRENT_4K60",
    "WALL_LAYOUTS",
    "focus_cell",
    "plan_wall",
]
