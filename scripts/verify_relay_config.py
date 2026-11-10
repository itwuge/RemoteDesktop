# ==========================================================================================
# 文件        : verify_relay_config.py
# 所属目录    : scripts/
# 文件用途    : 中继配置漂移检查: 仓库模板与服务器实际生效配置对安全基线逐项比对
# 架构定位    : 验收门禁的静态检查, 不连接服务器, 只读配置文件
# 核心功能    : TLS/端口/预算/并发上限的基线校验; 模板与生效配置双向比对
# 职责边界    : 不修改任何配置; 取不到生效配置时记为受阻并写明责任方与时限
# ==========================================================================================

"""中继配置漂移检查.

用法:
    python scripts/verify_relay_config.py
    python scripts/verify_relay_config.py --snapshot evidence/relay-effective/relay.conf

基线(安全项):
    [tls].tls_enabled      = true
    [server].port          = 8750
    [relay].egress_mbps    = 3
    [relay].max_sessions   = 1
    [server].max_connections = 64
    [server].health_bind   = 127.0.0.1

服务器实际生效配置来源(按优先级):
    1. --snapshot 指定文件
    2. 环境变量 RDC_RELAY_CONF_SNAPSHOT
    3. evidence/relay-effective/relay.conf (由部署线在服务器上导出)

退出码: 0 无漂移; 1 存在漂移; 2 缺生效配置快照(受阻).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
TEMPLATE = ROOT / "deploy" / "relay" / "relay.conf.example"
DEFAULT_SNAPSHOT = EVIDENCE / "relay-effective" / "relay.conf"
TZ = timezone(timedelta(hours=8))

SECURITY_BASELINE: dict[tuple[str, str], tuple[str, str]] = {
    ("tls", "tls_enabled"): ("true", "公网中继必须启用 TLS 1.3, 明文不得上线"),
    ("server", "port"): ("8750", "对外端口必须与客户端固定端口一致"),
    ("server", "health_bind"): ("127.0.0.1", "健康检查只能监听回环"),
    ("server", "max_connections"): ("64", "连接上限, 3 Mbps 档不得超过 64"),
    ("relay", "egress_mbps"): ("3", "出口带宽是预算唯一来源, 实测 3 Mbps 不得上调"),
    ("relay", "max_sessions"): ("1", "3 Mbps 只承载 1 路 720p30, 并发上限必须是 1"),
}

SNAPSHOT_HINT = (
    "生效配置快照缺失。由部署线在恢复 TLS 的同一窗口执行: "
    "scp root@47.109.32.249:/etc/rdc-relay/relay.conf evidence/relay-effective/relay.conf; "
    "文件落盘后本项自动转为实检。责任方: 部署线; 时限: 与 TLS 恢复同批(2026-11-10 内)"
)


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def parse_conf(path: Path) -> dict[str, dict[str, str]]:
    """解析 [section] / key = value 形式; 注释与空行忽略."""
    sections: dict[str, dict[str, str]] = {}
    current = ""
    if not path.is_file():
        return sections
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if "=" in line and current:
            key, value = line.split("=", 1)
            sections[current][key.strip()] = value.strip()
    return sections


def _drift(sections: dict[str, dict[str, str]], label: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for (section, key), (expected, reason) in SECURITY_BASELINE.items():
        actual = sections.get(section, {}).get(key, "<缺失>")
        items.append(
            {
                "source": label,
                "section": section,
                "key": key,
                "expected": expected,
                "actual": actual,
                "ok": actual.lower() == expected.lower(),
                "reason": reason,
            }
        )
    return items


def _write(payload: dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = EVIDENCE / f"relay-config-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "check.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 中继配置漂移检查",
        "",
        f"- 时间: {payload['started_at']} (Asia/Shanghai)",
        f"- 模板: {payload['template']}",
        f"- 生效配置: {payload['snapshot'] or '未取得'}",
        "",
        "## 模板 vs 安全基线",
        "",
    ]
    for item in payload["template_drift"]:
        mark = "一致" if item["ok"] else "漂移"
        lines.append(f"- [{mark}] [{item['section']}] {item['key']}: 期望 {item['expected']}, 实际 {item['actual']}")
    if payload["effective_drift"]:
        lines.extend(["", "## 生效配置 vs 安全基线", ""])
        for item in payload["effective_drift"]:
            mark = "一致" if item["ok"] else "漂移"
            lines.append(
                f"- [{mark}] [{item['section']}] {item['key']}: 期望 {item['expected']}, 实际 {item['actual']}"
            )
    else:
        lines.extend(["", "## 生效配置", "", f"- {payload['snapshot_hint']}"])
    lines.extend(["", f"## 总判定: {payload['verdict']}"])
    (target / "结论.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="中继配置漂移检查")
    parser.add_argument("--snapshot", default="")
    arguments = parser.parse_args(argv)
    snapshot_path = Path(arguments.snapshot or os.environ.get("RDC_RELAY_CONF_SNAPSHOT") or str(DEFAULT_SNAPSHOT))
    template_sections = parse_conf(TEMPLATE)
    template_drift = _drift(template_sections, "template")
    effective_drift: list[dict[str, Any]] = []
    if snapshot_path.is_file():
        effective_drift = _drift(parse_conf(snapshot_path), "effective")
    drifted = [item for item in template_drift + effective_drift if not item["ok"]]
    template_bad = [item for item in template_drift if not item["ok"]]
    if drifted:
        verdict = f"不通过: {len(drifted)} 项漂移"
        if not snapshot_path.is_file():
            verdict += "(生效快照缺失, 仅覆盖仓库模板)"
        code = 1
    elif not snapshot_path.is_file():
        verdict = "受阻: 缺生效配置快照"
        code = 2
    else:
        verdict = "通过: 模板与生效配置均符合安全基线"
        code = 0
    payload = {
        "started_at": _now(),
        "template": str(TEMPLATE),
        "snapshot": str(snapshot_path) if snapshot_path.is_file() else "",
        "snapshot_hint": SNAPSHOT_HINT,
        "template_drift": template_drift,
        "effective_drift": effective_drift,
        "template_unsafe": bool(template_bad),
        "verdict": verdict,
    }
    target = _write(payload)
    for item in template_bad:
        sys.stdout.write(f"模板漂移: [{item['section']}] {item['key']} 期望 {item['expected']} 实际 {item['actual']}\n")
    for item in effective_drift:
        if not item["ok"]:
            sys.stdout.write(
                f"生效配置漂移: [{item['section']}] {item['key']} 期望 {item['expected']} 实际 {item['actual']}\n"
            )
    sys.stdout.write(f"判定: {verdict}\n证据: {target}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
