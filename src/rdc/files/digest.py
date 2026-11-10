# ==========================================================================================
# 文件        : digest.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件与文件树的 SHA-256 计算, 供发送端、接收端与整体校验共用
# 架构定位    : 业务层纯函数, 不持有状态
# 核心功能    : 单文件摘要; 按相对路径排序的相对路径+内容整体摘要
# 职责边界    : 不读写业务配置; 不记录明文; 不决定失败文案
# 运行环境    : Windows 与 Ubuntu, Python 3.10 及以上
# 依赖关系    : 标准库 hashlib
# ==========================================================================================

"""文件摘要.

整体摘要口径与 v1.4 一致: 按 relative 排序后依次写入 relative、NUL、文件内容、NUL。
接收端必须复用同一口径, 否则跨端整体校验永远不可能一致。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

BLOCK_SIZE = 1024 * 1024


def file_digest(path: Path) -> str:
    """计算单个文件的 SHA-256."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(entries: list[tuple[str, Path]]) -> str:
    """按相对路径与内容计算整体哈希, 与传输顺序无关."""
    digest = hashlib.sha256()
    for relative, path in sorted(entries, key=lambda entry: entry[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(BLOCK_SIZE), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()
