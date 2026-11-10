# ==========================================================================================
# 文件        : remote.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 客户端访问服务端账号中心的 HTTPS 客户端
# 架构定位    : 账号线传输层, 复用现有 TLS 端口, 不新增监听
# 核心功能    : 注册、挑战、登录、登出、设备列表、受信、审计、准入判定
# 职责边界    : 不做本地存储; 不做业务裁决; 不打印令牌与口令; 不做明文降级
# ==========================================================================================

"""账号中心远端客户端.

安全口径:
- 仅允许 https; 证书链使用显式 CA 校验, 不关闭校验;
- 连接 IP 但证书为域名时, 通过 `sni` 显式指定校验名称;
- 写操作自动生成 Idempotency-Key, 超时或重试不会重复建账.
"""

from __future__ import annotations

import json
import platform
import secrets
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from rdc.accounts.remote_protocol import CODE_STORAGE_UNAVAILABLE, attestation_payload
from rdc.core.logging import log_event
from rdc.core.runtime import data_dir, is_frozen
from rdc.crypto.identity import DeviceIdentity
from rdc.crypto.identity_store import load_or_create_identity

DEFAULT_REMOTE_ENDPOINT = "https://relay.wuge.fun:8760"
DEFAULT_SNI = "relay.wuge.fun"
REQUEST_TIMEOUT_S = 8.0
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

Transport = Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, Any]]]


class RemoteUnavailable(RuntimeError):
    """账号中心不可达或证书校验失败; 界面提示中文原因."""


