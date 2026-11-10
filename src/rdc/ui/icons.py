# ==========================================================================================
# 文件        : icons.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 统一图标出口; 加载本地 Lucide SVG 并按主题与状态着色
# 架构定位    : 界面层表现资源; 不承载业务逻辑
# 核心功能    : 图标缓存、导航双状态图标、按钮图标、主题切换后批量刷新
# 职责边界    : 只读本地资产; 不联网; 不写文件
# ==========================================================================================

"""图标加载与着色."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton, QWidget

from rdc.ui.theme import APP_ICON, DARK, LIGHT, active

ASSET_DIR = Path(__file__).with_name("assets") / "icons"
_CACHE: dict[tuple[str, str, int, float], QPixmap] = {}
_NAMES: set[str] | None = None


def available(name: str) -> bool:
    """判断图标是否已随项目固化."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {path.stem for path in ASSET_DIR.glob("*.svg")}
    return name in _NAMES


def _render(name: str, color: str, size: int, ratio: float) -> QPixmap:
    key = (name, color, size, ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    path = ASSET_DIR / f"{name}.svg"
    source = path.read_text(encoding="utf-8") if path.exists() else ""
    source = source.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(source.encode("utf-8")))
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    _CACHE[key] = pixmap
    return pixmap


def pixmap(name: str, color: str, size: int = 18, ratio: float = 2.0) -> QPixmap:
    """返回按颜色替换后的矢量图标位图."""
    return _render(name, color, size, ratio)


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """单色图标."""
    return QIcon(_render(name, color, size, 2.0))


def nav_icon(name: str, light: bool = False) -> QIcon:
    """导航项: 默认态用次要色, 选中态用强调色."""
    token = LIGHT if light else DARK
    result = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active):
        result.addPixmap(pixmap(name, token["muted"], 18), mode, QIcon.State.Off)
        result.addPixmap(pixmap(name, token["accent"], 18), mode, QIcon.State.On)
    return result


def tone_color(tone: str, light: bool = False) -> str:
    """把语义色调映射为当前主题颜色."""
    token = LIGHT if light else DARK
    return {
        "text": token["text"],
        "muted": token["muted"],
        "accent": token["accent"],
        "ok": token["ok"],
        "warn": token["warn"],
        "danger": token["danger"],
        "white": token["white"],
    }.get(tone, token["text"])


def apply_icon(
    button: QPushButton,
    name: str,
    *,
    tone: str = "text",
    size: int = 16,
    light: bool | None = None,
) -> None:
    """给按钮设置图标并记录语义, 主题切换时可批量刷新."""
    is_light = _is_light() if light is None else light
    button.setProperty("iconName", name)
    button.setProperty("iconTone", tone)
    button.setProperty("iconPx", size)
    if button.property("nav"):
        button.setIcon(nav_icon(name, is_light))
    else:
        button.setIcon(icon(name, tone_color(tone, is_light), size))
    button.setIconSize(QSize(size, size))


def refresh_icons(root: QWidget, light: bool) -> None:
    """主题切换后刷新子树内所有登记过图标的按钮."""
    for button in root.findChildren(QPushButton):
        name = button.property("iconName")
        if not name:
            continue
        size = int(button.property("iconPx") or 16)
        if button.property("nav"):
            button.setIcon(nav_icon(str(name), light))
        else:
            tone = str(button.property("iconTone") or "text")
            button.setIcon(icon(str(name), tone_color(tone, light), size))
        button.setIconSize(QSize(size, size))
    _fade_window(root)


def _fade_window(root: QWidget) -> None:
    """主题切换时对窗口内容做一次轻量淡入, 不改变布局与状态."""
    from PySide6.QtWidgets import QMainWindow

    from rdc.ui.components import fade_in

    window = root.window()
    if not isinstance(window, QMainWindow) or not window.isVisible():
        return
    central = window.centralWidget()
    if central is not None:
        fade_in(central, 150)


def _is_light() -> bool:
    return active() is LIGHT


