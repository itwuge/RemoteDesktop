# ==========================================================================================
# 文件        : relay_public_bench.py
# 所属目录    : scripts/
# 文件用途    : 公网中继真实媒体链路实测: Windows 被控端采集编码 → 公网中继 → 控制端解码,
#               并与本地回环中继做同场景对照
# 架构定位    : 验收装置, 复用产品信令/中继/加密原语, 不修改产品代码与契约
# 核心功能    : 以仓库自签 CA 为信任锚的 TLS 1.3 链校验; 连接建立分阶段计时; 码率/帧率/丢帧/按键往返 RTT 采集;
#               固定测试卡 SSIM/PSNR 画质对照; 本地基线 vs 公网中继量化差异
# 职责边界    : 只作为客户端连接被测中继; 不修改服务端配置; 不重启服务; 不访问 22 端口与 nginx;
#               不写任何凭据; 模拟值不标注为实测
# ==========================================================================================

"""公网中继真实链路实测装置.

用法:
    python scripts/relay_public_bench.py probe --work <证据目录>
    python scripts/relay_public_bench.py suite --work <证据目录>
    python scripts/relay_public_bench.py host --work <目录> --relay-host <地址> --relay-port <端口>
        --session-id <会话号> [--tls] [--ca <CA 证书>] [--bitrate 1200000] [--duration 30]
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
import math
import os
import re
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
import psutil
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
from rdc.media.frame import (
    MAGIC as FRAME_MAGIC,
)
from rdc.media.frame import (
    VERSION_V1,
    VERSION_V2,
    VideoFrame,
    decode_v2,
    encode_v2,
    negotiate_frame_version,
)

# 公网中继地址: 环境变量可覆盖; 未设置时用仓库固化的公网端点, 不再以占位符搁置
PUBLIC_HOST = os.environ.get("RDC_RELAY_HOST", "").strip() or "47.109.32.249"
PUBLIC_PORT = 8750
# 信任锚: 服务端自签 CA 公钥, 客户端只以它为根做证书链校验.
# 不钉叶证书指纹: 服务端续期后叶证书会变, 钉 CA 才不会在 2027-02-08 到期续期时全线失效.
RELAY_CA_FILE = PROJECT_ROOT / "deploy" / "relay" / "ca.crt"
RELAY_CA_SHA256 = "31c7369bbfe9c0faf9a17e05e2d19ea83cf9d5ae46e3cd018c6062aeb5ccf9d8"
# 叶证书指纹仅作辅助输出, 供人工比对, 不参与任何校验
LEAF_CERT_SHA256 = "44f338101f4b79b966a9c873f6d759c5a961ed4218a37501ae063cb9bfdcfcd5"
LOCAL_RELAY_PORT = 18750
LOCAL_HEALTH_PORT = 18751
SAMPLE_PTS = (0, 10, 15, 20, 30, 40, 45, 50, 60)
CARD_WIDTH = 960
CARD_HEIGHT = 540
# RTT 探测间隔: 媒体帧在流时 2 次/秒已足够采样; 无媒体时 1 次/秒, 媒体本身即保活.
# 旧值固定 20 次/秒属于心跳式探测, 在 200 kbps 低码率下控制流量会占媒体的 5% 以上.
RTT_ACTIVE_INTERVAL_S = 0.5
RTT_IDLE_INTERVAL_S = 1.0
RTT_IDLE_GAP_S = 1.0
# 仅用于改前/改后对照: 设置后所有探测固定为该间隔, 0 表示按数据流自适应
RTT_INTERVAL_ENV = "RDC_BENCH_RTT_INTERVAL"
_LOGGER = logging.getLogger("relay_public_bench")


def _emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    _LOGGER.info(text)


# ================================ TLS 信任锚校验 ================================


def _load_ca(ca_file: Path, ca_sha256: str) -> tuple[str | bytes, x509.Certificate]:
    """读取本地 CA 证书, 先按 SHA-256 确认信任锚未被替换.

    返回 load_verify_locations 可直接使用的 cadata: PEM 文本或 DER 字节.
    """
    payload = ca_file.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != ca_sha256:
        raise E2EFailure(f"信任锚 CA 指纹不匹配: 期望 {ca_sha256}, 实际 {actual}")
    if payload.lstrip().startswith(b"-----BEGIN"):
        return payload.decode("ascii"), x509.load_pem_x509_certificate(payload)
    return payload, x509.load_der_x509_certificate(payload)


def _tls_connect(
    host: str,
    port: int,
    ca_file: Path,
    ca_sha256: str,
    timeout_s: float = 10.0,
) -> tuple[socket.socket, dict[str, Any]]:
    """建立 TLS 1.3 连接, 以本地 CA 公钥为唯一信任锚做证书链校验."""
    ca_cadata, ca_certificate = _load_ca(ca_file, ca_sha256)
    raw = socket.create_connection((host, port), timeout=timeout_s)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    # 按 IP 直连, 证书 CN 是域名, 因此不做主机名匹配; 信任锚由固定 CA 校验保证
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=ca_cadata)
    try:
        wrapped = context.wrap_socket(raw, server_hostname=host)
    except ssl.SSLCertVerificationError as error:
        raw.close()
        detail = error.verify_message or str(error)
        raise E2EFailure(f"服务端证书链校验失败: {detail}") from error
    except (ssl.SSLError, OSError):
        raw.close()
        raise
    der = wrapped.getpeercert(binary_form=True) or b""
    leaf_sha256 = hashlib.sha256(der).hexdigest()
    certificate = x509.load_der_x509_certificate(der)
    info = {
        "tls_version": wrapped.version(),
        "cipher": wrapped.cipher()[0] if wrapped.cipher() else "",
        "trust_anchor": "ca",
        "ca_sha256": ca_sha256,
        "ca_subject": ca_certificate.subject.rfc4514_string(),
        # 以下为辅助信息, 不参与校验
        "leaf_sha256": leaf_sha256,
        "leaf_sha256_expected": LEAF_CERT_SHA256,
        "cert_subject": certificate.subject.rfc4514_string(),
        "cert_issuer": certificate.issuer.rfc4514_string(),
        "cert_not_after": certificate.not_valid_after_utc.isoformat(),
    }
    return wrapped, info


class TrustedSignalingClient(SignalingClient):
    """以本地 CA 为信任锚做链校验的信令客户端."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float,
        use_tls: bool,
        ca_file: Path,
        ca_sha256: str,
    ) -> None:
        super().__init__(host, port, timeout_s=timeout_s)
        self.use_tls = use_tls
        self.ca_file = ca_file
        self.ca_sha256 = ca_sha256
        self.tls_info: dict[str, Any] = {}

    def connect(self) -> None:
        """建立 TCP 或 TLS 1.3 连接."""
        if self.use_tls:
            self.sock, self.tls_info = _tls_connect(self.host, self.port, self.ca_file, self.ca_sha256)
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
    ca_file: Path,
    ca_sha256: str,
) -> tuple[socket.socket, dict[str, Any]]:
    """接入中继数据面, 可选 TLS 1.3 CA 链校验."""
    info: dict[str, Any] = {}
    if use_tls:
        sock, info = _tls_connect(host, port, ca_file, ca_sha256)
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


