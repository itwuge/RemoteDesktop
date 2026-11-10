# ==========================================================================================
# 文件        : e2e_update.py
# 所属目录    : scripts/
# 文件用途    : 兼容入口; 本地更新闭环验证已并入 e2e_release.py
# 架构定位    : 旧命令的转发壳, 不复制验证逻辑
# 核心功能    : 保持 `python scripts/e2e_update.py` 可用
# 职责边界    : 不修改业务代码; 不执行下载的包; 不访问外部服务
# ==========================================================================================

"""本地更新端到端验证(兼容入口).

自更新改为读取 GitHub Releases 清单资产后, 完整链路(发布演练 -> 检查 -> 下载 -> 校验)
统一由 scripts/e2e_release.py 覆盖; 本文件只做转发, 避免两份验证逻辑漂移.

用法:
    python scripts/e2e_update.py
    python scripts/e2e_update.py --current-version 0.1.0
"""

from __future__ import annotations

import sys

from e2e_release import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
