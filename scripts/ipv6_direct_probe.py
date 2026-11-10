#!/usr/bin/env python3
"""IPv6 直连能力只读探测（Windows 控制端 ↔ Linux 被控端）.

用途
    按 docs/design/connect-v1.md 的 L1 判据，只读采集本机与被控端的 IPv6
    地址/前缀/默认路由/外网可达性，给出「可用 / 受阻 / 不通过」三分类，并明确
    标出受阻层级。脚本不建立业务会话、不发送任何媒体帧、不修改系统网络配置。

判据（与 connect-v1.md 第三节一致）
    - 全局单播：落在 2000::/3，排除 2001:db8::/32；ULA(fc00::/7)、fe80::/10、
      ::1 均不计为全局。
    - 本端可用：至少 1 个全局地址 + 存在 ::/0 默认路由 + 对 2 个目标各建一次
      TCP 连接且至少 1 次成功（单目标超时 800 ms）。
    - 双端可用才进入 L1 连通检查；本脚本只判「能力」，不冒充「直连通过」。

边界
    - 只读；不启动/重启虚机，不改防火墙，不改路由。
    - 被控端通过 --guest user@host 只执行 `ip -6 addr` / `ip -6 route`，
      不安装依赖、不写文件。

退出码
    0 本端与被控端均具备 L1 能力；1 任一端不具备（受阻）；2 采集失败。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GLOBAL_NET = ipaddress.ip_network("2000::/3")
DOC_NET = ipaddress.ip_network("2001:db8::/32")
ULA_NET = ipaddress.ip_network("fc00::/7")
LL_NET = ipaddress.ip_network("fe80::/10")
CN_ISP_NETS = (
    ipaddress.ip_network("240e::/20"),  # 中国电信
    ipaddress.ip_network("2408::/20"),  # 中国联通
    ipaddress.ip_network("2409::/20"),  # 中国移动
)
PROBE_TARGETS = (
    ("2606:4700:4700::1111", 443, "cloudflare-dns"),
    ("2400:3200::1", 443, "alidns"),
    ("2001:4860:4860::8888", 443, "google-dns"),
)
PROBE_TIMEOUT_S = 0.8


def classify(address: str) -> str:
    """把单个 IPv6 地址归类为 global / ula / link_local / loopback / other."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "invalid"
    if ip.is_loopback:
        return "loopback"
    if ip in LL_NET:
        return "link_local"
    if ip in ULA_NET:
        return "ula"
    if ip in GLOBAL_NET and ip not in DOC_NET:
        return "global"
    return "other"


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """执行只读命令，返回 (退出码, stdout+stderr)。命令缺失时返回 127。"""
    if not shutil.which(cmd[0]):
        return 127, f"missing:{cmd[0]}"
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def collect_windows() -> dict[str, Any]:
    """Windows：用 Get-NetIPAddress / Get-NetRoute 采集地址与前缀路由。"""
    ps = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-NetIPAddress -AddressFamily IPv6 | Select-Object IPAddress,PrefixLength,"
        "PrefixOrigin,SuffixOrigin,AddressState,InterfaceAlias | ConvertTo-Json -Compress",
    ]
    code, out = run(ps)
    addresses: list[dict[str, Any]] = []
    if code == 0 and out.strip().startswith(("[", "{")):
        try:
            raw = json.loads(out[out.index("[") if out.startswith("[") else out.index("{") :])
            raw = raw if isinstance(raw, list) else [raw]
            for item in raw:
                ip = str(item.get("IPAddress") or "")
                addresses.append(
                    {
                        "address": ip,
                        "prefix_len": item.get("PrefixLength"),
                        "kind": classify(ip),
                        "prefix_origin": item.get("PrefixOrigin"),
                        "suffix_origin": item.get("SuffixOrigin"),
                        "state": item.get("AddressState"),
                        "iface": item.get("InterfaceAlias"),
                    }
                )
        except (ValueError, KeyError) as exc:
            addresses.append({"address": "", "kind": "parse_error", "detail": type(exc).__name__})

    ps_route = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-NetRoute -AddressFamily IPv6 | Select-Object DestinationPrefix,NextHop,"
        "InterfaceAlias,RouteMetric | ConvertTo-Json -Compress",
    ]
    code, out = run(ps_route)
    routes: list[dict[str, Any]] = []
    if code == 0 and out.strip().startswith(("[", "{")):
        try:
            raw = json.loads(out[out.index("[") if out.startswith("[") else out.index("{") :])
            raw = raw if isinstance(raw, list) else [raw]
            for item in raw:
                routes.append(
                    {
                        "destination": item.get("DestinationPrefix"),
                        "next_hop": item.get("NextHop"),
                        "iface": item.get("InterfaceAlias"),
                        "metric": item.get("RouteMetric"),
                    }
                )
        except (ValueError, KeyError):
            pass
    return {"addresses": addresses, "routes": routes}


