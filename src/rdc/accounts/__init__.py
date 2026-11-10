# ==========================================================================================
# 文件        : __init__.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 账号与授权包入口
# 架构定位    : 业务层
# 核心功能    : 导出 AccountService 与 AccountStore
# 职责边界    : 不做初始化副作用
# ==========================================================================================

"""账号与授权."""

from rdc.accounts.service import AccountService
from rdc.accounts.store import AccountStore

__all__ = ["AccountService", "AccountStore"]
