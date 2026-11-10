# ==========================================================================================
# 文件        : store.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 账号、设备、令牌、授权、申请与审计的存储, 对应契约 v1.2 第五节数据模型
# 架构定位    : 业务层内部存储, 只被 accounts.service 使用
# 核心功能    : 口令派生、令牌摘要、绑定、授权、申请状态流转、审计追加、SQLite 落盘
# 职责边界    : 不生成用户文案; 不弹窗; 不打印; 不落明文口令与明文令牌
# ==========================================================================================

"""账号与授权存储.

契约依据: docs/contracts/desktop-v1.md v1.2 第五节.
口令只保存 PBKDF2-HMAC-SHA256 派生值与随机盐; 令牌只保存 SHA-256 摘要.
传入数据库路径时落盘到 SQLite 并在启动时载入; 不传路径则只存在于进程内存.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rdc.accounts.db import AccountDatabase

PBKDF2_ITERATIONS = 200_000
DEVICE_TOKEN_TTL_SECONDS = 30 * 24 * 3600
SESSION_TOKEN_TTL_SECONDS = 15 * 60
GRANT_DEFAULT_TTL_SECONDS = 30 * 60
PRESENCE_HEARTBEAT_SECONDS = 30
PRESENCE_TIMEOUT_SECONDS = 90

# scope 层级: view < control < full, 判定按覆盖
SCOPE_LEVELS = {"view": 1, "control": 2, "full": 3}


def scope_covers(grant_scope: str, requested: str) -> bool:
    """授权 scope 是否覆盖本次请求层级; 未知 scope 一律不覆盖."""
    return SCOPE_LEVELS.get(grant_scope, 0) >= SCOPE_LEVELS.get(requested, len(SCOPE_LEVELS) + 1)


def _iso(epoch: float) -> str:
    """把 epoch 秒转为带时区的 ISO 8601 字符串."""
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().isoformat(timespec="seconds")


def _hash_secret(secret: str) -> str:
    """令牌摘要; 只存摘要, 不存原文."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def derive_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256 口令派生."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


@dataclass
class Account:
    """账号记录."""

    account_id: str
    name: str
    salt: bytes
    password_hash: bytes
    created_at: str
    password_changed_at: str


@dataclass
class DeviceRecord:
    """设备记录; 一个账号可绑定多台设备, 一台设备可被多个账号持有授权."""

    device_id: str
    display_name: str
    owner_account_id: str | None = None
    bound_at: str | None = None
    require_confirm: bool = False
    last_seen_at: float | None = None
    presence_offline: bool = False


@dataclass
class TokenRecord:
    """设备令牌或会话令牌; 只存摘要."""

    token_hash: str
    kind: str
    account_id: str
    device_id: str | None
    issued_at: float
    expires_at: float
    revoked_at: float | None = None


@dataclass
class Grant:
    """授权: 对象、范围、有效期、可否转授, 另加一次性与使用标记."""

    grant_id: str
    device_id: str
    subject_account_id: str
    scope: str
    expires_at: float
    transferable: bool = False
    one_time: bool = False
    used_at: float | None = None
    revoked_at: float | None = None
    parent_grant_id: str | None = None

    @property
    def active(self) -> bool:
        """未过期、未撤销、未用尽."""
        now = time.time()
        if self.revoked_at is not None:
            return False
        if self.expires_at <= now:
            return False
        return not (self.one_time and self.used_at is not None)


@dataclass
class AccessRequest:
    """访问申请; 状态取 pending、approved、rejected、expired."""

    request_id: str
    device_id: str
    requester_account_id: str
    scope: str
    reason: str
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    decided_by: str | None = None


