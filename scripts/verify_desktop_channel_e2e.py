# ==========================================================================================
# 文件        : verify_desktop_channel_e2e.py
# 所属目录    : scripts/
# 文件用途    : 桌面端会话装配的真实链路实测: AeadChannel → SessionChannel → 文件与剪贴板服务
# 架构定位    : 验收脚本, 不进入产品运行路径
# 核心功能    : relay/desktop/peer 三进程; 真实 TCP + AEAD 握手; 双向文件与剪贴板收发与时延
# 职责边界    : 不修改产品代码; 不写用户目录; 不替代单测
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : scripts/e2e_common.py; rdc.session.aead; rdc.session.desktop; rdc.commands
# ==========================================================================================

"""桌面端会话装配实测.

用法::

    python scripts/verify_desktop_channel_e2e.py
    python scripts/verify_desktop_channel_e2e.py --bytes 2097152

两个端点都通过 ``AeadSessionChannel`` + ``DesktopSessionBinding`` 使用产品命令层的
同一份服务实例(与界面共用), 各发各收一个文件与一段剪贴板文本.
产物写入 ``evidence/desktop-channel-<时间戳>/``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SESSION_ID = "session-desktop-e2e"
TOKEN = "token-desktop-e2e"
BUDGET_KBPS = 100_000
TIMEOUT_SECONDS = 120
DEFAULT_BYTES = 2 * 1024 * 1024


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _prepare_path() -> None:
    scripts = str(Path(__file__).resolve().parent)
    source = str(ROOT / "src")
    for item in (str(ROOT), source, scripts):
        if item not in sys.path:
            sys.path.insert(0, item)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _wait_file(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- relay 进程
def run_relay(port: int, seconds: int, stats_path: Path) -> int:
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

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=_serve_connection, args=(plane, conn), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    _emit(f"relay listening on {port}")
    stop.wait(seconds)
    server.close()
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
    # 握手阶段用短超时; attach 会在本线程里同步跑转发循环, 必须先放宽,
    # 否则空闲 5 秒会被中继自身断开.
    conn.settimeout(120.0)
    plane.attach(conn, hello)


# ---------------------------------------------------------------- 端点进程
def run_endpoint(name: str, port: int, work: Path, source: Path, result_path: Path) -> int:
    _prepare_path()
    from e2e_common import AeadChannel, AeadSessionLink, relay_connect
    from rdc.commands import clipboard as clipboard_commands
    from rdc.commands import transfer as transfer_commands
    from rdc.crypto.identity import DeviceIdentity
    from rdc.session.aead import AeadSessionChannel
    from rdc.session.desktop import DesktopSessionBinding

    other = "peer" if name == "desktop" else "desktop"
    identity = DeviceIdentity.generate()
    (work / f"{name}.id").write_text(identity.device_id, encoding="utf-8")
    if not _wait_file(work / f"{other}.id"):
        _emit(f"{name}: 等待对端设备标识超时")
        return 1
    peer_id = (work / f"{other}.id").read_text(encoding="utf-8").strip()

    sock = relay_connect("127.0.0.1", port, TOKEN, SESSION_ID, "publisher" if name == "desktop" else "subscriber")
    sock.settimeout(TIMEOUT_SECONDS)
    from e2e_common import controller_handshake, host_handshake

    if name == "desktop":
        session = controller_handshake(sock, identity, SESSION_ID, peer_id)
    else:
        session = host_handshake(sock, identity, SESSION_ID, peer_id)
    channel = AeadChannel(sock, session)

    applied: list[tuple[str, str, int]] = []
    received: list[dict[str, Any]] = []
    applied_event = threading.Event()
    received_event = threading.Event()

    def _on_apply(kind: str, content: str) -> None:
        applied.append((kind, content, time.time_ns()))
        applied_event.set()

    def _on_files(payload: dict[str, Any]) -> None:
        received.append(payload)
        received_event.set()

    adapter = AeadSessionChannel(
        AeadSessionLink(channel),
        session_id=SESSION_ID,
        role=name,
        budget_kbps=BUDGET_KBPS,
    )
    adapter.open()
    transfer = transfer_commands.get_service()
    clipboard = clipboard_commands.get_service()
    inbox = work / f"{name}-inbox"
    binding = DesktopSessionBinding(
        adapter,
        transfer_service=transfer,
        clipboard_service=clipboard,
        destination_root=inbox,
        on_files_received=_on_files,
        on_clipboard_apply=_on_apply,
    )
    # 装配要求: 泵与界面读的是同一份服务实例
    shared = (
        binding.clipboard is clipboard_commands.get_service() and binding.transfer is transfer_commands.get_service()
    )

    (work / f"{name}.ready").write_text("1", encoding="utf-8")
    ready = _wait_file(work / f"{other}.ready")
    result: dict[str, Any] = {
        "name": name,
        "role": "controller" if name == "desktop" else "host",
        "shared_service_instance": shared,
        "ready": ready,
        "file_bytes": source.stat().st_size,
        "file_sha256": _sha256(source),
        "clip_text": "",
        "error": "",
    }
    try:
        if not ready:
            raise TimeoutError("对端未就绪")

        clip_text = f"{name} 剪贴板 {time.time_ns()}"
        result["clip_text"] = clip_text
        own_digest = hashlib.sha256(clip_text.encode("utf-8")).hexdigest()
        started = time.monotonic()
        pushed = clipboard.push(name, "text", clip_text)
        if not pushed.ok:
            raise RuntimeError(f"剪贴板发送失败: {pushed.message}")
        confirmed = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            binding.pump()
            receipt = clipboard.confirmed or {}
            if receipt.get("digest") == own_digest:
                confirmed = True
                break
            time.sleep(0.005)
        result["clip_push_rtt_ms"] = round((time.monotonic() - started) * 1000, 2)
        result["clip_push_confirmed"] = confirmed

        started = time.monotonic()
        begun = transfer.start_transfer(str(source), str(work / "remote"))
        if not begun.ok or begun.data is None:
            raise RuntimeError(f"文件发送失败: {begun.message}")
        job_id = str(begun.data["job_id"])
        final: dict[str, Any] = {}
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            final = transfer.get_transfer_progress(job_id).data or {}
            binding.pump()
            if final.get("state") in {"completed", "failed", "canceled"}:
                break
            time.sleep(0.005)
        elapsed = max(0.001, time.monotonic() - started)
        total = int(final.get("total_bytes") or 0)
        result["file_elapsed_s"] = round(elapsed, 3)
        result["file_speed_kbps"] = int(total / 1000 / elapsed)
        result["file_state"] = final.get("state")
        result["file_digest"] = final.get("digest")
        result["file_acked_bytes"] = final.get("acked_bytes")
        result["file_channel"] = final.get("channel")
        if final.get("state") != "completed":
            raise RuntimeError(f"文件未完成: {final.get('error_message') or final.get('state')}")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            binding.pump()
            if applied_event.is_set() and received_event.is_set():
                break
            time.sleep(0.005)
        result["clip_inbound_applied"] = applied[-1][1] if applied else ""
        result["clip_inbound_kind"] = applied[-1][0] if applied else ""
        if applied:
            sent_ns = int(applied[-1][1].rsplit(" ", 1)[-1])
            result["clip_inbound_latency_ms"] = round((applied[-1][2] - sent_ns) / 1e6, 2)
        else:
            result["clip_inbound_latency_ms"] = -1.0
        result["file_inbound"] = received[-1] if received else {}
        peers = [str(item) for item in (received[-1].get("files") if received else [])]
        if peers:
            target = inbox / peers[0]
            result["file_written_path"] = str(target)
            result["file_written_bytes"] = target.stat().st_size if target.exists() else 0
            result["file_written_sha256"] = _sha256(target) if target.exists() else ""
        result["clip_inbound_ok"] = bool(applied)
        result["file_inbound_ok"] = bool(received)
    except (OSError, RuntimeError, TimeoutError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        binding.close()
        adapter.close()
        with contextlib.suppress(OSError):
            sock.close()
    _write_json(result_path, result)
    _emit(f"{name}: state={result.get('file_state')} err={result.get('error')}")
    return 0 if not result["error"] else 1


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


def orchestrate(size: int) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = ROOT / "evidence" / f"desktop-channel-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    work = evidence / "work"
    work.mkdir(parents=True, exist_ok=True)
    desktop_source = work / "desktop.bin"
    peer_source = work / "peer.bin"
    desktop_source.write_bytes(bytes(range(256)) * (size // 256) + bytes(size % 256))
    peer_source.write_bytes(bytes(range(255, -1, -1)) * (size // 256) + bytes(size % 256))

    port = _free_port()
    python = sys.executable
    script = str(Path(__file__).resolve())
    relay = subprocess.Popen(
        [
            python,
            script,
            "--role",
            "relay",
            "--port",
            str(port),
            "--seconds",
            str(TIMEOUT_SECONDS + 60),
            "--stats",
            str(evidence / "relay.json"),
        ],
        cwd=ROOT,
    )
    endpoints: list[subprocess.Popen[bytes]] = []
    try:
        if not _wait_port(port):
            _emit("relay 未能启动")
            return 1
        for name, source in (("desktop", desktop_source), ("peer", peer_source)):
            endpoints.append(
                subprocess.Popen(
                    [
                        python,
                        script,
                        "--role",
                        "endpoint",
                        "--name",
                        name,
                        "--port",
                        str(port),
                        "--work",
                        str(work),
                        "--file",
                        str(source),
                        "--result",
                        str(evidence / f"{name}.json"),
                    ],
                    cwd=ROOT,
                )
            )
        codes = [process.wait(timeout=TIMEOUT_SECONDS + 40) for process in endpoints]
    except subprocess.TimeoutExpired:
        _emit("端到端超时")
        return 1
    finally:
        for process in [*endpoints, relay]:
            if process.poll() is None:
                process.terminate()
        for process in endpoints:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    if not all((evidence / f"{name}.json").exists() for name in ("desktop", "peer")):
        _emit("端点结果缺失")
        return 1
    desktop = json.loads((evidence / "desktop.json").read_text(encoding="utf-8"))
    peer = json.loads((evidence / "peer.json").read_text(encoding="utf-8"))
    checks = {
        "shared_service_instance": bool(desktop.get("shared_service_instance") and peer.get("shared_service_instance")),
        "file_both_completed": desktop.get("file_state") == "completed" and peer.get("file_state") == "completed",
        "file_desktop_to_peer": desktop.get("file_written_sha256") == peer.get("file_sha256"),
        "file_peer_to_desktop": peer.get("file_written_sha256") == desktop.get("file_sha256"),
        "clip_desktop_to_peer": peer.get("clip_inbound_applied") == desktop.get("clip_text"),
        "clip_peer_to_desktop": desktop.get("clip_inbound_applied") == peer.get("clip_text"),
        "clip_confirmed": bool(desktop.get("clip_push_confirmed") and peer.get("clip_push_confirmed")),
        "no_endpoint_error": not desktop.get("error") and not peer.get("error"),
        "process_exit_ok": codes == [0, 0],
    }
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "file_bytes": size,
        "desktop": desktop,
        "peer": peer,
        "checks": checks,
    }
    _write_json(evidence / "summary.json", summary)
    lines = [
        "# 桌面端会话装配实测",
        "",
        f"- 文件大小: {size} 字节（双向各一个）",
        (
            f"- 桌面发送: {desktop.get('file_elapsed_s')} 秒, {desktop.get('file_speed_kbps')} KB/s, "
            f"摘要 {desktop.get('file_digest')}"
        ),
        (
            f"- 对端发送: {peer.get('file_elapsed_s')} 秒, {peer.get('file_speed_kbps')} KB/s, "
            f"摘要 {peer.get('file_digest')}"
        ),
        f"- 桌面收文件 SHA-256: {desktop.get('file_written_sha256')}",
        f"- 对端收文件 SHA-256: {peer.get('file_written_sha256')}",
        f"- 剪贴板 桌面→对端: {desktop.get('clip_inbound_latency_ms')} ms",
        f"- 剪贴板 对端→桌面: {peer.get('clip_inbound_latency_ms')} ms",
        f"- 服务实例共享: {desktop.get('shared_service_instance')} / {peer.get('shared_service_instance')}",
        "",
        "## 校验",
        "",
        *[f"- {'通过' if passed else '失败'}: {name}" for name, passed in checks.items()],
    ]
    (evidence / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _emit("\n".join(lines))
    return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面端会话装配真实链路实测")
    parser.add_argument("--role", choices=["orchestrate", "relay", "endpoint"], default="orchestrate")
    parser.add_argument("--name", default="desktop")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--stats", default="")
    parser.add_argument("--work", default="")
    parser.add_argument("--file", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--bytes", type=int, default=DEFAULT_BYTES)
    arguments = parser.parse_args()
    if arguments.role == "relay":
        return run_relay(arguments.port, arguments.seconds, Path(arguments.stats))
    if arguments.role == "endpoint":
        return run_endpoint(
            arguments.name,
            arguments.port,
            Path(arguments.work),
            Path(arguments.file),
            Path(arguments.result),
        )
    return orchestrate(arguments.bytes)


if __name__ == "__main__":
    raise SystemExit(main())
