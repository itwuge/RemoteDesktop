# ==========================================================================================
# 文件        : lan.py
# 所属目录    : src/rdc/session/
# 文件用途    : 枚举本机全部 IPv4/IPv6 网卡与网段, 供被控端中继来源白名单使用
# 架构定位    : 会话层的平台小工具; 只做只读查询, 不绑定端口、不改路由
# 核心功能    : local_ipv4_networks()/local_ipv6_networks() 返回去重后的 CIDR 列表;
#               local_ipv4_addresses()/local_ipv6_addresses() 返回可对外公布的地址
# 职责边界    : 不决定放行策略; 不做 DNS; 失败时返回空列表由调用方兜底
# 依赖关系    : 标准库 socket / ipaddress / ctypes(Windows) / fcntl 与 /proc(Linux)
# ==========================================================================================

"""本机网段枚举.

单宿主取默认路由地址即可, 但装有多网卡(例如 VMware VMnet)的机器上,
被控端中继若只放行默认网段, 其他网段的合法控制端会被拒绝. IPv6 侧同理:
双栈监听后来源是全局地址, 白名单必须覆盖本机所在前缀, 否则合法控制端会被拒绝.
"""

from __future__ import annotations

import ctypes
import importlib
import ipaddress
import socket
import sys
from pathlib import Path
from typing import Any

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B

# /proc/net/if_inet6 第五列的内核地址标志位, 临时地址与次要地址共用该位
IFA_F_TEMPORARY = 0x01

# GetAdaptersAddresses: 只取单播地址, 跳过 anycast/组播/DNS 服务器, 减少返回体积
GAA_FLAGS_SKIP_ANYCAST_MULTICAST_DNS = 0x02 | 0x04 | 0x08
ERROR_BUFFER_OVERFLOW = 111
# iptypes.h 枚举值: IP_PREFIX_ORIGIN 的 RouterAdvertisement=4,
# IP_SUFFIX_ORIGIN 的 LinkLayerAddress=4 / Random=5. 前缀来自 RA 且后缀随机
# 即 RFC 4941 临时地址; 注意 3 是 Dhcp 不是 Random.
PREFIX_ORIGIN_ROUTER_ADVERTISEMENT = 4
SUFFIX_ORIGIN_RANDOM = 5


def _linux_interfaces() -> list[tuple[str, str]]:
    """Linux: 用 ioctl 逐网卡读取地址与掩码.

    fcntl.ioctl 只在 Unix 类型的 typeshed 里声明; 用动态导入拿到平台模块,
    在 Windows 上该分支不会被调用.
    """
    import struct

    fcntl: Any = importlib.import_module("fcntl")
    pairs: list[tuple[str, str]] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            request = struct.pack("256s", name[:15].encode("ascii"))
            try:
                address = fcntl.ioctl(probe.fileno(), SIOCGIFADDR, request)
                mask = fcntl.ioctl(probe.fileno(), SIOCGIFNETMASK, request)
            except OSError:
                continue
            pairs.append((socket.inet_ntoa(address[20:24]), socket.inet_ntoa(mask[20:24])))
    finally:
        probe.close()
    return pairs


class _IpAddrString(ctypes.Structure):
    pass


_IpAddrString._fields_ = [
    ("Next", ctypes.POINTER(_IpAddrString)),
    ("IpAddress", ctypes.c_char * 16),
    ("IpMask", ctypes.c_char * 16),
    ("Context", ctypes.c_ulong),
]


class _IpAdapterInfo(ctypes.Structure):
    pass


_IpAdapterInfo._fields_ = [
    ("Next", ctypes.POINTER(_IpAdapterInfo)),
    ("ComboIndex", ctypes.c_ulong),
    ("AdapterName", ctypes.c_char * 260),
    ("Description", ctypes.c_char * 132),
    ("AddressLength", ctypes.c_uint),
    ("Address", ctypes.c_ubyte * 8),
    ("Index", ctypes.c_ulong),
    ("Type", ctypes.c_uint),
    ("DhcpEnabled", ctypes.c_uint),
    ("CurrentIpAddress", ctypes.POINTER(_IpAddrString)),
    ("IpAddressList", _IpAddrString),
    ("GatewayList", _IpAddrString),
    ("DhcpServer", _IpAddrString),
    ("HaveWins", ctypes.c_int),
    ("PrimaryWinsServer", _IpAddrString),
    ("SecondaryWinsServer", _IpAddrString),
    ("LeaseObtained", ctypes.c_ulong),
    ("LeaseExpires", ctypes.c_ulong),
]


