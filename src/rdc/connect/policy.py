# ==========================================================================================
# 文件        : policy.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 连接策略 IPv6 直连 -> P2P 打洞 -> 中继兜底 的判定与编排, 对应 docs/design/connect-v1.md
# 架构定位    : 纯判定层, 不持有 socket、不依赖界面与平台 API; 网络能力全部由注入的可调用对象提供
# 核心功能    : 18 项共享参数; IPv6 自检; 候选收集与映射分类; 双向连通判据; 建连编排与中继预取;
#               运行期劣化判定与单向降级(不回切)
# 职责边界    : 只输出判定结果与事件字段; 不实现 UDP/STUN/ICE, 不写帧封装, 不修改契约;
#               各端原生实现(v6 枚举、socket 复用、防火墙)由调用方通过注入提供
# 运行环境    : Python 3.12
# 依赖关系    : 被会话编排层调用; 与 src/rdc/media/quality.py 的档位表只读协作
# ==========================================================================================

"""连接策略: IPv6 直连 -> P2P 打洞 -> 中继兜底.

策略依据: docs/design/connect-v1.md. 本模块只做判定与编排, 真实网络动作由注入实现:

- ``prober``: 探本机 IPv6 是否可用, 返回 :class:`Ipv6Probe`
- ``collector``: 收集 v6 host / v4 host / STUN srflx 候选, 返回 :class:`Candidate` 列表
- ``checker``: 对候选做双向连通检查, 返回 :class:`CheckOutcome`
- ``relay_requester``: 申请中继预算, 返回 ``relay_allocated`` 原文
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rdc.connect.udp_plane import UdpSessionPlane

# -------------------------------- 18 项共享参数 --------------------------------

IPV6_PROBE_TIMEOUT_MS = 800
IPV6_PROBE_TARGETS: tuple[str, ...] = ("2400:3200::1", "2400:3200:baba::1")
IPV6_BUDGET_MS = 1500
# L1 直连探测: 每候选 800 ms/次, 最多 2 次, 层总预算 1500 ms (spec ipv6-punch §4.1)
IPV6_TCP_TIMEOUT_MS = 800
IPV6_TCP_ATTEMPTS = 2
IPV6_TCP_BUDGET_MS = IPV6_BUDGET_MS
STUN_TIMEOUT_MS = 800
STUN_TOTAL_MS = 1500
CHECK_INTERVAL_MS = 100
CANDIDATE_BUDGET_MS = 2000
DIRECT_BUDGET_MS = 5000
SUCCESS_MIN_RESPONSES = 2
SUCCESS_MIN_RTT_SAMPLES = 3
RELAY_PREFETCH_AT_MS = 2000
DEGRADE_LOSS_PCT = 20.0
DEGRADE_LOSS_WINDOW_S = 3.0
DEGRADE_P95_MS = 300.0
DEGRADE_P95_WINDOW_S = 5.0
DEGRADE_NO_FRAME_S = 2.0
KEYFRAME_RETRY_LIMIT = 3
RECONNECT_RETRY = 1

# -------------------------------- 常量枚举 --------------------------------

LEVEL_IPV6 = "ipv6"
LEVEL_P2P = "p2p"
LEVEL_RELAY = "relay"

TRANSPORT_DIRECT_IPV6 = "direct_ipv6"
TRANSPORT_DIRECT_UDP = "direct_udp"
TRANSPORT_RELAY = "relay"

REASON_OK = "ok"
REASON_IPV6_NO_GLOBAL = "ipv6_no_global"
REASON_IPV6_UNREACHABLE = "ipv6_unreachable"
REASON_IPV6_DISABLED = "ipv6_disabled"
REASON_SYMMETRIC_NAT = "symmetric_nat"
REASON_PUNCH_TIMEOUT = "punch_timeout"
REASON_SINGLE_DIRECTION = "single_direction"
REASON_NO_CANDIDATE = "no_candidate"
REASON_RELAY_UNAVAILABLE = "relay_unavailable"

MAPPING_EIM = "endpoint-independent"
MAPPING_SYMMETRIC = "address-or-port-dependent"
MAPPING_UNKNOWN = "unknown"

KIND_HOST = "host"
KIND_SRFLX = "srflx"
KIND_RELAY = "relay"
# 本地态, 不直接上线: 序列化时改写为 kind=srflx + origin=mapped (spec ipv6-punch §3.4.5)
KIND_MAPPED = "mapped"
FAMILY_UNKNOWN = 0
FAMILY_V4 = 4
FAMILY_V6 = 6

SCOPE_GLOBAL = "global"
SCOPE_ULA = "ula"
SCOPE_LINK = "link"
SCOPE_PRIVATE = "private"

ORIGIN_HOST = "host"
ORIGIN_STUN = "stun"
ORIGIN_MAPPED = "mapped"
ORIGIN_RELAY = "relay"

PROTO_TCP = "tcp"
PROTO_UDP = "udp"

MAX_PRIORITY = 100
DEFAULT_PRIORITY = 50
# 优先级表见 spec ipv6-punch §3.2, 8 项与表中数值一一对应
PRIORITY_IPV6_GLOBAL = 100
PRIORITY_HOST_V4_PRIVATE = 96
# 端口映射候选: 读回校验通过 92, 外部探针确认 95; 均高于 srflx(90), 低于 v6 host(100)
MAPPED_PRIORITY_READBACK = 92
MAPPED_PRIORITY_EXTERNAL = 95
PRIORITY_SRFLX = 90
PRIORITY_HOST_V4_PUBLIC = 85
PRIORITY_IPV6_ULA = 70
PRIORITY_RELAY = 10

_ULA_NETWORK = ipaddress.ip_network("fc00::/7")


@dataclass(frozen=True)
class Candidate:
    """一个连通候选, 字段与 spec ipv6-punch §3.1 一致.

    ``family/scope/origin/related/proto`` 为只增的可选字段; ``kind`` 枚举不新增,
    本地态 ``mapped`` 只在内部流转, 上线前由 :func:`normalize_candidate` 改写.
    """

    kind: str
    family: int
    address: str
    port: int
    priority: int = DEFAULT_PRIORITY
    related: str = ""
    scope: str = ""
    origin: str = ""
    proto: str = ""


@dataclass(frozen=True)
class Ipv6Probe:
    """本机 IPv6 自检结果, 由注入的 prober 返回."""

    global_addresses: tuple[str, ...] = ()
    reachable_targets: tuple[str, ...] = ()
    rtt_ms: float | None = None
    error: str = ""

    @property
    def available(self) -> bool:
        """有全局地址且至少一个目标可达才算可用."""
        return bool(self.global_addresses) and bool(self.reachable_targets)


@dataclass(frozen=True)
class CheckOutcome:
    """一次双向连通检查的统计, 由注入的 checker 返回."""

    sent: int = 0
    responses_forward: int = 0
    responses_backward: int = 0
    rtt_samples_ms: tuple[float, ...] = ()
    first_packet_ms: float | None = None
    loss_pct: float = 0.0


@dataclass(frozen=True)
class TcpProbe:
    """L1 TCP 直连探测结果; 由注入的 tcp_prober 返回.

    ``ok=True`` 表示三次握手成功一次即通过, 不套用打洞的「≥2 响应、≥3 样本」判据.
    """

    ok: bool = False
    peer_address: str = ""
    peer_family: int = 0
    peer_port: int = 0
    local_port: int = 0
    rtt_ms: tuple[float, ...] = ()
    tried: int = 0
    reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class LevelResult:
    """单级结果, 记录耗时、原因与指标."""

    level: str
    ok: bool
    duration_ms: float
    reason: str
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QualitySample:
    """运行期采样, 窗口聚合由调用方完成, 本模块只做阈值判定."""

    loss_pct: float = 0.0
    rtt_p95_ms: float = 0.0
    no_frame_s: float = 0.0
    keyframe_retries: int = 0


@dataclass(frozen=True)
class Decision:
    """策略给出的传输选择."""

    transport: str
    reason: str


@dataclass(frozen=True)
class EstablishResult:
    """建连编排的完整结果, 便于一次落齐事件与日志."""

    decision: Decision
    ipv6: LevelResult
    p2p: LevelResult
    relay: Mapping[str, Any] | None = None
    relay_prefetched: bool = False
    # L1 直连层的独立结果(含 peer_address/local_port/rtt), 未尝试或跳过时为 None
    direct: LevelResult | None = None
    # L2 命中后接管打洞 socket 的 UDP 数据面; 非打洞路径为 None
    udp_plane: UdpSessionPlane | None = None
    # L2 命中的对端端点, 供 connect.path_selected 记录 peer_address/peer_port
    peer: tuple[str, int] | None = None


Prober = Callable[[int], Ipv6Probe]
Collector = Callable[[int], Sequence[Candidate]]
Checker = Callable[[Sequence[Candidate], int], CheckOutcome]
TcpProber = Callable[[Sequence[Candidate], int], TcpProbe]
RelayRequester = Callable[[], Mapping[str, Any]]


def _elapsed_ms(started: float, clock: Callable[[], float]) -> float:
    return round((clock() - started) * 1000.0, 3)


def _failed(level: str, reason: str, duration_ms: float, **detail: Any) -> LevelResult:
    return LevelResult(level=level, ok=False, duration_ms=duration_ms, reason=reason, detail=detail)


def _with_mapping(result: LevelResult, mapping: str, mapped_count: int = 0) -> LevelResult:
    """把候选映射分类与映射候选数补进 detail, 契约要求 p2p 级必带 mapping."""
    return LevelResult(
        level=result.level,
        ok=result.ok,
        duration_ms=result.duration_ms,
        reason=result.reason,
        detail={**result.detail, "mapping": mapping, "mapped_candidates": mapped_count},
    )


# -------------------------------- IPv6 自检 --------------------------------


def ipv6_self_check(
    prober: Prober,
    deadline_ms: int = IPV6_BUDGET_MS,
    clock: Callable[[], float] = time.monotonic,
) -> LevelResult:
    """判定本端 IPv6 是否可用于直连.

    只有全局地址不够, 还必须至少一个对外目标可达; 两者缺一即降级到下一层.
    """
    started = clock()
    try:
        probe = prober(deadline_ms)
    except (OSError, TimeoutError) as exc:
        return _failed(
            LEVEL_IPV6,
            REASON_IPV6_UNREACHABLE,
            _elapsed_ms(started, clock),
            error=f"{type(exc).__name__}: {exc}",
        )
    duration = _elapsed_ms(started, clock)
    if not probe.global_addresses:
        return _failed(LEVEL_IPV6, REASON_IPV6_NO_GLOBAL, duration, probe_targets=len(IPV6_PROBE_TARGETS))
    if not probe.reachable_targets:
        return _failed(LEVEL_IPV6, REASON_IPV6_UNREACHABLE, duration, error=probe.error)
    return LevelResult(
        level=LEVEL_IPV6,
        ok=True,
        duration_ms=duration,
        reason=REASON_OK,
        detail={
            "local_v6": list(probe.global_addresses),
            "probe_targets": list(IPV6_PROBE_TARGETS),
            "probe_ok": list(probe.reachable_targets),
            "rtt_p50_ms": probe.rtt_ms,
        },
    )


# -------------------------------- 候选 schema --------------------------------


def infer_family(address: str) -> int:
    """按字面量推断地址族; 主机名等非 IP 字面量返回 0, 只允许挂在 relay 候选上."""
    text = address.strip().strip("[]")
    try:
        return ipaddress.ip_address(text).version
    except ValueError:
        return FAMILY_UNKNOWN


def scope_of(address: str) -> str:
    """按地址推断作用域; 仅 IPv6 有语义, IPv4 只区分私网."""
    try:
        ip = ipaddress.ip_address(address.strip().strip("[]"))
    except ValueError:
        return ""
    if ip.version == 6:
        if ip.is_link_local:
            return SCOPE_LINK
        if ip in _ULA_NETWORK:
            return SCOPE_ULA
        return SCOPE_GLOBAL if ip.is_global else ""
    return SCOPE_PRIVATE if ip.is_private else ""


def _origin_of(kind: str) -> str:
    return {KIND_HOST: ORIGIN_HOST, KIND_SRFLX: ORIGIN_STUN, KIND_RELAY: ORIGIN_RELAY}.get(kind, "")


def _proto_of(kind: str, family: int) -> str:
    if kind == KIND_RELAY:
        return PROTO_TCP
    if kind in {KIND_SRFLX, KIND_MAPPED}:
        return PROTO_UDP
    if kind == KIND_HOST and family == FAMILY_V6:
        return PROTO_TCP
    if kind == KIND_HOST:
        return PROTO_UDP
    return ""


def normalize_candidate(item: Candidate) -> Candidate:
    """补齐 ``family/scope/origin/related/proto`` 并把本地态 mapped 改写为上线形态.

    改写规则见 spec ipv6-punch §3.4.5: 上线时 ``kind="mapped"`` 一律变成
    ``kind="srflx"`` + ``origin="mapped"``, ``kind`` 枚举本身不新增取值. 未知 kind 原样
    保留, 不抛异常.
    """
    mapped_state = item.kind == KIND_MAPPED
    kind = KIND_SRFLX if mapped_state else item.kind
    # family=0 只表示非字面量(主机名), 这类候选只能走中继, 不做强制改写
    family = item.family if item.family in {FAMILY_V4, FAMILY_V6} else infer_family(item.address)
    origin = item.origin or (ORIGIN_MAPPED if mapped_state else _origin_of(kind))
    return replace(
        item,
        kind=kind,
        family=family,
        priority=PRIORITY_RELAY if kind == KIND_RELAY else item.priority,
        origin=origin,
        scope=item.scope or scope_of(item.address),
        proto=item.proto or _proto_of(kind, family),
    )


def is_publishable(item: Candidate) -> bool:
    """能否写进 offer/answer: 无端口/无地址、IPv6 link-local 与未知 kind 一律不上线."""
    if item.port <= 0 or not item.address:
        return False
    normalized = normalize_candidate(item)
    if normalized.kind == KIND_HOST and normalized.family == FAMILY_V6:
        return normalized.scope in {SCOPE_GLOBAL, SCOPE_ULA}
    if normalized.family == FAMILY_UNKNOWN:
        return normalized.kind == KIND_RELAY
    return normalized.kind in {KIND_HOST, KIND_SRFLX, KIND_RELAY}


def dedupe_candidates(items: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """同一 ``(kind, address, port)`` 只保留优先级最高的一条; 输入沿用既有排序顺序."""
    kept: dict[tuple[str, str, int], Candidate] = {}
    for item in items:
        key = (item.kind, item.address, item.port)
        current = kept.get(key)
        if current is None or item.priority > current.priority:
            kept[key] = item
    return tuple(kept.values())


def sort_candidates(items: Sequence[Candidate]) -> tuple[Candidate, ...]:
    """按 spec §3.2 的优先级键排序; 只排序, 不改写候选内容."""
    return tuple(sorted(items, key=_candidate_sort_key))


def is_link_local(item: Candidate) -> bool:
    """是否链路本地候选; 这类地址不能跨网段使用, 既不下发也不用于打洞."""
    normalized = normalize_candidate(item)
    if normalized.scope == SCOPE_LINK:
        return True
    try:
        address = ipaddress.ip_address(normalized.address.strip().strip("[]"))
    except ValueError:
        return False
    return address.is_link_local


def filter_candidates(
    items: Sequence[Candidate],
    egress: Sequence[Candidate] = (),
) -> tuple[tuple[Candidate, ...], int]:
    """剔除不可上线、链路本地与 hairpin 候选.

    ``egress`` 是另一端的候选: 取其 srflx/mapped 地址作为出口集合, 本端同地址的
    ``host``/``srflx``/``relay`` 都视为 hairpin (依赖路由器回环, 现网不支持).
    口径与安卓 ``removeHairpin`` 对齐. 返回 ``(保留候选, 剔除的 hairpin 数)``,
    保留项按优先级排序并去重.
    """
    public = _public_addresses(egress)
    kept: list[Candidate] = []
    hairpin = 0
    for item in items:
        normalized = normalize_candidate(item)
        if not is_publishable(normalized) or is_link_local(normalized):
            continue
        if normalized.kind in {KIND_HOST, KIND_SRFLX, KIND_RELAY} and normalized.address in public:
            hairpin += 1
            continue
        kept.append(normalized)
    return dedupe_candidates(sort_candidates(kept)), hairpin


# -------------------------------- 候选收集 --------------------------------


def _candidate_sort_key(item: Candidate) -> tuple[int, int, str, int]:
    return (-item.priority, item.family, item.address, item.port)


def collect_candidates(
    collector: Collector,
    deadline_ms: int = STUN_TOTAL_MS,
) -> tuple[Candidate, ...]:
    """收集候选并按优先级排序; 超时或异常按「无候选」处理, 不抛出."""
    try:
        items = collector(deadline_ms)
    except (OSError, TimeoutError):
        return ()
    valid = [
        item for item in items if item.port > 0 and item.address and item.kind in {KIND_HOST, KIND_SRFLX, KIND_MAPPED}
    ]
    return dedupe_candidates(sorted(valid, key=_candidate_sort_key))


def classify_mapping(candidates: Sequence[Candidate]) -> str:
    """按 srflx 映射是否唯一判定 NAT 映射行为."""
    mapped = {(item.address, item.port) for item in candidates if item.kind == KIND_SRFLX}
    if not mapped:
        return MAPPING_UNKNOWN
    return MAPPING_EIM if len(mapped) == 1 else MAPPING_SYMMETRIC


def has_v6_host(candidates: Sequence[Candidate]) -> bool:
    return any(item.kind == KIND_HOST and item.family == FAMILY_V6 for item in candidates)


def _public_addresses(items: Sequence[Candidate]) -> set[str]:
    """取出公网候选地址; srflx 与 mapped 都是公网地址, 一起参与比较."""
    return {item.address for item in items if item.kind in {KIND_SRFLX, KIND_MAPPED}}


def same_egress(candidates: Sequence[Candidate], peer: Sequence[Candidate] | None = None) -> bool:
    """判断两端是否共用同一个公网出口.

    判定依据只有一个: 本端与对端各自的公网候选地址集合存在交集. 公网候选指 srflx 与
    mapped 两种, 它们的 address 都是路由器 WAN 侧地址. 同出口意味着公网回连要经过路由器
    hairpin, 而实测小米 R3D 不支持 hairpin, 此时必须剔除公网候选, 否则会把「路由不支持回环」
    误判成打洞失败.

    适用边界:

    - 只有 ``peer`` 显式给出时才按「两端各有候选且地址命中」判定, 这是精确模式;
    - ``peer`` 为 None 时退化为合并列表的保守模式, 要求至少两个公网候选且地址唯一,
      因为无法区分候选属于哪一端, 不能凭单个候选断言同出口;
    - 双出口(本端 srflx 与 mapped 落在两个 WAN 地址)时地址集合不唯一, 判定为否,
      不会误剔;
    - CGNAT 下多个用户共享同一个运营商公网地址: 这类地址本身不可被外部直连(映射会先被
      ``reconcile_with_stun`` 降级), 即使命中同一地址也不会因为剔除而损失可达路径.
    """
    if peer is not None:
        return bool(_public_addresses(candidates) & _public_addresses(peer))
    public = [item for item in candidates if item.kind in {KIND_SRFLX, KIND_MAPPED}]
    return len(public) >= 2 and len({item.address for item in public}) == 1


# -------------------------------- 连通检查 --------------------------------


def run_connectivity_checks(
    peer: Sequence[Candidate],
    checker: Checker,
    budget_ms: int = DIRECT_BUDGET_MS,
    clock: Callable[[], float] = time.monotonic,
) -> LevelResult:
    """执行一次双向连通检查并套用成功判据.

    成功必须有来有回: 双向各至少 ``SUCCESS_MIN_RESPONSES`` 个响应, 且 RTT 采样不少于
    ``SUCCESS_MIN_RTT_SAMPLES`` 个. 单向收到一律判失败.
    """
    started = clock()
    if not peer:
        return _failed(LEVEL_P2P, REASON_NO_CANDIDATE, _elapsed_ms(started, clock))
    try:
        outcome = checker(peer, budget_ms)
    except (OSError, TimeoutError) as exc:
        return _failed(
            LEVEL_P2P,
            REASON_PUNCH_TIMEOUT,
            _elapsed_ms(started, clock),
            error=f"{type(exc).__name__}: {exc}",
        )
    duration = _elapsed_ms(started, clock)
    detail: dict[str, Any] = {
        "candidates": [f"{item.address}:{item.port}" for item in peer],
        "sent_packets": outcome.sent,
        "recv_packets": outcome.responses_forward + outcome.responses_backward,
        "recv_forward": outcome.responses_forward,
        "recv_backward": outcome.responses_backward,
        "rtt_samples": len(outcome.rtt_samples_ms),
        "first_packet_ms": outcome.first_packet_ms,
        "loss_pct": outcome.loss_pct,
    }
    forward_ok = outcome.responses_forward >= SUCCESS_MIN_RESPONSES
    backward_ok = outcome.responses_backward >= SUCCESS_MIN_RESPONSES
    if not (forward_ok and backward_ok):
        if forward_ok or backward_ok:
            return _failed(LEVEL_P2P, REASON_SINGLE_DIRECTION, duration, **detail)
        return _failed(LEVEL_P2P, REASON_PUNCH_TIMEOUT, duration, **detail)
    if len(outcome.rtt_samples_ms) < SUCCESS_MIN_RTT_SAMPLES:
        return _failed(LEVEL_P2P, REASON_SINGLE_DIRECTION, duration, **detail)
    ordered = sorted(outcome.rtt_samples_ms)
    detail["rtt_p50_ms"] = round(ordered[len(ordered) // 2], 3)
    detail["rtt_p95_ms"] = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3)
    return LevelResult(level=LEVEL_P2P, ok=True, duration_ms=duration, reason=REASON_OK, detail=detail)


def run_tcp_direct_check(
    peer: Sequence[Candidate],
    tcp_prober: TcpProber,
    budget_ms: int = IPV6_TCP_BUDGET_MS,
    clock: Callable[[], float] = time.monotonic,
) -> LevelResult:
    """L1 IPv6 直连: TCP 三次握手成功一次即通过, 独立于打洞阈值.

    只用 ``family=6`` 候选; 每候选 800 ms、最多 2 次、层总预算 1500 ms 由注入的 prober
    执行, 本函数只做判据与耗时记账. 本机无 v6 地址时上层直接跳过, 不进这里.
    """
    started = clock()
    candidates = [item for item in peer if item.family == FAMILY_V6]
    if not candidates:
        return _failed(LEVEL_IPV6, REASON_IPV6_UNREACHABLE, _elapsed_ms(started, clock), candidates=0, tried=0)
    try:
        probe = tcp_prober(candidates, budget_ms)
    except (OSError, TimeoutError) as exc:
        return _failed(
            LEVEL_IPV6,
            REASON_IPV6_UNREACHABLE,
            _elapsed_ms(started, clock),
            candidates=len(candidates),
            error=f"{type(exc).__name__}: {exc}",
        )
    duration = _elapsed_ms(started, clock)
    detail: dict[str, Any] = {
        "candidates": len(candidates),
        "tried": probe.tried,
        "rtt_ms": list(probe.rtt_ms),
        "peer_address": probe.peer_address,
        "peer_family": probe.peer_family,
        "peer_port": probe.peer_port,
        "local_port": probe.local_port,
    }
    if not probe.ok or not probe.peer_address:
        return _failed(LEVEL_IPV6, REASON_IPV6_UNREACHABLE, duration, error=probe.error, **detail)
    return LevelResult(level=LEVEL_IPV6, ok=True, duration_ms=duration, reason=REASON_OK, detail=detail)


def direct_level(result: EstablishResult) -> LevelResult:
    """取 L1 层的可断言结果; 未尝试时按自检原因合成一条零耗时记录."""
    if result.direct is not None:
        return result.direct
    reason = result.ipv6.reason if not result.ipv6.ok else REASON_IPV6_UNREACHABLE
    return LevelResult(LEVEL_IPV6, False, 0.0, reason, {"candidates": 0, "tried": 0})


# -------------------------------- 决策 --------------------------------


def decide(ipv6: LevelResult, p2p: LevelResult, mapping: str, mapped: bool = False) -> Decision:
    """按优先级给出传输选择; 语义与 connect-v1.md 第二节一致.

    ``mapped=True`` 表示本端已拿到经读回校验的公网入站端口: 对称型 NAT 只影响打洞
    成功率, 静态映射仍然给出可达入站端口, 因此不再直接判死, 仍以实测结果为最终依据.
    """
    if ipv6.ok:
        return Decision(transport=TRANSPORT_DIRECT_IPV6, reason=REASON_OK)
    if mapping == MAPPING_SYMMETRIC and not mapped:
        return Decision(transport=TRANSPORT_RELAY, reason=REASON_SYMMETRIC_NAT)
    if p2p.ok:
        return Decision(transport=TRANSPORT_DIRECT_UDP, reason=REASON_OK)
    return Decision(transport=TRANSPORT_RELAY, reason=p2p.reason or REASON_PUNCH_TIMEOUT)


def establish_session(
    prober: Prober,
    collector: Collector,
    checker: Checker,
    relay_requester: RelayRequester,
    peer_candidates: Sequence[Candidate] | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_progress: Callable[[int, str], None] | None = None,
    tcp_prober: TcpProber | None = None,
) -> EstablishResult:
    """按 L1 -> L2 -> L3 顺序建连, 返回完整判定与各级结果.

    :param peer_candidates: 对端候选; None 表示注入桩口径(collector 结果为两端合并), 空元组表示真实无对端
    :param on_progress: 进度回调, 参数为 (阶段序号 1-4, 中文说明)
    :param tcp_prober: L1 TCP 直连探测; None 表示 IPv6 直连关闭, 记 ``ipv6_disabled``
    """
    notify = on_progress or _ignore_progress
    notify(1, "正在收集本机与对端候选")
    ipv6 = ipv6_self_check(prober, clock=clock)
    local = collect_candidates(collector)
    peer_supplied = peer_candidates is not None
    if peer_candidates is None:
        peer = tuple(sorted(local, key=_candidate_sort_key))
        combined = tuple(local)
    else:
        peer = tuple(sorted(peer_candidates, key=_candidate_sort_key))
        combined = (*local, *peer)

    mapping = classify_mapping(local)
    if classify_mapping(peer) == MAPPING_SYMMETRIC:
        mapping = MAPPING_SYMMETRIC
    local_v6 = [item for item in local if item.kind == KIND_HOST and item.family == FAMILY_V6]
    peer_v6 = [item for item in peer if item.kind == KIND_HOST and item.family == FAMILY_V6]
    # L2 只对 IPv4 候选做 UDP 打洞; v6 host 属于 L1, 失败后不在 L2 重试
    # 真实对端候选可用时按两端地址集合是否有交集判断同出口(精确模式), 避免把本端自己的
    # srflx + mapped(天然同地址)误当成两端同出口而误剔; 注入桩口径下两端混在同一个列表,
    # 只能退回合并列表的保守模式
    egress_shared = same_egress(local, peer) if peer_supplied else same_egress(combined)
    mapped_count = sum(1 for item in local if item.kind == KIND_MAPPED)
    has_mapped = mapped_count > 0
    # 同出口时公网地址回连依赖路由器 hairpin, 实测小米 R3D 不支持, 因此 srflx/mapped 一律剔除,
    # 避免把「路由不支持回环」误判成打洞失败
    peer_v4 = [
        item
        for item in peer
        if (item.kind == KIND_HOST and item.family == FAMILY_V4)
        or (item.kind in {KIND_SRFLX, KIND_MAPPED} and not egress_shared)
    ]

    notify(2, "正在尝试 IPv6 直连")
    direct: LevelResult | None = None
    if not ipv6.ok or not local_v6 or not peer_v6:
        # 本机无 v6 全局地址或对端无 v6 host: 直接跳过, 耗时 0
        p2p = _failed(LEVEL_P2P, REASON_IPV6_UNREACHABLE, 0.0)
    elif tcp_prober is None:
        direct = _failed(LEVEL_IPV6, REASON_IPV6_DISABLED, 0.0)
        p2p = _failed(LEVEL_P2P, REASON_IPV6_UNREACHABLE, 0.0)
    else:
        direct = run_tcp_direct_check(peer_v6, tcp_prober, clock=clock)
        if direct.ok:
            notify(4, "已建立 IPv6 直连")
            return EstablishResult(
                decision=Decision(transport=TRANSPORT_DIRECT_IPV6, reason=REASON_OK),
                ipv6=ipv6,
                p2p=_with_mapping(LevelResult(LEVEL_P2P, True, direct.duration_ms, REASON_OK, direct.detail), mapping),
                direct=direct,
            )
        p2p = _failed(LEVEL_P2P, REASON_IPV6_UNREACHABLE, direct.duration_ms, **dict(direct.detail))

    notify(3, "正在尝试 P2P 打洞")
    if mapping == MAPPING_SYMMETRIC and not has_mapped:
        p2p = _failed(LEVEL_P2P, REASON_SYMMETRIC_NAT, 0.0, mapping=mapping)
    elif peer_v4:
        p2p = run_connectivity_checks(peer_v4, checker, DIRECT_BUDGET_MS, clock=clock)
    else:
        p2p = _failed(LEVEL_P2P, REASON_NO_CANDIDATE, 0.0, mapping=mapping, hairpin_blocked=egress_shared)

    decision = decide(
        LevelResult(LEVEL_IPV6, False, ipv6.duration_ms, ipv6.reason),
        p2p,
        mapping,
        mapped=has_mapped,
    )
    if decision.transport != TRANSPORT_RELAY:
        notify(4, "已建立 P2P 直连")
        return EstablishResult(
            decision=decision, ipv6=ipv6, p2p=_with_mapping(p2p, mapping, mapped_count), direct=direct
        )

    notify(4, "直连不可用，正在申请中继")
    try:
        relay = dict(relay_requester())
    except (OSError, TimeoutError, KeyError, RuntimeError) as exc:
        return EstablishResult(
            decision=Decision(transport=TRANSPORT_RELAY, reason=REASON_RELAY_UNAVAILABLE),
            ipv6=ipv6,
            p2p=_with_mapping(p2p, mapping, mapped_count),
            relay={"error": f"{type(exc).__name__}: {exc}"},
            direct=direct,
        )
    return EstablishResult(
        decision=Decision(transport=TRANSPORT_RELAY, reason=decision.reason),
        ipv6=ipv6,
        p2p=_with_mapping(p2p, mapping, mapped_count),
        relay=relay,
        relay_prefetched=p2p.duration_ms >= RELAY_PREFETCH_AT_MS,
        direct=direct,
    )


# -------------------------------- 路径快照与断言 --------------------------------


def path_snapshot(result: EstablishResult) -> dict[str, Any]:
    """汇总 spec ipv6-punch §4.4 的 ``connect.path_selected`` 字段.

    L1 命中取 TCP 探测记录的 peer; L2 命中取打洞观察到的对端端点.
    """
    direct = result.direct
    detail: Mapping[str, Any] = direct.detail if direct is not None else {}
    peer_address = str(detail.get("peer_address", ""))
    peer_family = int(detail.get("peer_family", 0) or 0)
    peer_port = int(detail.get("peer_port", 0) or 0)
    local_port = int(detail.get("local_port", 0) or 0)
    if result.peer is not None:
        peer_address, peer_port = result.peer
        peer_family = infer_family(peer_address)
    return {
        "transport": result.decision.transport,
        "peer_address": peer_address,
        "peer_family": peer_family,
        "peer_port": peer_port,
        "relay_used": result.decision.transport == TRANSPORT_RELAY,
        "local_port": local_port,
        "reason": result.decision.reason,
    }


def is_ipv6_literal(address: str) -> bool:
    """是否为无方括号的合法 IPv6 字面量."""
    if not address or "[" in address or "]" in address:
        return False
    try:
        return ipaddress.ip_address(address).version == FAMILY_V6
    except ValueError:
        return False


def is_global_ipv6(address: str) -> bool:
    """是否为全局可路由 IPv6; ULA(fc00::/7) 与链路本地(fe80::/10) 都不算."""
    if not is_ipv6_literal(address):
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global and not parsed.is_link_local


def assert_direct_ipv6(path: Mapping[str, Any], relay_connections: int = 0) -> dict[str, Any]:
    """§4.4 断言对象: 四项全成立才算 pass, 否则逐项给出实际值.

    ``relay_connections`` 为进程连接表里到中继/STUN 端点的已建立连接数; 直连路径不申请
    中继, 信令通道已在候选交换后关闭, 因此调用方按 0 传入, 并在 §10.3 取证里另做外部快照.
    """
    address = str(path.get("peer_address", ""))
    checks = {
        "transport_direct_ipv6": path.get("transport") == TRANSPORT_DIRECT_IPV6,
        "peer_address_global_v6": is_global_ipv6(address),
        "relay_used_false": path.get("relay_used") is False,
        "no_relay_connection": relay_connections == 0,
    }
    return {
        "pass": all(checks.values()),
        **checks,
        "transport": path.get("transport", ""),
        "peer_address": address,
        "peer_family": path.get("peer_family", 0),
        "relay_used": path.get("relay_used"),
        "relay_connections": relay_connections,
    }


# -------------------------------- 运行期 --------------------------------


def watch_quality(sample: QualitySample) -> str | None:
    """运行期劣化判定: 命中任一阈值即建议切中继."""
    if sample.loss_pct > DEGRADE_LOSS_PCT:
        return TRANSPORT_RELAY
    if sample.rtt_p95_ms > DEGRADE_P95_MS:
        return TRANSPORT_RELAY
    if sample.no_frame_s >= DEGRADE_NO_FRAME_S:
        return TRANSPORT_RELAY
    if sample.keyframe_retries >= KEYFRAME_RETRY_LIMIT:
        return TRANSPORT_RELAY
    return None


class TransportMonitor:
    """运行期传输状态机: 只允许 direct -> relay, 切了不回切."""

    def __init__(self, transport: str) -> None:
        if transport not in {TRANSPORT_DIRECT_IPV6, TRANSPORT_DIRECT_UDP, TRANSPORT_RELAY}:
            raise ValueError(f"未知传输类型: {transport}")
        self._transport = transport

    @property
    def transport(self) -> str:
        return self._transport

    def observe(self, sample: QualitySample) -> str:
        """观察一次采样, 返回当前传输; 锁定后忽略一切后续采样."""
        if self._transport == TRANSPORT_RELAY:
            return self._transport
        if watch_quality(sample) is not None:
            self._transport = TRANSPORT_RELAY
        return self._transport


def _ignore_progress(stage: int, text: str) -> None:
    """默认进度回调: 不留存, 不报错."""
    del stage, text


REASON_TEXT: dict[str, str] = {
    REASON_OK: "正常",
    REASON_IPV6_NO_GLOBAL: "本机无可用全局 IPv6",
    REASON_IPV6_UNREACHABLE: "IPv6 直连不可达",
    REASON_IPV6_DISABLED: "IPv6 直连未启用",
    REASON_SYMMETRIC_NAT: "对端为对称型 NAT，无法打洞",
    REASON_PUNCH_TIMEOUT: "打洞超时",
    REASON_SINGLE_DIRECTION: "打洞只有单向可达",
    REASON_NO_CANDIDATE: "没有可用的对端候选",
    REASON_RELAY_UNAVAILABLE: "中继申请失败",
}


def describe_reason(reason: str) -> str:
    """把 reason 枚举翻成中文, 用于日志与失败提示."""
    return REASON_TEXT.get(reason, reason)


def describe_decision(decision: Decision, relay: Mapping[str, Any] | None = None) -> str:
    """把最终决定翻成可展示的中文文案."""
    if decision.transport == TRANSPORT_DIRECT_IPV6:
        return "已建立 IPv6 直连"
    if decision.transport == TRANSPORT_DIRECT_UDP:
        return "已建立 P2P 直连（UDP 打洞）"
    if decision.reason == REASON_RELAY_UNAVAILABLE:
        return "直连与中继均不可用：中继申请失败，请检查网络后重试"
    budget = (relay or {}).get("budget_kbps")
    amount = f"{budget} kbps" if isinstance(budget, int) else "预算未返回"
    return f"直连不可用，已转中继（720p30，{amount}）"
