# ==========================================================================================
# 文件        : scope.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件传输与剪贴板共用的授权范围判定（desktop-v1.6 第三节 scope 层级）
# 架构定位    : 业务层共享校验; 只依赖 core, 不碰账号存储
# 核心功能    : view < control < full 逐级覆盖判定; 不足与非法值分别给出拒绝结果
# 职责边界    : 不做授权签发与转授; 不读会话; 不修改契约正文
# ==========================================================================================

"""授权范围判定.

契约依据: docs/contracts/desktop-v1.md v1.6 第三节 scope 层级:

- 三层逐级包含 ``view`` < ``control`` < ``full``。
- ``control`` 含键鼠输入、剪贴板、拖拽与文件传输; ``view`` 一律拒绝这三类操作。
- 判定按「覆盖」: 授权范围等于或高于本次要求才算通过。

错误码统一取自 ``rdc.core.errors``: ``SCOPE_INVALID`` 表示取值不在三层之内,
``SCOPE_INSUFFICIENT`` 表示取值合法但层级不足。
"""

from __future__ import annotations

from rdc.core.errors import SCOPE_INSUFFICIENT, SCOPE_INVALID
from rdc.core.result import CommandResult

SCOPE_LEVELS: dict[str, int] = {"view": 0, "control": 1, "full": 2}
REQUIRED_SCOPE = "control"


def scope_covers(granted: str, required: str = REQUIRED_SCOPE) -> bool:
    """授权范围是否覆盖要求层级; 未知名一律不覆盖."""
    granted_level = SCOPE_LEVELS.get(granted)
    required_level = SCOPE_LEVELS.get(required)
    if granted_level is None or required_level is None:
        return False
    return granted_level >= required_level


def scope_rejection(granted: str, action: str) -> CommandResult | None:
    """返回 None 表示放行; 否则返回拒绝结果.

    :param granted: 当前会话的授权范围
    :param action: 用户可见的动作名, 如「文件传输」
    """
    if granted not in SCOPE_LEVELS:
        return CommandResult(
            ok=False,
            message=f"授权范围 {granted or '空'} 无效, 请重新选择",
            error_code=SCOPE_INVALID,
        )
    if not scope_covers(granted, REQUIRED_SCOPE):
        return CommandResult(
            ok=False,
            message=f"当前授权范围 {granted} 不足, {action}需要 control 权限, 请申请提升",
            error_code=SCOPE_INSUFFICIENT,
        )
    return None
