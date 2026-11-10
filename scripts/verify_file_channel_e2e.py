# ==========================================================================================
# 文件        : verify_file_channel_e2e.py
# 所属目录    : scripts/
# 文件用途    : 用本机真实 TCP + RelayDataPlane 跑三个独立进程的文件传输端到端实测
# 架构定位    : 验收脚本, 不进入产品运行路径
# 核心功能    : relay/sender/receiver 三进程编排; 记录大小、耗时、速率、摘要与落盘证据
# 职责边界    : 不修改产品代码; 不写用户目录; 不替代单测
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.session.channel; rdc.files.receiver; rdc.relay.dataplane
# ==========================================================================================

"""文件通道跨进程实测.

用法::

    python scripts/verify_file_channel_e2e.py
    python scripts/verify_file_channel_e2e.py --file <源文件> --dest <接收目录>

默认生成 2 MiB 测试文件, 产物写入 ``evidence/file-channel-<时间戳>/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SESSION_ID = "session-file-e2e"
TOKEN = "token-file-e2e"
BUDGET_KBPS = 100_000
DEFAULT_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 120


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prepare_path() -> None:
    """让子进程无需外部 PYTHONPATH 也能 import rdc."""
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- relay 进程
def run_relay(port: int, stats_path: Path, seconds: int) -> int:
    _prepare_path()
    from rdc.relay.dataplane import RelayDataPlane, RelayGrant

    grant = RelayGrant(session_id=SESSION_ID, budget_kbps=BUDGET_KBPS, expires_at=time.time() + seconds)
    plane = RelayDataPlane(lambda token: grant if token == TOKEN else None, max_sessions=4)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(8)
    server.settimeout(1.0)
    stop = threading.Event()
    stats_lock = threading.Lock()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=_serve_connection, args=(plane, conn), daemon=True).start()

    def stats_loop() -> None:
        while not stop.is_set():
            with stats_lock:
                _write_json(stats_path, plane.snapshot())
            time.sleep(0.5)

    threading.Thread(target=accept_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()
    _emit(f"relay listening on {port}")
    stop.wait(seconds)
    server.close()
    with stats_lock:
        _write_json(stats_path, plane.snapshot())
    return 0


def _serve_connection(plane: Any, conn: socket.socket) -> None:
    _prepare_path()
    from rdc.session.channel import recv_frame

    conn.settimeout(5.0)
    try:
        raw = recv_frame(conn)
    except (OSError, ValueError):
        conn.close()
        return
    if raw is None:
        conn.close()
        return
    hello = plane.parse_hello(raw)
    if hello is None:
        conn.close()
        return
    plane.attach(conn, hello)


# ---------------------------------------------------------------- receiver 进程
def run_receiver(port: int, destination: Path, result_path: Path) -> int:
    _prepare_path()
    from rdc.files.receiver import FileTransferReceiver
    from rdc.session.channel import RelayFrameChannel, SessionChannelError

    channel = RelayFrameChannel("127.0.0.1", port, TOKEN, SESSION_ID, "publisher")
    channel.open()
    done = threading.Event()
    completed: dict[str, object] = {}

    def on_complete(payload: dict[str, object]) -> None:
        completed.update(payload)
        done.set()

    receiver = FileTransferReceiver(destination, on_complete=on_complete)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    failure = ""
    try:
        while not done.is_set() and time.monotonic() < deadline:
            message = channel.receive(0.2)
            if message is None:
                continue
            reply = receiver.handle(message[0], message[1])
            if reply is not None:
                channel.send(reply["type"], reply["payload"])
    except SessionChannelError as exc:
        failure = f"{exc.code}: {exc.message}"
    finally:
        time.sleep(0.3)
        channel.close()
    raw_files = completed.get("files")
    files = [str(item) for item in raw_files] if isinstance(raw_files, list) else []
    payload: dict[str, object] = {
        "completed": done.is_set() and not failure,
        "files": files,
        "file_count": len(files),
        "digest": completed.get("digest", ""),
        "destination": str(destination),
        "error": failure,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    if files:
        target = destination / str(files[0])
        payload["written_bytes"] = target.stat().st_size if target.exists() else 0
        payload["written_sha256"] = _sha256(target) if target.exists() else ""
        payload["written_path"] = str(target)
    _write_json(result_path, payload)
    _emit(f"receiver completed={payload['completed']} files={len(files)}")
    return 0 if payload["completed"] else 1


# ---------------------------------------------------------------- sender 进程
def run_sender(port: int, source: Path, remote_dir: str, result_path: Path) -> int:
    _prepare_path()
    from rdc.files.service import FileTransferService
    from rdc.session.channel import RelayFrameChannel

    channel = RelayFrameChannel("127.0.0.1", port, TOKEN, SESSION_ID, "subscriber")
    channel.open()
    service = FileTransferService(
        channel=channel,
        allow_local_staging=False,
        poll_budget_bytes=16 * 1024 * 1024,
        ack_timeout_s=30.0,
    )
    started_at = time.monotonic()
    started = service.start_transfer(str(source), remote_dir)
    if not started.ok or started.data is None:
        _write_json(result_path, {"state": "failed", "error_code": started.error_code, "message": started.message})
        channel.close()
        return 1
    job_id = str(started.data["job_id"])
    final: dict[str, object] = {}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            final = service.get_transfer_progress(job_id).data or {}
            if final.get("state") in {"completed", "failed", "canceled"}:
                break
            time.sleep(0.02)
    finally:
        channel.close()
    elapsed = max(0.001, time.monotonic() - started_at)
    total = int(final.get("total_bytes", 0) or 0)
    payload = {
        "state": final.get("state", "unknown"),
        "error_code": final.get("error_code"),
        "message": final.get("error_message") or "",
        "file_bytes": source.stat().st_size,
        "total_bytes": total,
        "elapsed_seconds": round(elapsed, 3),
        "speed_kbps": int(total / 1000 / elapsed),
        "digest": final.get("digest"),
        "sent_bytes": final.get("sent_bytes"),
        "acked_bytes": final.get("acked_bytes"),
        "channel": final.get("channel"),
        "remote": final.get("remote"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(result_path, payload)
    _emit(f"sender state={payload['state']} elapsed={elapsed:.3f}s speed={payload['speed_kbps']}KB/s")
    return 0 if payload["state"] == "completed" else 1


# ---------------------------------------------------------------- 编排
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def orchestrate(source: Path, destination: Path) -> int:
    _prepare_path()
    from rdc.files.digest import tree_digest

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = ROOT / "evidence" / f"file-channel-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    python = sys.executable
    relay_cmd = [python, str(Path(__file__).resolve()), "--role", "relay", "--port", str(port)]
    relay_cmd += ["--stats", str(evidence / "relay.json"), "--seconds", "90"]
    relay = subprocess.Popen(relay_cmd, cwd=ROOT)
    processes: list[subprocess.Popen[bytes]] = [relay]
    try:
        if not _wait_port(port):
            _emit("relay 未能启动")
            return 1
        receiver_cmd = [
            python,
            str(Path(__file__).resolve()),
            "--role",
            "receiver",
            "--port",
            str(port),
            "--dest",
            str(destination),
            "--result",
            str(evidence / "receiver.json"),
        ]
        receiver = subprocess.Popen(receiver_cmd, cwd=ROOT)
        processes.append(receiver)
        sender_cmd = [
            python,
            str(Path(__file__).resolve()),
            "--role",
            "sender",
            "--port",
            str(port),
            "--file",
            str(source),
            "--remote",
            str(destination),
            "--result",
            str(evidence / "sender.json"),
        ]
        sender = subprocess.Popen(sender_cmd, cwd=ROOT)
        processes.append(sender)
        sender_code = sender.wait(timeout=TIMEOUT_SECONDS + 30)
        receiver_code = receiver.wait(timeout=TIMEOUT_SECONDS + 30)
    except subprocess.TimeoutExpired:
        _emit("端到端超时")
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes[1:]:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    sender_data = json.loads((evidence / "sender.json").read_text(encoding="utf-8"))
    receiver_data = json.loads((evidence / "receiver.json").read_text(encoding="utf-8"))
    expected = tree_digest([(source.name, source)])
    checks = {
        "sender_completed": sender_data.get("state") == "completed",
        "receiver_completed": bool(receiver_data.get("completed")),
        "digest_match": sender_data.get("digest") == expected == receiver_data.get("digest"),
        "bytes_match": receiver_data.get("written_bytes") == source.stat().st_size,
        "content_match": receiver_data.get("written_sha256") == _sha256(source),
        "process_exit_ok": sender_code == 0 and receiver_code == 0,
    }
    summary = {
        "file": str(source),
        "file_bytes": source.stat().st_size,
        "elapsed_seconds": sender_data.get("elapsed_seconds"),
        "speed_kbps": sender_data.get("speed_kbps"),
        "expected_tree_digest": expected,
        "sender": sender_data,
        "receiver": receiver_data,
        "checks": checks,
    }
    _write_json(evidence / "summary.json", summary)
    lines = [
        "# 文件通道跨进程实测",
        "",
        f"- 源文件: {source}",
        f"- 大小: {source.stat().st_size} 字节",
        f"- 耗时: {sender_data.get('elapsed_seconds')} 秒",
        f"- 速率: {sender_data.get('speed_kbps')} KB/s",
        f"- 整树摘要: {expected}",
        f"- 收端写入: {receiver_data.get('written_path')}（{receiver_data.get('written_bytes')} 字节）",
        f"- 收端内容 SHA-256: {receiver_data.get('written_sha256')}",
        "",
        "## 校验",
        "",
        *[f"- {'通过' if passed else '失败'}: {name}" for name, passed in checks.items()],
    ]
    (evidence / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _emit("\n".join(lines))
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="文件会话通道跨进程端到端实测")
    parser.add_argument("--role", choices=["relay", "receiver", "sender", "orchestrate"], default="orchestrate")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--file", default="")
    parser.add_argument("--dest", default="")
    parser.add_argument("--remote", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--stats", default="")
    parser.add_argument("--seconds", type=int, default=90)
    args = parser.parse_args()

    if args.role == "relay":
        return run_relay(args.port, Path(args.stats or "relay.json"), args.seconds)
    if args.role == "receiver":
        return run_receiver(args.port, Path(args.dest), Path(args.result))
    if args.role == "sender":
        return run_sender(args.port, Path(args.file), args.remote, Path(args.result))

    workspace = Path(tempfile.mkdtemp(prefix="rdc-file-e2e-"))
    source = Path(args.file) if args.file else workspace / "payload.bin"
    if not args.file:
        payload = (bytes(range(256)) * (DEFAULT_BYTES // 256 + 1))[:DEFAULT_BYTES]
        source.write_bytes(payload)
    if not source.exists():
        _emit(f"源文件不存在: {source}")
        return 1
    destination = Path(args.dest) if args.dest else workspace / "inbox"
    return orchestrate(source, destination)


if __name__ == "__main__":
    raise SystemExit(main())
