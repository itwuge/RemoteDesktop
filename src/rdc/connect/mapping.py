# ==========================================================================================
# 文件        : mapping.py
# 所属目录    : src/rdc/connect/
# 文件用途    : UPnP-IGD(SSDP 发现 + SOAP) 与 NAT-PMP/PCP 端口映射客户端, 以及映射生命周期管理
# 架构定位    : 连接策略的“入站端口来源”, 为 policy.Candidate 提供 kind="mapped" 强候选
# 核心功能    : 只认默认网关的 IGD 发现; 外部地址查询; AddPortMapping/GetSpecificPortMappingEntry/
#               DeletePortMapping; 租约一半续租; 进程退出与会话结束兜底清理; PCP 失败回退 NAT-PMP
# 职责边界    : 不改协议与命令层; 只映射 UDP 且不占用已有规则; 只写结构化日志, 不弹窗不阻塞界面
# 运行环境    : Python 3.12, 仅标准库
# 依赖关系    : 被 src/rdc/connect/runtime.py 调用; 返回 policy.Candidate / MappingOutcome
# ==========================================================================================

"""端口映射客户端.

三条通路按优先级尝试, 任一成功即返回:

1. UPnP-IGD (SSDP 发现 + SOAP 调用), 实测 15-31 ms, 信息最全;
2. PCP MAP (RFC 6887), 部分固件只应答 ANNOUNCE 不应答 MAP, 失败必须回退而不是卡住;
3. NAT-PMP (RFC 6886), PCP 不可用时的同网关备选.

安全与卫生约束(均来自 2026-11-10 路由器实测):

- 只接受来源 IP 等于默认网关的 IGD, 局域网里可能存在第二个 IGD;
- 只映射 UDP, 外部端口随机高位, 不碰 1024 以下端口;
- 不覆盖/不删除别人的规则: 外部端口被占用就换一个, 删除只删本次创建的;
- 租期默认 120 s, 会话期间按租期一半续租, 退出与会话结束显式删除, 异常退出靠租期自然过期兜底.
"""

from __future__ import annotations

import atexit
import http.client
import ipaddress
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urljoin, urlsplit

from rdc.connect.policy import (
    KIND_MAPPED,
    MAPPED_PRIORITY_EXTERNAL,
    MAPPED_PRIORITY_READBACK,
    Candidate,
)
from rdc.core import logging as rdc_logging

MODULE = "connect.mapping"

SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_STS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
SSDP_TIMEOUT_S = 1.0
NATPMP_PORT = 5351
NATPMP_TIMEOUT_S = 1.0
PCP_TIMEOUT_S = 0.6
SOAP_TIMEOUT_S = 4.0
DESCRIPTION_TIMEOUT_S = 3.0
DEFAULT_LEASE_S = 120
MIN_LEASE_S = 10
MAX_RENEW_FAILURES = 3
EXTERNAL_PORT_MIN = 49152
EXTERNAL_PORT_MAX = 65535
EXTERNAL_PORT_ATTEMPTS = 8
DISCOVERY_CACHE_TTL_S = 600.0
PROTOCOL_UDP = "UDP"
DESCRIPTION_MAX_BYTES = 256 * 1024

ENV_SWITCH = "RDC_PORT_MAPPING"
ENV_GATEWAY = "RDC_GATEWAY"

PROTOCOL_UPNP = "upnp"
PROTOCOL_NATPMP = "natpmp"
PROTOCOL_PCP = "pcp"

VERIFY_READBACK = "readback"
VERIFY_EXTERNAL = "external"

# 映射失败原因码. 这些码目前不出现在命令层契约里, 因此不并入 rdc.core.errors;
# 已按约定在 .team/records/契约变更记录-契约-202611-016-20261110.md 登记, 待 M03 批准后并入.
REASON_OK = "ok"
MAPPING_UNSUPPORTED = "MAPPING_UNSUPPORTED"
MAPPING_REJECTED = "MAPPING_REJECTED"
MAPPING_LEASE_FAILED = "MAPPING_LEASE_FAILED"
MAPPING_EXTERNAL_MISMATCH = "MAPPING_EXTERNAL_MISMATCH"
MAPPING_EXTERNAL_UNREACHABLE = "MAPPING_EXTERNAL_UNREACHABLE"
REASON_DISABLED = "mapping_disabled"
REASON_NO_GATEWAY = "mapping_no_gateway"
REASON_LEASE_EXPIRED = "mapping_lease_expired"
REASON_PCP_FALLBACK = "mapping_pcp_fallback"

REASON_TEXT: dict[str, str] = {
    REASON_OK: "端口映射已生效",
    MAPPING_UNSUPPORTED: "没有可用的端口映射通道（未发现 IGD，NAT-PMP/PCP 也不可用）",
    MAPPING_REJECTED: "路由器拒绝了端口映射请求",
    MAPPING_LEASE_FAILED: "端口映射租约未能建立或读回不一致",
    MAPPING_EXTERNAL_MISMATCH: "路由器外部地址与 STUN 反射地址不一致（多层 NAT 或 CGNAT），映射不可作为直连候选",
    MAPPING_EXTERNAL_UNREACHABLE: "映射端口在外部探测中不可达",
    REASON_DISABLED: "端口映射已按配置关闭",
    REASON_NO_GATEWAY: "无法确定默认网关",
    REASON_LEASE_EXPIRED: "端口映射条目已过期",
    REASON_PCP_FALLBACK: "PCP 映射不可用，已回退 NAT-PMP",
}

