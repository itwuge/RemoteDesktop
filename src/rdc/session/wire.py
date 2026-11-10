# ==========================================================================================
# 文件        : wire.py
# 所属目录    : src/rdc/session/
# 文件用途    : 信令与数据面共用的线格式: 4 字节大端长度帧 + JSON 帧收发
# 架构定位    : 协议基础层, 被信令客户端、数据面通道与被控端/控制端共用
# 核心功能    : send_packet / recv_packet / send_json / recv_json; 帧长上限与截断判定
# 职责边界    : 只做字节搬运与边界校验; 不加密、不解析业务语义、不重试
# 运行环境    : Windows 与 Ubuntu, Python 3.12
# 依赖关系    : 只依赖标准库; 被 rdc.session.control / dataplane / host / remote 调用
# ==========================================================================================

"""线格式: 4 字节大端长度 + 负载.

信令面与中继数据面使用同一套帧格式, 首个数据面帧是明文握手 JSON, 之后是 AEAD 密文.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

MAX_FRAME_BYTES = 6 << 20


class WireError(ValueError):
    """线格式错误, 携带可读原因; 调用方据此关闭连接或给出中文提示."""


def send_packet(sock: socket.socket, payload: bytes) -> None:
    """发送一帧: 4 字节大端长度 + 负载."""
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise WireError(f"帧长度非法: {len(payload)}")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    """读取恰好 size 字节; 对端正常关闭返回 None."""
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def recv_packet(sock: socket.socket) -> bytes | None:
    """接收一帧; 对端正常关闭返回 None."""
    header = recv_exactly(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise WireError(f"帧长度非法: {length}")
    body = recv_exactly(sock, length)
    if body is None:
        raise WireError("帧体被截断")
    return body


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    """发送一帧 JSON."""
    send_packet(sock, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def recv_json(sock: socket.socket) -> dict[str, Any] | None:
    """接收一帧 JSON; 对端关闭返回 None."""
    body = recv_packet(sock)
    if body is None:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError(f"对端帧不是合法 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WireError("对端帧必须是 JSON 对象")
    return value
