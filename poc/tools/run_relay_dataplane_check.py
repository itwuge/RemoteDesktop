# ==========================================================================================
# 文件        : run_relay_dataplane_check.py
# 所属目录    : poc/tools/
# 文件用途    : 中继数据面双进程验证: 独立进程中转发密文, 校验逐字节一致与预算限速
# 架构定位    : PoC 测试装置, 只在本机回环验证数据面行为, 不进入发布路径
# 核心功能    : 起中继子进程; 走真实信令注册/认证/relay_allocate 取令牌; 双进程收发; 输出证据
# 职责边界    : 不采集屏幕、不编码、不解密、不测画质; 不是画质通道验收
# 运行环境    : Python 3.12; Windows 或 Linux 均可, 只用回环地址
# 依赖关系    : rdc.relay、rdc.signaling、rdc.crypto.identity
# ==========================================================================================

"""中继数据面双进程验证执行器.

用法:
    $env:PYTHONPATH = 'src'; python -m poc.tools.run_relay_dataplane_check

验证内容:
1. 两个独立客户端进程经中继转发固定长度密文帧, 订阅端逐字节比对, 统计一致率.
2. 发布端以约 2 倍预算速率发送, 校验实际转发吞吐不超过预算, 且中继记录丢帧.
口径: 本机回环, 只证明数据面转发正确与限速生效; 不代表真实链路, 也不构成画质验收.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope
from rdc.relay.server import recv_frame, send_frame

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"


# ---------------------------------------------------------------------------- 帧与载荷


def _payload(index: int, size: int) -> bytes:
    """确定性载荷: 每个序号生成固定内容, 收发两端各自计算后比对."""
    block = hashlib.blake2b(index.to_bytes(4, "big"), digest_size=32).digest()
    return (block * (size // len(block) + 1))[:size]


def _send_raw(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(struct.pack(">I", len(payload)) + payload)


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"中继端口 {port} 未在 {timeout} 秒内就绪")


# ---------------------------------------------------------------------------- 信令取令牌


def _request_relay_token(port: int, session_id: str, requested_kbps: int) -> dict[str, Any]:
    """走真实信令流程: register → challenge → auth → relay_allocate, 返回授予信息."""
    identity = DeviceIdentity.generate()
    connection = socket.create_connection(("127.0.0.1", port), timeout=10.0)
    connection.settimeout(10.0)
    try:
        send_frame(connection, build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["relay"],
                "protocol_version": 1,
                "display_name": "dataplane-check",
            },
        ))
        challenge = json.loads(recv_frame(connection).decode("utf-8"))
        if challenge.get("type") != "challenge":
            raise RuntimeError(f"未收到 challenge: {challenge.get('type')}")

        nonce = challenge["payload"]["nonce"]
        send_frame(connection, build_envelope(
            identity,
            "auth",
            {
                "device_id": identity.device_id,
                "signature": identity.sign({"device_id": identity.device_id, "nonce": nonce}),
            },
        ))
        auth_ok = json.loads(recv_frame(connection).decode("utf-8"))
        if auth_ok.get("type") != "auth_ok":
            raise RuntimeError(f"认证失败: {auth_ok.get('payload')}")

        # 预算只签发给会话参与设备: 单机自检先发一条发给自己的 offer 建立会话并读走转发帧
        send_frame(connection, build_envelope(
            identity,
            "offer",
            {
                "session_id": session_id,
                "candidates": [{"kind": "host", "address": "127.0.0.1", "port": 5000, "priority": 1}],
                "media_caps": {"codecs": ["h264"]},
                "access_proof": "cHJvb2Y=",
                "from_device_id": identity.device_id,
                "to_device_id": identity.device_id,
            },
            session_id=session_id,
        ))
        if json.loads(recv_frame(connection).decode("utf-8")).get("type") != "offer":
            raise RuntimeError("自建会话失败: 未收到 offer 回显")

        # 契约缺口沿用 202611-001: v1 无 from_device_id 字段, 服务端靠它定位公钥验签
        send_frame(connection, build_envelope(
            identity,
            "relay_allocate",
            {
                "session_id": session_id,
                "requested_kbps": requested_kbps,
                "est_duration_s": 60,
                "from_device_id": identity.device_id,
            },
            session_id=session_id,
        ))
        allocated = json.loads(recv_frame(connection).decode("utf-8"))
        if allocated.get("type") != "relay_allocated":
            raise RuntimeError(f"预算申请失败: {allocated.get('payload')}")
        return allocated["payload"]
    finally:
        connection.close()


# ---------------------------------------------------------------------------- 客户端进程


def run_peer(args: argparse.Namespace) -> int:
    """子进程入口: 一个角色连中继收发数据面帧."""
    connection = socket.create_connection(("127.0.0.1", args.port), timeout=15.0)
    connection.settimeout(args.timeout)
    send_frame(connection, {
        "plane": "data",
        "v": 1,
        "type": "relay_hello",
        "relay_token": args.token,
        "session_id": args.session_id,
        "role": args.role,
    })
    ready = json.loads(recv_frame(connection).decode("utf-8"))
    if ready.get("type") != "relay_ready":
        _write_result(args.result, {"ok": False, "stage": "handshake", "detail": ready})
        return 2

    if args.role == "publisher":
        result = _publish(connection, args)
    else:
        result = _subscribe(connection, args)
    connection.close()
    _write_result(args.result, result)
    return 0 if result.get("ok") else 3


def _publish(connection: socket.socket, args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    sent_bytes = 0
    for index in range(args.count):
        frame = _payload(index, args.size)
        _send_raw(connection, frame)
        sent_bytes += len(frame)
        if args.target_bps > 0:
            expected = sent_bytes / args.target_bps
            delay = expected - (time.perf_counter() - started)
            if delay > 0:
                time.sleep(delay)
    elapsed = time.perf_counter() - started
    # 留时间让中继把最后几帧转发完再断开
    time.sleep(0.3)
    return {
        "ok": True,
        "role": "publisher",
        "sent_frames": args.count,
        "sent_bytes": sent_bytes,
        "elapsed_s": round(elapsed, 3),
        "target_bps": args.target_bps,
    }


def _subscribe(connection: socket.socket, args: argparse.Namespace) -> dict[str, Any]:
    received = 0
    mismatched = 0
    received_bytes = 0
    started = time.perf_counter()
    last_at = started
    while received < args.count:
        try:
            frame = recv_frame(connection)
        except (socket.timeout, TimeoutError):
            break
        except (OSError, ValueError):
            break
        if frame is None:
            break
        now = time.perf_counter()
        if args.verify and frame != _payload(received, args.size):
            mismatched += 1
        received += 1
        received_bytes += len(frame)
        last_at = now
    elapsed = max(last_at - started, 1e-6)
    return {
        "ok": mismatched == 0,
        "role": "subscriber",
        "expected_frames": args.count,
        "received_frames": received,
        "mismatched_frames": mismatched,
        "received_bytes": received_bytes,
        "elapsed_s": round(elapsed, 3),
        "effective_kbps": round(received_bytes * 8 / elapsed / 1000, 1),
    }


def _write_result(path: str, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_result(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- 编排


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _health(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _spawn_peer(
    label: str, args: list[str], timeout: float, log_path: Path
) -> tuple[subprocess.Popen[str], Any]:
    """起一个客户端子进程, 输出落日志文件.

    不能用 PIPE: 父进程在整轮结束前不读管道, Windows 管道缓冲约 4KB,
    中继日志写满后写线程阻塞并持有日志锁, 会让全部信令线程卡死.
    """
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "poc.tools.run_relay_dataplane_check", "--role", label, *args],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, handle


def _tail(path: Path, limit: int = 2000) -> str:
    """读子进程日志尾部, 供失败信息定位."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    return env


