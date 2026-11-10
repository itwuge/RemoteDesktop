"""中继服务三进程联调: 中继进程 + 发布端进程 + 订阅端进程.

覆盖: 注册/挑战/认证、presence 在线表、offer 路由时延、relay_allocate、
密文帧转发完整性与实测速率。所有凭据只在进程内存与管道中传递, 不落盘、不出现在命令行。
"""

from __future__ import annotations

import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from rdc.crypto.identity import DeviceIdentity
from rdc.relay.server import recv_frame, send_frame
from rdc.signaling.envelope import build_envelope

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "tests" / "relay_worker.py"
SESSION_ID = "9f1e2d3c-4b5a-4968-8776-655443322110"
LARGE_SESSION_ID = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"
ROUTE_SAMPLES = 20
FRAMES = 100
FRAME_BYTES = 2000
# 剪贴板 image ≤ 4 MiB, Base64 后约 5.34 MiB; 取 5.6 MB 验证数据面单帧上限
LARGE_FRAME_BYTES = 5_600_000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"中继端口 {port} 未就绪")


def _health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_health(port: int, predicate, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = _health(port)
        if predicate(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"健康检查未达到预期: {json.dumps(body, ensure_ascii=False)}")


def _write_config(path: Path, port: int, health_port: int) -> None:
    text = "\n".join(
        [
            "[server]",
            "bind = 127.0.0.1",
            f"port = {port}",
            "health_bind = 127.0.0.1",
            f"health_port = {health_port}",
            "max_connections = 16",
            "max_connections_per_ip = 8",
            "connect_rate_per_min = 0",
            "",
            "[relay]",
            f"endpoint = relay://127.0.0.1:{port}",
            "egress_mbps = 3",
            "overhead_ratio = 0.08",
            "max_sessions = 2",
            "min_session_kbps = 200",
            "degrade_profile = L0",
            "",
            "[logging]",
            "level = WARNING",
            "",
            "[tls]",
            "tls_enabled = false",
            "",
        ]
    )
    path.write_text(text, encoding="utf-8")


def _auth(sock: socket.socket, identity: DeviceIdentity) -> float:
    started = time.perf_counter()
    send_frame(
        sock,
        build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["desktop"],
                "protocol_version": 1,
            },
        ),
    )
    challenge = json.loads(recv_frame(sock).decode("utf-8"))
    assert challenge["type"] == "challenge"
    nonce = str(challenge["payload"]["nonce"])
    signature = identity.sign({"device_id": identity.device_id, "nonce": nonce})
    send_frame(sock, build_envelope(identity, "auth", {"device_id": identity.device_id, "signature": signature}))
    auth_ok = json.loads(recv_frame(sock).decode("utf-8"))
    assert auth_ok["type"] == "auth_ok"
    return (time.perf_counter() - started) * 1000


def _offer(identity: DeviceIdentity, peer: DeviceIdentity) -> dict:
    return build_envelope(
        identity,
        "offer",
        {
            "session_id": SESSION_ID,
            "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
            "media_caps": {"codecs": ["h264"]},
            "access_proof": "cHJvb2Y=",
            "from_device_id": identity.device_id,
            "to_device_id": peer.device_id,
        },
        session_id=SESSION_ID,
    )


def _start_worker(config: dict, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(ROOT),
        env=env,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(config))
    process.stdin.close()
    return process


def _worker_result(process: subprocess.Popen[str], timeout: float = 40.0) -> dict:
    stdout, stderr = process.communicate(timeout=timeout)
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"工作进程无输出: {stderr}"
    return json.loads(lines[-1])


def _percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


