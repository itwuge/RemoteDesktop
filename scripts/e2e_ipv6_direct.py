# ==========================================================================================
# 文件        : e2e_ipv6_direct.py
# 所属目录    : scripts/
# 文件用途    : IPv6 直连双向取证: Windows 控制端 ↔ Ubuntu 虚机被控端, 断言对端为 240e 且无中继
# 架构定位    : 取证脚本, 复用产品 RemoteSession/HostService; 不修改任何产品行为
# 核心功能    : 起被控端并解析横幅; IPv6 字面量 TCP 探测; 会话统计与解码帧落盘; 输入落盘回读;
#               双端 TCP 对端断言; 候选与最终路径写 run.json
# 职责边界    : 不做降级; 连不通就带 errno/耗时/监听地址报失败; 不写口令; 不改网络配置
# 依赖关系    : rdc.session.remote, rdc.commands.api, paramiko(虚机 SSH), Pillow(解码帧落盘)
# ==========================================================================================

"""IPv6 直连双向取证.

用法:
    python scripts/e2e_ipv6_direct.py --guest-ipv6 240e:... --direction both

前置:
    虚机已桥接并带 240e 全局地址; 凭据只从 RDC_VM_USER / RDC_VM_PASSWORD 读取.

判定:
    direct_ipv6 需同时满足: 连接 27888 成功; Windows 侧会话连接对端属于 240e;
    虚机侧 ss 对端属于本机 240e; 两端均无指向外部中继 47.109.32.249 的连接.
    任一条不满足即 failed, 并记录 blocked_at 与实测数值.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

GUEST_APP = "/home/wuge/rdc/app"
GUEST_PYTHON = "/home/wuge/rdc/venv/bin/python"
GUEST_DISPLAY = ":0"
GUEST_XAUTHORITY = "/run/user/1000/gdm/Xauthority"
EXTERNAL_RELAY_IP = "47.109.32.249"
CN_PREFIX = "240e:"
HOST_PORT = 27888
SSH_TIMEOUT_S = 20.0
TCP_PROBE_TIMEOUT_S = 3.0
SESSION_SECONDS = 8.0

_remote = importlib.import_module("rdc.session.remote")
_api = importlib.import_module("rdc.commands.api")


def emit(message: str) -> None:
    """把进度写到标准输出; 门禁禁止 print, 统一走这里."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def run_command(args: list[str], timeout: float = 60.0) -> tuple[int, str]:
    """执行本机命令, 返回 (退出码, stdout+stderr)."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        return 1, f"{type(error).__name__}: {error}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def powershell(script: str, timeout: float = 30.0) -> tuple[int, str]:
    """执行一段 PowerShell 并返回输出; 用于只读取 Windows 网络状态."""
    return run_command(["powershell", "-NoProfile", "-Command", script], timeout)


def is_cn_ipv6(address: str) -> bool:
    """判断地址是否为 240e: 开头的全局 IPv6."""
    raw = address.split("%", 1)[0].strip()
    if not raw.lower().startswith(CN_PREFIX):
        return False
    try:
        return ipaddress.ip_address(raw).version == 6
    except ValueError:
        return False


CN_ADDRESS_PATTERN = re.compile(r"240e:[0-9a-fA-F:]+")


def line_has_cn_ipv6(line: str) -> bool:
    """ss/pgrep 输出行里是否出现 240e 地址."""
    return bool(CN_ADDRESS_PATTERN.search(line))


def redact_code(text: str) -> str:
    """证据日志里遮蔽连接码行, 避免把一次性口令带进证据."""
    lines = ["连接码 : ***" if "连接码" in line else line for line in text.splitlines()]
    return "\n".join(lines) + "\n"


class Evidence:
    """证据目录: 所有原始输出与断言落盘到这里."""

    def __init__(self, root: Path) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_id = stamp
        self.root = root / stamp if root.name != stamp else root
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def write_text(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name: str, payload: Any) -> Path:
        return self.write_text(name, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


class Guest:
    """虚机 SSH 只读入口; 口令仅来自环境变量, 不落盘不打印."""

    def __init__(self, host: str, port: int) -> None:
        user = os.environ.get("RDC_VM_USER", "")
        password = os.environ.get("RDC_VM_PASSWORD", "")
        if not user or not password:
            raise RuntimeError("缺少 RDC_VM_USER / RDC_VM_PASSWORD 环境变量")
        paramiko = importlib.import_module("paramiko")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=port,
            username=user,
            password=password,
            timeout=SSH_TIMEOUT_S,
            allow_agent=False,
            look_for_keys=False,
        )
        self._client: Any = client

    def exec(self, command: str, timeout: float = 60.0, stdin_text: str = "") -> tuple[int, str]:
        """执行远端命令并返回 (退出码, 输出); 可选把 stdin_text 写入远端标准输入."""
        channel = self._client.get_transport().open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.exec_command(command)
        if stdin_text:
            channel.sendall(stdin_text.encode("utf-8"))
        channel.shutdown_write()
        chunks: list[bytes] = []
        while True:
            if channel.recv_ready():
                chunks.append(channel.recv(65536))
                continue
            if channel.exit_status_ready() and not channel.recv_ready():
                break
            time.sleep(0.05)
        exit_code = channel.recv_exit_status()
        channel.close()
        return exit_code, b"".join(chunks).decode("utf-8", "replace")

    def put_text(self, remote_path: str, text: str) -> None:
        sftp = self._client.open_sftp()
        try:
            with sftp.file(remote_path, "w") as handle:
                handle.write(text)
        finally:
            sftp.close()

    def get_file(self, remote_path: str, local_path: Path) -> bool:
        """取回远端文件; 不存在或读取失败返回 False."""
        sftp = self._client.open_sftp()
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_path, str(local_path))
            return True
        except OSError:
            return False
        finally:
            sftp.close()

    def close(self) -> None:
        with contextlib.suppress(OSError, EOFError):
            self._client.close()


def guest_ipv6_addresses(guest: Guest, preferred: str) -> list[str]:
    """列出虚机全部 240e 全局地址; 入参地址排在首位."""
    addresses = [preferred] if preferred else []
    _, out = guest.exec("ip -6 -o addr show scope global")
    for line in out.splitlines():
        parts = line.split()
        for index, part in enumerate(parts):
            if part == "inet6" and index + 1 < len(parts):
                address = parts[index + 1].split("/", 1)[0]
                if is_cn_ipv6(address) and address not in addresses:
                    addresses.append(address)
    return addresses


def local_windows_ipv6() -> list[str]:
    """读取本机 240e 全局 IPv6 地址列表."""
    script = (
        "Get-NetIPAddress -AddressFamily IPv6 | "
        "Where-Object { $_.IPAddress -like '240e:*' } | "
        "Select-Object -ExpandProperty IPAddress"
    )
    _, out = powershell(script)
    return [line.strip() for line in out.splitlines() if is_cn_ipv6(line.strip())]


def tcp_probe(host: str, port: int, timeout: float = TCP_PROBE_TIMEOUT_S) -> dict[str, Any]:
    """对 IPv6 字面量做一次 TCP 建连, 记录结果、errno 与耗时."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = (time.perf_counter() - started) * 1000.0
            return {"ok": True, "elapsed_ms": round(elapsed, 2), "error": ""}
    except OSError as error:
        elapsed = (time.perf_counter() - started) * 1000.0
        return {
            "ok": False,
            "elapsed_ms": round(elapsed, 2),
            "error": f"{type(error).__name__}: {error}",
            "errno": error.errno,
        }


