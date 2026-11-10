# ==========================================================================================
# 文件        : media_caps.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 媒体能力协商的兼容出口; 实现已收编到 rdc.media.caps, 此处只做转发
# 架构定位    : 命令层薄壳; 保留既有导入路径, 避免调用方改为引用媒体层
# 核心功能    : 原样转发 default_media_caps / negotiate_media 与版本常量
# 职责边界    : 不持有实现; 不添加上下文, 不改变返回值
# 依赖关系    : rdc.media.caps
# ==========================================================================================

"""媒体能力协商出口.

实现统一在 ``rdc.media.caps``: 被控端、控制端与命令层共用一份协商规则, 不允许各自硬编码.
"""

from __future__ import annotations

from rdc.media.caps import DEFAULT_FRAME_VERSIONS, MODULE, default_media_caps, negotiate_media

__all__ = ["DEFAULT_FRAME_VERSIONS", "MODULE", "default_media_caps", "negotiate_media"]
