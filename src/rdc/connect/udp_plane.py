# ==========================================================================================
# 文件        : udp_plane.py
# 所属目录    : src/rdc/connect/
# 文件用途    : IPv4 打洞的 UDP 数据面: 16 字节头、1200 字节数据报、1184 字节分片、重组与重传
# 架构定位    : 打洞通路的数据面适配层, 原样承载现有 AEAD 帧; 不参与候选收集与选路判定
# 核心功能    : 帧分片/编解码; 8 路乱序重组; 控制帧 ACK 重传 120/240/480 ms; 心跳 1 s; 3 s 失效
# 职责边界    : 不碰 policy/session; 不实现 STUN 与候选交换; 不做密钥协商, 只复用外部 AEAD 会话
# 运行环境    : Python 3.12
# 依赖关系    : 复用 src/rdc/session/crypto.py 的 64 帧重放窗口; 由打洞接线层持有
# ==========================================================================================

"""UDP 打洞数据面.

规格: docs/specs/ipv6-punch/spec.md 第 5 节.

- 每个数据报固定 1200 字节, 头部固定 16 字节, 负载上限 1184 字节; 单帧上限 6 MiB.
- 载荷是现有 AEAD 帧去掉 4 字节长度前缀后的字节; TCP 的长度前缀在 UDP 由 ``frame_len`` 表达.
- 视频帧不可靠, 缺片即整帧丢弃; 控制帧按 120/240/480 ms 重传, 3 次未 ACK 判通路失效.
- 心跳 1 s 一次, 3 s 内没有任何合法数据报判 ``path_dead``.
- 重放与计数器越界只丢该帧并计数, 不关闭会话; 连续 100 帧解密失败才判通路失效.
"""

from __future__ import annotations

import errno
import socket
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag

from rdc.core.logging import log_event

# -------------------------------- 协议常量 --------------------------------

PROTOCOL_VERSION = 1
TYPE_DATA = 1
TYPE_PING = 2
TYPE_PONG = 3
TYPE_ACK = 4
VALID_TYPES = frozenset({TYPE_DATA, TYPE_PING, TYPE_PONG, TYPE_ACK})

FLAG_LAST_FRAGMENT = 0x01
FLAG_RELIABLE = 0x02
FLAG_CONTROL = 0x04

UDP_DATAGRAM_BYTES = 1200
UDP_HEADER_BYTES = 16
UDP_FRAGMENT_BYTES = UDP_DATAGRAM_BYTES - UDP_HEADER_BYTES
MAX_FRAME_BYTES = 6 * 1024 * 1024
MAX_FRAG_COUNT = (MAX_FRAME_BYTES + UDP_FRAGMENT_BYTES - 1) // UDP_FRAGMENT_BYTES

MTU_DOWNGRADE_DATAGRAM_BYTES = 1100
RETRANSMIT_DELAYS_MS = (120, 240, 480)
RETRANSMIT_CUMULATIVE_MS = (120, 360, 840)
RETRANSMIT_LIMIT = len(RETRANSMIT_DELAYS_MS)

HEARTBEAT_INTERVAL_S = 1.0
PATH_DEAD_S = 3.0
REASSEMBLY_TIMEOUT_S = 0.1
REASSEMBLY_SLOTS = 8
REASSEMBLY_CAPACITY_BYTES = 64 * 1024 * 1024

REPLAY_FAILURE_LIMIT = 100
SOCKET_BUFFER_BYTES = 4 * 1024 * 1024
PING_PAYLOAD_BYTES = 8

MODULE = "connect.udp_plane"
REASON_PATH_DEAD = "path_dead"
REASON_RETRANSMIT_EXHAUSTED = "retransmit_exhausted"
REASON_REPLAY_STORM = "replay_storm"

_HEADER = struct.Struct(">BBHHHII")
_TIME_EPS = 1e-6
_EMSGSIZE = frozenset({errno.EMSGSIZE, getattr(errno, "WSAEMSGSIZE", errno.EMSGSIZE)})


class UdpFrameError(ValueError):
    """数据报不合规: 版本、类型、分片字段或长度越界."""


