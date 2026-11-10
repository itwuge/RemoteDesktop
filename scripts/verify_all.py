# ==========================================================================================
# 文件        : verify_all.py
# 所属目录    : scripts/
# 文件用途    : 单命令全量验证入口: 依次执行统一门禁、V1 冒烟(含远程输入链路)与性能基准,
#               把各阶段证据复制到项目内 evidence/verify/<时间戳>/ 并输出统一汇总
# 架构定位    : 验收入口, 只编排既有阶段, 不复制其判定逻辑
# 核心功能    : 阶段执行与实时日志、证据归集、冒烟计数与远程输入延迟/基准指标提取、非零退出
# 职责边界    : 不改网络与服务; 不重启机器; 不把未验证项算作通过
# ==========================================================================================

"""单命令全量验证入口.

用法:
    powershell -File scripts/verify_all.ps1 [-Duration 8]
    python scripts/verify_all.py [--duration 8] [--skip-gate] [--skip-smoke] [--skip-bench]

证据: 项目内 evidence/verify/<时间戳>/verify.json 与各阶段证据副本.
退出码: 任一阶段失败返回 1, 全部通过返回 0.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    build_root = Path(r"D:\RDC-Build")
    env["RDC_BUILD_ROOT"] = str(build_root if build_root.is_dir() else ROOT)
    if build_root.is_dir():
        env["RDC_LOG_DIR"] = str(build_root / "logs" / "rdc")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    parts = [str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]
    pylibs = build_root / "tools" / "pylibs"
    if pylibs.is_dir():
        parts.insert(0, str(pylibs))
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _run_stage(name: str, command: list[str], log_path: Path, env: dict[str, str]) -> dict[str, Any]:
    _emit("")
    _emit("=" * 100)
    _emit(f"== {name} ==")
    _emit("=" * 100)
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command,
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
    _emit(f"-- {name} 退出码 {code}, 用时 {elapsed}s")
    return {"name": name, "exit_code": code, "elapsed_s": elapsed, "log": str(log_path)}


def _latest_dir(root: Path, since: float) -> Path | None:
    if not root.is_dir():
        return None
    candidates = [item for item in root.iterdir() if item.is_dir() and item.stat().st_mtime >= since - 2]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _copy(source: Path | None, target: Path) -> None:
    if source is None or not source.is_dir():
        return
    shutil.copytree(source, target, dirs_exist_ok=True)


def _smoke_summary(smoke_dir: Path | None) -> dict[str, Any]:
    if smoke_dir is None:
        return {}
    payload = _read_json(smoke_dir / "smoke.json")
    return {
        "evidence": str(smoke_dir),
        "counts": payload.get("counts", {}),
        "control_loop_passed": payload.get("control_loop_passed"),
        "editor_control_passed": payload.get("editor_control_passed"),
        "elapsed_s": payload.get("elapsed_s"),
    }


def _bench_summary(bench_dir: Path | None) -> dict[str, Any]:
    if bench_dir is None:
        return {}
    payload = _read_json(bench_dir / "bench.json")
    pipeline = payload.get("pipeline") or {}
    control = payload.get("input") or {}
    return {
        "evidence": str(bench_dir),
        "resolution": pipeline.get("resolution"),
        "fps_target": pipeline.get("fps_target"),
        "fps_actual": pipeline.get("fps_actual"),
        "capture_ms_p50": pipeline.get("capture_ms_p50"),
        "capture_ms_p95": pipeline.get("capture_ms_p95"),
        "encode_ms_p50": pipeline.get("encode_ms_p50"),
        "encode_ms_p95": pipeline.get("encode_ms_p95"),
        "decode_ms_p50": pipeline.get("decode_ms_p50"),
        "decode_ms_p95": pipeline.get("decode_ms_p95"),
        "ssim_p50": pipeline.get("ssim_p50"),
        "ssim_p95": pipeline.get("ssim_p95"),
        "cpu_percent_avg": pipeline.get("process_cpu_percent_avg"),
        "rss_peak_mib": pipeline.get("rss_peak_mib"),
        "input_p50_ms": control.get("p50_ms"),
        "input_p95_ms": control.get("p95_ms"),
        "unverified": payload.get("unverified", []),
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
        "findings": payload.get("findings", []),
    }


def _print_summary(summary: dict[str, Any]) -> None:
    _emit("")
    _emit("=" * 100)
    _emit(f"全量验证汇总  {summary['generated_at']}  用时 {summary['elapsed_s']}s")
    _emit("=" * 100)
    for stage in summary["stages"]:
        verdict = "通过" if stage["exit_code"] == 0 else "失败"
        _emit(f"{stage['name']:<12}{verdict:<6}退出码 {stage['exit_code']}  用时 {stage['elapsed_s']}s")
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
    bench = summary.get("bench") or {}
    if bench:
        _emit(
            f"基准: {bench.get('resolution')} {bench.get('fps_actual')}fps; "
            f"采集 p50 {bench.get('capture_ms_p50')}ms; 编码 p50 {bench.get('encode_ms_p50')}ms; "
            f"解码 p50 {bench.get('decode_ms_p50')}ms; SSIM p50 {bench.get('ssim_p50')}; "
            f"CPU {bench.get('cpu_percent_avg')}%; 内存峰值 {bench.get('rss_peak_mib')}MiB; "
            f"本机输入 p50 {bench.get('input_p50_ms')}ms"
        )
    _emit(f"证据: {summary['evidence']}")
    _emit(f"结论: {summary['verdict']}")


def _run(arguments: argparse.Namespace) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    verify_dir = EVIDENCE / "verify" / stamp
    verify_dir.mkdir(parents=True, exist_ok=True)
    env = _environment()
    started = time.perf_counter()
    wall_start = time.time()
    stages: list[dict[str, Any]] = []

    if not arguments.skip_gate:
        stages.append(_run_stage("统一门禁", [sys.executable, "scripts/gate.py"], verify_dir / "gate.log", env))
    if not arguments.skip_smoke:
        stages.append(_run_stage("V1 冒烟", [sys.executable, "scripts/smoke.py"], verify_dir / "smoke.log", env))
    if not arguments.skip_bench:
        stages.append(
            _run_stage(
                "性能基准",
                [sys.executable, "scripts/bench.py", "--duration", str(arguments.duration)],
                verify_dir / "bench.log",
                env,
            )
        )

    build_evidence = Path(env["RDC_BUILD_ROOT"]) / "evidence"
    gate_dir = _latest_dir(build_evidence / "gate", wall_start) if not arguments.skip_gate else None
    smoke_dir = _latest_dir(build_evidence / "smoke", wall_start) if not arguments.skip_smoke else None
    bench_dir = _latest_dir(build_evidence / "bench", wall_start) if not arguments.skip_bench else None
    _copy(gate_dir, verify_dir / "gate")
    _copy(smoke_dir, verify_dir / "smoke")
    _copy(bench_dir, verify_dir / "bench")

    remote_path = EVIDENCE / "smoke" / (smoke_dir.name if smoke_dir else "") / "remote_input" / "e2e.json"
    _copy(remote_path.parent if remote_path.is_file() else None, verify_dir / "remote_input")

    smoke_summary = _smoke_summary(smoke_dir)
    bench_summary = _bench_summary(bench_dir)
    remote_summary = _remote_summary(remote_path)
    failures = sum(1 for stage in stages if stage["exit_code"] != 0)
    smoke_failures = int((smoke_summary.get("counts") or {}).get("失败", 0))
    verdict = "通过" if failures == 0 and smoke_failures == 0 else "失败"
    summary: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.perf_counter() - started, 2),
        "command": "python scripts/verify_all.py",
        "stages": stages,
        "smoke": smoke_summary,
        "remote_input": remote_summary,
        "bench": bench_summary,
        "verdict": verdict,
        "evidence": str(verify_dir),
        "caliber": "门禁/本机冒烟/本机基准; 未验证项不随汇总计为通过",
    }
    (verify_dir / "verify.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(summary)
    return 0 if verdict == "通过" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单命令全量验证入口")
    parser.add_argument("--duration", type=float, default=8.0, help="性能基准采集时长秒")
    parser.add_argument("--skip-gate", action="store_true", help="跳过统一门禁")
    parser.add_argument("--skip-smoke", action="store_true", help="跳过 V1 冒烟")
    parser.add_argument("--skip-bench", action="store_true", help="跳过性能基准")
    return _run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
