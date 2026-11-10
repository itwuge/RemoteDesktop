"""被控服务双栈监听回归: 同一端口同时接受 IPv4 与 IPv6, 来源白名单口径不变."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import rdc.relay.server as relay_module
import rdc.session.host as host_module
from rdc.commands.service import CommandService, parse_endpoint
from rdc.connect.policy import Candidate
from rdc.connect.punch import PunchResponder, decode_probe, encode_probe
from rdc.crypto.identity import DeviceIdentity
from rdc.media.frame import VERSION_V2
from rdc.relay.server import (
    RelayConfig,
    RelayServer,
    create_listener,
    ip_matches,
    normalize_peer,
    parse_ip_networks,
    send_frame,
)
from rdc.session import lan
from rdc.session.host import (
    ANSWER_CANDIDATE_BUDGET_MS,
    HostConfig,
    HostService,
    create_punch_socket,
    lan_allow_list,
)
from rdc.session.lan import local_ipv6_networks


def _ipv6_available() -> bool:
    """当前环境是否具备 IPv6 套接字能力."""
    if not socket.has_ipv6:
        return False
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return False
    probe.close()
    return True


def _free_port() -> int:
    """取一个当前空闲的端口, 由内核分配后立即释放."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _free_udp_port() -> int:
    """取一个当前空闲的 UDP 端口, 由内核分配后立即释放."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    """轮询等待条件成立; 超时返回 False."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _connect_once(family: socket.AddressFamily, host: str, port: int) -> None:
    """建立一次 TCP 连接后立即关闭; 连不上直接抛错."""
    with socket.socket(family, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect((host, port))


def _config(port: int, extra_ports: tuple[int, ...] = (), open_ports: tuple[int, ...] = ()) -> RelayConfig:
    """测试用中继配置: 空监听地址走双栈, 白名单只放行两侧回环."""
    return RelayConfig(
        bind="",
        port=port,
        extra_ports=extra_ports,
        open_ports=open_ports,
        health_bind="127.0.0.1",
        health_port=0,
        relay_endpoint=f"relay://127.0.0.1:{port}",
        egress_mbps=3,
        overhead_ratio=0.08,
        max_sessions=2,
        degrade_profile="L0",
        allow_ips="127.0.0.1/32,::1/128",
        log_level="WARNING",
    )


def _probe_ready(port: int) -> bool:
    """探测中继是否已开始监听; 未就绪时吞掉连接错误."""
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
    except OSError:
        return False
    return True


def test_dual_stack_listener_accepts_ipv4_and_ipv6():
    """同一监听套接字同时接受 IPv4 与 IPv6; IPv4 映射地址必须被白名单放行."""
    if not _ipv6_available():
        pytest.skip("当前环境不支持 IPv6, 双栈监听无法验证")
    port = _free_port()
    server = RelayServer(_config(port))
    thread = threading.Thread(target=server.serve_forever, name="relay-dual-stack-test", daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: _probe_ready(port), 10.0), "中继未在 10 秒内就绪"
        _connect_once(socket.AF_INET, "127.0.0.1", port)
        _connect_once(socket.AF_INET6, "::1", port)
        assert _wait_until(lambda: server.guard.snapshot()["active"] == 0, 5.0)
        refused = server.guard.snapshot()["rejected"]
        assert refused["denied"] == 0
        assert refused["rate_limited"] == 0
    finally:
        server.stop()
        thread.join(timeout=5.0)


def test_normalize_peer_restores_ipv4_and_strips_zone():
    """双栈监听看到的 IPv4 映射地址与带区号的链路本地地址要归一."""
    assert normalize_peer("::ffff:192.168.1.5") == "192.168.1.5"
    assert normalize_peer("::ffff:127.0.0.1") == "127.0.0.1"
    assert normalize_peer("::1") == "::1"
    assert normalize_peer("fe80::1%12") == "fe80::1"
    assert normalize_peer("240e:370:6303:cb0::1") == "240e:370:6303:cb0::1"


def test_lan_allow_list_covers_ipv6_loopback_and_link_local():
    """回环与链路本地控制端必须命中来源白名单, 不能因双栈改动被拒绝."""
    networks = parse_ip_networks(lan_allow_list(), "allow_ips")
    assert ip_matches("127.0.0.1", networks)
    assert ip_matches("::1", networks)
    assert ip_matches("fe80::1234", networks)


def test_local_ipv6_networks_are_ipv6_cidrs():
    """本机 IPv6 网段枚举只返回非回环的 IPv6 CIDR."""
    for item in local_ipv6_networks():
        network = ipaddress.ip_network(item)
        assert network.version == 6
        assert not network.is_loopback


def test_linux_if_inet6_parser_keeps_global_prefix_only():
    """Linux 路径: /proc/net/if_inet6 只保留全局地址, 回环与链路本地单列."""
    content = (
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "fe800000000000000000000000000001 02 40 20 80     eth0\n"
        "240e037063030cb00000000000000001 02 40 00 00     eth0\n"
        "240e037063030cb08c94c8d0eb5b0cc5 02 40 00 00     eth0\n"
        "240e0370630b1234aaaaaaaaaaaaaaaa 03 40 00 00    ens33\n"
    )
    assert lan._parse_if_inet6(content) == ["240e:370:6303:cb0::/64", "240e:370:630b:1234::/64"]


def test_windows_ipv6_networks_group_global_address_by_64(monkeypatch):
    """Windows 路径: 派发层给出的全局地址按前缀归网, 映射与链路本地剔除."""
    entries = [
        (ipaddress.IPv6Address("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5"), 64, True),
        (ipaddress.IPv6Address("240e:370:6303:cb0::6d1f"), 64, False),
    ]
    monkeypatch.setattr(lan, "_windows_ipv6_entries", lambda: entries)
    assert lan._windows_ipv6_networks() == ["240e:370:6303:cb0::/64"]


def test_hostname_entries_fallback_drops_loopback_and_link_local(monkeypatch):
    """GetAdaptersAddresses 不可用时退化为主机名解析, 回环与链路本地仍被剔除."""

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%12", 0, 0, 12)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:192.168.1.5", 0, 0, 0)),
        ]

    monkeypatch.setattr(lan.socket, "getaddrinfo", fake_getaddrinfo)
    assert [(str(address), temporary) for address, _prefix, temporary in lan._hostname_ipv6_entries()] == [
        ("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5", False)
    ]


