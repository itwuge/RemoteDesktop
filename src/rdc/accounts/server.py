# ==========================================================================================
# 文件        : server.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 服务端账号中心业务实现, 供现有 TLS 监听端口上的 /v1 HTTP 接口调用
# 架构定位    : 服务端业务层, 与客户端 AccountService 共用 AccountStore 与口令/令牌策略
# 核心功能    : 注册、登录(含设备身份声明)、登出、令牌吊销、设备列表、信任、审计、准入判定
# 职责边界    : 不直接监听端口; 不解析 HTTP; 不落明文口令与明文令牌; 不写日志明文凭据
# ==========================================================================================

"""服务端账号中心.

契约依据: .team/records/契约变更记录-契约-202611-018-20261110.md 第六部分.
HTTP 与 TLS 由 `rdc.accounts.api` 与 `rdc.relay.server` 负责, 本模块只做业务与校验.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rdc.accounts.messages_view import build_message
from rdc.accounts.remote_protocol import (
    CHALLENGE_TTL_SECONDS,
    CODE_ACCOUNT_EXISTS,
    CODE_BAD_REQUEST,
    CODE_CHALLENGE_INVALID,
    CODE_CREDENTIAL_INVALID,
    CODE_DEVICE_BOUND_OTHER,
    CODE_DEVICE_NOT_FOUND,
    CODE_PERMISSION_DENIED,
    CODE_SESSION_EXPIRED,
    CODE_SESSION_INVALID,
    CODE_SESSION_REVOKED,
    attestation_payload,
    envelope_fail,
    envelope_ok,
    normalize_platform,
)
from rdc.accounts.store import (
    DEVICE_TOKEN_TTL_SECONDS,
    SESSION_TOKEN_TTL_SECONDS,
    Account,
    AccountStore,
    DeviceRecord,
    TokenRecord,
    _iso,
    _iso_utc,
)
from rdc.crypto.identity import b64decode, device_id_from_public_key, verify_signature

ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
MAX_PASSWORD_LENGTH = 256
PASSWORDLESS_AUDIT_TTL_SECONDS = 24 * 3600

ApiPayload = tuple[int, dict[str, Any]]


@dataclass
class Principal:
    """一次已认证的 API 调用身份."""

    record: TokenRecord
    account: Account

    @property
    def account_id(self) -> str:
        return self.account.account_id

    @property
    def device_id(self) -> str | None:
        return self.record.device_id


class AccountCenter:
    """服务端账号中心; 所有公开方法在内部锁内完成, 避免并发写内存结构."""

    def __init__(self, store: AccountStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._challenges: dict[str, tuple[str, float, str]] = {}
        self._passwordless_audited: dict[str, float] = {}
        self._session_audited: dict[str, float] = {}
        self._kicker: Callable[[str], dict[str, Any]] | None = None

    def set_kicker(self, kicker: Callable[[str], dict[str, Any]]) -> None:
        """由监听层注入断链回调; 吊销令牌后立即踢断信令与数据面连接."""
        self._kicker = kicker

    # ------------------------------------------------------------------ 认证
    def authenticate(self, secret: str) -> tuple[Principal | None, str, str]:
        """校验 Bearer 令牌; 返回 (身份, 错误码, 中文提示)."""
        with self._lock:
            record = self.store.lookup_token(secret or "")
            if record is None or record.kind not in ("device", "session"):
                return None, CODE_SESSION_INVALID, "登录令牌无效"
            if record.revoked_at is not None:
                return None, CODE_SESSION_REVOKED, "登录令牌已被吊销"
            if record.expires_at <= time.time():
                return None, CODE_SESSION_EXPIRED, "登录令牌已过期"
            account = self.store.accounts.get(record.account_id)
            if account is None:
                return None, CODE_SESSION_INVALID, "登录令牌对应的账号不存在"
            return Principal(record=record, account=account), "", ""

    # ------------------------------------------------------------------ 注册
    def register(self, body: dict[str, Any]) -> ApiPayload:
        """注册账号; 账号与口令规则与客户端本地账号一致."""
        account_name = str(body.get("account") or "").strip()
        password = str(body.get("password") or "")
        if not ACCOUNT_NAME_PATTERN.fullmatch(account_name):
            return 400, envelope_fail(CODE_BAD_REQUEST, "账号需为 3 至 64 位字母、数字或 _ . @ -")
        if len(password) < 8 or len(password) > MAX_PASSWORD_LENGTH:
            return 400, envelope_fail(CODE_BAD_REQUEST, "口令至少 8 位")
        with self._lock:
            if self.store.find_account_by_name(account_name) is not None:
                return 409, envelope_fail(CODE_ACCOUNT_EXISTS, "账号已存在, 请直接登录")
            created = self.store.create_account(account_name, password)
            self.store.append_audit("account.register", created.account_id, created.account_id, "ok")
            return 200, envelope_ok(
                {"account_id": created.account_id, "account_name": created.name, "created_at": created.created_at},
                "账号已创建",
            )

    def create_challenge(self, body: dict[str, Any]) -> ApiPayload:
        """签发设备身份挑战; 登录时必须用该设备私钥签名, 防止伪造设备标识."""
        device_id = str(body.get("device_id") or "").strip()
        public_key_b64 = str(body.get("device_public_key") or "").strip()
        if not device_id or not public_key_b64:
            return 400, envelope_fail(CODE_BAD_REQUEST, "缺少设备标识或设备公钥")
        try:
            derived = device_id_from_public_key(_decode_public_key(public_key_b64))
        except ValueError:
            return 400, envelope_fail(CODE_BAD_REQUEST, "设备公钥格式不正确")
        if derived != device_id:
            return 400, envelope_fail(CODE_CHALLENGE_INVALID, "设备标识与设备公钥不匹配")
        nonce = f"nonce-{time.time_ns()}-{device_id[:8]}"
        expires_at = time.time() + CHALLENGE_TTL_SECONDS
        with self._lock:
            self._challenges[device_id] = (nonce, expires_at, public_key_b64)
        return 200, envelope_ok({"nonce": nonce, "expires_at": _iso(expires_at)}, "挑战已签发")

    # ------------------------------------------------------------------ 登录/登出
    def login(self, body: dict[str, Any]) -> ApiPayload:
        """口令登录并绑定设备身份; 成功后签发长期设备令牌与会话令牌."""
        name = str(body.get("account") or "").strip()
        password = str(body.get("password") or "")
        device_id = str(body.get("device_id") or "").strip()
        display_name = str(body.get("device_name") or device_id).strip() or device_id
        device_platform = normalize_platform(body.get("device_platform"))
        public_key_b64 = str(body.get("device_public_key") or "").strip()
        nonce = str(body.get("nonce") or "").strip()
        signature = str(body.get("signature") or "").strip()
        if not name or not password or not device_id or not public_key_b64 or not nonce or not signature:
            return 400, envelope_fail(CODE_BAD_REQUEST, "登录请求字段不完整")

        with self._lock:
            found = self.store.find_account_by_name(name)
            if found is None or not self.store.verify_password(found, password):
                actor = found.account_id if found is not None else name
                self.store.append_audit("account.login", actor, actor, "fail", reason="credential_invalid")
                return 401, envelope_fail(CODE_CREDENTIAL_INVALID, "账号或口令不正确")

            challenge = self._challenges.pop(device_id, None)
            if challenge is None or challenge[1] <= time.time():
                return 400, envelope_fail(CODE_CHALLENGE_INVALID, "设备挑战不存在或已过期, 请重试")
            if challenge[2] != public_key_b64:
                return 400, envelope_fail(CODE_CHALLENGE_INVALID, "设备挑战与当前设备公钥不一致")
            if not verify_signature(public_key_b64, attestation_payload(name, device_id, nonce), signature):
                return 401, envelope_fail(CODE_CHALLENGE_INVALID, "设备身份签名校验失败")

            existing = self.store.device_of(device_id)
            if existing is not None and existing.owner_account_id not in (None, found.account_id):
                self.store.append_audit(
                    "device.bind",
                    found.account_id,
                    device_id,
                    "fail",
                    device_id=device_id,
                    reason="bound_other",
                )
                return 409, envelope_fail(CODE_DEVICE_BOUND_OTHER, "该设备已绑定其他账号")

            device = self.store.ensure_device_bound(device_id, found.account_id, display_name, device_platform)
            self.store.append_audit(
                "device.bind",
                found.account_id,
                device_id,
                "ok",
                device_id=device_id,
                device_name=device.display_name,
                platform=device.platform,
            )
            device_secret, device_record = self.store.issue_token(
                "device", found.account_id, device_id, DEVICE_TOKEN_TTL_SECONDS
            )
            session_secret, session_record = self.store.issue_token(
                "session", found.account_id, device_id, SESSION_TOKEN_TTL_SECONDS
            )
            self.store.append_audit(
                "account.login",
                found.account_id,
                found.account_id,
                "ok",
                device_id=device_id,
            )
            return 200, envelope_ok(
                {
                    "account_id": found.account_id,
                    "account_name": found.name,
                    "created_at": _iso_utc(found.created_at),
                    "device_id": device_id,
                    "device_token": device_secret,
                    "session_token": session_secret,
                    "expires_at": _iso(session_record.expires_at),
                    "device_expires_at": _iso(device_record.expires_at),
                    "platform": device.platform,
                    "trusted": not device.require_confirm,
                    "trusted_at": _iso(device.trusted_at) if device.trusted_at else None,
                    "trust_source": device.trust_source,
                },
                "登录成功",
            )

    def logout(self, principal: Principal) -> ApiPayload:
        """登出: 吊销本设备全部令牌并踢断关联连接."""
        with self._lock:
            device_id = principal.device_id
            if device_id:
                revoked, _ = self.store.revoke_device_tokens(device_id)
            else:
                revoked, _ = self.store.revoke_token_hashes({principal.record.token_hash})
            kicked = self._kick(device_id) if device_id else 0
            self.store.append_audit(
                "account.logout",
                principal.account_id,
                principal.account_id,
                "ok",
                device_id=device_id or "",
                revoked_tokens=revoked,
                kicked_sessions=kicked,
            )
            return 200, envelope_ok(
                {"account_id": principal.account_id, "revoked_tokens": revoked, "kicked_connections": kicked},
                f"已登出, 吊销 {revoked} 个令牌, 断开 {kicked} 个会话",
            )

    def revoke(self, principal: Principal, body: dict[str, Any]) -> ApiPayload:
        """吊销指定令牌; 未指定时吊销本设备全部令牌, 与登出等价于立即断链."""
        explicit = str(body.get("token") or "").strip()
        with self._lock:
            if explicit:
                record = self.store.lookup_token(explicit)
                if record is None:
                    return 404, envelope_fail(CODE_SESSION_INVALID, "令牌不存在")
                revoked, _ = self.store.revoke_token_hashes({record.token_hash})
                device_id = record.device_id
            else:
                device_id = principal.device_id
                if device_id:
                    revoked, _ = self.store.revoke_device_tokens(device_id)
                else:
                    revoked, _ = self.store.revoke_token_hashes({principal.record.token_hash})
            kicked = self._kick(device_id) if device_id else 0
            self.store.append_audit(
                "account.token_revoke",
                principal.account_id,
                device_id or principal.account_id,
                "ok",
                device_id=device_id or "",
                revoked_tokens=revoked,
            )
            return 200, envelope_ok(
                {"revoked_tokens": revoked, "kicked_connections": kicked}, f"已吊销 {revoked} 个令牌"
            )

    def refresh_session(self, principal: Principal) -> ApiPayload:
        """用长期设备令牌换新的短时会话令牌; 设备令牌本身不轮换."""
        with self._lock:
            if principal.record.kind != "device":
                return 403, envelope_fail(CODE_PERMISSION_DENIED, "仅长期设备令牌可刷新会话令牌")
            secret, record = self.store.issue_token(
                "session", principal.account_id, principal.device_id, SESSION_TOKEN_TTL_SECONDS
            )
            return 200, envelope_ok(
                {"session_token": secret, "expires_at": _iso(record.expires_at)},
                "会话令牌已刷新",
            )

    # ------------------------------------------------------------------ 账号与设备
    def me(self, principal: Principal) -> ApiPayload:
        """返回当前登录账号与设备信息, 供客户端恢复登录态后校验."""
        with self._lock:
            device = self.store.device_of(principal.device_id or "")
            trusted = bool(
                device is not None and device.owner_account_id == principal.account_id and not device.require_confirm
            )
            return 200, envelope_ok(
                {
                    "account_id": principal.account_id,
                    "account_name": principal.account.name,
                    "created_at": _iso_utc(principal.account.created_at),
                    "device_id": principal.device_id,
                    "platform": device.platform if device is not None else "unknown",
                    "trusted": trusted,
                    "trusted_at": _iso(device.trusted_at) if device is not None and device.trusted_at else None,
                    "trust_source": device.trust_source if device is not None else "",
                    "expires_at": _iso(principal.record.expires_at),
                },
                "登录态有效",
            )

    def list_devices(self, principal: Principal) -> ApiPayload:
        """账号下全部设备与在线状态."""
        with self._lock:
            items = [
                self._device_item(device, principal.account_id, principal.device_id)
                for device in self.store.devices_of_account(principal.account_id)
            ]
            return 200, envelope_ok({"items": items}, f"共 {len(items)} 台设备")

    def set_trust(self, principal: Principal, device_id: str, body: dict[str, Any]) -> ApiPayload:
        """设置或撤销设备受信; 只允许账号所有者操作, 变更写审计."""
        trusted = bool(body.get("trusted"))
        with self._lock:
            device = self.store.device_of(device_id)
            if device is None:
                return 404, envelope_fail(CODE_DEVICE_NOT_FOUND, "设备不存在")
            if device.owner_account_id != principal.account_id:
                return 403, envelope_fail(CODE_PERMISSION_DENIED, "仅设备所有者可修改受信状态")
            source = "device" if principal.device_id == device_id else "owner"
            updated = self.store.set_device_trust(device_id, trusted, source)
            assert updated is not None
            self.store.append_audit(
                "trust.change",
                principal.account_id,
                device_id,
                "ok",
                device_id=device_id,
                trusted=trusted,
                require_confirm=updated.require_confirm,
                source=source,
                trusted_at=_iso(updated.trusted_at) if updated.trusted_at else "",
            )
            return 200, envelope_ok(
                {"items": [self._device_item(updated, principal.account_id, principal.device_id)]},
                "已设为受信设备, 同账号连接免连接码" if trusted else "已撤销受信, 后续连接需连接码",
            )

    def list_trusted(self, principal: Principal) -> ApiPayload:
        """账号下受信设备清单."""
        with self._lock:
            items = [
                self._device_item(device, principal.account_id, principal.device_id)
                for device in self.store.devices_of_account(principal.account_id)
                if device.owner_account_id == principal.account_id and not device.require_confirm
            ]
            return 200, envelope_ok({"items": items}, f"共 {len(items)} 台受信设备")

    def audits(self, principal: Principal, device_id: str, since: str) -> ApiPayload:
        """审计事件; 指定设备时校验归属."""
        with self._lock:
            if device_id:
                device = self.store.device_of(device_id)
                if device is None:
                    return 404, envelope_fail(CODE_DEVICE_NOT_FOUND, "设备不存在")
                if device.owner_account_id != principal.account_id:
                    return 403, envelope_fail(CODE_PERMISSION_DENIED, "无权查看该设备审计")
                events = self.store.audit_of_device(device_id, since)
            else:
                events = self.store.audit_of_account(principal.account_id, since)
            items = [event.to_dict() for event in events]
            return 200, envelope_ok({"items": items}, f"共 {len(items)} 条审计")

    def messages(self, principal: Principal, device_id: str, limit: int) -> ApiPayload:
        """账号消息列表; 字段与桌面端 list_messages 一致, 未读取服务端账号级水位."""
        with self._lock:
            device = None
            if device_id:
                device = self.store.device_of(device_id)
                if device is None:
                    return 404, envelope_fail(CODE_DEVICE_NOT_FOUND, "设备不存在")
                if device.owner_account_id != principal.account_id:
                    return 403, envelope_fail(CODE_PERMISSION_DENIED, "无权查看该设备消息")
            events = (
                self.store.audit_of_device(device_id, "")
                if device_id
                else self.store.audit_of_account(principal.account_id, "")
            )
            items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for event in events:
                detail = event.detail or {}
                event_device = str(detail.get("device_id") or event.target or "")
                owner = self.store.device_of(event_device)
                display = owner.display_name if owner is not None else event_device
                item = build_message(event_device, display, event.to_dict())
                message_id = str(item["message_id"])
                if message_id in seen:
                    continue
                seen.add(message_id)
                items.append(item)
            items.sort(key=lambda item: str(item["ts"]), reverse=True)
            cursor = self.store.message_read_until(principal.account_id)
            unread = sum(1 for item in items if not cursor or str(item["ts"]) > cursor)
            total = len(items)
            if limit > 0:
                items = items[:limit]
            return 200, envelope_ok(
                {"items": items, "unread": unread, "total": total, "read_until": cursor},
                f"共 {total} 条消息, 未读 {unread} 条",
            )

    def mark_messages_read(self, principal: Principal) -> ApiPayload:
        """把服务端账号级已读水位推进到当前时刻, 与安卓和桌面共享."""
        with self._lock:
            before = self.store.message_read_until(principal.account_id)
            events = self.store.audit_of_account(principal.account_id, "")
            cleared = sum(1 for event in events if not before or event.ts > before)
            cursor = _iso(time.time())
            self.store.set_message_read_until(principal.account_id, cursor)
            return 200, envelope_ok(
                {"read_until": cursor, "cleared": cleared},
                f"已全部标记为已读, 清除 {cleared} 条未读",
            )

    # ------------------------------------------------------------------ 准入
    def resolve_access(self, principal: Principal, body: dict[str, Any]) -> ApiPayload:
        """连接准入判定: 既有会话 → 同账号受信免码 → 生效授权 → 申请制.

        受信免码命中时写一条 `access.passwordless` 审计; 同一 session_id 只记一次.
        """
        device_id = str(body.get("device_id") or "").strip()
        scope = str(body.get("scope") or "control")
        existing_session = str(body.get("existing_session") or "").strip()
        session_id = str(body.get("session_id") or "").strip()
        dry_run = bool(body.get("dry_run"))
        if not device_id:
            return 400, envelope_fail(CODE_BAD_REQUEST, "缺少目标设备标识")
        if scope not in ("view", "control", "full"):
            return 400, envelope_fail(CODE_BAD_REQUEST, "scope 只能为 view、control 或 full")

        with self._lock:
            if existing_session:
                result = self._existing_session(principal, device_id, existing_session)
                if result is not None:
                    return 200, envelope_ok({**result, "audited": False}, result["reason"])

            device = self.store.device_of(device_id)
            if device is None:
                return 404, envelope_fail(CODE_DEVICE_NOT_FOUND, "设备不存在")
            same_account = device.owner_account_id == principal.account_id
            if same_account and not device.require_confirm:
                audited = False if dry_run else self._audit_passwordless(principal, device, session_id)
                return 200, envelope_ok(
                    {
                        "route": "passwordless",
                        "reason": "同账号且设备已受信, 免连接码",
                        "trusted": True,
                        "online": self.store.presence_state(device_id) == "online",
                        "last_connected_at": _iso(device.last_connected_at) if device.last_connected_at else None,
                        "audited": audited,
                    },
                    "免连接码准入",
                )

            grant = self.store.find_active_grant(device_id, principal.account_id, scope, consume=not dry_run)
            if grant is not None:
                return 200, envelope_ok(
                    {
                        "route": "grant",
                        "reason": "命中生效授权",
                        "grant_id": grant.grant_id,
                        "trusted": False,
                        "audited": False,
                    },
                    "授权准入",
                )
            reason = "同账号但设备未受信, 走连接码或申请制" if same_account else "非设备所有者, 走连接码或申请制"
            return 200, envelope_ok(
                {
                    "route": "approval",
                    "reason": reason,
                    "trusted": False,
                    "online": self.store.presence_state(device_id) == "online",
                    "audited": False,
                },
                reason,
            )

    # ------------------------------------------------------------------ 会话审计
    def record_session_event(
        self,
        kind: str,
        session_id: str,
        from_device_id: str,
        peer_device_id: str,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """记录会话建立/结束; 同一 (类型, 会话) 只记一次, 返回是否真的写入."""
        event = "session.start" if kind == "start" else "session.end"
        key = f"{event}:{session_id or f'{from_device_id}->{peer_device_id}'}"
        now = time.time()
        with self._lock:
            seen = self._session_audited.get(key)
            if seen is not None and now - seen < PASSWORDLESS_AUDIT_TTL_SECONDS:
                return False
            self._session_audited[key] = now
            actor = from_device_id
            device = self.store.device_of(from_device_id)
            if device is not None and device.owner_account_id:
                actor = device.owner_account_id
            payload: dict[str, Any] = {
                "session_id": session_id,
                "device_id": from_device_id,
                "peer_device_id": peer_device_id,
                "account_id": device.owner_account_id if device is not None and device.owner_account_id else actor,
            }
            for name in ("transport", "transport_kind", "quality_profile", "end_reason", "reason"):
                value = (detail or {}).get(name)
                if value:
                    payload[name] = value
            self.store.append_audit(event, actor, peer_device_id, "ok", **payload)
            if kind == "start":
                self.store.mark_device_connected(from_device_id)
                self.store.mark_device_connected(peer_device_id)
            self._prune()
            return True

    # ------------------------------------------------------------------ 内部
    def _device_item(self, device: DeviceRecord, account_id: str, current_device_id: str | None) -> dict[str, Any]:
        state = self.store.presence_state(device.device_id)
        return {
            "device_id": device.device_id,
            "display_name": device.display_name,
            "platform": device.platform,
            "bound_at": device.bound_at,
            "online": state == "online",
            "presence_state": state,
            "last_seen_at": _iso(device.last_seen_at) if device.last_seen_at else None,
            "require_confirm": device.require_confirm,
            "trusted": bool(device.owner_account_id == account_id and not device.require_confirm),
            "trust_source": device.trust_source,
            "trusted_at": _iso(device.trusted_at) if device.trusted_at else None,
            "last_connected_at": _iso(device.last_connected_at) if device.last_connected_at else None,
            "owner": device.owner_account_id == account_id,
            "current": device.device_id == current_device_id,
        }

    def _existing_session(self, principal: Principal, device_id: str, secret: str) -> dict[str, Any] | None:
        record = self.store.lookup_token(secret)
        if record is None or record.kind != "session":
            return {"route": "denied", "reason": "会话令牌无效"}
        if record.revoked_at is not None:
            return {"route": "denied", "reason": "会话令牌已被吊销"}
        if record.expires_at <= time.time():
            return {"route": "denied", "reason": "会话令牌已过期"}
        if record.account_id != principal.account_id:
            return {"route": "denied", "reason": "会话令牌与当前账号不一致"}
        if record.device_id is not None and record.device_id != device_id:
            return {"route": "denied", "reason": "会话令牌与目标设备不一致"}
        return {"route": "existing_session", "reason": "会话已建立", "trusted": False}

    def _audit_passwordless(self, principal: Principal, device: DeviceRecord, session_id: str) -> bool:
        key = session_id or f"{device.device_id}:{int(time.time() // 60)}"
        now = time.time()
        seen = self._passwordless_audited.get(key)
        if seen is not None and now - seen < PASSWORDLESS_AUDIT_TTL_SECONDS:
            return False
        self._passwordless_audited[key] = now
        self.store.append_audit(
            "access.passwordless",
            principal.account_id,
            device.device_id,
            "ok",
            device_id=device.device_id,
            device_name=device.display_name,
            account_name=principal.account.name,
            route="passwordless",
            session_id=session_id,
        )
        self.store.mark_device_connected(device.device_id)
        self._prune()
        return True

    def _kick(self, device_id: str) -> int:
        """立即断开该设备的信令与数据面连接; 无回调时返回 0."""
        if self._kicker is None:
            return 0
        result = self._kicker(device_id)
        return int(result.get("kicked", 0)) if isinstance(result, dict) else 0

    def _prune(self) -> None:
        now = time.time()
        for table in (self._passwordless_audited, self._session_audited):
            for key in [name for name, stamp in table.items() if now - stamp >= PASSWORDLESS_AUDIT_TTL_SECONDS]:
                table.pop(key, None)
        for device_id in [key for key, value in self._challenges.items() if value[1] <= now]:
            self._challenges.pop(device_id, None)


def _decode_public_key(public_key_b64: str) -> bytes:
    """解码设备公钥, 非法 Base64 抛 ValueError."""
    try:
        return b64decode(public_key_b64)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("设备公钥格式不正确") from exc
