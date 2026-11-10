# ==========================================================================================
# 文件        : host.py
# 所属目录    : src/rdc/session/
# 文件用途    : 产品被控端: 本机中继与信令、连接码校验、采集编码发布、输入注入与会话面处理
# 架构定位    : 会话层的被控端实现; 命令层 start_hosting 起它, 脚本只做薄壳
# 核心功能    : HostService 起停; HostSession 数据面发布与输入接收; 帧版本协商; 自动换码;
#               会话同端口的 UDP 打洞应答器与数据面 socket 交接
# 职责边界    : 不面向界面、不写证据、不解析命令层包络; 事件经回调与结构化日志外发
# 依赖关系    : rdc.session.* ; rdc.media.* ; rdc.input.* ; rdc.commands.host_ports (装配端口);
#               rdc.connect.punch / rdc.connect.udp_plane (打洞应答与 UDP 数据面)
# ==========================================================================================

"""产品被控端.

一条连接的生命周期: 信令 offer → 连接码挑战应答 → answer + relay_allocate →
relay_allocated → 数据面发布(relay_hello → hs=2 → ready) → 视频与输入 → session_end.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
import time
from base64 import b64encode
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rdc.clipboard.receiver import ClipboardReceiver
from rdc.clipboard.system import read_system_clipboard, write_system_clipboard
from rdc.commands.host_ports import create_capture_source, create_encoder, create_input_injector
from rdc.connect.native import native_host_candidates
from rdc.connect.policy import (
    FAMILY_V4,
    FAMILY_V6,
    KIND_HOST,
    KIND_RELAY,
    KIND_SRFLX,
    ORIGIN_RELAY,
    PRIORITY_RELAY,
    PROTO_TCP,
    Candidate,
    dedupe_candidates,
    filter_candidates,
    sort_candidates,
)
from rdc.connect.punch import PunchResponder
from rdc.connect.relay import candidate_to_payload, payload_to_candidate
from rdc.connect.stun import discover_srflx
from rdc.connect.udp_plane import UdpSessionPlane
from rdc.contracts.ports import (
    CaptureHandle,
    CaptureParams,
    CaptureSource,
    Encoder,
    EncoderHandle,
    EncoderParams,
    InputEvent,
    InputInjector,
    PortError,
)
from rdc.core.errors import CAPTURE_TIMEOUT
from rdc.core.logging import log_event
from rdc.crypto.identity import DeviceIdentity
from rdc.files.receiver import FileTransferReceiver
from rdc.files.service import default_destination_root
from rdc.media.caps import default_media_caps, negotiate_media
from rdc.media.frame import VERSION_V1, VERSION_V2, VideoFrame, encode_v2
from rdc.relay.server import RelayConfig, RelayServer, claim_port_exclusively, create_listener
from rdc.session.channel import SESSION_PLANE_TYPES
from rdc.session.control import SignalingClient, SignalingError
from rdc.session.crypto import HANDSHAKE_VERSION
from rdc.session.dataplane import AeadChannel, DataPlaneError, relay_connect
from rdc.session.handshake import AccessCodeGuard, HandshakeError, host_handshake
from rdc.session.lan import local_ipv4_networks, local_ipv6_networks
from rdc.session.wire import WireError, recv_json

MODULE = "rdc.session.host"
# 空串表示由内核选择全部本地地址并双栈监听; 被控端需要同时接受 IPv4 与 IPv6 直连
BIND_ALL = ""
DEFAULT_PORT = 27888
DEFAULT_FPS = 30
DEFAULT_BITRATE_BPS = 4_000_000
REQUEST_KBPS = 8000
KEEPALIVE_S = 10.0
SESSION_IO_TIMEOUT_S = 300.0
# 打洞应答线程的轮询粒度: 与控制端 100 ms 探测节奏对齐, 又不空转占 CPU
PUNCH_POLL_S = 0.1
# 控制端 L2 判据是每方向至少 2 个响应; 本端回过 2 次才认为对端确实可达
PUNCH_MIN_REPLIES = 2
# answer 候选采集预算与 STUN 总预算一致; 拿不到 srflx 时只发 host + relay, 不造值
ANSWER_CANDIDATE_BUDGET_MS = 1500
# 直连媒体握手超时: 与信令握手独立, 只约束首帧 e2e_hello 的交换
DIRECT_HANDSHAKE_TIMEOUT_S = 10.0
# 会话创建前等待直连落地/控制端选路结果的窗口; 覆盖控制端 2500 ms 直连预算并留调度余量.
# 控制端会先试直连再发 session_start, 明确回落中继时该等待立即结束.
DIRECT_GRACE_S = 2.8


@dataclass(frozen=True)
class HostEvent:
    """被控端事件: 供命令层与薄壳脚本展示, 不承载业务决策."""

    kind: str
    message: str


@dataclass
class HostConfig:
    """被控端启动参数; 端口与码率均可配置, 不硬编码到会话逻辑."""

    port: int = DEFAULT_PORT
    # 额外监听端口: 默认带 443, 供上游只放行 80/443 的 IPv6 入站场景; 置空元组即关闭.
    # 与主端口共用同一套 accept 逻辑(含首帧分流与直连接管).
    extra_ports: tuple[int, ...] = (443,)
    # 对外开放来源的端口: None 表示 extra_ports 全部对外, 显式元组可只开放其中部分,
    # () 表示全部按主端口口径收口. 开放只放宽来源白名单, 不放宽认证与连接码.
    open_ports: tuple[int, ...] | None = None
    fps: int = DEFAULT_FPS
    bitrate_bps: int = DEFAULT_BITRATE_BPS
    display_name: str = "Windows 被控端"
    access_code: str = ""
    bind: str = BIND_ALL
    display: str = ""


def local_ip() -> str:
    """取本机对外地址; 只做路由探测, 不发送任何报文."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.1.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def lan_allow_list() -> str:
    """演示链路来源白名单: 回环加本机全部 IPv4 与 IPv6 网段.

    多网卡(如 VMware VMnet)机器上其他网段的合法控制端也要放行,
    否则反向控制会被中继以 AUTH_FAILED 拒绝. IPv6 侧同理: 白名单补上
    回环 ::1 与链路本地 fe80::/10, 再加本机全局地址所在前缀.
    """
    networks = ["127.0.0.1/32", "::1/128", "fe80::/10"]
    ipv4 = local_ipv4_networks()
    if not ipv4:
        try:
            ipv4 = [str(ipaddress.ip_interface(f"{local_ip()}/24").network)]
        except ValueError:
            ipv4 = []
    networks.extend(network for network in ipv4 if network not in networks)
    networks.extend(network for network in local_ipv6_networks() if network not in networks)
    return ",".join(networks)