# -------------------------------- 头部编解码 --------------------------------


@dataclass(frozen=True)
class UdpHeader:
    """16 字节固定头; 同一帧的所有分片只有 ``frag_index``/``frag_len``/末片位不同."""

    ptype: int
    flags: int
    frag_index: int
    frag_count: int
    frag_len: int
    frame_id: int
    frame_len: int

    @property
    def last_fragment(self) -> bool:
        """是否本帧最后一个分片."""
        return bool(self.flags & FLAG_LAST_FRAGMENT)

    @property
    def reliable(self) -> bool:
        """是否需要接收方回 ACK 并进入重传队列."""
        return bool(self.flags & FLAG_RELIABLE)

    @property
    def control(self) -> bool:
        """是否为握手 JSON 等控制帧."""
        return bool(self.flags & FLAG_CONTROL)

    def encode(self) -> bytes:
        """编码为 16 字节头."""
        ver_type = (PROTOCOL_VERSION << 4) | self.ptype
        return _HEADER.pack(
            ver_type,
            self.flags,
            self.frag_index,
            self.frag_count,
            self.frag_len,
            self.frame_id,
            self.frame_len,
        )

    @classmethod
    def decode(cls, datagram: bytes) -> UdpHeader:
        """解析并校验头部; 任何越界都抛 :class:`UdpFrameError`."""
        if len(datagram) < UDP_HEADER_BYTES:
            raise UdpFrameError("UDP 数据报不足 16 字节头部")
        ver_type, flags, frag_index, frag_count, frag_len, frame_id, frame_len = _HEADER.unpack_from(datagram)
        version = ver_type >> 4
        ptype = ver_type & 0x0F
        if version != PROTOCOL_VERSION:
            raise UdpFrameError(f"UDP 协议版本不支持: {version}")
        if ptype not in VALID_TYPES:
            raise UdpFrameError(f"UDP 类型非法: {ptype}")
        if frag_count == 0 or frag_count > MAX_FRAG_COUNT:
            raise UdpFrameError(f"分片总数越界: {frag_count}")
        if frag_index >= frag_count:
            raise UdpFrameError(f"分片序号越界: {frag_index}/{frag_count}")
        if frag_len != len(datagram) - UDP_HEADER_BYTES:
            raise UdpFrameError("分片长度与数据报实际长度不一致")
        if frag_len > UDP_FRAGMENT_BYTES:
            raise UdpFrameError(f"分片负载超过 {UDP_FRAGMENT_BYTES} 字节")
        if frame_len > MAX_FRAME_BYTES:
            raise UdpFrameError(f"整帧超过 {MAX_FRAME_BYTES} 字节上限")
        if ptype == TYPE_DATA:
            if frame_len == 0:
                raise UdpFrameError("数据帧长度不能为 0")
        elif frag_index != 0 or frag_count != 1 or frame_len != frag_len:
            raise UdpFrameError("控制类数据报必须是单分片且整帧长度等于负载长度")
        return cls(
            ptype=ptype,
            flags=flags,
            frag_index=frag_index,
            frag_count=frag_count,
            frag_len=frag_len,
            frame_id=frame_id,
            frame_len=frame_len,
        )


