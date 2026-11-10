# ==========================================================================================
# 文件        : server.py
# 所属目录    : src/rdc/relay/
# 文件用途    : 信令与中继服务的可部署入口: TCP 信令接入、HTTP 健康检查、并发与资源上限
# 架构定位    : 中继服务层, 只依赖协议与加密端口, 不导入界面、媒体与客户端会话
# 核心功能    : INI 配置; 长度前缀帧收发; 设备连接登记与消息投递; 连接上限与限流; 健康检查
# 职责边界    : 不转发媒体明文, 不落盘会话内容, 不修改系统网络配置
# 运行环境    : Ubuntu 20.04/22.04 x86_64, Python 3.8 及以上
# 依赖关系    : rdc.signaling.server、rdc.signaling.envelope、rdc.relay.tls、rdc.core.logging
# ==========================================================================================

"""可部署的信令与中继服务骨架.

帧格式: 4 字节大端无符号长度 + UTF-8 JSON 包络.
健康检查: HTTP GET /healthz, 只监听回环地址.

加固要点:
    - 连接总数、每 IP 连接数与新建连接速率三道上限, 以及 IP 黑白名单.
    - 认证成功后登记设备连接, 路由消息真正投递到对端连接, 断开即清理.
    - TLS 上下文可在收到 SIGHUP 后热重载, 不中断监听.
"""

from __future__ import annotations

import configparser
import ipaddress
import json
import math
import os
import signal
import socket
import ssl
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rdc.accounts.api import AccountApi, looks_like_http
from rdc.accounts.server import AccountCenter
from rdc.connect.policy import is_global_ipv6
from rdc.core.errors import AUTH_FAILED, RATE_LIMITED, TOKEN_REVOKED
from rdc.core.logging import configure, log_event
from rdc.relay.revocation import AccountRevocationProbe
from rdc.relay.tls import (
    TLS_MIN_VERSION_NAME,
    build_server_context,
    certificate_days_left,
    certificate_not_after_epoch,
)
from rdc.signaling.envelope import (
    PROTOCOL_VERSION,
    ProtocolError,
    build_envelope,
    validate_envelope,
    verify_envelope,
)
from rdc.signaling.server import DEFAULT_CONTROL_GRANT_TTL_S, InMemorySignalingServer

# 单帧上限 1 MiB, 超过直接断开, 防止内存被撑爆
MAX_FRAME_BYTES = 1 << 20
RELAY_PROFILES = ("L0", "L1", "L2", "L3")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_CONNECTIONS_PER_IP = 8
DEFAULT_CONNECT_RATE_PER_MIN = 60
DEFAULT_MIN_SESSION_KBPS = 200
DEFAULT_HANDSHAKE_TIMEOUT_S = 30.0
RATE_WINDOW_S = 60.0

# 会进入路由与预算统计的消息类型, 只用于记日志, 不改变协议语义
RELAY_REQUEST_TYPES = frozenset(
    {"offer", "answer", "candidate", "session_start", "session_end", "display_update", "relay_allocate"}
)

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class RelayConfig:
    """服务配置, 来自 INI 文件, 不写死在代码里.

    :param bind: 信令监听地址
    :param port: 信令监听端口
    :param health_bind: 健康检查监听地址, 固定回环
    :param health_port: 健康检查端口
    :param relay_endpoint: 对外公布的中继地址
    :param egress_mbps: 出口带宽, 中继预算的唯一来源
    :param overhead_ratio: 协议与重传开销比例
    :param max_sessions: 单节点最大并发会话数
    :param degrade_profile: 默认中继降档档位
    :param dataplane_enabled: 是否启用中继数据面; 信令独立部署时置 false, 只保留信令与预算授予
    :param tls_enabled: 是否启用 TLS 1.3; False 时保持原明文 TCP 行为
    :param cert_file: 服务端证书链路径, TLS 启用时必填
    :param key_file: 服务端私钥路径, TLS 启用时必填
    :param ca_file: 客户端证书校验用 CA 路径, 双向校验时必填
    :param require_client_cert: 是否要求并校验客户端证书
    :param max_connections: 同时存在的 TCP 连接上限
    :param max_connections_per_ip: 单个来源 IP 的连接上限
    :param connect_rate_per_min: 单个来源 IP 每分钟新建连接上限, 0 表示关闭
    :param allow_ips: 允许来源的 IP 或 CIDR, 逗号分隔, 空表示不限制
    :param deny_ips: 拒绝来源的 IP 或 CIDR, 逗号分隔, 优先级高于 allow_ips
    :param open_ports: 对外开放来源的监听端口; 这些端口上白名单之外的全局 IPv6 来源可进入认证流程,
        认证与连接码校验照旧, deny_ips 仍优先. 默认空表示所有端口都按 allow_ips 收口
    :param min_session_kbps: 单会话最低预算, 低于此值不下发授予
    :param target_kbps: 固定画质档的目标码率, 0 表示不校验; 超过单会话预算时启动即失败
    :param profile_width: 固定档分辨率宽, 供客户端与编码器读取
    :param profile_height: 固定档分辨率高
    :param profile_fps: 固定档帧率
    :param preview_max_kbps: 屏幕墙预览单路预算上限, 不随会话预算无限放大
    :param log_level: 结构化日志级别, 默认 INFO, Debug 不常开
    """

    bind: str
    port: int
    health_bind: str
    health_port: int
    relay_endpoint: str
    egress_mbps: int
    overhead_ratio: float
    max_sessions: int
    degrade_profile: str
    dataplane_enabled: bool = True
    # 额外监听端口: 与主端口共用同一套 accept 逻辑; 单个端口绑定失败只降级该端口
    extra_ports: tuple[int, ...] = ()
    # 对外开放来源的端口: 仅供 WAN 入站使用; 只放宽来源白名单, 不放宽认证与连接码
    open_ports: tuple[int, ...] = ()
    tls_enabled: bool = False
    cert_file: str = ""
    key_file: str = ""
    ca_file: str = ""
    require_client_cert: bool = False
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_connections_per_ip: int = DEFAULT_MAX_CONNECTIONS_PER_IP
    connect_rate_per_min: int = DEFAULT_CONNECT_RATE_PER_MIN
    allow_ips: str = ""
    deny_ips: str = ""
    min_session_kbps: int = DEFAULT_MIN_SESSION_KBPS
    target_kbps: int = 0
    profile_width: int = 1280
    profile_height: int = 720
    profile_fps: int = 30
    preview_max_kbps: int = 300
    log_level: str = "INFO"
    # control 授予有效期: 决定控制端被强杀后中继名额的最长占用时间
    control_grant_ttl_s: int = DEFAULT_CONTROL_GRANT_TTL_S

    @property
    def budget_kbps(self) -> int:
        """单会话可用预算, 单位 kbps, 向下取整."""
        return math.floor(self.egress_mbps * 1000 * (1 - self.overhead_ratio) / self.max_sessions)