def test_windows_entries_prefer_adapters_api_over_hostname(monkeypatch):
    """GetAdaptersAddresses 有结果时不走主机名解析, 临时标志原样透传."""
    api_entries = [(ipaddress.IPv6Address("240e:370:6303:cb0::6d1f"), 64, True)]

    def fail_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise AssertionError("有结构化地址时不应回退到主机名解析")

    monkeypatch.setattr(lan, "_windows_ipv6_unicast", lambda: api_entries)
    monkeypatch.setattr(lan.socket, "getaddrinfo", fail_getaddrinfo)
    assert lan._windows_ipv6_entries() == api_entries


def test_local_ipv6_addresses_put_stable_before_temporary(monkeypatch):
    """对外 IPv6 地址稳定在前、临时在后, 各自去重排序; 回环等不参与."""
    entries = [
        (ipaddress.IPv6Address("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5"), 128, True),
        (ipaddress.IPv6Address("240e:370:6303:cb0::6d1f"), 64, False),
        (ipaddress.IPv6Address("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5"), 128, True),
    ]
    monkeypatch.setattr(lan, "_linux_ipv6_entries", lambda: entries)
    monkeypatch.setattr(lan, "_windows_ipv6_entries", lambda: entries)
    assert lan.ipv6_address_groups() == (
        ["240e:370:6303:cb0::6d1f"],
        ["240e:370:6303:cb0:8c94:c8d0:eb5b:cc5"],
    )
    assert lan.local_ipv6_addresses() == [
        "240e:370:6303:cb0::6d1f",
        "240e:370:6303:cb0:8c94:c8d0:eb5b:cc5",
    ]


def test_linux_if_inet6_flags_mark_temporary_address():
    """Linux 路径: 第五列标志位 0x01 判为 RFC 4941 临时地址."""
    content = (
        "240e037063030cb08c94c8d0eb5b0cc5 02 40 00 00     eth0\n240e037063030cb06d1f0e961212c57b 02 40 00 01     eth0\n"
    )
    assert [(str(address), temporary) for address, _prefix, temporary in lan._if_inet6_entries(content)] == [
        ("240e:370:6303:cb0:8c94:c8d0:eb5b:cc5", False),
        ("240e:370:6303:cb0:6d1f:e96:1212:c57b", True),
    ]


