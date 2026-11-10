# ==========================================================================================
# 文件        : service.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 账号、设备绑定、授权、申请制与免确认判定的业务实现, 对应契约 v1.2 与 v1.3
# 架构定位    : 业务层; 命令层转发到此, 界面层不直接调用
# 核心功能    : 13 个账号授权操作 + set_trust_policy + resolve_access 判定顺序
# 职责边界    : 不接真实网络; 不生成界面文案以外的内容; 不写日志; 不落明文凭据
# ==========================================================================================

"""账号与授权服务.

契约依据: docs/contracts/desktop-v1.md v1.2（第 14 至 26 条）与 v1.3（第 27 条）.
判定顺序见 v1.3 第二节: 既有会话 → 同账号免确认 → 生效授权 → 申请制.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from rdc.accounts.db import AccountStoreError
from rdc.accounts.store import (
    DEVICE_TOKEN_TTL_SECONDS,
    GRANT_DEFAULT_TTL_SECONDS,
    SESSION_TOKEN_TTL_SECONDS,
    Account,
    AccountStore,
    DeviceRecord,
    _iso,
    scope_covers,
)
from rdc.core.errors import (
    ACCESS_CODE_INVALID,
    ACCOUNT_NOT_FOUND,
    CREDENTIAL_INVALID,
    DEVICE_NOT_FOUND,
    GRANT_INVALID,
    GRANT_NOT_TRANSFERABLE,
    GRANT_SCOPE_EXCEEDED,
    PERMISSION_DENIED,
    REQUEST_NOT_FOUND,
    SCOPE_INVALID,
    TOKEN_EXPIRED,
    TOKEN_REVOKED,
)
from rdc.core.result import CommandResult

ACCOUNT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{3,64}$")
ACCESS_CODE_PATTERN = re.compile(r"^\d{6}$")
SCOPES = frozenset({"view", "control", "full"})
ACCESS_ROUTES = ("existing_session", "passwordless", "grant", "approval", "denied")


def _storage_guarded(func: Callable[..., CommandResult]) -> Callable[..., CommandResult]:
    """把落盘故障翻译成失败结果; 成功路径、字段与错误码一律不变."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> CommandResult:
        try:
            return func(*args, **kwargs)
        except AccountStoreError as exc:
            return CommandResult(ok=False, message=f"账号数据暂不可用: {exc}")

    return wrapper