def load_config(path: str | Path) -> RelayConfig:
    """读取并校验 INI 配置.

    :param path: 配置文件路径
    :return: 校验通过的配置
    :raises ValueError: 取值越界、档位未知或名单无法解析
    """
    parser = configparser.ConfigParser()
    if not parser.read(str(path), encoding="utf-8"):
        raise ValueError(f"配置文件不可读: {path}")
    server = parser["server"]
    relay = parser["relay"]
    config = RelayConfig(
        bind=server.get("bind", "127.0.0.1"),
        port=server.getint("port", 8750),
        health_bind=server.get("health_bind", "127.0.0.1"),
        health_port=server.getint("health_port", 8751),
        relay_endpoint=relay.get("endpoint", "relay://127.0.0.1:8750"),
        egress_mbps=relay.getint("egress_mbps", 3),
        overhead_ratio=relay.getfloat("overhead_ratio", 0.08),
        max_sessions=relay.getint("max_sessions", 1),
        degrade_profile=relay.get("degrade_profile", "L0"),
        dataplane_enabled=relay.getboolean("dataplane_enabled", True),
        extra_ports=_parse_port_list(_section_text(parser, "server", "extra_ports")),
        open_ports=_parse_port_list(_section_text(parser, "server", "open_ports")),
        tls_enabled=_section_flag(parser, "tls", "tls_enabled", False),
        cert_file=_section_text(parser, "tls", "cert_file"),
        key_file=_section_text(parser, "tls", "key_file"),
        ca_file=_section_text(parser, "tls", "ca_file"),
        require_client_cert=_section_flag(parser, "tls", "require_client_cert", False),
        max_connections=server.getint("max_connections", DEFAULT_MAX_CONNECTIONS),
        max_connections_per_ip=server.getint("max_connections_per_ip", DEFAULT_MAX_CONNECTIONS_PER_IP),
        connect_rate_per_min=server.getint("connect_rate_per_min", DEFAULT_CONNECT_RATE_PER_MIN),
        allow_ips=_section_text(parser, "security", "allow_ips"),
        deny_ips=_section_text(parser, "security", "deny_ips"),
        min_session_kbps=relay.getint("min_session_kbps", DEFAULT_MIN_SESSION_KBPS),
        target_kbps=relay.getint("target_kbps", 0),
        profile_width=relay.getint("profile_width", 1280),
        profile_height=relay.getint("profile_height", 720),
        profile_fps=relay.getint("profile_fps", 30),
        preview_max_kbps=_section_int(parser, "wall_preview", "max_session_kbps", 300),
        log_level=(_section_text(parser, "logging", "level") or "INFO").upper(),
        control_grant_ttl_s=relay.getint("control_grant_ttl_s", DEFAULT_CONTROL_GRANT_TTL_S),
    )
    if not 1 <= config.port <= 65535 or not 1 <= config.health_port <= 65535:
        raise ValueError("端口必须在 1 至 65535 之间")
    if config.egress_mbps <= 0:
        raise ValueError("egress_mbps 必须为正整数")
    if not 0 <= config.overhead_ratio < 1:
        raise ValueError("overhead_ratio 必须落在 [0, 1) 区间")
    if config.max_sessions <= 0:
        raise ValueError("max_sessions 必须为正整数")
    if config.degrade_profile not in RELAY_PROFILES:
        raise ValueError(f"degrade_profile 必须是 {RELAY_PROFILES} 之一")
    if config.max_connections <= 0:
        raise ValueError("max_connections 必须为正整数")
    if config.max_connections_per_ip <= 0:
        raise ValueError("max_connections_per_ip 必须为正整数")
    if config.max_connections_per_ip > config.max_connections:
        raise ValueError("max_connections_per_ip 不得大于 max_connections")
    if config.connect_rate_per_min < 0:
        raise ValueError("connect_rate_per_min 不得为负数")
    if config.min_session_kbps <= 0:
        raise ValueError("min_session_kbps 必须为正整数")
    if config.target_kbps < 0:
        raise ValueError("target_kbps 不得为负数")
    if config.profile_width <= 0 or config.profile_height <= 0 or config.profile_fps <= 0:
        raise ValueError("profile_width/profile_height/profile_fps 必须为正整数")
    if config.preview_max_kbps <= 0:
        raise ValueError("wall_preview.max_session_kbps 必须为正整数")
    if config.control_grant_ttl_s <= 0:
        raise ValueError("control_grant_ttl_s 必须为正整数")
    if config.target_kbps > config.budget_kbps:
        raise ValueError(
            f"固定档目标码率 {config.target_kbps} kbps 超过单会话预算 {config.budget_kbps} kbps; "
            "请调整 egress_mbps / max_sessions / overhead_ratio"
        )
    if config.log_level not in LOG_LEVELS:
        raise ValueError(f"日志级别必须是 {LOG_LEVELS} 之一")
    parse_ip_networks(config.allow_ips, "allow_ips")
    parse_ip_networks(config.deny_ips, "deny_ips")
    _validate_tls(config)
    return config


def _parse_port_list(text: str) -> tuple[int, ...]:
    """解析逗号分隔的额外监听端口; 空串表示关闭."""
    ports: list[int] = []
    for item in text.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            port = int(value)
        except ValueError as error:
            raise ValueError(f"extra_ports 含非法端口 {value}") from error
        if not 1 <= port <= 65535:
            raise ValueError(f"extra_ports 端口越界: {port}")
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def parse_ip_networks(text: str, key: str) -> tuple[IPNetwork, ...]:
    """把逗号分隔的 IP 或 CIDR 解析为网段元组; 空串表示空名单.

    :param text: 配置原文
    :param key: 配置键名, 只用于错误提示
    :raises ValueError: 任一项无法解析
    """
    networks: list[IPNetwork] = []
    for item in text.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as error:
            raise ValueError(f"{key} 含非法地址 {value}") from error
    return tuple(networks)


