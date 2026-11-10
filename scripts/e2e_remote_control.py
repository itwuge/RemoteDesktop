# ==========================================================================================
# 文件        : e2e_remote_control.py
# 所属目录    : scripts/
# 文件用途    : 端到端远程输入验证控制器: 起真实中继服务与被控端进程, 走真实 TCP 信令与中继数据面,
#               向被控端记事本注入固定字符串, 以剪贴板/文件/截图三方断言留证
# 架构定位    : 验收装置, 只调用产品信令/中继实现与本地 Win32 注入, 不修改产品代码
# 核心功能    : 依赖自检 → 起服务 → 双端认证 → offer/answer → 双端 relay_allocate →
#               数据面接入 → AEAD 握手 → 逐字符输入 → 三方取证 → 延迟分位 → 证据落盘
# 职责边界    : 本机环回, 不接公网; 不伪造通过; 焦点失败如实标「未验证」而非通过
# ==========================================================================================

"""端到端远程输入验证控制器.

用法:
    python scripts/e2e_remote_control.py [--evidence <目录>] [--text <固定字符串>]

证据目录默认: %RDC_BUILD_ROOT%\\evidence\\e2e\\remote-input\\<时间戳>\\
退出码: 0 已实测通过; 1 失败; 2 未验证(环境受阻).
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import smoke_env
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    controller_handshake,
    percentile_ms,
    relay_connect,
    tail_text,
    wait_json_file,
    write_json,
)
from rdc.crypto.identity import DeviceIdentity, b64encode

PROJECT_ROOT = smoke_env.PROJECT_ROOT
TARGET_TEXT = "RDC-REMOTE-INPUT-2026"
STATUS_PASS = "已实测通过"
STATUS_FAIL = "失败"
STATUS_UNVERIFIED = "未验证"


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _evidence_root() -> Path:
    """默认落在项目内 evidence/, 符合本轮验收“证据放在项目内”的要求."""
    configured = os.environ.get("RDC_EVIDENCE_ROOT")
    if configured:
        return Path(configured)
    return PROJECT_ROOT / "evidence"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _health(port: int) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        status = response.status
    finally:
        connection.close()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {"raw": body}
    if not isinstance(payload, dict):
        payload = {"raw": body}
    return status, payload


def _wait_health(port: int, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            status, payload = _health(port)
        except (OSError, http.client.HTTPException) as error:
            last = f"{type(error).__name__}: {error}"
            time.sleep(0.1)
            continue
        if status == 200:
            return payload
        last = f"HTTP {status}"
        time.sleep(0.1)
    raise E2EFailure(f"中继健康检查未在 {timeout_s}s 内就绪: {last}")


def _write_config(path: Path, relay_port: int, health_port: int) -> None:
    path.write_text(
        "[server]\n"
        "bind=127.0.0.1\n"
        f"port={relay_port}\n"
        "health_bind=127.0.0.1\n"
        f"health_port={health_port}\n"
        "max_connections=32\n"
        "max_connections_per_ip=16\n"
        "connect_rate_per_min=0\n"
        "\n"
        "[relay]\n"
        f"endpoint=relay://127.0.0.1:{relay_port}\n"
        "egress_mbps=3\n"
        "overhead_ratio=0.08\n"
        "max_sessions=4\n"
        "min_session_kbps=200\n"
        "degrade_profile=L0\n"
        "\n"
        "[logging]\n"
        "level=INFO\n",
        encoding="utf-8",
    )


def _range(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
    }


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run(arguments: argparse.Namespace) -> int:
    text: str = arguments.text
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = Path(arguments.evidence) if arguments.evidence else _evidence_root() / "e2e" / "remote-input" / stamp
    evidence = evidence.resolve()
    host_dir = evidence / "host"
    host_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    items: list[dict[str, Any]] = []
    reason = ""
    status = STATUS_FAIL
    session_id = f"e2e-{stamp}-{os.getpid()}"
    relay_port = _free_port()
    health_port = _free_port()
    config_path = evidence / "relay.conf"
    _write_config(config_path, relay_port, health_port)
    server_log = evidence / "relay.log"
    host_log = host_dir / "host.log"
    server_process: subprocess.Popen[bytes] | None = None
    host_process: subprocess.Popen[bytes] | None = None
    client: SignalingClient | None = None
    relay_socket: socket.socket | None = None
    server_handle = None
    host_handle = None
    evidence_payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session_id": session_id,
        "target_text": text,
        "transport": {
            "signaling": f"tcp://127.0.0.1:{relay_port}",
            "relay": f"tcp://127.0.0.1:{relay_port}",
            "health": f"http://127.0.0.1:{health_port}/healthz",
        },
        "caliber": "Windows 本机被控端 + 控制端独立进程 + 真实 TCP 信令/中继数据面 + AEAD 密文帧;"
        " 不含公网与跨端画面回传",
        "findings": [
            {
                "id": "E2E-GAP-01",
                "title": "订阅端在发布端配对前发帧会被数据面断链",
                "observed_at": "2026-11-10 09:32:19",
                "evidence": "evidence/smoke/20261110-093150/remote_input/relay.log",
                "repro": "subscriber 先接入并立即发送首帧, publisher 约 19ms 后接入; 数据面读帧时对端为空即 return",
                "expected": "未配对时丢弃该帧或等待配对完成, 不应关闭已通过令牌校验的连接",
                "handling": "验收装置改为等待双端配对后再发首帧; 实现是否符合契约要求请开发线确认",
            }
        ],
    }

    def add(name: str, passed: bool, detail: str, unverified: bool = False) -> None:
        items.append(
            {
                "name": name,
                "status": STATUS_UNVERIFIED if unverified else STATUS_PASS if passed else STATUS_FAIL,
                "detail": detail,
            }
        )

    try:
        env = os.environ.copy()
        parts = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts"), str(PROJECT_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env["PYTHONIOENCODING"] = "utf-8"

        server_handle = server_log.open("w", encoding="utf-8")
        server_process = subprocess.Popen(
            [sys.executable, "-m", "rdc.relay", "--config", str(config_path)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
        )
        health_before = _wait_health(health_port, 15.0)
        add("中继服务启动与健康检查", True, f"HTTP 200, budget_kbps={health_before.get('budget_kbps')}")

        host_handle = host_log.open("w", encoding="utf-8")
        host_process = subprocess.Popen(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "e2e_host_agent.py"),
                "--work",
                str(host_dir),
                "--host",
                "127.0.0.1",
                "--port",
                str(relay_port),
                "--session-id",
                session_id,
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=host_handle,
            stderr=subprocess.STDOUT,
        )
        ready = wait_json_file(host_dir / "ready.json", lambda value: bool(value.get("device_id")), 45.0)
        add("被控端记事本与身份就绪", True, f"窗口句柄 {ready.get('window')}, 设备 {str(ready.get('device_id'))[:12]}…")

        identity = DeviceIdentity.generate()
        client = SignalingClient("127.0.0.1", relay_port, timeout_s=30.0)
        client.connect()
        auth_payload = client.register_and_auth(identity, "e2e-controller")
        add("信令注册/挑战/认证", True, f"真实 TCP, relay_endpoint={auth_payload.get('relay_endpoint')}")

        client.send_envelope(
            "offer",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": str(ready["device_id"]),
                "candidates": [{"kind": "relay", "address": "127.0.0.1", "port": relay_port, "priority": 1}],
                "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
                "access_proof": b64encode(b"e2e-access-proof"),
            },
            session_id=session_id,
        )
        answer = client.recv_until("answer")
        add("offer/answer 路由", True, f"被控端应答会话 {answer['payload'].get('session_id')}")

        client.send_envelope(
            "session_start",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": str(ready["device_id"]),
                "transport_kind": "relay",
                "quality_profile": "L2",
            },
            session_id=session_id,
        )
        client.send_envelope(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 600, "est_duration_s": 120},
            session_id=session_id,
        )
        allocated = client.recv_until("relay_allocated")
        relay_token = str(allocated["payload"].get("relay_token", ""))
        budget = allocated["payload"].get("budget_kbps")
        add(
            "中继令牌签发(控制端)",
            True,
            f"令牌已签发, 预算 {budget} kbps, 档位 {allocated['payload'].get('quality_profile')}",
        )

        relay_socket = relay_connect("127.0.0.1", relay_port, relay_token, session_id, "subscriber")
        add("中继数据面接入", True, "控制端 subscriber 已就绪")
        wait_json_file(host_dir / "dataplane_ready.json", lambda value: value.get("ready") is True, 20.0)
        add("数据面双端配对", True, "publisher 与 subscriber 均已挂到同一会话")

        session = controller_handshake(relay_socket, identity)
        channel = AeadChannel(relay_socket, session)
        add("AEAD 握手与身份验签", True, "X25519 + ChaCha20-Poly1305, 被控端 Ed25519 签名已校验")

        channel.send({"kind": "prepare"})
        prepared = channel.recv()
        if not prepared.get("ok"):
            add("被控端记事本前台聚焦", False, str(prepared.get("reason", "焦点失败")), unverified=True)
            raise E2EFailure(f"被控端未取得前台焦点: {prepared.get('reason')}")
        add("被控端记事本前台聚焦", True, "SetForegroundWindow 成功")

        one_way: list[float] = []
        injection: list[float] = []
        round_trip: list[float] = []
        for index, char in enumerate(text):
            sent_ns = time.perf_counter_ns()
            channel.send({"kind": "key", "seq": index, "char": char, "sent_ns": sent_ns})
            ack = channel.recv()
            received_ns = time.perf_counter_ns()
            if ack.get("kind") != "ack" or ack.get("seq") != index:
                raise E2EFailure(f"字符 {index} 的应答不匹配: {ack}")
            one_way.append((int(ack["recv_ns"]) - sent_ns) / 1_000_000)
            injection.append((int(ack["injected_ns"]) - int(ack["recv_ns"])) / 1_000_000)
            round_trip.append((received_ns - sent_ns) / 1_000_000)
        add("远程输入逐字符送达", True, f"{len(text)} 个字符经中继密文帧送达并注入")

        channel.send({"kind": "finish", "expect": text})
        proof = channel.recv()
        deadline = time.monotonic() + 30.0
        while proof.get("kind") != "proof" and time.monotonic() < deadline:
            proof = channel.recv()
        if proof.get("kind") != "proof":
            raise E2EFailure("被控端未回传取证结果")
        host_result = wait_json_file(
            host_dir / "host_result.json",
            lambda value: value.get("status") in (STATUS_PASS, STATUS_FAIL, STATUS_UNVERIFIED),
            30.0,
        )
        evidence_payload["host_result"] = host_result

        clipboard_ok = proof.get("clipboard") == text
        add("剪贴板文本断言", clipboard_ok, f"收到的文本: {str(proof.get('clipboard'))!r}")
        file_text = str(proof.get("file", ""))
        host_file_ok = file_text == text
        add("被控端文件落盘断言", host_file_ok, f"被控端读到: {file_text!r}")
        try:
            disk_text = Path(str(proof.get("target", ""))).read_text(encoding="utf-8-sig")
        except OSError as error:
            disk_text = f"<读取失败 {error}>"
        independent_ok = disk_text == text
        add("控制端独立复读被控端文件", independent_ok, f"控制端读到: {disk_text!r}")

        screenshot = Path(str(proof.get("screenshot", "")))
        screenshot_ok = bool(proof.get("screenshot_ok")) and screenshot.is_file() and screenshot.stat().st_size > 1024
        size = screenshot.stat().st_size if screenshot.is_file() else 0
        add("被控端窗口截图取证", screenshot_ok, f"{screenshot.name}, {size} 字节")
        add(
            "逐字符计数一致",
            int(proof.get("char_count", 0)) == len(text),
            f"注入 {proof.get('char_count')} / 期望 {len(text)}",
        )

        latency = {
            "unit": "ms",
            "samples": len(one_way),
            "one_way": {
                "p50": percentile_ms(one_way, 0.50),
                "p95": percentile_ms(one_way, 0.95),
                **_range(one_way),
            },
            "injection": {
                "p50": percentile_ms(injection, 0.50),
                "p95": percentile_ms(injection, 0.95),
                **_range(injection),
            },
            "round_trip": {
                "p50": percentile_ms(round_trip, 0.50),
                "p95": percentile_ms(round_trip, 0.95),
                **_range(round_trip),
            },
            "one_way_samples": [round(value, 4) for value in one_way],
            "injection_samples": [round(value, 4) for value in injection],
            "round_trip_samples": [round(value, 4) for value in round_trip],
            "clock": "time.perf_counter_ns(), 控制端与被控端同机同一时钟源",
            "caliber": "本机环回 TCP + 真实中继转发; 非公网, 不含画面采集/编码/渲染",
        }
        evidence_payload["latency"] = latency
        evidence_payload["assertions"] = {
            "clipboard": clipboard_ok,
            "host_file": host_file_ok,
            "independent_file": independent_ok,
            "screenshot": screenshot_ok,
            "char_count": int(proof.get("char_count", 0)),
        }

        if relay_socket is not None:
            relay_socket.close()
            relay_socket = None
        client.close()
        reconnect = SignalingClient("127.0.0.1", relay_port, timeout_s=15.0)
        reconnect.connect()
        reconnect.register_and_auth(identity, "e2e-controller-reconnect")
        add("信令断线重连(真实TCP)", True, "断开后重新注册/挑战/认证成功")
        client = reconnect

        deadline = time.monotonic() + 10.0
        final_health: dict[str, Any] = {}
        while time.monotonic() < deadline:
            code, snapshot = _health(health_port)
            final_health = snapshot
            dataplane = snapshot.get("dataplane") or {}
            if code == 200 and int(dataplane.get("sessions", 1)) == 0:
                break
            time.sleep(0.1)
        final_code, final_health = _health(health_port)
        dataplane = final_health.get("dataplane") or {}
        sessions_left = int(dataplane.get("sessions", 1))
        add("数据面断开清理", sessions_left == 0, f"剩余数据面会话 {sessions_left}")
        frames = int(dataplane.get("total_frames_forwarded", dataplane.get("frames_forwarded", 0)))
        forwarded_bytes = int(dataplane.get("total_bytes_forwarded", dataplane.get("bytes_forwarded", 0)))
        add(
            "中继转发计数", frames >= 2 * len(text) + 2 and forwarded_bytes > 0, f"{frames} 帧 / {forwarded_bytes} 字节"
        )
        evidence_payload["transport"]["data_frames_forwarded"] = frames
        evidence_payload["transport"]["data_bytes_forwarded"] = forwarded_bytes
        evidence_payload["health_after"] = final_health
        add("中继健康快照", final_code == 200, f"HTTP {final_code}")

        if all(item["status"] == STATUS_PASS for item in items):
            status = STATUS_PASS
        reason = ""
    except E2EFailure as error:
        reason = str(error)
        status = STATUS_UNVERIFIED if any(item["status"] == STATUS_UNVERIFIED for item in items) else STATUS_FAIL
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        reason = f"{type(error).__name__}: {error}"
        status = STATUS_FAIL
    finally:
        if relay_socket is not None:
            relay_socket.close()
        if client is not None:
            client.close()
        _terminate(host_process)
        _terminate(server_process)
        if host_handle is not None:
            host_handle.close()
        if server_handle is not None:
            server_handle.close()

    elapsed = round(time.perf_counter() - started, 2)
    evidence_payload["status"] = status
    evidence_payload["reason"] = reason
    evidence_payload["elapsed_s"] = elapsed
    evidence_payload["items"] = items
    evidence_payload["evidence"] = {
        "root": str(evidence),
        "screenshot": str(host_dir / "screenshot.png"),
        "target": str(host_dir / "rdc-remote-input.txt"),
        "host_result": str(host_dir / "host_result.json"),
        "host_log": str(host_log),
        "relay_log": str(server_log),
    }
    evidence_payload["relay_log_tail"] = tail_text(server_log, 2000)
    evidence_payload["host_log_tail"] = tail_text(host_log, 2000)
    write_json(evidence / "e2e.json", evidence_payload)

    _emit("=" * 92)
    _emit(f"端到端远程输入验证  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  用时 {elapsed}s")
    _emit("=" * 92)
    for index, item in enumerate(items, start=1):
        _emit(f"{index:>2}  {item['status']:<6} {item['name']}: {item['detail']}")
    latency_payload = evidence_payload.get("latency", {})
    if latency_payload:
        _emit(
            "延迟(本机环回): "
            f"单向 p50 {latency_payload['one_way']['p50']}ms / p95 {latency_payload['one_way']['p95']}ms; "
            f"注入 p50 {latency_payload['injection']['p50']}ms / p95 {latency_payload['injection']['p95']}ms; "
            f"RTT p50 {latency_payload['round_trip']['p50']}ms / p95 {latency_payload['round_trip']['p95']}ms"
        )
    _emit(f"结论: {status}{(' - ' + reason) if reason else ''}")
    _emit(f"口径: {evidence_payload['caliber']}")
    _emit(f"证据: {evidence}")
    if status == STATUS_PASS:
        return 0
    return 2 if status == STATUS_UNVERIFIED else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端远程输入验证控制器")
    parser.add_argument("--evidence", default="", help="证据目录; 默认落在构建根")
    parser.add_argument("--text", default=TARGET_TEXT, help="要注入的固定字符串")
    arguments = parser.parse_args(argv)
    with contextlib.suppress(KeyboardInterrupt):
        return _run(arguments)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
