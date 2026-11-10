# ==========================================================================================
# 文件        : signaling.py
# 所属目录    : poc/signaling/
# 文件用途    : PoC 信令服务与信令客户端, 承载注册、挑战、认证与 offer/answer/candidate 转发
# 架构定位    : PoC 信令层, 对应设计书 3.8 与协议契约 v1 的最小子集
# 核心功能    : WSS + 七字段包络 + Ed25519 验签; 设备注册与挑战应答; 会话消息按目标设备转发
# 职责边界    : 只转发信令, 不接触媒体与中继数据面; 不写业务状态机
# 运行环境    : Python 3.12; 依赖 websockets 与 cryptography
# 依赖关系    : 引用 common/crypto.py; 被 tools/run_p0_02_smoke.py 引用
# ==========================================================================================

"""PoC 信令服务与客户端."""

from __future__ import annotations

import json
import os
import queue
import ssl
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from websockets.sync.client import connect as ws_connect
from websockets.sync.server import serve as ws_serve

from poc.common.crypto import (
    DeviceIdentity,
    base64_decode,
    base64_encode,
    device_id_from_public_key,
    now_iso,
    verify_envelope,
)

ROUTED_TYPES = {"offer", "answer", "candidate", "session_start", "session_end"}


# ================================ SignalingServer ================================

