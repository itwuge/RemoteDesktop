# ==========================================================================================
# 文件        : service.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 13 个命令函数的内存后端, 对应 desktop-v1 第三节
# 架构定位    : 命令层, 只依赖 core 与 crypto, 不依赖界面
# 核心功能    : 连接任务、会话状态、被控状态、画质档位、中继策略的骨架实现
# 职责边界    : 不弹窗、不阻塞界面线程、不打印用户提示; 单测不建真 socket, real 模式才走网络
# ==========================================================================================

"""命令层内存后端骨架.

契约依据: docs/contracts/desktop-v1.md 第三节.
签名与返回结构不得改动; 真实网络只在 ``connect_mode="real"`` 或注入 ``connector`` 时启用,
单测默认停留在 pending, 不用假数据冒充成功.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdc.accounts.db import AccountStoreError
from rdc.connect.mapping import close_all_mappings
from rdc.connect.policy import (
    REASON_RELAY_UNAVAILABLE,
    TRANSPORT_RELAY,
    EstablishResult,
    describe_decision,
)
from rdc.connect.runtime import establish_real_session
from rdc.core.errors import (
    ACCESS_CODE_INVALID,
    CONFIG_INVALID,
    DEVICE_NOT_FOUND,
    HOSTING_ALREADY_ACTIVE,
    HOSTING_NOT_ACTIVE,
    POLICY_INVALID,
    PROFILE_UNSUPPORTED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    TASK_NOT_FOUND,
)
from rdc.core.ids import iso_after, new_session_id, new_task_id, now_iso
from rdc.core.result import CommandResult
from rdc.crypto.identity import DeviceIdentity
from rdc.media.quality import (
    DIRECT_PROFILES,
    RELAY_ONLY_PROFILES,
    QualityController,
    QualityEvent,
    QualitySample,
)

# 契约 v1.1: 直连四档加 auto, 中继降档档位单列
QUALITY_PROFILES = DIRECT_PROFILES | RELAY_ONLY_PROFILES
RELAY_PROFILES = frozenset({"L0", "L1", "L2", "L3"})
DEVICE_ID_PATTERN = re.compile(r"^[A-Z2-7]{20}$")
ACCESS_CODE_PATTERN = re.compile(r"^\d{6}$")
CONNECT_STAGES = 4
# 契约变更记录 018-A8: 会话状态枚举, 界面按轮询消费
SESSION_STATES = frozenset({"connecting", "streaming", "reconnecting", "recovered", "failed"})
TASK_STATES = {
    "pending": "connecting",
    "running": "connecting",
    "done": "streaming",
    "failed": "failed",
    "canceled": "failed",
}

ProgressReporter = Callable[[int, str], None]
Connector = Callable[[str, str, ProgressReporter], EstablishResult]
AuditWriter = Callable[[str, str, str, dict[str, Any]], None]


def parse_endpoint(endpoint: str) -> tuple[str, int] | None:
    """解析服务端地址, 兼容 IPv6 字面量; 无法解析返回 None 由调用方兜底.

    支持三种写法: ``主机:端口``、``[IPv6]:端口``, 以及不带端口的 IPv6 字面量
    (按产品默认端口 27888 处理). 裸 IPv6 与端口连写存在歧义时按完整地址解析.
    """
    text = endpoint.strip()
    if not text:
        return None
    if text.startswith("["):
        host, separator, rest = text[1:].partition("]")
        if not separator or not host:
            return None
        if not rest:
            return host, 27888
        if not rest.startswith(":") or not rest[1:].isdigit():
            return None
        port = int(rest[1:])
        return (host, port) if 1 <= port <= 65535 else None
    if ":" not in text:
        return None
    try:
        ipaddress.ip_address(text)
    except ValueError:
        host, _, raw_port = text.rpartition(":")
        if not host or not raw_port.isdigit():
            return None
        port = int(raw_port)
        return (host, port) if 1 <= port <= 65535 else None
    return text, 27888


def local_addresses(primary: str) -> list[str]:
    """本机可被控制端连接的地址: 稳定 IPv6 在最前, 临时 IPv6 在最后.

    ``addresses[0]`` 在存在稳定 IPv6 时一定是稳定地址; RFC 4941 临时地址会
    轮换, 仍列出但排在主用地址与 IPv4 之后. ``address`` 字段语义不变,
    老端只读 ``address`` 不受影响.
    """
    from rdc.session.lan import ipv6_address_groups, local_ipv4_addresses

    stable, rotating = ipv6_address_groups()
    ordered: list[str] = []
    for address in [*stable, primary, *local_ipv4_addresses(), *rotating]:
        if address and address not in ordered:
            ordered.append(address)
    return ordered


def _audit_key(detail: dict[str, Any]) -> str:
    """会话审计去重键: 已建立的会话用 session_id, 未建立的尝试用 attempt_id."""
    return str(detail.get("session_id") or detail.get("attempt_id") or "")


def is_relay_host(host: str) -> bool:
    """该地址是否为本项目中继端点; 是则 transport_kind 记 relay, 否则记 direct."""
    from rdc.connect.relay import RelayUnavailable, relay_endpoint

    try:
        configured = relay_endpoint()[0]
    except RelayUnavailable:
        return False
    return host == configured


def session_state(record: SessionRecord) -> str:
    """把内部会话状态归一为契约枚举, 历史 connected 视为 streaming."""
    state = str(record.state or "")
    if state in SESSION_STATES:
        return state
    return "streaming" if state == "connected" else "connecting"


def write_account_audit(event: str, target: str, result: str, detail: dict[str, Any]) -> None:
    """把会话事件追加到账号审计库, 与设备绑定、免确认连接共用同一条时间线.

    未登录时 actor 退化成本机设备 ID; 同一会话同一事件只记一条; 账号库不可用时
    静默跳过, 审计写入失败不得改变连接与断开的结果.
    """
    from rdc.commands import accounts as accounts_commands

    service = accounts_commands.get_service()
    store = service.store
    actor = service.session_account_id or str(detail.get("from_device_id", ""))
    payload = {**detail, "account_id": service.session_account_id or ""}
    key = _audit_key(payload)
    try:
        if key and any(item.event == event and _audit_key(item.detail) == key for item in store.audit):
            return
        store.append_audit(event, actor, target, result, **payload)
    except AccountStoreError:
        return


@dataclass
class ConnectionTask:
    """连接任务."""

    task_id: str
    peer_device_id: str
    total: int = CONNECT_STAGES
    completed: int = 0
    status: str = "pending"
    cancelable: bool = True
    message: str = ""
    session_id: str = ""


@dataclass
class SessionRecord:
    """会话摘要与指标快照."""

    session_id: str
    peer_device_id: str
    state: str = "connected"
    reason: str = ""
    started_at: str = field(default_factory=now_iso)
    transport_kind: str = "none"
    quality_profile: str = "auto"
    gpu_percent: float = 0.0
    rss_mb: float = 0.0
    metrics: dict[str, Any] = field(
        default_factory=lambda: {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "fps": 0.0,
            "bitrate_kbps": 0,
            "dropped_frames": 0,
            "ssim": None,
        }
    )
    quality: QualityController | None = None


@dataclass
class CommandService:
    """命令层内存后端.

    ``connect_mode="real"`` 时 ``start_connection`` 在后台线程执行连接策略,
    成功后写入会话的 ``transport_kind``; 单测默认 ``stub``, 任务停在 pending.
    """

    identity: DeviceIdentity = field(default_factory=DeviceIdentity.generate)
    peers: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, ConnectionTask] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    hosting: dict[str, Any] | None = None
    relay_policy: dict[str, Any] | None = None
    connect_mode: str = "stub"
    connector: Connector | None = None
    audit_writer: AuditWriter | None = None
    # 真实被控端与控制端会话: 只在 real 模式装配, stub 模式保持 None
    host_service: Any = None
    live_sessions: dict[str, Any] = field(default_factory=dict)
    frame_sinks: dict[str, Callable[[Any], None]] = field(default_factory=dict)
    live_endpoint: str = ""
    hosting_port: int = 27888

    def _session_detail(
        self,
        session_id: str,
        peer_device_id: str,
        transport_kind: str = "none",
        quality_profile: str = "",
    ) -> dict[str, Any]:
        """会话审计的公共字段: 谁从哪台设备连了哪台被控端, 走什么传输与画质档."""
        return {
            "session_id": session_id,
            "from_device_id": self.identity.device_id,
            "peer_device_id": peer_device_id,
            "device_id": peer_device_id,
            "transport_kind": transport_kind,
            "quality_profile": quality_profile,
        }

    def _audit_session(self, event: str, target: str, result: str, detail: dict[str, Any]) -> None:
        """写一条会话审计; 审计不可用时不打断连接流程."""
        writer = self.audit_writer or write_account_audit
        writer(event, target, result, detail)

    # ---------------------------------------------------------------- 会话命令
    def start_connection(self, device_id: str, access_code: str) -> CommandResult:
        """发起连接, 立即返回任务句柄; real 模式下后台线程执行策略."""
        # 直连演示链路不经过设备簿: 设备 ID 由被控端启动时打印, 由信令判定是否在线
        if not self._use_live_endpoint() and (
            not DEVICE_ID_PATTERN.fullmatch(device_id or "") or device_id not in self.peers
        ):
            return CommandResult(ok=False, message="设备不存在, 请检查设备码", error_code=DEVICE_NOT_FOUND)
        session_id = new_session_id()
        if not (access_code or "").strip():
            # 同账号受信设备可免连接码: 只认账号准入函数的判定, 不采信客户端自述;
            # 免掉的只是连接码, 设备签名、hs=2 握手与 AEAD 校验仍由会话层执行.
            from rdc.commands.accounts import resolve_connection_access
            from rdc.session.handshake import TRUSTED_NO_CODE_ROUTES

            access = resolve_connection_access(device_id, "control", session_id=session_id)
            if not access.ok:
                # 令牌被吊销、登录过期等直接把中文原因交给界面并断开, 不静默重连
                return access
            if str((access.data or {}).get("route", "")) not in TRUSTED_NO_CODE_ROUTES:
                return CommandResult(ok=False, message="临时口令应为 6 位数字", error_code=ACCESS_CODE_INVALID)
        elif not ACCESS_CODE_PATTERN.fullmatch(access_code or ""):
            return CommandResult(ok=False, message="临时口令应为 6 位数字", error_code=ACCESS_CODE_INVALID)
        if self.sessions:
            return CommandResult(ok=False, message="本机已有会话, 请先结束", error_code=SESSION_BUSY)

        task = ConnectionTask(task_id=new_task_id(), peer_device_id=device_id, session_id=session_id)
        self.tasks[task.task_id] = task
        if self.connect_mode == "real" or self.connector is not None:
            task.status = "running"
            worker = threading.Thread(
                target=self._execute_connection,
                args=(task, device_id, access_code),
                name=f"rdc-connect-{task.task_id}",
                daemon=True,
            )
            worker.start()
        return CommandResult(
            ok=True,
            message=f"已开始连接, 任务 {task.task_id}, 共 {task.total} 个阶段",
            data={"task_id": task.task_id, "total": task.total},
        )

    def _execute_connection(self, task: ConnectionTask, device_id: str, access_code: str) -> None:
        """后台执行 establish_session, 成功则落会话, 失败写中文原因."""

        def progress(stage: int, text: str) -> None:
            task.completed = max(0, min(task.total, stage))
            if task.status == "running":
                task.message = text

        session_id = task.session_id or new_session_id()
        if self._use_live_endpoint():
            self._execute_live_connection(task, device_id, access_code, session_id, progress)
            return
        try:
            if self.connector is not None:
                result = self.connector(device_id, access_code, progress)
            else:
                result = establish_real_session(
                    self.identity,
                    device_id,
                    session_id,
                    access_code,
                    on_progress=progress,
                    tcp_port=self.hosting_port,
                )
        except (OSError, RuntimeError, TimeoutError, ValueError, KeyError) as exc:
            if task.status != "canceled":
                task.status = "failed"
                task.cancelable = False
                task.message = f"连接失败：{exc}；请检查网络后重试"
                reason = "timeout" if isinstance(exc, TimeoutError) else "failed"
                self._audit_session(
                    "session.end",
                    device_id,
                    "fail",
                    self._session_detail(session_id, device_id)
                    | {"reason": reason, "established": False, "attempt_id": task.task_id},
                )
            return

        if task.status == "canceled":
            return
        if result.decision.reason == REASON_RELAY_UNAVAILABLE:
            task.status = "failed"
            task.cancelable = False
            task.message = describe_decision(result.decision, result.relay)
            self._audit_session(
                "session.end",
                device_id,
                "fail",
                self._session_detail(session_id, device_id)
                | {"reason": REASON_RELAY_UNAVAILABLE, "established": False, "attempt_id": task.task_id},
            )
            return
        transport_kind = "relay" if result.decision.transport == TRANSPORT_RELAY else "direct"
        profile = "720p30" if transport_kind == "relay" else "auto"
        self.add_session(device_id, transport_kind=transport_kind, quality_profile=profile, session_id=session_id)
        task.completed = task.total
        task.status = "done"
        task.cancelable = False
        task.message = describe_decision(result.decision, result.relay)

    def _use_live_endpoint(self) -> bool:
        """是否走本机/局域网的真实会话链路; 注入 connector 时以 connector 为准."""
        if self.connector is not None:
            return False
        return bool(self.live_endpoint or os.environ.get("RDC_SIGNALING_ENDPOINT", ""))

    def _live_address(self, endpoint: str = "") -> tuple[str, int]:
        """解析真实会话的服务端地址; 支持 ``[v6]:port`` 与裸 v6, 非法配置按 127.0.0.1:27888 处理."""
        text = endpoint or self.live_endpoint or os.environ.get("RDC_SIGNALING_ENDPOINT", "")
        parsed = parse_endpoint(text)
        if parsed is None:
            return "127.0.0.1", 27888
        return parsed

    def _execute_live_connection(
        self,
        task: ConnectionTask,
        device_id: str,
        access_code: str,
        session_id: str,
        progress: ProgressReporter,
    ) -> None:
        """后台建立真实控制会话: 信令、连接码校验、数据面与首帧."""
        from rdc.commands import clipboard as clipboard_commands
        from rdc.commands import transfer as transfer_commands
        from rdc.files.service import default_destination_root
        from rdc.session.remote import RemoteConfig, RemoteSession, RemoteStatus

        host, port = self._live_address()

        def on_status(status: RemoteStatus) -> None:
            if task.status != "running":
                return
            if status.state == "streaming":
                progress(task.total, status.message)

        def on_frame(frame: Any) -> None:
            sink = self.frame_sinks.get(session_id)
            if sink is not None:
                sink(frame)

        session = RemoteSession(
            RemoteConfig(
                host=host,
                port=port,
                device_id=device_id,
                access_code=access_code,
                session_id=session_id,
            ),
            on_frame=on_frame,
            on_status=on_status,
            identity=self.identity,
            transfer_service=transfer_commands.get_service(),
            clipboard_service=clipboard_commands.get_service(),
            destination_root=default_destination_root(),
        )
        progress(1, f"正在连接 {host}:{port}")
        session.start()
        if not session.wait_ready(25.0):
            reason = session.failed_reason or "等待被控端画面通道超时"
            session.stop()
            if task.status == "canceled":
                return
            task.status = "failed"
            task.cancelable = False
            task.message = f"连接失败：{reason}"
            self._audit_session(
                "session.end",
                device_id,
                "fail",
                self._session_detail(session_id, device_id)
                | {"reason": "failed", "established": False, "attempt_id": task.task_id},
            )
            return
        if task.status == "canceled":
            session.stop()
            return
        self.live_sessions[session_id] = session
        # 以会话实际选路为准: 直连 IPv6 媒体成功为 direct, 其余回落 relay
        transport_kind = session.transport_kind
        profile = "720p30" if transport_kind == "relay" else "auto"
        self.add_session(device_id, transport_kind=transport_kind, quality_profile=profile, session_id=session_id)
        task.completed = task.total
        task.status = "done"
        task.cancelable = False
        task.message = f"已连接 {device_id}"

    def watch_remote_frames(self, session_id: str, sink: Callable[[Any], None]) -> bool:
        """登记远端画面回调; 不是契约命令, 界面经此取已解码像素."""
        if session_id not in self.live_sessions and session_id not in self.sessions:
            return False
        self.frame_sinks[session_id] = sink
        return True

    def send_remote_input(self, session_id: str, message: dict[str, Any]) -> bool:
        """把一条输入消息交给真实会话上行; 不是契约命令."""
        session = self.live_sessions.get(session_id)
        if session is None:
            return False
        session.send_input(message)
        return True

    def get_remote_stats(self, session_id: str) -> dict[str, Any]:
        """返回真实会话统计; 不是契约命令, 供界面状态栏与验收读取."""
        session = self.live_sessions.get(session_id)
        if session is None:
            return {}
        data = dict(session.stats())
        record = self.sessions.get(session_id)
        if record is not None:
            data["quality_profile"] = record.quality_profile
            data["transport_kind"] = record.transport_kind
        return data

    def request_remote_keyframe(self, session_id: str) -> CommandResult:
        """请求被控端立即产生关键帧; 不是契约命令, 供会话页右键菜单使用."""
        session = self.live_sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话未建立, 无法请求关键帧")
        session.request_keyframe()
        return CommandResult(ok=True, message="已请求关键帧, 画面将立即刷新")

    def get_hosting_info(self) -> dict[str, Any]:
        """返回本机被控信息快照; 不是契约命令, 供会话页复制连接信息."""
        return dict(self.hosting or {})

    def get_connection_progress(self, task_id: str) -> CommandResult:
        """查询连接任务进度; 失败或降级时附带中文原因."""
        task = self.tasks.get(task_id)
        if task is None:
            return CommandResult(ok=False, message="任务不存在或已过期", error_code=TASK_NOT_FOUND)
        data: dict[str, Any] = {
            "completed": task.completed,
            "total": task.total,
            "status": task.status,
            "cancelable": task.cancelable,
            "state": TASK_STATES.get(task.status, "connecting"),
        }
        if data["state"] == "failed":
            data["reason"] = "连接已取消" if task.status == "canceled" else (task.message or "连接失败")
        if task.message:
            data["message"] = task.message
        message = f"连接任务 {task.status}, 已完成 {task.completed}/{task.total}"
        if task.message:
            message = f"{message}；{task.message}"
        return CommandResult(ok=True, message=message, data=data)

    def cancel_connection(self, task_id: str) -> CommandResult:
        """取消连接任务."""
        task = self.tasks.get(task_id)
        if task is None:
            return CommandResult(ok=False, message="任务不存在或已过期", error_code=TASK_NOT_FOUND)
        if task.status in {"done", "failed", "canceled"}:
            return CommandResult(ok=False, message=f"任务已{task.status}, 不能取消")
        task.status = "canceled"
        task.cancelable = False
        self._audit_session(
            "session.end",
            task.peer_device_id,
            "ok",
            self._session_detail(task.session_id, task.peer_device_id)
            | {"reason": "canceled", "established": False, "attempt_id": task.task_id},
        )
        return CommandResult(ok=True, message="连接已取消", data={"task_id": task_id, "status": task.status})

    def stop_session(self, session_id: str) -> CommandResult:
        """停止已建立的会话."""
        session = self.sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        live = self.live_sessions.pop(session_id, None)
        if live is not None:
            live.stop()
        self.frame_sinks.pop(session_id, None)
        self.sessions.pop(session_id)
        if not self.sessions:
            # 最后一个会话结束时兜底清理端口映射; 仍有会话时交给各自租约, 避免误删在用映射
            close_all_mappings()
        self._audit_session(
            "session.end",
            session.peer_device_id,
            "ok",
            self._session_detail(
                session.session_id,
                session.peer_device_id,
                transport_kind=session.transport_kind,
                quality_profile=session.quality_profile,
            )
            | {"reason": "normal", "established": True},
        )
        return CommandResult(ok=True, message="会话已停止", data={"session_id": session_id, "status": "closed"})

    def get_session_status(self, session_id: str) -> CommandResult:
        """查询会话状态."""
        session = self.sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        data: dict[str, Any] = {
            "state": session_state(session),
            "transport_kind": session.transport_kind,
            "quality_profile": session.quality_profile,
            "peer_device_id": session.peer_device_id,
            "started_at": session.started_at,
        }
        if data["state"] == "failed":
            data["reason"] = session.reason or "会话异常结束"
        return CommandResult(
            ok=True,
            message="会话状态已刷新",
            data=data,
        )

    def list_sessions(self) -> CommandResult:
        """列出当前会话摘要."""
        items = [
            {
                "session_id": session.session_id,
                "state": session_state(session),
                "transport_kind": session.transport_kind,
                "quality_profile": session.quality_profile,
                "peer_device_id": session.peer_device_id,
                "started_at": session.started_at,
            }
            for session in self.sessions.values()
        ]
        return CommandResult(ok=True, message=f"当前会话 {len(items)} 个", data={"items": items})

    def apply_quality_profile(self, session_id: str, profile: str) -> CommandResult:
        """切换画质档位, 档位枚举受契约 v1.1 约束."""
        session = self.sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        # 中继会话只接受中继降档档位; 直连会话只接受四档直连档位与 auto
        allowed = RELAY_ONLY_PROFILES | {"auto"} if session.transport_kind == "relay" else DIRECT_PROFILES
        if profile not in allowed:
            return CommandResult(ok=False, message="当前版本不支持该画质档位", error_code=PROFILE_UNSUPPORTED)

        session.quality_profile = profile
        if session.quality is not None and profile in DIRECT_PROFILES:
            session.quality.set_manual(profile)
            session.quality_profile = session.quality.profile
        return CommandResult(
            ok=True,
            message="画质档位已切换",
            data={"session_id": session_id, "quality_profile": session.quality_profile},
        )

    def get_session_metrics(self, session_id: str) -> CommandResult:
        """返回会话指标快照, 未采集时为零值而不是伪造数据."""
        session = self.sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        data = dict(session.metrics)
        if session.quality is not None:
            data.update(session.quality.snapshot())
        else:
            data.update(
                {
                    "quality_profile": session.quality_profile,
                    "switches_used": 0,
                    "switches_limit": 0,
                    "quality_events": [],
                }
            )
        data["gpu_percent"] = session.gpu_percent
        data["rss_mb"] = session.rss_mb
        return CommandResult(ok=True, message="会话指标已刷新", data=data)

    def feed_quality_sample(self, session_id: str, sample: QualitySample) -> QualityEvent | None:
        """喂入一次采样, 由会话编排层调用; 不是契约命令.

        :param session_id: 会话标识
        :param sample: 采样值
        :return: 发生自动切换时返回事件
        """
        session = self.sessions.get(session_id)
        if session is None or session.quality is None:
            return None
        session.metrics["fps"] = sample.fps
        session.metrics["p95_ms"] = sample.p95_ms
        session.gpu_percent = sample.gpu_percent
        event = session.quality.observe(sample)
        session.quality_profile = session.quality.profile
        return event

    # ---------------------------------------------------------------- 被控命令
    def start_hosting(self, display_name: str) -> CommandResult:
        """启动被控: 生成 6 位一次性口令; real 模式同时起本机中继、信令与画面发布."""
        if self.hosting is not None:
            return CommandResult(ok=False, message="已处于被控状态, 请先停止", error_code=HOSTING_ALREADY_ACTIVE)
        access_code = f"{secrets.randbelow(1_000_000):06d}"
        error = ""
        address = "127.0.0.1"
        port = 27888
        listen_ports: tuple[int, ...] = (port,)
        if self.connect_mode == "real":
            started, error, address, port = self._start_host_service(display_name, access_code)
            if not started:
                return CommandResult(ok=False, message=f"被控端启动失败：{error}", error_code=CONFIG_INVALID)
            service = self.host_service
            listen_ports = tuple(service.listen_ports) if service is not None else (port,)
        self.hosting = {
            "device_id": self.identity.device_id,
            "access_code": access_code,
            "expires_at": iso_after(300),
            "display_name": display_name,
            "address": address,
            "addresses": local_addresses(address),
            "port": port,
            # 实际绑定成功的监听端口; 额外端口被占用时只降级该端口, 这里如实反映
            "listen_ports": list(listen_ports),
        }
        return CommandResult(
            ok=True,
            message="已被控, 口令 5 分钟内有效",
            data={
                "device_id": self.hosting["device_id"],
                "access_code": access_code,
                "expires_at": self.hosting["expires_at"],
                "address": address,
                "port": port,
                "listen_ports": list(listen_ports),
            },
        )

    def _start_host_service(self, display_name: str, access_code: str) -> tuple[bool, str, str, int]:
        """装配并启动真实被控端; 返回 (是否成功, 失败原因, 对外地址, 端口)."""
        from rdc.session.host import HostConfig, HostService, local_ip

        try:
            service = HostService(
                HostConfig(display_name=display_name, access_code=access_code, port=self.hosting_port),
                self.identity,
            )
            service.set_rotate_hook(self._rotate_host_code)
            service.start()
        except (OSError, RuntimeError, ValueError) as exc:
            return False, str(exc), "", 0
        self.host_service = service
        return True, "", local_ip(), service.port

    def _rotate_host_code(self) -> str:
        """被控端连续校验失败后的换码: 命令层生成新码并同步到状态."""
        if self.hosting is None:
            return ""
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.hosting["access_code"] = code
        self.hosting["expires_at"] = iso_after(300)
        return code

    def stop_hosting(self) -> CommandResult:
        """停止被控: 先停真实被控端, 再清状态."""
        if self.hosting is None:
            return CommandResult(ok=False, message="当前未处于被控状态")
        device_id = self.hosting["device_id"]
        service = self.host_service
        self.host_service = None
        if service is not None:
            service.stop()
        self.hosting = None
        return CommandResult(ok=True, message="已停止被控", data={"device_id": device_id, "status": "stopped"})

    def refresh_access_code(self) -> CommandResult:
        """刷新一次性口令."""
        if self.hosting is None:
            return CommandResult(ok=False, message="当前未处于被控状态", error_code=HOSTING_NOT_ACTIVE)
        self.hosting["access_code"] = f"{secrets.randbelow(1_000_000):06d}"
        self.hosting["expires_at"] = iso_after(300)
        service = self.host_service
        if service is not None:
            service.rotate_access_code(self.hosting["access_code"])
        return CommandResult(
            ok=True,
            message="口令已刷新",
            data={"access_code": self.hosting["access_code"], "expires_at": self.hosting["expires_at"]},
        )

    # ---------------------------------------------------------------- 设置命令
    def open_settings(self) -> CommandResult:
        """打开设置页."""
        return CommandResult(ok=True, message="已打开设置", data={"section": "general"})

    def apply_relay_policy(self, endpoint: str, egress_mbps: int, degrade_profile: str) -> CommandResult:
        """应用中继策略, 预算是计算值不是协议常量."""
        if not endpoint or egress_mbps <= 0 or degrade_profile not in RELAY_PROFILES:
            return CommandResult(ok=False, message="中继策略不合法, 请检查端点与带宽", error_code=POLICY_INVALID)
        budget_kbps = math.floor(egress_mbps * 1000 * (1 - 0.08))
        policy = {
            "endpoint": endpoint,
            "egress_mbps": egress_mbps,
            "degrade_profile": degrade_profile,
            "overhead_ratio": 0.08,
            "budget_kbps": budget_kbps,
        }
        self.relay_policy = policy
        return CommandResult(ok=True, message="中继策略已应用", data={"policy": policy})

    # ---------------------------------------------------------------- 测试与装配辅助
    def register_peer(self, device_id: str, display_name: str = "") -> None:
        """登记对端设备, 供单测与本地联调使用."""
        self.peers[device_id] = display_name

    def add_session(
        self,
        peer_device_id: str,
        transport_kind: str = "direct",
        quality_profile: str = "auto",
        state: str = "connected",
        session_id: str | None = None,
    ) -> SessionRecord:
        """创建会话记录, 供单测与界面联调使用; 建立即写一条 session.start 审计."""
        identifier = session_id or new_session_id()
        quality: QualityController | None = None
        if transport_kind != "relay" and quality_profile in DIRECT_PROFILES:
            quality = QualityController(session_id=identifier, profile=quality_profile)
        session = SessionRecord(
            session_id=identifier,
            peer_device_id=peer_device_id,
            state=state,
            transport_kind=transport_kind,
            quality_profile=quality.profile if quality is not None else quality_profile,
            quality=quality,
        )
        self.sessions[session.session_id] = session
        self._audit_session(
            "session.start",
            peer_device_id,
            "ok",
            self._session_detail(
                session.session_id,
                peer_device_id,
                transport_kind=session.transport_kind,
                quality_profile=session.quality_profile,
            )
            | {"established": True},
        )
        return session
