# ==========================================================================================
# 文件        : verify_no_console_stability.py
# 所属目录    : scripts/
# 文件用途    : 带负载连续启动无控制台客户端, 记录存活时长、退出码、conhost 与系统错误事件
# 架构定位    : 稳定性验证工具; 只启动与观测, 不修改业务代码
# 核心功能    : 每轮同时跑 pytest 与 CPU 负载; 轮询进程、子进程、日志、事件查看器
# 职责边界    : 不重启机器; 不访问外部服务; 不安装依赖; 结论区分实测与推断
# ==========================================================================================

"""无控制台稳定性验证.

用法:
    python scripts/verify_no_console_stability.py --rounds 3 --duration 45 --target both

证据目录: <构建根>/evidence/no-console/<时间戳>-stability/
目标:
- dev:      pythonw -m rdc.ui
- portable: <构建根>/artifacts/desktop/<产物基名>/rdc-desktop.exe
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _pythonw() -> Path:
    return Path(sys.executable).with_name("pythonw.exe")


def _artifact_basename() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from rdc.version import artifact_basename

    return artifact_basename()


def _launch(target: str, runtime_dir: Path, source: str) -> tuple[subprocess.Popen[bytes], str]:
    env = os.environ.copy()
    env["RDC_LOG_DIR"] = str(runtime_dir / "logs")
    env["RDC_UPDATE_SOURCE"] = source
    env["QT_QPA_PLATFORM"] = "windows"
    # DETACHED_PROCESS + DEVNULL 等价于资源管理器双击: 不继承父进程控制台与管道句柄
    detach = {
        "creationflags": subprocess.DETACHED_PROCESS,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if target == "dev":
        env["PYTHONPATH"] = str(ROOT / "src")
        args = [str(_pythonw()), "-m", "rdc.ui"]
        process = subprocess.Popen(args, cwd=str(ROOT), env=env, **detach)
        return process, "pythonw.exe"
    executable = DEFAULT_BUILD_ROOT / "artifacts" / "desktop" / _artifact_basename() / "rdc-desktop.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"未找到便携包可执行文件: {executable}")
    process = subprocess.Popen([str(executable)], cwd=str(executable.parent), env=env, **detach)
    return process, "rdc-desktop.exe"


def _start_load(load_dir: Path) -> list[subprocess.Popen[bytes]]:
    """起一组负载: 全量 pytest + 两个 CPU 忙等进程."""
    load_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["RDC_LOG_DIR"] = str(load_dir)
    flags = subprocess.CREATE_NO_WINDOW
    processes = [
        subprocess.Popen(
            [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    ]
    for _ in range(2):
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", "while True:\n    pass"],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        )
    return processes


def _stop_load(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _children_info(pid: int) -> tuple[list[str], int]:
    try:
        children = psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return [], 0
    names = [f"{item.name()}({item.pid})" for item in children]
    conhost = sum(1 for item in children if item.name().lower() == "conhost.exe")
    return names, conhost


def _close_window(pid: int) -> str:
    """优先发 WM_CLOSE 让应用自行退出; 超时再强制结束."""
    from diagnose_no_console import _find_window

    hwnd, _title = _find_window(pid, timeout=2)
    if hwnd:
        import ctypes

        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return "wm-close" if hwnd else "no-window"


def _read_log(log_path: Path) -> dict[str, object]:
    if not log_path.is_file():
        return {"exists": False, "bytes": 0, "heartbeats": 0, "has_start": False, "has_exit": False}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    names = [str(item.get("event", "")) for item in events]
    return {
        "exists": True,
        "bytes": len(text.encode("utf-8")),
        "heartbeats": names.count("app.alive"),
        "has_start": "app.start" in names,
        "has_exit": "app.exit" in names,
        "last_event": names[-1] if names else "",
    }


def _app_error_events(started: datetime, process_name: str) -> dict[str, object]:
    """查 Windows 应用程序日志的 1000/1001/1002 事件, 判断是否有崩溃记录."""
    since = started.strftime("%Y-%m-%dT%H:%M:%S")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$items=Get-WinEvent -FilterHashtable @{{LogName='Application';StartTime='{since}'}} -MaxEvents 300;"
        f"$hit=$items | Where-Object {{ $_.Id -in 1000,1001,1002 -and $_.Message -match '{process_name}' }};"
        "if ($hit) { $hit | Select-Object Id,TimeCreated,ProviderName,Message | ConvertTo-Json -Compress }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"available": False, "count": 0, "error": type(error).__name__}
    payload = result.stdout.strip()
    if not payload:
        return {"available": True, "count": 0, "events": []}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {"available": False, "count": 0, "error": "json-decode"}
    events = parsed if isinstance(parsed, list) else [parsed]
    return {"available": True, "count": len(events), "events": events[:3]}


def _run_round(index: int, target: str, duration: int, evidence: Path) -> dict[str, object]:
    round_dir = evidence / f"round-{index}-{target}"
    round_dir.mkdir(parents=True, exist_ok=True)
    source = f"local:{round_dir / 'empty-update-source'}"
    started_at = datetime.now()
    load = _start_load(round_dir / "load")
    process, process_name = _launch(target, round_dir, source)
    begin = time.monotonic()
    mid_children: list[str] = []
    mid_conhost = 0
    last_alive = begin
    exit_code: int | None = None
    while time.monotonic() - begin < duration:
        code = process.poll()
        if code is not None:
            exit_code = code
            break
        last_alive = time.monotonic()
        if 4.5 <= time.monotonic() - begin <= 6.0 and not mid_children:
            mid_children, mid_conhost = _children_info(process.pid)
        time.sleep(0.2)
    alive_at_end = process.poll() is None
    survival = round(last_alive - begin, 2)
    stop_reason = "still-alive"
    window = ""
    if alive_at_end:
        window = _close_window(process.pid)
        try:
            exit_code = process.wait(timeout=8)
            stop_reason = f"closed({window})"
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
            stop_reason = "terminated"
    else:
        stop_reason = "exited"
    end_children, end_conhost = _children_info(process.pid)
    _stop_load(load)

    log_path = round_dir / "logs" / "rdc.log"
    crash_path = round_dir / "logs" / "crash.log"
    result = {
        "round": index,
        "target": target,
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_limit_s": duration,
        "survival_s": survival,
        "alive_at_end": alive_at_end,
        "exit_code": exit_code,
        "stop_reason": stop_reason,
        "children_at_5s": mid_children,
        "conhost_at_5s": mid_conhost,
        "children_at_end": end_children,
        "conhost_at_end": end_conhost,
        "log": _read_log(log_path),
        "crash_log": {
            "exists": crash_path.is_file(),
            "bytes": crash_path.stat().st_size if crash_path.is_file() else 0,
        },
        "app_error_events": _app_error_events(started_at, process_name),
    }
    (round_dir / "round.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return result


def _markdown(results: list[dict[str, object]]) -> str:
    lines = [
        "# 无控制台稳定性验证",
        "",
        "| 轮次 | 目标 | 存活(s) | 退出码 | 停止方式 | conhost | 心跳 | crash.log | 应用错误事件 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        log = item.get("log", {})
        crash = item.get("crash_log", {})
        events = item.get("app_error_events", {})
        heartbeats = log.get("heartbeats", 0) if isinstance(log, dict) else 0
        crash_bytes = crash.get("bytes", 0) if isinstance(crash, dict) else 0
        event_count = events.get("count", 0) if isinstance(events, dict) else 0
        lines.append(
            f"| {item.get('round')} | {item.get('target')} | {item.get('survival_s')} | {item.get('exit_code')} | "
            f"{item.get('stop_reason')} | {item.get('conhost_at_5s')}/{item.get('conhost_at_end')} | "
            f"{heartbeats} | {crash_bytes}B | {event_count} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="无控制台稳定性验证")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--duration", type=int, default=45, help="每轮观测秒数")
    parser.add_argument("--target", choices=("dev", "portable", "both"), default="both")
    parser.add_argument("--build-root", default=os.environ.get("RDC_BUILD_ROOT", str(DEFAULT_BUILD_ROOT)))
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds 至少为 1")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = Path(args.build_root) / "evidence" / "no-console" / f"{stamp}-stability"
    evidence.mkdir(parents=True, exist_ok=True)
    targets = ["dev", "portable"] if args.target == "both" else [args.target]

    results = []
    for index in range(1, args.rounds + 1):
        for target in targets:
            _emit(f"[启动] 第 {index} 轮 {target} (负载: pytest + 2 个 CPU 忙等)")
            result = _run_round(index, target, args.duration, evidence)
            results.append(result)
            log = result["log"]
            log_summary = log if isinstance(log, dict) else {}
            _emit(
                f"[结果] 存活 {result['survival_s']}s, 退出码 {result['exit_code']}, {result['stop_reason']}, "
                f"conhost {result['conhost_at_5s']}/{result['conhost_at_end']}, "
                f"心跳 {log_summary.get('heartbeats', 0)}, 错误事件 {result['app_error_events']}"
            )

    summary = {
        "timestamp": stamp,
        "rounds": args.rounds,
        "duration_s": args.duration,
        "targets": targets,
        "results": results,
        "passed": all(item["survival_s"] >= min(args.duration, 10) for item in results),
    }
    (evidence / "stability.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (evidence / "stability.md").write_text(_markdown(results), encoding="utf-8", newline="\n")
    _emit(f"证据目录: {evidence}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