class CountingSocket:
    """统计数据面真实收发字节数, 用于核对中继预算口径."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.bytes_in = 0
        self.bytes_out = 0

    def recv(self, size: int) -> bytes:
        data = self.sock.recv(size)
        self.bytes_in += len(data)
        return data

    def sendall(self, data: bytes) -> None:
        self.bytes_out += len(data)
        self.sock.sendall(data)

    def settimeout(self, value: float) -> None:
        self.sock.settimeout(value)

    def close(self) -> None:
        self.sock.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.sock, name)


class LockedChannel:
    """给 AEAD 通道加发送锁, 允许视频线程与控制帧并发发送."""

    def __init__(self, channel: AeadChannel) -> None:
        self.channel = channel
        self.send_lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> int:
        with self.send_lock:
            return self.channel.send(payload)

    def send_bytes(self, payload: bytes) -> int:
        with self.send_lock:
            return self.channel.send_bytes(payload)

    def recv(self) -> dict[str, Any]:
        return self.channel.recv()

    def recv_raw(self) -> bytes:
        return self.channel.recv_raw()


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
        frame_height = source.shape[0]
        regions = {
            "text": (0, round(frame_height * 0.40)),
            "chart": (round(frame_height * 0.40), round(frame_height * 0.78)),
            "noise": (round(frame_height * 0.87), frame_height),
        }
        pairs[str(pts)] = {
            "available": True,
            "ssim": round(_ssim(source, decoded), 5),
            "psnr_db": round(_psnr(source, decoded), 3),
            "region_ssim": {name: round(_ssim(source[y0:y1], decoded[y0:y1]), 5) for name, (y0, y1) in regions.items()},
        }
    available = [item for item in pairs.values() if item.get("available")]
    region_means: dict[str, float | None] = {}
    for name in ("text", "chart", "noise"):
        values = [item["region_ssim"][name] for item in available if "region_ssim" in item]
        region_means[f"ssim_{name}_mean"] = round(float(np.mean(values)), 5) if values else None
    return {
        "pairs": pairs,
        "ssim_mean": round(float(np.mean([item["ssim"] for item in available])), 5) if available else None,
        "psnr_mean": round(float(np.mean([item["psnr_db"] for item in available])), 3) if available else None,
        **region_means,
        "sample_count": len(available),
    }


# ================================ 被控端测试卡 ================================


class CardWindow(QWidget):
    """合成内容窗口: 固定测试卡与静态桌面/文本编辑/视频播放/快速拖动四类场景.

    所有坐标按 960x540 设计稿等比缩放, 因此同一套内容可跑 540p 与 1080p。
    """

    MODES = ("card", "static", "text", "video", "drag")

    def __init__(
        self,
        app: QApplication,
        width: int,
        height: int,
        content: str = "card",
        fps_target: int = 30,
    ) -> None:
        super().__init__()
        self.app = app
        self.content = content if content in self.MODES else "card"
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
        self.scale_x = width / 960.0
        self.scale_y = height / 540.0
        self.band_height = max(1, round(70 * self.scale_y))
        self._noise = np.random.default_rng(20261110).integers(
            0, 256, size=(self.band_height, width, 3), dtype=np.uint8
        )
        self._base = self._build_base()
        self._floating = self._build_floating_window()
        self._base_np = self._to_np(self._base)
        self._floating_np = self._to_np(self._floating)
        self._frame_np = self._base_np
        # 视频场景横向平移整幅"片源", 需要独立的帧缓冲承接裁剪结果
        self._video_frame = np.empty((height, width, 4), dtype=np.uint8)
        self.image = self._wrap(self._base_np, width, height)
        # Windows 粗计时器把 33 ms 取整到 46.9 ms (21 fps), 必须显式用 PreciseTimer;
        # 实测 PreciseTimer + 33 ms 周期 33.00 ms, 正好 30 fps
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(max(1, round(1000.0 / max(1, fps_target))))
        self.timer.timeout.connect(self._advance)
        self.timer.start()
        self.show()
        self.raise_()
        self._advance()

    @staticmethod
    def _to_np(image: QImage) -> np.ndarray:
        """QImage 转 BGRA ndarray, 供合成帧直接做内存拷贝."""
        width = image.width()
        height = image.height()
        stride = image.bytesPerLine()
        buffer = np.frombuffer(image.constBits(), dtype=np.uint8, count=stride * height)
        return buffer.reshape(height, stride // 4, 4)[:, :width, :].copy()

    @staticmethod
    def _wrap(frame: np.ndarray, width: int, height: int) -> QImage:
        """零拷贝包装 ndarray 为 QImage; 调用方需保证数组生命周期."""
        return QImage(frame.data, width, height, width * 4, QImage.Format_ARGB32)

    def _font(self, size: float, bold: bool = False, family: str = "Consolas") -> QFont:
        """按纵向缩放比生成字号, 保证 540p 与 1080p 观感一致."""
        font = QFont(family, 1)
        font.setPixelSize(max(8, round(size * self.scale_y)))
        font.setBold(bold)
        return font

    def _build_base(self) -> QImage:
        if self.content == "video":
            return self._build_video_canvas()
        width = self.card_width
        height = self.card_height
        sx = self.scale_x
        sy = self.scale_y
        # 文本场景预渲染两页高的长文, 运行时整体滚动, 产生连续且接近真实阅读的运动
        canvas_height = height * 2 if self.content == "text" else height
        image = QImage(width, canvas_height, QImage.Format_ARGB32)
        painter = QPainter(image)
        if self.content == "card":
            image.fill(QColor(246, 246, 246))
            painter.setPen(QPen(QColor(24, 24, 24)))
            painter.setFont(self._font(26, bold=True))
            painter.drawText(round(24 * sx), round(52 * sy), "RDC PUBLIC RELAY BENCH")
            painter.setFont(self._font(15))
            painter.drawText(round(24 * sx), round(92 * sy), "REFERENCE TEXT: RDC-RELAY-2026-11-10")
            painter.drawText(round(24 * sx), round(118 * sy), "The quick brown fox jumps over the lazy dog 0123456789")
            painter.drawText(round(24 * sx), round(144 * sy), "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz")
            painter.drawText(round(24 * sx), round(170 * sy), "SSIM / PSNR / BITRATE / FPS / FRAME-DROP")
            painter.setFont(self._font(15, family="Microsoft YaHei"))
            painter.drawText(round(24 * sx), round(200 * sy), "远程桌面公网中继实测固定文本，用于清晰度判断。")
            painter.setPen(QPen(QColor(200, 200, 200), 1))
            for x in range(round(24 * sx), width - round(24 * sx), max(1, round(40 * sx))):
                painter.drawLine(x, round(216 * sy), x, round(420 * sy))
            for y in range(round(216 * sy), round(421 * sy), max(1, round(40 * sy))):
                painter.drawLine(round(24 * sx), y, width - round(24 * sx), y)
            colors = ("#ffffff", "#ffff00", "#00ffff", "#00ff00", "#ff00ff", "#ff0000", "#0000ff", "#000000")
            for index, value in enumerate(colors):
                painter.fillRect(
                    round((24 + index * 114) * sx),
                    round(366 * sy),
                    round(108 * sx),
                    round(44 * sy),
                    QColor(value),
                )
        elif self.content == "static":
            painter.fillRect(0, 0, width, height, QColor(32, 52, 78))
            painter.fillRect(0, 0, width, round(34 * sy), QColor(24, 24, 24))
            painter.setPen(QPen(QColor(235, 235, 235)))
            painter.setFont(self._font(14))
            painter.drawText(round(16 * sx), round(23 * sy), "文件  编辑  视图  帮助")
            panels = ((round(60 * sx), round(90 * sy), 0.42), (round(0.50 * width), round(150 * sy), 0.40))
            for left, top, ratio in panels:
                painter.fillRect(left, top, round(width * ratio), round(height * 0.42), QColor(250, 250, 250))
                painter.setPen(QPen(QColor(20, 20, 20)))
                painter.setFont(self._font(15))
                painter.drawText(left + round(16 * sx), top + round(28 * sy), "项目文件 / 静态桌面基线")
                painter.setPen(QPen(QColor(90, 90, 90)))
                painter.setFont(self._font(12))
                for line in range(8):
                    painter.drawText(
                        left + round(16 * sx),
                        top + round((60 + line * 22) * sy),
                        f"{line + 1:02d}  静态列表项 reference-item-{line + 1:02d}  状态: 已同步",
                    )
            painter.fillRect(0, height - round(40 * sy), width, round(40 * sy), QColor(24, 24, 24))
        elif self.content == "text":
            image.fill(QColor(255, 255, 255))
            painter.setPen(QPen(QColor(230, 230, 230)))
            line_height = max(1, round(26 * sy))
            for line in range(0, canvas_height, line_height):
                painter.drawLine(0, line, width, line)
            painter.setPen(QPen(QColor(28, 28, 28)))
            painter.setFont(self._font(16))
            content_lines = [
                "def relay_budget(egress_mbps, overhead=0.08, sessions=2):",
                "    usable = egress_mbps * (1 - overhead)",
                "    return usable / sessions",
                "",
                "# 1080p30 quality target: text SSIM >= 0.98",
                "for index, frame in enumerate(capture):",
                "    packet = encoder.encode(frame)",
                "    channel.send({'kind': 'video', 'data': packet})",
                "",
                "class SessionController:",
                "    def on_key(self, event):",
                "        self.forward(event.scancode, event.pressed)",
                "    def on_pointer(self, event):",
                "        self.move(event.x, event.y)",
                "",
                "public relay measured 2026-11-10:",
                "  wire 1.35 Mbps -> media 0.98 Mbps",
                "  framing overhead 1.38x",
                "  rtt p50 103 ms / p95 158 ms",
                "",
                "REMOTE DESKTOP QUALITY BENCH",
                "The quick brown fox jumps over the lazy dog 0123456789",
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz",
                "远程桌面文本编辑场景，用于评估文字边缘清晰度与码率关系。",
            ]
            page_lines = content_lines * 5
            for index, text in enumerate(page_lines):
                painter.drawText(round(40 * sx), round((46 + index * 40) * sy), text)
        else:
            image.fill(QColor(214, 220, 228))
            step = max(1, round(48 * sx))
            for x in range(0, width, step):
                for y in range(0, height, step):
                    if (x // step + y // step) % 2 == 0:
                        painter.fillRect(x, y, step, step, QColor(198, 206, 216))
            painter.setPen(QPen(QColor(150, 160, 172)))
            painter.setFont(self._font(13))
            painter.drawText(round(24 * sx), round(30 * sy), "DESKTOP DRAG BENCH 1920x1080")
        painter.end()
        return image

    def _build_video_canvas(self) -> QImage:
        """构造两屏宽的"片源"画布: 平滑渐变加字幕条, 横向平移即模拟视频播放.

        这是合成样张, 代表解码后视频帧的内容复杂度, 不是真实片源文件解码.
        """
        width = self.card_width
        height = self.card_height
        canvas_width = width * 2
        xs = np.linspace(0.0, 12.0, canvas_width, dtype=np.float32)
        ys = np.linspace(0.0, 6.0, height, dtype=np.float32)
        wave_x = np.sin(xs)[None, :] * 0.5 + 0.5
        wave_y = np.cos(ys * 1.7)[:, None] * 0.5 + 0.5
        red = 0.42 + 0.32 * wave_x * (1.0 - wave_y) + 0.12 * np.cos(xs * 0.7)[None, :]
        green = 0.28 + 0.34 * (1.0 - wave_x) * wave_y + 0.12 * np.sin(xs * 1.3)[None, :]
        blue = 0.34 + 0.30 * (wave_x + wave_y) * 0.5
        frame = np.empty((height, canvas_width, 4), dtype=np.uint8)
        frame[..., 0] = np.clip(blue, 0.0, 1.0) * 255.0
        frame[..., 1] = np.clip(green, 0.0, 1.0) * 255.0
        frame[..., 2] = np.clip(red, 0.0, 1.0) * 255.0
        frame[..., 3] = 255
        frame = cv2.GaussianBlur(frame, (0, 0), sigmaX=max(1.5, 6.0 * self.scale_x))
        image = QImage(frame.data, canvas_width, height, canvas_width * 4, QImage.Format_ARGB32).copy()
        painter = QPainter(image)
        painter.fillRect(0, round(height * 0.78), canvas_width, round(height * 0.22), QColor(0, 0, 0, 110))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.setFont(self._font(16))
        painter.drawText(
            round(36 * self.scale_x),
            round(height * 0.86),
            "视频播放样张 · 1080p30 · 字幕行用于判断文字清晰度 0123456789",
        )
        painter.setFont(self._font(14))
        painter.drawText(
            round(36 * self.scale_x),
            round(height * 0.93),
            "The quick brown fox jumps over the lazy dog — subtitle reference line",
        )
        painter.end()
        return image

    def _build_floating_window(self) -> QImage:
        width = max(1, round(700 * self.scale_x))
        height = max(1, round(420 * self.scale_y))
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(QColor(250, 250, 250))
        painter = QPainter(image)
        painter.fillRect(0, 0, width, round(34 * self.scale_y), QColor(52, 90, 140))
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.setFont(self._font(13, bold=True))
        painter.drawText(round(12 * self.scale_x), round(23 * self.scale_y), "拖动窗口 — drag-target.exe")
        painter.setPen(QPen(QColor(28, 28, 28)))
        painter.setFont(self._font(15))
        for line in range(9):
            painter.drawText(
                round(20 * self.scale_x),
                round((72 + line * 34) * self.scale_y),
                f"{line + 1:02d}  拖动测试行 reference-line-{line + 1:02d}  0123456789",
            )
        painter.end()
        return image

    def _advance(self) -> None:
        self.tick += 1
        self._render()
        self.update()

    def _render(self) -> None:
        width = self.card_width
        height = self.card_height
        sy = self.scale_y
        if self.content == "card":
            self._render_card()
            return
        if self.content == "static":
            self._frame_np = self._base_np
        elif self.content == "text":
            scroll = (self.tick * 6) % height
            self._frame_np = self._base_np[scroll : scroll + height]
        elif self.content == "video":
            # 以约 9 px/帧 横向平移两屏宽片源, 连续运动且不带不可压缩噪声, 接近视频播放
            offset = (self.tick * 9) % width
            np.copyto(self._video_frame, self._base_np[:, offset : offset + width])
            self._frame_np = self._video_frame
        else:
            frame = self._base_np.copy()
            span_x = max(1, width - self._floating_np.shape[1])
            span_y = max(1, height - self._floating_np.shape[0])
            left = round(abs(math.sin(self.tick * 0.11)) * span_x)
            top = round(abs(math.cos(self.tick * 0.09)) * span_y)
            win_h, win_w = self._floating_np.shape[:2]
            shadow_x = min(left + max(2, round(8 * self.scale_x)), width - win_w)
            shadow_y = min(top + max(2, round(8 * sy)), height - win_h)
            frame[shadow_y : shadow_y + win_h, shadow_x : shadow_x + win_w] = (90, 96, 106, 255)
            frame[top : top + win_h, left : left + win_w] = self._floating_np
            self._frame_np = frame
        self.image = self._wrap(self._frame_np, width, height)

    def _render_card(self) -> None:
        width = self.card_width
        height = self.card_height
        image = self._base.copy()
        painter = QPainter(image)
        band = np.roll(self._noise, (self.tick * 11) % width, axis=1)
        band_image = QImage(band.data, width, self.band_height, width * 3, QImage.Format_RGB888)
        painter.drawImage(0, height - self.band_height, band_image)
        painter.end()
        self.image = image

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
        frame_version: int = VERSION_V1,
    ) -> None:
        super().__init__(daemon=True, name="bench-video-sender")
        self.channel = channel
        self.capture = capture
        self.encoder = encoder
        self.fps = fps
        self.rect = rect
        self.work = work
        self.stop_event = stop_event
        self.frame_version = frame_version
        self.frames = 0
        self.packets = 0
        self.bytes = 0
        self.framed_bytes = 0
        self.keyframes = 0
        self.key_seqs: list[int] = []
        self.empty_grabs = 0
        self.encode_ms: list[float] = []
        self.capture_ms: list[float] = []
        self.send_ms: list[float] = []
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
                # Desktop Duplication 在画面未变化时返回 None; 在同一个帧槽内重试,
                # 否则每次 None 都会吞掉整整一个帧槽, 30 fps 会被压到 25 fps 左右
                while frame is None and not self.stop_event.is_set() and time.perf_counter() < deadline - 0.002:
                    time.sleep(0.003)
                    frame = self.capture.grab()
                if frame is None:
                    self.empty_grabs += 1
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
                    send_started = time.perf_counter()
                    if self.frame_version == VERSION_V2:
                        packet = encode_v2(
                            VideoFrame(
                                seq=self.packets,
                                pts=pts,
                                width=self.encoder.width,
                                height=self.encoder.height,
                                keyframe=is_key,
                                payload=payload,
                            )
                        )
                        self.channel.send_bytes(packet)
                        framed_size = len(packet)
                    else:
                        message = {
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
                        wire_plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        self.channel.send_bytes(wire_plaintext)
                        framed_size = len(wire_plaintext)
                    self.send_ms.append((time.perf_counter() - send_started) * 1000.0)
                    self.framed_bytes += framed_size
                    self.packets += 1
                    self.bytes += len(payload)
                    if is_key:
                        self.keyframes += 1
                        self.key_seqs.append(self.packets - 1)
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
            "framed_bytes": self.framed_bytes,
            "frame_version": self.frame_version,
            "keyframes": self.keyframes,
            "key_seqs": self.key_seqs,
            "empty_grabs": self.empty_grabs,
            "elapsed_s": round(elapsed, 3),
            "bitrate_kbps": round(self.bytes * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
            "encode_ms_p50": percentile_ms(self.encode_ms, 0.5),
            "encode_ms_p95": percentile_ms(self.encode_ms, 0.95),
            "send_ms_p50": percentile_ms(self.send_ms, 0.5),
            "send_ms_p95": percentile_ms(self.send_ms, 0.95),
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
    card = CardWindow(app, args.width, args.height, args.content, args.fps)
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
    client = TrustedSignalingClient(args.relay_host, args.relay_port, 90.0, args.tls, Path(args.ca), args.ca_sha256)
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
    peer_caps = offer_payload.get("media_caps")
    frame_version = negotiate_frame_version(
        peer_caps if isinstance(peer_caps, dict) else None,
        local_versions=(int(args.frame_version), VERSION_V1),
    )
    if frame_version != args.frame_version:
        _emit(f"对端未声明 v{args.frame_version}, 帧版本降级为 v{frame_version}")
    # 信令侧要求 relay_allocate 的调用方已加入会话; 用一条 session_start 先登记参与者.
    # 仍然保持"数据面就绪后再 answer"的原顺序, 避免转发帧先于 relay_ready 到达的乱序竞态.
    client.send_envelope(
        "session_start",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": str(offer_payload.get("from_device_id")),
            "transport_kind": "relay",
            "quality_profile": "L0",
        },
        session_id=session_id,
    )
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": 3000, "est_duration_s": int(args.duration) + 120},
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
        Path(args.ca),
        args.ca_sha256,
    )
    timings["data_plane_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    client.send_envelope(
        "answer",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": str(offer_payload.get("from_device_id")),
            "chosen_candidate": {"kind": "relay", "address": args.relay_host, "port": args.relay_port},
            "media_caps": {
                "codecs": ["h264"],
                "max_width": 1920,
                "max_height": 1080,
                "max_fps": 30,
                "frame_versions": [VERSION_V1, VERSION_V2] if frame_version == VERSION_V2 else [VERSION_V1],
            },
        },
        session_id=session_id,
    )
    timings["answer_sent_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    counting = CountingSocket(relay_socket)
    session = host_handshake(counting, identity, session_id, str(offer_payload.get("from_device_id", "")))
    channel = LockedChannel(AeadChannel(counting, session))
    _emit(f"被控端已接入中继: {args.relay_host}:{args.relay_port} 预算 {budget_kbps} kbps / {quality_profile}")

    start_message = channel.recv()
    if str(start_message.get("kind")) != "start":
        raise E2EFailure(f"未收到 start, 实际 {start_message}")
    timings["start_received_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    capture = DxcamCapture()
    capture.open()
    rect = card.physical_rect()
    left, top, _right, _bottom = rect
    left = min(max(left, 0), max(capture.width - args.width, 0))
    top = min(max(top, 0), max(capture.height - args.height, 0))
    rect = (left, top, left + args.width, top + args.height)
    encoder = BenchEncoder(args.width, args.height, args.fps, args.bitrate)
    encoder.open()
    sender = VideoSender(channel, capture, encoder, args.fps, rect, work, stop_event, frame_version=frame_version)
    sender.start()
    _emit(
        f"采集编码已启动: {args.width}x{args.height}@{args.fps}fps 场景 {args.content} "
        f"目标码率 {args.bitrate} bps 帧版本 v{frame_version}"
    )

    stopped_at = 0.0
    try:
        while True:
            message = channel.recv()
            kind = str(message.get("kind", ""))
            if kind == "ping":
                channel.send({"kind": "pong", "seq": int(message.get("seq", 0))})
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
        "encoder": f"{args.width}x{args.height}@{args.fps} {args.bitrate}bps",
        "content": args.content,
        "budget_kbps": budget_kbps,
        "quality_profile": quality_profile,
        "host_timings_ms": timings,
        "tls": {"signaling": client.tls_info, "data": data_tls},
        "data_plane_bytes": {"in": counting.bytes_in, "out": counting.bytes_out},
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
    client = TrustedSignalingClient(args.relay_host, args.relay_port, 90.0, args.tls, Path(args.ca), args.ca_sha256)
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
            "media_caps": {
                "codecs": ["h264"],
                "max_width": 1920,
                "max_height": 1080,
                "max_fps": 30,
                "frame_versions": [VERSION_V1, VERSION_V2] if args.frame_version == VERSION_V2 else [VERSION_V1],
            },
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
            "quality_profile": "L0",
        },
        session_id=session_id,
    )
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": 3000, "est_duration_s": int(args.duration) + 120},
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
        Path(args.ca),
        args.ca_sha256,
    )
    counting = CountingSocket(relay_socket)
    session = controller_handshake(counting, identity, session_id, peer_device_id)
    channel = LockedChannel(AeadChannel(counting, session))
    phases["data_plane_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    channel.send({"kind": "start", "client_started_ns": time.perf_counter_ns()})

    ping_stop = threading.Event()
    rtt_samples: list[float] = []
    pong_times: dict[int, int] = {}
    ping_state = {"sent": 0}
    media_state = {"last_frame_at": 0.0}
    fixed_interval = float(os.environ.get(RTT_INTERVAL_ENV, "0") or "0")

    def ping_loop() -> None:
        seq = 0
        while not ping_stop.is_set():
            # 有媒体在流时按活跃间隔探测, 长时间无媒体才退回空闲间隔
            busy = media_state["last_frame_at"] > 0.0 and (
                time.perf_counter() - media_state["last_frame_at"] < RTT_IDLE_GAP_S
            )
            interval = fixed_interval or (RTT_ACTIVE_INTERVAL_S if busy else RTT_IDLE_INTERVAL_S)
            if ping_stop.wait(interval):
                return
            try:
                pong_times[seq] = time.perf_counter_ns()
                channel.send({"kind": "ping", "seq": seq})
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
    decoded_pts: list[int] = []
    summary: dict[str, Any] | None = None
    stop_deadline = 0.0
    received_v1 = 0
    received_v2 = 0
    while True:
        try:
            plaintext = channel.recv_raw()
        except TimeoutError:
            if stop_deadline and time.perf_counter() > stop_deadline:
                break
            continue
        now = time.perf_counter()
        vframe: VideoFrame | None = None
        control: dict[str, Any] = {}
        if plaintext[:2] == FRAME_MAGIC:
            vframe = decode_v2(plaintext)
            kind = "video"
        else:
            try:
                parsed = json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise E2EFailure(f"AEAD 明文既不是 v2 视频帧也不是 JSON: {error}") from error
            if not isinstance(parsed, dict):
                raise E2EFailure("AEAD 控制帧必须是 JSON 对象")
            control = parsed
            kind = str(control.get("kind", ""))
        if kind == "video":
            if vframe is not None:
                received_v2 += 1
                raw = vframe.payload
                pts = vframe.pts
                seq = vframe.seq
            else:
                received_v1 += 1
                raw = base64.b64decode(str(control.get("data", "")).encode("ascii"))
                pts = int(control.get("pts", -1))
                seq = int(control.get("seq", -1))
            packets_received += 1
            bytes_received += len(raw)
            seq_seen.append(seq)
            if first_frame_at == 0.0:
                first_frame_at = now
                phases["first_video_frame_ms"] = round((now - started) * 1000.0, 3)
            last_frame_at = now
            media_state["last_frame_at"] = now
            try:
                decoded = decoder.decode(av.Packet(raw))
            except (av.error.FFmpegError, ValueError):
                decode_errors += 1
                decoded = []
            decode_ms.append((time.perf_counter() - now) * 1000.0)
            for frame in decoded:
                frames_decoded += 1
                decoded_pts.append(pts)
                if pts in SAMPLE_PTS:
                    image = frame.to_ndarray(format="bgr24")
                    cv2.imwrite(str(work / f"decoded_pts_{pts:03d}.png"), image)
            if first_frame_at and not stop_deadline and now - first_frame_at >= args.duration:
                stop_deadline = now + 15.0
                channel.send({"kind": "stop", "sent_ns": time.perf_counter_ns()})
        elif kind == "pong":
            recv_ns = time.perf_counter_ns()
            sent_ns = pong_times.pop(int(control.get("seq", -1)), None)
            if sent_ns is not None:
                rtt_samples.append((recv_ns - sent_ns) / 1_000_000.0)
        elif kind == "summary":
            summary = dict(control)
            break
        elif kind == "error":
            raise E2EFailure(f"被控端返回错误: {control}")

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
    # 控制流量 = 数据面真实入站 - 帧封装在线上占的字节(帧明文 + 每帧 4+12+16 字节封套)
    framed_wire = int(host_summary.get("framed_bytes", 0) or 0) + 32 * packets_received
    control_bytes = max(counting.bytes_in - framed_wire, 0)
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
        "media_bytes": bytes_received,
        "frame_version_counts": {"v1": received_v1, "v2": received_v2},
        "wire_media_ratio": round(counting.bytes_in / bytes_received, 4) if bytes_received else None,
        "control_bytes": control_bytes,
        "control_ratio": round(control_bytes / bytes_received, 4) if bytes_received else None,
        "rtt_interval_s": fixed_interval or None,
        "overhead_bytes_per_frame": (
            round((counting.bytes_in - bytes_received) / packets_received, 2) if packets_received else None
        ),
        "received_bitrate_kbps": round(bytes_received * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
        "fps_decoded": round(frames_decoded / elapsed, 2) if elapsed else 0.0,
        "fps_packets": round(packets_received / elapsed, 2) if elapsed else 0.0,
        "unique_packets": len(set(sequences)),
        "missing_seq_estimate": gaps,
        "missing_seq": sorted(set(range(max(sequences) + 1)) - set(sequences))[:200] if sequences else [],
        "loss_ratio_estimate": round(gaps / (max(sequences) + 1), 4) if sequences else None,
        "decoded_pts": decoded_pts,
        "data_plane_bytes": {"in": counting.bytes_in, "out": counting.bytes_out},
        "wire_bitrate_kbps": round(counting.bytes_in * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
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


def _environment_snapshot(relay_host: str, work: Path) -> dict[str, Any]:
    """记录测试时段的网络时延与本机负载, 用于说明测试前提."""
    cpu_percent = psutil.cpu_percent(interval=1.0)
    memory = psutil.virtual_memory()
    python_processes = 0
    for process in psutil.process_iter(["name"]):
        try:
            if str(process.info.get("name", "")).lower().startswith("python"):
                python_processes += 1
        except psutil.Error:
            continue
    ping = subprocess.run(
        ["ping", "-n", "10", relay_host],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=30,
        check=False,
    )
    (work / "ping.txt").write_text(ping.stdout, encoding="utf-8")
    samples = [float(value) for value in re.findall(r"time[=<]\s*(\d+)ms", ping.stdout)]
    lost_match = re.search(r"Lost = (\d+)", ping.stdout)
    return {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cpu_load_percent": round(cpu_percent, 1),
        "memory_total_mb": round(memory.total / 1024 / 1024, 1),
        "memory_available_mb": round(memory.available / 1024 / 1024, 1),
        "python_process_count": python_processes,
        "icmp_target": relay_host,
        "icmp_samples_ms": samples,
        "icmp_lost": int(lost_match.group(1)) if lost_match else None,
        "icmp_p50_ms": percentile_ms(samples, 0.5),
        "icmp_p95_ms": percentile_ms(samples, 0.95),
    }


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
            client = TrustedSignalingClient(args.relay_host, args.relay_port, 30.0, True, Path(args.ca), args.ca_sha256)
            client.connect()
            tls_info = client.tls_info
            tcp_tls_ms = round((time.perf_counter() - started) * 1000.0, 3)
            client.register_and_auth(identity, "relay-bench-probe")
            auth_ms = round((time.perf_counter() - started) * 1000.0, 3)
            # 预算只签发给会话参与设备: 探测端先发一条发给自己的 offer 建立会话;
            # recv_until 会跳过这条回显, 不影响后续等待 relay_allocated
            client.send_envelope(
                "offer",
                {
                    "session_id": session_id,
                    "candidates": [
                        {"kind": "host", "address": args.relay_host, "port": args.relay_port, "priority": 1}
                    ],
                    "media_caps": {"codecs": ["h264"]},
                    "access_proof": b64encode(b"relay-public-probe"),
                    "from_device_id": identity.device_id,
                    "to_device_id": identity.device_id,
                },
                session_id=session_id,
            )
            client.send_envelope(
                "relay_allocate",
                {"session_id": session_id, "requested_kbps": 3000, "est_duration_s": 60},
                session_id=session_id,
            )
            allocated = client.recv_until("relay_allocated")
            payload = allocated.get("payload", {})
            allocated_ms = round((time.perf_counter() - started) * 1000.0, 3)
            # 单路闸门下必须显式结束会话, 否则授予会占满 300 秒 TTL, 让后续接入被误拒
            client.send_envelope("session_end", {"session_id": session_id, "reason": "探测结束"}, session_id=session_id)
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


def _start_local_relay(work: Path, egress_mbps: float = 3.0) -> subprocess.Popen[bytes]:
    """启动本地回环中继; 默认与公网一致 (3 Mbps / 0 额外开销 / 1 路 = 3000 kbps, 固定 720p30)."""
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
                f"egress_mbps = {egress_mbps:g}",
                "overhead_ratio = 0.0",
                "max_sessions = 1",
                "min_session_kbps = 200",
                "degrade_profile = L0",
                "target_kbps = 2900",
                "profile_width = 1280",
                "profile_height = 720",
                "profile_fps = 30",
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
    ca_file: str,
    ca_sha256: str,
    width: int = CARD_WIDTH,
    height: int = CARD_HEIGHT,
    content: str = "card",
    fps: int = 30,
    frame_version: int = VERSION_V1,
) -> dict[str, Any]:
    scenario = work / name
    host_dir = scenario / "host"
    controller_dir = scenario / "controller"
    host_dir.mkdir(parents=True, exist_ok=True)
    controller_dir.mkdir(parents=True, exist_ok=True)
    # 复跑同一证据目录时必须清掉上一轮的就绪/结果文件, 否则控制端会读到旧 device_id 并报 PEER_OFFLINE
    for stale in (host_dir / "ready.json", host_dir / "host_result.json", controller_dir / "controller_result.json"):
        stale.unlink(missing_ok=True)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = f"{PROJECT_ROOT};{PROJECT_ROOT / 'src'};{PROJECT_ROOT / 'scripts'}"
    environment["PYTHONUNBUFFERED"] = "1"
    common = [
        sys.executable,
        str(Path(__file__).resolve()),
    ]
    tls_args = ["--tls", "--ca", ca_file, "--ca-sha256", ca_sha256] if use_tls else []
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
            "--width",
            str(width),
            "--height",
            str(height),
            "--content",
            content,
            "--fps",
            str(fps),
            "--frame-version",
            str(frame_version),
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
                "--frame-version",
                str(frame_version),
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
        "content": content,
        "frame_version": frame_version,
        "size": f"{width}x{height}@{fps}",
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
    result["environment"] = _environment_snapshot(args.relay_host, work)
    _emit(
        f"测试环境: {result['environment']['recorded_at']} CPU {result['environment']['cpu_load_percent']}% "
        f"ICMP p50 {result['environment']['icmp_p50_ms']} ms"
    )
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
                "--ca",
                args.ca,
                "--ca-sha256",
                args.ca_sha256,
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=360,
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
                "public-safe",
                args.relay_host,
                args.relay_port,
                True,
                args.safe_bitrate,
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
                name, work, host, port, use_tls, bitrate, duration, session_id, args.ca, args.ca_sha256
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


def _quality_summary(result: dict[str, Any], text_ssim: float, full_ssim: float) -> dict[str, Any]:
    """按场景分组汇总码率与画质, 给出满足阈值的最低编码码率."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for scenario in result.get("scenarios", {}).values():
        controller = scenario.get("controller", {})
        host = scenario.get("host", {})
        quality = controller.get("quality", {})
        content = str(scenario.get("content", "card"))
        frame_version = int(scenario.get("frame_version", VERSION_V1))
        group = f"{content}-v{frame_version}"
        media = controller.get("received_bitrate_kbps") or 0.0
        wire = controller.get("wire_bitrate_kbps") or 0.0
        host_summary = host.get("summary", {}) or {}
        media_bytes = controller.get("media_bytes") or 0
        framed_bytes = host_summary.get("framed_bytes") or 0
        framed_packets = host_summary.get("packets") or 0
        # 帧封装口径: 序列化帧 + 每帧 4 字节长度前缀与 12+16 字节 AEAD, 不含控制帧
        framed_wire = framed_bytes + 32 * framed_packets if framed_bytes else 0
        rows.setdefault(group, []).append(
            {
                "bitrate_kbps": round((scenario.get("encoder_target_bps") or 0) / 1000.0, 1),
                "frame_version": frame_version,
                "sent_kbps": host_summary.get("bitrate_kbps"),
                "media_kbps": media,
                "wire_kbps": wire,
                "wire_media_ratio": round(wire / media, 3) if media else None,
                "framed_ratio": round(framed_wire / media_bytes, 4) if framed_wire and media_bytes else None,
                "control_bytes": controller.get("control_bytes"),
                "control_ratio": controller.get("control_ratio"),
                "rtt_ms_p50": controller.get("rtt_ms_p50"),
                "rtt_samples": controller.get("rtt_samples"),
                "overhead_bytes_per_frame": controller.get("overhead_bytes_per_frame"),
                "loss_ratio": controller.get("loss_ratio_estimate"),
                "fps_decoded": controller.get("fps_decoded"),
                "send_ms_p50": host_summary.get("send_ms_p50"),
                "send_ms_p95": host_summary.get("send_ms_p95"),
                "decode_ms_p50": controller.get("decode_ms_p50"),
                "ssim_mean": quality.get("ssim_mean"),
                "ssim_text_mean": quality.get("ssim_text_mean"),
                "psnr_mean": quality.get("psnr_mean"),
                "samples": quality.get("sample_count"),
            }
        )
    acceptable: dict[str, float | None] = {}
    for content, items in rows.items():
        items.sort(key=lambda item: item["bitrate_kbps"])
        meeting = [
            item["bitrate_kbps"]
            for item in items
            if (item["ssim_text_mean"] or 0.0) >= text_ssim and (item["ssim_mean"] or 0.0) >= full_ssim
        ]
        acceptable[content] = min(meeting) if meeting else None
    return {"rows": rows, "acceptable_min_kbps": acceptable}


