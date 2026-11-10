# ==========================================================================================
# 文件        : model.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件传输的条目、任务与进度快照数据结构
# 架构定位    : 业务层数据模型, 只依赖 ratelimit
# 核心功能    : 状态机取值、进度与速率、用户可见文案
# 职责边界    : 不做磁盘读写; 不做限速计算; 不建连
# ==========================================================================================

"""文件传输数据模型.

契约依据: docs/contracts/desktop-v1.md v1.4 第 32 至 36 条与 3.5 节进度语义.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdc.files.ratelimit import CHANNEL_LOW_PRIORITY, TokenBucket

CHUNK_SIZE = 256 * 1024
DEFAULT_POLL_BUDGET_BYTES = 1024 * 1024

JOB_STATES: frozenset[str] = frozenset({"running", "paused", "completed", "failed", "canceled"})
TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "canceled"})


@dataclass
class TransferItem:
    """单个文件的传输状态; sent 为已写入目标的字节数."""

    relative: str
    source: Path
    destination: Path
    size: int
    sent: int = 0
    sha256: str = ""


@dataclass
class TransferJob:
    """一次传输任务; 字段与进度快照严格对应契约."""

    job_id: str
    source: Path
    destination: Path
    items: list[TransferItem]
    total_bytes: int
    chunk_size: int = CHUNK_SIZE
    rate_limit_bps: int = 0
    state: str = "running"
    sent_bytes: int = 0
    acked_bytes: int = 0
    remote: bool = False
    phase: str = ""
    current_index: int = 0
    error_code: str | None = None
    error_message: str = ""
    digest: str | None = None
    clamped: bool = False
    channel: str = CHANNEL_LOW_PRIORITY
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    bucket: TokenBucket | None = field(default=None, repr=False)

    def snapshot(self, now: float) -> dict[str, Any]:
        """构造进度快照; 字段与契约 3.5 节一致."""
        elapsed = max(0.001, (self.ended_at or now) - self.started_at)
        if self.total_bytes <= 0:
            percent = 100.0 if self.state == "completed" else 0.0
        else:
            percent = round(self.sent_bytes * 100 / self.total_bytes, 1)
        speed_kbps = max(0, int(self.sent_bytes / 1000 / elapsed))
        remaining = max(0, self.total_bytes - self.sent_bytes)
        eta_seconds = None if speed_kbps <= 0 or self.state != "running" else int(remaining / (speed_kbps * 1000))
        current = self.items[min(self.current_index, len(self.items) - 1)] if self.items else None
        return {
            "job_id": self.job_id,
            "state": self.state,
            "source": str(self.source),
            "destination": str(self.destination),
            "total_bytes": self.total_bytes,
            "sent_bytes": self.sent_bytes,
            "acked_bytes": self.acked_bytes,
            "remote": self.remote,
            "phase": self.phase,
            "percent": percent,
            "speed_kbps": speed_kbps,
            "eta_seconds": eta_seconds,
            "file_count": len(self.items),
            "current_file": current.relative if current is not None else "",
            "chunk_size": self.chunk_size,
            "rate_limit_kbps": int(self.rate_limit_bps / 1000),
            "channel": self.channel,
            "clamped": self.clamped,
            "digest": self.digest,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    def message(self) -> str:
        """用户可见文案; 失败时原样保留错误文案."""
        if self.state == "completed":
            return f"传输完成, 整体哈希一致（{self.digest[:16] if self.digest else ''}）"
        if self.state == "failed":
            return self.error_message or "传输失败"
        if self.state == "paused":
            return "传输已暂停, 可继续续传"
        if self.state == "canceled":
            return "传输已取消"
        percent = 0.0 if self.total_bytes <= 0 else self.sent_bytes * 100 / self.total_bytes
        return f"正在传输 {percent:.1f}%"
