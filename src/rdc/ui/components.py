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
from typing import Any, Protocol, cast

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
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
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rdc.ui import icons
from rdc.ui.theme import active


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
        gradient.setColorAt(0.0, QColor(token["accent"]))
        gradient.setColorAt(1.0, QColor(token["accent_2"]))
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
        painter.setBrush(QColor(0, 0, 0, 26))
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
        painter.setBrush(QColor(0, 0, 0, 22))
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
        painter.setBrush(QColor(0, 0, 0, 22))
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
        painter.setPen(QPen(QColor("#ffffff"), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(159, 89), QPointF(166, 96))
        painter.drawLine(QPointF(166, 96), QPointF(177, 82))

    def _paint_session(self, painter: QPainter, token: dict[str, str]) -> None:
        painter.setBrush(QColor(0, 0, 0, 26))
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
        painter.setPen(QPen(QColor("#ffffff"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(98, 88), QPointF(114, 104))
        painter.drawLine(QPointF(114, 104), QPointF(145, 71))
        painter.setPen(QPen(QColor(token["line_strong"]), 1.6))
        painter.drawEllipse(QRectF(52, 34, 7, 7))
        painter.drawEllipse(QRectF(184, 102, 5, 5))

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
    effect.setColor(QColor(4, 10, 20, alpha))
    widget.setGraphicsEffect(effect)


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


class DeviceCard(QFrame):
    """设备卡片: 缩略图、在线状态、名称、系统标识与绑定信息."""

    clicked = Signal(str)

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__()
        self.device_id = str(item.get("device_id", ""))
        self.setObjectName("deviceCard")
        self.setProperty("selected", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(246)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        apply_shadow(self, 18, 4, 30)
        online = bool(item.get("online"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(DeviceThumbnail(online))

        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        name = QLabel(str(item.get("display_name") or "未命名设备"))
        name.setObjectName("cardTitle")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_row.addWidget(name, 1)
        status = chip("在线" if online else "离线", "ok" if online else "")
        title_row.addWidget(status)
        layout.addLayout(title_row)

        device_label = QLabel(self.device_id or "-")
        device_label.setObjectName("mono")
        device_label.setProperty("class", "muted")
        layout.addWidget(device_label)

        os_name = str(item.get("os") or item.get("platform") or item.get("os_name") or "系统未上报")
        meta = QHBoxLayout()
        meta.setSpacing(6)
        meta.addWidget(chip(os_name))
        meta.addWidget(chip("所有者" if item.get("owner") else "被授权"))
        meta.addWidget(chip("免确认" if not item.get("require_confirm") else "需审批"))
        meta.addStretch(1)
        layout.addLayout(meta)

        bound = str(item.get("bound_at") or "")
        footer = QLabel(f"绑定时间 {bound}" if bound else "绑定时间 -")
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
            painter.setBrush(QColor(0, 0, 0, 60))
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
