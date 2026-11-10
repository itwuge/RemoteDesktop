# ==========================================================================================
# 文件        : accounts.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 账号、设备绑定、授权、申请制与免确认的命令函数, 对应契约 v1.2 与 v1.3
# 架构定位    : 命令层对外接口; 界面层只调本模块
# 核心功能    : 转发到注入的 AccountService; 提供 configure 供装配处替换实现
# 职责边界    : 不写业务判断; 不弹窗; 不打印; 不接收界面传入的身份凭据
# ==========================================================================================

"""账号与授权命令函数.

契约依据: docs/contracts/desktop-v1.md v1.2 第 14 至 26 条、v1.3 第 27 条.
"""

from __future__ import annotations

from pathlib import Path

from rdc.accounts.db import AccountStoreError, default_database_path
from rdc.accounts.service import AccountService
from rdc.accounts.store import AccountStore
from rdc.core.result import CommandResult

_service = AccountService()


def configure(service: AccountService) -> None:
    """由装配处注入账号后端."""
    global _service
    _service = service


def configure_persistent_store(path: str | Path | None = None) -> str | None:
    """装配落盘账号库; 成功返回 None, 不可用时退化为内存存储并返回用户可见原因.

    只替换内部存储实现, 命令函数签名、返回字段与错误码均不变.
    """
    try:
        store = AccountStore(default_database_path() if path is None else path)
    except AccountStoreError as exc:
        configure(AccountService())
        return f"账号数据文件不可用, 本次运行无法保存账号: {exc}"
    configure(AccountService(store=store))
    return None


def get_service() -> AccountService:
    """返回当前账号后端, 供测试与轮询断言."""
    return _service


def register_account(account: str, password: str) -> CommandResult:
    """注册账号."""
    return _service.register_account(account, password)


def login_account(account: str, password: str) -> CommandResult:
    """登录并签发设备令牌与会话令牌."""
    return _service.login_account(account, password)


def refresh_session_token(device_token: str) -> CommandResult:
    """用设备令牌换短时会话令牌."""
    return _service.refresh_session_token(device_token)


def bind_device(device_id: str, access_code: str) -> CommandResult:
    """绑定设备."""
    return _service.bind_device(device_id, access_code)


def unbind_device(device_id: str) -> CommandResult:
    """解绑设备."""
    return _service.unbind_device(device_id)


def list_devices() -> CommandResult:
    """列出当前账号可见设备."""
    return _service.list_devices()


def list_grants(device_id: str) -> CommandResult:
    """列出设备授权."""
    return _service.list_grants(device_id)


def revoke_grant(grant_id: str) -> CommandResult:
    """撤销授权."""
    return _service.revoke_grant(grant_id)


def request_access(device_id: str, scope: str, reason: str) -> CommandResult:
    """发起访问申请."""
    return _service.request_access(device_id, scope, reason)


def approve_access(request_id: str, ttl_seconds: int, one_time: bool) -> CommandResult:
    """审批通过并生成限时授权."""
    return _service.approve_access(request_id, ttl_seconds, one_time)


def reject_access(request_id: str, reason: str) -> CommandResult:
    """拒绝申请."""
    return _service.reject_access(request_id, reason)


def list_access_requests(direction: str) -> CommandResult:
    """按方向列出申请."""
    return _service.list_access_requests(direction)


def get_audit_log(device_id: str, since: str = "") -> CommandResult:
    """读取设备审计."""
    return _service.get_audit_log(device_id, since)


def set_trust_policy(device_id: str, require_confirm: bool) -> CommandResult:
    """切换免确认开关."""
    return _service.set_trust_policy(device_id, require_confirm)


def create_grant(
    device_id: str,
    subject: str,
    scope: str,
    ttl_seconds: int,
    one_time: bool,
    transferable: bool,
) -> CommandResult:
    """直接签发授权 (契约第 41 条)."""
    return _service.create_grant(device_id, subject, scope, ttl_seconds, one_time, transferable)


def transfer_grant(grant_id: str, subject: str, ttl_seconds: int, one_time: bool, scope: str = "") -> CommandResult:
    """转授授权, V1 只允许一层 (契约第 42 条)."""
    return _service.transfer_grant(grant_id, subject, ttl_seconds, one_time, scope)


def logout_account() -> CommandResult:
    """登出并吊销本机令牌 (契约第 43 条)."""
    return _service.logout_account()


def revoke_device_token(device_id: str) -> CommandResult:
    """主动吊销设备令牌 (契约第 44 条)."""
    return _service.revoke_device_token(device_id)
