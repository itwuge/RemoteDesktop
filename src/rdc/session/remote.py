# ==========================================================================================
# 文件        : remote.py
# 所属目录    : src/rdc/session/
# 文件用途    : 产品控制端会话: 信令连接、连接码应答、数据面接入、解码与输入上行
# 架构定位    : 会话层的控制端实现; 命令层 start_connection 用它, 界面只经命令层拿帧
# 核心功能    : RemoteSession 连接与断开; 帧回调; 输入上行队列; 剪贴板与文件会话面
# 职责边界    : 不建窗口、不画图、不读用户输入; 界面只拿已解码像素与状态
# 依赖关系    : rdc.session.* ; rdc.media.h264 ; rdc.commands.transfer/clipboard 的服务实例由调用方注入
# ==========================================================================================

"""产品控制端会话.

连接顺序: 信令注册认证 → offer(带连接码挑战应答) → answer → session_start →
relay_allocate → 数据面 relay_hello → hs=2 握手 → 等 ready → 收视频/发输入.
"""

from __future__ import annotations

import contextlib
import queue
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdc.connect.policy import (
    FAMILY_V6,
    IPV6_TCP_TIMEOUT_MS,
    KIND_HOST,
    Candidate,
    filter_candidates,
)
from rdc.connect.relay import payload_to_candidate
from rdc.core.logging import log_event
from rdc.crypto.identity import DeviceIdentity
from rdc.media.caps import default_media_caps, negotiate_media
from rdc.media.frame import MAGIC, VERSION_V2, VideoFrame, decode_v1, decode_v2
from rdc.media.h264 import DecodedFrame, H264Decoder
from rdc.session.aead import AeadSessionChannel
from rdc.session.control import SignalingClient, SignalingError
from rdc.session.crypto import HANDSHAKE_VERSION
from rdc.session.dataplane import AeadChannel, DataPlaneError, relay_connect, tls_connect
from rdc.session.desktop import DesktopSessionBinding
from rdc.session.handshake import build_access_proof, controller_handshake
from rdc.session.wire import WireError

MODULE = "rdc.session.remote"
REQUEST_KBPS = 8000
KEEPALIVE_S = 10.0
CLIPBOARD_PUMP_S = 0.1
CONNECT_TIMEOUT_S = 20.0
IO_TIMEOUT_S = 300.0
# 直连媒体预算: 逐个尝试 answer 里的全部 v6 host 候选(每候选 800 ms), 取第一个握手成功者.
# 多监听端口(如 443)会按端口展开候选, 上游只放行 443 时前面的候选可能整段超时.
DIRECT_MEDIA_BUDGET_MS = 2500
# 统计口径: 帧率按最近 1 秒实际解码帧计, 码率按同一窗口内线缆字节计;
# 延迟取最近若干次 ping/pong 往返, 样本不足时返回空值, 由界面显示「—」
STATS_WINDOW_S = 1.0
STATS_SAMPLE_KEEP_S = 2.0
PING_INTERVAL_S = 1.0
PING_TIMEOUT_S = 5.0
LATENCY_WINDOW = 30
LATENCY_MIN_SAMPLES = 3


@dataclass
class RemoteConfig:
    """控制端连接参数; 地址与端口来自配置, 不写死在会话逻辑里."""

    host: str
    port: int
    device_id: str
    access_code: str
    display_name: str = "Windows 控制端"
    use_tls: bool = False
    ca_file: str = ""
    sni: str = ""
    session_id: str = ""


def _percentile(ordered: list[float], ratio: float) -> float:
    """在已排序样本上取分位值; 小样本用最近秩, 不做插值."""
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * ratio)))
    return ordered[index]


@dataclass
class RemoteStatus:
    """控制端状态快照."""

    state: str = "idle"
    message: str = ""
    width: int = 0
    height: int = 0
    frames: int = 0
    frame_version: int = VERSION_V2
    extras: dict[str, Any] = field(default_factory=dict)


