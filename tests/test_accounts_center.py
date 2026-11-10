# ==========================================================================================
# 文件        : test_accounts_center.py
# 所属目录    : tests/
# 文件用途    : 服务端账号中心的账号、设备平台、受信来源与界面白名单字段回归测试
# 架构定位    : 单元测试
# 核心功能    : 登录绑定平台、created_at 为 UTC、受信来源与时间、撤销清空、界面状态不含令牌、会话 started_at
# 职责边界    : 不接网络; 不依赖外部服务; 落盘文件全部在临时目录
# ==========================================================================================

"""跨端账号中心的字段契约测试."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from rdc.accounts.api import AccountApi, looks_like_http
from rdc.accounts.remote import RemoteAccountClient
from rdc.accounts.remote_protocol import attestation_payload
from rdc.accounts.server import AccountCenter
from rdc.accounts.service import AccountService
from rdc.accounts.session_store import SessionVault
from rdc.accounts.store import AccountStore
from rdc.commands.service import CommandService, ConnectionTask, SessionRecord
from rdc.core.errors import CREDENTIAL_INVALID, PERMISSION_DENIED, TOKEN_EXPIRED, TOKEN_REVOKED
from rdc.core.logging import recent_events
from rdc.crypto.identity import DeviceIdentity
from rdc.crypto.identity_store import load_or_create_identity

ACCOUNT = "center@example.com"
PASSWORD = "correct-horse-1"
PEER_DEVICE_ID = "ABCDEFGHIJKLMNOPQRST"


def login_center(
    center: AccountCenter, identity: DeviceIdentity, *, platform: str = "windows"
) -> tuple[str, dict[str, object]]:
    """走真实挑战与签名登录, 返回设备令牌与登录响应 data."""
    status, challenge = center.create_challenge(
        {"device_id": identity.device_id, "device_public_key": identity.public_key_b64}
    )
    assert status == 200
    nonce = str(challenge["data"]["nonce"])
    signature = identity.sign(attestation_payload(ACCOUNT, identity.device_id, nonce))
    status, payload = center.login(
        {
            "account": ACCOUNT,
            "password": PASSWORD,
            "device_id": identity.device_id,
            "device_name": "测试机",
            "device_platform": platform,
            "device_public_key": identity.public_key_b64,
            "nonce": nonce,
            "signature": signature,
        }
    )
    assert status == 200
    data = payload["data"]
    return str(data["device_token"]), data


def make_center() -> AccountCenter:
    """内存账号中心 + 一个已注册账号."""
    center = AccountCenter(AccountStore())
    assert center.register({"account": ACCOUNT, "password": PASSWORD})[0] == 200
    return center


def test_login_reports_platform_and_utc_created_at() -> None:
    """登录响应带平台枚举与 UTC 的 created_at; 未知平台收敛为 unknown."""
    center = make_center()
    _, data = login_center(center, DeviceIdentity.generate(), platform="Windows")
    assert data["platform"] == "windows"
    assert str(data["created_at"]).endswith("Z")

    other = DeviceIdentity.generate()
    _, second = login_center(center, other, platform="plan9")
    assert second["platform"] == "unknown"


def test_device_item_carries_trust_source_and_time() -> None:
    """受信后设备项返回 trust_source/trusted_at, 撤销后立即清空."""
    center = make_center()
    identity = DeviceIdentity.generate()
    secret, _ = login_center(center, identity)
    principal, code, message = center.authenticate(secret)
    assert principal is not None, (code, message)

    status, payload = center.set_trust(principal, identity.device_id, {"trusted": True})
    assert status == 200
    item = payload["data"]["items"][0]
    assert item["trusted"] is True
    assert item["trust_source"] == "device"
    assert item["trusted_at"]

    status, payload = center.set_trust(principal, identity.device_id, {"trusted": False})
    assert status == 200
    item = payload["data"]["items"][0]
    assert item["trusted"] is False
    assert item["trust_source"] == ""
    assert item["trusted_at"] is None

    status, payload = center.me(principal)
    assert status == 200
    assert payload["data"]["trusted"] is False
    assert payload["data"]["platform"] == "windows"


def test_list_devices_returns_platform_fields() -> None:
    """设备列表按契约返回平台、受信来源、受信时间与最近连接时间."""
    center = make_center()
    identity = DeviceIdentity.generate()
    secret, _ = login_center(center, identity, platform="linux")
    principal, _, _ = center.authenticate(secret)
    assert principal is not None
    assert center.set_trust(principal, identity.device_id, {"trusted": True})[0] == 200

    status, payload = center.list_devices(principal)
    assert status == 200
    item = payload["data"]["items"][0]
    for key in ("platform", "trust_source", "trusted_at", "last_connected_at", "online", "presence_state"):
        assert key in item
    assert item["platform"] == "linux"


def test_account_state_never_exposes_tokens() -> None:
    """界面状态白名单不含任何令牌, 并带注册时间."""
    service = AccountService(store=AccountStore())
    assert service.register_account(ACCOUNT, PASSWORD).ok
    assert service.login_account(ACCOUNT, PASSWORD).ok
    state = service.account_state()
    assert state.ok and state.data is not None
    assert state.data["logged_in"] is True
    assert state.data["created_at"]
    assert "device_token" not in state.data
    assert "session_token" not in state.data


@pytest.mark.parametrize("method", ["list_sessions", "status"])
def test_session_started_at_available(method: str) -> None:
    """会话列表与状态都提供 started_at, 供界面显示已连接时长."""
    service = CommandService()
    service.sessions["s-1"] = SessionRecord(session_id="s-1", peer_device_id="PEERDEVICE")
    if method == "list_sessions":
        data = service.list_sessions().data
        assert data is not None
        items = data["items"]
        assert items[0]["started_at"]
    else:
        data = service.get_session_status("s-1").data
        assert data is not None
        assert data["started_at"]


def test_session_state_enum_and_failure_reason() -> None:
    """契约变更记录 018-A8: state 限定五个取值, failed 时带中文原因."""
    service = CommandService()
    service.sessions["s-ok"] = SessionRecord(session_id="s-ok", peer_device_id="PEERDEVICE")
    ok_data = service.get_session_status("s-ok").data
    assert ok_data is not None
    assert ok_data["state"] == "streaming"
    assert "reason" not in ok_data

    service.sessions["s-re"] = SessionRecord(session_id="s-re", peer_device_id="PEERDEVICE", state="reconnecting")
    assert service.get_session_status("s-re").data["state"] == "reconnecting"

    service.sessions["s-bad"] = SessionRecord(
        session_id="s-bad", peer_device_id="PEERDEVICE", state="failed", reason="被控端画面通道中断"
    )
    failed = service.get_session_status("s-bad").data
    assert failed["state"] == "failed"
    assert failed["reason"] == "被控端画面通道中断"


def test_connection_progress_reports_state_and_reason() -> None:
    """连接进度同样提供 state; 失败任务带中文原因."""
    service = CommandService()
    task = ConnectionTask(task_id="t-1", peer_device_id="PEERDEVICE")
    service.tasks["t-1"] = task
    assert service.get_connection_progress("t-1").data["state"] == "connecting"

    task.status = "done"
    assert service.get_connection_progress("t-1").data["state"] == "streaming"

    task.status = "failed"
    task.message = "连接失败：直连与中继均不可用"
    failed = service.get_connection_progress("t-1").data
    assert failed["state"] == "failed"
    assert failed["reason"] == "连接失败：直连与中继均不可用"


def _remote_service(status: int, code: str, message: str) -> AccountService:
    """构造已登录的远端账号服务, 传输层固定返回业务错误."""

    def transport(_method: str, _path: str, _headers: dict[str, str], _body: bytes) -> tuple[int, dict[str, object]]:
        return status, {"code": code, "message": message, "data": {}}

    client = RemoteAccountClient(
        endpoint="https://127.0.0.1:9",
        identity=DeviceIdentity.generate(),
        transport=transport,
    )
    service = AccountService(remote=client)
    service.session_account_id = "account-1"
    service.device_token = "device-token"
    service.remote_expires_at = time.time() + 3600
    return service


@pytest.mark.parametrize("code", [TOKEN_REVOKED, TOKEN_EXPIRED, CREDENTIAL_INVALID])
def test_authorize_connection_passes_token_error_codes(code: str) -> None:
    """令牌类错误码不再折成 PERMISSION_DENIED, 界面据此引导重新登录."""
    service = _remote_service(401, code, "登录状态已失效, 请重新登录")
    result = service.authorize_connection(PEER_DEVICE_ID, "control")
    assert result.ok is False
    assert result.error_code == code
    assert result.message == "登录状态已失效, 请重新登录"


def test_authorize_connection_keeps_permission_denied_for_other_rejections() -> None:
    """非令牌类拒绝仍返回 PERMISSION_DENIED, 中文原因保留."""
    service = _remote_service(403, "DEVICE_NOT_TRUSTED", "设备未受信")
    result = service.authorize_connection(PEER_DEVICE_ID, "control")
    assert result.ok is False
    assert result.error_code == PERMISSION_DENIED
    assert result.message == "设备未受信"


def test_messages_unread_follows_server_cursor(tmp_path: Path) -> None:
    """A4: 消息字段与桌面端一致, 未读以服务端账号级水位为准."""
    center = AccountCenter(AccountStore(tmp_path / "center.db"))
    assert center.register({"account": ACCOUNT, "password": PASSWORD})[0] == 200
    identity = DeviceIdentity.generate()
    secret, _ = login_center(center, identity)
    principal, code, message = center.authenticate(secret)
    assert principal is not None, (code, message)

    center.store.append_audit(
        "session.start",
        principal.account_id,
        identity.device_id,
        "ok",
        device_id=identity.device_id,
        session_id="session-1",
        account_id=principal.account_id,
    )
    status, payload = center.messages(principal, "", 200)
    assert status == 200
    data = payload["data"]
    assert data["unread"] == data["total"]
    assert data["read_until"] == ""
    events = [item["event"] for item in data["items"]]
    assert "session.start" in events
    item = next(entry for entry in data["items"] if entry["event"] == "session.start")
    for key in (
        "message_id",
        "ts",
        "category",
        "category_label",
        "event",
        "title",
        "device_id",
        "device_name",
        "actor",
        "result",
        "detail",
    ):
        assert key in item

    status, payload = center.mark_messages_read(principal)
    assert status == 200
    assert payload["data"]["cleared"] == data["unread"]
    status, payload = center.messages(principal, "", 200)
    assert payload["data"]["unread"] == 0
    assert payload["data"]["read_until"]


def _http(api: AccountApi, raw: bytes) -> tuple[int, dict[str, object]]:
    """用 socketpair 走一次真实 HTTP 解析与响应, 不监听端口."""
    client, server = socket.socketpair()
    try:
        client.sendall(raw[4:])
        api.serve(server, raw[:4])
        server.close()
        response = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    finally:
        client.close()
        server.close()
    head, _, body = response.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(body.decode("utf-8"))


def _post(path: str, payload: dict[str, object], key: str = "", token: str = "") -> bytes:
    """拼一条最小 HTTP POST 请求."""
    body = json.dumps(payload).encode("utf-8")
    lines = [
        f"POST {path} HTTP/1.1",
        "Host: rdc.local",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
    ]
    if key:
        lines.append(f"Idempotency-Key: {key}")
    if token:
        lines.append(f"Authorization: Bearer {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def test_http_first_byte_dispatch() -> None:
    """帧协议首字节为 0, HTTP 方法首字节为大写字母."""
    assert looks_like_http(b"\x00\x00\x01\x00") is False
    assert looks_like_http(b"GET /v1/accounts/me") is True


def test_http_register_requires_idempotency_and_replays() -> None:
    """写接口缺幂等键返回错误码; 同键重放返回首次结果; 换键重复注册返回冲突."""
    api = AccountApi(AccountCenter(AccountStore()))
    payload = {"account": ACCOUNT, "password": PASSWORD}

    status, envelope = _http(api, _post("/v1/accounts/register", payload))
    assert status == 400
    assert envelope["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    status, first = _http(api, _post("/v1/accounts/register", payload, key="k1"))
    assert status == 200 and first["code"] == 0

    status, replay = _http(api, _post("/v1/accounts/register", payload, key="k1"))
    assert status == 200
    assert replay["data"]["account_id"] == first["data"]["account_id"]

    status, conflict = _http(api, _post("/v1/accounts/register", payload, key="k2"))
    assert status == 409
    assert conflict["code"] == "ACCOUNT_EXISTS"


def test_http_me_returns_account_fields() -> None:
    """带 Bearer 的 /me 返回账号、UTC 创建时间与设备平台."""
    center = AccountCenter(AccountStore())
    api = AccountApi(center)
    assert center.register({"account": ACCOUNT, "password": PASSWORD})[0] == 200
    secret, _ = login_center(center, DeviceIdentity.generate(), platform="windows")
    raw = (f"GET /v1/accounts/me HTTP/1.1\r\nHost: rdc.local\r\nAuthorization: Bearer {secret}\r\n\r\n").encode()
    status, envelope = _http(api, raw)
    assert status == 200 and envelope["code"] == 0
    data = envelope["data"]
    assert data["account_name"] == ACCOUNT
    assert str(data["created_at"]).endswith("Z")
    assert data["platform"] == "windows"


def test_session_vault_roundtrip_and_no_plaintext(tmp_path: Path) -> None:
    """密文仓可恢复登录态, 文件里查不到明文令牌, 清空后读不到."""
    vault = SessionVault(tmp_path / "account_session.bin")
    payload = {
        "account_id": "acc-1",
        "account_name": ACCOUNT,
        "device_token": "device-secret-123456",
        "session_token": "session-secret-abcdef",
    }
    assert vault.save(payload) is True
    blob = (tmp_path / "account_session.bin").read_bytes()
    assert b"device-secret-123456" not in blob
    assert b"session-secret-abcdef" not in blob
    assert vault.load() == payload

    vault.clear()
    assert vault.load() is None


def test_service_restores_login_state_from_vault(tmp_path: Path) -> None:
    """重开进程时从密文仓恢复登录态, 界面状态只暴露白名单字段."""
    vault = SessionVault(tmp_path / "account_session.bin")
    assert vault.save(
        {
            "account_id": "acc-1",
            "account_name": ACCOUNT,
            "account_created_at": "2026-11-10T00:00:00Z",
            "device_id": "AAAABBBBCCCCDDDDEEEE",
            "device_token": "device-secret-123456",
            "device_expires_at": 4102444800.0,
        }
    )
    service = AccountService(remote=object(), session_store=vault)
    state = service.account_state()
    assert state.data is not None
    assert state.data["logged_in"] is True
    assert state.data["created_at"] == "2026-11-10T00:00:00Z"
    assert "device_token" not in state.data


def test_device_identity_persists(tmp_path: Path) -> None:
    """设备身份写入后重复载入保持同一标识."""
    path = tmp_path / "device_identity.json"
    first = load_or_create_identity(path)
    second = load_or_create_identity(path)
    assert first.device_id == second.device_id


def test_corrupt_identity_file_is_rebuilt_with_warning(tmp_path: Path) -> None:
    """身份文件损坏: 重建为可用身份, 第二次载入不再换 ID, 且必须留 WARNING 痕迹."""
    path = tmp_path / "device_identity.json"
    path.write_text("{不是合法 JSON", encoding="utf-8")
    rebuilt = load_or_create_identity(path)
    assert load_or_create_identity(path).device_id == rebuilt.device_id
    events = _rebuilt_events(path)
    assert events, "身份文件损坏重建必须记日志, 不能静默换 ID"
    assert events[-1]["level"] == "WARNING"
    assert events[-1]["reason"] == "invalid_json"


def test_unsupported_identity_version_is_rebuilt_with_reason(tmp_path: Path) -> None:
    """版本不匹配按损坏处理: 原因可区分, 不误报成首次启动."""
    path = tmp_path / "device_identity.json"
    path.write_text(json.dumps({"version": 99, "private_key": "AAAA"}), encoding="utf-8")
    load_or_create_identity(path)
    events = _rebuilt_events(path)
    assert events and events[-1]["reason"] == "unsupported_version"


def test_missing_identity_file_is_not_reported_as_damage(tmp_path: Path) -> None:
    """首次启动没有文件属正常路径, 不应产生损坏告警."""
    path = tmp_path / "device_identity.json"
    load_or_create_identity(path)
    assert _rebuilt_events(path) == []


def _rebuilt_events(path: Path) -> list[dict[str, object]]:
    """取本次临时文件对应的重建事件; 日志缓冲是进程级的, 必须按路径过滤."""
    return [
        item
        for item in recent_events(500)
        if item.get("event") == "device_identity_rebuilt" and item.get("path") == str(path)
    ]