def _windows_interfaces() -> list[tuple[str, str]]:
    """Windows: GetAdaptersInfo 遍历所有 IPv4 地址 (含 VMware 虚拟网卡)."""
    if sys.platform != "win32":
        return []
    # windll 只在 Windows 版 typeshed 中声明; 动态取平台模块避免跨平台类型误报
    windll: Any = importlib.import_module("ctypes").windll
    iphlpapi: Any = windll.iphlpapi
    size = ctypes.c_ulong(0)
    iphlpapi.GetAdaptersInfo(None, ctypes.byref(size))
    if size.value == 0:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    head = ctypes.cast(buffer, ctypes.POINTER(_IpAdapterInfo))
    if iphlpapi.GetAdaptersInfo(head, ctypes.byref(size)) != 0:
        return []
    pairs: list[tuple[str, str]] = []
    adapter: Any = head
    while adapter:
        node: Any = ctypes.pointer(adapter.contents.IpAddressList)
        while node:
            address = node.contents.IpAddress.decode("ascii", "ignore").strip()
            mask = node.contents.IpMask.decode("ascii", "ignore").strip()
            if address and mask:
                pairs.append((address, mask))
            node = node.contents.Next
        adapter = adapter.contents.Next
    return pairs


class _SockaddrIn6(ctypes.Structure):
    """struct sockaddr_in6 的前 24 字节; 只取地址本体, 端口与流标签不参与."""

    _fields_ = [
        ("sin6_family", ctypes.c_ushort),
        ("sin6_port", ctypes.c_ushort),
        ("sin6_flowinfo", ctypes.c_ulong),
        ("sin6_addr", ctypes.c_ubyte * 16),
        ("sin6_scope_id", ctypes.c_ulong),
    ]


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _AdapterUnicastAddress(ctypes.Structure):
    """IP_ADAPTER_UNICAST_ADDRESS_LH: 前 8 字节是 Length/Flags 联合体."""

    pass


_AdapterUnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterUnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong),
    ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _AdapterAddresses(ctypes.Structure):
    """IP_ADAPTER_ADDRESSES_LH 的前 5 个字段; 只需要拿到首个单播地址与下一条网卡."""

    pass


_AdapterAddresses._fields_ = [
    ("Length", ctypes.c_ulong),
    ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_AdapterAddresses)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_AdapterUnicastAddress)),
]


def _windows_ipv6_unicast() -> list[tuple[ipaddress.IPv6Address, int, bool]]:
    """Windows: GetAdaptersAddresses 读取单播 IPv6 地址、前缀长度与来源.

    ``SuffixOrigin == IpSuffixOriginRandom(3)`` 即 RFC 4941 临时地址; 这是
    结构化字段, 不像 netsh 文本那样随系统语言变化.
    """
    if sys.platform != "win32":
        return []
    # windll 只在 Windows 版 typeshed 中声明; 动态取平台模块避免跨平台类型误报
    iphlpapi: Any = importlib.import_module("ctypes").windll.iphlpapi
    size = ctypes.c_ulong(0)
    iphlpapi.GetAdaptersAddresses(socket.AF_INET6, GAA_FLAGS_SKIP_ANYCAST_MULTICAST_DNS, None, None, ctypes.byref(size))
    if size.value == 0:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    head = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
    result = iphlpapi.GetAdaptersAddresses(
        socket.AF_INET6, GAA_FLAGS_SKIP_ANYCAST_MULTICAST_DNS, None, head, ctypes.byref(size)
    )
    if result == ERROR_BUFFER_OVERFLOW:
        buffer = ctypes.create_string_buffer(size.value)
        head = ctypes.cast(buffer, ctypes.POINTER(_AdapterAddresses))
        result = iphlpapi.GetAdaptersAddresses(
            socket.AF_INET6, GAA_FLAGS_SKIP_ANYCAST_MULTICAST_DNS, None, head, ctypes.byref(size)
        )
    if result != 0:
        return []
    entries: list[tuple[ipaddress.IPv6Address, int, bool]] = []
    adapter: Any = head
    while adapter:
        node: Any = adapter.contents.FirstUnicastAddress
        while node:
            item = node.contents
            prefix = int(item.OnLinkPrefixLength) or 64
            sockaddr = ctypes.cast(item.Address.lpSockaddr, ctypes.POINTER(_SockaddrIn6))
            try:
                address = ipaddress.IPv6Address(bytes(sockaddr.contents.sin6_addr))
            except (ValueError, OSError):
                node = item.Next
                continue
            if _network_of(address, prefix) is not None:
                temporary = (
                    int(item.PrefixOrigin) == PREFIX_ORIGIN_ROUTER_ADVERTISEMENT
                    and int(item.SuffixOrigin) == SUFFIX_ORIGIN_RANDOM
                )
                entries.append((address, prefix, temporary))
            node = item.Next
        adapter = adapter.contents.Next
    return entries


def local_ipv4_networks() -> list[str]:
    """返回本机全部 IPv4 网段 CIDR, 去重并排序; 查询失败返回空列表."""
    pairs = _linux_interfaces() if sys.platform.startswith("linux") else _windows_interfaces()
    networks: set[str] = set()
    for address, mask in pairs:
        try:
            networks.add(str(ipaddress.ip_network(f"{address}/{mask}", strict=False)))
        except ValueError:
            continue
    return sorted(networks)


