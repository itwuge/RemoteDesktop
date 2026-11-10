# ==========================================================================================
# 文件        : punch.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 真实 UDP 双向打洞探测: 100ms 间隔发包、回声应答、RTT 采样与丢包统计
# 架构定位    : 连接策略的“真实 checker”, 只测量连通性, 不判定是否成功
# 核心功能    : 序列化探测包; 同一 socket 上收发; 双向响应计数; RTT 样本; 首包时延; 丢包率;
#               被控端 PunchResponder 应答探测并在命中后把同一 socket 交给数据面
# 职责边界    : 不处理信令、不选候选、不做业务加密; 成功判据由 policy.run_connectivity_checks 负责
# 运行环境    : Python 3.12
# 依赖关系    : 被 src/rdc/connect/runtime.py 调用; 返回 policy.CheckOutcome
# ==========================================================================================

"""UDP 双向打洞探测器.

探测包格式固定为 ``RDCP1`` + 方向字节(Q 请求 / R 应答) + 16 位十六进制令牌.
收到 Q 立即回 R(证明反向可达), 收到匹配令牌的 R 记录一次 RTT(证明正向可达).
"""

from __future__ import annotations

import contextlib
import secrets
import select
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence

from rdc.connect.policy import CHECK_INTERVAL_MS, DIRECT_BUDGET_MS, Candidate, CheckOutcome
from rdc.connect.stun import bind_udp_in_pool

PROBE_MAGIC = b"RDCP1"
PROBE_REQUEST = b"Q"
PROBE_REPLY = b"R"
PROBE_TOKEN_BYTES = 8
# 被控端主动探测节奏与控制端 CHECK_INTERVAL_MS 对齐; 预算覆盖 L2 5 s 与握手余量
RESPONDER_PROBE_INTERVAL_S = CHECK_INTERVAL_MS / 1000.0
RESPONDER_PROBE_BUDGET_S = 8.0


def encode_probe(token: str, kind: bytes) -> bytes:
    """编码一个探测包."""
    return PROBE_MAGIC + kind + token.encode("ascii")


def _send_endpoint(sock: socket.socket, address: str, port: int) -> tuple[str, int]:
    """把目标地址转成当前 socket 可发送的形式.

    被控端打洞 socket 是 ``AF_INET6`` 双栈, 直接向 IPv4 元组 ``sendto`` 会报地址族
    错误; 这里统一转成 v4-mapped, 与 STUN 的发送口径保持一致.
    """
    if sock.family == socket.AF_INET6 and ":" not in address:
        return (f"::ffff:{address}", port)
    return (address, port)


def decode_probe(data: bytes) -> tuple[str, str] | None:
    """解码探测包, 返回 (方向 Q/R, 令牌); 非本协议包返回 None."""
    if len(data) != len(PROBE_MAGIC) + 1 + PROBE_TOKEN_BYTES * 2 or not data.startswith(PROBE_MAGIC):
        return None
    kind = data[len(PROBE_MAGIC) : len(PROBE_MAGIC) + 1]
    if kind not in {PROBE_REQUEST, PROBE_REPLY}:
        return None
    token = data[len(PROBE_MAGIC) + 1 :]
    try:
        return kind.decode("ascii"), token.decode("ascii")
    except UnicodeDecodeError:
        return None


