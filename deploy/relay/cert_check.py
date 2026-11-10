#!/usr/bin/env python3
"""证书到期检查：按剩余天数分级告警，供巡检或定时任务调用。

退出码: 0=OK, 1=WARN, 2=CRITICAL。只读配置与证书，不修改任何文件。
用法:
  python3 cert_check.py --config /etc/rdc-relay/relay.conf
  python3 cert_check.py --cert /etc/rdc-relay/tls/server.crt --warn-days 14 --critical-days 7
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
from pathlib import Path


def read_config(path: Path) -> dict[str, str]:
    """读取 key = value 形式的配置，忽略注释与空行。"""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def certificate_info(cert_file: str) -> tuple[int, str] | None:
    """返回 (剩余天数向下取整, 到期时间字符串)；无法解析返回 None。"""
    decoder = getattr(getattr(ssl, "_ssl", None), "_test_decode_cert", None)
    if decoder is None or not cert_file:
        return None
    try:
        info = decoder(cert_file)
        not_after = str(info["notAfter"])
        expires_at = ssl.cert_time_to_seconds(not_after)
    except (OSError, ssl.SSLError, KeyError, TypeError, ValueError):
        return None
    return int((expires_at - time.time()) // 86400), not_after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中继证书到期检查")
    parser.add_argument("--config", default="/etc/rdc-relay/relay.conf")
    parser.add_argument("--cert", default="", help="直接指定证书路径，优先于配置")
    parser.add_argument("--warn-days", type=int, default=14)
    parser.add_argument("--critical-days", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config: dict[str, str] = {}
    if config_path.exists():
        config = read_config(config_path)
    cert_file = args.cert or config.get("cert_file", "")
    tls_enabled = config.get("tls_enabled", "false").lower() == "true"

    if not args.cert and not tls_enabled:
        result = {"status": "OK", "tls_enabled": False, "message": "未启用 TLS，无需检查"}
        print(json.dumps(result, ensure_ascii=False) if args.json else "OK 未启用 TLS，无需检查")
        return 0

    info = certificate_info(cert_file)
    if info is None:
        result = {
            "status": "CRITICAL",
            "tls_enabled": tls_enabled,
            "cert_file": cert_file,
            "message": "证书缺失或无法解析",
        }
        print(json.dumps(result, ensure_ascii=False) if args.json else f"CRITICAL 证书缺失或无法解析: {cert_file}")
        return 2

    days_left, not_after = info
    if days_left <= args.critical_days:
        status, code = "CRITICAL", 2
    elif days_left <= args.warn_days:
        status, code = "WARN", 1
    else:
        status, code = "OK", 0

    result = {
        "status": status,
        "tls_enabled": tls_enabled,
        "cert_file": cert_file,
        "days_left": days_left,
        "not_after": not_after,
        "warn_days": args.warn_days,
        "critical_days": args.critical_days,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(
            f"{status} cert={cert_file} days_left={days_left} not_after={not_after} "
            f"(阈值 warn<={args.warn_days} critical<={args.critical_days})"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