def _run_quality(args: argparse.Namespace) -> int:
    """1080p30 码率-画质扫描: 本地直连, 本地中继放开出口预算, 按内容类型找最低可接受码率."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = Path(args.work) if args.work else PROJECT_ROOT / "evidence" / f"quality-1080p-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    _configure_log(work / "quality.log")
    bitrates = [int(item) for item in args.bitrates.split(",") if item.strip()]
    contents = [item.strip() for item in args.contents.split(",") if item.strip()]
    frame_versions = [int(item) for item in args.frame_versions.split(",") if item.strip()]
    if not frame_versions or any(version not in (VERSION_V1, VERSION_V2) for version in frame_versions):
        raise ValueError(f"帧版本只能是 1 或 2: {args.frame_versions}")
    result: dict[str, Any] = {
        "state": "running",
        "work": str(work),
        "size": f"{args.width}x{args.height}@{args.fps}",
        "criteria": {"text_ssim_min": args.text_ssim, "full_ssim_min": args.full_ssim},
        "local_relay_egress_mbps": args.egress_mbps,
        "frame_versions": frame_versions,
        "scenarios": {},
    }
    result["environment"] = _environment_snapshot("127.0.0.1", work)
    relay_process: subprocess.Popen[bytes] | None = None
    try:
        relay_process = _start_local_relay(work, args.egress_mbps)
        _emit(f"本地中继已启动, 出口放开到 {args.egress_mbps} Mbps (不构成预算瓶颈)")
        for content in contents:
            for bitrate in bitrates:
                for frame_version in frame_versions:
                    suffix = f"-v{frame_version}" if len(frame_versions) > 1 else ""
                    name = f"{content}-{bitrate // 1000}k{suffix}"
                    _emit(
                        f"开始 {name}: {args.width}x{args.height}@{args.fps} "
                        f"目标码率 {bitrate} bps 帧版本 v{frame_version}"
                    )
                    result["scenarios"][name] = _run_scenario(
                        name,
                        work,
                        "127.0.0.1",
                        LOCAL_RELAY_PORT,
                        False,
                        bitrate,
                        args.duration,
                        "bench-quality",
                        args.ca,
                        args.ca_sha256,
                        width=args.width,
                        height=args.height,
                        content=content,
                        fps=args.fps,
                        frame_version=frame_version,
                    )
                    _emit(f"结束 {name}")
                    time.sleep(1.0)
        result["summary"] = _quality_summary(result, args.text_ssim, args.full_ssim)
        result["state"] = "ok"
    except (OSError, ValueError, RuntimeError, E2EFailure, subprocess.TimeoutExpired) as error:
        result["state"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        _emit(f"码率扫描失败: {result['error']}")
    finally:
        if relay_process is not None and relay_process.poll() is None:
            relay_process.terminate()
            try:
                relay_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay_process.kill()
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_json(work / "quality_result.json", result)
    _emit(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2))
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
            "wire_bitrate_kbps": controller.get("wire_bitrate_kbps"),
            "sent_bitrate_kbps": host_summary.get("bitrate_kbps"),
            "fps_decoded": controller.get("fps_decoded"),
            "fps_packets": controller.get("fps_packets"),
            "loss_ratio_estimate": controller.get("loss_ratio_estimate"),
            "missing_seq_count": len(controller.get("missing_seq", [])),
            "keyframes_sent": host_summary.get("keyframes"),
            "decode_errors": controller.get("decode_errors"),
            "rtt_ms_p50": controller.get("rtt_ms_p50"),
            "rtt_ms_p95": controller.get("rtt_ms_p95"),
            "ssim_mean": controller.get("quality", {}).get("ssim_mean"),
            "ssim_text_mean": controller.get("quality", {}).get("ssim_text_mean"),
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
    probe.add_argument("--ca", default=str(RELAY_CA_FILE))
    probe.add_argument("--ca-sha256", default=RELAY_CA_SHA256)
    probe.set_defaults(func=_run_probe)

    suite = subparsers.add_parser("suite", help="本地基线 vs 公网中继完整套件")
    suite.add_argument("--work", default="")
    suite.add_argument("--relay-host", default=PUBLIC_HOST)
    suite.add_argument("--relay-port", type=int, default=PUBLIC_PORT)
    suite.add_argument("--ca", default=str(RELAY_CA_FILE))
    suite.add_argument("--ca-sha256", default=RELAY_CA_SHA256)
    suite.add_argument("--static-bitrate", type=int, default=1_200_000)
    suite.add_argument("--safe-bitrate", type=int, default=900_000)
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
    host.add_argument("--frame-version", type=int, default=VERSION_V2, choices=(VERSION_V1, VERSION_V2))
    host.add_argument("--width", type=int, default=CARD_WIDTH)
    host.add_argument("--height", type=int, default=CARD_HEIGHT)
    host.add_argument("--content", default="card", choices=CardWindow.MODES)
    host.add_argument("--duration", type=int, default=30)
    host.add_argument("--tls", action="store_true")
    host.add_argument("--ca", default=str(RELAY_CA_FILE))
    host.add_argument("--ca-sha256", default=RELAY_CA_SHA256)
    host.set_defaults(func=_run_host)

    controller = subparsers.add_parser("controller", help="控制端 (解码与统计)")
    controller.add_argument("--work", required=True)
    controller.add_argument("--host-work", required=True)
    controller.add_argument("--relay-host", required=True)
    controller.add_argument("--relay-port", type=int, required=True)
    controller.add_argument("--session-id", required=True)
    controller.add_argument("--duration", type=int, default=12)
    controller.add_argument("--frame-version", type=int, default=VERSION_V2, choices=(VERSION_V1, VERSION_V2))
    controller.add_argument("--tls", action="store_true")
    controller.add_argument("--ca", default=str(RELAY_CA_FILE))
    controller.add_argument("--ca-sha256", default=RELAY_CA_SHA256)
    controller.set_defaults(func=_run_controller)

    quality = subparsers.add_parser("quality", help="1080p30 码率-画质扫描 (本地直连, 不限预算)")
    quality.add_argument("--work", default="")
    quality.add_argument("--width", type=int, default=1920)
    quality.add_argument("--height", type=int, default=1080)
    quality.add_argument("--fps", type=int, default=30)
    quality.add_argument("--duration", type=int, default=8)
    quality.add_argument("--contents", default="static,text,video,drag")
    quality.add_argument("--bitrates", default="2000000,3000000,4000000,6000000")
    quality.add_argument("--frame-versions", default="2", help="逗号分隔的帧版本; 2 为二进制头(默认), 1 为 JSON+base64")
    quality.add_argument("--egress-mbps", type=float, default=50.0)
    quality.add_argument("--text-ssim", type=float, default=0.98)
    quality.add_argument("--full-ssim", type=float, default=0.90)
    quality.add_argument("--ca", default=str(RELAY_CA_FILE))
    quality.add_argument("--ca-sha256", default=RELAY_CA_SHA256)
    quality.set_defaults(func=_run_quality)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    return int(arguments.func(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
