# ==========================================================================================
# 文件        : components.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 界面层通用视觉组件: 品牌标、插画、空状态、设备卡片、屏幕墙格
# 架构定位    : 纯表现组件; 只接收展示数据与点击回调
# 核心功能    : 自绘插画与图形, 深浅色自动取色; 不承载任何业务判断
# 职责边界    : 不发命令; 不读写文件; 不改写命令返回文案
# ==========================================================================================

"""通用视觉组件."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from PySide6.QtCore import (
    QDate,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QEnterEvent,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rdc.ui import icons
from rdc.ui.copy import platform_text, trusted_source_text
from rdc.ui.theme import BRAND_MARK, active


def _ink(token: dict[str, str], alpha: int) -> QColor:
    """投影与遮挡用色: 取当前主题的 shadow 令牌, 只调整透明度."""
    color = QColor(token["shadow"])
    color.setAlpha(alpha)
    return color


class BrandMark(QWidget):
    """侧栏品牌标: 自绘渐变圆角方块 + 显示器符号."""

    def __init__(self, side: int = 38, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._side = side
        self.setFixedSize(side, side)

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self._side, self._side)
        gradient = QLinearGradient(0, 0, self._side, self._side)
        gradient.setColorAt(0.0, QColor(BRAND_MARK["from"]))
        gradient.setColorAt(1.0, QColor(BRAND_MARK["to"]))
        painter.setBrush(gradient)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, self._side * 0.3, self._side * 0.3)
        glyph = icons.pixmap("monitor", token["white"], int(self._side * 0.52))
        offset = int((self._side - self._side * 0.52) / 2)
        painter.drawPixmap(offset, offset, glyph)
        painter.end()


class StatusDot(QWidget):
    """在线状态圆点, 外圈柔光."""

    def __init__(self, color: str = "ok", side: int = 10, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(side, side)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        color = QColor(token.get(self._color, token["muted"]))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glow = QColor(color)
        glow.setAlpha(60)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        painter.setBrush(color)
        margin = self.width() * 0.28
        painter.drawEllipse(QRectF(margin, margin, self.width() - margin * 2, self.height() - margin * 2))
        painter.end()


class Illustration(QWidget):
    """空状态自绘插画: 不使用外部图片素材, 颜色跟随主题."""

    def __init__(self, kind: str = "devices", width: int = 220, height: int = 148) -> None:
        super().__init__()
        self._kind = kind
        self.setFixedSize(width, height)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def sizeHint(self) -> QSize:
        return QSize(220, 148)

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = min(self.width() / 240.0, self.height() / 160.0)
        painter.translate((self.width() - 240 * scale) / 2, (self.height() - 160 * scale) / 2)
        painter.scale(scale, scale)
        glow = QRadialGradient(QPointF(120, 74), 96)
        accent = QColor(token["accent"])
        accent.setAlpha(30)
        glow.setColorAt(0, accent)
        accent.setAlpha(0)
        glow.setColorAt(1, accent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QRectF(18, 0, 204, 148))
        getattr(self, f"_paint_{self._kind}", self._paint_devices)(painter, token)
        painter.end()

    # ---------------------------------------------------------------- 场景
    @staticmethod
    def _panel(painter: QPainter, rect: QRectF, token: dict[str, str], radius: float = 8.0) -> None:
        painter.setBrush(QColor(token["card"]))
        painter.setPen(QPen(QColor(token["line_strong"]), 1.5))
        painter.drawRoundedRect(rect, radius, radius)

    @staticmethod
    def _screen(painter: QPainter, rect: QRectF, token: dict[str, str]) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(token["thumb_from"]))
        gradient.setColorAt(1.0, QColor(token["thumb_to"]))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(token["line"]), 1.1))
        painter.drawRoundedRect(rect, 5, 5)

    def _paint_devices(self, painter: QPainter, token: dict[str, str]) -> None:
        painter.setBrush(_ink(token, 26))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(50, 128, 132, 12))
        self._panel(painter, QRectF(44, 34, 120, 78), token, 10)
        self._screen(painter, QRectF(52, 42, 104, 62), token)
        painter.setPen(QPen(QColor(token["line_strong"]), 2))
        painter.drawLine(QPointF(104, 112), QPointF(104, 124))
        painter.drawLine(QPointF(86, 124), QPointF(122, 124))
        painter.setPen(QPen(QColor(token["accent"]), 1.8))
        painter.drawRoundedRect(QRectF(62, 54, 54, 34), 4, 4)
        painter.setPen(QPen(QColor(token["line_strong"]), 1.6))
        painter.drawRoundedRect(QRectF(152, 74, 36, 58), 8, 8)
        self._screen(painter, QRectF(157, 82, 26, 40), token)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(token["ok"]))
        painter.drawEllipse(QRectF(168, 126, 4, 4))
        painter.setBrush(QColor(token["accent"]))
        painter.drawEllipse(QRectF(150, 44, 8, 8))
        ring = QColor(token["accent"])
        ring.setAlpha(55)
        painter.setBrush(ring)
        painter.drawEllipse(QRectF(144, 38, 20, 20))
        painter.setBrush(QColor(token["accent"]))
        painter.drawEllipse(QRectF(150, 44, 8, 8))

    def _paint_wall(self, painter: QPainter, token: dict[str, str]) -> None:
        painter.setBrush(_ink(token, 22))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(38, 128, 164, 12))
        for index, (x, y) in enumerate(((40, 28), (126, 28), (40, 80), (126, 80))):
            accent = index == 0
            painter.setBrush(QColor(token["card"]))
            painter.setPen(QPen(QColor(token["accent"] if accent else token["line_strong"]), 2 if accent else 1.6))
            painter.drawRoundedRect(QRectF(x, y, 74, 44), 7, 7)
            self._screen(painter, QRectF(x + 6, y + 6, 62, 30), token)
            painter.setPen(QPen(QColor(token["line_strong"]), 1.6))
            painter.drawLine(QPointF(x + 37, y + 44), QPointF(x + 37, y + 51))
            painter.drawLine(QPointF(x + 28, y + 51), QPointF(x + 46, y + 51))

    def _paint_inbox(self, painter: QPainter, token: dict[str, str]) -> None:
        painter.setBrush(_ink(token, 22))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(46, 128, 148, 12))
        for offset, width in ((0, 104), (10, 124), (20, 84)):
            painter.setBrush(QColor(token["panel_2"]))
            painter.setPen(QPen(QColor(token["line"]), 1.4))
            painter.drawRoundedRect(QRectF(60 + offset / 2, 52 + offset, width, 11), 4, 4)
        painter.setBrush(QColor(token["card"]))
        painter.setPen(QPen(QColor(token["line_strong"]), 1.8))
        path = QPainterPath()
        path.moveTo(54, 92)
        path.lineTo(66, 126)
        path.quadTo(68, 132, 76, 132)
        path.lineTo(164, 132)
        path.quadTo(172, 132, 174, 126)
        path.lineTo(186, 92)
        path.closeSubpath()
        painter.drawPath(path)
        painter.setBrush(QColor(token["accent"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(150, 72, 34, 34))
        painter.setPen(QPen(QColor(token["white"]), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(159, 89), QPointF(166, 96))
        painter.drawLine(QPointF(166, 96), QPointF(177, 82))

    def _paint_session(self, painter: QPainter, token: dict[str, str]) -> None:
        painter.setBrush(_ink(token, 26))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(38, 130, 164, 12))
        self._panel(painter, QRectF(34, 24, 172, 104), token, 12)
        self._screen(painter, QRectF(44, 34, 152, 84), token)
        painter.setBrush(QColor(token["accent"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(98, 56, 40, 40))
        painter.setBrush(QColor(token["white"]))
        play = QPainterPath()
        play.moveTo(112, 68)
        play.lineTo(112, 84)
        play.lineTo(127, 76)
        play.closeSubpath()
        painter.drawPath(play)
        painter.setPen(QPen(QColor(token["line_strong"]), 2))
        painter.drawLine(QPointF(120, 128), QPointF(120, 138))
        painter.drawLine(QPointF(102, 138), QPointF(138, 138))
        painter.setBrush(QColor(token["card"]))
        painter.setPen(QPen(QColor(token["line_strong"]), 1.6))
        pointer = QPainterPath()
        pointer.moveTo(154, 92)
        pointer.lineTo(154, 112)
        pointer.lineTo(160, 106)
        pointer.lineTo(165, 115)
        pointer.lineTo(170, 112)
        pointer.lineTo(165, 103)
        pointer.lineTo(172, 102)
        pointer.closeSubpath()
        painter.drawPath(pointer)

    def _paint_shield(self, painter: QPainter, token: dict[str, str]) -> None:
        orbit = QColor(token["line_strong"])
        pen = QPen(orbit, 1.4, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(32, 16, 176, 132))
        path = QPainterPath()
        path.moveTo(120, 28)
        path.lineTo(178, 50)
        path.lineTo(178, 90)
        path.cubicTo(178, 118, 152, 136, 120, 146)
        path.cubicTo(88, 136, 62, 118, 62, 90)
        path.lineTo(62, 50)
        path.closeSubpath()
        fill = QColor(token["accent"])
        fill.setAlpha(30)
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(token["accent"]), 2.2))
        painter.drawPath(path)
        painter.setPen(QPen(QColor(token["white"]), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(98, 88), QPointF(114, 104))
        painter.drawLine(QPointF(114, 104), QPointF(145, 71))
        painter.setPen(QPen(QColor(token["line_strong"]), 1.6))
        painter.drawEllipse(QRectF(52, 34, 7, 7))
        painter.drawEllipse(QRectF(184, 102, 5, 5))

    def _paint_auth(self, painter: QPainter, token: dict[str, str]) -> None:
        """登录页设备示意: 显示器 + 手机 + 圆形加号, 几何取自 ui-v2 原型 auth-art."""
        painter.save()
        painter.translate(10.0, 24.0)
        line = QPen(QColor(token["line_strong"]), 2.0)
        line.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(line)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(16, 18, 76, 50), 7, 7)
        painter.drawLine(QPointF(36, 76), QPointF(72, 76))
        painter.drawLine(QPointF(46, 68), QPointF(46, 76))
        painter.drawLine(QPointF(62, 68), QPointF(62, 76))
        painter.drawRoundedRect(QRectF(108, 30, 42, 66), 8, 8)
        painter.drawLine(QPointF(124, 86), QPointF(134, 86))
        accent = QPen(QColor(token["accent"]), 2.0)
        accent.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(accent)
        painter.drawEllipse(QRectF(158, 24, 44, 44))
        painter.drawLine(QPointF(180, 35), QPointF(180, 57))
        painter.drawLine(QPointF(169, 46), QPointF(191, 46))
        dash = QPen(QColor(token["accent"]), 2.0)
        dash.setCapStyle(Qt.PenCapStyle.RoundCap)
        dash.setDashPattern([3.0, 4.0])
        painter.setPen(dash)
        painter.drawLine(QPointF(92, 48), QPointF(106, 48))
        painter.drawLine(QPointF(150, 48), QPointF(158, 48))
        painter.restore()

    def _paint_search(self, painter: QPainter, token: dict[str, str]) -> None:
        self._panel(painter, QRectF(34, 36, 116, 76), token, 10)
        self._screen(painter, QRectF(42, 44, 100, 60), token)
        painter.setPen(QPen(QColor(token["line_strong"]), 2))
        painter.drawLine(QPointF(92, 112), QPointF(92, 124))
        painter.drawLine(QPointF(76, 124), QPointF(108, 124))
        painter.setBrush(QColor(token["accent_soft"]))
        painter.setPen(QPen(QColor(token["accent"]), 2.4))
        painter.drawEllipse(QRectF(136, 62, 56, 56))
        painter.setBrush(QColor(token["card"]))
        painter.drawEllipse(QRectF(144, 70, 40, 40))
        painter.setPen(QPen(QColor(token["accent"]), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(180, 105), QPointF(196, 121))


class EmptyState(QWidget):
    """插画 + 标题 + 说明 + 可选主按钮, 用于各页空状态."""

    def __init__(
        self,
        kind: str,
        title: str,
        description: str,
        *,
        action_text: str = "",
        action_icon: str = "plus",
        action: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.illustration = Illustration(kind)
        layout.addWidget(self.illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        self.desc_label = QLabel(description)
        self.desc_label.setObjectName("emptyDesc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setMaximumWidth(430)
        self.desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.desc_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.action_button: QPushButton | None = None
        if action_text and action is not None:
            layout.addSpacing(4)
            self.action_button = QPushButton(action_text)
            self.action_button.setProperty("primary", True)
            icons.apply_icon(self.action_button, action_icon, tone="white", size=15)
            self.action_button.clicked.connect(action)
            layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)


def chip(text: str, tone: str = "") -> QLabel:
    """语义胶囊标签."""
    label = QLabel(text)
    label.setObjectName("chip")
    if tone:
        label.setProperty("tone", tone)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return label


def metric_card(label: str, value: str = "-", tone: str = "") -> tuple[QFrame, QLabel]:
    """小型数值卡片, 返回卡片与数值标签."""
    card = QFrame()
    card.setObjectName("statCard")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(14, 10, 14, 10)
    layout.setSpacing(2)
    value_label = QLabel(value)
    value_label.setObjectName("metricValue")
    if tone:
        value_label.setProperty("tone", tone)
    caption = QLabel(label)
    caption.setObjectName("metricLabel")
    layout.addWidget(value_label)
    layout.addWidget(caption)
    return card, value_label


class _EffectSetter(Protocol):
    def __call__(self, effect: QGraphicsOpacityEffect | None) -> None: ...


def _clear_effect(widget: QWidget) -> None:
    """移除淡入效果; Qt 允许传 None, 但类型桩未声明."""
    setter = cast(_EffectSetter, widget.setGraphicsEffect)
    setter(None)
    widget.update()


def fade_in(widget: QWidget, duration: int = 170) -> None:
    """页面切换与主题刷新时的淡入过渡, 结束后移除效果避免影响绘制."""
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: _clear_effect(widget))
    QTimer.singleShot(duration + 60, lambda: _clear_effect(widget))
    animation.start()


def apply_shadow(widget: QWidget, blur: int = 24, offset: int = 5, alpha: int = 40) -> None:
    """统一的卡片投影, 深浅色下使用同一套低透明度阴影."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset)
    effect.setColor(_ink(active(), alpha))
    widget.setGraphicsEffect(effect)


