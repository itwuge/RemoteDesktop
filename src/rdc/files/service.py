# ==========================================================================================
# 文件        : service.py
# 所属目录    : src/rdc/files/
# 文件用途    : 文件传输引擎: 分块、逐块校验、整体哈希、断点续传、限速、取消
# 架构定位    : 业务层, 只依赖 core 与本包; 由命令层薄封装后暴露给界面
# 核心功能    : 目录递归、进度与速率、暂停/继续/取消、中断恢复、失败错误码
# 职责边界    : 不建连; 不碰视频编码; 不写内容明文日志; 不阻塞界面线程（由轮询推进）
# ==========================================================================================

"""文件传输引擎.

契约依据: docs/contracts/desktop-v1.md v1.4 第 32 至 36 条与 3.5 节硬约束。

实现要点:

- 分块 256 KiB, 每块写入后回读比对 SHA-256。
- 全部完成后按相对路径与内容计算整体哈希并比对。
- 断点续传: 已传字节落盘到 ``<目标目录>/.rdc-transfers``, 进程中断后可恢复为
  ``paused`` 再续传; 恢复时以目标文件实际大小收敛偏移, 最终整体哈希兜底。
- 低优先级通道: 传输预算 = 链路带宽 − 视频地板档预留, 见 ``ratelimit``。
- 目标端权限不足返回 ``TRANSFER_PERMISSION_DENIED``, 源不存在返回 ``TRANSFER_IO_ERROR``。
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rdc.core.errors import (
    CHUNK_CHECKSUM_MISMATCH,
    TRANSFER_HASH_MISMATCH,
    TRANSFER_IO_ERROR,
    TRANSFER_NOT_FOUND,
    TRANSFER_PERMISSION_DENIED,
    TRANSFER_RATE_INVALID,
)
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.files.journal import JOURNAL_DIR_NAME, TransferJournal
from rdc.files.model import (
    CHUNK_SIZE,
    DEFAULT_POLL_BUDGET_BYTES,
    TERMINAL_STATES,
    TransferItem,
    TransferJob,
)
from rdc.files.ratelimit import DEFAULT_VIDEO_FLOOR_KBPS, TokenBucket, plan_bandwidth
from rdc.files.scope import SCOPE_INVALID, SCOPE_LEVELS, scope_rejection

_COPY_BLOCK = 1024 * 1024


def human_size(size: int) -> str:
    """字节数转可读文本."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def default_destination_root(base: str | None = None) -> Path:
    """默认接收目录: 显式参数 > RDC_TRANSFER_DIR > 临时目录下 rdc-inbox."""
    explicit = base or os.environ.get("RDC_TRANSFER_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("TEMP", ".")) / "rdc-inbox"


@dataclass
class FileTransferService:
    """内存态传输后端; 由 ``get_transfer_progress`` 轮询推进固定预算."""

    jobs: dict[str, TransferJob] = field(default_factory=dict)
    poll_budget_bytes: int = DEFAULT_POLL_BUDGET_BYTES
    clock: Callable[[], float] = time.monotonic
    scope: str = "control"

    # ---------------------------------------------------------------- 授权范围
    def set_scope(self, scope: str) -> CommandResult:
        """绑定当前会话的授权范围; 取值不在三层之内返回 SCOPE_INVALID."""
        if scope not in SCOPE_LEVELS:
            return CommandResult(ok=False, message="授权范围无效, 请重新选择", error_code=SCOPE_INVALID)
        self.scope = scope
        log_event("files.transfer", "transfer.scope", scope=scope)
        return CommandResult(ok=True, message=f"授权范围已设置为 {scope}", data={"scope": scope})

    # ---------------------------------------------------------------- 命令入口
    def start_transfer(
        self,
        source_path: str,
        destination_dir: str,
        *,
        rate_limit_kbps: int = 0,
        link_kbps: int = 0,
        video_floor_kbps: int = DEFAULT_VIDEO_FLOOR_KBPS,
        scope: str | None = None,
    ) -> CommandResult:
        """创建传输任务并落盘断点日志; view 授权一律拒绝."""
        denied = scope_rejection(self.scope if scope is None else scope, "文件传输")
        if denied is not None:
            log_event("files.transfer", "transfer.scope_rejected", level="WARNING", scope=scope or self.scope)
            return denied
        if rate_limit_kbps < 0:
            return CommandResult(ok=False, message="限速不能为负数", error_code=TRANSFER_RATE_INVALID)

        source = Path(source_path)
        if not source.exists():
            return CommandResult(ok=False, message="源文件不存在", error_code=TRANSFER_IO_ERROR)

        budget_kbps = rate_limit_kbps
        clamped = False
        if link_kbps > 0:
            try:
                plan = plan_bandwidth(link_kbps, video_floor_kbps, rate_limit_kbps)
            except ValueError as error:
                return CommandResult(ok=False, message=str(error), error_code=TRANSFER_RATE_INVALID)
            budget_kbps = plan.transfer_budget_kbps
            clamped = plan.clamped
            if budget_kbps <= 0:
                return CommandResult(
                    ok=False,
                    message="链路带宽不足以预留视频地板档, 已拒绝开始传输",
                    error_code=TRANSFER_RATE_INVALID,
                )

        destination = Path(destination_dir)
        if destination.exists() and not destination.is_dir():
            return CommandResult(ok=False, message="目标目录不可用", error_code=TRANSFER_IO_ERROR)
        try:
            items = _collect_items(source, destination)
        except OSError:
            return CommandResult(ok=False, message="读取源文件失败", error_code=TRANSFER_IO_ERROR)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return CommandResult(ok=False, message="目标目录没有写入权限", error_code=TRANSFER_PERMISSION_DENIED)
        except OSError:
            return CommandResult(ok=False, message="目标目录不可用", error_code=TRANSFER_IO_ERROR)

        total = sum(item.size for item in items)
        rate_limit_bps = budget_kbps * 1000
        job = TransferJob(
            job_id=f"transfer-{uuid.uuid4().hex[:12]}",
            source=source,
            destination=destination,
            items=items,
            total_bytes=total,
            rate_limit_bps=rate_limit_bps,
            clamped=clamped,
            started_at=self.clock(),
            bucket=(
                TokenBucket(
                    rate_limit_bps,
                    CHUNK_SIZE,
                    clock=self.clock,
                    initial_tokens=float(CHUNK_SIZE),
                )
                if rate_limit_bps > 0
                else None
            ),
        )
        self.jobs[job.job_id] = job
        TransferJournal.for_destination(destination).save(job)
        log_event(
            "files.transfer",
            "transfer.started",
            job_id=job.job_id,
            total_bytes=total,
            file_count=len(items),
            rate_limit_kbps=budget_kbps,
            clamped=clamped,
            channel=job.channel,
        )
        return CommandResult(
            ok=True,
            message=f"已开始传输 {len(items)} 个文件, 共 {human_size(total)}（任务 {job.job_id}）",
            data=job.snapshot(self.clock()),
        )

    def get_transfer_progress(self, job_id: str) -> CommandResult:
        """查询进度; 内存实现每次轮询推进固定预算."""
        denied = self._scope_gate("文件传输")
        if denied is not None:
            return denied
        job = self.jobs.get(job_id)
        if job is None:
            return CommandResult(ok=False, message="传输任务不存在", error_code=TRANSFER_NOT_FOUND)
        if job.state == "running":
            self._advance(job, self.poll_budget_bytes)
        return CommandResult(
            ok=job.state != "failed",
            message=job.message(),
            error_code=job.error_code,
            data=job.snapshot(self.clock()),
        )

    def pause_transfer(self, job_id: str) -> CommandResult:
        """暂停任务, 保留已传字节与断点日志用于续传."""
        denied = self._scope_gate("文件传输")
        if denied is not None:
            return denied
        job = self.jobs.get(job_id)
        if job is None:
            return CommandResult(ok=False, message="传输任务不存在", error_code=TRANSFER_NOT_FOUND)
        if job.state != "running":
            return CommandResult(ok=False, message="只有进行中的任务可以暂停")
        job.state = "paused"
        job.ended_at = self.clock()
        TransferJournal.for_destination(job.destination).save(job)
        log_event("files.transfer", "transfer.paused", job_id=job_id, sent_bytes=job.sent_bytes)
        return CommandResult(ok=True, message="传输已暂停, 可继续续传", data=job.snapshot(self.clock()))

    def resume_transfer(self, job_id: str) -> CommandResult:
        """从已传字节处继续传输."""
        denied = self._scope_gate("文件传输")
        if denied is not None:
            return denied
        job = self.jobs.get(job_id)
        if job is None:
            return CommandResult(ok=False, message="传输任务不存在", error_code=TRANSFER_NOT_FOUND)
        if job.state != "paused":
            return CommandResult(ok=False, message="只有已暂停的任务可以继续")
        now = self.clock()
        if job.ended_at is not None:
            job.started_at += max(0.0, now - job.ended_at)
        job.state = "running"
        job.ended_at = None
        TransferJournal.for_destination(job.destination).save(job)
        log_event("files.transfer", "transfer.resumed", job_id=job_id, sent_bytes=job.sent_bytes)
        return CommandResult(
            ok=True,
            message=f"已从 {human_size(job.sent_bytes)} 处继续传输",
            data=job.snapshot(self.clock()),
        )

    def cancel_transfer(self, job_id: str) -> CommandResult:
        """取消任务并清掉断点日志; 已写入的目标分块保留."""
        denied = self._scope_gate("文件传输")
        if denied is not None:
            return denied
        job = self.jobs.get(job_id)
        if job is None:
            return CommandResult(ok=False, message="传输任务不存在", error_code=TRANSFER_NOT_FOUND)
        if job.state in {"completed", "canceled"}:
            return CommandResult(ok=False, message="任务已结束")
        job.state = "canceled"
        job.ended_at = self.clock()
        TransferJournal.for_destination(job.destination).remove(job.job_id)
        log_event("files.transfer", "transfer.canceled", job_id=job_id, sent_bytes=job.sent_bytes)
        return CommandResult(
            ok=True,
            message=f"传输已取消, 已传 {human_size(job.sent_bytes)}",
            data=job.snapshot(self.clock()),
        )

    # ---------------------------------------------------------------- 中断恢复
    def recover_pending(self, destination_dir: str) -> list[CommandResult]:
        """扫描目标目录的断点日志并恢复为 paused; 返回每个任务的恢复结果."""
        denied = self._scope_gate("文件传输")
        if denied is not None:
            return [denied]
        destination = Path(destination_dir)
        journal = TransferJournal.for_destination(destination)
        results: list[CommandResult] = []
        for job_id in journal.pending_job_ids():
            job = journal.load(job_id)
            if job is None:
                continue
            self._reconcile_offsets(job)
            if job.rate_limit_bps > 0:
                job.bucket = TokenBucket(
                    job.rate_limit_bps,
                    job.chunk_size,
                    clock=self.clock,
                    initial_tokens=float(job.chunk_size),
                )
            job.state = "paused"
            job.started_at = self.clock()
            self.jobs[job.job_id] = job
            log_event(
                "files.transfer",
                "transfer.recovered",
                job_id=job.job_id,
                sent_bytes=job.sent_bytes,
                file_count=len(job.items),
            )
            results.append(
                CommandResult(
                    ok=True,
                    message=f"已恢复中断任务, 可从 {human_size(job.sent_bytes)} 处继续（任务 {job.job_id}）",
                    data=job.snapshot(self.clock()),
                )
            )
        return results

    # ---------------------------------------------------------------- 内部推进
    def _scope_gate(self, action: str) -> CommandResult | None:
        """按当前授权范围判定; 拒绝时留结构化日志."""
        denied = scope_rejection(self.scope, action)
        if denied is not None:
            log_event(
                "files.transfer",
                "transfer.scope_rejected",
                level="WARNING",
                scope=self.scope,
                action=action,
            )
        return denied

    def _advance(self, job: TransferJob, budget_bytes: int) -> None:
        """按预算推进分块; 每个分块写后回读校验."""
        remaining = budget_bytes
        progressed = False
        while job.state == "running" and remaining > 0:
            while job.current_index < len(job.items) and _is_item_done(job.items[job.current_index]):
                item = job.items[job.current_index]
                if item.size == 0 and not item.destination.exists() and not self._create_empty(job, item):
                    return
                job.current_index += 1
            if job.current_index >= len(job.items):
                self._finalize(job)
                break
            item = job.items[job.current_index]
            quota = min(job.chunk_size, item.size - item.sent, remaining)
            if job.bucket is not None:
                quota = min(quota, job.bucket.take(quota))
            if quota <= 0:
                break
            if not self._transfer_chunk(job, item, quota):
                break
            remaining -= quota
            progressed = True
        if progressed and job.state == "running":
            TransferJournal.for_destination(job.destination).save(job)

    def _create_empty(self, job: TransferJob, item: TransferItem) -> bool:
        """为空文件在目标端建出同名空文件."""
        try:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            item.destination.touch(exist_ok=True)
        except PermissionError:
            self._fail(job, TRANSFER_PERMISSION_DENIED, "目标端权限不足, 无法写入")
            return False
        except OSError:
            self._fail(job, TRANSFER_IO_ERROR, "传输过程中读写失败")
            return False
        return True

    def _transfer_chunk(self, job: TransferJob, item: TransferItem, length: int) -> bool:
        """传一块并回读比对 SHA-256; 失败置任务为 failed."""
        try:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            with item.source.open("rb") as reader:
                reader.seek(item.sent)
                payload = reader.read(length)
            if not payload:
                self._fail(job, TRANSFER_IO_ERROR, "源文件在传输过程中变短, 无法继续")
                return False
            mode = "r+b" if item.destination.exists() else "wb"
            with item.destination.open(mode) as writer:
                writer.seek(item.sent)
                writer.write(payload)
            with item.destination.open("rb") as verifier:
                verifier.seek(item.sent)
                written = verifier.read(len(payload))
        except PermissionError:
            self._fail(job, TRANSFER_PERMISSION_DENIED, "目标端权限不足, 无法写入")
            return False
        except OSError:
            self._fail(job, TRANSFER_IO_ERROR, "传输过程中读写失败")
            return False

        if hashlib.sha256(written).digest() != hashlib.sha256(payload).digest():
            self._fail(job, CHUNK_CHECKSUM_MISMATCH, "分块回读校验失败, 请重试")
            return False
        item.sent += len(payload)
        job.sent_bytes += len(payload)
        return True

    def _finalize(self, job: TransferJob) -> None:
        """全部传完后比对整体哈希."""
        try:
            source_digest = _tree_digest([(item.relative, item.source) for item in job.items])
            target_digest = _tree_digest([(item.relative, item.destination) for item in job.items])
        except OSError:
            self._fail(job, TRANSFER_IO_ERROR, "完成校验时读取文件失败")
            return
        job.ended_at = self.clock()
        if source_digest != target_digest:
            self._fail(job, TRANSFER_HASH_MISMATCH, "整体哈希不一致, 传输已判定失败")
            return
        job.digest = source_digest
        job.state = "completed"
        TransferJournal.for_destination(job.destination).remove(job.job_id)
        log_event(
            "files.transfer",
            "transfer.completed",
            job_id=job.job_id,
            total_bytes=job.total_bytes,
            digest=source_digest,
        )

    def _fail(self, job: TransferJob, error_code: str, message: str) -> None:
        """置任务失败并留痕; 不记录文件内容."""
        job.state = "failed"
        job.error_code = error_code
        job.error_message = message
        job.ended_at = self.clock()
        log_event(
            "files.transfer",
            "transfer.failed",
            level="ERROR",
            error_code=error_code,
            job_id=job.job_id,
            sent_bytes=job.sent_bytes,
        )

    @staticmethod
    def _reconcile_offsets(job: TransferJob) -> None:
        """恢复时用目标文件实际大小收敛偏移, 防止中断留下越界断点."""
        for item in job.items:
            if not item.destination.exists():
                item.sent = 0
                continue
            try:
                actual = item.destination.stat().st_size
            except OSError:
                actual = 0
            item.sent = min(item.sent, actual, item.size)
        job.sent_bytes = sum(item.sent for item in job.items)
        job.current_index = next(
            (index for index, item in enumerate(job.items) if item.sent < item.size),
            len(job.items),
        )
        if job.state in TERMINAL_STATES:
            job.state = "paused"
            job.error_code = None
            job.error_message = ""


def _is_item_done(item: TransferItem) -> bool:
    """条目是否已传完."""
    return item.sent >= item.size


def _collect_items(source: Path, destination_root: Path) -> list[TransferItem]:
    """展开源路径为待传文件列表; 目录递归并保留相对路径."""
    if source.is_file():
        return [
            TransferItem(
                relative=source.name,
                source=source,
                destination=destination_root / source.name,
                size=source.stat().st_size,
            )
        ]
    items: list[TransferItem] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or JOURNAL_DIR_NAME in path.relative_to(source).parts:
            continue
        relative = path.relative_to(source).as_posix()
        items.append(
            TransferItem(
                relative=relative,
                source=path,
                destination=destination_root / relative,
                size=path.stat().st_size,
            )
        )
    return items


def _tree_digest(entries: list[tuple[str, Path]]) -> str:
    """按相对路径与内容计算整体哈希, 与传输顺序无关."""
    digest = hashlib.sha256()
    for relative, path in sorted(entries, key=lambda entry: entry[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_COPY_BLOCK), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()
