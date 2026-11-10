# ==========================================================================================
# 文件        : verify_relay_a5_a6.py
# 所属目录    : scripts/
# 文件用途    : 独立交叉验证 A5 信令重放与 A6 屏幕墙越权
# 架构定位    : 验收取证脚本, 自写客户端, 不复用被验方的任何客户端或部署脚本
# 核心功能    : 公网 8750 重放计数; 公网墙操作探测; 本机回环越权对照组
# 职责边界    : 只读探测; 不改 src/android/docs/contracts; 不改远端服务器与配置; 不重启
# ==========================================================================================

"""独立最小客户端: A5 重放与 A6 越权交叉验证.

用法:
    python scripts/verify_relay_a5_a6.py
    python scripts/verify_relay_a5_a6.py --host 47.109.32.249 --port 8750 --sni relay.wuge.fun

三段:
    A5 公网重放: 两台已认证设备建立会话后, 把同一份 session_start 原字节连发三次;
       期望对端只收到一次, 第 2/3 次被丢弃; 再用新 id 的同一载荷发一次, 证明路由未被打死.
    A6 公网探测: alice 与 bob 分别对自己的/他人的 wall_id 做预览、聚焦、释放;
       公网进程没有墙归属登记面, 预期全部返回 SCOPE_INSUFFICIENT "墙不存在"; 该结果不能当作越权已修.
    A6 本机对照: 回环起一个未改动的 InMemorySignalingServer, 显式登记 wall-alice 归属 acct-a;
       alice 预览自己的墙必须成功, bob 三类操作必须被拒且不得释放 alice 的聚焦令牌.

取证: evidence/relay-a5-a6-<时间戳>/{tls.json,a5.json,a6.json,transcript.json,结论.md}
退出码: 0 表示 A5 通过且本机对照通过; 1 表示判定不成立; 2 表示公网不可达(TLS/连接受阻).
"""

from __future__ import annotations

import argparse
import base64
import functools
import hashlib
import json
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
CA_FILE = ROOT / "deploy" / "relay" / "ca.crt"
TZ = timezone(timedelta(hours=8))
MAX_FRAME = 4 * 1024 * 1024
REDACTED = "<redacted>"
SECRET_KEYS = {"relay_token", "token", "preview_token", "focus_token", "signature"}