def ip_matches(address: str, networks: tuple[IPNetwork, ...]) -> bool:
    """判断来源地址是否落在给定网段内; 地址不可解析返回 False."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed.version == network.version and parsed in network for network in networks)


def normalize_peer(address: str) -> str:
    """把来源地址归一成可比文本.

    双栈监听会把 IPv4 来源表示成 ``::ffff:a.b.c.d``, 这里还原成 IPv4, 保证
    来源白名单、每 IP 限流与日志口径与单栈时一致; 链路本地地址带接口区号,
    归一后去掉区号再参与网段匹配.
    """
    raw = address.split("%", 1)[0].strip()
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return address
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


# 只有"全部地址"才走双栈; 显式 0.0.0.0 与具体地址保持单栈, 不改变已有部署行为
DUAL_STACK_BINDS = frozenset({"", "::", "[::]"})


def claim_port_exclusively(sock: socket.socket) -> None:
    """给监听套接字设置端口占用策略, 防止第二进程重复绑定抢走连接.

    Windows 的 ``SO_REUSEADDR`` 语义是"允许重复绑定": 第二个实例只要也设置该
    选项, 就能静默绑同一端口并分走新连接(实测第二个 REUSEADDR 监听返回成功,
    第二个独占/默认监听返回 WSAEADDRINUSE). 重启复用已验证在存在 TIME_WAIT
    记录时仍可绑定, 所以 Windows 改用 ``SO_EXCLUSIVEADDRUSE`` 真正独占.

    POSIX 下 ``SO_REUSEADDR`` 只影响 TIME_WAIT 复用, 不允许两个存活监听并存
    (那需要 ``SO_REUSEPORT``), 也不存在 ``SO_EXCLUSIVEADDRUSE``, 保持原样.
    """
    if os.name == "nt":
        extension = socket.SO_EXCLUSIVEADDRUSE
        sock.setsockopt(socket.SOL_SOCKET, extension, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def create_listener(bind: str, port: int, backlog: int) -> socket.socket:
    """创建 TCP 监听套接字: 空地址按 IPv4/IPv6 双栈, 显式地址按地址族单栈.

    Windows 自 Vista 起 ``IPV6_V6ONLY`` 默认为 1, Linux 由
    ``net.ipv6.bindv6only`` 决定(发行版默认 0); 只有显式置 0 才能让两端行为
    一致, 同一端口同时接受 IPv4 与 IPv6 连接.
    """
    target = bind.strip()
    listener = socket.socket(socket.AF_INET6 if target in DUAL_STACK_BINDS else socket.AF_INET, socket.SOCK_STREAM)
    address = "::" if target in DUAL_STACK_BINDS else target
    try:
        claim_port_exclusively(listener)
        if listener.family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind((address, port))
        listener.listen(backlog)
    except OSError:
        listener.close()
        raise
    return listener


def _section_text(parser: configparser.ConfigParser, section: str, key: str) -> str:
    """读取可选的字符串配置, 段或键不存在时返回空串."""
    if not parser.has_section(section):
        return ""
    return parser.get(section, key, fallback="").strip()


def _section_int(parser: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    """读取可选的整数配置, 非法取值直接报错而不是静默降级."""
    if not parser.has_section(section) or not parser.has_option(section, key):
        return default
    try:
        return parser.getint(section, key)
    except ValueError as error:
        raise ValueError(f"配置项 {key} 必须是整数") from error


def _section_flag(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    """读取可选的布尔配置, 非法取值直接报错而不是静默降级."""
    if not parser.has_section(section) or not parser.has_option(section, key):
        return default
    try:
        return parser.getboolean(section, key)
    except ValueError as error:
        raise ValueError(f"配置项 {key} 必须是布尔值") from error


def _validate_tls(config: RelayConfig) -> None:
    """TLS 配置校验: 启用即必须有可读证书; 双向校验必须有 CA."""
    if not config.tls_enabled:
        if config.require_client_cert:
            raise ValueError("require_client_cert=true 时必须同时开启 tls_enabled")
        return
    if not config.cert_file or not config.key_file:
        raise ValueError("启用 TLS 时必须配置 cert_file 与 key_file")
    if not Path(config.cert_file).is_file():
        raise ValueError(f"cert_file 文件不存在: {config.cert_file}")
    if not Path(config.key_file).is_file():
        raise ValueError(f"key_file 文件不存在: {config.key_file}")
    if config.require_client_cert:
        if not config.ca_file:
            raise ValueError("require_client_cert=true 时必须配置 ca_file")
        if not Path(config.ca_file).is_file():
            raise ValueError(f"ca_file 文件不存在: {config.ca_file}")


def recv_frame(connection: socket.socket, header: bytes | None = None) -> bytes | None:
    """读取一帧, 对端正常关闭返回 None.

    :param connection: 已连接套接字
    :param header: 已读出的 4 字节长度头; 连接首帧分流时需要
    :return: 帧负载
    :raises ValueError: 长度越界
    """
    if header is None:
        header = _recv_exactly(connection, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ValueError(f"帧长度非法: {length}")
    payload = _recv_exactly(connection, length)
    if payload is None:
        raise ValueError("帧负载不完整")
    return payload


def send_frame(connection: socket.socket, payload: dict[str, Any]) -> None:
    """发送一帧 JSON."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    connection.sendall(struct.pack(">I", len(body)) + body)


