# ==========================================================================================
# 文件        : verify_relay_auth_boundary.py
# 所属目录    : scripts/
# 文件用途    : 独立交叉验证公网中继的未认证 relay_allocate 鉴权边界(A1)与合法分配闭环
# 架构定位    : 验收取证脚本, 不复用被验方的任何客户端与部署脚本
# 核心功能    : 自生成 Ed25519 身份、七字段包络签名、TLS 1.3 固定 CA 校验、原始帧取证
# 职责边界    : 只读探测; 不改代码/服务器/配置; 不重启; 不写入任何令牌或私钥
# ==========================================================================================

"""独立最小客户端: 公网中继鉴权边界交叉验证.

用法:
    python scripts/verify_relay_auth_boundary.py
    python scripts/verify_relay_auth_boundary.py --host 47.109.32.249 --port 8750 --sni relay.wuge.fun

流程:
    A. 未认证探测: 自生成密钥, relay_allocate 的 payload 自带 public_key, 期望 AUTH_FAILED.
    B. 合法对照: 两个自生成设备完成 register -> auth -> offer 建立会话 ->
       relay_allocate 取得 relay_allocated -> 立即 session_end 释放名额.

取证: evidence/relay-auth-boundary-<时间戳>/{tls.json,probe.json,transcript.json,结论.md}
退出码: 两条判定都成立返回 0, 否则返回 1.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import ssl
import struct
import sys
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


class Client:
    """一条 TLS 连接上的最小协议客户端."""

    def __init__(self, sock: ssl.SSLSocket, label: str) -> None:
        self.sock = sock
        self.label = label
        self.private_key = Ed25519PrivateKey.generate()
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

    def envelope(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "type": msg_type,
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "payload": payload,
        }
        body["sig"] = self._sign(body)
        return body

    def send(self, msg: dict[str, Any]) -> None:
        self.transcript.append({"direction": "send", "at": _now(), "message": _redact(msg)})
        raw = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    """令牌只保留存在性与长度, 原文不落盘; 其余字段保持原始响应形态."""
    if isinstance(message, dict):
        result = {}
        for key, value in message.items():
            if key in {"relay_token", "token"} and isinstance(value, str):
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
        "ca_sha256": _sha256_file(CA_FILE),
        "peer_cert_sha256": hashlib.sha256(der).hexdigest(),
        "peer_subject": subject,
        "peer_issuer": issuer,
        "peer_not_after": cert.get("notAfter"),
        "sni": sock.server_hostname,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _connect(host: str, port: int, sni: str, timeout: float) -> ssl.SSLSocket:
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(CA_FILE))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    raw = socket.create_connection((host, port), timeout=timeout)
    return context.wrap_socket(raw, server_hostname=sni)


def _first_error(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("type") != "error":
        return {}
    payload = message.get("payload") or {}
    return {"code": payload.get("code"), "message": payload.get("message")}


def _register_and_auth(client: Client) -> dict[str, Any]:
    register = client.envelope(
        "register",
        {
            "device_id": client.device_id,
            "public_key": client.public_b64,
            "capabilities": ["video", "input"],
            "protocol_version": 1,
            "display_name": f"audit-{client.label}",
        },
    )
    client.send(register)
    challenge = client.recv()
    nonce = str((challenge.get("payload") or {}).get("nonce") or "")
    auth = client.envelope(
        "auth",
        {
            "device_id": client.device_id,
            "signature": client._sign({"device_id": client.device_id, "nonce": nonce}),
        },
    )
    client.send(auth)
    auth_ok = client.recv()
    return {"challenge_type": challenge.get("type"), "auth_type": auth_ok.get("type")}


def probe_unauthenticated(host: str, port: int, sni: str, timeout: float) -> dict[str, Any]:
    sock = _connect(host, port, sni, timeout)
    client = Client(sock, "unauth")
    tls = _tls_metadata(sock)
    session_id = f"audit-{uuid.uuid4()}"
    request = client.envelope(
        "relay_allocate",
        {
            "session_id": session_id,
            "requested_kbps": 400,
            "est_duration_s": 10,
            "public_key": client.public_b64,
            "from_device_id": client.device_id,
        },
        session_id=session_id,
    )
    client.send(request)
    response = client.recv(timeout)
    client.sock.close()
    error = _first_error(response)
    allocated = response.get("type") == "relay_allocated"
    passed = error.get("code") == "AUTH_FAILED" and not allocated
    return {
        "tls": tls,
        "device_id": client.device_id,
        "public_key_b64": client.public_b64,
        "response_type": response.get("type"),
        "error": error,
        "allocated": allocated,
        "passed": passed,
        "transcript": client.transcript,
    }


def probe_legitimate(host: str, port: int, sni: str, timeout: float) -> dict[str, Any]:
    host_sock = _connect(host, port, sni, timeout)
    host_client = Client(host_sock, "host")
    host_auth = _register_and_auth(host_client)
    controller_sock = _connect(host, port, sni, timeout)
    controller = Client(controller_sock, "controller")
    controller_auth = _register_and_auth(controller)
    session_id = f"audit-{uuid.uuid4()}"
    transcript: list[dict[str, Any]] = []
    allocation: dict[str, Any] = {}
    release: dict[str, Any] = {}
    offer_delivery: dict[str, Any] = {}
    try:
        offer = controller.envelope(
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
        controller.send(offer)
        try:
            delivered = host_client.recv(3.0)
            offer_delivery = {"response_type": delivered.get("type"), "error": _first_error(delivered)}
        except (OSError, ConnectionError, ValueError) as error:
            offer_delivery = {"response_type": "no_response", "error": {"message": str(error)}}
        attempts = 0
        for attempt in range(12):
            attempts = attempt + 1
            allocate = controller.envelope(
                "relay_allocate",
                {"session_id": session_id, "requested_kbps": 400, "est_duration_s": 10},
                session_id=session_id,
            )
            controller.send(allocate)
            response = controller.recv(timeout)
            payload = response.get("payload") or {}
            error = _first_error(response)
            if error.get("code") != "RELAY_FULL":
                break
            time.sleep(3.0)
        allocation = {
            "attempts": attempts,
            "response_type": response.get("type"),
            "error": error,
            "budget_kbps": payload.get("budget_kbps"),
            "quality_profile": payload.get("quality_profile"),
            "relay_token_present": bool(payload.get("relay_token")),
            "endpoint": payload.get("endpoint"),
        }
    finally:
        end = controller.envelope(
            "session_end",
            {
                "session_id": session_id,
                "reason": "审计后立即释放",
                "from_device_id": controller.device_id,
                "to_device_id": host_client.device_id,
            },
            session_id=session_id,
        )
        controller.send(end)
        try:
            release_response = host_client.recv(3.0)
        except (OSError, ConnectionError, ValueError) as error:
            release_response = {"type": "no_response", "detail": str(error)}
        release = {"response_type": release_response.get("type"), "error": _first_error(release_response)}
        transcript = controller.transcript + host_client.transcript
        controller.sock.close()
        host_client.sock.close()
    passed = (
        offer_delivery.get("response_type") == "offer"
        and allocation.get("response_type") == "relay_allocated"
        and bool(allocation.get("relay_token_present"))
        and not allocation.get("error")
        and release.get("response_type") == "session_end"
    )
    return {
        "host": {"device_id": host_client.device_id, "auth": host_auth},
        "controller": {"device_id": controller.device_id, "auth": controller_auth},
        "session_id": session_id,
        "offer_delivery": offer_delivery,
        "allocation": allocation,
        "release": release,
        "passed": passed,
        "transcript": transcript,
    }


def _write_evidence(payload: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EVIDENCE / f"relay-auth-boundary-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "tls.json").write_text(json.dumps(payload["tls"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / "probe.json").write_text(
        json.dumps(payload["probe"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "transcript.json").write_text(
        json.dumps(payload["transcript"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (target / "结论.md").write_text(_report(payload), encoding="utf-8")
    return target


def _report(payload: dict[str, Any]) -> str:
    probe = payload["probe"]
    legit = payload["legit"]
    tls = payload["tls"]
    subject = tls.get("peer_subject") or {}
    allocation = legit.get("allocation") or {}
    blocked = (allocation.get("error") or {}).get("code") == "RELAY_FULL"
    if probe["passed"] and legit["passed"]:
        overall = "通过"
    elif blocked:
        overall = "未认证边界通过；合法对照受阻(RELAY_FULL, 单名额被占用)"
    else:
        overall = "不通过"
    lines = [
        "# 公网中继鉴权边界独立交叉验证",
        "",
        f"- 采集时间: {payload['started_at']} 至 {payload['finished_at']} (Asia/Shanghai)",
        f"- 目标: {payload['host']}:{payload['port']} (SNI {payload['sni']})",
        "- 客户端: scripts/verify_relay_auth_boundary.py 自实现最小客户端, 未复用 relay_public_bench 或部署脚本",
        f"- TLS: {tls.get('tls_version')} / {tls.get('cipher')} / 校验方式 {tls.get('verify_mode')}",
        f"- 证书: subject={subject.get('commonName')} issuer={(tls.get('peer_issuer') or {}).get('commonName')} "
        f"SHA-256={tls.get('peer_cert_sha256')}",
        f"- 根 CA: {tls.get('ca_sha256')}",
        "",
        "## 判定一: 未认证 relay_allocate",
        "",
        f"- 自生成 device_id: {probe['device_id']}",
        f"- 响应类型: {probe['response_type']}",
        f"- 错误码: {(probe.get('error') or {}).get('code')} / {(probe.get('error') or {}).get('message')}",
        f"- 是否被分配: {probe['allocated']}",
        f"- 结论: {'通过' if probe['passed'] else '不通过'}",
        "",
        "## 判定二: 合法分配与释放",
        "",
        f"- 控制端: {legit['controller']['device_id']} 认证 {legit['controller']['auth']}",
        f"- 被控端: {legit['host']['device_id']} 认证 {legit['host']['auth']}",
        f"- offer 送达: {legit['offer_delivery'].get('response_type')}",
        f"- 响应类型: {allocation.get('response_type')} 预算: {allocation.get('budget_kbps')} kbps "
        f"档位: {allocation.get('quality_profile')} 尝试次数: {allocation.get('attempts')}",
        f"- 中继令牌存在: {allocation.get('relay_token_present')} (令牌原文不落盘)",
        f"- 释放响应: {legit['release'].get('response_type')} 错误: {legit['release'].get('error')}",
        f"- 结论: {'通过' if legit['passed'] else ('受阻' if blocked else '不通过')}",
        "",
        f"## 总判定: {overall}",
        "",
        "- 取证: transcript.json 为去掉令牌原文后的完整收发帧; probe.json 为判定摘要.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公网中继未认证 relay_allocate 独立交叉验证")
    parser.add_argument("--host", default="47.109.32.249")
    parser.add_argument("--port", type=int, default=8750)
    parser.add_argument("--sni", default="relay.wuge.fun")
    parser.add_argument("--timeout", type=float, default=8.0)
    arguments = parser.parse_args(argv)
    started_at = _now()
    probe = probe_unauthenticated(arguments.host, arguments.port, arguments.sni, arguments.timeout)
    legit = probe_legitimate(arguments.host, arguments.port, arguments.sni, arguments.timeout)
    payload = {
        "started_at": started_at,
        "finished_at": _now(),
        "host": arguments.host,
        "port": arguments.port,
        "sni": arguments.sni,
        "tls": probe["tls"],
        "probe": {key: value for key, value in probe.items() if key != "tls"},
        "legit": legit,
        "transcript": probe["transcript"] + legit["transcript"],
    }
    target = _write_evidence(payload)
    allocation = legit.get("allocation") or {}
    blocked = (allocation.get("error") or {}).get("code") == "RELAY_FULL"
    ok = probe["passed"] and legit["passed"]
    verdict = "通过" if ok else ("受阻" if blocked else "不通过")
    sys.stdout.write(f"未认证探测: {probe['response_type']} {(probe.get('error') or {}).get('code')}\n")
    sys.stdout.write(
        f"合法对照: {allocation.get('response_type')} budget={allocation.get('budget_kbps')} "
        f"token={allocation.get('relay_token_present')} release={(legit.get('release') or {}).get('response_type')} "
        f"attempts={allocation.get('attempts')}\n"
    )
    sys.stdout.write(f"总判定: {verdict}\n")
    sys.stdout.write(f"证据目录: {target}\n")
    return 0 if ok else (2 if blocked else 1)


if __name__ == "__main__":
    raise SystemExit(main())
