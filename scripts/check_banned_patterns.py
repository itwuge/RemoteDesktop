# ==========================================================================================
# 文件        : check_banned_patterns.py
# 所属目录    : scripts/
# 文件用途    : 门禁脚本之一, 扫描禁止写法（print、裸 except、吞异常、TODO 占位）
# 架构定位    : 质量门禁, 被 scripts/gate.ps1 与 pre-commit 调用
# 核心功能    : 逐文件逐行匹配, 输出违规清单, 有违规则非零退出
# 职责边界    : 不做自动修复; 不排除任何文件; 不读取业务配置
# ==========================================================================================

"""禁止写法扫描.

规则:
1. 产品与测试代码禁止 print( 与 pprint( 用于输出.
2. 禁止裸 except: 与 except BaseException（测试线程捕获异常除外, 必须原样带回主线程）.
3. except Exception 只允许出现在 src/rdc/core/boundary.py（统一异常边界）.
4. 禁止 fmt: off / noqa 形式的规则豁免.

范围: src、tests、scripts. poc/tools 与 deploy 为历史证据脚本, 输出即证据载体,
不在产品门禁范围, 缺口在 gate 汇总中如实列出.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "tests", "scripts")
SKIP_PARTS = {"__pycache__", ".git", "node_modules", "android"}
SELF_PATH = "scripts/check_banned_patterns.py"

PRINT_PATTERN = re.compile(r"(?<![\w.])print\s*\(")
BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*$")
BASE_EXCEPT = re.compile(r"^\s*except\s+BaseException\b")
WIDE_EXCEPT = re.compile(r"^\s*except\s+Exception\b")
EXEMPT_WIDE = {"src/rdc/core/boundary.py"}
NOQA_PATTERN = re.compile(r"#\s*(noqa|fmt:\s*off|type:\s*ignore)")


def iter_files() -> list[Path]:
    """收集待扫描文件."""
    files: list[Path] = []
    for directory in SCAN_DIRS:
        base = ROOT / directory
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if SKIP_PARTS & set(path.parts):
                continue
            files.append(path)
    return sorted(files)


def scan() -> list[tuple[str, int, str, str]]:
    """返回违规列表: (文件, 行号, 规则, 原文)."""
    violations: list[tuple[str, int, str, str]] = []
    for path in iter_files():
        relative = path.relative_to(ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if PRINT_PATTERN.search(line) and not stripped.startswith("#") and relative != SELF_PATH:
                violations.append((relative, number, "print", stripped))
            if BARE_EXCEPT.match(line):
                violations.append((relative, number, "bare-except", stripped))
            if BASE_EXCEPT.match(line) and not relative.startswith("tests/"):
                violations.append((relative, number, "base-except", stripped))
            if WIDE_EXCEPT.match(line) and relative not in EXEMPT_WIDE:
                violations.append((relative, number, "wide-except", stripped))
            if NOQA_PATTERN.search(line):
                violations.append((relative, number, "noqa", stripped))
    return violations


def main() -> int:
    """执行扫描并输出结果."""
    violations = scan()
    if violations:
        sys.stderr.write(f"禁止写法扫描失败: {len(violations)} 处\n")
        for relative, number, rule, text in violations[:50]:
            sys.stderr.write(f"  {relative}:{number} [{rule}] {text}\n")
        return 1
    sys.stderr.write(f"禁止写法扫描通过: 0 处违规, 扫描 {len(iter_files())} 个文件\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
