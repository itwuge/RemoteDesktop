# ==========================================================================================
# 文件        : frame.py
# 所属目录    : src/rdc/media/
# 文件用途    : 视频帧线格式编解码: v2 定长二进制头 + 原始 H.264 净荷, 并保留 v1 JSON+base64 解码兼容
# 架构定位    : 封装层, 只做帧的序列化与反序列化, 不编码、不发送、不做画质决策
# 核心功能    : encode_v2 / decode_v2 定长头读写; decode_v1 旧格式兼容; decode_frame 自动识别;
#               negotiate_frame_version 版本协商与降级; 显式错误码覆盖截断、长度不符、未知版本与 CRC 不符
# 职责边界    : 不修改 docs/contracts 正文; 不读写文件; 不依赖网络、界面与编解码器实现
# 运行环境    : Python 3.12
# 依赖关系    : 被会话数据面与验收脚本调用; 只依赖标准库
# ==========================================================================================

"""视频帧线格式.

v2 定长头 (28 字节, 大端, ``>2sBBIqHHII``):

======  ======  ==================================================================
偏移    长度    字段
======  ======  ==================================================================
0       2       magic, 固定 ``b"RF"``
2       1       version, 固定 2; 非 2 一律按未知版本拒绝
3       1       flags, 位 0 为 keyframe, 位 1 为 CRC32 有效, 其余保留
4       4       seq, 无符号 32 位
8       8       pts, 有符号 64 位
16      2       width, 无符号 16 位
18      2       height, 无符号 16 位
20      4       payload_len, 无符号 32 位
24      4       crc32, payload 的 CRC32; flags 位 1 未置位时为 0
======  ======  ==================================================================

净荷是编码器输出的原始字节, 不做 base64、不塞进 JSON. v1 的 ``{"kind":"video","data":<base64>}``
继续由 :func:`decode_v1` 解码, 用于旧客户端与滚动升级期间的混用.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard

MAGIC = b"RF"
VERSION_V1 = 1
VERSION_V2 = 2
HEADER = struct.Struct(">2sBBIqHHII")
HEADER_SIZE = HEADER.size
FLAG_KEYFRAME = 0x01
FLAG_CRC32 = 0x02
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
UINT32_MAX = 0xFFFFFFFF
UINT16_MAX = 0xFFFF
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

# 错误码: 本模块局部使用, 尚未进入契约错误码表; 缺口已登记到契约变更记录
FRAME_TRUNCATED = "FRAME_TRUNCATED"
FRAME_LENGTH_MISMATCH = "FRAME_LENGTH_MISMATCH"
FRAME_BAD_MAGIC = "FRAME_BAD_MAGIC"
FRAME_UNSUPPORTED_VERSION = "FRAME_UNSUPPORTED_VERSION"
FRAME_CRC_MISMATCH = "FRAME_CRC_MISMATCH"
FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
FRAME_FIELD_RANGE = "FRAME_FIELD_RANGE"
FRAME_BAD_PAYLOAD = "FRAME_BAD_PAYLOAD"
FRAME_KIND_MISMATCH = "FRAME_KIND_MISMATCH"


class FrameError(ValueError):
    """帧编码或解码失败, code 为上面的局部错误码."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VideoFrame:
    """一帧视频的元数据与原始净荷; payload 不经过 base64."""

    seq: int
    pts: int
    width: int
    height: int
    keyframe: bool
    payload: bytes

    @property
    def payload_len(self) -> int:
        """净荷字节数."""
        return len(self.payload)


def encode_v2(frame: VideoFrame, *, with_crc: bool = False) -> bytes:
    """编码为 v2 二进制帧.

    :param frame: 帧元数据与原始净荷
    :param with_crc: 是否写入 CRC32 并置 flags 位 1
    :return: 28 字节定长头 + 原始净荷
    :raises FrameError: 字段越界或净荷超限
    """
    payload = frame.payload
    if not 0 <= frame.seq <= UINT32_MAX:
        raise FrameError(FRAME_FIELD_RANGE, f"seq 越界: {frame.seq}")
    if not INT64_MIN <= frame.pts <= INT64_MAX:
        raise FrameError(FRAME_FIELD_RANGE, f"pts 越界: {frame.pts}")
    if not 0 <= frame.width <= UINT16_MAX or not 0 <= frame.height <= UINT16_MAX:
        raise FrameError(FRAME_FIELD_RANGE, f"尺寸越界: {frame.width}x{frame.height}")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise FrameError(FRAME_TOO_LARGE, f"净荷 {len(payload)} 字节超过上限 {MAX_PAYLOAD_BYTES}")

    flags = FLAG_KEYFRAME if frame.keyframe else 0
    checksum = 0
    if with_crc:
        flags |= FLAG_CRC32
        checksum = zlib.crc32(payload) & UINT32_MAX
    header = HEADER.pack(
        MAGIC,
        VERSION_V2,
        flags,
        frame.seq,
        frame.pts,
        frame.width,
        frame.height,
        len(payload),
        checksum,
    )
    return header + payload


