# ==========================================================================================
# 文件        : verify_wall_bind.py
# 所属目录    : scripts/
# 文件用途    : 屏幕墙归属登记(wall_bind)的公网验收脚本, 提供 A6 越权的正向对照
# 架构定位    : 验收取证脚本, 自写最小客户端, 不复用被验方的客户端实现
# 核心功能    : 真实账号库播种; register→auth→offer/answer→wall_bind→预览正反对照
# 职责边界    : 不改服务端代码与配置; 不重启服务; 令牌与私钥只落本地证据目录且脱敏
# ==========================================================================================

"""屏幕墙归属登记公网验证: A6 越权的正向对照.

验收线在公网复现的完整序列:
    1. 准备真实账号库: 建两个账号 wall-verify-a / wall-verify-b, 各绑定一台新设备;
       本步骤需要写库权限, 在服务器上以 root 执行.
    2. 重启信令服务, 让运行中的进程重新加载账号库 (进程内缓存不会自动感知外部写入).
    3. 运行 verify: 两账号注册认证; 会话 S1 由 A 作被控端, A 上报 wall_bind;
       A 预览自己的墙成功; B 预览同一墙被 SCOPE_INSUFFICIENT 拒绝; A 再预览仍成功;
       会话 S2 由 B 作被控端, B 对同墙上报被 WALL_OWNERSHIP_CONFLICT 明确拒绝;
       未认证连接与非 host 参与方的 wall_bind 都被 AUTH_FAILED 拒绝.
    4. 再重启一次服务, 以 --expect-existing 重跑 verify: 归属仍在(幂等且落盘),
       A 仍可预览, B 仍被拒.

环境变量入口 (不传命令行参数时全部读环境变量):
    RDC_VERIFY_WALL_HOST          服务地址, 默认 127.0.0.1
    RDC_VERIFY_WALL_PORT          信令端口, 默认 8760 (relay 部署用 8750)
    RDC_VERIFY_WALL_SNI           TLS SNI, 默认 relay.wuge.fun
    RDC_VERIFY_WALL_CA            信任锚, 默认 deploy/relay/ca.crt; 文件不存在时退回明文 TCP
    RDC_VERIFY_WALL_ACCOUNTS_DB   真实账号库, 默认 /var/lib/rdc-signaling/accounts.db
                                  (relay 进程对应 /var/lib/rdc-relay/accounts.db)
    RDC_VERIFY_WALL_STATE         设备私钥与墙标识的本地保存路径,
                                  默认 evidence/wall-bind/fixtures.json (0600, 不随仓库提交)

用法:
    # 服务器上播种 (写真实账号库; 之后必须重启对应服务)
    sudo env PYTHONPATH=src python3 scripts/verify_wall_bind.py prepare
    sudo systemctl restart rdc-signaling

    # 本机或公网执行 (本机可直接连 127.0.0.1:8760; 公网按环境变量指向 relay.wuge.fun)
    PYTHONPATH=src python3 scripts/verify_wall_bind.py verify

    # 重启后确认归属仍在
    sudo systemctl restart rdc-signaling
    PYTHONPATH=src python3 scripts/verify_wall_bind.py verify --expect-existing

退出码: 0 全部通过; 1 有断言不成立; 2 连接失败或状态文件缺失.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CA = ROOT / "deploy" / "relay" / "ca.crt"
DEFAULT_STATE = ROOT / "evidence" / "wall-bind" / "fixtures.json"
DEFAULT_DB = "/var/lib/rdc-signaling/accounts.db"
MAX_FRAME = 4 * 1024 * 1024
SECRET_KEYS = {"relay_token", "token", "preview_token", "focus_token", "signature"}
ACCOUNT_A = "wall-verify-a"
ACCOUNT_B = "wall-verify-b"
PASSWORD = "wall-verify-password"


# ------------------------------------------------------------------ 最小协议客户端
class Client:
    """自生成 Ed25519 身份的最小客户端; device_id 与端点算法一致."""

    def __init__(self, sock: socket.socket, label: str, private_key: Ed25519PrivateKey) -> None:
        self.sock = sock
        self.label = label
        self.private_key = private_key
        self.public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        self.device_id = base64.b32encode(hashlib.sha256(self.public_raw).digest()).decode("ascii").rstrip("=")[:20]
        self.transcript: list[dict[str, Any]] = []

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode("ascii")

    def sign(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return base64.b64encode(self.private_key.sign(canonical.encode("utf-8"))).decode("ascii")

    def envelope(self, msg_type: str, payload: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "v": 1,
            "type": msg_type,
            "id": str(uuid.uuid4()),
            "session_id": session_id,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "payload": payload,
        }
        body["sig"] = self.sign(body)
        return body

    def send(self, message: dict[str, Any]) -> None:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.transcript.append({"direction": "send", "message": _redact(message)})
        self.sock.sendall(struct.pack(">I", len(raw)) + raw)

    def recv(self, timeout: float = 8.0) -> dict[str, Any]:
        self.sock.settimeout(timeout)
        header = _recv_exactly(self.sock, 4)
        if header is None:
            raise ConnectionError(f"{self.label} 连接被关闭")
        (length,) = struct.unpack(">I", header)
        if length == 0 or length > MAX_FRAME:
            raise ValueError(f"{self.label} 帧长度非法: {length}")
        body = _recv_exactly(self.sock, length)
        if body is None:
            raise ConnectionError(f"{self.label} 帧不完整")
        message = json.loads(body.decode("utf-8"))
        self.transcript.append({"direction": "recv", "message": _redact(message)})
        return message


def _recv_exactly(sock: socket.socket, size: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            return None
        buffer.extend(chunk)
    return bytes(buffer)


def _redact(message: Any) -> Any:
    if isinstance(message, dict):
        return {
            key: (f"<redacted:{len(value)}>" if key in SECRET_KEYS and isinstance(value, str) else _redact(value))
            for key, value in message.items()
        }
    if isinstance(message, list):
        return [_redact(item) for item in message]
    return message


def _error(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("type") != "error":
        return {}
    payload = message.get("payload") or {}
    return {"code": payload.get("code"), "message": payload.get("message")}


class Connector:
    """按 CA 是否存在选择 TLS 1.3 或明文 TCP; 生产 8760/8750 应为 TLS."""

    def __init__(self, host: str, port: int, sni: str, ca: Path, timeout: float) -> None:
        self.host = host
        self.port = port
        self.sni = sni
        self.timeout = timeout
        self.tls = ca.is_file()
        self.context: ssl.SSLContext | None = None
        if self.tls:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca))
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            self.context = context

    def connect(self) -> socket.socket:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.context is None:
            return raw
        return self.context.wrap_socket(raw, server_hostname=self.sni)

    def describe(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "sni": self.sni,
            "tls": self.tls,
        }


# ------------------------------------------------------------------ 播种真实账号库
def prepare(accounts_db: Path, state_path: Path) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.accounts.store import AccountStore

    if not accounts_db.exists():
        sys.stdout.write(f"账号库不存在: {accounts_db}\n")
        return 2
    store = AccountStore(accounts_db)
    fixtures: dict[str, Any] = {"accounts_db": str(accounts_db), "wall_id": f"wall-verify-{uuid.uuid4().hex[:8]}"}
    try:
        for label, name in (("a", ACCOUNT_A), ("b", ACCOUNT_B)):
            account = store.find_account_by_name(name) or store.create_account(name, PASSWORD)
            private_key = Ed25519PrivateKey.generate()
            public_raw = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )
            device_id = base64.b32encode(hashlib.sha256(public_raw).digest()).decode("ascii").rstrip("=")[:20]
            store.upsert_device(device_id, f"wall-verify-{label}")
            store.bind_device(device_id, account.account_id)
            fixtures[label] = {
                "account_id": account.account_id,
                "account_name": account.name,
                "device_id": device_id,
                "private_key": base64.b64encode(
                    private_key.private_bytes(
                        encoding=serialization.Encoding.Raw,
                        format=serialization.PrivateFormat.Raw,
                        encryption_algorithm=serialization.NoEncryption(),
                    )
                ).decode("ascii"),
            }
    finally:
        store.close()

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        state_path.chmod(0o600)
    sys.stdout.write(
        f"PREPARE_OK accounts_db={accounts_db} state={state_path} wall_id={fixtures['wall_id']}\n"
        "下一步: 重启对应服务让运行中的进程重新加载账号库, 再运行 verify\n"
    )
    return 0


# ------------------------------------------------------------------ 协议序列
def register_and_auth(client: Client) -> dict[str, Any]:
    client.send(
        client.envelope(
            "register",
            {
                "device_id": client.device_id,
                "public_key": client.public_b64,
                "capabilities": ["desktop"],
                "protocol_version": 1,
                "display_name": f"verify-{client.label}",
            },
        )
    )
    challenge = client.recv()
    nonce = str((challenge.get("payload") or {}).get("nonce") or "")
    client.send(
        client.envelope(
            "auth",
            {"device_id": client.device_id, "signature": client.sign({"device_id": client.device_id, "nonce": nonce})},
        )
    )
    return client.recv()


def establish(controller: Client, host: Client, session_id: str, timeout: float) -> None:
    """controller 发 offer, host 回 answer; host 由此成为该会话的被控端."""
    controller.send(
        controller.envelope(
            "offer",
            {
                "session_id": session_id,
                "candidates": [],
                "media_caps": {"codecs": ["h264"], "max_width": 1280, "max_height": 720},
                "access_proof": base64.b64encode(b"verify").decode("ascii"),
                "from_device_id": controller.device_id,
                "to_device_id": host.device_id,
            },
            session_id=session_id,
        )
    )
    delivered = host.recv(timeout)
    if delivered.get("type") != "offer":
        raise AssertionError(f"offer 未送达被控端: {delivered.get('type')}")
    host.send(
        host.envelope(
            "answer",
            {
                "session_id": session_id,
                "chosen_candidate": {},
                "media_caps": {"codecs": ["h264"]},
                "from_device_id": host.device_id,
                "to_device_id": controller.device_id,
            },
            session_id=session_id,
        )
    )
    if controller.recv(timeout).get("type") != "answer":
        raise AssertionError("answer 未送达控制端")


def _bind(client: Client, session_id: str, wall_id: str, timeout: float) -> dict[str, Any]:
    client.send(client.envelope("wall_bind", {"wall_id": wall_id, "session_id": session_id}, session_id=session_id))
    return client.recv(timeout)


def _preview(client: Client, session_id: str, wall_id: str, timeout: float) -> dict[str, Any]:
    client.send(client.envelope("wall_preview", {"wall_id": wall_id, "cell": 0}, session_id=session_id))
    return client.recv(timeout)


def verify(connector: Connector, state_path: Path, timeout: float, expect_existing: bool) -> int:
    if not state_path.is_file():
        sys.stdout.write(f"状态文件不存在, 先运行 prepare: {state_path}\n")
        return 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    wall_id = str(state["wall_id"])
    clients: list[Client] = []
    steps: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: dict[str, Any]) -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        sys.stdout.write(f"{'PASS' if ok else 'FAIL'} {name}: {json.dumps(detail, ensure_ascii=False)}\n")
        return ok

    def make(label: str) -> Client:
        key = base64.b64decode(state[label]["private_key"])
        client = Client(connector.connect(), label, Ed25519PrivateKey.from_private_bytes(key))
        clients.append(client)
        return client

    try:
        alice = make("a")
        bob = make("b")
        record("register_auth_a", register_and_auth(alice).get("type") == "auth_ok", {"device": alice.device_id})
        record("register_auth_b", register_and_auth(bob).get("type") == "auth_ok", {"device": bob.device_id})

        # 未认证连接: 不得登记他人墙
        anonymous = Client(connector.connect(), "anonymous", Ed25519PrivateKey.generate())
        clients.append(anonymous)
        anonymous.send(anonymous.envelope("wall_bind", {"wall_id": wall_id, "session_id": "none"}, session_id="none"))
        unauth = anonymous.recv(timeout)
        record(
            "anonymous_bind_rejected",
            unauth.get("type") == "error" and _error(unauth).get("code") == "AUTH_FAILED",
            _error(unauth),
        )

        # 会话 S1: B 控制 A (A 为被控端)
        s1 = f"verify-s1-{uuid.uuid4()}"
        establish(bob, alice, s1, timeout)
        first = _bind(alice, s1, wall_id, timeout)
        record("host_bind", first.get("type") == "wall_bound", {"type": first.get("type"), **_error(first)})
        bound_at = str((first.get("payload") or {}).get("bound_at") or "")

        # 非 host 参与方上报必须被拒
        denied = _bind(bob, s1, wall_id, timeout)
        record(
            "non_host_bind_rejected",
            denied.get("type") == "error" and _error(denied).get("code") == "AUTH_FAILED",
            _error(denied),
        )

        # 幂等重放
        again = _bind(alice, s1, wall_id, timeout)
        same_at = str((again.get("payload") or {}).get("bound_at") or "")
        record(
            "repeat_bind_idempotent",
            again.get("type") == "wall_bound" and same_at == bound_at,
            {"type": again.get("type"), "bound_at": same_at},
        )
        if expect_existing:
            record("bind_already_persisted", again.get("type") == "wall_bound", {"wall_id": wall_id})

        # 正向: A 预览自己的墙
        positive = _preview(alice, s1, wall_id, timeout)
        record("owner_preview_granted", positive.get("type") == "wall_preview_granted", {"type": positive.get("type")})

        # 反向: B 预览同一墙必须被拒
        negative = _preview(bob, s1, wall_id, timeout)
        record(
            "other_account_preview_rejected",
            negative.get("type") == "error" and _error(negative).get("code") == "SCOPE_INSUFFICIENT",
            _error(negative),
        )

        # 原账号仍然可用
        still = _preview(alice, s1, wall_id, timeout)
        record("owner_preview_still_granted", still.get("type") == "wall_preview_granted", {"type": still.get("type")})

        # 会话 S2: A 控制 B (B 为被控端), B 对同墙上报冲突必须明确拒绝
        s2 = f"verify-s2-{uuid.uuid4()}"
        establish(alice, bob, s2, timeout)
        conflict = _bind(bob, s2, wall_id, timeout)
        record(
            "conflict_rejected",
            conflict.get("type") == "error" and _error(conflict).get("code") == "WALL_OWNERSHIP_CONFLICT",
            _error(conflict),
        )
        after_conflict = _preview(alice, s1, wall_id, timeout)
        record(
            "owner_after_conflict_granted",
            after_conflict.get("type") == "wall_preview_granted",
            {"type": after_conflict.get("type")},
        )

        for client in clients:
            transcript.extend(client.transcript)
    except (OSError, ssl.SSLError, ConnectionError, ValueError, AssertionError) as error:
        record("exception", False, {"error": f"{type(error).__name__}: {error}"})
    finally:
        for client in clients:
            with contextlib.suppress(OSError):
                client.sock.close()

    passed = all(step["ok"] for step in steps)
    target = ROOT / "evidence" / f"wall-bind-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "result.json").write_text(
        json.dumps(
            {
                "target": connector.describe(),
                "wall_id": wall_id,
                "expect_existing": expect_existing,
                "steps": steps,
                "passed": passed,
                "transcript": transcript,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{'VERIFY_OK' if passed else 'VERIFY_FAILED'} 证据: {target}\n")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="屏幕墙归属登记 wall_bind 公网验证")
    parser.add_argument("command", choices=("prepare", "verify"), help="prepare 播种真实账号库; verify 跑协议序列")
    parser.add_argument("--host", default=os.environ.get("RDC_VERIFY_WALL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RDC_VERIFY_WALL_PORT", "8760")))
    parser.add_argument("--sni", default=os.environ.get("RDC_VERIFY_WALL_SNI", "relay.wuge.fun"))
    parser.add_argument("--ca", type=Path, default=Path(os.environ.get("RDC_VERIFY_WALL_CA", str(DEFAULT_CA))))
    parser.add_argument(
        "--accounts-db",
        type=Path,
        default=Path(os.environ.get("RDC_VERIFY_WALL_ACCOUNTS_DB", DEFAULT_DB)),
    )
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("RDC_VERIFY_WALL_STATE", str(DEFAULT_STATE))))
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--expect-existing",
        action="store_true",
        help="用于服务重启后复跑: 要求同一墙仍归属原账号且可预览",
    )
    arguments = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "src"))
    if arguments.command == "prepare":
        return prepare(arguments.accounts_db, arguments.state)
    connector = Connector(arguments.host, arguments.port, arguments.sni, arguments.ca, arguments.timeout)
    return verify(connector, arguments.state, arguments.timeout, arguments.expect_existing)


if __name__ == "__main__":
    raise SystemExit(main())
