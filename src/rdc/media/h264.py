# ==========================================================================================
# 文件        : h264.py
# 所属目录    : src/rdc/media/
# 文件用途    : 跨平台 H.264 编解码实现, 优先硬件编码, 硬编不可用时回落软编并记日志
# 架构定位    : 能力层; 被控端编码与控制端解码的唯一实现, 平台端口只做像素搬运
# 核心功能    : H264Encoder(硬编优先) / H264Decoder(输出 BGRA 原始像素)
# 职责边界    : 不采集、不发送、不落盘、不面向界面; 只处理编码参数与字节
# 依赖关系    : rdc.contracts.ports; 运行时依赖 av 与 numpy
# ==========================================================================================

"""H.264 编解码.

硬件编码按平台候选顺序尝试, 全部失败才回落 libx264, 并写一条 WARNING 结构化日志,
让"硬编不可用"这件事在日志里可查, 不是静默降级.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from rdc.contracts.ports import EncodedPacket, EncoderHandle, EncoderParams, Frame, PortError
from rdc.core.errors import DECODE_FAILED, DECODER_UNAVAILABLE, ENCODE_FAILED, ENCODER_UNAVAILABLE
from rdc.core.logging import log_event

MODULE = "rdc.media.h264"
X264_OPTIONS = {
    "preset": "ultrafast",
    "tune": "zerolatency",
    "x264-params": "bframes=0:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1",
}
HARDWARE_OPTIONS: dict[str, dict[str, str]] = {
    "h264_nvenc": {"preset": "p1", "tune": "ull", "rc": "cbr", "g": "30", "bf": "0"},
    "h264_qsv": {"preset": "veryfast", "low_delay_brc": "1", "async_depth": "1", "g": "30", "bf": "0"},
    "h264_amf": {"usage": "ultralowlatency", "quality": "speed", "rc": "cbr", "g": "30", "bf": "0"},
    "h264_vaapi": {"rc_mode": "CBR", "g": "30", "bf": "0"},
}
LINUX_CANDIDATES: tuple[str, ...] = ("h264_vaapi", "h264_nvenc", "h264_qsv")
WINDOWS_CANDIDATES: tuple[str, ...] = ("h264_nvenc", "h264_qsv", "h264_amf")
SOFTWARE_CODEC = "libx264"


def _import_av() -> Any:
    """延迟导入 av; 缺依赖按编码器不可用上报."""
    try:
        import av
    except ImportError as error:
        raise PortError(ENCODER_UNAVAILABLE, "缺少编解码依赖 av") from error
    return av


def hardware_candidates() -> tuple[str, ...]:
    """按当前平台给出硬件编码候选顺序; 不支持的平台返回空元组."""
    if sys.platform.startswith("win"):
        return WINDOWS_CANDIDATES
    if sys.platform.startswith("linux"):
        return LINUX_CANDIDATES
    return ()


class _EncoderState:
    """编码器私有状态: 编码上下文与单调递增的 pts."""

    def __init__(self, context: Any, codec: str) -> None:
        self.context = context
        self.codec = codec
        self.pts = 0


class H264Encoder:
    """常驻低延迟 H.264 编码器: 每帧自带 SPS/PPS, 控制端可独立解码."""

    def open(self, params: EncoderParams) -> EncoderHandle:
        """按目标尺寸、帧率与码率打开编码器, 硬件优先."""
        if params.width <= 0 or params.height <= 0 or params.fps <= 0 or params.bitrate_bps <= 0:
            raise PortError(ENCODER_UNAVAILABLE, "编码参数无效")
        av = _import_av()
        failures: list[str] = []
        for codec in hardware_candidates():
            context = _try_open(av, codec, params)
            if context is not None:
                log_event(
                    MODULE,
                    "encoder.opened",
                    codec=codec,
                    hardware=True,
                    width=params.width,
                    height=params.height,
                )
                return EncoderHandle(value=_EncoderState(context=context, codec=codec))
            failures.append(codec)
        context = _try_open(av, SOFTWARE_CODEC, params)
        if context is None:
            candidates = "/".join([*failures, SOFTWARE_CODEC])
            raise PortError(ENCODER_UNAVAILABLE, f"没有可用的 H.264 编码器: {candidates}")
        log_event(
            MODULE,
            "encoder.fallback",
            level="WARNING",
            codec=SOFTWARE_CODEC,
            hardware=False,
            failed_hardware=failures,
            reason="硬件编码不可用, 已回落到软件编码 libx264",
        )
        return EncoderHandle(value=_EncoderState(context=context, codec=SOFTWARE_CODEC))

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


def _try_open(av: Any, codec: str, params: EncoderParams) -> Any | None:
    """尝试打开一个编码器; 失败返回 None 由调用方继续尝试下一个候选."""
    try:
        context = av.CodecContext.create(codec, "w")
        context.width = params.width
        context.height = params.height
        context.pix_fmt = "yuv420p"
        context.framerate = Fraction(params.fps, 1)
        context.time_base = Fraction(1, params.fps)
        context.bit_rate = params.bitrate_bps
        context.options = dict(X264_OPTIONS if codec == SOFTWARE_CODEC else HARDWARE_OPTIONS.get(codec, {}))
        context.open()
    except (av.FFmpegError, ValueError, OSError):
        return None
    return context


@dataclass(frozen=True)
class DecodedFrame:
    """解码后的一帧: BGRA 原始像素, 可直接交给界面包装成图像."""

    width: int
    height: int
    pixels: bytes
    pts: int = 0


class H264Decoder:
    """H.264 解码器: 输入编码包字节, 输出 BGRA 像素."""

    def __init__(self) -> None:
        self._context: Any = None
        self._av: Any = None

    def open(self) -> None:
        """打开解码上下文; 每次会话重建, 保证不继承上一条会话的解码状态."""
        av = _import_av()
        try:
            context = av.CodecContext.create("h264", "r")
        except av.FFmpegError as error:
            raise PortError(DECODER_UNAVAILABLE, f"H.264 解码器不可用: {error}") from error
        self._context = context
        self._av = av

    def decode(self, data: bytes) -> list[DecodedFrame]:
        """解码一个编码包, 返回本次可输出的全部帧."""
        if self._context is None or self._av is None:
            raise PortError(DECODER_UNAVAILABLE, "解码器未打开")
        numpy = _import_numpy()
        try:
            packets = self._context.parse(data)
            frames: list[Any] = []
            for packet in packets:
                frames.extend(self._context.decode(packet))
        except (self._av.FFmpegError, ValueError) as error:
            raise PortError(DECODE_FAILED, f"H.264 解码失败: {error}") from error
        decoded: list[DecodedFrame] = []
        for frame in frames:
            image = frame.reformat(format="bgra")
            array = image.to_ndarray()
            decoded.append(
                DecodedFrame(
                    width=int(image.width),
                    height=int(image.height),
                    pixels=numpy.ascontiguousarray(array).tobytes(),
                    pts=int(frame.pts or 0),
                )
            )
        return decoded

    def close(self) -> None:
        """释放解码上下文."""
        self._context = None
        self._av = None


def _import_numpy() -> Any:
    """延迟导入 numpy; 缺依赖按编码错误上报."""
    try:
        import numpy
    except ImportError as error:
        raise PortError(ENCODE_FAILED, "缺少像素处理依赖 numpy") from error
    return numpy
