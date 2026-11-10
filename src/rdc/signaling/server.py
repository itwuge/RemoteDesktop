# ==========================================================================================
# 文件        : server.py
# 所属目录    : src/rdc/signaling/
# 文件用途    : 内存信令服务骨架, 覆盖 24 类消息的注册、挑战、认证、路由、会话数据面校验与中继预算
# 架构定位    : 协议层服务端骨架, 不依赖界面、媒体与客户端会话
# 核心功能    : 校验包络与签名; 设备注册与挑战应答; 消息转发; 中继预算分配
# 职责边界    : 不落库; 不接真实网络; 不做多中继集群
# ==========================================================================================

"""内存信令服务骨架.

只用于单测与联调, 不接真实 TLS 与数据库. 契约缺口见
.team/records/契约缺口-202611-001-20261110.md.

并发: 所有公开入口都在同一把可重入锁内执行, 供多连接服务端直接调用.
路由: 未注入投递回调时保持旧行为(把包络原样返回给调用方), 注入后真正投递给对端连接.
"""

from __future__ import annotations

import base64
import math
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rdc.core.errors import (
    AUTH_FAILED,
    FILE_CHUNK_INVALID,
    PEER_OFFLINE,
    PROTOCOL_VERSION_UNSUPPORTED,
    RELAY_FULL,
    SCOPE_INSUFFICIENT,
)
from rdc.core.ids import iso_after
from rdc.crypto.identity import DeviceIdentity, b64decode, b64encode, device_id_from_public_key, verify_signature
from rdc.signaling.envelope import ProtocolError, build_envelope, validate_envelope, verify_envelope
from rdc.signaling.messages import ROUTED_TYPES, SERVER_TYPES, SESSION_PLANE_TYPES
from rdc.signaling.session_plane import SessionPlaneTracker

# 会话结束后保留状态的秒数, 只为迟到的审计与排障保留, 到期即清理
SESSION_RETENTION_S = 60.0
# 会话无活动且无有效授予时的回收时间, 不涉及令牌吊销
SESSION_IDLE_TTL_S = 300.0
# presence 心跳 30 秒一条, 90 秒未收到判离线
PRESENCE_ONLINE_WINDOW_S = 90.0
PRESENCE_UNKNOWN = "unknown"
PRESENCE_ONLINE = "online"
PRESENCE_OFFLINE = "offline"
# 预览令牌有效期, 契约 v1.5 第 47 条
PREVIEW_TOKEN_TTL_S = 60
FOCUS_TOKEN_TTL_S = 300
DEFAULT_MIN_SESSION_KBPS = 200


@dataclass(frozen=True)
class PresenceRecord:
    """设备在线心跳记录, 只保存收到时刻与客户端自报状态."""

    last_seen_at: float
    state_hint: str = PRESENCE_ONLINE


@dataclass
class RegisteredDevice:
    """已注册设备."""

    public_key_b64: str
    display_name: str = ""


@dataclass(frozen=True)
class RelayGrantRecord:
    """中继预算授予记录, 供数据面校验 relay_token.

    :param session_id: 绑定的会话
    :param budget_kbps: 授予的预算, 单位 kbps
    :param expires_at: 过期时刻, epoch 秒
    :param device_id: 申请方设备, 断线时按此回收授予; 旧调用方可省略
    :param scope: control 正式会话 / view 屏幕墙预览 / focus 屏幕墙聚焦
    :param wall_id: 屏幕墙标识, 仅预览与聚焦使用
    :param cell: 屏幕墙格号, 仅预览与聚焦使用, -1 表示未绑定
    :param account_id: 签发时绑定的账号, 仅预览与聚焦使用
    """

    session_id: str
    budget_kbps: int
    expires_at: float
    device_id: str = ""
    scope: str = "control"
    wall_id: str = ""
    cell: int = -1
    account_id: str = ""


@dataclass
class SessionState:
    """会话生命周期状态, 只记参与者与时间, 不保存任何媒体内容."""

    session_id: str
    devices: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    ended: bool = False


@dataclass
class RoutedMessage:
    """一次成功转发的记录."""

    from_device_id: str
    to_device_id: str
    msg_type: str
    envelope: dict[str, Any]