APP_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""应用图标内置尺寸; 与 scripts/make_app_icon.py 导出的一致, 保证任务栏与资源管理器取到原尺寸."""
PLATE_STOPS = ((0.0, APP_ICON["plate_top"]), (0.52, APP_ICON["plate_bottom"]), (1.0, APP_ICON["plate_edge"]))
_MARK_DEEP = APP_ICON["mark_deep"]


def _app_pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _app_plate(painter: QPainter, size: float) -> None:
    """圆角渐变底板: 深色任务栏与浅色桌面下都能立住边界."""
    inset = size * 0.028
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = size * 0.235
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    for position, color in PLATE_STOPS:
        gradient.setColorAt(position, QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(rect, radius, radius)
    sheen = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.center().y()))
    sheen.setColorAt(0.0, QColor(255, 255, 255, 54))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(sheen)
    painter.drawRoundedRect(rect, radius, radius)


def _app_arrow(painter: QPainter, left: QPointF, right: QPointF, width: float, color: str) -> None:
    """双向箭头: 链路两端可互控."""
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_app_pen(color, width))
    painter.drawLine(left, right)
    head = width * 2.3
    painter.drawLine(left, QPointF(left.x() + head, left.y() - head * 0.86))
    painter.drawLine(left, QPointF(left.x() + head, left.y() + head * 0.86))
    painter.drawLine(right, QPointF(right.x() - head, right.y() - head * 0.86))
    painter.drawLine(right, QPointF(right.x() - head, right.y() + head * 0.86))


def _app_mark(painter: QPainter, size: float) -> None:
    """显示器 + 双向箭头; 小尺寸退化为实心屏幕, 只保留必要笔画."""
    if size >= 48:
        stroke = size * 0.052
        radius = size * 0.062
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_app_pen(APP_ICON["glyph"], stroke))
        painter.drawRoundedRect(QRectF(size * 0.196, size * 0.200, size * 0.608, size * 0.352), radius, radius)
        painter.drawLine(QPointF(size * 0.5, size * 0.552), QPointF(size * 0.5, size * 0.662))
        painter.drawLine(QPointF(size * 0.372, size * 0.681), QPointF(size * 0.628, size * 0.681))
        _app_arrow(
            painter,
            QPointF(size * 0.312, size * 0.376),
            QPointF(size * 0.688, size * 0.376),
            size * 0.045,
            APP_ICON["glyph"],
        )
    elif size >= 28:
        stroke = size * 0.062
        radius = size * 0.080
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_app_pen(APP_ICON["glyph"], stroke))
        painter.drawRoundedRect(QRectF(size * 0.152, size * 0.222, size * 0.696, size * 0.416), radius, radius)
        _app_arrow(
            painter,
            QPointF(size * 0.292, size * 0.430),
            QPointF(size * 0.708, size * 0.430),
            size * 0.058,
            APP_ICON["glyph"],
        )
        painter.drawLine(QPointF(size * 0.5, size * 0.638), QPointF(size * 0.5, size * 0.726))
        painter.drawLine(QPointF(size * 0.392, size * 0.750), QPointF(size * 0.608, size * 0.750))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(APP_ICON["glyph"]))
        painter.drawRoundedRect(
            QRectF(size * 0.128, size * 0.196, size * 0.744, size * 0.436), size * 0.11, size * 0.11
        )
        painter.drawRoundedRect(
            QRectF(size * 0.436, size * 0.632, size * 0.128, size * 0.086), size * 0.03, size * 0.03
        )
        painter.drawRoundedRect(
            QRectF(size * 0.310, size * 0.718, size * 0.380, size * 0.082), size * 0.040, size * 0.040
        )
        if size >= 22:
            _app_arrow(
                painter,
                QPointF(size * 0.246, size * 0.414),
                QPointF(size * 0.754, size * 0.414),
                max(1.3, size * 0.074),
                _MARK_DEEP,
            )


def app_pixmap(size: int) -> QPixmap:
    """按目标像素尺寸重绘应用图标, 不做位图缩放."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _app_plate(painter, float(size))
    _app_mark(painter, float(size))
    painter.end()
    return pixmap


def app_icon(light: bool = False) -> QIcon:
    """应用图标: 自绘品牌图形, 内置 16~256 原尺寸, 不随主题换色."""
    del light
    result = QIcon()
    for size in APP_SIZES:
        result.addPixmap(app_pixmap(size))
    return result