# Get-NetTCPConnection 的 State 在本机返回数字枚举(2=Listen, 5=Established), 字符串只在部分版本出现
LISTEN_STATES = {2, "Listen"}
ESTABLISHED_STATES = {5, "Established"}


def parse_connection_output(code: int, out: str) -> list[dict[str, Any]]:
    """把 ConvertTo-Json 输出解析成连接字典列表; 解析不了返回空表示无连接."""
    if code != 0:
        return []
    text = out.strip()
    start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
    if start < 0:
        return []
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError:
        return []
    items = payload if isinstance(payload, list) else [payload]
    return [item for item in items if isinstance(item, dict)]


def windows_connections(pid: int) -> list[dict[str, Any]]:
    """读取指定进程持有的 TCP 连接."""
    script = (
        f"Get-NetTCPConnection -OwningProcess {pid} -ErrorAction SilentlyContinue | "
        "Select-Object State,LocalAddress,LocalPort,RemoteAddress,RemotePort | ConvertTo-Json -Compress"
    )
    return parse_connection_output(*powershell(script))


def windows_port_connections(port: int) -> list[dict[str, Any]]:
    """读取本机全部落在指定本地端口上的 TCP 连接, 不按进程过滤."""
    script = (
        f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
        "Select-Object State,LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess "
        "| ConvertTo-Json -Compress"
    )
    return parse_connection_output(*powershell(script))