COPIED_TEXT = "已复制"
COPY_TOAST_MS = 1500


def copy_to_clipboard(text: str) -> bool:
    """写入系统剪贴板; 空内容不复制, 避免把「已复制」提示用在无内容可复制时."""
    value = (text or "").strip()
    clipboard = QApplication.clipboard()
    if not value or clipboard is None:
        return False
    clipboard.setText(value)
    return True


def show_toast(anchor: QWidget, text: str = COPIED_TEXT, duration: int = COPY_TOAST_MS) -> None:
    """在窗口底部居中弹出一条不接收鼠标的轻提示, 到时自动消失."""
    window = anchor.window()
    previous = window.findChild(QLabel, "toast")
    if previous is not None:
        previous.deleteLater()
    toast = QLabel(text, window)
    toast.setObjectName("toast")
    toast.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
    toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
    toast.adjustSize()
    left = max(12, (window.width() - toast.width()) // 2)
    top = max(12, window.height() - toast.height() - 46)
    toast.move(left, top)
    toast.show()
    toast.raise_()
    QTimer.singleShot(duration, toast.deleteLater)


class CopyButton(QPushButton):
    """行内复制按钮: 点击复制并显示 1.5 秒「已复制」轻提示."""

    copied = Signal(str)

    def __init__(self, value: str = "", tooltip: str = "复制", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value = value
        self.setObjectName("copyBtn")
        self.setFixedSize(26, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)
        icons.apply_icon(self, "copy", tone="muted", size=14)
        self.clicked.connect(self._on_clicked)

    def set_value(self, value: str, tooltip: str | None = None) -> None:
        self._value = value
        if tooltip is not None:
            self.setToolTip(tooltip)
        self.setEnabled(bool(value))

    def _on_clicked(self) -> None:
        if copy_to_clipboard(self._value):
            show_toast(self)
            self.copied.emit(self._value)


@dataclass(frozen=True)
class MenuItem:
    """右键菜单项; 禁用项置灰, 危险项用红色图标并单独隔开."""

    text: str
    callback: Callable[[], None] | None = None
    enabled: bool = True
    danger: bool = False
    checkable: bool = False
    checked: bool = False
    tip: str = ""


def open_menu(anchor: QWidget, position: QPoint, items: list[MenuItem | None]) -> None:
    """在指定位置打开右键菜单; None 表示分隔线."""
    menu = QMenu(anchor)
    menu.setToolTipsVisible(True)
    for entry in items:
        if entry is None:
            menu.addSeparator()
            continue
        action = menu.addAction(entry.text)
        action.setEnabled(entry.enabled)
        if entry.tip:
            action.setToolTip(entry.tip)
        if entry.checkable:
            action.setCheckable(True)
            action.setChecked(entry.checked)
        if entry.danger:
            action.setIcon(icons.icon("trash", active()["danger"], 14))
        if entry.callback is not None and entry.enabled:
            action.triggered.connect(entry.callback)
    menu.exec(anchor.mapToGlobal(position))


def confirm(parent: QWidget, title: str, text: str, ok_text: str = "确认", *, danger: bool = True) -> bool:
    """二次确认对话框; 确认与取消都用中文按钮."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    accept = box.addButton(ok_text, QMessageBox.ButtonRole.AcceptRole)
    reject = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(reject)
    box.exec()
    return box.clickedButton() is accept


class DeviceThumbnail(QWidget):
    """设备卡片缩略图位: 无真实画面时显示统一占位图."""

    def __init__(self, online: bool = False) -> None:
        super().__init__()
        self._online = online
        self.setFixedHeight(112)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(token["thumb_from"]))
        gradient.setColorAt(1.0, QColor(token["thumb_to"]))
        painter.fillPath(path, gradient)
        painter.setClipPath(path)

        glow = QRadialGradient(QPointF(self.width() * 0.78, 6), 150)
        accent = QColor(token["accent"])
        accent.setAlpha(36 if self._online else 16)
        glow.setColorAt(0.0, accent)
        accent.setAlpha(0)
        glow.setColorAt(1.0, accent)
        painter.fillRect(rect, glow)

        grid = QColor(token["line"])
        grid.setAlpha(18)
        painter.setPen(QPen(grid, 1))
        step = 28
        x = step
        while x < self.width():
            painter.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            x += step
        y = step
        while y < self.height():
            painter.drawLine(QPointF(0, y), QPointF(self.width(), y))
            y += step

        plate_w = max(96.0, min(168.0, self.width() * 0.42))
        plate_h = plate_w * 0.6
        plate = QRectF(
            (self.width() - plate_w) / 2,
            (self.height() - plate_h) / 2 - 9,
            plate_w,
            plate_h,
        )
        plate_fill = QColor(token["accent"])
        plate_fill.setAlpha(14)
        painter.setBrush(plate_fill)
        painter.setPen(QPen(QColor(token["line_strong"]), 1))
        painter.drawRoundedRect(plate, 10, 10)

        glyph_size = int(min(30, plate_h * 0.42))
        glyph = icons.pixmap("monitor", token["muted"], glyph_size)
        painter.drawPixmap(
            int(plate.center().x() - glyph.width() / 2),
            int(plate.center().y() - glyph.height() / 2 - plate_h * 0.1),
            glyph,
        )

        font = QFont(self.font())
        font.setPointSizeF(8.6)
        painter.setFont(font)
        painter.setPen(QColor(token["muted"]))
        painter.drawText(
            QRectF(0, self.height() - 30, self.width(), 20),
            Qt.AlignmentFlag.AlignHCenter,
            "无实时画面 · 占位缩略图",
        )

        if self._online:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(token["ok"]))
            painter.drawEllipse(QRectF(self.width() - 18, 12, 6, 6))
        painter.end()


PLATFORM_GLYPH = {
    "windows": "monitor",
    "win": "monitor",
    "macos": "laptop",
    "mac": "laptop",
    "linux": "server",
    "android": "smartphone",
    "ios": "smartphone",
    "iphone": "smartphone",
}


def relative_day(value: str | None) -> str:
    """把 ISO 时间压成设备卡上的相对日期; 空值显示「未知」."""
    text = (value or "").strip()
    if len(text) < 10:
        return "未知"
    today = QDate.currentDate()
    parsed = QDate.fromString(text[:10], "yyyy-MM-dd")
    if not parsed.isValid():
        return "未知"
    days = parsed.daysTo(today)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{parsed.month():02d}-{parsed.day():02d}"


class DeviceCard(QFrame):
    """设备卡片: 缩略图、在线状态、平台、设备编号与四项右键动作."""

    clicked = Signal(str)
    connect_requested = Signal(str)
    trust_requested = Signal(str, bool)
    remove_requested = Signal(str)

    def __init__(self, item: dict[str, Any], *, current: bool = False) -> None:
        super().__init__()
        self.device_id = str(item.get("device_id", ""))
        self.display_name = str(item.get("display_name") or "未命名设备")
        self.online = bool(item.get("online"))
        self.current = current
        self.trusted = bool(item.get("trusted"))
        presence = str(item.get("presence_state") or ("online" if self.online else "offline"))
        platform_key = str(item.get("platform") or "").strip().lower()
        self.setObjectName("deviceCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(246)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        apply_shadow(self, 18, 4, 30)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(DeviceThumbnail(self.online))

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        name = QLabel(self.display_name)
        name.setObjectName("cardTitle")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_row.addWidget(name, 1)
        if self.current:
            title_row.addWidget(chip("本机", "accent"))
        presence_text = {"online": "在线", "offline": "离线"}.get(presence, "状态未知")
        title_row.addWidget(chip(presence_text, "ok" if self.online else ""))
        layout.addLayout(title_row)

        platform_row = QHBoxLayout()
        platform_row.setSpacing(6)
        glyph = QLabel()
        glyph.setPixmap(icons.pixmap(PLATFORM_GLYPH.get(platform_key, "monitor-off"), active()["muted"], 15))
        platform_row.addWidget(glyph)
        platform_name = QLabel(platform_text(platform_key))
        platform_name.setObjectName("cardMeta")
        platform_row.addWidget(platform_name)
        platform_row.addStretch(1)
        if self.trusted:
            badge = chip("受信", "ok")
            badge.setToolTip(
                f"{trusted_source_text(item.get('trust_source'))} · 授信时间 {_plain_time(item.get('trusted_at'))}"
            )
        else:
            badge = chip("需连接码")
            badge.setToolTip("未受信设备连接时需要输入对方给出的连接码")
        platform_row.addWidget(badge)
        layout.addLayout(platform_row)

        code_row = QHBoxLayout()
        code_row.setSpacing(6)
        code_label = QLabel(self.device_id or "-")
        code_label.setObjectName("mono")
        code_label.setProperty("class", "muted")
        code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        code_label.setToolTip(f"设备编号 {self.device_id}")
        code_row.addWidget(code_label, 1)
        code_row.addWidget(CopyButton(self.device_id, "复制设备编号"))
        layout.addLayout(code_row)

        footer = QLabel(
            f"最近连接 {relative_day(item.get('last_connected_at'))} · 绑定 {_plain_time(item.get('bound_at'))}"
        )
        footer.setObjectName("cardMeta")
        layout.addWidget(footer)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.device_id:
            self.clicked.emit(self.device_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        """设备卡右键菜单: 6 项, 不可用项置灰, 危险项单独隔开."""
        if self.device_id:
            self.clicked.emit(self.device_id)
        connect_state = self.online and not self.current
        trust_text = "撤销信任" if self.trusted else "设为受信"
        items: list[MenuItem | None] = [
            MenuItem(
                "连接",
                lambda: self.connect_requested.emit(self.device_id),
                enabled=connect_state,
                tip="" if connect_state else ("本机不可自连" if self.current else "设备离线, 无法连接"),
            ),
            MenuItem(
                trust_text,
                lambda: self.trust_requested.emit(self.device_id, not self.trusted),
                enabled=not self.current,
                tip="" if not self.current else "本机不可设置信任",
            ),
            MenuItem("重命名", None, enabled=False, tip="契约暂未提供重命名命令"),
            MenuItem("复制设备编号", self._copy_device_id),
            None,
            MenuItem(
                "删除",
                lambda: self.remove_requested.emit(self.device_id),
                enabled=not self.current,
                danger=True,
                tip="只解绑并移出列表, 不卸载对端" if not self.current else "本机不可删除",
            ),
        ]
        open_menu(self, event.pos(), items)

    def _copy_device_id(self) -> None:
        if copy_to_clipboard(self.device_id):
            show_toast(self)


def _plain_time(value: object) -> str:
    """时间只保留到分钟; 空值显示「未知」."""
    text = str(value or "").strip()
    if len(text) >= 16 and text[4] == "-" and text[10] == "T":
        return f"{text[0:10]} {text[11:16]}"
    return text or "未知"


class WallCellButton(QPushButton):
    """屏幕墙格子: 自绘预览占位, 保留按钮文本与点击行为."""

    def __init__(self, text: str, focused: bool = False) -> None:
        super().__init__(text)
        self._focused = focused
        self._hover = False
        self.setMinimumHeight(124)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        token = active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        if self._focused:
            gradient.setColorAt(0.0, QColor(token["accent"]))
            gradient.setColorAt(1.0, QColor(token["thumb_from"]))
        elif self._hover:
            gradient.setColorAt(0.0, QColor(token["card_hover"]))
            gradient.setColorAt(1.0, QColor(token["thumb_to"]))
        else:
            gradient.setColorAt(0.0, QColor(token["thumb_from"]))
            gradient.setColorAt(1.0, QColor(token["thumb_to"]))
        painter.fillPath(path, gradient)
        border = QColor(token["accent"] if self._focused else token["line_strong"])
        painter.setPen(QPen(border, 2 if self._focused else 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        monitor_rect = QRectF(rect.center().x() - 34, rect.top() + 18, 68, 42)
        screen_color = QColor(token["white"] if self._focused else token["faint"])
        screen_color.setAlpha(70 if self._focused else 40)
        painter.setBrush(screen_color)
        painter.setPen(QPen(QColor(token["white"] if self._focused else token["line_strong"]), 1.6))
        painter.drawRoundedRect(monitor_rect, 6, 6)
        painter.drawLine(
            QPointF(rect.center().x(), monitor_rect.bottom()),
            QPointF(rect.center().x(), monitor_rect.bottom() + 7),
        )
        painter.drawLine(
            QPointF(rect.center().x() - 12, monitor_rect.bottom() + 7),
            QPointF(rect.center().x() + 12, monitor_rect.bottom() + 7),
        )

        if self._focused:
            badge = QRectF(rect.left() + 10, rect.top() + 10, 42, 20)
            painter.setBrush(_ink(token, 60))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(badge, 10, 10)
            painter.setPen(QColor(token["white"]))
            font = QFont(self.font())
            font.setPointSizeF(8)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "聚焦")

        lines = self.text().split("\n")
        text_color = QColor(token["white"] if self._focused else token["text"])
        font = QFont(self.font())
        font.setPointSizeF(9.6)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(rect.left() + 12, rect.bottom() - 50, rect.width() - 24, 20),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            lines[0] if lines else "",
        )
        if len(lines) > 1:
            font.setPointSizeF(8.2)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(token["white"] if self._focused else token["muted"]))
            painter.drawText(
                QRectF(rect.left() + 12, rect.bottom() - 30, rect.width() - 24, 18),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                lines[1],
            )
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(token["accent"]), 1.2, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 10, 10)
        painter.end()
