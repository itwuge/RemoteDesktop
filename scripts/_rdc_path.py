# ==========================================================================================
# 文件        : _rdc_path.py
# 所属目录    : scripts/
# 文件用途    : 开发脚本的模块搜索路径引导: 被导入即生效
# 架构定位    : 脚本侧引导件, 不进入运行时, 不承载业务逻辑
# 核心功能    : 把项目根、src、scripts 注入 sys.path, 使 scripts 下的独立脚本可直接运行
# 职责边界    : 只改 sys.path; 不读配置; 不打印; 不引入业务包
# ==========================================================================================

"""开发脚本的模块搜索路径引导.

用法:
    from _rdc_path import PROJECT_ROOT

    from rdc.ui.icons import app_pixmap

导入本模块时即完成 sys.path 注入, 后续项目内导入无需再手工插路径,
也不需要规则豁免注释.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

for _candidate in (str(PROJECT_ROOT), str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)
