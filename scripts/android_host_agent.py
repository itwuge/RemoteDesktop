# ==========================================================================================
# 文件        : android_host_agent.py
# 所属目录    : scripts/
# 文件用途    : Android 真机联调的 Windows 被控端: 本机启动信令/中继、注册上线、接受手机 offer、
#               采集桌面 H.264 编码上行、接收手机指针/滚轮/键盘事件并注入 Windows
# 架构定位    : 局域网联调装置, 复用产品信令/中继/PoC 采集原语, 不修改产品代码与契约
# 核心功能    : 一键启动本地中继 → 身份持久化 → 认证上线 → offer/answer → relay_allocate →
#               发布端握手 → 采集编码线程 + 输入注入线程
# 职责边界    : 只用于本机演示; 不写仓库凭据; 不修改系统设置; 不访问公网
# ==========================================================================================

"""Android 真机联调的 Windows 被控端.

用法:
    python scripts/android_host_agent.py [--port 18750] [--fps 20] [--bitrate 8000000]
        [--code 246810] [--no-relay]

启动后会打印本机 device_id、连接码与局域网地址; 手机端在连接页填 device_id 后连接.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from _rdc_path import PROJECT_ROOT
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    host_handshake,
    read_clipboard_text,
    relay_connect,
    write_clipboard_text,
)
from input_probe import Injector
from rdc.clipboard.receiver import ClipboardReceiver
from rdc.crypto.identity import DeviceIdentity
from rdc.files.receiver import FileTransferReceiver
from rdc.files.service import default_destination_root
from rdc.session.channel import SESSION_PLANE_TYPES
from session_host_agent import DxcamCapture, LiveEncoder, VideoSender

WORK_DIR = Path(os.environ.get("RDC_LAN_WORK", r"D:\RDC-Build\rdc-lan"))
IDENTITY_FILE = WORK_DIR / "host_identity.key"
READY_FILE = WORK_DIR / "host_ready.json"

VK_MAP = {
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "TAB": 0x09,
    "ESCAPE": 0x1B,
    "ESC": 0x1B,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "SPACE": 0x20,
    "ARROW_LEFT": 0x25,
    "ARROW_UP": 0x26,
    "ARROW_RIGHT": 0x27,
    "ARROW_DOWN": 0x28,
    "HOME": 0x24,
    "END": 0x23,
    "PAGE_UP": 0x21,
    "PAGE_DOWN": 0x22,
    "SHIFT_LEFT": 0x10,
    "SHIFT_RIGHT": 0x10,
    "CTRL_LEFT": 0x11,
    "CTRL_RIGHT": 0x11,
    "ALT_LEFT": 0x12,
    "ALT_RIGHT": 0x12,
}


def emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def load_identity() -> DeviceIdentity:
    """读取或创建持久化 Ed25519 身份, 保证重启后 device_id 不变."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if IDENTITY_FILE.exists():
        raw = IDENTITY_FILE.read_bytes()
        if len(raw) == 32:
            return DeviceIdentity(Ed25519PrivateKey.from_private_bytes(raw))
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    IDENTITY_FILE.write_bytes(raw)
    return DeviceIdentity(private)


