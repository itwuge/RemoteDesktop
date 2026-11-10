# ==========================================================================================
# 文件        : verify_clipboard_channel_e2e.py
# 所属目录    : scripts/
# 文件用途    : 用本机真实 TCP + RelayDataPlane 跑两个独立进程的剪贴板双向同步实测
# 架构定位    : 验收脚本, 不进入产品运行路径
# 核心功能    : 双向推送、对端确认、去重、关闭开关、反向拉取; 记录摘要与明文不落盘
# 职责边界    : 不修改产品代码; 不写系统剪贴板; 不替代单测
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : rdc.session.channel; rdc.clipboard.sync; rdc.relay.dataplane
# ==========================================================================================

"""剪贴板通道跨进程实测.

用法::

    python scripts/verify_clipboard_channel_e2e.py

产物写入 ``evidence/clipboard-channel-<时间戳>/``, 含两端结果与中继计数.
"""

from __future__ import annotations

import argparse
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

SESSION_ID = "session-clip-e2e"
TOKEN = "token-clip-e2e"
BUDGET_KBPS = 100_000
TIMEOUT_SECONDS = 60
STEP_TIMEOUT = 10.0

DESKTOP_TEXT = "桌面发送: 你好, 对端"
ANDROID_TEXT = "安卓回传: 已收到"
DUPLICATE_TEXT = ANDROID_TEXT
BLOCKED_TEXT = "关闭后不应发送"


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _prepare_path() -> None:
    """让子进程无需外部 PYTHONPATH 也能 import rdc."""
    source = str(ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def _wait_file(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


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
            _write_json(stats_path, plane.snapshot())
            time.sleep(0.5)

    threading.Thread(target=accept_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()
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
    plane.attach(conn, hello)


# ---------------------------------------------------------------- 公共驱动
def _status(service: Any) -> dict[str, Any]:
    data = service.status().data or {}
    return dict(data)


def _pump(service: Any, channel: Any, seconds: float, predicate: Any = None) -> bool:
    """持续消费通道直到谓词成立或超时; 谓词为 None 时只跑满时长."""
    from rdc.session.channel import SessionChannelError

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            service.poll_channel()
        except SessionChannelError:
            return False
        if predicate is not None and predicate():
            return True
        time.sleep(0.02)
    return predicate is None


def _receiver_text(service: Any) -> str:
    if service.receiver is None:
        return ""
    item = service.receiver.latest.get("text")
    return str(item["content"]) if item else ""


def _receiver_digest(service: Any) -> str:
    if service.receiver is None:
        return ""
    item = service.receiver.latest.get("text")
    return str(item["digest"]) if item else ""


# ---------------------------------------------------------------- desktop 进程
def run_desktop(port: int, workdir: Path, result_path: Path) -> int:
    _prepare_path()
    from rdc.clipboard.sync import ClipboardSyncService
    from rdc.session.channel import RelayFrameChannel

    channel = RelayFrameChannel("127.0.0.1", port, TOKEN, SESSION_ID, "publisher")
    channel.open()
    service = ClipboardSyncService()
    service.bind_channel(channel)
    result: dict[str, Any] = {"role": "desktop"}
    try:
        if not _wait_file(workdir / "android.ready", STEP_TIMEOUT):
            result["error"] = "对端未就绪"
        else:
            pushed = service.push("desktop", "text", DESKTOP_TEXT)
            result["push_ok"] = pushed.ok
            result["push_pending"] = bool((pushed.data or {}).get("pending"))
            result["push_digest"] = (pushed.data or {}).get("digest")
            result["push_confirmed"] = _pump(
                service, channel, STEP_TIMEOUT, lambda: bool(_status(service).get("confirmed"))
            )
            _touch(workdir / "desktop.pushed")
            result["received_ok"] = _pump(
                service, channel, STEP_TIMEOUT, lambda: _receiver_text(service) == ANDROID_TEXT
            )
            result["received_text_digest"] = _receiver_digest(service) if result["received_ok"] else None
            if _wait_file(workdir / "android.done", STEP_TIMEOUT):
                pulled = service.pull("desktop")
                result["pull_ok"] = pulled.ok
                result["pull_pending"] = bool((pulled.data or {}).get("pending"))
                result["pull_confirmed"] = _pump(
                    service, channel, STEP_TIMEOUT, lambda: _status(service).get("confirmed_action") == "pull"
                )
                result["pull_text"] = _receiver_text(service)
                _touch(workdir / "desktop.done")
    finally:
        _pump(service, channel, 0.5)
        channel.close()
    status = _status(service)
    result["mode"] = status.get("mode")
    result["visible_to_relay"] = status.get("visible_to_relay")
    result["encrypted"] = status.get("encrypted")
    result["status_has_plaintext"] = "text" in status or "image_b64" in status
    result["last_error"] = status.get("last_error")
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(result_path, result)
    _emit(f"desktop done confirmed={result.get('push_confirmed')} received={result.get('received_ok')}")
    return 0 if result.get("push_confirmed") and result.get("received_ok") else 1


# ---------------------------------------------------------------- android 进程
def run_android(port: int, workdir: Path, result_path: Path) -> int:
    _prepare_path()
    from rdc.clipboard.sync import ClipboardSyncService
    from rdc.session.channel import RelayFrameChannel

    channel = RelayFrameChannel("127.0.0.1", port, TOKEN, SESSION_ID, "subscriber")
    channel.open()
    service = ClipboardSyncService()
    service.bind_channel(channel)
    result: dict[str, Any] = {"role": "android"}
    _touch(workdir / "android.ready")
    try:
        result["received_ok"] = _pump(service, channel, STEP_TIMEOUT, lambda: _receiver_text(service) == DESKTOP_TEXT)
        result["received_digest"] = _receiver_digest(service) if result["received_ok"] else None
        _wait_file(workdir / "desktop.pushed", STEP_TIMEOUT)

        pushed = service.push("android", "text", ANDROID_TEXT)
        result["push_ok"] = pushed.ok
        result["push_digest"] = (pushed.data or {}).get("digest")
        result["push_confirmed"] = _pump(
            service, channel, STEP_TIMEOUT, lambda: bool(_status(service).get("confirmed"))
        )
        if service.receiver is not None:
            # 模拟本端系统剪贴板已更新, 供对端 clipboard_request 应答
            service.receiver.set_local("text", ANDROID_TEXT)

        duplicate = service.push("android", "text", DUPLICATE_TEXT)
        result["duplicate_applied"] = bool((duplicate.data or {}).get("applied"))
        result["duplicate_reason"] = (duplicate.data or {}).get("reason")

        result["disable_ok"] = service.set_enabled(False).ok
        blocked = service.push("android", "text", BLOCKED_TEXT)
        result["blocked_ok"] = blocked.ok
        result["blocked_error"] = blocked.error_code
        _touch(workdir / "android.done")

        # 继续消费通道, 直到桌面端完成反向拉取, 否则请求无人应答
        result["pull_served"] = _pump(service, channel, STEP_TIMEOUT, lambda: (workdir / "desktop.done").exists())
    finally:
        _pump(service, channel, 0.5)
        channel.close()
    status = _status(service)
    result["mode"] = status.get("mode")
    result["visible_to_relay"] = status.get("visible_to_relay")
    result["status_has_plaintext"] = "text" in status or "image_b64" in status
    result["last_error"] = status.get("last_error")
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(result_path, result)
    _emit(f"android done received={result.get('received_ok')} push_confirmed={result.get('push_confirmed')}")
    return 0 if result.get("received_ok") and result.get("push_confirmed") else 1


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


def orchestrate() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = ROOT / "evidence" / f"clipboard-channel-{stamp}"
    evidence.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="rdc-clip-"))
    port = _free_port()
    python = sys.executable
    script = str(Path(__file__).resolve())
    relay = subprocess.Popen(
        [python, script, "--role", "relay", "--port", str(port), "--stats", str(evidence / "relay.json")],
        cwd=ROOT,
    )
    processes = [relay]
    try:
        if not _wait_port(port):
            _emit("relay 未能启动")
            return 1
        android = subprocess.Popen(
            [
                python,
                script,
                "--role",
                "android",
                "--port",
                str(port),
                "--workdir",
                str(workdir),
                "--result",
                str(evidence / "android.json"),
            ],
            cwd=ROOT,
        )
        processes.append(android)
        desktop = subprocess.Popen(
            [
                python,
                script,
                "--role",
                "desktop",
                "--port",
                str(port),
                "--workdir",
                str(workdir),
                "--result",
                str(evidence / "desktop.json"),
            ],
            cwd=ROOT,
        )
        processes.append(desktop)
        desktop_code = desktop.wait(timeout=TIMEOUT_SECONDS)
        android_code = android.wait(timeout=TIMEOUT_SECONDS)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()

    desktop_data = json.loads((evidence / "desktop.json").read_text(encoding="utf-8"))
    android_data = json.loads((evidence / "android.json").read_text(encoding="utf-8"))
    checks = {
        "desktop_push_confirmed": bool(desktop_data.get("push_confirmed")),
        "android_push_confirmed": bool(android_data.get("push_confirmed")),
        "desktop_received_ok": bool(desktop_data.get("received_ok")),
        "android_received_ok": bool(android_data.get("received_ok")),
        "digest_match": desktop_data.get("push_digest") == android_data.get("received_digest")
        and android_data.get("push_digest") == desktop_data.get("received_text_digest"),
        "duplicate_suppressed": android_data.get("duplicate_applied") is False,
        "disabled_blocks_send": android_data.get("blocked_ok") is False,
        "pull_confirmed": bool(desktop_data.get("pull_confirmed")),
        "pull_content_match": desktop_data.get("pull_text") == ANDROID_TEXT,
        "relay_visible_flagged": bool(desktop_data.get("visible_to_relay")) and desktop_data.get("encrypted") is False,
        "status_no_plaintext": desktop_data.get("status_has_plaintext") is False
        and android_data.get("status_has_plaintext") is False,
        "process_exit_ok": desktop_code == 0 and android_code == 0,
    }
    summary = {
        "checks": checks,
        "all_passed": all(checks.values()),
        "desktop": desktop_data,
        "android": android_data,
        "evidence_dir": str(evidence),
    }
    _write_json(evidence / "summary.json", summary)
    _emit("")
    for name, passed in checks.items():
        _emit(f"{'PASS' if passed else 'FAIL'}  {name}")
    _emit(f"产物目录: {evidence}")
    return 0 if summary["all_passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="剪贴板会话通道跨进程实测")
    parser.add_argument("--role", choices=["relay", "desktop", "android", "orchestrate"], default="orchestrate")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--stats", default="")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--result", default="")
    parser.add_argument("--seconds", type=int, default=90)
    options = parser.parse_args(argv)
    if options.role == "relay":
        return run_relay(options.port, Path(options.stats), options.seconds)
    if options.role == "desktop":
        return run_desktop(options.port, Path(options.workdir), Path(options.result))
    if options.role == "android":
        return run_android(options.port, Path(options.workdir), Path(options.result))
    return orchestrate()


if __name__ == "__main__":
    raise SystemExit(main())