def decode_v2(packet: bytes) -> VideoFrame:
    """解码 v2 二进制帧.

    :param packet: 完整的 v2 帧字节
    :return: 帧元数据与原始净荷
    :raises FrameError: 魔数不符、版本未知、截断、长度不符、超出上限或 CRC 不符
    """
    if len(packet) < HEADER_SIZE:
        raise FrameError(FRAME_TRUNCATED, f"帧头只有 {len(packet)} 字节, 至少需要 {HEADER_SIZE}")
    magic, version, flags, seq, pts, width, height, payload_len, checksum = HEADER.unpack_from(packet)
    if magic != MAGIC:
        raise FrameError(FRAME_BAD_MAGIC, f"魔数不符: {magic!r}")
    if version != VERSION_V2:
        raise FrameError(FRAME_UNSUPPORTED_VERSION, f"未知帧版本: {version}")
    if payload_len > MAX_PAYLOAD_BYTES:
        raise FrameError(FRAME_TOO_LARGE, f"声明净荷 {payload_len} 字节超过上限 {MAX_PAYLOAD_BYTES}")
    body = packet[HEADER_SIZE:]
    if len(body) < payload_len:
        raise FrameError(FRAME_TRUNCATED, f"净荷被截断: 声明 {payload_len}, 实际 {len(body)}")
    if len(body) > payload_len:
        raise FrameError(FRAME_LENGTH_MISMATCH, f"长度字段与实际不符: 声明 {payload_len}, 实际 {len(body)}")
    if flags & FLAG_CRC32 and (zlib.crc32(body) & UINT32_MAX) != checksum:
        raise FrameError(FRAME_CRC_MISMATCH, "净荷 CRC32 与帧头不一致")
    return VideoFrame(
        seq=seq,
        pts=pts,
        width=width,
        height=height,
        keyframe=bool(flags & FLAG_KEYFRAME),
        payload=body,
    )


def decode_v1(message: Mapping[str, Any]) -> VideoFrame:
    """解码 v1 ``{"kind":"video","data":<base64>}`` 消息.

    只做解码兼容, 不再用 v1 编码新帧.

    :param message: 已解析的 v1 JSON 对象
    :return: 帧元数据与解码后的原始净荷
    :raises FrameError: 类型不是 video、字段类型非法或 base64 损坏
    """
    if str(message.get("kind", "")) != "video":
        raise FrameError(FRAME_KIND_MISMATCH, f"不是视频帧消息: {message.get('kind')!r}")
    data = message.get("data")
    if not isinstance(data, str):
        raise FrameError(FRAME_BAD_PAYLOAD, "v1 视频帧缺少字符串 data")
    try:
        payload = base64.b64decode(data.encode("ascii"), validate=True)
    except ValueError as error:
        raise FrameError(FRAME_BAD_PAYLOAD, f"v1 视频帧 base64 损坏: {error}") from error
    return VideoFrame(
        seq=_int_field(message, "seq"),
        pts=_int_field(message, "pts"),
        width=_int_field(message, "w"),
        height=_int_field(message, "h"),
        keyframe=bool(message.get("key", False)),
        payload=payload,
    )


def decode_frame(packet: bytes) -> VideoFrame:
    """自动识别并解码 v2 二进制帧或 v1 JSON 帧.

    :param packet: 线上帧明文
    :return: 帧元数据与原始净荷
    :raises FrameError: 既不是 v2 也不是合法 v1 JSON 时
    """
    if packet[:2] == MAGIC:
        return decode_v2(packet)
    try:
        message = json.loads(packet.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrameError(FRAME_BAD_MAGIC, "既不是 v2 帧也不是 v1 JSON") from error
    if not isinstance(message, dict):
        raise FrameError(FRAME_BAD_PAYLOAD, "v1 视频帧必须是 JSON 对象")
    return decode_v1(message)


def negotiate_frame_version(
    peer_caps: Mapping[str, Any] | None,
    *,
    local_versions: Sequence[int] = (VERSION_V2, VERSION_V1),
) -> int:
    """按对端媒体能力选择帧版本; 对端未声明或只支持 v1 时降级 v1.

    ``peer_caps`` 读取 ``frame_versions`` 列表, 兼容单个 ``frame_version`` 整数.
    v1 是永远可用的兜底版本, 因此无论对端声明什么, 本端都不会协商出 v1 以外的未知版本.

    :param peer_caps: offer/answer 里的 media_caps
    :param local_versions: 本端支持的版本, 按优先级从高到低
    :return: 本端应使用的帧版本
    """
    if not peer_caps:
        return VERSION_V1
    declared: Any = peer_caps.get("frame_versions")
    if declared is None:
        declared = peer_caps.get("video_frame_versions")
    versions: set[int] = set()
    if isinstance(declared, (list, tuple, set, frozenset)):
        versions = {item for item in declared if _is_int(item)}
    else:
        single = peer_caps.get("frame_version")
        if _is_int(single):
            versions = {int(single)}
    if not versions:
        return VERSION_V1
    for version in local_versions:
        if version in versions:
            return version
    return VERSION_V1


def _int_field(message: Mapping[str, Any], name: str) -> int:
    value = message.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FrameError(FRAME_BAD_PAYLOAD, f"v1 视频帧字段 {name} 不是整数: {value!r}")
    return value


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)
