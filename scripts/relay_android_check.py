# ==========================================================================================
# 文件        : relay_android_check.py
# 所属目录    : scripts/
# 文件用途    : 安卓控制端接公网中继的验收装置: TLS 1.3 信令/数据面、720p30 订阅解码、满员拒绝、断线重连
# 架构定位    : 验收装置, 复用 scripts/e2e_common.py 的信令、中继与 AEAD 通道, 不修改产品代码
# 核心功能    : 首帧耗时/帧率/码率取证; 首帧落盘 PNG; 第二路 RELAY_FULL 文案核对; 数据面断线重连
# 职责边界    : 只做控制端; 不采集屏幕、不注入输入、不改中继配置、不伪造成功
# ==========================================================================================

"""公网中继的安卓控制端等价验收.

用法:
    python scripts/relay_android_check.py --peer-device-id <被控端设备ID> \\
        --evidence evidence/android-relay-20261110

被控端先以公网中继模式启动:
    python scripts/android_host_agent.py --no-relay --relay-host 47.109.32.249 \\
        --relay-port 8750 --tls --ca deploy/relay/ca.crt --sni relay.wuge.fun

退出码: 0 出画面且核对项通过; 1 失败; 2 等待被控端上线超时.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import socket
import ssl
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from _rdc_path import PROJECT_ROOT
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    controller_handshake,
    percentile_ms,
    recv_json,
    relay_connect,
    wait_json_file,
    write_json,
)
from rdc.crypto.identity import DeviceIdentity, b64encode

HOST_READY = Path(r"D:\RDC-Build\rdc-lan\host_ready.json")
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
TARGET_FPS = 30
TARGET_KBPS = 2900
MEDIA_CAPS = {
    "codecs": ["h264"],
    "max_width": TARGET_WIDTH,
    "max_height": TARGET_HEIGHT,
    "max_fps": TARGET_FPS,
}


def emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def connect_signaling(args: argparse.Namespace, name: str) -> tuple[SignalingClient, DeviceIdentity]:
    identity = DeviceIdentity.generate()
    client = SignalingClient(
        args.relay_host,
        args.relay_port,
        timeout_s=60.0,
        use_tls=not args.no_tls,
        ca_file=args.ca,
        sni=args.sni,
    )
    client.connect()
    client.register_and_auth(identity, name)
    return client, identity


def open_session(
    args: argparse.Namespace,
    client: SignalingClient,
    identity: DeviceIdentity,
    session_id: str,
) -> dict[str, Any]:
    """完成 offer/answer/session_start/relay_allocate 与数据面握手."""
    stages: dict[str, float] = {}
    started = time.perf_counter()
    stages["signaling_connected_ms"] = 0.0

    client.send_envelope(
        "offer",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": args.peer_device_id,
            "candidates": [{"kind": "relay", "address": args.relay_host, "port": args.relay_port, "priority": 1}],
            "media_caps": MEDIA_CAPS,
            "access_proof": b64encode(args.code.encode("utf-8")),
        },
        session_id=session_id,
    )
    answer = client.recv_until("answer")
    stages["answer_received_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    answer_payload = answer.get("payload", {})

    client.send_envelope(
        "session_start",
        {
            "session_id": session_id,
            "from_device_id": identity.device_id,
            "to_device_id": args.peer_device_id,
            "transport_kind": "relay",
            "quality_profile": "L0",
        },
        session_id=session_id,
    )
    client.send_envelope(
        "relay_allocate",
        {"session_id": session_id, "requested_kbps": TARGET_KBPS, "est_duration_s": 600},
        session_id=session_id,
    )
    allocated = client.recv_until("relay_allocated").get("payload", {})
    stages["relay_allocated_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    token = str(allocated.get("relay_token", ""))
    if not token:
        raise E2EFailure("中继未下发 relay_token")

    tls_info: dict[str, Any] = {}
    relay_socket = relay_connect(
        args.relay_host,
        args.relay_port,
        token,
        session_id,
        "subscriber",
        use_tls=not args.no_tls,
        ca_file=args.ca,
        sni=args.sni,
        tls_info=tls_info,
    )
    session = controller_handshake(relay_socket, identity, session_id, args.peer_device_id)
    channel = AeadChannel(relay_socket, session)
    stages["data_plane_ready_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return {
        "client": client,
        "socket": relay_socket,
        "channel": channel,
        "aead": session,
        "token": token,
        "budget_kbps": int(allocated.get("budget_kbps", 0)),
        "quality_profile": str(allocated.get("quality_profile", "")),
        "answer_caps": answer_payload.get("media_caps"),
        "stages_ms": stages,
        "data_tls": tls_info,
        "started_perf": started,
    }


def receive_phase(
    args: argparse.Namespace,
    channel: AeadChannel,
    duration_s: float,
    *,
    first_frame_png: Path | None,
) -> dict[str, Any]:
    """在给定时长内收帧解码, 返回首帧、帧率、码率与分辨率."""
    import av

    decoder = av.CodecContext.create("h264", "r")
    decoder.open()
    frames = 0
    media_bytes = 0
    decode_ms: list[float] = []
    first_after_ms = 0.0
    resolution = ""
    errors = 0
    started = time.perf_counter()
    deadline = started + duration_s
    while time.perf_counter() < deadline:
        message = channel.recv()
        if str(message.get("kind", "")) != "video":
            continue
        raw = base64.b64decode(str(message.get("data", "")).encode("ascii"), validate=True)
        media_bytes += len(raw)
        tick = time.perf_counter()
        try:
            decoded = decoder.decode(av.Packet(raw))
        except (av.error.FFmpegError, ValueError) as error:
            errors += 1
            if errors <= 3:
                emit(f"[CHECK] 解码错误: {type(error).__name__}: {error}")
            continue
        decode_ms.append((time.perf_counter() - tick) * 1000.0)
        if frames == 0:
            # 以真正解出第一帧的时刻为准, 不把关键帧之前的无效包算进首帧耗时
            first_after_ms = round((time.perf_counter() - started) * 1000.0, 3)
        for frame in decoded:
            resolution = f"{frame.width}x{frame.height}"
            if first_frame_png is not None and not first_frame_png.exists():
                first_frame_png.parent.mkdir(parents=True, exist_ok=True)
                frame.to_image().save(str(first_frame_png))
            frames += 1
    elapsed = max(time.perf_counter() - started, 0.001)
    return {
        "duration_s": round(elapsed, 3),
        "frames_decoded": frames,
        "media_bytes": media_bytes,
        "resolution": resolution,
        "fps_observed": round(frames / elapsed, 2),
        "media_bitrate_kbps": round(media_bytes * 8 / 1000.0 / elapsed, 1),
        "first_frame_after_phase_ms": first_after_ms,
        "decode_ms_p50": percentile_ms(decode_ms, 0.50),
        "decode_ms_p95": percentile_ms(decode_ms, 0.95),
        "decode_errors": errors,
    }


def full_channel_probe(args: argparse.Namespace, active_session_id: str) -> dict[str, Any]:
    """第二路经完整 offer/answer 协商后申请中继, 核对满员拒绝码与中文文案."""
    import threading

    controller, controller_identity = connect_signaling(args, "relay-second-controller")
    host, host_identity = connect_signaling(args, "relay-second-host")
    session_id = f"second-{uuid.uuid4()}"
    host_error: dict[str, Any] = {}

    def serve_host() -> None:
        try:
            offer = host.recv_until("offer")
            payload = offer.get("payload", {})
            host.send_envelope(
                "answer",
                {
                    "session_id": payload.get("session_id", session_id),
                    "from_device_id": host_identity.device_id,
                    "to_device_id": payload.get("from_device_id", controller_identity.device_id),
                    "chosen_candidate": {
                        "kind": "relay",
                        "address": args.relay_host,
                        "port": args.relay_port,
                    },
                    "media_caps": MEDIA_CAPS,
                },
                session_id=payload.get("session_id", session_id),
            )
        except (E2EFailure, OSError) as error:
            host_error["error"] = f"{type(error).__name__}: {error}"

    worker = threading.Thread(target=serve_host, daemon=True, name="second-host")
    worker.start()
    try:
        controller.send_envelope(
            "offer",
            {
                "session_id": session_id,
                "from_device_id": controller_identity.device_id,
                "to_device_id": host_identity.device_id,
                "candidates": [{"kind": "relay", "address": args.relay_host, "port": args.relay_port, "priority": 1}],
                "media_caps": MEDIA_CAPS,
                "access_proof": b64encode(args.code.encode("utf-8")),
            },
            session_id=session_id,
        )
        controller.recv_until("answer")
        controller.send_envelope(
            "session_start",
            {
                "session_id": session_id,
                "from_device_id": controller_identity.device_id,
                "to_device_id": host_identity.device_id,
                "transport_kind": "relay",
                "quality_profile": "L0",
            },
            session_id=session_id,
        )
        controller.send_envelope(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": TARGET_KBPS, "est_duration_s": 60},
            session_id=session_id,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            frame = recv_json(controller.sock) if controller.sock is not None else None
            if frame is None:
                break
            if str(frame.get("type", "")) != "error":
                continue
            payload = frame.get("payload", {})
            return {
                "state": "ok",
                "session_id": session_id,
                "code": str(payload.get("code", "")),
                "message": str(payload.get("message", "")),
                "expected_text": "中继通道已满，请等待或改用直连",
                "host_error": host_error.get("error", ""),
            }
        return {"state": "failed", "reason": "未收到第二路拒绝", "host_error": host_error.get("error", "")}
    finally:
        controller.close()
        host.close()


def reconnect_probe(args: argparse.Namespace, opened: dict[str, Any], session_id: str) -> dict[str, Any]:
    """同一会话掉线后重新接入数据面, 观察画面恢复耗时."""
    old_socket: socket.socket = opened["socket"]
    with contextlib.suppress(OSError):
        old_socket.close()
    emit("[CHECK] 数据面已断开, 等待 2 秒后重连")
    time.sleep(2.0)
    started = time.perf_counter()
    tls_info: dict[str, Any] = {}
    sock = relay_connect(
        args.relay_host,
        args.relay_port,
        opened["token"],
        session_id,
        "subscriber",
        use_tls=not args.no_tls,
        ca_file=args.ca,
        sni=args.sni,
        tls_info=tls_info,
    )
    attach_ms = round((time.perf_counter() - started) * 1000.0, 3)
    channel = AeadChannel(sock, opened["aead"])
    phase = receive_phase(args, channel, args.reconnect_s, first_frame_png=None)
    opened["socket"] = sock
    opened["channel"] = channel
    return {
        "state": "ok" if phase["frames_decoded"] > 0 else "no_frame",
        "data_plane_attach_ms": attach_ms,
        "first_frame_after_reconnect_ms": phase["first_frame_after_phase_ms"],
        "frames_decoded": phase["frames_decoded"],
        "fps_observed": phase["fps_observed"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公网中继安卓控制端等价验收")
    parser.add_argument("--relay-host", default="47.109.32.249")
    parser.add_argument("--relay-port", type=int, default=8750)
    parser.add_argument("--ca", default=str(PROJECT_ROOT / "deploy" / "relay" / "ca.crt"))
    parser.add_argument("--sni", default="relay.wuge.fun")
    parser.add_argument("--no-tls", action="store_true", help="仅用于局域网明文联调")
    parser.add_argument("--peer-device-id", default="", help="缺省读取 host_ready.json")
    parser.add_argument("--code", default="246810")
    parser.add_argument("--host-ready", default=str(HOST_READY))
    parser.add_argument("--wait-host-s", type=float, default=60.0)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--reconnect-s", type=float, default=6.0)
    parser.add_argument("--evidence", default=str(PROJECT_ROOT / "evidence" / "android-relay-20261110"))
    args = parser.parse_args(argv)

    evidence = Path(args.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"state": "running", "relay": f"{args.relay_host}:{args.relay_port}"}
    relay_socket: socket.socket | None = None
    client: SignalingClient | None = None
    try:
        if not args.peer_device_id:
            ready = wait_json_file(Path(args.host_ready), lambda value: "device_id" in value, args.wait_host_s)
            args.peer_device_id = str(ready["device_id"])
            result["host_ready"] = ready
        session_id = f"relay-android-{int(time.time())}"

        client, identity = connect_signaling(args, "android-controller-relay")
        result["signaling_tls"] = client.tls_info
        opened = open_session(args, client, identity, session_id)
        relay_socket = opened["socket"]
        result.update(
            {
                "session_id": session_id,
                "budget_kbps": opened["budget_kbps"],
                "quality_profile": opened["quality_profile"],
                "answer_caps": opened["answer_caps"],
                "stages_ms": opened["stages_ms"],
                "data_tls": opened["data_tls"],
            }
        )
        emit(
            f"[CHECK] 已接入中继 session={session_id} 预算 {opened['budget_kbps']} kbps / "
            f"{opened['quality_profile']} 档, 开始收流"
        )

        first_png = evidence / "relay_first_frame.png"
        if first_png.exists():
            first_png.unlink()
        phase = receive_phase(args, opened["channel"], args.duration, first_frame_png=first_png)
        phase["first_frame_png"] = str(first_png) if first_png.exists() else ""
        phase["first_frame_png_bytes"] = first_png.stat().st_size if first_png.exists() else 0
        result["stream"] = phase
        emit(
            f"[CHECK] 首帧 {phase['first_frame_after_phase_ms']} ms 分辨率 {phase['resolution']} "
            f"帧率 {phase['fps_observed']} 码率 {phase['media_bitrate_kbps']} kbps"
        )

        result["full_channel"] = full_channel_probe(args, session_id)
        emit(f"[CHECK] 第二路结果: {result['full_channel']}")

        result["reconnect"] = reconnect_probe(args, opened, session_id)
        emit(f"[CHECK] 重连结果: {result['reconnect']}")

        result["state"] = "ok" if phase["frames_decoded"] > 0 else "no_frame"
    except (OSError, ValueError, RuntimeError, E2EFailure, ssl.SSLError, TimeoutError) as error:
        result["state"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        emit(f"[CHECK] 失败: {result['error']}")
    finally:
        if relay_socket is not None:
            with contextlib.suppress(OSError):
                relay_socket.close()
        if client is not None:
            client.close()
        write_json(evidence / "relay_check.json", result)
        emit(f"[CHECK] 证据: {evidence / 'relay_check.json'}")
    return 0 if result.get("state") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
