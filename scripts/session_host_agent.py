# ==========================================================================================
# 文件        : session_host_agent.py
# 所属目录    : scripts/
# 文件用途    : 跨机真实会话的被控端代理（Windows）: 采集屏幕 → H.264 编码 → 中继密文上行,
#               同时接收控制端键盘事件并注入前台记事本
# 架构定位    : 验收装置中的被控端进程, 复用产品信令/中继与 PoC 采集原语, 不修改产品代码
# 核心功能    : 注册认证 → offer/answer → relay_allocate → 数据面发布端 → AEAD 通道 →
#               采集编码发送线程 + 键盘注入接收线程 → 逐字符/文件/截图取证
# 职责边界    : 只写本进程证据目录; 不访问公网; 不伪造通过; 焦点失败如实报告
# ==========================================================================================

"""跨机真实会话被控端代理.

用法:
    python scripts/session_host_agent.py --work <证据目录> --host <信令/中继地址> --port <端口>
        --session-id <会话号> [--fps 15] [--bitrate 6000000] [--duration 300]

证据: <work>/ready.json、<work>/host_result.json、<work>/screenshot.png、<work>/host.log
退出码: 0 取证完成且断言一致; 2 记事本不可用; 3 前台焦点失败; 1 其他失败.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import numpy as np
from poc.host_capture.capture import DxcamCapture

import smoke_env
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    find_window_by_title,
    force_foreground,
    grab_window,
    host_handshake,
    read_clipboard_text,
    relay_connect,
    write_clipboard_text,
    write_json,
)
from input_probe import Injector
from rdc.crypto.identity import DeviceIdentity, b64encode

TARGET_TEXT_DEFAULT = "RDC-REMOTE-INPUT-2026"
PROJECT_ROOT = smoke_env.PROJECT_ROOT
_LOGGER: logging.Logger | None = None


def _emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()
    if _LOGGER is not None:
        _LOGGER.info(text)


class LiveEncoder:
    """常驻的 H.264 编码器: 每帧自带 SPS/PPS, 对端可独立解码."""

    def __init__(self, width: int, height: int, fps: int, bitrate: int) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate = bitrate
        self.context: av.CodecContext | None = None
        self.encode_ms: list[float] = []

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
        started = time.perf_counter()
        frame = av.VideoFrame.from_ndarray(image, format="bgra").reformat(format="yuv420p")
        frame.pts = pts
        packets = [(bytes(packet), bool(packet.is_keyframe)) for packet in self.context.encode(frame)]
        self.encode_ms.append((time.perf_counter() - started) * 1000.0)
        return packets


class VideoSender(threading.Thread):
    """采集编码发送线程: 按目标帧率抓屏并逐帧发往中继."""

    def __init__(
        self,
        channel: AeadChannel,
        capture: DxcamCapture,
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
        self.keyframes = 0
        self.dropped = 0
        self.first_frame_at = 0.0
        self.error = ""

    def run(self) -> None:
        interval = 1.0 / float(self.fps)
        next_deadline = time.perf_counter()
        pts = 0
        try:
            while not self.stop_event.is_set():
                next_deadline += interval
                frame = self.capture.grab()
                if frame is None:
                    self.dropped += 1
                    time.sleep(0.005)
                    continue
                for payload, is_key in self.encoder.encode(frame.image, pts):
                    self.channel.send(
                        {
                            "kind": "video",
                            "codec": "h264",
                            "w": int(frame.image.shape[1]),
                            "h": int(frame.image.shape[0]),
                            "fps": self.fps,
                            "key": is_key,
                            "pts": pts,
                            "data": b64encode(payload),
                            "sent_ns": time.time_ns(),
                        }
                    )
                    self.bytes += len(payload)
                    self.keyframes += 1 if is_key else 0
                pts += 1
                self.frames += 1
                if self.first_frame_at == 0.0:
                    self.first_frame_at = time.time()
                sleep_for = next_deadline - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_deadline = time.perf_counter()
        except (OSError, E2EFailure, RuntimeError) as error:
            self.error = f"{type(error).__name__}: {error}"
            _emit(f"视频发送线程结束: {self.error}")


def _wait_window(keyword: str, timeout_s: float) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        handle = find_window_by_title(keyword)
        if handle is not None:
            return handle
        time.sleep(0.2)
    return None


def _wait_clipboard(expected: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if read_clipboard_text() == expected:
            return True
        time.sleep(0.05)
    return False


def _wait_file_text(path: Path, expected: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if path.read_text(encoding="utf-8-sig") == expected:
                return True
        except OSError:
            pass
        time.sleep(0.1)
    return False


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 3)


def _finish(work: Path, payload: dict[str, Any], status: str, reason: str, code: int) -> int:
    write_json(work / "host_result.json", {"status": status, "reason": reason, **payload})
    _emit(f"被控端结果: {status} {reason}".strip())
    return code


def run(arguments: argparse.Namespace) -> int:
    global _LOGGER
    work: Path = Path(arguments.work)
    work.mkdir(parents=True, exist_ok=True)
    _LOGGER = logging.getLogger("session-host-agent")
    _LOGGER.setLevel(logging.INFO)
    handler = logging.FileHandler(work / "host.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S"))
    _LOGGER.addHandler(handler)
    session_id: str = arguments.session_id
    target = work / "rdc-remote-input.txt"
    target.write_text("", encoding="utf-8")

    notepad = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "notepad.exe"
    if not notepad.is_file():
        return _finish(work, {}, "未验证", f"未找到记事本: {notepad}", 2)

    identity = DeviceIdentity.generate()
    process = subprocess.Popen([str(notepad), str(target)])
    original_clipboard = read_clipboard_text()
    window = _wait_window(target.name, 15.0)
    if window is None:
        process.terminate()
        return _finish(work, {"notepad_pid": process.pid}, "未验证", "记事本窗口 15 秒内未出现", 2)

    client = SignalingClient(arguments.host, arguments.port, timeout_s=120.0)
    channel: AeadChannel | None = None
    relay_socket: socket.socket | None = None
    sender: VideoSender | None = None
    stop_event = threading.Event()
    typed = 0
    try:
        client.connect()
        client.register_and_auth(identity, "session-host-agent")
        write_json(
            work / "ready.json",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "target": str(target),
                "window": window,
                "notepad_pid": process.pid,
                "session_id": session_id,
            },
        )
        offer = client.recv_until("offer")
        offer_payload = offer.get("payload", {})
        if str(offer_payload.get("session_id")) != session_id:
            raise E2EFailure(f"offer 会话号不匹配: {offer_payload.get('session_id')}")
        client.send_envelope(
            "answer",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": str(offer_payload.get("from_device_id")),
                "chosen_candidate": {"kind": "relay", "address": arguments.host, "port": arguments.port},
                "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
            },
            session_id=session_id,
        )
        client.send_envelope(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 12000, "est_duration_s": int(arguments.duration) + 120},
            session_id=session_id,
        )
        allocated = client.recv_until("relay_allocated")
        allocated_payload = allocated.get("payload", {})
        relay_token = str(allocated_payload.get("relay_token", ""))
        relay_socket = relay_connect(arguments.host, arguments.port, relay_token, session_id, "publisher")
        session = host_handshake(relay_socket, identity)
        channel = AeadChannel(relay_socket, session)
        _emit(f"被控端已接入中继数据面: {arguments.host}:{arguments.port}")

        capture = DxcamCapture()
        capture.open()
        encoder = LiveEncoder(capture.width, capture.height, arguments.fps, arguments.bitrate)
        encoder.open()
        sender = VideoSender(channel, capture, encoder, arguments.fps, stop_event)
        sender.start()
        _emit(f"采集编码已启动: {capture.width}x{capture.height}@{arguments.fps}fps, 编码器 {encoder.context.name}")

        injector = Injector()
        while True:
            message = channel.recv()
            kind = str(message.get("kind", ""))
            if kind == "prepare":
                focused = False
                for _ in range(5):
                    if force_foreground(window):
                        focused = True
                        break
                    time.sleep(0.2)
                channel.send({"kind": "prepared", "ok": focused, "reason": "" if focused else "无法取得记事本前台焦点"})
                if not focused:
                    return _finish(work, {"char_count": 0}, "未验证", "无法取得记事本前台焦点", 3)
            elif kind == "key":
                received_ns = time.perf_counter_ns()
                char = str(message.get("char", ""))
                if len(char) != 1:
                    raise E2EFailure(f"非法字符载荷: {char!r}")
                started_ns, injected_ns = injector.type_char(char)
                typed += 1
                channel.send(
                    {
                        "kind": "ack",
                        "seq": message.get("seq"),
                        "char": char,
                        "recv_ns": received_ns,
                        "start_ns": started_ns,
                        "injected_ns": injected_ns,
                    }
                )
            elif kind == "finish":
                expected = str(message.get("expect", TARGET_TEXT_DEFAULT))
                focused = force_foreground(window)
                clipboard_text = ""
                file_text = ""
                clipboard_ok = False
                file_ok = False
                if focused:
                    injector.select_all()
                    injector.copy()
                    clipboard_ok = _wait_clipboard(expected, 5.0)
                    clipboard_text = read_clipboard_text() or ""
                    injector.save()
                    file_ok = _wait_file_text(target, expected, 5.0)
                try:
                    file_text = target.read_text(encoding="utf-8-sig")
                except OSError:
                    file_text = ""
                screenshot_ok = grab_window(window, work / "screenshot.png")
                stop_event.set()
                sender.join(timeout=5.0)
                proof = {
                    "kind": "proof",
                    "char_count": typed,
                    "focus_ok": focused,
                    "clipboard_ok": clipboard_ok,
                    "clipboard": clipboard_text,
                    "file_ok": file_ok,
                    "file": file_text,
                    "screenshot_ok": screenshot_ok,
                    "screenshot": str(work / "screenshot.png"),
                    "target": str(target),
                    "window": window,
                    "notepad_pid": process.pid,
                    "video": {
                        "frames_sent": sender.frames,
                        "bytes_sent": sender.bytes,
                        "keyframes": sender.keyframes,
                        "capture_dropped": sender.dropped,
                        "encoder": encoder.context.name if encoder.context else "",
                        "encode_ms_p50": _percentile(encoder.encode_ms, 0.50),
                        "encode_ms_p95": _percentile(encoder.encode_ms, 0.95),
                        "resolution": f"{capture.width}x{capture.height}",
                        "fps_target": arguments.fps,
                    },
                }
                channel.send(proof)
                capture.close()
                passed = typed == len(expected) and clipboard_ok and file_ok and screenshot_ok
                status = "已实测通过" if passed else "失败"
                reason = "" if passed else "逐字符/剪贴板/文件/截图断言未全部一致"
                return _finish(work, proof | {"typed": typed}, status, reason, 0 if passed else 1)
            elif kind == "abort":
                return _finish(work, {"char_count": typed}, "失败", str(message.get("reason", "控制端中止")), 1)
            else:
                raise E2EFailure(f"未知控制消息: {kind}")
    except (OSError, ValueError, RuntimeError, E2EFailure) as error:
        return _finish(work, {"char_count": typed}, "失败", f"{type(error).__name__}: {error}", 1)
    finally:
        stop_event.set()
        if sender is not None:
            sender.join(timeout=3.0)
        if relay_socket is not None:
            relay_socket.close()
        client.close()
        if original_clipboard is not None:
            write_clipboard_text(original_clipboard)
        with contextlib.suppress(OSError):
            process.terminate()
        if _LOGGER is not None:
            for existing in list(_LOGGER.handlers):
                existing.close()
                _LOGGER.removeHandler(existing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨机真实会话被控端代理")
    parser.add_argument("--work", required=True, help="证据目录")
    parser.add_argument("--host", default="127.0.0.1", help="信令与中继地址")
    parser.add_argument("--port", required=True, type=int, help="信令与中继端口")
    parser.add_argument("--session-id", required=True, help="会话号")
    parser.add_argument("--fps", default=15, type=int, help="采集编码帧率")
    parser.add_argument("--bitrate", default=6_000_000, type=int, help="目标码率 bps")
    parser.add_argument("--duration", default=300, type=int, help="预期会话时长秒")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