@dataclass
class AuditEvent:
    """审计事件; 只记事实, 不记密钥与口令."""

    ts: str
    event: str
    actor: str
    target: str
    result: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为契约规定的六字段."""
        return {
            "ts": self.ts,
            "event": self.event,
            "actor": self.actor,
            "target": self.target,
            "result": self.result,
            "detail": dict(self.detail),
        }


def _account_row(account: Account) -> tuple[Any, ...]:
    """账号记录转数据库行; 账号名另存小写键用于唯一约束."""
    return (
        account.account_id,
        account.name,
        account.name.strip().lower(),
        account.salt,
        account.password_hash,
        account.created_at,
        account.password_changed_at,
    )


def _account_from_row(row: tuple[Any, ...]) -> Account:
    account_id, name, salt, password_hash, created_at, password_changed_at = row
    return Account(
        account_id=str(account_id),
        name=str(name),
        salt=bytes(salt),
        password_hash=bytes(password_hash),
        created_at=str(created_at),
        password_changed_at=str(password_changed_at),
    )


def _device_row(device: DeviceRecord) -> tuple[Any, ...]:
    return (
        device.device_id,
        device.display_name,
        device.owner_account_id,
        device.bound_at,
        int(device.require_confirm),
        device.last_seen_at,
        int(device.presence_offline),
    )


def _device_from_row(row: tuple[Any, ...]) -> DeviceRecord:
    device_id, display_name, owner_account_id, bound_at, require_confirm, last_seen_at, presence_offline = row
    return DeviceRecord(
        device_id=str(device_id),
        display_name=str(display_name),
        owner_account_id=None if owner_account_id is None else str(owner_account_id),
        bound_at=None if bound_at is None else str(bound_at),
        require_confirm=bool(require_confirm),
        last_seen_at=None if last_seen_at is None else float(last_seen_at),
        presence_offline=bool(presence_offline),
    )


def _token_row(record: TokenRecord) -> tuple[Any, ...]:
    return (
        record.token_hash,
        record.kind,
        record.account_id,
        record.device_id,
        record.issued_at,
        record.expires_at,
        record.revoked_at,
    )


def _token_from_row(row: tuple[Any, ...]) -> TokenRecord:
    token_hash, kind, account_id, device_id, issued_at, expires_at, revoked_at = row
    return TokenRecord(
        token_hash=str(token_hash),
        kind=str(kind),
        account_id=str(account_id),
        device_id=None if device_id is None else str(device_id),
        issued_at=float(issued_at),
        expires_at=float(expires_at),
        revoked_at=None if revoked_at is None else float(revoked_at),
    )


def _grant_row(grant: Grant) -> tuple[Any, ...]:
    return (
        grant.grant_id,
        grant.device_id,
        grant.subject_account_id,
        grant.scope,
        grant.expires_at,
        int(grant.transferable),
        int(grant.one_time),
        grant.used_at,
        grant.revoked_at,
        grant.parent_grant_id,
    )


def _grant_from_row(row: tuple[Any, ...]) -> Grant:
    grant_id, device_id, subject, scope, expires_at, transferable, one_time, used_at, revoked_at, parent = row
    return Grant(
        grant_id=str(grant_id),
        device_id=str(device_id),
        subject_account_id=str(subject),
        scope=str(scope),
        expires_at=float(expires_at),
        transferable=bool(transferable),
        one_time=bool(one_time),
        used_at=None if used_at is None else float(used_at),
        revoked_at=None if revoked_at is None else float(revoked_at),
        parent_grant_id=None if parent is None else str(parent),
    )


def _request_row(request: AccessRequest) -> tuple[Any, ...]:
    return (
        request.request_id,
        request.device_id,
        request.requester_account_id,
        request.scope,
        request.reason,
        request.status,
        request.created_at,
        request.decided_at,
        request.decided_by,
    )


def _request_from_row(row: tuple[Any, ...]) -> AccessRequest:
    request_id, device_id, requester, scope, reason, status, created_at, decided_at, decided_by = row
    return AccessRequest(
        request_id=str(request_id),
        device_id=str(device_id),
        requester_account_id=str(requester),
        scope=str(scope),
        reason=str(reason),
        status=str(status),
        created_at=float(created_at),
        decided_at=None if decided_at is None else float(decided_at),
        decided_by=None if decided_by is None else str(decided_by),
    )


def _audit_row(event: AuditEvent) -> tuple[Any, ...]:
    return (
        event.ts,
        event.event,
        event.actor,
        event.target,
        event.result,
        json.dumps(event.detail, ensure_ascii=False),
    )


def _audit_from_row(row: tuple[Any, ...]) -> AuditEvent:
    ts, event, actor, target, result, detail = row
    loaded: Any = json.loads(detail)
    return AuditEvent(
        ts=str(ts),
        event=str(event),
        actor=str(actor),
        target=str(target),
        result=str(result),
        detail=loaded if isinstance(loaded, dict) else {},
    )


class AccountStore:
    """账号与授权存储; 传入数据库路径时落盘, 否则只存在于进程内存.

    两种形态共用同一套字段与摘要策略, 调用方无需感知差异.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.accounts: dict[str, Account] = {}
        self.devices: dict[str, DeviceRecord] = {}
        self.tokens: dict[str, TokenRecord] = {}
        self.grants: dict[str, Grant] = {}
        self.requests: dict[str, AccessRequest] = {}
        self.audit: list[AuditEvent] = []
        self.kicked_sessions: list[str] = []
        self.database: AccountDatabase | None = None
        if db_path is not None:
            self.database = AccountDatabase(db_path)
            self._load()

    @property
    def database_path(self) -> Path | None:
        """落盘文件路径; 纯内存存储时为 None."""
        return None if self.database is None else self.database.path

    def close(self) -> None:
        """关闭落盘连接; 纯内存存储下为空操作, 可重复调用.

        关闭后继续写入会抛 `AccountStoreError`, 不会静默退化成只写内存.
        """
        if self.database is not None:
            self.database.close()

    def _load(self) -> None:
        """按插入顺序载入历史数据; 只由构造期调用, 不触发回写."""
        if self.database is None:
            return
        for row in self.database.load_accounts():
            account = _account_from_row(row)
            self.accounts[account.account_id] = account
        for row in self.database.load_devices():
            device = _device_from_row(row)
            self.devices[device.device_id] = device
        for row in self.database.load_tokens():
            record = _token_from_row(row)
            self.tokens[record.token_hash] = record
        for row in self.database.load_grants():
            grant = _grant_from_row(row)
            self.grants[grant.grant_id] = grant
        for row in self.database.load_requests():
            request = _request_from_row(row)
            self.requests[request.request_id] = request
        self.audit = [_audit_from_row(row) for row in self.database.load_audit()]

    # -------------------------------------------------------------- 落盘同步
    def _save_accounts(self, accounts: list[Account]) -> None:
        if self.database is not None:
            self.database.save_accounts([_account_row(item) for item in accounts])

    def _save_devices(self, devices: list[DeviceRecord]) -> None:
        if self.database is not None:
            self.database.save_devices([_device_row(item) for item in devices])

    def _save_tokens(self, records: list[TokenRecord]) -> None:
        if self.database is not None:
            self.database.save_tokens([_token_row(item) for item in records])

    def _save_grants(self, grants: list[Grant]) -> None:
        if self.database is not None:
            self.database.save_grants([_grant_row(item) for item in grants])

    def _save_requests(self, requests: list[AccessRequest]) -> None:
        if self.database is not None:
            self.database.save_requests([_request_row(item) for item in requests])

    def save_device(self, device: DeviceRecord) -> None:
        """供服务层直接改字段后同步单台设备."""
        self._save_devices([device])

    def save_request(self, request: AccessRequest) -> None:
        """供服务层直接改状态后同步单条申请."""
        self._save_requests([request])

    # ------------------------------------------------------------------ 账号
    def create_account(self, name: str, password: str) -> Account:
        """创建账号; 口令只存派生值."""
        salt = secrets.token_bytes(16)
        now = _iso(time.time())
        account = Account(
            account_id=f"acc-{uuid.uuid4()}",
            name=name,
            salt=salt,
            password_hash=derive_password(password, salt),
            created_at=now,
            password_changed_at=now,
        )
        self._save_accounts([account])
        self.accounts[account.account_id] = account
        return account

    def find_account_by_name(self, name: str) -> Account | None:
        """按账号名查找, 不区分大小写."""
        lowered = name.strip().lower()
        for account in self.accounts.values():
            if account.name.lower() == lowered:
                return account
        return None

    def verify_password(self, account: Account, password: str) -> bool:
        """常量时间比对口令派生值."""
        candidate = derive_password(password, account.salt)
        return hmac.compare_digest(candidate, account.password_hash)

    def change_password(self, account: Account, new_password: str) -> None:
        """改密: 重新派生并立即吊销该账号全部令牌."""
        account.salt = secrets.token_bytes(16)
        account.password_hash = derive_password(new_password, account.salt)
        account.password_changed_at = _iso(time.time())
        self._save_accounts([account])
        self.revoke_account_tokens(account.account_id)

    # ------------------------------------------------------------------ 设备
    def upsert_device(self, device_id: str, display_name: str) -> DeviceRecord:
        """登记设备; 已存在则更新显示名, 不覆盖所有者."""
        device = self.devices.get(device_id)
        if device is None:
            device = DeviceRecord(device_id=device_id, display_name=display_name)
        else:
            device.display_name = display_name
        self._save_devices([device])
        self.devices[device_id] = device
        return device

    def bind_device(self, device_id: str, account_id: str) -> DeviceRecord:
        """绑定设备; 已绑定其他账号时拒绝, 由服务层翻译为失败结果."""
        device = self.devices[device_id]
        device.owner_account_id = account_id
        device.bound_at = _iso(time.time())
        self._save_devices([device])
        return device

    def unbind_device(self, device_id: str) -> None:
        """解绑设备并吊销其全部令牌."""
        device = self.devices[device_id]
        device.owner_account_id = None
        device.bound_at = None
        self._save_devices([device])
        self.revoke_device_tokens(device_id)

    def record_presence(self, device_id: str, state: str = "online", now: float | None = None) -> bool:
        """记录设备级心跳; 只影响在线状态, 不参与鉴权."""
        device = self.devices.get(device_id)
        if device is None:
            return False
        current = time.time() if now is None else now
        if state == "offline":
            device.presence_offline = True
            self._save_devices([device])
            return True
        device.presence_offline = False
        device.last_seen_at = current
        self._save_devices([device])
        return True

    def presence_state(self, device_id: str, now: float | None = None) -> str:
        """在线状态: 从未心跳为 unknown, 90 秒未心跳为 offline."""
        device = self.devices.get(device_id)
        if device is None or device.last_seen_at is None:
            return "unknown"
        if device.presence_offline:
            return "offline"
        current = time.time() if now is None else now
        return "online" if current - device.last_seen_at <= PRESENCE_TIMEOUT_SECONDS else "offline"

    def devices_of_account(self, account_id: str) -> list[DeviceRecord]:
        """该账号绑定或获授权的设备."""
        granted = {
            grant.device_id for grant in self.grants.values() if grant.subject_account_id == account_id and grant.active
        }
        return [
            device
            for device in self.devices.values()
            if device.owner_account_id == account_id or device.device_id in granted
        ]

    # ------------------------------------------------------------------ 令牌
    def issue_token(
        self, kind: str, account_id: str, device_id: str | None, ttl_seconds: int
    ) -> tuple[str, TokenRecord]:
        """签发令牌, 返回明文与记录; 明文只返回一次."""
        secret = secrets.token_urlsafe(32)
        now = time.time()
        record = TokenRecord(
            token_hash=_hash_secret(secret),
            kind=kind,
            account_id=account_id,
            device_id=device_id,
            issued_at=now,
            expires_at=now + ttl_seconds,
        )
        self.tokens[record.token_hash] = record
        self._save_tokens([record])
        return secret, record

    def lookup_token(self, secret: str) -> TokenRecord | None:
        """按明文查找令牌记录."""
        return self.tokens.get(_hash_secret(secret or ""))

    def revoke_device_tokens(self, device_id: str) -> tuple[int, int]:
        """吊销设备全部令牌, 返回 (吊销数, 被踢会话数)."""
        now = time.time()
        revoked = 0
        kicked = 0
        changed: list[TokenRecord] = []
        for record in self.tokens.values():
            if record.device_id == device_id and record.revoked_at is None:
                record.revoked_at = now
                changed.append(record)
                revoked += 1
                if record.kind == "session":
                    kicked += 1
        self._save_tokens(changed)
        if kicked:
            self.kicked_sessions.append(device_id)
        return revoked, kicked

    def revoke_token_hashes(self, hashes: set[str]) -> tuple[int, int]:
        """按令牌摘要批量吊销, 返回 (吊销数, 被踢会话数)."""
        now = time.time()
        revoked = 0
        kicked = 0
        changed: list[TokenRecord] = []
        for token_hash in hashes:
            record = self.tokens.get(token_hash)
            if record is None or record.revoked_at is not None:
                continue
            record.revoked_at = now
            changed.append(record)
            revoked += 1
            if record.kind == "session":
                kicked += 1
        self._save_tokens(changed)
        return revoked, kicked

    def revoke_account_tokens(self, account_id: str) -> int:
        """吊销账号全部令牌, 返回被踢的会话数."""
        now = time.time()
        kicked = 0
        changed: list[TokenRecord] = []
        for record in self.tokens.values():
            if record.account_id == account_id and record.revoked_at is None:
                record.revoked_at = now
                changed.append(record)
                if record.kind == "session":
                    kicked += 1
        self._save_tokens(changed)
        if kicked:
            self.kicked_sessions.append(account_id)
        return kicked

    # ------------------------------------------------------------------ 授权
    def create_grant(
        self,
        device_id: str,
        subject_account_id: str,
        scope: str,
        ttl_seconds: int = GRANT_DEFAULT_TTL_SECONDS,
        one_time: bool = False,
        transferable: bool = False,
        parent_grant_id: str | None = None,
        now: float | None = None,
    ) -> Grant:
        """创建有时限、可一次性的授权; parent_grant_id 标记转授来源."""
        issued_at = time.time() if now is None else now
        grant = Grant(
            grant_id=f"grant-{uuid.uuid4()}",
            device_id=device_id,
            subject_account_id=subject_account_id,
            scope=scope,
            expires_at=issued_at + max(1, ttl_seconds),
            transferable=transferable,
            one_time=one_time,
            parent_grant_id=parent_grant_id,
        )
        self.grants[grant.grant_id] = grant
        self._save_grants([grant])
        return grant

    def find_active_grant(
        self, device_id: str, subject_account_id: str, scope: str, *, consume: bool = True
    ) -> Grant | None:
        """查找覆盖本次范围的生效授权.

        :param consume: 为 True 时把命中的一次性授权标记为已用; 仅做可行性预检时传 False.
        """
        for grant in self.grants.values():
            if grant.device_id != device_id or grant.subject_account_id != subject_account_id:
                continue
            if not scope_covers(grant.scope, scope):
                continue
            if not grant.active:
                continue
            if consume and grant.one_time:
                grant.used_at = time.time()
                self._save_grants([grant])
            return grant
        return None

    def revoke_grant(self, grant_id: str) -> None:
        """撤销即时生效."""
        grant = self.grants[grant_id]
        grant.revoked_at = time.time()
        self._save_grants([grant])

    # ------------------------------------------------------------------ 申请
    def create_request(self, device_id: str, requester_account_id: str, scope: str, reason: str) -> AccessRequest:
        """创建待审批申请."""
        request = AccessRequest(
            request_id=f"req-{uuid.uuid4()}",
            device_id=device_id,
            requester_account_id=requester_account_id,
            scope=scope,
            reason=reason,
        )
        self.requests[request.request_id] = request
        self._save_requests([request])
        return request

    def pending_requests_for_device(self, device_id: str) -> list[AccessRequest]:
        """某设备的待审批申请, 供被控端或被授权审批人查看."""
        return [
            request
            for request in self.requests.values()
            if request.device_id == device_id and request.status == "pending"
        ]

    def expire_stale_requests(self, max_age_seconds: int = 24 * 3600) -> int:
        """把超时未处理的申请置为 expired, 返回条数."""
        now = time.time()
        expired = 0
        changed: list[AccessRequest] = []
        for request in self.requests.values():
            if request.status == "pending" and now - request.created_at > max_age_seconds:
                request.status = "expired"
                request.decided_at = now
                changed.append(request)
                expired += 1
        self._save_requests(changed)
        return expired

    # ------------------------------------------------------------------ 审计
    def append_audit(self, event: str, actor: str, target: str, result: str, **detail: Any) -> None:
        """追加审计事件; detail 中不得出现口令与令牌明文."""
        record = AuditEvent(
            ts=_iso(time.time()),
            event=event,
            actor=actor,
            target=target,
            result=result,
            detail=detail,
        )
        self.audit.append(record)
        if self.database is not None:
            self.database.append_audit([_audit_row(record)])

    def audit_of_device(self, device_id: str, since: str = "") -> list[AuditEvent]:
        """按设备过滤审计; since 为空表示全部."""
        items = [item for item in self.audit if item.target == device_id or item.detail.get("device_id") == device_id]
        if since:
            items = [item for item in items if item.ts >= since]
        return items
