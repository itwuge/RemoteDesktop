# ==========================================================================================
# 文件        : relay_public_bench.py
# 所属目录    : scripts/
# 文件用途    : 公网中继真实媒体链路实测: Windows 被控端采集编码 → 公网中继 → 控制端解码,
#               并与本地回环中继做同场景对照
# 架构定位    : 验收装置, 复用产品信令/中继/加密原语, 不修改产品代码与契约
# 核心功能    : 证书固定校验的 TLS 1.3 接入; 连接建立分阶段计时; 码率/帧率/丢帧/按键往返 RTT 采集;
#               固定测试卡 SSIM/PSNR 画质对照; 本地基线 vs 公网中继量化差异
# 职责边界    : 只作为客户端连接被测中继; 不修改服务端配置; 不重启服务; 不访问 22 端口与 nginx;
#               不写任何凭据; 模拟值不标注为实测
# ==========================================================================================

"""公网中继真实链路实测装置.

用法:
    python scripts/relay_public_bench.py probe --work <证据目录>
    python scripts/relay_public_bench.py suite --work <证据目录>
    python scripts/relay_public_bench.py host --work <目录> --relay-host <地址> --relay-port <端口>
        --session-id <会话号> [--tls] [--pin <证书指纹>] [--bitrate 1200000] [--duration 30]
    python scripts/relay_public_bench.py controller --work <目录> --host-work <被控端目录>
        --relay-host <地址> --relay-port <端口> --session-id <会话号> [--tls] [--duration 12]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import cv2
import numpy as np
from cryptography import x509
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

from _rdc_path import PROJECT_ROOT
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    controller_handshake,
    host_handshake,
    percentile_ms,
    recv_json,
    send_json,
    wait_json_file,
    write_json,
)
from rdc.crypto.identity import DeviceIdentity, b64encode

# 公网中继地址已脱敏: 真实值只从环境变量 RDC_RELAY_HOST 提供, 不写入仓库
PUBLIC_HOST = os.environ.get("RDC_RELAY_HOST", "<ECS-IP>")
PUBLIC_PORT = 8750
# 公网中继当前服务端证书 SHA-256 指纹, 连接前固定校验, 防止中间人冒充服务端
PUBLIC_CERT_SHA256 = "44f338101f4b79b966a9c873f6d759c5a961ed4218a37501ae063cb9bfdcfcd5"
LOCAL_RELAY_PORT = 18750
LOCAL_HEALTH_PORT = 18751
SAMPLE_PTS = (0, 15, 30, 45, 60)
CARD_WIDTH = 960
CARD_HEIGHT = 540
RTT_INTERVAL_S = 0.05
_LOGGER = logging.getLogger("relay_public_bench")


def _emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    _LOGGER.info(text)


# ================================ TLS 固定校验 ================================


def _tls_connect(host: str, port: int, pin: str, timeout_s: float = 10.0) -> tuple[socket.socket, dict[str, Any]]:
    """建立 TLS 1.3 连接并按 SHA-256 指纹固定校验服务端证书."""
    raw = socket.create_connection((host, port), timeout=timeout_s)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        wrapped = context.wrap_socket(raw, server_hostname=host)
    except (ssl.SSLError, OSError):
        raw.close()
        raise
    der = wrapped.getpeercert(binary_form=True) or b""
    actual = hashlib.sha256(der).hexdigest()
    if actual != pin:
        wrapped.close()
        raise E2EFailure(f"服务端证书指纹不匹配: 期望 {pin}, 实际 {actual}")
    certificate = x509.load_der_x509_certificate(der)
    info = {
        "tls_version": wrapped.version(),
        "cipher": wrapped.cipher()[0] if wrapped.cipher() else "",
        "cert_sha256": actual,
        "cert_subject": certificate.subject.rfc4514_string(),
        "cert_issuer": certificate.issuer.rfc4514_string(),
        "cert_not_after": certificate.not_valid_after_utc.isoformat(),
    }
    return wrapped, info


class PinnedSignalingClient(SignalingClient):
    """带 TLS 固定校验的信令客户端."""

    def __init__(self, host: str, port: int, timeout_s: float, use_tls: bool, pin: str) -> None:
        super().__init__(host, port, timeout_s=timeout_s)
        self.use_tls = use_tls
        self.pin = pin
        self.tls_info: dict[str, Any] = {}

    def connect(self) -> None:
        """建立 TCP 或 TLS 1.3 连接."""
        if self.use_tls:
            self.sock, self.tls_info = _tls_connect(self.host, self.port, self.pin)
        else:
            self.sock = socket.create_connection((self.host, self.port), timeout=10.0)
        self.sock.settimeout(self.timeout_s)


def _relay_connect(
    host: str,
    port: int,
    relay_token: str,
    session_id: str,
    role: str,
    use_tls: bool,
    pin: str,
) -> tuple[socket.socket, dict[str, Any]]:
    """接入中继数据面, 可选 TLS 1.3 固定校验."""
    info: dict[str, Any] = {}
    if use_tls:
        sock, info = _tls_connect(host, port, pin)
    else:
        sock = socket.create_connection((host, port), timeout=10.0)
    sock.settimeout(30.0)
    send_json(
        sock,
        {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": relay_token,
            "session_id": session_id,
            "role": role,
        },
    )
    hello = recv_json(sock)
    if hello is None or hello.get("type") != "relay_ready":
        sock.close()
        detail = "连接关闭" if hello is None else f"{hello.get('type')} {hello.get('message', '')}"
        raise E2EFailure(f"中继数据面握手失败: {detail}")
    return sock, info


class LockedChannel:
    """给 AEAD 通道加发送锁, 允许视频线程与控制帧并发发送."""

    def __init__(self, channel: AeadChannel) -> None:
        self.channel = channel
        self.send_lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> int:
        with self.send_lock:
            return self.channel.send(payload)

    def recv(self) -> dict[str, Any]:
        return self.channel.recv()


# ================================ 画质指标 ================================


def _ssim(reference: np.ndarray, target: np.ndarray) -> float:
    """灰度 SSIM, 11x11 高斯窗, 标准 C1/C2 常数."""
    ref = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.float64)
    tgt = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY).astype(np.float64)
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    mu_ref = cv2.GaussianBlur(ref, (11, 11), 1.5)
    mu_tgt = cv2.GaussianBlur(tgt, (11, 11), 1.5)
    mu_ref_sq = mu_ref * mu_ref
    mu_tgt_sq = mu_tgt * mu_tgt
    mu_cross = mu_ref * mu_tgt
    sigma_ref_sq = cv2.GaussianBlur(ref * ref, (11, 11), 1.5) - mu_ref_sq
    sigma_tgt_sq = cv2.GaussianBlur(tgt * tgt, (11, 11), 1.5) - mu_tgt_sq
    sigma_cross = cv2.GaussianBlur(ref * tgt, (11, 11), 1.5) - mu_cross
    numerator = (2 * mu_cross + c1) * (2 * sigma_cross + c2)
    denominator = (mu_ref_sq + mu_tgt_sq + c1) * (sigma_ref_sq + sigma_tgt_sq + c2)
    return float(np.mean(numerator / denominator))


def _psnr(reference: np.ndarray, target: np.ndarray) -> float:
    """灰度 PSNR, 单位为 dB."""
    ref = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY).astype(np.float64)
    tgt = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY).astype(np.float64)
    mse = float(np.mean((ref - tgt) ** 2))
    if mse <= 0.0:
        return 99.0
    return float(10.0 * np.log10((255.0 * 255.0) / mse))


def _quality_compare(host_dir: Path, controller_dir: Path) -> dict[str, Any]:
    """按 pts 配对源帧与解码帧, 输出 SSIM/PSNR."""
    pairs: dict[str, Any] = {}
    for pts in SAMPLE_PTS:
        source_path = host_dir / f"source_pts_{pts:03d}.png"
        decoded_path = controller_dir / f"decoded_pts_{pts:03d}.png"
        if not source_path.exists() or not decoded_path.exists():
            pairs[str(pts)] = {"available": False}
            continue
        source = cv2.imread(str(source_path))
        decoded = cv2.imread(str(decoded_path))
        if source is None or decoded is None or source.shape != decoded.shape:
            pairs[str(pts)] = {"available": False, "reason": "尺寸或读取失败"}
            continue
        pairs[str(pts)] = {
            "available": True,
            "ssim": round(_ssim(source, decoded), 5),
            "psnr_db": round(_psnr(source, decoded), 3),
        }
    available = [item for item in pairs.values() if item.get("available")]
    return {
        "pairs": pairs,
        "ssim_mean": round(float(np.mean([item["ssim"] for item in available])), 5) if available else None,
        "psnr_mean": round(float(np.mean([item["psnr_db"] for item in available])), 3) if available else None,
        "sample_count": len(available),
    }


# ================================ 被控端测试卡 ================================


class CardWindow(QWidget):
    """固定测试卡窗口, 供被控端真实抓屏; 含静态文本、色条、细网格与运动噪声带."""

    def __init__(self, app: QApplication, width: int, height: int) -> None:
        super().__init__()
        self.app = app
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setFixedSize(width, height)
        geometry = self.app.primaryScreen().geometry()
        self.move(
            max(0, (geometry.width() - width) // 2),
            max(0, (geometry.height() - height) // 2 - 40),
        )
        self.card_width = width
        self.card_height = height
        self.tick = 0
        self.image = QImage(width, height, QImage.Format_ARGB32)
        self._noise = np.random.default_rng(20261110).integers(0, 256, size=(70, width, 3), dtype=np.uint8)
        self._band = self._noise
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.timer.start(33)
        self.show()
        self.raise_()
        self._advance()

    def _advance(self) -> None:
        self.tick += 1
        self._render()
        self.update()

    def _render(self) -> None:
        image = QImage(self.card_width, self.card_height, QImage.Format_ARGB32)
        image.fill(QColor(246, 246, 246))
        painter = QPainter(image)
        painter.setPen(QPen(QColor(24, 24, 24)))
        painter.setFont(QFont("Consolas", 26, QFont.Bold))
        painter.drawText(24, 52, "RDC PUBLIC RELAY BENCH")
        painter.setFont(QFont("Consolas", 15))
        painter.drawText(24, 92, "REFERENCE TEXT: RDC-RELAY-2026-11-10")
        painter.drawText(24, 118, "The quick brown fox jumps over the lazy dog 0123456789")
        painter.drawText(24, 144, "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz")
        painter.drawText(24, 170, "SSIM / PSNR / BITRATE / FPS / FRAME-DROP")
        painter.setFont(QFont("Microsoft YaHei", 15))
        painter.drawText(24, 200, "远程桌面公网中继实测固定文本，用于清晰度判断。")
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        for x in range(24, self.card_width - 24, 40):
            painter.drawLine(x, 216, x, 420)
        for y in range(216, 421, 40):
            painter.drawLine(24, y, self.card_width - 24, y)
        colors = ("#ffffff", "#ffff00", "#00ffff", "#00ff00", "#ff00ff", "#ff0000", "#0000ff", "#000000")
        for index, value in enumerate(colors):
            painter.fillRect(24 + index * 114, 366, 108, 44, QColor(value))
        painter.end()
        self.image = image
        band = np.roll(self._noise, (self.tick * 11) % self.card_width, axis=1)
        self._band = band
        band_image = QImage(band.data, self.card_width, 70, self.card_width * 3, QImage.Format_RGB888)
        painter = QPainter(image)
        painter.drawImage(0, self.card_height - 70, band_image)
        painter.end()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.drawImage(0, 0, self.image)
        painter.end()

    def physical_rect(self) -> tuple[int, int, int, int]:
        """返回抓屏坐标系下的窗口矩形 (left, top, right, bottom)."""
        ratio = float(self.devicePixelRatioF())
        geometry = self.geometry()
        left = round(geometry.x() * ratio)
        top = round(geometry.y() * ratio)
        return left, top, left + self.card_width, top + self.card_height

    def closeEvent(self, event: Any) -> None:
        self.timer.stop()
        super().closeEvent(event)


# ================================ 编码与发送 ================================


class BenchEncoder:
    """H.264 低延时编码器, 每帧自带 SPS/PPS."""

    def __init__(self, width: int, height: int, fps: int, bitrate: int) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.context: av.CodecContext | None = None

    def open(self) -> None:
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
        if self.context is None:
            raise RuntimeError("编码器尚未打开")
        frame = av.VideoFrame.from_ndarray(image, format="bgra").reformat(format="yuv420p")
        frame.pts = pts
        return [(bytes(packet), bool(packet.is_keyframe)) for packet in self.context.encode(frame)]


class VideoSender(threading.Thread):
    """按目标帧率抓屏、裁剪测试卡、编码并发送."""

    def __init__(
        self,
        channel: LockedChannel,
        capture: Any,
        encoder: BenchEncoder,
        fps: int,
        rect: tuple[int, int, int, int],
        work: Path,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="bench-video-sender")
        self.channel = channel
        self.capture = capture
        self.encoder = encoder
        self.fps = fps
        self.rect = rect
        self.work = work
        self.stop_event = stop_event
        self.frames = 0
        self.packets = 0
        self.bytes = 0
        self.keyframes = 0
        self.empty_grabs = 0
        self.encode_ms: list[float] = []
        self.capture_ms: list[float] = []
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0

    def run(self) -> None:
        interval = 1.0 / float(self.fps)
        deadline = time.perf_counter()
        left, top, right, bottom = self.rect
        pts = 0
        self.started_at = time.time()
        try:
            while not self.stop_event.is_set():
                deadline += interval
                frame = self.capture.grab()
                if frame is None:
                    self.empty_grabs += 1
                    time.sleep(0.004)
                    continue
                crop = frame.image[top:bottom, left:right]
                if crop.shape[0] != self.encoder.height or crop.shape[1] != self.encoder.width:
                    crop = cv2.resize(
                        crop,
                        (self.encoder.width, self.encoder.height),
                        interpolation=cv2.INTER_AREA,
                    )
                crop = np.ascontiguousarray(crop)
                if pts in SAMPLE_PTS:
                    cv2.imwrite(str(self.work / f"source_pts_{pts:03d}.png"), cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR))
                started = time.perf_counter()
                packets = self.encoder.encode(crop, pts)
                self.encode_ms.append((time.perf_counter() - started) * 1000.0)
                self.capture_ms.append(float(frame.capture_ms))
                for payload, is_key in packets:
                    sent_ns = time.perf_counter_ns()
                    self.channel.send(
                        {
                            "kind": "video",
                            "codec": "h264",
                            "w": self.encoder.width,
                            "h": self.encoder.height,
                            "fps": self.fps,
                            "key": is_key,
                            "pts": pts,
                            "seq": self.packets,
                            "data": b64encode(payload),
                            "sent_ns": sent_ns,
                        }
                    )
                    self.packets += 1
                    self.bytes += len(payload)
                    self.keyframes += 1 if is_key else 0
                self.frames += 1
                pts += 1
                sleep_for = deadline - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    deadline = time.perf_counter()
        except (OSError, E2EFailure, RuntimeError) as error:
            self.error = f"{type(error).__name__}: {error}"
            _emit(f"视频发送线程结束: {self.error}")
        finally:
            self.finished_at = time.time()

    def summary(self) -> dict[str, Any]:
        elapsed = max(self.finished_at - self.started_at, 0.001) if self.finished_at else 0.0
        return {
            "frames": self.frames,
            "packets": self.packets,
            "bytes": self.bytes,
            "keyframes": self.keyframes,
            "empty_grabs": self.empty_grabs,
            "elapsed_s": round(elapsed, 3),
            "bitrate_kbps": round(self.bytes * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
            "encode_ms_p50": percentile_ms(self.encode_ms, 0.5),
            "encode_ms_p95": percentile_ms(self.encode_ms, 0.95),
            "capture_ms_p50": percentile_ms(self.capture_ms, 0.5),
            "capture_ms_p95": percentile_ms(self.capture_ms, 0.95),
            "error": self.error,
        }


# ================================ 被控端 ================================


def _run_host(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    _configure_log(work / "host.log")
    stop_event = threading.Event()
    app = QApplication.instance() or QApplication([])
    card = CardWindow(app, CARD_WIDTH, CARD_HEIGHT)
    result: dict[str, Any] = {"role": "host", "state": "starting"}
    done = threading.Event()

    def worker() -> None:
        nonlocal result
        try:
            result = _host_session(args, work, card, stop_event)
        except (OSError, ValueError, RuntimeError, E2EFailure, ssl.SSLError) as error:
            result = {"role": "host", "state": "failed", "error": f"{type(error).__name__}: {error}"}
            _emit(f"被控端失败: {result['error']}")
        finally:
            write_json(work / "host_result.json", result)
            done.set()

    thread = threading.Thread(target=worker, daemon=True, name="bench-host-session")
    thread.start()

    def poll() -> None:
        if done.is_set():
            card.close()
            card.app.exit(0 if result.get("state") == "ok" else 1)

    watchdog = QTimer()
    watchdog.timeout.connect(poll)
    watchdog.start(200)
    code = card.app.exec()
    stop_event.set()
    thread.join(timeout=5.0)
    return int(code)


def _host_session(
    args: argparse.Namespace,
    work: Path,
    card: CardWindow,
    stop_event: threading.Event,
) -> dict[str, Any]:
    from poc.host_capture.capture import DxcamCapture

    timings: dict[str, Any] = {}
    session_id = args.session_id
    identity = DeviceIdentity.generate()
    started = time.perf_counter()
    client = PinnedSignalingClient(args.relay_host, args.relay_port, 90.0, args.tls, args.pin)
    client.connect()
    timings["signaling_connected_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.register_and_auth(identity, "windows-host-bench")
    timings["auth_ok_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    write_json(
        work / "ready.json",
        {
            "device_id": identity.device_id,
            "public_key": identity.public_key_b64,
            "session_id": session_id,
            "tls": client.tls_info,
            "host_started_at": time.time(),
        },
    )
    offer = client.recv_until("offer")
    offer_payload = offer.get("payload", {})
    if str(offer_payload.get("session_id")) != session_id:
        raise E2EFailure(f"offer 会话号不匹配: {offer_payload.get('session_id')}")
    # 被控端先完成中继数据面接入, 再回 answer; 避免转发帧先于 relay_ready 到达的乱序竞态
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": 12000, "est_duration_s": int(args.duration) + 120},
        session_id=session_id,
    )
    allocated = client.recv_until("relay_allocated")
    allocated_payload = allocated.get("payload", {})
    budget_kbps = int(allocated_payload.get("budget_kbps", 0))
    quality_profile = str(allocated_payload.get("quality_profile", ""))
    relay_socket, data_tls = _relay_connect(
        args.relay_host,
        args.relay_port,
        str(allocated_payload.get("relay_token", "")),
        session_id,
        "publisher",
        args.tls,
        args.pin,
    )
    timings["data_plane_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.send_envelope(
        "answer",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": str(offer_payload.get("from_device_id")),
            "chosen_candidate": {"kind": "relay", "address": args.relay_host, "port": args.relay_port},
            "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
        },
        session_id=session_id,
    )
    timings["answer_sent_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    session = host_handshake(relay_socket, identity)
    channel = LockedChannel(AeadChannel(relay_socket, session))
    _emit(f"被控端已接入中继: {args.relay_host}:{args.relay_port} 预算 {budget_kbps} kbps / {quality_profile}")

    start_message = channel.recv()
    if str(start_message.get("kind")) != "start":
        raise E2EFailure(f"未收到 start, 实际 {start_message}")
    timings["start_received_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    capture = DxcamCapture()
    capture.open()
    rect = card.physical_rect()
    left, top, _right, _bottom = rect
    left = min(max(left, 0), max(capture.width - CARD_WIDTH, 0))
    top = min(max(top, 0), max(capture.height - CARD_HEIGHT, 0))
    rect = (left, top, left + CARD_WIDTH, top + CARD_HEIGHT)
    encoder = BenchEncoder(CARD_WIDTH, CARD_HEIGHT, args.fps, args.bitrate)
    encoder.open()
    sender = VideoSender(channel, capture, encoder, args.fps, rect, work, stop_event)
    sender.start()
    _emit(f"采集编码已启动: {CARD_WIDTH}x{CARD_HEIGHT}@{args.fps}fps 目标码率 {args.bitrate} bps")

    stopped_at = 0.0
    try:
        while True:
            message = channel.recv()
            kind = str(message.get("kind", ""))
            if kind == "ping":
                channel.send({"kind": "pong", "seq": int(message.get("seq", 0)), "recv_ns": time.perf_counter_ns()})
            elif kind == "stop":
                stopped_at = time.perf_counter()
                break
            elif kind == "error":
                raise E2EFailure(f"控制端返回错误: {message}")
    finally:
        stop_event.set()
        sender.join(timeout=5.0)
        capture.close()

    summary = sender.summary()
    summary["stopped_after_s"] = round(time.perf_counter() - stopped_at, 3) if stopped_at else 0.0
    timings["session_duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    channel.send(
        {
            "kind": "summary",
            "host": summary,
            "timings": timings,
            "budget_kbps": budget_kbps,
            "quality_profile": quality_profile,
        }
    )
    time.sleep(0.5)
    with _suppress_close():
        relay_socket.close()
    client.close()
    return {
        "role": "host",
        "state": "ok",
        "session_id": session_id,
        "resolved_rect": rect,
        "capture_size": f"{capture.width}x{capture.height}",
        "encoder": f"{CARD_WIDTH}x{CARD_HEIGHT}@{args.fps} {args.bitrate}bps",
        "budget_kbps": budget_kbps,
        "quality_profile": quality_profile,
        "host_timings_ms": timings,
        "tls": {"signaling": client.tls_info, "data": data_tls},
        "summary": summary,
    }


class _suppress_close:
    """吞掉关闭连接时的 OSError, 避免影响证据落盘."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


