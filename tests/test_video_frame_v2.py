"""视频帧 v2 封装单测: 往返、截断、长度不符、未知版本、CRC、v1 兼容与版本协商."""

from __future__ import annotations

import base64
import json
import struct

import pytest

from rdc.media.frame import (
    FLAG_CRC32,
    FLAG_KEYFRAME,
    HEADER,
    HEADER_SIZE,
    MAGIC,
    VERSION_V1,
    VERSION_V2,
    FrameError,
    VideoFrame,
    decode_frame,
    decode_v1,
    decode_v2,
    encode_v2,
    negotiate_frame_version,
)

# v1 的固定开销: 4 字节长度前缀 + 12 字节 nonce + 16 字节 tag
WIRE_AEAD_OVERHEAD = 4 + 12 + 16


def make_frame(payload: bytes, *, seq: int = 7, pts: int = 9000, keyframe: bool = True) -> VideoFrame:
    """构造 1080p 帧."""
    return VideoFrame(seq=seq, pts=pts, width=1920, height=1080, keyframe=keyframe, payload=payload)


def make_v1_message(payload: bytes, *, seq: int = 7, pts: int = 9000, keyframe: bool = True) -> dict[str, object]:
    """按 v1 的 JSON+base64 形状构造一条视频消息."""
    return {
        "kind": "video",
        "codec": "h264",
        "w": 1920,
        "h": 1080,
        "fps": 30,
        "key": keyframe,
        "pts": pts,
        "seq": seq,
        "data": base64.b64encode(payload).decode("ascii"),
        "sent_ns": 1_700_000_000_000_000_000,
    }


