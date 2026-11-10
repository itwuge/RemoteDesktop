# ==========================================================================================
# 文件        : test_connect_udp_plane.py
# 所属目录    : tests/
# 文件用途    : UDP 打洞数据面的单测: 分片、乱序、丢片、重传、心跳、越界、上限与重放
# 架构定位    : 只验证纯状态机与编解码; 不建真实 socket, 不访问网络, 不依赖本机网卡状态
# 核心功能    : 90000 B -> 77 片; 乱序重组 sha256 一致; 丢片 100 ms 清零; 120/240/480 重传
# 职责边界    : 不替代真机打洞验收; 真实 NAT 路径仍由 S6 验收脚本取证
# 运行环境    : Python 3.12
# 依赖关系    : 被测对象为 src/rdc/connect/udp_plane.py; 复用 src/rdc/session/crypto.py
# ==========================================================================================

"""UDP 打洞数据面单测.

日志一律写进临时目录, 不污染仓库 logs/; 需要计时的用例全部注入单调时钟.
"""

from __future__ import annotations

import errno
import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from rdc.connect.udp_plane import (
    FLAG_LAST_FRAGMENT,
    FLAG_RELIABLE,
    MAX_FRAG_COUNT,
    MAX_FRAME_BYTES,
    MTU_DOWNGRADE_DATAGRAM_BYTES,
    PATH_DEAD_S,
    REASON_PATH_DEAD,
    REASON_REPLAY_STORM,
    RETRANSMIT_LIMIT,
    TYPE_DATA,
    TYPE_PING,
    TYPE_PONG,
    UDP_DATAGRAM_BYTES,
    UDP_FRAGMENT_BYTES,
    UDP_HEADER_BYTES,
    ReassemblyTable,
    UdpFrameError,
    UdpHeader,
    UdpSessionPlane,
    encode_ack,
    encode_ping,
    fragment_frame,
)
from rdc.core.logging import reset_for_tests
from rdc.session.crypto import AeadSession

FROZEN_CLOCK = 0.0


@pytest.fixture(autouse=True)
def _isolated_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """日志目录按用例隔离到临时目录."""
    monkeypatch.setenv("RDC_LOG_DIR", str(tmp_path / "logs"))
    reset_for_tests()
    yield
    reset_for_tests()


def _frozen() -> float:
    return FROZEN_CLOCK


def _sample_frame(size: int = 90000) -> bytes:
    """确定性 90000 B 帧, 保证多次运行 sha256 一致."""
    return bytes((index * 37 + 11) % 256 for index in range(size))


def test_header_layout_and_limits_match_spec() -> None:
    """16 字节头与 1200/1184/6 MiB 常量必须与规格逐项相等."""
    header = UdpHeader(
        ptype=TYPE_DATA,
        flags=FLAG_LAST_FRAGMENT | FLAG_RELIABLE,
        frag_index=3,
        frag_count=77,
        frag_len=16,
        frame_id=9,
        frame_len=90000,
    )
    encoded = header.encode()
    assert len(encoded) == UDP_HEADER_BYTES == 16
    assert UdpHeader.decode(encoded + b"\x00" * header.frag_len) == header
    assert (encoded[0] >> 4, encoded[0] & 0x0F) == (1, TYPE_DATA)
    assert UDP_DATAGRAM_BYTES == 1200
    assert UDP_FRAGMENT_BYTES == 1184
    assert MAX_FRAME_BYTES == 6291456
    assert MAX_FRAG_COUNT == 5314
    assert RETRANSMIT_LIMIT == 3


def test_keyframe_splits_into_77_datagrams() -> None:
    """90000 B 关键帧必须切成 77 片, 每片不超过 1200 字节."""
    frame = _sample_frame()
    datagrams = fragment_frame(7, frame)
    assert len(datagrams) == 77
    headers = [UdpHeader.decode(item) for item in datagrams]
    assert {item.frame_id for item in headers} == {7}
    assert {item.frag_count for item in headers} == {77}
    assert [item.frag_index for item in headers] == list(range(77))
    assert all(len(item) <= UDP_DATAGRAM_BYTES for item in datagrams)
    assert sum(item.frag_len for item in headers) == len(frame)
    assert headers[-1].last_fragment is True
    assert not any(item.last_fragment for item in headers[:-1])


def test_out_of_order_reassembly_matches_sha256() -> None:
    """分片反转投递后重组, sha256 必须与原始帧一致且重组表清空."""
    frame = _sample_frame()
    table = ReassemblyTable()
    assert len(fragment_frame(11, frame)) == 77
    assembled: bytes | None = None
    for order, datagram in enumerate(reversed(fragment_frame(11, frame))):
        header = UdpHeader.decode(datagram)
        result = table.push(header, datagram[UDP_HEADER_BYTES:], now=order * 0.001)
        if result is not None:
            assembled = result
    assert assembled is not None
    assert hashlib.sha256(assembled).digest() == hashlib.sha256(frame).digest()
    assert table.size == 0
    assert table.dropped == 0


