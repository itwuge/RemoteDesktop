# ==========================================================================================
# 文件        : diagnose_no_console.py
# 所属目录    : scripts/
# 文件用途    : 实测 pythonw 无控制台下的退出路径: 标准流缺失、默认钩子、子进程继承、日志竞争
# 架构定位    : 诊断工具; 只读业务代码, 不修改运行时
# 核心功能    : 逐项跑子进程实验, 记录退出码、崩溃日志、弹窗是否出现、日志是否丢失
# 职责边界    : 不弹付费服务; 不重启机器; 不推送远端; 结论区分实测与推断
# ==========================================================================================

"""pythonw 无控制台退出路径诊断.

用法:
    python scripts/diagnose_no_console.py

证据目录: <构建根>/evidence/no-console/<时间戳>-diagnose/
全部子进程用 DETACHED_PROCESS 启动, 不继承父进程控制台句柄, 等价于资源管理器双击.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUILD_ROOT = Path(r"D:\RDC-Build")
LOG_DIR_NAME = "logs"
CRASH_NAME = "crash.log"

BASELINE_CHILD = """
import json, os, sys
from pathlib import Path
Path(os.environ["RDC_PROBE"]).write_text(json.dumps({
    "stdout_none": sys.stdout is None,
    "stderr_none": sys.stderr is None,
    "stdin_none": sys.stdin is None,
    "frozen": bool(getattr(sys, "frozen", False)),
}), encoding="utf-8")
"""

UNCAUGHT_DEFAULT = """
raise RuntimeError("probe-main-boom")
"""

UNCAUGHT_GUARDED = """
import rdc  # 包导入期补位标准流并安装异常钩子
from rdc.core.logging import configure
import rdc.core.crashguard as crashguard
_ = rdc.__version__
configure()
crashguard.install_exception_hooks(notify=False)
raise RuntimeError("probe-main-boom")
"""

THREAD_DEFAULT = """
import threading

def boom() -> None:
    raise RuntimeError("probe-thread-boom")

worker = threading.Thread(target=boom)
worker.start()
worker.join()
"""

THREAD_GUARDED = """
import threading
import rdc
from rdc.core.logging import configure
import rdc.core.crashguard as crashguard
_ = rdc.__version__
configure()
crashguard.install_exception_hooks(notify=False)

def boom() -> None:
    raise RuntimeError("probe-thread-boom")

worker = threading.Thread(target=boom)
worker.start()
worker.join()
"""

STDIO_WRITE = """
import sys
sys.stderr.write("probe-stderr-boom\\n")
sys.stderr.flush()
"""

SUBPROCESS_CHILD = """
import json, os, subprocess
from pathlib import Path
result = {}
try:
    inherited = subprocess.Popen(["cmd", "/c", "exit 0"])
    result["inherit"] = ["ok", inherited.wait()]
    result["inherit_stdout_none"] = inherited.stdout is None
except (OSError, ValueError) as error:
    result["inherit"] = [type(error).__name__, str(error)]
try:
    explicit = subprocess.Popen(["cmd", "/c", "exit 0"], stdout=None, stderr=None)
    result["explicit_none"] = ["ok", explicit.wait()]
except (OSError, ValueError) as error:
    result["explicit_none"] = [type(error).__name__, str(error)]
Path(os.environ["RDC_PROBE"]).write_text(json.dumps(result), encoding="utf-8")
"""

DIALOG_CHILD = """
import rdc
from rdc.core.logging import configure
import rdc.core.crashguard as crashguard
_ = rdc.__version__
configure()
crashguard.install_exception_hooks(notify=True)
raise RuntimeError("probe-dialog-boom")
"""

ROTATE_CHILD = """
import logging, os
from logging.handlers import RotatingFileHandler
handler = RotatingFileHandler(os.environ["ROTATE_LOG"], maxBytes=4096, backupCount=2, encoding="utf-8")
logger = logging.getLogger("rotate-probe")
logger.setLevel("INFO")
logger.addHandler(handler)
for index in range(1500):
    logger.info("line %s %s", os.getpid(), index)
