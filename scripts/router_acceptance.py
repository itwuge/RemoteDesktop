# ==========================================================================================
# 文件        : router_acceptance.py
# 所属目录    : scripts/
# 文件用途    : 换路由器后的 10 分钟网络能力验收: WAN 公网性 / IPv6 / NAT 映射 / 端口保持 /
#               UPnP 与 NAT-PMP 映射协商 / hairpin 回环
# 架构定位    : 验收脚本, 只读为主; 复用 src/rdc/connect 的 STUN 与 IPv6 探测实现
# 职责边界    : 不修改现有打洞与策略代码; 只有显式传 --map-test 才创建临时映射(60s 租约并主动删除)
# 运行环境    : Python 3.12, Windows / macOS / Linux
# 依赖关系    : rdc.connect.stun (Binding 请求与 srflx)、rdc.connect.native (地址枚举与 v6 自检)
# ==========================================================================================

"""换路由器后的网络能力验收.

用法::

    python scripts/router_acceptance.py --work evidence/router-acceptance/<时间戳>
    python scripts/router_acceptance.py --work ... --gateway 192.168.31.1 --map-test

判定口径见 docs/design/p2p-route-eval-2026-11-10.md 的「换路由器后的验收清单」一节.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import itertools
import json
import re
import secrets
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SRC = str(ROOT / "src")
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_STS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
PORT_SAMPLE_COUNT = 6
PORT_SAMPLE_BASE = 50120


def _core() -> tuple[Any, Any]:
    """延迟导入产品代码, 避免脚本执行时要求已安装 rdc 包."""
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    from rdc.connect import native, stun

    return native, stun


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _local_ipv4() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return str(sock.getsockname()[0])
    except OSError:
        return ""
    finally:
        sock.close()


def _is_public_ipv4(address: str) -> tuple[bool, str]:
    """判定地址是否为可用于被外部直连的公网 IPv4."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, "地址解析失败"
    if ip.version != 4:
        return False, "非 IPv4"
    if ip in CGNAT_NET:
        return False, "落入 100.64.0.0/10 (运营商 CGNAT)"
    if ip.is_private:
        return False, "落入私有地址段"
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return False, "非公网可路由地址"
    return True, "公网 IPv4"


# -------------------------------- 1. WAN 公网性 --------------------------------


