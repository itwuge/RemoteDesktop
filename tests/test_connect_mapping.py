# ==========================================================================================
# 文件        : test_connect_mapping.py
# 所属目录    : tests/
# 文件用途    : UPnP-IGD / NAT-PMP 端口映射客户端与生命周期的单测
# 架构定位    : 纯桩测试, 不接触真实路由器, 不发出任何真实网络包
# 核心功能    : 覆盖发现过滤、成功/拒绝/租约到期/续租、PCP 回退、外部地址不一致、
#               清理幂等、候选并入与同出口 hairpin 剔除
# 职责边界    : 只测 src/rdc/connect/mapping.py 与 policy 中的映射相关分支
# 运行环境    : Python 3.12
# 依赖关系    : pytest
# ==========================================================================================

"""端口映射单测, 全部使用伪造 IGD 响应与注入桩.

说明: 这里的「成功」只证明代码分支正确, 真实路由器可达性由 scripts/verify_port_mapping.py
在真机上验证, 二者不可互相替代.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

import pytest

from rdc.connect import mapping
from rdc.connect.mapping import (
    MAPPING_EXTERNAL_MISMATCH,
    MAPPING_EXTERNAL_UNREACHABLE,
    MAPPING_LEASE_FAILED,
    MAPPING_REJECTED,
    MAPPING_UNSUPPORTED,
    REASON_DISABLED,
    REASON_LEASE_EXPIRED,
    REASON_NO_GATEWAY,
    REASON_OK,
    REASON_PCP_FALLBACK,
    VERIFY_EXTERNAL,
    VERIFY_READBACK,
    IgdService,
    MappingLease,
    MappingOutcome,
    NatpmpResult,
    PcpResult,
    PortMapper,
    SsdpResponse,
    close_all_mappings,
    mapped_candidate,
    natpmp_add_mapping,
    parse_device_description,
    pcp_add_mapping,
    reconcile_with_stun,
)
from rdc.connect.policy import (
    MAPPING_EIM,
    MAPPING_SYMMETRIC,
    REASON_NO_CANDIDATE,
    REASON_SYMMETRIC_NAT,
    TRANSPORT_DIRECT_UDP,
    TRANSPORT_RELAY,
    Candidate,
    CheckOutcome,
    Ipv6Probe,
    LevelResult,
    collect_candidates,
    decide,
    establish_session,
    same_egress,
)

PUBLIC_V4 = "115.150.84.112"
GATEWAY = "192.168.31.1"
LOCAL_V4 = "192.168.31.80"
INTERNAL_PORT = 51234

DESCRIPTION_XML = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <friendlyName>XiaoMi Router</friendlyName>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
            <controlURL>/ctl/L3F</controlURL>
          </service>
        </serviceList>
        <deviceList>
          <device>
            <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
            <serviceList>
              <service>
                <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
                <controlURL>/ctl/IPConn</controlURL>
              </service>
            </serviceList>
          </device>
        </deviceList>
      </device>
    </deviceList>
  </device>
</root>
"""


def service(control_url: str = "http://192.168.31.1:5351/ctl/IPConn") -> IgdService:
    return IgdService(
        location="http://192.168.31.1:5351/rootDesc.xml",
        control_url=control_url,
        service_type="urn:schemas-upnp-org:service:WANIPConnection:1",
        server="Linux/3.4, UPnP/1.0, MiniUPnPd/2.0",
    )


def _value(body: str, tag: str) -> str:
    match = re.search(f"<{tag}>([^<]*)</{tag}>", body)
    return match.group(1) if match else ""


def _fault(code: int) -> str:
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body><s:Fault><faultcode>s:Client</faultcode><detail><UPnPError>"
        f"<errorCode>{code}</errorCode><errorDescription>fault</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )


