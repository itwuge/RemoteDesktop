# ==========================================================================================
# 文件        : smoke_env.py
# 所属目录    : scripts/
# 文件用途    : 冒烟脚本的路径与离屏环境引导, 保证直接 python 运行也能找到 src
# 架构定位    : 测试装置
# 核心功能    : 注入项目根与 src 到 sys.path; 默认离屏 Qt
# 职责边界    : 无业务逻辑; 无副作用型网络/文件写入
# ==========================================================================================

"""冒烟环境引导."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"

for _candidate in (str(PROJECT_ROOT), str(SRC_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