def has_state(item: dict[str, Any], states: set[Any]) -> bool:
    """连接状态匹配; 兼容数字枚举与字符串两种返回."""
    state = item.get("State")
    return state in states or str(state) in states


def is_loopback(address: str) -> bool:
    """回环或未指定地址, 不算入站对端."""
    raw = address.split("%", 1)[0].strip()
    if not raw:
        return True
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def guest_established(guest: Guest) -> list[str]:
    """虚机侧 established TCP 连接原始行."""
    _, out = guest.exec("ss -tnH state established")
    return [line.strip() for line in out.splitlines() if line.strip()]


def add_assertion(result: dict[str, Any], name: str, ok: bool, detail: str) -> None:
    """追加一条断言结果."""
    result["assertions"].append({"name": name, "ok": ok, "detail": detail})


def save_decoded_frame(frame: Any, path: Path) -> bool:
    """把 DecodedFrame 的 BGRA 像素落成 PNG; 失败返回 False."""
    try:
        image_module = importlib.import_module("PIL.Image")
        image = image_module.frombuffer("RGBA", (frame.width, frame.height), frame.pixels, "raw", "BGRA", 0, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, "PNG")
        return True
    except (ImportError, OSError, ValueError):
        return False


def session_metrics(session: Any) -> dict[str, Any]:
    """抓一次会话统计, 只保留非空字段."""
    try:
        stats = dict(session.stats())
    except (AttributeError, RuntimeError, ValueError):
        return {}
    return {key: value for key, value in stats.items() if value not in (None, "")}


def stop_guest_host(guest: Guest, pid: str) -> str:
    """按 PID 停掉本轮被控端, 不误杀其它并行会话."""
    if not pid.strip().isdigit():
        return ""
    _, out = guest.exec(f"kill -9 {pid.strip()} 2>/dev/null; sleep 0.3; pgrep -af 'rdc_host_linux.py' || true")
    return out


def fetch_guest_file(guest: Guest, remote_path: str, local_path: Path) -> bool:
    """从虚机取回文件; 不存在返回 False."""
    return guest.get_file(remote_path, local_path)


def read_guest_file(guest: Guest, path: str) -> tuple[int, str]:
    return guest.exec(f"cat {path} 2>/dev/null || true")


