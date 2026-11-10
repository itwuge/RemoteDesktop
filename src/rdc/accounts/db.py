# ==========================================================================================
# 文件        : db.py
# 所属目录    : src/rdc/accounts/
# 文件用途    : 账号、设备、令牌、授权、申请与审计的 SQLite 落盘
# 架构定位    : 业务层内部存储后端, 只被 accounts.store 使用
# 核心功能    : 建库建表、版本与完整性校验、事务内批量写入、按插入顺序读取
# 职责边界    : 不解释业务语义; 不生成用户文案以外的内容; 不落明文口令与明文令牌
# ==========================================================================================

"""账号库的 SQLite 后端.

库文件默认位于运行时数据目录, 可用环境变量 `RDC_ACCOUNTS_DB` 指定.
启用 WAL; 每次写入在事务内完成; 版本不匹配或文件损坏时抛 `AccountStoreError`,
由调用方翻译成用户可见提示, 不允许直接冒泡成崩溃.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rdc.core.runtime import data_dir

SCHEMA_VERSION = 1
DATABASE_FILE_NAME = "accounts.db"
ENV_DATABASE_PATH = "RDC_ACCOUNTS_DB"

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        salt BLOB NOT NULL,
        password_hash BLOB NOT NULL,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_name_key ON accounts (name_key)",
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        owner_account_id TEXT,
        bound_at TEXT,
        require_confirm INTEGER NOT NULL DEFAULT 0,
        last_seen_at REAL,
        presence_offline INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tokens (
        token_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        account_id TEXT NOT NULL,
        device_id TEXT,
        issued_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        revoked_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS grants (
        grant_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        subject_account_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        expires_at REAL NOT NULL,
        transferable INTEGER NOT NULL DEFAULT 0,
        one_time INTEGER NOT NULL DEFAULT 0,
        used_at REAL,
        revoked_at REAL,
        parent_grant_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        requester_account_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at REAL NOT NULL,
        decided_at REAL,
        decided_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        event TEXT NOT NULL,
        actor TEXT NOT NULL,
        target TEXT NOT NULL,
        result TEXT NOT NULL,
        detail TEXT NOT NULL
    )
    """,
)

ACCOUNT_UPSERT = """
INSERT INTO accounts (account_id, name, name_key, salt, password_hash, created_at, password_changed_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(account_id) DO UPDATE SET
    name = excluded.name,
    name_key = excluded.name_key,
    salt = excluded.salt,
    password_hash = excluded.password_hash,
    created_at = excluded.created_at,
    password_changed_at = excluded.password_changed_at
"""

DEVICE_UPSERT = """
INSERT INTO devices (device_id, display_name, owner_account_id, bound_at, require_confirm, last_seen_at, presence_offline)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(device_id) DO UPDATE SET
    display_name = excluded.display_name,
    owner_account_id = excluded.owner_account_id,
    bound_at = excluded.bound_at,
    require_confirm = excluded.require_confirm,
    last_seen_at = excluded.last_seen_at,
    presence_offline = excluded.presence_offline
"""

TOKEN_UPSERT = """
INSERT INTO tokens (token_hash, kind, account_id, device_id, issued_at, expires_at, revoked_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(token_hash) DO UPDATE SET
    kind = excluded.kind,
    account_id = excluded.account_id,
    device_id = excluded.device_id,
    issued_at = excluded.issued_at,
    expires_at = excluded.expires_at,
    revoked_at = excluded.revoked_at
"""

GRANT_UPSERT = """
INSERT INTO grants (grant_id, device_id, subject_account_id, scope, expires_at, transferable, one_time,
                    used_at, revoked_at, parent_grant_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(grant_id) DO UPDATE SET
    device_id = excluded.device_id,
    subject_account_id = excluded.subject_account_id,
    scope = excluded.scope,
    expires_at = excluded.expires_at,
    transferable = excluded.transferable,
    one_time = excluded.one_time,
    used_at = excluded.used_at,
    revoked_at = excluded.revoked_at,
    parent_grant_id = excluded.parent_grant_id
"""

REQUEST_UPSERT = """
INSERT INTO requests (request_id, device_id, requester_account_id, scope, reason, status,
                      created_at, decided_at, decided_by)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(request_id) DO UPDATE SET
    device_id = excluded.device_id,
    requester_account_id = excluded.requester_account_id,
    scope = excluded.scope,
    reason = excluded.reason,
    status = excluded.status,
    created_at = excluded.created_at,
    decided_at = excluded.decided_at,
    decided_by = excluded.decided_by
"""

AUDIT_INSERT = """
INSERT INTO audit (ts, event, actor, target, result, detail) VALUES (?, ?, ?, ?, ?, ?)
"""


class AccountStoreError(RuntimeError):
    """账号库不可用; 文案面向用户, 不含口令与令牌明文."""


