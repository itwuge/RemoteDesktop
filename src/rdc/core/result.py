# ==========================================================================================
# 文件        : result.py
# 所属目录    : src/rdc/core/
# 文件用途    : 统一返回结构 CommandResult, 对应 desktop-v1 契约第二节
# 架构定位    : 基础设施层, 不依赖任何上层模块
# 核心功能    : 五字段固定返回; 序列化为可传输字典; 可选字段为空时不输出
# 职责边界    : 不做业务判断; 不拼用户文案以外的内容
# ==========================================================================================

"""统一返回结构.

契约依据: docs/contracts/desktop-v1.md 第二节.
五字段固定: ok, message, error_code, data, confirm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    """命令层统一返回结构, 界面层原样呈现 message."""

    ok: bool
    message: str
    error_code: str | None = None
    data: dict[str, Any] | None = None
    confirm: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典, 省略空的可选字段."""
        payload: dict[str, Any] = {"ok": self.ok, "message": self.message}
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        if self.data is not None:
            payload["data"] = self.data
        if self.confirm is not None:
            payload["confirm"] = self.confirm
        return payload


@dataclass(frozen=True)
class ConfirmRequest:
    """需要用户确认时附带的确认请求."""

    prompt: str
    token: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为契约要求的 prompt 与 token 两项."""
        payload = {"prompt": self.prompt, "token": self.token}
        payload.update(self.extra)
        return payload
