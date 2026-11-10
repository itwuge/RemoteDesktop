# ==========================================================================================
# 文件        : base.py
# 所属目录    : src/rdc/ui/pages/
# 文件用途    : 页面基类与通用小部件, 统一页头、命令结果上报与未连接占位
# 架构定位    : 界面层; 只依赖 core 的 CommandResult
# 核心功能    : 页头、分组框、结果回报、按钮参数收集
# 职责边界    : 不写业务判断; 不直接调用网络与文件; 不改写 message
# ==========================================================================================

"""页面基类."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from rdc.core.result import CommandResult
from rdc.ui import icons

ResultSink = Callable[[str, CommandResult], None]


class PageBase(QWidget):
    """带页头的页面基类."""

    navigate = Signal(str)

    def __init__(self, page_key: str, title: str, subtitle: str = "", *, scrollable: bool = False) -> None:
        super().__init__()
        self.page_key = page_key
        self._sink: ResultSink | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(12)
        caption = QVBoxLayout()
        caption.setSpacing(3)
        self.eyebrow_label = QLabel("")
        self.eyebrow_label.setObjectName("eyebrow")
        self.eyebrow_label.setVisible(False)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("pageSub")
        self.subtitle_label.setWordWrap(True)
        caption.addWidget(self.eyebrow_label)
        caption.addWidget(self.title_label)
        caption.addWidget(self.subtitle_label)
        header.addLayout(caption)
        header.addStretch(1)
        self.header_actions = QHBoxLayout()
        header.addLayout(self.header_actions)
        outer.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        if scrollable:
            scroll = QScrollArea()
            scroll.setObjectName("pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            host = QWidget()
            host.setObjectName("scrollBody")
            host.setLayout(self.body)
            scroll.setWidget(host)
            outer.addWidget(scroll, 1)
        else:
            outer.addLayout(self.body, 1)

    def set_sink(self, sink: ResultSink) -> None:
        """装配处注入命令结果接收器."""
        self._sink = sink

    def set_eyebrow(self, text: str) -> None:
        """页头眉标: 页面名与登录态, 与原型页头一致."""
        self.eyebrow_label.setText(text)
        self.eyebrow_label.setVisible(bool(text))

    def report(self, action: str, result: CommandResult) -> None:
        """把命令结果交给装配处统一呈现, 页面只读结果."""
        if self._sink is not None:
            self._sink(action, result)

    def refresh(self) -> None:
        """页面进入时刷新; 子类按需覆盖."""

    def shutdown(self) -> None:
        """页面销毁前的清理钩子; 有后台线程的页面覆盖, 默认无操作."""

    @staticmethod
    def section(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """创建标准分组框."""
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setSpacing(8)
        return box, layout

    @staticmethod
    def form_section(title: str) -> tuple[QGroupBox, QFormLayout]:
        """创建表单式分组框."""
        box = QGroupBox(title)
        layout = QFormLayout(box)
        layout.setSpacing(8)
        return box, layout

    @staticmethod
    def button(
        text: str,
        callback: Callable[[], None],
        *,
        primary: bool = False,
        icon: str = "",
        tone: str | None = None,
    ) -> QPushButton:
        """创建按钮并绑定回调; 可附统一图标."""
        button = QPushButton(text)
        if primary:
            button.setProperty("primary", True)
        if icon:
            icons.apply_icon(button, icon, tone=tone or ("white" if primary else "text"), size=15)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def muted(text: str) -> QLabel:
        """次要说明文字."""
        label = QLabel(text)
        label.setProperty("class", "muted")
        label.setWordWrap(True)
        return label


def row(*widgets: QWidget) -> QHBoxLayout:
    """水平排列一组控件."""
    layout = QHBoxLayout()
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return layout