# ================================ 控制端 ================================


def _run_controller(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    _configure_log(work / "controller.log")
    result: dict[str, Any]
    try:
        result = _controller_session(args, work)
    except (OSError, ValueError, RuntimeError, E2EFailure, ssl.SSLError) as error:
        result = {"role": "controller", "state": "failed", "error": f"{type(error).__name__}: {error}"}
        _emit(f"控制端失败: {result['error']}")
    write_json(work / "controller_result.json", result)
    return 0 if result.get("state") == "ok" else 1


def _controller_session(args: argparse.Namespace, work: Path) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    started = time.perf_counter()
    peer = wait_json_file(Path(args.host_work) / "ready.json", lambda value: "device_id" in value, 90.0)
    peer_device_id = str(peer["device_id"])
    phases["wait_host_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    session_id = args.session_id
    identity = DeviceIdentity.generate()
    client = PinnedSignalingClient(args.relay_host, args.relay_port, 90.0, args.tls, args.pin)
    client.connect()
    phases["signaling_connected_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.register_and_auth(identity, "windows-controller-bench")
    phases["auth_ok_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.send_envelope(
        "offer",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": peer_device_id,
            "candidates": [{"kind": "relay", "address": args.relay_host, "port": args.relay_port, "priority": 1}],
            "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
            "access_proof": b64encode(b"relay-public-bench"),
        },
        session_id=session_id,
    )
    client.recv_until("answer")
    phases["answer_received_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.send_envelope(
        "session_start",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": peer_device_id,
            "transport_kind": "relay",
            "quality_profile": "L1",
        },
        session_id=session_id,
    )
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": 12000, "est_duration_s": int(args.duration) + 120},
        session_id=session_id,
    )
    allocated = client.recv_until("relay_allocated")
    allocated_payload = allocated.get("payload", {})
    budget_kbps = int(allocated_payload.get("budget_kbps", 0))
    quality_profile = str(allocated_payload.get("quality_profile", ""))
    phases["relay_allocated_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    relay_socket, data_tls = _relay_connect(
        args.relay_host,
        args.relay_port,
        str(allocated_payload.get("relay_token", "")),
        session_id,
        "subscriber",
        args.tls,
        args.pin,
    )
    session = controller_handshake(relay_socket, identity)
    channel = LockedChannel(AeadChannel(relay_socket, session))
    phases["data_plane_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    channel.send({"kind": "start", "client_started_ns": time.perf_counter_ns()})

    ping_stop = threading.Event()
    rtt_samples: list[float] = []
    pong_times: dict[int, int] = {}
    ping_state = {"sent": 0}

    def ping_loop() -> None:
        seq = 0
        while not ping_stop.wait(RTT_INTERVAL_S):
            try:
                pong_times[seq] = time.perf_counter_ns()
                channel.send(
                    {
                        "kind": "ping",
                        "seq": seq,
                        "sent_ns": time.perf_counter_ns(),
                    }
                )
                seq += 1
                ping_state["sent"] = seq
            except (OSError, RuntimeError):
                return

    ping_thread = threading.Thread(target=ping_loop, daemon=True, name="bench-ping")
    ping_thread.start()

    decoder = av.CodecContext.create("h264", "r")
    decoder.open()
    first_frame_at = 0.0
    last_frame_at = 0.0
    frames_decoded = 0
    packets_received = 0
    bytes_received = 0
    decode_ms: list[float] = []
    decode_errors = 0
    seq_seen: list[int] = []
    summary: dict[str, Any] | None = None
    stop_deadline = 0.0
    while True:
        try:
            message = channel.recv()
        except TimeoutError:
            if stop_deadline and time.perf_counter() > stop_deadline:
                break
            continue
        kind = str(message.get("kind", ""))
        if kind == "video":
            now = time.perf_counter()
            raw = base64.b64decode(str(message.get("data", "")).encode("ascii"))
            packets_received += 1
            bytes_received += len(raw)
            seq_seen.append(int(message.get("seq", -1)))
            if first_frame_at == 0.0:
                first_frame_at = now
                phases["first_video_frame_ms"] = round((now - started) * 1000.0, 3)
            last_frame_at = now
            pts = int(message.get("pts", -1))
            try:
                decoded = decoder.decode(av.Packet(raw))
            except (av.error.FFmpegError, ValueError):
                decode_errors += 1
                decoded = []
            decode_ms.append((time.perf_counter() - now) * 1000.0)
            for frame in decoded:
                frames_decoded += 1
                if pts in SAMPLE_PTS:
                    image = frame.to_ndarray(format="bgr24")
                    cv2.imwrite(str(work / f"decoded_pts_{pts:03d}.png"), image)
            if first_frame_at and not stop_deadline and now - first_frame_at >= args.duration:
                stop_deadline = now + 15.0
                channel.send({"kind": "stop", "sent_ns": time.perf_counter_ns()})
        elif kind == "pong":
            recv_ns = time.perf_counter_ns()
            sent_ns = pong_times.pop(int(message.get("seq", -1)), None)
            if sent_ns is not None:
                rtt_samples.append((recv_ns - sent_ns) / 1_000_000.0)
        elif kind == "summary":
            summary = dict(message)
            break
        elif kind == "error":
            raise E2EFailure(f"被控端返回错误: {message}")

    ping_stop.set()
    ping_thread.join(timeout=2.0)
    if summary is None:
        with contextlib.suppress(OSError, RuntimeError):
            channel.send({"kind": "stop", "sent_ns": time.perf_counter_ns()})
    with _suppress_close():
        relay_socket.close()
    client.close()

    elapsed = max(last_frame_at - first_frame_at, 0.001) if last_frame_at else 0.0
    host_summary = (summary or {}).get("host", {}) if summary else {}
    sequences = [seq for seq in seq_seen if seq >= 0]
    gaps = 0
    if len(sequences) >= 2:
        gaps = max(sequences) + 1 - len(set(sequences))
    return {
        "role": "controller",
        "state": "ok",
        "session_id": session_id,
        "relay": f"{args.relay_host}:{args.relay_port}",
        "tls_enabled": bool(args.tls),
        "tls": {"signaling": client.tls_info, "data": data_tls},
        "budget_kbps": budget_kbps,
        "quality_profile": quality_profile,
        "phases_ms": phases,
        "stream_window_s": round(elapsed, 3),
        "packets_received": packets_received,
        "frames_decoded": frames_decoded,
        "bytes_received": bytes_received,
        "received_bitrate_kbps": round(bytes_received * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
        "fps_decoded": round(frames_decoded / elapsed, 2) if elapsed else 0.0,
        "fps_packets": round(packets_received / elapsed, 2) if elapsed else 0.0,
        "unique_packets": len(set(sequences)),
        "missing_seq_estimate": gaps,
        "loss_ratio_estimate": round(gaps / (max(sequences) + 1), 4) if sequences else None,
        "decode_errors": decode_errors,
        "decode_ms_p50": percentile_ms(decode_ms, 0.5),
        "decode_ms_p95": percentile_ms(decode_ms, 0.95),
        "rtt_ms_p50": percentile_ms(rtt_samples, 0.5),
        "rtt_ms_p95": percentile_ms(rtt_samples, 0.95),
        "rtt_ms_min": round(min(rtt_samples), 3) if rtt_samples else None,
        "rtt_ms_max": round(max(rtt_samples), 3) if rtt_samples else None,
        "rtt_samples": len(rtt_samples),
        "pings_sent": ping_state["sent"],
        "host_summary": host_summary,
        "host_timings_ms": (summary or {}).get("timings", {}) if summary else {},
        "quality": _quality_compare(Path(args.host_work), work),
    }


# ================================ 探针与套件 ================================


def _run_probe(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    _configure_log(work / "probe.log")
    result: dict[str, Any] = {"state": "failed"}
    # 复用与正式场景相同的会话号, 避免多次探测在服务端占用额外并发名额
    session_id = "bench-public"
    give_up_at = time.monotonic() + 300.0
    attempt = 0
    while True:
        try:
            started = time.perf_counter()
            identity = DeviceIdentity.generate()
            client = PinnedSignalingClient(args.relay_host, args.relay_port, 30.0, True, args.pin)
            client.connect()
            tls_info = client.tls_info
            tcp_tls_ms = round((time.perf_counter() - started) * 1000.0, 3)
            client.register_and_auth(identity, "relay-bench-probe")
            auth_ms = round((time.perf_counter() - started) * 1000.0, 3)
            client.send_envelope(
                "relay_allocate",
                {"session_id": session_id, "requested_kbps": 12000, "est_duration_s": 60},
                session_id=session_id,
            )
            allocated = client.recv_until("relay_allocated")
            payload = allocated.get("payload", {})
            allocated_ms = round((time.perf_counter() - started) * 1000.0, 3)
            client.close()
            result = {
                "state": "ok",
                "relay": f"{args.relay_host}:{args.relay_port}",
                "tls": tls_info,
                "tcp_tls_ms": tcp_tls_ms,
                "auth_ok_ms": auth_ms,
                "relay_allocated_ms": allocated_ms,
                "budget_kbps": int(payload.get("budget_kbps", 0)),
                "quality_profile": str(payload.get("quality_profile", "")),
                "attempt": attempt + 1,
            }
            break
        except (OSError, ValueError, RuntimeError, E2EFailure, ssl.SSLError) as error:
            result = {"state": "failed", "error": f"{type(error).__name__}: {error}", "attempt": attempt + 1}
            if "RELAY_FULL" not in str(error) or time.monotonic() >= give_up_at:
                break
            attempt += 1
            _emit("中继并发名额被旧会话占用, 15 秒后重试探测")
            time.sleep(15.0)
    write_json(work / "probe.json", result)
    _emit(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("state") == "ok" else 1


def _start_local_relay(work: Path) -> subprocess.Popen[bytes]:
    """启动本地回环中继, 预算参数与公网中继一致 (3 Mbps / 8% 开销 / 2 路 = 1380 kbps)."""
    config = work / "local-relay.conf"
    config.write_text(
        "\n".join(
            [
                "[server]",
                "bind = 127.0.0.1",
                f"port = {LOCAL_RELAY_PORT}",
                "health_bind = 127.0.0.1",
                f"health_port = {LOCAL_HEALTH_PORT}",
                "max_connections = 32",
                "max_connections_per_ip = 16",
                "connect_rate_per_min = 0",
                "",
                "[relay]",
                f"endpoint = relay://127.0.0.1:{LOCAL_RELAY_PORT}",
                "egress_mbps = 3",
                "overhead_ratio = 0.08",
                "max_sessions = 2",
                "min_session_kbps = 200",
                "degrade_profile = L1",
                "",
                "[logging]",
                "level = INFO",
                "",
                "[tls]",
                "tls_enabled = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = f"{PROJECT_ROOT};{PROJECT_ROOT / 'src'}"
    environment["PYTHONUNBUFFERED"] = "1"
    log = (work / "local-relay.log").open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config)],
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", LOCAL_RELAY_PORT), timeout=0.5):
                return process
        except OSError:
            if process.poll() is not None:
                raise E2EFailure("本地中继启动失败, 详见 local-relay.log") from None
            time.sleep(0.2)
    process.terminate()
    raise E2EFailure("本地中继 15 秒内未监听")


def _run_scenario(
    name: str,
    work: Path,
    relay_host: str,
    relay_port: int,
    use_tls: bool,
    bitrate: int,
    duration: int,
    session_id: str,
    tls_pin: str,
) -> dict[str, Any]:
    scenario = work / name
    host_dir = scenario / "host"
    controller_dir = scenario / "controller"
    host_dir.mkdir(parents=True, exist_ok=True)
    controller_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = f"{PROJECT_ROOT};{PROJECT_ROOT / 'src'};{PROJECT_ROOT / 'scripts'}"
    environment["PYTHONUNBUFFERED"] = "1"
    common = [
        sys.executable,
        str(Path(__file__).resolve()),
    ]
    tls_args = ["--tls", "--pin", tls_pin] if use_tls else []
    host_log = (scenario / "host_stdout.log").open("wb")
    controller_log = (scenario / "controller_stdout.log").open("wb")
    host = subprocess.Popen(
        [
            *common,
            "host",
            "--work",
            str(host_dir),
            "--relay-host",
            relay_host,
            "--relay-port",
            str(relay_port),
            "--session-id",
            session_id,
            "--bitrate",
            str(bitrate),
            "--duration",
            str(duration + 30),
            *tls_args,
        ],
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=host_log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_json_file(host_dir / "ready.json", lambda value: "device_id" in value, 60.0)
        controller = subprocess.Popen(
            [
                *common,
                "controller",
                "--work",
                str(controller_dir),
                "--host-work",
                str(host_dir),
                "--relay-host",
                relay_host,
                "--relay-port",
                str(relay_port),
                "--session-id",
                session_id,
                "--duration",
                str(duration),
                *tls_args,
            ],
            cwd=str(PROJECT_ROOT),
            env=environment,
            stdout=controller_log,
            stderr=subprocess.STDOUT,
        )
        controller_code = controller.wait(timeout=duration + 120)
        host_code = host.wait(timeout=60)
    finally:
        for process in (host,):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
    controller_result = _load_json(controller_dir / "controller_result.json")
    host_result = _load_json(host_dir / "host_result.json")
    return {
        "scenario": name,
        "relay": f"{relay_host}:{relay_port}",
        "tls": use_tls,
        "encoder_target_bps": bitrate,
        "duration_target_s": duration,
        "host_exit": host_code,
        "controller_exit": controller_code,
        "host": host_result,
        "controller": controller_result,
    }


def _run_suite(args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(args.work) if args.work else PROJECT_ROOT / "evidence" / f"relay-public-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    _configure_log(work / "suite.log")
    result: dict[str, Any] = {"state": "running", "work": str(work), "scenarios": {}}
    relay_process: subprocess.Popen[bytes] | None = None
    try:
        probe = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "probe",
                "--work",
                str(work / "probe"),
                "--relay-host",
                args.relay_host,
                "--relay-port",
                str(args.relay_port),
                "--pin",
                args.pin,
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=60,
            check=False,
        )
        result["probe"] = _load_json(work / "probe" / "probe.json")
        result["probe_exit"] = probe.returncode
        relay_process = _start_local_relay(work)
        _emit("本地回环中继已启动")
        scenarios = [
            ("local-static", "127.0.0.1", LOCAL_RELAY_PORT, False, args.static_bitrate, args.duration, "bench-local"),
            ("local-stress", "127.0.0.1", LOCAL_RELAY_PORT, False, args.stress_bitrate, args.duration, "bench-local"),
            (
                "public-static",
                args.relay_host,
                args.relay_port,
                True,
                args.static_bitrate,
                args.duration,
                "bench-public",
            ),
            (
                "public-stress",
                args.relay_host,
                args.relay_port,
                True,
                args.stress_bitrate,
                args.duration,
                "bench-public",
            ),
        ]
        for name, host, port, use_tls, bitrate, duration, session_id in scenarios:
            _emit(f"开始场景 {name}: {host}:{port} TLS={use_tls} 目标码率 {bitrate} bps")
            result["scenarios"][name] = _run_scenario(
                name, work, host, port, use_tls, bitrate, duration, session_id, args.pin
            )
            _emit(f"场景 {name} 结束")
            time.sleep(2.0)
        result["state"] = "ok"
    except (OSError, ValueError, RuntimeError, E2EFailure, subprocess.TimeoutExpired) as error:
        result["state"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        _emit(f"套件失败: {result['error']}")
    finally:
        if relay_process is not None and relay_process.poll() is None:
            relay_process.terminate()
            try:
                relay_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay_process.kill()
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_json(work / "suite_result.json", result)
    _emit(json.dumps(_compact(result), ensure_ascii=False, indent=2))
    return 0 if result.get("state") == "ok" else 1


def _compact(result: dict[str, Any]) -> dict[str, Any]:
    """把套件结果压缩为便于阅读的摘要."""
    summary: dict[str, Any] = {"state": result.get("state"), "work": result.get("work")}
    if "probe" in result:
        summary["probe"] = result["probe"]
    for name, scenario in result.get("scenarios", {}).items():
        controller = scenario.get("controller", {})
        host = scenario.get("host", {})
        host_summary = host.get("summary", {})
        summary[name] = {
            "controller_state": controller.get("state"),
            "host_state": host.get("state"),
            "budget_kbps": controller.get("budget_kbps"),
            "profile": controller.get("quality_profile"),
            "phases_ms": controller.get("phases_ms"),
            "received_bitrate_kbps": controller.get("received_bitrate_kbps"),
            "sent_bitrate_kbps": host_summary.get("bitrate_kbps"),
            "fps_decoded": controller.get("fps_decoded"),
            "loss_ratio_estimate": controller.get("loss_ratio_estimate"),
            "decode_errors": controller.get("decode_errors"),
            "rtt_ms_p50": controller.get("rtt_ms_p50"),
            "rtt_ms_p95": controller.get("rtt_ms_p95"),
            "ssim_mean": controller.get("quality", {}).get("ssim_mean"),
            "psnr_mean": controller.get("quality", {}).get("psnr_mean"),
            "error": controller.get("error") or host.get("error"),
        }
    return summary


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _configure_log(path: Path) -> None:
    _LOGGER.handlers.clear()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="公网中继真实媒体链路实测")
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser("probe", help="信令与预算探测")
    probe.add_argument("--work", required=True)
    probe.add_argument("--relay-host", default=PUBLIC_HOST)
    probe.add_argument("--relay-port", type=int, default=PUBLIC_PORT)
    probe.add_argument("--pin", default=PUBLIC_CERT_SHA256)
    probe.set_defaults(func=_run_probe)

    suite = subparsers.add_parser("suite", help="本地基线 vs 公网中继完整套件")
    suite.add_argument("--work", default="")
    suite.add_argument("--relay-host", default=PUBLIC_HOST)
    suite.add_argument("--relay-port", type=int, default=PUBLIC_PORT)
    suite.add_argument("--pin", default=PUBLIC_CERT_SHA256)
    suite.add_argument("--static-bitrate", type=int, default=1_200_000)
    suite.add_argument("--stress-bitrate", type=int, default=3_000_000)
    suite.add_argument("--duration", type=int, default=12)
    suite.set_defaults(func=_run_suite)

    host = subparsers.add_parser("host", help="被控端 (Windows 抓屏编码)")
    host.add_argument("--work", required=True)
    host.add_argument("--relay-host", required=True)
    host.add_argument("--relay-port", type=int, required=True)
    host.add_argument("--session-id", required=True)
    host.add_argument("--bitrate", type=int, default=1_200_000)
    host.add_argument("--fps", type=int, default=30)
    host.add_argument("--duration", type=int, default=30)
    host.add_argument("--tls", action="store_true")
    host.add_argument("--pin", default=PUBLIC_CERT_SHA256)
    host.set_defaults(func=_run_host)

    controller = subparsers.add_parser("controller", help="控制端 (解码与统计)")
    controller.add_argument("--work", required=True)
    controller.add_argument("--host-work", required=True)
    controller.add_argument("--relay-host", required=True)
    controller.add_argument("--relay-port", type=int, required=True)
    controller.add_argument("--session-id", required=True)
    controller.add_argument("--duration", type=int, default=12)
    controller.add_argument("--tls", action="store_true")
    controller.add_argument("--pin", default=PUBLIC_CERT_SHA256)
    controller.set_defaults(func=_run_controller)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    return int(arguments.func(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
