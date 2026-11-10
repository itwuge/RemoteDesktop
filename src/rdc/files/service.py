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

import base64
import hashlib
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdc.core.errors import (
    CHUNK_CHECKSUM_MISMATCH,
    FILE_OFFER_REJECTED,
    SESSION_CHANNEL_TIMEOUT,
    SESSION_CHANNEL_UNAVAILABLE,
    TRANSFER_HASH_MISMATCH,
    TRANSFER_IO_ERROR,
    TRANSFER_NOT_FOUND,
    TRANSFER_PERMISSION_DENIED,
    TRANSFER_RATE_INVALID,
)
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.files.digest import tree_digest
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
from rdc.session.channel import (
    CHANNEL_BACKPRESSURE,
    CHANNEL_CLOSED,
    ChannelInfo,
    SessionChannel,
    SessionChannelError,
)

_COPY_BLOCK = 1024 * 1024
_HASH_READ_BYTES = 1024 * 1024


@dataclass
class _RemoteState:
    """通道模式任务的发送端状态; 只存在内存, 断点续传由收端 .part 决定."""

    session_id: str
    transfer_id: str
    phase: str = "hashing"
    round: int = 0
    offer_sent: bool = False
    last_activity_at: float = 0.0
    hash_order: list[int] = field(default_factory=list)
    hash_pos: int = 0
    hash_offset: int = 0
    item_started: bool = False
    item_hasher: Any = field(default_factory=hashlib.sha256)
    tree: Any = field(default_factory=hashlib.sha256)
    acked: list[int] = field(default_factory=list)
    digest: str = ""


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
    channel: SessionChannel | None = None
    allow_local_staging: bool = True
    ack_timeout_s: float = 30.0
    inflight_limit_bytes: int = 4 * 1024 * 1024
    remote_states: dict[str, _RemoteState] = field(default_factory=dict)

    # ---------------------------------------------------------------- 会话通道
    def bind_channel(self, channel: SessionChannel | None) -> CommandResult:
        """接入或解除会话通道; 接入后传输不再做本机落盘复制."""
        self.channel = channel
        if channel is None:
            log_event("files.transfer", "transfer.channel_unbound")
            return CommandResult(ok=True, message="已解除会话通道", data=self.channel_data())
        info = channel.info()
        log_event(
            "files.transfer",
            "transfer.channel_bound",
            kind=info.kind,
            session_id=info.session_id,
            visible_to_relay=info.visible_to_relay,
            encrypted=info.encrypted,
        )
        return CommandResult(ok=True, message=self.channel_message(info), data=self.channel_data())

    def channel_status(self) -> CommandResult:
        """查询当前传输通道; 未接入时明确区分本机暂存与未接入."""
        return CommandResult(ok=True, message=self.channel_message(), data=self.channel_data())

    def channel_data(self) -> dict[str, Any]:
        info = self._channel_info()
        if info is None:
            return {
                "bound": False,
                "kind": "",
                "session_id": "",
                "encrypted": False,
                "visible_to_relay": False,
                "budget_kbps": 0,
                "mode": "local_staging" if self.allow_local_staging else "unbound",
            }
        return {
            "bound": True,
            "kind": info.kind,
            "session_id": info.session_id,
            "encrypted": info.encrypted,
            "visible_to_relay": info.visible_to_relay,
            "budget_kbps": info.budget_kbps,
            "mode": "channel",
        }

    def channel_message(self, info: ChannelInfo | None = None) -> str:
        """通道提示文案; 降级通道必须让用户知道载荷对中继可见."""
        current = info if info is not None else self._channel_info()
        if current is None:
            if self.allow_local_staging:
                return "未接入会话通道: 当前为本机暂存, 文件不会离开本机"
            return "未接入会话通道: 当前无法跨端传输"
        if current.visible_to_relay:
            return "已接入信令降级通道: 载荷对中继可见, 未达端到端加密"
        return f"已接入{current.kind}通道"

    def _channel_info(self) -> ChannelInfo | None:
        return None if self.channel is None else self.channel.info()

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
        if self.channel is not None:
            return self._start_remote_transfer(
                source,
                destination,
                rate_limit_kbps=rate_limit_kbps,
                video_floor_kbps=video_floor_kbps,
            )
        if not self.allow_local_staging:
            return CommandResult(
                ok=False,
                message="尚未接入会话通道, 无法跨端传输",
                error_code=SESSION_CHANNEL_UNAVAILABLE,
            )
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
            if job.remote:
                self._advance_remote(job)
            else:
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
        if not job.remote:
            TransferJournal.for_destination(job.destination).save(job)
        log_event("files.transfer", "transfer.paused", job_id=job_id, sent_bytes=job.sent_bytes, remote=job.remote)
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
        if job.remote:
            state = self.remote_states.get(job_id)
            if state is None:
                return CommandResult(ok=False, message="通道任务状态已丢失, 请重新开始传输")
            state.last_activity_at = now
            if state.phase != "hashing":
                state.round += 1
                state.transfer_id = f"{job_id}-{state.round}"
                state.phase = "offering"
                state.offer_sent = False
                state.acked = [0] * len(job.items)
                for item in job.items:
                    item.sent = 0
                job.sent_bytes = 0
                job.acked_bytes = 0
                job.phase = "waiting_accept"
        else:
            TransferJournal.for_destination(job.destination).save(job)
        log_event("files.transfer", "transfer.resumed", job_id=job_id, sent_bytes=job.sent_bytes, remote=job.remote)
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
        if job.remote:
            state = self.remote_states.pop(job_id, None)
            if state is not None and self.channel is not None:
                try:
                    self.channel.send(
                        "file_abort",
                        {
                            "session_id": state.session_id,
                            "transfer_id": state.transfer_id,
                            "reason": "用户取消",
                        },
                    )
                except SessionChannelError as exc:
                    log_event("files.transfer", "transfer.abort_send_failed", level="WARNING", code=exc.code)
        else:
            TransferJournal.for_destination(job.destination).remove(job.job_id)
        log_event("files.transfer", "transfer.canceled", job_id=job_id, sent_bytes=job.sent_bytes, remote=job.remote)
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

    # ---------------------------------------------------------------- 通道发送
    def _start_remote_transfer(
        self,
        source: Path,
        destination: Path,
        *,
        rate_limit_kbps: int,
        video_floor_kbps: int,
    ) -> CommandResult:
        """建立通道模式任务; 先读源文件摘要, 不写本机目标目录."""
        channel = self.channel
        if channel is None:
            return CommandResult(ok=False, message="尚未接入会话通道", error_code=SESSION_CHANNEL_UNAVAILABLE)
        info = channel.info()
        budget_kbps = rate_limit_kbps
        if info.budget_kbps > 0:
            usable = info.budget_kbps - video_floor_kbps
            if usable <= 0:
                return CommandResult(
                    ok=False,
                    message="链路预算不足以预留视频地板档, 已拒绝开始传输",
                    error_code=TRANSFER_RATE_INVALID,
                )
            budget_kbps = min(rate_limit_kbps, usable) if rate_limit_kbps > 0 else usable
        try:
            channel.open()
        except SessionChannelError as exc:
            return CommandResult(ok=False, message=exc.message, error_code=exc.code)
        try:
            items = _collect_items(source, destination)
        except OSError:
            return CommandResult(ok=False, message="读取源文件失败", error_code=TRANSFER_IO_ERROR)
        total = sum(item.size for item in items)
        capacity = max(1, min(CHUNK_SIZE, total))
        bucket = (
            TokenBucket(budget_kbps * 1000 / 8, capacity, clock=self.clock, initial_tokens=capacity)
            if budget_kbps > 0
            else None
        )
        job = TransferJob(
            job_id=f"transfer-{uuid.uuid4().hex[:10]}",
            source=source,
            destination=destination,
            items=items,
            total_bytes=total,
            chunk_size=CHUNK_SIZE,
            rate_limit_bps=budget_kbps * 1000,
            clamped=bool(rate_limit_kbps) and budget_kbps < rate_limit_kbps,
            channel="session",
            remote=True,
            phase="hashing",
            started_at=self.clock(),
            bucket=bucket,
        )
        self.jobs[job.job_id] = job
        state = _RemoteState(
            session_id=info.session_id,
            transfer_id=f"{job.job_id}-1",
            last_activity_at=self.clock(),
            hash_order=sorted(range(len(items)), key=lambda index: items[index].relative),
            acked=[0] * len(items),
        )
        self.remote_states[job.job_id] = state
        log_event(
            "files.transfer",
            "transfer.started",
            job_id=job.job_id,
            channel="session",
            files=len(items),
            total_bytes=total,
            visible_to_relay=info.visible_to_relay,
            encrypted=info.encrypted,
        )
        suffix = "; 信令降级通道对中继可见" if info.visible_to_relay else ""
        return CommandResult(
            ok=True,
            message=f"已排队 {len(items)} 个文件（{human_size(total)}）, 正在校验源文件（任务 {job.job_id}）{suffix}",
            data=job.snapshot(self.clock()),
        )

    def _advance_remote(self, job: TransferJob) -> None:
        state = self.remote_states.get(job.job_id)
        if state is None:
            self._fail_remote(job, TRANSFER_IO_ERROR, "通道任务状态已丢失, 请重新开始")
            return
        if not self._drain_remote(job, state):
            return
        now = self.clock()
        if state.phase != "hashing" and now - state.last_activity_at > self.ack_timeout_s:
            self._fail_remote(
                job,
                SESSION_CHANNEL_TIMEOUT,
                f"对端 {int(self.ack_timeout_s)} 秒未确认, 可能链路丢帧或对端未接线",
            )
            return
        if state.phase == "hashing":
            self._hash_step(job, state)
        elif state.phase == "offering":
            self._send_offer(job, state)
        elif state.phase == "accepted":
            self._send_chunks(job, state)

    def _drain_remote(self, job: TransferJob, state: _RemoteState) -> bool:
        channel = self.channel
        if channel is None:
            self._fail_remote(job, SESSION_CHANNEL_UNAVAILABLE, "会话通道已解除")
            return False
        for _ in range(64):
            try:
                message = channel.receive(0)
            except SessionChannelError as exc:
                if exc.code == CHANNEL_BACKPRESSURE:
                    continue
                code = SESSION_CHANNEL_UNAVAILABLE if exc.code == CHANNEL_CLOSED else exc.code
                self._fail_remote(job, code, exc.message)
                return False
            if message is None:
                break
            self._handle_remote(job, state, message[0], message[1])
            if job.state != "running":
                return False
        return True

    def _handle_remote(self, job: TransferJob, state: _RemoteState, msg_type: str, payload: dict[str, Any]) -> None:
        if str(payload.get("transfer_id") or "") != state.transfer_id:
            return
        if msg_type == "file_accept":
            self._on_accept(job, state, payload)
        elif msg_type == "file_ack":
            self._on_ack(job, state, payload)
        elif msg_type == "file_result":
            self._on_result(job, state, payload)
        elif msg_type == "file_abort":
            self._fail_remote(
                job,
                str(payload.get("error_code") or TRANSFER_IO_ERROR),
                str(payload.get("reason") or "对端中止了传输"),
            )
        elif msg_type == "error":
            self._fail_remote(
                job,
                str(payload.get("code") or "PROTOCOL_ERROR"),
                str(payload.get("message") or "对端返回协议错误"),
            )

    def _on_accept(self, job: TransferJob, state: _RemoteState, payload: dict[str, Any]) -> None:
        accepted = payload.get("accepted_items")
        if not isinstance(accepted, list):
            self._fail_remote(job, FILE_OFFER_REJECTED, "对端未返回可接收条目")
            return
        expected = {item.relative for item in job.items}
        if {str(relative) for relative in accepted} != expected:
            self._fail_remote(job, FILE_OFFER_REJECTED, "对端未接受全部条目, 已停止以免漏传")
            return
        resume: dict[str, int] = {}
        raw_resume = payload.get("resume")
        if isinstance(raw_resume, list):
            for entry in raw_resume:
                if not isinstance(entry, dict):
                    continue
                relative = entry.get("relative")
                value = entry.get("received_bytes")
                if isinstance(relative, str) and isinstance(value, int) and not isinstance(value, bool):
                    resume[relative] = value
        for index, item in enumerate(job.items):
            amount = resume.get(item.relative, 0)
            item.sent = max(0, min(item.size, amount))
            state.acked[index] = item.sent
        job.sent_bytes = sum(item.sent for item in job.items)
        job.acked_bytes = sum(state.acked)
        job.current_index = next(
            (index for index, item in enumerate(job.items) if item.sent < item.size),
            max(0, len(job.items) - 1),
        )
        state.phase = "accepted"
        state.offer_sent = False
        state.last_activity_at = self.clock()
        job.phase = "sending"
        log_event(
            "files.transfer",
            "transfer.accepted",
            job_id=job.job_id,
            resume_bytes=job.sent_bytes,
            files=len(job.items),
        )

    def _on_ack(self, job: TransferJob, state: _RemoteState, payload: dict[str, Any]) -> None:
        index = payload.get("file_index")
        acked = payload.get("acked_bytes")
        if not isinstance(index, int) or not 0 <= index < len(job.items) or not isinstance(acked, int):
            return
        ceiling = job.items[index].sent
        state.acked[index] = max(state.acked[index], min(acked, ceiling))
        job.acked_bytes = sum(state.acked)
        state.last_activity_at = self.clock()

    def _on_result(self, job: TransferJob, state: _RemoteState, payload: dict[str, Any]) -> None:
        digest = str(payload.get("digest") or "")
        if bool(payload.get("verified")) and digest == state.digest:
            job.digest = digest
            job.state = "completed"
            job.phase = "completed"
            job.acked_bytes = job.total_bytes
            job.ended_at = self.clock()
            self.remote_states.pop(job.job_id, None)
            log_event("files.transfer", "transfer.completed", job_id=job.job_id, digest=digest)
            return
        code = str(payload.get("error_code") or TRANSFER_HASH_MISMATCH)
        self._fail_remote(job, code, str(payload.get("message") or "对端整体校验失败, 文件未落盘"))

    def _hash_step(self, job: TransferJob, state: _RemoteState) -> None:
        remaining = self.poll_budget_bytes
        while remaining > 0:
            if state.hash_pos >= len(state.hash_order):
                state.digest = state.tree.hexdigest()
                state.phase = "offering"
                state.offer_sent = False
                job.phase = "waiting_accept"
                state.last_activity_at = self.clock()
                return
            item = job.items[state.hash_order[state.hash_pos]]
            if not state.item_started:
                state.tree.update(item.relative.encode("utf-8") + b"\0")
                state.item_started = True
            need = item.size - state.hash_offset
            if need <= 0:
                state.tree.update(b"\0")
                item.sha256 = state.item_hasher.hexdigest()
                self._next_hash_item(state)
                continue
            take = min(need, remaining, _HASH_READ_BYTES)
            try:
                with item.source.open("rb") as handle:
                    handle.seek(state.hash_offset)
                    data = handle.read(take)
            except OSError:
                self._fail_remote(job, TRANSFER_IO_ERROR, "读取源文件失败")
                return
            if not data:
                self._fail_remote(job, TRANSFER_IO_ERROR, "源文件在传输开始前发生变化")
                return
            state.item_hasher.update(data)
            state.tree.update(data)
            state.hash_offset += len(data)
            remaining -= len(data)
            if state.hash_offset >= item.size:
                state.tree.update(b"\0")
                item.sha256 = state.item_hasher.hexdigest()
                self._next_hash_item(state)

    @staticmethod
    def _next_hash_item(state: _RemoteState) -> None:
        state.hash_pos += 1
        state.hash_offset = 0
        state.item_started = False
        state.item_hasher = hashlib.sha256()

    def _send_offer(self, job: TransferJob, state: _RemoteState) -> None:
        channel = self.channel
        if channel is None or state.offer_sent:
            return
        payload = {
            "session_id": state.session_id,
            "transfer_id": state.transfer_id,
            "items": [{"relative": item.relative, "size": item.size, "sha256": item.sha256} for item in job.items],
            "total_bytes": job.total_bytes,
            "chunk_size": job.chunk_size,
            "digest_alg": "sha256",
            "digest": state.digest,
        }
        try:
            channel.send("file_offer", payload)
        except SessionChannelError as exc:
            if exc.code == CHANNEL_BACKPRESSURE:
                return
            self._fail_remote(job, exc.code, exc.message)
            return
        state.offer_sent = True
        state.last_activity_at = self.clock()
        job.phase = "waiting_accept"

    def _send_chunks(self, job: TransferJob, state: _RemoteState) -> None:
        channel = self.channel
        if channel is None:
            self._fail_remote(job, SESSION_CHANNEL_UNAVAILABLE, "会话通道已解除")
            return
        remaining = self.poll_budget_bytes
        while remaining > 0:
            index = next(
                (position for position, item in enumerate(job.items) if item.sent < item.size),
                None,
            )
            if index is None:
                self._send_complete(job, state)
                return
            item = job.items[index]
            inflight = job.sent_bytes - job.acked_bytes
            allowance = min(
                remaining,
                job.chunk_size,
                item.size - item.sent,
                self.inflight_limit_bytes - inflight,
            )
            if allowance <= 0:
                return
            if job.bucket is not None:
                allowance = min(allowance, job.bucket.take(allowance))
                if allowance <= 0:
                    return
            try:
                with item.source.open("rb") as handle:
                    handle.seek(item.sent)
                    data = handle.read(allowance)
            except OSError:
                self._fail_remote(job, TRANSFER_IO_ERROR, "读取源文件失败")
                return
            if not data:
                self._fail_remote(job, TRANSFER_IO_ERROR, "源文件在传输过程中发生变化")
                return
            payload = {
                "session_id": state.session_id,
                "transfer_id": state.transfer_id,
                "file_index": index,
                "offset": item.sent,
                "length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data": base64.b64encode(data).decode("ascii"),
            }
            try:
                channel.send("file_chunk", payload)
            except SessionChannelError as exc:
                if exc.code == CHANNEL_BACKPRESSURE:
                    return
                self._fail_remote(job, exc.code, exc.message)
                return
            item.sent += len(data)
            job.sent_bytes += len(data)
            job.current_index = index
            job.phase = "sending"
            state.last_activity_at = self.clock()
            remaining -= len(data)

    def _send_complete(self, job: TransferJob, state: _RemoteState) -> None:
        channel = self.channel
        if channel is None:
            self._fail_remote(job, SESSION_CHANNEL_UNAVAILABLE, "会话通道已解除")
            return
        try:
            channel.send(
                "file_complete",
                {
                    "session_id": state.session_id,
                    "transfer_id": state.transfer_id,
                    "total_bytes": job.total_bytes,
                    "file_count": len(job.items),
                    "digest": state.digest,
                },
            )
        except SessionChannelError as exc:
            if exc.code == CHANNEL_BACKPRESSURE:
                return
            self._fail_remote(job, exc.code, exc.message)
            return
        state.phase = "completing"
        state.last_activity_at = self.clock()
        job.phase = "verifying"

    def _fail_remote(self, job: TransferJob, code: str, message: str) -> None:
        job.state = "failed"
        job.phase = "failed"
        job.error_code = code
        job.error_message = message
        job.ended_at = self.clock()
        self.remote_states.pop(job.job_id, None)
        log_event("files.transfer", "transfer.failed", level="WARNING", job_id=job.job_id, code=code, reason=message)

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
    """按相对路径与内容计算整体哈希; 口径统一由 rdc.files.digest 提供."""
    return tree_digest(entries)
