# ==========================================================================================
# 文件        : session.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 会话窗: 远端画面容器、状态条、工具栏、文件传输面板、剪贴板与拖拽落点
# 架构定位    : 界面层, 只调用 rdc.commands.api / transfer / clipboard
# 核心功能    : 会话状态与指标呈现、画质切换、传输进度轮询、剪贴板双向同步、拖拽落点
# 职责边界    : 不解码; 不建连; 不写传输状态; 所有进度来自命令返回
# ==========================================================================================

"""会话窗页."""

from __future__ import annotations

import base64

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSpinBox,
    QVBoxLayout,
)

from rdc.commands import api, clipboard, transfer
from rdc.core.result import CommandResult
from rdc.media.quality import DIRECT_PROFILES
from rdc.ui.components import Illustration, StatusDot, chip, fade_in
from rdc.ui.pages.base import PageBase, row


class SessionPage(PageBase):
    """桌面会话窗."""

    def __init__(self) -> None:
        super().__init__(
            "session",
            "会话窗口",
            "远端画面容器与工具栏; 传输与剪贴板全部经命令层, 未连接时相关按钮置灰.",
            scrollable=True,
        )
        self._session_id: str | None = None
        self._job_id: str | None = None
        self._transfer_state = "idle"
        self._dragging = False
        self._build()
        self.body.setSpacing(12)
        self._transfer_timer = QTimer(self)
        self._transfer_timer.setInterval(300)
        self._transfer_timer.timeout.connect(self._poll_transfer)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_rows = QVBoxLayout(toolbar)
        toolbar_rows.setContentsMargins(12, 10, 12, 10)
        toolbar_rows.setSpacing(6)
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setSpacing(6)
        toolbar_layout.addWidget(self.button("返回连接页", self._on_back, icon="arrow-left"))
        self.session_input = QLineEdit()
        self.session_input.setPlaceholderText("会话 ID")
        self.session_input.setMinimumWidth(180)
        self.session_input.editingFinished.connect(self._sync_session_id)
        toolbar_layout.addWidget(self.session_input, 1)
        self.quality_combo = QComboBox()
        self.quality_combo.addItem("auto")
        self.quality_combo.addItems(sorted(DIRECT_PROFILES - {"auto"}))
        self.quality_combo.setFixedWidth(120)
        toolbar_layout.addWidget(self.quality_combo)
        self.apply_quality_button = self.button("应用档位", self._on_quality, icon="gauge")
        toolbar_layout.addWidget(self.apply_quality_button)
        toolbar_rows.addLayout(toolbar_layout)

        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.button("刷新状态", self._on_status, icon="refresh-cw"))
        actions_layout.addWidget(self.button("刷新指标", self._on_metrics, icon="activity"))
        actions_layout.addStretch(1)
        actions_layout.addWidget(self.button("全屏", self._on_fullscreen, icon="maximize"))
        actions_layout.addWidget(self.button("结束会话", self._on_stop, primary=True, icon="phone-off"))
        toolbar_rows.addLayout(actions_layout)
        self.body.addWidget(toolbar)

        self.viewport = QFrame()
        self.viewport.setObjectName("dropTarget")
        self.viewport.setMinimumHeight(280)
        view_layout = QVBoxLayout(self.viewport)
        view_layout.setContentsMargins(24, 18, 24, 18)
        view_layout.setSpacing(6)
        view_layout.addStretch(1)
        self.canvas_illustration = Illustration("session", 260, 168)
        view_layout.addWidget(self.canvas_illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.picture_label = QLabel("未连接 · 远端画面容器")
        self.picture_label.setObjectName("sectionTitle")
        self.picture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        view_layout.addWidget(self.picture_label)
        self.canvas_hint = QLabel("在首页输入设备码与口令, 或从最近会话进入; 连接建立后此处显示远端画面.")
        self.canvas_hint.setObjectName("muted")
        self.canvas_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas_hint.setWordWrap(True)
        view_layout.addWidget(self.canvas_hint)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addStretch(1)
        for text in ("鼠标 / 键盘直通", "剪贴板双向同步", "文件拖拽传输"):
            chips.addWidget(chip(text))
        chips.addStretch(1)
        view_layout.addLayout(chips)
        self.drop_hint = QLabel("松开以传输到远端")
        self.drop_hint.setObjectName("badgeOk")
        self.drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_hint.setVisible(False)
        view_layout.addWidget(self.drop_hint, 0)
        view_layout.addStretch(1)
        self.body.addWidget(self.viewport, 2)
        self.setAcceptDrops(True)

        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(14, 8, 14, 8)
        strip_layout.setSpacing(10)
        strip_layout.addWidget(StatusDot("faint", 8))
        self.status_line = QLabel("状态: 未连接 · 通路: - · 档位: -")
        self.metrics_line = QLabel("帧率 - · p50 - ms · p95 - ms · 码率 - kbps")
        self.metrics_line.setObjectName("muted")
        strip_layout.addWidget(self.status_line)
        strip_layout.addStretch(1)
        strip_layout.addWidget(self.metrics_line)
        self.body.addWidget(strip)

        self._build_transfer()
        self._build_clipboard()
        self._set_session_controls(False)
        self._sync_transfer_buttons("idle")

    def _build_transfer(self) -> None:
        box, layout = self.section("文件传输 · 低优先级通道")
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("源文件或目录路径, 也可直接把文件拖到上方画面区")
        self.destination_input = QLineEdit()
        self.destination_input.setText(transfer.default_destination())
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(0, 100_000)
        self.rate_spin.setSuffix(" KB/s")
        self.rate_spin.setSpecialValueText("不限速")
        layout.addLayout(row(QLabel("源路径"), self.source_input))
        layout.addLayout(row(QLabel("接收目录"), self.destination_input))
        layout.addLayout(row(QLabel("限速"), self.rate_spin))
        layout.addWidget(self.muted("视频地板档 2500 KB/s 预留, 传输不会抢占."))

        self.start_button = self.button("开始传输", self._on_start_transfer, primary=True)
        self.pause_button = self.button("暂停", self._on_pause_transfer)
        self.resume_button = self.button("继续", self._on_resume_transfer)
        self.cancel_button = self.button("取消", self._on_cancel_transfer)
        self.retry_button = self.button("失败重试", self._on_retry_transfer)
        layout.addLayout(
            row(
                self.start_button,
                self.pause_button,
                self.resume_button,
                self.cancel_button,
                self.retry_button,
            )
        )
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setRange(0, 100)
        self.transfer_bar.setValue(0)
        self.transfer_line = QLabel("未开始 · 当前文件 - · 速率 - · 剩余 -")
        self.transfer_line.setObjectName("muted")
        self.transfer_notice = QLabel("未连接时无法传输.")
        self.transfer_notice.setObjectName("badgeWarn")
        layout.addWidget(self.transfer_bar)
        layout.addWidget(self.transfer_line)
        layout.addWidget(self.transfer_notice)
        self.body.addWidget(box)

    def _build_clipboard(self) -> None:
        box, layout = self.section("剪贴板同步")
        self.clipboard_toggle = QCheckBox("开启双向同步（文本 256 KiB 截断, 图片 4 MiB 上限）")
        self.clipboard_toggle.setChecked(True)
        self.clipboard_toggle.toggled.connect(self._on_clipboard_toggle)
        self.clipboard_status = QLabel("状态: 未同步")
        self.clipboard_status.setObjectName("muted")
        self.clipboard_notice = QLabel("")
        self.clipboard_notice.setObjectName("badgeWarn")
        self.clipboard_notice.setWordWrap(True)
        self.send_text_button = self.button("发送本端文本", self._on_send_text)
        self.send_image_button = self.button("发送本端图片", self._on_send_image)
        self.pull_button = self.button("拉取远端文本", self._on_pull_text)
        layout.addWidget(self.clipboard_toggle)
        layout.addLayout(row(self.send_text_button, self.send_image_button, self.pull_button))
        layout.addWidget(self.clipboard_status)
        layout.addWidget(self.clipboard_notice)
        self.body.addWidget(box)

    # ---------------------------------------------------------------- 会话命令
    def _sync_session_id(self) -> None:
        value = self.session_input.text().strip()
        self._session_id = value or None
        self._set_session_controls(self._session_id is not None)
        self.picture_label.setText("已填写会话 ID · 点击刷新状态" if self._session_id else "未连接 · 远端画面容器")

    def set_session(self, session_id: str) -> None:
        """供装配与测试设置当前会话."""
        self.session_input.setText(session_id)
        self._sync_session_id()
        self._on_status()

    def show_connecting(self, peer: str, task_id: str) -> None:
        """连接中过渡态: 不伪造已连接, 只显示任务与等待会话建立."""
        self._session_id = None
        self.session_input.clear()
        self._set_session_controls(False)
        self.status_line.setText(f"状态: 连接中 · 对端 {peer or '-'} · 任务 {task_id}")
        self.picture_label.setText(f"连接中 · 对端 {peer or '-'} · 等待会话建立")
        self.transfer_notice.setText("连接中 · 会话建立后可传输")
        self.drop_hint.setVisible(False)

    def open_session(self, session_id: str, peer: str) -> None:
        """从最近会话进入: 会话 ID 交给命令层查询, 状态以命令返回为准."""
        if not session_id:
            return
        self.session_input.setText(session_id)
        self._session_id = session_id
        self._set_session_controls(True)
        self.picture_label.setText(f"远端画面容器 · 对端 {peer or '-'} · 等待首帧")
        self._on_status()

    def _on_back(self) -> None:
        self.navigate.emit("home")

    def _on_status(self) -> None:
        if self._session_id is None:
            self.transfer_notice.setText("未连接 · 请先填写会话 ID")
            return
        result = api.get_session_status(self._session_id)
        self._render_status(result)
        self.report("查询会话", result)

    def _render_status(self, result: CommandResult) -> None:
        if not result.ok or result.data is None:
            self.status_line.setText(f"状态: 未连接 · {result.message}")
            self.picture_label.setText("未连接 · 远端画面容器")
            return
        data = result.data
        self.status_line.setText(
            f"状态: {data.get('state')} · 通路: {data.get('transport_kind')} · 档位: {data.get('quality_profile')}"
        )
        self.picture_label.setText(f"远端画面容器 · 对端 {data.get('peer_device_id')} · 等待首帧")

    def _on_metrics(self) -> None:
        if self._session_id is None:
            self.metrics_line.setText("未连接 · 无指标")
            return
        result = api.get_session_metrics(self._session_id)
        if result.ok and result.data:
            data = result.data
            self.metrics_line.setText(
                f"帧率 {data.get('fps')} · p50 {data.get('p50_ms')} ms · p95 {data.get('p95_ms')} ms · "
                f"码率 {data.get('bitrate_kbps')} kbps"
            )
        self.report("查询指标", result)

    def _on_quality(self) -> None:
        if self._session_id is None:
            return
        result = api.apply_quality_profile(self._session_id, self.quality_combo.currentText())
        if result.ok and result.data:
            self.status_line.setText(f"状态: 已连接 · 档位: {result.data.get('quality_profile')}")
        self.report("切换画质", result)

    def _on_stop(self) -> None:
        if self._session_id is None:
            return
        result = api.stop_session(self._session_id)
        if result.ok:
            self._session_id = None
            self._set_session_controls(False)
            self.status_line.setText("状态: 未连接 · 通路: - · 档位: -")
            self.picture_label.setText("未连接 · 远端画面容器")
            self.navigate.emit("home")
        self.report("结束会话", result)

    def _on_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            window.showNormal()
        else:
            window.showFullScreen()

    def _set_session_controls(self, connected: bool) -> None:
        for widget in (
            self.apply_quality_button,
            self.send_text_button,
            self.send_image_button,
            self.pull_button,
        ):
            widget.setEnabled(connected)
        self.apply_quality_button.setToolTip("" if connected else "请先建立会话")
        for widget in (self.send_text_button, self.send_image_button, self.pull_button):
            widget.setToolTip("" if connected else "请先建立会话")
        if not connected:
            self.transfer_notice.setText("未连接 · 请先建立会话后再传输")
        self._sync_transfer_buttons(self._transfer_state)

    # ---------------------------------------------------------------- 传输
    def _on_start_transfer(self) -> None:
        self._start_transfer(self.source_input.text().strip())

    def _start_transfer(self, source: str) -> None:
        if self._session_id is None:
            self.transfer_notice.setText("未连接 · 无法传输, 请先建立会话")
            return
        if not source:
            self.transfer_notice.setText("请填写源文件路径, 或把文件拖到画面区")
            return
        result = transfer.start_transfer(
            source,
            self.destination_input.text().strip(),
            rate_limit_kbps=self.rate_spin.value(),
        )
        if result.ok and result.data:
            self._job_id = str(result.data.get("job_id"))
            self._transfer_state = "running"
            self._transfer_timer.start()
            self._render_transfer(result)
        else:
            self.transfer_notice.setText(result.message)
        self.report("开始传输", result)

    def _on_pause_transfer(self) -> None:
        if self._job_id is None:
            return
        result = transfer.pause_transfer(self._job_id)
        if result.ok:
            self._transfer_timer.stop()
            self._transfer_state = "paused"
            self._render_transfer(result)
        self.report("暂停传输", result)

    def _on_resume_transfer(self) -> None:
        if self._job_id is None:
            return
        result = transfer.resume_transfer(self._job_id)
        if result.ok:
            self._transfer_state = "running"
            self._transfer_timer.start()
            self._render_transfer(result)
        self.report("继续传输", result)

    def _on_cancel_transfer(self) -> None:
        if self._job_id is None:
            return
        result = transfer.cancel_transfer(self._job_id)
        if result.ok:
            self._transfer_timer.stop()
            self._transfer_state = "canceled"
            self._render_transfer(result)
        self.report("取消传输", result)

    def _on_retry_transfer(self) -> None:
        if self._job_id is None:
            self._transfer_state = "idle"
            self._start_transfer(self.source_input.text().strip())
            return
        resume = transfer.resume_transfer(self._job_id)
        if resume.ok:
            self._transfer_state = "running"
            self._transfer_timer.start()
            self._render_transfer(resume)
            self.report("重试传输", resume)
            return
        self._job_id = None
        self._transfer_state = "idle"
        self._start_transfer(self.source_input.text().strip())

    def _poll_transfer(self) -> None:
        if self._job_id is None:
            self._transfer_timer.stop()
            return
        result = transfer.get_transfer_progress(self._job_id)
        if result.data:
            self._transfer_state = str(result.data.get("state", self._transfer_state))
            self._render_transfer(result)
            if self._transfer_state in {"completed", "failed", "canceled", "paused"}:
                self._transfer_timer.stop()
        self.report("传输进度", result)

    def _render_transfer(self, result: CommandResult) -> None:
        if result.data is None:
            self.transfer_notice.setText(result.message)
            return
        data = result.data
        percent = int(float(data.get("percent", 0)))
        self.transfer_bar.setValue(percent)
        eta = data.get("eta_seconds")
        self.transfer_line.setText(
            f"{data.get('state')} · 当前文件 {data.get('current_file') or '-'} · "
            f"速率 {data.get('speed_kbps')} KB/s · 剩余 {eta if eta is not None else '-'} s · "
            f"通道 {data.get('channel')} · 限速 {data.get('rate_limit_kbps')} KB/s"
        )
        notice = result.message
        if data.get("clamped"):
            notice += " · 请求限速已按视频地板档收敛"
        self.transfer_notice.setText(notice)
        self._sync_transfer_buttons(str(data.get("state", "idle")))

    def _sync_transfer_buttons(self, state: str) -> None:
        connected = self._session_id is not None
        has_job = self._job_id is not None
        self.start_button.setEnabled(connected and not has_job)
        self.pause_button.setEnabled(connected and has_job and state == "running")
        self.resume_button.setEnabled(connected and has_job and state == "paused")
        self.cancel_button.setEnabled(connected and has_job and state in {"running", "paused"})
        self.retry_button.setEnabled(connected and state in {"failed", "canceled"})
        for button in (self.start_button, self.pause_button, self.resume_button, self.cancel_button, self.retry_button):
            button.setToolTip("" if connected else "请先建立会话")

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
        self._dragging = dragging
        self.drop_hint.setVisible(dragging)
        self.viewport.setProperty("dragging", dragging)
        self.viewport.style().unpolish(self.viewport)
        self.viewport.style().polish(self.viewport)

    def handle_dropped_paths(self, paths: list[str]) -> None:
        """供拖拽与离屏测试调用."""
        if not paths:
            return
        if self._session_id is None:
            self.transfer_notice.setText("未连接 · 拖拽已接收, 但需要先建立会话")
            return
        self.source_input.setText(paths[0])
        self.transfer_notice.setText(f"松开已接收: {paths[0]}")
        self._start_transfer(paths[0])

    # ---------------------------------------------------------------- 剪贴板
    def _on_clipboard_toggle(self, enabled: bool) -> None:
        result = clipboard.set_clipboard_enabled(enabled)
        self.send_text_button.setEnabled(enabled and self._session_id is not None)
        self.send_image_button.setEnabled(enabled and self._session_id is not None)
        self.pull_button.setEnabled(enabled and self._session_id is not None)
        self.clipboard_status.setText(result.message)
        self.report("切换剪贴板", result)

    def _on_send_text(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            self.clipboard_notice.setText("本端剪贴板没有文本")
            return
        result = clipboard.push_clipboard("desktop", "text", text)
        self._render_clipboard(result)

    def _on_send_image(self) -> None:
        image = QApplication.clipboard().image()
        if image.isNull():
            self.clipboard_notice.setText("本端剪贴板没有图片")
            return
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, b"PNG")
        payload = base64.b64encode(buffer.data().data()).decode("ascii")
        result = clipboard.push_clipboard("desktop", "image", payload)
        self._render_clipboard(result)

    def _on_pull_text(self) -> None:
        result = clipboard.pull_clipboard("desktop")
        if result.ok and result.data and result.data.get("applied") and "text" in result.data:
            QApplication.clipboard().setText(str(result.data["text"]))
        self._render_clipboard(result)

    def _render_clipboard(self, result: CommandResult) -> None:
        self.clipboard_status.setText(result.message)
        self.clipboard_notice.setText(result.message if "截断" in result.message or not result.ok else "")
        self.report("剪贴板同步", result)

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        status = clipboard.get_clipboard_status()
        if status.data:
            self.clipboard_toggle.blockSignals(True)
            self.clipboard_toggle.setChecked(bool(status.data.get("enabled", True)))
            self.clipboard_toggle.blockSignals(False)
        self._sync_session_id()
