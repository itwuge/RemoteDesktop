# ==========================================================================================
# 文件        : encode.py
# 所属目录    : poc/host_encode/
# 文件用途    : PoC H.264 编码模块, 按硬件优先顺序选择编码器并逐帧计时
# 架构定位    : PoC 编码层, 正式版由 native/codec_bridge 的 FFmpeg C ABI 桥替代
# 核心功能    : 编码器探测与选择; BGRA 转 yuv420p; 逐帧编码与耗时统计
# 职责边界    : 只负责编码与计时; 不采集, 不传输, 不做码率自适应
# 运行环境    : Python 3.12 + PyAV 19.0.2; 仅 Windows 被控端
# 依赖关系    : 被 tools/run_p0_03_capture_encode.py 引用
# ==========================================================================================

"""PoC H.264 编码模块."""

from __future__ import annotations

import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import av
import numpy as np

# 探测顺序: 硬件编码优先, Media Foundation 次之, libx264 仅作 PoC 兜底
ENCODER_CANDIDATES: list[tuple[str, dict[str, str], str]] = [
    (
        "h264_nvenc",
        {"preset": "p1", "tune": "ull", "rc": "cbr", "zerolatency": "1", "bf": "0", "g": "60"},
        "hardware",
    ),
    (
        "h264_qsv",
        {"preset": "veryfast", "look_ahead": "0", "async_depth": "1", "bf": "0", "g": "60"},
        "hardware",
    ),
    (
        "h264_amf",
        {"usage": "ultralowlatency", "quality": "speed", "bf": "0", "g": "60"},
        "hardware",
    ),
    ("h264_mf", {"g": "60"}, "media_foundation"),
    (
        "libx264",
        {"preset": "ultrafast", "tune": "zerolatency", "x264-params": "bframes=0:keyint=60:min-keyint=60:scenecut=0"},
        "software",
    ),
]


@dataclass(frozen=True)
class EncodedFrame:
    """一帧编码结果.

    :param packets: 编码产生的包数量
    :type packets: int
    :param size_bytes: 编码产生的字节数
    :type size_bytes: int
    :param encode_ms: 含色彩转换的编码耗时毫秒
    :type encode_ms: float
    """

    packets: int
    size_bytes: int
    encode_ms: float


# ================================ VideoEncoder ================================

class VideoEncoder:
    """H.264 编码器封装, 自动选择可用编码器.

    工作流程:
        1. open() 按候选顺序尝试创建并试编一帧, 第一个成功者即为本次编码器.
        2. encode() 把 BGRA 帧转 yuv420p 后编码, 返回包数与耗时.
        3. flush() 输出剩余包并释放上下文.

    实例属性清单:
        width (int): 帧宽.
        height (int): 帧高.
        fps (int): 帧率.
        bitrate (int): 目标码率, 单位 bps.
        encoder_name (str): 实际选中的编码器名.
        encoder_kind (str): hardware、media_foundation 或 software.
        context (av.CodecContext 或 None): 编码上下文.

    构造参数:
        :param width: 帧宽
        :type width: int
        :param height: 帧高
        :type height: int
        :param fps: 帧率
        :type fps: int
        :param bitrate: 目标码率, 单位 bps
        :type bitrate: int

    运行约束:
        单线程逐帧编码; 不并发复用同一个上下文.
    """

    def __init__(
        self,
        width: int,
        height: int,
        fps: int,
        bitrate: int = 4_000_000,
        force_encoder: str | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.force_encoder = force_encoder
        self.encoder_name = ""
        self.encoder_kind = ""
        self.context: av.CodecContext | None = None
        self.attempts: list[dict[str, str]] = []

    def open(self) -> None:
        """按候选顺序打开可用编码器.

        :return: 无
        :rtype: None
        :raises RuntimeError: 全部候选编码器均失败时抛出
        """
        sample = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        for name, options, kind in ENCODER_CANDIDATES:
            if self.force_encoder is not None and name != self.force_encoder:
                continue
            try:
                context = self._build_context(name, options)
                context.open()
                frame = av.VideoFrame.from_ndarray(sample, format="bgra").reformat(format="yuv420p")
                frame.pts = 0
                context.encode(frame)
                self.context = context
                self.encoder_name = name
                self.encoder_kind = kind
                self.attempts.append({"encoder": name, "result": "ok"})
                return
            except Exception as error:  # 编码器不可用属正常探测失败
                self.attempts.append({"encoder": name, "result": f"failed: {type(error).__name__}: {error}"})
        raise RuntimeError(f"没有可用 H.264 编码器: {self.attempts}")

    def _build_context(self, name: str, options: dict[str, str]) -> av.CodecContext:
        context = av.CodecContext.create(name, "w")
        context.width = self.width
        context.height = self.height
        context.pix_fmt = "yuv420p"
        context.framerate = Fraction(self.fps, 1)
        context.time_base = Fraction(1, self.fps)
        context.bit_rate = self.bitrate
        context.options = dict(options)
        return context

    def encode(self, image: np.ndarray, pts: int) -> tuple[list[av.Packet], EncodedFrame]:
        """编码一帧 BGRA 图像.

        :param image: BGRA 帧数据
        :type image: numpy.ndarray
        :param pts: 帧序号, 同时作为显示时间戳
        :type pts: int
        :return: 原始包列表与本次编码统计
        :rtype: tuple
        """
        if self.context is None:
            raise RuntimeError("编码器尚未打开")
        started = time.perf_counter()
        frame = av.VideoFrame.from_ndarray(image, format="bgra").reformat(format="yuv420p")
        frame.pts = pts
        packets = list(self.context.encode(frame))
        finished = time.perf_counter()
        size_bytes = sum(packet.size for packet in packets if packet.size)
        return packets, EncodedFrame(packets=len(packets), size_bytes=size_bytes, encode_ms=(finished - started) * 1000.0)

    def flush(self) -> list[av.Packet]:
        """输出编码器缓冲中的剩余包.

        :return: 剩余包列表
        :rtype: list
        """
        if self.context is None:
            return []
        return list(self.context.encode(None))

    def summary(self) -> dict[str, Any]:
        """返回编码器信息.

        :return: 编码器名、类别与探测过程
        :rtype: dict
        """
        return {
            "encoder": self.encoder_name,
            "kind": self.encoder_kind,
            "attempts": self.attempts,
        }