class FakeIgd:
    """伪造 IGD 控制端点, 记录动作序列并按需返回 SOAP Fault."""

    def __init__(self, external: str = PUBLIC_V4) -> None:
        self.external = external
        self.entries: dict[int, dict[str, str]] = {}
        self.actions: list[str] = []
        self.add_fault: int | None = None
        self.delete_fault: int | None = None
        self.readback_garbage = False
        self.added = False

    def __call__(self, _service: IgdService, action: str, body: str) -> str:
        self.actions.append(action)
        external_port = int(_value(body, "NewExternalPort") or 0)
        if action == "GetExternalIPAddress":
            return (
                '<u:GetExternalIPAddressResponse xmlns:u="urn:schemas-upnp-org:service:WANIPConnection:1">'
                f"<NewExternalIPAddress>{self.external}</NewExternalIPAddress>"
                "</u:GetExternalIPAddressResponse>"
            )
        if action == "AddPortMapping":
            if self.add_fault is not None:
                return _fault(self.add_fault)
            self.added = True
            self.entries[external_port] = {
                "internal_port": _value(body, "NewInternalPort"),
                "internal_client": _value(body, "NewInternalClient"),
                "lease_s": _value(body, "NewLeaseDuration"),
                "description": _value(body, "NewPortMappingDescription"),
                "enabled": "1",
            }
            return "<u:AddPortMappingResponse/>"
        if action == "GetSpecificPortMappingEntry":
            if self.readback_garbage and self.added:
                # 模拟「Add 成功但读回端口/客户端不一致」
                return (
                    "<NewInternalPort>1</NewInternalPort><NewInternalClient>192.168.31.99</NewInternalClient>"
                    "<NewEnabled>1</NewEnabled><NewPortMappingDescription>x</NewPortMappingDescription>"
                    "<NewLeaseDuration>120</NewLeaseDuration>"
                )
            entry = self.entries.get(external_port)
            if entry is None:
                return _fault(714)
            return (
                "<NewInternalPort>{internal_port}</NewInternalPort>"
                "<NewInternalClient>{internal_client}</NewInternalClient>"
                "<NewEnabled>1</NewEnabled>"
                "<NewPortMappingDescription>{description}</NewPortMappingDescription>"
                "<NewLeaseDuration>{lease_s}</NewLeaseDuration>"
            ).format(**entry)
        if action == "DeletePortMapping":
            if self.delete_fault is not None:
                return _fault(self.delete_fault)
            if self.entries.pop(external_port, None) is None:
                return _fault(714)
            return "<u:DeletePortMappingResponse/>"
        return _fault(401)


