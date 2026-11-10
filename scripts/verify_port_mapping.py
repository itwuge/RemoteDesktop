# ==========================================================================================
# 文件        : verify_port_mapping.py
# 所属目录    : scripts/
# 文件用途    : 对真实路由器执行 UPnP-IGD / NAT-PMP 端口映射验收, 并落盘证据 JSON
# 架构定位    : 真机验证脚本, 直接调用产品代码 src/rdc/connect/mapping.py, 不复刻实现
# 核心功能    : 发现 IGD -> 查外部地址并与 STUN 对齐 -> 申请映射 -> 读回校验 -> 续租 -> 删除 -> 复验
# 职责边界    : 只操作本次申请的外部端口; 不覆盖、不删除路由器上已有规则; 不修改任何服务端配置
# 运行环境    : Python 3.12; 需要与被控机同网段且路由器开启 UPnP 或 NAT-PMP
# 依赖关系    : rdc.connect.mapping, rdc.connect.stun
# ==========================================================================================

"""真实路由器端口映射验收.

用法:
    python scripts/verify_port_mapping.py
    python scripts/verify_port_mapping.py --gateway 192.168.31.1 --lease 120
    python scripts/verify_port_mapping.py --discover-only
    python scripts/verify_port_mapping.py --probe 47.109.32.249:3478

安全口径:
- 外部端口从 49152 以上随机取, 申请前先读回确认空闲, 已占用则换端口;
- 只删除本次创建且读回仍指向本进程内部端口的条目;
- 外部可达性只有在传入 --probe 且对端确有应答时才记为实测, 否则明确标注为未验证.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from _rdc_path import PROJECT_ROOT
from rdc.connect import mapping
from rdc.connect.stun import discover_srflx

ROOT = PROJECT_ROOT
DEFAULT_EVIDENCE = ROOT / "evidence" / "port-mapping"


def write(text: str = "") -> None:
    """统一输出入口; 门禁禁止 print."""
    sys.stdout.write(text + "\n")


def probe_udp(address: str, timeout_s: float = 2.0) -> bool:
    """向指定 UDP 端点发一个 STUN Binding 请求, 收到应答即视为外部可达."""
    host, _, port_text = address.partition(":")
    try:
        port = int(port_text)
    except ValueError:
        return False
    request = b"\x00\x01\x00\x00\x21\x12\xa4\x42" + b"rdc-probe"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout_s)
        sock.sendto(request, (host, port))
        data, _peer = sock.recvfrom(2048)
        return bool(data)
    except OSError:
        return False
    finally:
        sock.close()


def timed() -> float:
    return time.monotonic()


def main() -> int:
    parser = argparse.ArgumentParser(description="端口映射真机验收")
    parser.add_argument("--gateway", default="", help="默认网关, 留空按本机 /24 的 .1 推测")
    parser.add_argument("--lease", type=int, default=mapping.DEFAULT_LEASE_S, help="租期秒数")
    parser.add_argument("--external-port", type=int, default=0, help="指定外部端口, 0 表示随机")
    parser.add_argument("--discover-only", action="store_true", help="只做发现与外部地址对比")
    parser.add_argument("--probe", default="", help="外部探针 host:port, 用于验证映射端口的真实外部可达性")
    parser.add_argument("--json", default="", help="证据 JSON 路径, 留空写入 evidence/port-mapping/<时间戳>/")
    args = parser.parse_args()

    local_ip = mapping.local_ipv4()
    gateway = args.gateway or mapping.guess_gateway(local_ip)
    report: dict[str, Any] = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "local_ip": local_ip,
        "gateway": gateway,
        "lease_s": args.lease,
        "steps": [],
        "conclusion": "",
    }
    write(f"本机 {local_ip or '未知'}, 网关 {gateway or '未知'}")

    mapper = mapping.PortMapper(gateway=gateway, local_ip=local_ip)
    service = mapper.service()
    if service is None:
        report["conclusion"] = "未发现默认网关上的 IGD, 且无法继续"
        report["steps"].append({"step": "discover", "ok": False, "reason": mapping.MAPPING_UNSUPPORTED})
        _dump(report, args.json)
        write("未发现 IGD: 路由器可能关闭了 UPnP, 或网关不是默认网关")
        return 2
    report["steps"].append(
        {
            "step": "discover",
            "ok": True,
            "service_type": service.service_type,
            "control_url": service.control_url,
            "server": service.server,
        }
    )
    write(f"发现 IGD: {service.service_type} ({service.server or '无 server 头'})")
    write(f"控制地址: {service.control_url}")

    started = timed()
    external = mapper.external_ip()
    report["steps"].append(
        {
            "step": "external_address",
            "ok": True,
            "address": external,
            "duration_ms": round((timed() - started) * 1000, 3),
        }
    )
    write(f"IGD 外部地址: {external}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    any_v4 = ""
    sock.bind((any_v4, 0))
    internal_port = int(sock.getsockname()[1])
    report["internal_port"] = internal_port
    try:
        srflx, stun_error = discover_srflx(sock)
        stun_address = srflx[0].address if srflx else ""
        report["steps"].append(
            {
                "step": "stun",
                "ok": bool(stun_address),
                "address": stun_address,
                "error": stun_error,
            }
        )
        write(f"STUN 反射地址: {stun_address or '未取得'} ({stun_error or 'ok'})")
        if stun_address:
            write(f"外部地址一致性: {'一致' if stun_address == external else '不一致(疑似多层 NAT/CGNAT)'}")
        if args.discover_only:
            report["conclusion"] = "仅发现与外部地址检查完成"
            _finish(report, args.json)
            return 0

        outcome = mapper.map_udp_port(
            internal_port,
            internal_client=local_ip,
            lease_s=args.lease,
            description="rdc-verify",
            requested_external_port=args.external_port,
            sock=sock,
        )
        report["steps"].append({"step": "add_mapping", **outcome.as_event(), "ok": outcome.ok})
        if not outcome.ok:
            report["conclusion"] = f"映射失败: {outcome.reason} ({mapping.describe_reason(outcome.reason)})"
            _dump(report, args.json)
            write(f"映射失败: {outcome.reason} {mapping.describe_reason(outcome.reason)}")
            return 3
        write(
            f"映射成功: {outcome.protocol} {local_ip}:{internal_port} -> {outcome.external_address}:"
            f"{outcome.external_port} 租期 {outcome.lease_s}s"
        )

        reconciled = mapping.reconcile_with_stun(
            outcome,
            stun_address,
            probe=(lambda addr, port: probe_udp(f"{addr}:{port}")) if args.probe else None,
        )
        report["steps"].append(
            {
                "step": "reconcile",
                "ok": reconciled.ok,
                "reason": reconciled.reason,
                "verification": reconciled.verification,
            }
        )
        if not reconciled.ok:
            write(f"一致性判定未通过: {reconciled.reason} {mapping.describe_reason(reconciled.reason)}")
        elif reconciled.verification == mapping.VERIFY_EXTERNAL:
            write("外部探针已确认映射端口可从外网到达(实测)")
        else:
            write("外部可达性未做真外部探测, 仅完成地址一致性与条目读回校验(替代口径)")

        renewed = mapper.renew(outcome)
        report["steps"].append({"step": "renew", "ok": renewed.ok, "reason": renewed.reason})
        write(f"续租: {'成功' if renewed.ok else '失败 ' + mapping.describe_reason(renewed.reason)}")

        deleted = mapper.delete(outcome)
        remaining = mapper.read_entry(outcome.external_port)
        report["steps"].append(
            {"step": "delete", "ok": deleted and remaining is None, "deleted": deleted, "readback": remaining}
        )
        readback_text = "已无条目" if remaining is None else str(remaining)
        write(f"删除映射: {'已删除' if deleted else '删除未确认'}, 读回: {readback_text}")
        report["conclusion"] = "映射链路验收完成" if reconciled.ok and remaining is None else "映射链路存在未通过项"
        _finish(report, args.json)
        return 0 if reconciled.ok and remaining is None else 4
    finally:
        sock.close()


def _finish(report: dict[str, Any], path_text: str) -> None:
    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _dump(report, path_text)
    write(f"结论: {report['conclusion']}")


def _dump(report: dict[str, Any], path_text: str) -> None:
    """证据落盘; 未指定路径时写入带时间戳的目录."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(path_text) if path_text else DEFAULT_EVIDENCE / stamp / "result.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write(f"证据: {target}")


if __name__ == "__main__":
    raise SystemExit(main())
