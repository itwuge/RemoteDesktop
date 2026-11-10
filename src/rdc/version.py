# ==========================================================================================
# 文件        : version.py
# 所属目录    : src/rdc/
# 文件用途    : 产品版本号唯一来源, 供运行时、构建脚本与更新清单共用
# 架构定位    : 基础设施层, 不依赖任何上层模块
# 核心功能    : 声明语义化版本; 生成带版本号与平台标识的产物名
# 职责边界    : 不读取配置; 不写文件; 不判断更新逻辑
# ==========================================================================================

"""版本号唯一来源.

改版本只改本文件的 __version__, 构建脚本、客户端更新检查与文档都从这里读取.
语义化版本格式: 主版本.次版本.修订号, 可选 -预发布标识 与 +构建元数据.
"""

from __future__ import annotations

import platform
import sys

__version__ = "0.2.0"

APP_NAME = "rdc-desktop"
PRODUCT_NAME = "RDC 远程桌面"

_PLATFORM_NAMES = {"win32": "windows", "darwin": "macos", "linux": "linux"}
_MACHINE_NAMES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x86": "x86",
    "i386": "x86",
    "i686": "x86",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def platform_tag() -> str:
    """返回构建产物使用的平台标识, 例如 windows-x64."""
    system = _PLATFORM_NAMES.get(sys.platform, sys.platform)
    machine = (platform.machine() or "unknown").lower()
    return f"{system}-{_MACHINE_NAMES.get(machine, machine)}"


def artifact_basename() -> str:
    """返回不带扩展名的产物名, 例如 rdc-desktop-0.2.0-windows-x64."""
    return f"{APP_NAME}-{__version__}-{platform_tag()}"