def test_missing_fragment_clears_frame_after_100ms() -> None:
    """丢 1 片超过 100 ms 后整帧丢弃并清零, 后续帧照常重组."""
    frame = _sample_frame()
    table = ReassemblyTable()
    datagrams = fragment_frame(12, frame)
    for datagram in datagrams[:-1]:
        header = UdpHeader.decode(datagram)
        assert table.push(header, datagram[UDP_HEADER_BYTES:], now=0.0) is None
    assert table.size == 1
    assert table.expire(0.099) == 0
    assert table.size == 1
    assert table.expire(0.101) == 1
    assert table.size == 0
    assert table.dropped == 1

    follow_up = b"next-frame-after-drop"
    next_datagrams = fragment_frame(13, follow_up)
    result = None
    for datagram in next_datagrams:
        header = UdpHeader.decode(datagram)
        result = table.push(header, datagram[UDP_HEADER_BYTES:], now=0.102)
    assert result == follow_up
    assert table.size == 0


def test_reliable_frame_retransmits_120_240_480_then_path_dead() -> None:
    """可靠帧未 ACK 时按 120/240/480 ms 重传 3 次, 随后判通路失效."""
    plane = UdpSessionPlane(clock=_frozen)
    plane.send_frame(b"control-frame", reliable=True, control=True)
    assert plane.tick(0.119) == ()
    assert len(plane.tick(0.120)) == 1
    assert plane.tick(0.359) == ()
    assert len(plane.tick(0.360)) == 1
    assert plane.tick(0.839) == ()
    assert len(plane.tick(0.840)) == 1
    assert plane.retransmits == RETRANSMIT_LIMIT
    assert plane.path_dead is True


def test_ack_stops_retransmission() -> None:
    """收到 ACK 后不再重传, 也不判失效."""
    plane = UdpSessionPlane(clock=_frozen)
    frame_id = plane.send_frame(b"control-frame", reliable=True, control=True)
    inbound = plane.receive(encode_ack(frame_id))
    assert inbound is not None
    assert inbound.kind == "ack" and inbound.frame_id == frame_id
    plane.tick(0.5)
    plane.tick(2.0)
    assert plane.retransmits == 0
    assert plane.path_dead is False


def test_video_frame_is_not_retransmitted() -> None:
    """视频帧不可靠: 不回 ACK 也不重传."""
    plane = UdpSessionPlane(clock=_frozen)
    plane.send_frame(_sample_frame(4096), reliable=False)
    assert plane.tick(0.5) == ()
    assert plane.tick(2.0) != ()
    assert plane.retransmits == 0


def test_heartbeat_is_one_second_and_pong_echoes() -> None:
    """每 1 s 发一次 PING; 收到 PING 回 PONG 且回显 frame_id 与时间戳."""
    plane = UdpSessionPlane(clock=_frozen)
    assert plane.tick(0.999) == ()
    first = plane.tick(1.0)
    assert len(first) == 1
    ping_header = UdpHeader.decode(first[0])
    assert ping_header.ptype == TYPE_PING
    assert plane.tick(1.999) == ()
    second = plane.tick(2.0)
    assert [UdpHeader.decode(item).ptype for item in second] == [TYPE_PING]

    responder = UdpSessionPlane(clock=_frozen)
    inbound = responder.receive(encode_ping(5, 123456), now=0.1)
    assert inbound is not None
    assert inbound.kind == "ping" and inbound.frame_id == 5
    queued = responder.tick(0.1)
    assert len(queued) == 1
    pong_header = UdpHeader.decode(queued[0])
    assert pong_header.ptype == TYPE_PONG
    assert pong_header.frame_id == 5
    assert queued[0][UDP_HEADER_BYTES:] == (123456).to_bytes(8, "big")


def test_three_seconds_without_valid_datagram_marks_path_dead() -> None:
    """3 s 内没有任何合法数据报判 path_dead; 有合法包则重新计时."""
    silent = UdpSessionPlane(clock=_frozen)
    silent.tick(PATH_DEAD_S - 0.01)
    assert silent.path_dead is False
    silent.tick(PATH_DEAD_S)
    assert silent.path_dead is True
    assert silent.dead_reason == REASON_PATH_DEAD

    alive = UdpSessionPlane(clock=_frozen)
    alive.receive(encode_ack(1), now=2.5)
    alive.tick(3.0)
    assert alive.path_dead is False
    alive.tick(5.5)
    assert alive.path_dead is True


def test_invalid_datagram_does_not_refresh_liveness() -> None:
    """非法数据报只计数, 不刷新 3 s 失效计时."""
    plane = UdpSessionPlane(clock=_frozen)
    assert plane.receive(b"\x00\x01\x02", now=2.9) is None
    assert plane.invalid_datagrams == 1
    plane.tick(PATH_DEAD_S)
    assert plane.path_dead is True