@dataclass
class AccountService:
    """账号模式、申请制与免确认的内存实现.

    当前调用方身份来自 `login_account` 或 `refresh_session_token` 写入的会话;
    真实实现应由命令层从本机安全存储注入, 不接受界面传入的 account_id.
    """

    store: AccountStore = field(default_factory=AccountStore)
    session_account_id: str | None = None
    session_device_id: str | None = None
    session_token_hash: str | None = None
    device_token_hash: str | None = None
    device_lookup: Callable[[str], str | None] = field(default=lambda device_id: device_id)
    access_code_check: Callable[[str, str], bool] = field(
        default=lambda device_id, code: bool(ACCESS_CODE_PATTERN.fullmatch(code or ""))
    )

    # ------------------------------------------------------------------ 内部
    def register_device(self, device_id: str, display_name: str = "") -> None:
        """登记被控设备; 仅用于装配与测试, 不作为契约命令对外."""
        self.store.upsert_device(device_id, display_name or device_id)

    def _bind_session(self, account_id: str, device_id: str | None, token_hash: str) -> None:
        """登录后绑定调用方身份与当次会话令牌摘要."""
        self.session_account_id = account_id
        self.session_device_id = device_id
        self.session_token_hash = token_hash

    def _current_session_error(self) -> CommandResult | None:
        """校验当前登录会话; 吊销与过期即时失效, 不允许旧会话继续调用命令."""
        if not self.session_account_id or not self.session_token_hash:
            return CommandResult(ok=False, message="请先登录账号", error_code=CREDENTIAL_INVALID)
        record = self.store.tokens.get(self.session_token_hash)
        if record is None or record.revoked_at is not None:
            return CommandResult(ok=False, message="登录状态已失效, 请重新登录", error_code=TOKEN_REVOKED)
        if record.expires_at <= time.time():
            return CommandResult(ok=False, message="登录状态已过期, 请重新登录", error_code=TOKEN_EXPIRED)
        return None

    def _is_owner(self, device: DeviceRecord) -> bool:
        """当前登录账号是否为设备所有者."""
        return bool(self.session_account_id) and device.owner_account_id == self.session_account_id

    def _is_device_side(self, device: DeviceRecord) -> bool:
        """当前会话是否来自该被控设备本机."""
        return bool(self.session_device_id) and self.session_device_id == device.device_id

    def _can_approve(self, device: DeviceRecord) -> bool:
        """设备所有者或被控端本机可审批."""
        return self._is_owner(device) or self._is_device_side(device)

    def _has_full_grant(self, device: DeviceRecord) -> bool:
        """当前账号是否持有该设备的 full 授权."""
        if not self.session_account_id:
            return False
        grant = self.store.find_active_grant(device.device_id, self.session_account_id, "full", consume=False)
        return grant is not None

    def _can_manage(self, device: DeviceRecord) -> bool:
        """设备管理、授权管理与审计读取: 所有者、被控端本机或 full 授权持有者."""
        return self._can_approve(device) or self._has_full_grant(device)

    def can_manage_device(self, device_id: str) -> bool:
        """供屏幕墙审计等集成点判定: 当前登录账号是否对该设备具备 full 级管理权.

        不构造 `CommandResult`, 不修改任何状态; 未登录或会话失效一律返回 False,
        调用方不得把「未接线」当作放行。
        """
        if self._current_session_error() is not None:
            return False
        device = self.store.devices.get(device_id)
        return device is not None and self._can_manage(device)

    def _resolve_subject(self, subject: str) -> Account | None:
        """授权对象按账号 ID 或账号名解析."""
        return self.store.accounts.get(subject) or self.store.find_account_by_name(subject or "")

    def _require_account(self) -> tuple[Account | None, CommandResult | None]:
        """取当前登录账号, 未登录或会话已失效返回契约错误."""
        failure = self._current_session_error()
        if failure is not None:
            return None, failure
        account = self.store.accounts.get(self.session_account_id or "")
        if account is None:
            return None, CommandResult(ok=False, message="账号不存在", error_code=ACCOUNT_NOT_FOUND)
        return account, None

    # ------------------------------------------------------------- 账号命令
    @_storage_guarded
    def register_account(self, account: str, password: str) -> CommandResult:
        """注册账号; 口令只存 PBKDF2 派生值."""
        if not ACCOUNT_NAME_PATTERN.fullmatch(account or ""):
            return CommandResult(ok=False, message="账号需为 3 至 64 位字母、数字或 _ . @ -")
        if len(password or "") < 8:
            return CommandResult(ok=False, message="口令至少 8 位")
        if self.store.find_account_by_name(account):
            return CommandResult(ok=False, message="账号已存在, 请直接登录")

        created = self.store.create_account(account, password)
        self.store.append_audit("account.register", created.account_id, created.account_id, "ok")
        return CommandResult(
            ok=True,
            message="账号已创建",
            data={"account_id": created.account_id, "created_at": created.created_at},
        )

    @_storage_guarded
    def login_account(self, account: str, password: str) -> CommandResult:
        """登录并签发长期设备令牌与短时会话令牌."""
        found = self.store.find_account_by_name(account)
        if found is None:
            self.store.append_audit("account.login", account, account, "fail", reason="account_not_found")
            return CommandResult(ok=False, message="账号或口令不正确", error_code=ACCOUNT_NOT_FOUND)
        if not self.store.verify_password(found, password):
            self.store.append_audit(
                "account.login", found.account_id, found.account_id, "fail", reason="credential_invalid"
            )
            return CommandResult(ok=False, message="账号或口令不正确", error_code=CREDENTIAL_INVALID)

        device_token, device_record = self.store.issue_token("device", found.account_id, None, DEVICE_TOKEN_TTL_SECONDS)
        session_token, session_record = self.store.issue_token(
            "session", found.account_id, None, SESSION_TOKEN_TTL_SECONDS
        )
        self._bind_session(found.account_id, None, session_record.token_hash)
        self.device_token_hash = device_record.token_hash
        self.store.append_audit("account.login", found.account_id, found.account_id, "ok")
        return CommandResult(
            ok=True,
            message="登录成功",
            data={
                "account_id": found.account_id,
                "device_token": device_token,
                "session_token": session_token,
                "expires_at": _iso(session_record.expires_at),
            },
        )

    @_storage_guarded
    def refresh_session_token(self, device_token: str) -> CommandResult:
        """用长期设备令牌换短时会话令牌; 吊销与过期一律拒绝."""
        record = self.store.lookup_token(device_token)
        if record is None or record.kind != "device":
            return CommandResult(ok=False, message="设备令牌无效", error_code=TOKEN_REVOKED)
        if record.revoked_at is not None:
            return CommandResult(ok=False, message="设备令牌已吊销, 请重新登录", error_code=TOKEN_REVOKED)
        if record.expires_at <= time.time():
            return CommandResult(ok=False, message="设备令牌已过期, 请重新登录", error_code=TOKEN_EXPIRED)

        session_token, session_record = self.store.issue_token(
            "session", record.account_id, record.device_id, SESSION_TOKEN_TTL_SECONDS
        )
        self._bind_session(record.account_id, record.device_id, session_record.token_hash)
        self.device_token_hash = record.token_hash
        return CommandResult(
            ok=True,
            message="会话令牌已刷新",
            data={"session_token": session_token, "expires_at": _iso(session_record.expires_at)},
        )

    # ------------------------------------------------------------- 设备命令
    @_storage_guarded
    def bind_device(self, device_id: str, access_code: str) -> CommandResult:
        """绑定设备到当前账号; 需有效临时连接码."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        if self.device_lookup(device_id) is None or device_id not in self.store.devices:
            return CommandResult(ok=False, message="设备不存在, 请检查设备码", error_code=DEVICE_NOT_FOUND)
        if not ACCESS_CODE_PATTERN.fullmatch(access_code or "") or not self.access_code_check(device_id, access_code):
            return CommandResult(ok=False, message="临时口令错误或已过期", error_code=ACCESS_CODE_INVALID)

        device = self.store.devices[device_id]
        if device.owner_account_id and device.owner_account_id != account.account_id:
            return CommandResult(ok=False, message="该设备已被其他账号绑定, 请先解绑", error_code=PERMISSION_DENIED)

        bound = self.store.bind_device(device_id, account.account_id)
        self.store.append_audit("device.bind", account.account_id, device_id, "ok", device_id=device_id)
        return CommandResult(
            ok=True,
            message="设备已绑定",
            data={"device_id": device_id, "bound_at": bound.bound_at},
        )

    @_storage_guarded
    def unbind_device(self, device_id: str) -> CommandResult:
        """解绑设备; 解绑即吊销该设备全部令牌并踢掉在线会话."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if not self._is_owner(device):
            return CommandResult(ok=False, message="仅设备所有者可解绑", error_code=PERMISSION_DENIED)

        _, kicked = self.store.revoke_device_tokens(device_id)
        self.store.unbind_device(device_id)
        self.store.append_audit(
            "device.unbind", account.account_id, device_id, "ok", device_id=device_id, kicked_sessions=kicked
        )
        if kicked:
            self.store.append_audit(
                "session.kicked", account.account_id, device_id, "ok", device_id=device_id, reason="unbind"
            )
        return CommandResult(
            ok=True,
            message="设备已解绑",
            data={"device_id": device_id, "status": "unbound", "kicked_sessions": kicked},
        )

    @_storage_guarded
    def change_password(self, account: str, old_password: str, new_password: str) -> CommandResult:
        """改密: 立即吊销该账号全部令牌, 在线会话被踢."""
        found = self.store.find_account_by_name(account)
        if found is None:
            return CommandResult(ok=False, message="账号不存在", error_code=ACCOUNT_NOT_FOUND)
        if not self.store.verify_password(found, old_password):
            return CommandResult(ok=False, message="原口令不正确", error_code=CREDENTIAL_INVALID)
        if len(new_password or "") < 8:
            return CommandResult(ok=False, message="新口令至少 8 位")

        kicked = self.store.revoke_account_tokens(found.account_id)
        self.store.change_password(found, new_password)
        self.store.append_audit("account.password_changed", found.account_id, found.account_id, "ok")
        if kicked:
            self.store.append_audit(
                "session.kicked", found.account_id, found.account_id, "ok", reason="password_changed"
            )
        return CommandResult(
            ok=True,
            message="口令已修改, 已登录会话全部失效",
            data={"account_id": found.account_id, "kicked_sessions": kicked},
        )

    def list_devices(self) -> CommandResult:
        """当前账号可见设备; 追加在线状态与最近心跳时间."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        items = []
        for device in self.store.devices_of_account(account.account_id):
            state = self.store.presence_state(device.device_id)
            items.append(
                {
                    "device_id": device.device_id,
                    "display_name": device.display_name,
                    "bound_at": device.bound_at,
                    "online": state == "online",
                    "presence_state": state,
                    "last_seen_at": _iso(device.last_seen_at) if device.last_seen_at else None,
                    "require_confirm": device.require_confirm,
                    "owner": device.owner_account_id == account.account_id,
                }
            )
        return CommandResult(ok=True, message=f"共 {len(items)} 台设备", data={"items": items})

    def record_presence(self, device_id: str, state: str = "online") -> bool:
        """记录信令服务上报的设备级心跳; 只影响在线显示, 不参与鉴权."""
        return self.store.record_presence(device_id, state)

    def presence_state(self, device_id: str) -> str:
        """查询在线状态: online、offline 或 unknown."""
        return self.store.presence_state(device_id)

    @_storage_guarded
    def set_trust_policy(self, device_id: str, require_confirm: bool) -> CommandResult:
        """切换免确认开关; 默认 require_confirm=False 即免确认开启.

        启动被控端本机与设备所有者都可对单台设备关闭免确认; 变更进审计.
        """
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if not self._can_manage(device):
            return CommandResult(
                ok=False, message="仅设备所有者、被控端或 full 授权账号可修改免确认开关", error_code=PERMISSION_DENIED
            )

        device.require_confirm = bool(require_confirm)
        self.store.save_device(device)
        source = "device" if self._is_device_side(device) else "owner"
        self.store.append_audit(
            "trust_policy.change",
            account.account_id,
            device_id,
            "ok",
            device_id=device_id,
            require_confirm=device.require_confirm,
            source=source,
        )
        return CommandResult(
            ok=True,
            message="免确认已关闭, 后续连接需审批" if device.require_confirm else "免确认已开启, 同账号直连免审批",
            data={"device_id": device_id, "require_confirm": device.require_confirm, "updated_at": _iso(time.time())},
        )

    # ------------------------------------------------------------- 授权命令
    def list_grants(self, device_id: str) -> CommandResult:
        """授权列表; 设备所有者看全部, 被授权账号只看与自己相关的."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        is_owner = self._is_owner(device)
        can_view_all = is_owner or self._is_device_side(device) or self._has_full_grant(device)
        items = []
        for grant in self.store.grants.values():
            if grant.device_id != device_id:
                continue
            if not can_view_all and grant.subject_account_id != account.account_id:
                continue
            items.append(
                {
                    "grant_id": grant.grant_id,
                    "subject": grant.subject_account_id,
                    "scope": grant.scope,
                    "expires_at": _iso(grant.expires_at),
                    "transferable": grant.transferable,
                    "one_time": grant.one_time,
                    "revoked_at": _iso(grant.revoked_at) if grant.revoked_at else None,
                    "active": grant.active,
                }
            )
        return CommandResult(ok=True, message=f"共 {len(items)} 条授权", data={"items": items})

    @_storage_guarded
    def revoke_grant(self, grant_id: str) -> CommandResult:
        """撤销授权; 立即生效."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        grant = self.store.grants.get(grant_id)
        if grant is None:
            return CommandResult(ok=False, message="授权不存在", error_code=GRANT_INVALID)
        device = self.store.devices.get(grant.device_id)
        allowed = self._can_manage(device) if device else False
        if not allowed and grant.subject_account_id != account.account_id:
            return CommandResult(ok=False, message="无权撤销该授权", error_code=PERMISSION_DENIED)

        self.store.revoke_grant(grant_id)
        self.store.append_audit(
            "grant.revoke", account.account_id, grant.device_id, "ok", grant_id=grant_id, device_id=grant.device_id
        )
        return CommandResult(
            ok=True,
            message="授权已撤销并即时生效",
            data={"grant_id": grant_id, "status": "revoked"},
        )

    # ------------------------------------------------------- 授权补强命令 (v1.5)
    @_storage_guarded
    def create_grant(
        self,
        device_id: str,
        subject: str,
        scope: str,
        ttl_seconds: int,
        one_time: bool,
        transferable: bool,
    ) -> CommandResult:
        """设备所有者、被控端或 full 授权账号直接签发授权."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if not self._can_manage(device):
            return CommandResult(ok=False, message="无权签发授权", error_code=PERMISSION_DENIED)
        if scope not in SCOPES:
            return CommandResult(
                ok=False, message="权限范围不合法, 只能是 view、control 或 full", error_code=SCOPE_INVALID
            )
        ttl = int(ttl_seconds or 0)
        if ttl <= 0 or ttl > 7 * 24 * 3600:
            return CommandResult(ok=False, message="有效期需在 1 秒至 7 天之间")
        target = self._resolve_subject(subject)
        if target is None:
            return CommandResult(ok=False, message="授权对象账号不存在", error_code=ACCOUNT_NOT_FOUND)

        grant = self.store.create_grant(
            device_id, target.account_id, scope, ttl, one_time=bool(one_time), transferable=bool(transferable)
        )
        self.store.append_audit(
            "grant.create",
            account.account_id,
            device_id,
            "ok",
            grant_id=grant.grant_id,
            device_id=device_id,
            subject=grant.subject_account_id,
            scope=grant.scope,
            one_time=grant.one_time,
            transferable=grant.transferable,
        )
        return CommandResult(
            ok=True,
            message="授权已签发",
            data={
                "grant_id": grant.grant_id,
                "subject": grant.subject_account_id,
                "scope": grant.scope,
                "expires_at": _iso(grant.expires_at),
                "one_time": grant.one_time,
                "transferable": grant.transferable,
            },
        )

    @_storage_guarded
    def transfer_grant(
        self,
        grant_id: str,
        subject: str,
        ttl_seconds: int,
        one_time: bool,
        scope: str = "",
    ) -> CommandResult:
        """转授: 仅原授权持有人可转, 一层, scope 与到期不得超过原授权."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        parent = self.store.grants.get(grant_id)
        if parent is None:
            self.store.append_audit(
                "grant.transfer", account.account_id, grant_id, "deny", grant_id=grant_id, reason="grant_invalid"
            )
            return CommandResult(ok=False, message="原授权不存在或已失效", error_code=GRANT_INVALID)
        device_id = parent.device_id

        def deny(error_code: str, message: str, reason: str) -> CommandResult:
            """拒绝转授并落审计."""
            self.store.append_audit(
                "grant.transfer",
                account.account_id,
                device_id,
                "deny",
                grant_id=grant_id,
                device_id=device_id,
                reason=reason,
            )
            return CommandResult(ok=False, message=message, error_code=error_code)

        if parent.subject_account_id != account.account_id:
            return deny(PERMISSION_DENIED, "仅原授权持有人可转授", "not_holder")
        if not parent.active:
            return deny(GRANT_INVALID, "原授权不存在或已失效", "grant_invalid")
        if not parent.transferable or parent.parent_grant_id is not None:
            return deny(GRANT_NOT_TRANSFERABLE, "原授权未开启转授或已是子授权", "not_transferable")

        requested_scope = scope or parent.scope
        if requested_scope not in SCOPES:
            return deny(SCOPE_INVALID, "权限范围不合法, 只能是 view、control 或 full", "scope_invalid")
        if not scope_covers(parent.scope, requested_scope):
            return deny(GRANT_SCOPE_EXCEEDED, "转授范围不得高于原授权", "scope_exceeded")

        ttl = int(ttl_seconds or 0)
        now = time.time()
        if ttl <= 0 or now + ttl > parent.expires_at:
            return deny(GRANT_SCOPE_EXCEEDED, "转授到期不得晚于原授权", "expiry_exceeded")
        if parent.one_time and not one_time:
            return deny(GRANT_SCOPE_EXCEEDED, "一次性授权只能转授为一次性授权", "one_time_downgrade")

        target = self._resolve_subject(subject)
        if target is None:
            return deny(ACCOUNT_NOT_FOUND, "转授对象账号不存在", "subject_not_found")

        child = self.store.create_grant(
            device_id,
            target.account_id,
            requested_scope,
            ttl,
            one_time=bool(one_time),
            transferable=False,
            parent_grant_id=parent.grant_id,
            now=now,
        )
        self.store.append_audit(
            "grant.transfer",
            account.account_id,
            device_id,
            "ok",
            grant_id=child.grant_id,
            parent_grant_id=parent.grant_id,
            device_id=device_id,
            subject=child.subject_account_id,
            scope=child.scope,
            one_time=child.one_time,
        )
        return CommandResult(
            ok=True,
            message="授权已转授, 子授权不可再转",
            data={
                "grant_id": child.grant_id,
                "parent_grant_id": parent.grant_id,
                "subject": child.subject_account_id,
                "scope": child.scope,
                "expires_at": _iso(child.expires_at),
                "one_time": child.one_time,
                "transferable": child.transferable,
            },
        )

    @_storage_guarded
    def logout_account(self) -> CommandResult:
        """登出: 吊销本机设备令牌与会话令牌, 并踢出关联会话."""
        if not self.session_account_id and not self.session_token_hash and not self.device_token_hash:
            return CommandResult(ok=False, message="当前未登录")
        account_id = self.session_account_id or ""
        hashes: set[str] = {item for item in (self.session_token_hash, self.device_token_hash) if item}
        revoked, kicked = self.store.revoke_token_hashes(hashes)
        if self.session_device_id:
            _, device_kicked = self.store.revoke_device_tokens(self.session_device_id)
            kicked += device_kicked
        now = time.time()
        self.session_account_id = None
        self.session_device_id = None
        self.session_token_hash = None
        self.device_token_hash = None
        self.store.append_audit(
            "account.logout",
            account_id,
            account_id,
            "ok",
            revoked_tokens=revoked,
            kicked_sessions=kicked,
        )
        return CommandResult(
            ok=True,
            message=f"已登出, 吊销 {revoked} 个令牌, 踢出 {kicked} 个会话",
            data={
                "account_id": account_id,
                "status": "logged_out",
                "revoked_at": _iso(now),
                "kicked_sessions": kicked,
            },
        )

    @_storage_guarded
    def revoke_device_token(self, device_id: str) -> CommandResult:
        """主动吊销某设备全部令牌; 吊销先于返回, 关联会话一并踢出."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if not self._can_manage(device):
            return CommandResult(ok=False, message="无权吊销该设备令牌", error_code=PERMISSION_DENIED)

        revoked, kicked = self.store.revoke_device_tokens(device_id)
        now = time.time()
        self.store.append_audit(
            "device.token_revoke",
            account.account_id,
            device_id,
            "ok",
            device_id=device_id,
            revoked_tokens=revoked,
            kicked_sessions=kicked,
        )
        if kicked:
            self.store.append_audit(
                "session.kicked", account.account_id, device_id, "ok", device_id=device_id, reason="token_revoked"
            )
        return CommandResult(
            ok=True,
            message=f"已吊销 {revoked} 个令牌, 踢出 {kicked} 个会话",
            data={
                "device_id": device_id,
                "status": "revoked",
                "revoked_tokens": revoked,
                "kicked_sessions": kicked,
                "revoked_at": _iso(now),
            },
        )

    # ------------------------------------------------------------- 申请制
    @_storage_guarded
    def request_access(self, device_id: str, scope: str, reason: str) -> CommandResult:
        """发起访问申请; 同账号免确认生效时不需要申请."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if (scope or "control") not in SCOPES:
            return CommandResult(ok=False, message="权限范围不合法")
        route = self.resolve_access(device_id, scope, consume=False)
        if route["route"] == "passwordless":
            return CommandResult(
                ok=False,
                message="该设备对当前账号已开启免确认, 无需申请",
                error_code=PERMISSION_DENIED,
                data={"route": "passwordless"},
            )
        if route["route"] == "grant":
            return CommandResult(
                ok=False, message="已有生效授权, 无需申请", error_code=PERMISSION_DENIED, data={"route": "grant"}
            )

        request = self.store.create_request(device_id, account.account_id, scope or "control", reason or "")
        self.store.append_audit(
            "access.request",
            account.account_id,
            device_id,
            "ok",
            request_id=request.request_id,
            device_id=device_id,
            scope=request.scope,
        )
        return CommandResult(
            ok=True,
            message="申请已提交, 等待设备所有者审批",
            data={"request_id": request.request_id, "status": request.status},
        )

    @_storage_guarded
    def approve_access(self, request_id: str, ttl_seconds: int, one_time: bool) -> CommandResult:
        """审批通过并生成有时限、可一次性的授权."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        request = self.store.requests.get(request_id)
        if request is None:
            return CommandResult(ok=False, message="申请不存在或已失效", error_code=REQUEST_NOT_FOUND)
        device = self.store.devices.get(request.device_id)
        if device is None or not self._can_approve(device):
            return CommandResult(ok=False, message="仅设备所有者或被控端可审批", error_code=PERMISSION_DENIED)
        if request.status != "pending":
            return CommandResult(ok=False, message="该申请已处理, 不能重复审批")

        ttl = int(ttl_seconds or GRANT_DEFAULT_TTL_SECONDS)
        if ttl <= 0 or ttl > 7 * 24 * 3600:
            return CommandResult(ok=False, message="有效期需在 1 秒至 7 天之间")
        request.status = "approved"
        request.decided_at = time.time()
        request.decided_by = account.account_id
        self.store.save_request(request)
        grant = self.store.create_grant(
            request.device_id, request.requester_account_id, request.scope, ttl, one_time=bool(one_time)
        )
        self.store.append_audit(
            "access.approve",
            account.account_id,
            request.device_id,
            "ok",
            request_id=request_id,
            grant_id=grant.grant_id,
            one_time=grant.one_time,
            device_id=request.device_id,
        )
        return CommandResult(
            ok=True,
            message="已批准并生成限时授权",
            data={"grant_id": grant.grant_id, "expires_at": _iso(grant.expires_at), "one_time": grant.one_time},
        )

    @_storage_guarded
    def reject_access(self, request_id: str, reason: str) -> CommandResult:
        """拒绝申请; 拒绝留痕并进审计."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        request = self.store.requests.get(request_id)
        if request is None:
            return CommandResult(ok=False, message="申请不存在或已失效", error_code=REQUEST_NOT_FOUND)
        device = self.store.devices.get(request.device_id)
        if device is None or not self._can_approve(device):
            return CommandResult(ok=False, message="仅设备所有者或被控端可拒绝", error_code=PERMISSION_DENIED)
        if request.status != "pending":
            return CommandResult(ok=False, message="该申请已处理, 不能重复处理")

        request.status = "rejected"
        request.decided_at = time.time()
        request.decided_by = account.account_id
        self.store.save_request(request)
        self.store.append_audit(
            "access.reject",
            account.account_id,
            request.device_id,
            "ok",
            request_id=request_id,
            reason=reason or "",
            device_id=request.device_id,
        )
        return CommandResult(ok=True, message="已拒绝该申请", data={"request_id": request_id, "status": "rejected"})

    @_storage_guarded
    def list_access_requests(self, direction: str) -> CommandResult:
        """按方向列出申请; incoming 为本机设备待审, outgoing 为本人发起."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        if direction not in ("incoming", "outgoing"):
            return CommandResult(ok=False, message="direction 只能为 incoming 或 outgoing")
        self.store.expire_stale_requests()
        items = []
        for request in self.store.requests.values():
            device = self.store.devices.get(request.device_id)
            if direction == "outgoing" and request.requester_account_id != account.account_id:
                continue
            if direction == "incoming" and not (device and self._can_approve(device)):
                continue
            items.append(
                {
                    "request_id": request.request_id,
                    "device_id": request.device_id,
                    "requester": request.requester_account_id,
                    "scope": request.scope,
                    "reason": request.reason,
                    "status": request.status,
                    "created_at": _iso(request.created_at),
                    "decided_at": _iso(request.decided_at) if request.decided_at else None,
                }
            )
        return CommandResult(ok=True, message=f"共 {len(items)} 条申请", data={"items": items})

    def get_audit_log(self, device_id: str, since: str = "") -> CommandResult:
        """按设备读审计; 仅设备所有者或被控端本机可读."""
        account, failure = self._require_account()
        if failure:
            return failure
        assert account is not None
        device = self.store.devices.get(device_id)
        if device is None:
            return CommandResult(ok=False, message="设备不存在", error_code=DEVICE_NOT_FOUND)
        if not self._can_manage(device):
            return CommandResult(ok=False, message="无权查看该设备审计", error_code=PERMISSION_DENIED)
        items = [event.to_dict() for event in self.store.audit_of_device(device_id, since)]
        return CommandResult(ok=True, message=f"共 {len(items)} 条审计", data={"items": items})

    # ------------------------------------------------------------- 判定顺序
    def _resolve_existing_session(self, device_id: str, existing_session: str) -> dict[str, Any]:
        """校验既有会话令牌; 只认未过期、未吊销且属于当前账号的会话令牌."""
        record = self.store.lookup_token(existing_session)
        if record is None or record.kind != "session":
            return {"route": "denied", "reason": "会话令牌无效"}
        if record.revoked_at is not None:
            return {"route": "denied", "reason": "会话令牌已被吊销"}
        if record.expires_at <= time.time():
            return {"route": "denied", "reason": "会话令牌已过期"}
        if not self.session_account_id or record.account_id != self.session_account_id:
            return {"route": "denied", "reason": "会话令牌与当前账号不一致"}
        if record.device_id is not None and record.device_id != device_id:
            return {"route": "denied", "reason": "会话令牌与目标设备不一致"}
        return {"route": "existing_session", "reason": "会话已建立", "session_id": f"session-{record.token_hash[:12]}"}

    def resolve_access(
        self,
        device_id: str,
        scope: str = "control",
        existing_session: str | None = None,
        *,
        consume: bool = True,
    ) -> dict[str, Any]:
        """按契约 v1.3 第二节顺序判定连接路径.

        1. 既有会话; 2. 同账号且设备免确认; 3. 生效授权; 4. 申请制; 5. 拒绝.
        免确认只免连接码与逐次确认, 不免握手、加密与审计.
        :param consume: 命中一次性授权时是否立即标记已用; 仅做可行性预检时传 False.
        """
        if existing_session:
            return self._resolve_existing_session(device_id, existing_session)

        failure = self._current_session_error()
        if failure is not None:
            return {"route": "denied", "reason": failure.message}

        device = self.store.devices.get(device_id)
        if device is None:
            return {"route": "denied", "reason": "设备不存在"}

        same_account = device.owner_account_id == self.session_account_id
        if same_account and not device.require_confirm:
            return {"route": "passwordless", "reason": "同账号且设备免确认开启"}

        grant = self.store.find_active_grant(device_id, self.session_account_id or "", scope, consume=consume)
        if grant is not None:
            return {"route": "grant", "reason": "命中生效授权", "grant_id": grant.grant_id}

        if same_account and device.require_confirm:
            return {"route": "approval", "reason": "同账号但设备已关闭免确认, 走申请制"}
        return {"route": "approval", "reason": "非设备所有者, 走申请制"}