def collect_linux() -> dict[str, Any]:
    """Linux：优先 ip -6 -j，缺失时退回文本解析。"""
    addresses: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    code, out = run(["ip", "-6", "-j", "addr", "show"])
    if code == 0 and out.strip().startswith("["):
        try:
            for link in json.loads(out):
                iface = link.get("ifname") or ""
                for info in link.get("addr_info") or []:
                    ip = str(info.get("local") or "")
                    if not ip:
                        continue
                    addresses.append(
                        {
                            "address": ip,
                            "prefix_len": info.get("prefixlen"),
                            "kind": classify(ip),
                            "scope": info.get("scope"),
                            "iface": iface,
                        }
                    )
        except (ValueError, KeyError):
            addresses.append({"address": "", "kind": "parse_error"})
    code, out = run(["ip", "-6", "-j", "route", "show"])
    if code == 0 and out.strip().startswith("["):
        try:
            for item in json.loads(out):
                routes.append(
                    {
                        "destination": item.get("dst"),
                        "next_hop": item.get("gateway"),
                        "iface": item.get("dev"),
                        "metric": item.get("metric"),
                    }
                )
        except (ValueError, KeyError):
            pass
    return {"addresses": addresses, "routes": routes}


def probe_targets() -> list[dict[str, Any]]:
    """对固定目标各建一次 TCP 连接，单目标 800 ms。"""
    results = []
    for host, port, name in PROBE_TARGETS:
        entry: dict[str, Any] = {"target": f"[{host}]:{port}", "name": name, "ok": False}
        try:
            with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_S):
                entry["ok"] = True
        except OSError as exc:
            entry["error"] = f"{type(exc).__name__}: {getattr(exc, 'strerror', exc)}"
        results.append(entry)
    return results


def has_default_route(routes: list[dict[str, Any]]) -> bool:
    """判断是否存在 IPv6 默认路由。"""
    for route in routes:
        dest = str(route.get("destination") or "")
        if dest in {"::/0", "default", "0:0:0:0:0:0:0:0/0"}:
            return True
    return False


def summarize(side: dict[str, Any], probes: list[dict[str, Any]] | None) -> dict[str, Any]:
    """汇总某一端的 L1 能力判定。"""
    addresses = side.get("addresses") or []
    kinds: dict[str, int] = {}
    for item in addresses:
        kinds[item.get("kind", "invalid")] = kinds.get(item.get("kind", "invalid"), 0) + 1
    global_addrs = [a for a in addresses if a.get("kind") == "global"]
    ula_addrs = [a for a in addresses if a.get("kind") == "ula"]
    cn_prefix_hit = [a for a in addresses if _in_cn_prefix(a.get("address", ""))]
    default_route = has_default_route(side.get("routes") or [])
    probe_measured = probes is not None
    probe_ok = sum(1 for p in (probes or []) if p.get("ok"))
    usable = bool(global_addrs) and default_route and (probe_ok >= 1 if probe_measured else True)
    reason = ""
    if not global_addrs:
        reason = "no_global_address"
    elif not default_route:
        reason = "no_default_route"
    elif probe_measured and probe_ok == 0:
        reason = "global_unreachable"
    return {
        "counts": kinds,
        "global_addresses": [a.get("address") for a in global_addrs],
        "ula_addresses": [a.get("address") for a in ula_addrs],
        "cn_isp_prefix_addresses": [a.get("address") for a in cn_prefix_hit],
        "default_route": default_route,
        "probe_measured": probe_measured,
        "probe_ok": probe_ok,
        "probe_total": len(probes or []),
        "l1_capable": usable,
        "reason": reason,
    }


def _in_cn_prefix(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in net for net in CN_ISP_NETS)


