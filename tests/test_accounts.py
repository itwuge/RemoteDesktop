# ==========================================================================================
# 文件        : test_accounts.py
# 所属目录    : tests/
# 文件用途    : 账号、设备绑定、授权、申请制、免确认与审计的契约测试
# 架构定位    : 单元测试
# 核心功能    : 覆盖 desktop-v1 v1.2 第 14 至 26 条与 v1.3 第 27 条、判定顺序
# 职责边界    : 不接网络; 不依赖外部服务; 全部内存执行
# ==========================================================================================

"""账号与授权测试."""

from __future__ import annotations

import time

from rdc.accounts.service import AccountService
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

ACCOUNT_A = "alice@example.com"
ACCOUNT_B = "bob@example.com"
ACCOUNT_C = "carol@example.com"
PASSWORD = "correct-horse-1"
DEVICE_1 = "AAAABBBBCCCCDDDDEEEE"
DEVICE_2 = "FFFFGGGGHHHHIIIIJJJJ"
DEVICE_3 = "KKKKLLLLMMMMNNNNOOOO"


def make_service(*devices: str) -> AccountService:
    """构造带设备的服务实例."""
    service = AccountService()
    for index, device_id in enumerate(devices or (DEVICE_1,)):
        service.register_device(device_id, f"设备{index + 1}")
    return service


def login(service: AccountService, account: str, password: str = PASSWORD) -> dict:
    """注册并登录, 返回 data."""
    if service.store.find_account_by_name(account) is None:
        assert service.register_account(account, password).ok
    result = service.login_account(account, password)
    assert result.ok, result.message
    return result.data


def skipped_login(service: AccountService, account: str) -> dict:
    """只登录已存在账号."""
    result = service.login_account(account, PASSWORD)
    assert result.ok, result.message
    return result.data


def test_register_and_duplicate() -> None:
    """注册成功; 重复账号与短口令被拒."""
    service = make_service()
    first = service.register_account(ACCOUNT_A, PASSWORD)
    assert first.ok and first.data["account_id"].startswith("acc-")
    assert not service.register_account(ACCOUNT_A, PASSWORD).ok
    assert not service.register_account("ab", PASSWORD).ok
    assert not service.register_account(ACCOUNT_B, "short").ok


def test_password_not_stored_in_plain_text() -> None:
    """口令只存派生值与随机盐."""
    service = make_service()
    service.register_account(ACCOUNT_A, PASSWORD)
    account = service.store.find_account_by_name(ACCOUNT_A)
    assert account is not None
    assert PASSWORD.encode() not in account.password_hash
    assert account.salt not in account.password_hash


def test_login_failure_codes() -> None:
    """账号不存在与口令错误返回不同错误码."""
    service = make_service()
    missing = service.login_account(ACCOUNT_A, PASSWORD)
    assert not missing.ok and missing.error_code == ACCOUNT_NOT_FOUND
    service.register_account(ACCOUNT_A, PASSWORD)
    wrong = service.login_account(ACCOUNT_A, "wrong-password")
    assert not wrong.ok and wrong.error_code == CREDENTIAL_INVALID


def test_token_issued_hashed_and_refreshable() -> None:
    """登录返回明文令牌, 存储只留摘要; 可刷新会话令牌."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    assert data["session_token"] not in service.store.tokens
    refreshed = service.refresh_session_token(data["device_token"])
    assert refreshed.ok and refreshed.data["session_token"] != data["session_token"]


def test_revoked_device_token_cannot_refresh() -> None:
    """吊销后刷新被拒."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    service.store.revoke_account_tokens(data["account_id"])
    result = service.refresh_session_token(data["device_token"])
    assert not result.ok and result.error_code == TOKEN_REVOKED


def test_expired_device_token_rejected() -> None:
    """过期令牌返回 TOKEN_EXPIRED."""
    service = make_service()
    secret, record = service.store.issue_token("device", "acc-x", None, 1)
    record.expires_at = 0
    result = service.refresh_session_token(secret)
    assert not result.ok and result.error_code == TOKEN_EXPIRED


