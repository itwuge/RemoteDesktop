# ==========================================================================================
# 文件        : run_p0_02_smoke.py
# 所属目录    : poc/tools/
# 文件用途    : 第 0 批 P0-02 工作包执行器, 跑通信令注册认证、offer/answer 与 UDP 直连密文收发
# 架构定位    : PoC 测试装置, 串联 signaling 与 transport, 不进入发布路径
# 核心功能    : 启动本机 WSS 信令; 两设备注册认证; 候选交换; 三向握手; AEAD 收发与篡改检测
# 职责边界    : 只验证信令与直连闭环; 不采集屏幕, 不编码, 不测延迟指标
# 运行环境    : Python 3.12; 单机双进程角色模拟, 用于无第二台机器时的功能闭环
# 依赖关系    : 引用 signaling/signaling.py、signaling/tls_cert.py 与 transport/udp_direct.py
# ==========================================================================================

"""P0-02 信令与直连冒烟执行器."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from poc.common.crypto import DeviceIdentity
from poc.signaling.signaling import SignalingClient, SignalingServer
from poc.signaling.tls_cert import ensure_self_signed_cert
from poc.transport.udp_direct import DirectTransport

MARKER = b"RDC-POC-PLAINTEXT-MARKER-20261110"


def _build_root() -> Path:
    root = os.environ.get("RDC_BUILD_ROOT")
    if not root:
        raise RuntimeError("未设置 RDC_BUILD_ROOT, 不得写入系统盘或项目目录")
    return Path(root)


def run() -> dict[str, Any]:
    """执行信令与直连闭环冒烟.

    :return: 结果摘要
    :rtype: dict
    """
    root = _build_root()
    temp_dir = root / "temp" / "p0-02"
    evidence_dir = root / "evidence" / "poc" / "security"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = ensure_self_signed_cert(temp_dir / "signaling.crt", temp_dir / "signaling.key")

    server = SignalingServer(cert_path, key_path)
    port = server.start("127.0.0.1", 0)
    url = f"wss://127.0.0.1:{port}"

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    host_identity = DeviceIdentity.create()
    controller_identity = DeviceIdentity.create()
    host_client = SignalingClient(host_identity)
    controller_client = SignalingClient(controller_identity)
    host_transport = DirectTransport(host_identity)
    controller_transport = DirectTransport(controller_identity)

    result: dict[str, Any] = {
        "work_package": "P0-02",
        "topology": "单机双角色, UDP 回环",
        "server_url": url,
        "host_device_id": host_identity.device_id,
        "controller_device_id": controller_identity.device_id,
        "checks": {},
        "timings_ms": {},
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    received: list[bytes] = []
    raw_datagrams: list[bytes] = []
    passive_error: list[str] = []

    try:
        host_client.connect(url, context)
        controller_client.connect(url, context)
        host_auth = host_client.register()
        controller_auth = controller_client.register()
        result["checks"]["host_auth_ok"] = host_auth.get("type") == "auth_ok"
        result["checks"]["controller_auth_ok"] = controller_auth.get("type") == "auth_ok"

        controller_address = controller_transport.bind("127.0.0.1", 0)
        session_id = str(uuid.uuid4())
        offer_started = time.perf_counter()
        controller_client.send(
            "offer",
            {
                "target_device_id": host_identity.device_id,
                "session_id": session_id,
                "candidates": [
                    {"kind": "host", "address": controller_address[0], "port": controller_address[1], "priority": 100}
                ],
                "media_caps": {"codec": "h264", "max_width": 1920, "max_height": 1080, "fps": 30},
                "access_proof": "",
            },
            session_id,
        )
        offer = host_client.wait_for({"offer"}, timeout=5.0)
        result["checks"]["offer_routed"] = offer.get("payload", {}).get("session_id") == session_id

        host_address = host_transport.bind("127.0.0.1", 0)
        host_client.send(
            "answer",
            {
                "target_device_id": controller_identity.device_id,
                "session_id": session_id,
                "chosen_candidate": {"kind": "host", "address": host_address[0], "port": host_address[1]},
                "media_caps": {"codec": "h264", "max_width": 1920, "max_height": 1080, "fps": 30},
            },
            session_id,
        )

        def passive_worker() -> None:
            try:
                host_transport.handshake_passive(timeout=5.0)
                while len(received) < 10:
                    plaintext, raw = host_transport.recv(timeout=3.0)
                    raw_datagrams.append(raw)
                    received.append(plaintext)
            except Exception as error:
                passive_error.append(f"{type(error).__name__}: {error}")

        passive_thread = threading.Thread(target=passive_worker, daemon=True)
        passive_thread.start()

        answer = controller_client.wait_for({"answer"}, timeout=5.0)
        candidate = answer["payload"]["chosen_candidate"]
        handshake_started = time.perf_counter()
        controller_transport.handshake_active((candidate["address"], int(candidate["port"])), timeout=5.0)
        result["timings_ms"]["direct_handshake"] = round((time.perf_counter() - handshake_started) * 1000.0, 3)
        result["checks"]["direct_handshake_ok"] = True
        result["timings_ms"]["offer_to_answer"] = round((time.perf_counter() - offer_started) * 1000.0, 3)

        for index in range(10):
            controller_transport.send(MARKER + b"-%02d" % index)

        passive_thread.join(timeout=10.0)
        result["checks"]["decrypted_packets"] = len(received)
        result["checks"]["plaintext_match"] = len(received) == 10 and all(
            item.startswith(MARKER) for item in received
        )

        marker_leaked = any(MARKER in datagram for datagram in raw_datagrams)
        result["checks"]["plaintext_not_in_datagram"] = not marker_leaked
        result["security"] = {
            "raw_datagram_count": len(raw_datagrams),
            "raw_datagram_bytes_first": len(raw_datagrams[0]) if raw_datagrams else 0,
            "raw_datagram_hexdump_first": raw_datagrams[0][:96].hex() if raw_datagrams else "",
            "plaintext_marker_hex": MARKER.hex(),
        }

        tampered_rejected = False
        if raw_datagrams:
            tampered = bytearray(raw_datagrams[0])
            tampered[-1] ^= 0x01
            try:
                controller_transport.session.decrypt(bytes(tampered[5:]))
            except Exception:
                tampered_rejected = True
        result["checks"]["tamper_rejected"] = tampered_rejected
        result["checks"]["passive_worker_error"] = "; ".join(passive_error)
        result["signaling_flow"] = server.snapshot()
    finally:
        controller_transport.close()
        host_transport.close()
        controller_client.close()
        host_client.close()
        server.stop()

    result["checks"]["all_passed"] = all(
        value is True or value == 10
        for key, value in result["checks"].items()
        if key not in {"decrypted_packets", "passive_worker_error"}
    ) and result["checks"]["decrypted_packets"] == 10 and not result["checks"]["passive_worker_error"]

    output = evidence_dir / "p0-02_result.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["evidence"] = str(output)
    return result


def main() -> None:
    """命令行入口.

    :return: 无
    :rtype: None
    """
    print(json.dumps(run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