class UdpPunchChecker:
    """持有 v4/v6 两个 UDP socket 的双向探测检查器.

    两个 socket 绑定在 49152-65535 端口池内; STUN 必须复用这里的 v4 socket,
    否则映射端口与打洞端口不一致, 对端无法命中.
    """

    def __init__(self, v4_port: int = 0, v6_port: int = 0) -> None:
        self._sockets: dict[int, socket.socket] = {}
        self._peer: tuple[str, int] | None = None
        for family, port, host in (
            (4, v4_port, ""),
            (6, v6_port, "::"),
        ):
            try:
                sock = bind_udp_in_pool(socket.AF_INET6 if family == 6 else socket.AF_INET)
                if port:
                    sock.close()
                    sock = socket.socket(socket.AF_INET6 if family == 6 else socket.AF_INET, socket.SOCK_DGRAM)
                    sock.bind((host, port))
                sock.setblocking(False)
            except OSError:
                continue
            self._sockets[family] = sock

    @property
    def ports(self) -> dict[int, int]:
        """各地址族实际绑定的端口; 未绑定的族不出现."""
        return {family: int(sock.getsockname()[1]) for family, sock in self._sockets.items()}

    @property
    def peer(self) -> tuple[str, int] | None:
        """探测中观察到的对端来源地址, 供 L2 命中后数据面 ``connect()`` 锁定."""
        return self._peer

    def socket_for(self, family: int) -> socket.socket | None:
        """返回指定地址族的 socket, 未绑定时为 None."""
        return self._sockets.get(family)

    def release(self, family: int) -> socket.socket | None:
        """交出指定族 socket 的所有权(数据面复用同一端口); 之后 ``close()`` 不再关闭它."""
        return self._sockets.pop(family, None)

    def close(self) -> None:
        """关闭全部 socket, 可重复调用."""
        for sock in self._sockets.values():
            try:
                sock.close()
            except OSError:
                continue
        self._sockets.clear()

    def __call__(self, peer: Sequence[Candidate], budget_ms: int = DIRECT_BUDGET_MS) -> CheckOutcome:
        """对候选列表发起到期为止的双向探测."""
        active = [item for item in peer if item.port > 0 and self._sockets.get(item.family) is not None]
        if not active:
            return CheckOutcome()

        started = time.monotonic()
        deadline = started + max(0.05, budget_ms / 1000.0)
        pending: dict[str, float] = {}
        sent = 0
        forward = 0
        backward = 0
        samples: list[float] = []
        first_packet: float | None = None
        next_send = started
        waitable = list(self._sockets.values())

        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_send:
                for candidate in active:
                    sock = self._sockets[candidate.family]
                    token = secrets.token_hex(PROBE_TOKEN_BYTES)
                    try:
                        sock.sendto(encode_probe(token, PROBE_REQUEST), (candidate.address, candidate.port))
                    except OSError:
                        continue
                    pending[token] = now
                    sent += 1
                next_send = now + CHECK_INTERVAL_MS / 1000.0

            ready, _, _ = select.select(waitable, [], [], max(0.0, min(0.02, deadline - now)))
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except OSError:
                    continue
                parsed = decode_probe(data)
                if parsed is None:
                    continue
                if first_packet is None:
                    first_packet = (time.monotonic() - started) * 1000.0
                kind, token = parsed
                self._peer = (str(addr[0]), int(addr[1]))
                if kind == "Q":
                    try:
                        sock.sendto(encode_probe(token, PROBE_REPLY), addr)
                    except OSError:
                        continue
                    backward += 1
                elif token in pending:
                    samples.append((time.monotonic() - pending.pop(token)) * 1000.0)
                    forward += 1

        loss_pct = round(100.0 * (sent - forward) / sent, 2) if sent else 0.0
        return CheckOutcome(
            sent=sent,
            responses_forward=forward,
            responses_backward=backward,
            rtt_samples_ms=tuple(samples),
            first_packet_ms=first_packet,
            loss_pct=max(0.0, loss_pct),
        )


