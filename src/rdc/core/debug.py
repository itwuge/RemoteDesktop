# ==========================================================================================
# 文件        : debug.py
# 所属目录    : src/rdc/core/
# 文件用途    : 调试开关、按模块分级与脱敏诊断包导出
# 架构定位    : 基础设施层
# 核心功能    : RDC_DEBUG 总开关、RDC_DEBUG_MODULES 分级、export_diagnostics 脱敏打包
# 职责边界    : 默认关闭; 诊断包不含口令、令牌、密钥; 不改系统设置
# ==========================================================================================

"""调试开关与诊断包."""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rdc.core import logging as rdc_logging

_TRUE = {"1", "true", "yes", "on"}


def debug_enabled(module: str = "") -> bool:
    """总开关关闭时一律返回 False; 总开关开启时按模块级别判定."""
    if os.environ.get("RDC_DEBUG", "0").lower() not in _TRUE:
        return False
    spec = os.environ.get("RDC_DEBUG_MODULES", "").strip()
    if not spec:
        return True
    levels = {}
    for item in spec.split(","):
        name, _, level = item.partition(":")
        levels[name.strip()] = level.strip() or "1"
    return any(module.startswith(name) and level not in ("0", "") for name, level in levels.items())


def dlog(module: str, event: str, **fields: Any) -> None:
    """仅调试开启时写事件; 关闭时零开销."""
    if not debug_enabled(module):
        return
    rdc_logging.log_event(module, event, level="DEBUG", **fields)


def diagnostics_path() -> Path:
    """诊断包目录: RDC_DIAG_DIR > RDC_BUILD_ROOT/diagnostics > ./diagnostics."""
    explicit = os.environ.get("RDC_DIAG_DIR")
    if explicit:
        return Path(explicit)
    build_root = os.environ.get("RDC_BUILD_ROOT")
    if build_root:
        return Path(build_root) / "diagnostics"
    return Path("diagnostics")


def export_diagnostics(extra: dict[str, Any] | None = None, destination: Path | None = None) -> dict[str, Any]:
    """导出脱敏诊断包.

    内容: 环境摘要、版本、最近事件、调用方给的指标摘要.
    不包含: 口令、令牌、密钥、私钥与证书内容.
    """
    directory = diagnostics_path()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    target = destination or directory / f"rdc-diag-{stamp}.json"

    payload: dict[str, Any] = {
        "generated_at": stamp,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "debug_enabled": debug_enabled(),
        "env": {
            "RDC_BUILD_ROOT": os.environ.get("RDC_BUILD_ROOT"),
            "RDC_LOG_DIR": os.environ.get("RDC_LOG_DIR"),
        },
        "recent_events": rdc_logging.recent_events(100),
        "extra": extra or {},
    }
    safe = rdc_logging.redact(payload)
    target.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    rdc_logging.log_event("core.debug", "diagnostics.export", path=str(target))
    return {"ok": True, "path": str(target), "bytes": target.stat().st_size}