def v1_wire_bytes(payload: bytes) -> int:
    """v1 帧在 AEAD 通道上的真实字节数."""
    body = json.dumps(make_v1_message(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return WIRE_AEAD_OVERHEAD + len(body)


@pytest.mark.parametrize("size", [0, 1, 150, 1500, 6000, 12000, 90000])
@pytest.mark.parametrize("keyframe", [True, False])
def test_v2_roundtrip_preserves_fields(size: int, keyframe: bool) -> None:
    """任意大小的净荷与关键帧标志都能原样往返."""
    payload = bytes((index * 31 + 7) & 0xFF for index in range(size))
    frame = make_frame(payload, keyframe=keyframe)
    decoded = decode_v2(encode_v2(frame))
    assert decoded == frame
    assert decoded.payload == payload
    assert decoded.keyframe is keyframe


def test_v2_header_is_fixed_and_documented() -> None:
    """v2 头固定 28 字节, 字段偏移与文档一致."""
    assert HEADER_SIZE == 28
    assert HEADER.format == ">2sBBIqHHII"
    encoded = encode_v2(make_frame(b"x" * 100))
    assert len(encoded) == HEADER_SIZE + 100
    magic, version, flags, seq, pts, width, height, length, checksum = HEADER.unpack_from(encoded)
    assert magic == MAGIC and version == VERSION_V2
    assert flags == FLAG_KEYFRAME
    assert (seq, pts, width, height, length) == (7, 9000, 1920, 1080, 100)
    assert checksum == 0


def test_v2_overhead_beats_v1_and_stays_under_five_percent() -> None:
    """典型 1080p 帧的 v2 线路膨胀 ≤ 1.05 倍, 且明显低于 v1."""
    for size in (1200, 3000, 6000, 12000, 90000):
        payload = b"\x00\x00\x00\x01" * (size // 4) + b"\x00" * (size % 4)
        v2_wire = WIRE_AEAD_OVERHEAD + len(encode_v2(make_frame(payload)))
        ratio = v2_wire / size
        assert ratio <= 1.05, f"{size} 字节时 v2 膨胀 {ratio:.4f}"
        assert v2_wire < v1_wire_bytes(payload)


def test_v2_small_frame_still_beats_v1_even_if_ratio_is_high() -> None:
    """极小帧固定头占比高, 膨胀比不可能压到 1.05, 但仍优于 v1."""
    payload = b"\x00" * 150
    v2_wire = WIRE_AEAD_OVERHEAD + len(encode_v2(make_frame(payload)))
    assert v2_wire < v1_wire_bytes(payload)
    assert v2_wire / len(payload) > 1.05


def test_crc_flag_roundtrip_and_mismatch() -> None:
    """置 CRC 位时校验通过; 净荷被改则报 CRC 不符."""
    frame = make_frame(b"h264-payload" * 10)
    encoded = encode_v2(frame, with_crc=True)
    assert struct.unpack_from(">B", encoded, 3)[0] & FLAG_CRC32
    assert decode_v2(encoded) == frame

    corrupted = bytearray(encoded)
    corrupted[HEADER_SIZE] ^= 0xFF
    with pytest.raises(FrameError) as error:
        decode_v2(bytes(corrupted))
    assert error.value.code == "FRAME_CRC_MISMATCH"


def test_truncated_header_and_payload() -> None:
    """帧头不足与净荷被切都报截断."""
    encoded = encode_v2(make_frame(b"a" * 200))
    for cut in (0, 1, HEADER_SIZE - 1, HEADER_SIZE, HEADER_SIZE + 199):
        with pytest.raises(FrameError) as error:
            decode_v2(encoded[:cut])
        assert error.value.code == "FRAME_TRUNCATED", cut


def test_length_field_mismatch() -> None:
    """声明长度小于实际净荷时报长度不符."""
    payload = b"b" * 300
    encoded = bytearray(encode_v2(make_frame(payload)))
    HEADER.pack_into(encoded, 0, MAGIC, VERSION_V2, FLAG_KEYFRAME, 1, 0, 1920, 1080, 299, 0)
    with pytest.raises(FrameError) as error:
        decode_v2(bytes(encoded))
    assert error.value.code == "FRAME_LENGTH_MISMATCH"


def test_declared_length_over_limit() -> None:
    """声明长度超过净荷上限时直接拒绝, 不尝试分配."""
    header = HEADER.pack(MAGIC, VERSION_V2, 0, 0, 0, 1920, 1080, 8 * 1024 * 1024 + 1, 0)
    with pytest.raises(FrameError) as error:
        decode_v2(header)
    assert error.value.code == "FRAME_TOO_LARGE"


def test_unknown_version_is_rejected() -> None:
    """魔数正确但版本不是 2 时拒绝, 便于后续 v3 显式分流."""
    header = HEADER.pack(MAGIC, 3, 0, 0, 0, 1920, 1080, 0, 0)
    with pytest.raises(FrameError) as error:
        decode_v2(header)
    assert error.value.code == "FRAME_UNSUPPORTED_VERSION"
    assert "3" in str(error.value)


def test_bad_magic_is_rejected() -> None:
    """魔数不符时拒绝."""
    header = HEADER.pack(b"XX", VERSION_V2, 0, 0, 0, 1920, 1080, 0, 0)
    with pytest.raises(FrameError) as error:
        decode_v2(header)
    assert error.value.code == "FRAME_BAD_MAGIC"


def test_encode_rejects_out_of_range_fields() -> None:
    """编码端对超出字段宽度的值显式报错, 不做静默截断."""
    base = make_frame(b"x")
    for frame in (
        VideoFrame(seq=-1, pts=0, width=1, height=1, keyframe=False, payload=b"x"),
        VideoFrame(seq=2**32, pts=0, width=1, height=1, keyframe=False, payload=b"x"),
        VideoFrame(seq=0, pts=2**63, width=1, height=1, keyframe=False, payload=b"x"),
        VideoFrame(seq=0, pts=0, width=70000, height=1, keyframe=False, payload=b"x"),
        VideoFrame(seq=0, pts=0, width=1, height=70000, keyframe=False, payload=b"x"),
    ):
        with pytest.raises(FrameError) as error:
            encode_v2(frame)
        assert error.value.code == "FRAME_FIELD_RANGE"
    assert decode_v2(encode_v2(base)) == base


def test_v1_decode_keeps_compatibility() -> None:
    """v1 JSON+base64 仍可解码, 字段映射到 VideoFrame."""
    payload = bytes(range(256))
    frame = decode_v1(make_v1_message(payload))
    assert frame.payload == payload
    assert (frame.seq, frame.pts, frame.width, frame.height) == (7, 9000, 1920, 1080)
    assert frame.keyframe is True


def test_v1_decode_rejects_wrong_kind_and_broken_base64() -> None:
    """v1 解码对类型与 base64 都做显式校验."""
    with pytest.raises(FrameError) as kind_error:
        decode_v1({"kind": "audio", "data": ""})
    assert kind_error.value.code == "FRAME_KIND_MISMATCH"

    with pytest.raises(FrameError) as base64_error:
        decode_v1({"kind": "video", "data": "!!!!"})
    assert base64_error.value.code == "FRAME_BAD_PAYLOAD"

    with pytest.raises(FrameError) as field_error:
        decode_v1({"kind": "video", "data": "", "seq": "0"})
    assert field_error.value.code == "FRAME_BAD_PAYLOAD"


def test_decode_frame_auto_detects_both_versions() -> None:
    """同一入口既能解 v2 二进制帧, 也能解 v1 JSON 帧."""
    payload = b"segment" * 100
    v2 = decode_frame(encode_v2(make_frame(payload)))
    v1 = decode_frame(json.dumps(make_v1_message(payload)).encode("utf-8"))
    assert v2.payload == payload and v1.payload == payload
    assert v2.keyframe is True and v1.keyframe is True

    with pytest.raises(FrameError) as error:
        decode_frame(b"not-a-frame")
    assert error.value.code == "FRAME_BAD_MAGIC"


def test_negotiation_prefers_v2_and_degrades_to_v1() -> None:
    """双方都支持时选 v2; 对端缺失、只支持 v1 或声明未知版本时降级 v1."""
    assert negotiate_frame_version({"frame_versions": [1, 2]}) == VERSION_V2
    assert negotiate_frame_version({"frame_versions": [2]}) == VERSION_V2
    assert negotiate_frame_version({"frame_version": 2}) == VERSION_V2
    assert negotiate_frame_version({"frame_versions": [1]}) == VERSION_V1
    assert negotiate_frame_version({"frame_versions": [3]}) == VERSION_V1
    assert negotiate_frame_version({"frame_versions": ["2", "x"]}) == VERSION_V1
    assert negotiate_frame_version({"frame_versions": [True]}) == VERSION_V1
    assert negotiate_frame_version({}) == VERSION_V1
    assert negotiate_frame_version(None) == VERSION_V1


def test_negotiation_respects_local_downgrade() -> None:
    """本端强制 v1 时即使对端支持 v2 也不升版本."""
    assert negotiate_frame_version({"frame_versions": [1, 2]}, local_versions=(VERSION_V1,)) == VERSION_V1


def test_mixed_versions_still_decode_after_negotiation() -> None:
    """滚动升级期: 协商降级到 v1 后仍能解 v2 帧; 协商 v2 后也能解旧端发来的 v1 帧."""
    payload = b"rolling-upgrade"
    negotiated_v1 = negotiate_frame_version({"frame_versions": [1]})
    assert negotiated_v1 == VERSION_V1
    assert decode_frame(encode_v2(make_frame(payload))).payload == payload

    negotiated_v2 = negotiate_frame_version({"frame_versions": [1, 2]})
    assert negotiated_v2 == VERSION_V2
    assert decode_frame(json.dumps(make_v1_message(payload)).encode("utf-8")).payload == payload