class PunchResponder:
    """被控端 UDP 打洞应答器: 在会话 socket 上回 ``RDCP1/R``, 命中后把同一 socket 交给数据面.

    socket 由调用方持有(通常是绑定好端口的会话 socket), 探测器与数据面复用它, 不另开端口,
    保证 NAT 映射端口与打洞端口一致. ``release()`` 交出所有权后本类不再读写该 socket, 数据面
    负责 ``connect()`` 锁定对端并最终关闭.
    """

    def __init__(self, sock: socket.socket, clock: Callable[[], float] = time.monotonic) -> None:
        self._sock: socket.socket | None = sock
        self._clock = clock
        self._io_lock = threading.Lock()
        self._targets: tuple[tuple[str, int], ...] = ()
        self._pending: dict[str, float] = {}
        self._next_probe = 0.0
        self._probe_deadline = 0.0
        self.replies = 0
        self.ignored = 0
        self.sent = 0
        self.forward = 0
        self.peer: tuple[str, int] | None = None
        self.last_seen: float | None = None

    @property
    def responsive(self) -> bool:
        """是否至少应答过一次探测, 即对端已能到达本端."""
        return self.replies > 0

    @property
    def local_port(self) -> int:
        """打洞 socket 实际绑定的 UDP 端口; 未持有 socket 时为 0."""
        sock = self._sock
        return int(sock.getsockname()[1]) if sock is not None else 0

    @property
    def targets(self) -> tuple[tuple[str, int], ...]:
        """当前主动探测目标, 供状态展示与排障."""
        return self._targets

    def set_targets(
        self,
        targets: Sequence[tuple[str, int]],
        budget_s: float = RESPONDER_PROBE_BUDGET_S,
    ) -> None:
        """设置主动探测目标(控制端 IPv4 候选), 同时打开本端 NAT 映射.

        只被动应答时控制端 ``backward`` 恒为 0, 双向判据无法成立; 因此命中准入后
        由被控端按同一节奏主动发 ``RDCP1/Q``. 预算到期后停止发包, 仍继续应答.
        """
        unique: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for address, port in targets:
            key = (str(address).strip().strip("[]"), int(port))
            if not key[0] or key[1] <= 0 or key in seen:
                continue
            seen.add(key)
            unique.append(key)
        now = self._clock()
        with self._io_lock:
            self._targets = tuple(unique)
            self._pending.clear()
            self._next_probe = now
            self._probe_deadline = now + max(0.1, budget_s) if unique else 0.0

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[socket.socket | None]:
        """独占 socket 做一次性收发(如 STUN); 期间轮询与探测等待, 避免抢走响应."""
        with self._io_lock:
            yield self._sock

    def poll(self, wait_s: float = 0.0) -> int:
        """主动探测一轮并处理可读数据报, 返回本轮应答数."""
        with self._io_lock:
            sock = self._sock
            if sock is None:
                return 0
            self._send_probes(sock)
            ready, _, _ = select.select([sock], [], [], max(0.0, wait_s))
            answers = 0
            for _ in ready:
                answers += self._drain(sock)
            return answers

    def _send_probes(self, sock: socket.socket) -> None:
        """按 100 ms 节奏向目标发 Q; 预算到期或没有目标时静默."""
        if not self._targets:
            return
        now = self._clock()
        if now < self._next_probe or now > self._probe_deadline:
            return
        for address, port in self._targets:
            token = secrets.token_hex(PROBE_TOKEN_BYTES)
            try:
                sock.sendto(encode_probe(token, PROBE_REQUEST), _send_endpoint(sock, address, port))
            except OSError:
                continue
            self._pending[token] = now
            self.sent += 1
        self._next_probe = now + RESPONDER_PROBE_INTERVAL_S

    def _drain(self, sock: socket.socket) -> int:
        answers = 0
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            parsed = decode_probe(data)
            if parsed is None:
                self.ignored += 1
                continue
            kind, token = parsed
            if kind == "R":
                # 本端主动探测的回声: 只记正向可达, 不能计入 ignored, 否则会误判数据面交接
                if self._pending.pop(token, None) is None:
                    self.ignored += 1
                    continue
                self.peer = (str(addr[0]), int(addr[1]))
                self.last_seen = self._clock()
                self.forward += 1
                continue
            try:
                sock.sendto(encode_probe(token, PROBE_REPLY), addr)
            except OSError:
                continue
            self.peer = (str(addr[0]), int(addr[1]))
            self.last_seen = self._clock()
            self.replies += 1
            answers += 1
        return answers

    def release(self) -> socket.socket | None:
        """交出 socket 所有权给数据面; 之后 ``poll``/``close`` 均为空操作."""
        with self._io_lock:
            sock = self._sock
            self._sock = None
        return sock

    def close(self) -> None:
        """关闭 socket; 已 ``release`` 时为空操作, 由数据面负责关闭."""
        with self._io_lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()
