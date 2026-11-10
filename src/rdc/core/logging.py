# ==========================================================================================
# 文件        : logging.py
# 所属目录    : src/rdc/core/
# 文件用途    : 结构化日志统一封装, 固定字段、脱敏、落盘与轮转
# 架构定位    : 基础设施层, 所有模块只通过 log_event 写日志
# 核心功能    : JSON 行日志、字段固定、敏感字段脱敏、按大小轮转、最近事件环形缓冲
# 职责边界    : 不抛业务异常; 不打印明文凭据; 不依赖上层模块
# ==========================================================================================

"""结构化日志.

固定字段: timestamp、level、session_id、trace_id、module、event、duration_ms、error_code.
禁止字符串拼接式日志: 一律用 log_event(module, event, **fields).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import deque
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FILE_NAME = "rdc.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
RECENT_LIMIT = 200

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "access_code",
    "secret",
    "key",
    "private_key",
    "credential",
    "authorization",
    "cookie",
    "relay_token",
    "device_token",
    "session_token",
    "signature",
    "sig",
}
IP_KEYS = {"ip", "remote_ip", "client_ip", "peer_ip", "local_ip"}

REDACTED = "***"

_configured = False
_recent: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)


class RecentEventsHandler(logging.Handler):
    """把最近事件留在内存, 供脱敏诊断包使用."""

    def emit(self, record: logging.LogRecord) -> None:
        """追加一条已脱敏的事件."""
        payload = _payload_of(record)
        _recent.append(payload)


class JsonFormatter(logging.Formatter):
    """JSON 行格式; 字段固定且先脱敏再落盘."""

    def format(self, record: logging.LogRecord) -> str:
        """序列化单条日志."""
        payload = _payload_of(record)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _payload_of(record: logging.LogRecord) -> dict[str, Any]:
    """构造固定字段并合并扩展字段, 最后统一脱敏."""
    event = record.getMessage()
    payload: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, tz=UTC).astimezone().isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "session_id": None,
        "trace_id": None,
        "module": record.name,
        "event": event,
        "duration_ms": None,
        "error_code": None,
    }
    fields = getattr(record, "rdc_fields", None) or {}
    for key, value in fields.items():
        payload[key] = value
    return redact(payload)


def redact(value: Any, *, mask_ip: bool | None = None) -> Any:
    """递归脱敏; 敏感键一律替换为 ***, IP 按配置脱敏."""
    if mask_ip is None:
        mask_ip = os.environ.get("RDC_LOG_REDACT_IP", "0") in ("1", "true", "yes")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if (
                lowered in SENSITIVE_KEYS
                or any(part in lowered for part in ("password", "token", "secret"))
                or (mask_ip and lowered in IP_KEYS)
            ):
                result[key] = REDACTED
            else:
                result[key] = redact(item, mask_ip=mask_ip)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, mask_ip=mask_ip) for item in value]
    if isinstance(value, str) and len(value) > 16 and value.startswith("rdc_"):
        return REDACTED
    return value


def log_dir() -> Path:
    """日志目录: RDC_LOG_DIR > RDC_BUILD_ROOT/logs/rdc > ./logs."""
    explicit = os.environ.get("RDC_LOG_DIR")
    if explicit:
        return Path(explicit)
    build_root = os.environ.get("RDC_BUILD_ROOT")
    if build_root:
        return Path(build_root) / "logs" / "rdc"
    return Path("logs")


def configure(level: str = "INFO", *, console: bool = False) -> None:
    """初始化 root logger; 幂等, 重复调用不叠加 handler."""
    global _configured
    if _configured:
        if level:
            logging.getLogger("rdc").setLevel(level.upper())
        return

    directory = log_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / LOG_FILE_NAME, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        # 便携包放在只读位置时退到系统临时目录, 保证错误仍有落盘证据
        directory = Path(tempfile.gettempdir()) / "rdc-logs"
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            directory / LOG_FILE_NAME, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    root = logging.getLogger("rdc")
    root.setLevel((level or "INFO").upper())
    root.propagate = False
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    recent_handler = RecentEventsHandler(level=logging.DEBUG)
    root.addHandler(recent_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(JsonFormatter())
        root.addHandler(console_handler)

    _configured = True


def get_logger(module: str) -> logging.Logger:
    """取模块 logger; 名称统一为 rdc.<module>."""
    configure()
    name = module if module.startswith("rdc.") else f"rdc.{module}"
    return logging.getLogger(name)


def log_event(
    module: str,
    event: str,
    *,
    level: str = "INFO",
    duration_ms: float | None = None,
    error_code: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    **fields: Any,
) -> None:
    """写一条结构化事件; 扩展字段自动并入并脱敏."""
    logger = get_logger(module)
    payload: dict[str, Any] = dict(fields)
    payload["duration_ms"] = duration_ms
    payload["error_code"] = error_code
    payload["session_id"] = session_id
    payload["trace_id"] = trace_id
    logger.log(
        logging.getLevelName(level.upper()) if isinstance(level, str) else level, event, extra={"rdc_fields": payload}
    )


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    """读取最近事件副本, 供诊断包与冒烟抽检."""
    return list(_recent)[-limit:]


def reset_for_tests() -> None:
    """测试用: 清空环形缓冲与已初始化标记."""
    global _configured
    _recent.clear()
    _configured = False
    root = logging.getLogger("rdc")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
