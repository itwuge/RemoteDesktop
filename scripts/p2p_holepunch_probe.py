# ==========================================================================================
# 文件        : p2p_holepunch_probe.py
# 所属目录    : scripts/
# 文件用途    : P2P UDP 打洞可行性验证装置: STUN 映射探测、NAT 过滤行为测试、双向打洞、直连吞吐测量
# 架构定位    : 验收 / 可行性验证脚本, 不属于产品代码, 不修改 src 与协议契约
# 核心功能    : RFC 5389 Binding 请求与 XOR-MAPPED-ADDRESS 解析; 映射行为分类;
#               两阶段单向发包的过滤行为测试; 双向打洞与首包时延、RTT 统计; 打洞后链路吞吐
# 职责边界    : 只做收发与统计; 不改系统网络配置、不改防火墙、不重启任何设备;
#               打洞协调走 adb 文件交换(等价于生产环境的信令通道), 不写入任何凭据
# ==========================================================================================

"""P2P 打洞可行性验证装置.

用法:
    python scripts/p2p_holepunch_probe.py stun --work evidence/p2p/<时间戳>
    python scripts/p2p_holepunch_probe.py run --work evidence/p2p/<时间戳> --mode same-nat
    python scripts/p2p_holepunch_probe.py run --work evidence/p2p/<时间戳> --mode cross-nat
    python scripts/p2p_holepunch_probe.py run --work evidence/p2p/<时间戳> --mode lan

装置两端:
    Windows 侧: 本脚本 peer 子命令;
    Android 侧: scripts/p2p_android_peer 交叉编译出的 arm64 静态二进制, 经 adb 推送运行.

打洞包格式(32 字节): 魔数 RDP1 + 类型 + 序号 + 纳秒时间戳 + 补零.
吞吐包格式: 同样的 32 字节头部补零到 1200 字节, 类型为 3.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAGIC = b"RDP1"
KIND_PROBE = 1
KIND_ACK = 2
KIND_BULK = 3
STUN_MAGIC = 0x2112A442
BULK_PACKET_BYTES = 1200
DEFAULT_STUN = ("111.206.174.2:3478", "106.12.251.193:3478")
ADB = Path(r"D:\RDC-Build\tools\android\sdk\platform-tools\adb.exe")
PHONE_DIR = "/sdcard/rdc_p2p"
PHONE_BIN = "/data/local/tmp/p2p_peer"
LOCAL_PEER_BIN = Path(r"D:\RDC-Build\temp\p2p_peer_arm64")


def emit(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


# ================================ STUN ================================


def build_stun_request() -> tuple[bytes, bytes]:
    txid = os.urandom(12)
    return struct.pack(">HHI12s", 0x0001, 0, STUN_MAGIC, txid), txid


def parse_stun_response(data: bytes, txid: bytes = b"") -> str:
    if len(data) < 20:
        return ""
    msg_type, _length, _magic, got_txid = struct.unpack(">HHI12s", data[:20])
    if msg_type != 0x0101:
        return ""
    if txid and got_txid != txid:
        return ""
    offset = 20
    while offset + 4 <= len(data):
        attr_type, attr_len = struct.unpack(">HH", data[offset : offset + 4])
        if offset + 4 + attr_len > len(data):
            return ""
        value = data[offset + 4 : offset + 4 + attr_len]
        if attr_type == 0x0020 and attr_len >= 8 and value[1] == 0x01:
            port = struct.unpack(">H", value[2:4])[0] ^ 0x2112
            raw = bytes(byte ^ mask for byte, mask in zip(value[4:8], struct.pack(">I", STUN_MAGIC), strict=True))
            return f"{socket.inet_ntoa(raw)}:{port}"
        if attr_type == 0x0001 and attr_len >= 8 and value[1] == 0x01:
            port = struct.unpack(">H", value[2:4])[0]
            return f"{socket.inet_ntoa(value[4:8])}:{port}"
        offset += 4 + attr_len + ((4 - attr_len % 4) % 4)
    return ""


def stun_query(sock: socket.socket, server: str, timeout_s: float = 2.5) -> dict[str, Any]:
    host, _, port_text = server.partition(":")
    try:
        info = socket.getaddrinfo(host, int(port_text), socket.AF_INET, socket.SOCK_DGRAM)
        target = info[0][4]
        started = time.perf_counter()
        request, txid = build_stun_request()
        sock.sendto(request, target)
        sock.settimeout(timeout_s)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            data, _peer = sock.recvfrom(2048)
            mapped = parse_stun_response(data, txid)
            if mapped:
                return {
                    "server": server,
                    "resolved": target[0],
                    "rtt_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "mapped": mapped,
                    "response_bytes": len(data),
                }
        return {"server": server, "error": "无响应"}
    except OSError as exc:
        return {"server": server, "error": f"{type(exc).__name__}: {exc}"}


def classify_mapping(results: list[dict[str, Any]]) -> str:
    mapped = {item["mapped"] for item in results if item.get("mapped")}
    if not mapped:
        return "unknown"
    if len(mapped) == 1:
        return "endpoint-independent"
    return "address-or-port-dependent"


def encode_packet(kind: int, seq: int, stamp_ns: int, size: int = 32) -> bytes:
    head = MAGIC + bytes([kind]) + struct.pack(">IQ", seq, stamp_ns)
    return head + b"\x00" * (size - len(head))


def decode_packet(data: bytes) -> tuple[int, int, int] | None:
    if len(data) < 17 or data[:4] != MAGIC:
        return None
    kind = data[4]
    seq, stamp_ns = struct.unpack(">IQ", data[5:17])
    return kind, seq, stamp_ns


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return round(ordered[index], 3)


# ================================ Windows 侧对端 ================================


def run_peer(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"role": args.role, "state": "starting"}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind_ip, args.local_port))
    local_port = sock.getsockname()[1]
    result["local_port"] = local_port
    try:
        mapped: list[str] = []
        stun_results: list[dict[str, Any]] = []
        if not args.skip_stun:
            for server in args.stun.split(","):
                item = stun_query(sock, server)
                stun_results.append(item)
                if item.get("mapped"):
                    mapped.append(item["mapped"])
        result["stun"] = stun_results
        result["mapped"] = mapped
        result["mapping_behavior"] = classify_mapping(stun_results)
        exchange_out = Path(args.exchange_out)
        exchange_out.write_text(
            json.dumps(
                {"role": args.role, "mapped": mapped, "port": local_port, "behavior": result["mapping_behavior"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        peer_addr = args.peer_addr or wait_peer_file(Path(args.exchange_in), args.wait)
        if not peer_addr:
            result["error"] = "未拿到对端映射地址"
            return finish(work, result, sock)
        result["peer_addr"] = peer_addr
        emit(f"[peer] 本端映射 {mapped or '无'} 对端 {peer_addr}")
        time.sleep(args.start_delay)

        phase = float(args.phase)
        phase_recv = 0
        phase_sent = 0
        if args.role == "windows":
            phase_recv = listen_only(sock, phase)
            phase_sent = send_probes(sock, peer_addr, phase)
        else:
            phase_sent = send_probes(sock, peer_addr, phase)
            phase_recv = listen_only(sock, phase)
        result["filter_phase_recv"] = phase_recv
        result["filter_phase_sent"] = phase_sent

        punch = punch_loop(sock, peer_addr, float(args.punch))
        result.update(punch)

        if args.bulk_kbps > 0 and punch["punch_recv"] > 0:
            if args.role == "windows":
                result["bulk"] = bulk_send(sock, peer_addr, args.bulk_kbps, float(args.bulk))
            else:
                result["bulk"] = bulk_receive(sock, float(args.bulk))
        return finish(work, result, sock)
    finally:
        sock.close()


def finish(work: Path, result: dict[str, Any], sock: socket.socket) -> int:
    samples = result.get("punch_rtt_samples")
    result["punch_rtt_samples"] = samples if isinstance(samples, int) else len(samples or [])
    result["state"] = "ok" if not result.get("error") else "failed"
    (work / "windows_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(
        json.dumps(
            {
                "role": "windows",
                "punch_recv": result.get("punch_recv"),
                "first_ms": result.get("punch_first_ms"),
                "rtt_p50": result.get("punch_rtt_p50_ms"),
            },
            ensure_ascii=False,
        )
    )
    return 0


def wait_peer_file(path: Path, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text and ":" in text:
                return text.splitlines()[0].strip()
        time.sleep(0.2)
    return ""


def listen_only(sock: socket.socket, duration: float) -> int:
    received = 0
    sock.settimeout(0.1)
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        try:
            data, _peer = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError:
            continue
        if decode_packet(data):
            received += 1
    return received


def send_probes(sock: socket.socket, peer: str, duration: float) -> int:
    host, _, port_text = peer.partition(":")
    target = (host, int(port_text))
    sent = 0
    seq = 0
    next_at = time.monotonic()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if time.monotonic() >= next_at:
            sock.sendto(encode_packet(KIND_PROBE, seq, time.perf_counter_ns()), target)
            seq += 1
            sent += 1
            next_at += 0.2
        time.sleep(0.02)
    return sent


def punch_loop(sock: socket.socket, peer: str, duration: float) -> dict[str, Any]:
    host, _, port_text = peer.partition(":")
    target = (host, int(port_text))
    seq = 0
    sent = 0
    received = 0
    acks = 0
    rtt: list[float] = []
    first_ms = 0.0
    started = time.monotonic()
    next_at = started
    deadline = started + duration
    sock.settimeout(0.1)
    while time.monotonic() < deadline:
        if time.monotonic() >= next_at:
            sock.sendto(encode_packet(KIND_PROBE, seq, time.perf_counter_ns()), target)
            seq += 1
            sent += 1
            next_at += 0.1
        try:
            data, from_peer = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError:
            continue
        decoded = decode_packet(data)
        if not decoded:
            continue
        kind, peer_seq, stamp_ns = decoded
        if first_ms == 0.0:
            first_ms = round((time.monotonic() - started) * 1000.0, 3)
        if kind == KIND_PROBE:
            received += 1
            sock.sendto(encode_packet(KIND_ACK, peer_seq, stamp_ns), from_peer)
        elif kind == KIND_ACK:
            acks += 1
            delta_ms = (time.perf_counter_ns() - stamp_ns) / 1_000_000.0
            if 0 <= delta_ms < 10_000:
                rtt.append(delta_ms)
    return {
        "punch_sent": sent,
        "punch_recv": received,
        "punch_acks": acks,
        "punch_first_ms": first_ms,
        "punch_rtt_p50_ms": percentile(rtt, 0.5),
        "punch_rtt_p95_ms": percentile(rtt, 0.95),
        "punch_rtt_max_ms": round(max(rtt), 3) if rtt else 0.0,
        "punch_rtt_samples": len(rtt),
    }


def bulk_send(sock: socket.socket, peer: str, kbps: int, duration: float) -> dict[str, Any]:
    host, _, port_text = peer.partition(":")
    target = (host, int(port_text))
    interval = BULK_PACKET_BYTES * 8 / (kbps * 1000.0)
    seq = 0
    sent_bytes = 0
    started = time.monotonic()
    next_at = started
    deadline = started + duration
    sock.setblocking(True)
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now < next_at:
            time.sleep(min(next_at - now, 0.002))
            continue
        sock.sendto(encode_packet(KIND_BULK, seq, time.perf_counter_ns(), BULK_PACKET_BYTES), target)
        seq += 1
        sent_bytes += BULK_PACKET_BYTES
        next_at += interval
        if next_at < now - 0.5:
            next_at = now
    elapsed = time.monotonic() - started
    return {
        "target_kbps": kbps,
        "sent_bytes": sent_bytes,
        "sent_packets": seq,
        "elapsed_s": round(elapsed, 3),
        "sent_kbps": round(sent_bytes * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
    }


def bulk_receive(sock: socket.socket, duration: float) -> dict[str, Any]:
    total_bytes = 0
    packets = 0
    started = 0.0
    sock.settimeout(0.5)
    deadline = time.monotonic() + duration + 2.0
    while time.monotonic() < deadline:
        try:
            data, _peer = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError:
            break
        decoded = decode_packet(data)
        if not decoded or decoded[0] != KIND_BULK:
            continue
        now = time.monotonic()
        if started == 0.0:
            started = now
        total_bytes += len(data)
        packets += 1
    elapsed = (time.monotonic() - started) if started else 0.0
    return {
        "recv_bytes": total_bytes,
        "recv_packets": packets,
        "elapsed_s": round(elapsed, 3),
        "recv_kbps": round(total_bytes * 8 / 1000.0 / elapsed, 1) if elapsed else 0.0,
    }


# ================================ NAT 类型探测 ================================


def run_stun(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", args.local_port))
    local_port = sock.getsockname()[1]
    rounds: list[dict[str, Any]] = []
    servers = args.stun.split(",")
    for index in range(max(1, args.repeat)):
        for server in servers:
            item = stun_query(sock, server)
            item["round"] = index + 1
            rounds.append(item)
        time.sleep(float(args.interval))
    sock.close()
    mapped = [item["mapped"] for item in rounds if item.get("mapped")]
    rtts = [float(item["rtt_ms"]) for item in rounds if item.get("rtt_ms") is not None]
    report = {
        "local_port": local_port,
        "repeat": max(1, args.repeat),
        "servers": servers,
        "rounds": rounds,
        "mapped": mapped,
        "unique_mapped": sorted(set(mapped)),
        "port_preserved": bool(mapped) and all(int(item.rsplit(":", 1)[1]) == local_port for item in mapped),
        "success_rounds": len(mapped),
        "rtt_min_ms": round(min(rtts), 3) if rtts else None,
        "rtt_p50_ms": percentile(rtts, 0.5),
        "rtt_p95_ms": percentile(rtts, 0.95),
        "rtt_max_ms": round(max(rtts), 3) if rtts else None,
        "mapping_behavior": classify_mapping(rounds),
    }
    (work / "stun.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ================================ 编排 ================================


def adb(*arguments: str, timeout: float = 30.0) -> str:
    completed = subprocess.run([str(ADB), *arguments], capture_output=True, text=True, timeout=timeout, check=False)
    return (completed.stdout or "") + (completed.stderr or "")


def adb_cat(path: str) -> str:
    return adb("shell", "cat", path).strip()


def firewall_state() -> str:
    completed = subprocess.run(
        ["netsh", "advfirewall", "show", "allprofiles", "state"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (completed.stdout or "").strip()


def run_orchestrator(args: argparse.Namespace) -> int:
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    mode = args.mode
    adb("shell", f"mkdir -p {PHONE_DIR}")
    adb("shell", f"rm -f {PHONE_DIR}/me.json {PHONE_DIR}/peer.txt {PHONE_DIR}/result.json")

    windows_exchange = work / "windows_me.json"
    phone_exchange = work / "phone_me.txt"
    for stale in (windows_exchange, phone_exchange):
        if stale.exists():
            stale.unlink()

    peer_common = [
        "--local-port",
        str(args.local_port),
        "--start-delay",
        str(args.start_delay),
        "--phase",
        str(args.phase),
        "--punch",
        str(args.punch),
        "--bulk",
        str(args.bulk),
    ]
    windows_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "peer",
        "--role",
        "windows",
        "--work",
        str(work),
        "--exchange-out",
        str(windows_exchange),
        "--exchange-in",
        str(phone_exchange),
        "--bulk-kbps",
        str(args.bulk_kbps),
        *peer_common,
    ]
    phone_cmd = [
        str(PHONE_BIN),
        "-role",
        "android",
        "-local-port",
        str(args.local_port),
        "-exchange-out",
        f"{PHONE_DIR}/me.json",
        "-exchange-in",
        f"{PHONE_DIR}/peer.txt",
        "-out",
        f"{PHONE_DIR}/result.json",
        "-start-delay",
        str(round(args.start_delay)),
        "-phase",
        str(round(args.phase)),
        "-punch",
        str(round(args.punch)),
        "-bulk",
        str(round(args.bulk)),
        "-bulk-kbps",
        str(args.bulk_kbps),
    ]
    if mode == "lan":
        windows_cmd += ["--skip-stun", "--peer-addr", args.phone_lan]
        phone_cmd += ["-bind-ip", args.phone_lan.split(":")[0], "-peer-addr", args.windows_lan]
    else:
        stun_servers = args.stun
        windows_cmd += ["--stun", stun_servers]
        phone_cmd += ["-stun", stun_servers]

    summary: dict[str, Any] = {
        "mode": mode,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "firewall": firewall_state(),
        "windows_cmd": windows_cmd,
        "phone_cmd": phone_cmd,
    }

    windows_proc = subprocess.Popen(windows_cmd, cwd=str(Path(__file__).resolve().parent.parent))
    phone_proc = subprocess.Popen([str(ADB), "shell", *phone_cmd])

    peer_addr_windows = ""
    peer_addr_phone = ""
    if mode != "lan":
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline and not (peer_addr_windows and peer_addr_phone):
            if not peer_addr_windows and windows_exchange.exists():
                data = json.loads(windows_exchange.read_text(encoding="utf-8") or "{}")
                mapped = data.get("mapped") or []
                peer_addr_windows = mapped[0] if mapped else ""
            if not peer_addr_phone:
                text = adb_cat(f"{PHONE_DIR}/me.json")
                if text.startswith("{"):
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = {}
                    mapped = data.get("mapped") or []
                    peer_addr_phone = mapped[0] if mapped else ""
            time.sleep(0.3)
        if peer_addr_windows and peer_addr_phone:
            adb("shell", f"echo {peer_addr_windows} > {PHONE_DIR}/peer.txt")
            phone_exchange.write_text(peer_addr_phone, encoding="utf-8")

    windows_proc.wait(timeout=args.wait + args.start_delay + args.phase * 2 + args.punch + args.bulk + 30)
    phone_proc.wait(timeout=60)

    phone_result_text = adb_cat(f"{PHONE_DIR}/result.json")
    try:
        phone_result = json.loads(phone_result_text)
    except json.JSONDecodeError:
        phone_result = {"error": phone_result_text[:400]}
    try:
        windows_result = json.loads((work / "windows_result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        windows_result = {"error": f"{type(exc).__name__}: {exc}"}

    summary["windows"] = windows_result
    summary["phone"] = phone_result
    summary["exchange"] = {"windows": peer_addr_windows, "phone": peer_addr_phone}
    summary["verdict"] = judge(windows_result, phone_result)
    (work / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(json.dumps(summary["verdict"], ensure_ascii=False, indent=2))
    return 0


def _sftp_read(sftp: Any, path: str) -> str:
    try:
        with sftp.open(path, "r") as handle:
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _vm_exec(transport: Any, command: str, timeout: float = 30.0) -> str:
    """在虚机执行一条命令并取回 stdout, 超时抛 OSError."""
    channel = transport.open_session(timeout=timeout)
    try:
        channel.exec_command(command)
        chunks: list[bytes] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if channel.recv_ready():
                chunks.append(channel.recv(65536))
                continue
            if channel.exit_status_ready():
                break
            time.sleep(0.05)
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        channel.close()


def _vm_spawn(transport: Any, command: str) -> None:
    """后台拉起虚机侧进程: 用 nohup 脱离会话, 只确认 shell 退出, 不等业务进程."""
    channel = transport.open_session(timeout=10)
    try:
        channel.exec_command(command)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not channel.exit_status_ready():
            time.sleep(0.05)
    finally:
        channel.close()


def run_vm(args: argparse.Namespace) -> int:
    """编排 Windows 宿主与 Ubuntu 虚机做一次验证, 虚机侧经 SSH 拉起的 peer 子命令."""
    import hashlib

    import paramiko  # 延迟导入: 只有该子命令需要

    password = os.environ.get(args.vm_password_env, "")
    if not password:
        emit(f"缺少虚机口令环境变量 {args.vm_password_env}")
        return 2
    if not args.vm_user:
        emit(f"缺少虚机账号环境变量 {args.vm_user_env}")
        return 2
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    script_path = str(Path(__file__).resolve())
    transport = paramiko.Transport((args.vm_host, args.vm_port))
    try:
        transport.start_client(timeout=10)
        host_key = transport.get_remote_server_key()
        fingerprint = hashlib.sha256(host_key.asbytes()).hexdigest()
        expected = os.environ.get(args.vm_fingerprint_env, "").strip().lower()
        if expected and fingerprint != expected:
            emit(f"虚机主机密钥指纹不匹配: 期望 {expected}, 实际 {fingerprint}")
            return 3
        transport.auth_password(args.vm_user, password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        remote_dir = args.vm_dir
        _vm_exec(transport, f"mkdir -p {remote_dir} && rm -f {remote_dir}/*.json {remote_dir}/peer.txt")
        sftp.put(script_path, f"{remote_dir}/probe.py")

        windows_exchange = work / "windows_me.json"
        vm_exchange = work / "vm_me.txt"
        for stale in (windows_exchange, vm_exchange):
            if stale.exists():
                stale.unlink()

        common = [
            "--start-delay",
            str(args.start_delay),
            "--phase",
            str(args.phase),
            "--punch",
            str(args.punch),
            "--bulk",
            str(args.bulk),
        ]
        windows_cmd = [
            sys.executable,
            script_path,
            "peer",
            "--role",
            "windows",
            "--work",
            str(work),
            "--local-port",
            str(args.local_port),
            "--exchange-out",
            str(windows_exchange),
            "--exchange-in",
            str(vm_exchange),
            "--bulk-kbps",
            str(args.bulk_kbps),
            *common,
        ]
        remote_cmd = [
            "python3",
            f"{remote_dir}/probe.py",
            "peer",
            "--role",
            "android",
            "--work",
            remote_dir,
            "--local-port",
            str(args.local_port),
            "--exchange-out",
            f"{remote_dir}/me.json",
            "--exchange-in",
            f"{remote_dir}/peer.txt",
            "--bulk-kbps",
            str(args.bulk_kbps),
            *common,
        ]
        if args.mode == "lan":
            windows_cmd += ["--skip-stun", "--peer-addr", args.vm_lan]
            remote_cmd += ["--skip-stun", "--peer-addr", args.windows_lan, "--bind-ip", args.vm_lan.split(":")[0]]
        else:
            windows_cmd += ["--stun", args.stun]
            remote_cmd += ["--stun", args.stun]
        _vm_spawn(transport, f"cd {remote_dir} && nohup {' '.join(remote_cmd)} > {remote_dir}/peer.log 2>&1 &")
        windows_proc = subprocess.Popen(windows_cmd, cwd=str(Path(__file__).resolve().parent.parent))

        win_addr = args.vm_lan if args.mode == "lan" else ""
        vm_addr = args.windows_lan if args.mode == "lan" else ""
        if args.mode != "lan":
            deadline = time.monotonic() + args.wait
            while time.monotonic() < deadline and not (win_addr and vm_addr):
                if not win_addr and windows_exchange.exists():
                    data = json.loads(windows_exchange.read_text(encoding="utf-8") or "{}")
                    mapped = data.get("mapped") or []
                    win_addr = mapped[0] if mapped else ""
                if not vm_addr:
                    text = _sftp_read(sftp, f"{remote_dir}/me.json")
                    if text.startswith("{"):
                        try:
                            data = json.loads(text)
                        except json.JSONDecodeError:
                            data = {}
                        mapped = data.get("mapped") or []
                        vm_addr = mapped[0] if mapped else ""
                time.sleep(0.3)
            if win_addr and vm_addr:
                with sftp.open(f"{remote_dir}/peer.txt", "w") as handle:
                    handle.write(win_addr)
                vm_exchange.write_text(vm_addr, encoding="utf-8")

        windows_proc.wait(timeout=args.wait + 90)
        time.sleep(8)
        vm_text = _sftp_read(sftp, f"{remote_dir}/windows_result.json")
        (work / "vm_peer.log").write_text(_sftp_read(sftp, f"{remote_dir}/peer.log"), encoding="utf-8")
        sftp.close()
    finally:
        transport.close()
    try:
        vm_result = json.loads(vm_text)
    except json.JSONDecodeError:
        vm_result = {"error": vm_text[:400] or "虚机侧无结果文件"}
    try:
        win_result = json.loads((work / "windows_result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        win_result = {"error": f"{type(exc).__name__}: {exc}"}

    verdict = judge(win_result, vm_result)
    verdict["mode"] = args.mode
    verdict["peer"] = "vm"
    verdict["vm_host_key_sha256"] = fingerprint
    verdict["windows_mapped"] = win_result.get("mapped")
    verdict["vm_mapped"] = vm_result.get("mapped")
    sent = int((win_result.get("bulk") or {}).get("sent_packets", 0) or 0)
    vm_bulk = vm_result.get("bulk") or {}
    recv = int(vm_bulk.get("recv_packets", 0) or 0)
    if sent:
        verdict["bulk_sent_kbps"] = (win_result.get("bulk") or {}).get("sent_kbps")
        verdict["bulk_recv_packets"] = recv
        verdict["bulk_recv_kbps"] = vm_bulk.get("recv_kbps")
        verdict["bulk_loss_pct"] = round((1 - recv / sent) * 100.0, 2)
    (work / "summary.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0


def judge(windows_result: dict[str, Any], phone_result: dict[str, Any]) -> dict[str, Any]:
    windows_recv = int(windows_result.get("punch_recv", 0) or 0)
    phone_recv = int(phone_result.get("punch_recv", 0) or 0)
    success = windows_recv > 0 and phone_recv > 0
    verdict: dict[str, Any] = {
        "punch_success": success,
        "windows_recv": windows_recv,
        "phone_recv": phone_recv,
        "windows_first_ms": windows_result.get("punch_first_ms"),
        "phone_first_ms": phone_result.get("punch_first_ms"),
        "windows_rtt_p50_ms": windows_result.get("punch_rtt_p50_ms"),
        "phone_rtt_p50_ms": phone_result.get("punch_rtt_p50_ms"),
        "filter_phase_windows_listen_recv": windows_result.get("filter_phase_recv"),
        "filter_phase_phone_listen_recv": phone_result.get("filter_phase_recv"),
    }
    sent_packets = int((windows_result.get("bulk") or {}).get("sent_packets", 0) or 0)
    recv_packets = int(phone_result.get("bulk_recv_packets", 0) or 0)
    if sent_packets:
        verdict["bulk_sent_packets"] = sent_packets
        verdict["bulk_recv_packets"] = recv_packets
        verdict["bulk_loss_pct"] = round((1 - recv_packets / sent_packets) * 100.0, 2)
        verdict["bulk_target_kbps"] = (windows_result.get("bulk") or {}).get("target_kbps")
        verdict["bulk_recv_kbps"] = phone_result.get("bulk_kbps")
    return verdict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P2P 打洞可行性验证")
    sub = parser.add_subparsers(dest="command", required=True)

    stun_parser = sub.add_parser("stun", help="本机 STUN 映射行为探测")
    stun_parser.add_argument("--work", required=True)
    stun_parser.add_argument("--local-port", type=int, default=41010)
    stun_parser.add_argument("--stun", default=",".join(DEFAULT_STUN))
    stun_parser.add_argument("--repeat", type=int, default=1, help="每个 STUN 服务连做几次, 用于稳定性复测")
    stun_parser.add_argument("--interval", type=float, default=0.2, help="两轮之间的间隔秒数")
    stun_parser.set_defaults(func=run_stun)

    peer_parser = sub.add_parser("peer", help="Windows 侧对端(由编排调用)")
    peer_parser.add_argument("--role", default="windows", choices=["windows", "android"])
    peer_parser.add_argument("--work", required=True)
    peer_parser.add_argument("--local-port", type=int, default=41000)
    peer_parser.add_argument("--bind-ip", default="")
    peer_parser.add_argument("--stun", default=",".join(DEFAULT_STUN))
    peer_parser.add_argument("--exchange-in", required=True)
    peer_parser.add_argument("--exchange-out", required=True)
    peer_parser.add_argument("--peer-addr", default="")
    peer_parser.add_argument("--skip-stun", action="store_true")
    peer_parser.add_argument("--wait", type=float, default=60.0)
    peer_parser.add_argument("--start-delay", type=float, default=5.0)
    peer_parser.add_argument("--phase", type=float, default=4.0)
    peer_parser.add_argument("--punch", type=float, default=16.0)
    peer_parser.add_argument("--bulk", type=float, default=10.0)
    peer_parser.add_argument("--bulk-kbps", type=int, default=0)
    peer_parser.set_defaults(func=run_peer)

    run_parser = sub.add_parser("run", help="编排两端做一次完整验证")
    run_parser.add_argument("--work", required=True)
    run_parser.add_argument("--mode", default="same-nat", choices=["same-nat", "cross-nat", "lan"])
    run_parser.add_argument("--local-port", type=int, default=41000)
    run_parser.add_argument("--stun", default=",".join(DEFAULT_STUN))
    run_parser.add_argument("--wait", type=float, default=60.0)
    run_parser.add_argument("--start-delay", type=float, default=5.0)
    run_parser.add_argument("--phase", type=float, default=4.0)
    run_parser.add_argument("--punch", type=float, default=16.0)
    run_parser.add_argument("--bulk", type=float, default=10.0)
    run_parser.add_argument("--bulk-kbps", type=int, default=0)
    run_parser.add_argument("--windows-lan", default="192.168.31.80:41000")
    run_parser.add_argument("--phone-lan", default="192.168.31.134:41000")
    run_parser.set_defaults(func=run_orchestrator)

    vm_parser = sub.add_parser("run-vm", help="编排 Windows 宿主与 Ubuntu 虚机(SSH)做一次验证")
    vm_parser.add_argument("--work", required=True)
    vm_parser.add_argument("--mode", default="lan", choices=["lan", "same-nat", "cross-nat"])
    vm_parser.add_argument("--local-port", type=int, default=41000)
    vm_parser.add_argument("--stun", default=",".join(DEFAULT_STUN))
    vm_parser.add_argument("--wait", type=float, default=60.0)
    vm_parser.add_argument("--start-delay", type=float, default=5.0)
    vm_parser.add_argument("--phase", type=float, default=4.0)
    vm_parser.add_argument("--punch", type=float, default=16.0)
    vm_parser.add_argument("--bulk", type=float, default=10.0)
    vm_parser.add_argument("--bulk-kbps", type=int, default=0)
    vm_parser.add_argument("--vm-host", default="192.168.37.132")
    vm_parser.add_argument("--vm-port", type=int, default=22022)
    vm_parser.add_argument("--vm-user", default=os.environ.get("RDC_VM_USER", ""))
    vm_parser.add_argument("--vm-user-env", default="RDC_VM_USER")
    vm_parser.add_argument("--vm-password-env", default="RDC_VM_PASSWORD")
    vm_parser.add_argument("--vm-fingerprint-env", default="RDC_VM_HOST_KEY_SHA256")
    vm_parser.add_argument("--vm-dir", default="/tmp/rdc_p2p")
    vm_parser.add_argument("--windows-lan", default="192.168.37.1:41000")
    vm_parser.add_argument("--vm-lan", default="192.168.37.132:41000")
    vm_parser.set_defaults(func=run_vm)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
