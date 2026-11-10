# 文件: __init__.py | 用途: 媒体层包入口 | 边界: 导出画质策略与帧封装, 不引入平台实现
"""媒体层: 画质档位、自动降级策略与视频帧线格式."""

from rdc.media.frame import (
    FrameError,
    VideoFrame,
    decode_frame,
    decode_v1,
    decode_v2,
    encode_v2,
    negotiate_frame_version,
)
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
    "FrameError",
    "QualityController",
    "QualityEvent",
    "QualitySample",
    "Tier",
    "VideoFrame",
    "decode_frame",
    "decode_v1",
    "decode_v2",
    "encode_v2",
    "negotiate_frame_version",
]