def fragment_frame(
    frame_id: int,
    payload: bytes,
    *,
    reliable: bool = False,
    control: bool = False,
    datagram_bytes: int = UDP_DATAGRAM_BYTES,
) -> tuple[bytes, ...]:
    """把一个整帧切成若干 1200 字节数据报; 首片到末片的 ``frame_id`` 相同."""
    if not 0 < frame_id <= 0xFFFFFFFF:
        raise UdpFrameError(f"frame_id 超出 32 位范围: {frame_id}")
    if not payload:
        raise UdpFrameError("空帧不可发送")
    if len(payload) > MAX_FRAME_BYTES:
        raise UdpFrameError(f"单帧超过 {MAX_FRAME_BYTES} 字节上限")
    fragment_bytes = datagram_bytes - UDP_HEADER_BYTES
    if fragment_bytes <= 0:
        raise UdpFrameError("数据报长度不足以容纳 16 字节头部")
    frag_count = (len(payload) + fragment_bytes - 1) // fragment_bytes
    if frag_count > MAX_FRAG_COUNT:
        raise UdpFrameError(f"分片数超过 {MAX_FRAG_COUNT}")
    base_flags = (FLAG_RELIABLE if reliable else 0) | (FLAG_CONTROL if control else 0)
    datagrams: list[bytes] = []
    for frag_index in range(frag_count):
        chunk = payload[frag_index * fragment_bytes : (frag_index + 1) * fragment_bytes]
        flags = base_flags | (FLAG_LAST_FRAGMENT if frag_index == frag_count - 1 else 0)
        header = UdpHeader(
            ptype=TYPE_DATA,
            flags=flags,
            frag_index=frag_index,
            frag_count=frag_count,
            frag_len=len(chunk),
            frame_id=frame_id,
            frame_len=len(payload),
        )
        datagrams.append(header.encode() + chunk)
    return tuple(datagrams)


def _control_datagram(ptype: int, frame_id: int, payload: bytes = b"") -> bytes:
    """编码单分片控制类数据报 (ACK/PING/PONG)."""
    header = UdpHeader(
        ptype=ptype,
        flags=0,
        frag_index=0,
        frag_count=1,
        frag_len=len(payload),
        frame_id=frame_id,
        frame_len=len(payload),
    )
    return header.encode() + payload


def encode_ack(frame_id: int) -> bytes:
    """编码 ACK; 接收方用它结束该可靠帧的重传定时."""
    return _control_datagram(TYPE_ACK, frame_id)


def encode_ping(frame_id: int, timestamp_ms: int) -> bytes:
    """编码 PING; 载荷为 8 字节时间戳, 供 PONG 原样回显."""
    return _control_datagram(TYPE_PING, frame_id, timestamp_ms.to_bytes(8, "big"))


def encode_pong(frame_id: int, echo: bytes) -> bytes:
    """编码 PONG; 回显 PING 的 ``frame_id`` 与载荷."""
    return _control_datagram(TYPE_PONG, frame_id, echo)


# -------------------------------- 重组 --------------------------------


@dataclass
class _Assembly:
    """一路正在重组的帧."""

    frame_len: int
    frag_count: int
    reliable: bool
    control: bool
    started_at: float
    slots: dict[int, bytes] = field(default_factory=dict)
    received: int = 0


