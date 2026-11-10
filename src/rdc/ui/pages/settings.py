# ==========================================================================================
# 文件        : settings.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 设置页: 画质档、降级策略、硬件编码、中继策略、账号与免确认说明
# 架构定位    : 界面层, 只调用 rdc.commands.api
# 核心功能    : 中继策略提交; 其余为只读呈现, 明确标注 v1 无写入命令
# 职责边界    : 不臆造命令; 不本地保存偏好; 不改变会话状态
# ==========================================================================================

"""设置页."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit

from rdc.commands import api
from rdc.media.quality import DIRECT_PROFILES
from rdc.ui import icons
from rdc.ui.components import chip
from rdc.ui.pages.base import PageBase, row
from rdc.ui.theme import active
from rdc.ui.update_panel import UpdatePanel


class SettingsPage(PageBase):
    """设置项呈现与中继策略."""

    def __init__(self) -> None:
        super().__init__(
            "settings",
            "设置",
            "画质默认值、降级策略与硬件编码在 v1 没有写入命令, 此处只读呈现; 中继策略可提交.",
            scrollable=True,
        )
        self._build()

    def _build(self) -> None:
        self._update_panel: UpdatePanel | None = None
        self.header_actions.addWidget(self.button("检查更新", self._on_check_update, icon="refresh-cw"))
        banner = QFrame()
        banner.setObjectName("statusStrip")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(14, 10, 14, 10)
        banner_layout.setSpacing(10)
        info_icon = QLabel()
        info_icon.setPixmap(icons.pixmap("info", active()["accent"], 17))
        banner_layout.addWidget(info_icon)
        banner_text = QLabel("v1 仅中继策略可提交; 其余配置只读呈现, 不写入本地, 不改变当前会话.")
        banner_text.setObjectName("muted")
        banner_layout.addWidget(banner_text, 1)
        banner_layout.addWidget(chip("只读优先", "accent"))
        self.body.addWidget(banner)

        quality_box, quality_layout = self.form_section("画质与降级")
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["auto", *sorted(DIRECT_PROFILES - {"auto"})])
        self.quality_combo.setEnabled(False)
        self.quality_combo.setToolTip("v1 没有默认画质写入命令, 会话内请用 apply_quality_profile")
        quality_layout.addRow("默认画质档", self.quality_combo)
        quality_layout.addRow("降级顺序", self.muted("弱网降档 → 1080p30 地板 → 每会话最多 6 次切换 → 稳定 10 秒回升."))
        self.hardware_encode = QCheckBox("硬件编码优先（v1 无写入命令, 仅呈现）")
        self.hardware_encode.setEnabled(False)
        quality_layout.addRow(self.hardware_encode)
        self.body.addWidget(quality_box)

        relay_box, relay_layout = self.form_section("中继策略")
        self.relay_endpoint = QLineEdit()
        self.relay_endpoint.setPlaceholderText("relay://host:port")
        self.relay_egress = QLineEdit()
        self.relay_egress.setPlaceholderText("出口带宽 Mbps, 例如 50")
        self.relay_profile = QComboBox()
        self.relay_profile.addItems(["L0", "L1", "L2", "L3"])
        relay_layout.addRow("中继端点", self.relay_endpoint)
        relay_layout.addRow("出口带宽", self.relay_egress)
        relay_layout.addRow("降级档", self.relay_profile)
        relay_layout.addRow(row(self.button("应用中继策略", self._on_apply_relay, primary=True, icon="check")))
        self.relay_status = QLabel("未提交")
        self.relay_status.setObjectName("muted")
        relay_layout.addRow("提交状态", self.relay_status)
        relay_layout.addRow("计费口径", self.muted("预算按出口带宽扣除 8% 开销计算; 失败码原样呈现."))
        self.body.addWidget(relay_box)

        account_box, account_layout = self.form_section("账号与免确认")
        account_layout.addRow("免确认策略", self.muted("同账号免确认默认开启; 单台设备可关闭, 关闭后连接需审批."))
        account_layout.addRow("操作入口", self.muted("账号登录、设备绑定与审批在对应页面操作, 本页不重复入口."))
        self.body.addWidget(account_box)

        appearance_box, appearance_layout = self.form_section("外观")
        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        theme_row.addWidget(QLabel("主题"))
        theme_row.addWidget(chip("深色 · 默认", "accent"))
        theme_row.addWidget(chip("浅色 · 可切换"))
        theme_row.addStretch(1)
        theme_row.addWidget(self.muted("在左下角「切换浅色 / 切换深色」即时生效."))
        appearance_layout.addRow(theme_row)
        self.body.addWidget(appearance_box)
        self.body.addStretch(1)

    def _on_check_update(self) -> None:
        """打开更新面板并立即检查; 检查失败只在面板内提示."""
        if self._update_panel is None:
            self._update_panel = UpdatePanel(self.window())
        self._update_panel.show()
        self._update_panel.raise_()
        self._update_panel.activateWindow()
        self._update_panel.start_check()

    def _on_apply_relay(self) -> None:
        raw = self.relay_egress.text().strip()
        if not raw.isdigit() or int(raw) <= 0:
            self.relay_status.setText("出口带宽必须是正整数")
            return
        result = api.apply_relay_policy(
            self.relay_endpoint.text().strip(),
            int(raw),
            self.relay_profile.currentText(),
        )
        self.relay_status.setText(result.message)
        self.report("应用中继策略", result)
