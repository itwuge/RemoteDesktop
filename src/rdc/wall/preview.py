# ==========================================================================================
# 文件        : preview.py
# 所属目录    : src/rdc/wall/
# 文件用途    : 屏幕墙预览令牌签发与校验, 对应契约 v1.5 第 47 条
# 架构定位    : 业务层内部存储, 只被屏幕墙命令与预览接入侧使用
# 核心功能    : 60 秒有效期、绑定墙+格+账号+设备、scope 固定 view、重签吊销旧令牌
# 职责边界    : 不替代身份鉴权; 不落明文令牌; 不打印; 不写日志
# ==========================================================================================

"""屏幕墙预览令牌.

契约依据: docs/contracts/desktop-v1.md v1.5 §3.7. 令牌只绑定单墙单格, 不得跨格跨墙跨账号复用.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

PREVIEW_TOKEN_TTL_SECONDS = 60
PREVIEW_SCOPE = "view"


def _hash_secret(secret: str) -> str:
    """令牌摘要; 只存摘要, 不存原文."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class PreviewTokenRecord:
    """预览令牌记录; 只存摘要与绑定信息."""

    token_hash: str
    wall_id: str
    cell: int
    account_id: str
    device_id: str
    issued_at: float
    expires_at: float
    scope: str = PREVIEW_SCOPE
    revoked_at: float | None = None

    @property
    def active(self) -> bool:
        """未过期且未吊销."""
        return self.revoked_at is None and self.expires_at > time.time()


class PreviewTokenStore:
    """预览令牌内存存储; 接入持久层时保持绑定字段与摘要策略不变."""

    def __init__(self) -> None:
        self.tokens: dict[str, PreviewTokenRecord] = {}

    def issue(
        self,
        wall_id: str,
        cell: int,
        account_id: str,
        device_id: str,
        now: float | None = None,
    ) -> tuple[str, PreviewTokenRecord]:
        """签发预览令牌; 同墙同格同账号重签即吊销旧令牌."""
        current = time.time() if now is None else now
        for record in self.tokens.values():
            if (
                record.wall_id == wall_id
                and record.cell == cell
                and record.account_id == account_id
                and record.revoked_at is None
            ):
                record.revoked_at = current
        secret = secrets.token_urlsafe(32)
        record = PreviewTokenRecord(
            token_hash=_hash_secret(secret),
            wall_id=wall_id,
            cell=cell,
            account_id=account_id,
            device_id=device_id,
            issued_at=current,
            expires_at=current + PREVIEW_TOKEN_TTL_SECONDS,
        )
        self.tokens[record.token_hash] = record
        return secret, record

    def lookup(self, secret: str) -> PreviewTokenRecord | None:
        """按明文查找令牌记录."""
        return self.tokens.get(_hash_secret(secret or ""))

    def revoke_for_wall(self, wall_id: str, now: float | None = None) -> int:
        """墙关闭时吊销该墙全部预览令牌, 返回吊销数."""
        current = time.time() if now is None else now
        revoked = 0
        for record in self.tokens.values():
            if record.wall_id == wall_id and record.revoked_at is None:
                record.revoked_at = current
                revoked += 1
        return revoked

    def validate(
        self,
        secret: str,
        wall_id: str,
        cell: int,
        account_id: str,
        now: float | None = None,
    ) -> tuple[PreviewTokenRecord | None, str]:
        """校验令牌; 失败返回原因, 由接入侧转换为 PREVIEW_TOKEN_INVALID."""
        current = time.time() if now is None else now
        record = self.lookup(secret)
        if record is None:
            return None, "预览令牌无效"
        if record.revoked_at is not None:
            return None, "预览令牌已被重签吊销"
        if record.expires_at <= current:
            return None, "预览令牌已过期"
        if record.wall_id != wall_id:
            return None, "预览令牌与墙不匹配"
        if record.cell != cell:
            return None, "预览令牌与格不匹配"
        if record.account_id != account_id:
            return None, "预览令牌与账号不匹配"
        if record.scope != PREVIEW_SCOPE:
            return None, "预览令牌范围非法"
        return record, ""