def test_local_ipv4_addresses_skip_loopback_and_link_local(monkeypatch):
    """对外 IPv4 地址不含 127/8 回环与 169.254/16 链路本地."""
    pairs = [
        ("127.0.0.1", "255.0.0.0"),
        ("192.168.1.227", "255.255.255.0"),
        ("169.254.10.2", "255.255.0.0"),
    ]
    monkeypatch.setattr(lan, "_linux_interfaces", lambda: pairs)
    monkeypatch.setattr(lan, "_windows_interfaces", lambda: pairs)
    assert lan.local_ipv4_addresses() == ["192.168.1.227"]


def test_hosting_info_exposes_addresses_and_keeps_address(monkeypatch):
    """被控信息: addresses 首位是稳定 IPv6, 临时地址在最后, address 语义不变."""
    monkeypatch.setattr(
        lan,
        "ipv6_address_groups",
        lambda: (["240e:370:6303:cb0::6d1f"], ["240e:370:6303:cb0:8c94:c8d0:eb5b:cc5"]),
    )
    monkeypatch.setattr(lan, "local_ipv4_addresses", lambda: ["127.0.0.1", "192.168.1.227"])
    service = CommandService()
    started = service.start_hosting("本机")
    try:
        info = service.get_hosting_info()
        assert info["address"] == "127.0.0.1"
        assert info["addresses"] == [
            "240e:370:6303:cb0::6d1f",
            "127.0.0.1",
            "192.168.1.227",
            "240e:370:6303:cb0:8c94:c8d0:eb5b:cc5",
        ]
        assert started.data["address"] == info["address"]
        assert started.data["port"] == info["port"]
    finally:
        service.stop_hosting()


def test_create_listener_rejects_second_binder():
    """监听端口独占: 第二个监听即便设置 SO_REUSEADDR 也拿不到同一端口.

    Windows 的 SO_REUSEADDR 允许重复绑定, 旧实现下一个新实例能静默抢走连接;
    改成独占后第二个绑定必须以 WSAEADDRINUSE(EADDRINUSE) 失败.
    """
    port = _free_port()
    listener = create_listener("", port, 8)
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as second:
            second.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            second.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            with pytest.raises(OSError):
                second.bind(("::", port))
    finally:
        listener.close()


def test_create_listener_allows_rebind_after_close():
    """独占不能妨碍重启: 关闭旧监听后立刻在同一端口重新绑定必须成功."""
    port = _free_port()
    create_listener("", port, 8).close()
    listener = create_listener("", port, 8)
    try:
        assert listener.getsockname()[1] == port
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("127.0.0.1:27888", ("127.0.0.1", 27888)),
        ("[240e:370:6303:cb0::2e5d]:27888", ("240e:370:6303:cb0::2e5d", 27888)),
        ("240e:370:6303:cb0::2e5d:27888", ("240e:370:6303:cb0::2e5d", 27888)),
        ("::1", ("::1", 27888)),
        ("[::1]", ("::1", 27888)),
        ("", None),
        ("bad", None),
        ("host:0", None),
        ("[::1]:70000", None),
    ],
)
def test_parse_endpoint_supports_ipv6_literals(endpoint: str, expected: tuple[str, int] | None):
    """控制端地址解析要接受 IPv6 字面量, 非法输入仍回退默认地址."""
    assert parse_endpoint(endpoint) == expected


def test_create_punch_socket_binds_dual_stack_udp():
    """打洞 socket 与会话同端口、双栈绑定: 地址族为 AF_INET6, V6ONLY 关闭."""
    sock = create_punch_socket("", 0)
    try:
        assert sock.family == socket.AF_INET6
        assert sock.getsockname()[0] == "::"
        assert sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 0
    finally:
        sock.close()


def test_host_punch_responder_lifecycle_binds_and_releases():
    """应答器随 HostService 生命周期: 起后监听同端口并应答, 停止后端口完整释放."""
    port = _free_udp_port()
    service = HostService(HostConfig(port=port, access_code="284617"), DeviceIdentity.generate())
    service._start_punch_responder()
    try:
        assert service.status()["punch"]["listening"] is True
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("127.0.0.1", 0))
            client.settimeout(2.0)
            client.sendto(encode_probe("aabbccddeeff0011", b"Q"), ("127.0.0.1", port))
            reply, _addr = client.recvfrom(2048)
        assert decode_probe(reply) == ("R", "aabbccddeeff0011")
        assert _wait_until(lambda: service.punch_stats()["replies"] >= 1)
        assert service.punch_stats()["ignored"] == 0
    finally:
        service.stop()
    assert service.punch is None
    assert service.udp_plane is None
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
        probe.bind(("::", port))


