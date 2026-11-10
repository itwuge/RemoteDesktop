"""演示链路安全回归: 连接码挑战应答、握手会话绑定与 AEAD 重放拒绝.

三组用例分别对应 A2/A3/A4:
- A2: 连接码不再以可逆 Base64 明文出现在信令里, 连续错误会退避并作废换码;
- A3: 握手把期望对端 device_id 与 session_id 纳入校验与 HKDF 上下文;
- A4: 同一密文重复解密、落后窗口的计数器都必须失败.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from typing import Any

import pytest

from e2e_common import (
    ACCESS_PROOF_ALG,
    AccessCodeGuard,
    AeadSession,
    E2EFailure,
    EphemeralKeyPair,
    build_access_proof,
    controller_handshake,
    derive_aead_session,
    host_handshake,
    recv_json,
    send_json,
    verify_access_proof,
)
from rdc.crypto.identity import DeviceIdentity, b64encode

SESSION_ID = "rdc-session-0001"
OTHER_SESSION_ID = "rdc-session-0002"
ACCESS_CODE = "246810"


# ================================ A2 连接码挑战应答 ================================


def test_access_proof_does_not_carry_plaintext_code() -> None:
    """挑战应答只带随机数与 HMAC 摘要, 连接码本身与可逆 Base64 都不出现."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    proof = build_access_proof(ACCESS_CODE, SESSION_ID, controller.device_id, host.device_id)

    assert set(proof) == {"alg", "nonce", "response"}
    assert proof["alg"] == ACCESS_PROOF_ALG
    assert base64.b64decode(proof["nonce"]) != ACCESS_CODE.encode("utf-8")
    assert proof["nonce"] != b64encode(ACCESS_CODE.encode("utf-8"))
    assert proof["response"] != b64encode(ACCESS_CODE.encode("utf-8"))
    assert len(proof["response"]) == 64

    serialized = json.dumps(proof, ensure_ascii=False)
    assert b64encode(ACCESS_CODE.encode("utf-8")) not in serialized
    assert verify_access_proof(ACCESS_CODE, SESSION_ID, controller.device_id, host.device_id, proof) is True


