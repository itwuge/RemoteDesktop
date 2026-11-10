# ==========================================================================================
# 文件        : caps.py
# 所属目录    : src/rdc/media/
# 文件用途    : 媒体能力协商: 产品路径默认帧版本 v2, v1 只作兼容解码
# 架构定位    : 媒体能力协商的单一出处; 命令层、被控端、控制端都调这里, 不各自硬编码
# 核心功能    : default_media_caps 生成 offer/answer 的 media_caps; negotiate_media 协商并记降级日志
# 职责边界    : 不做帧编解码、不发送、不改契约正文; 不依赖界面与平台实现
# 依赖关系    : rdc.media.frame 的版本常量与 negotiate_frame_version; rdc.core.logging
# ==========================================================================================

"""媒体能力协商.

默认帧版本是 v2: offer/answer 都声明 ``frame_versions: [2, 1]``, 对端也支持 v2 时
双方直接走二进制帧. 只有对端未声明、只声明 v1 或声明了未知版本时才降级 v1, 且必须
留下结构化日志与可读原因, 不允许静默回退到 v1 把带宽悄悄涨回去.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rdc.core.logging import log_event
from rdc.media.frame import VERSION_V1, VERSION_V2, negotiate_frame_version

# 本端支持的帧版本, 按优先级从高到低; v2 是默认路径, v1 只作兼容解码
DEFAULT_FRAME_VERSIONS: tuple[int, ...] = (VERSION_V2, VERSION_V1)
MODULE = "rdc.media.caps"


def default_media_caps(*, max_width: int = 1920, max_height: int = 1080, max_fps: int = 30) -> dict[str, Any]:
    """生成本端 offer/answer 的 media_caps, 默认声明 v2 优先."""
    return {
        "codecs": ["h264"],
        "max_width": max_width,
        "max_height": max_height,
        "max_fps": max_fps,
        "frame_versions": list(DEFAULT_FRAME_VERSIONS),
    }


def negotiate_media(peer_caps: Mapping[str, Any] | None, *, role: str) -> tuple[int, str]:
    """按对端 media_caps 协商帧版本; 降级时返回可展示的中文原因."""
    version = negotiate_frame_version(peer_caps, local_versions=DEFAULT_FRAME_VERSIONS)
    if version == DEFAULT_FRAME_VERSIONS[0]:
        return version, ""
    reason = _degrade_reason(peer_caps)
    log_event(
        MODULE,
        "frame_version.degraded",
        level="WARNING",
        role=role,
        reason=reason,
        frame_version=version,
        peer_frame_versions=_declared_versions(peer_caps),
    )
    return version, reason


def _declared_versions(peer_caps: Mapping[str, Any] | None) -> list[int]:
    """取出对端声明的帧版本, 只用于日志与降级原因, 不参与协商本身."""
    if not peer_caps:
        return []
    declared = peer_caps.get("frame_versions")
    if declared is None:
        declared = peer_caps.get("video_frame_versions")
    if isinstance(declared, (list, tuple, set, frozenset)):
        return [int(item) for item in declared if isinstance(item, int) and not isinstance(item, bool)]
    single = peer_caps.get("frame_version")
    if isinstance(single, int) and not isinstance(single, bool):
        return [int(single)]
    return []


def _degrade_reason(peer_caps: Mapping[str, Any] | None) -> str:
    """给出降级到 v1 的可读原因; 用于日志与界面提示, 不参与协商决策."""
    if not peer_caps:
        return "对端未声明媒体能力, 帧版本降级为 v1"
    declared = _declared_versions(peer_caps)
    if not declared:
        return "对端未声明帧版本, 帧版本降级为 v1"
    if set(declared) == {VERSION_V1}:
        return "对端只声明了 v1, 帧版本降级为 v1"
    return f"对端声明的帧版本 {declared} 与本端不匹配, 帧版本降级为 v1"
