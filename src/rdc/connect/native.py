# ==========================================================================================
# 文件        : native.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 平台原生能力: 本机地址枚举(Windows GetAdaptersAddresses)、IPv6 自检、host 候选
# 架构定位    : 连接策略的“真实 prober / collector”实现层, 只做系统查询与探测, 不做决策
# 核心功能    : 枚举在用网卡单播地址; 全局 IPv6 判定; 默认路由与真实外连探测; host 候选生成
# 职责边界    : 不读契约、不发业务信令; 非 Windows 平台退化为 getaddrinfo 枚举
# 运行环境    : Python 3.12, Windows(iphlpapi), macOS/Linux 退化实现
# 依赖关系    : 被 src/rdc/connect/runtime.py 调用; 返回 policy.Candidate / policy.Ipv6Probe
# ==========================================================================================

"""真实网络探测与地址枚举.

Windows 分支直接调用 iphlpapi!GetAdaptersAddresses, 与 netsh / ipconfig 同源;
不用第三方库, 不读取注册表. 其他平台回退到 socket.getaddrinfo(hostname).
"""

from __future__ import annotations

import ctypes
import ipaddress
import socket
import sys
import time

from rdc.connect.policy import (
    FAMILY_V4,
    FAMILY_V6,
    IPV6_BUDGET_MS,
    IPV6_PROBE_TARGETS,
    KIND_HOST,
    ORIGIN_HOST,
    PRIORITY_HOST_V4_PRIVATE,
    PRIORITY_HOST_V4_PUBLIC,
    PRIORITY_IPV6_GLOBAL,
    PRIORITY_IPV6_ULA,
    PROTO_TCP,
    PROTO_UDP,
    SCOPE_GLOBAL,
    SCOPE_PRIVATE,
    SCOPE_ULA,
    Candidate,
    Ipv6Probe,
    scope_of,
)

AF_UNSPEC = 0
AF_INET = 2
AF_INET6 = 23
GAA_FLAG_SKIP_ANYCAST = 0x02
GAA_FLAG_SKIP_MULTICAST = 0x04
GAA_FLAG_SKIP_DNS_SERVER = 0x08
ERROR_BUFFER_OVERFLOW = 111
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_OPER_STATUS_UP = 1


class _Sockaddr(ctypes.Structure):
    """SOCKET_ADDRESS, 只读取指针与长度."""

    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _UnicastAddress(ctypes.Structure):
    """IP_ADAPTER_UNICAST_ADDRESS_LH, 只需 Next 与 Address."""

    pass


_UnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _Sockaddr),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _AdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES_LH, 读取到 OperStatus 即可."""

    pass


_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_void_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_void_p),
    ("Description", ctypes.c_void_p),
    ("FriendlyName", ctypes.c_void_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
]


def _sockaddr_to_text(raw: bytes) -> tuple[int, str] | None:
    """把 SOCKADDR 原始字节转成 (family 4/6, 地址文本)."""
    if len(raw) < 8:
        return None
    family = int.from_bytes(raw[0:2], "little")
    if family == AF_INET:
        return 4, socket.inet_ntop(socket.AF_INET, raw[4:8])
    if family == AF_INET6 and len(raw) >= 24:
        return 6, socket.inet_ntop(socket.AF_INET6, raw[8:24])
    return None


def _windows_unicast_addresses() -> list[tuple[int, str]]:
    """调用 GetAdaptersAddresses 枚举在用网卡的单播地址."""
    iphlpapi = ctypes.WinDLL("iphlpapi.dll")
    get_adapters = iphlpapi.GetAdaptersAddresses
    get_adapters.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    get_adapters.restype = ctypes.c_ulong

    flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER
    size = ctypes.c_ulong(16 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    code = get_adapters(AF_UNSPEC, flags, None, buffer, ctypes.byref(size))
    if code == ERROR_BUFFER_OVERFLOW:
        buffer = ctypes.create_string_buffer(size.value)
        code = get_adapters(AF_UNSPEC, flags, None, buffer, ctypes.byref(size))
    if code != 0:
        raise OSError(code, f"GetAdaptersAddresses 失败: {code}")

    results: list[tuple[int, str]] = []
    adapter = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    while adapter:
        item = adapter.contents
        if item.OperStatus == IF_OPER_STATUS_UP and item.IfType != IF_TYPE_SOFTWARE_LOOPBACK:
            unicast = item.FirstUnicastAddress
            while unicast:
                entry = unicast.contents
                if entry.Address.lpSockaddr:
                    raw = ctypes.string_at(entry.Address.lpSockaddr, entry.Address.iSockaddrLength)
                    parsed = _sockaddr_to_text(raw)
                    if parsed is not None:
                        results.append(parsed)
                unicast = entry.Next
        adapter = item.Next
    return results


def _fallback_unicast_addresses() -> list[tuple[int, str]]:
    """非 Windows 或 API 不可用时, 用 getaddrinfo 枚举本机地址."""
    results: list[tuple[int, str]] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), 0, socket.AF_UNSPEC, socket.SOCK_DGRAM)
    except OSError:
        return results
    for info in infos:
        family = 6 if info[0] == socket.AF_INET6 else 4
        address = str(info[4][0])
        results.append((family, address))
    return results


def enumerate_unicast_addresses() -> tuple[tuple[int, str], ...]:
    """返回 (family 4/6, 地址文本) 去重元组; 失败返回空元组而不是抛错."""
    try:
        raw = _windows_unicast_addresses() if sys.platform == "win32" else _fallback_unicast_addresses()
    except OSError:
        return ()
    seen: set[tuple[int, str]] = set()
    ordered: list[tuple[int, str]] = []
    for family, address in raw:
        key = (family, address)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return tuple(ordered)


def is_global_v6(address: str) -> bool:
    """是否全局单播 IPv6: 排除回环、链路本地、ULA、组播与未指定地址."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 6 and ip.is_global