class FakeSocket:
    """最小 UDP socket 替身, 只覆盖 NAT-PMP/PCP 用到的四个方法."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.destination: tuple[str, int] | None = None

    def gettimeout(self) -> float | None:
        return self.timeout

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def sendto(self, data: bytes, destination: tuple[str, int]) -> int:
        self.sent.append(data)
        self.destination = destination
        return len(data)

    def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
        return self.reply, (GATEWAY, 5351)


def natpmp_reply(result: int, internal_port: int, external_port: int, lifetime: int) -> bytes:
    return (
        b"\x00\x81"
        + result.to_bytes(2, "big")
        + (0).to_bytes(4, "big")
        + internal_port.to_bytes(2, "big")
        + external_port.to_bytes(2, "big")
        + lifetime.to_bytes(4, "big")
    )


def make_mapper(
    igd: FakeIgd | None,
    *,
    natpmp_result: NatpmpResult | None = None,
    pcp_result: PcpResult | None = None,
    external_address: str = PUBLIC_V4,
) -> PortMapper:
    """构造全注入的映射器: 发现、SOAP、NAT-PMP、PCP 都不触网."""
    return PortMapper(
        gateway=GATEWAY,
        local_ip=LOCAL_V4,
        discover=lambda: service() if igd is not None else None,
        soap=igd if igd is not None else (lambda *_a, **_k: _fault(501)),
        natpmp_add=lambda *_a, **_k: natpmp_result or NatpmpResult(result_code=1),
        natpmp_remove=lambda *_a, **_k: True,
        pcp_add=lambda *_a, **_k: pcp_result or PcpResult(version=0, result_code=-1),
        natpmp_external=lambda _gateway: (0, external_address),
        clock=lambda: 1000.0,
    )


# -------------------------------- 发现与解析 --------------------------------


def test_parse_device_description_skips_non_wan_and_resolves_relative_url() -> None:
    result = parse_device_description("http://192.168.31.1:5351/rootDesc.xml", DESCRIPTION_XML)
    assert result is not None
    assert result.service_type == "urn:schemas-upnp-org:service:WANIPConnection:1"
    assert result.control_url == "http://192.168.31.1:5351/ctl/IPConn"


def test_parse_device_description_returns_none_without_wan_service() -> None:
    xml = (
        "<root><service><serviceType>urn:x:Layer3Forwarding:1</serviceType><controlURL>/a</controlURL></service></root>"
    )
    assert parse_device_description("http://192.168.31.1/rootDesc.xml", xml) is None


def test_discover_igd_only_trusts_default_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = (
        SsdpResponse(source="192.168.31.12", location="http://192.168.31.12:8200/rootDesc.xml"),
        SsdpResponse(source=GATEWAY, location="http://192.168.31.1:5351/rootDesc.xml"),
    )
    monkeypatch.setattr(mapping, "ssdp_search", lambda *_a, **_k: responses)
    monkeypatch.setattr(mapping, "fetch_device_description", lambda _url, **_k: DESCRIPTION_XML)
    result = mapping.discover_igd(LOCAL_V4, GATEWAY)
    assert result is not None
    assert "192.168.31.1" in result.location


def test_discover_igd_refuses_ambiguous_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = (
        SsdpResponse(source="192.168.31.1", location="http://192.168.31.1:5351/rootDesc.xml"),
        SsdpResponse(source="192.168.31.12", location="http://192.168.31.12:8200/rootDesc.xml"),
    )
    monkeypatch.setattr(mapping, "ssdp_search", lambda *_a, **_k: responses)
    monkeypatch.setattr(mapping, "fetch_device_description", lambda _url, **_k: DESCRIPTION_XML)
    # 未显式指定网关且同网段存在两个 IGD 时必须拒绝猜测
    assert mapping.discover_igd(LOCAL_V4, "") is None


def test_discover_igd_returns_none_without_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mapping, "ssdp_search", lambda *_a, **_k: ())
    assert mapping.discover_igd(LOCAL_V4, GATEWAY) is None


# -------------------------------- 映射申请 --------------------------------


def test_map_udp_port_success_reports_readback() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4, description="rdc-test")
    assert outcome.ok
    assert outcome.protocol == mapping.PROTOCOL_UPNP
    assert outcome.reason == REASON_OK
    assert outcome.external_address == PUBLIC_V4
    assert outcome.internal_port == INTERNAL_PORT
    assert outcome.lease_s == mapping.DEFAULT_LEASE_S
    assert outcome.verification == VERIFY_READBACK
    assert igd.actions == [
        "GetExternalIPAddress",
        "GetSpecificPortMappingEntry",
        "AddPortMapping",
        "GetSpecificPortMappingEntry",
    ]
    assert igd.entries[outcome.external_port]["internal_port"] == str(INTERNAL_PORT)


def test_map_udp_port_rejected_keeps_soap_code() -> None:
    igd = FakeIgd()
    igd.add_fault = 718
    mapper = make_mapper(igd)
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert not outcome.ok
    assert outcome.reason == MAPPING_REJECTED
    assert outcome.detail["soap_code"] == 718


def test_map_udp_port_never_overwrites_existing_rule() -> None:
    igd = FakeIgd()
    igd.entries[50000] = {
        "internal_port": "8080",
        "internal_client": "192.168.31.50",
        "lease_s": "0",
        "description": "NAS",
        "enabled": "1",
    }
    mapper = make_mapper(igd)
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4, requested_external_port=50000)
    assert outcome.ok
    # 已占用端口被跳过, 别人的规则原样保留
    assert outcome.external_port != 50000
    assert igd.entries[50000]["internal_port"] == "8080"


def test_map_udp_port_readback_mismatch_is_lease_failure() -> None:
    igd = FakeIgd()
    igd.readback_garbage = True
    mapper = make_mapper(igd)
    # AddPortMapping 成功但读回端口不一致, 必须判租约失败并删除半成品
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert not outcome.ok
    assert outcome.reason == MAPPING_LEASE_FAILED
    assert "DeletePortMapping" in igd.actions


def test_map_udp_port_no_igd_and_no_natpmp_is_unsupported() -> None:
    mapper = make_mapper(None)
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert not outcome.ok
    assert outcome.reason == MAPPING_UNSUPPORTED


def test_map_udp_port_without_gateway_is_rejected_early() -> None:
    mapper = PortMapper(gateway="", local_ip="", discover=lambda: service())
    outcome = mapper.map_udp_port(INTERNAL_PORT)
    assert not outcome.ok
    assert outcome.reason == REASON_NO_GATEWAY


def test_map_udp_port_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RDC_PORT_MAPPING", "off")
    mapper = make_mapper(FakeIgd())
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert not outcome.ok
    assert outcome.reason == REASON_DISABLED


# -------------------------------- PCP 回退 NAT-PMP --------------------------------


def test_pcp_failure_falls_back_to_natpmp() -> None:
    mapper = make_mapper(
        None,
        pcp_result=PcpResult(version=0, result_code=-1),
        natpmp_result=NatpmpResult(result_code=0, internal_port=INTERNAL_PORT, external_port=52000, lifetime_s=120),
    )
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert outcome.ok
    assert outcome.protocol == mapping.PROTOCOL_NATPMP
    assert outcome.external_port == 52000
    assert outcome.detail["pcp_fallback"] is True


def test_pcp_success_reports_pcp_protocol() -> None:
    mapper = make_mapper(
        None,
        pcp_result=PcpResult(
            version=2,
            result_code=0,
            internal_port=INTERNAL_PORT,
            external_port=53000,
            lifetime_s=120,
            external_address=PUBLIC_V4,
        ),
    )
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4)
    assert outcome.ok
    assert outcome.protocol == mapping.PROTOCOL_PCP
    assert outcome.external_address == PUBLIC_V4


def test_pcp_unsupported_reply_is_not_ok() -> None:
    # 实测小米固件把 PCP MAP 当 NAT-PMP 处理, 回 version=0 的报文
    sock = FakeSocket(b"\x00\x81\x00\x03" + b"\x00" * 12)
    result = pcp_add_mapping(sock, GATEWAY, INTERNAL_PORT, 0, 120)
    assert not result.ok
    assert result.version == 0


def test_natpmp_request_wire_format_and_reply_parse() -> None:
    sock = FakeSocket(natpmp_reply(0, INTERNAL_PORT, 52000, 120))
    result = natpmp_add_mapping(sock, GATEWAY, INTERNAL_PORT, 0, 120)
    assert result.ok
    assert result.external_port == 52000
    assert result.lifetime_s == 120
    request = sock.sent[0]
    assert request[0:2] == b"\x00\x01"
    assert int.from_bytes(request[4:6], "big") == INTERNAL_PORT
    assert int.from_bytes(request[8:12], "big") == 120
    assert sock.destination == (GATEWAY, 5351)


# -------------------------------- 续租与清理 --------------------------------


def _mapping_outcome(mapper: PortMapper, igd: FakeIgd) -> MappingOutcome:
    outcome = mapper.map_udp_port(INTERNAL_PORT, internal_client=LOCAL_V4, description="rdc-test")
    assert outcome.ok
    return outcome


def test_renew_readds_when_entry_disappeared() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    igd.entries.clear()
    renewed = mapper.renew(outcome)
    assert renewed.ok
    assert renewed.detail["renewed"] is True
    assert igd.entries[outcome.external_port]["internal_port"] == str(INTERNAL_PORT)


def test_renew_after_lease_expiry_reports_expired() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    igd.entries.clear()
    igd.add_fault = 718
    renewed = mapper.renew(outcome)
    assert not renewed.ok
    assert renewed.reason == REASON_LEASE_EXPIRED


def test_renew_is_noop_for_failed_mapping() -> None:
    mapper = make_mapper(FakeIgd())
    failed = MappingOutcome(ok=False, protocol=mapping.PROTOCOL_UPNP, reason=MAPPING_REJECTED)
    assert mapper.renew(failed) == failed


def test_delete_is_idempotent() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    assert mapper.delete(outcome)
    # 第二次删除时条目已不存在, IGD 返回 714, 视为已清理且不抛异常
    assert mapper.delete(outcome)


def test_lease_close_is_idempotent_and_deletes_once() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    lease = MappingLease(mapper, outcome)
    lease.start()
    assert lease.renewal_interval_s == mapping.DEFAULT_LEASE_S / 2
    lease.close()
    external_port = outcome.external_port
    assert external_port not in igd.entries
    lease.close()
    assert igd.actions.count("DeletePortMapping") == 1


def test_lease_expiry_degrades_mapping_and_cleans_up() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    lease = MappingLease(mapper, outcome)
    igd.entries.clear()
    igd.add_fault = 718
    for _ in range(mapping.MAX_RENEW_FAILURES - 1):
        assert lease.renew_once()
        assert not lease.expired
    assert not lease.renew_once()
    assert lease.expired
    degraded = lease.mapping
    assert degraded is not None
    # 租约到期是独立原因, 不能被记成「外部不可达」
    assert degraded.reason == REASON_LEASE_EXPIRED
    assert degraded.reason != MAPPING_EXTERNAL_UNREACHABLE
    assert degraded.detail["expired_after_failures"] == mapping.MAX_RENEW_FAILURES
    assert mapped_candidate(degraded) is None
    lease.close()
    assert igd.actions.count("DeletePortMapping") == 1


def test_lease_expiry_survives_renew_transport_errors() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)

    def broken(_mapping: MappingOutcome, _lease: int | None, _sock: object = None) -> MappingOutcome:
        raise TimeoutError("gateway unreachable")

    mapper.renew = broken
    lease = MappingLease(mapper, outcome)
    for _ in range(mapping.MAX_RENEW_FAILURES):
        lease.renew_once()
    assert lease.expired
    assert lease.mapping is not None
    assert lease.mapping.reason == REASON_LEASE_EXPIRED


def test_close_all_mappings_sweeps_active_leases() -> None:
    igd = FakeIgd()
    mapper = make_mapper(igd)
    outcome = _mapping_outcome(mapper, igd)
    lease = MappingLease(mapper, outcome)
    lease.start()
    assert close_all_mappings() >= 1
    assert outcome.external_port not in igd.entries
    assert close_all_mappings() == 0


# -------------------------------- 外部一致性 --------------------------------


def test_external_address_mismatch_with_stun_degrades() -> None:
    outcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address="10.0.0.5",
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
        verification=VERIFY_READBACK,
    )
    degraded = reconcile_with_stun(outcome, PUBLIC_V4)
    assert not degraded.ok
    assert degraded.reason == MAPPING_EXTERNAL_MISMATCH
    assert mapped_candidate(degraded) is None


def test_public_external_address_mismatch_with_stun_degrades() -> None:
    outcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address="115.150.84.113",
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
        verification=VERIFY_READBACK,
    )
    degraded = reconcile_with_stun(outcome, PUBLIC_V4)
    assert not degraded.ok
    assert degraded.reason == MAPPING_EXTERNAL_MISMATCH
    assert degraded.detail["stun_address"] == PUBLIC_V4


def test_cgnat_external_address_is_not_a_candidate() -> None:
    outcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address="100.64.3.7",
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
        verification=VERIFY_READBACK,
    )
    degraded = reconcile_with_stun(outcome, "")
    assert not degraded.ok
    assert degraded.reason == MAPPING_EXTERNAL_MISMATCH
    assert mapped_candidate(degraded) is None


def test_external_probe_upgrades_verification_and_priority() -> None:
    outcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address=PUBLIC_V4,
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
        verification=VERIFY_READBACK,
    )
    confirmed = reconcile_with_stun(outcome, PUBLIC_V4, probe=lambda _addr, _port: True)
    assert confirmed.ok
    assert confirmed.verification == VERIFY_EXTERNAL
    candidate = mapped_candidate(confirmed)
    readback_only = mapped_candidate(outcome)
    assert candidate is not None
    assert readback_only is not None
    assert candidate.kind == "mapped"
    assert candidate.priority > readback_only.priority


def test_failed_external_probe_degrades() -> None:
    outcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address=PUBLIC_V4,
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
        verification=VERIFY_READBACK,
    )
    degraded = reconcile_with_stun(outcome, PUBLIC_V4, probe=lambda _addr, _port: False)
    assert not degraded.ok
    assert degraded.reason == MAPPING_EXTERNAL_UNREACHABLE


# -------------------------------- 策略接入 --------------------------------


def mapped(port: int = 50000, address: str = PUBLIC_V4, priority: int = 92) -> Candidate:
    return Candidate(kind="mapped", family=4, address=address, port=port, priority=priority, related="upnp")


def v4_host(address: str = LOCAL_V4) -> Candidate:
    return Candidate(kind="host", family=4, address=address, port=INTERNAL_PORT, priority=80)


def srflx(port: int = INTERNAL_PORT, address: str = PUBLIC_V4) -> Candidate:
    return Candidate(kind="srflx", family=4, address=address, port=port, priority=90, related=LOCAL_V4)


def ok_outcome() -> CheckOutcome:
    return CheckOutcome(sent=10, responses_forward=3, responses_backward=3, rtt_samples_ms=(10.0, 11.0, 12.0))


def test_collect_candidates_keeps_mapped() -> None:
    items = collect_candidates(lambda _deadline: [mapped(), v4_host(), srflx()])
    assert [item.kind for item in items].count("mapped") == 1


def test_same_egress_counts_mapped_as_public_candidate() -> None:
    assert same_egress([srflx(49160), mapped(50000)])
    assert not same_egress([mapped(50000)])


def test_same_egress_precise_mode_requires_both_sides() -> None:
    # 本端 srflx + mapped 天然同地址, 对端只有内网 host 时不得误判为同出口
    assert not same_egress([srflx(), mapped()], [v4_host("10.0.0.9")])
    assert same_egress([srflx()], [srflx(52000)])
    # 双出口: 两端公网地址不同, 不得误剔
    assert not same_egress([srflx()], [srflx(52000, "203.0.113.8")])


def test_decide_allows_punch_when_mapped_exists() -> None:
    ipv6 = LevelResult(level="ipv6", ok=False, duration_ms=0.0, reason="ipv6_no_global")
    p2p = LevelResult(level="p2p", ok=True, duration_ms=100.0, reason="ok")
    decision = decide(ipv6, p2p, MAPPING_SYMMETRIC, mapped=True)
    assert decision.transport == TRANSPORT_DIRECT_UDP


def test_decide_without_mapped_still_relays_on_symmetric() -> None:
    ipv6 = LevelResult(level="ipv6", ok=False, duration_ms=0.0, reason="ipv6_no_global")
    p2p = LevelResult(level="p2p", ok=True, duration_ms=100.0, reason="ok")
    decision = decide(ipv6, p2p, MAPPING_SYMMETRIC)
    assert decision.transport == TRANSPORT_RELAY
    assert decision.reason == REASON_SYMMETRIC_NAT


def test_symmetric_nat_with_mapped_still_punches() -> None:
    budgets: list[int] = []

    def checker(_peer: Sequence[Candidate], budget_ms: int) -> CheckOutcome:
        budgets.append(budget_ms)
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host(), srflx(49160), srflx(52190), mapped(50000)],
        checker=checker,
        relay_requester=lambda: {"budget_kbps": 3000, "quality_profile": "720p30"},
    )
    assert budgets
    assert result.decision.transport == TRANSPORT_DIRECT_UDP
    assert result.p2p.detail["mapped_candidates"] == 1


def test_hairpin_shared_egress_excludes_mapped_and_srflx() -> None:
    seen: list[list[str]] = []

    def checker(peer: Sequence[Candidate], _budget_ms: int) -> CheckOutcome:
        seen.append([item.kind for item in peer])
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host(), srflx(INTERNAL_PORT), mapped(50000)],
        checker=checker,
        relay_requester=lambda: {"budget_kbps": 3000, "quality_profile": "720p30"},
        peer_candidates=[srflx(52000), mapped(51000)],
    )
    # 两端公网候选同一地址 -> 同出口, 公网回连依赖 hairpin(实测不支持), 一个都不许试
    assert seen == []
    assert result.p2p.reason == REASON_NO_CANDIDATE
    assert result.p2p.detail["hairpin_blocked"] is True
    assert result.decision.transport == TRANSPORT_RELAY


def test_mapped_candidate_is_exchanged_before_decision() -> None:
    seen: list[list[str]] = []

    def checker(peer: Sequence[Candidate], _budget_ms: int) -> CheckOutcome:
        seen.append([item.kind for item in peer])
        return ok_outcome()

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host(), srflx(INTERNAL_PORT), mapped(50000)],
        checker=checker,
        relay_requester=lambda: {"budget_kbps": 3000, "quality_profile": "720p30"},
        peer_candidates=[
            Candidate(kind="host", family=4, address="10.0.0.9", port=49160, priority=80),
            mapped(51000, "203.0.113.8"),
        ],
    )
    # 按优先级排序: mapped(92) 先于 host(80)
    assert seen == [["mapped", "host"]]
    assert result.decision.transport == TRANSPORT_DIRECT_UDP
    assert result.p2p.detail["mapping"] == MAPPING_EIM


def test_mapping_detail_present_even_when_no_mapping() -> None:
    result = establish_session(
        prober=lambda _deadline: Ipv6Probe(),
        collector=lambda _deadline: [v4_host()],
        checker=lambda _p, _b: ok_outcome(),
        relay_requester=lambda: {"budget_kbps": 3000, "quality_profile": "720p30"},
    )
    assert result.p2p.detail["mapped_candidates"] == 0


def test_mapping_outcome_event_fields_are_redacted() -> None:
    outcome: MappingOutcome = MappingOutcome(
        ok=True,
        protocol=mapping.PROTOCOL_UPNP,
        reason=REASON_OK,
        external_address=PUBLIC_V4,
        external_port=50000,
        internal_port=INTERNAL_PORT,
        lease_s=120,
    )
    event: dict[str, Any] = outcome.as_event()
    assert "external_address" not in event
    assert event["mapping_external_port"] == 50000


def test_describe_reason_covers_fallback() -> None:
    assert "NAT-PMP" in mapping.describe_reason(REASON_PCP_FALLBACK)