def test_bind_and_list_devices() -> None:
    """绑定设备后出现在设备列表, 默认免确认开启."""
    service = make_service()
    login(service, ACCOUNT_A)
    bound = service.bind_device(DEVICE_1, "123456")
    assert bound.ok
    listed = service.list_devices()
    assert listed.ok
    item = listed.data["items"][0]
    assert item["device_id"] == DEVICE_1 and item["require_confirm"] is False and item["owner"] is True


def test_bind_requires_valid_access_code() -> None:
    """连接码非法被拒; 设备不存在被拒."""
    service = make_service()
    login(service, ACCOUNT_A)
    assert service.bind_device(DEVICE_1, "12ab56").error_code == ACCESS_CODE_INVALID
    assert service.bind_device(DEVICE_3, "123456").error_code == DEVICE_NOT_FOUND


def test_bind_other_owner_denied() -> None:
    """同一设备已被绑定, 其他账号不能再绑."""
    service = make_service()
    login(service, ACCOUNT_A)
    assert service.bind_device(DEVICE_1, "123456").ok
    login(service, ACCOUNT_B)
    denied = service.bind_device(DEVICE_1, "123456")
    assert not denied.ok and denied.error_code == PERMISSION_DENIED


def test_unbind_kicks_sessions() -> None:
    """解绑吊销该设备全部令牌, 会话被踢并留痕."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.store.issue_token("session", "acc-x", DEVICE_1, 900)
    result = service.unbind_device(DEVICE_1)
    assert result.ok and result.data["kicked_sessions"] >= 1
    events = [item.event for item in service.store.audit]
    assert "device.unbind" in events and "session.kicked" in events


def test_trust_policy_owner_only_and_audited() -> None:
    """免确认开关仅所有者可改, 默认关闭确认, 变更进审计."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    changed = service.set_trust_policy(DEVICE_1, True)
    assert changed.ok and changed.data["require_confirm"] is True
    assert any(item.event == "trust_policy.change" for item in service.store.audit)

    login(service, ACCOUNT_B)
    denied = service.set_trust_policy(DEVICE_1, False)
    assert not denied.ok and denied.error_code == PERMISSION_DENIED


def test_resolve_access_order() -> None:
    """判定顺序: 既有会话 → 免确认 → 授权 → 申请制."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    session_token, _ = service.store.issue_token("session", data["account_id"], None, 900)

    assert service.resolve_access(DEVICE_1, existing_session=session_token)["route"] == "existing_session"
    assert service.resolve_access(DEVICE_1, existing_session="forged-session")["route"] == "denied"
    assert service.resolve_access(DEVICE_1)["route"] == "passwordless"

    service.set_trust_policy(DEVICE_1, True)
    assert service.resolve_access(DEVICE_1)["route"] == "approval"

    service.set_trust_policy(DEVICE_1, False)


def test_passwordless_request_rejected() -> None:
    """免确认生效时申请被明确拒绝, 不产生申请记录."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    result = service.request_access(DEVICE_1, "control", "临时看一眼")
    assert not result.ok and result.error_code == PERMISSION_DENIED
    assert not service.store.requests