def test_host_punch_responder_replies_and_hands_over_same_socket():
    """被控端应答 RDCP1 后把同一个 socket 交给数据面, 交接后不再持有."""
    service = HostService(HostConfig(port=27888, access_code="284617"), DeviceIdentity.generate())
    sock = create_punch_socket("", 0)
    port = int(sock.getsockname()[1])
    responder = PunchResponder(sock)
    service.punch = responder
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.bind(("127.0.0.1", 0))
            client.settimeout(2.0)
            client_port = int(client.getsockname()[1])
            client.sendto(encode_probe("0011223344556677", b"Q"), ("127.0.0.1", port))
            assert responder.poll(1.0) == 1
            reply, _addr = client.recvfrom(2048)
        assert decode_probe(reply) == ("R", "0011223344556677")
        stats = service.punch_stats()
        assert stats["replies"] == 1
        assert stats["plane"] is False
        assert str(stats["peer"]).endswith(f":{client_port}")

        plane = service.release_punch_plane()
        assert plane is not None
        assert service.release_punch_plane() is plane
        assert service.punch_stats()["plane"] is True
        assert responder.release() is None
        # 同一个 socket 被数据面 connect 到探测来源, 本地端口保持不变
        peer_host, peer_port = sock.getpeername()[:2]
        assert str(peer_host).endswith("127.0.0.1")
        assert int(peer_port) == client_port
        assert int(sock.getsockname()[1]) == port
    finally:
        service.stop()


def test_answer_candidates_cover_hosts_srflx_and_relay(monkeypatch):
    """answer 候选必须含 v6 host(会话端口)、v4 host(打洞端口)、srflx 与 relay."""
    service = HostService(HostConfig(port=27888, access_code="284617"), DeviceIdentity.generate())
    sock = create_punch_socket("", 0)
    udp_port = int(sock.getsockname()[1])
    service.punch = PunchResponder(sock)
    hosts = (
        Candidate(
            kind="host",
            family=6,
            address="240e:370:6303:cb0::5",
            port=27888,
            priority=100,
            scope="global",
            origin="host",
            proto="tcp",
        ),
        Candidate(
            kind="host",
            family=4,
            address="192.168.1.80",
            port=udp_port,
            priority=96,
            scope="private",
            origin="host",
            proto="udp",
        ),
    )
    srflx_items = (Candidate(kind="srflx", family=4, address="115.150.84.112", port=udp_port, priority=90),)
    captured: dict[str, object] = {}

    def fake_native(v4_port: int = 0, v6_port: int = 0) -> tuple[Candidate, ...]:
        captured["ports"] = (v4_port, v6_port)
        return hosts

    def fake_srflx(sock_arg: socket.socket, total_ms: int = 0) -> tuple[tuple[Candidate, ...], str]:
        assert sock_arg is sock
        captured["total_ms"] = total_ms
        return srflx_items, ""

    monkeypatch.setattr(host_module, "native_host_candidates", fake_native)
    monkeypatch.setattr(host_module, "discover_srflx", fake_srflx)
    try:
        items = service._answer_candidates()
    finally:
        service.stop()

    assert captured["ports"] == (udp_port, 27888)
    assert captured["total_ms"] == ANSWER_CANDIDATE_BUDGET_MS
    endpoints = {(item.kind, item.family, item.address, item.port) for item in items}
    assert ("host", 6, "240e:370:6303:cb0::5", 27888) in endpoints
    assert ("host", 4, "192.168.1.80", udp_port) in endpoints
    assert ("srflx", 4, "115.150.84.112", udp_port) in endpoints
    relay = [item for item in items if item.kind == "relay"]
    assert len(relay) == 1
    assert (relay[0].priority, relay[0].port, relay[0].origin, relay[0].proto) == (10, 27888, "relay", "tcp")


