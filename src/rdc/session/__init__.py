# ==========================================================================================
# 文件        : __init__.py
# 所属目录    : src/rdc/session/
# 文件用途    : 会话通道包入口, 导出可替换的通道协议与实现
# 架构定位    : 传输层之上的会话字节通道, 与信令/数据面解耦
# 核心功能    : 导出 SessionChannel、ChannelInfo、LoopbackChannel、RelayFrameChannel
# 职责边界    : 不解析文件与剪贴板语义; 不建会话; 不改中继配置
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.session.channel
# ==========================================================================================

"""会话通道."""

from rdc.session.channel import (
    CHANNEL_AUTH_FAILED,
    CHANNEL_BACKPRESSURE,
    CHANNEL_CLOSED,
    CHANNEL_TIMEOUT,
    CHANNEL_UNAVAILABLE,
    ChannelInfo,
    LoopbackChannel,
    RelayFrameChannel,
    SessionChannel,
    SessionChannelError,
    connect_relay_channel,
)

__all__ = [
    "CHANNEL_AUTH_FAILED",
    "CHANNEL_BACKPRESSURE",
    "CHANNEL_CLOSED",
    "CHANNEL_TIMEOUT",
    "CHANNEL_UNAVAILABLE",
    "ChannelInfo",
    "LoopbackChannel",
    "RelayFrameChannel",
    "SessionChannel",
    "SessionChannelError",
    "connect_relay_channel",
]
