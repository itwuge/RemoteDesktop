# ==========================================================================================
# 文件        : ports.py
# 所属目录    : src/rdc/contracts/
# 文件用途    : 采集、编码与输入注入端口的签名和值对象, 对应 desktop-v1 第五节
# 架构定位    : 端口与适配层, 只依赖标准库, 不反向依赖能力层与命令层
# 核心功能    : CaptureSource / Encoder / InputInjector 协议; Frame、EncodedPacket、InputEvent 值对象
# 职责边界    : 不实现任何平台行为; 不打印; 不落盘; 不生成用户文案
# ==========================================================================================

"""契约端口.

端口签名见 docs/contracts/desktop-v1.md 第五节. 设计书第六节尚未列出这些值对象的字段,
此处只保留端口签名所需的最小字段, 字段缺口已登记到契约变更记录.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class PortError(RuntimeError):
    """端口异常, code 取契约第五节错误码, 由命令边界统一转换."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CaptureParams:
    """采集参数; output_index 对应 X11 显示器序号, 0 为主屏."""

    output_index: int = 0
    even_dimensions: bool = True


@dataclass
class CaptureHandle:
    """采集句柄; value 为平台实现私有状态, width/height 是配对编码器所需的画面尺寸."""

    value: Any = field(default=None, repr=False)
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Frame:
    """一帧原始画面; pixels 为未压缩像素, 默认 BGRA 字节序."""

    width: int
    height: int
    pixels: bytes
    pixel_format: str = "bgra"
    monotonic_s: float = 0.0


@dataclass(frozen=True)
class EncoderParams:
    """编码参数; 尺寸与帧率来自采集句柄与画质决策."""

    width: int
    height: int
    fps: int
    bitrate_bps: int
    codec: str = "h264"


@dataclass
class EncoderHandle:
    """编码句柄; value 为平台实现私有状态."""

    value: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class EncodedPacket:
    """一个编码输出包; keyframe 供控制端判断能否独立解码."""

    data: bytes
    keyframe: bool = False
    pts: int = 0


@dataclass(frozen=True)
class InputEvent:
    """一条输入事件; kind 取 key 或 mouse, action 区分字符、按键、组合键与鼠标动作."""

    kind: str
    action: str = ""
    text: str = ""
    key: str = ""
    mods: tuple[str, ...] = ()
    x: int = 0
    y: int = 0
    button: int = 0


class CaptureSource(Protocol):
    """采集端口."""

    def open(self, params: CaptureParams) -> CaptureHandle: ...

    def read(self, handle: CaptureHandle) -> Frame: ...

    def close(self, handle: CaptureHandle) -> None: ...


class Encoder(Protocol):
    """编码端口."""

    def open(self, params: EncoderParams) -> EncoderHandle: ...

    def encode(self, handle: EncoderHandle, frame: Frame) -> list[EncodedPacket]: ...

    def close(self, handle: EncoderHandle) -> None: ...


class InputInjector(Protocol):
    """输入注入端口."""

    def inject(self, event: InputEvent) -> None: ...