def forward_direction(
    evidence: Evidence, guest: Guest, guest_addresses: list[str], port: int, marker: str
) -> dict[str, Any]:
    """正向: Windows 控制端 → 虚机被控端."""
    result: dict[str, Any] = {
        "direction": "forward",
        "guest_ipv6": "",
        "port": port,
        "candidates": [],
        "selected_path": {"kind": "none"},
        "assertions": [],
        "metrics": {},
        "verdict": "failed",
        "blocked_at": "",
    }
    run_tag = evidence.run_id
    host_log = f"/tmp/rdc-e2e-host-{run_tag}.log"
    input_path = f"/tmp/rdc-e2e-input-{run_tag}.txt"
    xterm_log = f"/tmp/rdc-e2e-xterm-{run_tag}.log"

    guest.exec(f"pkill -f 'rdc_host_linux.py --port {port}' 2>/dev/null; rm -f {host_log} {input_path}; true")
    launch = (
        f"cd {GUEST_APP}; DISPLAY={GUEST_DISPLAY} XAUTHORITY={GUEST_XAUTHORITY} "
        f"nohup bash scripts/start-host.sh --port {port} > {host_log} 2>&1 & echo $!"
    )
    _, host_pid = guest.exec(launch)
    emit(f"正向: 被控端 PID {host_pid.strip()}")

    deadline = time.monotonic() + 45.0
    banner = ""
    while time.monotonic() < deadline:
        _, banner = guest.exec(f"cat {host_log} 2>/dev/null || true")
        if "被控端已就绪" in banner or "启动失败" in banner:
            break
        time.sleep(1.0)
    evidence.write_text("forward/host_banner.log", redact_code(banner))

    _, listen = guest.exec(f"ss -lntH | grep -F ':{port}' || true")
    evidence.write_text("forward/guest_listen.txt", listen or "空: 27888 未监听\n")
    result["guest_listen"] = listen.strip()

    parsed = {"device_id": "", "access_code": "", "address": "", "port": port}
    for line in banner.splitlines():
        if "设备 ID" in line:
            parsed["device_id"] = line.split(":", 1)[-1].strip()
        elif "连接码" in line:
            parsed["access_code"] = line.split(":", 1)[-1].strip()
        elif "监听地址" in line:
            address_part = line.split(":", 1)[-1].strip()
            if ":" in address_part:
                parsed["address"], _, port_text = address_part.rpartition(":")
                if port_text.isdigit():
                    parsed["port"] = int(port_text)

    probes = [{"address": address, **tcp_probe(address, port, 2.0)} for address in guest_addresses]
    result["guest_ipv6_probes"] = probes
    evidence.write_json("forward/guest_ipv6_probes.json", probes)
    selected = next((item for item in probes if item["ok"]), None)
    for item in probes:
        emit(f"正向: TCP 探测 {item['address']}:{port} -> ok={item['ok']} {item.get('errno', '')} {item['error']}")
    add_assertion(
        result,
        "ipv6_tcp_reachable",
        selected is not None,
        json.dumps(probes, ensure_ascii=False),
    )

    ipv6_listen = any("*:" in item or "[::]" in item for item in listen.splitlines())
    add_assertion(
        result,
        "guest_listen_ipv6",
        ipv6_listen,
        f"监听行={listen.strip() or '无'}",
    )

    if selected is None:
        result["blocked_at"] = "guest_ipv6_unreachable" if "被控端已就绪" in banner else "guest_host_start_failed"
        result["host_log_tail"] = "\n".join(banner.splitlines()[-10:])
        stop_guest_host(guest, host_pid)
        fetch_guest_file(guest, host_log, evidence.root / "logs" / "guest-host.log")
        return result

    guest_ipv6 = selected["address"]
    result["guest_ipv6"] = guest_ipv6
    result["candidates"] = [{"kind": "host-direct", "address": guest_ipv6, "port": port, "source": "controller offer"}]

    if not parsed["device_id"] or not parsed["access_code"]:
        result["blocked_at"] = "banner_parse_failed"
        stop_guest_host(guest, host_pid)
        return result

    guest.exec(
        f"DISPLAY={GUEST_DISPLAY} XAUTHORITY={GUEST_XAUTHORITY} nohup xterm -T rdc-e2e-input "
        f"-e bash -c 'cat > {input_path}' > {xterm_log} 2>&1 & echo $!"
    )

    config = _remote.RemoteConfig(
        host=guest_ipv6,
        port=port,
        device_id=parsed["device_id"],
        access_code=parsed["access_code"],
        display_name="Windows 控制端(IPv6 取证)",
    )
    frames: list[Path] = []
    frame_counter = {"value": 0}

    def on_frame(frame: Any) -> None:
        frame_counter["value"] += 1
        if frame_counter["value"] in {1, 30, 90}:
            target = evidence.root / "forward" / f"frame_{frame_counter['value']:03d}.png"
            if save_decoded_frame(frame, target):
                frames.append(target)

    def on_status(status: Any) -> None:
        evidence.write_text("forward/status.log", f"{status.state} {status.message}\n")

    session = _remote.RemoteSession(config, on_frame=on_frame, on_status=on_status)
    session.start()
    ready = session.wait_ready(25.0)
    add_assertion(result, "session_ready", ready, f"wait_ready={ready}")

    if ready:
        metrics_samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + SESSION_SECONDS
        typed = False
        while time.monotonic() < deadline:
            sample = session_metrics(session)
            if sample:
                metrics_samples.append(sample)
            if not typed and time.monotonic() > deadline - SESSION_SECONDS + 2.0:
                guest.exec(
                    f"DISPLAY={GUEST_DISPLAY} XAUTHORITY={GUEST_XAUTHORITY} "
                    "xdotool search --name rdc-e2e-input windowactivate --sync 2>/dev/null || true"
                )
                for char in marker:
                    session.send_input({"kind": "key", "action": "char", "text": char})
                    time.sleep(0.03)
                session.send_input({"kind": "key", "action": "key", "key": "Return"})
                time.sleep(0.3)
                session.send_input({"kind": "key", "action": "combo", "mods": ["ctrl"], "key": "d"})
                typed = True
            time.sleep(0.5)
        result["metrics_samples"] = metrics_samples
        result["frames_saved"] = [path.name for path in frames]
        _, landed = read_guest_file(guest, input_path)
        evidence.write_text("forward/input_landed.txt", landed)
        add_assertion(result, "input_landed", marker in landed, f"期望包含 {marker}; 实得 {landed!r}")
        conns = windows_connections(os.getpid())
        session_conns = [item for item in conns if str(item.get("RemotePort")) == str(port)]
        peers = [str(item.get("RemoteAddress", "")) for item in session_conns]
        add_assertion(result, "windows_peer_ipv6", bool(peers) and all(is_cn_ipv6(peer) for peer in peers), str(peers))
        add_assertion(
            result,
            "no_external_relay_windows",
            not any(EXTERNAL_RELAY_IP in json.dumps(item) for item in conns),
            f"连接数={len(conns)}",
        )
        guest_lines = guest_established(guest)
        evidence.write_text("forward/guest_established.txt", "\n".join(guest_lines) + "\n")
        guest_peers = [line for line in guest_lines if line_has_cn_ipv6(line)]
        add_assertion(result, "guest_peer_ipv6", bool(guest_peers), str(guest_peers[:3]))
        add_assertion(
            result,
            "no_external_relay_guest",
            not any(EXTERNAL_RELAY_IP in line for line in guest_lines),
            f"连接数={len(guest_lines)}",
        )
        if all(item["ok"] for item in result["assertions"]):
            result["selected_path"] = {"kind": "direct_ipv6", "peer": guest_ipv6, "port": port}
            result["verdict"] = "direct_ipv6"
        session.stop()

    stop_guest_host(guest, host_pid)
    fetch_guest_file(guest, host_log, evidence.root / "logs" / "guest-host.log")
    guest.exec("pkill -f 'xterm -T rdc-e2e-input' 2>/dev/null; true")
    return result