def test_request_approve_grant_flow() -> None:
    """非所有者申请 → 所有者审批 → 生成限时一次性授权 → 命中授权."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")

    login(service, ACCOUNT_B)
    requested = service.request_access(DEVICE_1, "control", "协助排障")
    assert requested.ok and requested.data["status"] == "pending"
    request_id = requested.data["request_id"]
    assert service.resolve_access(DEVICE_1)["route"] == "approval"

    login(service, ACCOUNT_A)
    approved = service.approve_access(request_id, 600, True)
    assert approved.ok and approved.data["one_time"] is True

    login(service, ACCOUNT_B)
    route = service.resolve_access(DEVICE_1, "control")
    assert route["route"] == "grant"
    assert service.resolve_access(DEVICE_1, "control")["route"] == "approval", "一次性授权使用后立即失效"


def test_reject_is_recorded() -> None:
    """拒绝申请留痕并可查询."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    login(service, ACCOUNT_B)
    request_id = service.request_access(DEVICE_1, "view", "看下界面").data["request_id"]

    login(service, ACCOUNT_A)
    rejected = service.reject_access(request_id, "当前不在工位")
    assert rejected.ok and rejected.data["status"] == "rejected"
    events = [item.event for item in service.store.audit]
    assert "access.reject" in events

    service = service
    incoming = service.list_access_requests("incoming")
    assert any(item["request_id"] == request_id and item["status"] == "rejected" for item in incoming.data["items"])


def test_revoke_grant_immediate() -> None:
    """撤销授权即时生效, 后续判定回到申请制."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    login(service, ACCOUNT_B)
    request_id = service.request_access(DEVICE_1, "control", "协助").data["request_id"]
    login(service, ACCOUNT_A)
    grant_id = service.approve_access(request_id, 600, False).data["grant_id"]

    login(service, ACCOUNT_B)
    assert service.resolve_access(DEVICE_1, "control")["route"] == "grant"
    revoked = service.revoke_grant(grant_id)
    assert revoked.ok
    assert service.resolve_access(DEVICE_1, "control")["route"] == "approval"
    assert not service.revoke_grant("grant-missing").ok


def test_change_password_invalidates_tokens() -> None:
    """改密后旧设备令牌与会话令牌立即失效."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    changed = service.change_password(ACCOUNT_A, PASSWORD, "new-password-2")
    assert changed.ok and changed.data["kicked_sessions"] >= 1
    assert service.refresh_session_token(data["device_token"]).error_code == TOKEN_REVOKED


def test_audit_permission_and_filter() -> None:
    """非所有者不能读审计; 所有者可按设备读取."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.set_trust_policy(DEVICE_1, True)

    login(service, ACCOUNT_B)
    denied = service.get_audit_log(DEVICE_1)
    assert not denied.ok and denied.error_code == PERMISSION_DENIED

    login(service, ACCOUNT_A)
    allowed = service.get_audit_log(DEVICE_1, "")
    assert allowed.ok and allowed.data["items"]
    events = {item["event"] for item in allowed.data["items"]}
    assert {"device.bind", "trust_policy.change"} <= events


def test_multi_device_and_multi_account() -> None:
    """一个账号可控制多台设备; 多账号可控制同一台设备（经授权）."""
    service = make_service(DEVICE_1, DEVICE_2)
    login(service, ACCOUNT_A)
    assert service.bind_device(DEVICE_1, "123456").ok
    assert service.bind_device(DEVICE_2, "123456").ok
    assert len(service.list_devices().data["items"]) == 2

    login(service, ACCOUNT_B)
    request_id = service.request_access(DEVICE_1, "control", "协作").data["request_id"]
    login(service, ACCOUNT_A)
    service.approve_access(request_id, 600, False)

    login(service, ACCOUNT_B)
    devices = service.list_devices().data["items"]
    assert any(item["device_id"] == DEVICE_1 and item["owner"] is False for item in devices)


def test_unauthenticated_calls_rejected() -> None:
    """未登录时账号命令统一返回凭据错误."""
    service = make_service()
    for result in (
        service.list_devices(),
        service.bind_device(DEVICE_1, "123456"),
        service.request_access(DEVICE_1, "view", ""),
        service.set_trust_policy(DEVICE_1, True),
        service.list_grants(DEVICE_1),
    ):
        assert not result.ok and result.error_code == CREDENTIAL_INVALID


def test_unknown_ids_contract_codes() -> None:
    """未知申请与授权返回契约错误码."""
    service = make_service()
    login(service, ACCOUNT_A)
    assert service.approve_access("req-missing", 60, False).error_code == REQUEST_NOT_FOUND
    assert service.reject_access("req-missing", "").error_code == REQUEST_NOT_FOUND
    assert service.revoke_grant("grant-missing").error_code == GRANT_INVALID
    assert service.list_grants(DEVICE_3).error_code == DEVICE_NOT_FOUND


def test_refresh_with_session_token_type_mismatch() -> None:
    """会话令牌不能当设备令牌用."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    result = service.refresh_session_token(data["session_token"])
    assert not result.ok and result.error_code == TOKEN_REVOKED


