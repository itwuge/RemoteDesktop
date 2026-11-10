# ==========================================================================================
# 文件        : linux_x11.py
# 所属目录    : src/rdc/media/
# 文件用途    : Linux (X11) 屏幕采集与 H.264 编码端口实现, 供被控端命令层装配使用
# 架构定位    : 能力层的平台适配, 只实现 rdc.contracts.ports 定义的端口
# 核心功能    : mss 采集主屏 BGRA 原始像素; PyAV libx264 低延迟编码, 每帧自带 SPS/PPS
# 职责边界    : 不解析画面内容; 不落盘; 不直接面向界面; 不访问公网
# 依赖关系    : rdc.contracts.ports、rdc.core.errors; 运行时依赖 mss、numpy、av
# ==========================================================================================

"""Linux X11 采集与编码端口.

mss 只在 open/read 时导入, 非 Linux 环境或缺少依赖时抛 CAPTURE_UNAVAILABLE,
避免把可选依赖变成产品导入期硬依赖.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from rdc.contracts.ports import (
    CaptureHandle,
    CaptureParams,
    EncodedPacket,
    EncoderHandle,
    EncoderParams,
    Frame,
    PortError,
)
from rdc.core.errors import (
    CAPTURE_PERMISSION_DENIED,
    CAPTURE_TIMEOUT,
    CAPTURE_UNAVAILABLE,
    ENCODE_FAILED,
    ENCODER_UNAVAILABLE,
)

X264_OPTIONS = {
    "preset": "ultrafast",
    "tune": "zerolatency",
    "x264-params": "bframes=0:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1",
}


@dataclass(frozen=True)
class _ScreenSize:
    """采集句柄里的平台私有状态, 只记录采集区域尺寸."""

    width: int
    height: int


@dataclass
class _EncoderState:
    """编码句柄里的平台私有状态; pts 由端口内部累加, 调用方无需维护帧号."""

    context: Any
    pts: int = 0


def _import_mss() -> Any:
    """延迟导入 mss; 缺依赖按端口错误码上报."""
    try:
        import mss
    except ImportError as error:
        raise PortError(CAPTURE_UNAVAILABLE, "缺少 X11 采集依赖 mss") from error
    return mss


def _import_av() -> Any:
    """延迟导入 PyAV; 缺依赖按端口错误码上报."""
    try:
        import av
    except ImportError as error:
        raise PortError(ENCODER_UNAVAILABLE, "缺少 H.264 编码依赖 av") from error
    return av


def _screen_size(handle: CaptureHandle) -> _ScreenSize:
    """取出采集尺寸; 句柄类型不对说明已被关闭或来自别的实现."""
    size = handle.value
    if not isinstance(size, _ScreenSize):
        raise PortError(CAPTURE_UNAVAILABLE, "采集句柄无效或已关闭")
    return size


class X11CaptureSource:
    """用 mss 采集 X11 主屏, 返回 BGRA 原始字节; 每个线程持有独立 mss 实例."""

    def __init__(self) -> None:
        self._local = threading.local()

    def open(self, params: CaptureParams) -> CaptureHandle:
        """探测显示器并登记采集区域; 宽高按需向下取偶数以适配 yuv420p."""
        mss = _import_mss()
        try:
            with mss.MSS() as screen:
                monitors = screen.monitors
                index = params.output_index + 1
                if index >= len(monitors):
                    raise PortError(CAPTURE_UNAVAILABLE, "未检测到可采集的 X11 显示器")
                monitor = monitors[index]
                width = int(monitor["width"])
                height = int(monitor["height"])
        except mss.ScreenShotError as error:
            raise PortError(CAPTURE_PERMISSION_DENIED, f"X11 采集被拒绝: {error}") from error
        except OSError as error:
            raise PortError(CAPTURE_UNAVAILABLE, f"X11 采集不可用: {error}") from error
        if width <= 0 or height <= 0:
            raise PortError(CAPTURE_UNAVAILABLE, "显示器尺寸无效")
        if params.even_dimensions:
            width = width // 2 * 2
            height = height // 2 * 2
        return CaptureHandle(value=_ScreenSize(width, height), width=width, height=height)

    def read(self, handle: CaptureHandle) -> Frame:
        """抓一帧主屏; 抓取失败按 CAPTURE_TIMEOUT 上报, 由调用方决定是否跳帧."""
        size = _screen_size(handle)
        screen = self._thread_screen()
        mss = _import_mss()
        try:
            shot = screen.grab({"left": 0, "top": 0, "width": size.width, "height": size.height})
        except mss.ScreenShotError as error:
            raise PortError(CAPTURE_TIMEOUT, f"抓屏失败: {error}") from error
        except OSError as error:
            raise PortError(CAPTURE_TIMEOUT, f"X11 连接中断: {error}") from error
        if shot.width != size.width or shot.height != size.height:
            raise PortError(CAPTURE_TIMEOUT, "抓屏尺寸与采集句柄不一致")
        return Frame(
            width=shot.width,
            height=shot.height,
            pixels=shot.bgra,
            monotonic_s=time.monotonic(),
        )

    def close(self, handle: CaptureHandle) -> None:
        """释放当前线程的 mss 实例; 其他线程的实例随线程退出回收."""
        handle.value = None
        screen = getattr(self._local, "screen", None)
        if screen is not None:
            with suppress(OSError):
                screen.close()
            self._local.screen = None

    def _thread_screen(self) -> Any:
        """取本线程的 mss 实例, 首次调用时创建."""
        screen = getattr(self._local, "screen", None)
        if screen is not None:
            return screen
        mss = _import_mss()
        try:
            screen = mss.MSS()
        except mss.ScreenShotError as error:
            raise PortError(CAPTURE_PERMISSION_DENIED, f"X11 采集被拒绝: {error}") from error
        except OSError as error:
            raise PortError(CAPTURE_UNAVAILABLE, f"X11 采集不可用: {error}") from error
        self._local.screen = screen
        return screen


class Libx264Encoder:
    """常驻 libx264 低延迟编码器: 每帧自带 SPS/PPS, 控制端可独立解码."""

    def open(self, params: EncoderParams) -> EncoderHandle:
        """按目标尺寸、帧率与码率打开编码器."""
        if params.width <= 0 or params.height <= 0 or params.fps <= 0 or params.bitrate_bps <= 0:
            raise PortError(ENCODER_UNAVAILABLE, "编码参数无效")
        av = _import_av()
        try:
            context = av.CodecContext.create("libx264", "w")
            context.width = params.width
            context.height = params.height
            context.pix_fmt = "yuv420p"
            context.framerate = Fraction(params.fps, 1)
            context.time_base = Fraction(1, params.fps)
            context.bit_rate = params.bitrate_bps
            context.options = dict(X264_OPTIONS)
            context.open()
        except av.FFmpegError as error:
            raise PortError(ENCODER_UNAVAILABLE, f"libx264 不可用: {error}") from error
        except ValueError as error:
            raise PortError(ENCODER_UNAVAILABLE, f"编码参数被拒绝: {error}") from error
        return EncoderHandle(value=_EncoderState(context=context))

    def encode(self, handle: EncoderHandle, frame: Frame) -> list[EncodedPacket]:
        """编码一帧 BGRA 画面, 返回本次产生的全部编码包."""
        state = handle.value
        if not isinstance(state, _EncoderState):
            raise PortError(ENCODE_FAILED, "编码句柄无效或已关闭")
        if len(frame.pixels) != frame.width * frame.height * 4:
            raise PortError(ENCODE_FAILED, "帧尺寸与像素数据不一致")
        av = _import_av()
        numpy = _import_numpy()
        image = numpy.frombuffer(frame.pixels, dtype=numpy.uint8).reshape(frame.height, frame.width, 4)
        frame_pts = state.pts
        try:
            video = av.VideoFrame.from_ndarray(image, format="bgra").reformat(format="yuv420p")
            video.pts = frame_pts
            packets = state.context.encode(video)
        except (av.FFmpegError, ValueError) as error:
            raise PortError(ENCODE_FAILED, f"H.264 编码失败: {error}") from error
        state.pts += 1
        return [
            EncodedPacket(data=bytes(packet), keyframe=bool(packet.is_keyframe), pts=frame_pts) for packet in packets
        ]

    def close(self, handle: EncoderHandle) -> None:
        """丢弃编码上下文; PyAV 无显式 close, 句柄释放即回收原生资源."""
        handle.value = None


def _import_numpy() -> Any:
    """延迟导入 numpy; 缺依赖按编码错误上报."""
    try:
        import numpy
    except ImportError as error:
        raise PortError(ENCODE_FAILED, "缺少像素处理依赖 numpy") from error
    return numpy
