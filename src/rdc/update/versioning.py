# ==========================================================================================
# 文件        : versioning.py
# 所属目录    : src/rdc/update/
# 文件用途    : 语义化版本解析与比较, 供更新检查判定新旧与拒绝降级
# 架构定位    : 更新模块的基础设施, 不依赖界面与网络
# 核心功能    : 解析 主.次.修订[-预发布][+构建元数据]; 按 SemVer 规则比较
# 职责边界    : 不读取文件; 不做更新决策; 不生成用户文案
# ==========================================================================================

"""语义化版本解析与比较."""

from __future__ import annotations

import re
from dataclasses import dataclass

SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_NUMERIC = re.compile(r"^\d+$")


@dataclass(frozen=True)
class SemVer:
    """语义化版本; build 元数据不参与比较."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base

    def comparison_key(self) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
        """构造可比较的键: 正式版高于同号预发布版."""
        return (self.major, self.minor, self.patch, 0 if self.prerelease else 1, self._prerelease_key())

    def _prerelease_key(self) -> tuple[tuple[int, int, str], ...]:
        parts: list[tuple[int, int, str]] = []
        for item in self.prerelease:
            if _NUMERIC.fullmatch(item):
                parts.append((0, int(item), ""))
            else:
                parts.append((1, 0, item))
        return tuple(parts)


def parse_version(version: str) -> SemVer:
    """解析语义化版本, 非法格式抛出 ValueError."""
    match = SEMVER_PATTERN.fullmatch((version or "").strip())
    if match is None:
        raise ValueError(f"版本号不符合语义化版本格式: {version}")
    prerelease = tuple(item for item in (match.group("prerelease") or "").split(".") if item)
    return SemVer(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=prerelease,
    )


def compare_versions(left: str, right: str) -> int:
    """比较两个版本; 左大于右返回正数, 相等返回 0, 小于返回负数."""
    left_key = parse_version(left).comparison_key()
    right_key = parse_version(right).comparison_key()
    if left_key == right_key:
        return 0
    return 1 if left_key > right_key else -1