def test_rejects_oversized_fragment_count_and_frame_len() -> None:
    """frag_count 超过 5314 或 frame_len 超过 6 MiB 时丢弃且不分配重组槽."""
    with pytest.raises(UdpFrameError):
        fragment_frame(1, b"x" * (MAX_FRAME_BYTES + 1))
    forged = UdpHeader(TYPE_DATA, FLAG_LAST_FRAGMENT, 0, MAX_FRAG_COUNT + 1, 4, 1, 4).encode() + b"abcd"
    with pytest.raises(UdpFrameError):
        UdpHeader.decode(forged)
    oversized = UdpHeader(TYPE_DATA, FLAG_LAST_FRAGMENT, 0, 1, 4, 2, MAX_FRAME_BYTES + 1).encode() + b"abcd"
    with pytest.raises(UdpFrameError):
        UdpHeader.decode(oversized)

    plane = UdpSessionPlane(clock=_frozen)
    assert plane.receive(forged) is None
    assert plane.receive(oversized) is None
    assert plane.invalid_datagrams == 2
    assert plane.reasm_pending == 0
    assert plane.reasm_drop == 0


def test_mtu_downgrade_on_emsgsize() -> None:
    """发送 1200 字节数据报遇 EMSGSIZE 时降到 1100 字节重发一次."""
    frame = _sample_frame(90000)
    sock = _MtuSocket()
    plane = UdpSessionPlane(sock, peer=("127.0.0.1", 40000), clock=_frozen)
    plane.send_frame(frame)
    assert plane.datagram_bytes == MTU_DOWNGRADE_DATAGRAM_BYTES
    assert sock.connected == [("127.0.0.1", 40000)]
    assert len(sock.datagrams) == 84
    assert all(len(item) <= MTU_DOWNGRADE_DATAGRAM_BYTES for item in sock.datagrams)
    assert sum(len(item) - UDP_HEADER_BYTES for item in sock.datagrams) == len(frame)


def test_replay_overflow_drops_only_that_frame() -> None:
    """越界计数器与重放只丢该帧并计数, 会话仍可继续解密后续帧."""
    send_key = bytes(range(32))
    recv_key = bytes(range(32, 64))
    sender = AeadSession(send_key=send_key, recv_key=recv_key)
    receiver = AeadSession(send_key=recv_key, recv_key=send_key)
    frames = [sender.encrypt(f"payload-{index}".encode()) for index in range(72)]
    plane = UdpSessionPlane(clock=_frozen)

    assert plane.decrypt_payload(frames[0], receiver.decrypt) == b"payload-0"
    for index in range(1, 70):
        assert plane.decrypt_payload(frames[index], receiver.decrypt) == f"payload-{index}".encode()
    assert plane.decrypt_payload(frames[0], receiver.decrypt) is None
    assert plane.replay_drop == 1
    assert plane.path_dead is False
    assert plane.decrypt_payload(frames[70], receiver.decrypt) == b"payload-70"
    assert plane.decrypt_payload(frames[69], receiver.decrypt) is None
    assert plane.replay_drop == 2
    assert plane.path_dead is False
    assert plane.decrypt_payload(frames[71], receiver.decrypt) == b"payload-71"


def test_hundred_consecutive_decrypt_failures_mark_path_dead() -> None:
    """连续 100 帧解密失败才判通路失效, 前 99 次只丢帧."""

    def always_reject(_payload: bytes) -> bytes:
        raise ValueError("计数器落后接收窗口, 已断开")

    plane = UdpSessionPlane(clock=_frozen)
    for _ in range(99):
        assert plane.decrypt_payload(b"cipher", always_reject) is None
    assert plane.replay_drop == 99
    assert plane.path_dead is False
    assert plane.decrypt_payload(b"cipher", always_reject) is None
    assert plane.replay_drop == 100
    assert plane.path_dead is True
    assert plane.dead_reason == REASON_REPLAY_STORM


class _MtuSocket:
    """首次发送超过 1100 字节的数据报抛 EMSGSIZE 的最小 socket 替身."""

    def __init__(self) -> None:
        self.datagrams: list[bytes] = []
        self.connected: list[tuple[str, int]] = []

    def connect(self, address: tuple[str, int]) -> None:
        self.connected.append(address)

    def setsockopt(self, _level: int, _option: int, _value: int) -> None:
        return None

    def send(self, datagram: bytes) -> int:
        if len(datagram) > MTU_DOWNGRADE_DATAGRAM_BYTES:
            raise OSError(errno.EMSGSIZE, "message too long")
        self.datagrams.append(datagram)
        return len(datagram)

    def close(self) -> None:
        return None
