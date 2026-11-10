# 文件: __init__.py | 用途: 端口与值对象包入口 | 边界: 只放契约端口定义, 不引入平台实现
"""契约端口层: desktop-v1 第五节的端口签名与值对象."""

from rdc.contracts.ports import (
    CaptureHandle,
    CaptureParams,
    CaptureSource,
    EncodedPacket,
    Encoder,
    EncoderHandle,
    EncoderParams,
    Frame,
    InputEvent,
    InputInjector,
    PortError,
)

__all__ = [
    "CaptureHandle",
    "CaptureParams",
    "CaptureSource",
    "EncodedPacket",
    "Encoder",
    "EncoderHandle",
    "EncoderParams",
    "Frame",
    "InputEvent",
    "InputInjector",
    "PortError",
]
