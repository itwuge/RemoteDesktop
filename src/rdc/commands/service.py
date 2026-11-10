# ==========================================================================================
# 文件        : service.py
# 所属目录    : src/rdc/commands/
# 文件用途    : 13 个命令函数的内存后端, 对应 desktop-v1 第三节
# 架构定位    : 命令层, 只依赖 core 与 crypto, 不依赖界面
# 核心功能    : 连接任务、会话状态、被控状态、画质档位、中继策略的骨架实现
# 职责边界    : 不弹窗、不阻塞界面线程、不打印用户提示; 不接真实网络
# ==========================================================================================

"""命令层内存后端骨架.

契约依据: docs/contracts/desktop-v1.md 第三节.
签名与返回结构不得改动; 未实现的网络能力一律返回契约错误码, 不用假数据冒充成功.
"""

from __future__ import annotations

import math
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from rdc.core.errors import (
    ACCESS_CODE_INVALID,
    DEVICE_NOT_FOUND,
    HOSTING_ALREADY_ACTIVE,
    HOSTING_NOT_ACTIVE,
    POLICY_INVALID,
    PROFILE_UNSUPPORTED,
    SESSION_BUSY,
    SESSION_NOT_FOUND,
    TASK_NOT_FOUND,
)
from rdc.core.ids import iso_after, new_session_id, new_task_id
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


@dataclass
class ConnectionTask:
    """连接任务."""

    task_id: str
    peer_device_id: str
    total: int = CONNECT_STAGES
    completed: int = 0
    status: str = "pending"
    cancelable: bool = True


@dataclass
class SessionRecord:
    """会话摘要与指标快照."""

    session_id: str
    peer_device_id: str
    state: str = "connected"
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

    真实网络、采集与渲染未接入, 所有连接任务停在 pending;
    不返回「已连接」等未经验证的状态.
    """

    identity: DeviceIdentity = field(default_factory=DeviceIdentity.generate)
    peers: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, ConnectionTask] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    hosting: dict[str, Any] | None = None
    relay_policy: dict[str, Any] | None = None

    # ---------------------------------------------------------------- 会话命令
    def start_connection(self, device_id: str, access_code: str) -> CommandResult:
        """发起连接, 立即返回任务句柄."""
        if not DEVICE_ID_PATTERN.fullmatch(device_id or "") or device_id not in self.peers:
            return CommandResult(ok=False, message="设备不存在, 请检查设备码", error_code=DEVICE_NOT_FOUND)
        if not ACCESS_CODE_PATTERN.fullmatch(access_code or ""):
            return CommandResult(ok=False, message="临时口令应为 6 位数字", error_code=ACCESS_CODE_INVALID)
        if self.sessions:
            return CommandResult(ok=False, message="本机已有会话, 请先结束", error_code=SESSION_BUSY)

        task = ConnectionTask(task_id=new_task_id(), peer_device_id=device_id)
        self.tasks[task.task_id] = task
        return CommandResult(
            ok=True,
            message=f"已开始连接, 任务 {task.task_id}, 共 {task.total} 个阶段",
            data={"task_id": task.task_id, "total": task.total},
        )

    def get_connection_progress(self, task_id: str) -> CommandResult:
        """查询连接任务进度."""
        task = self.tasks.get(task_id)
        if task is None:
            return CommandResult(ok=False, message="任务不存在或已过期", error_code=TASK_NOT_FOUND)
        return CommandResult(
            ok=True,
            message=f"连接任务 {task.status}, 已完成 {task.completed}/{task.total}",
            data={
                "completed": task.completed,
                "total": task.total,
                "status": task.status,
                "cancelable": task.cancelable,
            },
        )

    def cancel_connection(self, task_id: str) -> CommandResult:
        """取消连接任务."""
        task = self.tasks.get(task_id)
        if task is None:
            return CommandResult(ok=False, message="任务不存在或已过期", error_code=TASK_NOT_FOUND)
        if task.status in {"done", "failed", "canceled"}:
            return CommandResult(ok=False, message=f"任务已{task.status}, 不能取消")
        task.status = "canceled"
        task.cancelable = False
        return CommandResult(ok=True, message="连接已取消", data={"task_id": task_id, "status": task.status})

    def stop_session(self, session_id: str) -> CommandResult:
        """停止已建立的会话."""
        if session_id not in self.sessions:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        self.sessions.pop(session_id)
        return CommandResult(ok=True, message="会话已停止", data={"session_id": session_id, "status": "closed"})

    def get_session_status(self, session_id: str) -> CommandResult:
        """查询会话状态."""
        session = self.sessions.get(session_id)
        if session is None:
            return CommandResult(ok=False, message="会话不存在或已关闭", error_code=SESSION_NOT_FOUND)
        return CommandResult(
            ok=True,
            message="会话状态已刷新",
            data={
                "state": session.state,
                "transport_kind": session.transport_kind,
                "quality_profile": session.quality_profile,
                "peer_device_id": session.peer_device_id,
            },
        )

    def list_sessions(self) -> CommandResult:
        """列出当前会话摘要."""
        items = [
            {
                "session_id": session.session_id,
                "state": session.state,
                "transport_kind": session.transport_kind,
                "quality_profile": session.quality_profile,
                "peer_device_id": session.peer_device_id,
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
        """启动被控, 生成 6 位一次性口令."""
        if self.hosting is not None:
            return CommandResult(ok=False, message="已处于被控状态, 请先停止", error_code=HOSTING_ALREADY_ACTIVE)
        access_code = f"{secrets.randbelow(1_000_000):06d}"
        self.hosting = {
            "device_id": self.identity.device_id,
            "access_code": access_code,
            "expires_at": iso_after(300),
            "display_name": display_name,
        }
        return CommandResult(
            ok=True,
            message="已被控, 口令 5 分钟内有效",
            data={
                "device_id": self.hosting["device_id"],
                "access_code": access_code,
                "expires_at": self.hosting["expires_at"],
            },
        )

    def stop_hosting(self) -> CommandResult:
        """停止被控."""
        if self.hosting is None:
            return CommandResult(ok=False, message="当前未处于被控状态")
        device_id = self.hosting["device_id"]
        self.hosting = None
        return CommandResult(ok=True, message="已停止被控", data={"device_id": device_id, "status": "stopped"})

    def refresh_access_code(self) -> CommandResult:
        """刷新一次性口令."""
        if self.hosting is None:
            return CommandResult(ok=False, message="当前未处于被控状态", error_code=HOSTING_NOT_ACTIVE)
        self.hosting["access_code"] = f"{secrets.randbelow(1_000_000):06d}"
        self.hosting["expires_at"] = iso_after(300)
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
    ) -> SessionRecord:
        """创建会话记录, 供单测与界面联调使用."""
        session_id = new_session_id()
        quality: QualityController | None = None
        if transport_kind != "relay" and quality_profile in DIRECT_PROFILES:
            quality = QualityController(session_id=session_id, profile=quality_profile)
        session = SessionRecord(
            session_id=session_id,
            peer_device_id=peer_device_id,
            state=state,
            transport_kind=transport_kind,
            quality_profile=quality.profile if quality is not None else quality_profile,
            quality=quality,
        )
        self.sessions[session.session_id] = session
        return session
