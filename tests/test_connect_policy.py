# ==========================================================================================
# 文件        : test_connect_policy.py
# 所属目录    : tests/
# 文件用途    : 连接策略判定层单测: IPv6 优先、打洞、超时切中继、运行期降级、不回切
# 架构定位    : 纯桩测试, 不建立任何真实 socket, 不依赖本机是否有 IPv6
# 核心功能    : 覆盖 18 项共享参数的判定语义与建连编排分支
# 职责边界    : 只测 src/rdc/connect/policy.py, 不测平台原生实现
# 运行环境    : Python 3.12
# 依赖关系    : pytest
# ==========================================================================================

"""连接策略单测, 全部使用注入桩.

注意: 这些用例证明的是判定逻辑, 不是真机通路的实测结果. 本机当前无全局 IPv6,
锥形<->锥形也未取得第二个独立公网出口, 对应路径在报告中仍标注为未验证.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from rdc.connect.policy import (
    CANDIDATE_BUDGET_MS,
    DEGRADE_LOSS_PCT,
    DEGRADE_NO_FRAME_S,
    DEGRADE_P95_MS,
    DIRECT_BUDGET_MS,
    IPV6_TCP_BUDGET_MS,
    KEYFRAME_RETRY_LIMIT,
    KIND_MAPPED,
    MAPPED_PRIORITY_EXTERNAL,
    MAPPED_PRIORITY_READBACK,
    MAPPING_EIM,
    MAPPING_SYMMETRIC,
    MAPPING_UNKNOWN,
    PRIORITY_HOST_V4_PRIVATE,
    PRIORITY_HOST_V4_PUBLIC,
    PRIORITY_IPV6_GLOBAL,
    PRIORITY_IPV6_ULA,
    PRIORITY_RELAY,
    PRIORITY_SRFLX,
    REASON_IPV6_NO_GLOBAL,
    REASON_IPV6_UNREACHABLE,
    REASON_NO_CANDIDATE,
    REASON_OK,
    REASON_PUNCH_TIMEOUT,
    REASON_RELAY_UNAVAILABLE,
    REASON_SINGLE_DIRECTION,
    REASON_SYMMETRIC_NAT,
    RELAY_PREFETCH_AT_MS,
    STUN_TOTAL_MS,
    TRANSPORT_DIRECT_IPV6,
    TRANSPORT_DIRECT_UDP,
    TRANSPORT_RELAY,
    Candidate,
    CheckOutcome,
    Ipv6Probe,
    QualitySample,
    TcpProbe,
    TransportMonitor,
    classify_mapping,
    collect_candidates,
    decide,
    direct_level,
    establish_session,
    filter_candidates,
    has_v6_host,
    infer_family,
    ipv6_self_check,
    is_link_local,
    is_publishable,
    path_snapshot,
    run_connectivity_checks,
    same_egress,
    scope_of,
    sort_candidates,
    watch_quality,
)
from rdc.connect.relay import candidate_to_payload, payload_to_candidate

PUBLIC_V4 = "115.150.84.112"


class FakeClock:
    """可控时钟, 让各级耗时与预取阈值可断言."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, milliseconds: float) -> None:
        self.now += milliseconds / 1000.0


def v6_host(address: str = "240e:1:2:3::10") -> Candidate:
    return Candidate(kind="host", family=6, address=address, port=49160, priority=100)


def v4_host(address: str = "192.168.1.80") -> Candidate:
    return Candidate(kind="host", family=4, address=address, port=49160, priority=80)


def srflx(port: int = 49160, address: str = PUBLIC_V4) -> Candidate:
    return Candidate(kind="srflx", family=4, address=address, port=port, priority=90, related="192.168.1.80")


def ok_outcome(forward: int = 3, backward: int = 3, rtts: tuple[float, ...] = (10.0, 11.0, 12.0)) -> CheckOutcome:
    return CheckOutcome(sent=10, responses_forward=forward, responses_backward=backward, rtt_samples_ms=rtts)


def relay_ok() -> dict[str, object]:
    return {"budget_kbps": 3000, "quality_profile": "720p30"}


# -------------------------------- IPv6 自检 --------------------------------