class RemoteRejected(RuntimeError):
    """服务端返回业务错误码; code 供调用方分支."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class RemoteAccountClient:
    """服务端账号中心客户端."""

    endpoint: str
    identity: DeviceIdentity
    ca_path: Path | None = None
    sni: str = ""
    timeout_s: float = REQUEST_TIMEOUT_S
    transport: Transport | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 业务
    def register(self, account: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/v1/accounts/register", {"account": account, "password": password})

    def login(self, account: str, password: str, device_name: str = "") -> dict[str, Any]:
        device_id = self.identity.device_id
        challenge = self._request(
            "POST",
            "/v1/devices/challenges",
            {"device_id": device_id, "device_public_key": self.identity.public_key_b64},
        )
        nonce = str(challenge.get("nonce") or "")
        if not nonce:
            raise RemoteRejected(502, CODE_STORAGE_UNAVAILABLE, "账号中心未返回设备挑战")
        signature = self.identity.sign(attestation_payload(account, device_id, nonce))
        return self._request(
            "POST",
            "/v1/accounts/login",
            {
                "account": account,
                "password": password,
                "device_id": device_id,
                "device_name": device_name or device_id,
                "device_platform": local_platform(),
                "device_public_key": self.identity.public_key_b64,
                "nonce": nonce,
                "signature": signature,
            },
        )

    def logout(self, token: str) -> dict[str, Any]:
        return self._request("POST", "/v1/accounts/logout", {}, token=token)

    def refresh(self, token: str) -> dict[str, Any]:
        return self._request("POST", "/v1/tokens/refresh", {}, token=token)

    def me(self, token: str) -> dict[str, Any]:
        return self._request("GET", "/v1/accounts/me", token=token)

    def list_devices(self, token: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/devices", token=token)
        return _items(data)

    def set_trust(self, token: str, device_id: str, trusted: bool) -> dict[str, Any]:
        return self._request("POST", f"/v1/devices/{device_id}/trust", {"trusted": trusted}, token=token)

    def list_trusted(self, token: str) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/trusted-devices", token=token)
        return _items(data)

    def audits(self, token: str, device_id: str = "", since: str = "") -> list[dict[str, Any]]:
        query = []
        if device_id:
            query.append(f"device_id={device_id}")
        if since:
            query.append(f"since={since}")
        path = "/v1/audits" + ("?" + "&".join(query) if query else "")
        return _items(self._request("GET", path, token=token))

    def messages(self, token: str, device_id: str = "", limit: int = 200) -> dict[str, Any]:
        """账号消息列表; 未读以服务端账号级水位为准."""
        query = [f"limit={limit}"]
        if device_id:
            query.append(f"device_id={device_id}")
        return self._request("GET", "/v1/messages?" + "&".join(query), token=token)

    def mark_messages_read(self, token: str) -> dict[str, Any]:
        """推进服务端已读水位, 与安卓和桌面共享."""
        return self._request("POST", "/v1/messages/read", token=token)

    def resolve_access(
        self,
        token: str,
        device_id: str,
        scope: str,
        existing_session: str = "",
        session_id: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/access/resolve",
            {
                "device_id": device_id,
                "scope": scope,
                "existing_session": existing_session,
                "session_id": session_id,
                "dry_run": dry_run,
            },
            token=token,
        )

    # ------------------------------------------------------------------ 传输
    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        raw_body = b""
        if method == "POST":
            headers["Idempotency-Key"] = secrets.token_hex(16)
            raw_body = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        try:
            status, payload = self._do_transport(method, path, headers, raw_body)
        except RemoteRejected:
            raise
        except (OSError, ValueError) as exc:
            log_event("accounts", "account_center_unreachable", level="WARNING", reason=type(exc).__name__)
            raise RemoteUnavailable("账号中心不可达, 请检查网络或稍后重试") from exc
        if status >= 400:
            raise RemoteRejected(
                status,
                str(payload.get("code") or CODE_STORAGE_UNAVAILABLE),
                str(payload.get("message") or "账号中心请求失败"),
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def _do_transport(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if self.transport is not None:
            return self.transport(method, path, headers, body)
        return self._https_request(method, path, headers, body)

    def _https_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(self.endpoint)
        if parts.scheme != "https":
            raise ValueError("账号中心地址必须使用 https")
        host = parts.hostname or ""
        port = parts.port or 443
        if not host:
            raise ValueError("账号中心地址缺少主机")
        if self.ca_path is None or not self.ca_path.is_file():
            raise ValueError("账号中心证书锚点缺失")
        context = ssl.create_default_context(cafile=str(self.ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        server_name = self.sni or (host if host not in LOOPBACK_HOSTS else DEFAULT_SNI)
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {parts.netloc}\r\n"
            "User-Agent: rdc-account-client/1\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in headers.items())
            + f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        with (
            socket.create_connection((host, port), timeout=self.timeout_s) as raw,
            context.wrap_socket(raw, server_hostname=server_name) as connection,
        ):
            connection.settimeout(self.timeout_s)
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        return _parse_http_response(response)


def local_platform() -> str:
    """本机平台枚举; 只认 windows/linux/macos, 其余一律 unknown."""
    system = platform.system().strip().lower()
    return {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(system, "unknown")


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _parse_http_response(raw: bytes) -> tuple[int, dict[str, Any]]:
    if not raw:
        raise ValueError("账号中心返回空响应")
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise ValueError("账号中心响应行格式不正确")
    status = int(parts[1])
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("账号中心响应不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("账号中心响应不是 JSON 对象")
    return status, payload


def default_ca_path() -> Path | None:
    """证书锚点优先级: 环境变量 → 数据目录 → 仓库部署目录."""
    explicit = data_dir() / "ca.crt"
    if explicit.is_file():
        return explicit
    repo = Path(__file__).resolve().parents[3] / "deploy" / "relay" / "ca.crt"
    return repo if repo.is_file() else None


def build_remote_client_from_config() -> RemoteAccountClient | None:
    """按环境变量或打包默认值构造远端客户端; 未配置时返回 None 保持本地账号行为."""
    import os

    endpoint = os.environ.get("RDC_ACCOUNT_ENDPOINT", "").strip()
    if not endpoint and is_frozen():
        endpoint = DEFAULT_REMOTE_ENDPOINT
    if not endpoint:
        return None
    ca_env = os.environ.get("RDC_RELAY_CA", "").strip()
    ca_path = Path(ca_env) if ca_env else default_ca_path()
    sni = os.environ.get("RDC_ACCOUNT_SNI", "").strip()
    return RemoteAccountClient(
        endpoint=endpoint,
        identity=load_or_create_identity(),
        ca_path=ca_path,
        sni=sni,
    )


__all__ = [
    "DEFAULT_REMOTE_ENDPOINT",
    "DEFAULT_SNI",
    "RemoteAccountClient",
    "RemoteRejected",
    "RemoteUnavailable",
    "build_remote_client_from_config",
    "default_ca_path",
]
