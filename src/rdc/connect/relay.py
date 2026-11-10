# ==========================================================================================
# 文件        : relay.py
# 所属目录    : src/rdc/connect/
# 文件用途    : 真实信令/中继客户端: TLS 1.3 连接、设备鉴权、候选交换、relay_allocate 预算申请
# 架构定位    : 连接策略的“真实 relay_requester / peer 候选来源”, 只走已上线公网中继
# 核心功能    : 信任锚查找(环境变量 → 包内 assets → 开发树); SNI 校验; offer/answer 交换; 中继预算申请
# 职责边界    : 不改中继配置、不落盘凭据、不处理媒体帧; 失败一律转中文原因
# 运行环境    : Python 3.12
# 依赖关系    : 被 src/rdc/connect/runtime.py 调用; 返回 policy.Candidate / relay_allocated 载荷
# ==========================================================================================

"""公网中继与信令接入.

端点默认 47.109.32.249:8750, 证书 SNI 为 relay.wuge.fun. 信任锚按
``RDC_RELAY_CA`` → 冻结包内 ``assets/ca.crt`` → 开发树 ``deploy/relay/ca.crt``
的顺序查找, 全部缺失时报中文原因, 不会退回空信任锚. 端点与 SNI 可用环境变量覆盖:

- ``RDC_RELAY_ENDPOINT``  host:port
- ``RDC_RELAY_SNI``       证书主机名
- ``RDC_RELAY_CA``        CA 文件路径
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rdc.connect.policy import (
    DEFAULT_PRIORITY,
    FAMILY_V4,
    FAMILY_V6,
    KIND_HOST,
    MAX_PRIORITY,
    Candidate,
    infer_family,
    is_publishable,
    normalize_candidate,
)
from rdc.crypto.identity import DeviceIdentity
from rdc.signaling.envelope import build_envelope

MAX_FRAME_BYTES = 6 << 20
DEFAULT_ENDPOINT = "47.109.32.249:8750"
DEFAULT_SNI = "relay.wuge.fun"
RELAY_BUDGET_KBPS = 2900
RELAY_PROFILE = "720p30"
EST_DURATION_S = 3600


class RelayUnavailable(RuntimeError):
    """中继或信令不可用, 消息为可展示的中文原因."""


def relay_endpoint() -> tuple[str, int]:
    """读取中继端点, 非法配置抛 RelayUnavailable."""
    text = os.environ.get("RDC_RELAY_ENDPOINT", DEFAULT_ENDPOINT).strip()
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise RelayUnavailable(f"中继端点配置非法: {text}")
    return host, int(port)


def default_ca_path() -> Path:
    """返回开发树内中继信任锚路径."""
    return Path(__file__).resolve().parents[3] / "deploy" / "relay" / "ca.crt"


def bundled_ca_roots() -> tuple[Path, ...]:
    """返回冻结包内资源根目录; 开发态为空."""
    roots: list[Path] = []
    resource_root = getattr(sys, "_MEIPASS", "")
    if resource_root:
        roots.append(Path(resource_root))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    return tuple(roots)


def ca_candidates() -> tuple[Path, ...]:
    """按环境变量、包内资源、开发树的顺序给出信任锚候选路径."""
    override = os.environ.get("RDC_RELAY_CA", "").strip()
    if override:
        return (Path(override),)
    candidates: list[Path] = []
    for root in bundled_ca_roots():
        candidates.append(root / "assets" / "ca.crt")
        candidates.append(root / "rdc" / "assets" / "ca.crt")
        candidates.append(root / "deploy" / "relay" / "ca.crt")
    candidates.append(default_ca_path())
    return tuple(dict.fromkeys(candidates))


def relay_ca_path() -> Path:
    """定位中继信任锚; 全部缺失时抛中文原因, 不使用空信任锚."""
    candidates = ca_candidates()
    for path in candidates:
        if path.is_file():
            return path
    if os.environ.get("RDC_RELAY_CA", "").strip():
        raise RelayUnavailable(f"中继信任锚不存在, 请检查 RDC_RELAY_CA: {candidates[0]}")
    searched = "、".join(str(path) for path in candidates)
    raise RelayUnavailable(f"缺少中继信任锚, 已查找: {searched}; 可用 RDC_RELAY_CA 指定 ca.crt")


class SignalingChannel:
    """TLS 1.3 信令通道, 负责帧收发与设备鉴权."""

    def __init__(
        self,
        identity: DeviceIdentity,
        endpoint: tuple[str, int] | None = None,
        sni: str | None = None,
        ca_path: Path | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self.identity = identity
        self.endpoint = endpoint or relay_endpoint()
        self.sni = sni or os.environ.get("RDC_RELAY_SNI", DEFAULT_SNI)
        self.ca_path = ca_path
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        """建立 TLS 1.3 连接并完成设备鉴权."""
        ca_path = self.ca_path or relay_ca_path()
        self.ca_path = ca_path
        if not ca_path.is_file():
            raise RelayUnavailable(f"缺少中继信任锚: {ca_path}")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = True
        try:
            raw = socket.create_connection(self.endpoint, timeout=self.timeout_s)
            self.sock = context.wrap_socket(raw, server_hostname=self.sni)
            self.sock.settimeout(self.timeout_s)
        except (OSError, ssl.SSLError) as exc:
            raise RelayUnavailable(f"中继连接失败: {exc}") from exc
        self._authenticate()

    def close(self) -> None:
        """关闭连接, 可重复调用."""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> None:
        """按七字段包络发送一条消息."""
        if self.sock is None:
            raise RelayUnavailable("信令连接未建立")
        body = json.dumps(
            build_envelope(self.identity, msg_type, payload, session_id=session_id),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self.sock.sendall(struct.pack(">I", len(body)) + body)
        except OSError as exc:
            raise RelayUnavailable(f"信令发送失败: {exc}") from exc

    def recv(self) -> dict[str, Any]:
        """接收一帧 JSON; 连接关闭或超时转中文错误."""
        if self.sock is None:
            raise RelayUnavailable("信令连接未建立")
        try:
            header = self._recv_exactly(4)
            if header is None:
                raise RelayUnavailable("信令连接被对端关闭")
            (length,) = struct.unpack(">I", header)
            if length == 0 or length > MAX_FRAME_BYTES:
                raise RelayUnavailable(f"信令帧长度非法: {length}")
            body = self._recv_exactly(length)
            if body is None:
                raise RelayUnavailable("信令帧被截断")
        except TimeoutError as exc:
            raise RelayUnavailable("等待中继响应超时") from exc
        except OSError as exc:
            raise RelayUnavailable(f"信令接收失败: {exc}") from exc
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RelayUnavailable(f"信令帧不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise RelayUnavailable("信令帧不是 JSON 对象")
        return value

    def expect(self, msg_type: str) -> dict[str, Any]:
        """读取直到指定类型; 收到 error 直接转中文原因."""
        frame = self.recv()
        current = str(frame.get("type", ""))
        if current == "error":
            payload = frame.get("payload")
            message = (
                str(payload.get("message", "中继返回未知错误")) if isinstance(payload, dict) else "中继返回未知错误"
            )
            raise RelayUnavailable(message)
        if current != msg_type:
            raise RelayUnavailable(f"等待 {msg_type} 时收到 {current}")
        payload = frame.get("payload")
        return payload if isinstance(payload, dict) else {}

    def _recv_exactly(self, size: int) -> bytes | None:
        if self.sock is None:
            return None
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self.sock.recv(size - len(buffer))
            if not chunk:
                return None
            buffer.extend(chunk)
        return bytes(buffer)

    def _authenticate(self) -> None:
        self.send(
            "register",
            {
                "device_id": self.identity.device_id,
                "public_key": self.identity.public_key_b64,
                "capabilities": ["desktop", "control"],
                "protocol_version": 1,
                "display_name": "rdc-connect",
            },
        )
        nonce = str(self.expect("challenge").get("nonce", ""))
        self.send(
            "auth",
            {
                "device_id": self.identity.device_id,
                "signature": self.identity.sign({"device_id": self.identity.device_id, "nonce": nonce}),
            },
        )
        self.expect("auth_ok")


def candidate_to_payload(candidate: Candidate, session_id: str) -> dict[str, Any]:
    """候选转信令载荷, 输出 spec ipv6-punch §3.1 的全部字段.

    ``mapped`` 本地态在这里改写成 ``kind="srflx"`` + ``origin="mapped"``, 未知 kind 原样
    透传; 老端只读它认识的字段, 新增字段属于附加信息.
    """
    item = normalize_candidate(candidate)
    return {
        "session_id": session_id,
        "kind": item.kind,
        "address": item.address,
        "port": item.port,
        "priority": item.priority,
        "family": item.family,
        "scope": item.scope,
        "origin": item.origin,
        "related": item.related,
        "proto": item.proto,
    }


def payload_to_candidate(payload: dict[str, Any]) -> Candidate | None:
    """信令候选转 Candidate; 缺字段按下表容错推断, 不因未知字段抛异常.

    - ``family`` 缺失或非法: 按 ``address`` 字面量推断, 失败记 0(只允许中继使用);
    - ``kind="mapped"``: 归一为 ``srflx`` + ``origin="mapped"``;
    - ``scope``/``origin``/``proto``: 缺失按地址与 kind 推断.
    """
    address = str(payload.get("address", "")).strip().strip("[]")
    port = _payload_int(payload.get("port"), 0)
    if not address or port <= 0:
        return None
    family = infer_family(address)
    declared = payload.get("family")
    if isinstance(declared, int) and declared in {FAMILY_V4, FAMILY_V6}:
        family = declared
    priority = _payload_int(payload.get("priority"), DEFAULT_PRIORITY)
    candidate = Candidate(
        kind=str(payload.get("kind", KIND_HOST)),
        family=family,
        address=address,
        port=port,
        priority=max(0, min(MAX_PRIORITY, priority)),
        related=str(payload.get("related", "")),
        scope=str(payload.get("scope", "")),
        origin=str(payload.get("origin", "")),
        proto=str(payload.get("proto", "")),
    )
    normalized = normalize_candidate(candidate)
    return normalized if is_publishable(normalized) else None


def _payload_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def exchange_candidates(
    identity: DeviceIdentity,
    session_id: str,
    peer_device_id: str,
    local_candidates: Sequence[Candidate],
    access_proof: str,
    timeout_s: float = 3.0,
) -> tuple[Candidate, ...]:
    """发 offer 并等待 answer, 返回对端候选; 对端离线或超时返回空元组."""
    channel = SignalingChannel(identity, timeout_s=timeout_s)
    try:
        channel.connect()
        channel.send(
            "offer",
            {
                "session_id": session_id,
                "from_device_id": identity.device_id,
                "to_device_id": peer_device_id,
                "candidates": [
                    candidate_to_payload(item, session_id) for item in local_candidates if is_publishable(item)
                ],
                "media_caps": {"video": ["1080p30"], "audio": ["opus"]},
                "access_proof": access_proof,
            },
            session_id=session_id,
        )
        answer = channel.expect("answer")
    except RelayUnavailable:
        return ()
    finally:
        channel.close()
    chosen = answer.get("chosen_candidate")
    if not isinstance(chosen, dict):
        return ()
    candidate = payload_to_candidate(chosen)
    return (candidate,) if candidate is not None else ()


def request_relay_allocation(
    identity: DeviceIdentity,
    session_id: str,
    requested_kbps: int = RELAY_BUDGET_KBPS,
    est_duration_s: int = EST_DURATION_S,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    """申请中继预算, 失败抛 RelayUnavailable(中文)."""
    channel = SignalingChannel(identity, timeout_s=timeout_s)
    try:
        channel.connect()
        channel.send(
            "relay_allocate",
            {
                "session_id": session_id,
                "requested_kbps": requested_kbps,
                "est_duration_s": est_duration_s,
            },
            session_id=session_id,
        )
        return channel.expect("relay_allocated")
    finally:
        channel.close()