def default_database_path() -> Path:
    """账号库默认路径: 环境变量优先, 否则落在运行时数据目录下."""
    explicit = os.environ.get(ENV_DATABASE_PATH, "").strip()
    if explicit:
        return Path(explicit)
    return data_dir() / DATABASE_FILE_NAME


class AccountDatabase:
    """单个 SQLite 文件的读写封装; 线程安全, 写入走事务."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = self._connect()
        self._prepare()

    # ------------------------------------------------------------------ 生命周期
    def _connect(self) -> sqlite3.Connection:
        """建立连接并开启 WAL; 打不开时给出明确错误."""
        if str(self.path) != ":memory:":
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AccountStoreError(f"账号数据目录不可用: {self.path.parent}") from exc
        try:
            connection = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        except sqlite3.Error as exc:
            raise AccountStoreError(f"无法打开账号数据文件: {self.path}") from exc
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            connection.close()
            raise AccountStoreError(f"账号数据文件损坏或不是有效的数据库: {self.path}") from exc
        return connection

    def _prepare(self) -> None:
        """校验版本与完整性, 首次运行建库建表."""
        try:
            version_row = self._conn.execute("PRAGMA user_version").fetchone()
            check_row = self._conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            self.close()
            raise AccountStoreError(f"账号数据文件损坏或不是有效的数据库: {self.path}") from exc
        version = int(version_row[0]) if version_row else 0
        if version not in (0, SCHEMA_VERSION):
            self.close()
            raise AccountStoreError(
                f"账号数据文件版本不兼容: 文件版本 {version}, 当前程序需要 {SCHEMA_VERSION}, 路径 {self.path}"
            )
        check = str(check_row[0]).lower() if check_row else ""
        if check != "ok":
            self.close()
            raise AccountStoreError(f"账号数据文件完整性校验未通过: {self.path}")
        try:
            with self._conn:
                for statement in SCHEMA_STATEMENTS:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            self.close()
            raise AccountStoreError(f"账号数据表创建失败: {self.path}") from exc

    def close(self) -> None:
        """关闭连接; 幂等."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                return

    # ------------------------------------------------------------------ 读
    def _fetch(self, sql: str) -> list[tuple[Any, ...]]:
        """按插入顺序读取, 返回原始行; 读失败视为库不可用."""
        with self._lock:
            try:
                cursor = self._conn.execute(sql)
                return [tuple(row) for row in cursor.fetchall()]
            except sqlite3.Error as exc:
                raise AccountStoreError(f"账号数据读取失败: {self.path}") from exc

    def load_accounts(self) -> list[tuple[Any, ...]]:
        return self._fetch(
            "SELECT account_id, name, salt, password_hash, created_at, password_changed_at"
            " FROM accounts ORDER BY rowid"
        )

    def load_devices(self) -> list[tuple[Any, ...]]:
        return self._fetch(
            "SELECT device_id, display_name, owner_account_id, bound_at, require_confirm, last_seen_at,"
            " presence_offline FROM devices ORDER BY rowid"
        )

    def load_tokens(self) -> list[tuple[Any, ...]]:
        return self._fetch(
            "SELECT token_hash, kind, account_id, device_id, issued_at, expires_at, revoked_at"
            " FROM tokens ORDER BY rowid"
        )

    def load_grants(self) -> list[tuple[Any, ...]]:
        return self._fetch(
            "SELECT grant_id, device_id, subject_account_id, scope, expires_at, transferable, one_time,"
            " used_at, revoked_at, parent_grant_id FROM grants ORDER BY rowid"
        )

    def load_requests(self) -> list[tuple[Any, ...]]:
        return self._fetch(
            "SELECT request_id, device_id, requester_account_id, scope, reason, status, created_at,"
            " decided_at, decided_by FROM requests ORDER BY rowid"
        )

    def load_audit(self) -> list[tuple[Any, ...]]:
        return self._fetch("SELECT ts, event, actor, target, result, detail FROM audit ORDER BY id")

    # ------------------------------------------------------------------ 写
    def _execute_many(self, sql: str, params: Sequence[Sequence[Any]]) -> None:
        """同一事务内批量写入; 任一条失败整体回滚."""
        if not params:
            return
        with self._lock:
            try:
                with self._conn:
                    self._conn.executemany(sql, params)
            except sqlite3.IntegrityError as exc:
                raise AccountStoreError("同名账号已存在, 请直接登录") from exc
            except sqlite3.Error as exc:
                raise AccountStoreError(f"账号数据写入失败: {self.path}") from exc

    def save_accounts(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(ACCOUNT_UPSERT, rows)

    def save_devices(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(DEVICE_UPSERT, rows)

    def save_tokens(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(TOKEN_UPSERT, rows)

    def save_grants(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(GRANT_UPSERT, rows)

    def save_requests(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(REQUEST_UPSERT, rows)

    def append_audit(self, rows: Sequence[Sequence[Any]]) -> None:
        self._execute_many(AUDIT_INSERT, rows)
