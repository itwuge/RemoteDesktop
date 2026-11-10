# ==========================================================================================
# 文件        : test_connect_runtime.py
# 所属目录    : tests/
# 文件用途    : 真实实现层与命令层接线的单测, 覆盖 STUN 报文、打洞包、中文文案、后台任务
# 架构定位    : 只验证纯函数与注入桩; 不建真实 socket, 不访问公网, 不依赖本机 IPv6 状态
# 核心功能    : XOR-MAPPED 编解码; 探测包编解码; 全局 v6 过滤; 决策文案; 连接任务落会话
# 职责边界    : 不替代真机验证; 真机路径仍以「未验证」标注
# 运行环境    : Python 3.12
# 依赖关系    : 被测对象为 src/rdc/connect/{stun,punch,native,policy}.py 与 commands/service.py
# ==========================================================================================

"""真实实现层单测.

网络相关用例只用回环 socket 验证协议格式与收发, 不访问公网; 命令层用例全部注入 connector 桩.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

import rdc.connect.runtime as runtime_module
from rdc.commands.service import CommandService, is_relay_host
from rdc.connect.native import is_global_v6
from rdc.connect.policy import (
    IPV6_TCP_BUDGET_MS,
    MAPPING_EIM,
    REASON_IPV6_DISABLED,
    REASON_IPV6_NO_GLOBAL,
    REASON_IPV6_UNREACHABLE,
    REASON_OK,
    REASON_RELAY_UNAVAILABLE,
    TRANSPORT_DIRECT_IPV6,
    TRANSPORT_DIRECT_UDP,
    TRANSPORT_RELAY,
    Candidate,
    CheckOutcome,
    Decision,
    EstablishResult,
    Ipv6Probe,
    LevelResult,
    TcpProbe,
    assert_direct_ipv6,
    describe_decision,
    direct_level,
    establish_session,
    path_snapshot,
)
from rdc.connect.punch import PunchResponder, decode_probe, encode_probe
from rdc.connect.runtime import log_path_events
from rdc.connect.stun import MAGIC_COOKIE, build_binding_request, parse_binding_response, query_server
from rdc.core import logging as rdc_logging

PEER_ID = "ABCDEFGHIJKLMNOPQRST"
V6_PEER = "240e:370:6303:cb0::2"


def _binding_response(txid: bytes, address: str, port: int) -> bytes:
    """按 RFC 5389 构造一个 Binding 成功响应, 用于解析测试."""
    cookie = struct.pack(">I", MAGIC_COOKIE)
    xport = port ^ (MAGIC_COOKIE >> 16)
    xaddr = bytes(a ^ b for a, b in zip(socket.inet_aton(address), cookie, strict=True))
    value = b"\x00\x01" + struct.pack(">H", xport) + xaddr
    attr = struct.pack(">HH", 0x0020, len(value)) + value
    return struct.pack(">HHI", 0x0101, len(attr), MAGIC_COOKIE) + txid + attr


def test_stun_response_parses_xor_mapped_address() -> None:
    _request, txid = build_binding_request()
    address, port = parse_binding_response(_binding_response(txid, "115.150.84.112", 49360), txid)
    assert address == "115.150.84.112"
    assert port == 49360


def test_stun_response_rejects_wrong_transaction() -> None:
    _request, txid = build_binding_request()
    with pytest.raises(ValueError):
        parse_binding_response(_binding_response(b"0" * 12, "1.2.3.4", 50000), txid)


def test_probe_codec_roundtrip_and_rejects_foreign_packet() -> None:
    token = "0123456789abcdef"
    assert decode_probe(encode_probe(token, b"Q")) == ("Q", token)
    assert decode_probe(encode_probe(token, b"R")) == ("R", token)
    assert decode_probe(b"HTTP/1.1 200 OK\r\n") is None
    assert decode_probe(encode_probe(token, b"X")) is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("240e:1:2:3::10", True),
        ("2408:8000::1", True),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::1", False),
        ("2001:db8::1", False),
        ("192.168.31.80", False),
    ],
)
def test_global_v6_filter(address: str, expected: bool) -> None:
    assert is_global_v6(address) is expected


def test_describe_decision_uses_chinese_reason() -> None:
    relay = describe_decision(Decision(TRANSPORT_RELAY, REASON_OK), {"budget_kbps": 2900})
    assert relay == "直连不可用，已转中继（720p30，2900 kbps）"
    failed = describe_decision(Decision(TRANSPORT_RELAY, REASON_RELAY_UNAVAILABLE))
    assert "直连与中继均不可用" in failed
    direct = describe_decision(Decision(TRANSPORT_DIRECT_UDP, REASON_OK))
    assert "P2P 直连" in direct


def _relay_result(reason: str = REASON_OK) -> EstablishResult:
    ipv6 = LevelResult(level="ipv6", ok=False, duration_ms=0.0, reason="ipv6_no_global")
    p2p = LevelResult(level="p2p", ok=False, duration_ms=10.0, reason="no_candidate")
    return EstablishResult(
        decision=Decision(transport=TRANSPORT_RELAY, reason=reason),
        ipv6=ipv6,
        p2p=p2p,
        relay={"budget_kbps": 2900, "quality_profile": "720p30"},
    )


def _wait_task(service: CommandService, task_id: str, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if service.tasks[task_id].status in {"done", "failed", "canceled"}:
            return
        time.sleep(0.01)
    raise AssertionError("连接任务未在超时内结束")


def test_real_mode_records_transport_and_chinese_message() -> None:
    def connector(_device_id: str, _access_code: str, progress: Callable[[int, str], None]) -> EstablishResult:
        progress(1, "正在收集本机与对端候选")
        progress(4, "直连不可用，正在申请中继")
        return _relay_result()

    service = CommandService(connector=connector)
    service.peers[PEER_ID] = "对端"
    started = service.start_connection(PEER_ID, "123456")
    assert started.ok and started.data is not None
    task_id = str(started.data["task_id"])
    _wait_task(service, task_id)

    task = service.tasks[task_id]
    assert task.status == "done"
    assert "已转中继" in task.message
    session = next(iter(service.sessions.values()))
    assert session.transport_kind == "relay"
    assert session.quality_profile == "720p30"
    progress = service.get_connection_progress(task_id)
    assert progress.data is not None
    assert progress.data["status"] == "done"
    assert "已转中继" in progress.data["message"]


def test_real_mode_reports_relay_failure_in_chinese() -> None:
    def connector(_device_id: str, _access_code: str, _progress: Callable[[int, str], None]) -> EstablishResult:
        return _relay_result(REASON_RELAY_UNAVAILABLE)

    service = CommandService(connector=connector)
    service.peers[PEER_ID] = "对端"
    started = service.start_connection(PEER_ID, "123456")
    assert started.data is not None
    task_id = str(started.data["task_id"])
    _wait_task(service, task_id)

    task = service.tasks[task_id]
    assert task.status == "failed"
    assert "直连与中继均不可用" in task.message
    assert not service.sessions


def test_real_mode_wraps_exception_with_retry_hint() -> None:
    def connector(_device_id: str, _access_code: str, _progress: Callable[[int, str], None]) -> EstablishResult:
        raise OSError("网卡枚举失败")

    service = CommandService(connector=connector)
    service.peers[PEER_ID] = "对端"
    started = service.start_connection(PEER_ID, "123456")
    assert started.data is not None
    task_id = str(started.data["task_id"])
    _wait_task(service, task_id)

    task = service.tasks[task_id]
    assert task.status == "failed"
    assert task.message.startswith("连接失败：")
    assert "请检查网络后重试" in task.message


def test_empty_peer_candidates_skips_punch_and_falls_to_relay() -> None:
    calls: list[tuple[Candidate, ...]] = []

    def checker(peer: Sequence[Candidate], _budget: int) -> CheckOutcome:
        calls.append(tuple(peer))
        return CheckOutcome(sent=10, responses_forward=10, responses_backward=10, rtt_samples_ms=(1.0, 1.0, 1.0))

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe((), (), None, "无全局 IPv6 地址"),
        collector=lambda _deadline: [
            Candidate(kind="host", family=4, address="192.168.31.80", port=49160, priority=80),
            Candidate(kind="srflx", family=4, address="115.150.84.112", port=49160, priority=90),
        ],
        checker=checker,
        relay_requester=lambda: {"budget_kbps": 2900},
        peer_candidates=(),
    )

    assert calls == []
    assert result.decision.transport == TRANSPORT_RELAY
    assert result.p2p.reason == "no_candidate"
    assert result.p2p.detail["mapping"] == MAPPING_EIM


def test_live_connection_uses_command_service_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实链路构造 RemoteSession 必须带命令层设备身份, 不能用随机身份."""
    import rdc.session.remote as remote_module

    captured: dict[str, Any] = {}

    class RecordingSession:
        """只记录构造参数, 不建 socket, 不启线程."""

        transport_kind = "relay"

        def __init__(self, config: object, **kwargs: object) -> None:
            captured["config"] = config
            captured.update(kwargs)
            self.failed_reason = ""

        def start(self) -> None:
            captured["started"] = True

        def wait_ready(self, _timeout: float) -> bool:
            return True

        def stop(self) -> None:
            pass

    monkeypatch.setattr(remote_module, "RemoteSession", RecordingSession)
    service = CommandService(connect_mode="real")
    service.live_endpoint = "127.0.0.1:27888"
    started = service.start_connection(PEER_ID, "123456")
    assert started.ok and started.data is not None
    _wait_task(service, str(started.data["task_id"]))

    assert service.tasks[str(started.data["task_id"])].status == "done"
    identity = captured["identity"]
    assert identity is service.identity
    assert identity.device_id == service.identity.device_id
    assert captured["config"].device_id == PEER_ID


