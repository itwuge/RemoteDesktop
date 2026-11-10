# ==========================================================================================
# 文件        : windows_dxcam.py
# 所属目录    : src/rdc/media/
# 文件用途    : Windows 屏幕采集端口实现 (DXGI Desktop Duplication, 经 dxcam)
# 架构定位    : 能力层的平台适配, 只实现 rdc.contracts.ports 定义的 CaptureSource
# 核心功能    : 主屏 BGRA 原始像素采集; 尺寸按 H.264 要求裁成偶数
# 职责边界    : 不编码、不发送、不落盘、不面向界面
# 依赖关系    : rdc.contracts.ports、rdc.core.errors; 运行时依赖 dxcam 与 numpy
# ==========================================================================================

"""Windows DXGI 采集端口.

dxcam 只在 open 时导入, 非 Windows 或缺少依赖时抛 CAPTURE_UNAVAILABLE,
避免把桌面采集依赖变成全平台的导入期硬依赖.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from rdc.contracts.ports import CaptureHandle, CaptureParams, Frame, PortError
from rdc.core.errors import CAPTURE_PERMISSION_DENIED, CAPTURE_TIMEOUT, CAPTURE_UNAVAILABLE

GRAB_TIMEOUT_S = 0.5


@dataclass
class _CameraState:
    """采集私有状态: dxcam 相机与偶数化后的画布尺寸."""

    camera: Any
    width: int = 0
    height: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


def _import_dxcam() -> Any:
    """延迟导入 dxcam; 缺依赖按采集不可用上报."""
    try:
        import dxcam
    except ImportError as error:
        raise PortError(CAPTURE_UNAVAILABLE, "缺少 Windows 采集依赖 dxcam") from error
    return dxcam


def _import_numpy() -> Any:
    """延迟导入 numpy; 缺依赖按采集不可用上报."""
    try:
        import numpy
    except ImportError as error:
        raise PortError(CAPTURE_UNAVAILABLE, "缺少像素处理依赖 numpy") from error
    return numpy


class DxcamCaptureSource:
    """基于 dxcam 的 Windows 主屏采集源."""

    def open(self, params: CaptureParams) -> CaptureHandle:
        """打开指定显示器; output_index 对应 dxcam 的 output_idx."""
        dxcam = _import_dxcam()
        try:
            camera = dxcam.create(output_idx=params.output_index, output_color="BGRA")
        except (OSError, RuntimeError, ValueError) as error:
            raise PortError(CAPTURE_PERMISSION_DENIED, f"无法创建屏幕采集: {error}") from error
        if camera is None:
            raise PortError(CAPTURE_UNAVAILABLE, "屏幕采集对象创建失败, 请确认桌面会话已登录")
        frame = camera.grab()
        if frame is None:
            camera.release()
            raise PortError(CAPTURE_UNAVAILABLE, "屏幕采集首帧为空, 请确认桌面会话已登录")
        height, width = int(frame.shape[0]), int(frame.shape[1])
        if params.even_dimensions:
            width -= width % 2
            height -= height % 2
        return CaptureHandle(value=_CameraState(camera=camera, width=width, height=height), width=width, height=height)

    def read(self, handle: CaptureHandle) -> Frame:
        """抓一帧 BGRA 画面; 规定时间内没有新帧按采集超时上报."""
        state = handle.value
        if not isinstance(state, _CameraState):
            raise PortError(CAPTURE_UNAVAILABLE, "采集句柄无效或已关闭")
        numpy = _import_numpy()
        deadline = time.monotonic() + GRAB_TIMEOUT_S
        image = state.camera.grab()
        while image is None and time.monotonic() < deadline:
            time.sleep(0.002)
            image = state.camera.grab()
        if image is None:
            raise PortError(CAPTURE_TIMEOUT, "屏幕采集超时, 没有取到新帧")
        cropped = image[: state.height, : state.width]
        pixels = numpy.ascontiguousarray(cropped).tobytes()
        return Frame(
            width=state.width,
            height=state.height,
            pixels=pixels,
            pixel_format="bgra",
            monotonic_s=time.monotonic(),
        )

    def close(self, handle: CaptureHandle) -> None:
        """释放采集对象; 可重复调用."""
        state = handle.value
        if isinstance(state, _CameraState):
            with contextlib.suppress(OSError, RuntimeError, AttributeError):
                state.camera.release()
        handle.value = None