_SERVICE_BLOCK = re.compile(r"<service>(.*?)</service>", re.DOTALL)
_SERVICE_TYPE = re.compile(r"<serviceType>([^<]+)</serviceType>")
_CONTROL_URL = re.compile(r"<controlURL>([^<]+)</controlURL>")
_element = "<{tag}>([^<]*)</{tag}>"
_ERROR_CODE = re.compile(r"<errorCode>\s*(\d+)\s*</errorCode>")
_LOCATION = re.compile(r"(?im)^location:\s*(\S+)")
_SERVER = re.compile(r"(?im)^server:\s*(.+)$")

_ANY_V4 = ""


def describe_reason(reason: str) -> str:
    """把映射原因翻成中文, 供日志与界面提示."""
    return REASON_TEXT.get(reason, reason)


# -------------------------------- 基础判定 --------------------------------


def port_mapping_enabled() -> bool:
    """读取开关; ``RDC_PORT_MAPPING`` 显式关闭时不做任何映射尝试."""
    value = (os.environ.get(ENV_SWITCH) or "").strip().lower()
    return value not in {"0", "off", "false", "no", "disabled"}


def local_ipv4() -> str:
    """取本机出站网卡地址; UDP connect 不发包, 只让内核选路."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def guess_gateway(local_ip: str) -> str:
    """按本机 /24 的 .1 推测网关; 环境变量 ``RDC_GATEWAY`` 优先."""
    configured = (os.environ.get(ENV_GATEWAY) or "").strip()
    if configured:
        return configured
    if not local_ip:
        return ""
    try:
        network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    except ValueError:
        return ""
    return str(network.network_address + 1)


def is_public_ipv4(address: str) -> tuple[bool, str]:
    """判定地址是否为可被外部直连的公网 IPv4, 返回 (是否公网, 原因)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, "地址解析失败"
    if ip.version != 4:
        return False, "非 IPv4"
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return False, "落入 100.64.0.0/10 (运营商 CGNAT)"
    if ip.is_private:
        return False, "落入私有地址段"
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return False, "非公网可路由地址"
    return True, "公网 IPv4"


# -------------------------------- 数据结构 --------------------------------


@dataclass(frozen=True)
class SsdpResponse:
    """一条 SSDP 应答."""

    source: str
    location: str
    server: str = ""


@dataclass(frozen=True)
class IgdService:
    """IGD 的 WAN 连接服务描述."""

    location: str
    control_url: str
    service_type: str
    server: str = ""


@dataclass(frozen=True)
class NatpmpResult:
    """NAT-PMP 映射应答."""

    result_code: int
    internal_port: int = 0
    external_port: int = 0
    lifetime_s: int = 0
    epoch_s: int = 0

    @property
    def ok(self) -> bool:
        return self.result_code == 0 and self.external_port > 0


@dataclass(frozen=True)
class PcpResult:
    """PCP MAP 应答; ``version`` 非 2 表示对端不支持 PCP."""

    version: int
    result_code: int
    internal_port: int = 0
    external_port: int = 0
    lifetime_s: int = 0
    external_address: str = ""

    @property
    def ok(self) -> bool:
        return self.version == 2 and self.result_code == 0 and self.external_port > 0


@dataclass(frozen=True)
class MappingOutcome:
    """一次映射尝试的完整结果, 用作日志、候选来源与清理凭据."""

    ok: bool
    protocol: str
    reason: str
    external_address: str = ""
    external_port: int = 0
    internal_port: int = 0
    lease_s: int = 0
    verification: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        """脱敏后的事件字段."""
        return {
            "mapping_protocol": self.protocol,
            "mapping_reason": self.reason,
            "mapping_external_port": self.external_port,
            "mapping_internal_port": self.internal_port,
            "mapping_lease_s": self.lease_s,
            "mapping_verification": self.verification,
        }


class MappingError(RuntimeError):
    """映射过程中的可诊断失败; ``soap_code`` 保留 UPnP 错误码(如 714/718)."""

    def __init__(self, reason: str, detail: str = "", soap_code: int | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.soap_code = soap_code


# -------------------------------- SSDP 发现 --------------------------------


def _ssdp_message(st: str, timeout_s: float) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(1, int(timeout_s))}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode("ascii")


def ssdp_search(
    local_ip: str,
    gateway: str,
    sts: tuple[str, ...] = SSDP_STS,
    timeout_s: float = SSDP_TIMEOUT_S,
) -> tuple[SsdpResponse, ...]:
    """组播 + 网关 1900 单播双路发现.

    同一 socket 先发完所有 M-SEARCH, 再统一收包, 总耗时约等于单次超时,
    避免 2 个 ST × 2 个目的地串行把连接预算吃掉.
    """
    destinations: list[tuple[str, int]] = [SSDP_ADDRESS]
    if gateway:
        destinations.append((gateway, 1900))
    found: list[SsdpResponse] = []
    seen: set[tuple[str, str]] = set()
    sock: socket.socket | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if local_ip:
            # 多网卡主机默认组播出口可能落在虚拟网卡, 必须显式绑定物理网卡
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        for st in sts:
            for destination in destinations:
                sock.sendto(_ssdp_message(st, timeout_s), destination)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, address = sock.recvfrom(4096)
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            lines = text.splitlines()
            if not lines or "200" not in lines[0]:
                continue
            location = _LOCATION.search(text)
            if location is None:
                continue
            key = (address[0], location.group(1))
            if key in seen:
                continue
            seen.add(key)
            server = _SERVER.search(text)
            found.append(
                SsdpResponse(
                    source=address[0],
                    location=location.group(1),
                    server=server.group(1).strip() if server else "",
                )
            )
    except OSError:
        pass
    finally:
        if sock is not None:
            sock.close()
    return tuple(found)