# -------------------------------- S3 IPv6 直连选路 --------------------------------


class _Clock:
    """可控时钟: 只推进注入的探测耗时, 不真实等待."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, milliseconds: float) -> None:
        self.now += milliseconds / 1000.0


def _v6_probe() -> Ipv6Probe:
    return Ipv6Probe(global_addresses=("240e:1:2:3::10",), reachable_targets=("2400:3200::1",))


def _local_candidates() -> list[Candidate]:
    return [
        Candidate(kind="host", family=6, address="240e:1:2:3::10", port=27888, priority=100),
        Candidate(kind="host", family=4, address="192.168.1.80", port=49160, priority=96),
    ]


def _tcp_success_probe(peer: Sequence[Candidate], budget_ms: int) -> TcpProbe:
    assert budget_ms == IPV6_TCP_BUDGET_MS
    target = peer[0]
    return TcpProbe(
        ok=True,
        peer_address=target.address,
        peer_family=target.family,
        peer_port=target.port,
        local_port=53422,
        rtt_ms=(27.4,),
        tried=1,
        reason=REASON_OK,
    )


def _direct_ipv6_result() -> EstablishResult:
    def unreachable(_peer: Sequence[Candidate], _budget: int) -> CheckOutcome:
        raise AssertionError("L1 已通过, L2 不应执行")

    return establish_session(
        prober=lambda _deadline: _v6_probe(),
        collector=lambda _deadline: _local_candidates(),
        checker=unreachable,
        relay_requester=lambda: (_ for _ in ()).throw(AssertionError("直连路径不应申请中继")),
        peer_candidates=[Candidate(kind="host", family=6, address=V6_PEER, port=27888, priority=100)],
        tcp_prober=_tcp_success_probe,
    )


def test_live_address_accepts_v6_forms() -> None:
    service = CommandService()
    assert service._live_address("[240e::1]:27888") == ("240e::1", 27888)
    assert service._live_address("240e::1") == ("240e::1", 27888)
    assert service._live_address("1.2.3.4:27888") == ("1.2.3.4", 27888)


def test_l1_tcp_success_selects_direct_ipv6_without_relay() -> None:
    result = _direct_ipv6_result()
    assert result.decision.transport == TRANSPORT_DIRECT_IPV6
    assert result.relay is None
    path = path_snapshot(result)
    assert path["transport"] == TRANSPORT_DIRECT_IPV6
    assert str(path["peer_address"]).startswith("240e:")
    assert path["peer_family"] == 6
    assert path["peer_port"] == 27888
    assert path["peer_address"] == V6_PEER
    assert path["relay_used"] is False
    verdict = assert_direct_ipv6(path)
    assert verdict["pass"] is True


def test_l1_tcp_unreachable_stays_within_budget() -> None:
    clock = _Clock()

    def failing_probe(_peer: Sequence[Candidate], _budget: int) -> TcpProbe:
        clock.advance(IPV6_TCP_BUDGET_MS)
        return TcpProbe(ok=False, tried=2, reason=REASON_IPV6_UNREACHABLE, error="timeout")

    def ok_checker(_peer: Sequence[Candidate], _budget: int) -> CheckOutcome:
        return CheckOutcome(sent=10, responses_forward=10, responses_backward=10, rtt_samples_ms=(1.0, 1.0, 1.0))

    result = establish_session(
        prober=lambda _deadline: _v6_probe(),
        collector=lambda _deadline: _local_candidates(),
        checker=ok_checker,
        relay_requester=lambda: {"budget_kbps": 2900},
        peer_candidates=[
            Candidate(kind="host", family=6, address=V6_PEER, port=27888, priority=100),
            Candidate(kind="host", family=4, address="192.168.1.90", port=49160, priority=96),
        ],
        clock=clock,
        tcp_prober=failing_probe,
    )
    level = direct_level(result)
    assert level.ok is False
    assert level.reason == REASON_IPV6_UNREACHABLE
    assert level.duration_ms <= IPV6_TCP_BUDGET_MS
    assert result.decision.transport == TRANSPORT_DIRECT_UDP


def test_l1_skipped_when_no_global_v6() -> None:
    def unused_prober(_peer: Sequence[Candidate], _budget: int) -> TcpProbe:
        raise AssertionError("本机无 v6 时不得进入 TCP 探测")

    result = establish_session(
        prober=lambda _deadline: Ipv6Probe((), (), None, "无全局 IPv6 地址"),
        collector=lambda _deadline: [Candidate(kind="host", family=4, address="192.168.1.80", port=49160, priority=96)],
        checker=lambda _peer, _budget: CheckOutcome(sent=10, responses_forward=10, responses_backward=10),
        relay_requester=lambda: {"budget_kbps": 2900},
        peer_candidates=(),
        tcp_prober=unused_prober,
    )
    level = direct_level(result)
    assert level.reason == REASON_IPV6_NO_GLOBAL
    assert level.duration_ms < 10


def test_l1_reports_disabled_without_tcp_prober() -> None:
    result = establish_session(
        prober=lambda _deadline: _v6_probe(),
        collector=lambda _deadline: _local_candidates(),
        checker=lambda _peer, _budget: CheckOutcome(sent=10, responses_forward=10, responses_backward=10),
        relay_requester=lambda: {"budget_kbps": 2900},
    )
    assert result.direct is not None
    assert result.direct.reason == REASON_IPV6_DISABLED
    assert direct_level(result).duration_ms == 0.0


def test_assert_direct_ipv6_fails_on_relay_path() -> None:
    verdict = assert_direct_ipv6({"transport": TRANSPORT_RELAY, "peer_address": "", "relay_used": True})
    assert verdict["pass"] is False
    assert verdict["transport_direct_ipv6"] is False
    assert verdict["relay_used_false"] is False


def test_path_selected_event_carries_peer_address_and_relay_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("RDC_LOG_DIR", str(log_dir))
    rdc_logging.reset_for_tests()
    try:
        log_path_events("S3SESSION", _direct_ipv6_result())
        lines = (log_dir / "rdc.log").read_text(encoding="utf-8").splitlines()
    finally:
        rdc_logging.reset_for_tests()
    events = [json.loads(line) for line in lines]
    selected = next(item for item in events if item["event"] == "connect.path_selected")
    assert selected["transport"] == TRANSPORT_DIRECT_IPV6
    assert selected["peer_address"] == V6_PEER
    assert selected["relay_used"] is False
    level_event = next(item for item in events if item["event"] == "connect.level_result")
    assert level_event["layer"] == "L1"
    assert level_event["ok"] is True
    assert level_event["duration_ms"] >= 0
    verdict = next(item for item in events if item["event"] == "assert_direct_ipv6")
    assert verdict["assert_direct_ipv6"]["pass"] is True


def test_relay_host_detection() -> None:
    assert is_relay_host("47.109.32.249") is True
    assert is_relay_host("240e:1:2:3::10") is False


class _PortOnlyChecker:
    """只暴露端口与空 socket, 用于验证候选端口接线, 不建真实 socket."""

    def __init__(self) -> None:
        self.ports = {4: 49160, 6: 51234}

    def socket_for(self, _family: int) -> None:
        return None


def test_v6_host_candidate_uses_session_tcp_port(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, int] = {}

    def fake_hosts(v4_port: int = 0, v6_port: int = 0) -> tuple[Candidate, ...]:
        captured["v4"] = v4_port
        captured["v6"] = v6_port
        return ()

    monkeypatch.setattr(runtime_module, "native_host_candidates", fake_hosts)
    assert runtime_module.collect_real_candidates(_PortOnlyChecker(), tcp_port=27888) == ()
    # v4 host 用打洞 socket 的 UDP 端口, v6 host 用命令层传入的会话 TCP 监听端口
    assert captured == {"v4": 49160, "v6": 27888}


def test_attach_udp_plane_reuses_punch_socket() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)

    class _Checker:
        peer = ("127.0.0.1", 40000)

        def __init__(self) -> None:
            self.released: list[int] = []
            self.closed = False

        def release(self, family: int) -> socket.socket:
            self.released.append(family)
            return sock

        def close(self) -> None:
            self.closed = True

    checker = _Checker()
    result = EstablishResult(
        decision=Decision(TRANSPORT_DIRECT_UDP, REASON_OK),
        ipv6=LevelResult("ipv6", True, 12.0, REASON_OK),
        p2p=LevelResult("p2p", True, 900.0, REASON_OK),
    )
    updated = runtime_module.attach_udp_plane(result, checker)
    assert updated.udp_plane is not None
    assert updated.peer == ("127.0.0.1", 40000)
    assert checker.released == [4]
    path = path_snapshot(updated)
    assert path["transport"] == TRANSPORT_DIRECT_UDP
    assert path["peer_address"] == "127.0.0.1"
    assert path["peer_port"] == 40000
    assert path["relay_used"] is False
    # 数据面接管后 checker.close() 不应再关掉已交出的 socket
    checker.close()
    sock.close()


def test_attach_udp_plane_skips_non_punch_path() -> None:
    class _Checker:
        peer = ("127.0.0.1", 40000)

        def __init__(self) -> None:
            self.released: list[int] = []

        def release(self, family: int) -> socket.socket | None:
            self.released.append(family)
            return None

    checker = _Checker()
    result = EstablishResult(
        decision=Decision(TRANSPORT_RELAY, REASON_OK),
        ipv6=LevelResult("ipv6", True, 12.0, REASON_OK),
        p2p=LevelResult("p2p", True, 900.0, REASON_OK),
    )
    updated = runtime_module.attach_udp_plane(result, checker)
    assert updated.udp_plane is None
    assert updated.peer is None
    assert checker.released == []


def test_punch_responder_replies_and_hands_socket_over() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.setblocking(False)
    try:
        responder = PunchResponder(server)
        client.sendto(encode_probe("0011223344556677", b"Q"), server.getsockname())
        assert responder.poll(0.5) == 1
        reply, _addr = client.recvfrom(2048)
        assert decode_probe(reply) == ("R", "0011223344556677")
        assert responder.responsive is True
        assert responder.peer == client.getsockname()

        client.sendto(b"not-a-probe", server.getsockname())
        assert responder.poll(0.5) == 0
        assert responder.ignored == 1

        assert responder.release() is server
        assert responder.poll(0.5) == 0
        responder.close()
    finally:
        client.close()
        server.close()


def test_punch_responder_probes_peer_candidates() -> None:
    """被控端必须主动发 Q, 控制端才能看到反向包; 收到的 R 只记 forward, 不计 ignored."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    phone.setblocking(False)
    try:
        responder = PunchResponder(server)
        responder.set_targets([phone.getsockname()])
        assert responder.targets == (phone.getsockname(),)
        assert responder.poll(0.0) == 0
        assert responder.sent == 1

        probe, _addr = phone.recvfrom(2048)
        parsed = decode_probe(probe)
        assert parsed is not None
        assert parsed[0] == "Q"
        phone.sendto(encode_probe(parsed[1], b"R"), server.getsockname())
        assert responder.poll(0.2) == 0
        assert responder.forward == 1
        assert responder.ignored == 0
        assert responder.peer == phone.getsockname()

        responder.close()
    finally:
        phone.close()
        server.close()