def test_ipv6_self_check_requires_global_and_reachable() -> None:
    result = ipv6_self_check(lambda _deadline: Ipv6Probe(global_addresses=("240e::1",)))
    assert not result.ok
    assert result.reason == REASON_IPV6_UNREACHABLE

    no_global = ipv6_self_check(lambda _deadline: Ipv6Probe(reachable_targets=("2400:3200::1",)))
    assert not no_global.ok
    assert no_global.reason == REASON_IPV6_NO_GLOBAL

    good = ipv6_self_check(lambda _deadline: Ipv6Probe(("240e::1",), ("2400:3200::1",), rtt_ms=28.0))
    assert good.ok
    assert good.reason == REASON_OK
    assert good.detail["rtt_p50_ms"] == 28.0


def test_ipv6_probe_error_degrades() -> None:
    def boom(_deadline: int) -> Ipv6Probe:
        raise TimeoutError("probe timeout")

    result = ipv6_self_check(boom)
    assert not result.ok
    assert result.reason in {REASON_IPV6_UNREACHABLE, REASON_IPV6_NO_GLOBAL}


# -------------------------------- 候选与映射 --------------------------------


def test_classify_mapping() -> None:
    assert classify_mapping([]) == MAPPING_UNKNOWN
    assert classify_mapping([srflx(49160)]) == MAPPING_EIM
    assert classify_mapping([srflx(49160), srflx(49160)]) == MAPPING_EIM
    assert classify_mapping([srflx(49160), srflx(52190)]) == MAPPING_SYMMETRIC


def test_collect_candidates_sorts_and_survives_timeout() -> None:
    collected = collect_candidates(lambda _deadline: [v4_host(), v6_host(), srflx()])
    assert collected[0].family == 6
    assert [item.kind for item in collected].count("srflx") == 1

    def stun_timeout(_deadline: int) -> Sequence[Candidate]:
        raise TimeoutError("stun timeout")

    assert collect_candidates(stun_timeout) == ()
    assert collect_candidates(lambda _deadline: [Candidate("relay", 4, "1.1.1.1", 0)]) == ()


def test_same_egress_detection() -> None:
    assert same_egress([srflx(49160), srflx(56763)])
    assert not same_egress([srflx(49160, "115.150.84.112"), srflx(49160, "1.2.3.4")])
    assert not same_egress([v4_host()])
    assert has_v6_host([v6_host()])
    assert not has_v6_host([v4_host()])


# -------------------------------- 连通检查 --------------------------------


def test_connectivity_requires_both_directions() -> None:
    only_forward = run_connectivity_checks([v4_host()], lambda _peer, _budget: ok_outcome(backward=0))
    assert not only_forward.ok
    assert only_forward.reason == REASON_SINGLE_DIRECTION

    too_few_rtt = run_connectivity_checks([v4_host()], lambda _peer, _budget: ok_outcome(rtts=(10.0,)))
    assert not too_few_rtt.ok

    passed = run_connectivity_checks([v4_host()], lambda _peer, _budget: ok_outcome())
    assert passed.ok
    assert passed.detail["rtt_p50_ms"] == 11.0


def test_connectivity_empty_candidates() -> None:
    result = run_connectivity_checks([], lambda _peer, _budget: ok_outcome())
    assert not result.ok
    assert result.reason == REASON_NO_CANDIDATE


# -------------------------------- 决策 --------------------------------


def test_decide_prefers_ipv6() -> None:
    ipv6 = ipv6_self_check(lambda _deadline: Ipv6Probe(("240e::1",), ("2400:3200::1",), rtt_ms=20.0))
    decision = decide(ipv6, run_connectivity_checks([v6_host()], lambda _p, _b: ok_outcome()), MAPPING_EIM)
    assert decision.transport == TRANSPORT_DIRECT_IPV6


def test_decide_symmetric_never_punches() -> None:
    ipv6 = ipv6_self_check(lambda _deadline: Ipv6Probe())
    decision = decide(ipv6, run_connectivity_checks([v4_host()], lambda _p, _b: ok_outcome()), MAPPING_SYMMETRIC)
    assert decision.transport == TRANSPORT_RELAY
    assert decision.reason == REASON_SYMMETRIC_NAT


# -------------------------------- 建连编排 --------------------------------


def test_only_ipv6_available_takes_l1() -> None:
    calls: list[int] = []

    def checker(peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        calls.append(budget_ms)
        return ok_outcome()

    def tcp_prober(peer: Sequence[Candidate], budget_ms: int) -> TcpProbe:
        assert budget_ms == IPV6_TCP_BUDGET_MS
        target = peer[0]
        return TcpProbe(
            ok=True,
            peer_address=target.address,
            peer_family=target.family,
            peer_port=target.port,
            local_port=53422,
            rtt_ms=(25.0,),
            tried=1,
            reason=REASON_OK,
        )

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(("240e::1",), ("2400:3200::1",), rtt_ms=25.0),
        collector=lambda _deadline: [v6_host(), v4_host()],
        checker=checker,
        relay_requester=relay_ok,
        tcp_prober=tcp_prober,
    )
    assert result.decision.transport == TRANSPORT_DIRECT_IPV6
    assert calls == []
    assert result.relay is None
    assert result.direct is not None
    assert result.direct.detail["peer_address"] == v6_host().address
    assert path_snapshot(result)["relay_used"] is False