@dataclass
class InMemorySignalingServer:
    """内存信令服务.

    :param relay_endpoint: 中继端点, 由配置注入
    :param egress_mbps: 中继出口带宽, 只作部署默认值
    :param overhead_ratio: 链路开销比
    :param max_sessions: 单中继并发会话数
    :param min_session_kbps: 单会话最低预算, 低于此值返回 RELAY_FULL
    :param deliver: 向对端连接投递包络的回调, 返回是否投递成功; 为空时保持旧行为
    """

    relay_endpoint: str = "relay://127.0.0.1:0"
    egress_mbps: int = 3
    overhead_ratio: float = 0.08
    max_sessions: int = 1
    challenge_ttl_s: int = 300
    min_session_kbps: int = DEFAULT_MIN_SESSION_KBPS
    deliver: Callable[[str, dict[str, Any]], bool] | None = None
    identity: DeviceIdentity = field(default_factory=DeviceIdentity.generate)
    devices: dict[str, RegisteredDevice] = field(default_factory=dict)
    challenges: dict[str, tuple[str, str]] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)
    token_devices: dict[str, str] = field(default_factory=dict)
    token_expiry: dict[str, float] = field(default_factory=dict)
    relay_grants: dict[str, RelayGrantRecord] = field(default_factory=dict)
    sessions: dict[str, SessionState] = field(default_factory=dict)
    # 已认证的传输连接, 与 presence 的在线口径分开
    connected: set[str] = field(default_factory=set)
    presence: dict[str, PresenceRecord] = field(default_factory=dict)
    focus_token: str = ""
    errors: list[str] = field(default_factory=list)
    routed: list[RoutedMessage] = field(default_factory=list)
    # 在线表外部落点: 组装层可注入 accounts.record_presence, 使 list_devices 不再长期停在 unknown
    presence_sink: Callable[[str, str], None] | None = None
    # 设备到账号的解析: 组装层注入账号服务后据此绑定预览/聚焦, 禁止 payload 自报账号
    account_of: Callable[[str], str] | None = None
    session_plane: SessionPlaneTracker = field(default_factory=SessionPlaneTracker)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _current_device: str = field(default="", repr=False)
    _presence_notified: dict[str, str] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- 对外入口
    def handle(self, envelope: dict[str, Any], device_id: str | None = None) -> list[dict[str, Any]]:
        """处理一条入站消息, 返回需要发出的响应列表.

        :param envelope: 七字段包络
        :param device_id: 当前连接已认证的设备; 未认证连接传 None
        """
        with self._lock:
            self._current_device = device_id or ""
            self._purge_expired()
            try:
                validate_envelope(envelope)
            except ProtocolError as exc:
                self.errors.append(f"{exc.code}: {exc.message}")
                if exc.code == PROTOCOL_VERSION_UNSUPPORTED:
                    return [
                        self._error(
                            PROTOCOL_VERSION_UNSUPPORTED,
                            "协议版本不兼容",
                            envelope.get("session_id") if isinstance(envelope, dict) else None,
                        )
                    ]
                return []

            msg_type = envelope["type"]
            if msg_type in SERVER_TYPES:
                self.errors.append(f"UNEXPECTED_TYPE: 客户端不得发送 {msg_type}")
                return []

            handler = getattr(self, f"_handle_{msg_type}", None)
            if handler is None:
                self.errors.append(f"UNKNOWN_TYPE: {msg_type}")
                return []
            responses = handler(envelope, envelope["payload"])
            self._after_handle(msg_type, envelope, device_id)
            return responses

    # ---------------------------------------------------------------- 设备与认证
    def _handle_register(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        public_key = str(payload["public_key"])
        device_id = str(payload["device_id"])
        try:
            derived = device_id_from_public_key(b64decode(public_key))
        except ValueError:
            self.errors.append("AUTH_FAILED: public_key 不是合法 Base64")
            return [self._error(AUTH_FAILED, "设备公钥不合法", envelope["session_id"])]

        if derived != device_id:
            self.errors.append("AUTH_FAILED: device_id 与公钥不一致")
            return [self._error(AUTH_FAILED, "设备标识与公钥不一致", envelope["session_id"])]

        if not verify_envelope(envelope, public_key):
            self.errors.append("AUTH_FAILED: register 签名无效")
            return [self._error(AUTH_FAILED, "注册签名无效", envelope["session_id"])]

        self.devices[device_id] = RegisteredDevice(
            public_key_b64=public_key,
            display_name=str(payload.get("display_name", "")),
        )
        nonce = b64encode(os.urandom(16))
        expires_at = iso_after(self.challenge_ttl_s)
        self.challenges[device_id] = (nonce, expires_at)
        return [
            build_envelope(
                self.identity,
                "challenge",
                {"nonce": nonce, "expires_at": expires_at},
                session_id=envelope["session_id"],
            )
        ]

    def _handle_auth(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        device_id = str(payload["device_id"])
        device = self.devices.get(device_id)
        if device is None:
            self.errors.append("AUTH_FAILED: 设备未注册")
            return [self._error(AUTH_FAILED, "设备未注册", envelope["session_id"])]

        challenge = self.challenges.pop(device_id, None)
        if challenge is None:
            self.errors.append("AUTH_FAILED: 缺少挑战")
            return [self._error(AUTH_FAILED, "认证挑战不存在或已使用", envelope["session_id"])]

        nonce, expires_at = challenge
        if _iso_expired(expires_at):
            self.errors.append("AUTH_FAILED: 认证挑战已过期")
            return [self._error(AUTH_FAILED, "认证挑战已过期", envelope["session_id"])]
        if not verify_signature(
            device.public_key_b64,
            {"device_id": device_id, "nonce": nonce},
            str(payload["signature"]),
        ):
            self.errors.append("AUTH_FAILED: 认证签名无效")
            return [self._error(AUTH_FAILED, "认证签名无效", envelope["session_id"])]

        token = secrets.token_urlsafe(32)
        self.tokens.add(token)
        self.token_devices[token] = device_id
        expires_at = iso_after(self.challenge_ttl_s)
        return [
            build_envelope(
                self.identity,
                "auth_ok",
                {"token": token, "expires_at": expires_at, "relay_endpoint": self.relay_endpoint},
                session_id=envelope["session_id"],
            )
        ]

    # ---------------------------------------------------------------- 在线心跳
    def _handle_presence(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """设备级在线心跳: 只记录收到时间, 不参与鉴权与授权判定.

        契约 v1.2: 必须来自已认证连接; 本实现不回包, last_seen_at 经内部查询提供给 list_devices.
        """
        device_id = str(payload.get("device_id") or "")
        state = str(payload.get("state") or "")
        if not device_id or not state:
            self.errors.append("AUTH_FAILED: presence 缺少 device_id 或 state")
            return [self._error(AUTH_FAILED, "presence 缺少必要字段", envelope["session_id"])]
        if not self._current_device or device_id != self._current_device:
            self.errors.append("AUTH_FAILED: presence 与已认证设备不一致")
            return [self._error(AUTH_FAILED, "presence 必须来自已认证连接", envelope["session_id"])]
        device = self.devices.get(device_id)
        if device is None:
            self.errors.append("AUTH_FAILED: presence 设备未注册")
            return [self._error(AUTH_FAILED, "设备未注册", envelope["session_id"])]
        if not verify_envelope(envelope, device.public_key_b64):
            self.errors.append("AUTH_FAILED: presence 签名无效")
            return [self._error(AUTH_FAILED, "presence 签名无效", envelope["session_id"])]
        self.presence[device_id] = PresenceRecord(last_seen_at=time.time(), state_hint=state)
        self._notify_presence(device_id, PRESENCE_ONLINE)
        return []

    def _notify_presence(self, device_id: str, state: str) -> None:
        """状态变化时通知组装层一次, 避免每次心跳重复写账号库."""
        if self._presence_notified.get(device_id) == state:
            return
        self._presence_notified[device_id] = state
        if self.presence_sink is not None:
            self.presence_sink(device_id, state)

    # ---------------------------------------------------------------- 消息路由
    def _handle_offer(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._route(envelope, payload)

    def _handle_answer(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._route(envelope, payload)

    def _handle_candidate(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._route(envelope, payload)

    def _handle_session_start(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._route(envelope, payload)

    def _handle_session_end(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._route(envelope, payload)

    # ---------------------------------------------------------------- 会话数据面(降级转发)
    def _handle_file_offer(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_file_accept(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_file_chunk(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_file_ack(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_file_complete(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_file_abort(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_clipboard_update(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_clipboard_request(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _handle_clipboard_ack(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return self._session_plane(envelope, payload)

    def _session_plane(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """校验会话数据面消息并投递给会话对端; 契约规定正式链路走数据面, 信令侧为降级通道."""
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            self.errors.append("FILE_CHUNK_INVALID: 缺少 session_id")
            return [self._error(FILE_CHUNK_INVALID, "缺少 session_id", None)]
        device_id = self._current_device
        if not device_id:
            self.errors.append("AUTH_FAILED: 会话数据面消息来自未认证连接")
            return [self._error(AUTH_FAILED, "会话数据面消息必须来自已认证连接", session_id)]
        if not self._control_scope_ok(session_id):
            self.errors.append("SCOPE_INSUFFICIENT: view 授权不允许文件与剪贴板传输")
            return [self._error(SCOPE_INSUFFICIENT, "view 授权不允许文件与剪贴板传输", session_id)]
        result = self.session_plane.check(envelope["type"], payload)
        if not result.ok:
            self.errors.append(f"{result.code}: {result.message}")
            return [self._error(result.code, result.message, session_id)]
        if result.suppress:
            # 重复块与重复剪贴板摘要不再转发, 幂等由发送端按无回包处理
            return []
        return self._route_session_peer(envelope, payload, session_id, device_id)

    # ---------------------------------------------------------------- 屏幕墙
    def _handle_wall_focus(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        actor = self._wall_actor(envelope)
        wall_id, cell, error = _wall_target(payload)
        if actor is None:
            return [self._error(AUTH_FAILED, "屏幕墙操作必须来自已认证连接", envelope.get("session_id"))]
        if error:
            return [self._error("INVALID_MESSAGE", error, envelope.get("session_id"))]
        device_id, account_id = actor
        token, code = self.issue_focus_grant(wall_id, cell, account_id, device_id)
        if code:
            self.errors.append(f"{code}: 已有其它墙聚焦")
            return [self._error(code, "已有其它墙处于聚焦状态，请先退出原聚焦", envelope.get("session_id"))]
        return [
            build_envelope(
                self.identity,
                "wall_focus_granted",
                {
                    "wall_id": wall_id,
                    "cell": cell,
                    "focus_token": token,
                    "expires_at": iso_after(FOCUS_TOKEN_TTL_S),
                },
                session_id=envelope.get("session_id"),
            )
        ]

    def _handle_wall_unfocus(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self._wall_actor(envelope) is None:
            return [self._error(AUTH_FAILED, "屏幕墙操作必须来自已认证连接", envelope.get("session_id"))]
        wall_id = payload.get("wall_id")
        if not isinstance(wall_id, str) or not wall_id:
            return [self._error("INVALID_MESSAGE", "wall_id 必须是非空字符串", envelope.get("session_id"))]
        released = self.stop_focus(wall_id)
        return [
            build_envelope(
                self.identity,
                "wall_unfocused",
                {"wall_id": wall_id, "released": released},
                session_id=envelope.get("session_id"),
            )
        ]

    def _handle_wall_preview(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        actor = self._wall_actor(envelope)
        wall_id, cell, error = _wall_target(payload)
        if actor is None:
            return [self._error(AUTH_FAILED, "屏幕墙操作必须来自已认证连接", envelope.get("session_id"))]
        if error:
            return [self._error("INVALID_MESSAGE", error, envelope.get("session_id"))]
        device_id, account_id = actor
        revoked = [
            grant.device_id
            for grant in self.relay_grants.values()
            if grant.scope == "view"
            and grant.wall_id == wall_id
            and grant.cell == cell
            and grant.account_id == account_id
            and grant.device_id != device_id
        ]
        token = self.issue_preview_grant(wall_id, cell, account_id, device_id)
        self._push_preview_revoked(revoked, wall_id, cell, "重新签发同格预览令牌")
        return [
            build_envelope(
                self.identity,
                "wall_preview_granted",
                {
                    "wall_id": wall_id,
                    "cell": cell,
                    "preview_token": token,
                    "expires_at": iso_after(PREVIEW_TOKEN_TTL_S),
                },
                session_id=envelope.get("session_id"),
            )
        ]

    def _wall_actor(self, envelope: dict[str, Any]) -> tuple[str, str] | None:
        """身份只取自已认证连接并校验签名; 账号由组装层解析, 绝不采信 payload 自报."""
        device_id = self._current_device
        if not device_id:
            return None
        device = self.devices.get(device_id)
        if device is None or not verify_envelope(envelope, device.public_key_b64):
            return None
        account_id = self.account_of(device_id) if self.account_of is not None else device_id
        return device_id, account_id or device_id

    def _push_preview_revoked(self, device_ids: list[str], wall_id: str, cell: int, reason: str) -> None:
        if not device_ids or self.deliver is None:
            return
        envelope = build_envelope(
            self.identity,
            "wall_preview_revoked",
            {"wall_id": wall_id, "cell": cell, "reason": reason},
        )
        for device_id in dict.fromkeys(device_ids):
            self.deliver(device_id, envelope)

    def _control_scope_ok(self, session_id: str) -> bool:
        """会话存在 view 或 focus 授予而不存在 control 授予时拒绝传输."""
        now = time.time()
        scopes = {
            grant.scope
            for grant in self.relay_grants.values()
            if grant.session_id == session_id and grant.expires_at > now
        }
        return "control" in scopes or not scopes

    def _route_session_peer(
        self,
        envelope: dict[str, Any],
        payload: dict[str, Any],
        session_id: str,
        device_id: str,
    ) -> list[dict[str, Any]]:
        """按会话参与者确定唯一的对端连接并投递, 不依赖契约未定义的收发方字段."""
        device = self.devices.get(device_id)
        if device is None:
            return [self._error(AUTH_FAILED, "发送方未注册", session_id)]
        if not verify_envelope(envelope, device.public_key_b64):
            return [self._error(AUTH_FAILED, "会话数据面消息签名无效", session_id)]
        state = self.sessions.get(session_id)
        peers = [item for item in (state.devices if state is not None else set()) if item != device_id]
        if state is None or state.ended or len(peers) != 1:
            return [self._error(PEER_OFFLINE, "对端设备不在线", session_id)]
        peer = peers[0]
        record = RoutedMessage(
            from_device_id=device_id,
            to_device_id=peer,
            msg_type=envelope["type"],
            envelope=envelope,
        )
        if self.deliver is None:
            self.routed.append(record)
            return [envelope]
        if not self.deliver(peer, envelope):
            return [self._error(PEER_OFFLINE, "对端设备不在线", session_id)]
        self.routed.append(record)
        return []

    def _route(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        if envelope["type"] not in ROUTED_TYPES:
            self.errors.append(f"UNROUTABLE_TYPE: {envelope['type']}")
            return []

        # 契约缺口: v1 未定义路由用的收发方字段, 本实现要求可选 from_device_id/to_device_id
        from_device_id = payload.get("from_device_id")
        to_device_id = payload.get("to_device_id")
        if not from_device_id or not to_device_id:
            self.errors.append("MISSING_ROUTE_FIELD: 缺少 from_device_id 或 to_device_id")
            return []
        device = self.devices.get(str(from_device_id))
        if device is None:
            self.errors.append("AUTH_FAILED: 发送方未注册")
            return [self._error(AUTH_FAILED, "发送方未注册", envelope["session_id"])]
        if not verify_envelope(envelope, device.public_key_b64):
            self.errors.append("AUTH_FAILED: 转发消息签名无效")
            return [self._error(AUTH_FAILED, "转发消息签名无效", envelope["session_id"])]
        if str(to_device_id) not in self.devices:
            return [self._error(PEER_OFFLINE, "对端设备不在线", envelope["session_id"])]

        record = RoutedMessage(
            from_device_id=str(from_device_id),
            to_device_id=str(to_device_id),
            msg_type=envelope["type"],
            envelope=envelope,
        )
        if self.deliver is None:
            # 单进程用法: 保持旧行为, 把包络返回给调用方自行处理
            self.routed.append(record)
            return [envelope]
        if not self.deliver(str(to_device_id), envelope):
            return [self._error(PEER_OFFLINE, "对端设备不在线", envelope["session_id"])]
        self.routed.append(record)
        return []

    # ---------------------------------------------------------------- 中继预算
    def _handle_relay_allocate(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        session_id = str(payload["session_id"])
        requested = max(0, int(payload["requested_kbps"]))
        budget = self.session_budget_kbps()
        granted = max(0, min(requested, budget))

        # 会话数上限只统计正式会话; 屏幕墙预览与聚焦走独立令牌通道
        now = time.time()
        active_sessions = {
            grant.session_id
            for grant in self.relay_grants.values()
            if grant.expires_at > now and grant.scope == "control"
        }
        if session_id not in active_sessions and len(active_sessions) >= self.max_sessions:
            self.errors.append("RELAY_FULL: 中继会话已满")
            return [self._error(RELAY_FULL, "中继会话已满", envelope["session_id"])]
        if granted < self.min_session_kbps:
            self.errors.append("RELAY_FULL: 中继预算不足")
            return [self._error(RELAY_FULL, "中继预算不足", envelope["session_id"])]

        # 只吊销同设备同会话的旧令牌: 令牌绑定「设备 + 会话」, 同会话对端令牌必须继续有效
        self._revoke_device_session_grants(session_id, self._current_device)
        profile = "L0" if granted >= 2000 else "L1" if granted >= 1200 else "L2" if granted >= 600 else "L3"
        relay_token = secrets.token_urlsafe(24)
        # 令牌绑定会话、预算与过期时间, 数据面只认这份记录
        self.relay_grants[relay_token] = RelayGrantRecord(
            session_id=session_id,
            budget_kbps=granted,
            expires_at=now + self.challenge_ttl_s,
            device_id=self._current_device,
            scope="control",
        )
        return [
            build_envelope(
                self.identity,
                "relay_allocated",
                {
                    "session_id": payload["session_id"],
                    "relay_token": relay_token,
                    "endpoint": self.relay_endpoint,
                    "budget_kbps": granted,
                    "quality_profile": profile,
                },
                session_id=envelope["session_id"],
            )
        ]

    # ---------------------------------------------------------------- 本地消息
    def _handle_heartbeat(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def _handle_error(self, envelope: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        self.errors.append(f"PEER_ERROR: {payload.get('code')} {payload.get('message')}")
        return []

    # ---------------------------------------------------------------- 内部工具
    def _error(self, code: str, message: str, session_id: str | None) -> dict[str, Any]:
        return build_envelope(
            self.identity,
            "error",
            {"code": code, "message": message},
            session_id=session_id,
        )

    def _after_handle(self, msg_type: str, envelope: dict[str, Any], device_id: str | None) -> None:
        """处理会话生命周期: 记录参与者与活动时间, 收到 session_end 即回收授予."""
        payload = envelope["payload"]
        session_id = envelope.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        if msg_type == "session_end":
            self._end_session(session_id)
            return
        if msg_type not in SESSION_TRACKED_TYPES:
            return
        sender = device_id or str(payload.get("from_device_id") or "")
        self._touch_session(session_id, sender)

    def _touch_session(self, session_id: str, device_id: str) -> None:
        state = self.sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self.sessions[session_id] = state
        elif state.ended:
            # 已结束会话的迟到消息不重新激活, 避免僵尸会话占位
            return
        if device_id:
            state.devices.add(device_id)
        state.last_activity = time.time()

    def _end_session(self, session_id: str) -> None:
        state = self.sessions.get(session_id)
        if state is not None:
            state.ended = True
            state.last_activity = time.time()
        self._revoke_session_grants(session_id)

    def _revoke_session_grants(self, session_id: str) -> None:
        for token, grant in list(self.relay_grants.items()):
            if grant.session_id == session_id:
                self.relay_grants.pop(token, None)

    def _revoke_device_session_grants(self, session_id: str, device_id: str) -> None:
        """只吊销同设备同会话的旧控制令牌; 令牌绑定设备与会话, 不能误伤同会话对端."""
        for token, grant in list(self.relay_grants.items()):
            if grant.session_id == session_id and grant.device_id == device_id:
                self.relay_grants.pop(token, None)
                if token == self.focus_token:
                    self.focus_token = ""

    def _purge_expired(self) -> None:
        """清理过期挑战、令牌、授予与已结束会话, 防止长跑进程内存只增不减."""
        now = time.time()
        for device_id, (_nonce, challenge_expires) in list(self.challenges.items()):
            if _iso_expired(challenge_expires, now):
                self.challenges.pop(device_id, None)
        for token, token_expires in list(self.token_expiry.items()):
            if token_expires <= now:
                self.token_expiry.pop(token, None)
                self.tokens.discard(token)
                self.token_devices.pop(token, None)
        for token, grant in list(self.relay_grants.items()):
            if grant.expires_at <= now:
                self.relay_grants.pop(token, None)
                if token == self.focus_token:
                    self.focus_token = ""
        for session_id, state in list(self.sessions.items()):
            if state.ended and now - state.last_activity > SESSION_RETENTION_S:
                self.sessions.pop(session_id, None)
                self.session_plane.forget_session(session_id)
            elif (
                not state.ended
                and now - state.last_activity > SESSION_IDLE_TTL_S
                and not any(grant.session_id == session_id for grant in self.relay_grants.values())
            ):
                # 无活动且无有效授予的会话按超时结束, 不涉及令牌吊销
                self._end_session(session_id)
        for device_id, record in self.presence.items():
            if now - record.last_seen_at > PRESENCE_ONLINE_WINDOW_S:
                # 90 秒未心跳只把在线状态改为离线, 不吊销令牌、不结束会话
                self._notify_presence(device_id, PRESENCE_OFFLINE)

    # ---------------------------------------------------------------- 在线状态与统计
    def mark_online(self, device_id: str) -> None:
        """连接认证成功后登记传输连接; 不等于 presence 在线."""
        if not device_id:
            return
        with self._lock:
            self.connected.add(device_id)

    def mark_offline(self, device_id: str) -> list[str]:
        """连接断开时只摘除传输连接.

        契约 v1.2: 离线只影响在线状态, 不吊销令牌、不结束会话.
        """
        if not device_id:
            return []
        with self._lock:
            self.connected.discard(device_id)
            return []

    def presence_for(self, device_id: str, now: float | None = None) -> dict[str, Any]:
        """按 90 秒窗口计算设备在线状态.

        :return: 含 presence_state、last_seen_at、online; 未上报过 presence 为 unknown
        """
        with self._lock:
            record = self.presence.get(device_id)
        if record is None:
            return {"presence_state": PRESENCE_UNKNOWN, "last_seen_at": None, "online": False}
        current = time.time() if now is None else now
        state = PRESENCE_ONLINE if current - record.last_seen_at <= PRESENCE_ONLINE_WINDOW_S else PRESENCE_OFFLINE
        return {
            "presence_state": state,
            "last_seen_at": _iso(record.last_seen_at),
            "online": state == PRESENCE_ONLINE,
        }

    def presence_snapshot(self, device_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        """批量返回在线状态, 供 list_devices 一次性取齐, 避免逐台口径漂移."""
        with self._lock:
            targets = list(self.devices) if device_ids is None else list(device_ids)
        return {device_id: self.presence_for(device_id) for device_id in targets}

    def session_stats(self) -> dict[str, int]:
        """返回可公开的在线、会话与授予计数, 不含令牌与内容."""
        with self._lock:
            now = time.time()
            online = sum(
                1 for record in self.presence.values() if now - record.last_seen_at <= PRESENCE_ONLINE_WINDOW_S
            )
            return {
                "connected_devices": len(self.connected),
                "online_devices": online,
                "known_presence": len(self.presence),
                "active_sessions": sum(1 for state in self.sessions.values() if not state.ended),
                "retained_sessions": len(self.sessions),
                "active_grants": sum(1 for grant in self.relay_grants.values() if grant.expires_at > now),
            }

    def issue_token(self, device_id: str) -> str:
        """测试辅助: 直接签发令牌, 不经过挑战流程."""
        token = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
        with self._lock:
            self.tokens.add(token)
            self.token_devices[token] = device_id
            self.token_expiry[token] = time.time() + self.challenge_ttl_s
            self.devices.setdefault(device_id, RegisteredDevice(public_key_b64=""))
        return token

    def resolve_relay_token(self, relay_token: str) -> RelayGrantRecord | None:
        """按 relay_token 取授予记录; 过期或不存在返回 None."""
        with self._lock:
            self._purge_expired()
            return self.relay_grants.get(relay_token)

    # ---------------------------------------------------------------- 登出与吊销
    def kick_device(self, device_id: str) -> dict[str, int]:
        """登出或吊销设备令牌后的立即清理.

        :return: 被结束的会话数与吊销的令牌数, 供命令层回填 kicked_sessions
        """
        with self._lock:
            self.presence.pop(device_id, None)
            self.connected.discard(device_id)
            ended = 0
            for session_id, state in list(self.sessions.items()):
                if not state.ended and device_id in state.devices:
                    self._end_session(session_id)
                    ended += 1
            revoked = 0
            for token, owner in list(self.token_devices.items()):
                if owner == device_id:
                    self.token_devices.pop(token, None)
                    self.tokens.discard(token)
                    self.token_expiry.pop(token, None)
                    revoked += 1
            for token, grant in list(self.relay_grants.items()):
                if grant.device_id == device_id:
                    self.relay_grants.pop(token, None)
                    if token == self.focus_token:
                        self.focus_token = ""
            return {"kicked_sessions": ended, "revoked_tokens": revoked}

    # ---------------------------------------------------------------- 屏幕墙令牌
    def issue_preview_grant(
        self,
        wall_id: str,
        cell: int,
        account_id: str,
        device_id: str,
        ttl_s: int = PREVIEW_TOKEN_TTL_S,
    ) -> str:
        """签发预览令牌: 绑定墙、格、账号与设备, 60 秒有效, 非一次性.

        同一墙同一格同一账号重新签发即吊销旧令牌; 只允许订阅方向使用.
        """
        if ttl_s <= 0 or ttl_s > 300:
            raise ValueError("预览令牌有效期必须在 1 至 300 秒之间")
        with self._lock:
            for token, grant in list(self.relay_grants.items()):
                if (
                    grant.scope == "view"
                    and grant.wall_id == wall_id
                    and grant.cell == cell
                    and grant.account_id == account_id
                ):
                    self.relay_grants.pop(token, None)
            token = secrets.token_urlsafe(24)
            self.relay_grants[token] = RelayGrantRecord(
                session_id=f"preview:{wall_id}:{cell}",
                budget_kbps=min(300, self.session_budget_kbps()),
                expires_at=time.time() + ttl_s,
                device_id=device_id,
                scope="view",
                wall_id=wall_id,
                cell=cell,
                account_id=account_id,
            )
            return token

    def revoke_preview_grant(self, relay_token: str) -> bool:
        """吊销单个预览令牌; 不存在或不是预览令牌返回 False."""
        with self._lock:
            grant = self.relay_grants.get(relay_token)
            if grant is None or grant.scope != "view":
                return False
            self.relay_grants.pop(relay_token, None)
            return True

    def issue_focus_grant(
        self,
        wall_id: str,
        cell: int,
        account_id: str,
        device_id: str,
        ttl_s: int = 300,
    ) -> tuple[str, str]:
        """签发聚焦令牌, 全服务同一时刻只允许一块墙聚焦.

        :return: (令牌, 错误码); 名额被其它墙占用时返回 ("", "WALL_BUDGET_EXCEEDED")
        """
        if ttl_s <= 0 or ttl_s > 300:
            raise ValueError("聚焦令牌有效期必须在 1 至 300 秒之间")
        with self._lock:
            self._purge_expired()
            holder = self.relay_grants.get(self.focus_token) if self.focus_token else None
            if holder is not None and holder.wall_id != wall_id:
                return "", "WALL_BUDGET_EXCEEDED"
            if self.focus_token:
                self.relay_grants.pop(self.focus_token, None)
            token = secrets.token_urlsafe(24)
            self.relay_grants[token] = RelayGrantRecord(
                session_id=f"focus:{wall_id}:{cell}",
                budget_kbps=self.session_budget_kbps(),
                expires_at=time.time() + ttl_s,
                device_id=device_id,
                scope="focus",
                wall_id=wall_id,
                cell=cell,
                account_id=account_id,
            )
            self.focus_token = token
            return token, ""

    def stop_focus(self, wall_id: str = "") -> bool:
        """退出聚焦并释放名额; 指定 wall_id 时只释放该墙的名额."""
        with self._lock:
            holder = self.relay_grants.get(self.focus_token) if self.focus_token else None
            if holder is None:
                self.focus_token = ""
                return False
            if wall_id and holder.wall_id != wall_id:
                return False
            self.relay_grants.pop(self.focus_token, None)
            self.focus_token = ""
            return True

    def revoke_wall_grants(self, wall_id: str) -> int:
        """关闭屏幕墙时吊销它的全部预览与聚焦令牌."""
        with self._lock:
            removed = 0
            for token, grant in list(self.relay_grants.items()):
                if grant.wall_id == wall_id:
                    self.relay_grants.pop(token, None)
                    removed += 1
            if self.focus_token and self.focus_token not in self.relay_grants:
                self.focus_token = ""
            return removed

    def focus_stats(self) -> dict[str, Any]:
        """返回当前聚焦名额持有情况, 供审计与健康检查."""
        with self._lock:
            self._purge_expired()
            holder = self.relay_grants.get(self.focus_token) if self.focus_token else None
            if holder is None:
                return {"focused": False, "wall_id": None, "cell": None, "account_id": None}
            return {
                "focused": True,
                "wall_id": holder.wall_id,
                "cell": holder.cell,
                "account_id": holder.account_id,
            }

    def session_budget_kbps(self) -> int:
        """按出口与并发计算单会话预算, 与 relay_allocate 使用同一公式."""
        return math.floor(self.egress_mbps * 1000 * (1 - self.overhead_ratio) / max(1, self.max_sessions))


# 参与会话生命周期统计的消息类型: 路由类消息加上预算申请与保活
SESSION_TRACKED_TYPES: frozenset[str] = ROUTED_TYPES | SESSION_PLANE_TYPES | {"relay_allocate", "heartbeat"}


def _wall_target(payload: dict[str, Any]) -> tuple[str, int, str]:
    """解析屏幕墙目标, 返回 (wall_id, cell, 错误信息)."""
    wall_id = payload.get("wall_id")
    cell = payload.get("cell")
    if not isinstance(wall_id, str) or not wall_id:
        return "", -1, "wall_id 必须是非空字符串"
    if isinstance(cell, bool) or not isinstance(cell, int) or cell < 0:
        return "", -1, "cell 必须是非负整数"
    return wall_id, cell, ""


def _iso(timestamp: float) -> str:
    """把 epoch 秒转成与契约一致的 ISO 8601 UTC 字符串."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _iso_expired(value: str, now: float | None = None) -> bool:
    """判断 ISO 时间是否已过期; 不可解析按过期处理, 宁可拒绝."""
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    if moment.tzinfo is None:
        return True
    current = time.time() if now is None else now
    return moment.astimezone(UTC).timestamp() <= current