def fetch_device_description(url: str, timeout_s: float = DESCRIPTION_TIMEOUT_S) -> str:
    """读取设备描述 XML; 只允许 http 且限制体积."""
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("仅支持 http 设备描述地址")
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout_s)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse()
        return response.read(DESCRIPTION_MAX_BYTES).decode("utf-8", "replace")
    finally:
        conn.close()


def parse_device_description(location: str, xml: str, server: str = "") -> IgdService | None:
    """从设备描述里取 WANIPConnection/WANPPPConnection 服务; 控制 URL 支持相对路径."""
    for block in _SERVICE_BLOCK.findall(xml):
        service_type = _SERVICE_TYPE.search(block)
        control_url = _CONTROL_URL.search(block)
        if service_type is None or control_url is None:
            continue
        if "WANIPConnection" not in service_type.group(1) and "WANPPPConnection" not in service_type.group(1):
            continue
        return IgdService(
            location=location,
            control_url=urljoin(location, control_url.group(1).strip()),
            service_type=service_type.group(1).strip(),
            server=server,
        )
    return None


def discover_igd(
    local_ip: str = "",
    gateway: str = "",
    timeout_s: float = SSDP_TIMEOUT_S,
) -> IgdService | None:
    """发现默认网关上的 IGD; 无法唯一确定网关时拒绝猜测.

    只接受来源 IP 等于默认网关的应答: 实测局域网内存在第二个 IGD(192.168.31.12),
    盲目采信会把 SOAP 发到错误的设备.
    """
    responses = ssdp_search(local_ip, gateway, timeout_s=timeout_s)
    if not responses:
        return None
    if gateway:
        candidates = [row for row in responses if row.source == gateway]
    else:
        candidates = [row for row in responses if _same_subnet(row.source, local_ip)]
        if len({row.source for row in candidates}) != 1:
            return None
    for row in candidates:
        try:
            xml = fetch_device_description(row.location)
        except (OSError, ValueError, http.client.HTTPException):
            continue
        service = parse_device_description(row.location, xml, server=row.server)
        if service is not None:
            return service
    return None


def _same_subnet(address: str, local_ip: str) -> bool:
    """判断地址与本机是否同 /24; 用于网关缺省时的保守筛选."""
    if not local_ip:
        return False
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(f"{local_ip}/24", strict=False)
    except ValueError:
        return False


# -------------------------------- UPnP SOAP --------------------------------


def soap_call(service: IgdService, action: str, body: str, timeout_s: float = SOAP_TIMEOUT_S) -> str:
    """调用一个 SOAP 动作, 返回响应正文; 连接失败抛 OSError."""
    parts = urlsplit(service.control_url)
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service.service_type}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout_s)
    try:
        conn.request(
            "POST",
            parts.path or "/",
            body=envelope,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service.service_type}#{action}"',
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        return response.read(64 * 1024).decode("utf-8", "replace")
    finally:
        conn.close()


def _xml_value(text: str, tag: str) -> str:
    match = re.search(_element.format(tag=re.escape(tag)), text)
    return match.group(1).strip() if match else ""


def get_external_ip(service: IgdService, soap: Callable[..., str] = soap_call) -> str:
    """查询 IGD 记录的 WAN 外部地址; 被拒绝时抛 MappingError."""
    text = soap(service, "GetExternalIPAddress", "")
    address = _xml_value(text, "NewExternalIPAddress")
    if not address:
        code = soap_error_code(text)
        raise MappingError(MAPPING_REJECTED, f"GetExternalIPAddress 无外部地址, errorCode={code}", code)
    return address


def add_port_mapping(
    service: IgdService,
    *,
    internal_client: str,
    internal_port: int,
    external_port: int,
    lease_s: int,
    description: str,
    soap: Callable[..., str] = soap_call,
) -> None:
    """创建一条 UDP 映射; 被拒绝时抛 MappingError."""
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        f"<NewProtocol>{PROTOCOL_UDP}</NewProtocol>"
        f"<NewInternalPort>{internal_port}</NewInternalPort>"
        f"<NewInternalClient>{internal_client}</NewInternalClient>"
        "<NewEnabled>1</NewEnabled>"
        f"<NewPortMappingDescription>{description}</NewPortMappingDescription>"
        f"<NewLeaseDuration>{lease_s}</NewLeaseDuration>"
    )
    text = soap(service, "AddPortMapping", body)
    if "AddPortMappingResponse" not in text:
        code = soap_error_code(text)
        raise MappingError(MAPPING_REJECTED, f"AddPortMapping 未返回成功, errorCode={code}", code)


def get_port_mapping_entry(
    service: IgdService,
    external_port: int,
    soap: Callable[..., str] = soap_call,
) -> dict[str, str] | None:
    """读回映射条目; 714(NoSuchEntryInArray) 返回 None."""
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        f"<NewProtocol>{PROTOCOL_UDP}</NewProtocol>"
    )
    text = soap(service, "GetSpecificPortMappingEntry", body)
    internal_port = _xml_value(text, "NewInternalPort")
    if not internal_port:
        return None
    return {
        "internal_port": internal_port,
        "internal_client": _xml_value(text, "NewInternalClient"),
        "lease_s": _xml_value(text, "NewLeaseDuration"),
        "description": _xml_value(text, "NewPortMappingDescription"),
        "enabled": _xml_value(text, "NewEnabled"),
    }


def delete_port_mapping(
    service: IgdService,
    external_port: int,
    soap: Callable[..., str] = soap_call,
) -> bool:
    """删除一条 UDP 映射; 条目不存在视为已清理."""
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        f"<NewProtocol>{PROTOCOL_UDP}</NewProtocol>"
    )
    text = soap(service, "DeletePortMapping", body)
    if "DeletePortMappingResponse" in text:
        return True
    return soap_error_code(text) == 714


