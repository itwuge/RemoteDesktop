# ==========================================================================================
# 文件        : e2e_host_agent.py
# 所属目录    : scripts/
# 文件用途    : 端到端远程输入验证的被控端代理: 真实信令认证、中继数据面接收、记事本注入与取证
# 架构定位    : 验收装置中的被控端进程, 与控制器分进程运行, 输入事件经真实 TCP 中继送达
# 核心功能    : 注册/认证 → 等待 offer → answer/relay_allocate → 数据面握手 → AEAD 通道 →
#               前台聚焦 → 逐字符 SendInput → Ctrl+A/C/S 与文件/截图三重取证 → 回传 proof
# 职责边界    : 只写本进程证据目录; 不改产品代码; 不访问公网; 焦点拿不到时如实报告不注入
# ==========================================================================================

"""端到端远程输入验证的被控端代理.

用法:
    python scripts/e2e_host_agent.py --work <证据目录> --host 127.0.0.1 --port <信令端口>
        --session-id <会话号>

证据: <work>/ready.json、<work>/host_result.json、<work>/screenshot.png
退出码: 0 取证完成且断言一致; 2 记事本不可用; 3 前台焦点失败; 1 其他失败.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from rdc.crypto.identity import DeviceIdentity

TARGET_TEXT_DEFAULT = "RDC-REMOTE-INPUT-2026"
PROJECT_ROOT = smoke_env.PROJECT_ROOT


def _emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


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


def _relay_target(endpoint: str, fallback_host: str, fallback_port: int) -> tuple[str, int]:
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.hostname and parsed.port:
            return parsed.hostname, parsed.port
    return fallback_host, fallback_port


def _finish(
    work: Path,
    payload: dict[str, Any],
    status: str,
    reason: str,
    code: int,
) -> int:
    result = {"status": status, "reason": reason, **payload}
    write_json(work / "host_result.json", result)
    _emit(f"被控端结果: {status} {reason}".strip())
    return code


def run(arguments: argparse.Namespace) -> int:
    work: Path = Path(arguments.work)
    work.mkdir(parents=True, exist_ok=True)
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

    client = SignalingClient(arguments.host, arguments.port, timeout_s=60.0)
    channel: AeadChannel | None = None
    relay_socket: socket.socket | None = None
    try:
        client.connect()
        client.register_and_auth(identity, "e2e-host-agent")
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
                "chosen_candidate": {"kind": "relay", "address": "127.0.0.1", "port": arguments.port},
                "media_caps": {"codecs": ["h264"], "max_width": 1920, "max_height": 1080, "max_fps": 30},
            },
            session_id=session_id,
        )
        client.send_envelope(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 600, "est_duration_s": 120},
            session_id=session_id,
        )
        allocated = client.recv_until("relay_allocated")
        allocated_payload = allocated.get("payload", {})
        relay_token = str(allocated_payload.get("relay_token", ""))
        relay_host, relay_port = _relay_target(
            str(allocated_payload.get("endpoint", "")),
            arguments.host,
            arguments.port,
        )
        relay_socket = relay_connect(relay_host, relay_port, relay_token, session_id, "publisher")
        # 先告知控制端“发布端已配对”, 避免订阅端在无对端时先发帧被数据面断开
        write_json(work / "dataplane_ready.json", {"ready": True, "role": "publisher"})
        session = host_handshake(relay_socket, identity)
        channel = AeadChannel(relay_socket, session)
        _emit(f"被控端已接入中继数据面: {relay_host}:{relay_port}")

        injector = Injector()
        typed = 0
        while True:
            message = channel.recv()
            kind = str(message.get("kind", ""))
            if kind == "prepare":
                focused = False
                for _ in range(3):
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
                clipboard_ok = False
                file_ok = False
                screenshot_ok = False
                clipboard_text = ""
                file_text = ""
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
                }
                channel.send(proof)
                passed = typed == len(expected) and clipboard_ok and file_ok and screenshot_ok
                status = "已实测通过" if passed else "失败"
                reason = "" if passed else "逐字符/剪贴板/文件/截图断言未全部一致"
                return _finish(work, proof | {"typed": typed}, status, reason, 0 if passed else 1)
            elif kind == "abort":
                return _finish(work, {"char_count": typed}, "失败", str(message.get("reason", "控制端中止")), 1)
            else:
                raise E2EFailure(f"未知控制消息: {kind}")
    except (OSError, ValueError, RuntimeError, E2EFailure) as error:
        return _finish(work, {"char_count": 0}, "失败", f"{type(error).__name__}: {error}", 1)
    finally:
        if relay_socket is not None:
            relay_socket.close()
        client.close()
        if original_clipboard is not None:
            write_clipboard_text(original_clipboard)
        with contextlib.suppress(OSError):
            process.terminate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端远程输入验证被控端代理")
    parser.add_argument("--work", required=True, help="证据目录")
    parser.add_argument("--host", default="127.0.0.1", help="信令服务地址")
    parser.add_argument("--port", required=True, type=int, help="信令服务端口")
    parser.add_argument("--session-id", required=True, help="会话号")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
