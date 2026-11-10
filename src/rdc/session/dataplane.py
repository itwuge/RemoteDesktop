# ==========================================================================================
# 文件        : dataplane.py
# 所属目录    : src/rdc/session/
# 文件用途    : 中继数据面接入与 AEAD 密文通道
# 架构定位    : 数据面传输层; 被控端发布视频、控制端接收视频与上行输入都走这里
# 核心功能    : TLS 1.3 连接; relay_hello/relay_ready 握手; 加密收发 JSON 与原始字节
# 职责边界    : 不解析业务语义、不做编码解码、不做重试与降级; 错误统一抛出
# 运行环境    : Windows 与 Ubuntu, Python 3.12
# 依赖关系    : rdc.session.crypto 的 AeadSession; rdc.session.wire 的帧格式
# ==========================================================================================

"""中继数据面.

首个帧是明文 ``relay_hello``, 服务端回 ``relay_ready`` 之后, 后续帧全部由 AEAD 会话
加密. 发送侧加锁: 视频线程与控制帧并发写同一 socket 时, nonce 递增必须与发送同序.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import ssl
import threading
import time
from typing import Any

from cryptography import x509

from rdc.session.crypto import AeadSession
from rdc.session.wire import WireError, recv_json, recv_packet, send_json, send_packet

DEFAULT_TIMEOUT_S = 10.0
DATA_TIMEOUT_S = 30.0


class DataPlaneError(RuntimeError):
    """数据面失败, 消息为可直接展示的中文原因."""


def tls_connect(
    host: str,
    port: int,
    ca_file: str,
    sni: str = "",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[socket.socket, dict[str, Any]]:
    """以仓库 CA 为唯一信任锚建立 TLS 1.3 连接.

    公网中继按 IP 直连时证书 CN 是域名, 因此不做主机名匹配, 链校验由固定 CA 保证.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cafile=ca_file)
    except (OSError, ssl.SSLError) as error:
        raise DataPlaneError(f"信任锚 CA 加载失败: {error}") from error
    raw = socket.create_connection((host, port), timeout=timeout_s)
    try:
        wrapped = context.wrap_socket(raw, server_hostname=sni or host)
    except ssl.SSLCertVerificationError as error:
        raw.close()
        detail = getattr(error, "verify_message", "") or str(error)
        raise DataPlaneError(f"服务端证书链校验失败: {detail}") from error
    except (ssl.SSLError, OSError):
        raw.close()
        raise
    der = wrapped.getpeercert(binary_form=True) or b""
    certificate = x509.load_der_x509_certificate(der) if der else None
    cipher = wrapped.cipher()
    info = {
        "tls_version": wrapped.version(),
        "cipher": cipher[0] if cipher else "",
        "sni": sni or host,
        "ca_file": str(ca_file),
        "cert_sha256": hashlib.sha256(der).hexdigest() if der else "",
        "cert_subject": certificate.subject.rfc4514_string() if certificate is not None else "",
        "cert_issuer": certificate.issuer.rfc4514_string() if certificate is not None else "",
    }
    return wrapped, info


def relay_connect(
    host: str,
    port: int,
    relay_token: str,
    session_id: str,
    role: str,
    *,
    use_tls: bool = False,
    ca_file: str = "",
    sni: str = "",
    tls_info: dict[str, Any] | None = None,
) -> socket.socket:
    """接入中继数据面并完成明文握手, 返回已就绪的连接."""
    if role not in ("publisher", "subscriber"):
        raise DataPlaneError(f"非法数据面角色: {role}")
    if use_tls:
        sock, info = tls_connect(host, port, ca_file, sni)
        if tls_info is not None:
            tls_info.update(info)
    else:
        sock = socket.create_connection((host, port), timeout=DEFAULT_TIMEOUT_S)
    sock.settimeout(DATA_TIMEOUT_S)
    send_json(
        sock,
        {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": relay_token,
            "session_id": session_id,
            "role": role,
        },
    )
    hello = recv_json(sock)
    if hello is None or hello.get("type") != "relay_ready":
        sock.close()
        detail = "连接关闭" if hello is None else f"{hello.get('type')} {hello.get('message', '')}"
        raise DataPlaneError(f"中继数据面握手失败: {detail}")
    return sock


class AeadChannel:
    """在已建立的数据面连接上收发 AEAD 密文帧, 并作为 ``AeadSessionChannel`` 的底层链路.

    同时提供 ``send(dict)`` 与 ``recv_raw()``, 满足 ``rdc.session.aead.AeadLink`` 协议.
    """

    def __init__(self, sock: socket.socket, session: AeadSession) -> None:
        self.sock = sock
        self.session = session
        self.sent_frames = 0
        self.recv_frames = 0
        # 线缆字节(含 4 字节帧长前缀与 AEAD 标签)与净荷字节分开累计, 供会话统计算码率与封装占比
        self.sent_wire_bytes = 0
        self.recv_wire_bytes = 0
        self.sent_payload_bytes = 0
        self.recv_payload_bytes = 0
        # 视频线程与控制帧会并发写同一个 socket, 加密与发送必须在同一把锁内
        self.send_lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> int:
        """加密并发送一条 JSON, 返回发送完成时刻(纳秒)."""
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.send_bytes(raw)

    def send_bytes(self, payload: bytes) -> int:
        """加密并发送一段原始字节; v2 视频帧走这里, 不做 JSON 与 Base64."""
        with self.send_lock:
            ciphertext = self.session.encrypt(payload)
            try:
                send_packet(self.sock, ciphertext)
            except (OSError, WireError) as exc:
                raise DataPlaneError(f"数据面发送失败: {exc}") from exc
        self.sent_frames += 1
        self.sent_payload_bytes += len(payload)
        # send_packet 固定 4 字节大端长度前缀; AEAD 标签已计入 ciphertext
        self.sent_wire_bytes += 4 + len(ciphertext)
        return time.time_ns()

    def recv_raw(self) -> bytes:
        """接收并解密一段原始明文, 不做 JSON 解析."""
        try:
            body = recv_packet(self.sock)
        except (OSError, WireError) as exc:
            raise DataPlaneError(f"数据面接收失败: {exc}") from exc
        if body is None:
            raise DataPlaneError("AEAD 通道被对端关闭")
        try:
            plaintext = self.session.decrypt(body)
        except ValueError as exc:
            raise DataPlaneError(f"AEAD 解密失败: {exc}") from exc
        self.recv_frames += 1
        self.recv_payload_bytes += len(plaintext)
        self.recv_wire_bytes += 4 + len(body)
        return plaintext

    def recv(self) -> dict[str, Any]:
        """接收并解密一条 JSON."""
        plaintext = self.recv_raw()
        try:
            value = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataPlaneError(f"AEAD 明文不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise DataPlaneError("AEAD 明文必须是 JSON 对象")
        return value

    def close(self) -> None:
        """关闭底层连接, 让阻塞中的读线程退出; 可重复调用."""
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()