def _recv_exactly(connection: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = connection.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


class ConnectionGuard:
    """连接准入: 黑白名单、每 IP 新建速率、每 IP 并发与全局并发的上限.

    开放端口(``open_ports``)只放宽来源判定: 白名单之外的全局 IPv6 可进入认证流程,
    deny 名单、速率、并发与后续设备认证/连接码校验都不变.

    :param config: 服务配置
    """

    def __init__(self, config: RelayConfig) -> None:
        self._max_total = config.max_connections
        self._max_per_ip = config.max_connections_per_ip
        self._rate_per_min = config.connect_rate_per_min
        self._allow = parse_ip_networks(config.allow_ips, "allow_ips")
        self._deny = parse_ip_networks(config.deny_ips, "deny_ips")
        self._open_ports = frozenset(config.open_ports)
        self._total = 0
        self._per_ip: dict[str, int] = {}
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.rejected = {"denied": 0, "rate_limited": 0, "too_many": 0}

    def admits_public_source(self, peer: str, port: int) -> bool:
        """该来源是否因开放端口而越过 allow 白名单; 仅全局 IPv6 成立, deny 名单优先."""
        if port not in self._open_ports:
            return False
        if ip_matches(peer, self._deny):
            return False
        if self._allow and ip_matches(peer, self._allow):
            return False
        return is_global_ipv6(peer)

    def admit(self, peer: str, port: int = 0) -> tuple[bool, str]:
        """判断是否放行一个新连接; 放行后调用方必须在 finally 里 release.

        拒绝原因: ``source_denied`` 来源不在白名单、``deny_listed`` 命中拒绝名单、
        ``rate_limited`` 新建过快、``too_many`` 并发超额.
        """
        address_allowed = ip_matches(peer, self._allow) if self._allow else True
        if not address_allowed and self.admits_public_source(peer, port):
            address_allowed = True
        if ip_matches(peer, self._deny):
            with self._lock:
                self.rejected["denied"] += 1
            return False, "deny_listed"
        if not address_allowed:
            with self._lock:
                self.rejected["denied"] += 1
            return False, "source_denied"
        now = time.monotonic()
        with self._lock:
            if self._rate_per_min > 0:
                hits = self._hits.setdefault(peer, deque())
                while hits and now - hits[0] > RATE_WINDOW_S:
                    hits.popleft()
                hits.append(now)
                if len(hits) > self._rate_per_min:
                    self.rejected["rate_limited"] += 1
                    return False, "rate_limited"
                if len(self._hits) > 2048:
                    # 冷静期外的来源不再保留计数, 避免长跑进程按 IP 无限增长
                    stale_keys = [
                        key for key, value in self._hits.items() if not value or now - value[-1] > RATE_WINDOW_S
                    ]
                    for stale in stale_keys:
                        self._hits.pop(stale, None)
            if self._total >= self._max_total:
                self.rejected["too_many"] += 1
                return False, "too_many"
            if self._per_ip.get(peer, 0) >= self._max_per_ip:
                self.rejected["too_many"] += 1
                return False, "too_many"
            self._total += 1
            self._per_ip[peer] = self._per_ip.get(peer, 0) + 1
        return True, ""

    def release(self, peer: str) -> None:
        """归还一个连接名额."""
        with self._lock:
            self._total = max(0, self._total - 1)
            current = self._per_ip.get(peer, 0)
            if current <= 1:
                self._per_ip.pop(peer, None)
            else:
                self._per_ip[peer] = current - 1

    def snapshot(self) -> dict[str, Any]:
        """返回连接计数与拒绝原因, 不含来源明细."""
        with self._lock:
            return {
                "active": self._total,
                "max": self._max_total,
                "per_ip_max": self._max_per_ip,
                "connect_rate_per_min": self._rate_per_min,
                "distinct_ips": len(self._per_ip),
                "rejected": dict(self.rejected),
            }


@dataclass
class _ClientConnection:
    sock: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)


