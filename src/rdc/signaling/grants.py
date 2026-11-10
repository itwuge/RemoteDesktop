"""跨进程中继授予表: 信令进程签发落库, 中继进程按 relay_token 回查.

两个服务共用同一个 SQLite 账号库; 本表只存令牌摘要, 不落明文令牌.
表结构用 `CREATE TABLE IF NOT EXISTS` 增量创建, 旧库直接可开, 不触碰既有表与 user_version.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

GRANTS_TABLE = "relay_grants"

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS relay_grants (
        token_hash TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        device_id TEXT NOT NULL DEFAULT '',
        budget_kbps INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        scope TEXT NOT NULL DEFAULT 'control',
        wall_id TEXT NOT NULL DEFAULT '',
        cell INTEGER NOT NULL DEFAULT -1,
        account_id TEXT NOT NULL DEFAULT '',
        issued_at REAL NOT NULL
    )
    """,
    # 同一会话同一设备同一用途只保留一条: 并发签发时后写覆盖先写, 不会出现两条同时有效
    "CREATE UNIQUE INDEX IF NOT EXISTS relay_grants_session ON relay_grants (session_id, device_id, scope)",
    "CREATE INDEX IF NOT EXISTS relay_grants_expiry ON relay_grants (expires_at)",
)

_SQL_PUT = (
    "INSERT INTO relay_grants "
    "(token_hash, session_id, device_id, budget_kbps, expires_at, scope, wall_id, cell, account_id, issued_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(session_id, device_id, scope) DO UPDATE SET "
    "token_hash=excluded.token_hash, budget_kbps=excluded.budget_kbps, "
    "expires_at=excluded.expires_at, wall_id=excluded.wall_id, cell=excluded.cell, "
    "account_id=excluded.account_id, issued_at=excluded.issued_at"
)
_SQL_GET = (
    "SELECT session_id, budget_kbps, expires_at, device_id, scope, wall_id, cell, account_id "
    "FROM relay_grants WHERE token_hash = ?"
)
_SQL_DELETE = "DELETE FROM relay_grants WHERE token_hash = ?"
_SQL_DELETE_SESSION = "DELETE FROM relay_grants WHERE session_id = ?"
_SQL_DELETE_DEVICE_SESSION = "DELETE FROM relay_grants WHERE session_id = ? AND device_id = ?"
_SQL_DELETE_WALL = "DELETE FROM relay_grants WHERE wall_id = ?"
_SQL_PURGE = "DELETE FROM relay_grants WHERE expires_at <= ?"
_SQL_COUNT = "SELECT COUNT(*) FROM relay_grants"


class RelayGrantStoreError(RuntimeError):
    """跨进程授予表不可用; 文案面向运维, 不含令牌明文."""


def token_digest(relay_token: str) -> str:
    """令牌摘要, 用于落库与回查; 原文不进入数据库与日志."""
    return hashlib.sha256(relay_token.encode("utf-8")).hexdigest()


class RelayGrantStore:
    """共享账号库上的中继授予表读写封装; 多进程安全, 写入走事务."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RelayGrantStoreError(f"授予表目录不可用: {self.path.parent}") from exc
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            with self._conn:
                for statement in _DDL:
                    self._conn.execute(statement)
        except sqlite3.Error as exc:
            raise RelayGrantStoreError(f"无法初始化授予表: {self.path}") from exc

    # ------------------------------------------------------------------ 写入
    def put(self, relay_token: str, record: dict[str, Any]) -> None:
        """写入或覆盖一条授予; 同会话同设备同用途的旧行被替换."""
        params = (
            token_digest(relay_token),
            str(record.get("session_id", "")),
            str(record.get("device_id", "")),
            int(record.get("budget_kbps", 0)),
            float(record.get("expires_at", 0.0)),
            str(record.get("scope", "control")),
            str(record.get("wall_id", "")),
            int(record.get("cell", -1)),
            str(record.get("account_id", "")),
            float(record.get("issued_at", time.time())),
        )
        self._execute(_SQL_PUT, params)

    def delete(self, relay_token: str) -> None:
        self._execute(_SQL_DELETE, (token_digest(relay_token),))

    def delete_session(self, session_id: str) -> int:
        return self._execute(_SQL_DELETE_SESSION, (session_id,))

    def delete_device_session(self, session_id: str, device_id: str) -> int:
        return self._execute(_SQL_DELETE_DEVICE_SESSION, (session_id, device_id))

    def delete_wall(self, wall_id: str) -> int:
        return self._execute(_SQL_DELETE_WALL, (wall_id,))

    def purge_expired(self, now: float | None = None) -> int:
        return self._execute(_SQL_PURGE, (time.time() if now is None else now,))

    # ------------------------------------------------------------------ 读取
    def get(self, relay_token: str, now: float | None = None) -> dict[str, Any] | None:
        """按令牌取授予; 过期行顺手删除并返回 None, 不依赖信令进程是否已清理."""
        current = time.time() if now is None else now
        digest = token_digest(relay_token)
        with self._lock:
            if self._closed:
                raise RelayGrantStoreError("授予表已关闭")
            try:
                row = self._conn.execute(_SQL_GET, (digest,)).fetchone()
            except sqlite3.Error as exc:
                raise RelayGrantStoreError(f"读取授予表失败: {self.path}") from exc
        if row is None:
            return None
        if float(row[2]) <= current:
            self._execute(_SQL_DELETE, (digest,))
            return None
        return {
            "session_id": str(row[0]),
            "budget_kbps": int(row[1]),
            "expires_at": float(row[2]),
            "device_id": str(row[3]),
            "scope": str(row[4]),
            "wall_id": str(row[5]),
            "cell": int(row[6]),
            "account_id": str(row[7]),
        }

    def count(self) -> int:
        with self._lock:
            if self._closed:
                raise RelayGrantStoreError("授予表已关闭")
            try:
                row = self._conn.execute(_SQL_COUNT).fetchone()
            except sqlite3.Error as exc:
                raise RelayGrantStoreError(f"统计授予表失败: {self.path}") from exc
        return int(row[0]) if row is not None else 0

    def snapshot(self) -> dict[str, Any]:
        """健康检查可公开字段: 不含令牌、会话与设备标识."""
        try:
            return {"available": True, "database": str(self.path), "grants": self.count()}
        except RelayGrantStoreError:
            return {"available": False, "database": str(self.path), "grants": 0}

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._lock:
            if self._closed:
                raise RelayGrantStoreError("授予表已关闭")
            try:
                with self._conn:
                    cursor = self._conn.execute(sql, params)
                    return int(cursor.rowcount)
            except sqlite3.Error as exc:
                raise RelayGrantStoreError(f"写入授予表失败: {self.path}") from exc