def test_ipv6_unavailable_falls_to_punch() -> None:
    budgets: list[int] = []

    def checker(peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        budgets.append(budget_ms)
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host(), srflx()],
        checker=checker,
        relay_requester=relay_ok,
    )
    assert result.decision.transport == TRANSPORT_DIRECT_UDP
    assert budgets == [DIRECT_BUDGET_MS]
    assert result.ipv6.reason == REASON_IPV6_NO_GLOBAL


def test_l2_after_ipv6_failure_uses_only_v4() -> None:
    seen: list[list[int]] = []

    def checker(peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        seen.append([item.family for item in peer])
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(("240e::1",), ("2400:3200::1",)),
        collector=lambda _deadline: [v6_host(), v4_host(), srflx()],
        checker=checker,
        relay_requester=relay_ok,
        tcp_prober=lambda _peer, _budget: TcpProbe(ok=False, tried=2, reason=REASON_IPV6_UNREACHABLE),
    )
    # L1 用 TCP 探测 v6 host 失败后, L2 只重试 v4 host 与 srflx, 不重复 v6
    assert seen == [[4, 4]]
    assert direct_level(result).reason == REASON_IPV6_UNREACHABLE
    assert result.decision.transport == TRANSPORT_DIRECT_UDP


def test_punch_timeout_falls_back_to_relay() -> None:
    requested: list[int] = []

    def relay() -> dict[str, object]:
        requested.append(1)
        return relay_ok()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host()],
        checker=lambda _p, _b: CheckOutcome(sent=50, responses_forward=0, responses_backward=0),
        relay_requester=relay,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.decision.reason == REASON_PUNCH_TIMEOUT
    assert result.relay == relay_ok()
    assert requested == [1]
    assert result.relay_prefetched is False


def test_symmetric_nat_skips_punch() -> None:
    called: list[int] = []

    def checker(peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        called.append(budget_ms)
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host(), srflx(49160), srflx(52190)],
        checker=checker,
        relay_requester=relay_ok,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.decision.reason == REASON_SYMMETRIC_NAT
    assert called == []


def test_empty_candidates_go_relay() -> None:
    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [],
        checker=lambda _p, _b: ok_outcome(),
        relay_requester=relay_ok,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.decision.reason == REASON_NO_CANDIDATE


def test_stun_timeout_goes_relay() -> None:
    def collector(_deadline: int) -> Sequence[Candidate]:
        raise TimeoutError("stun no response")

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(("240e::1",), ("2400:3200::1",)),
        collector=collector,
        checker=lambda _p, _b: ok_outcome(),
        relay_requester=relay_ok,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.decision.reason == REASON_NO_CANDIDATE


def test_relay_unavailable_is_reported() -> None:
    def relay() -> dict[str, object]:
        raise OSError("relay closed")

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host()],
        checker=lambda _p, _b: CheckOutcome(),
        relay_requester=relay,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.decision.reason == REASON_RELAY_UNAVAILABLE


def test_relay_prefetch_flag_after_budget() -> None:
    clock = FakeClock()

    def checker(peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        clock.advance(RELAY_PREFETCH_AT_MS)
        return CheckOutcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host()],
        checker=checker,
        relay_requester=relay_ok,
        clock=clock,
    )
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.p2p.duration_ms >= RELAY_PREFETCH_AT_MS
    assert result.relay_prefetched is True


def test_collector_and_checks_receive_documented_deadlines() -> None:
    seen: list[int] = []

    def collector(deadline_ms: int) -> Sequence[Candidate]:
        seen.append(deadline_ms)
        return [v4_host()]

    establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=collector,
        checker=lambda _p, _b: ok_outcome(),
        relay_requester=relay_ok,
    )
    assert seen == [STUN_TOTAL_MS]
    assert CANDIDATE_BUDGET_MS == 2000


