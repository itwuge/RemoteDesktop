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

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton, QWidget

from rdc.ui.theme import DARK, LIGHT, active

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


def app_icon(light: bool = False) -> QIcon:
    """应用图标: 自绘圆角渐变底 + 显示器符号, 不依赖外部品牌素材."""
    token = LIGHT if light else DARK
    size = 128
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    from PySide6.QtGui import QLinearGradient

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor(token["accent"]))
    gradient.setColorAt(1.0, QColor(token["accent_2"]))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, size - 8, size - 8, 28, 28)
    glyph = _render("monitor", token["white"], 60, 1.0)
    painter.drawPixmap((size - 60) // 2, (size - 60) // 2, glyph)
    painter.end()
    return QIcon(pixmap)