def port_free(port: int, bind: str = BIND_ALL) -> bool:
    """检查端口是否可绑定; 探测用的地址族必须与真实监听一致(空地址按双栈探测)."""
    try:
        probe = create_listener(bind, port, 1)
    except OSError:
        return False
    probe.close()
    return True


def create_punch_socket(bind: str, port: int) -> socket.socket:
    """与会话 TCP 同端口创建 UDP 打洞 socket; 空地址按双栈 ``::`` 绑定.

    探测端口与后续数据面必须是同一个 socket, 因此这里只建一次、由 PunchResponder
    持有, L2 判通后原样交给 ``UdpSessionPlane``, 不重新绑定.
    """
    target = bind.strip()
    dual = target in {"", "::", "[::]"}
    family = socket.AF_INET6 if dual or ":" in target else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        claim_port_exclusively(sock)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(("::" if dual else target.strip("[]"), port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class VideoSender(threading.Thread):
    """采集编码发送线程: 按目标帧率抓屏并逐帧发往中继."""

    def __init__(
        self,
        channel: AeadChannel,
        capture: CaptureSource,
        capture_handle: CaptureHandle,
        encoder: Encoder,
        encoder_handle: EncoderHandle,
        encoder_params: EncoderParams,
        fps: int,
        stop_event: threading.Event,
        frame_version: int,
    ) -> None:
        super().__init__(daemon=True, name="video-sender")
        self.channel = channel
        self.capture = capture
        self.capture_handle = capture_handle
        self.encoder = encoder
        self.encoder_handle = encoder_handle
        self.encoder_params = encoder_params
        self.fps = fps
        self.stop_event = stop_event
        self.frame_version = frame_version
        self.keyframe = threading.Event()
        self.seq = 0
        self.frames = 0
        self.bytes = 0
        self.dropped = 0
        self.error = ""

    def run(self) -> None:
        interval = 1.0 / float(self.fps)
        next_deadline = time.perf_counter()
        try:
            while not self.stop_event.is_set():
                next_deadline += interval
                try:
                    frame = self.capture.read(self.capture_handle)
                except PortError as error:
                    if error.code != CAPTURE_TIMEOUT:
                        raise
                    self.dropped += 1
                    time.sleep(0.005)
                    continue
                if self.keyframe.is_set():
                    self.keyframe.clear()
                    # 重开编码器让下一帧成为 IDR: 控制端请求关键帧时用于快速恢复画面
                    self.encoder.close(self.encoder_handle)
                    self.encoder_handle = self.encoder.open(self.encoder_params)
                for packet in self.encoder.encode(self.encoder_handle, frame):
                    if self.frame_version == VERSION_V2:
                        self.channel.send_bytes(
                            encode_v2(
                                VideoFrame(
                                    seq=self.seq,
                                    pts=packet.pts,
                                    width=frame.width,
                                    height=frame.height,
                                    keyframe=packet.keyframe,
                                    payload=packet.data,
                                )
                            )
                        )
                    else:
                        self.channel.send(
                            {
                                "kind": "video",
                                "codec": "h264",
                                "w": frame.width,
                                "h": frame.height,
                                "fps": self.fps,
                                "key": packet.keyframe,
                                "pts": packet.pts,
                                "data": b64encode(packet.data).decode("ascii"),
                                "sent_ns": time.time_ns(),
                            }
                        )
                    self.seq += 1
                    self.bytes += len(packet.data)
                self.frames += 1
                sleep_for = next_deadline - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_deadline = time.perf_counter()
        except (DataPlaneError, PortError, OSError, RuntimeError) as error:
            self.error = f"{type(error).__name__}: {error}"


class HostService:
    """产品被控端主控: 中继、信令、连接码与会话状态."""

    def __init__(
        self,
        config: HostConfig,
        identity: DeviceIdentity,
        on_event: Callable[[HostEvent], None] | None = None,
    ) -> None:
        self.config = config
        self.identity = identity
        self.on_event = on_event
        self.rotate_hook: Callable[[], str] | None = None
        self.device_id = identity.device_id
        self.access_code = config.access_code
        self.port = config.port
        self.display = config.display
        self.guard = AccessCodeGuard(self._rotate_access_code)
        self.guard.set_code(self.access_code)
        self.client: SignalingClient | None = None
        self.server: RelayServer | None = None
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.session: HostSession | None = None
        self.pending: dict[str, Any] | None = None
        # 直连媒体 socket: 由 accept 线程完成握手后暂存, 会话创建时取走注入 HostSession
        self._direct_ready: tuple[socket.socket, Any] | None = None
        self._direct_inflight = False
        # 选路结果信号: 直连握手完成或控制端 session_start 到达时置位
        self._direct_event = threading.Event()
        self.stop_event = threading.Event()
        self._reader: threading.Thread | None = None
        # 与 TCP 同端口的 UDP 打洞应答器; L2 判通后 socket 原样交给 udp_plane
        self.punch: PunchResponder | None = None
        self.udp_plane: UdpSessionPlane | None = None
        self._punch_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """启动中继与信令并进入等待; 端口不可用或采集不可用直接抛 RuntimeError."""
        if self.stop_event.is_set():
            raise RuntimeError("被控端已停止, 不能重复启动")
        if not self.access_code:
            raise RuntimeError("缺少连接码, 请先由命令层生成")
        problem = self._probe_capture()
        if problem:
            raise RuntimeError(f"当前没有可用的桌面画面: {problem}")
        if not port_free(self.config.port, self.config.bind):
            raise RuntimeError(f"端口 {self.config.port} 已被占用")
        if not port_free(self.config.port + 1, "127.0.0.1"):
            raise RuntimeError(f"健康检查端口 {self.config.port + 1} 已被占用")
        self._start_relay()
        self._start_punch_responder()
        self._connect_signaling()
        self._reader = threading.Thread(target=self._read_loop, name="host-signaling", daemon=True)
        self._reader.start()
        ports = self.listen_ports
        log_event(
            MODULE,
            "host.started",
            port=self.port,
            listen_ports=list(ports),
            device_id=self.device_id,
            address=local_ip(),
        )
        self._emit("started", f"被控端已就绪, 监听 {local_ip()}:{', '.join(str(item) for item in ports)}")

    def stop(self) -> None:
        """停止被控端: 结束会话、关信令、停中继; 可重复调用."""
        self.stop_event.set()
        with self.state_lock:
            session = self.session
            pending = self.pending
            self.pending = None
            self._discard_direct_locked()
        if pending is not None and self.server is not None:
            self.server.unregister_direct(str(pending.get("session_id", "")))
        if session is not None:
            session.stop()
        self._stop_punch_responder()
        if self.client is not None:
            with contextlib.suppress(OSError):
                self.client.close()
        if self.server is not None:
            self.server.stop()
            self.server = None
        log_event(MODULE, "host.stopped", port=self.port)

    def rotate_access_code(self, code: str) -> None:
        """命令层换码后同步到校验器, 旧码立即失效."""
        self.access_code = code
        self.guard.set_code(code)

    @property
    def listen_ports(self) -> tuple[int, ...]:
        """实际绑定成功的监听端口; 额外端口失败时只保留主端口."""
        server = self.server
        if server is not None and server.listen_ports:
            return server.listen_ports
        return (self.config.port,)

    def status(self) -> dict[str, Any]:
        """返回被控端当前状态, 供命令层与界面展示."""
        with self.state_lock:
            session = self.session
        return {
            "device_id": self.device_id,
            "access_code": self.access_code,
            "port": self.port,
            "listen_ports": list(self.listen_ports),
            "address": local_ip(),
            "running": not self.stop_event.is_set(),
            "connected": session is not None,
            "peer_device_id": session.peer_device_id if session is not None else "",
            "punch": self.punch_stats(),
        }

    # ------------------------------------------------------------------ 打洞应答
    def _start_punch_responder(self) -> None:
        """起 UDP 打洞应答线程; 绑定失败只降级打洞能力, 不阻断 TCP 直连."""
        try:
            sock = create_punch_socket(self.config.bind, self.config.port)
        except OSError as error:
            log_event(
                MODULE,
                "punch.responder_unavailable",
                level="WARNING",
                port=self.config.port,
                error=type(error).__name__,
            )
            return
        self.punch = PunchResponder(sock)
        self._punch_thread = threading.Thread(target=self._punch_loop, name="punch-responder", daemon=True)
        self._punch_thread.start()

    def _punch_loop(self) -> None:
        """轮询 RDCP1 探测并应答; 控制端切到数据面后把同一 socket 交给数据面."""
        while not self.stop_event.is_set():
            responder = self.punch
            if responder is None:
                return
            before_ignored = responder.ignored
            try:
                responder.poll(PUNCH_POLL_S)
            except (OSError, ValueError):
                return
            if self.udp_plane is not None:
                return
            if responder.replies < PUNCH_MIN_REPLIES or responder.ignored <= before_ignored:
                continue
            # 非 RDCP1 数据报 = 控制端已判 L2 通过并切到数据面, 此刻才交出 socket
            self.release_punch_plane()

    def release_punch_plane(self) -> UdpSessionPlane | None:
        """把打洞 socket 原样交给 UDP 数据面; 重复调用返回同一个平面.

        探测端口与数据面端口必须是同一个, 这里只转移所有权, 不重建 socket;
        交接后 ``PunchResponder.poll``/``close`` 变空操作, 关闭由数据面负责.
        """
        with self.state_lock:
            if self.udp_plane is not None:
                return self.udp_plane
            responder = self.punch
            if responder is None:
                return None
            peer = responder.peer
            sock = responder.release()
            if sock is None:
                return None
            self.udp_plane = UdpSessionPlane(sock, peer=peer)
            log_event(
                MODULE,
                "punch.plane_attached",
                port=self.config.port,
                peer="" if peer is None else f"{peer[0]}:{peer[1]}",
                replies=responder.replies,
            )
            return self.udp_plane

    def punch_stats(self) -> dict[str, Any]:
        """打洞统计: 监听中、应答数、忽略数、主动探测、对端与最后命中时刻."""
        responder = self.punch
        peer = responder.peer if responder is not None else None
        return {
            "listening": responder is not None and self.udp_plane is None,
            "replies": responder.replies if responder is not None else 0,
            "sent": responder.sent if responder is not None else 0,
            "forward": responder.forward if responder is not None else 0,
            "targets": len(responder.targets) if responder is not None else 0,
            "ignored": responder.ignored if responder is not None else 0,
            "peer": "" if peer is None else f"{peer[0]}:{peer[1]}",
            "last_seen": responder.last_seen if responder is not None else None,
            "plane": self.udp_plane is not None,
        }

    def _stop_punch_responder(self) -> None:
        """停止应答线程并释放绑定; 已交接的 socket 由数据面关闭, 不重复关."""
        responder = self.punch
        plane = self.udp_plane
        thread = self._punch_thread
        self.punch = None
        self.udp_plane = None
        self._punch_thread = None
        if responder is not None:
            responder.close()
        if plane is not None:
            plane.close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    # ------------------------------------------------------------------ 启动细节
    def _probe_capture(self) -> str:
        source: CaptureSource | None = None
        handle: CaptureHandle | None = None
        try:
            source = create_capture_source()
            handle = source.open(CaptureParams())
        except PortError as error:
            return f"{error.code}: {error}"
        finally:
            if source is not None and handle is not None:
                source.close(handle)
        return ""

    def _start_relay(self) -> None:
        config = RelayConfig(
            bind=self.config.bind,
            port=self.config.port,
            health_bind="127.0.0.1",
            health_port=self.config.port + 1,
            relay_endpoint=f"relay://{local_ip()}:{self.config.port}",
            egress_mbps=20,
            overhead_ratio=0.08,
            max_sessions=2,
            degrade_profile="L0",
            extra_ports=self.config.extra_ports,
            open_ports=self.config.extra_ports if self.config.open_ports is None else self.config.open_ports,
            allow_ips=lan_allow_list(),
            log_level="WARNING",
        )
        self.server = RelayServer(config)
        threading.Thread(target=self.server.serve_forever, name="relay", daemon=True).start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(("127.0.0.1", self.config.port))
                    return
                except OSError:
                    time.sleep(0.1)
        raise RuntimeError("中继监听未就绪, 请检查端口占用")

    def _connect_signaling(self) -> None:
        self.client = SignalingClient("127.0.0.1", self.config.port, timeout_s=60.0)
        self.client.connect()
        self.client.register_and_auth(self.identity, self.config.display_name)
        if self.client.sock is not None:
            # 认证后改为阻塞读: 被控端要长时间等待控制端, 不能被读超时误判为断开
            self.client.sock.settimeout(None)
        threading.Thread(target=self._keepalive_loop, name="host-keepalive", daemon=True).start()

    def _keepalive_loop(self) -> None:
        seq = 0
        while not self.stop_event.wait(KEEPALIVE_S):
            seq += 1
            self._send("heartbeat", {"session_id": "", "seq": seq})

    # ------------------------------------------------------------------ 信令循环
    def _read_loop(self) -> None:
        while not self.stop_event.is_set() and self.client is not None:
            try:
                frame = recv_json(self.client.sock) if self.client.sock is not None else None
            except (OSError, WireError) as error:
                self._emit("signaling_lost", f"与信令服务的连接中断: {error}")
                return
            if frame is None:
                self._emit("signaling_lost", "信令连接已关闭")
                return
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            msg_type = str(frame.get("type", ""))
            if msg_type == "offer":
                self._on_offer(payload)
            elif msg_type == "session_start":
                self._on_session_start(payload)
            elif msg_type == "relay_allocated":
                self._on_allocated(payload)
            elif msg_type == "error":
                self._emit("signaling_error", f"服务提示: {payload.get('message', '')}")

    def _on_offer(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id", ""))
        peer = str(payload.get("from_device_id", ""))
        if not session_id or not peer:
            return
        with self.state_lock:
            busy = self.session is not None
        if busy:
            self._answer(session_id, peer, False, "被控端已有会话正在进行")
            return
        proof = payload.get("access_auth")
        if not isinstance(proof, dict):
            proof = payload.get("access_proof")
        accepted, reason = self.guard.accept(proof, session_id, peer, self.device_id)
        if not accepted:
            self._emit("rejected", f"已拒绝一次连接: {reason}")
            self._answer(session_id, peer, False, f"{reason}, 请检查后重试")
            return
        frame_version, degrade_reason = negotiate_media(payload.get("media_caps"), role="host")
        if degrade_reason:
            self._emit("degraded", f"{degrade_reason}; 帧封装回退 v1, 带宽开销会上升")
        # 先采本端候选, 再用它剔除控制端的同出口 hairpin, 最后下发并设置主动探测目标
        local = self._answer_candidates()
        peer_candidates = self._peer_candidates(payload, local)
        answer_items, _hairpin = filter_candidates(local, peer_candidates)
        # 竞态口径: pending 与直连接管登记必须在 answer 下发前就位. 控制端只有拿到 answer
        # 里的 v6 候选才可能发起直连, 所以不存在「早到的直连被丢弃」, 也不必缓存未认证 socket.
        with self.state_lock:
            self.pending = {
                "session_id": session_id,
                "peer": peer,
                "frame_version": frame_version,
                "transport": "",
                "direct_possible": any(item.kind == KIND_HOST and item.family == FAMILY_V6 for item in answer_items),
            }
            self._discard_direct_locked()
            self._direct_event.clear()
        if self.server is not None:
            self.server.register_direct(session_id, self._accept_direct)
        self._answer(session_id, peer, True, "", frame_version, answer_items)
        self._start_punch_targets(peer_candidates)
        self._send(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": REQUEST_KBPS, "est_duration_s": 7200},
            session_id,
        )
        self._emit("authorized", f"控制端 {peer} 已通过准入校验, 正在建立画面通道")

    def _answer_candidates(self) -> tuple[Candidate, ...]:
        """构造 answer 下发的本端候选: v6 host / v4 host / srflx / relay.

        v6 host 端口用会话 TCP 监听端口, 服务控制端 L1 直连; v4 host 与 srflx 必须落在
        打洞 socket 的 UDP 端口上, 否则控制端打过来的包收不到. 没有打洞 socket 时不发
        v4 host; STUN 失败时只发 host + relay, 不伪造地址或端口.
        """
        responder = self.punch
        udp_port = responder.local_port if responder is not None else 0
        ports = self.listen_ports
        # v6 host 按每个实际监听成功的端口各出一条, 由控制端逐个尝试; 候选结构本身带 port 字段
        items: list[Candidate] = list(native_host_candidates(v4_port=udp_port, v6_port=ports[0]))
        for port in ports[1:]:
            items.extend(item for item in native_host_candidates(v4_port=0, v6_port=port) if item.family == FAMILY_V6)
        if responder is not None:
            with responder.exclusive() as sock:
                if sock is not None:
                    srflx, _error = discover_srflx(sock, total_ms=ANSWER_CANDIDATE_BUDGET_MS)
                    items.extend(srflx)
        items.append(
            Candidate(
                kind=KIND_RELAY,
                family=FAMILY_V4,
                address=local_ip(),
                port=self.config.port,
                priority=PRIORITY_RELAY,
                origin=ORIGIN_RELAY,
                proto=PROTO_TCP,
            )
        )
        return dedupe_candidates(sort_candidates(items))

    def _peer_candidates(
        self,
        payload: dict[str, Any],
        local: Sequence[Candidate],
    ) -> tuple[Candidate, ...]:
        """解析 offer 候选, 剔除链路本地与同出口 hairpin, 只留可打洞的 IPv4 host/srflx."""
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            return ()
        parsed: list[Candidate] = [
            item for item in (payload_to_candidate(row) for row in raw if isinstance(row, dict)) if item is not None
        ]
        usable, hairpin = filter_candidates(parsed, local)
        targets = tuple(item for item in usable if item.family == FAMILY_V4 and item.kind in {KIND_HOST, KIND_SRFLX})
        log_event(
            MODULE,
            "punch.targets",
            parsed=len(parsed),
            peer=len(targets),
            hairpin_blocked=hairpin,
        )
        return targets

    def _start_punch_targets(self, candidates: Sequence[Candidate]) -> None:
        """把控制端候选设为主动探测目标; 没有打洞器或没有候选时静默降级到中继."""
        responder = self.punch
        if responder is None or not candidates:
            return
        responder.set_targets(tuple((item.address, item.port) for item in candidates))

    def _answer(
        self,
        session_id: str,
        peer: str,
        accepted: bool,
        reason: str,
        frame_version: int = VERSION_V2,
        candidates: Sequence[Candidate] = (),
    ) -> None:
        media_caps = default_media_caps()
        # answer 里只声明本端真正会用的版本, 控制端据此定版
        media_caps["frame_versions"] = [VERSION_V2, VERSION_V1] if frame_version == VERSION_V2 else [VERSION_V1]
        media_caps["frame_version"] = frame_version
        payload: dict[str, Any] = {
            "session_id": session_id,
            "from_device_id": self.device_id,
            "to_device_id": peer,
            "accepted": accepted,
            "chosen_candidate": {"kind": "relay", "address": local_ip(), "port": self.config.port},
            "media_caps": media_caps,
        }
        if not accepted:
            payload["reason"] = reason
        elif candidates:
            # 只增字段: 旧客户端继续读 chosen_candidate 走中继, 新客户端用全量候选打洞
            payload["candidates"] = [candidate_to_payload(item, session_id) for item in candidates]
        self._send("answer", payload, session_id)

    def _on_session_start(self, payload: dict[str, Any]) -> None:
        """记录控制端选路结果并唤醒会话创建, 让直连与回落中继在同一口径上收敛."""
        session_id = str(payload.get("session_id", ""))
        transport = str(payload.get("transport_kind", ""))
        with self.state_lock:
            pending = self.pending
            if pending is None or pending.get("session_id") != session_id:
                return
            pending["transport"] = transport
        self._direct_event.set()

    def _accept_direct(self, connection: socket.socket, hello: dict[str, Any]) -> bool:
        """中继服务首帧分流后的接管者: 校验会话并完成 hs=2, 把 socket 暂存给会话.

        返回 True 表示 socket 所有权已转移; 任何校验失败都返回 False, 由中继服务关闭连接.
        """
        session_id = str(hello.get("session_id", ""))
        with self.state_lock:
            pending = self.pending
            if (
                pending is None
                or pending.get("session_id") != session_id
                or self.session is not None
                or self._direct_ready is not None
                or self._direct_inflight
            ):
                return False
            peer = str(pending.get("peer", ""))
            if not peer or str(hello.get("device_id", "")) != peer:
                return False
            self._direct_inflight = True
        session: Any = None
        error = ""
        try:
            connection.settimeout(DIRECT_HANDSHAKE_TIMEOUT_S)
            session = host_handshake(connection, self.identity, session_id, peer, hello=hello)
        except (OSError, HandshakeError, ValueError) as exc:
            error = type(exc).__name__
        with self.state_lock:
            self._direct_inflight = False
            accepted = False
            if not error and not self.stop_event.is_set() and self.session is None and self._direct_ready is None:
                self._direct_ready = (connection, session)
                accepted = True
            self._direct_event.set()
        if not accepted:
            if error:
                log_event(MODULE, "direct.handshake_failed", level="WARNING", session_id=session_id, error=error)
            return False
        log_event(MODULE, "direct.accepted", session_id=session_id, peer=peer)
        return True

    def _discard_direct_locked(self) -> None:
        """关闭尚未被会话取走的直连 socket; 调用方必须持有 state_lock."""
        ready = self._direct_ready
        self._direct_ready = None
        if ready is not None:
            with contextlib.suppress(OSError):
                ready[0].close()

    def _on_allocated(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id", ""))
        token = str(payload.get("relay_token", ""))
        # 控制端收到 answer 后会先试直连再发 session_start; 这里做有界等待, 让两端对同一条
        # 媒体通道达成一致. 只有本端确实下发了 v6 host 候选时才等待, 纯中继路径不受影响.
        with self.state_lock:
            pending = self.pending
            wait_s = DIRECT_GRACE_S if pending is not None and pending.get("direct_possible") else 0.0
        if wait_s > 0 and not self.stop_event.is_set():
            self._direct_event.wait(wait_s)
        with self.state_lock:
            pending = self.pending
            self.pending = None
            if pending is None or pending["session_id"] != session_id:
                return
            if self.session is not None or not token:
                self._discard_direct_locked()
                return
            direct = self._direct_ready
            self._direct_ready = None
            session = HostSession(
                self,
                session_id,
                str(pending["peer"]),
                token,
                int(pending.get("frame_version", VERSION_V2)),
                direct=direct,
            )
            self.session = session
        if self.server is not None:
            self.server.unregister_direct(session_id)
        log_event(
            MODULE,
            "session.transport",
            session_id=session_id,
            transport="direct" if direct is not None else "relay",
        )
        session.start()

    def _send(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> None:
        with self.send_lock:
            if self.client is None:
                return
            try:
                self.client.send_envelope(msg_type, payload, session_id=session_id)
            except (OSError, SignalingError, WireError):
                self._emit("signaling_send_failed", f"{msg_type} 发送失败")

    # ------------------------------------------------------------------ 会话回调
    def mark_connected(self, session: HostSession, detail: str) -> None:
        self._emit("session_connected", f"已连接: {session.peer_device_id} · 画面 {detail}")

    def session_finished(self, session: HostSession, reason: str) -> None:
        with self.state_lock:
            if self.session is session:
                self.session = None
            self._discard_direct_locked()
        if self.server is not None:
            self.server.unregister_direct(session.session_id)
        self._send(
            "session_end",
            {
                "session_id": session.session_id,
                "from_device_id": self.device_id,
                "to_device_id": session.peer_device_id,
                "reason": reason,
            },
            session.session_id,
        )
        self._emit("session_closed", f"已断开: {reason}")

    def set_rotate_hook(self, hook: Callable[[], str]) -> None:
        """注入换码钩子: 命令层负责生成新码, 会话层不反向依赖命令层."""
        self.rotate_hook = hook

    def _rotate_access_code(self) -> str:
        """连续校验失败后作废旧码: 由命令层生成新码, 失败则保留当前码."""
        self._emit("access_code_rotated", "连接码连续错误, 正在更换")
        if self.rotate_hook is None:
            return self.access_code
        code = self.rotate_hook()
        if code:
            self.access_code = code
        return self.access_code

    def _emit(self, kind: str, message: str) -> None:
        log_event(MODULE, f"host.{kind}", message=message)
        if self.on_event is not None:
            self.on_event(HostEvent(kind=kind, message=message))


class HostSession(threading.Thread):
    """单次会话: 数据面发布端 + 画面线程 + 输入接收循环."""

    def __init__(
        self,
        agent: HostService,
        session_id: str,
        peer_device_id: str,
        relay_token: str,
        frame_version: int,
        direct: tuple[socket.socket, Any] | None = None,
    ) -> None:
        super().__init__(daemon=True, name=f"host-session-{session_id}")
        self.agent = agent
        self.session_id = session_id
        self.peer_device_id = peer_device_id
        self.relay_token = relay_token
        self.frame_version = frame_version
        # 直连媒体: accept 线程已完成 hs=2 的 socket 与 AEAD 会话; 为空时走中继
        self.direct = direct
        self.stop_event = threading.Event()
        self.channel: AeadChannel | None = None
        self.reason = ""

    def stop(self) -> None:
        """请求结束本会话并关闭数据面连接."""
        self.stop_event.set()
        channel = self.channel
        if channel is not None:
            channel.close()

    def run(self) -> None:
        relay_socket: socket.socket | None = None
        sender: VideoSender | None = None
        capture: CaptureSource | None = None
        capture_handle: CaptureHandle | None = None
        encoder: Encoder | None = None
        encoder_handle: EncoderHandle | None = None
        file_receiver: FileTransferReceiver | None = None
        clipboard_receiver: ClipboardReceiver | None = None
        try:
            direct = self.direct
            self.direct = None
            if direct is not None:
                # 直连 socket 已在 accept 阶段完成 e2e 握手, 这里直接接管, 不再连本机中继
                relay_socket, session = direct
                relay_socket.settimeout(SESSION_IO_TIMEOUT_S)
            else:
                relay_socket = relay_connect(
                    "127.0.0.1", self.agent.port, self.relay_token, self.session_id, "publisher"
                )
                relay_socket.settimeout(SESSION_IO_TIMEOUT_S)
                session = host_handshake(relay_socket, self.agent.identity, self.session_id, self.peer_device_id)
            channel = AeadChannel(relay_socket, session)
            self.channel = channel
            # 接收端只在会话建立后装配: 空闲时既不监听也不落盘
            file_receiver = FileTransferReceiver(default_destination_root(), on_complete=self._on_files_received)
            clipboard_receiver = ClipboardReceiver(on_apply=self._on_clipboard_apply)
            clipboard_receiver.set_local("text", read_system_clipboard())

            capture = create_capture_source()
            capture_handle = capture.open(CaptureParams())
            encoder = create_encoder()
            encoder_params = EncoderParams(
                width=capture_handle.width,
                height=capture_handle.height,
                fps=self.agent.config.fps,
                bitrate_bps=self.agent.config.bitrate_bps,
            )
            encoder_handle = encoder.open(encoder_params)
            sender = VideoSender(
                channel,
                capture,
                capture_handle,
                encoder,
                encoder_handle,
                encoder_params,
                self.agent.config.fps,
                self.stop_event,
                self.frame_version,
            )
            sender.start()
            channel.send(
                {
                    "kind": "ready",
                    "w": capture_handle.width,
                    "h": capture_handle.height,
                    "fps": self.agent.config.fps,
                    "frame_version": self.frame_version,
                    "hs": HANDSHAKE_VERSION,
                }
            )
            detail = f"{capture_handle.width}x{capture_handle.height}@{self.agent.config.fps}fps"
            self.agent.mark_connected(self, detail)

            injector = create_input_injector(self.agent.display or None)
            while not self.stop_event.is_set():
                message = channel.recv()
                if self._handle_session_plane(channel, message, file_receiver, clipboard_receiver):
                    continue
                if str(message.get("kind", "")) == "ping":
                    # 控制端用 ping/pong 往返测延迟, 这里只回显不进入输入注入
                    channel.send({"kind": "pong", "seq": message.get("seq", 0), "t": message.get("t", 0)})
                    continue
                if self._handle_input(message, injector, sender):
                    break
            self.reason = self.reason or "控制端主动断开"
        except (OSError, ValueError, RuntimeError, DataPlaneError) as error:
            self.reason = f"{type(error).__name__}: {error}"
        finally:
            self.stop_event.set()
            if sender is not None:
                sender.join(timeout=3.0)
            if encoder is not None and encoder_handle is not None:
                encoder.close(encoder_handle)
            if capture is not None and capture_handle is not None:
                capture.close(capture_handle)
            if relay_socket is not None:
                with contextlib.suppress(OSError):
                    relay_socket.close()
            if file_receiver is not None and clipboard_receiver is not None:
                snapshot = file_receiver.snapshot()
                log_event(
                    MODULE,
                    "session.resources_released",
                    files_completed=snapshot["completed"],
                    received_bytes=snapshot["received_bytes"],
                    clipboard_applied=clipboard_receiver.applied,
                    clipboard_suppressed=clipboard_receiver.suppressed,
                )
            self.agent.session_finished(self, self.reason or "连接已结束")

    def _handle_session_plane(
        self,
        channel: AeadChannel,
        message: dict[str, Any],
        file_receiver: FileTransferReceiver,
        clipboard_receiver: ClipboardReceiver,
    ) -> bool:
        """文件与剪贴板消息分流; 返回 True 表示该消息已被会话面消费."""
        msg_type = str(message.get("type", ""))
        if msg_type not in SESSION_PLANE_TYPES:
            return False
        raw_payload = message.get("payload")
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        payload.setdefault("session_id", str(message.get("session_id") or self.session_id))
        reply = file_receiver.handle(msg_type, payload)
        if reply is None:
            reply = clipboard_receiver.handle(msg_type, payload)
        if reply is not None:
            channel.send(
                {
                    "type": str(reply.get("type", "")),
                    "session_id": self.session_id,
                    "payload": reply.get("payload") or {},
                }
            )
        return True

    def _on_files_received(self, result: dict[str, Any]) -> None:
        files = result.get("files") or []
        log_event(MODULE, "session.files_received", count=len(files), digest=str(result.get("digest", ""))[:12])

    def _on_clipboard_apply(self, kind: str, content: str) -> None:
        if not write_system_clipboard(content):
            log_event(MODULE, "clipboard.write_failed", level="WARNING", kind=kind)

    def _handle_input(
        self,
        message: dict[str, Any],
        injector: InputInjector,
        sender: VideoSender | None = None,
    ) -> bool:
        """处理一条输入消息; 返回 True 表示会话应结束."""
        kind = str(message.get("kind", ""))
        if kind == "bye":
            self.reason = "控制端主动断开"
            return True
        if kind == "keyframe":
            if sender is not None:
                sender.keyframe.set()
            return False
        if kind == "key":
            self._handle_key(message, injector)
        elif kind == "mouse":
            self._handle_mouse(message, injector)
        return False

    def _handle_key(self, message: dict[str, Any], injector: InputInjector) -> None:
        action = str(message.get("action", ""))
        if action == "char":
            injector.inject(InputEvent(kind="key", action="char", text=str(message.get("text", ""))))
        elif action == "key":
            injector.inject(InputEvent(kind="key", action="key", key=str(message.get("key", ""))))
        elif action == "combo":
            raw = message.get("mods", [])
            mods = tuple(str(item) for item in raw) if isinstance(raw, list) else ()
            injector.inject(InputEvent(kind="key", action="combo", key=str(message.get("key", "")), mods=mods))

    def _handle_mouse(self, message: dict[str, Any], injector: InputInjector) -> None:
        try:
            x = int(message.get("x", 0))
            y = int(message.get("y", 0))
            button = int(message.get("button", 0))
        except (TypeError, ValueError):
            return
        injector.inject(InputEvent(kind="mouse", action=str(message.get("action", "")), x=x, y=y, button=button))
