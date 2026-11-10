# ==========================================================================================
# 文件        : verify_relay_transport.py
# 所属目录    : scripts/
# 文件用途    : 公网中继传输层常态检查: TLS 1.3 必须成立, 同端口明文必须被拒
# 架构定位    : 验收门禁的独立探针, 只做传输层判定, 不涉及鉴权与会话
# 核心功能    : TLS 版本/证书链/SNI 校验; 明文帧探测; 证据落盘与退出码
# 职责边界    : 只读探测; 不改服务器与配置; 不依赖被验方脚本
# ==========================================================================================

"""公网中继传输层常态检查.

用法:
    python scripts/verify_relay_transport.py
    python scripts/verify_relay_transport.py --host 47.109.32.249 --port 8750

判定:
    TLS 1.3 握手成功且证书链校验通过 = TLS 项成立
    同端口明文帧得不到有效 JSON 应答     = 明文项成立(明文被拒)
    两项同时成立才通过; 任一不成立返回 1; 完全不可达返回 2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import ssl
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
CA_FILE = ROOT / "deploy" / "relay" / "ca.crt"
TZ = timezone(timedelta(hours=8))
DEFAULT_HOST = "47.109.32.249"
DEFAULT_PORT = 8750
DEFAULT_SNI = "relay.wuge.fun"


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def _tls_check(host: str, port: int, sni: str, timeout: float) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "at": _now()}
    try:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(CA_FILE))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        raw = socket.create_connection((host, port), timeout=timeout)
        sock = context.wrap_socket(raw, server_hostname=sni)
        certificate = sock.getpeercert()
        der = sock.getpeercert(binary_form=True) or b""
        cipher = sock.cipher()
        result.update(
            {
                "ok": sock.version() == "TLSv1.3",
                "tls_version": sock.version(),
                "cipher": {"name": cipher[0], "protocol": cipher[1], "bits": cipher[2]} if cipher else None,
                "verify_mode": "CERT_REQUIRED + check_hostname",
                "ca_file": str(CA_FILE),
                "ca_sha256": hashlib.sha256(CA_FILE.read_bytes()).hexdigest(),
                "peer_cert_sha256": hashlib.sha256(der).hexdigest(),
                "peer_subject": {k: v for part in certificate.get("subject", ()) for k, v in part},
                "peer_issuer": {k: v for part in certificate.get("issuer", ()) for k, v in part},
                "peer_not_after": certificate.get("notAfter"),
            }
        )
        sock.close()
    except (OSError, ssl.SSLError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    return result


def _plaintext_check(host: str, port: int, timeout: float) -> dict[str, Any]:
    """发一帧非法 JSON; 明文服务会回 signed error, TLS 服务会重置或关闭连接."""
    result: dict[str, Any] = {"rejected": False, "at": _now()}
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.sendall(struct.pack(">I", 1) + b"{")
        sock.settimeout(min(timeout, 5.0))
        header = _recv_exactly(sock, 4)
        result["response_header"] = header.hex() if header else ""
        if header is None:
            result["rejected"] = True
            result["detail"] = "连接被关闭, 未返回明文帧"
            sock.close()
            return result
        (length,) = struct.unpack(">I", header)
        if length <= 0 or length > 4 * 1024 * 1024:
            result["rejected"] = True
            result["detail"] = f"帧长度非法({length}), 已拒绝"
            sock.close()
            return result
        body = _recv_exactly(sock, length)
        sock.close()
        try:
            message = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result["rejected"] = True
            result["detail"] = "返回内容不是有效 JSON 帧"
            return result
        result["detail"] = "收到明文 JSON 应答, 说明同端口仍接受明文"
        result["response_type"] = message.get("type")
        result["response_error"] = (message.get("payload") or {}).get("code") if isinstance(message, dict) else None
    except TimeoutError as error:
        result["rejected"] = True
        result["detail"] = f"明文帧无应答({type(error).__name__})"
    except (OSError, ConnectionError) as error:
        result["rejected"] = True
        result["detail"] = f"连接被重置({type(error).__name__}: {error})"
    return result


def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def _write(payload: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EVIDENCE / f"relay-transport-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "check.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tls = payload["tls"]
    plain = payload["plaintext"]
    lines = [
        "# 公网中继传输层检查",
        "",
        f"- 时间: {payload['started_at']} (Asia/Shanghai)",
        f"- 目标: {payload['host']}:{payload['port']} (SNI {payload['sni']})",
        "",
        "## TLS 1.3",
        "",
        f"- 结果: {'成立' if tls.get('ok') else '不成立'}",
        f"- 版本: {tls.get('tls_version')}, 密码套件: {(tls.get('cipher') or {}).get('name')}",
        f"- 校验: {tls.get('verify_mode')}, CA SHA-256: {tls.get('ca_sha256')}",
        f"- 叶证书 SHA-256: {tls.get('peer_cert_sha256')}, 有效期至 {tls.get('peer_not_after')}",
        f"- 失败原因: {tls.get('error') or '无'}",
        "",
        "## 同端口明文",
        "",
        f"- 明文被拒: {plain.get('rejected')}",
        f"- 明细: {plain.get('detail')}",
        f"- 明文应答类型: {plain.get('response_type')} ({plain.get('response_error')})",
        "",
        f"## 总判定: {'通过' if payload['passed'] else '不通过'}",
    ]
    (target / "结论.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中继传输层: TLS 1.3 必须成立, 明文必须被拒")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--sni", default=DEFAULT_SNI)
    parser.add_argument("--timeout", type=float, default=8.0)
    arguments = parser.parse_args(argv)
    started_at = _now()
    tls = _tls_check(arguments.host, arguments.port, arguments.sni, arguments.timeout)
    plain = _plaintext_check(arguments.host, arguments.port, arguments.timeout)
    unreachable = bool(tls.get("error")) and not plain.get("response_header")
    passed = bool(tls.get("ok")) and bool(plain.get("rejected"))
    payload = {
        "started_at": started_at,
        "finished_at": _now(),
        "host": arguments.host,
        "port": arguments.port,
        "sni": arguments.sni,
        "tls": tls,
        "plaintext": plain,
        "passed": passed,
    }
    target = _write(payload)
    sys.stdout.write(f"TLS 1.3: {'成立' if tls.get('ok') else '不成立'} {tls.get('error') or ''}\n")
    sys.stdout.write(f"明文被拒: {plain.get('rejected')} / {plain.get('detail')}\n")
    sys.stdout.write(f"判定: {'通过' if passed else '不通过'}; 证据: {target}\n")
    if unreachable and not passed:
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
