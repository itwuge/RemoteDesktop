# ==========================================================================================
# 文件        : api.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 服务端账号中心 /v1 HTTP 接口, 复用现有 TLS 监听端口
# 架构定位    : 协议适配层, 把 HTTP 请求翻译成 AccountCenter 调用并写出统一包络
# 核心功能    : HTTP/1.1 解析、Bearer 认证、幂等键、路径路由、统一错误包络
# 职责边界    : 不监听端口; 不做业务判定; 不打印请求与令牌; 日志只记脱敏后的结果
# ==========================================================================================

"""账号中心 HTTP 接口.

由 `rdc.relay.server` 在同一条 TLS 连接上按首字节分流进入:
HTTP 请求首字节为大写字母, 既有帧协议首字节为 0（长度高字节）.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from rdc.accounts.db import AccountStoreError
from rdc.accounts.remote_protocol import (
    CODE_BAD_REQUEST,
    CODE_IDEMPOTENCY_KEY_REQUIRED,
    CODE_METHOD_NOT_ALLOWED,
    CODE_NOT_FOUND,
    CODE_STORAGE_UNAVAILABLE,
    IDEMPOTENCY_MAX_ENTRIES,
    IDEMPOTENCY_TTL_SECONDS,
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    envelope_fail,
)
from rdc.accounts.server import AccountCenter, ApiPayload, Principal

HTTP_METHODS = ("GET", "POST", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS")


def looks_like_http(first_bytes: bytes) -> bool:
    """首字节判定: 既有帧协议长度高字节为 0, HTTP 方法首字节为大写字母."""
    return bool(first_bytes) and first_bytes[0] != 0


@dataclass
class HttpRequest:
    """解析后的 HTTP 请求."""

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)


class AccountApi:
    """账号中心 HTTP 接口; 幂等缓存进程内共享."""

    def __init__(self, center: AccountCenter) -> None:
        self.center = center
        self._lock = threading.RLock()
        self._idempotent: OrderedDict[str, tuple[float, int, dict[str, Any]]] = OrderedDict()

    # ------------------------------------------------------------------ 入口
    def serve(self, connection: Any, first_bytes: bytes) -> None:
        """处理一条已建立的 TLS 连接; 读完一个请求后关闭连接."""
        try:
            request = self._read_request(connection, first_bytes)
        except _HttpError as exc:
            _write_response(connection, exc.status, envelope_fail(exc.code, exc.message))
            return
        except TimeoutError:
            _write_response(connection, 408, envelope_fail(CODE_BAD_REQUEST, "请求超时"))
            return
        except (OSError, ValueError):
            return
        if request is None:
            return
        status, payload = self._dispatch(request)
        _write_response(connection, status, payload)

    # ------------------------------------------------------------------ 解析
    def _read_request(self, connection: Any, first_bytes: bytes) -> HttpRequest | None:
        buffer = first_bytes
        while b"\r\n\r\n" not in buffer:
            chunk = connection.recv(4096)
            if not chunk:
                return None
            buffer += chunk
            if len(buffer) > MAX_HEADER_BYTES:
                raise _HttpError(431, CODE_BAD_REQUEST, "请求头过大")
        head, rest = buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("iso-8859-1").split("\r\n")
        if not lines or not lines[0]:
            raise _HttpError(400, CODE_BAD_REQUEST, "请求行不完整")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            raise _HttpError(400, CODE_BAD_REQUEST, "请求行格式不正确")
        method = parts[0].upper()
        if method not in HTTP_METHODS:
            raise _HttpError(405, CODE_METHOD_NOT_ALLOWED, "不支持的请求方法")
        target = urlsplit(parts[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        length_text = headers.get("content-length", "0")
        try:
            length = int(length_text or 0)
        except ValueError as exc:
            raise _HttpError(400, CODE_BAD_REQUEST, "Content-Length 不合法") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise _HttpError(400, CODE_BAD_REQUEST, "请求体过大")
        body_bytes = rest
        while len(body_bytes) < length:
            chunk = connection.recv(min(65536, length - len(body_bytes)))
            if not chunk:
                raise _HttpError(400, CODE_BAD_REQUEST, "请求体不完整")
            body_bytes += chunk
        body_bytes = body_bytes[:length]
        body: dict[str, Any] = {}
        if body_bytes:
            try:
                loaded = json.loads(body_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _HttpError(400, CODE_BAD_REQUEST, "请求体不是合法 JSON") from exc
            if not isinstance(loaded, dict):
                raise _HttpError(400, CODE_BAD_REQUEST, "请求体必须是 JSON 对象")
            body = loaded
        query_raw = parse_qs(target.query, keep_blank_values=True)
        query = {name: values[-1] for name, values in query_raw.items()}
        return HttpRequest(method=method, path=target.path, query=query, headers=headers, body=body)

    # ------------------------------------------------------------------ 路由
    def _dispatch(self, request: HttpRequest) -> ApiPayload:
        path = request.path.rstrip("/") or "/"
        if not path.startswith("/v1/"):
            return 404, envelope_fail(CODE_NOT_FOUND, "接口不存在")
        if request.method == "POST" and not request.headers.get("idempotency-key", "").strip():
            return 400, envelope_fail(CODE_IDEMPOTENCY_KEY_REQUIRED, "写操作必须带 Idempotency-Key 头")
        try:
            handler, principal, cache_key = self._route(request, path)
        except _HttpError as exc:
            return exc.status, envelope_fail(exc.code, exc.message)
        if handler is None:
            return 404, envelope_fail(CODE_NOT_FOUND, "接口不存在")
        if request.method == "POST":
            cached = self._idempotent_get(cache_key, request.headers.get("idempotency-key", ""))
            if cached is not None:
                return cached
        try:
            status, payload = handler(principal)
        except AccountStoreError:
            return 503, envelope_fail(CODE_STORAGE_UNAVAILABLE, "账号数据暂不可用, 请稍后重试")
        except (KeyError, TypeError, ValueError):
            return 400, envelope_fail(CODE_BAD_REQUEST, "请求格式不正确")
        if request.method == "POST":
            self._idempotent_put(cache_key, request.headers.get("idempotency-key", ""), status, payload)
        return status, payload

    def _route(self, request: HttpRequest, path: str) -> tuple[Any, Principal | None, str]:
        """返回 (处理函数, 身份, 幂等缓存键); 未知路由返回 (None, None, "")."""
        method = request.method
        public_key = _public_cache_key(request)
        if path == "/v1/accounts/register" and method == "POST":
            return (lambda _principal: self.center.register(request.body)), None, f"public:{public_key}"
        if path == "/v1/devices/challenges" and method == "POST":
            return (lambda _principal: self.center.create_challenge(request.body)), None, f"public:{public_key}"
        if path == "/v1/accounts/login" and method == "POST":
            return (lambda _principal: self.center.login(request.body)), None, f"public:{public_key}"

        principal = self._authenticate(request.headers.get("authorization", ""))
        cache_key = f"{principal.record.token_hash}:{method}:{path}"
        if path == "/v1/accounts/logout" and method == "POST":
            return (lambda _principal: self.center.logout(principal)), principal, cache_key
        if path == "/v1/tokens/revoke" and method == "POST":
            return (lambda _principal: self.center.revoke(principal, request.body)), principal, cache_key
        if path == "/v1/tokens/refresh" and method == "POST":
            return (lambda _principal: self.center.refresh_session(principal)), principal, cache_key
        if path == "/v1/accounts/me" and method == "GET":
            return (lambda _principal: self.center.me(principal)), principal, cache_key
        if path == "/v1/devices" and method == "GET":
            return (lambda _principal: self.center.list_devices(principal)), principal, cache_key
        if path == "/v1/trusted-devices" and method == "GET":
            return (lambda _principal: self.center.list_trusted(principal)), principal, cache_key
        if path == "/v1/messages" and method == "GET":
            try:
                limit = int(request.query.get("limit", "200"))
            except ValueError:
                limit = 200
            return (
                (lambda _principal: self.center.messages(principal, request.query.get("device_id", ""), limit)),
                principal,
                cache_key,
            )
        if path == "/v1/messages/read" and method == "POST":
            return (lambda _principal: self.center.mark_messages_read(principal)), principal, cache_key
        if path == "/v1/audits" and method == "GET":
            return (
                (
                    lambda _principal: self.center.audits(
                        principal, request.query.get("device_id", ""), request.query.get("since", "")
                    )
                ),
                principal,
                cache_key,
            )
        if path == "/v1/access/resolve" and method == "POST":
            return (lambda _principal: self.center.resolve_access(principal, request.body)), principal, cache_key
        parts = [unquote(item) for item in path.strip("/").split("/")]
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "devices" and parts[3] == "trust" and method == "POST":
            device_id = parts[2]
            return (
                (lambda _principal: self.center.set_trust(principal, device_id, request.body)),
                principal,
                f"{cache_key}:{device_id}",
            )
        return None, None, ""

    def _authenticate(self, authorization: str) -> Principal:
        scheme, _, secret = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secret.strip():
            raise _HttpError(401, "TOKEN_INVALID", "缺少登录令牌")
        principal, code, message = self.center.authenticate(secret.strip())
        if principal is None:
            raise _HttpError(401, code, message)
        return principal

    # ------------------------------------------------------------------ 幂等
    def _idempotent_get(self, cache_key: str, key: str) -> ApiPayload | None:
        if not key:
            return None
        with self._lock:
            item = self._idempotent.get(f"{cache_key}:{key}")
            if item is None:
                return None
            stamp, status, payload = item
            if time.time() - stamp > IDEMPOTENCY_TTL_SECONDS:
                self._idempotent.pop(f"{cache_key}:{key}", None)
                return None
            return status, payload

    def _idempotent_put(self, cache_key: str, key: str, status: int, payload: dict[str, Any]) -> None:
        if not key:
            return
        with self._lock:
            self._idempotent[f"{cache_key}:{key}"] = (time.time(), status, payload)
            while len(self._idempotent) > IDEMPOTENCY_MAX_ENTRIES:
                self._idempotent.popitem(last=False)


class _HttpError(RuntimeError):
    """HTTP 层错误; 直接映射为统一包络."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _public_cache_key(request: HttpRequest) -> str:
    account = str(request.body.get("account") or "")
    device = str(request.body.get("device_id") or "")
    return f"{account}:{device}"


def _write_response(connection: Any, status: int, payload: dict[str, Any]) -> None:
    """写回 HTTP 响应; 连接随后由监听层关闭."""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Error"
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connection.sendall(head + body)
    except OSError:
        return