def check_wan() -> dict[str, Any]:
    _native, stun = _core()
    rows: list[dict[str, Any]] = []
    sock = stun.bind_udp_in_pool(socket.AF_INET)
    try:
        for server in stun.stun_servers():
            try:
                address, port, rtt = stun.query_server(sock, server)
                rows.append(
                    {"server": f"{server[0]}:{server[1]}", "mapped": f"{address}:{port}", "rtt_ms": round(rtt, 2)}
                )
            except (OSError, ValueError) as exc:
                rows.append({"server": f"{server[0]}:{server[1]}", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        sock.close()
    ok = [row for row in rows if "mapped" in row]
    result: dict[str, Any] = {"rows": rows, "success": len(ok), "total": len(rows)}
    if not ok:
        result.update({"verdict": "blocked", "reason": "全部 STUN 失败, 无法判定 WAN 地址"})
        return result
    public_ips = sorted({row["mapped"].rsplit(":", 1)[0] for row in ok})
    public_ip = public_ips[0]
    if len(public_ips) > 1:
        result.update(
            {
                "public_ip": public_ip,
                "public_ips": public_ips,
                "is_public": False,
                "reason": "多个 STUN 返回不同公网 IPv4, 出口不固定(多出口或多层 NAT)",
            }
        )
        return result
    public, reason = _is_public_ipv4(public_ip)
    result.update({"public_ip": public_ip, "public_ips": public_ips, "is_public": public, "reason": reason})
    return result


# -------------------------------- 2. IPv6 --------------------------------


def check_ipv6() -> dict[str, Any]:
    native, _stun = _core()
    addresses = [address for family, address in native.enumerate_unicast_addresses() if family == 6]
    probe = native.native_ipv6_probe()
    return {
        "addresses": addresses,
        "global_addresses": list(probe.global_addresses),
        "reachable_targets": list(probe.reachable_targets),
        "rtt_ms": probe.rtt_ms,
        "error": probe.error,
        "usable": probe.available,
        "note": "PD 与 SLAAC 无法从主机侧区分, 只见全局地址与出站结果; 前缀获取方式需看路由器 WAN 状态页",
    }


# -------------------------------- 3. NAT 映射与端口保持 --------------------------------


def check_nat_mapping(rounds: int = 3) -> dict[str, Any]:
    """同一 socket 向多个 STUN 连续查询, 比对映射是否随目的地址变化."""
    _native, stun = _core()
    sock = stun.bind_udp_in_pool(socket.AF_INET)
    local_port = int(sock.getsockname()[1])
    rows: list[dict[str, Any]] = []
    try:
        for index in range(rounds):
            for server in stun.stun_servers():
                try:
                    address, port, rtt = stun.query_server(sock, server)
                    rows.append(
                        {
                            "round": index + 1,
                            "server": f"{server[0]}:{server[1]}",
                            "mapped": f"{address}:{port}",
                            "rtt_ms": round(rtt, 2),
                        }
                    )
                except (OSError, ValueError) as exc:
                    rows.append(
                        {
                            "round": index + 1,
                            "server": f"{server[0]}:{server[1]}",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
    finally:
        sock.close()
    ok = [row for row in rows if "mapped" in row]
    mappings = sorted({row["mapped"] for row in ok})
    public_ips = sorted({row["mapped"].rsplit(":", 1)[0] for row in ok})
    verdict = "unknown"
    if len(public_ips) > 1:
        verdict = "multi-egress (公网 IP 随目的变化, 多层 NAT 或多出口)"
    elif len(mappings) == 1 and len(ok) >= 3:
        verdict = "endpoint-independent (锥形)"
    elif len(mappings) > 1:
        verdict = "address-or-port-dependent (对称型)"
    rtts = sorted(row["rtt_ms"] for row in ok)
    return {
        "local_port": local_port,
        "rows": rows,
        "unique_mappings": mappings,
        "public_ips": public_ips,
        "verdict": verdict,
        "rtt_p50_ms": rtts[len(rtts) // 2] if rtts else None,
    }


def check_port_preservation() -> dict[str, Any]:
    """连续本地端口各查一次 STUN, 观察公网端口是否保持或按固定步长变化."""
    _native, stun = _core()
    server = stun.stun_servers()[0]
    rows: list[dict[str, Any]] = []
    for offset in range(PORT_SAMPLE_COUNT):
        local_port = PORT_SAMPLE_BASE + offset
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", local_port))
        except OSError as exc:
            rows.append({"local_port": local_port, "error": f"bind: {exc}"})
            continue
        try:
            address, mapped_port, _rtt = stun.query_server(sock, server)
            rows.append(
                {
                    "local_port": local_port,
                    "mapped": f"{address}:{mapped_port}",
                    "delta": mapped_port - local_port,
                }
            )
        except (OSError, ValueError) as exc:
            rows.append({"local_port": local_port, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            sock.close()
    ok = [row for row in rows if "delta" in row]
    deltas = [row["delta"] for row in ok]
    preserved = sum(1 for delta in deltas if delta == 0)
    steps = {b - a for a, b in itertools.pairwise(deltas)}
    sequential = len(deltas) >= 2 and len(steps) == 1
    if ok and preserved == len(ok):
        verdict = "映射端口与本地端口一致 (直接命中, 预测无意义)"
    elif sequential:
        verdict = "映射端口按固定步长变化 (可预测)"
    elif ok:
        verdict = "映射端口无固定规律 (不可预测)"
    else:
        verdict = "unavailable"
    return {
        "rows": rows,
        "preserved": preserved,
        "samples": len(ok),
        "deltas": deltas,
        "verdict": verdict,
        "sample_servers": [f"{server[0]}:{server[1]}"],
    }


# -------------------------------- 4. UPnP / NAT-PMP / PCP --------------------------------


def _ssdp_once(st: str, dest: tuple[str, int], local_ip: str, timeout_s: float) -> list[dict[str, Any]]:
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {max(1, int(timeout_s))}\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    if dest[0] == SSDP_ADDR[0] and local_ip:
        # 多网卡主机默认组播出口可能落在虚拟网卡, 必须显式绑定物理网卡
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
    rows: list[dict[str, Any]] = []
    try:
        sock.sendto(message, dest)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                break
            text = data.decode("utf-8", "replace")
            if "200" not in text.splitlines()[0]:
                continue
            location = re.search(r"(?im)^location:\s*(\S+)", text)
            server = re.search(r"(?im)^server:\s*(.+)$", text)
            rows.append(
                {
                    "st": st,
                    "dest": "multicast" if dest[0] == SSDP_ADDR[0] else "unicast",
                    "from": addr[0],
                    "location": location.group(1) if location else "",
                    "server": server.group(1).strip() if server else "",
                }
            )
    except OSError:
        pass
    finally:
        sock.close()
    return rows


def _ssdp_discover(local_ip: str, gateway: str, timeout_s: float = 2.0) -> list[dict[str, Any]]:
    destinations = [SSDP_ADDR]
    if gateway:
        destinations.append((gateway, 1900))
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for st in SSDP_STS:
        for dest in destinations:
            for row in _ssdp_once(st, dest, local_ip, timeout_s):
                key = (row["from"], row["location"])
                if key in seen:
                    continue
                seen.add(key)
                found.append(row)
    return found


def _http_get(url: str, timeout_s: float = 3.0) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("仅支持 http 设备描述地址")
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout_s)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        response = conn.getresponse()
        return response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _soap_call(location: str, control_url: str, service_type: str, action: str, body: str) -> str:
    location_parts = urlsplit(location)
    control_parts = urlsplit(control_url)
    host = control_parts.hostname or location_parts.hostname
    port = control_parts.port or location_parts.port or 80
    path = control_parts.path or "/"
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service_type}">{body}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    conn = http.client.HTTPConnection(host, port, timeout=4.0)
    try:
        conn.request(
            "POST",
            path,
            body=envelope,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPAction": f'"{service_type}#{action}"',
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        return response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _mapping_entry(location: str, control: str, service_type: str, external_port: int) -> str:
    body = (
        "<NewRemoteHost></NewRemoteHost>"
        f"<NewExternalPort>{external_port}</NewExternalPort>"
        "<NewProtocol>UDP</NewProtocol>"
    )
    return _soap_call(location, control, service_type, "GetSpecificPortMappingEntry", body)


def check_upnp(map_test: bool, local_ip: str, gateway: str) -> dict[str, Any]:
    _native, stun = _core()
    responses = _ssdp_discover(local_ip, gateway)
    services: list[dict[str, Any]] = []
    for item in responses:
        if not item.get("location") or item["from"] != gateway:
            # 只认默认网关自己宣告的 IGD, 避免被局域网内其他 UPnP 设备误导
            continue
        try:
            xml = _http_get(item["location"])
            types = re.findall(r"<serviceType>([^<]+)</serviceType>", xml)
            controls = re.findall(r"<controlURL>([^<]+)</controlURL>", xml)
            wan = [
                (t, c)
                for t, c in zip(types, controls, strict=False)
                if "WANIPConnection" in t or "WANPPPConnection" in t
            ]
            services.append(
                {
                    "location": item["location"],
                    "wan_services": [t for t, _ in wan],
                    "controls": [c for _, c in wan],
                }
            )
        except (OSError, ValueError, http.client.HTTPException) as exc:
            services.append({"location": item["location"], "error": f"{type(exc).__name__}: {exc}"})
    result: dict[str, Any] = {
        "responses": responses,
        "gateway_responses": [item for item in responses if item.get("from") == gateway],
        "services": services,
        "available": any(service.get("wan_services") for service in services),
        "map_test": "skipped",
    }
    if not (map_test and result["available"]):
        return result
    service = next(item for item in services if item.get("wan_services"))
    local_sock = stun.bind_udp_in_pool(socket.AF_INET)
    local_port = int(local_sock.getsockname()[1])
    external_port = 50190 + secrets.randbelow(50)
    service_type = service["wan_services"][0]
    control = service["controls"][0]
    try:
        body = (
            "<NewRemoteHost></NewRemoteHost>"
            f"<NewExternalPort>{external_port}</NewExternalPort>"
            "<NewProtocol>UDP</NewProtocol>"
            f"<NewInternalPort>{local_port}</NewInternalPort>"
            f"<NewInternalClient>{local_ip}</NewInternalClient>"
            "<NewEnabled>1</NewEnabled>"
            "<NewPortMappingDescription>rdc-acceptance</NewPortMappingDescription>"
            "<NewLeaseDuration>60</NewLeaseDuration>"
        )
        added = _soap_call(service["location"], control, service_type, "AddPortMapping", body)
        result["add_response_ok"] = "AddPortMappingResponse" in added
        result["map_test"] = "added" if result["add_response_ok"] else "add_failed"
        result["external_port_requested"] = external_port
        entry = _mapping_entry(service["location"], control, service_type, external_port)
        result["entry_ok"] = "NewInternalPort" in entry
        result["entry_internal_port"] = re.search(r"<NewInternalPort>([^<]*)<", entry)
        result["entry_internal_port"] = (
            result["entry_internal_port"].group(1) if result["entry_internal_port"] else None
        )
        address, mapped_port, _rtt = stun.query_server(local_sock, stun.stun_servers()[0])
        result["outbound_srflx_after_add"] = f"{address}:{mapped_port}"
        result["outbound_srflx_note"] = "出向 SNAT 不必然复用静态映射, 该值不作为映射生效判据"
        delete_body = (
            "<NewRemoteHost></NewRemoteHost>"
            f"<NewExternalPort>{external_port}</NewExternalPort>"
            "<NewProtocol>UDP</NewProtocol>"
        )
        _soap_call(service["location"], control, service_type, "DeletePortMapping", delete_body)
        result["deleted"] = True
        after = _mapping_entry(service["location"], control, service_type, external_port)
        result["deleted_verified"] = "714" in after or "NoSuchEntryInArray" in after
    except (OSError, ValueError, http.client.HTTPException) as exc:
        result["map_test"] = f"error: {type(exc).__name__}: {exc}"
    finally:
        local_sock.close()
    return result


def check_natpmp_pcp(map_test: bool, gateway: str) -> dict[str, Any]:
    _native, stun = _core()
    result: dict[str, Any] = {"gateway": gateway}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    try:
        sock.sendto(b"\x00\x00", (gateway, 5351))
        data, _ = sock.recvfrom(64)
        if len(data) >= 12 and data[1] == 128:
            result["natpmp"] = {
                "result_code": int.from_bytes(data[2:4], "big"),
                "epoch_s": int.from_bytes(data[4:8], "big"),
                "external_ip": socket.inet_ntoa(data[8:12]),
            }
        else:
            result["natpmp"] = {"raw_hex": data.hex(), "note": "非 NAT-PMP 外部地址应答"}
    except OSError as exc:
        result["natpmp"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    try:
        sock.sendto(bytes([2, 0]) + b"\x00\x00" + (0).to_bytes(4, "big") + bytes(16), (gateway, 5351))
        data, _ = sock.recvfrom(64)
        result["pcp"] = {"raw_hex": data.hex(), "result_code": data[3] if len(data) >= 4 else None}
    except OSError as exc:
        result["pcp"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        sock.close()

    result["map_test"] = "skipped"
    natpmp = result.get("natpmp", {})
    if not (map_test and "external_ip" in natpmp):
        return result
    local_sock = stun.bind_udp_in_pool(socket.AF_INET)
    local_port = int(local_sock.getsockname()[1])
    requested = 50200 + secrets.randbelow(50)
    request = (
        b"\x00\x01\x00\x00" + local_port.to_bytes(2, "big") + requested.to_bytes(2, "big") + (60).to_bytes(4, "big")
    )
    local_sock.settimeout(1.5)
    try:
        # NAT-PMP 部分实现要求请求源端口等于内部端口, 因此复用绑定内部端口的 socket
        local_sock.sendto(request, (gateway, 5351))
        data, _ = local_sock.recvfrom(64)
        if len(data) >= 16:
            external_port = int.from_bytes(data[10:12], "big")
            result["map_test"] = {
                "result_code": int.from_bytes(data[2:4], "big"),
                "internal_port": int.from_bytes(data[8:10], "big"),
                "external_port": external_port,
                "lifetime_s": int.from_bytes(data[12:16], "big"),
            }
            cancel = (
                b"\x00\x01\x00\x00"
                + local_port.to_bytes(2, "big")
                + external_port.to_bytes(2, "big")
                + (0).to_bytes(4, "big")
            )
            local_sock.sendto(cancel, (gateway, 5351))
    except OSError as exc:
        result["map_test"] = f"error: {type(exc).__name__}: {exc}"
    finally:
        local_sock.close()
    return result


# -------------------------------- 5. hairpin --------------------------------


def check_hairpin() -> dict[str, Any]:
    """同一主机两个 socket: A 取映射, B 发到 A 的公网映射, 看 A 能否收到."""
    _native, stun = _core()
    target = stun.bind_udp_in_pool(socket.AF_INET)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        candidates, error = stun.discover_srflx(target)
        if not candidates:
            return {"supported": None, "error": error or "无 srflx"}
        mapped = candidates[0]
        target.settimeout(0.2)
        payload = b"RDCH1" + secrets.token_hex(4).encode("ascii")
        sent = 0
        for _ in range(10):
            try:
                sender.sendto(payload, (mapped.address, mapped.port))
                sent += 1
            except OSError:
                break
            time.sleep(0.05)
        received = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                data, _addr = target.recvfrom(2048)
            except OSError:
                continue
            if data.startswith(b"RDCH1"):
                received += 1
        return {
            "mapped": f"{mapped.address}:{mapped.port}",
            "sent": sent,
            "received": received,
            "supported": received > 0,
            "note": "同一主机自测; 部分路由器只对同网段不同主机放行 hairpin",
        }
    finally:
        target.close()
        sender.close()


# -------------------------------- 6. 结论 --------------------------------


def conclude(report: dict[str, Any]) -> dict[str, Any]:
    wan = report["wan"]
    nat = report["nat_mapping"]
    ipv6 = report["ipv6"]
    hairpin = report["hairpin"]
    upnp = report["upnp"]
    mapping_capable = bool(upnp.get("available")) or "external_ip" in report["natpmp_pcp"].get("natpmp", {})
    actions: list[str] = []
    if ipv6.get("usable"):
        actions.append("L1 IPv6 可优先: 保持 connect-v1.md 的 L1 先行, 复测两端 v6 直连")
    else:
        actions.append("L1 维持跳过: 本机未取得可用全局 IPv6, 继续 L2 -> L3")
    if wan.get("is_public") and str(nat.get("verdict", "")).startswith("endpoint-independent"):
        actions.append("L2 可优先尝试打洞: WAN 为公网 IPv4 且映射 endpoint-independent")
    elif not wan.get("is_public"):
        actions.append("L2 仅保留 host 候选: WAN 非公网(疑似 CGNAT), 公网 srflx 打洞不可依赖")
    else:
        actions.append("L2 预期不可穿: 映射随目的地址变化, 直接走 L3 中继")
    if hairpin.get("supported") is False:
        actions.append("同出口设备必须用 host 候选: 路由器不支持 hairpin, 禁止用 srflx 回环")
    if mapping_capable:
        actions.append("可把主动端口映射列为 L2 增强项: UPnP 或 NAT-PMP 可协商, 上线前需在真实对端复验")
    return {"actions": actions, "mapping_capable": mapping_capable}


def main() -> int:
    parser = argparse.ArgumentParser(description="换路由器后的网络能力验收")
    parser.add_argument("--work", default="", help="证据目录, 默认 evidence/router-acceptance/<时间戳>")
    parser.add_argument("--gateway", default="", help="网关地址, 用于 NAT-PMP/PCP; 留空按本机 /24 的 .1 推测")
    parser.add_argument("--map-test", action="store_true", help="创建临时映射验证协商能力(60s 租约并主动删除)")
    parser.add_argument("--skip-hairpin", action="store_true", help="跳过 hairpin 自测")
    args = parser.parse_args()

    work = Path(args.work) if args.work else ROOT / "evidence" / "router-acceptance" / time.strftime("%Y%m%d-%H%M%S")
    work.mkdir(parents=True, exist_ok=True)

    local_ip = _local_ipv4()
    gateway = args.gateway
    if not gateway and local_ip:
        gateway = str(ipaddress.ip_network(f"{local_ip}/24", strict=False).network_address + 1)

    report: dict[str, Any] = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "gateway_guess": gateway}
    emit("[1/6] WAN 公网性")
    report["wan"] = check_wan()
    emit("[2/6] IPv6 可用性")
    report["ipv6"] = check_ipv6()
    emit("[3/6] NAT 映射行为")
    report["nat_mapping"] = check_nat_mapping()
    emit("[4/6] 端口保持与可预测性")
    report["port_preservation"] = check_port_preservation()
    emit("[5/6] UPnP / NAT-PMP / PCP")
    report["upnp"] = check_upnp(args.map_test, local_ip, gateway)
    report["natpmp_pcp"] = check_natpmp_pcp(args.map_test, gateway) if gateway else {"error": "无网关地址"}
    emit("[6/6] hairpin 回环")
    report["hairpin"] = {"supported": None, "note": "已跳过"} if args.skip_hairpin else check_hairpin()
    report["conclusion"] = conclude(report)

    out = work / "result.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    emit("")
    emit(
        f"WAN          : {report['wan'].get('public_ip', '-')} "
        f"公网={report['wan'].get('is_public')} ({report['wan'].get('reason', '-')})"
    )
    emit(f"IPv6         : 可用={report['ipv6'].get('usable')} 全局地址={report['ipv6'].get('global_addresses')}")
    emit(f"NAT 映射     : {report['nat_mapping'].get('verdict')}")
    emit(f"  唯一映射    : {report['nat_mapping'].get('unique_mappings')}")
    emit(f"端口保持     : {report['port_preservation'].get('verdict')}")
    emit(f"  deltas      : {report['port_preservation'].get('deltas')}")
    emit(f"UPnP         : {report['upnp'].get('available')} 响应数={len(report['upnp'].get('responses', []))}")
    emit(f"NAT-PMP      : {report['natpmp_pcp'].get('natpmp')}")
    emit(f"PCP          : {report['natpmp_pcp'].get('pcp')}")
    emit(f"hairpin      : {report['hairpin']}")
    emit("")
    emit("策略结论:")
    for action in report["conclusion"]["actions"]:
        emit(f"  - {action}")
    emit("")
    emit(f"证据: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