def soap_error_code(text: str) -> int | None:
    """从 SOAP Fault 里取 errorCode(如 714/718)."""
    match = _ERROR_CODE.search(text)
    return int(match.group(1)) if match else None


# -------------------------------- NAT-PMP / PCP --------------------------------


def _exchange(sock: socket.socket, gateway: str, request: bytes, timeout_s: float) -> bytes:
    """在给定 socket 上做一次请求/应答, 用完恢复原超时(打洞 socket 保持非阻塞)."""
    previous = sock.gettimeout()
    sock.settimeout(max(0.05, timeout_s))
    try:
        sock.sendto(request, (gateway, NATPMP_PORT))
        data, _address = sock.recvfrom(128)
        return data
    finally:
        sock.settimeout(previous)


def natpmp_external_address(gateway: str, timeout_s: float = NATPMP_TIMEOUT_S) -> tuple[int, str]:
    """NAT-PMP 外部地址查询, 返回 (result_code, 地址)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        data = _exchange(sock, gateway, b"\x00\x00", timeout_s)
    finally:
        sock.close()
    if len(data) < 12 or data[1] != 128:
        raise MappingError(MAPPING_UNSUPPORTED, "NAT-PMP 外部地址应答非法")
    return int.from_bytes(data[2:4], "big"), socket.inet_ntoa(data[8:12])


def natpmp_add_mapping(
    sock: socket.socket | None,
    gateway: str,
    internal_port: int,
    requested_external_port: int,
    lifetime_s: int,
    timeout_s: float = NATPMP_TIMEOUT_S,
) -> NatpmpResult:
    """NAT-PMP 建 UDP 映射; 请求源端口必须等于内部端口, 因此复用调用方 socket."""
    owned = sock is None
    active = sock or _bind_socket(internal_port)
    request = (
        b"\x00\x01\x00\x00"
        + internal_port.to_bytes(2, "big")
        + requested_external_port.to_bytes(2, "big")
        + lifetime_s.to_bytes(4, "big")
    )
    try:
        data = _exchange(active, gateway, request, timeout_s)
    finally:
        if owned:
            active.close()
    if len(data) < 16 or data[1] != 129:
        raise MappingError(MAPPING_UNSUPPORTED, "NAT-PMP MAP 应答非法")
    return NatpmpResult(
        result_code=int.from_bytes(data[2:4], "big"),
        internal_port=int.from_bytes(data[8:10], "big"),
        external_port=int.from_bytes(data[10:12], "big"),
        lifetime_s=int.from_bytes(data[12:16], "big"),
    )


def natpmp_remove_mapping(
    sock: socket.socket | None,
    gateway: str,
    internal_port: int,
    external_port: int,
    timeout_s: float = NATPMP_TIMEOUT_S,
) -> bool:
    """NAT-PMP 删除映射: lifetime 置 0; 不抛异常, 返回是否已发出并收到应答."""
    try:
        result = natpmp_add_mapping(sock, gateway, internal_port, external_port, 0, timeout_s)
    except (OSError, TimeoutError, MappingError):
        return False
    return result.result_code == 0 or result.external_port == 0


def pcp_add_mapping(
    sock: socket.socket,
    gateway: str,
    internal_port: int,
    requested_external_port: int,
    lifetime_s: int,
    timeout_s: float = PCP_TIMEOUT_S,
) -> PcpResult:
    """PCP MAP 请求(RFC 6887); 对端不支持时返回 version=0, 由调用方回退 NAT-PMP."""
    nonce = secrets.token_bytes(12)
    header = b"\x02\x01\x00\x00" + lifetime_s.to_bytes(4, "big") + b"\x00" * 16
    opcode = (
        nonce + b"\x11" + internal_port.to_bytes(2, "big") + requested_external_port.to_bytes(2, "big") + b"\x00" * 16
    )
    try:
        data = _exchange(sock, gateway, header + opcode, timeout_s)
    except (OSError, TimeoutError):
        return PcpResult(version=0, result_code=-1)
    if len(data) < 57 or data[0] != 2 or data[1] != 0x81:
        # 实测小米固件把 PCP MAP 当 NAT-PMP 处理, 返回 version=0 的报文
        return PcpResult(version=int(data[0]) if data else 0, result_code=-1)
    return PcpResult(
        version=2,
        result_code=data[3],
        internal_port=int.from_bytes(data[38:40], "big"),
        external_port=int.from_bytes(data[40:42], "big"),
        lifetime_s=int.from_bytes(data[4:8], "big"),
        external_address=_pcp_ipv4(data[42:58]),
    )


def _pcp_ipv4(raw: bytes) -> str:
    """解析 PCP 的 16 字节外部地址字段, 只认 IPv4-mapped 形式."""
    if len(raw) != 16 or raw[:10] != b"\x00" * 10 or raw[10:12] not in {b"\xff\xff", b"\x00\x00"}:
        return ""
    try:
        return socket.inet_ntoa(raw[12:16])
    except OSError:
        return ""


def _bind_socket(port: int) -> socket.socket:
    """绑定到指定 UDP 端口, 失败抛 OSError."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((_ANY_V4, port))
    except OSError:
        sock.close()
        raise
    return sock


# -------------------------------- 映射器 --------------------------------


Discover = Callable[[], IgdService | None]
Soap = Callable[..., str]
NatpmpAdd = Callable[..., NatpmpResult]
NatpmpRemove = Callable[..., bool]
PcpAdd = Callable[..., PcpResult]