def _run_round(
    temp_dir: Path,
    label: str,
    port: int,
    requested_kbps: int,
    count: int,
    size: int,
    target_bps: int,
) -> dict[str, Any]:
    session_id = f"{label}-{int(time.time() * 1000)}"
    publisher_grant = _request_relay_token(port, session_id, requested_kbps)
    subscriber_grant = _request_relay_token(port, session_id, requested_kbps)
    token = publisher_grant["relay_token"]
    budget_kbps = int(publisher_grant["budget_kbps"])

    def peer_args(role: str, token_value: str, result: Path) -> list[str]:
        return [
            "--port", str(port),
            "--session-id", session_id,
            "--token", token_value,
            "--count", str(count),
            "--size", str(size),
            "--target-bps", str(target_bps),
            "--timeout", "10",
            "--verify", "1" if target_bps == 0 else "0",
            "--result", str(result),
        ]

    subscriber_result = temp_dir / f"{label}-subscriber.json"
    publisher_result = temp_dir / f"{label}-publisher.json"
    subscriber_log = temp_dir / f"{label}-subscriber.log"
    publisher_log = temp_dir / f"{label}-publisher.log"
    subscriber, subscriber_handle = _spawn_peer(
        "subscriber", peer_args("subscriber", subscriber_grant["relay_token"], subscriber_result), 60, subscriber_log
    )
    time.sleep(0.3)
    publisher, publisher_handle = _spawn_peer(
        "publisher", peer_args("publisher", token, publisher_result), 60, publisher_log
    )

    try:
        publisher.communicate(timeout=60)
        subscriber.communicate(timeout=60)
    finally:
        publisher_handle.close()
        subscriber_handle.close()
    if publisher.returncode != 0:
        raise RuntimeError(f"publisher 子进程失败 {publisher.returncode}: {_tail(publisher_log)}")
    if subscriber.returncode != 0:
        raise RuntimeError(f"subscriber 子进程失败 {subscriber.returncode}: {_tail(subscriber_log)}")

    return {
        "label": label,
        "session_id": session_id,
        "budget_kbps": budget_kbps,
        "publisher": _read_result(str(publisher_result)),
        "subscriber": _read_result(str(subscriber_result)),
    }


