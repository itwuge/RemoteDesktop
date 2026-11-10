# ==========================================================================================
# 文件        : runtime.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 真实建连管线: 网卡/SRFLX 候选 + 信令候选交换 + 打洞 + 中继申请, 装配成 establish_session
# 架构定位    : 策略层(policy)与系统实现层(native/stun/punch/relay)之间的装配层
# 核心功能    : 端口池 socket 复用; 先交换候选再判定; 真实 prober/checker/relay 注入
# 职责边界    : 不做判定、不改会话状态; 失败原因由 policy 的 reason 枚举表达
# 运行环境    : Python 3.12
# 依赖关系    : 被 src/rdc/commands/service.py 调用; 依赖 native / stun / punch / relay
# ==========================================================================================

"""真实建连装配.

关键约束: STUN、端口映射与打洞必须复用同一个 UDP socket, 否则映射端口与打洞端口不一致,
对端发来的包无法命中. 因此先创建 ``UdpPunchChecker``, 再用它的端口收集 host 候选并申请映射.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from rdc.connect.mapping import (
    MAPPING_LEASE_FAILED,
    MappingLease,
    MappingOutcome,
    PortMapper,
    local_ipv4,
    mapped_candidate,
    reconcile_with_stun,
)
from rdc.connect.native import native_host_candidates, native_ipv6_probe
from rdc.connect.policy import (
    FAMILY_V4,
    IPV6_TCP_ATTEMPTS,
    IPV6_TCP_TIMEOUT_MS,
    KIND_HOST,
    KIND_SRFLX,
    REASON_IPV6_UNREACHABLE,
    REASON_OK,
    STUN_TOTAL_MS,
    TRANSPORT_DIRECT_IPV6,
    TRANSPORT_DIRECT_UDP,
    Candidate,
    EstablishResult,
    TcpProbe,
    assert_direct_ipv6,
    direct_level,
    establish_session,
    path_snapshot,
)
from rdc.connect.punch import UdpPunchChecker
from rdc.connect.relay import exchange_candidates, request_relay_allocation
from rdc.connect.stun import discover_srflx
from rdc.connect.udp_plane import UdpSessionPlane
from rdc.core import logging as rdc_logging
from rdc.crypto.identity import DeviceIdentity

MODULE = "connect.runtime"


class TcpDirectProber:
    """L1 真实探测: 对 v6 候选逐个 TCP 建连, 三次握手成功一次即通过.

    每候选超时 800 ms、最多 2 次、层总预算 1500 ms (§4.1). 探测 socket 建连后立即关闭,
    不占用数据面连接: 数据面仍由既有 TCP 会话按判定结果建立, 本类不改封装.
    """

    def __init__(self, timeout_ms: int = IPV6_TCP_TIMEOUT_MS, attempts: int = IPV6_TCP_ATTEMPTS) -> None:
        self._timeout_s = max(timeout_ms, 1) / 1000.0
        self._attempts = max(1, attempts)

    def __call__(self, peer: Sequence[Candidate], budget_ms: int) -> TcpProbe:
        deadline = time.monotonic() + max(budget_ms, 1) / 1000.0
        tried = 0
        last_error = ""
        rtts: list[float] = []
        for candidate in list(peer)[: self._attempts]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            tried += 1
            started = time.monotonic()
            try:
                sock = socket.create_connection(
                    (candidate.address, candidate.port), timeout=min(self._timeout_s, remaining)
                )
            except OSError as exc:
                last_error = f"{candidate.address}:{candidate.port} {type(exc).__name__}"
                continue
            rtts.append(round((time.monotonic() - started) * 1000.0, 3))
            try:
                local_port = int(sock.getsockname()[1])
            except OSError:
                local_port = 0
            finally:
                sock.close()
            return TcpProbe(
                ok=True,
                peer_address=candidate.address,
                peer_family=candidate.family,
                peer_port=candidate.port,
                local_port=local_port,
                rtt_ms=tuple(rtts),
                tried=tried,
                reason=REASON_OK,
            )
        return TcpProbe(
            ok=False,
            rtt_ms=tuple(rtts),
            tried=tried,
            reason=REASON_IPV6_UNREACHABLE,
            error=last_error,
        )


def log_path_events(session_id: str, result: EstablishResult) -> None:
    """按 spec ipv6-punch §4.4 落 L1 结果、路径选择与直连断言.

    事件字段名 ``level`` 与结构化日志的严重级别同名, 这里用 ``layer`` 承载 ``L1``, 避免
    覆盖 ``level`` 字段导致日志级别丢失; 其余字段名与 §4.4 一致.
    """
    level = direct_level(result)
    rdc_logging.log_event(
        MODULE,
        "connect.level_result",
        level="INFO" if level.ok else "WARNING",
        duration_ms=level.duration_ms,
        session_id=session_id,
        layer="L1",
        transport=TRANSPORT_DIRECT_IPV6,
        ok=level.ok,
        reason=level.reason,
        candidates=level.detail.get("candidates", 0),
        tried=level.detail.get("tried", 0),
        rtt_ms=level.detail.get("rtt_ms", []),
        peer_address=level.detail.get("peer_address", ""),
    )
    path = path_snapshot(result)
    rdc_logging.log_event(MODULE, "connect.path_selected", session_id=session_id, **path)
    verdict = assert_direct_ipv6(path)
    rdc_logging.log_event(
        MODULE,
        "assert_direct_ipv6",
        level="INFO" if verdict["pass"] else "WARNING",
        session_id=session_id,
        assert_direct_ipv6=verdict,
    )


def collect_real_candidates(
    checker: UdpPunchChecker,
    tcp_port: int,
    deadline_ms: int = STUN_TOTAL_MS,
) -> tuple[Candidate, ...]:
    """收集本机候选: v4 host 用打洞端口, v6 host 用会话 TCP 端口, srflx 复用打洞 socket.

    v6 host 候选供 L1 TCP 直连使用, 端口必须是监听侧的会话端口(由命令层传入实际值);
    打洞 socket 的 UDP 端口不能用于 TCP 探测.
    """
    ports = checker.ports
    hosts = native_host_candidates(v4_port=ports.get(4, 0), v6_port=tcp_port)
    srflx: tuple[Candidate, ...] = ()
    sock = checker.socket_for(4)
    if sock is not None:
        srflx, _error = discover_srflx(sock, total_ms=deadline_ms)
    return (*hosts, *srflx)


def attach_port_mapping(
    checker: UdpPunchChecker,
    candidates: Sequence[Candidate],
    description: str = "",
) -> tuple[tuple[Candidate, ...], MappingOutcome | None, MappingLease | None]:
    """为打洞端口申请 UPnP/NAT-PMP 映射, 并把可用映射并入候选.

    映射必须与 STUN 反射地址一致: 不一致说明存在多层 NAT/CGNAT, 映射不能作为直连候选,
    只保留失败原因供日志与界面提示. 返回 (候选, 映射结果, 租约), 失败时租约为 None.
    """
    sock = checker.socket_for(4)
    port = checker.ports.get(4, 0)
    if sock is None or port <= 0:
        return tuple(candidates), None, None
    stun_address = next((item.address for item in candidates if item.kind == KIND_SRFLX), "")
    internal_client = (
        next(
            (item.address for item in candidates if item.kind == KIND_HOST and item.family == FAMILY_V4),
            "",
        )
        or local_ipv4()
    )
    try:
        mapper = PortMapper(local_ip=internal_client)
        outcome = mapper.map_udp_port(
            port,
            internal_client=internal_client,
            description=description,
            sock=sock,
        )
        outcome = reconcile_with_stun(outcome, stun_address)
        candidate = mapped_candidate(outcome)
    except (OSError, ValueError, RuntimeError) as exc:
        # 端口映射是锦上添花, 任何本地环境问题都不允许阻断建连主流程
        rdc_logging.log_event(
            MODULE,
            "mapping.attach_failed",
            level="WARNING",
            reason=MAPPING_LEASE_FAILED,
            error=type(exc).__name__,
        )
        return tuple(candidates), None, None
    if candidate is None:
        return tuple(candidates), outcome, None
    lease = MappingLease(mapper, outcome, sock=sock)
    lease.start()
    return (*candidates, candidate), outcome, lease


def attach_udp_plane(
    result: EstablishResult,
    checker: UdpPunchChecker,
    clock: Callable[[], float] = time.monotonic,
) -> EstablishResult:
    """L2 命中后把打洞 socket 交给 UDP 数据面, 探测与数据面复用同一端口.

    只接管 v4 打洞 socket; 非打洞路径或未观察到对端端点时原样返回. 接管后该 socket 由数据面
    负责 ``connect()`` 锁定对端并在会话结束时关闭.
    """
    peer = checker.peer
    if result.decision.transport != TRANSPORT_DIRECT_UDP or peer is None:
        return result
    sock = checker.release(FAMILY_V4)
    if sock is None:
        return result
    plane = UdpSessionPlane(sock, peer=peer, clock=clock)
    return replace(result, udp_plane=plane, peer=peer)


def establish_real_session(
    identity: DeviceIdentity,
    peer_device_id: str,
    session_id: str,
    access_code: str,
    on_progress: Callable[[int, str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    *,
    tcp_port: int,
) -> EstablishResult:
    """真实建连入口: 收候选 -> 端口映射 -> 信令换对端候选 -> L1/L2/L3 判定 -> 中继兜底.

    :param tcp_port: 本端会话 TCP 监听端口(命令层传入实际配置值), 用于 v6 host 候选
    """
    checker = UdpPunchChecker()
    lease: MappingLease | None = None
    try:
        local = collect_real_candidates(checker, tcp_port=tcp_port)
        local, _mapping, lease = attach_port_mapping(
            checker,
            local,
            description=f"rdc-{identity.device_id[:8]}",
        )
        peer = exchange_candidates(identity, session_id, peer_device_id, local, access_code)

        def collector(_deadline_ms: int) -> Sequence[Candidate]:
            return local

        def relay_requester() -> Mapping[str, Any]:
            return request_relay_allocation(identity, session_id)

        result = establish_session(
            native_ipv6_probe,
            collector,
            checker,
            relay_requester,
            peer_candidates=peer,
            clock=clock,
            on_progress=on_progress,
            tcp_prober=TcpDirectProber(),
        )
        if result.decision.transport == TRANSPORT_DIRECT_UDP and checker.peer is not None:
            # 打洞已命中, 映射租约不再续期, 打洞 socket 转交数据面复用同一端口
            if lease is not None:
                lease.close()
                lease = None
            result = attach_udp_plane(result, checker, clock)
        log_path_events(session_id, result)
        return result
    finally:
        if lease is not None:
            lease.close()
        checker.close()
