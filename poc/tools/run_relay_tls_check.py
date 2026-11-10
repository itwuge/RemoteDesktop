# ==========================================================================================
# 文件        : run_relay_tls_check.py
# 所属目录    : poc/tools/
# 文件用途    : 中继 TLS 1.3 本地两/多进程等价验证: 单向加密转发、双向校验通过与拒绝、错误 CA 拒绝
# 架构定位    : PoC 测试装置, 只在本机回环验证 TLS 接入, 不进入发布路径
# 核心功能    : 起中继子进程; 信令与数据面全走 TLS; 四场景断言; 输出 JSON 证据
# 职责边界    : 不采集屏幕、不编码、不解密业务内容、不测画质; 未跨公网, 不是画质通道验收
# 运行环境    : Python 3.12; 需要 D:\RDC-Build\certs\relay 下已生成证书
# 依赖关系    : rdc.relay.tls、rdc.relay.server、rdc.signaling、rdc.crypto.identity
# ==========================================================================================

r"""中继 TLS 1.3 本地等价验证执行器.

用法:
    $env:PYTHONPATH = 'src'; $env:RDC_BUILD_ROOT = 'D:\RDC-Build'
    python -m poc.tools.run_relay_tls_check

场景:
a) TLS 1.3 单向(仅服务端证书): 32 帧密文经中继转发, 订阅端逐字节一致.
b) require_client_cert=true 且客户端无证书: 握手/首帧失败, 被拒绝.
c) require_client_cert=true 且客户端证书正确: 转发通过.
d) 客户端只信任错误 CA: 握手失败.

口径: 本机回环, 证书与私钥只在 D:\RDC-Build; 只证明 TLS 1.3 接入等价, 不代表真实链路.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from rdc.crypto.identity import DeviceIdentity
from rdc.relay.server import recv_frame, send_frame
from rdc.relay.tls import build_client_context
from rdc.signaling.envelope import build_envelope

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
CERT_FILES = ("ca.crt", "server.crt", "server.key", "client.crt", "client.key", "wrong-ca.crt")


# ---------------------------------------------------------------------------- 证书与环境


def _build_root() -> Path:
    return Path(os.environ.get("RDC_BUILD_ROOT") or r"D:\RDC-Build")


def _cert_dir() -> Path:
    explicit = os.environ.get("RDC_TLS_CERT_DIR")
    if explicit:
        return Path(explicit)
    return _build_root() / "certs" / "relay"


def _require_certs() -> Path:
    directory = _cert_dir()
    missing = [name for name in CERT_FILES if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(
            f"证书缺失: {directory} 下缺少 {', '.join(missing)}; 请先按 deploy/relay/README.md 生成"
        )
    return directory


def _child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    env["RDC_TLS_CERT_DIR"] = str(_cert_dir())
    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------- 帧与载荷


def _payload(index: int, size: int) -> bytes:
    block = hashlib.blake2b(index.to_bytes(4, "big"), digest_size=32).digest()
    return (block * (size // len(block) + 1))[:size]


def _send_raw(connection: ssl.SSLSocket, payload: bytes) -> None:
    connection.sendall(struct.pack(">I", len(payload)) + payload)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"中继端口 {port} 未在 {timeout} 秒内就绪")


def _health(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _connect_tls(port: int, context: ssl.SSLContext, timeout: float = 10.0) -> ssl.SSLSocket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    raw.settimeout(timeout)
    try:
        return context.wrap_socket(raw, server_hostname="localhost")
    except Exception:
        raw.close()
        raise


def _describe(connection: ssl.SSLSocket) -> dict[str, Any]:
    version = connection.version()
    cipher = connection.cipher()
    return {
        "tls_version": version,
        "cipher_suite": cipher[0] if cipher else None,
        "cipher_protocol": cipher[1] if cipher else None,
    }


# ---------------------------------------------------------------------------- 配置与中继进程


def _config_text(port: int, health_port: int, *, require_client_cert: bool, tls_enabled: bool = True) -> str:
    directory = _cert_dir()
    lines = [
        "[server]",
        "bind = 127.0.0.1",
        f"port = {port}",
        "health_bind = 127.0.0.1",
        f"health_port = {health_port}",
        "",
        "[relay]",
        f"endpoint = relay://127.0.0.1:{port}",
        "egress_mbps = 3",
        "overhead_ratio = 0.08",
        "max_sessions = 2",
        "degrade_profile = L0",
        "",
        "[tls]",
        f"tls_enabled = {'true' if tls_enabled else 'false'}",
        f"cert_file = {directory / 'server.crt'}",
        f"key_file = {directory / 'server.key'}",
        f"ca_file = {directory / 'ca.crt'}",
        f"require_client_cert = {'true' if require_client_cert else 'false'}",
        "",
    ]
    return "\n".join(lines)


def _start_relay(config_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "rdc.relay", "--config", str(config_path)],
        cwd=str(PROJECT_ROOT),
        env=_child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stop_relay(process: subprocess.Popen[str]) -> str:
    process.terminate()
    try:
        _, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate(timeout=5)
    return (stderr or "").strip()


def _run_server_scenario(
    name: str,
    *,
    require_client_cert: bool,
    action,
) -> dict[str, Any]:
    """为单个场景起中继子进程, 执行动作后回收进程."""
    temp_dir = _build_root() / "temp" / "relay-tls"
    temp_dir.mkdir(parents=True, exist_ok=True)
    port, health_port = _free_port(), _free_port()
    config_path = temp_dir / f"{name}.conf"
    config_path.write_text(
        _config_text(port, health_port, require_client_cert=require_client_cert), encoding="utf-8"
    )
    relay = _start_relay(config_path)
    try:
        _wait_port(port)
        health = _health(health_port)
        return action(port, health)
    finally:
        stderr = _stop_relay(relay)
        if stderr:
            # 只保留末行错误摘要, 不打印证书或私钥内容
            print(f"[{name}] 中继退出信息: {stderr.splitlines()[-1]}", file=sys.stderr)


# ---------------------------------------------------------------------------- 信令取令牌


def _request_relay_token(
    port: int, session_id: str, requested_kbps: int, context: ssl.SSLContext
) -> tuple[dict[str, Any], dict[str, Any]]:
    """走真实信令流程(全 TLS): register → challenge → auth → relay_allocate."""
    identity = DeviceIdentity.generate()
    connection = _connect_tls(port, context)
    try:
        send_frame(connection, build_envelope(
            identity,
            "register",
            {
                "device_id": identity.device_id,
                "public_key": identity.public_key_b64,
                "capabilities": ["relay"],
                "protocol_version": 1,
                "display_name": "tls-check",
            },
        ))
        challenge = json.loads(recv_frame(connection).decode("utf-8"))
        if challenge.get("type") != "challenge":
            raise RuntimeError(f"未收到 challenge: {challenge.get('type')}")

        nonce = challenge["payload"]["nonce"]
        send_frame(connection, build_envelope(
            identity,
            "auth",
            {
                "device_id": identity.device_id,
                "signature": identity.sign({"device_id": identity.device_id, "nonce": nonce}),
            },
        ))
        auth_ok = json.loads(recv_frame(connection).decode("utf-8"))
        if auth_ok.get("type") != "auth_ok":
            raise RuntimeError(f"认证失败: {auth_ok.get('payload')}")

        # 契约缺口沿用 202611-001: v1 无 from_device_id 字段, 服务端靠它定位公钥验签
        send_frame(connection, build_envelope(
            identity,
            "relay_allocate",
            {
                "session_id": session_id,
                "requested_kbps": requested_kbps,
                "est_duration_s": 60,
                "from_device_id": identity.device_id,
            },
            session_id=session_id,
        ))
        allocated = json.loads(recv_frame(connection).decode("utf-8"))
        if allocated.get("type") != "relay_allocated":
            raise RuntimeError(f"预算申请失败: {allocated.get('payload')}")
        return allocated["payload"], _describe(connection)
    finally:
        connection.close()


# ---------------------------------------------------------------------------- 客户端子进程


def run_peer(args: argparse.Namespace) -> int:
    """子进程入口: 一个角色以 TLS 连中继收发数据面帧.

    令牌与证书开关通过环境变量传递, 避免私钥路径或令牌出现在命令行.
    """
    cert_dir = _cert_dir()
    use_client_cert = os.environ.get("RDC_PEER_CLIENT_CERT") == "1"
    ca_name = os.environ.get("RDC_PEER_CA_NAME", "ca.crt")
    context = build_client_context(
        ca_file=str(cert_dir / ca_name),
        cert_file=str(cert_dir / "client.crt") if use_client_cert else "",
        key_file=str(cert_dir / "client.key") if use_client_cert else "",
    )
    try:
        connection = _connect_tls(args.port, context)
    except (ssl.SSLError, OSError) as error:
        _write_result(args.result, {"ok": False, "stage": "handshake", "error": str(error)})
        return 2
    try:
        description = _describe(connection)
        send_frame(connection, {
            "plane": "data",
            "v": 1,
            "type": "relay_hello",
            "relay_token": os.environ.get("RDC_PEER_TOKEN", ""),
            "session_id": args.session_id,
            "role": args.role,
        })
        ready = json.loads(recv_frame(connection).decode("utf-8"))
        if ready.get("type") != "relay_ready":
            _write_result(args.result, {"ok": False, "stage": "handshake", "detail": ready, **description})
            return 2
        if args.role == "publisher":
            result = _publish(connection, args)
        else:
            result = _subscribe(connection, args)
        result.update(description)
    finally:
        connection.close()
    _write_result(args.result, result)
    return 0 if result.get("ok") else 3


def _publish(connection: ssl.SSLSocket, args: argparse.Namespace) -> dict[str, Any]:
    for index in range(args.count):
        _send_raw(connection, _payload(index, args.size))
    # 留时间让中继把最后几帧转发完再断开
    time.sleep(0.3)
    return {"ok": True, "role": "publisher", "sent_frames": args.count}


def _subscribe(connection: ssl.SSLSocket, args: argparse.Namespace) -> dict[str, Any]:
    received = 0
    mismatched = 0
    while received < args.count:
        try:
            frame = recv_frame(connection)
        except (socket.timeout, TimeoutError, OSError, ValueError):
            break
        if frame is None:
            break
        if frame != _payload(received, args.size):
            mismatched += 1
        received += 1
    return {
        "ok": mismatched == 0,
        "role": "subscriber",
        "expected_frames": args.count,
        "received_frames": received,
        "mismatched_frames": mismatched,
    }


def _write_result(path: str, payload: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------- 场景


def scenario_unidirectional(cert_dir: Path, temp_dir: Path) -> dict[str, Any]:
    """a) TLS 1.3 单向 + 32 帧密文逐字节一致."""
    context = build_client_context(ca_file=str(cert_dir / "ca.crt"))

    def action(port: int, health: dict[str, Any]) -> dict[str, Any]:
        session_id = f"tls-a-{int(time.time() * 1000)}"
        publisher_grant, publisher_tls = _request_relay_token(port, session_id, 1380, context)
        subscriber_grant, subscriber_tls = _request_relay_token(port, session_id, 1380, context)
        subscriber_result = temp_dir / "a-subscriber.json"
        publisher_result = temp_dir / "a-publisher.json"
        subscriber = _spawn_peer("subscriber", port, session_id, subscriber_grant["relay_token"], 32, 2048, subscriber_result, client_cert=False)
        time.sleep(0.3)
        publisher = _spawn_peer("publisher", port, session_id, publisher_grant["relay_token"], 32, 2048, publisher_result, client_cert=False)
        _collect_peer(publisher, "publisher")
        _collect_peer(subscriber, "subscriber")
        subscriber_data = _read_result(subscriber_result)
        publisher_data = _read_result(publisher_result)
        ok = (
            subscriber_data.get("received_frames") == 32
            and subscriber_data.get("mismatched_frames") == 0
            and publisher_data.get("sent_frames") == 32
        )
        return {
            "ok": ok,
            "reason": "32 帧密文逐字节一致" if ok else "转发帧数或内容不一致",
            **subscriber_tls,
            "detail": {
                "publisher": publisher_data,
                "subscriber": subscriber_data,
                "publisher_tls": publisher_tls,
                "health_tls": health.get("tls"),
            },
        }

    return _run_server_scenario("a-unidirectional", require_client_cert=False, action=action)


def scenario_client_cert_missing(cert_dir: Path) -> dict[str, Any]:
    """b) 双向校验但客户端无证书: 必须被拒."""
    context = build_client_context(ca_file=str(cert_dir / "ca.crt"))

    def action(port: int, health: dict[str, Any]) -> dict[str, Any]:
        tls_info: dict[str, Any] = {"tls_version": None, "cipher_suite": None}
        rejected = False
        error_text = ""
        try:
            connection = _connect_tls(port, context)
            try:
                tls_info = _describe(connection)
                send_frame(connection, {
                    "plane": "data",
                    "v": 1,
                    "type": "relay_hello",
                    "relay_token": "invalid-for-rejection-test",
                    "session_id": "tls-b-reject",
                    "role": "publisher",
                })
                try:
                    frame = recv_frame(connection)
                except (OSError, ValueError):
                    frame = None
                rejected = frame is None
                if frame is not None:
                    error_text = f"服务端未拒绝, 收到帧: {frame[:120]!r}"
            finally:
                connection.close()
        except ssl.SSLError as error:
            rejected = True
            error_text = f"握手失败: {error.reason or error}"
        except OSError as error:
            rejected = True
            error_text = f"连接失败: {error}"
        return {
            "ok": rejected,
            "reason": "无客户端证书被拒绝: " + (error_text or "首帧前连接关闭") if rejected else error_text,
            **tls_info,
            "detail": {"health_tls": health.get("tls")},
        }

    return _run_server_scenario("b-client-cert-missing", require_client_cert=True, action=action)


def scenario_client_cert_present(cert_dir: Path, temp_dir: Path) -> dict[str, Any]:
    """c) 双向校验且客户端证书正确: 转发通过."""
    context = build_client_context(
        ca_file=str(cert_dir / "ca.crt"),
        cert_file=str(cert_dir / "client.crt"),
        key_file=str(cert_dir / "client.key"),
    )

    def action(port: int, health: dict[str, Any]) -> dict[str, Any]:
        session_id = f"tls-c-{int(time.time() * 1000)}"
        publisher_grant, publisher_tls = _request_relay_token(port, session_id, 1380, context)
        subscriber_grant, subscriber_tls = _request_relay_token(port, session_id, 1380, context)
        subscriber_result = temp_dir / "c-subscriber.json"
        publisher_result = temp_dir / "c-publisher.json"
        subscriber = _spawn_peer("subscriber", port, session_id, subscriber_grant["relay_token"], 8, 1024, subscriber_result, client_cert=True)
        time.sleep(0.3)
        publisher = _spawn_peer("publisher", port, session_id, publisher_grant["relay_token"], 8, 1024, publisher_result, client_cert=True)
        _collect_peer(publisher, "publisher")
        _collect_peer(subscriber, "subscriber")
        subscriber_data = _read_result(subscriber_result)
        publisher_data = _read_result(publisher_result)
        ok = (
            subscriber_data.get("received_frames") == 8
            and subscriber_data.get("mismatched_frames") == 0
            and publisher_data.get("sent_frames") == 8
            and subscriber_data.get("tls_version") == "TLSv1.3"
        )
        return {
            "ok": ok,
            "reason": "正确客户端证书通过且 8 帧密文一致" if ok else "双向校验通过但转发结果不符",
            **subscriber_tls,
            "detail": {
                "publisher": publisher_data,
                "subscriber": subscriber_data,
                "publisher_tls": publisher_tls,
                "health_tls": health.get("tls"),
            },
        }

    return _run_server_scenario("c-client-cert-present", require_client_cert=True, action=action)


def scenario_wrong_ca(cert_dir: Path) -> dict[str, Any]:
    """d) 客户端只信任错误 CA: 必须握手失败."""
    context = build_client_context(ca_file=str(cert_dir / "wrong-ca.crt"))

    def action(port: int, health: dict[str, Any]) -> dict[str, Any]:
        error_text = ""
        failed = False
        try:
            connection = _connect_tls(port, context)
            try:
                send_frame(connection, {"plane": "data", "v": 1, "type": "relay_hello"})
            finally:
                connection.close()
            error_text = "错误 CA 下握手仍成功"
        except ssl.SSLCertVerificationError as error:
            failed = True
            error_text = f"证书校验失败: {error.verify_message}"
        except ssl.SSLError as error:
            failed = True
            error_text = f"握手失败: {error.reason or error}"
        except OSError as error:
            failed = True
            error_text = f"连接失败: {error}"
        return {
            "ok": failed,
            "reason": error_text,
            "tls_version": None,
            "cipher_suite": None,
            "detail": {"health_tls": health.get("tls")},
        }

    return _run_server_scenario("d-wrong-ca", require_client_cert=False, action=action)


# ---------------------------------------------------------------------------- 子进程编排


def _spawn_peer(
    role: str,
    port: int,
    session_id: str,
    token: str,
    count: int,
    size: int,
    result_path: Path,
    *,
    client_cert: bool,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable, "-m", "poc.tools.run_relay_tls_check",
            "--role", role,
            "--port", str(port),
            "--session-id", session_id,
            "--count", str(count),
            "--size", str(size),
            "--result", str(result_path),
        ],
        cwd=str(PROJECT_ROOT),
        env=_child_env({
            "RDC_PEER_TOKEN": token,
            "RDC_PEER_CLIENT_CERT": "1" if client_cert else "0",
        }),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect_peer(process: subprocess.Popen[str], label: str) -> None:
    _, stderr = process.communicate(timeout=60)
    if process.returncode != 0:
        raise RuntimeError(f"{label} 子进程失败 {process.returncode}: {stderr.strip()}")


# ---------------------------------------------------------------------------- 入口


def run() -> dict[str, Any]:
    cert_dir = _require_certs()
    temp_dir = _build_root() / "temp" / "relay-tls"
    evidence_dir = _build_root() / "evidence" / "poc" / "relay"
    temp_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "work_item": "relay-tls",
        "topology": "本机回环: 中继为独立子进程, 发布端与订阅端各为独立子进程",
        "scope": "只验证 TLS 1.3 接入与握手/转发等价性; 未跨公网验证, 不是画质通道验收",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cert_dir": str(cert_dir),
        "scenarios": {},
    }
    scenarios = (
        ("tls13_unidirectional_forward", lambda: scenario_unidirectional(cert_dir, temp_dir)),
        ("client_cert_missing_rejected", lambda: scenario_client_cert_missing(cert_dir)),
        ("client_cert_present_allowed", lambda: scenario_client_cert_present(cert_dir, temp_dir)),
        ("wrong_ca_handshake_failed", lambda: scenario_wrong_ca(cert_dir)),
    )
    for name, runner in scenarios:
        try:
            result["scenarios"][name] = runner()
        except Exception as error:  # 场景失败也要落证据
            result["scenarios"][name] = {
                "ok": False,
                "reason": f"场景执行异常: {error}",
                "tls_version": None,
                "cipher_suite": None,
            }
    result["all_passed"] = all(item["ok"] for item in result["scenarios"].values())
    output = evidence_dir / "tls-check.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中继 TLS 1.3 本地等价验证")
    parser.add_argument("--role", choices=("publisher", "subscriber"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--session-id")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--result")
    args = parser.parse_args(argv)

    if args.role:
        return run_peer(args)

    result = run()
    for name, scenario in result["scenarios"].items():
        print(
            f"{'通过' if scenario['ok'] else '失败'}  {name}  "
            f"{scenario.get('tls_version')} / {scenario.get('cipher_suite')}  {scenario.get('reason')}"
        )
    print(f"证据: {_build_root() / 'evidence' / 'poc' / 'relay' / 'tls-check.json'}")
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
