# ==========================================================================================
# 文件        : settings.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 设置页: 连接配置持久化、中继策略、诊断信息与服务器连通性检测
# 架构定位    : 界面层, 只调用 rdc.commands.api 与 rdc.commands.settings
# 核心功能    : 读写本机设置并就地校验; 展示版本/设备码/信令延迟/端口连通; 后台探测服务器
# 职责边界    : 不直接读写配置文件; 不直接发起网络; 不改写命令返回文案
# ==========================================================================================

"""设置页."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from rdc.commands import api
from rdc.commands import settings as settings_commands
from rdc.core.result import CommandResult
from rdc.ui.components import CopyButton, chip
from rdc.ui.copy import message_text, profile_text
from rdc.ui.pages.base import PageBase

LOG_LEVEL_TEXT = {"DEBUG": "调试", "INFO": "信息", "WARNING": "警告", "ERROR": "错误"}


class ServerProbeWorker(QThread):
    """后台执行 TCP 连通性检测, 避免超时阻塞界面线程."""

    probed = Signal(object)

    def __init__(self, endpoint: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint

    def run(self) -> None:
        """执行检测并把 CommandResult 交回主线程."""
        self.probed.emit(settings_commands.check_server(self._endpoint))


class SettingsPage(PageBase):
    """设置项持久化、诊断与中继策略."""

    def __init__(self) -> None:
        super().__init__(
            "settings",
            "设置",
            "连接配置写入本机配置文件; 画质档位为 720P 30 帧中继档, 直连会话不受此档限制.",
            scrollable=True,
        )
        self._probe_worker: ServerProbeWorker | None = None
        self._build()

    def _build(self) -> None:
        self.header_actions.addWidget(self.button("保存设置", self._on_save, primary=True, icon="check"))
        columns = QHBoxLayout()
        columns.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(10)
        right = QVBoxLayout()
        right.setSpacing(10)
        left.addWidget(self._build_connection_card())
        left.addWidget(self._build_appearance_card())
        left.addStretch(1)
        right.addWidget(self._build_diagnostics_card())
        right.addWidget(self._build_relay_card())
        right.addStretch(1)
        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        self.body.addLayout(columns, 1)
        self.body.addStretch(1)

    # ---------------------------------------------------------------- 连接设置
    def _build_connection_card(self) -> QFrame:
        card, layout, head = self._card("连接设置")
        head.addWidget(chip("本机"))
        form = QFormLayout()
        form.setSpacing(8)
        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setPlaceholderText("主机:端口，例如 192.168.37.132:27888")
        self.endpoint_edit.textChanged.connect(self._on_endpoint_changed)
        form.addRow(self._form_label("信令服务器"), self.endpoint_edit)
        self.endpoint_hint = self._hint_label()
        form.addRow("", self.endpoint_hint)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("显示在本机与设备列表的名称，最长 32 个字符")
        self.name_edit.setMaxLength(settings_commands.MAX_DEVICE_NAME)
        form.addRow(self._form_label("本机设备名"), self.name_edit)

        self.quality_combo = QComboBox()
        for profile in settings_commands.QUALITY_PROFILES:
            self.quality_combo.addItem(f"{profile_text(profile)}（中继档）", profile)
        form.addRow(self._form_label("默认画质"), self.quality_combo)
        form.addRow("", self.muted("中继档只约束经中继的会话; 直连会话按四档与自动模式运行, 不受此档限制."))

        self.log_combo = QComboBox()
        for level in settings_commands.LOG_LEVELS:
            self.log_combo.addItem(LOG_LEVEL_TEXT.get(level, "信息"), level)
        form.addRow(self._form_label("日志级别"), self.log_combo)
        form.addRow("", self.muted("保存后下次启动生效; 当前进程的日志级别不变."))

        self.autostart_check = QCheckBox("登录系统后自动启动 RDC（自启时静默进入托盘, 不弹主窗口）")
        self.autostart_check.clicked.connect(self._on_autostart_clicked)
        form.addRow(self._form_label("开机自启"), self.autostart_check)
        self.autostart_status = QLabel("正在读取系统状态…")
        self.autostart_status.setObjectName("muted")
        self.autostart_status.setWordWrap(True)
        form.addRow("", self.autostart_status)
        layout.addLayout(form)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.button("恢复默认", self._on_restore_defaults, icon="refresh-cw"))
        self.save_status = QLabel("尚未保存")
        self.save_status.setObjectName("muted")
        self.save_status.setWordWrap(True)
        actions.addWidget(self.save_status, 1)
        layout.addLayout(actions)
        return card

    # ---------------------------------------------------------------- 外观
    def _build_appearance_card(self) -> QFrame:
        card, layout, head = self._card("外观")
        head.addWidget(chip("即时生效"))
        layout.addWidget(self.muted("深浅色与设备端共用同一套令牌，桌面与安卓保持一致。"))
        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        theme_row.addWidget(self._form_label("主题"))
        theme_row.addWidget(chip("深色 · 默认", "accent"))
        theme_row.addWidget(chip("浅色 · 可切换"))
        theme_row.addStretch(1)
        layout.addLayout(theme_row)
        layout.addWidget(self.muted("左下角可即时切换；跟随系统暂不提供，避免与远程画面亮度冲突。"))
        return card

    # ---------------------------------------------------------------- 诊断
    def _build_diagnostics_card(self) -> QFrame:
        card, layout, head = self._card("本机诊断")
        self.probe_button = self.button("重新检测", self._on_probe, icon="refresh-cw")
        head.addWidget(self.probe_button)
        form = QFormLayout()
        form.setSpacing(8)
        self.version_label = self._value_label(mono=True)
        self.device_code_label = self._value_label(mono=True)
        self.version_copy = CopyButton("", "复制版本号")
        self.device_code_copy = CopyButton("", "复制设备编号")
        form.addRow(self._form_label("客户端版本"), self._copy_value_row(self.version_label, self.version_copy))
        form.addRow(self._form_label("设备编号"), self._copy_value_row(self.device_code_label, self.device_code_copy))
        self.latency_label = self._value_label()
        self.port_label = self._value_label()
        form.addRow(self._form_label("信令延迟"), self.latency_label)
        form.addRow(self._form_label("端口连通"), self.port_label)
        layout.addLayout(form)
        self.probe_status = QLabel("点击「重新检测」会调用 check_server 并刷新信令延迟与端口连通.")
        self.probe_status.setObjectName("muted")
        self.probe_status.setWordWrap(True)
        layout.addWidget(self.probe_status)
        return card

    # ---------------------------------------------------------------- 中继策略
    def _build_relay_card(self) -> QFrame:
        card, layout, head = self._card("中继策略")
        head.addWidget(chip("服务端"))
        form = QFormLayout()
        form.setSpacing(8)
        self.relay_endpoint = QLineEdit()
        self.relay_endpoint.setPlaceholderText("中继地址，例如 relay://主机:端口")
        self.relay_egress = QLineEdit()
        self.relay_egress.setPlaceholderText("出口带宽（兆比特/秒），例如 50")
        self.relay_profile = QComboBox()
        for level in ("L0", "L1", "L2", "L3"):
            self.relay_profile.addItem(profile_text(level), level)
        form.addRow(self._form_label("中继端点"), self.relay_endpoint)
        form.addRow(self._form_label("出口带宽"), self.relay_egress)
        form.addRow(self._form_label("降级档"), self.relay_profile)
        layout.addLayout(form)

        self.relay_status = QLabel("未提交")
        self.relay_status.setObjectName("muted")
        self.relay_status.setWordWrap(True)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.button("应用中继策略", self._on_apply_relay, primary=True, icon="check"))
        actions.addWidget(self.relay_status, 1)
        layout.addLayout(actions)
        layout.addWidget(self.muted("预算按出口带宽扣除 8% 开销计算; 失败原因以中文显示, 错误码只留在运行日志."))
        return card

    # ---------------------------------------------------------------- 刷新与校验
    def refresh(self) -> None:
        """进入页面时从命令层重新读取设置与诊断信息."""
        result = settings_commands.get_settings()
        values = (result.data or {}).get("settings") if result.data else None
        if isinstance(values, dict):
            self.endpoint_edit.setText(str(values.get("relay_endpoint", "")))
            self.name_edit.setText(str(values.get("device_name", "")))
            self._select(self.quality_combo, str(values.get("quality_profile", "")))
            self._select(self.log_combo, str(values.get("log_level", "INFO")))
            self.save_status.setText(result.message)
        self._refresh_autostart()
        self.refresh_diagnostics()

    def refresh_diagnostics(self) -> None:
        """读取诊断信息并写入只读标签."""
        result = settings_commands.get_diagnostics()
        data = result.data or {}
        version = str(data.get("version", "-"))
        device_id = str(data.get("device_id", "-"))
        self.version_label.setText(version)
        self.device_code_label.setText(device_id)
        self.version_copy.set_value(version, "复制版本号")
        self.device_code_copy.set_value(device_id, "复制设备编号")

    def _on_endpoint_changed(self, text: str) -> None:
        self._show_endpoint_error(self._endpoint_error(text))

    def _endpoint_error(self, text: str) -> str:
        if not text.strip():
            return ""
        try:
            settings_commands.parse_endpoint(text)
        except ValueError as error:
            return str(error)
        return ""

    def _show_endpoint_error(self, message: str) -> None:
        self.endpoint_hint.setText(message)
        self.endpoint_hint.setVisible(bool(message))

    # ---------------------------------------------------------------- 开机自启
    def _refresh_autostart(self, *, update_text: bool = True) -> None:
        """按系统真实状态刷新开关; 读不到时置灰并显示原因, 不用本地缓存兜底."""
        result = settings_commands.get_autostart_status()
        data = result.data or {}
        supported = bool(data.get("supported", False)) and result.ok
        self.autostart_check.setChecked(bool(data.get("enabled", False)))
        self.autostart_check.setEnabled(supported)
        if not update_text:
            return
        if result.ok:
            suffix = "；便携版默认关闭, 可在此手动开启" if data.get("portable") and not data.get("enabled") else ""
            self.autostart_status.setText(f"{result.message}{suffix}")
            self._set_autostart_tone("muted")
            return
        self.autostart_status.setText(message_text(result.error_code, result.message, ok=False))
        self._set_autostart_tone("badgeDanger")

    def _on_autostart_clicked(self, checked: bool) -> None:
        """切换即生效; 失败回读真实状态并把原因留在页面上."""
        result = settings_commands.set_autostart_enabled(bool(checked))
        self.report("开机自启", result)
        if result.ok:
            self._refresh_autostart()
            self.autostart_status.setText(message_text(result.error_code, result.message, ok=True))
            self._set_autostart_tone("badgeOk")
            return
        self.autostart_status.setText(message_text(result.error_code, result.message, ok=False))
        self._set_autostart_tone("badgeDanger")
        self._refresh_autostart(update_text=False)

    def _set_autostart_tone(self, tone: str) -> None:
        self.autostart_status.setObjectName(tone)
        self.autostart_status.style().unpolish(self.autostart_status)
        self.autostart_status.style().polish(self.autostart_status)

    # ---------------------------------------------------------------- 交互
    def _on_save(self) -> None:
        """提交保存; 开关状态以系统真实状态为准, 不取界面上的临时值."""
        self.save_status.setText("保存中…")
        autostart_state = settings_commands.get_autostart_status()
        auto_start = (
            bool((autostart_state.data or {}).get("enabled"))
            if autostart_state.ok and autostart_state.data
            else self.autostart_check.isChecked()
        )
        result = settings_commands.save_settings(
            self.endpoint_edit.text(),
            self.name_edit.text(),
            str(self.quality_combo.currentData()),
            str(self.log_combo.currentData()),
            auto_start,
        )
        self.save_status.setText(message_text(result.error_code, result.message, ok=result.ok))
        self.report("保存设置", result)
        if result.ok:
            self.refresh_diagnostics()

    def _on_restore_defaults(self) -> None:
        """把内置默认值填入表单; 仍需点「保存设置」才写入本机配置."""
        defaults = settings_commands.DEFAULT_SETTINGS
        self.endpoint_edit.setText(str(defaults["relay_endpoint"]))
        self.name_edit.setText(str(defaults["device_name"]))
        self._select(self.quality_combo, str(defaults["quality_profile"]))
        self._select(self.log_combo, str(defaults["log_level"]))
        self.save_status.setText("已填入默认值, 点「保存设置」后写入本机配置; 开机自启不受默认值影响")

    def _on_probe(self) -> None:
        """后台探测服务器; 结果回主线程显示中文结论."""
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        error = self._endpoint_error(self.endpoint_edit.text())
        self._show_endpoint_error(error)
        if error:
            self.probe_status.setText(error)
            return
        if not self.endpoint_edit.text().strip():
            self.probe_status.setText("请先填写服务器地址")
            return
        self.probe_button.setEnabled(False)
        self.latency_label.setText("检测中…")
        self.port_label.setText("检测中…")
        self.probe_status.setText("正在检测服务器连通性…")
        worker = ServerProbeWorker(self.endpoint_edit.text().strip(), self)
        worker.probed.connect(self._on_probed)
        worker.finished.connect(self._clear_probe_worker)
        self._probe_worker = worker
        worker.start()

    def _on_probed(self, result: object) -> None:
        if not isinstance(result, CommandResult):
            return
        data = result.data or {}
        rtt_ms = data.get("rtt_ms")
        port = data.get("port")
        if result.ok:
            self.latency_label.setText(f"{rtt_ms} ms" if rtt_ms is not None else "-")
            self.port_label.setText(f"TCP {port} 可达" if port else "可达")
        else:
            self.latency_label.setText("不可达")
            self.port_label.setText("不可达")
        self._set_status_tone("badgeOk" if result.ok else "badgeDanger")
        self.probe_status.setText(message_text(result.error_code, result.message, ok=result.ok))
        self.report("检测服务器", result)

    def _set_status_tone(self, tone: str) -> None:
        self.probe_status.setObjectName(tone)
        self.probe_status.style().unpolish(self.probe_status)
        self.probe_status.style().polish(self.probe_status)

    def _clear_probe_worker(self) -> None:
        self.probe_button.setEnabled(True)
        self._probe_worker = None

    def shutdown(self) -> None:
        """关闭前等待探测线程结束, 避免销毁运行中的 QThread."""
        if self._probe_worker is not None and self._probe_worker.isRunning():
            self._probe_worker.wait(3000)

    def _on_apply_relay(self) -> None:
        raw = self.relay_egress.text().strip()
        if not raw.isdigit() or int(raw) <= 0:
            self.relay_status.setText("出口带宽必须是正整数")
            return
        result = api.apply_relay_policy(
            self.relay_endpoint.text().strip(), int(raw), str(self.relay_profile.currentData())
        )
        self.relay_status.setText(message_text(result.error_code, result.message, ok=result.ok))
        self.report("应用中继策略", result)

    # ---------------------------------------------------------------- 小工具
    @staticmethod
    def _card(title: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        """卡片容器: 卡头 + 内容, 卡头右侧可追加胶囊或按钮."""
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("sectionTitle")
        head.addWidget(title_label)
        head.addStretch(1)
        layout.addLayout(head)
        return card, layout, head

    @staticmethod
    def _form_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("formLabel")
        return label

    def _hint_label(self) -> QLabel:
        label = QLabel("")
        label.setObjectName("badgeDanger")
        label.setWordWrap(True)
        label.setVisible(False)
        return label

    def _copy_value_row(self, label: QLabel, button: CopyButton) -> QHBoxLayout:
        """只读诊断值 + 行尾复制按钮."""
        holder = QHBoxLayout()
        holder.setSpacing(6)
        holder.addWidget(label, 1)
        holder.addWidget(button)
        return holder

    def _value_label(self, *, mono: bool = False) -> QLabel:
        label = QLabel("-")
        label.setObjectName("mono" if mono else "muted")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index < 0:
            index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)