class ReassemblyTable:
    """按 ``frame_id`` 键控的乱序重组表.

    分片按 ``frag_index`` 定址写入, 天然容忍乱序与重复; 重复分片覆盖同槽位.
    超过 100 ms 未收齐即整帧丢弃, 后续帧不受影响.
    """

    def __init__(
        self,
        *,
        timeout_s: float = REASSEMBLY_TIMEOUT_S,
        slots: int = REASSEMBLY_SLOTS,
        capacity_bytes: int = REASSEMBLY_CAPACITY_BYTES,
    ) -> None:
        self._timeout_s = timeout_s
        self._slots = slots
        self._capacity_bytes = capacity_bytes
        self._pending: dict[int, _Assembly] = {}
        self._bytes = 0
        self.dropped = 0
        self.rejected = 0

    @property
    def size(self) -> int:
        """当前并发重组路数."""
        return len(self._pending)

    @property
    def bytes_reserved(self) -> int:
        """当前已为未完成帧预留的字节数."""
        return self._bytes

    def push(self, header: UdpHeader, fragment: bytes, now: float) -> bytes | None:
        """投入一个分片; 收齐则返回整帧, 否则返回 None."""
        self.expire(now)
        if header.frag_len != len(fragment) or header.frag_len > UDP_FRAGMENT_BYTES:
            self.rejected += 1
            return None
        assembly = self._pending.get(header.frame_id)
        if assembly is None:
            minimum_frags = (header.frame_len + UDP_FRAGMENT_BYTES - 1) // UDP_FRAGMENT_BYTES
            if header.frame_len > self._capacity_bytes or header.frag_count < minimum_frags:
                self.rejected += 1
                return None
            if self._bytes + header.frame_len > self._capacity_bytes:
                self._evict_oldest()
            if len(self._pending) >= self._slots:
                self._evict_oldest()
            assembly = _Assembly(
                frame_len=header.frame_len,
                frag_count=header.frag_count,
                reliable=header.reliable,
                control=header.control,
                started_at=now,
            )
            self._pending[header.frame_id] = assembly
            self._bytes += header.frame_len
        elif assembly.frame_len != header.frame_len or assembly.frag_count != header.frag_count:
            self.rejected += 1
            return None
        previous = assembly.slots.get(header.frag_index)
        if previous is not None:
            assembly.received -= len(previous)
        assembly.slots[header.frag_index] = fragment
        assembly.received += len(fragment)
        if len(assembly.slots) < assembly.frag_count:
            return None
        if assembly.received != assembly.frame_len:
            self._discard(header.frame_id, count_drop=True)
            return None
        payload = b"".join(assembly.slots[index] for index in range(assembly.frag_count))
        self._discard(header.frame_id, count_drop=False)
        if len(payload) != assembly.frame_len:
            self.dropped += 1
            return None
        return payload

    def expire(self, now: float) -> int:
        """清理超时未收齐的帧, 返回被清理路数."""
        expired = [key for key, item in self._pending.items() if now - item.started_at >= self._timeout_s]
        for frame_id in expired:
            self._discard(frame_id, count_drop=True)
        return len(expired)

    def _evict_oldest(self) -> None:
        """容量或并发路数超限时淘汰最早开始的一路."""
        if not self._pending:
            return
        oldest = min(self._pending, key=lambda key: self._pending[key].started_at)
        self._discard(oldest, count_drop=True)

    def _discard(self, frame_id: int, *, count_drop: bool) -> None:
        assembly = self._pending.pop(frame_id, None)
        if assembly is None:
            return
        self._bytes -= assembly.frame_len
        if count_drop:
            self.dropped += 1


# -------------------------------- 重传 --------------------------------


@dataclass
class _PendingControl:
    """一个已发出、等待 ACK 的可靠帧."""

    datagrams: tuple[bytes, ...]
    sent_at: float
    resends: int = 0


class RetransmitQueue:
    """可靠帧的 RTO 队列: 120/240/480 ms 各重传一次, 第 3 次后仍无 ACK 判耗尽."""

    def __init__(self) -> None:
        self._pending: dict[int, _PendingControl] = {}
        self.retransmits = 0
        self.exhausted = 0

    @property
    def pending(self) -> int:
        """仍在等待 ACK 的帧数."""
        return len(self._pending)

    def track(self, frame_id: int, datagrams: Sequence[bytes], now: float) -> None:
        """登记一个已发出的可靠帧."""
        self._pending[frame_id] = _PendingControl(datagrams=tuple(datagrams), sent_at=now)

    def acknowledge(self, frame_id: int) -> bool:
        """收到 ACK 后撤销重传; 返回该帧是否在等待队列中."""
        return self._pending.pop(frame_id, None) is not None

    def due(self, now: float) -> tuple[bytes, ...]:
        """返回本次到期需要重发的数据报; 第 3 次重传后把该帧记为耗尽."""
        outgoing: list[bytes] = []
        for frame_id in list(self._pending):
            item = self._pending[frame_id]
            due_at = item.sent_at + RETRANSMIT_CUMULATIVE_MS[item.resends] / 1000.0
            if now + _TIME_EPS < due_at:
                continue
            self.retransmits += 1
            outgoing.extend(item.datagrams)
            item.resends += 1
            if item.resends >= RETRANSMIT_LIMIT:
                del self._pending[frame_id]
                self.exhausted += 1
        return tuple(outgoing)


# -------------------------------- 会话平面 --------------------------------


@dataclass(frozen=True)
class InboundFrame:
    """接收侧交付物; ``kind`` 取 data/ack/ping/pong, 仅 data 且重组完成时带整帧载荷."""

    kind: str
    frame_id: int
    payload: bytes = b""