def start_windows_host() -> tuple[dict[str, Any], Any]:
    """在本进程起真实 Windows 被控端; 返回 (hosting 信息, 服务对象)."""
    result = _api.start_hosting("RDC-E2E-Windows")
    if not result.ok:
        return {"ok": False, "message": result.message}, None
    info = _api.get_hosting_info()
    return {"ok": True, "message": result.message, "info": info}, _api


def stop_windows_host() -> None:
    _api.stop_hosting()


def reverse_direction(
    evidence: Evidence, guest: Guest, guest_host: str, ssh_port: int, port: int, marker: str
) -> dict[str, Any]:
    """反向: 虚机控制端 → Windows 被控端."""
    result: dict[str, Any] = {
        "direction": "reverse",
        "port": port,
        "candidates": [],
        "selected_path": {"kind": "none"},
        "assertions": [],
        "metrics": {},
        "verdict": "failed",
        "blocked_at": "",
    }
    windows_addresses = local_windows_ipv6()
    result["windows_ipv6"] = windows_addresses
    evidence.write_text("reverse/windows_ipv6.txt", "\n".join(windows_addresses) + "\n")
    if not windows_addresses:
        result["blocked_at"] = "windows_no_240e"
        return result

    hosting, _ = start_windows_host()
    if not hosting.get("ok"):
        result["blocked_at"] = "windows_host_start_failed"
        result["hosting"] = hosting
        return result
    info = hosting.get("info") or {}
    result["hosting_port"] = info.get("port")
    time.sleep(1.0)

    listen_rows = [item for item in windows_port_connections(port) if has_state(item, LISTEN_STATES)]
    evidence.write_json("reverse/windows_listen.json", listen_rows)
    owners = sorted({str(item.get("OwningProcess")) for item in listen_rows})
    add_assertion(result, "windows_listen_recorded", bool(listen_rows), f"监听进程={owners}")
    add_assertion(
        result,
        "windows_listen_owner",
        str(os.getpid()) in owners,
        f"本进程={os.getpid()} 监听进程={owners}",
    )

    probes: list[dict[str, Any]] = []
    selected = ""
    for address in windows_addresses:
        output = probe_from_guest(guest, address, port)
        item = {"address": address, "output": output.strip(), "ok": output.strip().startswith("OK")}
        probes.append(item)
        if item["ok"] and not selected:
            selected = address
    evidence.write_json("reverse/guest_probes.json", probes)
    result["guest_probe"] = probes
    for item in probes:
        emit(f"反向: 虚机探测 {item['address']}:{port} -> {item['output']}")
    add_assertion(result, "ipv6_tcp_reachable", bool(selected), json.dumps(probes, ensure_ascii=False))

    if not selected:
        result["blocked_at"] = "windows_ipv6_unreachable"
        stop_windows_host()
        return result

    target = selected
    result["candidates"] = [{"kind": "host-direct", "address": target, "port": port, "source": "reverse offer"}]

    snippet = (
        "import json, sys\n"
        "sys.path.insert(0, '/home/wuge/rdc/app/src')\n"
        "from rdc.session.remote import RemoteConfig, RemoteSession\n"
        "from PIL import Image\n"
        "host, port, device_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]\n"
        "marker = sys.argv[4]\n"
        "code = sys.stdin.readline().strip()\n"
        "frames = {'n': 0}\n"
        "def on_frame(frame):\n"
        "    frames['n'] += 1\n"
        "    if frames['n'] == 1:\n"
        "        try:\n"
        "            Image.frombuffer('RGBA', (frame.width, frame.height), frame.pixels, 'raw', 'BGRA', 0, 1)"
        ".convert('RGB').save('/tmp/rdc-e2e-reverse-frame.png')\n"
        "        except (OSError, ValueError) as exc:\n"
        "            sys.stdout.write('FRAME_ERROR %s: %s\\n' % (type(exc).__name__, exc))\n"
        "session = RemoteSession(RemoteConfig(host=host, port=port, device_id=device_id, "
        "access_code=code, display_name='Linux 控制端(IPv6 取证)'), on_frame=on_frame)\n"
        "try:\n"
        "    session.start()\n"
        "    ready = session.wait_ready(20.0)\n"
        "except (OSError, RuntimeError, ValueError) as exc:\n"
        "    sys.stdout.write('ERROR %s: %s\\n' % (type(exc).__name__, exc))\n"
        "    raise SystemExit(2)\n"
        "sys.stdout.write('READY %r\\n' % ready)\n"
        "if ready:\n"
        "    import time\n"
        "    for _ in range(6):\n"
        "        stats = session.stats()\n"
        "        sys.stdout.write('STATS ' + json.dumps({k: v for k, v in stats.items() if v not in (None, '')}, "
        "ensure_ascii=False) + '\\n')\n"
        "        time.sleep(1.0)\n"
        "    time.sleep(1.0)\n"
        "    for char in marker:\n"
        "        session.send_input({'kind': 'key', 'action': 'char', 'text': char})\n"
        "        time.sleep(0.03)\n"
        "    session.send_input({'kind': 'key', 'action': 'key', 'key': 'Return'})\n"
        "    time.sleep(3.0)\n"
        "    session.stop()\n"
        "sys.stdout.write('FRAMES %s\\n' % frames['n'])\n"
    )
    guest.put_text("/tmp/rdc-e2e-controller.py", snippet)

    console_script = (
        "$Host.UI.RawUI.WindowTitle='RDC-E2E-INPUT'; $x = Read-Host; "
        f"Set-Content -LiteralPath '{evidence.root / 'reverse' / 'windows_input.txt'}' -Value $x"
    )
    creation = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    console = subprocess.Popen(
        ["powershell", "-NoProfile", "-NoExit", "-Command", console_script],
        creationflags=creation,
    )
    time.sleep(2.0)
    _, focus = powershell("$w = New-Object -ComObject WScript.Shell; $w.AppActivate('RDC-E2E-INPUT')")
    evidence.write_text("reverse/focus.txt", focus)
    add_assertion(result, "console_focused", "True" in focus, focus.strip())

    samples: dict[str, Any] = {"windows": [], "port": [], "guest": [], "stop": False}
    aux = Guest(guest_host, ssh_port)

    def sample_loop() -> None:
        while not samples["stop"]:
            samples["windows"].append(windows_connections(os.getpid()))
            samples["port"].append(windows_port_connections(port))
            _, listing = aux.exec("ss -tnH state established")
            samples["guest"].append(listing)
            time.sleep(1.0)

    sampler = threading.Thread(target=sample_loop, daemon=True, name="e2e-tcp-sampler")
    sampler.start()

    command = f"{GUEST_PYTHON} /tmp/rdc-e2e-controller.py {target} {port} {info.get('device_id', '')} {marker}"
    code, output = guest.exec(command, timeout=90.0, stdin_text=str(info.get("access_code", "")) + "\n")
    samples["stop"] = True
    sampler.join(timeout=5.0)
    aux.close()
    evidence.write_text("reverse/controller.log", output)
    result["controller_exit"] = code
    result["metrics"] = [line[6:] for line in output.splitlines() if line.startswith("STATS ")]
    fetched = fetch_guest_file(guest, "/tmp/rdc-e2e-reverse-frame.png", evidence.root / "reverse" / "frame_001.png")
    add_assertion(result, "reverse_frame_saved", fetched, "虚机解码首帧已回传" if fetched else "未取到首帧")

    input_file = evidence.root / "reverse" / "windows_input.txt"
    landed = input_file.read_text(encoding="utf-8", errors="replace") if input_file.is_file() else ""
    add_assertion(result, "input_landed", marker in landed, f"期望包含 {marker}; 实得 {landed!r}")

    own_conns = [item for batch in samples["windows"] for item in batch]
    port_conns = [item for batch in samples["port"] for item in batch]
    evidence.write_json(
        "reverse/windows_samples.json",
        {"script_pid": os.getpid(), "own": own_conns, "port": port_conns},
    )
    inbound = [
        item
        for item in port_conns
        if has_state(item, ESTABLISHED_STATES)
        and str(item.get("LocalPort")) == str(port)
        and not is_loopback(str(item.get("RemoteAddress", "")))
    ]
    peers = sorted({str(item.get("RemoteAddress", "")) for item in inbound})
    owned = [item for item in inbound if str(item.get("OwningProcess")) == str(os.getpid())]
    add_assertion(
        result,
        "windows_peer_ipv6",
        bool(peers) and all(is_cn_ipv6(peer) for peer in peers),
        f"入站={len(inbound)} 对端={peers}",
    )
    add_assertion(
        result,
        "windows_inbound_owned",
        bool(owned),
        f"本进程={os.getpid()} 本进程入站={len(owned)} 全部入站={len(inbound)}",
    )
    add_assertion(
        result,
        "no_external_relay_windows",
        not any(EXTERNAL_RELAY_IP in json.dumps(item) for item in own_conns + port_conns),
        f"采样连接数={len(own_conns) + len(port_conns)}",
    )
    guest_ss = "\n".join(line for batch in samples["guest"] for line in batch.splitlines())
    evidence.write_text("reverse/guest_established.txt", guest_ss + "\n")
    guest_peers = [line.strip() for line in guest_ss.splitlines() if line_has_cn_ipv6(line)]
    add_assertion(result, "guest_peer_ipv6", bool(guest_peers), str(sorted(set(guest_peers))[:3]))
    add_assertion(
        result,
        "no_external_relay_guest",
        not any(EXTERNAL_RELAY_IP in line for line in guest_ss.splitlines()),
        f"采样行数={len(guest_ss.splitlines())}",
    )

    stop_windows_host()
    with contextlib.suppress(OSError):
        console.terminate()
    if all(item["ok"] for item in result["assertions"]):
        result["selected_path"] = {"kind": "direct_ipv6", "peer": target, "port": port}
        result["verdict"] = "direct_ipv6"
    return result


