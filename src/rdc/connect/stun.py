# ==========================================================================================
# 文件        : stun.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 真实 STUN 客户端: Binding 请求构造、响应解析、srflx 候选发现
# 架构定位    : 连接策略的“真实 collector”组成部分, 只做 RFC 5389 最小子集
# 核心功能    : XOR-MAPPED-ADDRESS 解析; 49152-65535 端口池绑定; 800ms/1.5s 预算与重试
# 职责边界    : 不判定 NAT 类型(由 policy.classify_mapping 负责); 不建 TCP; 不读写文件
# 运行环境    : Python 3.12
# 依赖关系    : 被 src/rdc/connect/runtime.py 调用; 返回 policy.Candidate
# ==========================================================================================

"""STUN Binding 客户端.

服务端默认使用已实测可用的 <ECS-PUBLIC-IP>:3478 (UDP), 可用环境变量
``RDC_STUN_SERVERS="host:port,host:port"`` 覆盖. 映射行为判定需要两个服务;
只有一个成功时映射分类为 unknown, 不直接判对称型.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import select
import socket
import struct
import time

from rdc.connect.policy import STUN_TIMEOUT_MS, STUN_TOTAL_MS, Candidate

MAGIC_COOKIE = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
ATTR_XOR_MAPPED_ADDRESS = 0x0020
PORT_POOL_START = 49152
PORT_POOL_END = 65535
DEFAULT_STUN_SERVERS: tuple[tuple[str, int], ...] = (("47.109.32.249", 3478), ("106.12.251.193", 3478))


def stun_servers() -> tuple[tuple[str, int], ...]:
    """读取 STUN 服务列表; 环境变量格式 ``host:port,host:port``."""
    text = os.environ.get("RDC_STUN_SERVERS", "").strip()
    if not text:
        return DEFAULT_STUN_SERVERS
    parsed: list[tuple[str, int]] = []
    for item in text.split(","):
        host, _, port = item.strip().partition(":")
        if host and port.isdigit():
            parsed.append((host, int(port)))
    return tuple(parsed) or DEFAULT_STUN_SERVERS


def build_binding_request() -> tuple[bytes, bytes]:
    """构造 Binding 请求, 返回 (报文, 事务 ID)."""
    txid = secrets.token_bytes(12)
    return struct.pack(">HHI", BINDING_REQUEST, 0, MAGIC_COOKIE) + txid, txid


def _xor_address(family: int, raw: bytes) -> str:
    cookie = struct.pack(">I", MAGIC_COOKIE)
    if family == 0x01:
        return socket.inet_ntop(socket.AF_INET, bytes(a ^ b for a, b in zip(raw, cookie, strict=False)))
    mask = cookie + b"\x00" * 12
    return socket.inet_ntop(socket.AF_INET6, bytes(a ^ b for a, b in zip(raw, mask, strict=False)))


def parse_binding_response(data: bytes, txid: bytes) -> tuple[str, int]:
    """解析 Binding 成功响应, 返回 (映射地址, 映射端口); 非法报文抛 ValueError."""
    if len(data) < 20:
        raise ValueError("STUN 响应长度不足")
    msg_type, length, cookie = struct.unpack(">HHI", data[:8])
    if cookie != MAGIC_COOKIE or data[8:20] != txid:
        raise ValueError("STUN 响应校验失败: cookie 或事务 ID 不匹配")
    if msg_type != BINDING_SUCCESS:
        raise ValueError(f"STUN 响应类型异常: 0x{msg_type:04X}")
    offset = 20
    end = min(len(data), 20 + length)
    while offset + 4 <= end:
        attr_type, attr_len = struct.unpack(">HH", data[offset : offset + 4])
        value = data[offset + 4 : offset + 4 + attr_len]
        if attr_type == ATTR_XOR_MAPPED_ADDRESS and len(value) >= 8:
            family = value[1]
            (xport,) = struct.unpack(">H", value[2:4])
            port = xport ^ (MAGIC_COOKIE >> 16)
            return _xor_address(family, value[4:]), port
        offset += 4 + ((attr_len + 3) // 4) * 4
    raise ValueError("STUN 响应缺少 XOR-MAPPED-ADDRESS")


def bind_udp_in_pool(family: int = socket.AF_INET) -> socket.socket:
    """在 49152-65535 端口池内绑定一个 UDP socket, 全占用时回退到系统临时端口."""
    host = "::" if family == socket.AF_INET6 else ""
    for _ in range(32):
        port = PORT_POOL_START + secrets.randbelow(PORT_POOL_END - PORT_POOL_START + 1)
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.bind((host, port))
        except OSError:
            sock.close()
            continue
        return sock
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.bind((host, 0))
    return sock


def _server_endpoint(sock: socket.socket, server: tuple[str, int]) -> tuple[str, int]:
    """把 STUN 服务地址转成当前 socket 可发送的形式.

    被控端打洞 socket 是 ``AF_INET6`` 双栈, 直接 ``sendto`` IPv4 元组会报地址族
    错误; 这里把 IPv4 服务转成 v4-mapped 形式, 源端口仍是打洞端口, 映射结果不变.
    """
    host, port = server
    if sock.family != socket.AF_INET6:
        return (host, port)
    try:
        if ipaddress.ip_address(host).version == 6:
            return (host, port)
        return (f"::ffff:{host}", port)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return (host, port)
    if not infos:
        return (host, port)
    return (f"::ffff:{infos[0][4][0]}", port)


def query_server(
    sock: socket.socket,
    server: tuple[str, int],
    timeout_ms: int = STUN_TIMEOUT_MS,
) -> tuple[str, int, float]:
    """向单个 STUN 服务发一次 Binding 请求, 返回 (地址, 端口, RTT 毫秒)."""
    request, txid = build_binding_request()
    started = time.monotonic()
    sock.sendto(request, _server_endpoint(sock, server))
    deadline = started + max(0.05, timeout_ms / 1000.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"STUN {server[0]}:{server[1]} 超时")
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            raise TimeoutError(f"STUN {server[0]}:{server[1]} 超时")
        data, _addr = sock.recvfrom(2048)
        try:
            address, port = parse_binding_response(data, txid)
        except ValueError:
            continue
        return address, port, (time.monotonic() - started) * 1000.0


def discover_srflx(
    sock: socket.socket,
    servers: tuple[tuple[str, int], ...] | None = None,
    per_timeout_ms: int = STUN_TIMEOUT_MS,
    total_ms: int = STUN_TOTAL_MS,
) -> tuple[tuple[Candidate, ...], str]:
    """在同一 socket 上依次查询 STUN 服务, 返回 srflx 候选与最后一次错误.

    必须复用同一 socket: 不同 socket 的映射端口不同, 会让映射行为判定失真.
    """
    candidates: list[Candidate] = []
    error = ""
    deadline = time.monotonic() + max(0.1, total_ms / 1000.0)
    for server in servers or stun_servers():
        remaining_ms = (deadline - time.monotonic()) * 1000.0
        if remaining_ms <= 20:
            error = error or "STUN 总预算耗尽"
            break
        try:
            address, port, _rtt = query_server(sock, server, min(per_timeout_ms, int(remaining_ms)))
        except (OSError, TimeoutError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        family = 6 if ":" in address else 4
        candidates.append(Candidate(kind="srflx", family=family, address=address, port=port, priority=90))
    return tuple(candidates), error