def collect_guest(target: str) -> dict[str, Any]:
    """通过 SSH 只读采集被控端；不在被控端落任何文件。"""
    if not target:
        return {"status": "not_requested"}
    code, out = run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=accept-new",
            target,
            "ip -6 -o addr show 2>/dev/null | awk '{print $2,$4}'; echo '---routes---'; ip -6 route show 2>/dev/null",
        ],
        timeout=15.0,
    )
    if code != 0:
        return {"status": "unreachable", "target": target, "detail": out.strip()[:200]}
    addresses: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    section = "addr"
    for line in out.splitlines():
        line = line.strip()
        if line == "---routes---":
            section = "route"
            continue
        if not line:
            continue
        if section == "addr":
            parts = line.split()
            if len(parts) < 2:
                continue
            iface, cidr = parts[0], parts[1]
            ip, _, plen = cidr.partition("/")
            addresses.append(
                {
                    "address": ip,
                    "prefix_len": int(plen) if plen.isdigit() else None,
                    "kind": classify(ip),
                    "iface": iface,
                }
            )
        else:
            parts = line.split()
            dest = parts[0] if parts else ""
            routes.append(
                {
                    "destination": dest,
                    "next_hop": parts[2] if "via" in parts and len(parts) > 2 else "",
                    "iface": parts[4] if "dev" in parts and len(parts) > 4 else "",
                }
            )
    return {"status": "collected", "target": target, "addresses": addresses, "routes": routes}


def vmnet_ipv6_support(conf: Path) -> dict[str, Any]:
    """只读检查 VMware NAT 配置是否声明 IPv6（用于解释虚机侧受限原因）。"""
    if os.name != "nt":
        return {"checked": False, "reason": "not_windows"}
    if not conf.is_file():
        return {"checked": False, "reason": "config_missing", "path": str(conf)}
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"checked": False, "reason": "config_unreadable", "path": str(conf)}
    ipv6_lines = [
        line.strip()
        for line in text.splitlines()
        if "ipv6" in line.lower() and line.strip() and not line.strip().startswith("#")
    ]
    return {"checked": True, "path": str(conf), "ipv6_lines": ipv6_lines, "ipv6_configured": bool(ipv6_lines)}


def main() -> int:
    parser = argparse.ArgumentParser(description="IPv6 直连能力只读探测")
    parser.add_argument("--guest", default="", help="被控端 SSH 目标，如 wuge@192.168.37.132")
    parser.add_argument("--out", default="", help="结果 JSON 路径")
    parser.add_argument(
        "--vmnet-conf",
        default=r"C:\ProgramData\VMware\vmnetnat.conf",
        help="VMware NAT 配置路径，仅用于只读判定虚机侧 IPv6 支持",
    )
    args = parser.parse_args()

    system = platform.system().lower()
    if system == "windows":
        side = collect_windows()
    elif system == "linux":
        side = collect_linux()
    else:
        side = {"addresses": [], "routes": []}

    probes = probe_targets()
    host = summarize(side, probes)
    host.update({"platform": system, "probes": probes, "snapshot": side})
    guest_raw = collect_guest(args.guest)
    guest = None
    if guest_raw.get("status") == "collected":
        guest = summarize(guest_raw, None)
        guest.update({"target": args.guest})
    vmnet = vmnet_ipv6_support(Path(args.vmnet_conf))

    if host["l1_capable"] and guest is not None and guest["l1_capable"]:
        verdict, blocked_layer = "capable", ""
    elif not host["l1_capable"] and host["reason"] == "no_global_address":
        verdict, blocked_layer = "blocked", "host_no_global_prefix"
    elif not host["l1_capable"] and host["reason"] == "no_default_route":
        verdict, blocked_layer = "blocked", "host_no_ipv6_default_route"
    elif not host["l1_capable"]:
        verdict, blocked_layer = "blocked", "host_global_unreachable"
    elif guest is None:
        verdict, blocked_layer = "blocked", "guest_unverified"
    else:
        verdict, blocked_layer = "blocked", "guest_not_l1_capable"

    result = {
        "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "scope": "ipv6_l1_capability_probe_readonly",
        "criteria": {
            "global": "2000::/3 且不含 2001:db8::/32",
            "excluded": ["fc00::/7", "fe80::/10", "::1"],
            "probe_targets": [f"[{h}]:{p}" for h, p, _ in PROBE_TARGETS],
            "probe_timeout_s": PROBE_TIMEOUT_S,
            "l1_capable": "≥1 全局地址 且 有 ::/0 默认路由 且 ≥1 目标 TCP 建连成功",
        },
        "host": host,
        "guest": guest if guest is not None else guest_raw,
        "virtualization": vmnet,
        "verdict": verdict,
        "blocked_layer": blocked_layer,
        "direct_connect": {
            "run": False,
            "reason": "本次只做能力探测；未建立会话、未出画面、未采集输入，不得记为直连通过",
        },
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    sys.stdout.write(text + "\n")
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0 if verdict == "capable" else 1


if __name__ == "__main__":
    sys.exit(main())