def local_ipv4_addresses() -> list[str]:
    """返回本机可对外的 IPv4 地址, 去重排序; 回环与链路本地不参与."""
    pairs = _linux_interfaces() if sys.platform.startswith("linux") else _windows_interfaces()
    found: set[str] = set()
    for address, _mask in pairs:
        try:
            parsed = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if parsed.is_loopback or parsed.is_link_local:
            continue
        found.add(str(parsed))
    return sorted(found)


def _network_of(address: ipaddress.IPv6Address, prefix: int) -> str | None:
    """把地址按前缀归网; 回环、链路本地、组播与映射地址不参与白名单."""
    if address.is_loopback or address.is_link_local or address.is_multicast or address.ipv4_mapped is not None:
        return None
    try:
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
    except ValueError:
        return None


def _if_inet6_entries(content: str) -> list[tuple[ipaddress.IPv6Address, int, bool]]:
    """解析 /proc/net/if_inet6: 每行首列为 32 位十六进制地址, 第三列为前缀长度(十六进制).

    第五列是内核地址标志位, ``IFA_F_TEMPORARY``(0x01) 表示 RFC 4941 临时地址.
    """
    entries: list[tuple[ipaddress.IPv6Address, int, bool]] = []
    for line in content.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            address = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            prefix = int(fields[2], 16)
            flags = int(fields[4], 16) if len(fields) > 4 else 0
        except ValueError:
            continue
        if _network_of(address, prefix) is None:
            continue
        entries.append((address, prefix, bool(flags & IFA_F_TEMPORARY)))
    return entries


def _parse_if_inet6(content: str) -> list[str]:
    """解析 /proc/net/if_inet6 内容, 只保留全局地址归出的网段."""
    found: list[str] = []
    for address, prefix, _temporary in _if_inet6_entries(content):
        network = _network_of(address, prefix)
        if network is not None and network not in found:
            found.append(network)
    return found


def _linux_ipv6_entries() -> list[tuple[ipaddress.IPv6Address, int, bool]]:
    """Linux: 读 /proc/net/if_inet6, 返回全局地址、前缀长度与是否临时地址."""
    try:
        content = Path("/proc/net/if_inet6").read_text(encoding="utf-8")
    except OSError:
        return []
    return _if_inet6_entries(content)


def _linux_ipv6_networks() -> list[str]:
    """Linux: 读 /proc/net/if_inet6, 取地址与前缀长度逐条归网."""
    try:
        content = Path("/proc/net/if_inet6").read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_if_inet6(content)


def _hostname_ipv6_entries() -> list[tuple[ipaddress.IPv6Address, int, bool]]:
    """兜底: 从主机名解析本机 IPv6 地址, 全局单播按 /64 处理且视作稳定地址.

    ``GetAdaptersAddresses`` 不可用时才会走到这里; 该路径拿不到地址来源,
    只能按稳定地址上报.
    """
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        return []
    entries: list[tuple[ipaddress.IPv6Address, int, bool]] = []
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.IPv6Address(raw)
        except ValueError:
            continue
        if _network_of(address, 64) is None:
            continue
        entries.append((address, 64, False))
    return entries


def _windows_ipv6_entries() -> list[tuple[ipaddress.IPv6Address, int, bool]]:
    """Windows: 优先用 GetAdaptersAddresses 取地址与前缀, 失败时退化为主机名解析."""
    entries = _windows_ipv6_unicast()
    return entries if entries else _hostname_ipv6_entries()


def _windows_ipv6_networks() -> list[str]:
    """Windows: 逐条全局地址归网, 回环与链路本地剔除."""
    found: list[str] = []
    for address, prefix, _temporary in _windows_ipv6_entries():
        network = _network_of(address, prefix)
        if network is not None and network not in found:
            found.append(network)
    return found


def local_ipv6_networks() -> list[str]:
    """返回本机 IPv6 网段 CIDR, 去重并排序; 回环与链路本地由调用方单列."""
    networks = _linux_ipv6_networks() if sys.platform.startswith("linux") else _windows_ipv6_networks()
    return sorted(set(networks))


def ipv6_address_groups() -> tuple[list[str], list[str]]:
    """返回 (稳定地址, RFC 4941 临时地址) 两组, 各去重排序.

    临时地址会过期轮换, 对外公布时排在稳定地址之后; 调用方需要把稳定地址
    放在最前时用这个出口, 不需要区分顺序时用 :func:`local_ipv6_addresses`.
    """
    entries = _linux_ipv6_entries() if sys.platform.startswith("linux") else _windows_ipv6_entries()
    stable = sorted({str(address) for address, _prefix, temporary in entries if not temporary})
    rotating = sorted({str(address) for address, _prefix, temporary in entries if temporary})
    return stable, rotating


def local_ipv6_addresses() -> list[str]:
    """返回本机全局 IPv6 地址: 稳定地址在前, RFC 4941 临时地址在后.

    回环、链路本地、组播与映射地址不参与.
    """
    stable, rotating = ipv6_address_groups()
    return [*stable, *rotating]
