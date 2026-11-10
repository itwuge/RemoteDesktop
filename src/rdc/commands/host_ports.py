# ==========================================================================================
# 文件        : host_ports.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 被控端媒体端口装配: 命令层按平台选择采集、编码与输入注入实现
# 架构定位    : 命令层的装配入口; 界面与验收脚本只拿端口对象, 不直接引用平台模块
# 核心功能    : 创建 CaptureSource / Encoder / InputInjector; 启动前采集自检
# 职责边界    : 不实现具体平台行为; 不解析控制消息; 不生成用户文案
# 依赖关系    : rdc.contracts.ports、rdc.media.h264、rdc.media.windows_dxcam、rdc.media.linux_x11
# ==========================================================================================

"""被控端平台端口装配.

Windows 走 DXGI 采集与 SendInput, Linux 走 X11 采集与 xdotool; 编码统一走
``rdc.media.h264``, 硬件可用时优先硬件. 其他平台按契约错误码上报, 由命令层给提示.
"""

from __future__ import annotations

import os
import sys

from rdc.contracts.ports import CaptureHandle, CaptureParams, CaptureSource, Encoder, InputInjector, PortError
from rdc.core.errors import CAPTURE_UNAVAILABLE, INPUT_PERMISSION_DENIED
from rdc.input.linux_x11 import XdotoolInputInjector
from rdc.input.windows_sendinput import SendInputInjector
from rdc.media.h264 import H264Encoder
from rdc.media.linux_x11 import X11CaptureSource
from rdc.media.windows_dxcam import DxcamCaptureSource


def create_capture_source() -> CaptureSource:
    """返回当前平台的采集端口; 不支持的平台按 CAPTURE_UNAVAILABLE 上报."""
    if sys.platform.startswith("win"):
        return DxcamCaptureSource()
    if sys.platform.startswith("linux"):
        return X11CaptureSource()
    raise PortError(CAPTURE_UNAVAILABLE, "当前平台未实现屏幕采集端口")


def create_encoder() -> Encoder:
    """返回当前平台的编码端口; 硬件优先, 不可用时回落 libx264 并记日志."""
    return H264Encoder()


def create_input_injector(display: str | None = None) -> InputInjector:
    """返回当前平台的输入注入端口; 不支持的平台按 INPUT_PERMISSION_DENIED 上报."""
    if sys.platform.startswith("win"):
        return SendInputInjector()
    if sys.platform.startswith("linux"):
        return XdotoolInputInjector(display or os.environ.get("DISPLAY", ":0"))
    raise PortError(INPUT_PERMISSION_DENIED, "当前平台未实现输入注入端口")


def probe_capture() -> str:
    """打开并关闭一次采集端口; 返回空串表示可用, 否则返回错误码与原因."""
    source: CaptureSource | None = None
    handle: CaptureHandle | None = None
    try:
        source = create_capture_source()
        handle = source.open(CaptureParams())
    except PortError as error:
        return f"{error.code}: {error}"
    finally:
        if source is not None and handle is not None:
            source.close(handle)
    return ""