def probe_from_guest(guest: Guest, target: str, port: int) -> str:
    """在虚机里对目标 IPv6 做一次 TCP 建连, 返回原始结果行."""
    _, output = guest.exec(
        f"RDC_TARGET={target} RDC_PORT={port} python3 - <<'PY'\n"
        "import os, socket, sys, time\n"
        "host = os.environ['RDC_TARGET']\n"
        "port = int(os.environ['RDC_PORT'])\n"
        "started = time.perf_counter()\n"
        "try:\n"
        "    socket.create_connection((host, port), 3).close()\n"
        "    sys.stdout.write('OK %.1fms\\n' % ((time.perf_counter() - started) * 1000))\n"
        "except OSError as exc:\n"
        "    elapsed_ms = (time.perf_counter() - started) * 1000\n"
        "    sys.stdout.write('ERR %s errno=%s %.1fms\\n' % (type(exc).__name__, exc.errno, elapsed_ms))\n"
        "PY",
        timeout=15.0,
    )
    return output


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="IPv6 直连双向取证")
    parser.add_argument("--guest-host", default="192.168.1.215")
    parser.add_argument("--ssh-port", type=int, default=22022)
    parser.add_argument("--guest-ipv6", default="")
    parser.add_argument("--port", type=int, default=HOST_PORT)
    parser.add_argument("--direction", choices=("forward", "reverse", "both"), default="both")
    parser.add_argument("--evidence", default="")
    parser.add_argument("--marker", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行取证; 任一端失败返回 1."""
    arguments = parse_args(argv)
    root = Path(arguments.evidence) if arguments.evidence else ROOT / "evidence" / "ipv6-direct"
    evidence = Evidence(root)
    # 本轮产品日志也收进证据目录, 避免与并行线的默认日志目录互相覆盖
    os.environ["RDC_LOG_DIR"] = str(evidence.logs)
    marker = arguments.marker or f"RDC-IPV6-{evidence.run_id}"
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": evidence.run_id,
        "marker": marker,
        "external_relay_ip": EXTERNAL_RELAY_IP,
        "results": {},
    }
    emit(f"证据目录: {evidence.root}")
    guest = Guest(arguments.guest_host, arguments.ssh_port)
    try:
        guest_addresses = guest_ipv6_addresses(guest, arguments.guest_ipv6)
        payload["guest_ipv6_candidates"] = guest_addresses
        emit(f"虚机 IPv6 候选: {', '.join(guest_addresses) or '未取到'}")
        if arguments.direction in {"forward", "both"}:
            payload["results"]["forward"] = forward_direction(evidence, guest, guest_addresses, arguments.port, marker)
            emit(f"正向结论: {payload['results']['forward']['verdict']}")
        if arguments.direction in {"reverse", "both"}:
            payload["results"]["reverse"] = reverse_direction(
                evidence, guest, arguments.guest_host, arguments.ssh_port, arguments.port, marker
            )
            emit(f"反向结论: {payload['results']['reverse']['verdict']}")
    finally:
        guest.close()
    verdicts = [item["verdict"] for item in payload["results"].values()]
    payload["verdict"] = "direct_ipv6" if verdicts and all(item == "direct_ipv6" for item in verdicts) else "failed"
    evidence.write_json("run.json", payload)
    emit(f"总判定: {payload['verdict']} -> {evidence.root / 'run.json'}")
    return 0 if payload["verdict"] == "direct_ipv6" else 1


if __name__ == "__main__":
    raise SystemExit(main())
