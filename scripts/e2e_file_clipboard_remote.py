# ==========================================================================================
# 文件        : e2e_file_clipboard_remote.py
# 所属目录    : scripts/
# 文件用途    : 跨机验证 Linux host 的文件接收与剪贴板会话通道(信令 + 中继 + AEAD 加密数据面)
# 架构定位    : 验收装置; 只作为控制端客户端连接被测 host, 不改产品代码与契约
# 核心功能    : 发送目录树并校验整树摘要; 剪贴板下行写入与上行读取各一次; 记录大小/耗时/速率
# 职责边界    : 不启动被控端; 不修改中继; 不写任何凭据; 不把模拟值标注为实测
# ==========================================================================================

"""跨机文件与剪贴板会话通道实测(控制端).

用法:
    python scripts/e2e_file_clipboard_remote.py --host 192.168.37.132 --port 27888 \
        --peer <设备ID> --session-id <会话号> --send-dir <待发目录> --evidence <证据目录> \
        --clip-text "RDC-CLIP-DOWN-2026"
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

from _rdc_path import PROJECT_ROOT
from e2e_common import (
    AeadChannel,
    E2EFailure,
    SignalingClient,
    build_access_proof,
    controller_handshake,
    relay_connect,
)
from rdc.crypto.identity import DeviceIdentity
from rdc.files.digest import tree_digest
from rdc.files.model import CHUNK_SIZE

CHUNK = min(CHUNK_SIZE, 64 * 1024)
RECV_TIMEOUT_S = 30.0


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _collect(root: Path) -> list[tuple[str, Path]]:
    """列出目录树里的普通文件, 相对路径用 POSIX 分隔符, 顺序稳定."""
    entries = [(path.relative_to(root).as_posix(), path) for path in sorted(root.rglob("*")) if path.is_file()]
    if not entries:
        raise E2EFailure(f"待发目录为空: {root}")
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SessionClient:
    """控制端会话: 信令 → 中继 → AEAD, 之后只收发 JSON 会话消息."""

    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.channel: AeadChannel | None = None
        self.client: SignalingClient | None = None
        self.socket: socket.socket | None = None
        self.budget_kbps = 0

    def open(self) -> None:
        identity = DeviceIdentity.generate()
        arguments = self.arguments
        client = SignalingClient(arguments.host, arguments.port, timeout_s=60.0)
        client.connect()
        client.register_and_auth(identity, "rdc-file-e2e-controller")
        self.client = client
        session_id = arguments.session_id
        proof = build_access_proof(arguments.access_code, session_id, identity.device_id, arguments.peer)
        client.send_envelope(
            "offer",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": arguments.peer,
                "candidates": [{"kind": "relay", "address": arguments.host, "port": arguments.port, "priority": 1}],
                "media_caps": {"codecs": ["h264"], "frame_versions": [2, 1]},
                # 契约要求的凭据字段 + 被控端连接码挑战应答
                "access_proof": proof["response"],
                "access_auth": proof,
            },
            session_id=session_id,
        )
        client.recv_until("answer")
        client.send_envelope(
            "session_start",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": arguments.peer,
                "transport_kind": "relay",
                "quality_profile": "L1",
            },
            session_id=session_id,
        )
        client.send_envelope(
            "relay_allocate",
            {"session_id": session_id, "requested_kbps": 8000, "est_duration_s": 300},
            session_id=session_id,
        )
        allocated = client.recv_until("relay_allocated")
        payload = allocated.get("payload", {})
        self.budget_kbps = int(payload.get("budget_kbps") or 0)
        relay_socket = relay_connect(
            arguments.host, arguments.port, str(payload.get("relay_token", "")), session_id, "subscriber"
        )
        relay_socket.settimeout(RECV_TIMEOUT_S)
        session = controller_handshake(relay_socket, identity, session_id, arguments.peer)
        self.socket = relay_socket
        self.channel = AeadChannel(relay_socket, session)

    def close(self) -> None:
        if self.socket is not None:
            with contextlib.suppress(OSError):
                self.socket.close()
        if self.client is not None:
            self.client.close()

    def send(self, msg_type: str, payload: dict[str, Any]) -> None:
        assert self.channel is not None
        self.channel.send({"type": msg_type, "session_id": self.arguments.session_id, "payload": payload})

    def wait_for(self, wanted: set[str], timeout_s: float = RECV_TIMEOUT_S) -> dict[str, Any]:
        """读数据面直到出现目标类型; 二进制视频帧直接跳过."""
        assert self.channel is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = self.channel.recv_raw()
            if raw[:1] != b"{":
                continue
            message = json.loads(raw.decode("utf-8"))
            msg_type = str(message.get("type") or message.get("kind") or "")
            if msg_type in wanted:
                return message
        raise E2EFailure(f"等待 {sorted(wanted)} 超时")


def _clipboard_down(client: SessionClient, text: str) -> dict[str, Any]:
    raw = text.encode("utf-8")
    started = time.perf_counter()
    client.send(
        "clipboard_update",
        {
            "session_id": client.arguments.session_id,
            "kind": "text",
            "content": text,
            "size_bytes": len(raw),
            "digest": hashlib.sha256(raw).hexdigest(),
        },
    )
    reply = client.wait_for({"clipboard_ack", "error"})
    return {
        "direction": "desktop->host",
        "kind": "text",
        "size_bytes": len(raw),
        "digest": hashlib.sha256(raw).hexdigest(),
        "reply_type": reply.get("type"),
        "applied": (reply.get("payload") or {}).get("applied"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }


def _clipboard_up(client: SessionClient) -> dict[str, Any]:
    started = time.perf_counter()
    client.send("clipboard_request", {"session_id": client.arguments.session_id, "kind": "text"})
    reply = client.wait_for({"clipboard_update", "error"})
    payload = reply.get("payload") or {}
    content = str(payload.get("content") or "")
    return {
        "direction": "host->desktop",
        "kind": str(payload.get("kind") or ""),
        "size_bytes": payload.get("size_bytes"),
        "digest": str(payload.get("digest") or ""),
        "content_prefix": content[:32],
        "reply_type": reply.get("type"),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }


def _send_tree(client: SessionClient, root: Path, transfer_id: str) -> dict[str, Any]:
    entries = _collect(root)
    items = [{"relative": relative, "size": path.stat().st_size, "sha256": _sha256(path)} for relative, path in entries]
    total = sum(item["size"] for item in items)
    digest = tree_digest(entries)
    started = time.perf_counter()
    client.send(
        "file_offer",
        {
            "session_id": client.arguments.session_id,
            "transfer_id": transfer_id,
            "chunk_size": CHUNK,
            "digest_alg": "sha256",
            "digest": digest,
            "total_bytes": total,
            "items": items,
        },
    )
    accepted = client.wait_for({"file_accept", "file_abort"})
    if accepted.get("type") == "file_abort":
        raise E2EFailure(f"offer 被拒绝: {accepted.get('payload')}")
    for index, (relative, path) in enumerate(entries):
        offset = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(CHUNK), b""):
                client.send(
                    "file_chunk",
                    {
                        "session_id": client.arguments.session_id,
                        "transfer_id": transfer_id,
                        "file_index": index,
                        "offset": offset,
                        "length": len(block),
                        "sha256": hashlib.sha256(block).hexdigest(),
                        "data": base64.b64encode(block).decode("ascii"),
                    },
                )
                offset += len(block)
                ack = client.wait_for({"file_ack", "file_abort"})
                if ack.get("type") == "file_abort":
                    raise E2EFailure(f"分块被拒绝: {ack.get('payload')}")
        _emit(f"  已发送 {relative} ({offset} 字节)")
    client.send(
        "file_complete",
        {"session_id": client.arguments.session_id, "transfer_id": transfer_id, "digest": digest},
    )
    result = client.wait_for({"file_result", "file_abort"})
    elapsed = time.perf_counter() - started
    payload = result.get("payload") or {}
    return {
        "files": len(items),
        "total_bytes": total,
        "digest_expected": digest,
        "digest_reported": str(payload.get("digest") or ""),
        "verified": bool(payload.get("verified")),
        "error_code": str(payload.get("error_code") or ""),
        "elapsed_s": round(elapsed, 3),
        "rate_kbps": round(total * 8 / elapsed / 1000.0, 1) if elapsed > 0 else 0.0,
        "rate_kib_s": round(total / elapsed / 1024.0, 1) if elapsed > 0 else 0.0,
        "items": [{"relative": item["relative"], "size": item["size"], "sha256": item["sha256"]} for item in items],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨机文件与剪贴板会话通道实测")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--peer", required=True, help="被控端设备 ID")
    parser.add_argument("--access-code", required=True, help="被控端启动横幅里的连接码")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--send-dir", required=True)
    parser.add_argument(
        "--evidence",
        default=str(PROJECT_ROOT / "evidence" / f"file-clipboard-{time.strftime('%Y%m%d-%H%M%S')}"),
        help="证据目录, 默认落在仓库 evidence 下",
    )
    parser.add_argument("--clip-text", default="RDC-CLIP-DOWN-2026")
    arguments = parser.parse_args(argv)
    evidence = Path(arguments.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"state": "starting", "session_id": arguments.session_id}
    client = SessionClient(arguments)
    try:
        client.open()
        summary["relay_budget_kbps"] = client.budget_kbps
        _emit(f"[1/3] 会话已建立, 中继预算 {client.budget_kbps} kbps")
        summary["clipboard_up"] = _clipboard_up(client)
        _emit(
            f"[剪贴板 上行] {summary['clipboard_up']['reply_type']} 前缀={summary['clipboard_up']['content_prefix']!r}"
        )
        summary["clipboard_down"] = _clipboard_down(client, arguments.clip_text)
        _emit(f"[剪贴板 下行] accepted={summary['clipboard_down']['applied']}")
        _emit("[2/3] 开始发送目录树")
        summary["transfer"] = _send_tree(client, Path(arguments.send_dir), f"t-{int(time.time())}")
        _emit(
            f"[3/3] 传输完成: {summary['transfer']['files']} 个文件 "
            f"{summary['transfer']['total_bytes']} 字节, {summary['transfer']['elapsed_s']} 秒, "
            f"{summary['transfer']['rate_kbps']} kbps, verified={summary['transfer']['verified']}"
        )
        summary["state"] = "done" if summary["transfer"]["verified"] else "failed"
    except (E2EFailure, OSError, ValueError, RuntimeError) as error:
        summary["state"] = "failed"
        summary["error"] = f"{type(error).__name__}: {error}"
        _emit(f"失败: {summary['error']}")
    finally:
        client.close()
    (evidence / "controller_result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if summary.get("state") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
