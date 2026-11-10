# ==========================================================================================
# 文件        : verify_all.py
# 所属目录    : scripts/
# 文件用途    : 单命令验收门禁入口: 统一门禁 -> V1 冒烟 -> 关键实测子集 -> 证据索引
# 架构定位    : 验收入口, 只编排既有阶段, 不复制其判定逻辑
# 核心功能    : 阶段编排、受阻项登记、证据归集与 SHA-256 索引、非零退出
# 职责边界    : 不改网络与服务; 不重启机器; 不把未验证项算作通过
# ==========================================================================================

"""单命令验收门禁入口.

用法:
    python scripts/verify_all.py
    python scripts/verify_all.py --skip-gate --skip-smoke
    python scripts/verify_all.py --with-bench --duration 8
    python scripts/verify_all.py --skip-update --skip-relay

阶段:
    1. 统一门禁   scripts/gate.py           规范、格式、禁止写法、结构、类型、单测
    2. V1 冒烟    scripts/smoke.py          命令层、信令、界面七页、控制闭环、远程输入
    3. 关键实测   scripts/e2e_remote_control.py   真实中继 + 真实输入注入 + 三方取证
                  scripts/e2e_release.py          更新检查、下载、SHA-256 校验、拒绝降级
                  scripts/verify_relay_transport.py  TLS 1.3 必须成立, 同端口明文必须被拒
                  scripts/verify_relay_config.py     生效配置与仓库模板的 6 项安全基线必须一致
                  scripts/verify_relay_auth_boundary.py  A1: 未认证分配必须 AUTH_FAILED
                  scripts/verify_relay_a5_a6.py      A5 重放去重; A6 先建正向对照再测越权
                  scripts/relay_public_bench.py   公网中继握手探测(默认端点 47.109.32.249:8750)
    4. 证据索引   evidence/verify/<时间戳>/index.json 与 index.md

退出码: 任一阶段失败返回 1; 全部通过返回 0; 环境受阻项单独列出, 不计为通过.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
HASH_CHUNK = 1024 * 1024


@dataclass
class Stage:
    """一个待执行阶段; blocked 非空表示环境受阻."""

    name: str
    command: list[str] = field(default_factory=list)
    blocked: str = ""
    source: Path | None = None
    copy_to: str = ""
    blocked_exit_code: int = 0
    blocked_detail: str = ""
    result: dict[str, Any] = field(default_factory=dict)


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    build_root = Path(env.get("RDC_BUILD_ROOT") or DEFAULT_BUILD_ROOT)
    env["RDC_BUILD_ROOT"] = str(build_root if build_root.is_dir() else ROOT)
    if build_root.is_dir():
        env["RDC_LOG_DIR"] = str(build_root / "logs" / "rdc")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    signing_key = env.get("RDC_UPDATE_SIGNING_KEY", "").strip()
    default_key = build_root / "keys" / "update-signing-ed25519.pem"
    if not signing_key and default_key.is_file():
        env["RDC_UPDATE_SIGNING_KEY"] = str(default_key)
    parts = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]
    pylibs = build_root / "tools" / "pylibs"
    if pylibs.is_dir():
        parts.insert(0, str(pylibs))
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_stage(stage: Stage, log_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    _emit("")
    _emit("=" * 100)
    _emit(f"== {stage.name} ==")
    if stage.blocked:
        _emit(f"-- 受阻: {stage.blocked}")
        return {"name": stage.name, "status": "受阻", "exit_code": None, "elapsed_s": 0.0, "detail": stage.blocked}
    _emit("命令: " + " ".join(stage.command))
    started = time.perf_counter()
    log_path = log_dir / (stage.copy_to + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            stage.command,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is not None:
            for line in process.stdout:
                sys.stdout.write(line)
                handle.write(line)
        code = process.wait()
    elapsed = round(time.perf_counter() - started, 2)
    if code == 0:
        status = "通过"
    elif stage.blocked_exit_code and code == stage.blocked_exit_code:
        status = "受阻"
    else:
        status = "失败"
    _emit(f"-- {stage.name} {status}, 退出码 {code}, 用时 {elapsed}s")
    detail = ""
    if status == "受阻":
        detail = stage.blocked_detail or "环境受阻(退出码 2)"
    elif status == "失败":
        detail = f"见 {log_path}"
    return {
        "name": stage.name,
        "status": status,
        "exit_code": code,
        "elapsed_s": elapsed,
        "detail": detail,
    }


def _latest_dir(root: Path, since: float) -> Path | None:
    if not root.is_dir():
        return None
    candidates = [item for item in root.iterdir() if item.is_dir() and item.stat().st_mtime >= since - 2]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _copy(source: Path | None, target: Path) -> None:
    if source is None or not source.is_dir():
        return
    shutil.copytree(source, target, dirs_exist_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_root(env: dict[str, str]) -> Path:
    return Path(env["RDC_BUILD_ROOT"])


def _update_blocked(build_root: Path, env: dict[str, str]) -> str:
    desktop = build_root / "artifacts" / "desktop"
    setup = sorted(desktop.glob("rdc-desktop-*-windows-x64-setup.exe"))
    portable = sorted(desktop.glob("rdc-desktop-*-windows-x64-portable.zip"))
    if not setup or not portable:
        return "构建根缺少 Windows 安装包/便携包, 先执行 scripts/build_windows.py"
    if not env.get("RDC_UPDATE_SIGNING_KEY", "").strip():
        return "未配置 RDC_UPDATE_SIGNING_KEY 且构建根缺少默认签名私钥, 更新清单无法签名"
    return ""


PUBLIC_RELAY_HOST = "47.109.32.249"
PUBLIC_RELAY_PORT = 8750


def _relay_target() -> tuple[str, int]:
    """公网中继地址: 环境变量优先, 否则用仓库固化的公网端点, 不再以未设置搁置."""
    host = os.environ.get("RDC_RELAY_HOST", "").strip() or PUBLIC_RELAY_HOST
    port_text = os.environ.get("RDC_RELAY_PORT", "").strip()
    port = int(port_text) if port_text.isdigit() else PUBLIC_RELAY_PORT
    return host, port


CONFIG_SNAPSHOT_HINT = (
    "缺服务器生效配置快照; 由部署线在恢复 TLS 的同一窗口执行 "
    "scp root@47.109.32.249:/etc/rdc-relay/relay.conf evidence/relay-effective/relay.conf, "
    "落盘后本项自动转为实检"
)

# 已绑定生产账号的设备身份: 存在即自动用于 A6 正向对照, 不需要人工加开关
A6_SEED_FILE = ROOT / "evidence" / "relay-a6-fixtures" / "fixtures.json"


def _stage_specs(arguments: argparse.Namespace, verify_dir: Path, env: dict[str, str]) -> list[Stage]:
    build_root = _build_root(env)
    stages: list[Stage] = []
    if not arguments.skip_gate:
        stages.append(Stage("统一门禁", [sys.executable, "scripts/gate.py"], copy_to="gate"))
    if not arguments.skip_smoke:
        stages.append(Stage("V1 冒烟", [sys.executable, "scripts/smoke.py"], copy_to="smoke"))
    if not arguments.skip_remote_input:
        stages.append(
            Stage(
                "远程输入实测",
                [sys.executable, "scripts/e2e_remote_control.py", "--evidence", str(verify_dir / "remote_input")],
                copy_to="remote_input",
            )
        )
    if not arguments.skip_update:
        stages.append(
            Stage(
                "更新闭环实测",
                [sys.executable, "scripts/e2e_release.py"],
                blocked=_update_blocked(build_root, env),
                copy_to="update",
            )
        )
    host, port = _relay_target()
    if not arguments.skip_transport:
        stages.append(
            Stage(
                "传输层 TLS/明文",
                [
                    sys.executable,
                    "scripts/verify_relay_transport.py",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                copy_to="relay_transport",
                blocked_exit_code=2,
                blocked_detail=f"公网中继 {host}:{port} 不可达, TLS 与明文均无法探测",
            )
        )
    if not arguments.skip_config:
        stages.append(
            Stage(
                "配置漂移",
                [sys.executable, "scripts/verify_relay_config.py"],
                copy_to="relay_config",
                blocked_exit_code=2,
                blocked_detail=CONFIG_SNAPSHOT_HINT,
            )
        )
    if not arguments.skip_auth_probe:
        stages.append(
            Stage(
                "中继鉴权边界",
                [
                    sys.executable,
                    "scripts/verify_relay_auth_boundary.py",
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
                copy_to="relay_auth_boundary",
                blocked_exit_code=2,
                blocked_detail="公网中继不可达或单名额被占用, 合法对照未执行",
            )
        )
    if not arguments.skip_a5a6:
        a5a6_command = [
            sys.executable,
            "scripts/verify_relay_a5_a6.py",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if A6_SEED_FILE.is_file():
            a5a6_command.extend(["--state", str(A6_SEED_FILE)])
        stages.append(
            Stage(
                "A5/A6 独立交叉校验",
                a5a6_command,
                copy_to="relay_a5_a6",
                blocked_exit_code=2,
                blocked_detail="公网中继不可达, A5/A6 公网段未执行(本机对照不受影响)",
            )
        )
    if not arguments.skip_relay:
        stages.append(
            Stage(
                "公网中继探测",
                [
                    sys.executable,
                    "scripts/relay_public_bench.py",
                    "probe",
                    "--work",
                    str(verify_dir / "relay_probe"),
                    "--relay-host",
                    host,
                    "--relay-port",
                    str(port),
                ],
                copy_to="relay_probe",
            )
        )
    if arguments.with_bench:
        stages.append(
            Stage(
                "性能基准",
                [sys.executable, "scripts/bench.py", "--duration", str(arguments.duration)],
                copy_to="bench",
            )
        )
    return stages


def _smoke_summary(smoke_dir: Path | None) -> dict[str, Any]:
    if smoke_dir is None:
        return {}
    payload = _read_json(smoke_dir / "smoke.json")
    items = payload.get("items") or []
    not_passed = [
        {
            "key": item.get("key"),
            "title": item.get("title"),
            "status": item.get("status"),
            "message": item.get("message"),
        }
        for item in items
        if isinstance(item, dict) and item.get("status") != "已实测通过"
    ]
    return {
        "evidence": str(smoke_dir),
        "counts": payload.get("counts", {}),
        "control_loop_passed": payload.get("control_loop_passed"),
        "editor_control_passed": payload.get("editor_control_passed"),
        "elapsed_s": payload.get("elapsed_s"),
        "not_passed": not_passed,
    }


def _remote_summary(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return {}
    latency = payload.get("latency") or {}
    return {
        "evidence": str(path.parent),
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "assertions": payload.get("assertions"),
        "one_way": latency.get("one_way"),
        "injection": latency.get("injection"),
        "round_trip": latency.get("round_trip"),
        "caliber": payload.get("caliber"),
    }


def _update_summary(update_dir: Path | None) -> dict[str, Any]:
    if update_dir is None:
        return {}
    payload = _read_json(update_dir / "result.json")
    return {
        "evidence": str(update_dir),
        "passed": payload.get("passed"),
        "current_version": payload.get("current_version"),
        "channels": sorted({item.get("step", "") for item in payload.get("steps", []) if isinstance(item, dict)}),
        "remote_actions_executed": payload.get("remote_actions_executed"),
    }


def _auth_probe_summary(probe_dir: Path | None) -> dict[str, Any]:
    if probe_dir is None:
        return {}
    payload = _read_json(probe_dir / "probe.json")
    return {
        "evidence": str(probe_dir),
        "response_type": payload.get("response_type"),
        "error": payload.get("error"),
        "allocated": payload.get("allocated"),
        "passed": payload.get("passed"),
    }


def _relay_summary(relay_dir: Path | None) -> dict[str, Any]:
    if relay_dir is None:
        return {}
    payload = _read_json(relay_dir / "probe.json")
    return {
        "evidence": str(relay_dir),
        "tls": payload.get("tls"),
        "verify_mode": payload.get("verify_mode"),
        "budget_kbps": payload.get("budget_kbps"),
        "quality_profile": payload.get("quality_profile"),
        "phases_ms": payload.get("phases_ms"),
    }


def _artifact_inventory(build_root: Path) -> list[dict[str, Any]]:
    artifacts = build_root / "artifacts"
    if not artifacts.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(artifacts.rglob("*")):
        if not path.is_file() or not path.name.endswith((".exe", ".zip", ".deb", ".apk", ".tar.gz", ".dmg")):
            continue
        if "_internal" in path.parts:
            continue
        rows.append(
            {
                "path": str(path),
                "size": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
                "sha256": _sha256(path),
            }
        )
    return rows


def _write_index(verify_dir: Path, summary: dict[str, Any]) -> tuple[Path, Path]:
    files: list[dict[str, Any]] = []
    for path in sorted(verify_dir.rglob("*")):
        if not path.is_file() or path.name in {"index.json", "index.md"}:
            continue
        stat = path.stat()
        files.append(
            {
                "path": path.relative_to(verify_dir).as_posix(),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "sha256": _sha256(path),
            }
        )
    index = {**summary, "files": files}
    json_path = verify_dir / "index.json"
    json_path.write_text(_dump(index), encoding="utf-8")

    lines = [
        f"# 验收证据索引 {summary['generated_at']}",
        "",
        f"- 结论: {summary['verdict']}",
        f"- 用时: {summary['elapsed_s']}s",
        f"- 入口: `{summary['command']}`",
        "",
        "## 阶段",
        "",
        "| 阶段 | 状态 | 退出码 | 用时(s) | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for stage in summary["stages"]:
        detail = stage.get("detail", "")
        lines.append(
            f"| {stage['name']} | {stage['status']} | {stage['exit_code']} | {stage['elapsed_s']} | {detail} |"
        )
    lines.extend(["", "## 受阻与未验证", ""])
    blocked = summary.get("blocked") or []
    if blocked:
        lines.extend(f"- 受阻: {item}" for item in blocked)
    not_passed = (summary.get("smoke") or {}).get("not_passed") or []
    if not_passed:
        lines.extend(f"- 冒烟未通过项: {item['title']}（{item['status']}）{item['message']}" for item in not_passed)
    if not blocked and not not_passed:
        lines.append("- 无")
    lines.extend(["", "## 文件", "", "| 文件 | 字节 | 采集时间 | SHA-256 |", "| --- | --- | --- | --- |"])
    for item in files:
        lines.append(f"| {item['path']} | {item['size']} | {item['modified']} | {item['sha256']} |")
    md_path = verify_dir / "index.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _print_summary(summary: dict[str, Any], index_path: Path) -> None:
    _emit("")
    _emit("=" * 100)
    _emit(f"验收门禁汇总  {summary['generated_at']}  用时 {summary['elapsed_s']}s")
    _emit("=" * 100)
    _emit(f"{'阶段':<14}{'状态':<8}{'退出码':<8}{'用时(s)':<10}说明")
    for stage in summary["stages"]:
        code = "-" if stage["exit_code"] is None else str(stage["exit_code"])
        _emit(f"{stage['name']:<14}{stage['status']:<8}{code:<8}{stage['elapsed_s']:<10}{stage.get('detail', '')}")
    smoke = summary.get("smoke") or {}
    counts = smoke.get("counts") or {}
    if counts:
        _emit(
            "冒烟计数: "
            + ", ".join(f"{key} {value}" for key, value in counts.items())
            + f"; 控制闭环 {smoke.get('control_loop_passed')}; 编辑器取证 {smoke.get('editor_control_passed')}"
        )
    remote = summary.get("remote_input") or {}
    if remote:
        one_way = remote.get("one_way") or {}
        injection = remote.get("injection") or {}
        _emit(
            f"远程输入: {remote.get('status')}; 单向 p50 {one_way.get('p50')}ms/p95 {one_way.get('p95')}ms; "
            f"注入 p50 {injection.get('p50')}ms/p95 {injection.get('p95')}ms"
        )
    update = summary.get("update") or {}
    if update:
        _emit(f"更新闭环: passed={update.get('passed')}; 远端动作={update.get('remote_actions_executed')}")
    relay = summary.get("relay_probe") or {}
    if relay:
        _emit(
            f"公网中继: TLS={relay.get('tls')} 预算={relay.get('budget_kbps')}kbps 档位={relay.get('quality_profile')}"
        )
    auth_probe = summary.get("auth_probe") or {}
    if auth_probe:
        error = auth_probe.get("error") or {}
        if auth_probe.get("blocked"):
            verdict = "受阻(中继单名额被占用)"
        else:
            verdict = "通过" if auth_probe.get("passed") else "不通过"
        _emit(
            f"中继鉴权边界: 未认证响应={auth_probe.get('response_type')} 错误码={error.get('code')} "
            f"被分配={auth_probe.get('allocated')} 判定={verdict}"
        )
    blocked = summary.get("blocked") or []
    if blocked:
        _emit("受阻项(不计为通过):")
        for item in blocked:
            _emit(f"  - {item}")
    not_passed = smoke.get("not_passed") or []
    if not_passed:
        _emit("冒烟未通过项:")
        for item in not_passed:
            _emit(f"  - {item['title']}: {item['status']} · {item['message']}")
    _emit(f"证据索引: {index_path}")
    _emit(f"结论: {summary['verdict']}")


def _run(arguments: argparse.Namespace) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    verify_dir = EVIDENCE / "verify" / stamp
    verify_dir.mkdir(parents=True, exist_ok=True)
    env = _environment()
    started = time.perf_counter()
    wall_start = time.time()
    stages = _stage_specs(arguments, verify_dir, env)
    results: list[dict[str, Any]] = []
    for stage in stages:
        results.append(_run_stage(stage, verify_dir, env))

    build_evidence = _build_root(env) / "evidence"
    gate_dir = _latest_dir(build_evidence / "gate", wall_start) if not arguments.skip_gate else None
    smoke_dir = _latest_dir(build_evidence / "smoke", wall_start) if not arguments.skip_smoke else None
    update_dir = None
    if not arguments.skip_update and not _update_blocked(_build_root(env), env):
        update_dir = _latest_dir(EVIDENCE / "update", wall_start)
    relay_dir = verify_dir / "relay_probe" if _read_json(verify_dir / "relay_probe" / "probe.json") else None
    auth_probe_dir = None
    if any(item["name"] == "中继鉴权边界" and item["status"] in {"通过", "失败"} for item in results):
        candidates = [
            item
            for item in EVIDENCE.glob("relay-auth-boundary-*")
            if item.is_dir() and item.stat().st_mtime >= wall_start - 2
        ]
        auth_probe_dir = max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None
    _copy(auth_probe_dir, verify_dir / "relay_auth_boundary")
    _copy(gate_dir, verify_dir / "gate")
    _copy(smoke_dir, verify_dir / "smoke")
    _copy(update_dir, verify_dir / "update")

    smoke_summary = _smoke_summary(smoke_dir)
    remote_summary = _remote_summary(verify_dir / "remote_input" / "e2e.json")
    update_summary = _update_summary(update_dir)
    relay_summary = _relay_summary(relay_dir)
    auth_probe_summary = _auth_probe_summary(auth_probe_dir)
    auth_stage = next((item for item in results if item["name"] == "中继鉴权边界"), None)
    if auth_stage and auth_stage["status"] == "受阻" and auth_probe_summary:
        auth_probe_summary["blocked"] = True
    failures = sum(1 for item in results if item["status"] == "失败")
    blocked = [f"{item['name']}: {item['detail']}" for item in results if item["status"] == "受阻"]
    smoke_failures = int((smoke_summary.get("counts") or {}).get("失败", 0))
    if failures or smoke_failures:
        verdict = "失败"
    elif blocked:
        verdict = "通过(存在受阻项, 不计为通过)"
    else:
        verdict = "通过"
    summary: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "command": "python scripts/verify_all.py",
        "stages": results,
        "blocked": blocked,
        "smoke": smoke_summary,
        "remote_input": remote_summary,
        "update": update_summary,
        "relay_probe": relay_summary,
        "auth_probe": auth_probe_summary,
        "artifacts": _artifact_inventory(_build_root(env)),
        "verdict": verdict,
        "evidence": str(verify_dir),
        "caliber": "门禁/本机冒烟/本机关键实测; 公网与真机项按受阻登记, 未验证项不随汇总计为通过",
    }
    index_path, md_path = _write_index(verify_dir, summary)
    (EVIDENCE / "verify" / "LATEST.txt").write_text(str(verify_dir) + "\n", encoding="utf-8")
    _print_summary(summary, index_path)
    _emit(f"索引 Markdown: {md_path}")
    return 0 if verdict == "通过" else (1 if verdict == "失败" else 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单命令验收门禁入口")
    parser.add_argument("--duration", type=float, default=8.0, help="性能基准采集时长秒")
    parser.add_argument("--skip-gate", action="store_true", help="跳过统一门禁")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过 V1 冒烟")
    parser.add_argument("--skip-remote-input", action="store_true", help="跳过远程输入实测")
    parser.add_argument("--skip-update", action="store_true", help="跳过更新闭环实测")
    parser.add_argument("--skip-relay", action="store_true", help="跳过公网中继探测")
    parser.add_argument("--skip-auth-probe", action="store_true", help="跳过公网中继鉴权边界探测")
    parser.add_argument("--skip-transport", action="store_true", help="跳过公网中继 TLS/明文检查")
    parser.add_argument("--skip-config", action="store_true", help="跳过中继配置漂移检查")
    parser.add_argument("--skip-a5a6", action="store_true", help="跳过 A5 重放/A6 越权独立交叉校验")
    parser.add_argument("--with-bench", action="store_true", help="追加性能基准")
    return _run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