class RemoteSession:
    """一次远程控制会诊: 从信令到画面与输入上行."""

    def __init__(
        self,
        config: RemoteConfig,
        *,
        on_frame: Callable[[DecodedFrame], None] | None = None,
        on_status: Callable[[RemoteStatus], None] | None = None,
        transfer_service: Any = None,
        clipboard_service: Any = None,
        destination_root: Any = None,
        on_files_received: Callable[[dict[str, Any]], None] | None = None,
        on_clipboard_apply: Callable[[str, str], None] | None = None,
        identity: DeviceIdentity | None = None,
    ) -> None:
        self.config = config
        self.identity = identity or DeviceIdentity.generate()
        self.on_frame = on_frame
        self.on_status = on_status
        self.transfer_service = transfer_service
        self.clipboard_service = clipboard_service
        self.destination_root = destination_root
        self.on_files_received = on_files_received
        self.on_clipboard_apply = on_clipboard_apply
        self.session_id = config.session_id or f"rdc-{int(time.time())}-{secrets.token_hex(3)}"
        self.status = RemoteStatus()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.failed_reason = ""
        self.outbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.channel: AeadChannel | None = None
        self.sock: socket.socket | None = None
        self.client: SignalingClient | None = None
        self.adapter: AeadSessionChannel | None = None
        self.binding: DesktopSessionBinding | None = None
        self.decoder: H264Decoder | None = None
        self.frame_version = VERSION_V2
        self.stream_fps = 0
        # 实际选路结果: 直连 IPv6 媒体成功为 direct, 否则回落 relay; 命令层据此写会话记录
        self.transport = "relay"
        self._write_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._frame_times: deque[float] = deque(maxlen=600)
        self._byte_samples: deque[tuple[float, int, int]] = deque(maxlen=32)
        self._rtt_samples: deque[float] = deque(maxlen=LATENCY_WINDOW)
        self._ping_marks: dict[int, float] = {}
        self._ping_seq = 0
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """后台线程发起连接; 立即返回, 进度经 on_status 汇报."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="remote-session", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        """等待数据面就绪; 超时或被拒返回 False."""
        return self.ready_event.wait(timeout_s)

    @property
    def transport_kind(self) -> str:
        """本次会话实际选路: direct 或 relay."""
        return self.transport

    def stop(self) -> None:
        """主动断开: 先通知被控端, 再关闭数据面与信令."""
        self.stop_event.set()
        with self._write_lock:
            if self.channel is not None:
                with contextlib.suppress(DataPlaneError, OSError, WireError):
                    self.channel.send({"kind": "bye"})
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.shutdown(socket.SHUT_RDWR)
        self._publish("closed", "已断开")

    def send_input(self, message: dict[str, Any]) -> None:
        """把一条输入消息放进上行队列, 由发送线程按序写入."""
        self.outbound.put(message)

    def request_keyframe(self) -> None:
        """请求被控端立即产生关键帧; 只投递控制消息, 不动本地解码器."""
        self.outbound.put({"kind": "keyframe"})

    def pump_clipboard(self) -> int:
        """消费会话面里待处理的剪贴板事件; 返回处理条数."""
        if self.binding is None:
            return 0
        return self.binding.pump()

    def stats(self) -> dict[str, Any]:
        """返回当前会话统计, 供界面与验收读取.

        帧率来自实际解码帧时间戳; 码率来自线缆字节增量(含 4 字节帧长前缀与 AEAD 标签);
        延迟来自会话内 ping/pong 往返; 档位由实际画面分辨率与协商帧率推出.
        取不到数据的字段返回 ``None``, 由界面显示「—」, 不用配置常量填充.
        """
        now = time.monotonic()
        fps = self._window_fps(now)
        bitrate_kbps, payload_ratio = self._window_bitrate(now)
        latency_p50, latency_p95 = self._latency_percentiles()
        return {
            "session_id": self.session_id,
            "state": self.status.state,
            "message": self.status.message,
            "width": self.status.width,
            "height": self.status.height,
            "frames": self.status.frames,
            "frame_version": self.frame_version,
            "handshake": HANDSHAKE_VERSION,
            "host": self.config.host,
            "port": self.config.port,
            "sent_frames": self.channel.sent_frames if self.channel is not None else 0,
            "recv_frames": self.channel.recv_frames if self.channel is not None else 0,
            "fps": fps,
            "bitrate_kbps": bitrate_kbps,
            "payload_ratio": payload_ratio,
            "latency_p50_ms": latency_p50,
            "latency_p95_ms": latency_p95,
            "profile": self._actual_profile(),
        }

    def _window_fps(self, now: float) -> float | None:
        """最近 1 秒实际解码帧数; 窗口内没有帧时返回 None."""
        with self._metrics_lock:
            while self._frame_times and now - self._frame_times[0] > STATS_WINDOW_S:
                self._frame_times.popleft()
            count = len(self._frame_times)
        return float(count) if count else None

    def _window_bitrate(self, now: float) -> tuple[float | None, float | None]:
        """按线缆字节增量算码率与净荷占比; 样本不足一段窗口时返回 None."""
        channel = self.channel
        if channel is None:
            return None, None
        wire = channel.sent_wire_bytes + channel.recv_wire_bytes
        payload = channel.sent_payload_bytes + channel.recv_payload_bytes
        with self._metrics_lock:
            self._byte_samples.append((now, wire, payload))
            while len(self._byte_samples) > 2 and now - self._byte_samples[0][0] > STATS_SAMPLE_KEEP_S:
                self._byte_samples.popleft()
            start_at, start_wire, start_payload = self._byte_samples[0]
        elapsed = now - start_at
        if elapsed <= 0.05:
            return None, None
        wire_delta = wire - start_wire
        payload_delta = payload - start_payload
        if wire_delta <= 0:
            return None, None
        return round(wire_delta * 8 / 1000 / elapsed, 1), round(payload_delta / wire_delta, 3)

    def _latency_percentiles(self) -> tuple[float | None, float | None]:
        """ping/pong 往返的中位与 95 分位; 样本少于 3 次时返回 None."""
        with self._metrics_lock:
            samples = sorted(self._rtt_samples)
        if len(samples) < LATENCY_MIN_SAMPLES:
            return None, None
        return round(_percentile(samples, 0.5), 1), round(_percentile(samples, 0.95), 1)

    def _actual_profile(self) -> str:
        """按实际分辨率与协商帧率给出档位短标, 不读配置里的档位名."""
        height = self.status.height
        if not self.status.width or not height:
            return ""
        label = "4K" if height >= 2160 else f"{height}P"
        if self.stream_fps:
            label = f"{label} {self.stream_fps} 帧"
        return label

    # ------------------------------------------------------------------ 线程主体
    def _run(self) -> None:
        try:
            self._connect()
        except (OSError, ValueError, RuntimeError, DataPlaneError, SignalingError) as error:
            self.failed_reason = f"{type(error).__name__}: {error}"
            log_event(MODULE, "session.failed", level="WARNING", reason=self.failed_reason)
            if not self.stop_event.is_set():
                self._publish("failed", f"连接失败: {self.failed_reason}")
        finally:
            self._cleanup()

    def _connect(self) -> None:
        self._publish("connecting", f"正在连接 {self.config.host}:{self.config.port} …")
        self.client = SignalingClient(
            self.config.host,
            self.config.port,
            timeout_s=CONNECT_TIMEOUT_S,
            use_tls=self.config.use_tls,
            ca_file=self.config.ca_file,
            sni=self.config.sni,
        )
        self.client.connect()
        identity = self.identity
        self.client.register_and_auth(identity, self.config.display_name)
        threading.Thread(target=self._keepalive_loop, name="remote-keepalive", daemon=True).start()
        # 契约要求 access_proof 字段常驻; 免连接码时留空, 由被控端按账号准入
        # 函数自行判定受信, 控制端不自述同账号声明. 完整挑战应答放 access_auth.
        offer: dict[str, Any] = {
            "session_id": self.session_id,
            "from_device_id": identity.device_id,
            "to_device_id": self.config.device_id,
            "candidates": [{"kind": "relay", "address": self.config.host, "port": self.config.port, "priority": 1}],
            "media_caps": default_media_caps(),
            "access_proof": "",
        }
        if self.config.access_code:
            self._publish("connecting", "已连上服务, 正在校验连接码 …")
            proof = build_access_proof(
                self.config.access_code,
                self.session_id,
                identity.device_id,
                self.config.device_id,
            )
            offer["access_proof"] = proof["response"]
            offer["access_auth"] = proof
        else:
            self._publish("connecting", "已连上服务, 正在按受信设备免连接码接入 …")
        self.client.send_envelope("offer", offer, session_id=self.session_id)
        answer = self.client.recv_until("answer")
        payload = dict(answer.get("payload", {}))
        if not payload.get("accepted"):
            raise RuntimeError(str(payload.get("reason") or "被控端拒绝了本次连接"))
        peer = str(payload.get("from_device_id") or self.config.device_id)
        self.frame_version, degrade_reason = negotiate_media(payload.get("media_caps"), role="controller")
        if degrade_reason:
            self._publish("connecting", f"{degrade_reason}; 画面使用 v1 兼容封装")
        # 先按 answer 的 v6 host 候选试真直连; 成功即用, 失败回落中继. 直连与中继共用
        # 同一条 TCP 会话协议, 选路只影响 socket 来源, 不改数据面封装.
        direct = self._connect_direct(payload)
        transport_kind = "direct" if direct is not None else "relay"
        self.transport = transport_kind
        try:
            self.client.send_envelope(
                "session_start",
                {
                    "session_id": self.session_id,
                    "from_device_id": identity.device_id,
                    "to_device_id": peer,
                    "transport_kind": transport_kind,
                    "quality_profile": "L1",
                },
                session_id=self.session_id,
            )
            self.client.send_envelope(
                "relay_allocate",
                {"session_id": self.session_id, "requested_kbps": REQUEST_KBPS, "est_duration_s": 7200},
                session_id=self.session_id,
            )
            allocated = dict(self.client.recv_until("relay_allocated").get("payload", {}))
        except (OSError, RuntimeError):
            # 信令失败时直连 socket 还没交给会话, 必须在这里关掉, 避免泄漏
            if direct is not None:
                with contextlib.suppress(OSError):
                    direct[0].close()
            raise
        token = str(allocated.get("relay_token", ""))
        if not token:
            if direct is not None:
                with contextlib.suppress(OSError):
                    direct[0].close()
            raise RuntimeError("中继未下发令牌, 无法建立画面通道")
        if direct is not None:
            self.sock, crypto, _candidate = direct
            self.sock.settimeout(IO_TIMEOUT_S)
        else:
            self.sock = relay_connect(
                self.config.host,
                self.config.port,
                token,
                self.session_id,
                "subscriber",
                use_tls=self.config.use_tls,
                ca_file=self.config.ca_file,
                sni=self.config.sni,
            )
            self.sock.settimeout(IO_TIMEOUT_S)
            crypto = controller_handshake(self.sock, identity, self.session_id, self.config.device_id)
        channel = AeadChannel(self.sock, crypto)
        self.channel = channel
        self.decoder = H264Decoder()
        self.decoder.open()
        self.adapter = AeadSessionChannel(
            channel,
            session_id=self.session_id,
            role="controller",
            budget_kbps=int(allocated.get("budget_kbps") or REQUEST_KBPS),
            on_control=self._on_control,
        )
        self.adapter.open()
        session_plane_ready = (
            self.transfer_service is not None
            and self.clipboard_service is not None
            and self.destination_root is not None
        )
        if session_plane_ready:
            self.binding = DesktopSessionBinding(
                self.adapter,
                transfer_service=self.transfer_service,
                clipboard_service=self.clipboard_service,
                destination_root=self.destination_root,
                on_files_received=self.on_files_received,
                on_clipboard_apply=self.on_clipboard_apply,
            )
        threading.Thread(target=self._send_loop, name="remote-sender", daemon=True).start()
        threading.Thread(target=self._clipboard_loop, name="remote-clipboard", daemon=True).start()
        threading.Thread(target=self._ping_loop, name="remote-ping", daemon=True).start()
        self.ready_event.set()
        self._publish("connected", "已连接, 正在等待远端画面 …")
        while not self.stop_event.wait(0.2):
            if self.failed_reason:
                break

    def _direct_candidates(self, payload: dict[str, Any]) -> tuple[Candidate, ...]:
        """从 answer 候选里取可做 TCP 直连的 IPv6 host (已剔除链路本地)."""
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            return ()
        parsed = [
            item for item in (payload_to_candidate(row) for row in raw if isinstance(row, dict)) if item is not None
        ]
        usable, _hairpin = filter_candidates(parsed)
        return tuple(item for item in usable if item.family == FAMILY_V6 and item.kind == KIND_HOST)

    def _connect_direct(self, payload: dict[str, Any]) -> tuple[socket.socket, Any, Candidate] | None:
        """按 v6 host 候选另开 TCP 直连并完成 hs=2; 失败返回 None 回落中继.

        与 L1 探测同预算(每候选 800 ms, 最多 2 次, 总 1500 ms), 但这里是真正的媒体连接:
        探测仍然只建连即关, 直连由选路结果触发新建, 成功后直接交给 AeadChannel.
        """
        candidates = self._direct_candidates(payload)
        if not candidates:
            log_event(MODULE, "direct.unavailable", reason="no_ipv6_candidate")
            return None
        deadline = time.monotonic() + DIRECT_MEDIA_BUDGET_MS / 1000.0
        last_error = ""
        for candidate in candidates:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock: socket.socket | None = None
            try:
                timeout_s = min(IPV6_TCP_TIMEOUT_MS / 1000.0, remaining)
                if self.config.use_tls:
                    sock, _info = tls_connect(
                        candidate.address,
                        candidate.port,
                        ca_file=self.config.ca_file,
                        sni=self.config.sni,
                        timeout_s=timeout_s,
                    )
                else:
                    sock = socket.create_connection((candidate.address, candidate.port), timeout=timeout_s)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with contextlib.suppress(OSError):
                        sock.close()
                    break
                sock.settimeout(remaining)
                crypto = controller_handshake(sock, self.identity, self.session_id, self.config.device_id)
            except (OSError, ValueError, RuntimeError, DataPlaneError) as exc:
                last_error = type(exc).__name__
                if sock is not None:
                    with contextlib.suppress(OSError):
                        sock.close()
                continue
            log_event(
                MODULE,
                "direct.selected",
                peer_address=candidate.address,
                peer_port=candidate.port,
                family=candidate.family,
            )
            return sock, crypto, candidate
        log_event(MODULE, "direct.unavailable", level="INFO", reason=last_error or "timeout")
        return None

    def _keepalive_loop(self) -> None:
        seq = 0
        while not self.stop_event.wait(KEEPALIVE_S):
            seq += 1
            if self.client is None:
                return
            try:
                self.client.send_envelope("heartbeat", {"session_id": "", "seq": seq})
            except (OSError, SignalingError, WireError):
                return

    def _ping_loop(self) -> None:
        """每秒发一次会话内 ping, 用对端 pong 往返算延迟."""
        while not self.stop_event.wait(PING_INTERVAL_S):
            now = time.monotonic()
            with self._metrics_lock:
                for seq, sent_at in list(self._ping_marks.items()):
                    if now - sent_at > PING_TIMEOUT_S:
                        self._ping_marks.pop(seq, None)
                self._ping_seq += 1
                seq = self._ping_seq
                self._ping_marks[seq] = now
            self.outbound.put({"kind": "ping", "seq": seq, "t": time.time_ns()})

    def _send_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._write_lock:
                channel = self.channel
            if channel is None:
                continue
            try:
                channel.send(message)
            except (OSError, DataPlaneError, WireError):
                return

    def _clipboard_loop(self) -> None:
        while not self.stop_event.wait(CLIPBOARD_PUMP_S):
            if self.binding is None:
                return
            try:
                self.binding.pump()
            except (RuntimeError, ValueError):
                return

    # ------------------------------------------------------------------ 收帧与状态
    def _on_control(self, item: bytes | dict[str, Any]) -> None:
        """适配层读线程回调: 二进制是视频帧, JSON 是握手后的控制消息."""
        if isinstance(item, bytes):
            if item[:2] == MAGIC and self.decoder is not None:
                self._on_video(decode_v2(item))
            return
        kind = str(item.get("kind", ""))
        if kind == "video":
            self._on_video(decode_v1(item))
        elif kind == "ready":
            width = int(item.get("w", 0))
            height = int(item.get("h", 0))
            self.status.width = width
            self.status.height = height
            self.stream_fps = int(item.get("fps", 0) or 0)
            self._publish("streaming", f"远端桌面 {width}x{height}, 帧封装 v{self.frame_version}")
        elif kind == "pong":
            seq = int(item.get("seq", 0) or 0)
            with self._metrics_lock:
                sent_at = self._ping_marks.pop(seq, None)
            if sent_at is not None:
                self._rtt_samples.append((time.monotonic() - sent_at) * 1000.0)
        elif kind == "error":
            self.failed_reason = str(item.get("reason") or "被控端返回错误")
            self.stop_event.set()

    def _on_video(self, video: VideoFrame) -> None:
        decoder = self.decoder
        if decoder is None:
            return
        try:
            frames = decoder.decode(video.payload)
        except RuntimeError:
            log_event(MODULE, "video.decode_skipped", level="WARNING", pts=video.pts)
            return
        for frame in frames:
            self.status.frames += 1
            if frame is not None:
                with self._metrics_lock:
                    self._frame_times.append(time.monotonic())
            if self.on_frame is not None:
                self.on_frame(frame)

    def _publish(self, state: str, message: str) -> None:
        self.status.state = state
        self.status.message = message
        log_event(MODULE, f"session.{state}", message=message, session_id=self.session_id)
        if self.on_status is not None:
            self.on_status(self.status)

    def _cleanup(self) -> None:
        self.stop_event.set()
        if self.binding is not None:
            self.binding.close()
            self.binding = None
        if self.adapter is not None:
            self.adapter.close()
            self.adapter = None
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
        if self.client is not None:
            self.client.close()
