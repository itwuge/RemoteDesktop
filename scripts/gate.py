# ==========================================================================================
# 文件        : gate.py
# 所属目录    : scripts/
# 文件用途    : 一条命令跑全量质量门禁: 规范、格式、禁止写法、结构、类型、单测
# 架构定位    : 质量门禁入口, 本地与 CI 共用; 与 pre-commit 使用同一套规则
# 核心功能    : 逐项执行并统计违规数; 输出通过/失败表; 证据落构建根; 任一失败非零退出
# 职责边界    : 只读源码与配置; 不修改业务代码; 不做规则豁免; 不吞掉失败
# ==========================================================================================

"""统一质量门禁.

用法:
    powershell -File scripts/gate.ps1
    python scripts/gate.py [--skip-tests]

证据目录: %RDC_BUILD_ROOT%\\evidence\\gate\\<时间戳>\\
日志目录: %RDC_BUILD_ROOT%\\logs\\gate\\<时间戳>\\ (本次运行独占, 不与他人共用 rdc.log)
并发控制: %RDC_BUILD_ROOT%\\gate.lock 单飞锁, 后到进程排队等待而不是并行开跑
退出码: 任一项失败返回 1, 全绿返回 0.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
SCAN_PATHS = ("src", "tests", "scripts")
FILE_SUFFIXES = {".py", ".md", ".toml", ".json", ".yaml", ".yml"}
ENCODING_DIRS = ("src", "tests", "scripts", "docs", ".team")
CONTRACT_PATH = ROOT / "docs" / "contracts" / "desktop-v1.md"
ERRORS_PATH = ROOT / "src" / "rdc" / "core" / "errors.py"
_LOCK_OFFSET = 4096
_RUN_LOG_DIR: Path | None = None


@dataclass
class StepResult:
    """单项门禁结果."""

    name: str
    passed: bool
    count: int
    detail: str
    output: str = field(default="", repr=False)


def _build_root() -> Path:
    """返回构建根: RDC_BUILD_ROOT > D:\\RDC-Build > 仓库根."""
    configured = os.environ.get("RDC_BUILD_ROOT")
    if configured and Path(configured).is_dir():
        return Path(configured)
    default = Path(r"D:\RDC-Build")
    if default.is_dir():
        return default
    return ROOT


def _evidence_root() -> Path:
    root = _build_root()
    if root == ROOT:
        root.mkdir(parents=True, exist_ok=True)
    return root / "evidence"


def _lock_path() -> Path:
    return _build_root() / "gate.lock"


def _try_lock(handle: TextIO) -> bool:
    """尝试取 OS 级排他锁, 拿不到立即返回 False.

    锁区放在 4 KiB 偏移处: Windows 的字节锁是强制的, 锁住首字节会让其他进程
    读不到锁文件里的持有者信息; 偏移区之外仍可正常读取 pid.
    """
    fileno = handle.fileno()
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: TextIO) -> None:
    fileno = handle.fileno()
    if os.name == "nt":
        import msvcrt

        try:
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            return
        return
    import fcntl

    try:
        fcntl.flock(fileno, fcntl.LOCK_UN)
    except OSError:
        return


def _pid_alive(pid: int) -> bool:
    """判断 PID 是否仍存在; 不存在说明锁是异常退出的残留."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(process, ctypes.byref(code))
        kernel32.CloseHandle(process)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_holder(handle: TextIO) -> dict[str, int]:
    """读取锁文件里的持有者信息."""
    try:
        handle.seek(0)
        text = handle.read() or ""
    except (OSError, ValueError):
        return {}
    holder: dict[str, int] = {}
    match = re.search(r"pid=(\d+)", text)
    if match:
        holder["pid"] = int(match.group(1))
    return holder


def _write_holder(handle: TextIO) -> None:
    """把当前进程信息写入锁文件, 便于等待方与残留检测."""
    payload = f"pid={os.getpid()}\nstarted={datetime.now().isoformat(timespec='seconds')}\ncwd={Path.cwd()}\n"
    handle.seek(0)
    handle.truncate()
    handle.write(payload)
    handle.flush()