class Client:
    """一条连接上的最小协议客户端; 自生成 Ed25519 身份, 不依赖被验方代码."""

    def __init__(
        self,
        sock: socket.socket,
        label: str,
        private_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self.sock = sock
        self.label = label
        self.private_key = private_key or Ed25519PrivateKey.generate()
        self.public_raw = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        digest = hashlib.sha256(self.public_raw).digest()
        self.device_id = base64.b32encode(digest).decode("ascii").rstrip("=")[:20]
        self.transcript: list[dict[str, Any]] = []

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode("ascii")

    def _sign(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return base64.b64encode(self.private_key.sign(canonical.encode("utf-8"))).decode("ascii")

    def envelope(
        self,
        msg_type: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "type": msg_type,
            "id": message_id or str(uuid.uuid4()),
            "session_id": session_id,
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "payload": payload,
        }
        body["sig"] = self._sign(body)
        return body

    def send(self, msg: dict[str, Any]) -> None:
        self.send_raw(serialize(msg), msg)

    def send_raw(self, raw: bytes, msg: dict[str, Any]) -> None:
        self.transcript.append({"direction": "send", "at": _now(), "message": _redact(msg)})
        self.sock.sendall(struct.pack(">I", len(raw)) + raw)

    def recv(self, timeout: float = 8.0) -> dict[str, Any]:
        self.sock.settimeout(timeout)
        header = _recv_exactly(self.sock, 4)
        if header is None:
            raise ConnectionError(f"{self.label} 连接被关闭")
        (length,) = struct.unpack(">I", header)
        if length == 0 or length > MAX_FRAME:
            raise ValueError(f"{self.label} 帧长度非法: {length}")
        body = _recv_exactly(self.sock, length)
        if body is None:
            raise ConnectionError(f"{self.label} 帧不完整")
        message = json.loads(body.decode("utf-8"))
        self.transcript.append({"direction": "recv", "at": _now(), "message": _redact(message)})
        return message

    def recv_or_none(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.recv(timeout)
        except TimeoutError:
            self.transcript.append({"direction": "recv", "at": _now(), "message": {"type": "no_response"}})
            return None


def serialize(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def _redact(message: Any) -> Any:
    """令牌与签名只保留存在性与长度; 其余字段保持原始响应形态."""
    if isinstance(message, dict):
        result = {}
        for key, value in message.items():
            if key in SECRET_KEYS and isinstance(value, str):
                result[key] = f"{REDACTED}:{len(value)}"
            else:
                result[key] = _redact(value)
        return result
    if isinstance(message, list):
        return [_redact(item) for item in message]
    return message


def _tls_metadata(sock: ssl.SSLSocket) -> dict[str, Any]:
    cert = sock.getpeercert() or {}
    der = sock.getpeercert(binary_form=True) or b""
    subject = {key: value for part in cert.get("subject", ()) for key, value in part}
    issuer = {key: value for part in cert.get("issuer", ()) for key, value in part}
    cipher = sock.cipher()
    return {
        "tls_version": sock.version(),
        "cipher": {"name": cipher[0], "protocol": cipher[1], "bits": cipher[2]} if cipher else None,
        "verify_mode": "CERT_REQUIRED + check_hostname",
        "ca_file": str(CA_FILE),
        "ca_sha256": hashlib.sha256(CA_FILE.read_bytes()).hexdigest(),
        "peer_cert_sha256": hashlib.sha256(der).hexdigest(),
        "peer_subject": subject,
        "peer_issuer": issuer,
        "peer_not_after": cert.get("notAfter"),
        "sni": sock.server_hostname,
    }


def connect_tls(host: str, port: int, sni: str, timeout: float) -> ssl.SSLSocket:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(CA_FILE))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    raw = socket.create_connection((host, port), timeout=timeout)
    return context.wrap_socket(raw, server_hostname=sni)


def connect_plain(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def probe_transport(host: str, port: int, sni: str, timeout: float) -> dict[str, Any]:
    """先试 TLS, 再试明文; 只发一帧非法 JSON, 不改变任何鉴权状态."""
    result: dict[str, Any] = {"tls": {}, "tls_error": "", "plaintext": {}}
    try:
        sock = connect_tls(host, port, sni, timeout)
        result["tls"] = _tls_metadata(sock)
        sock.close()
        return result
    except (OSError, ssl.SSLError) as error:
        result["tls_error"] = f"{type(error).__name__}: {error}"
    try:
        sock = connect_plain(host, port, timeout)
        sock.sendall(struct.pack(">I", 1) + b"{")
        sock.settimeout(5.0)
        header = _recv_exactly(sock, 4)
        if header is None:
            result["plaintext"] = {"ok": False, "detail": "无响应"}
            sock.close()
            return result
        (length,) = struct.unpack(">I", header)
        body = _recv_exactly(sock, length)
        sock.close()
        message = json.loads((body or b"{}").decode("utf-8"))
        result["plaintext"] = {
            "ok": True,
            "response_type": message.get("type"),
            "error": _error(message),
            "frame_bytes": length,
        }
    except (OSError, ValueError) as error:
        result["plaintext"] = {"ok": False, "detail": f"{type(error).__name__}: {error}"}
    return result


def _error(message: dict[str, Any] | None) -> dict[str, Any]:
    if not message or message.get("type") != "error":
        return {}
    payload = message.get("payload") or {}
    return {"code": payload.get("code"), "message": payload.get("message")}


def register_and_auth(client: Client) -> dict[str, Any]:
    client.send(
        client.envelope(
            "register",
            {
                "device_id": client.device_id,
                "public_key": client.public_b64,
                "capabilities": ["video", "input"],
                "protocol_version": 1,
                "display_name": f"audit-{client.label}",
            },
        )
    )
    challenge = client.recv()
    nonce = str((challenge.get("payload") or {}).get("nonce") or "")
    client.send(
        client.envelope(
            "auth",
            {"device_id": client.device_id, "signature": client._sign({"device_id": client.device_id, "nonce": nonce})},
        )
    )
    auth_ok = client.recv()
    return {"challenge": challenge.get("type"), "auth": auth_ok.get("type")}


class _Reader(threading.Thread):
    """对端接收计数器: 独立线程持续读帧, 记录类型与 id."""

    def __init__(self, client: Client) -> None:
        super().__init__(daemon=True)
        self.client = client
        self.received: list[dict[str, Any]] = []
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self.client.recv(0.3)
            except TimeoutError:
                continue
            except (OSError, ConnectionError, ValueError):
                return
            self.received.append({"type": message.get("type"), "id": message.get("id"), "at": _now()})

    def stop(self) -> list[dict[str, Any]]:
        time.sleep(0.6)
        self._stop_event.set()
        self.join(timeout=2.0)
        return self.received


# ------------------------------------------------------------------ A5 公网重放
def public_a5(connect, timeout: float, replay_wait: float = 0.0) -> dict[str, Any]:
    host_client = Client(connect(), "host")
    host_auth = register_and_auth(host_client)
    controller = Client(connect(), "controller")
    controller_auth = register_and_auth(controller)
    session_id = f"audit-a5-{uuid.uuid4()}"
    try:
        controller.send(
            controller.envelope(
                "offer",
                {
                    "session_id": session_id,
                    "candidates": [],
                    "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                    "access_proof": base64.b64encode(b"audit-only").decode("ascii"),
                    "from_device_id": controller.device_id,
                    "to_device_id": host_client.device_id,
                },
                session_id=session_id,
            )
        )
        offer = host_client.recv(timeout)

        start = controller.envelope(
            "session_start",
            {
                "session_id": session_id,
                "transport_kind": "relay",
                "quality_profile": "L3",
                "from_device_id": controller.device_id,
                "to_device_id": host_client.device_id,
            },
            session_id=session_id,
        )
        raw = serialize(start)
        reader = _Reader(host_client)
        reader.start()

        responses: list[dict[str, Any]] = []
        for _ in range(3):
            controller.send_raw(raw, start)
            response = controller.recv_or_none(1.2)
            responses.append({"type": response.get("type") if response else "no_response", "error": _error(response)})
        time.sleep(1.2)
        first_count = sum(1 for item in reader.received if item.get("id") == start["id"])

        # 负向对照: 同一载荷换新 id, 路由必须仍然可用
        fresh = controller.envelope(
            "session_start",
            {
                "session_id": session_id,
                "transport_kind": "relay",
                "quality_profile": "L3",
                "from_device_id": controller.device_id,
                "to_device_id": host_client.device_id,
            },
            session_id=session_id,
        )
        controller.send(fresh)
        fresh_response = controller.recv_or_none(1.2)
        time.sleep(1.2)
        receipts = reader.stop()
        fresh_count = sum(1 for item in receipts if item.get("id") == fresh["id"])

        controller.send(
            controller.envelope(
                "session_end",
                {
                    "session_id": session_id,
                    "reason": "审计后立即释放",
                    "from_device_id": controller.device_id,
                    "to_device_id": host_client.device_id,
                },
                session_id=session_id,
            )
        )
        end = host_client.recv(timeout)
        first_sent_at = time.time()
        host_identity = host_client.private_key
        controller_identity = controller.private_key
        transcript = list(controller.transcript) + list(host_client.transcript)

        # 阶段二: 断开重连后同一身份重发同一 id, 验证去重窗口内行为
        controller.sock.close()
        host_client.sock.close()
        time.sleep(0.7)
        host2 = Client(connect(), "host", host_identity)
        register_and_auth(host2)
        controller2 = Client(connect(), "controller", controller_identity)
        register_and_auth(controller2)
        session_id2 = f"audit-a5-reconnect-{uuid.uuid4()}"
        offer2 = _offer(controller2, host2, session_id2, timeout)
        reader2 = _Reader(host2)
        reader2.start()
        replay_fresh_ts = controller2.envelope(
            "session_start",
            _start_payload(session_id2, controller2.device_id, host2.device_id),
            session_id=session_id2,
            message_id=start["id"],
        )
        controller2.send(replay_fresh_ts)
        raw_replay_response = controller2.recv_or_none(1.2)
        time.sleep(1.2)
        window_in_count = sum(1 for item in reader2.received if item.get("id") == start["id"])
        # 原字节重放: 保留首次 ts, 在 300 秒包络窗口内同样应被去重
        controller2.send_raw(raw, start)
        raw_replay_response2 = controller2.recv_or_none(1.2)
        time.sleep(1.0)
        window_in_raw_count = sum(1 for item in reader2.received if item.get("id") == start["id"])
        reader2.stop()
        controller2.send(
            controller2.envelope(
                "session_end",
                {
                    "session_id": session_id2,
                    "reason": "窗口内重放取证后释放",
                    "from_device_id": controller2.device_id,
                    "to_device_id": host2.device_id,
                },
                session_id=session_id2,
            )
        )
        end2 = host2.recv(timeout)
        transcript += controller2.transcript + host2.transcript

        # 阶段三: 等待超过 300 秒去重窗口, 同一 id 换新 ts 应重新送达
        window_out_count: int | None = None
        window_out_elapsed = 0.0
        end3: dict[str, Any] = {}
        if replay_wait > 0:
            remaining = first_sent_at + replay_wait - time.time()
            if remaining > 0:
                time.sleep(remaining)
            window_out_elapsed = round(time.time() - first_sent_at, 1)
            controller2.sock.close()
            host2.sock.close()
            time.sleep(0.7)
            host3 = Client(connect(), "host", host_identity)
            register_and_auth(host3)
            controller3 = Client(connect(), "controller", controller_identity)
            register_and_auth(controller3)
            session_id3 = f"audit-a5-expired-{uuid.uuid4()}"
            _offer(controller3, host3, session_id3, timeout)
            reader3 = _Reader(host3)
            reader3.start()
            expired_replay = controller3.envelope(
                "session_start",
                _start_payload(session_id3, controller3.device_id, host3.device_id),
                session_id=session_id3,
                message_id=start["id"],
            )
            controller3.send(expired_replay)
            expired_response = controller3.recv_or_none(1.2)
            time.sleep(1.2)
            window_out_count = sum(1 for item in reader3.received if item.get("id") == start["id"])
            reader3.stop()
            controller3.send(
                controller3.envelope(
                    "session_end",
                    {
                        "session_id": session_id3,
                        "reason": "窗口外重放取证后释放",
                        "from_device_id": controller3.device_id,
                        "to_device_id": host3.device_id,
                    },
                    session_id=session_id3,
                )
            )
            end3 = host3.recv(timeout)
            transcript += controller3.transcript + host3.transcript
            controller3.sock.close()
            host3.sock.close()

        window_in_ok = window_in_count == 0 and window_in_raw_count == 0
        window_out_ok = window_out_count is None or window_out_count == 1
        passed = first_count == 1 and fresh_count == 1 and window_in_ok and window_out_ok
        return {
            "host": {"device_id": host_client.device_id, "auth": host_auth},
            "controller": {"device_id": controller.device_id, "auth": controller_auth},
            "session_id": session_id,
            "offer_delivery": offer.get("type"),
            "replay_id": start["id"],
            "replay_send_count": 3,
            "replay_received_count": first_count,
            "replay_responses": responses,
            "fresh_id": fresh["id"],
            "fresh_received_count": fresh_count,
            "fresh_response": {
                "type": fresh_response.get("type") if fresh_response else "no_response",
                "error": _error(fresh_response),
            },
            "peer_receipts": receipts,
            "release": end.get("type"),
            "reconnect_offer": offer2.get("type"),
            "window_in_received": window_in_count,
            "window_in_raw_received": window_in_raw_count,
            "window_in_responses": [
                raw_replay_response.get("type") if raw_replay_response else "no_response",
                raw_replay_response2.get("type") if raw_replay_response2 else "no_response",
            ],
            "window_in_release": end2.get("type"),
            "replay_wait_s": replay_wait,
            "window_out_elapsed_s": window_out_elapsed,
            "window_out_received": window_out_count,
            "window_out_response": (
                expired_response.get("type") if window_out_count is not None and expired_response else "no_response"
            ),
            "window_out_release": end3.get("type"),
            "passed": passed,
            "transcript": transcript,
        }
    finally:
        controller.sock.close()
        host_client.sock.close()


def _start_payload(session_id: str, controller_id: str, host_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "transport_kind": "relay",
        "quality_profile": "L3",
        "from_device_id": controller_id,
        "to_device_id": host_id,
    }


def _offer(controller: Client, host: Client, session_id: str, timeout: float) -> dict[str, Any]:
    controller.send(
        controller.envelope(
            "offer",
            {
                "session_id": session_id,
                "candidates": [],
                "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                "access_proof": base64.b64encode(b"audit-only").decode("ascii"),
                "from_device_id": controller.device_id,
                "to_device_id": host.device_id,
            },
            session_id=session_id,
        )
    )
    return host.recv(timeout)


# ------------------------------------------------------------------ A6 公网探测
def _seeded_client(connect, label: str, entry: dict[str, Any]) -> Client:
    """用已登记账号的设备私钥接入; 账号绑定必须存在于服务端账号库, 客户端不自报账号."""
    seed = base64.b64decode(str(entry["private_key"]))
    return Client(connect(), label, Ed25519PrivateKey.from_private_bytes(seed))


def load_state(path: str) -> dict[str, Any]:
    """读取已播种设备状态文件(a/b 私钥); 文件缺失或字段不全直接报错, 不静默退回新设备."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("a", "b"):
        entry = payload.get(key) or {}
        if not entry.get("private_key"):
            raise ValueError(f"状态文件缺少 {key}.private_key")
    return payload


def public_a6(
    connect,
    timeout: float,
    seeded: dict[str, Any] | None = None,
    wall_id: str | None = None,
) -> dict[str, Any]:
    """先建立正向对照, 再测越权.

    顺序: bob 发起 offer -> alice 以被控端 answer 建立会话 -> alice 用 wall_bind 登记
    wall_id 归属 -> alice 预览/聚焦自己的墙必须成功 -> 之后才允许 bob 预览/聚焦/释放.
    只有正向对照成立时, bob 的拒绝才能说明归属校验生效, 否则一律记「不可证」.
    """
    if seeded is not None:
        alice = _seeded_client(connect, "alice", seeded["a"])
        bob = _seeded_client(connect, "bob", seeded["b"])
    else:
        alice = Client(connect(), "alice")
        bob = Client(connect(), "bob")
    alice_auth = register_and_auth(alice)
    bob_auth = register_and_auth(bob)
    wall_id = wall_id or f"audit-wall-{uuid.uuid4().hex[:8]}"
    session_id = f"audit-a6-{uuid.uuid4()}"
    cases: list[dict[str, Any]] = []
    bind: dict[str, Any] = {"attempted": False}
    released_by_alice = False
    focus_token_present = False
    answer_delivery = ""
    step = "send_offer"

    def _partial() -> dict[str, Any]:
        return {
            "alice": {"device_id": alice.device_id, "auth": alice_auth},
            "bob": {"device_id": bob.device_id, "auth": bob_auth},
            "wall_id": wall_id,
            "session_id": session_id,
            "bind": bind,
            "seeded_identity": seeded is not None,
            "cases": cases,
            "positive_control_ok": False,
            "bob_all_denied": False,
            "deny_reason_is_ownership": False,
            "alice_token_still_valid": released_by_alice,
            "passed": False,
            "all_rejected": all(item["response_type"] == "error" for item in cases),
            "transcript": alice.transcript + bob.transcript,
        }

    try:
        bind["attempted"] = True
        # 1) 建立以 alice 为被控端的会话: 只有该会话 host 才能登记墙归属
        bob.send(
            bob.envelope(
                "offer",
                {
                    "session_id": session_id,
                    "candidates": [],
                    "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                    "access_proof": base64.b64encode(b"audit-only").decode("ascii"),
                    "from_device_id": bob.device_id,
                    "to_device_id": alice.device_id,
                },
                session_id=session_id,
            )
        )
        step = "offer_delivery"
        offer = alice.recv(timeout)
        alice.send(
            alice.envelope(
                "answer",
                {
                    "session_id": session_id,
                    "chosen_candidate": None,
                    "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                    "from_device_id": alice.device_id,
                    "to_device_id": bob.device_id,
                },
                session_id=session_id,
            )
        )
        answer_error = alice.recv_or_none(0.6)
        if answer_error is not None:
            bind["answer_error"] = _error(answer_error) or {"type": answer_error.get("type")}
        step = "answer_delivery"
        answer = bob.recv(timeout)
        answer_delivery = answer.get("type", "")
        bind["session_ready"] = offer.get("type") == "offer" and answer_delivery == "answer"

        # 2) 正向对照第一步: 被控端 alice 合法登记这块墙的归属
        step = "wall_bind"
        alice.send(
            alice.envelope(
                "wall_bind",
                {"wall_id": wall_id, "session_id": session_id},
                session_id=session_id,
            )
        )
        bound = alice.recv_or_none(timeout)
        bind["response_type"] = bound.get("type") if bound else "no_response"
        bind["error"] = _error(bound)
        bound_payload = (bound or {}).get("payload") or {}
        bind["account_present"] = bool(bound_payload.get("account_id"))
        bind["bound_at"] = bound_payload.get("bound_at")
        bind["ok"] = bind["response_type"] == "wall_bound"

        # 3) alice 先持有自己的聚焦令牌, 供 bob 越权尝试后验证令牌仍有效
        step = "alice_focus_own"
        alice.send(alice.envelope("wall_focus", {"wall_id": wall_id, "cell": 1}, session_id=session_id))
        alice_focus = alice.recv(timeout)
        focus_token_present = bool(((alice_focus.get("payload") or {}).get("focus_token")) or "")
        cases.append(
            {
                "case": "alice_focus_own",
                "msg_type": "wall_focus",
                "wall_id": wall_id,
                "response_type": alice_focus.get("type"),
                "error": _error(alice_focus),
                "token_present": focus_token_present,
            }
        )

        # 4) 正向对照第二步: alice 预览自己的墙
        step = "alice_preview_own"
        alice.send(alice.envelope("wall_preview", {"wall_id": wall_id, "cell": 0}, session_id=session_id))
        own_preview = alice.recv(timeout)
        cases.insert(
            0,
            {
                "case": "alice_preview_own",
                "msg_type": "wall_preview",
                "wall_id": wall_id,
                "response_type": own_preview.get("type"),
                "error": _error(own_preview),
                "token_present": bool(((own_preview.get("payload") or {}).get("preview_token")) or ""),
            },
        )

        # 5) 反向: alice 预览未登记的墙, bob 预览/聚焦/释放 alice 的墙
        for label, client, msg_type, payload in [
            ("alice_preview_foreign", alice, "wall_preview", {"wall_id": "other-users-wall", "cell": 0}),
            ("bob_preview_foreign", bob, "wall_preview", {"wall_id": wall_id, "cell": 0}),
            ("bob_focus_foreign", bob, "wall_focus", {"wall_id": wall_id, "cell": 2}),
            ("bob_unfocus_foreign", bob, "wall_unfocus", {"wall_id": wall_id}),
        ]:
            step = label
            client.send(client.envelope(msg_type, payload, session_id=session_id))
            response = client.recv(timeout)
            cases.append(
                {
                    "case": label,
                    "msg_type": msg_type,
                    "wall_id": payload["wall_id"],
                    "response_type": response.get("type"),
                    "error": _error(response),
                }
            )

        # 6) bob 尝试后 alice 仍能释放自己的聚焦, 证明其令牌未被越权操作影响
        step = "alice_unfocus_own"
        alice.send(alice.envelope("wall_unfocus", {"wall_id": wall_id}, session_id=session_id))
        own_unfocus = alice.recv(timeout)
        released_by_alice = bool((own_unfocus.get("payload") or {}).get("released"))
        cases.append(
            {
                "case": "alice_unfocus_own",
                "msg_type": "wall_unfocus",
                "wall_id": wall_id,
                "response_type": own_unfocus.get("type"),
                "error": _error(own_unfocus),
                "released": released_by_alice,
            }
        )

        bob_cases = [item for item in cases if item["case"].startswith("bob_")]
        bob_all_denied = all(
            item["response_type"] == "error" and item["error"].get("code") == "SCOPE_INSUFFICIENT" for item in bob_cases
        )
        ownership_denied = all("无权访问该屏幕墙" in (item["error"].get("message") or "") for item in bob_cases)
        own_preview_ok = next(item for item in cases if item["case"] == "alice_preview_own")["response_type"] == (
            "wall_preview_granted"
        )
        positive_ok = bool(bind["ok"] and own_preview_ok and focus_token_present)
        passed = positive_ok and bob_all_denied and ownership_denied and released_by_alice
        return {
            "alice": {"device_id": alice.device_id, "auth": alice_auth},
            "bob": {"device_id": bob.device_id, "auth": bob_auth},
            "wall_id": wall_id,
            "session_id": session_id,
            "bind": bind,
            "seeded_identity": seeded is not None,
            "cases": cases,
            "positive_control_ok": positive_ok,
            "bob_all_denied": bob_all_denied,
            "deny_reason_is_ownership": ownership_denied,
            "alice_token_still_valid": released_by_alice,
            "passed": passed,
            "all_rejected": all(item["response_type"] == "error" for item in cases),
            "transcript": alice.transcript + bob.transcript,
        }
    except TimeoutError as error:
        bind["failed_step"] = step
        bind["timeout_detail"] = f"{type(error).__name__}: {error}"
        return _partial()
    finally:
        try:
            bob.send(
                bob.envelope(
                    "session_end",
                    {
                        "session_id": session_id,
                        "reason": "审计后释放会话",
                        "from_device_id": bob.device_id,
                        "to_device_id": alice.device_id,
                    },
                    session_id=session_id,
                )
            )
            alice.recv_or_none(timeout)
        except (OSError, ssl.SSLError):
            pass
        alice.sock.close()
        bob.sock.close()


# ------------------------------------------------------------------ A6 本机回环对照
class _LoopbackServer:
    """未改动的 InMemorySignalingServer + 最小帧接入; 仅本机回环, 供对照组取证."""

    def __init__(self, accounts: dict[str, str], walls: dict[str, str]) -> None:
        from rdc.signaling.server import InMemorySignalingServer

        def bind_wall(wall_id: str, account_id: str, device_id: str) -> tuple[str, str]:
            """本机替代落库: 归属写进程内表, 但登记路径与线上一致, 走 wall_bind 消息."""
            status = "unchanged" if walls.get(wall_id) == account_id else "created"
            walls[wall_id] = account_id
            return status, datetime.now(TZ).isoformat(timespec="seconds")

        self.sockets: dict[str, socket.socket] = {}
        self.server = InMemorySignalingServer(
            deliver=self._deliver,
            account_of=lambda device_id: accounts.get(device_id, ""),
            wall_owner_of=lambda wall_id: walls.get(wall_id),
            wall_binder=bind_wall,
        )
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _deliver(self, device_id: str, envelope: dict[str, Any]) -> bool:
        sock = self.sockets.get(device_id)
        if sock is None:
            return False
        try:
            sock.sendall(struct.pack(">I", len(serialize(envelope))) + serialize(envelope))
        except OSError:
            return False
        return True

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.3)
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve_connection, args=(connection,), daemon=True).start()

    def _serve_connection(self, connection: socket.socket) -> None:
        device_id = ""
        try:
            while not self._stop.is_set():
                header = _recv_exactly(connection, 4)
                if header is None:
                    return
                (length,) = struct.unpack(">I", header)
                body = _recv_exactly(connection, length)
                if body is None:
                    return
                envelope = json.loads(body.decode("utf-8"))
                responses = self.server.handle(envelope, device_id or None)
                if envelope.get("type") == "auth" and any(item.get("type") == "auth_ok" for item in responses):
                    device_id = str((envelope.get("payload") or {}).get("device_id") or "")
                    self.sockets[device_id] = connection
                for response in responses:
                    connection.sendall(struct.pack(">I", len(serialize(response))) + serialize(response))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        finally:
            if device_id:
                self.sockets.pop(device_id, None)
            with _suppress_oserror():
                connection.close()

    def close(self) -> None:
        self._stop.set()
        with _suppress_oserror():
            self._listener.close()


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def local_a6(timeout: float) -> dict[str, Any]:
    """本机回环: 用真实协议消息 wall_bind 登记归属, 再验证正向与越权.

    与线上差异只有落库实现(进程内表替代 accounts.db), 消息路径与鉴权/归属校验代码均未改动.
    """
    accounts: dict[str, str] = {}
    walls: dict[str, str] = {}
    server = _LoopbackServer(accounts, walls)
    alice = Client(socket.create_connection(("127.0.0.1", server.port), timeout=timeout), "alice")
    bob = Client(socket.create_connection(("127.0.0.1", server.port), timeout=timeout), "bob")
    accounts[alice.device_id] = "acct-a"
    accounts[bob.device_id] = "acct-b"
    alice_auth = register_and_auth(alice)
    bob_auth = register_and_auth(bob)
    wall_id = "wall-alice"
    session_id = "local-a6"
    cases: list[dict[str, Any]] = []
    focus_token = ""
    preview_token = ""
    released_by_alice = False
    try:
        # 1) 建立会话: bob 发起 offer, alice 以被控端 answer; 只有 host 可登记归属
        bob.send(
            bob.envelope(
                "offer",
                {
                    "session_id": session_id,
                    "candidates": [],
                    "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                    "access_proof": base64.b64encode(b"local-only").decode("ascii"),
                    "from_device_id": bob.device_id,
                    "to_device_id": alice.device_id,
                },
                session_id=session_id,
            )
        )
        offer = alice.recv(timeout)
        alice.send(
            alice.envelope(
                "answer",
                {
                    "session_id": session_id,
                    "chosen_candidate": None,
                    "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                    "from_device_id": alice.device_id,
                    "to_device_id": bob.device_id,
                },
                session_id=session_id,
            )
        )
        answer = bob.recv(timeout)

        # 2) 正向对照第一步: 被控端 alice 用 wall_bind 登记归属
        alice.send(alice.envelope("wall_bind", {"wall_id": wall_id, "session_id": session_id}, session_id=session_id))
        bound = alice.recv(timeout)
        cases.append(
            {
                "case": "alice_wall_bind",
                "response_type": bound.get("type"),
                "error": _error(bound),
                "account_present": bool((bound.get("payload") or {}).get("account_id")),
                "bound_at": (bound.get("payload") or {}).get("bound_at"),
            }
        )

        # 3) 正向对照第二步: alice 预览自己的墙
        alice.send(alice.envelope("wall_preview", {"wall_id": wall_id, "cell": 0}, session_id=session_id))
        preview = alice.recv(timeout)
        preview_token = str((preview.get("payload") or {}).get("preview_token") or "")
        cases.append(
            {
                "case": "alice_preview_own",
                "response_type": preview.get("type"),
                "error": _error(preview),
                "token_present": bool(preview_token),
            }
        )

        # 4) 反向: bob 预览 alice 的墙
        bob.send(bob.envelope("wall_preview", {"wall_id": wall_id, "cell": 0}, session_id=session_id))
        bob_preview = bob.recv(timeout)
        cases.append(
            {
                "case": "bob_preview_foreign",
                "response_type": bob_preview.get("type"),
                "error": _error(bob_preview),
            }
        )

        # 5) alice 取得聚焦令牌
        alice.send(alice.envelope("wall_focus", {"wall_id": wall_id, "cell": 1}, session_id=session_id))
        alice_focus = alice.recv(timeout)
        focus_token = str((alice_focus.get("payload") or {}).get("focus_token") or "")
        cases.append(
            {
                "case": "alice_focus_own",
                "response_type": alice_focus.get("type"),
                "error": _error(alice_focus),
                "token_present": bool(focus_token),
            }
        )

        # 6) bob 越权聚焦与释放
        bob.send(bob.envelope("wall_focus", {"wall_id": wall_id, "cell": 2}, session_id=session_id))
        bob_focus = bob.recv(timeout)
        cases.append({"case": "bob_focus_foreign", "response_type": bob_focus.get("type"), "error": _error(bob_focus)})

        bob.send(bob.envelope("wall_unfocus", {"wall_id": wall_id}, session_id=session_id))
        bob_unfocus = bob.recv(timeout)
        cases.append(
            {"case": "bob_unfocus_foreign", "response_type": bob_unfocus.get("type"), "error": _error(bob_unfocus)}
        )

        # 7) bob 尝试后 alice 的令牌仍有效, 且能自行释放
        focus_valid_after_bob = server.server.resolve_relay_token(focus_token) is not None
        preview_valid_after_bob = server.server.resolve_relay_token(preview_token) is not None
        alice.send(alice.envelope("wall_unfocus", {"wall_id": wall_id}, session_id=session_id))
        alice_unfocus = alice.recv(timeout)
        released_by_alice = bool((alice_unfocus.get("payload") or {}).get("released"))
        cases.append(
            {
                "case": "alice_unfocus_own",
                "response_type": alice_unfocus.get("type"),
                "error": _error(alice_unfocus),
                "released": (alice_unfocus.get("payload") or {}).get("released"),
            }
        )

        bob_cases = [item for item in cases if item["case"].startswith("bob_")]
        bob_all_denied = all(
            item["response_type"] == "error" and item["error"].get("code") == "SCOPE_INSUFFICIENT" for item in bob_cases
        )
        owner_messages = {item["error"].get("message") for item in bob_cases}
        bind_ok = cases[0]["response_type"] == "wall_bound" and walls.get(wall_id) == "acct-a"
        passed = (
            bind_ok
            and cases[1]["response_type"] == "wall_preview_granted"
            and bob_all_denied
            and all("无权访问该屏幕墙" in (message or "") for message in owner_messages)
            and focus_valid_after_bob
            and preview_valid_after_bob
            and released_by_alice
        )
        return {
            "alice": {"device_id": alice.device_id, "auth": alice_auth},
            "bob": {"device_id": bob.device_id, "auth": bob_auth},
            "wall_id": wall_id,
            "owner_account": "acct-a",
            "bob_account": "acct-b",
            "session_id": session_id,
            "offer_delivery": offer.get("type"),
            "answer_delivery": answer.get("type"),
            "cases": cases,
            "bind_ok": bind_ok,
            "bob_all_denied": bob_all_denied,
            "deny_reason_is_ownership": all("无权访问该屏幕墙" in (message or "") for message in owner_messages),
            "alice_focus_token_valid_after_bob": focus_valid_after_bob,
            "alice_preview_token_valid_after_bob": preview_valid_after_bob,
            "alice_unfocus_released": released_by_alice,
            "passed": passed,
            "transcript": alice.transcript + bob.transcript,
        }
    finally:
        alice.sock.close()
        bob.sock.close()
        server.close()


# ------------------------------------------------------------------ 取证
def _write_evidence(payload: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EVIDENCE / f"relay-a5-a6-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "tls.json").write_text(json.dumps(payload["tls"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "transport.json").write_text(
        json.dumps(payload["transport"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "a5.json").write_text(json.dumps(payload["a5"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "a6.json").write_text(
        json.dumps(
            {"public": payload["a6_public"], "loopback": payload["a6_local"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (target / "transcript.json").write_text(
        json.dumps(payload["transcript"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "结论.md").write_text(_report(payload), encoding="utf-8")
    return target


def _report(payload: dict[str, Any]) -> str:
    a5 = payload["a5"]
    public = payload["a6_public"]
    local = payload["a6_local"]
    tls = payload["tls"]
    subject = tls.get("peer_subject") or {}
    if a5.get("passed") and local.get("passed") and public.get("passed"):
        overall = "通过" if tls else "逻辑通过, 但公网 8750 当前为明文(见传输层)"
    elif a5.get("passed") and local.get("passed") and not public.get("cases"):
        overall = "受阻: 公网不可达, A6 公网未执行"
    elif a5.get("passed") and local.get("passed"):
        overall = "部分通过: A5 与本机对照成立, A6 公网" + (
            "正向对照已成立但越权拒绝不达标" if public.get("positive_control_ok") else "正向对照未成立, 不可证"
        )
        if not tls:
            overall += "; 且本次公网帧为明文"
    elif payload["blocked"]:
        overall = "受阻"
    else:
        overall = "不通过"
    lines = [
        "# A5 重放 / A6 越权 独立交叉验证",
        "",
        f"- 采集时间: {payload['started_at']} 至 {payload['finished_at']} (Asia/Shanghai)",
        f"- 公网目标: {payload['host']}:{payload['port']} (SNI {payload['sni']})",
        "- 客户端: scripts/verify_relay_a5_a6.py 自实现最小客户端, 未复用 relay_public_bench 或部署脚本",
    ]
    if tls:
        lines.extend(
            [
                f"- TLS: {tls.get('tls_version')} / {(tls.get('cipher') or {}).get('name')} / "
                f"校验方式 {tls.get('verify_mode')}",
                f"- 证书: subject={subject.get('commonName')} "
                f"issuer={(tls.get('peer_issuer') or {}).get('commonName')} "
                f"SHA-256={tls.get('peer_cert_sha256')}",
                f"- 根 CA: {tls.get('ca_sha256')}",
            ]
        )
    else:
        lines.append("- TLS: 不可用, 本次帧为明文 TCP, 无证书校验")
    lines.extend(
        [
            f"- 传输层实测: TLS 可用={bool(tls)}, "
            f"TLS 失败原因={payload.get('transport', {}).get('tls_error') or '无'}, "
            f"明文可用={(payload.get('transport', {}).get('plaintext') or {}).get('ok')}",
            "",
        ]
    )
    if a5.get("session_id"):
        lines.extend(
            [
                "## A5 重放",
                "",
                f"- 会话: {a5['session_id']}",
                f"- 同一 session_start 原字节发送次数: {a5['replay_send_count']}",
                f"- 对端实际收到次数: {a5['replay_received_count']}",
                f"- 控制端在第 1/2/3 次后的响应: {json.dumps(a5['replay_responses'], ensure_ascii=False)}",
                f"- 换新 id 同载荷对照: 收到 {a5['fresh_received_count']} 次, 响应 {a5['fresh_response'].get('type')}",
                f"- 释放: {a5['release']}",
                f"- 断线重连后同 id 窗口内送达次数: {a5['window_in_received']}"
                f"(原字节重放后累计 {a5['window_in_raw_received']})",
                f"- 跨窗口({a5['replay_wait_s']}s, 实测 {a5['window_out_elapsed_s']}s)同 id 送达次数: "
                f"{a5['window_out_received']}"
                if a5.get("replay_wait_s")
                else "- 跨窗口: 本次未启用 --replay-wait, 未测窗口外放行(窗口边界见 18:14 证据)",
                f"- 判定: {'通过' if a5['passed'] else '不通过'}",
                "",
            ]
        )
    else:
        lines.extend(["- 公网不可达, 未执行", ""])
    bind = public.get("bind") or {}
    bind_error = bind.get("error") or {}
    bind_error_text = ""
    if bind_error.get("code"):
        bind_error_text = f" {bind_error.get('code')} / {bind_error.get('message')}"
    lines.extend(
        [
            "## A6 公网探测",
            "",
            f"- alice: {(public.get('alice') or {}).get('device_id')}, "
            f"bob: {(public.get('bob') or {}).get('device_id')}",
            f"- 探测墙: {public.get('wall_id')} 与 other-users-wall",
            "- 身份来源: "
            + ("--state 已登记账号设备" if public.get("seeded_identity") else "临时自生成设备(不在服务端账号库)"),
            f"- 会话建立: {public.get('session_id')}",
            f"- 归属登记 wall_bind: {bind.get('response_type')}{bind_error_text} "
            f"(account_id 存在={bind.get('account_present')}, bound_at={bind.get('bound_at')})",
            f"- 正向对照(alice 预览/聚焦自己的墙)是否成立: {public['positive_control_ok']}",
        ]
    )
    for item in public["cases"]:
        error = item["error"] or {}
        suffix = ""
        if error.get("code"):
            suffix = f" {error.get('code')} / {error.get('message')}"
        lines.append(f"  - {item['case']}: {item['response_type']}{suffix}")
    if public.get("passed"):
        conclusion = "正向对照成立, bob 的预览/聚焦/释放均因归属不符被拒, alice 令牌仍有效."
    elif public["positive_control_ok"]:
        conclusion = "正向对照成立, 但 bob 的越权拒绝未同时满足「归属不符」与「SCOPE_INSUFFICIENT」, 判不通过."
    else:
        conclusion = (
            "正向对照未成立, 反向探测结果一律不可证. 归属登记返回 "
            f"{bind.get('response_type')} {(bind.get('error') or {}).get('code')}; "
            "在该项修复前, 任何「bob 被拒即修复成功」的说法都不成立."
        )
    lines.extend(
        [
            f"- 结论: {conclusion}",
            "",
            "## A6 本机回环对照",
            "",
            "- 服务: 未改动的 InMemorySignalingServer; 归属经真实协议 wall_bind 登记, "
            "仅落库实现换成进程内表, 鉴权与归属校验代码未改",
            f"- 会话: {local.get('session_id')}, offer/answer 送达: "
            f"{local.get('offer_delivery')}/{local.get('answer_delivery')}",
        ]
    )
    for item in local["cases"]:
        local_error = item["error"] or {}
        local_suffix = ""
        if local_error.get("code"):
            local_suffix = f" {local_error.get('code')} / {local_error.get('message')}"
        lines.append(f"  - {item['case']}: {item['response_type']}{local_suffix}")
    lines.extend(
        [
            f"- 归属登记成立且落为 acct-a: {local.get('bind_ok')}",
            f"- bob 三项全部 SCOPE_INSUFFICIENT 且原因为归属校验: {local['bob_all_denied']} / "
            f"{local['deny_reason_is_ownership']}",
            f"- bob 尝试后 alice 聚焦令牌仍有效: {local['alice_focus_token_valid_after_bob']}",
            f"- bob 尝试后 alice 预览令牌仍有效: {local['alice_preview_token_valid_after_bob']}",
            f"- alice 自己释放返回 released: {local['alice_unfocus_released']}",
            f"- 判定: {'通过' if local['passed'] else '不通过'}",
            "",
            f"## 总判定: {overall}",
            "",
            "- 取证: transcript.json 为脱敏后的完整收发帧; a5.json / a6.json 为判定摘要.",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A5 重放与 A6 越权独立交叉验证")
    parser.add_argument("--host", default="47.109.32.249")
    parser.add_argument("--port", type=int, default=8750)
    parser.add_argument("--sni", default="relay.wuge.fun")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--replay-wait",
        type=float,
        default=0.0,
        help="窗口外重放测试需要等待的秒数; 大于 300 才会跨过去重窗口, 0 表示跳过",
    )
    parser.add_argument("--skip-public", action="store_true", help="只跑本机回环对照")
    parser.add_argument("--state", help="已登记账号的设备状态文件(含 a/b 私钥), 用于公网正向对照")
    parser.add_argument("--wall-id", help="指定登记用的 wall_id; 缺省每次生成 audit-wall-<随机>")
    arguments = parser.parse_args(argv)
    seeded = load_state(arguments.state) if arguments.state else None
    sys.path.insert(0, str(ROOT / "src"))
    started_at = _now()
    tls: dict[str, Any] = {}
    blocked = False
    a5: dict[str, Any] = {"passed": False, "transcript": []}
    a6_public: dict[str, Any] = {"cases": [], "transcript": [], "positive_control_ok": False}
    transport: dict[str, Any] = {}
    if not arguments.skip_public:
        transport = probe_transport(arguments.host, arguments.port, arguments.sni, arguments.timeout)
        tls = transport.get("tls") or {}
        if tls:
            connector = functools.partial(connect_tls, arguments.host, arguments.port, arguments.sni, arguments.timeout)
        elif (transport.get("plaintext") or {}).get("ok"):
            connector = functools.partial(connect_plain, arguments.host, arguments.port, arguments.timeout)
        else:
            connector = None
        if connector is None:
            blocked = True
            sys.stdout.write(f"公网不可达: TLS {transport.get('tls_error')} 明文 {transport.get('plaintext')}\n")
        else:
            try:
                a5 = public_a5(connector, arguments.timeout, arguments.replay_wait)
                a6_public = public_a6(connector, arguments.timeout, seeded, arguments.wall_id)
            except (OSError, ssl.SSLError, ConnectionError, ValueError) as error:
                blocked = True
                sys.stdout.write(f"公网探测受阻: {error}\n")
    a6_local = local_a6(arguments.timeout)
    payload = {
        "started_at": started_at,
        "finished_at": _now(),
        "host": arguments.host,
        "port": arguments.port,
        "sni": arguments.sni,
        "tls": tls,
        "transport": transport,
        "a5": a5,
        "a6_public": a6_public,
        "a6_local": a6_local,
        "blocked": blocked,
        "transcript": a5.get("transcript", []) + a6_public.get("transcript", []) + a6_local.get("transcript", []),
    }
    target = _write_evidence(payload)
    sys.stdout.write(
        f"传输层: TLS 可用={bool(tls)} (失败: {transport.get('tls_error') or '无'}), "
        f"明文可用={(transport.get('plaintext') or {}).get('ok')}\n"
    )
    sys.stdout.write(
        f"A5: 发送 3 次, 对端收到 {a5.get('replay_received_count')} 次, "
        f"新 id 收到 {a5.get('fresh_received_count')} 次, 判定 {'通过' if a5.get('passed') else '不通过'}\n"
    )
    sys.stdout.write(
        f"A6 公网: 归属登记={((a6_public.get('bind') or {}).get('response_type'))} "
        f"{(((a6_public.get('bind') or {}).get('error')) or {}).get('code')}, "
        f"正向对照成立={a6_public.get('positive_control_ok')}, 逐项 "
        + json.dumps(
            [(item["case"], (item["error"] or {}).get("code")) for item in a6_public.get("cases", [])],
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.write(
        f"A6 本机对照: 判定 {'通过' if a6_local.get('passed') else '不通过'}, "
        f"bob 全拒={a6_local.get('bob_all_denied')}, 令牌仍有效={a6_local.get('alice_focus_token_valid_after_bob')}\n"
    )
    sys.stdout.write(f"证据目录: {target}\n")
    if blocked:
        return 2
    if a5.get("passed") and a6_local.get("passed") and a6_public.get("passed"):
        return 0
    bind_error = ((a6_public.get("bind") or {}).get("error") or {}).get("code")
    if a5.get("passed") and a6_local.get("passed") and bind_error == "AUTH_FAILED":
        # 登记被拒在鉴权/账号绑定阶段: 缺少可用的真实账号前置, 记受阻而非产品判定失败
        sys.stdout.write("A6 公网受阻: 归属登记缺少真实账号绑定, 无法建立正向对照\n")
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
