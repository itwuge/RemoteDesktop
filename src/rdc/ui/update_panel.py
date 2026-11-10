# ==========================================================================================
# 文件        : update_panel.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 软件更新面板: 当前版本、最新版本、更新说明、下载进度与校验结果
# 架构定位    : 界面层, 只调用 rdc.commands.update
# 核心功能    : 检查更新、下载更新、显示进度、显示 SHA-256 校验结论与安装提示
# 职责边界    : 不执行安装包; 不改写命令返回文案; 不改主题 token
# ==========================================================================================

"""软件更新面板."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import update as update_commands
from rdc.core.result import CommandResult
from rdc.ui import icons
from rdc.ui.components import chip
from rdc.ui.theme import active
from rdc.update.service import UpdateService

DEFAULT_HINT = "更新包只在校验通过后提示安装; 本轮不自动执行安装, 下载的包不会被运行."


class UpdateCheckWorker(QThread):
    """后台检查更新, 避免网络或文件读取阻塞界面线程."""

    checked = Signal(object)

    def __init__(self, service: UpdateService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:
        """执行检查并把 CommandResult 交回主线程."""
        self.checked.emit(self._service.refresh_update_status())


class UpdatePanel(QDialog):
    """更新面板; 打开后自动检查, 下载由命令层在后台线程完成."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("软件更新")
        self.setMinimumSize(720, 700)
        self._worker: UpdateCheckWorker | None = None
        self._task_id = ""
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll_progress)
        self._build()
        self._reset_view()

    # ---------------------------------------------------------------- 装配
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("软件更新")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.kind_chip = chip("未检查")
        header.addWidget(self.kind_chip)
        layout.addLayout(header)

        subtitle = QLabel("启动时静默检查, 失败不阻塞启动; 下载完成后必须通过 SHA-256 校验, 校验失败拒绝安装.")
        subtitle.setObjectName("pageSub")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        info_box = QGroupBox("版本信息")
        info = QFormLayout(info_box)
        info.setSpacing(8)
        self.current_label = QLabel("-")
        self.latest_label = QLabel("-")
        self.date_label = QLabel("-")
        self.min_label = QLabel("-")
        self.source_label = QLabel("-")
        self.source_label.setWordWrap(True)
        info.addRow("当前版本", self.current_label)
        info.addRow("最新版本", self.latest_label)
        info.addRow("发布日期", self.date_label)
        info.addRow("最低可运行版本", self.min_label)
        info.addRow("更新源", self.source_label)
        layout.addWidget(info_box)

        notes_box = QGroupBox("更新说明")
        notes_layout = QVBoxLayout(notes_box)
        self.notes_view = QPlainTextEdit()
        self.notes_view.setReadOnly(True)
        self.notes_view.setMaximumHeight(120)
        self.notes_view.setPlaceholderText("检查后显示更新说明")
        notes_layout.addWidget(self.notes_view)
        layout.addWidget(notes_box)

        progress_box = QGroupBox("下载与校验")
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.setSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)
        self.progress_text = QLabel("未开始下载")
        self.progress_text.setObjectName("muted")
        progress_layout.addWidget(self.progress_text)
        verify_row = QHBoxLayout()
        verify_row.setSpacing(8)
        verify_row.addWidget(QLabel("校验结果"))
        self.verify_chip = chip("未校验")
        verify_row.addWidget(self.verify_chip)
        self.verify_text = QLabel("-")
        self.verify_text.setObjectName("muted")
        self.verify_text.setWordWrap(True)
        verify_row.addWidget(self.verify_text, 1)
        progress_layout.addLayout(verify_row)
        layout.addWidget(progress_box)

        hint = QFrame()
        hint.setObjectName("statusStrip")
        hint_layout = QHBoxLayout(hint)
        hint_layout.setContentsMargins(12, 9, 12, 9)
        hint_layout.setSpacing(8)
        hint_icon = QLabel()
        hint_icon.setPixmap(icons.pixmap("info", active()["accent"], 16))
        hint_layout.addWidget(hint_icon)
        self.hint_label = QLabel(DEFAULT_HINT)
        self.hint_label.setObjectName("muted")
        self.hint_label.setWordWrap(True)
        hint_layout.addWidget(self.hint_label, 1)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.check_button = self._button("检查更新", self.start_check, "refresh-cw")
        self.download_button = self._button("下载更新", self._on_download, "download", primary=True)
        self.cancel_button = self._button("取消下载", self._on_cancel, "x")
        self.close_button = self._button("关闭", self.accept)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _button(self, text: str, callback: Callable[[], None], icon: str = "", *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setProperty("primary", True)
        if icon:
            icons.apply_icon(button, icon, tone="white" if primary else "text", size=15)
        button.clicked.connect(callback)
        return button

    # ---------------------------------------------------------------- 状态
    def _reset_view(self) -> None:
        self.current_label.setText(update_commands.get_service().current_version)
        self.latest_label.setText("-")
        self.date_label.setText("-")
        self.min_label.setText("-")
        self.source_label.setText("-")
        self.notes_view.setPlainText("")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_text.setText("未开始下载")
        self._set_chip(self.verify_chip, "未校验")
        self.verify_text.setText("-")
        self.hint_label.setText(DEFAULT_HINT)
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(False)

    def start_check(self) -> None:
        """后台检查更新; 失败只在面板内提示, 不弹错误框."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.check_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.latest_label.setText("检查中…")
        self.hint_label.setText("正在检查更新源…")
        worker = UpdateCheckWorker(update_commands.get_service(), self)
        worker.checked.connect(self._on_checked)
        worker.finished.connect(self._clear_worker)
        self._worker = worker
        worker.start()

    # ---------------------------------------------------------------- 回调
    def _on_checked(self, result: object) -> None:
        if not isinstance(result, CommandResult):
            return
        self.check_button.setEnabled(True)
        data = result.data or {}
        if not result.ok:
            self.latest_label.setText("-")
            self.notes_view.setPlainText(result.message)
            self._set_chip(self.kind_chip, "检查失败", "danger")
            self.hint_label.setText("检查更新失败, 不影响本地使用, 可稍后重试.")
            self.verify_text.setText(result.error_code or "-")
            return

        current = str(data.get("current_version", "-"))
        latest = str(data.get("latest_version", "-"))
        available = bool(data.get("available"))
        mandatory = bool(data.get("mandatory"))
        self.current_label.setText(current)
        self.latest_label.setText(latest)
        self.date_label.setText(str(data.get("release_date", "-")))
        self.min_label.setText(str(data.get("min_supported_version", "-")))
        source_text = str(data.get("source_label") or data.get("source", "-"))
        asset_name = str(data.get("asset_name", ""))
        if asset_name:
            source_text = f"{source_text}\n产物: {asset_name}"
        self.source_label.setText(source_text)
        self.notes_view.setPlainText(str(data.get("notes", "")))
        if available:
            self._set_chip(self.kind_chip, "强制更新" if mandatory else "有新版本", "warn" if mandatory else "accent")
            self.hint_label.setText(f"发现新版本 {latest}, 可下载后校验; 校验通过才会给出安装提示.")
            self.download_button.setEnabled(True)
        else:
            self._set_chip(self.kind_chip, "已是最新", "ok")
            self.hint_label.setText("当前已是最新版本, 无需更新.")
            self.download_button.setEnabled(False)

    def _on_download(self) -> None:
        result = update_commands.start_update_download()
        if not result.ok:
            self.hint_label.setText(result.message)
            return
        data = result.data or {}
        self._task_id = str(data.get("task_id", ""))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_text.setText("0 / 0 字节")
        self._set_chip(self.verify_chip, "下载中", "accent")
        self.verify_text.setText("下载完成后自动计算 SHA-256")
        self.hint_label.setText(result.message)
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self._timer.start()

    def _poll_progress(self) -> None:
        if not self._task_id:
            self._timer.stop()
            return
        result = update_commands.get_update_progress(self._task_id)
        data = result.data or {}
        completed = int(data.get("completed", 0))
        total = int(data.get("total", 0))
        status = str(data.get("status", ""))
        self._set_progress(completed, total)
        if status == "done":
            self._timer.stop()
            self.cancel_button.setEnabled(False)
            self._set_chip(self.verify_chip, "校验通过", "ok")
            digest = str(data.get("sha256", ""))
            package = str(data.get("package_path", ""))
            self.verify_text.setText(f"SHA-256 {digest}\n安装包: {package}")
            self.hint_label.setText("更新包已通过 SHA-256 校验, 可以安装; 本轮不自动执行安装.")
        elif status == "failed":
            self._timer.stop()
            self.cancel_button.setEnabled(False)
            self._set_chip(self.verify_chip, "校验失败", "danger")
            self.verify_text.setText(result.error_code or result.message)
            self.hint_label.setText(f"已拒绝安装: {result.message}")
        elif status == "canceled":
            self._timer.stop()
            self.cancel_button.setEnabled(False)
            self._set_chip(self.verify_chip, "已取消")
            self.verify_text.setText("下载已取消, 未执行任何校验或安装")
            self.hint_label.setText("下载已取消.")
        else:
            self.verify_text.setText("正在下载, 尚未校验")

    def _on_cancel(self) -> None:
        if not self._task_id:
            return
        result = update_commands.cancel_update_download(self._task_id)
        self.hint_label.setText(result.message)
        self._poll_progress()

    def _set_progress(self, completed: int, total: int) -> None:
        if total > 0:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(min(100, int(completed * 100 / total)))
            self.progress_text.setText(f"{completed} / {total} 字节")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_text.setText(f"已下载 {completed} 字节")

    @staticmethod
    def _set_chip(label: QLabel, text: str, tone: str = "") -> None:
        label.setText(text)
        label.setProperty("tone", tone)
        label.updateGeometry()
        label.style().unpolish(label)
        label.style().polish(label)

    def _clear_worker(self) -> None:
        self._worker = None

    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭前等待检查线程结束, 避免销毁运行中的 QThread."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)