class UdpSessionPlane:
    """打洞通路的 UDP 会话平面: 分片发送、乱序重组、ACK 重传与心跳.

    socket 可以为 None, 此时所有拟发送的数据报只记录在 ``sent_datagrams``, 便于编排层接管与单测.
    ``clock`` 注入后所有时间判定可完全复现, 不受真实时钟影响.
    """

    def __init__(
        self,
        sock: socket.socket | None = None,
        *,
        peer: tuple[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        module: str = MODULE,
    ) -> None:
        self._sock = sock
        self._clock = clock
        self._module = module
        started = clock()
        self._last_rx = started
        self._last_ping = started
        self._next_id = 1
        self._datagram_bytes = UDP_DATAGRAM_BYTES
        self._reassembly = ReassemblyTable()
        self._retransmit = RetransmitQueue()
        self._outbox: list[bytes] = []
        self.sent_datagrams: list[bytes] = []
        self.frames_sent = 0
        self.datagrams_sent = 0
        self.frames_reassembled = 0
        self.invalid_datagrams = 0
        self.replay_drop = 0
        self._consecutive_decrypt_failures = 0
        self.dead_reason = ""
        if sock is not None:
            if peer is not None:
                sock.connect(peer)
            self._tune_socket(sock)

    @property
    def path_dead(self) -> bool:
        """通路是否已判失效 (3 s 无合法包 / 重传耗尽 / 连续解密失败)."""
        return bool(self.dead_reason)

    @property
    def datagram_bytes(self) -> int:
        """当前发送使用的数据报长度; 发生 EMSGSIZE 降级后为 1100."""
        return self._datagram_bytes

    @property
    def reasm_drop(self) -> int:
        """重组超时或容量淘汰导致整帧丢弃的次数."""
        return self._reassembly.dropped

    @property
    def reasm_pending(self) -> int:
        """当前并发重组路数."""
        return self._reassembly.size

    @property
    def retransmits(self) -> int:
        """累计重传次数."""
        return self._retransmit.retransmits

    def send_frame(self, payload: bytes, *, reliable: bool = False, control: bool = False) -> int:
        """发送一个整帧并返回 ``frame_id``; 可靠帧进入重传队列, 视频帧不重传."""
        frame_id = self._allocate_frame_id()
        datagrams = fragment_frame(
            frame_id,
            payload,
            reliable=reliable,
            control=control,
            datagram_bytes=self._datagram_bytes,
        )
        try:
            self._transmit(datagrams)
        except OSError as exc:
            if exc.errno not in _EMSGSIZE or self._datagram_bytes <= MTU_DOWNGRADE_DATAGRAM_BYTES:
                raise
            self._datagram_bytes = MTU_DOWNGRADE_DATAGRAM_BYTES
            datagrams = fragment_frame(
                frame_id,
                payload,
                reliable=reliable,
                control=control,
                datagram_bytes=self._datagram_bytes,
            )
            self._transmit(datagrams)
            log_event(
                self._module,
                "udp.mtu_downgrade",
                level="WARNING",
                datagram_bytes=self._datagram_bytes,
                frame_id=frame_id,
            )
        self.frames_sent += 1
        if reliable:
            self._retransmit.track(frame_id, datagrams, self._clock())
        return frame_id

    def acknowledge(self, frame_id: int) -> None:
        """把 ACK 放进待发队列, 由下一次 :meth:`tick` 或 :meth:`drain_outbox` 发出."""
        self._outbox.append(encode_ack(frame_id))

    def receive(self, datagram: bytes, now: float | None = None) -> InboundFrame | None:
        """接收一个数据报; 返回交付物, 未收齐或非法时返回 None."""
        stamp = self._clock() if now is None else now
        try:
            header = UdpHeader.decode(datagram)
        except UdpFrameError:
            self.invalid_datagrams += 1
            return None
        payload = datagram[UDP_HEADER_BYTES:]
        self._last_rx = stamp
        if header.ptype == TYPE_ACK:
            self._retransmit.acknowledge(header.frame_id)
            return InboundFrame("ack", header.frame_id)
        if header.ptype == TYPE_PING:
            self._outbox.append(encode_pong(header.frame_id, payload))
            return InboundFrame("ping", header.frame_id, payload)
        if header.ptype == TYPE_PONG:
            return InboundFrame("pong", header.frame_id, payload)
        assembled = self._reassembly.push(header, payload, stamp)
        if assembled is None:
            return None
        self.frames_reassembled += 1
        return InboundFrame("data", header.frame_id, assembled)

    def open_frame(
        self,
        datagram: bytes,
        decrypt: Callable[[bytes], bytes],
        now: float | None = None,
    ) -> bytes | None:
        """接收并解密一帧; 重放或越界只丢该帧并计数, 不关闭会话."""
        inbound = self.receive(datagram, now)
        if inbound is None or inbound.kind != "data":
            return None
        return self.decrypt_payload(inbound.payload, decrypt)

    def decrypt_payload(self, payload: bytes, decrypt: Callable[[bytes], bytes]) -> bytes | None:
        """解密整帧; 计数器越界、重放与认证失败一律丢帧计数, 不抛异常."""
        try:
            plaintext = decrypt(payload)
        except (InvalidTag, ValueError):
            self.replay_drop += 1
            self._consecutive_decrypt_failures += 1
            if self._consecutive_decrypt_failures >= REPLAY_FAILURE_LIMIT:
                self.mark_dead(REASON_REPLAY_STORM)
            return None
        self._consecutive_decrypt_failures = 0
        return plaintext

    def tick(self, now: float | None = None) -> tuple[bytes, ...]:
        """周期驱动: 清理重组、检查失效、发到期重传与 1 s 心跳, 并冲出待发队列."""
        stamp = self._clock() if now is None else now
        self._reassembly.expire(stamp)
        if self.path_dead:
            return ()
        if stamp - self._last_rx >= PATH_DEAD_S:
            self.mark_dead(REASON_PATH_DEAD)
            return ()
        exhausted_before = self._retransmit.exhausted
        outgoing = list(self._retransmit.due(stamp))
        if self._retransmit.exhausted > exhausted_before:
            self.mark_dead(REASON_RETRANSMIT_EXHAUSTED)
        if stamp - self._last_ping >= HEARTBEAT_INTERVAL_S:
            self._last_ping = stamp
            outgoing.append(encode_ping(self._allocate_frame_id(), int(stamp * 1000)))
        outgoing.extend(self._outbox)
        self._outbox.clear()
        if outgoing:
            self._transmit(outgoing)
        return tuple(outgoing)

    def drain_outbox(self) -> tuple[bytes, ...]:
        """取走全部待发数据报, 供调用方自行发送."""
        outgoing = tuple(self._outbox)
        self._outbox.clear()
        return outgoing

    def mark_dead(self, reason: str) -> None:
        """标记通路失效并落一次结构化事件; 重复调用只记首次."""
        if self.path_dead:
            return
        self.dead_reason = reason
        log_event(
            self._module,
            "udp.path_dead",
            level="WARNING",
            reason=reason,
            reasm_drop=self.reasm_drop,
            replay_drop=self.replay_drop,
            retransmits=self.retransmits,
            frames_sent=self.frames_sent,
        )

    def close(self) -> None:
        """关闭底层 socket; 未接 socket 时为空操作."""
        if self._sock is None:
            return
        try:
            self._sock.close()
        except OSError:
            return
        finally:
            self._sock = None

    def _allocate_frame_id(self) -> int:
        frame_id = self._next_id
        self._next_id = 1 if frame_id >= 0xFFFFFFFF else frame_id + 1
        return frame_id

    def _transmit(self, datagrams: Sequence[bytes]) -> None:
        if self._sock is None:
            self.sent_datagrams.extend(datagrams)
            self.datagrams_sent += len(datagrams)
            return
        for datagram in datagrams:
            self._sock.send(datagram)
            self.datagrams_sent += 1

    @staticmethod
    def _tune_socket(sock: socket.socket) -> None:
        """把收发缓冲区抬到 4 MiB, 避免 1080p 突发被内核丢弃."""
        for option in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, option, SOCKET_BUFFER_BYTES)
            except OSError:
                continue
