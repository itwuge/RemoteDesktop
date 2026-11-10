# ==========================================================================================
# 文件        : service.py
# 所属目录    : src/rdc/update/
# 文件用途    : 客户端更新后端: 检查更新、下载更新包、SHA-256 校验与进度查询
# 架构定位    : 更新模块的业务实现, 由命令层调用; 不依赖界面
# 核心功能    : 支持 GitHub Releases、本地目录与自建镜像; 拒绝降级; 校验失败删除文件
# 职责边界    : 不弹窗; 不执行下载的包; 不写日志明文凭据; 不阻塞调用线程
# ==========================================================================================

"""客户端更新后端."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from rdc.core.errors import (
    TASK_NOT_FOUND,
    UPDATE_DOWNGRADE_REJECTED,
    UPDATE_HASH_MISMATCH,
    UPDATE_MANIFEST_INVALID,
    UPDATE_SIGNATURE_INVALID,
    UPDATE_SOURCE_INSECURE,
    UPDATE_SOURCE_UNREACHABLE,
)
from rdc.core.logging import log_event
from rdc.core.result import CommandResult
from rdc.core.runtime import is_portable
from rdc.update.manifest import UpdateManifest, parse_manifest
from rdc.update.signing import BUILTIN_TRUST, ManifestSignatureError, TrustAnchor
from rdc.update.source import UpdateSource, default_source_value, is_insecure_http_url, parse_source
from rdc.update.versioning import compare_versions, parse_version
from rdc.version import __version__, artifact_basename, platform_tag

CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0
UPDATES_DIR = "updates"


def default_build_root() -> Path:
    """返回构建根: 优先 RDC_BUILD_ROOT, 其次项目既定的 D 盘目录."""
    configured = os.environ.get("RDC_BUILD_ROOT")
    if configured:
        return Path(configured)
    if sys.platform == "win32":
        return Path(r"D:\RDC-Build")
    return Path.home() / ".rdc"


def default_update_source() -> str:
    """返回默认更新源: 默认 GitHub Releases, 可用 RDC_UPDATE_SOURCE 或 RDC_UPDATE_REPO 覆盖."""
    return default_source_value()


class DownloadCancelled(Exception):
    """下载取消的内部信号, 不越过命令边界."""


@dataclass
class DownloadTask:
    """一次更新包下载任务."""

    task_id: str
    version: str
    status: str = "pending"
    completed: int = 0
    total: int = 0
    cancelable: bool = True
    verified: bool = False
    sha256: str = ""
    package_path: str = ""
    error_code: str | None = None
    message: str = "更新包下载已排队"


@dataclass
class UpdateService:
    """更新后端; 所有网络与磁盘操作在下载线程内完成."""

    current_version: str = __version__
    source: str | None = field(default_factory=default_update_source)
    work_dir: Path = field(default_factory=lambda: default_build_root() / UPDATES_DIR)
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    preferred_kind: str | None = None
    trust_anchor: TrustAnchor = field(default=BUILTIN_TRUST, repr=False)
    _resolved_source: UpdateSource = field(init=False, repr=False)
    _asset_kind: str = field(init=False, default="", repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _tasks: dict[str, DownloadTask] = field(default_factory=dict, repr=False)
    _cancel_events: dict[str, threading.Event] = field(default_factory=dict, repr=False)
    _threads: dict[str, threading.Thread] = field(default_factory=dict, repr=False)
    _manifest: UpdateManifest | None = field(default=None, repr=False)
    _status: dict[str, Any] | None = field(default=None, repr=False)
    _active_task_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """构造时校验当前版本并解析更新源, 避免运行期才发现配置不合法."""
        parse_version(self.current_version)
        self._resolved_source = parse_source(self.source or default_source_value())
        self._asset_kind = self.preferred_kind or ("portable" if is_portable() else "installer")

    @property
    def source_label(self) -> str:
        """更新源短标识, 用于界面与日志, 不含令牌."""
        return self._resolved_source.label

    @property
    def manifest_url(self) -> str:
        """实际读取的清单地址."""
        return self._resolved_source.manifest_url

    # ---------------------------------------------------------------- 检查更新
    def refresh_update_status(self) -> CommandResult:
        """读取更新源并比较版本; 不安全源与验签失败一律拒绝."""
        started = time.perf_counter()
        if is_insecure_http_url(self.manifest_url):
            self._log_check(started, None, UPDATE_SOURCE_INSECURE)
            return CommandResult(
                ok=False,
                message=f"更新源不安全: {self.source_label} 使用 http, 只允许 https 或本地目录",
                error_code=UPDATE_SOURCE_INSECURE,
            )
        try:
            payload = self._read_manifest_payload()
            manifest = parse_manifest(
                payload,
                source=self.manifest_url,
                platform=platform_tag(),
                preferred_kind=self._asset_kind,
                trust=self.trust_anchor,
            )
        except OSError as error:
            self._log_check(started, None, UPDATE_SOURCE_UNREACHABLE)
            return CommandResult(
                ok=False,
                message=f"更新源不可达: {error}",
                error_code=UPDATE_SOURCE_UNREACHABLE,
            )
        except ManifestSignatureError as error:
            self._log_check(started, None, UPDATE_SIGNATURE_INVALID)
            return CommandResult(
                ok=False,
                message=f"更新清单签名无效, 已拒绝更新: {error}",
                error_code=UPDATE_SIGNATURE_INVALID,
            )
        except ValueError as error:
            self._log_check(started, None, UPDATE_MANIFEST_INVALID)
            return CommandResult(ok=False, message=f"更新清单不合法: {error}", error_code=UPDATE_MANIFEST_INVALID)

        comparison = compare_versions(manifest.version, self.current_version)
        if comparison < 0:
            self._log_check(started, manifest.version, UPDATE_DOWNGRADE_REJECTED)
            return CommandResult(
                ok=False,
                message=f"更新源版本 {manifest.version} 低于当前版本 {self.current_version}, 已拒绝降级安装",
                error_code=UPDATE_DOWNGRADE_REJECTED,
            )

        below_floor = compare_versions(self.current_version, manifest.min_supported_version) < 0
        available = comparison > 0
        mandatory = manifest.mandatory or below_floor
        status = {
            "current_version": self.current_version,
            "latest_version": manifest.version,
            "available": available,
            "mandatory": mandatory,
            "release_date": manifest.release_date,
            "notes": manifest.notes,
            "sha256": manifest.sha256,
            "download_url": manifest.download_url,
            "min_supported_version": manifest.min_supported_version,
            "source": manifest.source,
            "source_kind": self._resolved_source.kind,
            "source_label": self.source_label,
            "asset_platform": manifest.asset_platform,
            "asset_kind": manifest.asset_kind,
            "asset_name": manifest.asset_name,
            "schema": manifest.schema,
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with self._lock:
            self._manifest = manifest
            self._status = status
        self._log_check(started, manifest.version, None)
        if available:
            suffix = ", 该版本为强制更新" if mandatory else ""
            return CommandResult(ok=True, message=f"发现新版本 {manifest.version}{suffix}", data=dict(status))
        return CommandResult(ok=True, message="当前已是最新版本", data=dict(status))

    # ---------------------------------------------------------------- 下载与校验
    def start_update_download(self) -> CommandResult:
        """开始下载已检查到的更新包, 立即返回任务句柄."""
        with self._lock:
            manifest = self._manifest
            status = self._status
            active = self._active_task_id
            if active is not None and active in self._tasks and self._tasks[active].status in {"pending", "running"}:
                return CommandResult(ok=False, message="已有更新包正在下载, 请等待完成")
            if manifest is None or status is None or not status.get("available"):
                return CommandResult(ok=False, message="没有可下载的更新, 请先检查更新")

            task = DownloadTask(task_id=f"update-{uuid.uuid4().hex[:12]}", version=manifest.version)
            self._tasks[task.task_id] = task
            self._active_task_id = task.task_id
            cancel_event = threading.Event()
            self._cancel_events[task.task_id] = cancel_event
            thread = threading.Thread(
                target=self._download_worker,
                args=(task, manifest, cancel_event),
                name=f"rdc-update-{task.task_id}",
                daemon=True,
            )
            self._threads[task.task_id] = thread
            thread.start()
            total = task.total
        return CommandResult(
            ok=True,
            message=f"开始下载 {manifest.version}, 共 {total} 字节",
            data={"task_id": task.task_id, "total": total, "version": manifest.version},
        )

    def get_update_progress(self, task_id: str) -> CommandResult:
        """查询下载进度与校验结果; 任务不存在返回 TASK_NOT_FOUND."""
        with self._lock:
            task = self._tasks.get(task_id)
            snapshot = None if task is None else self._snapshot(task)
        if snapshot is None:
            return CommandResult(ok=False, message="更新任务不存在或已过期", error_code=TASK_NOT_FOUND)
        if snapshot["status"] == "failed":
            return CommandResult(
                ok=False,
                message=str(snapshot.pop("message")),
                error_code=str(snapshot.pop("error_code") or "") or None,
                data=snapshot,
            )
        message = str(snapshot.pop("message"))
        snapshot.pop("error_code", None)
        return CommandResult(ok=True, message=message, data=snapshot)

    def cancel_update_download(self, task_id: str) -> CommandResult:
        """取消下载; 已完成或已失败的任务不可取消."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return CommandResult(ok=False, message="更新任务不存在或已过期", error_code=TASK_NOT_FOUND)
            if task.status in {"done", "failed", "canceled"}:
                return CommandResult(ok=False, message=f"任务已{task.status}, 不能取消")
            event = self._cancel_events.get(task_id)
            if event is not None:
                event.set()
            task.status = "canceled"
            task.cancelable = False
            task.message = "更新包下载已取消"
        return CommandResult(ok=True, message="更新包下载已取消", data={"task_id": task_id, "status": "canceled"})

    # ---------------------------------------------------------------- 内部实现
    def _read_manifest_payload(self) -> object:
        url = self.manifest_url
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                raw = response.read()
        elif parsed.scheme == "file":
            raw = Path(url2pathname(parsed.path)).read_bytes()
        else:
            raw = Path(url).read_bytes()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"更新清单不是合法 JSON: {error}") from error

    def _open_source(self, url: str) -> IO[bytes]:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"}:
            return urllib.request.urlopen(url, timeout=self.timeout)
        if parsed.scheme == "file":
            return Path(url2pathname(parsed.path)).open("rb")
        return Path(url).open("rb")

    def _download_worker(self, task: DownloadTask, manifest: UpdateManifest, cancel_event: threading.Event) -> None:
        target = self.work_dir / self._artifact_name(manifest)
        started = time.perf_counter()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            hasher = hashlib.sha256()
            completed = 0
            with target.open("wb") as handle, self._open_source(manifest.download_url) as response:
                total = self._content_length(response)
                if total > 0:
                    with self._lock:
                        task.total = total
                        task.status = "running"
                        task.message = f"正在下载 {manifest.version}"
                while True:
                    if cancel_event.is_set():
                        raise DownloadCancelled
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    completed += len(chunk)
                    with self._lock:
                        task.completed = completed
                        if task.total <= 0:
                            task.total = completed
            if cancel_event.is_set():
                raise DownloadCancelled
            digest = hasher.hexdigest()
            verified = digest.lower() == manifest.sha256.lower()
            with self._lock:
                task.sha256 = digest
                task.verified = verified
                task.cancelable = False
                if verified:
                    task.status = "done"
                    task.package_path = str(target)
                    task.message = f"更新包 {manifest.version} 已通过 SHA-256 校验, 可以安装"
                else:
                    task.status = "failed"
                    task.error_code = UPDATE_HASH_MISMATCH
                    task.message = "更新包 SHA-256 校验失败, 已删除下载文件, 拒绝安装"
            if not verified:
                target.unlink(missing_ok=True)
            self._log_download(manifest, task, started, None if verified else UPDATE_HASH_MISMATCH)
        except DownloadCancelled:
            target.unlink(missing_ok=True)
            with self._lock:
                task.status = "canceled"
                task.cancelable = False
                task.message = "更新包下载已取消"
            self._log_download(manifest, task, started, None)
        except OSError as error:
            target.unlink(missing_ok=True)
            with self._lock:
                task.status = "failed"
                task.cancelable = False
                task.error_code = UPDATE_SOURCE_UNREACHABLE
                task.message = f"更新包下载失败: {error}"
            self._log_download(manifest, task, started, UPDATE_SOURCE_UNREACHABLE)

    @staticmethod
    def _content_length(response: IO[bytes]) -> int:
        headers = getattr(response, "headers", None)
        if headers is None:
            return 0
        raw = headers.get("Content-Length")
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    @staticmethod
    def _artifact_name(manifest: UpdateManifest) -> str:
        parsed = urlparse(manifest.download_url)
        name = Path(parsed.path.replace("\\", "/")).name
        return name or f"{artifact_basename()}.zip"

    @staticmethod
    def _snapshot(task: DownloadTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "version": task.version,
            "completed": task.completed,
            "total": task.total,
            "status": task.status,
            "cancelable": task.cancelable,
            "verified": task.verified,
            "sha256": task.sha256,
            "package_path": task.package_path,
            "message": task.message,
            "error_code": task.error_code,
        }

    def _log_check(self, started: float, latest: str | None, error_code: str | None) -> None:
        log_event(
            "update",
            "update.check",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_code=error_code,
            latest_version=latest,
            source_kind=self._resolved_source.kind,
            source_label=self.source_label,
        )

    @staticmethod
    def _log_download(manifest: UpdateManifest, task: DownloadTask, started: float, error_code: str | None) -> None:
        log_event(
            "update",
            "update.download",
            level="WARNING" if error_code else "INFO",
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            error_code=error_code,
            version=manifest.version,
            status=task.status,
            bytes=task.completed,
            verified=task.verified,
        )
