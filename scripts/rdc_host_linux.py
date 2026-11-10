# ==========================================================================================
# 文件        : rdc_host_linux.py
# 所属目录    : scripts/
# 文件用途    : Ubuntu (X11) 被控端: 一条命令启动, 打印监听地址/设备 ID/连接码,
#               采集桌面 → H.264 编码 → 中继上行, 同时接收控制端键鼠事件并注入本机
# 架构定位    : 反向链路 (Windows 控制 Ubuntu) 的被控端入口; 复用产品信令/中继/加密实现
# 核心功能    : start_hosting 取设备 ID 与连接码 → 进程内起中继 → 注册认证 → 校验连接码 →
#               mss 采集 + libx264 编码 + xdotool 键鼠注入 → 逐次会话状态提示
# 职责边界    : 不写凭据到文件; 不访问公网; 不修改系统网络配置; 不伪造状态
# ==========================================================================================

"""Ubuntu (X11) 被控端.

用法:
    python scripts/rdc_host_linux.py [--port 27888] [--fps 15] [--bitrate 5000000]

启动成功后会打印监听地址、设备 ID 与连接码; 在 Windows 控制端填写这三项即可连接.
退出: Ctrl+C (或关闭终端).
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from fractions import Fraction
from typing import Any

import av
import mss
import numpy as np

from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    host_handshake,
    recv_json,
    relay_connect,
)
from rdc.commands import api as commands_api
from rdc.crypto.identity import DeviceIdentity, b64decode, b64encode
from rdc.relay.server import RelayConfig, RelayServer

DEFAULT_PORT = 27888
DEFAULT_FPS = 15
DEFAULT_BITRATE = 5_000_000
REQUEST_KBPS = 8000
MAX_CHAR_LEN = 4
BIND_ALL = str(ipaddress.IPv4Address(0))

KEY_NAMES = {
    "Return",
    "BackSpace",
    "Tab",
    "Escape",
    "Delete",
    "Left",
    "Right",
    "Up",
    "Down",
    "Home",
    "End",
    "Page_Up",
    "Page_Down",
    "space",
}
MODIFIER_NAMES = {"ctrl", "shift", "alt", "super"}


def _emit(text: str) -> None:
    """向终端输出一行用户可读状态."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _local_ip() -> str:
    """取本机对外地址; 只做路由探测, 不发送任何报文."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.37.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _port_free(port: int) -> bool:
    """检查端口是否可绑定."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((BIND_ALL, port))
        except OSError:
            return False
    return True


def _probe_display() -> str:
    """检查 X11 桌面可用性, 返回空串表示可用."""
    try:
        with mss.MSS() as screen:
            if len(screen.monitors) < 2:
                return "未检测到可采集的 X11 显示器"
            monitor = screen.monitors[1]
            if int(monitor["width"]) <= 0 or int(monitor["height"]) <= 0:
                return "显示器尺寸无效"
    except (OSError, mss.ScreenShotError) as error:
        return f"{type(error).__name__}: {error}"
    return ""


class X11Capture:
    """mss 采集 X11 主屏, 返回 BGRA 数组; 每线程独立 mss 实例."""

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self._local = threading.local()

    def open(self) -> None:
        """读取主屏尺寸, 宽高向下取偶数以适配 yuv420p."""
        with mss.MSS() as screen:
            monitor = screen.monitors[1]
            self.width = int(monitor["width"]) // 2 * 2
            self.height = int(monitor["height"]) // 2 * 2

    def grab(self) -> np.ndarray | None:
        """抓一帧主屏; 尺寸异常返回 None."""
        screen = getattr(self._local, "screen", None)
        if screen is None:
            screen = mss.MSS()
            self._local.screen = screen
        shot = screen.grab({"left": 0, "top": 0, "width": self.width, "height": self.height})
        if shot.width != self.width or shot.height != self.height:
            return None
        return np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)


