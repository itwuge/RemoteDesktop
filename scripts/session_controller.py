# ==========================================================================================
# 文件        : session_controller.py
# 所属目录    : scripts/
# 文件用途    : 跨机真实会话的控制端（Linux, Xorg）: 真实 PySide6 窗口渲染被控端 H.264 画面,
#               键盘事件经中继密文下行送达被控端并注入记事本
# 架构定位    : 验收装置中的控制端进程, 复用产品信令/中继/加密实现, 不修改产品代码
# 核心功能    : 注册认证 → offer → relay_allocate → 数据面订阅端 → AEAD 通道 → H.264 解码渲染 →
#               键盘上行 → 完成取证（窗口截图 + 结果 JSON + 逐字符延迟分位）
# 职责边界    : 不伪造画面; 解码失败如实记录; 不访问公网; 只写本进程证据目录
# ==========================================================================================

"""跨机真实会话控制端（Linux Xorg）.

用法:
    DISPLAY=:0 python3 scripts/session_controller.py --host 192.168.37.1 --port 18750 \\
        --session-id s1 --peer-device-id XXXX --evidence ~/rdc/evidence --expect RDC-REMOTE-INPUT-2026

退出码: 0 完成并取证; 1 失败; 2 超时未收到画面.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import av
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QKeyEvent, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    controller_handshake,
    percentile_ms,
    relay_connect,
)
from rdc.crypto.identity import DeviceIdentity, b64encode


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class SessionWorker(QThread):
    """信令 + 中继 + 解码的承载线程."""

    frame_ready = Signal(QImage)
    prepared_changed = Signal(bool, str)
    proof_ready = Signal(dict)
    failed = Signal(str)
    state_changed = Signal(str)
    ack_received = Signal(int, float, float, float)

    def __init__(self, arguments: argparse.Namespace, outbound: queue.Queue[dict[str, Any]]) -> None:
        super().__init__()
        self.arguments = arguments
        self.outbound = outbound
        self.stop_event = threading.Event()
        self.channel: AeadChannel | None = None
        self.send_lock = threading.Lock()
        self.frames_decoded = 0
        self.bytes_received = 0
        self.first_frame_at = 0.0
        self.decode_ms: list[float] = []
        self.video_size = (0, 0)
        self.encoder_error = ""
        self.result_path = Path(arguments.evidence) / "controller_result.json"
        self.encode_started = time.time()

    # ------------------------------------------------------------------ 线程主体
    def run(self) -> None:
        try:
            identity = DeviceIdentity.generate()
            client = SignalingClient(self.arguments.host, self.arguments.port, timeout_s=60.0)
            client.connect()
            client.register_and_auth(identity, "ubuntu-controller")
            session_id = self.arguments.session_id
            self.state_changed.emit("信令已认证, 发送 offer")
            client.send_envelope(
                "offer",
                {
                    "session_id": session_id,
                    "from_device_id": identity.device_id,
                    "to_device_id": self.arguments.peer_device_id,
                    "candidates": [
                        {
                            "kind": "relay",
                            "address": self.arguments.host,
                            "port": self.arguments.port,
                            "priority": 1,
                        }
                    ],
                    "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
                    "access_proof": b64encode(b"cross-machine-session"),
                },
                session_id=session_id,
            )
            client.recv_until("answer")
            client.send_envelope(
                "session_start",
                {
                    "session_id": session_id,
                    "from_device_id": identity.device_id,
                    "to_device_id": self.arguments.peer_device_id,
                    "transport_kind": "relay",
                    "quality_profile": "L1",
                },
                session_id=session_id,
            )
            client.send_envelope(
                "relay_allocate",
                {"session_id": session_id, "requested_kbps": 12000, "est_duration_s": 600},
                session_id=session_id,
            )
            allocated = client.recv_until("relay_allocated")
            payload = allocated.get("payload", {})
            relay_token = str(payload.get("relay_token", ""))
            self.state_changed.emit(f"中继预算 {payload.get('budget_kbps')} kbps, 接入数据面")
            relay_socket = relay_connect(
                self.arguments.host, self.arguments.port, relay_token, session_id, "subscriber"
            )
            session = controller_handshake(relay_socket, identity)
            channel = AeadChannel(relay_socket, session)
            self.channel = channel
            self.state_changed.emit("数据面与 AEAD 握手完成, 等待画面")

            sender = threading.Thread(target=self._send_loop, daemon=True, name="session-sender")
            sender.start()

            channel.send({"kind": "prepare", "expect": self.arguments.expect})
            decoder = av.CodecContext.create("h264", "r")
            decoder.open()
            _write_json(
                Path(self.arguments.evidence) / "connected.json",
                {
                    "state": "connected",
                    "signaling": f"tcp://{self.arguments.host}:{self.arguments.port}",
                    "relay_budget_kbps": payload.get("budget_kbps"),
                    "device_id": identity.device_id,
                    "connected_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
            while not self.stop_event.is_set():
                message = channel.recv()
                kind = str(message.get("kind", ""))
                if kind == "video":
                    self._on_video(decoder, message)
                elif kind == "prepared":
                    self.prepared_changed.emit(bool(message.get("ok")), str(message.get("reason", "")))
                elif kind == "ack":
                    self.ack_received.emit(
                        int(message.get("seq", 0)),
                        float(message.get("recv_ns", 0)),
                        float(message.get("start_ns", 0)),
                        float(message.get("injected_ns", 0)),
                    )
                elif kind == "proof":
                    _write_json(self.result_path, {"state": "proof", **message})
                    self.proof_ready.emit(dict(message))
                    break
                elif kind == "error":
                    raise E2EFailure(f"被控端返回错误: {message}")
            self.stop_event.set()
            with contextlib.suppress(OSError), self.send_lock:
                relay_socket.close()
            client.close()
        except (OSError, ValueError, RuntimeError, E2EFailure) as error:
            self.failed.emit(f"{type(error).__name__}: {error}")

    # ------------------------------------------------------------------ 收发
    def send(self, message: dict[str, Any]) -> None:
        self.outbound.put(message)

    def _send_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                message = self.outbound.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.channel is None:
                continue
            try:
                with self.send_lock:
                    self.channel.send(message)
            except (OSError, RuntimeError) as error:
                self.encoder_error = f"{type(error).__name__}: {error}"
                self.stop_event.set()
                return

    def _on_video(self, decoder: av.CodecContext, message: dict[str, Any]) -> None:
        started = time.perf_counter()
        raw = _b64decode_safe(str(message.get("data", "")))
        self.bytes_received += len(raw)
        try:
            frames = decoder.decode(av.Packet(raw))
        except (av.error.FFmpegError, ValueError) as error:
            self.encoder_error = f"decode: {type(error).__name__}: {error}"
            return
        for frame in frames:
            bitmap = frame.to_ndarray(format="bgra")
            height, width = bitmap.shape[0], bitmap.shape[1]
            image = QImage(bitmap.data, width, height, bitmap.strides[0], QImage.Format_ARGB32).copy()
            self.video_size = (width, height)
            self.frames_decoded += 1
            if self.first_frame_at == 0.0:
                self.first_frame_at = time.time()
            self.frame_ready.emit(image)
        self.decode_ms.append((time.perf_counter() - started) * 1000.0)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict[str, Any]:
        elapsed = max(time.time() - (self.first_frame_at or time.time()), 0.001)
        return {
            "frames_decoded": self.frames_decoded,
            "bytes_received": self.bytes_received,
            "resolution": f"{self.video_size[0]}x{self.video_size[1]}" if self.video_size[0] else "",
            "fps_observed": round(self.frames_decoded / elapsed, 2) if self.frames_decoded else 0.0,
            "decode_ms_p50": percentile_ms(self.decode_ms, 0.50),
            "decode_ms_p95": percentile_ms(self.decode_ms, 0.95),
            "decode_error": self.encoder_error,
        }


def _b64decode_safe(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


class ControllerWindow(QMainWindow):
    """控制端主窗口: 远端画面 + 状态栏 + 键盘上行."""

    def __init__(self, arguments: argparse.Namespace) -> None:
        super().__init__()
        self.arguments = arguments
        self.setWindowTitle("RDC 控制端 · 跨机会话 [rdc-controller]")
        self.resize(1280, 800)
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = QLabel("等待被控端画面…", central)
        self.video.setAlignment(Qt.AlignCenter)
        self.video.setStyleSheet("background:#101418;color:#c8d2dc;font-size:16px;")
        self.video.setMinimumSize(640, 360)
        self.status = QLabel("初始化", central)
        self.status.setStyleSheet("background:#1b2129;color:#d8e2ec;padding:6px;font-size:13px;")
        layout.addWidget(self.video, 1)
        layout.addWidget(self.status)
        self.setCentralWidget(central)
        self.outbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker = SessionWorker(arguments, self.outbound)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.state_changed.connect(self._on_state)
        self.worker.prepared_changed.connect(self._on_prepared)
        self.worker.proof_ready.connect(self._on_proof)
        self.worker.failed.connect(self._on_failed)
        self.worker.ack_received.connect(self._on_ack)
        self.typed = ""
        self.pending: dict[int, int] = {}
        self.round_trip: list[float] = []
        self.injection: list[float] = []
        self.finishing = False
        self.finished_ok = False
        self.prepared = False
        self.first_frame_seen = 0.0
        self.evidence = Path(arguments.evidence)
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.worker.start()

    # ------------------------------------------------------------------ 槽
    def _on_frame(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        self.video.setPixmap(
            pixmap.scaled(
                self.video.size() * (self.devicePixelRatioF() or 1), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
        if self.first_frame_seen == 0.0:
            self.first_frame_seen = time.time()
            self.status.setText(f"远端画面 {image.width()}x{image.height()} · 等待键盘输入")
        elif not self.finishing and not self.prepared:
            self.status.setText(f"远端画面 {image.width()}x{image.height()} · 等待被控端聚焦记事本")

    def _on_state(self, text: str) -> None:
        self.status.setText(text)

    def _on_prepared(self, ok: bool, reason: str) -> None:
        self.prepared = ok
        self.status.setText("被控端记事本已聚焦, 可以输入" if ok else f"被控端聚焦失败: {reason}")

    def _on_ack(self, seq: int, recv_ns: float, start_ns: float, injected_ns: float) -> None:
        sent_ns = self.pending.pop(seq, None)
        if sent_ns is None:
            return
        self.round_trip.append((time.perf_counter_ns() - sent_ns) / 1_000_000)
        self.injection.append((injected_ns - start_ns) / 1_000_000)
        self.status.setText(f"已输入 {seq + 1}/{len(self.arguments.expect)} 字符 · 远端已注入")

    def _on_failed(self, reason: str) -> None:
        _write_json(self.evidence / "controller_result.json", {"state": "failed", "reason": reason})
        self.status.setText(f"失败: {reason}")
        self._quit(1)

    def _on_proof(self, proof: dict[str, Any]) -> None:
        self.finished_ok = True
        self.status.setText("被控端取证完成, 正在保存控制端证据")
        self._save_result(proof)
        self._quit(0)

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self.finishing or not self.prepared:
            super().keyPressEvent(event)
            return
        text = event.text()
        if len(text) == 1 and 32 <= ord(text) <= 126:
            seq = len(self.typed)
            self.pending[seq] = time.perf_counter_ns()
            self.typed += text
            self.outbound.put({"kind": "key", "seq": seq, "char": text, "sent_ns": time.time_ns()})
            expected = self.arguments.expect
            if self.typed == expected:
                self.finishing = True
                self.outbound.put({"kind": "finish", "expect": expected})
            elif not expected.startswith(self.typed):
                self.status.setText(f"输入与被控端期望不一致: {self.typed!r}")
            super().keyPressEvent(event)
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 收尾
    def _save_result(self, proof: dict[str, Any]) -> None:
        screenshot = self.evidence / "controller_window.png"
        self.centralWidget().grab().save(str(screenshot), "PNG")
        desktop = self.evidence / "controller_desktop.png"
        screen = QApplication.primaryScreen()
        if screen is not None:
            screen.grabWindow(0).save(str(desktop), "PNG")
        result = {
            "state": "done",
            "typed": self.typed,
            "expect": self.arguments.expect,
            "remote_proof": proof,
            "controller": self.worker.stats(),
            "latency_ms": {
                "samples": len(self.round_trip),
                "rtt_p50": percentile_ms(self.round_trip, 0.50),
                "rtt_p95": percentile_ms(self.round_trip, 0.95),
                "injection_p50": percentile_ms(self.injection, 0.50),
                "injection_p95": percentile_ms(self.injection, 0.95),
                "caliber": "rtt 为控制端本机时钟下单次按键往返耗时（含中继与远端注入）; 单程需两端时钟同步, 未测",
            },
            "screenshot": str(screenshot),
            "screenshot_bytes": screenshot.stat().st_size if screenshot.is_file() else 0,
            "desktop_screenshot": str(desktop),
            "desktop_screenshot_bytes": desktop.stat().st_size if desktop.is_file() else 0,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_json(self.evidence / "controller_result.json", result)

    def _quit(self, code: int) -> None:
        self.worker.stop_event.set()
        self.worker.wait(3000)
        QApplication.instance().exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨机真实会话控制端")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--peer-device-id", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--expect", default="RDC-REMOTE-INPUT-2026")
    arguments = parser.parse_args(argv)
    application = QApplication([sys.argv[0]])
    window = ControllerWindow(arguments)
    window.show()
    window.raise_()
    window.activateWindow()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
