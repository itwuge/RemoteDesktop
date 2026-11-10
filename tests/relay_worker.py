"""双进程联调用的数据面工作进程.

从 stdin 读取 JSON 参数, 连到中继数据面, 发布或订阅固定帧, 结果以单行 JSON 写到 stdout.
不打印令牌; 令牌只经 stdin 传入, 不出现在命令行与日志.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import sys
import time

MAX_FRAME = 6 << 20


def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def _recv_frame(sock: socket.socket) -> bytes | None:
    header = _recv_exactly(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME:
        raise ValueError(f"帧长度非法: {length}")
    payload = _recv_exactly(sock, length)
    if payload is None:
        raise ValueError("帧不完整")
    return payload


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def _frame(index: int, size: int) -> bytes:
    body = bytes([index % 251]) * (size - 4)
    return struct.pack(">I", index) + body


def main() -> int:
    config = json.load(sys.stdin)
    role = str(config["role"])
    frames = int(config["frames"])
    frame_bytes = int(config["frame_bytes"])
    interval_s = float(config.get("interval_s", 0.0))
    start_delay_s = float(config.get("start_delay_s", 0.0))

    sock = socket.create_connection((str(config["host"]), int(config["port"])), timeout=10)
    sock.settimeout(20)
    try:
        hello = {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "session_id": config["session_id"],
            "relay_token": config["relay_token"],
            "role": role,
        }
        _send_frame(sock, json.dumps(hello, separators=(",", ":")).encode("utf-8"))
        ready = json.loads((_recv_frame(sock) or b"{}").decode("utf-8"))
        if ready.get("type") == "relay_error":
            result = {"role": role, "error": ready.get("code")}
            sys.stdout.write(json.dumps(result) + "\n")
            return 0

        if role == "publisher":
            if start_delay_s > 0:
                time.sleep(start_delay_s)
            digest = hashlib.sha256()
            started = time.perf_counter()
            for index in range(frames):
                payload = _frame(index, frame_bytes)
                digest.update(payload)
                _send_frame(sock, payload)
                if interval_s > 0:
                    time.sleep(interval_s)
            elapsed = time.perf_counter() - started
            result = {
                "role": role,
                "frames_sent": frames,
                "bytes_sent": frames * frame_bytes,
                "elapsed_s": round(elapsed, 4),
                "kbps": round(frames * frame_bytes * 8 / elapsed / 1000, 1),
                "sha256": digest.hexdigest(),
            }
        else:
            digest = hashlib.sha256()
            received = 0
            received_bytes = 0
            while received < frames:
                payload = _recv_frame(sock)
                if payload is None:
                    break
                digest.update(payload)
                received += 1
                received_bytes += len(payload)
            result = {
                "role": role,
                "frames_received": received,
                "bytes_received": received_bytes,
                "sha256": digest.hexdigest(),
            }
        sys.stdout.write(json.dumps(result) + "\n")
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