class LiveEncoder:
    """常驻 H.264 编码器: 每帧自带 SPS/PPS, 控制端可独立解码."""

    def __init__(self, width: int, height: int, fps: int, bitrate: int) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.context: av.CodecContext | None = None

    def open(self) -> None:
        """打开 libx264 低延迟编码器."""
        context = av.CodecContext.create("libx264", "w")
        context.width = self.width
        context.height = self.height
        context.pix_fmt = "yuv420p"
        context.framerate = Fraction(self.fps, 1)
        context.time_base = Fraction(1, self.fps)
        context.bit_rate = self.bitrate
        context.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x264-params": "bframes=0:keyint=30:min-keyint=30:scenecut=0:repeat-headers=1",
        }
        context.open()
        self.context = context

    def encode(self, image: np.ndarray, pts: int) -> list[tuple[bytes, bool]]:
        """编码一帧, 返回 (负载, 是否关键帧) 列表."""
        if self.context is None:
            raise RuntimeError("编码器尚未打开")
        frame = av.VideoFrame.from_ndarray(image, format="bgra").reformat(format="yuv420p")
        frame.pts = pts
        return [(bytes(packet), bool(packet.is_keyframe)) for packet in self.context.encode(frame)]


class VideoSender(threading.Thread):
    """采集编码发送线程: 按目标帧率抓屏并逐帧发往中继."""

    def __init__(
        self,
        channel: AeadChannel,
        capture: X11Capture,
        encoder: LiveEncoder,
        fps: int,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="video-sender")
        self.channel = channel
        self.capture = capture
        self.encoder = encoder
        self.fps = fps
        self.stop_event = stop_event
        self.frames = 0
        self.bytes = 0
        self.dropped = 0
        self.error = ""

    def run(self) -> None:
        interval = 1.0 / float(self.fps)
        next_deadline = time.perf_counter()
        pts = 0
        try:
            while not self.stop_event.is_set():
                next_deadline += interval
                image = self.capture.grab()
                if image is None:
                    self.dropped += 1
                    time.sleep(0.005)
                    continue
                for payload, is_key in self.encoder.encode(image, pts):
                    self.channel.send(
                        {
                            "kind": "video",
                            "codec": "h264",
                            "w": int(image.shape[1]),
                            "h": int(image.shape[0]),
                            "fps": self.fps,
                            "key": is_key,
                            "pts": pts,
                            "data": b64encode(payload),
                            "sent_ns": time.time_ns(),
                        }
                    )
                    self.bytes += len(payload)
                pts += 1
                self.frames += 1
                sleep_for = next_deadline - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_deadline = time.perf_counter()
        except (OSError, E2EFailure, RuntimeError) as error:
            self.error = f"{type(error).__name__}: {error}"


