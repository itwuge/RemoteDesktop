# ==========================================================================================
# 文件        : transfer.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 文件传输页: 会话内传输任务列表、进度、暂停/续传/取消/失败重试与拖拽落点
# 架构定位    : 界面层, 只调用 rdc.commands.api 与 rdc.commands.transfer
# 核心功能    : 无会话空态、任务分组、轮询进度、任务详情卡、传输参数卡
# 职责边界    : 不分块; 不校验哈希; 不写传输状态; 不改写命令返回文案
# ==========================================================================================

"""文件传输页."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QShowEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api, transfer
from rdc.core.result import CommandResult
from rdc.ui.components import (
    CopyButton,
    EmptyState,
    MenuItem,
    chip,
    copy_to_clipboard,
    fade_in,
    open_menu,
    show_toast,
)
from rdc.ui.copy import error_text, message_text
from rdc.ui.pages.base import PageBase, row

STATE_LABELS = {
    "running": "进行中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
STATE_TONES = {"running": "ok", "completed": "ok", "paused": "warn", "failed": "danger"}
GROUP_RANK = {"running": 0, "paused": 1, "completed": 2, "failed": 3, "canceled": 4}
FILTERS = (
    ("全部分组", ""),
    ("进行中", "active"),
    ("已完成", "completed"),
    ("失败", "failed"),
)
COLUMNS = ("状态", "文件", "进度", "速率", "剩余", "大小", "说明")
DETAIL_FIELDS = (
    ("文件", "file"),
    ("大小", "size"),
    ("目标路径", "target"),
    ("平均速度", "speed"),
    ("预计剩余", "eta"),
    ("校验方式", "verify"),
)

NO_SESSION, SESSION_FAILED, MAIN = range(3)


def _human_size(size: int) -> str:
    """字节数转可读文本; 与命令层口径保持一致, 界面不参与计算."""
    value = float(size)
    for unit in ("字节", "千字节", "兆字节", "吉字节"):
        if value < 1024 or unit == "吉字节":
            return f"{int(value)} {unit}" if unit == "字节" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} 吉字节"


def _format_eta(seconds: Any) -> str:
    """剩余时间: 秒数转「N 秒」或「M 分 S 秒」, 未知显示短横线."""
    if not isinstance(seconds, int) or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds} 秒"
    return f"{seconds // 60} 分 {seconds % 60} 秒"


class TransferPage(PageBase):
    """会话内文件传输."""

    def __init__(self) -> None:
        super().__init__(
            "transfer",
            "文件传输",
            "低优先级通道, 分块 256 千字节逐块回读校验; 失败原因取自命令层.",
            scrollable=True,
        )
        self._session: dict[str, Any] | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._selected: str | None = None
        self._channel_ready = False
        self._build()
        self.body.setSpacing(12)
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._poll_jobs)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        self.session_chip = QLabel("未建立会话")
        self.session_chip.setObjectName("badgeWarn")
        self.header_actions.addWidget(self.session_chip)
        self.header_actions.addWidget(self.button("选择文件", self._on_pick_file, primary=True, icon="folder-open"))
        self.header_actions.addWidget(self.button("刷新", self.refresh, icon="refresh-cw"))

        self.content_stack = QStackedWidget()

        self.no_session = EmptyState(
            "session",
            "需要先建立会话",
            "文件传输只在已建立的会话内进行. 请先在首页发起连接, 建立会话后再回到这里传输文件.",
            action_text="去建立会话",
            action_icon="plug-zap",
            action=lambda: self.navigate.emit("home"),
        )
        self.content_stack.addWidget(self.no_session)

        self.session_failed = EmptyState(
            "session",
            "无法读取会话",
            "命令层当前不可用, 无法确认是否存在活动会话. 请检查后端连接后重试.",
            action_text="重试",
            action_icon="refresh-cw",
            action=self.refresh,
        )
        self.content_stack.addWidget(self.session_failed)

        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.session_line = QLabel("活动会话 -")
        self.session_line.setObjectName("cardMeta")
        layout.addWidget(self.session_line)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(12, 8, 12, 8)
        toolbar_layout.setSpacing(10)
        self.channel_banner = QLabel("未接入会话通道: 当前无法跨端传输.")
        self.channel_banner.setObjectName("badgeWarn")
        self.channel_banner.setWordWrap(True)
        self.channel_banner.setVisible(False)
        toolbar_layout.addWidget(self.channel_banner, 1)
        direction = QLabel("方向：本机 → 对端")
        direction.setObjectName("cardMeta")
        toolbar_layout.addWidget(direction)
        self.counts_label = QLabel("队列 0 · 进行中 0 · 已完成 0 · 失败 0")
        self.counts_label.setObjectName("chip")
        toolbar_layout.addWidget(self.counts_label)
        layout.addWidget(toolbar)

        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("dropTarget")
        self.drop_zone.setMinimumHeight(66)
        drop_layout = QVBoxLayout(self.drop_zone)
        drop_layout.setContentsMargins(18, 12, 18, 12)
        drop_layout.setSpacing(4)
        self.drop_title = QLabel("把文件或文件夹拖到这里")
        self.drop_title.setObjectName("sectionTitle")
        self.drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(self.drop_title)
        drop_hint = self.muted("松开即调用传输命令; 也可以填写源路径后点「开始传输」.")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(drop_hint)
        layout.addWidget(self.drop_zone)
        self.setAcceptDrops(True)

        params_card, params_layout, params_head = self._card("传输参数")
        params_head.addWidget(chip("源路径必填"))
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("源文件或目录的本地路径")
        self.destination_input = QLineEdit()
        self.destination_input.setText(transfer.default_destination())
        self.destination_input.setPlaceholderText("接收目录, 留空使用默认目录")
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(0, 100_000)
        self.rate_spin.setSuffix(" 千字节/秒")
        self.rate_spin.setSpecialValueText("不限速")
        self.rate_spin.setToolTip("视频地板档 2500 千字节/秒由命令层预留, 传输不会抢占.")
        self.rate_spin.setFixedWidth(128)
        params_layout.addLayout(
            row(
                self._form_label("源路径"),
                self.source_input,
                self.button("选择文件", self._on_pick_file, icon="folder-open"),
            )
        )
        params_layout.addLayout(
            row(
                self._form_label("接收目录"),
                self.destination_input,
                self._form_label("限速"),
                self.rate_spin,
            )
        )
        self.start_button = self.button("开始传输", self._on_start, primary=True, icon="upload")
        params_layout.addLayout(row(self.start_button, self.muted("依赖活动会话与已绑定的会话通道.")))
        self.notice = QLabel("填写源路径后开始传输.")
        self.notice.setObjectName("muted")
        self.notice.setWordWrap(True)
        params_layout.addWidget(self.notice)
        layout.addWidget(params_card)

        self.filter_combo = QComboBox()
        for label, value in FILTERS:
            self.filter_combo.addItem(label, value)
        self.filter_combo.currentIndexChanged.connect(self._render)
        layout.addLayout(row(self.filter_combo, self.muted("选中一行任务, 在下方查看详情.")))

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        header = self.table.horizontalHeader()
        for column in range(len(COLUMNS) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(160)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_menu)
        layout.addWidget(self.table, 1)

        self.list_hint = QLabel("还没有传输任务.")
        self.list_hint.setObjectName("muted")
        self.list_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_hint.setMinimumHeight(120)
        layout.addWidget(self.list_hint, 1)

        detail_card, detail_layout, detail_head = self._card("任务详情")
        self.detail_chip = chip("未选择")
        detail_head.addWidget(self.detail_chip)
        detail_grid = QGridLayout()
        detail_grid.setHorizontalSpacing(16)
        detail_grid.setVerticalSpacing(6)
        self.detail_values: dict[str, QLabel] = {}
        for offset, (caption, key) in enumerate(DETAIL_FIELDS):
            caption_label = self._form_label(caption)
            value_label = QLabel("-")
            value_label.setObjectName("mono" if key == "target" else "muted")
            value_label.setWordWrap(key == "target")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            detail_grid.addWidget(caption_label, offset, 0)
            detail_grid.addWidget(value_label, offset, 1)
            self.detail_values[key] = value_label
        detail_grid.setColumnStretch(1, 1)
        detail_layout.addLayout(detail_grid)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        detail_layout.addWidget(self.progress_bar)

        self.detail_line = QLabel("未选择任务.")
        self.detail_line.setObjectName("muted")
        self.detail_line.setWordWrap(True)
        detail_layout.addWidget(self.detail_line)

        self.pause_button = self.button("暂停", self._on_pause, icon="square")
        self.resume_button = self.button("继续", self._on_resume, icon="play")
        self.cancel_button = self.button("取消", self._on_cancel, icon="x")
        self.retry_button = self.button("失败重试", self._on_retry, icon="refresh-cw")
        self.path_button = self.button("复制路径", self._copy_selected_path, icon="copy")
        self.detail_copy = CopyButton("", "复制任务详情: 路径、错误原因与整体摘要")
        detail_layout.addLayout(
            row(
                self.pause_button,
                self.resume_button,
                self.cancel_button,
                self.retry_button,
                self.path_button,
                self.detail_copy,
            )
        )
        detail_layout.addWidget(self.muted("右键任务行可暂停、取消、重试或打开所在位置。"))
        layout.addWidget(detail_card)

        self.content_stack.addWidget(main)
        self.body.addWidget(self.content_stack, 1)
        self._set_state(NO_SESSION)
        self._sync_buttons()

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        caption = QLabel(title)
        caption.setObjectName("cardTitle")
        head.addWidget(caption)
        head.addStretch(1)
        layout.addLayout(head)
        return card, layout, head

    @staticmethod
    def _form_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("formLabel")
        return label

    def _set_state(self, state: int) -> None:
        self.content_stack.setCurrentIndex(state)

    def _set_notice(self, text: str, *, ok: bool | None = None) -> None:
        self.notice.setText(text)
        self.notice.setObjectName("muted" if ok is None else ("badgeOk" if ok else "badgeWarn"))
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.update()

    # ---------------------------------------------------------------- 会话
    def refresh(self) -> None:
        result = api.list_sessions()
        self.report("刷新会话", result)
        if not result.ok or result.data is None:
            self._session = None
            self._channel_ready = False
            self.channel_banner.setVisible(False)
            self.session_chip.setText("会话未知")
            self._set_chip(False)
            reason = error_text(result.error_code, result.message)
            self.session_failed.desc_label.setText(f"{reason}. 请确认命令层与后端连接后重试.")
            self._set_state(SESSION_FAILED)
            return
        items = [dict(item) for item in result.data.get("items", [])]
        if not items:
            self._session = None
            self._channel_ready = False
            self.channel_banner.setVisible(False)
            self.session_chip.setText("未建立会话")
            self._set_chip(False)
            self._set_state(NO_SESSION)
            return
        self._session = items[0]
        session_id = str(self._session.get("session_id", ""))
        peer = str(self._session.get("peer_device_id", ""))
        route = "中继" if self._session.get("transport_kind") == "relay" else "直连"
        self.session_chip.setText(f"会话 {session_id[:8]}…")
        self._set_chip(True)
        self.session_line.setText(f"活动会话 {session_id} · 对端 {peer or '-'} · {route}")
        self._set_state(MAIN)
        self._refresh_channel()
        self._render()

    def _refresh_channel(self) -> None:
        """通道状态条: 未接入禁止开始; 降级通道必须显示对中继可见与未达端到端加密."""
        result = transfer.get_transfer_channel_status()
        data = result.data or {}
        mode = str(data.get("mode") or "unbound")
        self._channel_ready = mode in {"channel", "local_staging"}
        if mode == "channel" and data.get("visible_to_relay"):
            text = "当前走信令降级通道：文件内容与剪贴板对中继可见，未达端到端加密。"
            style = "badgeWarn"
        elif mode == "channel":
            text = "已接入会话通道：文件发送到对端, 由对端逐块校验并落盘。"
            style = "badgeOk"
        elif mode == "local_staging":
            text = "本机暂存模式：文件只在本机复制, 不会发送到对端。"
            style = "badgeWarn"
        else:
            text = "未接入会话通道：当前无法跨端传输, 请先建立会话并绑定通道。"
            style = "badgeWarn"
        self.channel_banner.setText(text)
        self.channel_banner.setObjectName(style)
        self.channel_banner.style().unpolish(self.channel_banner)
        self.channel_banner.style().polish(self.channel_banner)
        self.channel_banner.update()
        self.channel_banner.setVisible(True)

    def _set_chip(self, ok: bool) -> None:
        self.session_chip.setObjectName("badgeOk" if ok else "badgeWarn")
        self.session_chip.style().unpolish(self.session_chip)
        self.session_chip.style().polish(self.session_chip)
        self.session_chip.update()

    # ---------------------------------------------------------------- 命令
    def _on_pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择要传输的文件")
        if path:
            self.source_input.setText(path)

    def _on_start(self) -> None:
        self._start_transfer(self.source_input.text().strip())

    def _start_transfer(self, source: str) -> None:
        if self._session is None:
            self._set_notice("需要先建立会话, 当前无法传输.")
            return
        if not source:
            self._set_notice("请填写源路径, 或把文件拖到上方落点.")
            return
        result = transfer.start_transfer(
            source,
            self.destination_input.text().strip(),
            rate_limit_kbps=self.rate_spin.value(),
        )
        self.report("开始传输", result)
        if not result.ok or not result.data:
            self._set_notice(error_text(result.error_code, result.message))
            return
        data = dict(result.data)
        job_id = str(data.get("job_id", ""))
        if not job_id:
            self._set_notice(error_text(result.error_code, result.message))
            return
        self._jobs[job_id] = data
        self._selected = job_id
        self._set_notice(result.message, ok=True)
        self._render()
        self._sync_timer()

    def _on_pause(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        self._apply_job_command("暂停传输", transfer.pause_transfer(str(job.get("job_id", ""))))

    def _on_resume(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        self._apply_job_command("继续传输", transfer.resume_transfer(str(job.get("job_id", ""))))

    def _on_cancel(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        self._apply_job_command("取消传输", transfer.cancel_transfer(str(job.get("job_id", ""))))

    def _on_retry(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        source = str(job.get("source", ""))
        destination = str(job.get("destination", ""))
        result = transfer.start_transfer(
            source,
            destination,
            rate_limit_kbps=int(job.get("rate_limit_kbps", 0)),
        )
        self.report("重试传输", result)
        if not result.ok or not result.data:
            self._set_notice(error_text(result.error_code, result.message))
            return
        data = dict(result.data)
        job_id = str(data.get("job_id", ""))
        if job_id:
            self._jobs[job_id] = data
            self._selected = job_id
        self._set_notice(result.message, ok=True)
        self._render()
        self._sync_timer()

    def _apply_job_command(self, action: str, result: CommandResult) -> None:
        """暂停/继续/取消的共用处理: 成功时用命令返回的快照覆盖本页记录."""
        self.report(action, result)
        if result.data is not None:
            data = dict(result.data)
            job_id = str(data.get("job_id", ""))
            if job_id:
                self._jobs[job_id] = data
                self._selected = job_id
        self._set_notice(message_text(result.error_code, result.message, ok=result.ok))
        self._render()
        self._sync_timer()

    def handle_dropped_paths(self, paths: list[str]) -> None:
        """供拖拽与离屏测试调用."""
        if not paths:
            return
        if self._session is None:
            self._set_notice("拖拽已接收, 但需要先建立会话.")
            return
        self.source_input.setText(paths[0])
        self._start_transfer(paths[0])

    # ---------------------------------------------------------------- 轮询
    def _poll_jobs(self) -> None:
        active = [job_id for job_id, job in self._jobs.items() if job.get("state") == "running"]
        if not active:
            self._timer.stop()
            return
        changed = False
        for job_id in active:
            previous = str(self._jobs.get(job_id, {}).get("state", ""))
            result = transfer.get_transfer_progress(job_id)
            if result.data is None:
                continue
            data = dict(result.data)
            self._jobs[job_id] = data
            changed = True
            if not result.ok:
                self.report("传输进度", result)
                self._set_notice(error_text(result.error_code, result.message), ok=False)
            elif data.get("state") == "completed" and previous != "completed":
                self._set_notice(result.message, ok=True)
        if changed:
            self._render()
        self._sync_timer()

    def _sync_timer(self) -> None:
        running = any(job.get("state") == "running" for job in self._jobs.values())
        if running and not self._timer.isActive():
            self._timer.start()
        elif not running and self._timer.isActive():
            self._timer.stop()

    # ---------------------------------------------------------------- 渲染
    def _visible_jobs(self) -> list[dict[str, Any]]:
        group = str(self.filter_combo.currentData())
        if group == "active":
            return [job for job in self._jobs.values() if job.get("state") in {"running", "paused"}]
        if group == "completed":
            return [job for job in self._jobs.values() if job.get("state") == "completed"]
        if group == "failed":
            return [job for job in self._jobs.values() if job.get("state") in {"failed", "canceled"}]
        return list(self._jobs.values())

    def _render(self) -> None:
        grouped = sorted(self._visible_jobs(), key=lambda job: GROUP_RANK.get(str(job.get("state", "")), 9))
        self.table.blockSignals(True)
        self.table.setRowCount(len(grouped))
        selected_row = -1
        for index, job in enumerate(grouped):
            job_id = str(job.get("job_id", ""))
            if job_id == self._selected:
                selected_row = index
            for column, value in enumerate(self._row_values(job)):
                cell = QTableWidgetItem(value)
                cell.setToolTip(f"{job.get('source', '')} → {job.get('destination', '')}")
                if column in {2, 3, 4, 5}:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                cell.setData(Qt.ItemDataRole.UserRole, job_id)
                self.table.setItem(index, column, cell)
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
            if self._selected is not None and self._selected not in {str(job.get("job_id", "")) for job in grouped}:
                self._selected = None
        self.table.blockSignals(False)

        has_rows = bool(grouped)
        self.table.setVisible(has_rows)
        self.list_hint.setVisible(not has_rows)
        if not has_rows:
            self.list_hint.setText(self._empty_hint())
        self._update_counts()
        self._update_detail()
        self._sync_buttons()

    def _row_values(self, job: dict[str, Any]) -> list[str]:
        state = str(job.get("state", ""))
        percent = float(job.get("percent", 0) or 0)
        sent = int(job.get("sent_bytes", 0) or 0)
        total = int(job.get("total_bytes", 0) or 0)
        current = str(job.get("current_file") or self._base_name(job.get("source", "")))
        speed = f"{int(job.get('speed_kbps', 0) or 0)} 千字节/秒" if state == "running" else "-"
        note = self._job_note(job)
        return [
            STATE_LABELS.get(state, state or "-"),
            current or "-",
            f"{percent:.1f}%",
            speed,
            _format_eta(job.get("eta_seconds")) if state == "running" else "-",
            f"{_human_size(sent)} / {_human_size(total)}",
            note,
        ]

    @staticmethod
    def _job_note(job: dict[str, Any]) -> str:
        state = str(job.get("state", ""))
        if state == "failed":
            return error_text(str(job.get("error_code") or ""), str(job.get("error_message") or ""))
        if state == "completed":
            digest = str(job.get("digest") or "")
            return f"整体哈希一致 {digest[:12]}…" if digest else "传输完成"
        if state == "paused":
            return "可继续续传"
        if state == "canceled":
            return "已取消, 目标分块保留"
        if state == "running" and str(job.get("phase") or "") == "verifying":
            return "已发完, 等待对端校验与确认"
        return "低优先级通道"

    def _empty_hint(self) -> str:
        group = str(self.filter_combo.currentData())
        if not self._jobs:
            return "还没有传输任务. 拖入文件或填写源路径后点「开始传输」."
        labels = {"active": "没有进行中的任务", "completed": "还没有已完成的任务", "failed": "没有失败的任务"}
        return f"{labels.get(group, '当前分组没有任务')}."

    def _update_counts(self) -> None:
        active = sum(1 for job in self._jobs.values() if job.get("state") in {"running", "paused"})
        completed = sum(1 for job in self._jobs.values() if job.get("state") == "completed")
        failed = sum(1 for job in self._jobs.values() if job.get("state") in {"failed", "canceled"})
        self.counts_label.setText(f"队列 {len(self._jobs)} · 进行中 {active} · 已完成 {completed} · 失败 {failed}")

    def _update_detail(self) -> None:
        job = self._selected_job()
        if job is None:
            self._fill_detail(None)
            self._set_detail_chip("", "未选择")
            self.progress_bar.setValue(0)
            self.detail_line.setText("未选择任务.")
            self.detail_copy.set_value("")
            self.path_button.setEnabled(False)
            return
        self._fill_detail(job)
        state = str(job.get("state") or "")
        self._set_detail_chip(state, STATE_LABELS.get(state, "-"))
        self.detail_copy.set_value(self._job_summary(job), "复制任务详情: 路径、错误原因与整体摘要")
        self.path_button.setEnabled(bool(job.get("source")))
        self.progress_bar.setValue(int(float(job.get("percent", 0) or 0)))
        sent = _human_size(int(job.get("sent_bytes", 0) or 0))
        total = _human_size(int(job.get("total_bytes", 0) or 0))
        eta = _format_eta(job.get("eta_seconds"))
        if state == "running" and str(job.get("phase") or "") == "verifying":
            self.detail_line.setText(f"已发完 {sent} / {total} · 等待对端校验与整文件哈希确认, 确认前不视为完成")
            return
        self.detail_line.setText(
            f"{STATE_LABELS.get(state, '-')} · 当前文件 {job.get('current_file') or '-'} · "
            f"已传 {sent} / {total} · 速率 {int(job.get('speed_kbps', 0) or 0)} 千字节/秒 · 剩余 {eta}"
        )

    def _fill_detail(self, job: dict[str, Any] | None) -> None:
        """任务详情卡的各字段: 文件、大小、目标路径、平均速度、预计剩余、校验方式."""
        if job is None:
            for value in self.detail_values.values():
                value.setText("-")
                value.setToolTip("")
            return
        state = str(job.get("state") or "")
        source = str(job.get("source") or "")
        destination = str(job.get("destination") or "")
        digest = str(job.get("digest") or "")
        file_name = str(job.get("current_file") or self._base_name(source) or "-")
        values = {
            "file": file_name,
            "size": _human_size(int(job.get("total_bytes", 0) or 0)),
            "target": destination or "-",
            "speed": f"{int(job.get('speed_kbps', 0) or 0)} 千字节/秒" if state == "running" else "-",
            "eta": _format_eta(job.get("eta_seconds")) if state == "running" else "-",
            "verify": f"SHA-256 {digest[:12]}…" if digest else "SHA-256",
        }
        for key, text in values.items():
            self.detail_values[key].setText(text)
        self.detail_values["file"].setToolTip(source)
        self.detail_values["target"].setToolTip(destination)

    def _set_detail_chip(self, state: str, text: str) -> None:
        self.detail_chip.setText(text)
        self.detail_chip.setProperty("tone", STATE_TONES.get(state, ""))
        self.detail_chip.style().unpolish(self.detail_chip)
        self.detail_chip.style().polish(self.detail_chip)
        self.detail_chip.update()

    def _on_table_menu(self, position: QPoint) -> None:
        """文件传输右键菜单: 暂停 / 取消 / 重试 / 在文件夹中显示 / 复制路径."""
        index = self.table.indexAt(position)
        if index.isValid():
            self.table.selectRow(index.row())
        job = self._selected_job()
        state = str(job.get("state") or "") if job else ""
        source = str(job.get("source") or "") if job else ""
        items: list[MenuItem | None] = [
            MenuItem(
                "暂停",
                self._on_pause,
                enabled=state == "running",
                tip="" if state == "running" else "只有进行中的任务可以暂停",
            ),
            MenuItem(
                "取消",
                self._on_cancel,
                enabled=state in {"running", "paused"},
                danger=True,
                tip="" if state in {"running", "paused"} else "只有进行中或已暂停的任务可以取消",
            ),
            MenuItem(
                "重试",
                self._on_retry,
                enabled=state in {"failed", "canceled"},
                tip="" if state in {"failed", "canceled"} else "只有失败或已取消的任务可以重试",
            ),
            None,
            MenuItem("在文件夹中显示", self._reveal_selected, enabled=bool(source) and Path(source).parent.exists()),
            MenuItem("复制路径", self._copy_selected_path, enabled=bool(source)),
        ]
        open_menu(self.table, position, items)

    def _reveal_selected(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        folder = Path(str(job.get("source") or "")).parent
        if not folder.exists():
            self._set_notice("源文件所在目录不存在, 无法在文件夹中显示")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _copy_selected_path(self) -> None:
        job = self._selected_job()
        if job is None:
            return
        if copy_to_clipboard(str(job.get("source") or "")):
            show_toast(self)

    @staticmethod
    def _job_summary(job: dict[str, Any]) -> str:
        state = str(job.get("state") or "")
        digest = str(job.get("digest") or "")
        note = TransferPage._job_note(job)
        return (
            f"任务 {job.get('job_id') or '未知'}\n"
            f"状态 {STATE_LABELS.get(state, '未知')}\n"
            f"源路径 {job.get('source') or '未知'}\n"
            f"目标路径 {job.get('destination') or '未知'}\n"
            f"说明 {note}\n"
            f"整体摘要 {digest or '未知'}"
        )

    def _selected_job(self) -> dict[str, Any] | None:
        if self._selected is None:
            return None
        return self._jobs.get(self._selected)

    def _on_select(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        if not rows:
            return
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return
        self._selected = str(item.data(Qt.ItemDataRole.UserRole) or "") or None
        self._update_detail()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        job = self._selected_job()
        state = str(job.get("state", "")) if job is not None else ""
        has_session = self._session is not None
        self.start_button.setEnabled(has_session and self._channel_ready)
        self.pause_button.setEnabled(has_session and state == "running")
        self.resume_button.setEnabled(has_session and state == "paused")
        self.cancel_button.setEnabled(has_session and state in {"running", "paused"})
        self.retry_button.setEnabled(has_session and state in {"failed", "canceled"})
        self.path_button.setEnabled(bool(job and job.get("source")))
        for button in (self.pause_button, self.resume_button, self.cancel_button, self.retry_button):
            button.setToolTip("" if state else "请先选择一个任务")

    # ---------------------------------------------------------------- 拖拽
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_dragging(True)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_dragging(False)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.toLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.handle_dropped_paths(paths)

    def _set_dragging(self, dragging: bool) -> None:
        self.drop_zone.setProperty("dragging", dragging)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)
        self.drop_zone.update()

    @staticmethod
    def _base_name(path: Any) -> str:
        """从完整路径取末段名称; 仅用于展示, 不解析文件系统."""
        text = str(path or "").rstrip("\\/")
        return text.replace("\\", "/").rsplit("/", 1)[-1]
