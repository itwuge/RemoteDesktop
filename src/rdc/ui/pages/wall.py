# ==========================================================================================
# 文件        : wall.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 屏幕墙页: 1/4/9/16 宫格、预览格升级聚焦、预算与降级提示
# 架构定位    : 界面层, 只调用 rdc.commands.wall
# 核心功能    : 布局切换、格点选聚焦、预算占用展示、关闭释放
# 职责边界    : 不计算预算; 不启动真实解码; 不伪造在线状态
# ==========================================================================================

"""屏幕墙页."""

from __future__ import annotations

from functools import partial

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import accounts, wall
from rdc.core.result import CommandResult
from rdc.ui.components import EmptyState, WallCellButton, chip, fade_in
from rdc.ui.copy import profile_text
from rdc.ui.pages.base import PageBase

CELL_STATE_TEXT = {
    "preview": "预览",
    "focus": "聚焦 4K 60 帧",
    "paused": "已暂停",
}


class WallPage(PageBase):
    """屏幕墙宫格."""

    def __init__(self) -> None:
        super().__init__(
            "wall",
            "屏幕墙",
            "预览格 1 个解码单位, 聚焦格 4 个单位, 默认预算 12; 4K 60 帧同时最多 1 路.",
        )
        self._wall_id: str | None = None
        self._layout = 4
        self._cell_buttons: list[WallCellButton] = []
        self._build()
        self.body.setSpacing(12)
        self._update_summary()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    def _build(self) -> None:
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 10, 12, 10)
        toolbar_layout.setSpacing(6)
        self.layout_buttons: list[QPushButton] = []
        for count in (1, 4, 9, 16):
            button = self.button(f"{count} 路", partial(self._on_layout, count))
            button.setProperty("seg", True)
            button.setCheckable(True)
            button.setChecked(count == 4)
            self.layout_buttons.append(button)
            toolbar_layout.addWidget(button)
        self.online_label = chip("0 / 0 路在线")
        toolbar_layout.addWidget(self.online_label)
        self.layout_label = QLabel("布局：2 × 2")
        self.layout_label.setObjectName("cardMeta")
        toolbar_layout.addWidget(self.layout_label)
        toolbar_layout.addStretch(1)
        self.budget_label = chip("未创建 · 预算 12 单位")
        toolbar_layout.addWidget(self.budget_label)
        toolbar_layout.addWidget(self.button("刷新在线状态", self._on_refresh, icon="refresh-cw"))
        self.header_actions.addWidget(self.button("关闭屏幕墙", self._on_close, icon="x"))
        self.body.addWidget(toolbar)

        self.degrade_label = QLabel("")
        self.degrade_label.setObjectName("badgeWarn")
        self.degrade_label.setWordWrap(True)
        self.body.addWidget(self.degrade_label)

        self.content_stack = QStackedWidget()
        self.empty = EmptyState(
            "wall",
            "屏幕墙当前没有画面",
            "把多台在线设备铺成宫格, 预览低码率画面; 选中任意一格可升级为 4K 60 帧聚焦.",
            action_text="创建屏幕墙",
            action_icon="layout-grid",
            action=lambda: self._on_layout(4),
        )
        self.content_stack.addWidget(self.empty)

        surface = QFrame()
        surface.setObjectName("wallSurface")
        surface_layout = QVBoxLayout(surface)
        surface_layout.setContentsMargins(12, 12, 12, 12)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(0, 0, 0, 0)
        surface_layout.addWidget(self.grid_host)
        self.content_stack.addWidget(surface)
        self.body.addWidget(self.content_stack, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        bottom.addWidget(self.muted("未连接真实会话时仅演示预算与布局, 不产生画面."))
        bottom.addStretch(1)
        self.body.addLayout(bottom)

    # ---------------------------------------------------------------- 命令
    def _on_layout(self, count: int) -> None:
        result = wall.start_wall(count) if self._wall_id is None else wall.set_wall_layout(self._wall_id, count)
        if result.ok and result.data:
            self._wall_id = str(result.data.get("wall_id", self._wall_id or ""))
            self._layout = count
            self._sync_layout_buttons(count)
            self._update_summary()
            self._render(result)
        self.report(f"屏幕墙 {count} 格", result)

    def _on_refresh(self) -> None:
        if self._wall_id is None:
            self._on_layout(self._layout)
            return
        self._on_layout(self._layout)

    def _on_close(self) -> None:
        if self._wall_id is None:
            self.degrade_label.setText("屏幕墙未创建")
            return
        result = wall.close_wall(self._wall_id)
        if result.ok:
            self._wall_id = None
            self._clear_grid()
            self.budget_label.setText("未创建 · 预算 12 单位")
            self.degrade_label.setText("")
            self.content_stack.setCurrentIndex(0)
            self._update_summary()
        self.report("关闭屏幕墙", result)

    def _on_focus(self, cell: int) -> None:
        if self._wall_id is None:
            self.degrade_label.setText("请先创建屏幕墙")
            return
        result = wall.focus_wall_cell(self._wall_id, cell)
        if result.ok and result.data:
            self._render(result)
        self.report(f"聚焦第 {cell + 1} 格", result)

    # ---------------------------------------------------------------- 呈现
    def _render(self, result: CommandResult) -> None:
        if result.data is None:
            return
        data = result.data
        cells = data.get("cells", [])
        self._clear_grid()
        columns = 4 if len(cells) > 9 else (3 if len(cells) > 4 else (2 if len(cells) > 1 else 1))
        for index, cell in enumerate(cells):
            state = str(cell.get("state", "preview"))
            button = WallCellButton(self._cell_text(index, cell), focused=state == "focus")
            button.clicked.connect(lambda checked=False, value=index: self._on_focus(value))
            self.grid.addWidget(button, index // columns, index % columns)
            self._cell_buttons.append(button)
        self.budget_label.setText(
            f"预算 {data.get('units_used')}/{data.get('units_budget')} 单位 · "
            f"4K60 并发 {data.get('concurrent_4k60')}/{data.get('max_concurrent_4k60')}"
        )
        steps = data.get("degrade_steps") or []
        if steps:
            names = {"reduce_cells": "减格", "lower_preview": "预览降至 360P 10 帧", "pause_unfocused": "暂停未聚焦格"}
            self.degrade_label.setText("已降级: " + " → ".join(names.get(step, step) for step in steps))
        else:
            self.degrade_label.setText("")
        self.content_stack.setCurrentIndex(1)
        self._update_summary()

    def _update_summary(self) -> None:
        """在线路数与布局说明只读设备列表与当前布局, 不新增命令."""
        result = accounts.list_devices()
        items = (result.data or {}).get("items", []) if result.ok else []
        online = sum(1 for item in items if item.get("online"))
        total = len(items) if self._wall_id is None else self._layout
        self.online_label.setText(f"{min(online, total)} / {total} 路在线")
        grid = {1: "1 × 1", 4: "2 × 2", 9: "3 × 3", 16: "4 × 4"}
        self.layout_label.setText(f"布局：{grid.get(self._layout, '2 × 2')}")

    def _cell_text(self, index: int, cell: dict[str, object]) -> str:
        names = self._device_names()
        name = names[index] if index < len(names) else "未绑定设备"
        state = CELL_STATE_TEXT.get(str(cell.get("state", "preview")), "预览")
        profile = profile_text(str(cell.get("preview_profile", "preview_480p15"))).removeprefix("预览 ")
        return f"格 {index + 1} · {name}\n{state} · {profile}"

    @staticmethod
    def _device_names() -> list[str]:
        result = accounts.list_devices()
        if not result.ok or not result.data:
            return []
        names: list[str] = []
        for item in result.data.get("items", []):
            suffix = "免确认" if not item.get("require_confirm") else "需审批"
            suffix = suffix if item.get("online") else "离线"
            names.append(f"{item.get('display_name') or item.get('device_id')} · {suffix}")
        return names

    def _sync_layout_buttons(self, count: int) -> None:
        for button, value in zip(self.layout_buttons, (1, 4, 9, 16), strict=False):
            button.setChecked(value == count)

    def _clear_grid(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._cell_buttons.clear()

    def refresh(self) -> None:
        if self._wall_id is not None:
            self._on_refresh()
