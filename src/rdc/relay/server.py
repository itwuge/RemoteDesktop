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

from rdc.core.errors import TOKEN_REVOKED
from rdc.core.logging import configure, log_event
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
from rdc.signaling.server import InMemorySignalingServer

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
    :param min_session_kbps: 单会话最低预算, 低于此值不下发授予
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
    log_level: str = "INFO"

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
        log_level=(_section_text(parser, "logging", "level") or "INFO").upper(),
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
    if config.log_level not in LOG_LEVELS:
        raise ValueError(f"日志级别必须是 {LOG_LEVELS} 之一")
    parse_ip_networks(config.allow_ips, "allow_ips")
    parse_ip_networks(config.deny_ips, "deny_ips")
    _validate_tls(config)
    return config


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


def _section_text(parser: configparser.ConfigParser, section: str, key: str) -> str:
    """读取可选的字符串配置, 段或键不存在时返回空串."""
    if not parser.has_section(section):
        return ""
    return parser.get(section, key, fallback="").strip()


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


def recv_frame(connection: socket.socket) -> bytes | None:
    """读取一帧, 对端正常关闭返回 None.

    :param connection: 已连接套接字
    :return: 帧负载
    :raises ValueError: 长度越界
    """
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

    :param config: 服务配置
    """

    def __init__(self, config: RelayConfig) -> None:
        self._max_total = config.max_connections
        self._max_per_ip = config.max_connections_per_ip
        self._rate_per_min = config.connect_rate_per_min
        self._allow = parse_ip_networks(config.allow_ips, "allow_ips")
        self._deny = parse_ip_networks(config.deny_ips, "deny_ips")
        self._total = 0
        self._per_ip: dict[str, int] = {}
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.rejected = {"denied": 0, "rate_limited": 0, "too_many": 0}

    def admit(self, peer: str) -> tuple[bool, str]:
        """判断是否放行一个新连接; 放行后调用方必须在 finally 里 release."""
        address_allowed = ip_matches(peer, self._allow) if self._allow else True
        if ip_matches(peer, self._deny) or not address_allowed:
            with self._lock:
                self.rejected["denied"] += 1
            return False, "denied"
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


class RelayServer:
    """单个 TCP 信令接入线程加一个回环健康检查端口."""

    def __init__(self, config: RelayConfig, presence_sink: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self.guard = ConnectionGuard(config)
        self.registry = ConnectionRegistry()
        self.signaling = InMemorySignalingServer(
            relay_endpoint=config.relay_endpoint,
            egress_mbps=config.egress_mbps,
            overhead_ratio=config.overhead_ratio,
            max_sessions=config.max_sessions,
            min_session_kbps=config.min_session_kbps,
            deliver=self.registry.deliver,
            presence_sink=presence_sink or _account_presence_sink,
        )
        # 延迟导入: dataplane 复用本模块的帧收发, 模块级导入会形成循环
        from rdc.relay.dataplane import RelayDataPlane

        self.dataplane = RelayDataPlane(self._grant_lookup, config.max_sessions)
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

    def serve_forever(self) -> None:
        """阻塞运行, 直到 stop() 或收到终止信号."""
        configure(level=self.config.log_level, console=True)
        self._health = self._build_health_server()
        threading.Thread(target=self._health.serve_forever, name="health", daemon=True).start()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.config.bind, self.config.port))
        self._listener.listen(min(128, max(1, self.config.max_connections * 2)))
        self._listener.settimeout(1.0)
        self._started = time.monotonic()
        log_event(
            "relay.server",
            "service.started",
            relay_endpoint=self.config.relay_endpoint,
            port=self.config.port,
            budget_kbps=self.config.budget_kbps,
            max_sessions=self.config.max_sessions,
            max_connections=self.config.max_connections,
            tls_enabled=self.config.tls_enabled,
        )
        while not self._stop.is_set():
            try:
                connection, address = self._listener.accept()
            except TimeoutError:
                continue
            peer = str(address[0])
            allowed, reason = self.guard.admit(peer)
            if not allowed:
                log_event(
                    "relay.server",
                    "connection.rejected",
                    level="WARNING",
                    error_code="RATE_LIMITED" if reason == "rate_limited" else "AUTH_FAILED",
                    peer_ip=peer,
                    reason=reason,
                )
                with suppress(OSError):
                    connection.close()
                continue
            threading.Thread(target=self._handle_connection, args=(connection, peer), daemon=True).start()
        self._shutdown()
        log_event("relay.server", "service.stopped")

    def stop(self) -> None:
        """请求退出."""
        self._stop.set()

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
            with connection:
                while not self._stop.is_set():
                    try:
                        payload = recv_frame(connection)
                    except (OSError, ValueError):
                        return
                    if payload is None:
                        return
                    request_type, auth_device = _peek_request(payload)
                    hello = self.dataplane.parse_hello(payload)
                    if hello is not None:
                        # 数据面连接: 首帧握手后转交数据面, 后续帧是密文, 本层不再解析
                        connection.settimeout(300.0)
                        self.dataplane.attach(connection, hello)
                        return
                    responses = self._process(payload, device_id)
                    auth_ok = any(isinstance(item, dict) and item.get("type") == "auth_ok" for item in responses)
                    for response in responses:
                        try:
                            send_frame(connection, response)
                        except OSError:
                            return
                    if request_type == "auth":
                        if auth_ok and auth_device:
                            replaced = self.registry.attach(auth_device, connection)
                            if replaced is not None:
                                # 同一设备重新认证: 关闭旧连接, 新连接接管投递
                                with suppress(OSError):
                                    replaced.close()
                            device_id = auth_device
                            self.signaling.mark_online(device_id)
                            log_event("relay.server", "device.authenticated", device_id=device_id, peer_ip=peer)
                        else:
                            log_event(
                                "relay.server",
                                "auth.failed",
                                level="WARNING",
                                error_code="AUTH_FAILED",
                                peer_ip=peer,
                            )
                    elif request_type in RELAY_REQUEST_TYPES:
                        self._log_request(request_type, responses, device_id)
                # end while
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

    def _process(self, payload: bytes, device_id: str | None = None) -> list[dict[str, Any]]:
        """校验并交给内存信令服务; 非法帧只回错误, 不中断服务."""
        try:
            envelope = json.loads(payload.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("包络必须是对象")
            validate_envelope(envelope)
            if not verify_envelope(envelope, self._public_key(envelope, device_id)):
                raise ValueError("签名校验失败")
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
        )

    def _public_key(self, envelope: dict[str, Any], device_id: str | None = None) -> str:
        """取验签公钥: 已认证连接以连接设备为准, 注册消息用载荷公钥."""
        payload = envelope["payload"]
        target = str(
            device_id
            or envelope.get("from_device_id")
            or payload.get("from_device_id")
            or payload.get("device_id")
            or ""
        )
        device = self.signaling.devices.get(target)
        return device.public_key_b64 if device is not None else payload.get("public_key", "")

    def _error(self, message: str) -> dict[str, Any]:
        return build_envelope(
            self.signaling.identity,
            "error",
            {"code": "PROTOCOL_VERSION_UNSUPPORTED", "message": message},
        )

    def _build_health_server(self) -> ThreadingHTTPServer:
        config = self.config
        signaling = self.signaling
        dataplane = self.dataplane
        guard = self.guard
        registry = self.registry
        started = self._started

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/healthz":
                    self.send_error(404)
                    return
                cert_expires_at = certificate_not_after_epoch(config.cert_file) if config.tls_enabled else None
                cert_days_left = certificate_days_left(config.cert_file) if config.tls_enabled else None
                body = json.dumps(
                    {
                        "status": "ok",
                        "service": "rdc-relay",
                        "protocol_version": PROTOCOL_VERSION,
                        "relay_endpoint": config.relay_endpoint,
                        "budget_kbps": config.budget_kbps,
                        "min_session_kbps": config.min_session_kbps,
                        "degrade_profile": config.degrade_profile,
                        "uptime_s": round(time.monotonic() - started, 1),
                        "devices": len(signaling.devices),
                        "connections": guard.snapshot(),
                        "registry": registry.snapshot(),
                        "signaling": signaling.session_stats(),
                        "focus": signaling.focus_stats(),
                        "dataplane": dataplane.snapshot(),
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
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # 不打印访问日志, 避免刷屏与泄露
                return

        return ThreadingHTTPServer((config.health_bind, config.health_port), Handler)

    def _shutdown(self) -> None:
        self.dataplane.close()
        self.registry.close_all()
        if self._listener is not None:
            self._listener.close()
        if self._health is not None:
            self._health.shutdown()


def _peek_request(payload: bytes) -> tuple[str, str]:
    """快速取出请求类型与认证消息里的 device_id; 解析失败返回空串."""
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(envelope, dict):
        return "", ""
    request_type = str(envelope.get("type") or "")
    if request_type != "auth":
        return request_type, ""
    request_payload = envelope.get("payload")
    if not isinstance(request_payload, dict):
        return request_type, ""
    return request_type, str(request_payload.get("device_id") or "")


def main(argv: Iterable[str] | None = None) -> int:
    """命令行入口: python -m rdc.relay --config /etc/rdc-relay/relay.conf."""
    import argparse

    parser = argparse.ArgumentParser(description="RDC 信令与中继服务")
    parser.add_argument("--config", default="/etc/rdc-relay/relay.conf")
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

    server = RelayServer(config)
    for code in (signal.SIGTERM, signal.SIGINT):
        signal.signal(code, lambda *_: server.stop())
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: server.reload_tls())
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