def test_access_proof_rejects_wrong_code_and_wrong_binding() -> None:
    """连接码错误、会话号或双方设备标识被替换都必须校验失败."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    proof = build_access_proof(ACCESS_CODE, SESSION_ID, controller.device_id, host.device_id)

    assert verify_access_proof("111111", SESSION_ID, controller.device_id, host.device_id, proof) is False
    assert verify_access_proof(ACCESS_CODE, OTHER_SESSION_ID, controller.device_id, host.device_id, proof) is False
    assert verify_access_proof(ACCESS_CODE, SESSION_ID, host.device_id, controller.device_id, proof) is False
    assert verify_access_proof(ACCESS_CODE, SESSION_ID, controller.device_id, host.device_id, "MjQ2ODEw") is False


def test_access_guard_backs_off_then_rotates_code() -> None:
    """连续错误按次数退避, 第 5 次作废旧码并换新, 旧码与重放随机数均不可再用."""
    now = [1000.0]
    rotations: list[str] = []

    def rotate() -> str:
        rotations.append("rotated")
        return "777777"

    guard = AccessCodeGuard(rotate, clock=lambda: now[0])
    guard.set_code(ACCESS_CODE)
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()

    for attempt in range(1, 5):
        bad = build_access_proof("000000", SESSION_ID, controller.device_id, host.device_id)
        accepted, reason = guard.accept(bad, SESSION_ID, controller.device_id, host.device_id)
        assert accepted is False
        assert f"({attempt}/5)" in reason
        assert guard.lock_remaining() > 0
        now[0] += 60.0

    bad = build_access_proof("000000", SESSION_ID, controller.device_id, host.device_id)
    accepted, reason = guard.accept(bad, SESSION_ID, controller.device_id, host.device_id)
    assert accepted is False
    assert "已作废并更换新连接码" in reason
    assert rotations == ["rotated"]
    assert guard.code == "777777"

    old_code = build_access_proof(ACCESS_CODE, SESSION_ID, controller.device_id, host.device_id)
    assert guard.accept(old_code, SESSION_ID, controller.device_id, host.device_id)[0] is False
    now[0] += 60.0

    fresh = build_access_proof("777777", SESSION_ID, controller.device_id, host.device_id)
    assert guard.accept(fresh, SESSION_ID, controller.device_id, host.device_id)[0] is True
    assert guard.accept(fresh, SESSION_ID, controller.device_id, host.device_id)[0] is False


def test_trusted_device_may_skip_access_code_only() -> None:
    """同账号受信设备可不带挑战应答; 未受信设备必须回退 6 位连接码."""
    from rdc.accounts.service import AccountService
    from rdc.commands import accounts as account_commands

    guard = AccessCodeGuard(lambda: "777777")
    guard.set_code(ACCESS_CODE)
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    account_service = AccountService()
    account_service.register_device(controller.device_id, "控制端")
    assert account_service.register_account("peer@example.com", "secret-1").ok
    assert account_service.login_account("peer@example.com", "secret-1").ok
    assert account_service.bind_device(controller.device_id, "123456").ok
    assert account_service.set_trust_policy(controller.device_id, False).ok
    account_commands.configure(account_service)
    try:
        accepted, reason = guard.accept(None, SESSION_ID, controller.device_id, host.device_id)
        assert accepted is True
        assert reason == ""
    finally:
        account_commands.configure(AccountService())

    accepted, reason = guard.accept(None, SESSION_ID, controller.device_id, host.device_id)
    assert accepted is False
    assert reason


# ================================ A3 握手会话绑定 ================================


def _handshake_pair(
    controller_identity: DeviceIdentity,
    host_identity: DeviceIdentity,
    *,
    controller_session: str,
    host_session: str,
    controller_peer: str,
    host_peer: str,
) -> dict[str, Any]:
    """在线程里跑被控端握手, 主线程跑控制端握手, 收集双方结果与异常."""
    client_socket, host_socket = socket.socketpair()
    results: dict[str, Any] = {}

    def host_worker() -> None:
        try:
            results["host"] = host_handshake(host_socket, host_identity, host_session, host_peer)
        except BaseException as error:  # 测试线程把异常原样带回主线程
            results["host_error"] = error
        finally:
            host_socket.close()

    thread = threading.Thread(target=host_worker)
    thread.start()
    try:
        results["controller"] = controller_handshake(
            client_socket, controller_identity, controller_session, controller_peer
        )
    except BaseException as error:  # 测试线程把异常原样带回主线程
        results["controller_error"] = error
    finally:
        client_socket.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    return results


def _assert_handshake_failed(results: dict[str, Any]) -> None:
    """握手必须失败, 且不允许任何一端拿到可用会话."""
    assert "controller" not in results
    assert "host" not in results
    errors = [results.get("controller_error"), results.get("host_error")]
    assert any(isinstance(item, E2EFailure) for item in errors)


def test_handshake_binds_expected_peer_and_session() -> None:
    """两端期望的 device_id 与 session_id 一致时握手成功, 密文可互通."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    results = _handshake_pair(
        controller,
        host,
        controller_session=SESSION_ID,
        host_session=SESSION_ID,
        controller_peer=host.device_id,
        host_peer=controller.device_id,
    )
    assert isinstance(results["controller"], AeadSession)
    assert isinstance(results["host"], AeadSession)
    assert results["host"].decrypt(results["controller"].encrypt(b"ping")) == b"ping"