def test_existing_session_token_validation() -> None:
    """既有会话必须通过令牌校验: 伪造、吊销、过期、跨账号、类型不符一律拒绝."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")

    revoked_token, revoked_record = service.store.issue_token("session", data["account_id"], None, 900)
    revoked_record.revoked_at = time.time()
    assert service.resolve_access(DEVICE_1, existing_session=revoked_token)["route"] == "denied"

    expired_token, expired_record = service.store.issue_token("session", data["account_id"], None, 900)
    expired_record.expires_at = 0
    assert service.resolve_access(DEVICE_1, existing_session=expired_token)["route"] == "denied"

    foreign_token, _ = service.store.issue_token("session", "acc-foreign", None, 900)
    assert service.resolve_access(DEVICE_1, existing_session=foreign_token)["route"] == "denied"

    device_token, _ = service.store.issue_token("device", data["account_id"], None, 900)
    assert service.resolve_access(DEVICE_1, existing_session=device_token)["route"] == "denied"


def test_one_time_grant_not_consumed_by_request_precheck() -> None:
    """申请前的可行性预检不得消耗一次性授权, 只有真正判定连接才标记已用."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    login(service, ACCOUNT_B)
    request_id = service.request_access(DEVICE_1, "control", "临时").data["request_id"]
    login(service, ACCOUNT_A)
    assert service.approve_access(request_id, 600, True).ok

    login(service, ACCOUNT_B)
    again = service.request_access(DEVICE_1, "control", "再申请")
    assert not again.ok and again.data == {"route": "grant"}
    assert service.resolve_access(DEVICE_1, "control")["route"] == "grant"
    assert service.resolve_access(DEVICE_1, "control")["route"] == "approval"


def test_device_side_can_toggle_trust_policy() -> None:
    """被控端本机可对单台设备关闭免确认, 审计标记来源为 device."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")

    login(service, ACCOUNT_B)
    service.session_device_id = DEVICE_1
    changed = service.set_trust_policy(DEVICE_1, True)
    assert changed.ok and changed.data["require_confirm"] is True
    event = [item for item in service.store.audit if item.event == "trust_policy.change"][-1]
    assert event.detail["source"] == "device"


def test_device_side_sees_all_grants_and_can_revoke() -> None:
    """设备侧授权列表可见该设备全部授权, 并可即时撤销."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    login(service, ACCOUNT_B)
    request_id = service.request_access(DEVICE_1, "view", "查看").data["request_id"]
    login(service, ACCOUNT_A)
    service.approve_access(request_id, 600, False)
    other = service.store.create_grant(DEVICE_1, "acc-other", "view", 600)

    login(service, ACCOUNT_B)
    service.session_device_id = DEVICE_1
    listed = service.list_grants(DEVICE_1)
    assert listed.ok and len(listed.data["items"]) == 2
    assert service.revoke_grant(other.grant_id).ok
    assert not service.store.grants[other.grant_id].active