def test_answer_payload_keeps_chosen_candidate_and_adds_full_fields():
    """answer 保留 chosen_candidate(relay), 同时下发带全字段的 candidates."""
    service = HostService(HostConfig(port=27888, access_code="284617"), DeviceIdentity.generate())
    sent: list[tuple[str, dict[str, object]]] = []

    def capture(msg_type: str, payload: dict[str, object], session_id: str | None = None) -> None:
        sent.append((msg_type, payload))

    service._send = capture
    candidates = (
        Candidate(kind="host", family=6, address="240e:370:6303:cb0::5", port=27888, priority=100),
        Candidate(kind="host", family=4, address="192.168.1.80", port=27888, priority=96),
        Candidate(kind="srflx", family=4, address="115.150.84.112", port=27888, priority=90),
        Candidate(kind="relay", family=4, address="192.168.1.80", port=27888, priority=10),
    )
    try:
        service._answer("S1", "PEER", True, "", VERSION_V2, candidates)
    finally:
        service.stop()

    assert sent[0][0] == "answer"
    payload = sent[0][1]
    assert payload["chosen_candidate"] == {
        "kind": "relay",
        "address": host_module.local_ip(),
        "port": 27888,
    }
    rows = payload["candidates"]
    assert isinstance(rows, list)
    assert len(rows) == 4
    required = {"kind", "address", "port", "priority", "family", "scope", "origin", "related", "proto"}
    for row in rows:
        assert isinstance(row, dict)
        assert required <= set(row)
        assert row["kind"] != "mapped"
    assert {row["kind"] for row in rows} == {"host", "srflx", "relay"}


def test_peer_candidates_filter_link_local_and_hairpin():
    """offer 候选里链路本地与同出口 hairpin 必须剔除, 只留可打洞的 IPv4 host/srflx."""
    service = HostService(HostConfig(port=27888, access_code="284617"), DeviceIdentity.generate())
    local = [Candidate(kind="srflx", family=4, address="115.150.84.112", port=27888, priority=90)]
    payload = {
        "candidates": [
            {"kind": "srflx", "address": "115.150.84.112", "port": 50000, "priority": 90},
            {"kind": "host", "address": "fe80::10", "port": 50001, "priority": 100, "family": 6},
            {"kind": "host", "address": "169.254.1.2", "port": 50002, "priority": 96},
            {"kind": "host", "address": "192.168.1.90", "port": 50003, "priority": 96},
            {"kind": "host", "address": "240e:370:6303:cb0::9", "port": 50004, "priority": 100, "family": 6},
            {"kind": "relay", "address": "47.109.32.249", "port": 8750, "priority": 10},
        ]
    }
    try:
        targets = service._peer_candidates(payload, local)
    finally:
        service.stop()

    assert [(item.address, item.port) for item in targets] == [("192.168.1.90", 50003)]


# ------------------------------------------------------------------ 额外监听端口


def _hosts() -> tuple[int, int, int]:
    """取三个互不相同的空闲端口, 依次用作主端口、额外端口、占用端口."""
    ports: list[int] = []
    while len(ports) < 3:
        port = _free_port()
        if port not in ports:
            ports.append(port)
    return ports[0], ports[1], ports[2]


def _global_v6() -> str:
    """取一个本机全局 IPv6 地址; 没有则跳过, 该环境无法验证按地址绑定."""
    addresses = relay_module.local_global_ipv6_addresses()
    if not addresses:
        pytest.skip("当前环境没有全局 IPv6 地址, 无法验证额外端口按地址绑定")
    return addresses[0]


def test_relay_extra_ports_share_the_same_accept_logic():
    """额外端口逐个绑全局 IPv6 地址: 该地址可接管直连, 回环不再可达."""
    if not _ipv6_available():
        pytest.skip("当前环境不支持 IPv6, 双栈监听无法验证")
    address = _global_v6()
    main, extra, _unused = _hosts()
    server = RelayServer(_config(main, extra_ports=(extra,), open_ports=(extra,)))
    received: dict[str, dict[str, object]] = {}

    def handler(session_id: str):
        def accept(connection: socket.socket, hello: dict[str, object]) -> bool:
            received[session_id] = {"hello": hello, "sock": connection}
            return True

        return accept

    server.register_direct("s-main", handler("s-main"))
    server.register_direct("s-extra", handler("s-extra"))
    thread = threading.Thread(target=server.serve_forever, name="relay-extra-port-test", daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: server.listen_ports == (main, extra), 10.0), "额外端口未在 10 秒内监听"
        for host, port, session_id in (("127.0.0.1", main, "s-main"), (address, extra, "s-extra")):
            with socket.create_connection((host, port), 5.0) as client:
                send_frame(client, {"kind": "e2e_hello", "session_id": session_id, "device_id": "d-1"})
                assert _wait_until(lambda sid=session_id: sid in received, 5.0), f"{port} 端口未接管直连"
            assert received[session_id]["hello"]["session_id"] == session_id
            # 接管成功: 服务端不关闭该 socket, 所有权已转移
            assert received[session_id]["sock"].fileno() != -1
        # 额外端口不绑通配与回环, 从 ::1 连不上
        with pytest.raises(OSError):
            _connect_once(socket.AF_INET6, "::1", extra)
    finally:
        for item in received.values():
            item["sock"].close()
        server.stop()
        thread.join(timeout=5.0)