class SignalingServer:
    """PoC 信令服务, 负责设备注册、挑战认证与会话消息转发.

    工作流程:
        1. start() 绑定端口并在后台线程运行 WSS 服务.
        2. 客户端先 register, 服务下发 challenge, 客户端 auth 后拿到 token.
        3. offer/answer/candidate 按 payload.target_device_id 转发给对端.

    实例属性清单:
        identity (DeviceIdentity): 服务自身签名身份.
        connections (dict): 设备标识到连接的映射.
        public_keys (dict): 设备标识到公钥的映射.

    构造参数:
        :param cert_path: TLS 证书路径
        :type cert_path: Path
        :param key_path: TLS 私钥路径
        :type key_path: Path

    运行约束:
        单进程; 连接对象加锁后发送; 不持久化任何设备数据.
    """

    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self.cert_path = cert_path
        self.key_path = key_path
        self.identity = DeviceIdentity.create()
        self.connections: dict[str, Any] = {}
        self.public_keys: dict[str, str] = {}
        self.authenticated: set[str] = set()
        self.challenges: dict[str, bytes] = {}
        self._send_locks: dict[str, threading.Lock] = {}
        self._server = None
        self._thread: threading.Thread | None = None
        self.messages: list[dict[str, Any]] = []
        self._messages_lock = threading.Lock()
        self.errors: list[str] = []

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """启动信令服务并返回实际端口.

        :param host: 监听地址
        :type host: str
        :param port: 监听端口, 0 表示由系统分配
        :type port: int
        :return: 实际监听端口
        :rtype: int
        """
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        self._server = ws_serve(self._handle, host, port, ssl=context)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return int(self._server.socket.getsockname()[1])

    def stop(self) -> None:
        """停止信令服务.

        :return: 无
        :rtype: None
        """
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    def snapshot(self) -> list[dict[str, Any]]:
        """返回处理过的消息类型流水, 用作证据.

        :return: 消息类型与时间戳列表
        :rtype: list
        """
        with self._messages_lock:
            return list(self.messages)

    def _record(self, envelope: dict[str, Any], direction: str) -> None:
        with self._messages_lock:
            self.messages.append(
                {
                    "ts": now_iso(),
                    "type": envelope.get("type", ""),
                    "id": envelope.get("id", ""),
                    "direction": direction,
                }
            )

    def _send(self, device_id: str, envelope: dict[str, Any]) -> bool:
        connection = self.connections.get(device_id)
        if connection is None:
            return False
        lock = self._send_locks.setdefault(device_id, threading.Lock())
        with lock:
            connection.send(json.dumps(envelope, ensure_ascii=False))
        return True

    def _reply(self, connection: Any, message_type: str, payload: dict[str, Any], session_id: str = "") -> None:
        envelope = self.identity.make_envelope(message_type, payload, session_id or None)
        connection.send(json.dumps(envelope, ensure_ascii=False))
        self._record(envelope, "server->client")

    def _handle(self, connection: Any) -> None:
        device_id: str | None = None
        try:
            for raw in connection:
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    self._reply(connection, "error", {"code": "BAD_JSON", "message": "消息不是合法 JSON"})
                    continue
                device_id = self._dispatch(connection, envelope) or device_id
        except Exception as error:
            with self._messages_lock:
                self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            if device_id is not None and self.connections.get(device_id) is connection:
                self.connections.pop(device_id, None)

    def _dispatch(self, connection: Any, envelope: dict[str, Any]) -> str | None:
        message_type = str(envelope.get("type", ""))
        payload = envelope.get("payload") or {}
        session_id = str(envelope.get("session_id", ""))
        self._record(envelope, "client->server")

        if message_type == "register":
            public_key_b64 = str(payload.get("public_key", ""))
            claimed = str(payload.get("device_id", ""))
            try:
                expected = device_id_from_public_key(base64_decode(public_key_b64))
            except ValueError:
                self._reply(connection, "error", {"code": "BAD_PUBLIC_KEY", "message": "公钥不是合法 Base64"})
                return None
            if claimed != expected:
                self._reply(connection, "error", {"code": "DEVICE_ID_MISMATCH", "message": "设备标识与公钥不一致"})
                return None
            if not verify_envelope(public_key_b64, envelope):
                self._reply(connection, "error", {"code": "SIGNATURE_INVALID", "message": "注册消息签名无效"})
                return None
            self.public_keys[claimed] = public_key_b64
            self.connections[claimed] = connection
            self._send_locks.setdefault(claimed, threading.Lock())
            nonce = os.urandom(32)
            self.challenges[claimed] = nonce
            self._reply(connection, "challenge", {"nonce": base64_encode(nonce), "expires_at": now_iso()})
            return claimed

        if message_type == "auth":
            claimed = str(payload.get("device_id", ""))
            public_key_b64 = self.public_keys.get(claimed)
            nonce = self.challenges.get(claimed)
            if public_key_b64 is None or nonce is None:
                self._reply(connection, "error", {"code": "NOT_REGISTERED", "message": "请先注册设备"})
                return claimed
            try:
                signature = base64_decode(str(payload.get("signature", "")))
            except ValueError:
                self._reply(connection, "error", {"code": "BAD_SIGNATURE", "message": "签名不是合法 Base64"})
                return claimed
            if not self._verify_nonce(public_key_b64, nonce, signature):
                self._reply(connection, "error", {"code": "AUTH_FAILED", "message": "挑战应答失败"})
                return claimed
            self.authenticated.add(claimed)
            token = base64_encode(os.urandom(16))
            self._reply(connection, "auth_ok", {"token": token, "expires_at": now_iso(), "relay_endpoint": ""})
            return claimed

        if message_type in ROUTED_TYPES:
            source = self._device_of(connection)
            if source is None or source not in self.authenticated:
                self._reply(connection, "error", {"code": "NOT_AUTHENTICATED", "message": "请先完成认证"})
                return source
            public_key_b64 = self.public_keys.get(source, "")
            if not verify_envelope(public_key_b64, envelope):
                self._reply(connection, "error", {"code": "SIGNATURE_INVALID", "message": "消息签名无效"})
                return source
            target = str(payload.get("target_device_id", ""))
            forwarded = dict(payload)
            forwarded["from_device_id"] = source
            outgoing = self.identity.make_envelope(message_type, forwarded, session_id or None)
            if not self._send(target, outgoing):
                self._reply(
                    connection,
                    "error",
                    {"code": "PEER_OFFLINE", "message": "对端不在线", "target_device_id": target},
                    session_id,
                )
                return source
            self._record(outgoing, "server->client")
            return source

        if message_type == "heartbeat":
            self._reply(connection, "heartbeat", {"seq": payload.get("seq", 0), "ts": now_iso()}, session_id)
            return self._device_of(connection)

        self._reply(connection, "error", {"code": "UNKNOWN_TYPE", "message": f"未知消息类型 {message_type}"}, session_id)
        return self._device_of(connection)

    def _device_of(self, connection: Any) -> str | None:
        for device_id, existing in self.connections.items():
            if existing is connection:
                return device_id
        return None

    @staticmethod
    def _verify_nonce(public_key_b64: str, nonce: bytes, signature: bytes) -> bool:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(base64_decode(public_key_b64))
        try:
            key.verify(signature, nonce)
        except Exception:
            return False
        return True