class PortMapper:
    """按 UPnP -> PCP -> NAT-PMP 顺序申请 UDP 入站端口, 并管理已有映射的续租与删除."""

    def __init__(
        self,
        gateway: str = "",
        local_ip: str = "",
        *,
        discover: Discover | None = None,
        soap: Soap = soap_call,
        natpmp_add: NatpmpAdd = natpmp_add_mapping,
        natpmp_remove: NatpmpRemove = natpmp_remove_mapping,
        pcp_add: PcpAdd = pcp_add_mapping,
        natpmp_external: Callable[[str], tuple[int, str]] = natpmp_external_address,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._local_ip = local_ip
        self._gateway = gateway or guess_gateway(local_ip)
        self._discover = discover or self._default_discover
        self._soap = soap
        self._natpmp_add = natpmp_add
        self._natpmp_remove = natpmp_remove
        self._pcp_add = pcp_add
        self._natpmp_external = natpmp_external
        self._clock = clock
        self._igd: IgdService | None = None
        self._discovered_at = 0.0
        self._discovered = False
        self._lock = threading.Lock()

    @property
    def gateway(self) -> str:
        return self._gateway

    def _default_discover(self) -> IgdService | None:
        return discover_igd(self._local_ip, self._gateway)

    def service(self) -> IgdService | None:
        """取 IGD 描述, 按 10 分钟 TTL 缓存; 换网后由调用方重建实例."""
        with self._lock:
            fresh = self._discovered and (self._clock() - self._discovered_at) < DISCOVERY_CACHE_TTL_S
            if fresh:
                return self._igd
            self._igd = self._discover()
            self._discovered_at = self._clock()
            self._discovered = True
        rdc_logging.log_event(
            MODULE,
            "mapping.discover",
            level="INFO" if self._igd else "WARNING",
            reason=MAPPING_UNSUPPORTED if self._igd is None else REASON_OK,
            igd_found=self._igd is not None,
            igd_service=self._igd.service_type if self._igd else "",
            gateway=self._gateway,
        )
        return self._igd

    def external_ip(self) -> str:
        """读取 IGD 记录的 WAN 外部地址; 未发现 IGD 时抛 MappingError."""
        service = self.service()
        if service is None:
            raise MappingError(MAPPING_UNSUPPORTED, "未发现 IGD")
        return get_external_ip(service, soap=self._soap)

    def read_entry(self, external_port: int) -> dict[str, str] | None:
        """读回指定外部端口的映射条目, 不存在返回 None."""
        service = self.service()
        if service is None:
            return None
        return self._read_entry(service, external_port)

    def map_udp_port(
        self,
        internal_port: int,
        *,
        internal_client: str = "",
        lease_s: int = DEFAULT_LEASE_S,
        description: str = "",
        requested_external_port: int = 0,
        sock: socket.socket | None = None,
    ) -> MappingOutcome:
        """申请 UDP 映射; 三条通路都失败时返回带原因的结果而不是抛异常."""
        if not port_mapping_enabled():
            return MappingOutcome(ok=False, protocol="", reason=REASON_DISABLED)
        if not self._gateway:
            return MappingOutcome(ok=False, protocol="", reason=REASON_NO_GATEWAY)
        lease = max(MIN_LEASE_S, lease_s)
        client = internal_client or self._local_ip
        started = self._clock()
        owned = False
        active = sock
        if active is None:
            try:
                active = _bind_socket(internal_port)
                owned = True
            except OSError as exc:
                return MappingOutcome(
                    ok=False,
                    protocol="",
                    reason=MAPPING_LEASE_FAILED,
                    detail={"error": f"内部端口不可绑定: {exc}"},
                )
        try:
            upnp = self._map_upnp(internal_port, client, lease, description, requested_external_port)
            if upnp.ok:
                return self._log_outcome(upnp, started)
            pcp = self._map_pcp(active, internal_port, requested_external_port, lease)
            if pcp.ok:
                return self._log_outcome(pcp, started)
            natpmp = self._map_natpmp(active, internal_port, requested_external_port, lease, pcp_attempted=True)
            if natpmp.ok:
                return self._log_outcome(natpmp, started)
            detail = {
                **dict(upnp.detail),
                "upnp_reason": upnp.reason,
                "pcp_reason": pcp.reason,
                "natpmp_reason": natpmp.reason,
            }
            return self._log_outcome(MappingOutcome(ok=False, protocol="", reason=upnp.reason, detail=detail), started)
        finally:
            if owned:
                active.close()

    def _map_upnp(
        self,
        internal_port: int,
        internal_client: str,
        lease_s: int,
        description: str,
        requested_external_port: int,
    ) -> MappingOutcome:
        service = self.service()
        if service is None:
            return MappingOutcome(ok=False, protocol=PROTOCOL_UPNP, reason=MAPPING_UNSUPPORTED)
        try:
            external_address = get_external_ip(service, soap=self._soap)
        except (OSError, ValueError, http.client.HTTPException, MappingError) as exc:
            reason = exc.reason if isinstance(exc, MappingError) else MAPPING_UNSUPPORTED
            return MappingOutcome(ok=False, protocol=PROTOCOL_UPNP, reason=reason, detail={"error": str(exc)})
        for external_port in self._external_port_candidates(requested_external_port):
            occupied = self._read_entry(service, external_port)
            if occupied is not None and occupied.get("internal_port") != str(internal_port):
                continue
            try:
                add_port_mapping(
                    service,
                    internal_client=internal_client,
                    internal_port=internal_port,
                    external_port=external_port,
                    lease_s=lease_s,
                    description=description,
                    soap=self._soap,
                )
            except (OSError, ValueError, http.client.HTTPException, MappingError) as exc:
                reason = exc.reason if isinstance(exc, MappingError) else MAPPING_REJECTED
                code = exc.soap_code if isinstance(exc, MappingError) else None
                return MappingOutcome(
                    ok=False,
                    protocol=PROTOCOL_UPNP,
                    reason=reason,
                    detail={"error": str(exc), "soap_code": code},
                )
            entry = self._read_entry(service, external_port)
            if entry is None or entry.get("internal_port") != str(internal_port):
                delete_port_mapping(service, external_port, soap=self._soap)
                return MappingOutcome(
                    ok=False,
                    protocol=PROTOCOL_UPNP,
                    reason=MAPPING_LEASE_FAILED,
                    external_address=external_address,
                    external_port=external_port,
                    internal_port=internal_port,
                    detail={"readback": dict(entry or {})},
                )
            client_ok = entry.get("internal_client") in {"", internal_client}
            return MappingOutcome(
                ok=True,
                protocol=PROTOCOL_UPNP,
                reason=REASON_OK,
                external_address=external_address,
                external_port=external_port,
                internal_port=internal_port,
                lease_s=lease_s,
                verification=VERIFY_READBACK,
                detail={
                    "internal_client_verified": client_ok,
                    "igd_service": service.service_type,
                    "description": description,
                },
            )
        return MappingOutcome(
            ok=False,
            protocol=PROTOCOL_UPNP,
            reason=MAPPING_REJECTED,
            detail={"error": "外部端口全部被占用"},
        )

    def _read_entry(self, service: IgdService, external_port: int) -> dict[str, str] | None:
        try:
            return get_port_mapping_entry(service, external_port, soap=self._soap)
        except (OSError, ValueError, http.client.HTTPException, MappingError):
            return None

    def _map_pcp(
        self,
        sock: socket.socket,
        internal_port: int,
        requested_external_port: int,
        lease_s: int,
    ) -> MappingOutcome:
        try:
            result = self._pcp_add(sock, self._gateway, internal_port, requested_external_port, lease_s)
        except (OSError, TimeoutError, ValueError) as exc:
            return MappingOutcome(
                ok=False,
                protocol=PROTOCOL_PCP,
                reason=MAPPING_UNSUPPORTED,
                detail={"error": str(exc)},
            )
        if not result.ok:
            return MappingOutcome(
                ok=False,
                protocol=PROTOCOL_PCP,
                reason=MAPPING_UNSUPPORTED,
                detail={"pcp_version": result.version, "pcp_result": result.result_code},
            )
        address = result.external_address
        if not address:
            try:
                _code, address = self._natpmp_external(self._gateway)
            except (OSError, TimeoutError, MappingError):
                address = ""
        return MappingOutcome(
            ok=True,
            protocol=PROTOCOL_PCP,
            reason=REASON_OK,
            external_address=address,
            external_port=result.external_port,
            internal_port=internal_port,
            lease_s=result.lifetime_s or lease_s,
            verification=VERIFY_READBACK,
        )

    def _map_natpmp(
        self,
        sock: socket.socket,
        internal_port: int,
        requested_external_port: int,
        lease_s: int,
        *,
        pcp_attempted: bool,
    ) -> MappingOutcome:
        try:
            result = self._natpmp_add(sock, self._gateway, internal_port, requested_external_port, lease_s)
        except (OSError, TimeoutError, ValueError, MappingError) as exc:
            reason = REASON_PCP_FALLBACK if pcp_attempted else MAPPING_UNSUPPORTED
            return MappingOutcome(
                ok=False,
                protocol=PROTOCOL_NATPMP,
                reason=reason,
                detail={"error": str(exc), "pcp_attempted": pcp_attempted},
            )
        if not result.ok:
            return MappingOutcome(
                ok=False,
                protocol=PROTOCOL_NATPMP,
                reason=REASON_PCP_FALLBACK if pcp_attempted else MAPPING_REJECTED,
                detail={"natpmp_result": result.result_code, "pcp_attempted": pcp_attempted},
            )
        try:
            _code, address = self._natpmp_external(self._gateway)
        except (OSError, TimeoutError, MappingError):
            address = ""
        return MappingOutcome(
            ok=True,
            protocol=PROTOCOL_NATPMP,
            reason=REASON_OK,
            external_address=address,
            external_port=result.external_port,
            internal_port=internal_port,
            lease_s=result.lifetime_s or lease_s,
            verification=VERIFY_READBACK,
            detail={"pcp_fallback": pcp_attempted},
        )

    def _external_port_candidates(self, requested: int) -> tuple[int, ...]:
        """外部端口候选: 用户指定优先, 其余随机高位; 绝不使用 1024 以下端口."""
        ports: list[int] = []
        if requested >= 1024:
            ports.append(requested)
        span = EXTERNAL_PORT_MAX - EXTERNAL_PORT_MIN + 1
        while len(ports) < EXTERNAL_PORT_ATTEMPTS:
            ports.append(EXTERNAL_PORT_MIN + secrets.randbelow(span))
        return tuple(ports)

    def renew(
        self,
        mapping: MappingOutcome,
        lease_s: int | None = None,
        sock: socket.socket | None = None,
    ) -> MappingOutcome:
        """续租: UPnP 先读回再重发 AddPortMapping; 条目丢失则重新申请."""
        if not mapping.ok:
            return mapping
        lease = max(MIN_LEASE_S, lease_s or mapping.lease_s or DEFAULT_LEASE_S)
        if mapping.protocol == PROTOCOL_UPNP:
            service = self.service()
            if service is None:
                return replace(mapping, ok=False, reason=MAPPING_UNSUPPORTED)
            entry = self._read_entry(service, mapping.external_port)
            try:
                add_port_mapping(
                    service,
                    internal_client=self._local_ip,
                    internal_port=mapping.internal_port,
                    external_port=mapping.external_port,
                    lease_s=lease,
                    description=self._description(mapping),
                    soap=self._soap,
                )
            except (OSError, ValueError, http.client.HTTPException, MappingError) as exc:
                reason = REASON_LEASE_EXPIRED if entry is None else MAPPING_LEASE_FAILED
                return replace(mapping, ok=False, reason=reason, detail={"error": str(exc)})
            rdc_logging.log_event(
                MODULE,
                "mapping.renew",
                mapping_protocol=mapping.protocol,
                mapping_external_port=mapping.external_port,
                mapping_lease_s=lease,
                mapping_readded=entry is None,
            )
            return replace(mapping, lease_s=lease, detail={**mapping.detail, "renewed": True})
        owned = sock is None
        active = sock if sock is not None else _bind_socket(mapping.internal_port)
        try:
            if mapping.protocol == PROTOCOL_PCP:
                result = self._pcp_add(active, self._gateway, mapping.internal_port, mapping.external_port, lease)
                ok = result.ok
            else:
                natpmp = self._natpmp_add(active, self._gateway, mapping.internal_port, mapping.external_port, lease)
                ok = natpmp.ok
        except (OSError, TimeoutError, ValueError) as exc:
            return replace(mapping, ok=False, reason=MAPPING_LEASE_FAILED, detail={"error": str(exc)})
        finally:
            if owned:
                active.close()
        if not ok:
            return replace(mapping, ok=False, reason=REASON_LEASE_EXPIRED)
        rdc_logging.log_event(
            MODULE,
            "mapping.renew",
            mapping_protocol=mapping.protocol,
            mapping_external_port=mapping.external_port,
            mapping_lease_s=lease,
        )
        return replace(mapping, lease_s=lease, detail={**mapping.detail, "renewed": True})

    def delete(self, mapping: MappingOutcome, sock: socket.socket | None = None) -> bool:
        """删除本次创建的映射; 重复调用安全, 失败只记日志."""
        if not mapping.ok or mapping.external_port <= 0:
            return False
        removed = False
        if mapping.protocol == PROTOCOL_UPNP:
            service = self.service()
            if service is not None:
                try:
                    removed = delete_port_mapping(service, mapping.external_port, soap=self._soap)
                except (OSError, ValueError, http.client.HTTPException):
                    removed = False
        else:
            owned = False
            active = sock
            try:
                if active is None:
                    active = _bind_socket(mapping.internal_port)
                    owned = True
                removed = bool(self._natpmp_remove(active, self._gateway, mapping.internal_port, mapping.external_port))
            except (OSError, TimeoutError):
                removed = False
            finally:
                if owned and active is not None:
                    active.close()
        rdc_logging.log_event(
            MODULE,
            "mapping.delete",
            level="INFO" if removed else "WARNING",
            reason=REASON_OK if removed else MAPPING_LEASE_FAILED,
            mapping_protocol=mapping.protocol,
            mapping_external_port=mapping.external_port,
            mapping_removed=removed,
        )
        return removed

    def _description(self, mapping: MappingOutcome) -> str:
        return str(mapping.detail.get("description", "rdc"))

    def _log_outcome(self, outcome: MappingOutcome, started: float) -> MappingOutcome:
        rdc_logging.log_event(
            MODULE,
            "mapping.result",
            level="WARNING" if not outcome.ok else "INFO",
            duration_ms=round((self._clock() - started) * 1000.0, 3),
            reason=outcome.reason,
            **outcome.as_event(),
        )
        return outcome


# -------------------------------- 外部一致性校验 --------------------------------


def reconcile_with_stun(
    outcome: MappingOutcome,
    stun_address: str,
    probe: Callable[[str, int], bool] | None = None,
) -> MappingOutcome:
    """把映射结果与 STUN 反射地址对齐, 判定能否作为直连候选.

    - 外部地址非公网 -> 降级(CGNAT/多层 NAT);
    - 与 STUN 不一致 -> 视为多层 NAT, 不宣称可用;
    - 提供外部探针且失败 -> 外部不可达;
    - 通过 -> ``verification`` 升级为 external.
    """
    if not outcome.ok:
        return outcome
    public, public_reason = is_public_ipv4(outcome.external_address)
    if not public:
        return replace(
            outcome,
            ok=False,
            reason=MAPPING_EXTERNAL_MISMATCH,
            detail={**outcome.detail, "external_reason": public_reason},
        )
    if stun_address and stun_address != outcome.external_address:
        return replace(
            outcome,
            ok=False,
            reason=MAPPING_EXTERNAL_MISMATCH,
            detail={
                **outcome.detail,
                "external_address": outcome.external_address,
                "stun_address": stun_address,
                "note": "路由器外部地址与 STUN 反射不一致, 疑似多层 NAT 或 CGNAT",
            },
        )
    if probe is not None:
        try:
            reachable = probe(outcome.external_address, outcome.external_port)
        except (OSError, TimeoutError):
            reachable = False
        if not reachable:
            return replace(
                outcome,
                ok=False,
                reason=MAPPING_EXTERNAL_UNREACHABLE,
                detail={**outcome.detail, "external_probe": "failed"},
            )
        return replace(outcome, verification=VERIFY_EXTERNAL, detail={**outcome.detail, "external_probe": "ok"})
    return outcome


def mapped_candidate(outcome: MappingOutcome) -> Candidate | None:
    """把可用映射转成 kind="mapped" 候选; 不可用返回 None."""
    if not outcome.ok or not outcome.external_address or outcome.external_port <= 0:
        return None
    public, _reason = is_public_ipv4(outcome.external_address)
    if not public:
        return None
    priority = MAPPED_PRIORITY_EXTERNAL if outcome.verification == VERIFY_EXTERNAL else MAPPED_PRIORITY_READBACK
    return Candidate(
        kind=KIND_MAPPED,
        family=4,
        address=outcome.external_address,
        port=outcome.external_port,
        priority=priority,
        related=outcome.protocol,
    )


# -------------------------------- 生命周期 --------------------------------


_ACTIVE_LEASES: set[MappingLease] = set()
_LEASE_LOCK = threading.Lock()


class MappingLease:
    """映射租约: 按租期一半续租, 过期即失效并清理, 异常路径由 ``close_all_mappings`` 兜底.

    续租失败不会无限重试: 连续失败达到 ``MAX_RENEW_FAILURES`` 次即判定租约到期, 把映射
    降级为 ``ok=False`` + ``REASON_LEASE_EXPIRED``, 删除路由器上的条目并退出续租线程.
    这样日志与返回结构能明确区分「租约到期」与「外部不可达」, 也不会给路由器留残规则.
    """

    def __init__(
        self,
        mapper: PortMapper,
        mapping: MappingOutcome,
        *,
        lease_s: int | None = None,
        sock: socket.socket | None = None,
    ) -> None:
        self._mapper = mapper
        self._mapping = mapping
        self._lease_s = max(MIN_LEASE_S, lease_s or mapping.lease_s or DEFAULT_LEASE_S)
        self._sock = sock
        self._stop = threading.Event()
        self._closed = False
        self._expired = False
        self._failures = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def mapping(self) -> MappingOutcome | None:
        """当前映射; 租约到期后返回降级结果(ok=False), 关闭后返回 None."""
        return None if self._closed else self._mapping

    @property
    def expired(self) -> bool:
        """租约是否已判定到期并完成清理."""
        return self._expired

    @property
    def renewal_interval_s(self) -> float:
        """按租期一半续租."""
        return self._lease_s / 2.0

    def start(self) -> None:
        """登记到全局清理表并启动续租线程; 未成功映射时不启动."""
        if not self._mapping.ok or self._thread is not None:
            return
        with _LEASE_LOCK:
            _ACTIVE_LEASES.add(self)
        self._thread = threading.Thread(target=self._renew_loop, name="rdc-mapping-renew", daemon=True)
        self._thread.start()

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.renewal_interval_s):
            if not self.renew_once():
                return

    def renew_once(self) -> bool:
        """执行一次续租; 返回租约是否仍然有效(False 表示已到期并清理完毕)."""
        with self._lock:
            if self._closed or self._expired:
                return False
            current = self._mapping
        try:
            renewed = self._mapper.renew(current, self._lease_s, self._sock)
        except (OSError, ValueError, RuntimeError) as exc:
            # 单次续租遇到本地网络异常只计一次失败, 不能把续租线程打死, 更不能留残规则
            renewed = replace(
                current,
                ok=False,
                reason=MAPPING_LEASE_FAILED,
                detail={**current.detail, "error": type(exc).__name__},
            )
        if renewed.ok:
            with self._lock:
                if self._closed:
                    # 续租与关闭并发: 关闭已删除条目, 这里重发的规则必须立刻删掉
                    self._mapper.delete(renewed, self._sock)
                    return False
                self._mapping = renewed
                self._failures = 0
            return True
        self._failures += 1
        rdc_logging.log_event(
            MODULE,
            "mapping.renew_failed",
            level="WARNING",
            reason=renewed.reason,
            renew_failures=self._failures,
            **renewed.as_event(),
        )
        if self._failures < MAX_RENEW_FAILURES:
            return True
        self._expire(current, renewed)
        return False

    def _expire(self, active: MappingOutcome, last: MappingOutcome) -> None:
        """判定租约到期: 降级映射、删除路由器条目、退出活动表."""
        with self._lock:
            if self._expired or self._closed:
                return
            self._expired = True
            self._mapping = replace(
                active,
                ok=False,
                reason=REASON_LEASE_EXPIRED,
                detail={
                    **active.detail,
                    "expired_after_failures": self._failures,
                    "last_reason": last.reason,
                },
            )
            expired = self._mapping
        self._stop.set()
        self._mapper.delete(active, self._sock)
        with _LEASE_LOCK:
            _ACTIVE_LEASES.discard(self)
        rdc_logging.log_event(
            MODULE,
            "mapping.lease_expired",
            level="WARNING",
            reason=REASON_LEASE_EXPIRED,
            last_reason=last.reason,
            renew_failures=self._failures,
            mapping_protocol=expired.protocol,
            mapping_external_port=expired.external_port,
            mapping_reason_text=describe_reason(REASON_LEASE_EXPIRED),
        )

    def close(self) -> None:
        """停止续租并删除映射; 重复调用不重复删除, 到期已清理的租约不再重复删除."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            expired = self._expired
            mapping = self._mapping
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if not expired:
            self._mapper.delete(mapping, self._sock)
        with _LEASE_LOCK:
            _ACTIVE_LEASES.discard(self)

    def __enter__(self) -> MappingLease:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


def close_all_mappings() -> int:
    """删除所有仍在活动的映射, 返回清理条数; 供会话结束与进程退出兜底调用."""
    with _LEASE_LOCK:
        leases = list(_ACTIVE_LEASES)
    for lease in leases:
        lease.close()
    return len(leases)


atexit.register(close_all_mappings)
