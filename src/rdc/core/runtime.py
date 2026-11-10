# ==========================================================================================
# 文件        : runtime.py
# 所属目录    : src/rdc/core/
# 文件用途    : 打包运行时的数据目录、日志目录与本地配置装配
# 架构定位    : 基础设施层; 只在进程启动早期调用
# 核心功能    : 识别便携包; 让配置与日志跟随包目录; 已安装版落到用户本地目录
# 职责边界    : 不创建界面; 不发起网络; 不改变业务命令
# ==========================================================================================

"""运行时目录与配置.

便携包: 可执行文件旁存在 portable.marker 时, 配置与日志写入 <包目录>/data.
安装包: 配置与日志写入 %LOCALAPPDATA%/RDC (Windows) 或 ~/.local/share/rdc.
开发态: 不干预既有约定, 日志仍为工作目录下 logs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

APP_DIR_NAME = "RDC"
DATA_DIR_NAME = "data"
CONFIG_FILE_NAME = "config.json"
PORTABLE_MARKER = "portable.marker"
UPDATE_SOURCE_DIR = "update-source"
MANIFEST_FILE_NAME = "update-manifest.json"


def is_frozen() -> bool:
    """判断是否运行在打包产物中."""
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """打包产物所在目录; 开发态返回当前工作目录."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def is_portable() -> bool:
    """便携包判定: 可执行文件旁存在 portable.marker."""
    return is_frozen() and (executable_dir() / PORTABLE_MARKER).is_file()


def data_dir() -> Path:
    """运行时数据目录."""
    explicit = os.environ.get("RDC_DATA_DIR")
    if explicit:
        return Path(explicit)
    if is_portable():
        return executable_dir() / DATA_DIR_NAME
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / "rdc"


def load_config() -> dict[str, Any]:
    """读取本地 JSON 配置; 文件缺失或损坏返回空配置, 不阻塞启动."""
    path = data_dir() / CONFIG_FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def bootstrap_runtime() -> dict[str, Any]:
    """打包运行时装配目录与环境变量; 幂等且失败不阻塞启动."""
    if not is_frozen():
        return {}
    directory = data_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "logs").mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    os.environ.setdefault("RDC_DATA_DIR", str(directory))
    os.environ.setdefault("RDC_LOG_DIR", str(directory / "logs"))

    config = load_config()
    _apply_update_source(config)
    return config


def _apply_update_source(config: dict[str, Any]) -> None:
    """把 config.json 的更新源写入环境变量; 未配置时保留默认 GitHub Releases."""
    if os.environ.get("RDC_UPDATE_SOURCE", "").strip():
        return
    source = config.get("update_source")
    if isinstance(source, str) and source.strip():
        os.environ["RDC_UPDATE_SOURCE"] = source.strip()
        return
    repo = config.get("update_repo")
    if isinstance(repo, str) and repo.strip():
        os.environ["RDC_UPDATE_REPO"] = repo.strip()