# -------------------------------- 运行期降级 --------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        QualitySample(loss_pct=DEGRADE_LOSS_PCT + 1.0),
        QualitySample(rtt_p95_ms=DEGRADE_P95_MS + 1.0),
        QualitySample(no_frame_s=DEGRADE_NO_FRAME_S),
        QualitySample(keyframe_retries=KEYFRAME_RETRY_LIMIT),
    ],
)
def test_watch_quality_triggers_on_each_threshold(sample: QualitySample) -> None:
    assert watch_quality(sample) == TRANSPORT_RELAY


@pytest.mark.parametrize(
    "sample",
    [
        QualitySample(loss_pct=DEGRADE_LOSS_PCT),
        QualitySample(rtt_p95_ms=DEGRADE_P95_MS),
        QualitySample(no_frame_s=DEGRADE_NO_FRAME_S - 0.5),
        QualitySample(keyframe_retries=KEYFRAME_RETRY_LIMIT - 1),
    ],
)
def test_watch_quality_stays_below_thresholds(sample: QualitySample) -> None:
    assert watch_quality(sample) is None


def test_monitor_degrades_once_and_never_rolls_back() -> None:
    monitor = TransportMonitor(TRANSPORT_DIRECT_UDP)
    assert monitor.observe(QualitySample(loss_pct=5.0)) == TRANSPORT_DIRECT_UDP
    assert monitor.observe(QualitySample(loss_pct=DEGRADE_LOSS_PCT + 5.0)) == TRANSPORT_RELAY
    assert monitor.transport == TRANSPORT_RELAY
    assert monitor.observe(QualitySample(loss_pct=0.0, rtt_p95_ms=10.0)) == TRANSPORT_RELAY


def test_monitor_rejects_unknown_transport() -> None:
    with pytest.raises(ValueError):
        TransportMonitor("carrier-pigeon")


# -------------------------------- 候选 schema 与上线序列化 --------------------------------


def test_priority_table_matches_spec_section_3_2() -> None:
    table = {
        "ipv6_global": PRIORITY_IPV6_GLOBAL,
        "host_v4_private": PRIORITY_HOST_V4_PRIVATE,
        "mapped_external": MAPPED_PRIORITY_EXTERNAL,
        "mapped_readback": MAPPED_PRIORITY_READBACK,
        "srflx": PRIORITY_SRFLX,
        "host_v4_public": PRIORITY_HOST_V4_PUBLIC,
        "ipv6_ula": PRIORITY_IPV6_ULA,
        "relay": PRIORITY_RELAY,
    }
    assert table == {
        "ipv6_global": 100,
        "host_v4_private": 96,
        "mapped_external": 95,
        "mapped_readback": 92,
        "srflx": 90,
        "host_v4_public": 85,
        "ipv6_ula": 70,
        "relay": 10,
    }


def test_family_and_scope_are_inferred_from_address() -> None:
    assert infer_family("240e:1:2:3::10") == 6
    assert infer_family("192.168.1.80") == 4
    assert infer_family("relay.example") == 0
    assert scope_of("240e:1:2:3::10") == "global"
    assert scope_of("fd00::10") == "ula"
    assert scope_of("fe80::10") == "link"
    assert scope_of("192.168.1.80") == "private"
    assert scope_of(PUBLIC_V4) == ""


def test_candidate_payload_carries_all_new_fields() -> None:
    v6_payload = candidate_to_payload(v6_host(), "S1")
    assert v6_payload["family"] == 6
    assert v6_payload["scope"] == "global"
    assert v6_payload["origin"] == "host"
    assert v6_payload["proto"] == "tcp"
    assert v6_payload["related"] == ""
    srflx_payload = candidate_to_payload(srflx(), "S1")
    assert srflx_payload["origin"] == "stun"
    assert srflx_payload["related"] == "192.168.1.80"
    assert srflx_payload["proto"] == "udp"
    relay_payload = candidate_to_payload(
        Candidate(kind="relay", family=4, address="47.109.32.249", port=8750, priority=10), "S1"
    )
    assert relay_payload["origin"] == "relay"
    assert relay_payload["proto"] == "tcp"
    assert relay_payload["priority"] == PRIORITY_RELAY


def test_legacy_payload_without_new_fields_is_accepted() -> None:
    legacy = {"kind": "host", "address": "240e:1:2:3::10", "port": 27888, "priority": 100, "future_key": 1}
    item = payload_to_candidate(legacy)
    assert item is not None
    assert item.family == 6
    assert item.scope == "global"
    assert item.proto == "tcp"
    hostname = {"kind": "relay", "address": "relay.example", "port": 8750, "priority": 10}
    relay_item = payload_to_candidate(hostname)
    assert relay_item is not None
    assert relay_item.family == 0
    assert relay_item.kind == "relay"