def _route_ready(probe_target: str, timeout_s: float) -> bool:
    """用 UDP connect 验证内核存在到目标的 IPv6 路由(不发包)."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.settimeout(max(0.05, timeout_s))
            sock.connect((probe_target, 443))
    except OSError:
        return False
    return True


def native_ipv6_probe(deadline_ms: int = IPV6_BUDGET_MS) -> Ipv6Probe:
    """真实 IPv6 自检: 枚举全局地址 -> 验证路由 -> 逐个目标 TCP 建连."""
    deadline = time.monotonic() + max(0.05, deadline_ms / 1000.0)
    addresses = tuple(address for family, address in enumerate_unicast_addresses() if family == 6)
    globals_ = tuple(address for address in addresses if is_global_v6(address))
    if not globals_:
        return Ipv6Probe(global_addresses=(), reachable_targets=(), rtt_ms=None, error="无全局 IPv6 地址")

    reachable: list[str] = []
    best_rtt: float | None = None
    last_error = ""
    for target in IPV6_PROBE_TARGETS:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_error = "IPv6 探测预算耗尽"
            break
        if not _route_ready(target, min(remaining, IPV6_BUDGET_MS / 1000.0)):
            last_error = f"无到 {target} 的 IPv6 路由"
            continue
        started = time.monotonic()
        try:
            with socket.create_connection((target, 443), timeout=min(remaining, IPV6_BUDGET_MS / 1000.0)):
                elapsed = (time.monotonic() - started) * 1000.0
                reachable.append(target)
                best_rtt = elapsed if best_rtt is None else min(best_rtt, elapsed)
        except OSError as exc:
            last_error = f"{target}: {exc}"
    if not reachable:
        return Ipv6Probe(global_addresses=globals_, reachable_targets=(), rtt_ms=None, error=last_error)
    return Ipv6Probe(global_addresses=globals_, reachable_targets=tuple(reachable), rtt_ms=best_rtt)


def native_host_candidates(v4_port: int = 0, v6_port: int = 0) -> tuple[Candidate, ...]:
    """生成 host 候选: IPv6 收全局与 ULA 地址, IPv4 排除回环与链路本地.

    优先级与作用域按 spec ipv6-punch §3.2: v6 全局 100, v4 私网 96, v4 公网 85, v6 ULA 70.
    """
    items: list[Candidate] = []
    for _family, address in enumerate_unicast_addresses():
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_multicast:
            continue
        if ip.version == 6:
            scope = scope_of(address)
            if scope not in {SCOPE_GLOBAL, SCOPE_ULA} or v6_port <= 0:
                continue
            priority = PRIORITY_IPV6_GLOBAL if scope == SCOPE_GLOBAL else PRIORITY_IPV6_ULA
            items.append(
                Candidate(
                    kind=KIND_HOST,
                    family=FAMILY_V6,
                    address=address,
                    port=v6_port,
                    priority=priority,
                    scope=scope,
                    origin=ORIGIN_HOST,
                    proto=PROTO_TCP,
                )
            )
        else:
            if v4_port <= 0:
                continue
            private = ip.is_private
            items.append(
                Candidate(
                    kind=KIND_HOST,
                    family=FAMILY_V4,
                    address=address,
                    port=v4_port,
                    priority=PRIORITY_HOST_V4_PRIVATE if private else PRIORITY_HOST_V4_PUBLIC,
                    scope=SCOPE_PRIVATE if private else "",
                    origin=ORIGIN_HOST,
                    proto=PROTO_UDP,
                )
            )
    return tuple(items)
