# 文件: __init__.py | 用途: 媒体层包入口 | 边界: 只导出画质策略, 不引入平台实现
"""媒体层: 画质档位与自动降级策略."""

from rdc.media.quality import (
    DIRECT_PROFILES,
    DIRECT_TIERS,
    FLOOR_PROFILE,
    RELAY_ONLY_PROFILES,
    TIERS,
    QualityController,
    QualityEvent,
    QualitySample,
    Tier,
)

__all__ = [
    "DIRECT_PROFILES",
    "DIRECT_TIERS",
    "FLOOR_PROFILE",
    "RELAY_ONLY_PROFILES",
    "TIERS",
    "QualityController",
    "QualityEvent",
    "QualitySample",
    "Tier",
]