def lan_ip() -> str:
    """获取本机对外网卡地址, 不实际发包."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.31.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def read_system_clipboard() -> tuple[str, str] | None:
    """会话开始时读一次本机文本剪贴板; 无内容或读取失败返回 None, 不轮询."""
    text = read_clipboard_text()
    if text is None or not text.strip():
        return None
    return "text", text


def write_system_clipboard(kind: str, content: str) -> None:
    """把对端剪贴板写进本机并回读校验; 失败抛 RuntimeError, 不静默丢弃."""
    if kind != "text":
        raise RuntimeError(f"Windows 侧尚不支持写入 {kind} 剪贴板")
    write_clipboard_text(content)
    if read_clipboard_text() != content:
        raise RuntimeError("写入系统剪贴板失败或未生效")


def handle_session_plane(
    channel: AeadChannel,
    message: dict[str, Any],
    file_receiver: FileTransferReceiver,
    clipboard_receiver: ClipboardReceiver,
    session_id: str,
) -> bool:
    """文件与剪贴板消息分流; 返回 True 表示该消息已被会话面消费."""
    msg_type = str(message.get("type", ""))
    if msg_type not in SESSION_PLANE_TYPES:
        return False
    raw_payload = message.get("payload")
    payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    payload.setdefault("session_id", str(message.get("session_id") or session_id))
    reply = file_receiver.handle(msg_type, payload)
    if reply is None:
        reply = clipboard_receiver.handle(msg_type, payload)
    if reply is not None:
        channel.send(
            {
                "type": str(reply.get("type", "")),
                "session_id": session_id,
                "payload": reply.get("payload") or {},
            }
        )
    return True


def start_relay(port: int, health_port: int) -> subprocess.Popen[bytes]:
    """启动本地信令 + 中继服务, 绑定 0.0.0.0 供手机访问."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    config = WORK_DIR / "relay_lan.conf"
    config.write_text(
        "[server]\n"
        "bind = 0.0.0.0\n"
        f"port = {port}\n"
        "health_bind = 127.0.0.1\n"
        f"health_port = {health_port}\n"
        "max_connections = 32\n"
        "max_connections_per_ip = 16\n"
        "\n[relay]\n"
        f"endpoint = relay://{lan_ip()}:{port}\n"
        "egress_mbps = 20\n"
        "max_sessions = 4\n"
        "min_session_kbps = 500\n"
        "\n[security]\n"
        "allow_ips = 127.0.0.1/32,192.168.31.0/24\n"
        "\n[logging]\n"
        "level = INFO\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    process = subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise E2EFailure("本地中继启动失败")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise E2EFailure("本地中继端口未就绪")


class HostInput:
    """把 Android 上行事件注入 Windows 桌面."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.user32 = ctypes.windll.user32
        self.injector = Injector()

    def _to_screen(self, event: dict[str, Any]) -> tuple[int, int]:
        x = max(0.0, min(1.0, float(event.get("x", 0.0))))
        y = max(0.0, min(1.0, float(event.get("y", 0.0))))
        return round(x * (self.width - 1)), round(y * (self.height - 1))

    def handle(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind", ""))
        if kind == "pointer":
            self._pointer(event)
        elif kind == "gesture":
            self._gesture(event)
        elif kind == "key":
            self._key(event)

    def _pointer(self, event: dict[str, Any]) -> None:
        x, y = self._to_screen(event)
        action = str(event.get("action", "MOVE")).upper()
        button = int(event.get("button", 0))
        if button == 3:
            flags_down, flags_up = 0x0020, 0x0040
        else:
            flags_down, flags_up = 0x0002, 0x0004
        self.user32.SetCursorPos(x, y)
        if action == "DOWN":
            self.user32.mouse_event(flags_down, 0, 0, 0, 0)
        elif action == "UP":
            self.user32.mouse_event(flags_up, 0, 0, 0, 0)

    def _gesture(self, event: dict[str, Any]) -> None:
        x, y = self._to_screen(event)
        self.user32.SetCursorPos(x, y)
        notches = round(float(event.get("dy", 0.0)))
        if notches:
            self.user32.mouse_event(0x0800, 0, 0, notches * 120, 0)

    def _key(self, event: dict[str, Any]) -> None:
        key = str(event.get("key", ""))
        action = str(event.get("action", "DOWN")).upper()
        if not key:
            return
        if key.startswith("char:"):
            if action == "DOWN":
                self.injector.type_char(key[5:])
            return
        name = key.upper().removeprefix("KEYCODE_")
        vk = VK_MAP.get(name)
        if vk is None and len(name) == 1 and name.isalnum():
            vk = ord(name)
        if vk is None:
            return
        flags = 0 if action == "DOWN" else 0x0002
        self.user32.keybd_event(vk, 0, flags, 0)


def run_host(
    identity: DeviceIdentity,
    port: int,
    code: str,
    fps: int,
    bitrate: int,
    relay_host: str = "127.0.0.1",
    *,
    use_tls: bool = False,
    ca_file: str = "",
    sni: str = "",
    width: int = 1280,
    height: int = 720,
) -> None:
    client = SignalingClient(
        relay_host,
        port,
        timeout_s=180.0,
        use_tls=use_tls,
        ca_file=ca_file,
        sni=sni,
    )
    client.connect()
    client.register_and_auth(identity, "Windows Relay Host")
    emit(
        f"[LAN-HOST] 已上线 device_id={identity.device_id} endpoint={relay_host}:{port} "
        f"tls={client.tls_info.get('tls_version', 'off') if use_tls else 'off'}"
    )
    offer = client.recv_until("offer")
    payload = offer.get("payload", {})
    session_id = str(payload.get("session_id", ""))
    peer = str(payload.get("from_device_id", ""))
    proof = str(payload.get("access_proof", ""))
    expected = base64.b64encode(code.encode()).decode()
    if not hmac.compare_digest(proof, expected):
        raise E2EFailure("连接码不匹配, 已拒绝本次会话")
    emit(f"[LAN-HOST] 收到手机 offer session={session_id[:8]} peer={peer[:8]}")

    answer = {
        "session_id": session_id,
        "from_device_id": identity.device_id,
        "to_device_id": peer,
        "chosen_candidate": {"kind": "relay", "address": relay_host, "port": port},
        "media_caps": {"codecs": ["h264"], "max_width": width, "max_height": height, "max_fps": fps},
    }
    client.send_envelope("answer", answer, session_id=session_id)
    client.recv_until("session_start")
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": bitrate // 1000, "est_duration_s": 3600},
        session_id=session_id,
    )
    allocated = client.recv_until("relay_allocated").get("payload", {})
    token = str(allocated.get("relay_token", ""))
    emit(f"[LAN-HOST] 中继预算 {allocated.get('budget_kbps')} kbps / {allocated.get('quality_profile')} 档, 接入数据面")

    received = {"batches": 0}

    def on_files_received(result: dict[str, Any]) -> None:
        received["batches"] += 1
        files = result.get("files") or []
        digest = str(result.get("digest", ""))
        emit(f"[LAN-HOST] 已接收文件 {len(files)} 个, 整树摘要 {digest[:12]}…")

    def on_clipboard_apply(kind: str, content: str) -> None:
        try:
            write_system_clipboard(kind, content)
        except (RuntimeError, ValueError) as error:
            emit(f"[LAN-HOST] 剪贴板写入失败: {error}")

    data_tls: dict[str, Any] = {}
    relay_socket = relay_connect(
        relay_host,
        port,
        token,
        session_id,
        "publisher",
        use_tls=use_tls,
        ca_file=ca_file,
        sni=sni,
        tls_info=data_tls,
    )
    # hs=2 会话绑定: 安卓 0.3.5 起已带 session_id/peer_device_id, 不再放行旧握手
    channel = AeadChannel(relay_socket, host_handshake(relay_socket, identity, session_id, peer))
    # 会话面接收端只在数据面握手成功后装配: 空闲期既不监听也不建目录
    file_receiver = FileTransferReceiver(default_destination_root(), on_complete=on_files_received)
    clipboard_receiver = ClipboardReceiver(on_apply=on_clipboard_apply)
    emit(f"[LAN-HOST] 文件落盘目录 {file_receiver.destination_root}")
    # 会话开始读一次本机剪贴板, 供对端 clipboard_request 取用; 之后不轮询
    local_clipboard = read_system_clipboard()
    if local_clipboard is not None:
        clipboard_receiver.set_local(*local_clipboard)
    emit("[LAN-HOST] 数据面握手完成, 开始采集")

    capture = DxcamCapture(output_index=0)
    capture.open()
    encoder = LiveEncoder(width, height, fps, bitrate)
    encoder.open()
    stop = threading.Event()
    sender = VideoSender(channel, capture, encoder, fps, stop)
    sender.start()

    input_sink = HostInput(capture.width, capture.height)

    def receive_input() -> None:
        while not stop.is_set():
            try:
                message = channel.recv()
            except (OSError, E2EFailure):
                break
            if handle_session_plane(channel, message, file_receiver, clipboard_receiver, session_id):
                continue
            if str(message.get("kind", "")) == "video":
                continue
            if "type" in message:
                # 数据面以外的带 type 消息: 打印后跳过, 不静默丢弃
                emit(f"[LAN-HOST] 忽略未知会话消息 type={message.get('type')!r}")
                continue
            try:
                input_sink.handle(message)
            except (ValueError, TypeError, ctypes.ArgumentError):
                emit("[LAN-HOST] 忽略无法解析的输入事件")

    worker = threading.Thread(target=receive_input, daemon=True, name="input-sink")
    worker.start()
    emit(f"[LAN-HOST] 画面 {width}x{height}@{fps} 已开始推流")

    try:
        while not stop.is_set():
            time.sleep(1.0)
            if sender.error:
                raise E2EFailure(sender.error)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sender.join(timeout=3.0)
        capture.close()
        relay_socket.close()
        client.close()
        snapshot = file_receiver.snapshot()
        emit(
            "[LAN-HOST] 会话资源已清理: 文件接收完成 "
            f"{received['batches']} 批, 未完成 {snapshot['transfers']} 个任务 / "
            f"{snapshot['received_bytes']} 字节, 剪贴板应用 {clipboard_receiver.applied} 次, "
            f"抑制回声 {clipboard_receiver.suppressed} 次"
        )
        emit("[LAN-HOST] 已停止")


def main() -> int:
    parser = argparse.ArgumentParser(description="Android 被控端(局域网直连或公网中继)")
    parser.add_argument("--port", type=int, default=18750)
    parser.add_argument("--health-port", type=int, default=18751)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=2_900_000)
    parser.add_argument("--code", default="246810")
    parser.add_argument("--no-relay", action="store_true", help="复用已在运行的中继(如公网中继)")
    parser.add_argument(
        "--relay-host",
        default="127.0.0.1",
        help="复用中继时连接的地址; 公网中继填 47.109.32.249",
    )
    parser.add_argument(
        "--relay-port",
        type=int,
        default=0,
        help="复用中继时的端口, 缺省与 --port 相同; 公网中继填 8750",
    )
    parser.add_argument("--tls", action="store_true", help="信令与数据面均走 TLS 1.3")
    parser.add_argument("--ca", default=str(PROJECT_ROOT / "deploy" / "relay" / "ca.crt"))
    parser.add_argument("--sni", default="relay.wuge.fun")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    args = parser.parse_args()

    relay_port = args.relay_port or args.port
    if args.tls and not Path(args.ca).is_file():
        emit(f"[LAN-HOST] 失败: 信任锚 CA 不存在 {args.ca}")
        return 1

    identity = load_identity()
    READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text(
        json.dumps(
            {
                "device_id": identity.device_id,
                "port": relay_port,
                "endpoint": f"{args.relay_host}:{relay_port}",
                "tls": args.tls,
                "code": args.code,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    relay: subprocess.Popen[bytes] | None = None
    try:
        if not args.no_relay:
            relay = start_relay(args.port, args.health_port)
        emit("=" * 72)
        emit("手机连接信息")
        emit(f"  设备 ID : {identity.device_id}")
        emit(f"  连接码   : {args.code}")
        emit(f"  地址     : {args.relay_host}:{relay_port} (TLS={'1.3' if args.tls else 'off'})")
        emit(f"  档位     : {args.width}x{args.height}@{args.fps} / {args.bitrate // 1000} kbps")
        emit("=" * 72)
        run_host(
            identity,
            relay_port,
            args.code,
            args.fps,
            args.bitrate,
            args.relay_host,
            use_tls=args.tls,
            ca_file=args.ca,
            sni=args.sni,
            width=args.width,
            height=args.height,
        )
        return 0
    except E2EFailure as error:
        emit(f"[LAN-HOST] 失败: {error}")
        return 1
    finally:
        if relay is not None:
            relay.terminate()
            try:
                relay.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                relay.kill()


if __name__ == "__main__":
    raise SystemExit(main())
