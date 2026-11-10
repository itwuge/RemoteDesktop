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
import time
from typing import Any

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPixmap,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api, clipboard, transfer
from rdc.core.result import CommandResult
from rdc.media.h264 import DecodedFrame
from rdc.media.quality import DIRECT_PROFILES
from rdc.ui.components import CopyButton, Illustration, StatusDot, chip, fade_in
from rdc.ui.copy import error_text, message_text
from rdc.ui.pages.base import PageBase, row

SPECIAL_KEYS = {
    Qt.Key.Key_Return: "Return",
    Qt.Key.Key_Enter: "Return",
    Qt.Key.Key_Backspace: "BackSpace",
    Qt.Key.Key_Tab: "Tab",
    Qt.Key.Key_Escape: "Escape",
    Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Left: "Left",
    Qt.Key.Key_Right: "Right",
    Qt.Key.Key_Up: "Up",
    Qt.Key.Key_Down: "Down",
    Qt.Key.Key_Home: "Home",
    Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "Page_Up",
    Qt.Key.Key_PageDown: "Page_Down",
    Qt.Key.Key_Space: "space",
}

# 状态与档位一律给中文; 封装与握手版本只保留数字短标
STATE_TEXT = {
    "idle": "未连接",
    "connecting": "连接中",
    "connected": "已连接",
    "streaming": "正在传输",
    "reconnecting": "正在重连",
    "recovered": "已恢复",
    "failed": "连接失败",
    "closed": "已断开",
}
TRANSPORT_TEXT = {"direct": "直连", "relay": "中继"}
PROFILE_TEXT = {
    "auto": "自适应",
    "4K60": "4K 60 帧",
    "1440p60": "1440P 60 帧",
    "1080p60": "1080P 60 帧",
    "1080p30": "1080P 30 帧",
}
TRANSFER_TEXT = {
    "idle": "未开始",
    "running": "传输中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
CHANNEL_TEXT = {"low_priority": "低优先级通道", "session": "会话通道"}
INFO_TITLES = (
    ("device", "设备编号"),
    ("endpoint", "接入地址"),
    ("access_code", "连接码"),
    ("error", "错误原因"),
    ("log", "日志片段"),
    ("protocol", "协议版本"),
)
STALL_SECONDS = 2.0
# 右列宽度克制在 320 逻辑像素, 保证画面区不被压得过窄; 内容超出时右列内部滚动
SIDE_WIDTH = 320


def _metric_text(value: object) -> str:
    """统计缺失一律显示破折号, 不以常量代填; 整数值去掉多余小数位."""
    if value is None:
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class FrameBridge(QObject):
    """把解码线程的画面搬回界面线程; 只做线程转交, 不做解码."""

    frame_ready = Signal(object)


class SessionPage(PageBase):
    """桌面会话窗."""

    def __init__(self) -> None:
        super().__init__(
            "session",
            "会话窗口",
            "远端画面容器与工具栏; 传输与剪贴板全部经命令层, 未连接时相关按钮置灰.",
            scrollable=False,
        )
        self._session_id: str | None = None
        self._job_id: str | None = None
        self._task_id: str | None = None
        self._transfer_state = "idle"
        self._dragging = False
        self._frame_size = (0, 0)
        self._last_pixels: bytes | None = None
        self._subscribed = False
        self._state = "idle"
        self._message = ""
        self._transport = "-"
        self._profile = "-"
        self._protocol: tuple[int, int] = (0, 0)
        self._last_frame_at = 0.0
        self._log_lines: list[str] = []
        self._info: dict[str, str] = {}
        self._info_labels: dict[str, QLabel] = {}
        self._info_buttons: dict[str, CopyButton] = {}
        self._bridge = FrameBridge()
        self._bridge.frame_ready.connect(self._on_frame)
        self._build()
        self.body.setSpacing(12)
        self._transfer_timer = QTimer(self)
        self._transfer_timer.setInterval(300)
        self._transfer_timer.timeout.connect(self._poll_transfer)
        self._subscribe_timer = QTimer(self)
        self._subscribe_timer.setInterval(1000)
        self._subscribe_timer.timeout.connect(self._subscribe_frames)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(1000)
        self._stats_timer.timeout.connect(self._poll_stats)
        self._task_timer = QTimer(self)
        self._task_timer.setInterval(500)
        self._task_timer.timeout.connect(self._poll_task)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        fade_in(self)

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        work = QHBoxLayout()
        work.setSpacing(12)
        stage = QVBoxLayout()
        stage.setSpacing(10)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_rows = QVBoxLayout(toolbar)
        toolbar_rows.setContentsMargins(12, 10, 12, 10)
        toolbar_rows.setSpacing(6)
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setSpacing(6)
        toolbar_layout.addWidget(self.button("返回连接页", self._on_back, icon="arrow-left"))
        self.session_input = QLineEdit()
        self.session_input.setPlaceholderText("会话编号")
        self.session_input.setMinimumWidth(180)
        self.session_input.editingFinished.connect(self._sync_session_id)
        toolbar_layout.addWidget(self.session_input, 1)
        self.quality_combo = QComboBox()
        for profile in ("auto", *sorted(DIRECT_PROFILES - {"auto"})):
            self.quality_combo.addItem(PROFILE_TEXT.get(profile, profile), profile)
        self.quality_combo.setFixedWidth(120)
        toolbar_layout.addWidget(self.quality_combo)
        self.apply_quality_button = self.button("应用档位", self._on_quality, icon="gauge")
        toolbar_layout.addWidget(self.apply_quality_button)
        toolbar_rows.addLayout(toolbar_layout)

        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.button("刷新状态", self._on_status, icon="refresh-cw"))
        actions_layout.addWidget(self.button("刷新指标", self._on_metrics, icon="activity"))
        self.copy_toast = QLabel("已复制")
        self.copy_toast.setObjectName("badgeOk")
        self.copy_toast.setVisible(False)
        actions_layout.addWidget(self.copy_toast)
        actions_layout.addStretch(1)
        actions_layout.addWidget(self.button("请求关键帧", self._on_request_keyframe, icon="refresh-cw"))
        actions_layout.addWidget(self.button("全屏", self._on_fullscreen, icon="maximize"))
        actions_layout.addWidget(self.button("断开连接", self._on_stop, primary=True, icon="phone-off"))
        toolbar_rows.addLayout(actions_layout)
        stage.addWidget(toolbar)

        self.viewport = QFrame()
        self.viewport.setObjectName("dropTarget")
        self.viewport.setMinimumHeight(280)
        view_layout = QVBoxLayout(self.viewport)
        view_layout.setContentsMargins(24, 18, 24, 18)
        view_layout.setSpacing(6)
        view_layout.addStretch(1)
        self.canvas_illustration = Illustration("session", 260, 168)
        view_layout.addWidget(self.canvas_illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.frame_label = QLabel()
        self.frame_label.setObjectName("remoteFrame")
        self.frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_label.setMinimumSize(320, 180)
        self.frame_label.setScaledContents(False)
        self.frame_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.frame_label.setMouseTracking(True)
        self.frame_label.setVisible(False)
        self.frame_label.installEventFilter(self)
        self.frame_label.setToolTip("点击画面后可直接使用键盘与鼠标操作远端")
        view_layout.addWidget(self.frame_label, 1)
        self.recovery_label = QLabel("画面恢复中 · 正在等待关键帧")
        self.recovery_label.setObjectName("badgeWarn")
        self.recovery_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.recovery_label.setVisible(False)
        view_layout.addWidget(self.recovery_label)
        self.picture_label = QLabel("未连接 · 远端画面容器")
        self.picture_label.setObjectName("sectionTitle")
        self.picture_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        view_layout.addWidget(self.picture_label)
        self.canvas_hint = QLabel("在首页输入设备编号与连接码, 或从最近会话进入; 连接建立后此处显示远端画面.")
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
        stage.addWidget(self.viewport, 1)
        self.setAcceptDrops(True)

        strip = QFrame()
        strip.setObjectName("statusStrip")
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(14, 8, 14, 8)
        strip_layout.setSpacing(10)
        strip_layout.addWidget(StatusDot("faint", 8))
        self.status_line = QLabel("状态：未连接 · 通路：- · 档位：-")
        self.status_line.setWordWrap(True)
        strip_layout.addWidget(self.status_line, 1)
        stage.addWidget(strip)

        for widget in (self.viewport, self.frame_label):
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(self._on_context_menu)

        work.addLayout(stage, 1)
        work.addWidget(self._build_side())
        self.body.addLayout(work, 1)
        self._set_session_controls(False)
        self._sync_transfer_buttons("idle")

    def _build_side(self) -> QScrollArea:
        """右列: 连接信息、性能指标与会话内操作卡片, 窄列内部滚动."""
        scroll = QScrollArea()
        scroll.setObjectName("sideColumn")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(SIDE_WIDTH)
        host = QWidget()
        host.setObjectName("scrollBody")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._build_connection_info(layout)
        self._build_metrics(layout)
        self._build_transfer(layout)
        self._build_clipboard(layout)
        layout.addStretch(1)
        scroll.setWidget(host)
        return scroll

    def _build_connection_info(self, target: QVBoxLayout) -> None:
        """连接信息逐项可复制; 空值置灰, 不用只读输入框代替复制按钮."""
        box, layout = self.section("连接信息 · 逐项可复制")
        for key, title in INFO_TITLES:
            value = QLabel("-")
            value.setObjectName("mono")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            copy = CopyButton("", f"复制{title}")
            copy.setEnabled(False)
            self._info_labels[key] = value
            self._info_buttons[key] = copy
            line = QHBoxLayout()
            line.setSpacing(6)
            line.addWidget(QLabel(title))
            line.addWidget(value, 1)
            line.addWidget(copy)
            layout.addLayout(line)
        layout.addWidget(self.button("复制连接信息", self._copy_connection_info, icon="copy"))
        target.addWidget(box)

    def _build_metrics(self, target: QVBoxLayout) -> None:
        """性能指标随右列滚动; 取不到的字段显示破折号, 不用常量填充."""
        box, layout = self.section("性能指标")
        self.metrics_line = QLabel("帧率 - 帧/秒 · 延迟中位 - 毫秒 · 95 分位 - 毫秒 · 码率 - 千比特/秒")
        self.metrics_line.setObjectName("muted")
        self.metrics_line.setWordWrap(True)
        self.metrics_copy = self.button("复制指标", self._copy_metrics, icon="copy")
        self.metrics_copy.setEnabled(False)
        self.metrics_copy.setToolTip("复制指标行原文")
        layout.addWidget(self.metrics_line)
        layout.addWidget(self.metrics_copy)
        target.addWidget(box)

    def _set_info(self, key: str, value: str) -> None:
        """写入一项可复制信息, 空值显示为 - 并置灰复制按钮."""
        text = value.strip() or "-"
        self._info[key] = "" if text == "-" else text
        label = self._info_labels.get(key)
        if label is not None:
            label.setText(text)
        button = self._info_buttons.get(key)
        if button is not None:
            button.set_value(self._info[key])

    def _copy(self, text: str) -> None:
        """复制到系统剪贴板并给出 1.5 秒轻提示."""
        QApplication.clipboard().setText(text)
        self.copy_toast.setText("已复制")
        self.copy_toast.setVisible(True)
        self._toast_timer.start(1500)

    def _copy_connection_info(self) -> None:
        lines = [f"{title}：{self._info[key]}" for key, title in INFO_TITLES if self._info.get(key)]
        if not lines:
            self._copy("暂无连接信息")
            return
        self._copy("\n".join(lines))

    def _copy_metrics(self) -> None:
        """复制指标行原文, 单位保持中文."""
        text = self.metrics_line.text()
        if self._session_id is None or not text:
            return
        self._copy(text)

    def _copy_transfer_detail(self) -> None:
        """复制传输详情原文, 单位保持中文."""
        if self._job_id is None:
            return
        self._copy(self.transfer_line.text())

    def _hide_toast(self) -> None:
        self.copy_toast.setVisible(False)

    def report(self, action: str, result: CommandResult) -> None:
        """把最近命令结果留作日志片段供复制, 正文仍交给装配处."""
        text = message_text(result.error_code, result.message, ok=result.ok)
        self._log_lines.append(f"{action}：{text}")
        del self._log_lines[:-5]
        self._set_info("log", " | ".join(self._log_lines))
        super().report(action, result)

    def _build_transfer(self, target: QVBoxLayout) -> None:
        """传输卡片: 窄列里表单逐行竖排, 动作按钮分两行避免挤压."""
        box, layout = self.section("文件传输 · 低优先级通道")
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("源文件路径, 也可把文件拖到画面区")
        self.destination_input = QLineEdit()
        self.destination_input.setText(transfer.default_destination())
        self.rate_spin = QSpinBox()
        self.rate_spin.setRange(0, 100_000)
        self.rate_spin.setSuffix(" 千字节/秒")
        self.rate_spin.setSpecialValueText("不限速")
        layout.addWidget(QLabel("源路径"))
        layout.addWidget(self.source_input)
        layout.addWidget(QLabel("接收目录"))
        layout.addWidget(self.destination_input)
        rate_row = QHBoxLayout()
        rate_row.setSpacing(8)
        rate_row.addWidget(QLabel("限速"))
        rate_row.addWidget(self.rate_spin, 1)
        layout.addLayout(rate_row)
        layout.addWidget(self.muted("视频地板档预留 2500 千字节/秒，传输不会抢占。"))

        self.start_button = self.button("开始传输", self._on_start_transfer, primary=True)
        self.pause_button = self.button("暂停", self._on_pause_transfer)
        self.resume_button = self.button("继续", self._on_resume_transfer)
        self.cancel_button = self.button("取消", self._on_cancel_transfer)
        self.retry_button = self.button("失败重试", self._on_retry_transfer)
        layout.addLayout(row(self.start_button, self.pause_button, self.resume_button))
        layout.addLayout(row(self.cancel_button, self.retry_button))
        self.transfer_bar = QProgressBar()
        self.transfer_bar.setRange(0, 100)
        self.transfer_bar.setValue(0)
        self.transfer_line = QLabel("未开始 · 当前文件 - · 速率 - · 剩余 -")
        self.transfer_line.setObjectName("muted")
        self.transfer_line.setWordWrap(True)
        self.transfer_copy = self.button("复制传输详情", self._copy_transfer_detail, icon="copy")
        self.transfer_copy.setEnabled(False)
        self.transfer_copy.setToolTip("复制传输详情原文")
        self.transfer_notice = QLabel("未连接时无法传输.")
        self.transfer_notice.setObjectName("badgeWarn")
        self.transfer_notice.setWordWrap(True)
        layout.addWidget(self.transfer_bar)
        layout.addWidget(self.transfer_line)
        layout.addWidget(self.transfer_copy)
        layout.addWidget(self.transfer_notice)
        target.addWidget(box)

    def _build_clipboard(self, target: QVBoxLayout) -> None:
        """剪贴板卡片: 开关、动作与状态同卡堆叠."""
        box, layout = self.section("剪贴板同步")
        self.clipboard_toggle = QCheckBox("开启双向同步")
        self.clipboard_toggle.setChecked(True)
        self.clipboard_toggle.toggled.connect(self._on_clipboard_toggle)
        self.clipboard_status = QLabel("状态：未同步")
        self.clipboard_status.setObjectName("muted")
        self.clipboard_status.setWordWrap(True)
        self.clipboard_notice = QLabel("")
        self.clipboard_notice.setObjectName("badgeWarn")
        self.clipboard_notice.setWordWrap(True)
        self.send_text_button = self.button("发送本端文本", self._on_send_text)
        self.send_image_button = self.button("发送本端图片", self._on_send_image)
        self.pull_button = self.button("拉取远端文本", self._on_pull_text)
        layout.addWidget(self.clipboard_toggle)
        layout.addWidget(self.muted("文本 256 千字节截断，图片 4 兆字节上限。"))
        layout.addLayout(row(self.send_text_button, self.send_image_button))
        layout.addWidget(self.pull_button)
        layout.addWidget(self.clipboard_status)
        layout.addWidget(self.clipboard_notice)
        target.addWidget(box)

    # ---------------------------------------------------------------- 会话命令
    def _sync_session_id(self) -> None:
        value = self.session_input.text().strip()
        self._session_id = value or None
        self._set_session_controls(self._session_id is not None)
        self.picture_label.setText("已填写会话编号 · 点击刷新状态" if self._session_id else "未连接 · 远端画面容器")
        if self._session_id is not None:
            self._subscribe_timer.start()
            self._subscribe_frames()
            self._stats_timer.start()
            self._task_timer.stop()
            self._task_id = None
        else:
            self._reset_frames()
            self._stats_timer.stop()

    def set_session(self, session_id: str) -> None:
        """供装配与测试设置当前会话."""
        self.session_input.setText(session_id)
        self._sync_session_id()
        self._on_status()

    def show_connecting(self, peer: str, task_id: str) -> None:
        """连接中过渡态: 不伪造已连接, 只显示任务与等待会话建立."""
        self._session_id = None
        self._task_id = task_id
        self._state = "connecting"
        self.session_input.clear()
        self._set_session_controls(False)
        self._reset_frames()
        self._set_info("device", peer)
        self._set_info("error", "")
        self._set_info("protocol", "")
        self.status_line.setText(f"状态：连接中 · 对端 {peer or '-'}")
        self.picture_label.setText(f"连接中 · 对端 {peer or '-'} · 等待会话建立")
        self.transfer_notice.setText("连接中 · 会话建立后可传输")
        self.drop_hint.setVisible(False)
        self._stats_timer.stop()
        self._task_timer.start()

    def open_session(self, session_id: str, peer: str) -> None:
        """从最近会话进入: 会话编号交给命令层查询, 状态以命令返回为准."""
        if not session_id:
            return
        self.session_input.setText(session_id)
        self._session_id = session_id
        self._task_id = None
        self._task_timer.stop()
        self._set_session_controls(True)
        self._set_info("device", peer)
        self.picture_label.setText(f"远端画面容器 · 对端 {peer or '-'} · 等待首帧")
        self._subscribe_timer.start()
        self._subscribe_frames()
        self._stats_timer.start()
        self._on_status()

    def _poll_task(self) -> None:
        """连接任务失败时给出中文原因, 不留无解释的黑屏."""
        if self._session_id is not None or self._task_id is None:
            self._task_timer.stop()
            return
        result = api.get_connection_progress(self._task_id)
        data = result.data or {}
        if str(data.get("state", "")) == "failed":
            reason = error_text(str(data.get("error_code") or ""), str(data.get("reason") or result.message))
            self._state = "failed"
            self._message = reason
            self.status_line.setText(f"状态：连接失败 · 原因：{reason}")
            self._set_info("error", reason)
            self._show_recovery(f"连接失败 · {reason}")
            self._task_timer.stop()

    def _on_back(self) -> None:
        self.navigate.emit("home")

    def _on_status(self) -> None:
        if self._session_id is None:
            self.transfer_notice.setText("未连接 · 请先填写会话编号")
            return
        result = api.get_session_status(self._session_id)
        self._render_status(result)
        self.report("查询会话", result)

    def _render_status(self, result: CommandResult) -> None:
        if not result.ok or result.data is None:
            reason = error_text(result.error_code, result.message)
            self._state = "failed"
            self._message = reason
            self.status_line.setText(f"状态：连接失败 · 原因：{reason}")
            self._set_info("error", reason)
            self.picture_label.setText("未连接 · 远端画面容器")
            self._show_recovery(f"连接失败 · {reason}")
            return
        data = result.data
        self._state = str(data.get("state", "connected"))
        self._set_info("device", str(data.get("peer_device_id", "")))
        self._transport = TRANSPORT_TEXT.get(str(data.get("transport_kind", "")), "未知")
        profile = str(data.get("quality_profile", "-"))
        self._profile = PROFILE_TEXT.get(profile, profile)
        self._set_info("protocol", self._protocol_text())
        self.status_line.setText(
            f"状态：{STATE_TEXT.get(self._state, '未知')} · 通路：{self._transport} · 档位：{self._profile}"
        )
        self.picture_label.setText(f"远端画面容器 · 对端 {data.get('peer_device_id')} · 等待首帧")

    def _protocol_text(self) -> str:
        """封装与握手版本只在连接信息里给中文短标, 不拼进状态行."""
        frame_version, handshake = self._protocol
        if not frame_version:
            return ""
        return f"画面封装 v{frame_version} · 握手 {handshake}"

    def _poll_stats(self) -> None:
        """按真实会话统计刷新状态条与连接信息, 断流时提示画面恢复中."""
        if self._session_id is None:
            self._stats_timer.stop()
            return
        stats = api.get_remote_stats(self._session_id)
        if not stats:
            self._render_metrics({})
            self.metrics_copy.setEnabled(False)
            return
        self._state = str(stats.get("state", "")) or self._state or "connecting"
        self._message = str(stats.get("message", ""))
        self._render_profile(stats)
        self._render_metrics(stats)
        self.metrics_copy.setEnabled(True)
        self._protocol = (int(stats.get("frame_version", 0) or 0), int(stats.get("handshake", 0) or 0))
        host = str(stats.get("host", ""))
        port = stats.get("port", "")
        if host:
            self._set_info("endpoint", f"{host}:{port}")
        else:
            hosting = api.get_hosting_info()
            address = str(hosting.get("address", ""))
            if address:
                self._set_info("endpoint", f"{address}:{hosting.get('port', '')}")
        access_code = str(api.get_hosting_info().get("access_code", ""))
        self._set_info("access_code", access_code)
        state_text = STATE_TEXT.get(self._state, "未知")
        self._set_info("protocol", self._protocol_text())
        size = ""
        width, height = (int(stats.get("width", 0) or 0), int(stats.get("height", 0) or 0))
        if width and height:
            size = f" · 远端 {width}×{height}"
        self.status_line.setText(f"状态：{state_text}{size} · 通路：{self._transport} · 档位：{self._profile}")
        if self._state == "failed":
            reason = error_text(str(stats.get("error_code") or ""), self._message)
            self._message = reason
            self._set_info("error", reason)
            self._show_recovery(f"连接失败 · {reason}")
        elif self._state == "reconnecting":
            self._show_recovery("画面恢复中 · 正在重连")
        elif self._last_frame_at and time.monotonic() - self._last_frame_at > STALL_SECONDS and width and height:
            self._show_recovery("画面恢复中 · 正在等待关键帧")
        else:
            self._hide_recovery()

    def _render_profile(self, stats: dict[str, Any]) -> None:
        """档位取真实生效值; 自适应模式标出当前实际回落档."""
        actual = str(stats.get("profile") or "")
        if not actual:
            self._profile = "自适应" if str(stats.get("quality_profile", "")) == "auto" else self._profile
            return
        if str(stats.get("quality_profile", "")) == "auto":
            self._profile = f"自适应 → {actual}"
        else:
            self._profile = actual

    def _render_metrics(self, stats: dict[str, Any]) -> None:
        """指标只读会话层统计; 取不到的字段显示「—」, 不用常量填充."""
        if not stats or not stats.get("width"):
            self.metrics_line.setText("帧率 — · 延迟中位 — · 95 分位 — · 码率 —")
            return
        ratio = stats.get("payload_ratio")
        share = f" · 净荷 {round(float(ratio) * 100)}%" if isinstance(ratio, (int, float)) else ""
        self.metrics_line.setText(
            f"帧率 {_metric_text(stats.get('fps'))} 帧/秒 · "
            f"延迟中位 {_metric_text(stats.get('latency_p50_ms'))} 毫秒 · "
            f"95 分位 {_metric_text(stats.get('latency_p95_ms'))} 毫秒 · "
            f"码率 {_metric_text(stats.get('bitrate_kbps'))} 千比特/秒{share}"
        )

    def _show_recovery(self, text: str) -> None:
        self.recovery_label.setText(text)
        self.recovery_label.setVisible(True)

    def _hide_recovery(self) -> None:
        self.recovery_label.setVisible(False)

    def _on_context_menu(self, pos) -> None:
        """画面区右键菜单; 未建立会话时动作项置灰."""
        source = self.sender()
        if not isinstance(source, QWidget):
            return
        has_session = self._session_id is not None
        streaming = self._state in {"streaming", "recovered"}
        menu = QMenu(self)
        keyframe = menu.addAction("请求关键帧")
        keyframe.setEnabled(has_session and streaming)
        fullscreen = menu.addAction("退出全屏" if self.window().isFullScreen() else "切换全屏")
        adaptive = menu.addAction("自适应画质")
        adaptive.setEnabled(has_session)
        copy_info = menu.addAction("复制连接信息")
        copy_info.setEnabled(has_session)
        menu.addSeparator()
        disconnect = menu.addAction("断开连接")
        disconnect.setEnabled(has_session)
        chosen = menu.exec(source.mapToGlobal(pos))
        if chosen is keyframe:
            self._on_request_keyframe()
        elif chosen is fullscreen:
            self._on_fullscreen()
        elif chosen is adaptive:
            self._on_adaptive_quality()
        elif chosen is copy_info:
            self._copy_connection_info()
        elif chosen is disconnect:
            self._on_stop()

    def _on_request_keyframe(self) -> None:
        if self._session_id is None:
            self.report("请求关键帧", CommandResult(ok=False, message="会话未建立, 无法请求关键帧"))
            return
        result = api.request_remote_keyframe(self._session_id)
        if not result.ok:
            self._set_info("error", error_text(result.error_code, result.message))
        self.report("请求关键帧", result)

    def _on_adaptive_quality(self) -> None:
        if self._session_id is None:
            return
        self.quality_combo.setCurrentIndex(0)
        result = api.apply_quality_profile(self._session_id, "auto")
        if result.ok:
            self._profile = "自适应"
            self.status_line.setText(f"状态：{STATE_TEXT.get(self._state, '已连接')} · 档位：自适应")
        self.report("切换为自适应画质", result)

    def _on_metrics(self) -> None:
        if self._session_id is None:
            self.metrics_line.setText("未连接 · 无指标")
            return
        result = api.get_session_metrics(self._session_id)
        stats = api.get_remote_stats(self._session_id) or {}
        self._render_metrics(stats)
        if stats:
            self._render_profile(stats)
        self.report("查询指标", result)

    def _on_quality(self) -> None:
        if self._session_id is None:
            return
        profile = str(self.quality_combo.currentData())
        result = api.apply_quality_profile(self._session_id, profile)
        if result.ok and result.data:
            applied = str(result.data.get("quality_profile"))
            self._profile = PROFILE_TEXT.get(applied, applied)
            self.status_line.setText(f"状态：{STATE_TEXT.get(self._state, '已连接')} · 档位：{self._profile}")
        self.report("切换画质", result)

    def _on_stop(self) -> None:
        if self._session_id is None:
            return
        result = api.stop_session(self._session_id)
        if result.ok:
            self._session_id = None
            self._state = "closed"
            self._stats_timer.stop()
            self._task_timer.stop()
            self._set_session_controls(False)
            self._reset_frames()
            self._set_info("error", "")
            self._set_info("protocol", "")
            self.status_line.setText("状态：已断开 · 通路：- · 档位：-")
            self.picture_label.setText("未连接 · 远端画面容器")
            self.navigate.emit("home")
        self.report("断开连接", result)

    def _on_fullscreen(self) -> None:
        window = self.window()
        if window.isFullScreen():
            window.showNormal()
        else:
            window.showFullScreen()

    def _set_session_controls(self, connected: bool) -> None:
        for widget in (
            self.apply_quality_button,
            self.quality_combo,
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
        self.metrics_copy.setEnabled(connected)
        self._sync_transfer_buttons(self._transfer_state)

    # ---------------------------------------------------------------- 远端画面
    def _subscribe_frames(self) -> None:
        """向命令层登记画面回调; 会话记录就绪前每秒重试一次."""
        if self._session_id is None:
            self._subscribe_timer.stop()
            return
        if api.watch_remote_frames(self._session_id, self._bridge.frame_ready.emit):
            self._subscribed = True
            self._subscribe_timer.stop()

    def _on_frame(self, frame: DecodedFrame) -> None:
        """界面线程收到已解码像素; 只做 QImage 包装与缩放呈现."""
        self._last_pixels = frame.pixels
        self._frame_size = (frame.width, frame.height)
        image = QImage(frame.pixels, frame.width, frame.height, frame.width * 4, QImage.Format.Format_ARGB32)
        self._present(image)

    def _present(self, image: QImage) -> None:
        if image.isNull():
            return
        self.canvas_illustration.setVisible(False)
        self.picture_label.setVisible(False)
        self.canvas_hint.setVisible(False)
        self.frame_label.setVisible(True)
        target = self.frame_label.size()
        pixmap = QPixmap.fromImage(image)
        if target.width() > 8 and target.height() > 8:
            pixmap = pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        self.frame_label.setPixmap(pixmap)
        self._last_frame_at = time.monotonic()
        if self._state in {"reconnecting", "recovered"}:
            self._state = "recovered"
        elif self._state in {"idle", "connecting", "connected"}:
            self._state = "streaming"
        self._hide_recovery()
        self._set_info("protocol", self._protocol_text())
        self.status_line.setText(
            f"状态：{STATE_TEXT.get(self._state, '正在传输')} · 远端 {image.width()}×{image.height()}"
            f" · 通路：{self._transport} · 档位：{self._profile}"
        )

    def _reset_frames(self) -> None:
        """断开会话时回到占位态, 不保留上一会话的画面."""
        self._subscribed = False
        self._last_pixels = None
        self._frame_size = (0, 0)
        self._last_frame_at = 0.0
        self._subscribe_timer.stop()
        self._hide_recovery()
        self.frame_label.clear()
        self.frame_label.setVisible(False)
        self.canvas_illustration.setVisible(True)
        self.picture_label.setVisible(True)
        self.canvas_hint.setVisible(True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """画面区的鼠标与键盘在这里转成协议消息, 经命令层上行."""
        if watched is self.frame_label and self._session_id is not None:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
                if event.button() == Qt.MouseButton.RightButton and not (
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                ):
                    # 右键开本地菜单; 需要远端右键时按住 Shift 再点
                    return super().eventFilter(watched, event)
                self.frame_label.setFocus()
                self._send_click(event, pressed=True)
            elif event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
                self._send_click(event, pressed=False)
            elif event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                self._send_move(event)
            elif event.type() == QEvent.Type.Wheel and isinstance(event, QWheelEvent):
                direction = 1 if event.angleDelta().y() > 0 else -1
                self._send_input({"kind": "mouse", "action": "wheel", "x": 0, "y": 0, "button": direction})
            elif event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                self._send_key(event)
        return super().eventFilter(watched, event)

    def _remote_point(self, event: QMouseEvent) -> tuple[int, int]:
        """把画面控件坐标换算成远端桌面坐标."""
        width, height = self._frame_size
        if width <= 0 or height <= 0:
            return 0, 0
        widget = self.frame_label
        spark = widget.pixmap()
        pixmap_width = spark.width() if spark is not None and not spark.isNull() else widget.width()
        pixmap_height = spark.height() if spark is not None and not spark.isNull() else widget.height()
        if pixmap_width <= 0 or pixmap_height <= 0:
            return 0, 0
        x = int(event.position().x() * width / pixmap_width)
        y = int(event.position().y() * height / pixmap_height)
        return max(0, min(width - 1, x)), max(0, min(height - 1, y))

    def _send_click(self, event: QMouseEvent, pressed: bool) -> None:
        if not pressed:
            return
        button = 1
        if event.button() == Qt.MouseButton.RightButton:
            button = 2
        elif event.button() == Qt.MouseButton.MiddleButton:
            button = 3
        x, y = self._remote_point(event)
        self._send_input({"kind": "mouse", "action": "click", "x": x, "y": y, "button": button})

    def _send_move(self, event: QMouseEvent) -> None:
        x, y = self._remote_point(event)
        self._send_input({"kind": "mouse", "action": "move", "x": x, "y": y, "button": 0})

    def _send_key(self, event: QKeyEvent) -> None:
        mods = []
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            mods.append("ctrl")
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            mods.append("shift")
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            mods.append("alt")
        name = SPECIAL_KEYS.get(Qt.Key(event.key()))
        if name is not None:
            if mods:
                self._send_input({"kind": "key", "action": "combo", "mods": mods, "key": name})
            else:
                self._send_input({"kind": "key", "action": "key", "key": name})
            return
        text = event.text()
        if text and not mods:
            self._send_input({"kind": "key", "action": "char", "text": text})

    def _send_input(self, message: dict[str, object]) -> None:
        if self._session_id is not None:
            api.send_remote_input(self._session_id, message)

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
            self.transfer_notice.setText(error_text(result.error_code, result.message))
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
            self.transfer_notice.setText(error_text(result.error_code, result.message))
            return
        data = result.data
        percent = int(float(data.get("percent", 0)))
        self.transfer_bar.setValue(percent)
        eta = data.get("eta_seconds")
        state = str(data.get("state", "idle"))
        channel = str(data.get("channel", ""))
        self.transfer_line.setText(
            f"状态：{TRANSFER_TEXT.get(state, '未知')} · 当前文件 {data.get('current_file') or '-'} · "
            f"速率 {data.get('speed_kbps')} 千字节/秒 · 剩余 {eta if eta is not None else '-'} 秒 · "
            f"通道 {CHANNEL_TEXT.get(channel, '未知通道')} · 限速 {data.get('rate_limit_kbps')} 千字节/秒"
        )
        notice = message_text(result.error_code, result.message, ok=result.ok)
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
        self.transfer_copy.setEnabled(connected and has_job)
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
        self.clipboard_status.setText(message_text(result.error_code, result.message, ok=result.ok))
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
        text = message_text(result.error_code, result.message, ok=result.ok)
        self.clipboard_status.setText(text)
        self.clipboard_notice.setText(text if "截断" in text or not result.ok else "")
        self.report("剪贴板同步", result)

    # ---------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        status = clipboard.get_clipboard_status()
        if status.data:
            self.clipboard_toggle.blockSignals(True)
            self.clipboard_toggle.setChecked(bool(status.data.get("enabled", True)))
            self.clipboard_toggle.blockSignals(False)
        self._sync_session_id()
        self._poll_stats()