def test_handshake_rejects_unexpected_host_device() -> None:
    """控制端期望连接 A, 实际应答的是 B, 即使签名有效也必须失败."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    impostor = DeviceIdentity.generate()
    results = _handshake_pair(
        controller,
        host,
        controller_session=SESSION_ID,
        host_session=SESSION_ID,
        controller_peer=impostor.device_id,
        host_peer=controller.device_id,
    )
    _assert_handshake_failed(results)


def test_handshake_rejects_session_id_mismatch() -> None:
    """两端会话号不一致时握手失败, 防止把会话密文挪到另一会话复用."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    results = _handshake_pair(
        controller,
        host,
        controller_session=SESSION_ID,
        host_session=OTHER_SESSION_ID,
        controller_peer=host.device_id,
        host_peer=controller.device_id,
    )
    _assert_handshake_failed(results)


def test_derived_keys_bind_session_context() -> None:
    """共享密钥与 salt 相同但会话上下文不同, 派生密钥必须不同."""
    shared = b"\x11" * 32
    salt = b"\x22" * 16
    first = derive_aead_session(shared, salt, True, context=b"hs2|session-a|controller|host")
    second = derive_aead_session(shared, salt, True, context=b"hs2|session-b|controller|host")
    assert first.send_key != second.send_key
    assert first.recv_key != second.recv_key


def test_host_rejects_legacy_handshake_without_binding() -> None:
    """不带 hs/session_id/peer_device_id 的旧握手默认被拒绝."""
    controller = DeviceIdentity.generate()
    host = DeviceIdentity.generate()
    client_socket, host_socket = socket.socketpair()
    errors: list[BaseException] = []

    def host_worker() -> None:
        try:
            host_handshake(host_socket, host, SESSION_ID, controller.device_id)
        except BaseException as error:  # 测试线程把异常原样带回主线程
            errors.append(error)
        finally:
            host_socket.close()

    thread = threading.Thread(target=host_worker)
    thread.start()
    pair = EphemeralKeyPair.generate()
    legacy: dict[str, Any] = {
        "kind": "e2e_hello",
        "v": 1,
        "x25519": b64encode(pair.public_bytes),
        "salt": b64encode(b"\x33" * 16),
        "device_id": controller.device_id,
        "public_key": controller.public_key_b64,
    }
    legacy["sig"] = controller.sign(legacy)
    send_json(client_socket, legacy)
    assert recv_json(client_socket) is None
    client_socket.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], E2EFailure)


# ================================ A4 AEAD 重放 ================================


def _session_pair() -> tuple[AeadSession, AeadSession]:
    """发送端与接收端使用互补密钥."""
    return AeadSession(b"a" * 32, b"b" * 32), AeadSession(b"b" * 32, b"a" * 32)


def test_same_ciphertext_decrypts_only_once() -> None:
    """同一密文第二次解密必须失败."""
    sender, receiver = _session_pair()
    ciphertext = sender.encrypt(b"hello-rdc")
    assert receiver.decrypt(ciphertext) == b"hello-rdc"
    with pytest.raises(ValueError):
        receiver.decrypt(ciphertext)


def test_counter_out_of_window_breaks_channel() -> None:
    """计数器落后接收窗口视为越界重放, 必须拒绝."""
    sender, receiver = _session_pair()
    frames = [sender.encrypt(b"frame-%d" % index) for index in range(80)]
    assert receiver.decrypt(frames[79]) == b"frame-79"
    with pytest.raises(ValueError):
        receiver.decrypt(frames[0])


def test_reordered_frames_within_window_are_accepted_once() -> None:
    """窗口内乱序可容忍, 但每个计数器只接受一次, 适配 UDP 传输."""
    sender, receiver = _session_pair()
    frames = [sender.encrypt(b"frame-%d" % index) for index in range(3)]
    assert receiver.decrypt(frames[2]) == b"frame-2"
    assert receiver.decrypt(frames[0]) == b"frame-0"
    assert receiver.decrypt(frames[1]) == b"frame-1"
    with pytest.raises(ValueError):
        receiver.decrypt(frames[0])