def test_extra_listen_port_conflict_degrades_to_main(monkeypatch):
    """额外端口在这些全局地址上全被占用时只降级该端口, 主端口照常监听."""
    if not _ipv6_available():
        pytest.skip("当前环境不支持 IPv6, 双栈监听无法验证")
    _global_v6()
    main, extra, _unused = _hosts()
    blockers: list[socket.socket] = []
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        relay_module,
        "log_event",
        lambda _module, event, **fields: events.append((event, fields)),
    )
    for item in relay_module.local_global_ipv6_addresses():
        blockers.append(create_listener(item, extra, 8))
    server = RelayServer(_config(main, extra_ports=(extra,)))
    thread = threading.Thread(target=server.serve_forever, name="relay-degrade-test", daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: bool(server.listen_ports), 10.0), "主端口未在 10 秒内监听"
        assert server.listen_ports == (main,)
        # 主端口不受额外端口失败影响
        assert _wait_until(lambda: _probe_ready(main), 5.0)
    finally:
        server.stop()
        thread.join(timeout=5.0)
        for blocker in blockers:
            blocker.close()

    failed = [fields for event, fields in events if event == "listen.extra_failed"]
    assert failed and failed[0]["port"] == extra
    assert failed[0]["level"] == "WARNING"


def test_extra_port_rebinds_when_local_address_appears(monkeypatch):
    """本机全局地址出现后, 额外端口在刷新周期内自动补绑."""
    if not _ipv6_available():
        pytest.skip("当前环境不支持 IPv6, 双栈监听无法验证")
    address = _global_v6()
    main, extra, _unused = _hosts()
    monkeypatch.setattr(relay_module, "EXTRA_PORT_REFRESH_S", 0.2)
    state = {"addresses": []}
    server = RelayServer(
        _config(main, extra_ports=(extra,), open_ports=(extra,)),
        extra_bind_addresses=lambda: list(state["addresses"]),
    )
    thread = threading.Thread(target=server.serve_forever, name="relay-extra-rebind-test", daemon=True)
    thread.start()
    try:
        assert _wait_until(lambda: server.listen_ports == (main,), 10.0)
        state["addresses"] = [address]
        assert _wait_until(lambda: server.listen_ports == (main, extra), 10.0), "地址出现后未重绑"
        with socket.create_connection((address, extra), 5.0):
            pass
    finally:
        server.stop()
        thread.join(timeout=5.0)


def test_answer_candidates_expand_one_v6_host_per_listen_port(monkeypatch):
    """answer 的 v6 host 候选按实际监听端口各出一条; relay 候选仍只有主端口一条."""
    service = HostService(HostConfig(port=27888, access_code="284617"), DeviceIdentity.generate())
    service.server = SimpleNamespace(listen_ports=(27888, 443))  # 类型: RelayServer, 这里只取端口

    def fake_native(v4_port: int = 0, v6_port: int = 0) -> tuple[Candidate, ...]:
        items = [
            Candidate(kind="host", family=6, address="240e:370:6303:cb0::5", port=v6_port, priority=100),
        ]
        if v4_port:
            items.append(Candidate(kind="host", family=4, address="192.168.1.80", port=v4_port, priority=96))
        return tuple(items)

    monkeypatch.setattr(host_module, "native_host_candidates", fake_native)
    items = service._answer_candidates()

    v6_ports = sorted(item.port for item in items if item.kind == "host" and item.family == 6)
    assert v6_ports == [443, 27888]
    relay = [item for item in items if item.kind == "relay"]
    assert len(relay) == 1
    assert relay[0].port == 27888