def test_session_invalidated_by_password_change_and_unbind() -> None:
    """改密与解绑都让当次会话即时失效, 旧会话不能继续调用命令."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    assert service.change_password(ACCOUNT_A, PASSWORD, "new-password-3").ok
    assert service.list_devices().error_code == TOKEN_REVOKED

    other = make_service()
    login(other, ACCOUNT_A)
    other.bind_device(DEVICE_1, "123456")
    _, record = other.store.issue_token("session", other.session_account_id or "", DEVICE_1, 900)
    other.session_token_hash = record.token_hash
    assert other.unbind_device(DEVICE_1).ok
    assert other.list_devices().error_code == TOKEN_REVOKED


def test_create_grant_and_scope_hierarchy() -> None:
    """直接签发授权; control 覆盖 view, 不覆盖 full."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.register_account(ACCOUNT_B, PASSWORD)
    created = service.create_grant(DEVICE_1, ACCOUNT_B, "control", 600, False, True)
    assert created.ok and created.data["transferable"] is True

    login(service, ACCOUNT_B)
    assert service.resolve_access(DEVICE_1, "view")["route"] == "grant"
    assert service.resolve_access(DEVICE_1, "control")["route"] == "grant"
    assert service.resolve_access(DEVICE_1, "full")["route"] == "approval"
    assert service.create_grant(DEVICE_1, ACCOUNT_A, "view", 600, False, False).error_code == PERMISSION_DENIED

    login(service, ACCOUNT_A)
    invalid = service.create_grant(DEVICE_1, ACCOUNT_A, "admin", 600, False, False)
    assert invalid.error_code == SCOPE_INVALID
    assert service.create_grant("missing-device", ACCOUNT_B, "view", 600, False, False).error_code == DEVICE_NOT_FOUND
    assert (
        service.create_grant(DEVICE_1, "nobody@example.com", "view", 600, False, False).error_code == ACCOUNT_NOT_FOUND
    )


def test_transfer_grant_one_layer() -> None:
    """转授成功且只剩一层; 子授权不能再转, 范围按父授权收窄."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.register_account(ACCOUNT_B, PASSWORD)
    service.register_account(ACCOUNT_C, PASSWORD)
    parent = service.create_grant(DEVICE_1, ACCOUNT_B, "control", 600, False, True).data

    login(service, ACCOUNT_B)
    child = service.transfer_grant(parent["grant_id"], ACCOUNT_C, 300, False, "view")
    assert child.ok
    assert child.data["parent_grant_id"] == parent["grant_id"]
    assert child.data["transferable"] is False and child.data["scope"] == "view"

    login(service, ACCOUNT_C)
    assert service.resolve_access(DEVICE_1, "view")["route"] == "grant"
    assert service.resolve_access(DEVICE_1, "control")["route"] == "approval"
    second = service.transfer_grant(child.data["grant_id"], ACCOUNT_B, 60, False)
    assert second.error_code == GRANT_NOT_TRANSFERABLE


def test_transfer_grant_rejections_and_audit() -> None:
    """转授越界一律拒绝并落审计."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.register_account(ACCOUNT_B, PASSWORD)
    service.register_account(ACCOUNT_C, PASSWORD)
    fixed = service.create_grant(DEVICE_1, ACCOUNT_B, "control", 600, False, False).data
    opened = service.create_grant(DEVICE_1, ACCOUNT_B, "control", 600, False, True).data
    one_time = service.create_grant(DEVICE_1, ACCOUNT_B, "view", 600, True, True).data

    login(service, ACCOUNT_B)
    assert service.transfer_grant(fixed["grant_id"], ACCOUNT_C, 60, False).error_code == GRANT_NOT_TRANSFERABLE
    assert service.transfer_grant(opened["grant_id"], ACCOUNT_C, 60, False, "full").error_code == GRANT_SCOPE_EXCEEDED
    assert service.transfer_grant(opened["grant_id"], ACCOUNT_C, 60, False, "admin").error_code == SCOPE_INVALID
    assert service.transfer_grant(opened["grant_id"], ACCOUNT_C, 100000, False).error_code == GRANT_SCOPE_EXCEEDED
    assert service.transfer_grant(one_time["grant_id"], ACCOUNT_C, 60, False).error_code == GRANT_SCOPE_EXCEEDED
    assert service.transfer_grant("grant-missing", ACCOUNT_C, 60, False).error_code == GRANT_INVALID

    login(service, ACCOUNT_A)
    assert service.transfer_grant(opened["grant_id"], ACCOUNT_C, 60, False).error_code == PERMISSION_DENIED

    service.store.grants[opened["grant_id"]].expires_at = time.time() - 1
    login(service, ACCOUNT_B)
    assert service.transfer_grant(opened["grant_id"], ACCOUNT_C, 60, False).error_code == GRANT_INVALID

    denied = [item for item in service.store.audit if item.event == "grant.transfer" and item.result == "deny"]
    assert len(denied) >= 6