@pytest.mark.skipif(not WORKER.is_file(), reason="联调工作进程缺失")
def test_three_process_signaling_and_dataplane_roundtrip(tmp_path):
    port = _free_port()
    health_port = _free_port()
    config_path = tmp_path / "relay.conf"
    _write_config(config_path, port, health_port)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["RDC_LOG_DIR"] = str(tmp_path / "logs")

    relay = subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(ROOT),
        env=env,
    )
    alice = DeviceIdentity.generate()
    bob = DeviceIdentity.generate()
    alice_sock: socket.socket | None = None
    bob_sock: socket.socket | None = None
    subscriber: subprocess.Popen[str] | None = None
    publisher: subprocess.Popen[str] | None = None
    try:
        _wait_port(port)
        alice_sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        alice_sock.settimeout(5)
        bob_sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        bob_sock.settimeout(5)

        auth_ms = (_auth(alice_sock, alice) + _auth(bob_sock, bob)) / 2
        for sock, identity in ((alice_sock, alice), (bob_sock, bob)):
            send_frame(sock, build_envelope(identity, "presence", {"device_id": identity.device_id, "state": "online"}))
        online = _wait_health(health_port, lambda body: body["signaling"]["online_devices"] == 2)
        assert online["registry"]["devices"] == 2

        # 20 次 offer 路由, 记录本机回环时延
        latencies: list[float] = []
        for _ in range(ROUTE_SAMPLES):
            offer = _offer(alice, bob)
            started = time.perf_counter()
            send_frame(alice_sock, offer)
            routed = json.loads(recv_frame(bob_sock).decode("utf-8"))
            latencies.append((time.perf_counter() - started) * 1000)
            assert routed["id"] == offer["id"]
            assert routed["type"] == "offer"

        send_frame(
            alice_sock,
            build_envelope(
                alice,
                "relay_allocate",
                {"session_id": SESSION_ID, "requested_kbps": 2760, "est_duration_s": 60},
                session_id=SESSION_ID,
            ),
        )
        allocated = json.loads(recv_frame(alice_sock).decode("utf-8"))
        assert allocated["type"] == "relay_allocated"
        relay_token = str(allocated["payload"]["relay_token"])
        assert allocated["payload"]["budget_kbps"] == 1380

        base = {
            "host": "127.0.0.1",
            "port": port,
            "session_id": SESSION_ID,
            "relay_token": relay_token,
            "frames": FRAMES,
            "frame_bytes": FRAME_BYTES,
        }
        subscriber = _start_worker({**base, "role": "subscriber"}, env)
        _wait_health(health_port, lambda body: body["dataplane"]["active_subscribers"] == 1)
        publisher = _start_worker({**base, "role": "publisher", "interval_s": 0.02}, env)

        sent = _worker_result(publisher)
        received = _worker_result(subscriber)
        assert sent["frames_sent"] == FRAMES
        assert received["frames_received"] == FRAMES
        assert received["bytes_received"] == FRAMES * FRAME_BYTES
        assert received["sha256"] == sent["sha256"]

        final = _wait_health(health_port, lambda body: body["dataplane"]["total_frames_forwarded"] >= FRAMES)
        assert final["dataplane"]["total_bytes_forwarded"] == FRAMES * FRAME_BYTES
        assert final["signaling"]["active_sessions"] >= 1

        # 大帧: 剪贴板 image ≤ 4 MiB, Base64 后约 5.34 MiB; 验证数据面单帧上限已放宽到 6 MiB
        send_frame(
            alice_sock,
            build_envelope(
                alice,
                "relay_allocate",
                {"session_id": LARGE_SESSION_ID, "requested_kbps": 2760, "est_duration_s": 60},
                session_id=LARGE_SESSION_ID,
            ),
        )
        large_allocated = json.loads(recv_frame(alice_sock).decode("utf-8"))
        assert large_allocated["type"] == "relay_allocated"
        large_base = {
            "host": "127.0.0.1",
            "port": port,
            "session_id": LARGE_SESSION_ID,
            "relay_token": str(large_allocated["payload"]["relay_token"]),
        }
        _wait_health(health_port, lambda body: body["dataplane"]["active_subscribers"] == 0)
        subscriber = _start_worker(
            {**large_base, "role": "subscriber", "frames": 1, "frame_bytes": LARGE_FRAME_BYTES}, env
        )
        _wait_health(health_port, lambda body: body["dataplane"]["active_subscribers"] == 1)
        publisher = _start_worker(
            {**large_base, "role": "publisher", "frames": 1, "frame_bytes": LARGE_FRAME_BYTES}, env
        )
        large_sent = _worker_result(publisher, timeout=60.0)
        large_received = _worker_result(subscriber, timeout=60.0)
        assert large_received["frames_received"] == 1
        assert large_received["bytes_received"] == LARGE_FRAME_BYTES
        assert large_received["sha256"] == large_sent["sha256"]

        totals = _wait_health(health_port, lambda body: body["dataplane"]["total_frames_forwarded"] >= FRAMES + 1)
        assert totals["dataplane"]["total_bytes_forwarded"] == FRAMES * FRAME_BYTES + LARGE_FRAME_BYTES

        evidence = {
            "measured": True,
            "processes": 3,
            "auth_ms_avg": round(auth_ms, 3),
            "route_samples": ROUTE_SAMPLES,
            "route_p50_ms": round(statistics.median(latencies), 3),
            "route_p95_ms": round(_percentile(latencies, 0.95), 3),
            "route_max_ms": round(max(latencies), 3),
            "frames": FRAMES,
            "frame_bytes": FRAME_BYTES,
            "publisher_elapsed_s": sent["elapsed_s"],
            "publisher_kbps": sent["kbps"],
            "budget_kbps": allocated["payload"]["budget_kbps"],
            "large_frame_bytes": LARGE_FRAME_BYTES,
            "large_frame_elapsed_s": large_sent["elapsed_s"],
            "large_frame_kbps": large_sent["kbps"],
            "large_frame_sha256_match": large_received["sha256"] == large_sent["sha256"],
            "bytes_forwarded": totals["dataplane"]["total_bytes_forwarded"],
            "frames_forwarded": totals["dataplane"]["total_frames_forwarded"],
            "frames_dropped": totals["dataplane"]["total_frames_dropped"],
            "sha256_match": received["sha256"] == sent["sha256"],
            "online_devices": totals["signaling"]["online_devices"],
        }
        build_root = os.environ.get("RDC_BUILD_ROOT")
        if not build_root and Path(r"D:\RDC-Build").is_dir():
            build_root = r"D:\RDC-Build"
        if build_root:
            target = Path(build_root) / "evidence" / "poc" / "relay"
            target.mkdir(parents=True, exist_ok=True)
            (target / "signaling-integration.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        for process in (publisher, subscriber):
            if process is not None and process.poll() is None:
                process.kill()
        for sock in (alice_sock, bob_sock):
            if sock is not None:
                sock.close()
        relay.terminate()
        try:
            relay.wait(timeout=10)
        except subprocess.TimeoutExpired:
            relay.kill()