def test_mapped_state_is_written_as_srflx_with_origin_mapped() -> None:
    mapped = Candidate(
        kind=KIND_MAPPED,
        family=4,
        address=PUBLIC_V4,
        port=50000,
        priority=MAPPED_PRIORITY_READBACK,
        related="192.168.1.80",
    )
    payload = candidate_to_payload(mapped, "S1")
    assert payload["kind"] == "srflx"
    assert payload["origin"] == "mapped"
    inbound = payload_to_candidate({"kind": "mapped", "address": PUBLIC_V4, "port": 50000, "priority": 92})
    assert inbound is not None
    assert inbound.kind == "srflx"
    assert inbound.origin == "mapped"


def test_link_local_v6_candidate_is_not_published() -> None:
    payload = {"kind": "host", "address": "fe80::10", "port": 27888, "priority": 100, "family": 6}
    assert payload_to_candidate(payload) is None
    assert not is_publishable(Candidate(kind="host", family=6, address="fe80::10", port=27888, scope="link"))


def test_duplicate_candidate_keeps_highest_priority() -> None:
    low = Candidate(kind="host", family=4, address="10.0.0.9", port=49160, priority=50)
    high = Candidate(kind="host", family=4, address="10.0.0.9", port=49160, priority=96)
    items = collect_candidates(lambda _deadline: [low, high])
    assert len(items) == 1
    assert items[0].priority == 96


def test_sort_candidates_orders_by_priority_then_endpoint() -> None:
    relay = Candidate(kind="relay", family=4, address="192.168.1.80", port=27888, priority=PRIORITY_RELAY)
    ula = Candidate(kind="host", family=6, address="fd00::1", port=27888, priority=PRIORITY_IPV6_ULA)
    global_v6 = v6_host()
    ordered = sort_candidates([relay, ula, global_v6])
    assert [item.kind for item in ordered] == ["host", "host", "relay"]
    assert [item.priority for item in ordered] == [PRIORITY_IPV6_GLOBAL, PRIORITY_IPV6_ULA, PRIORITY_RELAY]


def test_link_local_filter_handles_v4_and_v6() -> None:
    assert is_link_local(Candidate(kind="host", family=6, address="fe80::10", port=27888))
    assert is_link_local(Candidate(kind="host", family=4, address="169.254.1.2", port=27888))
    assert not is_link_local(Candidate(kind="host", family=4, address="192.168.1.80", port=27888))


def test_filter_candidates_drops_link_local_and_hairpin() -> None:
    local = [
        Candidate(kind="host", family=6, address="240e:1:2:3::10", port=27888, priority=100),
        Candidate(kind="host", family=6, address="fe80::10", port=27888, priority=100),
        Candidate(kind="host", family=4, address="192.168.1.80", port=27888, priority=96),
        srflx(),
        Candidate(kind="host", family=4, address="169.254.1.2", port=27888, priority=96),
        Candidate(kind="relay", family=4, address="192.168.1.80", port=27888, priority=10),
    ]
    kept, hairpin = filter_candidates(local)
    assert hairpin == 0
    assert [item.address for item in kept] == [
        "240e:1:2:3::10",
        "192.168.1.80",
        "115.150.84.112",
        "192.168.1.80",
    ]
    assert all(item.kind != "host" or item.address != "fe80::10" for item in kept)


def test_filter_candidates_removes_same_egress_hairpin() -> None:
    local = [
        Candidate(kind="host", family=4, address="192.168.1.80", port=27888, priority=96),
        srflx(),
        Candidate(kind="relay", family=4, address=PUBLIC_V4, port=27888, priority=10),
    ]
    peer = [Candidate(kind="srflx", family=4, address=PUBLIC_V4, port=50000, priority=90)]
    kept, hairpin = filter_candidates(local, peer)
    assert hairpin == 2
    assert [(item.kind, item.address) for item in kept] == [("host", "192.168.1.80")]


def test_filter_candidates_dedupes_and_keeps_high_priority() -> None:
    low = Candidate(kind="host", family=4, address="10.0.0.9", port=49160, priority=50)
    high = Candidate(kind="host", family=4, address="10.0.0.9", port=49160, priority=96)
    kept, hairpin = filter_candidates([low, high])
    assert hairpin == 0
    assert len(kept) == 1
    assert kept[0].priority == 96