def test_punch_responder_does_not_probe_without_targets() -> None:
    """没有控制端候选时不能凭空发包, 只保持应答."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.setblocking(False)
    try:
        responder = PunchResponder(server)
        assert responder.poll(0.0) == 0
        assert responder.sent == 0
        assert responder.targets == ()
        responder.close()
    finally:
        server.close()


def test_punch_responder_probes_ipv4_target_from_dual_stack_socket() -> None:
    """被控端双栈 socket 必须能向 IPv4 控制端候选主动发包, 不能被地址族挡住."""
    server = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    server.bind(("::", 0))
    server.setblocking(False)
    phone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone.bind(("127.0.0.1", 0))
    phone.setblocking(False)
    try:
        responder = PunchResponder(server)
        responder.set_targets([phone.getsockname()])
        assert responder.poll(0.0) == 0
        assert responder.sent == 1
        probe, _addr = phone.recvfrom(2048)
        parsed = decode_probe(probe)
        assert parsed is not None and parsed[0] == "Q"
        responder.close()
    finally:
        phone.close()
        server.close()


def test_stun_query_uses_v4_mapped_endpoint_on_dual_stack_socket() -> None:
    """被控端双栈 socket 必须能把 IPv4 STUN 服务转成 v4-mapped 发送并收到映射结果."""
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    client = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    client.bind(("::", 0))

    def serve() -> None:
        request, addr = server.recvfrom(2048)
        server.sendto(_binding_response(request[8:20], "127.0.0.1", int(addr[1])), addr)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        address, port, _rtt = query_server(client, server.getsockname(), timeout_ms=1500)
        assert address == "127.0.0.1"
        assert port == int(client.getsockname()[1])
    finally:
        worker.join(timeout=2.0)
        client.close()
        server.close()
