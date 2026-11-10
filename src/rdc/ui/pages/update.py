# ==========================================================================================
# 文件        : update.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 更新面板导航页: 把软件更新面板包进独立页面
# 架构定位    : 界面层, 只承载 rdc.ui.update_panel 的既有面板
# 核心功能    : 进入页面即检查更新; 保留检查/下载/取消/安装全部命令调用
# 职责边界    : 不复制更新命令逻辑; 不执行安装包; 不改写面板内的命令返回文案
# ==========================================================================================

"""更新面板页."""

from __future__ import annotations

from PySide6.QtCore import Qt

from rdc.ui.pages.base import PageBase
from rdc.ui.update_panel import UpdatePanel


class UpdatePage(PageBase):
    """独立导航页, 内容复用软件更新面板."""

    def __init__(self) -> None:
        super().__init__(
            "update",
            "更新面板",
            "启动时静默检查, 失败不阻塞启动; 下载完成后必须通过完整性校验（SHA-256）, 校验通过才提示安装.",
            scrollable=True,
        )
        self.panel = UpdatePanel(self)
        self.panel.setWindowFlag(Qt.WindowType.Widget, True)
        self.panel.close_button.setVisible(False)
        self.body.addWidget(self.panel, 1)

    def refresh(self) -> None:
        """进入页面即检查一次更新; 失败只落在面板内提示."""
        self.panel.show()
        self.panel.start_check()

    def shutdown(self) -> None:
        """关闭前等待检查线程结束, 避免销毁运行中的 QThread."""
        self.panel.close()