def run() -> dict[str, Any]:
    """执行数据面双进程验证, 返回证据摘要."""
    root = os.environ.get("RDC_BUILD_ROOT")
    if not root:
        raise RuntimeError("未设置 RDC_BUILD_ROOT, 不得写入系统盘或项目目录")
    temp_dir = Path(root) / "temp" / "relay-dataplane"
    evidence_dir = Path(root) / "evidence" / "poc" / "relay"
    temp_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    port, health_port = _free_port(), _free_port()
    config_path = temp_dir / "relay.conf"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                "bind = 127.0.0.1",
                f"port = {port}",
                "health_bind = 127.0.0.1",
                f"health_port = {health_port}",
                "",
                "[relay]",
                f"endpoint = relay://127.0.0.1:{port}",
                "egress_mbps = 3",
                "overhead_ratio = 0.08",
                "max_sessions = 2",
                "degrade_profile = L0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    relay_log = temp_dir / "relay-process.log"
    relay_handle = relay_log.open("w", encoding="utf-8")
    relay = subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config_path)],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdout=relay_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )

    result: dict[str, Any] = {
        "work_item": "relay-dataplane",
        "topology": "本机回环, 中继与两个客户端各为独立进程",
        "scope": "只验证密文转发正确性与预算限速; 不是画质通道验收, 不代表真实链路",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checks": {},
        "health_before": {},
        "health_after": {},
    }
    try:
        _wait_port(port)
        result["health_before"] = _health(health_port)

        # 1) 转发正确性: 32 帧 × 2 KiB, 不限速发送, 逐字节比对
        correct = _run_round(temp_dir, "forward", port, 1380, count=32, size=2048, target_bps=0)
        result["checks"]["forward_byte_exact"] = {
            "passed": (
                correct["subscriber"]["received_frames"] == 32
                and correct["subscriber"]["mismatched_frames"] == 0
            ),
            "detail": correct,
        }

        # 2) 预算限速: 200 kbps 预算(信令侧最小授予), 发布端按 2 倍预算发送, 校验吞吐不超预算
        throttle = _run_round(temp_dir, "throttle", port, 200, count=8, size=8192, target_bps=50000)
        subscriber = throttle["subscriber"]
        budget_bps = throttle["budget_kbps"] * 1000
        effective_bps = subscriber["effective_kbps"] * 1000
        result["checks"]["budget_throttle"] = {
            "passed": effective_bps <= budget_bps * 1.25 and subscriber["received_frames"] < 8,
            "detail": throttle,
            "note": "200 kbps 为压测取值, 只为验证限速生效, 不是线上档位",
        }

        time.sleep(0.5)
        result["health_after"] = _health(health_port)
        dataplane = result["health_after"].get("dataplane", {})
        result["checks"]["dropped_frames_recorded"] = {
            "passed": int(dataplane.get("total_frames_dropped", 0)) > 0,
            "detail": dataplane,
        }
    finally:
        relay.terminate()
        try:
            relay.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            relay.kill()
            relay.communicate(timeout=5)
        relay_handle.close()

    result["all_passed"] = all(item["passed"] for item in result["checks"].values())
    output = evidence_dir / "dataplane-check.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中继数据面双进程验证")
    parser.add_argument("--role", choices=("publisher", "subscriber"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--session-id")
    parser.add_argument("--token")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--target-bps", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--verify", type=int, default=0, help="1 表示逐字节比对载荷")
    parser.add_argument("--result")
    args = parser.parse_args(argv)

    if args.role:
        return run_peer(args)

    result = run()
    for name, check in result["checks"].items():
        print(f"{'通过' if check['passed'] else '失败'}  {name}")
    print(f"证据: {Path(os.environ['RDC_BUILD_ROOT']) / 'evidence' / 'poc' / 'relay' / 'dataplane-check.json'}")
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