def test_logout_revokes_host_tokens() -> None:
    """登出吊销本机设备令牌与会话令牌, 重复登出只返回文案."""
    service = make_service()
    data = login(service, ACCOUNT_A)
    first = service.logout_account()
    assert first.ok and first.data["status"] == "logged_out"
    assert first.data["kicked_sessions"] >= 1
    assert service.list_devices().error_code == CREDENTIAL_INVALID
    assert service.refresh_session_token(data["device_token"]).error_code == TOKEN_REVOKED

    again = service.logout_account()
    assert not again.ok and again.error_code is None


def test_revoke_device_token_kicks_sessions() -> None:
    """主动吊销设备令牌, 关联会话一并踢出并记审计."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    _, device_record = service.store.issue_token("device", service.session_account_id or "", DEVICE_1, 900)
    _, session_record = service.store.issue_token("session", service.session_account_id or "", DEVICE_1, 900)

    revoked = service.revoke_device_token(DEVICE_1)
    assert revoked.ok
    assert revoked.data["revoked_tokens"] == 2 and revoked.data["kicked_sessions"] == 1
    assert device_record.revoked_at is not None and session_record.revoked_at is not None

    login(service, ACCOUNT_B)
    assert service.revoke_device_token(DEVICE_1).error_code == PERMISSION_DENIED
    login(service, ACCOUNT_A)
    assert service.revoke_device_token("missing-device").error_code == DEVICE_NOT_FOUND


def test_device_presence_state_and_list_consistency() -> None:
    """心跳 30 秒、90 秒过期; list_devices 的 online 与 presence_state 同口径."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")

    unknown = service.list_devices().data["items"][0]
    assert unknown["presence_state"] == "unknown" and unknown["online"] is False and unknown["last_seen_at"] is None

    assert service.record_presence(DEVICE_1, "online") is True
    online = service.list_devices().data["items"][0]
    assert online["presence_state"] == "online" and online["online"] is True and online["last_seen_at"]

    service.store.devices[DEVICE_1].last_seen_at = time.time() - 100
    stale = service.list_devices().data["items"][0]
    assert stale["presence_state"] == "offline" and stale["online"] is False

    service.record_presence(DEVICE_1, "offline")
    assert service.presence_state(DEVICE_1) == "offline"
    assert service.record_presence("missing-device") is False


def test_full_grant_can_manage_device() -> None:
    """full 授权可按 v1.5 范围管理设备、授权与审计."""
    service = make_service()
    login(service, ACCOUNT_A)
    service.bind_device(DEVICE_1, "123456")
    service.register_account(ACCOUNT_B, PASSWORD)
    service.register_account(ACCOUNT_C, PASSWORD)
    assert service.create_grant(DEVICE_1, ACCOUNT_B, "full", 600, False, False).ok

    login(service, ACCOUNT_B)
    assert service.set_trust_policy(DEVICE_1, True).ok
    assert len(service.list_grants(DEVICE_1).data["items"]) == 1
    assert service.get_audit_log(DEVICE_1).ok
    assert service.create_grant(DEVICE_1, ACCOUNT_C, "view", 600, False, False).ok
    assert service.revoke_device_token(DEVICE_1).ok