def _acquire_lock(path: Path) -> TextIO:
    """单飞: 同一构建根只允许一个门禁运行, 后到者排队等待."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    waited = False
    stale_reported = False
    while True:
        if _try_lock(handle):
            if waited:
                _emit("门禁锁已释放, 继续执行")
            _write_holder(handle)
            return handle
        holder = _read_holder(handle)
        pid = holder.get("pid", 0)
        if pid and not _pid_alive(pid):
            if not stale_reported:
                _emit(f"发现残留门禁锁 (PID {pid} 已退出), 清理后重试")
                stale_reported = True
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
            except OSError:
                pass
        elif not waited:
            _emit(f"已有门禁在运行, 等待中 (持有 PID {pid or '未知'})")
            waited = True
        time.sleep(0.5)


def _pylibs() -> Path:
    return Path(os.environ.get("RDC_PYLIBS", r"D:\RDC-Build\tools\pylibs"))


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(_pylibs()), str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT)]
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if _RUN_LOG_DIR is not None:
        env["RDC_LOG_DIR"] = str(_RUN_LOG_DIR)
    return env


def _run(args: list[str], timeout: int = 900) -> tuple[int, str, str]:
    """执行子进程并原样返回退出码与输出."""
    process = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_subprocess_env(),
    )
    return process.returncode, process.stdout, process.stderr


def _emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def check_ruff_lint() -> StepResult:
    """ruff 静态检查, 违规数取 JSON 条目数."""
    code, out, err = _run([sys.executable, "-m", "ruff", "check", "--output-format=json", *SCAN_PATHS])
    try:
        violations = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        violations = []
    count = len(violations) if isinstance(violations, list) else 0
    samples = []
    for item in (violations if isinstance(violations, list) else [])[:5]:
        location = item.get("location", {})
        samples.append(
            f"{Path(item.get('filename', '?')).name}:{location.get('row', '?')} "
            f"{item.get('code', '?')} {item.get('message', '')}"
        )
    detail = f"违规 {count} 处" if count else "违规 0 处"
    if err.strip() and not violations:
        detail = err.strip().splitlines()[-1]
    return StepResult("ruff 代码规范", code == 0, count, "; ".join(samples) or detail, out + err)


def check_ruff_format() -> StepResult:
    """ruff 格式检查, 计数为需要重排的文件数."""
    code, out, err = _run([sys.executable, "-m", "ruff", "format", "--check", *SCAN_PATHS])
    pending = [line for line in out.splitlines() if line.startswith("Would reformat")]
    count = len(pending)
    detail = "格式违规 0 个文件" if count == 0 else f"格式违规 {count} 个文件: {', '.join(pending[:5])}"
    if code != 0 and not pending:
        detail = (err or out).strip().splitlines()[-1] if (err or out).strip() else "格式检查失败"
    return StepResult("ruff 格式", code == 0, count, detail, out + err)


def check_banned_patterns() -> StepResult:
    """禁止写法扫描, 计数取脚本输出的违规数."""
    code, out, err = _run([sys.executable, "scripts/check_banned_patterns.py"])
    text = out + err
    failed = re.search(r"失败:\s*(\d+)\s*处", text)
    passed = re.search(r"通过:\s*(\d+)\s*处违规", text)
    if failed:
        count = int(failed.group(1))
    elif passed:
        count = int(passed.group(1))
    else:
        count = 0 if code == 0 else 1
    detail = "违规 0 处" if count == 0 else f"违规 {count} 处"
    return StepResult("禁止写法", code == 0, count, detail, text)


def _command_section(contract: str) -> str:
    return contract.split("## 三、命令函数", 1)[1].split("## 四、", 1)[0]


def _command_names(contract: str) -> list[str]:
    return sorted(set(re.findall(r"`([a-z][a-z0-9_]+)\(", _command_section(contract))))


def _expected_command_count(contract: str) -> int:
    numbers = [int(item) for item in re.findall(r"^\|\s*(\d+)\s*\|", _command_section(contract), re.MULTILINE)]
    return max(numbers) if numbers else 0


def _contract_error_codes(contract: str) -> set[str]:
    section3 = contract.split("## 三、命令函数", 1)[1].split("## 四、", 1)[0]
    section5 = contract.split("## 五、模块端口", 1)[1].split("## 六、", 1)[0]
    return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", section3 + section5))


def _defined_commands(commands: set[str]) -> tuple[set[str], set[str]]:
    """返回命令层函数名集合与返回注解不是 CommandResult 的函数名集合."""
    names: set[str] = set()
    wrong_return: set[str] = set()
    for path in sorted((ROOT / "src" / "rdc" / "commands").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            names.add(node.name)
            returns = node.returns
            is_result = isinstance(returns, ast.Name) and returns.id == "CommandResult"
            if node.name in commands and not is_result:
                wrong_return.add(node.name)
    return names, wrong_return


def _error_constants() -> set[str]:
    tree = ast.parse(ERRORS_PATH.read_text(encoding="utf-8"))
    constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.add(target.id)
    return constants


def check_structure() -> StepResult:
    """结构检查: 目录、包、契约命令覆盖、错误码常量、文件编码."""
    problems: list[str] = []
    required = (
        "pyproject.toml",
        "src/rdc",
        "tests",
        "scripts",
        "docs/contracts/desktop-v1.md",
        "docs/contracts/protocol-v1.md",
    )
    problems.extend(f"缺少必需路径 {item}" for item in required if not (ROOT / item).exists())

    for package in sorted((ROOT / "src" / "rdc").iterdir()):
        if package.is_dir() and package.name != "__pycache__" and not (package / "__init__.py").is_file():
            problems.append(f"包缺少 __init__.py: {package.relative_to(ROOT).as_posix()}")

    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    commands = _command_names(contract)
    expected = _expected_command_count(contract)
    if len(commands) != expected:
        problems.append(f"契约命令解析数 {len(commands)}, 表格编号上限 {expected}")
    defined, wrong_return = _defined_commands(set(commands))
    missing = [name for name in commands if name not in defined]
    if missing:
        problems.append("契约命令未实现: " + ", ".join(missing))
    if wrong_return:
        problems.append("命令返回注解不是 CommandResult: " + ", ".join(sorted(wrong_return)))

    constants = _error_constants()
    missing_codes = sorted(_contract_error_codes(contract) - constants)
    if missing_codes:
        problems.append("契约错误码未声明: " + ", ".join(missing_codes))

    for directory in ENCODING_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in FILE_SUFFIXES or "__pycache__" in path.parts:
                continue
            payload = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            if payload.startswith(b"\xef\xbb\xbf"):
                problems.append(f"文件带 BOM: {relative}")
            if b"\r\n" in payload:
                problems.append(f"文件使用 CRLF: {relative}")

    detail = f"违规 {len(problems)} 处" if problems else f"违规 0 处, 契约命令 {len(commands)} 个全覆盖"
    return StepResult("结构检查", not problems, len(problems), "; ".join(problems[:8]) or detail, "\n".join(problems))


def check_mypy() -> StepResult:
    """mypy 类型检查, 计数为 error 行数."""
    code, out, err = _run([sys.executable, "-m", "mypy"])
    text = out + err
    count = len(re.findall(r": error:", text))
    detail = "类型错误 0 处" if count == 0 else f"类型错误 {count} 处"
    if code != 0 and count == 0:
        detail = text.strip().splitlines()[-1] if text.strip() else "类型检查失败"
    return StepResult("mypy 类型检查", code == 0, count, detail, text)


def _pytest_counts(text: str) -> tuple[int, int, int, int]:
    """从 pytest 汇总行提取 (通过, 失败, 错误, 跳过)."""
    summary = text.strip().splitlines()[-1] if text.strip() else ""
    passed = int(match.group(1)) if (match := re.search(r"(\d+) passed", summary)) else 0
    failures = int(match.group(1)) if (match := re.search(r"(\d+) failed", summary)) else 0
    errors = int(match.group(1)) if (match := re.search(r"(\d+) error", summary)) else 0
    skipped = int(match.group(1)) if (match := re.search(r"(\d+) skipped", summary)) else 0
    return passed, failures, errors, skipped


def check_pytest() -> StepResult:
    """pytest 全量单测, 计数为失败与错误用例数."""
    code, out, err = _run([sys.executable, "-m", "pytest", "--tb=short"])
    text = out + err
    passed, failures, errors, skipped = _pytest_counts(text)
    total = passed + failures + errors + skipped
    if total == 0:
        detail = text.strip().splitlines()[-1] if text.strip() else "测试未产出结果"
        return StepResult("pytest 单元测试", False, 1, detail, text)
    ok = code == 0 and failures == 0 and errors == 0
    detail = f"{total} 项: 通过 {passed}, 失败 {failures}, 错误 {errors}, 跳过 {skipped}"
    return StepResult("pytest 单元测试", ok, failures + errors, detail, text)


def _prepare_run(stamp: str) -> Path:
    """创建本次运行的证据与日志目录; 日志目录按实例隔离, 避免共用 rdc.log."""
    global _RUN_LOG_DIR
    _RUN_LOG_DIR = _build_root() / "logs" / "gate" / stamp
    _RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
    evidence_dir = _evidence_root() / "gate" / stamp
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    skip_tests = "--skip-tests" in arguments
    lock_path = _lock_path()
    lock_handle = _acquire_lock(lock_path)
    try:
        started = time.perf_counter()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        evidence_dir = _prepare_run(stamp)

        _emit("=" * 100)
        _emit(f"RDC 统一门禁  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        _emit(f"单飞锁: {lock_path}")
        _emit(f"范围: {' '.join(SCAN_PATHS)}; 类型: src/rdc; 证据: {evidence_dir}")
        _emit(f"本次日志目录: {_RUN_LOG_DIR}")
        _emit("=" * 100)

        steps = [
            check_ruff_lint,
            check_ruff_format,
            check_banned_patterns,
            check_structure,
            check_mypy,
        ]
        results: list[StepResult] = []
        for step in steps:
            _emit("")
            _emit(f"== {step.__name__} ==")
            result = step()
            results.append(result)
            code = "PASS" if result.passed else "FAIL"
            _emit(f"{code} {result.name}: {result.detail}")
        if not skip_tests:
            _emit("")
            _emit("== pytest ==")
            result = check_pytest()
            results.append(result)
            code = "PASS" if result.passed else "FAIL"
            _emit(f"{code} {result.name}: {result.detail}")

        elapsed = round(time.perf_counter() - started, 2)
        failed = [item for item in results if not item.passed]
        total_violations = sum(item.count for item in results)
        _emit("")
        _emit("-" * 100)
        _emit(f"{'项目':<20}{'结论':<8}{'违规计数':<10}说明")
        for item in results:
            _emit(f"{item.name:<20}{'通过' if item.passed else '失败':<8}{item.count:<10}{item.detail}")
        _emit("-" * 100)
        passed_count = len(results) - len(failed)
        _emit(
            f"总计 {len(results)} 项: 通过 {passed_count}, 失败 {len(failed)}, 违规 {total_violations}, 用时 {elapsed}s"
        )
        _emit(f"证据: {evidence_dir}")
        _emit(f"日志: {_RUN_LOG_DIR}")

        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": elapsed,
            "scope": list(SCAN_PATHS),
            "total_violations": total_violations,
            "passed": not failed,
            "steps": [
                {"name": item.name, "passed": item.passed, "count": item.count, "detail": item.detail}
                for item in results
            ],
        }
        (evidence_dir / "gate.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log_text = "\n\n".join(f"===== {item.name} =====\n{item.output}" for item in results)
        (evidence_dir / "gate.log").write_text(log_text, encoding="utf-8")

        if failed:
            _emit("结果: 门禁失败")
            return 1
        _emit("结果: 门禁全绿")
        return 0
    finally:
        _unlock(lock_handle)
        lock_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
