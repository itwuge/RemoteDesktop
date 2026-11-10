# ==========================================================================================
# 文件        : revocation.py
# 所属目录    : src/rdc/relay/
# 文件用途    : 从账号库读取令牌吊销状态, 让吊销对已在线连接立即生效
# 架构定位    : 中继服务层的吊销观测点, 只读账号库, 不写库、不改配置
# 核心功能    : 以 PRAGMA data_version 感知他进程提交; 按设备聚合最近一次签发令牌判定吊销
# 职责边界    : 不签发令牌; 不结束会话; 不解析口令与令牌明文
# 运行环境    : Ubuntu 20.04/22.04 x86_64, Python 3.8 及以上
# 依赖关系    : 仅标准库; 由 rdc.relay.server 装配
# ==========================================================================================

"""账号令牌吊销探测.

吊销动作由账号命令层写入 accounts.db（登出、改密、解绑设备）, 也可能由运维脚本直接改库.
中继进程不能依赖同进程内存状态, 因此这里按「设备」聚合最近一次签发的令牌:
该令牌已置 revoked_at 即判定该设备已吊销; 重新登录会签发更新的令牌, 自动恢复.

刷新时机: PRAGMA data_version 变化即刷新（他进程提交立刻可见）, 无变化时按 TTL 兜底,
因此数据面每帧调用一次也不会反复读盘.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from rdc.core.logging import log_event

# 无变化时的兜底刷新间隔: 只在本进程看不到 data_version 变化时生效
DEFAULT_TTL_S = 0.25
# 账号库打开失败后的重试间隔, 避免每条消息都尝试打开
RETRY_AFTER_ERROR_S = 2.0

_DEVICES_QUERY = "SELECT device_id, owner_account_id FROM devices"
_TOKENS_QUERY = "SELECT device_id, account_id, issued_at, revoked_at FROM tokens"


class AccountRevocationProbe:
    """账号库令牌吊销探测器; 只读, 可被多个连接线程并发调用.

    :param path: accounts.db 路径, 生产环境来自 RDC_DATA_DIR/accounts.db
    :param ttl_s: 无 data_version 变化时的兜底刷新间隔
    """

    def __init__(self, path: str | Path, ttl_s: float = DEFAULT_TTL_S) -> None:
        self._path = Path(path)
        self._ttl_s = max(0.0, ttl_s)
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._revision: int | None = None
        self._loaded_at = 0.0
        self._next_attempt = 0.0
        self._revoked_devices: frozenset[str] = frozenset()
        self._available = False

    @property
    def path(self) -> Path:
        """账号库路径, 只用于日志与健康检查展示."""
        return self._path

    def is_revoked(self, device_id: str) -> bool:
        """设备是否已吊销; 账号库不可用时按未吊销处理并限频告警."""
        if not device_id:
            return False
        with self._lock:
            self._refresh()
            return device_id in self._revoked_devices

    def snapshot(self) -> dict[str, Any]:
        """返回不含设备标识的观测值, 供健康检查与证据核对."""
        with self._lock:
            return {
                "available": self._available,
                "database": self._path.name,
                "revision": self._revision,
                "revoked_devices": len(self._revoked_devices),
                "checked": self._loaded_at > 0.0,
            }

    def close(self) -> None:
        """关闭只读连接; 幂等."""
        with self._lock:
            self._reset()

    # ------------------------------------------------------------------ 内部
    def _refresh(self) -> None:
        now = time.monotonic()
        connection = self._connection_for(now)
        if connection is None:
            return
        try:
            revision = int(connection.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error as error:
            self._fail(error)
            return
        if revision == self._revision and now - self._loaded_at < self._ttl_s:
            return
        try:
            devices = connection.execute(_DEVICES_QUERY).fetchall()
            tokens = connection.execute(_TOKENS_QUERY).fetchall()
        except sqlite3.Error as error:
            self._fail(error)
            return
        self._revoked_devices = _revoked_devices(devices, tokens)
        self._revision = revision
        self._loaded_at = now
        self._next_attempt = 0.0
        self._available = True

    def _connection_for(self, now: float) -> sqlite3.Connection | None:
        if self._connection is not None:
            return self._connection
        if now < self._next_attempt:
            return None
        self._next_attempt = now + RETRY_AFTER_ERROR_S
        if not self._path.is_file():
            # 还没建库: 视为没有吊销记录, 不告警
            return None
        uri = f"file:{self._path.as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0, check_same_thread=False)
        except (sqlite3.Error, ValueError) as error:
            self._fail(error)
            return None
        connection.isolation_level = None
        with suppress(sqlite3.Error):
            connection.execute("PRAGMA busy_timeout=1000")
        self._connection = connection
        return connection

    def _fail(self, error: sqlite3.Error | ValueError) -> None:
        self._available = False
        log_event(
            "relay.revocation",
            "accounts.unreadable",
            level="WARNING",
            error_code="ACCOUNTS_UNREADABLE",
            database=self._path.name,
            detail=f"{type(error).__name__}: {error}",
        )
        # 失败后限频重试, 避免每条消息都尝试打开并重复告警
        self._next_attempt = time.monotonic() + RETRY_AFTER_ERROR_S
        self._reset()

    def _reset(self) -> None:
        connection, self._connection = self._connection, None
        self._revision = None
        self._loaded_at = 0.0
        if connection is not None:
            with suppress(sqlite3.Error):
                connection.close()


def _revoked_devices(
    devices: Sequence[Sequence[Any]],
    tokens: Sequence[Sequence[Any]],
) -> frozenset[str]:
    """按设备聚合最近一次签发的令牌, 返回已吊销设备集合.

    设备级令牌直接绑定设备; 账号级令牌（device_id 为空）作用于该账号名下所有设备,
    两者统一按签发时间比较, 保证「吊销后重新登录」能自动恢复。
    """
    devices_by_account: dict[str, list[str]] = {}
    for row in devices:
        device = str(row[0] or "")
        owner = str(row[1] or "")
        if device and owner:
            devices_by_account.setdefault(owner, []).append(device)

    latest: dict[str, tuple[float, bool]] = {}
    for row in tokens:
        device = str(row[0] or "")
        targets = [device] if device else devices_by_account.get(str(row[1] or ""), [])
        revoked = row[3] is not None
        issued = float(row[2] or 0.0)
        for key in targets:
            current = latest.get(key)
            # 同一时刻签发的令牌里只要有一条被吊销, 就按已吊销处理, 不放过任何一条
            if current is None or issued > current[0] or (issued == current[0] and revoked):
                latest[key] = (issued, revoked)
    return frozenset(key for key, (_issued, revoked) in latest.items() if revoked)