class XdotoolInjector:
    """用 xdotool 注入键鼠事件; 失败只记一次提示, 不中断会话."""

    def __init__(self, display: str) -> None:
        self._env = {**os.environ, "DISPLAY": display}
        self.failed = 0

    def _run(self, arguments: list[str]) -> None:
        try:
            subprocess.run(
                ["xdotool", *arguments],
                env=self._env,
                timeout=3,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            self.failed += 1

    def type_text(self, text: str) -> None:
        """逐字符输入文本."""
        if text:
            self._run(["type", "--clearmodifiers", "--delay", "0", "--", text])

    def press_key(self, name: str) -> None:
        """按单个功能键."""
        self._run(["key", "--clearmodifiers", name])

    def press_combo(self, mods: list[str], name: str) -> None:
        """按组合键, 例如 ctrl+c."""
        self._run(["key", "--clearmodifiers", "+".join([*mods, name])])

    def mouse_click(self, x: int, y: int, button: int) -> None:
        """移动并点击."""
        self._run(["mousemove", str(x), str(y), "click", str(button)])

    def mouse_move(self, x: int, y: int) -> None:
        """移动指针."""
        self._run(["mousemove", str(x), str(y)])

    def mouse_wheel(self, x: int, y: int, button: int) -> None:
        """滚轮: 4 上 5 下."""
        self._run(["mousemove", str(x), str(y), "click", str(button)])


class HostSession(threading.Thread):
    """单次会话: 数据面发布端 + 画面线程 + 输入接收循环."""

    def __init__(
        self,
        agent: LinuxHostAgent,
        session_id: str,
        peer_device_id: str,
        relay_token: str,
        display: str,
    ) -> None:
        super().__init__(daemon=True, name=f"host-session-{session_id}")
        self.agent = agent
        self.session_id = session_id
        self.peer_device_id = peer_device_id
        self.relay_token = relay_token
        self.display = display
        self.stop_event = threading.Event()
        self.channel: AeadChannel | None = None
        self.reason = ""

    def run(self) -> None:
        relay_socket: socket.socket | None = None
        sender: VideoSender | None = None
        try:
            relay_socket = relay_connect("127.0.0.1", self.agent.port, self.relay_token, self.session_id, "publisher")
            relay_socket.settimeout(300.0)
            session = host_handshake(relay_socket, self.agent.identity)
            channel = AeadChannel(relay_socket, session)
            self.channel = channel

            capture = X11Capture()
            capture.open()
            encoder = LiveEncoder(capture.width, capture.height, self.agent.fps, self.agent.bitrate)
            encoder.open()
            sender = VideoSender(channel, capture, encoder, self.agent.fps, self.stop_event)
            sender.start()
            channel.send({"kind": "ready", "w": capture.width, "h": capture.height, "fps": self.agent.fps})
            self.agent.mark_connected(self, f"{capture.width}x{capture.height}@{self.agent.fps}fps")

            injector = XdotoolInjector(self.display)
            while not self.stop_event.is_set():
                message = channel.recv()
                if self._handle_input(message, injector):
                    break
            self.reason = "控制端主动断开"
        except (OSError, ValueError, RuntimeError, E2EFailure) as error:
            self.reason = f"{type(error).__name__}: {error}"
        finally:
            self.stop_event.set()
            if sender is not None:
                sender.join(timeout=3.0)
            if relay_socket is not None:
                with contextlib.suppress(OSError):
                    relay_socket.close()
            self.agent.session_finished(self, self.reason or "连接已结束")

    def _handle_input(self, message: dict[str, Any], injector: XdotoolInjector) -> bool:
        """处理一条输入消息; 返回 True 表示会话应结束."""
        kind = str(message.get("kind", ""))
        if kind == "bye":
            self.reason = "控制端主动断开"
            return True
        if kind == "key":
            self._handle_key(message, injector)
        elif kind == "mouse":
            self._handle_mouse(message, injector)
        return False

    def _handle_key(self, message: dict[str, Any], injector: XdotoolInjector) -> None:
        action = str(message.get("action", ""))
        if action == "char":
            text = str(message.get("text", ""))
            if 0 < len(text) <= MAX_CHAR_LEN:
                injector.type_text(text)
        elif action == "key":
            name = str(message.get("key", ""))
            if name in KEY_NAMES:
                injector.press_key(name)
        elif action == "combo":
            raw = message.get("mods", [])
            mods = [str(item) for item in raw] if isinstance(raw, list) else []
            name = str(message.get("key", ""))
            if name and all(item in MODIFIER_NAMES for item in mods):
                injector.press_combo(mods, name)

    def _handle_mouse(self, message: dict[str, Any], injector: XdotoolInjector) -> None:
        try:
            x = int(message.get("x", 0))
            y = int(message.get("y", 0))
            button = int(message.get("button", 0))
        except (TypeError, ValueError):
            return
        action = str(message.get("action", ""))
        if action == "move":
            injector.mouse_move(x, y)
        elif action == "click" and button in (1, 2, 3):
            injector.mouse_click(x, y, button)
        elif action == "wheel" and button in (4, 5):
            injector.mouse_wheel(x, y, button)


class LinuxHostAgent:
    """被控端主控: 中继、信令与状态提示."""

    def __init__(self, arguments: argparse.Namespace) -> None:
        self.port = int(arguments.port)
        self.fps = int(arguments.fps)
        self.bitrate = int(arguments.bitrate)
        self.display = os.environ.get("DISPLAY", ":0")
        self.identity: DeviceIdentity = DeviceIdentity.generate()
        self.device_id = ""
        self.access_code = ""
        self.client: SignalingClient | None = None
        self.server: RelayServer | None = None
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.session: HostSession | None = None
        self.pending: dict[str, str] | None = None
        self.stop_event = threading.Event()

    # ------------------------------------------------------------------ 启动
    def run(self) -> int:
        """启动被控端并阻塞在信令循环; 返回进程退出码."""
        problem = _probe_display()
        if problem:
            _emit(f"启动失败: 当前没有可用的 X11 桌面 ({problem})")
            _emit("请在该虚机的桌面会话里执行本脚本, 或先设置 DISPLAY=:0。")
            return 2
        if not _port_free(self.port) or not _port_free(self.port + 1):
            _emit(f"启动失败: 端口 {self.port} 或 {self.port + 1} 已被占用")
            return 1
        hosting = commands_api.start_hosting("Ubuntu 被控端")
        if not hosting.ok or not isinstance(hosting.data, dict):
            _emit(f"启动失败: {hosting.message}")
            return 1
        self.identity = commands_api.get_service().identity
        self.device_id = str(hosting.data.get("device_id", self.identity.device_id))
        self.access_code = str(hosting.data.get("access_code", ""))
        try:
            self._start_relay()
            self._connect_signaling()
            self._print_banner()
            self._read_loop()
        except KeyboardInterrupt:
            _emit("")
            _emit("[状态] 已停止被控端")
            return 0
        except (OSError, ValueError, RuntimeError, E2EFailure) as error:
            _emit(f"启动失败: {type(error).__name__}: {error}")
            return 1
        finally:
            self.stop_event.set()
            with contextlib.suppress(OSError):
                if self.client is not None:
                    self.client.close()
            if self.server is not None:
                self.server.stop()
            commands_api.stop_hosting()
        return 0

    def _start_relay(self) -> None:
        config = RelayConfig(
            bind=BIND_ALL,
            port=self.port,
            health_bind="127.0.0.1",
            health_port=self.port + 1,
            relay_endpoint=f"relay://{_local_ip()}:{self.port}",
            egress_mbps=20,
            overhead_ratio=0.08,
            max_sessions=2,
            degrade_profile="L0",
            log_level="WARNING",
        )
        self.server = RelayServer(config)
        threading.Thread(target=self.server.serve_forever, name="relay", daemon=True).start()
        self._wait_relay_ready()

    def _wait_relay_ready(self) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(("127.0.0.1", self.port))
                    return
                except OSError:
                    time.sleep(0.1)
        raise RuntimeError("监听未就绪, 请检查端口占用")

    def _connect_signaling(self) -> None:
        self.client = SignalingClient("127.0.0.1", self.port, timeout_s=60.0)
        self.client.connect()
        self.client.register_and_auth(self.identity, "rdc-ubuntu-host")
        if self.client.sock is not None:
            # 认证后改为阻塞读: 被控端要长时间等待控制端, 不能被读超时误判为断开
            self.client.sock.settimeout(None)
        self._start_keepalive()

    def _start_keepalive(self) -> None:
        """每 10 秒发一次心跳, 防止信令连接因空闲被判为超时."""

        def loop() -> None:
            seq = 0
            while not self.stop_event.wait(10.0):
                seq += 1
                self._send("heartbeat", {"session_id": "", "seq": seq})

        threading.Thread(target=loop, daemon=True, name="keepalive").start()

    def _print_banner(self) -> None:
        _emit("")
        _emit("=" * 56)
        _emit("启动成功: Ubuntu 被控端已就绪, 正在等待控制端连接")
        _emit(f"监听地址: {_local_ip()}:{self.port}")
        _emit(f"设备 ID : {self.device_id}")
        _emit(f"连接码  : {self.access_code}")
        _emit("-" * 56)
        _emit("在 Windows 控制端填写以上三项, 然后点击「连接」。")
        _emit("停止被控端: 按 Ctrl+C")
        _emit("=" * 56)
        _emit("")

    # ------------------------------------------------------------------ 信令循环
    def _read_loop(self) -> None:
        while not self.stop_event.is_set() and self.client is not None:
            try:
                frame = recv_json(self.client.sock) if self.client.sock is not None else None
            except (OSError, E2EFailure) as error:
                _emit(f"[状态] 与信令服务的连接中断: {type(error).__name__}: {error}")
                return
            if frame is None:
                _emit("[状态] 信令连接已关闭")
                return
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                continue
            msg_type = str(frame.get("type", ""))
            if msg_type == "offer":
                self._on_offer(payload)
            elif msg_type == "relay_allocated":
                self._on_allocated(payload)
            elif msg_type == "error":
                _emit(f"[状态] 服务提示: {payload.get('message', '')}")

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
        provided = ""
        try:
            provided = b64decode(str(payload.get("access_proof", ""))).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            provided = ""
        if not hmac.compare_digest(provided, self.access_code):
            _emit("[状态] 已拒绝一次连接: 连接码不正确")
            self._answer(session_id, peer, False, "连接码不正确, 请检查后重试")
            return
        self._answer(session_id, peer, True, "")
        with self.state_lock:
            self.pending = {"session_id": session_id, "peer": peer}
        self._send(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": REQUEST_KBPS, "est_duration_s": 7200},
            session_id,
        )
        _emit(f"[状态] 控制端 {peer} 已通过连接码校验, 正在建立画面通道…")

    def _answer(self, session_id: str, peer: str, accepted: bool, reason: str) -> None:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "from_device_id": self.device_id,
            "to_device_id": peer,
            "accepted": accepted,
            "chosen_candidate": {"kind": "relay", "address": _local_ip(), "port": self.port},
            "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
        }
        if not accepted:
            payload["reason"] = reason
        self._send("answer", payload, session_id)

    def _on_allocated(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id", ""))
        token = str(payload.get("relay_token", ""))
        with self.state_lock:
            pending = self.pending
            self.pending = None
            if pending is None or pending["session_id"] != session_id or self.session is not None or not token:
                return
            session = HostSession(self, session_id, pending["peer"], token, self.display)
            self.session = session
        session.start()

    def _send(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> None:
        with self.send_lock:
            if self.client is None:
                return
            with contextlib.suppress(OSError, E2EFailure):
                self.client.send_envelope(msg_type, payload, session_id=session_id)

    # ------------------------------------------------------------------ 会话状态
    def mark_connected(self, session: HostSession, detail: str) -> None:
        """会话数据面就绪, 输出已连接提示."""
        _emit(f"[状态] 已连接: {session.peer_device_id} · 画面 {detail}")

    def session_finished(self, session: HostSession, reason: str) -> None:
        """会话结束: 释放状态并通知信令结束会话."""
        with self.state_lock:
            if self.session is session:
                self.session = None
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
        _emit(f"[状态] 已断开: {reason}")
        _emit("[状态] 等待新的连接")


def main(argv: list[str] | None = None) -> int:
    """解析参数并运行被控端."""
    parser = argparse.ArgumentParser(description="Ubuntu (X11) 被控端")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="监听端口")
    parser.add_argument("--fps", default=DEFAULT_FPS, type=int, help="画面帧率")
    parser.add_argument("--bitrate", default=DEFAULT_BITRATE, type=int, help="目标码率 bps")
    arguments = parser.parse_args(argv)
    return LinuxHostAgent(arguments).run()


if __name__ == "__main__":
    raise SystemExit(main())
