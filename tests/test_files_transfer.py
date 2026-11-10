"""文件传输: 分块、断点续传、中断恢复、限速、目录递归、取消与拖拽触发."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from rdc.core import errors
from rdc.core.logging import recent_events, reset_for_tests
from rdc.files import CHUNK_SIZE, FileTransferService, TransferJournal, handle_drop, plan_bandwidth
from rdc.files.dnd import NO_SESSION_MESSAGE
from rdc.files.protocol import TRANSFER_MESSAGES, missing_protocol_messages, wire_plan
from rdc.files.ratelimit import TokenBucket
from rdc.files.scope import SCOPE_INSUFFICIENT, SCOPE_INVALID, scope_covers
from rdc.signaling.messages import MESSAGE_TYPES


def _make_file(path: Path, size: int, *, fill: bytes = b"") -> str:
    """生成恰好 size 字节的测试文件, 返回 SHA-256; fill 不为空时整体填充该字节."""
    payload = (fill * size)[:size] if fill else (bytes(range(256)) * (size // 256 + 1))[:size]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _drain(service: FileTransferService, job_id: str, limit: int = 40) -> dict[str, object]:
    """轮询到任务结束, 返回最后一帧进度."""
    result = service.get_transfer_progress(job_id)
    for _ in range(limit):
        data = result.data or {}
        if data.get("state") in {"completed", "failed", "canceled"}:
            return data
        result = service.get_transfer_progress(job_id)
    assert result.data is not None
    return result.data


# ---------------------------------------------------------------- 正确性
def test_single_file_chunked_with_matching_hash(tmp_path: Path) -> None:
    """单文件分块传完, 目标内容与整体哈希一致."""
    source = tmp_path / "source.bin"
    digest = _make_file(source, CHUNK_SIZE + 1234)
    service = FileTransferService(poll_budget_bytes=200_000)

    started = service.start_transfer(str(source), str(tmp_path / "inbox"))
    assert started.ok and started.data is not None
    job_id = str(started.data["job_id"])
    assert job_id in started.message
    data = _drain(service, job_id)

    assert data["state"] == "completed"
    assert data["percent"] == 100.0
    assert data["digest"] is not None
    assert hashlib.sha256((tmp_path / "inbox" / "source.bin").read_bytes()).hexdigest() == digest
    assert data["chunk_size"] == CHUNK_SIZE


def test_directory_recursion_and_empty_file(tmp_path: Path) -> None:
    """目录递归保留相对路径, 空文件也能在目标端建出."""
    root = tmp_path / "tree"
    _make_file(root / "a" / "one.bin", 4096)
    _make_file(root / "b" / "two.bin", 10)
    (root / "a" / "empty.txt").write_bytes(b"")

    service = FileTransferService()
    job_id = str(service.start_transfer(str(root), str(tmp_path / "inbox")).data["job_id"])
    data = _drain(service, job_id)

    assert data["state"] == "completed"
    assert data["file_count"] == 3
    assert (tmp_path / "inbox" / "a" / "one.bin").stat().st_size == 4096
    assert (tmp_path / "inbox" / "b" / "two.bin").read_bytes() == (root / "b" / "two.bin").read_bytes()
    assert (tmp_path / "inbox" / "a" / "empty.txt").exists()


def test_pause_resume_continues_from_offset(tmp_path: Path) -> None:
    """暂停后从已传字节续传, 不重传已写入部分."""
    source = tmp_path / "big.bin"
    digest = _make_file(source, 50_000)
    service = FileTransferService(poll_budget_bytes=10_000)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "inbox")).data["job_id"])

    service.get_transfer_progress(job_id)
    service.get_transfer_progress(job_id)
    paused = service.pause_transfer(job_id)
    assert paused.ok and paused.data is not None
    assert paused.data["sent_bytes"] == 20_000
    assert paused.data["state"] == "paused"

    resumed = service.resume_transfer(job_id)
    assert resumed.ok and resumed.data["sent_bytes"] == 20_000
    data = _drain(service, job_id)
    assert data["state"] == "completed"
    assert data["digest"] is not None
    assert hashlib.sha256((tmp_path / "inbox" / "big.bin").read_bytes()).hexdigest() == digest


# ---------------------------------------------------------------- 中断恢复
def test_interrupted_transfer_recovers_from_journal(tmp_path: Path) -> None:
    """进程中断后由断点日志恢复为 paused, 续传后整体哈希仍一致."""
    source = tmp_path / "interrupt.bin"
    digest = _make_file(source, 400_000)
    destination = tmp_path / "inbox"
    service = FileTransferService(poll_budget_bytes=100_000)
    job_id = str(service.start_transfer(str(source), str(destination)).data["job_id"])
    service.get_transfer_progress(job_id)

    journal_path = TransferJournal.for_destination(destination).path_for(job_id)
    assert journal_path.exists()
    raw_journal = journal_path.read_text(encoding="utf-8")
    assert source.name in raw_journal
    assert digest not in raw_journal

    # 模拟进程退出: 丢弃内存任务, 由新实例从磁盘恢复
    restarted = FileTransferService(poll_budget_bytes=100_000)
    recovered = restarted.recover_pending(str(destination))
    assert len(recovered) == 1
    assert recovered[0].ok and recovered[0].data is not None
    assert recovered[0].data["state"] == "paused"
    assert recovered[0].data["sent_bytes"] == 100_000

    assert restarted.resume_transfer(job_id).ok
    data = _drain(restarted, job_id)
    assert data["state"] == "completed"
    assert data["digest"] is not None
    assert hashlib.sha256((destination / source.name).read_bytes()).hexdigest() == digest
    assert not journal_path.exists()


def test_cancel_stops_progress_and_removes_journal(tmp_path: Path) -> None:
    """取消后不再推进, 断点日志被清理."""
    source = tmp_path / "cancel.bin"
    _make_file(source, 60_000)
    destination = tmp_path / "inbox"
    service = FileTransferService(poll_budget_bytes=1_000)
    job_id = str(service.start_transfer(str(source), str(destination)).data["job_id"])
    service.get_transfer_progress(job_id)

    assert service.cancel_transfer(job_id).ok
    progress = service.get_transfer_progress(job_id)
    assert progress.data["state"] == "canceled"
    assert not TransferJournal.for_destination(destination).path_for(job_id).exists()
    assert not service.resume_transfer(job_id).ok


def test_whole_hash_mismatch_detected(tmp_path: Path) -> None:
    """目标分块被外部篡改时, 整体哈希不一致使任务失败."""
    source = tmp_path / "tamper.bin"
    _make_file(source, 400_000)
    destination = tmp_path / "inbox"
    service = FileTransferService(poll_budget_bytes=100_000)
    job_id = str(service.start_transfer(str(source), str(destination)).data["job_id"])
    service.get_transfer_progress(job_id)

    target = destination / source.name
    with target.open("r+b") as handle:
        handle.write(b"\x00" * 8)
    data = _drain(service, job_id)
    assert data["state"] == "failed"
    assert data["error_code"] == errors.TRANSFER_HASH_MISMATCH


def test_missing_source_and_unknown_job(tmp_path: Path) -> None:
    """源不存在与任务不存在返回契约错误码."""
    service = FileTransferService()
    missing = service.start_transfer(str(tmp_path / "none.bin"), str(tmp_path / "inbox"))
    assert not missing.ok and missing.error_code == errors.TRANSFER_IO_ERROR
    assert service.get_transfer_progress("transfer-nope").error_code == errors.TRANSFER_NOT_FOUND


def test_permission_denied_maps_to_contract_code(tmp_path: Path) -> None:
    """目标端只读时返回 TRANSFER_PERMISSION_DENIED."""
    source = tmp_path / "readonly.bin"
    _make_file(source, 300_000)
    destination = tmp_path / "inbox"
    destination.mkdir()
    target = destination / source.name
    target.write_bytes(b"")
    os.chmod(target, stat.S_IREAD)
    if not _write_blocked(target):
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        pytest.skip("当前运行账号不受文件只读属性限制, 无法构造权限不足场景")

    service = FileTransferService()
    try:
        job_id = str(service.start_transfer(str(source), str(destination)).data["job_id"])
        data = _drain(service, job_id)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    assert data["state"] == "failed"
    assert data["error_code"] == errors.TRANSFER_PERMISSION_DENIED


def _write_blocked(path: Path) -> bool:
    """当前账号是否被只读属性挡住写打开."""
    try:
        with path.open("r+b"):
            return False
    except PermissionError:
        return True


# ---------------------------------------------------------------- 限速与带宽
def test_token_bucket_exact_rate_with_fake_clock() -> None:
    """令牌桶在 1.5 秒内恰好放行 rate × 1.5 字节."""
    clock = {"now": 0.0}
    bucket = TokenBucket(64_000, 65_536, clock=lambda: clock["now"])
    assert bucket.take(10_000) == 0

    clock["now"] = 0.5
    assert bucket.take(100_000) == 32_000
    assert bucket.take(100_000) == 0

    clock["now"] = 1.5
    assert bucket.take(100_000) == 64_000


def test_rate_limit_throttles_between_polls(tmp_path: Path) -> None:
    """首次只放行一个分块, 时钟不前进时不再放行, 前进 1 秒只多放行限速值."""
    source = tmp_path / "throttle.bin"
    _make_file(source, 1_000_000)
    clock = {"now": 0.0}
    service = FileTransferService(poll_budget_bytes=1_000_000, clock=lambda: clock["now"])
    started = service.start_transfer(str(source), str(tmp_path / "inbox"), rate_limit_kbps=32)
    job_id = str(started.data["job_id"])

    first = service.get_transfer_progress(job_id)
    second = service.get_transfer_progress(job_id)
    assert first.data["sent_bytes"] == CHUNK_SIZE
    assert second.data["sent_bytes"] == CHUNK_SIZE
    assert first.data["channel"] == "low_priority"
    assert first.data["rate_limit_kbps"] == 32

    clock["now"] = 1.0
    third = service.get_transfer_progress(job_id)
    assert third.data["sent_bytes"] - second.data["sent_bytes"] == 32_000


def test_bandwidth_budget_reserves_video_floor(tmp_path: Path) -> None:
    """传输预算等于链路带宽减视频地板档, 超出时收敛并标记 clamped."""
    plan = plan_bandwidth(link_kbps=10_000, video_floor_kbps=2_500, requested_kbps=9_000)
    assert plan.transfer_budget_kbps == 7_500
    assert plan.clamped is True
    unlimited = plan_bandwidth(link_kbps=10_000, video_floor_kbps=2_500)
    assert unlimited.transfer_budget_kbps == 7_500
    assert unlimited.clamped is False
    with pytest.raises(ValueError):
        plan_bandwidth(link_kbps=-1)

    source = tmp_path / "budget.bin"
    _make_file(source, 4_096)
    service = FileTransferService()
    started = service.start_transfer(str(source), str(tmp_path / "inbox"), rate_limit_kbps=9_000, link_kbps=10_000)
    assert started.ok and started.data is not None
    assert started.data["rate_limit_kbps"] == 7_500
    assert started.data["clamped"] is True

    rejected = service.start_transfer(str(source), str(tmp_path / "other"), link_kbps=2_000)
    assert not rejected.ok
    assert rejected.error_code == errors.TRANSFER_RATE_INVALID


# ---------------------------------------------------------------- 拖拽触发
def test_drop_requires_connected_session(tmp_path: Path) -> None:
    """未建立会话时拒绝拖拽并给出明确文案."""
    source = tmp_path / "drop.bin"
    _make_file(source, 1_024)
    service = FileTransferService()

    idle = handle_drop(
        service,
        session_id=None,
        session_state="idle",
        paths=[str(source)],
        destination_dir=str(tmp_path / "inbox"),
    )
    assert not idle.ok
    assert idle.message == NO_SESSION_MESSAGE
    assert "会话" in idle.message
    assert not service.jobs


def test_drop_starts_jobs_and_reports_rejections(tmp_path: Path) -> None:
    """拖入多个路径逐个开始; 缺失路径作为失败项明确回报."""
    first = tmp_path / "one.bin"
    second = tmp_path / "two.bin"
    _make_file(first, 2_048)
    _make_file(second, 2_048)
    missing = tmp_path / "gone.bin"
    service = FileTransferService()

    result = handle_drop(
        service,
        session_id="session-1",
        session_state="connected",
        paths=[str(first), str(first), str(second), str(missing)],
        destination_dir=str(tmp_path / "inbox"),
        rate_limit_kbps=64,
    )
    assert result.ok and result.data is not None
    assert len(result.data["job_ids"]) == 2
    assert len(result.data["failed"]) == 1
    assert result.data["failed"][0]["path"] == str(missing)
    assert result.data["protocol_ready"] is True
    assert result.data["missing_messages"] == []


def test_drop_rejects_non_local_entries(tmp_path: Path) -> None:
    """URL 与不存在的路径不算本地文件, 全部拒绝时返回明确错误码."""
    service = FileTransferService()
    result = handle_drop(
        service,
        session_id="session-1",
        session_state="connected",
        paths=["https://example.com/a.txt"],
        destination_dir=str(tmp_path / "inbox"),
    )
    assert not result.ok
    assert result.error_code == errors.TRANSFER_IO_ERROR
    assert "本地" in result.message
    empty = handle_drop(
        service,
        session_id="session-1",
        session_state="connected",
        paths=[],
        destination_dir=str(tmp_path / "inbox"),
    )
    assert not empty.ok and "不含" in empty.message


def test_transfer_protocol_messages_are_defined() -> None:
    """protocol-v1 v1.3 已定义 6 类文件传输消息, 线上形态为会话数据面."""
    assert missing_protocol_messages() == {}
    assert set(TRANSFER_MESSAGES) <= MESSAGE_TYPES
    plan = wire_plan(2, 1024)
    assert plan["protocol_ready"] is True
    assert plan["transport"] == "session_data_plane"


# ---------------------------------------------------------------- 授权范围
def test_scope_hierarchy_is_cumulative() -> None:
    """view < control < full 逐级包含; 未知名不覆盖."""
    assert scope_covers("full", "control") is True
    assert scope_covers("control", "control") is True
    assert scope_covers("control", "view") is True
    assert scope_covers("view", "control") is False
    assert scope_covers("view", "full") is False
    assert scope_covers("admin", "control") is False


def test_view_scope_rejected_for_transfer(tmp_path: Path) -> None:
    """view 授权一律拒绝文件传输, 不创建任务也不落任何字节."""
    source = tmp_path / "view.bin"
    _make_file(source, 4_096)
    destination = tmp_path / "inbox"
    service = FileTransferService()
    assert service.set_scope("view").ok

    result = service.start_transfer(str(source), str(destination))
    assert not result.ok
    assert result.error_code == SCOPE_INSUFFICIENT
    assert "control" in result.message
    assert not service.jobs
    assert not destination.exists()


def test_view_scope_rejected_on_running_job_commands(tmp_path: Path) -> None:
    """会话降为 view 后, 进度与控制类命令同样拒绝."""
    source = tmp_path / "downgrade.bin"
    _make_file(source, 400_000)
    service = FileTransferService(poll_budget_bytes=100_000)
    job_id = str(service.start_transfer(str(source), str(tmp_path / "inbox")).data["job_id"])
    assert service.set_scope("view").ok

    for result in (
        service.get_transfer_progress(job_id),
        service.pause_transfer(job_id),
        service.resume_transfer(job_id),
        service.cancel_transfer(job_id),
    ):
        assert not result.ok
        assert result.error_code == SCOPE_INSUFFICIENT
    assert service.recover_pending(str(tmp_path / "inbox"))[0].error_code == SCOPE_INSUFFICIENT


def test_control_and_full_scopes_allow_transfer(tmp_path: Path) -> None:
    """control 与 full 均可传输, full 是 control 的超集."""
    service = FileTransferService()
    assert service.set_scope("full").ok
    source = tmp_path / "full.bin"
    _make_file(source, 2_048)
    started = service.start_transfer(str(source), str(tmp_path / "inbox"))
    assert started.ok
    assert _drain(service, str(started.data["job_id"]))["state"] == "completed"

    control = FileTransferService()
    assert control.set_scope("control").ok
    assert control.start_transfer(str(source), str(tmp_path / "inbox2")).ok


def test_invalid_scope_returns_scope_invalid(tmp_path: Path) -> None:
    """非法 scope 取值返回 SCOPE_INVALID, 且不开始传输."""
    source = tmp_path / "invalid.bin"
    _make_file(source, 1_024)
    service = FileTransferService()
    denied = service.set_scope("admin")
    assert not denied.ok and denied.error_code == SCOPE_INVALID
    assert service.start_transfer(str(source), str(tmp_path / "inbox"), scope="admin").error_code == SCOPE_INVALID
    assert not service.jobs


def test_drop_rejected_for_view_scope(tmp_path: Path) -> None:
    """view 授权下拖拽同样拒绝, 文案指明需要 control."""
    source = tmp_path / "drop-view.bin"
    _make_file(source, 1_024)
    service = FileTransferService()
    result = handle_drop(
        service,
        session_id="session-1",
        session_state="connected",
        paths=[str(source)],
        destination_dir=str(tmp_path / "inbox"),
        scope="view",
    )
    assert not result.ok
    assert result.error_code == SCOPE_INSUFFICIENT
    assert "control" in result.message
    assert not service.jobs


# ---------------------------------------------------------------- 日志红线
def test_logs_never_contain_file_content(tmp_path: Path) -> None:
    """传输日志只记任务与字节数, 不出现文件内容明文."""
    reset_for_tests()
    secret = b"TOPSECRET-FILE-CONTENT"
    source = tmp_path / "secret.bin"
    source.write_bytes(secret * 100)
    service = FileTransferService()
    job_id = str(service.start_transfer(str(source), str(tmp_path / "inbox")).data["job_id"])
    _drain(service, job_id)

    dumped = json.dumps(recent_events(200), ensure_ascii=False)
    assert "TOPSECRET-FILE-CONTENT" not in dumped
    assert "transfer.completed" in dumped
