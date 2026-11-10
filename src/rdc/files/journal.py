# ==========================================================================================
# 文件        : journal.py
# 所属目录    : src/rdc/files/
# 文件用途    : 传输断点日志, 支撑进程中断后的续传恢复
# 架构定位    : 业务层持久化组件, 只依赖 model 与 core
# 核心功能    : 原子写入 job 进度、按目标目录枚举未完成任务、加载并校验相对路径
# 职责边界    : 不读写文件内容; 不记录源文件名以外的隐私字段; 不推进传输
# ==========================================================================================

"""断点续传日志.

落盘位置: ``<目标目录>/.rdc-transfers/<job_id>.json``。
内容仅含相对路径、大小与已传字节, 不含任何文件内容或摘要明文。
写入采用临时文件 + ``os.replace``, 避免进程中断留下半截 JSON。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from rdc.core.logging import log_event
from rdc.files.model import TransferItem, TransferJob

JOURNAL_DIR_NAME = ".rdc-transfers"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

_SAVE_FAILED = "transfer.journal.save_failed"
_LOAD_FAILED = "transfer.journal.load_failed"


def journal_root(destination: Path) -> Path:
    """返回目标目录下的日志目录."""
    return destination / JOURNAL_DIR_NAME


def _safe_relative(value: str) -> str:
    """校验并标准化日记中的相对路径, 拒绝绝对路径与上跳."""
    relative = Path(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("断点日志中的相对路径非法")
    return relative.as_posix()


class TransferJournal:
    """单个目标目录下的传输断点日志."""

    def __init__(self, destination: Path) -> None:
        self._root = journal_root(destination)

    @classmethod
    def for_destination(cls, destination: Path) -> TransferJournal:
        """按目标目录构造."""
        return cls(destination)

    def path_for(self, job_id: str) -> Path:
        """返回指定任务的日志路径; job_id 非法时抛 ValueError."""
        if not JOB_ID_PATTERN.match(job_id):
            raise ValueError("任务标识非法, 无法定位断点日志")
        return self._root / f"{job_id}.json"

    def save(self, job: TransferJob) -> bool:
        """原子写入任务进度; 失败只记日志并返回 False, 不打断传输."""
        try:
            path = self.path_for(job.job_id)
        except ValueError:
            return False
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "source": str(job.source),
            "source_is_dir": job.source.is_dir(),
            "destination": str(job.destination),
            "chunk_size": job.chunk_size,
            "rate_limit_bps": job.rate_limit_bps,
            "total_bytes": job.total_bytes,
            "items": [{"relative": item.relative, "size": item.size, "sent": item.sent} for item in job.items],
        }
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as error:
            log_event(
                "files.transfer",
                _SAVE_FAILED,
                level="WARNING",
                job_id=job.job_id,
                error_type=type(error).__name__,
            )
            return False
        return True

    def load(self, job_id: str) -> TransferJob | None:
        """读取并校验任务进度; 不存在或损坏时返回 None."""
        try:
            path = self.path_for(job_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return _job_from_payload(raw)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log_event(
                "files.transfer",
                _LOAD_FAILED,
                level="WARNING",
                job_id=job_id,
                error_type=type(error).__name__,
            )
            return None

    def pending_job_ids(self) -> list[str]:
        """列出该目标目录下未完成的 job 标识."""
        if not self._root.is_dir():
            return []
        identifiers: list[str] = []
        for entry in sorted(self._root.glob("*.json")):
            if JOB_ID_PATTERN.match(entry.stem):
                identifiers.append(entry.stem)
        return identifiers

    def remove(self, job_id: str) -> None:
        """删除任务日志; 不存在时静默返回, 其他失败记警告."""
        try:
            path = self.path_for(job_id)
            path.unlink(missing_ok=True)
        except ValueError:
            return
        except OSError as error:
            log_event(
                "files.transfer",
                "transfer.journal.remove_failed",
                level="WARNING",
                job_id=job_id,
                error_type=type(error).__name__,
            )


def _job_from_payload(raw: Any) -> TransferJob:
    """把 JSON 载荷还原为任务对象, 字段缺失或类型非法时抛 ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("断点日志根节点必须是对象")
    job_id = str(raw["job_id"])
    source = Path(str(raw["source"]))
    source_is_dir = bool(raw["source_is_dir"])
    destination = Path(str(raw["destination"]))
    chunk_size = int(raw["chunk_size"])
    rate_limit_bps = int(raw["rate_limit_bps"])
    total_bytes = int(raw["total_bytes"])
    raw_items = raw["items"]
    if not isinstance(raw_items, list):
        raise ValueError("断点日志 items 必须是数组")

    items: list[TransferItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise ValueError("断点日志条目必须是对象")
        relative = _safe_relative(str(entry["relative"]))
        size = int(entry["size"])
        sent = int(entry["sent"])
        if size < 0 or sent < 0:
            raise ValueError("断点日志中的大小不能为负数")
        item_source = source / relative if source_is_dir else source
        items.append(
            TransferItem(
                relative=relative,
                source=item_source,
                destination=destination / relative,
                size=size,
                sent=min(sent, size),
            )
        )
    return TransferJob(
        job_id=job_id,
        source=source,
        destination=destination,
        items=items,
        total_bytes=total_bytes,
        chunk_size=max(1, chunk_size),
        rate_limit_bps=max(0, rate_limit_bps),
        state="paused",
        sent_bytes=sum(item.sent for item in items),
    )