handler.close()
"""


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _pythonw() -> Path:
    return Path(sys.executable).with_name("pythonw.exe")


def _build_root() -> Path:
    configured = os.environ.get("RDC_BUILD_ROOT", "").strip()
    return Path(configured) if configured else DEFAULT_BUILD_ROOT


def _run_child(code: str, evidence: Path, name: str, env_extra: dict[str, str] | None = None) -> dict[str, object]:
    """用 pythonw 启动子脚本, 不继承控制台句柄; 返回退出码与耗时."""
    work = evidence / name
    work.mkdir(parents=True, exist_ok=True)
    script = work / "child.py"
    script.write_text(textwrap.dedent(code), encoding="utf-8", newline="\n")
    probe = work / "probe.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["RDC_LOG_DIR"] = str(work / LOG_DIR_NAME)
    env["RDC_PROBE"] = str(probe)
    env.pop("QT_QPA_PLATFORM", None)
    if env_extra:
        env.update(env_extra)
    started = time.monotonic()
    process = subprocess.Popen(
        [str(_pythonw()), str(script)], env=env, cwd=str(ROOT), creationflags=subprocess.DETACHED_PROCESS
    )
    code_value = process.wait(timeout=60)
    return {
        "name": name,
        "exit_code": code_value,
        "duration_s": round(time.monotonic() - started, 2),
        "probe": _read_json(probe),
        "crash_log": _read_text(work / LOG_DIR_NAME / CRASH_NAME),
        "log_files": sorted(item.name for item in (work / LOG_DIR_NAME).glob("*"))
        if (work / LOG_DIR_NAME).is_dir()
        else [],
    }


def _read_json(path: Path) -> object:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_window(pid: int, timeout: float = 8.0) -> tuple[int, str]:
    """等待指定进程的可见窗口; 返回 (句柄, 标题)."""
    if sys.platform != "win32":
        return 0, ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = _enum_windows(pid)
        if found[0]:
            return found
        time.sleep(0.1)
    return 0, ""


def _enum_windows(pid: int) -> tuple[int, str]:
    handles: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _: int) -> bool:
        owner = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
            if "RDC" in buffer.value:
                handles.append((int(hwnd), buffer.value))
        return True

    ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
    return handles[0] if handles else (0, "")


def _dialog_experiment(evidence: Path) -> dict[str, object]:
    """验证无控制台、无 Qt 时的可见提示: 子进程弹系统对话框, 父进程确认窗口后关闭."""
    work = evidence / "dialog-guarded"
    work.mkdir(parents=True, exist_ok=True)
    script = work / "child.py"
    script.write_text(textwrap.dedent(DIALOG_CHILD), encoding="utf-8", newline="\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["RDC_LOG_DIR"] = str(work / LOG_DIR_NAME)
    started = time.monotonic()
    process = subprocess.Popen(
        [str(_pythonw()), str(script)], env=env, cwd=str(ROOT), creationflags=subprocess.DETACHED_PROCESS
    )
    hwnd, title = _find_window(process.pid)
    visible = bool(hwnd)
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    try:
        exit_code = process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        exit_code = process.wait(timeout=10)
    return {
        "name": "dialog-guarded",
        "exit_code": exit_code,
        "duration_s": round(time.monotonic() - started, 2),
        "dialog_visible": visible,
        "dialog_title": title,
        "crash_log": _read_text(work / LOG_DIR_NAME / CRASH_NAME),
    }


def _rotation_experiment(evidence: Path) -> dict[str, object]:
    """两个进程写同一日志文件并高频轮转, 观察是否丢行或崩溃."""
    work = evidence / "rotation-contention"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "shared.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["ROTATE_LOG"] = str(target)
    scripts = []
    for index in range(2):
        script = work / f"child-{index}.py"
        script.write_text(textwrap.dedent(ROTATE_CHILD), encoding="utf-8", newline="\n")
        scripts.append(script)
    processes = [
        subprocess.Popen(
            [str(_pythonw()), str(script)], env=env, cwd=str(ROOT), creationflags=subprocess.DETACHED_PROCESS
        )
        for script in scripts
    ]
    exit_codes = [process.wait(timeout=60) for process in processes]
    lines = 0
    for path in work.glob("shared.log*"):
        lines += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return {
        "name": "rotation-contention",
        "exit_codes": exit_codes,
        "expected_lines": 3000,
        "actual_lines": lines,
        "lost_lines": 3000 - lines,
        "files": sorted(item.name for item in work.glob("shared.log*")),
    }


def main() -> int:
    if sys.platform != "win32":
        _emit("本诊断只在 Windows 上有效")
        return 1
    if not _pythonw().is_file():
        _emit(f"未找到 pythonw: {_pythonw()}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence = _build_root() / "evidence" / "no-console" / f"{stamp}-diagnose"
    evidence.mkdir(parents=True, exist_ok=True)

    results: dict[str, object] = {"timestamp": stamp, "pythonw": str(_pythonw()), "experiments": {}}
    experiments: dict[str, object] = {}

    experiments["baseline"] = _run_child(BASELINE_CHILD, evidence, "baseline")
    experiments["uncaught_default"] = _run_child(UNCAUGHT_DEFAULT, evidence, "uncaught-default")
    experiments["uncaught_guarded"] = _run_child(UNCAUGHT_GUARDED, evidence, "uncaught-guarded")
    experiments["thread_default"] = _run_child(THREAD_DEFAULT, evidence, "thread-default")
    experiments["thread_guarded"] = _run_child(THREAD_GUARDED, evidence, "thread-guarded")
    experiments["stdio_write"] = _run_child(STDIO_WRITE, evidence, "stdio-write")
    experiments["subprocess"] = _run_child(SUBPROCESS_CHILD, evidence, "subprocess-inheritance")
    experiments["rotation"] = _rotation_experiment(evidence)
    experiments["dialog"] = _dialog_experiment(evidence)
    results["experiments"] = experiments
    results["measured"] = _measured_facts(experiments)
    results["inference"] = _inference(experiments)
    text = json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    (evidence / "diagnose.json").write_text(text, encoding="utf-8", newline="\n")

    for name, item in experiments.items():
        if isinstance(item, dict):
            summary = ", ".join(
                f"{key}={value}"
                for key, value in item.items()
                if key in {"exit_code", "exit_codes", "dialog_visible", "lost_lines", "duration_s"}
            )
            _emit(f"[{name}] {summary}")
    _emit(f"证据目录: {evidence}")
    return 0


def _measured_facts(experiments: dict[str, object]) -> list[str]:
    baseline = experiments.get("baseline", {})
    probe = baseline.get("probe") if isinstance(baseline, dict) else None
    facts: list[str] = []
    if isinstance(probe, dict):
        facts.append(
            "pythonw 未继承控制台句柄时: "
            f"stdout={probe.get('stdout_none')}, "
            f"stderr={probe.get('stderr_none')}, "
            f"stdin={probe.get('stdin_none')} 为 None"
        )
    for key, label in (
        ("uncaught_default", "默认钩子主线程异常"),
        ("uncaught_guarded", "安装钩子后主线程异常"),
        ("thread_default", "默认钩子子线程异常"),
        ("thread_guarded", "安装钩子后子线程异常"),
        ("stdio_write", "直接写 stderr"),
    ):
        item = experiments.get(key)
        if isinstance(item, dict):
            facts.append(
                f"{label}: 退出码 {item.get('exit_code')}, 崩溃日志 {len(str(item.get('crash_log', '')))} 字节, "
                f"耗时 {item.get('duration_s')}s"
            )
    dialog = experiments.get("dialog")
    if isinstance(dialog, dict):
        facts.append(f"无 Qt 时未捕获异常弹窗可见={dialog.get('dialog_visible')}, 标题={dialog.get('dialog_title')}")
    rotation = experiments.get("rotation")
    if isinstance(rotation, dict):
        facts.append(
            f"两进程共享轮转日志: 退出码 {rotation.get('exit_codes')}, "
            f"写入 {rotation.get('actual_lines')}/3000 行, 丢失 {rotation.get('lost_lines')} 行"
        )
    subprocess_item = experiments.get("subprocess")
    if isinstance(subprocess_item, dict) and isinstance(subprocess_item.get("probe"), dict):
        facts.append(f"子进程继承实验: {subprocess_item.get('probe')}")
    return facts


def _inference(experiments: dict[str, object]) -> list[str]:
    uncaught = experiments.get("uncaught_default")
    guarded = experiments.get("uncaught_guarded")
    notes: list[str] = []
    if isinstance(uncaught, dict) and isinstance(guarded, dict):
        if uncaught.get("exit_code") not in (0, None) and len(str(uncaught.get("crash_log", ""))) == 0:
            notes.append("默认路径下未捕获异常确实会无日志退出, 与 pythonw 标准流为 None 一致.")
        if len(str(guarded.get("crash_log", ""))) > 0:
            notes.append("安装钩子后同一异常能落盘, 说明静默退出可被本模块兜住.")
    thread_item = experiments.get("thread_default")
    if isinstance(thread_item, dict) and thread_item.get("exit_code") == 0:
        notes.append("子线程异常不会结束进程, 默认行为是线程静默死亡, 进程继续运行.")
    rotation = experiments.get("rotation")
    if isinstance(rotation, dict) and int(rotation.get("lost_lines", 0)) > 0:
        notes.append("并发写同一轮转日志会丢行, 但实验进程退出码为 0, 属日志丢失而非进程退出.")
    notes.append("第 11/30 秒的整体退出无法由单次实验直接复现; 若现场无崩溃事件且无异常日志,")
    notes.append("更可能是外部终止(并发构建/门禁/杀软/会话回收)而非 Python 异常, 需结合现场监控继续定位.")
    return notes


if __name__ == "__main__":
    raise SystemExit(main())