class ConnectionRegistry:
    """已认证设备到连接的登记表, 供信令层把消息真正投递给对端."""

    def __init__(self) -> None:
        self._clients: dict[str, _ClientConnection] = {}
        self._lock = threading.Lock()

    def attach(self, device_id: str, sock: socket.socket) -> socket.socket | None:
        """登记设备连接; 同一设备重复认证时返回被替换的旧连接."""
        with self._lock:
            previous = self._clients.pop(device_id, None)
            self._clients[device_id] = _ClientConnection(sock=sock)
        return previous.sock if previous is not None else None

    def detach(self, device_id: str, sock: socket.socket) -> bool:
        """仅当登记表里仍是这条连接时才移除, 避免误删重连后的新连接."""
        with self._lock:
            current = self._clients.get(device_id)
            if current is not None and current.sock is sock:
                self._clients.pop(device_id, None)
                return True
            return False

    def remove(self, device_id: str) -> _ClientConnection | None:
        """取出并移除设备连接, 供登出或吊销时立即断开."""
        with self._lock:
            return self._clients.pop(device_id, None)

    def deliver(self, device_id: str, envelope: dict[str, Any]) -> bool:
        """向指定设备的连接投递一条包络, 未连接或写失败返回 False."""
        with self._lock:
            client = self._clients.get(device_id)
        if client is None:
            return False
        try:
            with client.lock:
                send_frame(client.sock, envelope)
        except OSError:
            return False
        return True

    def close_all(self) -> None:
        """关闭全部登记连接, 供服务退出时使用."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            with suppress(OSError):
                client.sock.close()

    def snapshot(self) -> dict[str, int]:
        """返回在线设备连接数."""
        with self._lock:
            return {"devices": len(self._clients)}


def _account_presence_sink(device_id: str, state: str) -> None:
    """默认在线状态落点: 进程内账号服务的 presence 表.

    跨进程部署时由装配处注入共享存储实现, 不得在此处伪造在线.
    """
    from rdc.commands import accounts as accounts_commands

    accounts_commands.get_service().record_presence(device_id, state)


def _account_of(device_id: str) -> str:
    """默认设备到账号解析: 账号库里设备记录的归属账号, 未绑定返回空串.

    屏幕墙的归属校验依赖这条解析, 禁止从消息载荷自报账号.
    """
    from rdc.commands import accounts as accounts_commands

    device = accounts_commands.get_service().store.devices.get(device_id)
    return (device.owner_account_id or "") if device is not None else ""


def _wall_owner_of(wall_id: str) -> str | None:
    """默认墙到账号解析: 账号库的墙归属表优先, 再回退同进程墙服务.

    公网进程里墙服务恒为空, 归属必须落在 accounts.db 才能跨重启生效.
    """
    from rdc.commands import accounts as accounts_commands

    owner = accounts_commands.get_service().store.wall_owner(wall_id)
    if owner:
        return owner
    from rdc.commands import wall as wall_commands

    return wall_commands.get_service().wall_owner(wall_id)


def _wall_bind(wall_id: str, account_id: str, device_id: str) -> tuple[str, str]:
    """默认墙归属落库: 写 accounts.db 的 walls 表; 库不可用时返回 unavailable 由服务端回可重试错误."""
    from rdc.accounts.db import AccountStoreError
    from rdc.commands import accounts as accounts_commands

    try:
        return accounts_commands.get_service().store.bind_wall(wall_id, account_id, device_id)
    except AccountStoreError:
        return "unavailable", ""


class RelayServer:
    """单个 TCP 信令接入线程加一个回环健康检查端口."""

    def __init__(
        self,
        config: RelayConfig,
        presence_sink: Callable[[str, str], None] | None = None,
        account_of: Callable[[str], str] | None = None,
        wall_owner_of: Callable[[str], str | None] | None = None,
        wall_binder: Callable[[str, str, str], tuple[str, str]] | None = None,
        revocation_probe: AccountRevocationProbe | None = None,
        account_center: AccountCenter | None = None,
    ) -> None:
        self.config = config
        # 跨端账号中心: 与信令共用同一条 TLS 监听, 不新增端口
        self.account_center = account_center
        self.account_api = AccountApi(account_center) if account_center is not None else None
        self._heartbeat_presence: dict[str, float] = {}
        if account_center is not None:
            account_center.set_kicker(self._kick_for_accounts)
        self.guard = ConnectionGuard(config)
        self.registry = ConnectionRegistry()
        self.signaling = InMemorySignalingServer(
            relay_endpoint=config.relay_endpoint,
            egress_mbps=config.egress_mbps,
            overhead_ratio=config.overhead_ratio,
            max_sessions=config.max_sessions,
            control_grant_ttl_s=config.control_grant_ttl_s,
            min_session_kbps=config.min_session_kbps,
            preview_max_kbps=config.preview_max_kbps,
            deliver=self.registry.deliver,
            presence_sink=presence_sink or _account_presence_sink,
            account_of=account_of or _account_of,
            wall_owner_of=wall_owner_of or _wall_owner_of,
            wall_binder=wall_binder or _wall_bind,
        )
        # 账号库吊销状态: 生产环境按 RDC_DATA_DIR/accounts.db 观测, 测试可注入临时库
        if revocation_probe is None:
            from rdc.accounts.db import default_database_path

            revocation_probe = AccountRevocationProbe(default_database_path())
        self.revocation = revocation_probe
        # 延迟导入: dataplane 复用本模块的帧收发, 模块级导入会形成循环
        from rdc.relay.dataplane import RelayDataPlane

        self.dataplane: RelayDataPlane | None = (
            RelayDataPlane(
                self._grant_lookup,
                config.max_sessions,
                config.target_kbps,
                token_active=self._token_active,
            )
            if config.dataplane_enabled
            else None
        )
        # 授予一旦被回收(会话结束、登出、吊销、过期), 数据面立即断开对应连接
        self.signaling.grant_revoked = self._on_grant_revoked
        # 上下文在构造期建立: 证书不可用时启动即失败, 不做明文回退
        self.tls_context: ssl.SSLContext | None = (
            build_server_context(
                cert_file=config.cert_file,
                key_file=config.key_file,
                ca_file=config.ca_file,
                require_client_cert=config.require_client_cert,
            )
            if config.tls_enabled
            else None
        )
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._health: ThreadingHTTPServer | None = None
        self._started = time.monotonic()
        # 直连媒体首帧分流: session_id -> 接管函数; 由被控端在一个会话准入后登记
        self._direct_lock = threading.Lock()
        self._direct_handlers: dict[str, Callable[[socket.socket, dict[str, Any]], bool]] = {}
        # 主端口与额外端口共用同一套 accept 逻辑, 这里保存实际绑定成功的监听 socket
        self._listeners: list[tuple[int, socket.socket]] = []
        self.listen_ports: tuple[int, ...] = ()

    def register_direct(
        self,
        session_id: str,
        handler: Callable[[socket.socket, dict[str, Any]], bool],
    ) -> None:
        """登记某会话的直连接管者; handler 返回 True 表示 socket 所有权已转移."""
        with self._direct_lock:
            self._direct_handlers[session_id] = handler

    def unregister_direct(self, session_id: str) -> None:
        """撤销登记; 之后该会话的 e2e_hello 首帧按普通帧处理并关闭."""
        with self._direct_lock:
            self._direct_handlers.pop(session_id, None)

    def serve_forever(self) -> None:
        """阻塞运行, 直到 stop() 或收到终止信号."""
        configure(level=self.config.log_level, console=True)
        self._health = self._build_health_server()
        threading.Thread(target=self._health.serve_forever, name="health", daemon=True).start()
        backlog = min(128, max(1, self.config.max_connections * 2))
        self._listener = create_listener(self.config.bind, self.config.port, backlog)
        self._listener.settimeout(1.0)
        self._listeners = [(self.config.port, self._listener)]
        self._bind_extra_listeners(backlog)
        self.listen_ports = tuple(port for port, _listener in self._listeners)
        self._started = time.monotonic()
        # 定时清理与是否有入站消息无关: 客户端被强杀后也能按周期回收名额
        self.signaling.start_maintenance()
        log_event(
            "relay.server",
            "service.started",
            relay_endpoint=self.config.relay_endpoint,
            port=self.config.port,
            listen_ports=list(self.listen_ports),
            open_ports=list(self.config.open_ports),
            listen_address=str(self._listener.getsockname()[0]),
            listen_family=self._listener.family.name,
            budget_kbps=self.config.budget_kbps,
            max_sessions=self.config.max_sessions,
            max_connections=self.config.max_connections,
            tls_enabled=self.config.tls_enabled,
        )
        for port, listener in self._listeners:
            threading.Thread(
                target=self._accept_loop,
                args=(listener, port),
                name=f"accept-{port}",
                daemon=True,
            ).start()
        self._stop.wait()
        self._shutdown()
        log_event("relay.server", "service.stopped")

    def _bind_extra_listeners(self, backlog: int) -> None:
        """按配置额外监听端口; 单个端口失败只降级该端口并记 WARNING, 不影响主端口."""
        for port in self.config.extra_ports:
            if port == self.config.port:
                continue
            try:
                listener = create_listener(self.config.bind, port, backlog)
            except OSError as error:
                log_event(
                    "relay.server",
                    "listen.extra_failed",
                    level="WARNING",
                    port=port,
                    error=type(error).__name__,
                )
                continue
            listener.settimeout(1.0)
            self._listeners.append((port, listener))
            log_event("relay.server", "listen.extra_ready", port=port)

    def _accept_loop(self, listener: socket.socket, port: int) -> None:
        """统一 accept 循环: 主端口与额外端口共用同一套准入与首帧分流."""
        while not self._stop.is_set():
            try:
                connection, address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                # 监听 socket 在 stop() 时关闭, 属于正常退出
                return
            peer = normalize_peer(str(address[0]))
            allowed, reason = self.guard.admit(peer, port)
            if not allowed:
                fields: dict[str, Any] = {"peer_ip": peer, "port": port, "reason": reason}
                if reason == "rate_limited":
                    fields["error_code"] = RATE_LIMITED
                log_event("relay.server", "connection.rejected", level="WARNING", **fields)
                with suppress(OSError):
                    connection.close()
                continue
            if self.guard.admits_public_source(peer, port):
                # 白名单之外但被开放端口放行: 只进入认证流程, 认证与连接码校验仍在后面
                log_event("relay.server", "connection.open_admitted", peer_ip=peer, port=port, family=6)
            threading.Thread(target=self._handle_connection, args=(connection, peer), daemon=True).start()

    def stop(self) -> None:
        """请求退出."""
        self._stop.set()
        self.signaling.stop_maintenance()

    def kick_device(self, device_id: str, message: str = "账号已登出，请重新认证") -> dict[str, int]:
        """登出或吊销令牌后立即通知并断开设备在线连接.

        使用既有 error 消息承载 TOKEN_REVOKED, 不新增协议消息类型.
        :return: 含 kicked_connections、kicked_sessions、revoked_tokens 的计数
        """
        client = self.registry.remove(device_id)
        connections = 0
        if client is not None:
            with suppress(OSError), client.lock:
                send_frame(
                    client.sock,
                    build_envelope(
                        self.signaling.identity,
                        "error",
                        {"code": TOKEN_REVOKED, "message": message},
                    ),
                )
            with suppress(OSError):
                client.sock.close()
            connections = 1
        counts = self.signaling.kick_device(device_id)
        counts["kicked_connections"] = connections
        log_event(
            "relay.server",
            "device.kicked",
            device_id=device_id,
            kicked_connections=connections,
            kicked_sessions=counts["kicked_sessions"],
            revoked_tokens=counts["revoked_tokens"],
        )
        return counts

    def _kick_for_accounts(self, device_id: str) -> dict[str, Any]:
        """账号中心吊销令牌后立即断链; 数据面授予随之回收并断开."""
        counts = self.kick_device(device_id)
        return {
            "kicked": int(counts.get("kicked_connections", 0)) + int(counts.get("kicked_sessions", 0)),
            "kicked_connections": counts.get("kicked_connections", 0),
            "kicked_sessions": counts.get("kicked_sessions", 0),
        }

    def _on_grant_revoked(self, relay_token: str) -> None:
        """信令层回收授予后, 立即断开数据面上使用该令牌的连接."""
        if self.dataplane is not None:
            self.dataplane.revoke_token(relay_token)

    def _token_active(self, relay_token: str) -> bool:
        """数据面运行期复核: 授予未被回收, 且签发设备未被吊销."""
        record = self.signaling.resolve_relay_token(relay_token)
        if record is None:
            return False
        return not record.device_id or not self._device_revoked(record.device_id)

    def _device_revoked(self, device_id: str) -> bool:
        """账号库中该设备最近一次签发的令牌是否已吊销.

        以库文件为准而不是进程内缓存, 因此账号命令层或运维脚本改库都能立即生效.
        """
        return bool(device_id) and self.revocation.is_revoked(device_id)

    def _reject_revoked(self, connection: socket.socket, device_id: str) -> None:
        """吊销后的统一处置: 回明确错误码, 结束会话, 回收授予并断开数据面."""
        counts = self.signaling.kick_device(device_id)
        with suppress(OSError):
            send_frame(
                connection,
                build_envelope(
                    self.signaling.identity,
                    "error",
                    {"code": TOKEN_REVOKED, "message": "账号令牌已吊销，请重新登录"},
                ),
            )
        stale = self.registry.remove(device_id)
        if stale is not None and stale.sock is not connection:
            with suppress(OSError):
                stale.sock.close()
        log_event(
            "relay.server",
            "device.revoked",
            level="WARNING",
            error_code=TOKEN_REVOKED,
            device_id=device_id,
            kicked_connections=1 if stale is not None else 0,
            kicked_sessions=counts["kicked_sessions"],
            revoked_tokens=counts["revoked_tokens"],
        )

    def reload_tls(self) -> bool:
        """按当前证书文件重建 TLS 上下文; 失败时保留旧上下文并返回 False."""
        if not self.config.tls_enabled:
            return False
        try:
            context = build_server_context(
                cert_file=self.config.cert_file,
                key_file=self.config.key_file,
                ca_file=self.config.ca_file,
                require_client_cert=self.config.require_client_cert,
            )
        except ValueError as error:
            log_event(
                "relay.server",
                "tls.reload_failed",
                level="ERROR",
                error_code="TLS_RELOAD_FAILED",
                detail=str(error),
            )
            return False
        self.tls_context = context
        log_event("relay.server", "tls.reloaded", cert_file=self.config.cert_file)
        return True

    def _handle_connection(self, connection: socket.socket, peer: str) -> None:
        device_id = ""
        transferred = False
        try:
            # 握手前先设超时, 避免半开连接长期占用线程
            connection.settimeout(DEFAULT_HANDSHAKE_TIMEOUT_S)
            if self.tls_context is not None:
                # 信令与数据面共用同一条 TLS 通道, 包装后帧格式保持不变
                try:
                    connection = self.tls_context.wrap_socket(connection, server_side=True)
                except (ssl.SSLError, OSError):
                    with suppress(OSError):
                        connection.close()
                    return
            try:
                # 同端口分流: 首字节为 0 走既有长度前缀帧, 为大写字母走账号中心 HTTP.
                try:
                    first_header = _recv_exactly(connection, 4)
                except (OSError, ValueError):
                    return
                if first_header is None:
                    return
                if looks_like_http(first_header):
                    if self.account_api is not None and self.tls_context is not None:
                        self.account_api.serve(connection, first_header)
                    return
                pending_header: bytes | None = first_header
                while not self._stop.is_set():
                    try:
                        payload = recv_frame(connection, pending_header)
                    except (OSError, ValueError):
                        return
                    pending_header = None
                    if payload is None:
                        return
                    direct = _peek_direct(payload)
                    if direct is not None:
                        # 直连媒体: 首帧是 e2e_hello, 交给该会话登记的接管者做 hs=2 握手
                        transferred = self._hand_off_direct(connection, direct)
                        return
                    request_type, request_device = _peek_request(payload)
                    hello = self.dataplane.parse_hello(payload) if self.dataplane is not None else None
                    if hello is not None and self.dataplane is not None:
                        # 数据面连接: 首帧握手后转交数据面, 后续帧是密文, 本层不再解析
                        connection.settimeout(300.0)
                        self.dataplane.attach(connection, hello)
                        return
                    if device_id and self._device_revoked(device_id):
                        # 已认证连接的下一条消息即复核吊销: 不静默放行, 立即断开
                        self._reject_revoked(connection, device_id)
                        return
                    responses = self._process(payload, device_id)
                    error = _first_error(responses)
                    if request_type == "register":
                        _log_register(peer, request_device, error)
                    auth_ok = any(isinstance(item, dict) and item.get("type") == "auth_ok" for item in responses)
                    if request_type == "auth" and auth_ok and request_device and self._device_revoked(request_device):
                        # 认证通过但账号令牌已吊销: 不回 auth_ok, 不登记在线, 直接拒绝
                        self._reject_revoked(connection, request_device)
                        return
                    for response in responses:
                        try:
                            send_frame(connection, response)
                        except OSError:
                            return
                    if error is None and self.account_center is not None:
                        if request_type in ("session_start", "session_end"):
                            self._record_session_audit(payload, device_id)
                        elif request_type == "heartbeat" and device_id:
                            self._refresh_presence(device_id)
                    if request_type == "auth":
                        if auth_ok and request_device:
                            replaced = self.registry.attach(request_device, connection)
                            if replaced is not None:
                                # 同一设备重新认证: 关闭旧连接, 新连接接管投递
                                with suppress(OSError):
                                    replaced.close()
                            device_id = request_device
                            self.signaling.mark_online(device_id)
                            log_event("relay.server", "device.authenticated", device_id=device_id, peer_ip=peer)
                        else:
                            log_event(
                                "relay.server",
                                "auth.failed",
                                level="WARNING",
                                error_code="AUTH_FAILED",
                                peer_ip=peer,
                                reason=(error[1][:200] if error else ""),
                            )
                    elif request_type in RELAY_REQUEST_TYPES:
                        self._log_request(request_type, responses, device_id)
                # end while
            finally:
                # 直连接管成功时 socket 所有权已转移给会话, 这里不能再关
                if not transferred:
                    with suppress(OSError):
                        connection.close()
        finally:
            self.guard.release(peer)
            if device_id and self.registry.detach(device_id, connection):
                ended = self.signaling.mark_offline(device_id)
                log_event(
                    "relay.server",
                    "device.disconnected",
                    device_id=device_id,
                    ended_sessions=len(ended),
                )

    def _hand_off_direct(self, connection: socket.socket, hello: dict[str, Any]) -> bool:
        """把 e2e_hello 首帧交给该会话登记的接管者; 未登记或接管失败返回 False."""
        session_id = str(hello.get("session_id") or "")
        with self._direct_lock:
            handler = self._direct_handlers.get(session_id) if session_id else None
        if handler is None:
            log_event(
                "relay.server",
                "direct.rejected",
                level="WARNING",
                error_code="AUTH_FAILED",
                session_id=session_id or None,
            )
            return False
        try:
            transferred = handler(connection, hello)
        except (OSError, ValueError, RuntimeError) as error:
            log_event(
                "relay.server",
                "direct.failed",
                level="WARNING",
                error_code="AUTH_FAILED",
                session_id=session_id,
                error=type(error).__name__,
            )
            return False
        if transferred:
            log_event("relay.server", "direct.accepted", session_id=session_id)
        return transferred

    def _log_request(self, request_type: str, responses: list[dict[str, Any]], device_id: str) -> None:
        """记录路由与预算结果; 只记类型、会话与预算, 不记载荷与令牌."""
        session_id = ""
        budget = None
        outcome = "ok"
        for response in responses:
            if not isinstance(response, dict):
                continue
            if response.get("type") == "error":
                outcome = "error"
            payload = response.get("payload")
            if isinstance(payload, dict):
                budget = payload.get("budget_kbps", budget)
                session_id = str(payload.get("session_id") or session_id)
        log_event(
            "relay.server",
            "request.handled",
            device_id=device_id,
            session_id=session_id or None,
            request_type=request_type,
            outcome=outcome,
            budget_kbps=budget,
        )

    def _record_session_audit(self, payload: bytes, device_id: str) -> None:
        """把会话建立/结束写入服务端账号库; 同一会话只记一次, 字段缺失时留空."""
        if self.account_center is None or not device_id:
            return
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(envelope, dict):
            return
        body = envelope.get("payload")
        if not isinstance(body, dict):
            body = {}
        session_id = str(body.get("session_id") or envelope.get("session_id") or "")
        peer = str(body.get("peer_device_id") or envelope.get("to_device_id") or body.get("device_id") or "")
        detail = {
            name: body.get(name)
            for name in ("transport", "transport_kind", "quality_profile", "end_reason", "reason")
            if body.get(name)
        }
        kind = "start" if envelope.get("type") == "session_start" else "end"
        self.account_center.record_session_event(kind, session_id, device_id, peer, detail)

    def _refresh_presence(self, device_id: str) -> None:
        """心跳每 30 秒刷新一次在线时间; 只影响在线展示, 不参与鉴权."""
        now = time.monotonic()
        last = self._heartbeat_presence.get(device_id, 0.0)
        if now - last < 30.0:
            return
        self._heartbeat_presence[device_id] = now
        if self.account_center is not None:
            self.account_center.store.record_presence(device_id, "online")

    def _process(self, payload: bytes, device_id: str | None = None) -> list[dict[str, Any]]:
        """校验并交给内存信令服务; 非法帧只回错误, 不中断服务."""
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("包络必须是对象")
            validate_envelope(envelope)
            # register 是唯一的自举消息: 此时尚无落库公钥, 只能用它自报的公钥验证自身签名;
            # 其余消息一律只认 register 已落库的公钥, 绝不采信载荷里的 public_key.
            if envelope["type"] == "register":
                public_key = str(envelope["payload"].get("public_key", ""))
            else:
                public_key = self._public_key(envelope, device_id)
            if not public_key or not verify_envelope(envelope, public_key):
                # 验签失败属于鉴权类拒绝, 不能落成协议版本错误
                return [self._error("签名校验失败", AUTH_FAILED)]
        except (ProtocolError, ValueError, KeyError, TypeError, AttributeError) as error:
            # 网络入参不可信, 校验失败一律转成 error 消息返回
            return [self._error(str(error))]
        return self.signaling.handle(envelope, device_id)

    def _grant_lookup(self, relay_token: str) -> Any:
        """把信令层的授予记录转成数据面授予类型; 不存在返回 None."""
        record = self.signaling.resolve_relay_token(relay_token)
        if record is None:
            return None
        from rdc.relay.dataplane import RelayGrant

        return RelayGrant(
            session_id=record.session_id,
            budget_kbps=record.budget_kbps,
            expires_at=record.expires_at,
            scope=record.scope,
            account_id=record.account_id,
            wall_id=record.wall_id,
            device_id=record.device_id,
        )

    def _public_key(self, envelope: dict[str, Any], device_id: str | None = None) -> str:
        """取验签公钥: 只来自 register 已落库记录, 不采信任何载荷自报公钥."""
        payload = envelope["payload"]
        target = str(
            device_id
            or envelope.get("from_device_id")
            or payload.get("from_device_id")
            or payload.get("device_id")
            or ""
        )
        device = self.signaling.devices.get(target)
        return device.public_key_b64 if device is not None else ""

    def _error(self, message: str, code: str = "PROTOCOL_VERSION_UNSUPPORTED") -> dict[str, Any]:
        return build_envelope(
            self.signaling.identity,
            "error",
            {"code": code, "message": message},
        )

    def _build_health_server(self) -> ThreadingHTTPServer:
        config = self.config
        signaling = self.signaling
        dataplane = self.dataplane
        guard = self.guard
        registry = self.registry
        revocation = self.revocation
        started = self._started

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/healthz":
                    self.send_error(404)
                    return
                cert_expires_at = certificate_not_after_epoch(config.cert_file) if config.tls_enabled else None
                cert_days_left = certificate_days_left(config.cert_file) if config.tls_enabled else None
                payload: dict[str, Any] = {
                    "status": "ok",
                    "service": "rdc-relay" if dataplane is not None else "rdc-signaling",
                    "protocol_version": PROTOCOL_VERSION,
                    "budget_kbps": config.budget_kbps,
                    "min_session_kbps": config.min_session_kbps,
                    "degrade_profile": config.degrade_profile,
                    "uptime_s": round(time.monotonic() - started, 1),
                    "devices": len(signaling.devices),
                    "connections": guard.snapshot(),
                    "registry": registry.snapshot(),
                    "signaling": signaling.session_stats(),
                    "focus": signaling.focus_stats(),
                    "dataplane": dataplane.snapshot() if dataplane is not None else None,
                    "revocation": revocation.snapshot(),
                    "tls": {
                        "enabled": config.tls_enabled,
                        "min_version": TLS_MIN_VERSION_NAME if config.tls_enabled else None,
                        "client_cert_required": config.require_client_cert,
                        "cert_days_left": cert_days_left,
                        "cert_expires_at": (
                            datetime.fromtimestamp(cert_expires_at, tz=UTC).isoformat()
                            if cert_expires_at is not None
                            else None
                        ),
                    },
                }
                # 模板占位符不得原样对外: 未注入真实域名时不返回该字段
                endpoint = public_relay_endpoint(config.relay_endpoint)
                if endpoint is not None:
                    payload["relay_endpoint"] = endpoint
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # 不打印访问日志, 避免刷屏与泄露
                return

        return ThreadingHTTPServer((config.health_bind, config.health_port), Handler)

    def _shutdown(self) -> None:
        if self.dataplane is not None:
            self.dataplane.close()
        self.signaling.stop_maintenance()
        self.registry.close_all()
        for _port, listener in self._listeners:
            with suppress(OSError):
                listener.close()
        self._listeners = []
        self._listener = None
        if self._health is not None:
            self._health.shutdown()


def public_relay_endpoint(value: str) -> str | None:
    """过滤模板占位符; 未注入真实域名时返回 None, 避免健康检查对外误导."""
    if not value or "<" in value or ">" in value:
        return None
    return value


def _peek_direct(payload: bytes) -> dict[str, Any] | None:
    """识别端到端直连首帧; 不是 e2e_hello 返回 None, 数据面与信令帧走原逻辑."""
    try:
        hello = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(hello, dict) or hello.get("kind") != "e2e_hello":
        return None
    return hello


def _peek_request(payload: bytes) -> tuple[str, str]:
    """快速取出请求类型, 以及 auth/register 消息里的 device_id; 解析失败返回空串."""
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(envelope, dict):
        return "", ""
    request_type = str(envelope.get("type") or "")
    if request_type not in ("auth", "register"):
        return request_type, ""
    request_payload = envelope.get("payload")
    if not isinstance(request_payload, dict):
        return request_type, ""
    return request_type, str(request_payload.get("device_id") or "")


def _first_error(responses: list[dict[str, Any]]) -> tuple[str, str] | None:
    """取第一条错误响应的 (code, message); 全部成功返回 None."""
    for item in responses:
        if isinstance(item, dict) and item.get("type") == "error":
            payload = item.get("payload")
            if isinstance(payload, dict):
                return str(payload.get("code") or "AUTH_FAILED"), str(payload.get("message") or "")
            return "AUTH_FAILED", ""
    return None


def _log_register(peer: str, device_id: str, error: tuple[str, str] | None) -> None:
    """记录 register/challenge 阶段来源与结果, 供公网暴露后的异常来源排查."""
    if error is None:
        log_event("relay.server", "challenge.issued", peer_ip=peer, device_id=device_id or None)
        return
    log_event(
        "relay.server",
        "register.rejected",
        level="WARNING",
        error_code=error[0],
        peer_ip=peer,
        device_id=device_id or None,
        reason=error[1][:200],
    )


def _configure_account_store() -> AccountCenter:
    """启动时装配落盘账号库并返回账号中心; 信令、中继与 /v1 接口共用同一份数据.

    库路径取 RDC_ACCOUNTS_DB, 未设置时落在 RDC_DATA_DIR/accounts.db(即服务状态目录);
    库不可用只记警告并退化为内存, 不阻断信令服务启动.
    """
    from rdc.commands import accounts as accounts_commands

    warning = accounts_commands.configure_persistent_store(restore_session=False, remote=None)
    if warning:
        log_event(
            "relay.server",
            "accounts.unavailable",
            level="WARNING",
            error_code="CONFIG_INVALID",
            reason=warning,
        )
    return AccountCenter(accounts_commands.get_service().store)


def main(argv: Iterable[str] | None = None, default_config: str = "/etc/rdc-relay/relay.conf") -> int:
    """命令行入口: python -m rdc.relay --config /etc/rdc-relay/relay.conf."""
    import argparse

    parser = argparse.ArgumentParser(description="RDC 信令与中继服务")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--check-config", action="store_true", help="只校验配置并退出")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        config = load_config(args.config)
    except ValueError as error:
        configure(level="INFO", console=True)
        log_event(
            "relay.server",
            "config.invalid",
            level="ERROR",
            error_code="CONFIG_INVALID",
            reason=str(error),
        )
        return 2
    if args.check_config:
        configure(level="INFO", console=True)
        log_event(
            "relay.server",
            "config.checked",
            relay_endpoint=config.relay_endpoint,
            budget_kbps=config.budget_kbps,
            degrade_profile=config.degrade_profile,
        )
        return 0

    configure(level=config.log_level, console=True)
    account_center = _configure_account_store()
    server = RelayServer(config, account_center=account_center)
    for code in (signal.SIGTERM, signal.SIGINT):
        signal.signal(code, lambda *_: server.stop())
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: server.reload_tls())
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