# ================================ SignalingClient ================================

class SignalingClient:
    """PoC 信令客户端, 负责注册、认证与收发信令消息.

    工作流程:
        1. connect() 建立 WSS 连接并启动接收线程.
        2. register()/authenticate() 完成设备注册与挑战应答.
        3. send() 发送消息; wait_for() 等待指定类型消息.

    实例属性清单:
        identity (DeviceIdentity): 本机设备身份.
        inbox (queue.Queue): 接收到的消息队列.
        connected (bool): 连接是否仍然有效.

    构造参数:
        :param identity: 本机设备身份
        :type identity: DeviceIdentity

    运行约束:
        单进程; 接收线程只入队, 不做业务判断.
    """

    def __init__(self, identity: DeviceIdentity) -> None:
        self.identity = identity
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.connected = False
        self._connection = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()

    def connect(self, url: str, context: ssl.SSLContext, timeout: float = 5.0) -> None:
        """连接信令服务.

        :param url: WSS 地址
        :type url: str
        :param context: 客户端 SSL 上下文
        :type context: ssl.SSLContext
        :param timeout: 连接超时秒数
        :type timeout: float
        :return: 无
        :rtype: None
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._connection = ws_connect(url, ssl=context, open_timeout=timeout + attempt * 5.0, max_size=None)
                break
            except Exception as error:
                last_error = error
                time.sleep(0.2)
        if self._connection is None:
            raise TimeoutError(f"信令连接失败: {last_error}")
        self.connected = True
        self._thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._thread.start()

    def _receive_loop(self) -> None:
        try:
            for raw in self._connection:
                try:
                    self.inbox.put(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        finally:
            self.connected = False

    def send(self, message_type: str, payload: dict[str, Any], session_id: str = "") -> dict[str, Any]:
        """发送一条已签名消息.

        :param message_type: 消息类型
        :type message_type: str
        :param payload: 消息体
        :type payload: dict
        :param session_id: 会话标识
        :type session_id: str
        :return: 发送的包络
        :rtype: dict
        """
        envelope = self.identity.make_envelope(message_type, payload, session_id or None)
        with self._send_lock:
            self._connection.send(json.dumps(envelope, ensure_ascii=False))
        return envelope

    def wait_for(self, types: Iterable[str], timeout: float = 5.0) -> dict[str, Any]:
        """等待指定类型的消息.

        :param types: 允许的消息类型集合
        :type types: Iterable[str]
        :param timeout: 超时秒数
        :type timeout: float
        :return: 匹配到的包络
        :rtype: dict
        :raises TimeoutError: 超时未收到指定类型消息时抛出
        """
        wanted = set(types)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"等待消息超时: {sorted(wanted)}")
            message = self.inbox.get(timeout=remaining)
            if message.get("type") in wanted:
                return message

    def register(self) -> dict[str, Any]:
        """注册设备并完成挑战应答.

        :return: auth_ok 包络
        :rtype: dict
        """
        self.send(
            "register",
            {
                "device_id": self.identity.device_id,
                "public_key": base64_encode(self.identity.public_key_bytes),
                "capabilities": ["screen", "input", "h264"],
                "protocol_version": 1,
            },
        )
        challenge = self.wait_for({"challenge"})
        nonce = base64_decode(str(challenge["payload"]["nonce"]))
        self.send("auth", {"device_id": self.identity.device_id, "signature": base64_encode(self.identity.sign(nonce))})
        return self.wait_for({"auth_ok", "error"})

    def close(self) -> None:
        """关闭连接.

        :return: 无
        :rtype: None
        """
        self.connected = False
        if self._connection is not None:
            self._connection.close()
            self._connection = None
