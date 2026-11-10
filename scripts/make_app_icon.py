# ==========================================================================================
# 文件        : make_app_icon.py
# 所属目录    : scripts/
# 文件用途    : 生成 RDC 桌面端应用图标: 三方向候选预览 + 多尺寸 ico / png / icns
# 架构定位    : 设计期生成脚本, 不进入运行时; 图形全部由 QPainter 参数化绘制
# 核心功能    : A/B/C 三方向矢量绘制; 16~1024 png; ico; icns; 预览拼图
# 职责边界    : 不联网; 不引入外部图片素材; 不修改应用源码
# ==========================================================================================

"""应用图标生成.

用法:
    python scripts/make_app_icon.py --out assets/app-icon
    python scripts/make_app_icon.py --out assets/app-icon --final a

产物:
    assets/app-icon/png/rdc-<size>.png      全尺寸 png
    assets/app-icon/rdc.ico                16/32/48/64/128/256
    assets/app-icon/rdc.icns               macOS 备用
    assets/app-icon/preview-<a|b|c>.png    单方向 16/32/48/128 预览拼图
    assets/app-icon/preview-compare.png    三方向对照拼图
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

from _rdc_path import PROJECT_ROOT
from rdc.ui.icons import app_pixmap

ROOT = PROJECT_ROOT

ICO_SIZES = (16, 32, 48, 64, 128, 256)
PNG_SIZES = (16, 22, 24, 32, 48, 64, 72, 96, 128, 144, 192, 256, 512, 1024)
ICNS_TYPES = (
    (16, b"icp4"),
    (32, b"icp5"),
    (64, b"icp6"),
    (128, b"ic07"),
    (256, b"ic08"),
    (512, b"ic09"),
    (1024, b"ic10"),
)
DARK_BG = "#0b1017"
LIGHT_BG = "#f2f5f9"
WHITE = "#ffffff"
"""图标底版渐变色, 取自主题强调色 accent (#4d8dff) 的深-中-浅三段."""
PLATE_STOPS = ((0.0, "#6fa8ff"), (0.52, "#3b7bf6"), (1.0, "#1f55cc"))
DEEP = "#1b4099"


def _pen(color: str, width: float) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _plate(painter: QPainter, size: float, inset_ratio: float = 0.028, radius_ratio: float = 0.235) -> None:
    """圆角渐变底板; 深色任务栏与浅色桌面下都能保持边界."""
    inset = size * inset_ratio
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = size * radius_ratio
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


def _double_arrow(painter: QPainter, left: QPointF, right: QPointF, width: float, color: str) -> None:
    """双向箭头: 表示远程链路两端可互相控制."""
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_pen(color, width))
    painter.drawLine(left, right)
    head = width * 2.3
    painter.drawLine(left, QPointF(left.x() + head, left.y() - head * 0.86))
    painter.drawLine(left, QPointF(left.x() + head, left.y() + head * 0.86))
    painter.drawLine(right, QPointF(right.x() - head, right.y() - head * 0.86))
    painter.drawLine(right, QPointF(right.x() - head, right.y() + head * 0.86))


def _letter_r(painter: QPainter, size: float) -> None:
    """方向 B: 字母 R 的几何构成, 强调品牌."""
    stem = size * (0.135 if size < 40 else 0.118)
    left = size * 0.286
    right = size * 0.545
    top = size * 0.196 if size >= 40 else size * 0.206
    bottom = size * 0.804
    bowl_bottom = top + (bottom - top) * 0.505
    path = QPainterPath()
    path.moveTo(left, top)
    path.lineTo(right, top)
    path.arcTo(QRectF(right - (bowl_bottom - top) / 2, top, bowl_bottom - top, bowl_bottom - top), 90, -180)
    path.lineTo(left, bowl_bottom)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(_pen(WHITE, stem))
    painter.drawPath(path)
    painter.drawLine(QPointF(left, (top + bottom) / 2), QPointF(left, bottom))
    painter.drawLine(
        QPointF(left + (right - left) * 0.42, bowl_bottom - stem * 0.25),
        QPointF(size * 0.712, bottom),
    )


def _cursor_path(box: QRectF) -> QPainterPath:
    """经典指针形状, 映射到给定方框."""
    unit = (
        (0.00, 0.00),
        (0.66, 0.46),
        (0.37, 0.49),
        (0.53, 0.86),
        (0.38, 0.93),
        (0.22, 0.56),
        (0.02, 0.74),
    )
    path = QPainterPath()
    for index, (ux, uy) in enumerate(unit):
        point = QPointF(box.left() + ux * box.width(), box.top() + uy * box.height())
        if index == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


def _screen_cursor(painter: QPainter, size: float) -> None:
    """方向 C: 屏幕 + 光标, 强调接管."""
    if size >= 44:
        stroke = size * 0.050
        radius = size * 0.060
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_pen(WHITE, stroke))
        painter.drawRoundedRect(QRectF(size * 0.180, size * 0.186, size * 0.640, size * 0.360), radius, radius)
        painter.drawLine(QPointF(size * 0.5, size * 0.546), QPointF(size * 0.5, size * 0.652))
        painter.drawLine(QPointF(size * 0.366, size * 0.672), QPointF(size * 0.634, size * 0.672))
        cursor = _cursor_path(QRectF(size * 0.352, size * 0.256, size * 0.300, size * 0.352))
        painter.setPen(_pen(DEEP, stroke * 1.9))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(cursor)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(WHITE))
        painter.drawPath(cursor)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(WHITE))
        painter.drawRoundedRect(
            QRectF(size * 0.128, size * 0.196, size * 0.744, size * 0.448), size * 0.10, size * 0.10
        )
        painter.drawRoundedRect(
            QRectF(size * 0.330, size * 0.712, size * 0.340, size * 0.078), size * 0.035, size * 0.035
        )
        painter.setBrush(QColor(DEEP))
        painter.drawPath(_cursor_path(QRectF(size * 0.372, size * 0.276, size * 0.286, size * 0.340)))


MARKS = {"b": _letter_r, "c": _screen_cursor}


def render(direction: str, size: int) -> QImage:
    """按实际像素尺寸重绘, 而不是缩放位图, 保证小尺寸清晰."""
    if direction == "a":
        return app_pixmap(size).toImage()
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    _plate(painter, size)
    MARKS[direction](painter, float(size))
    painter.end()
    return image


def _png_bytes(image: QImage) -> bytes:
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    payload = b""
    for size, data in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset + len(payload))
        payload += data
    return header + entries + payload


def _icns_bytes(images: dict[int, bytes]) -> bytes:
    body = b""
    for size, tag in ICNS_TYPES:
        data = images[size]
        body += tag + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def _nearest(image: QImage, factor: int) -> QImage:
    return image.scaled(
        image.width() * factor,
        image.height() * factor,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def _blit(painter: QPainter, image: QImage, x: float, y: float) -> None:
    painter.drawImage(QPointF(x, y), image)


def _sheet(direction: str, title: str, note: str) -> QImage:
    """单方向预览: 深/浅两行, 128/48/32/16 实际尺寸 + 放大后的像素真相."""
    width, row_height, pad = 1080, 172, 34
    height = pad + 46 + row_height * 2 + 26
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setPen(QColor("#101828"))
    font = QFont("Microsoft YaHei UI", 13)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QPointF(pad, 34), title)
    painter.setPen(QColor("#5b6675"))
    painter.setFont(QFont("Microsoft YaHei UI", 9))
    painter.drawText(QPointF(pad, 54), note)

    columns = [
        (128, 1, "128"),
        (48, 1, "48"),
        (32, 1, "32"),
        (16, 1, "16"),
        (32, 5, "32 x5"),
        (16, 8, "16 x8"),
    ]
    labels = [0, 0, 0, 0, 0, 0]
    for row, background in enumerate((DARK_BG, LIGHT_BG)):
        top = pad + 46 + row * row_height - 10
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(pad - 12, top, width - 2 * (pad - 12), row_height - 8), 10, 10)
        x = pad + 16
        baseline = top + row_height - 44
        for index, (size, factor, _label) in enumerate(columns):
            icon = render(direction, size)
            shown = icon if factor == 1 else _nearest(icon, factor)
            _blit(painter, shown, x, baseline - shown.height())
            labels[index] = x + shown.width() / 2
            x += shown.width() + (58 if factor > 1 else 46)
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.setPen(QColor("#9aa4b2") if background == DARK_BG else QColor("#7b8595"))
        for index, (_, _, label) in enumerate(columns):
            text_width = QFontMetrics(painter.font()).horizontalAdvance(label)
            painter.drawText(QPointF(labels[index] - text_width / 2, baseline + 14), label)
    painter.end()
    return image


def _compare_sheet() -> QImage:
    width, cell, pad, top = 1080, 352, 34, 88
    height = top + cell * 2 + 30
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setPen(QColor("#101828"))
    font = QFont("Microsoft YaHei UI", 13)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QPointF(pad, 34), "RDC 应用图标 · 三方向候选")
    painter.setFont(QFont("Microsoft YaHei UI", 9))
    painter.setPen(QColor("#5b6675"))
    painter.drawText(QPointF(pad, 54), "同一图形按实际像素重绘: 16 与 32 看放大后的像素真相, 判断小尺寸是否糊")
    titles = {"a": "A  显示器 + 连接箭头 / 远程控制", "b": "B  字母 R 几何构成 / 品牌", "c": "C  屏幕 + 光标 / 接管"}
    column_width = (width - 2 * pad) / 3
    for index, key in enumerate(("a", "b", "c")):
        left = pad + index * column_width
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.setPen(QColor("#334155"))
        painter.drawText(QPointF(left + 6, top - 12), titles[key])
    for row, background in enumerate((DARK_BG, LIGHT_BG)):
        row_top = top + row * cell
        for index, key in enumerate(("a", "b", "c")):
            left = pad + index * column_width
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(QRectF(left, row_top + 6, column_width - 14, cell - 26), 10, 10)
            big = render(key, 128)
            _blit(painter, big, left + 26, row_top + 40)
            small_x = left + 26
            for size in (48, 32, 16):
                small = render(key, size)
                _blit(painter, small, small_x, row_top + 40 + 128 + 22)
                small_x += small.width() + 22
    painter.end()
    return image


ANDROID_DENSITIES = (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192))
LINUX_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


def _spread_android(rendered: dict[int, QImage], target: Path) -> int:
    """按密度写入 Android launcher 位图; 自适应图标仍由 mipmap-anydpi-v26 组合."""
    count = 0
    for density, size in ANDROID_DENSITIES:
        folder = target / f"mipmap-{density}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ic_launcher.png").write_bytes(_png_bytes(rendered[size]))
        count += 1
    return count


def _spread_linux(rendered: dict[int, QImage], target: Path) -> int:
    """写入 hicolor 主题图标目录, 供 .desktop 的 Icon=rdc-desktop 解析."""
    count = 0
    for size in LINUX_SIZES:
        folder = target / f"{size}x{size}" / "apps"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "rdc-desktop.png").write_bytes(_png_bytes(rendered[size]))
        count += 1
    return count


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 RDC 应用图标")
    parser.add_argument("--out", default=str(ROOT / "assets" / "app-icon"))
    parser.add_argument("--final", default="a", choices=("a", "b", "c"))
    parser.add_argument("--android", default="", help="Android res 目录, 写入 mipmap 各密度位图")
    parser.add_argument("--linux", default="", help="hicolor 根目录, 写入各尺寸 png")
    args = parser.parse_args(argv)

    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app
    out = Path(args.out)
    (out / "png").mkdir(parents=True, exist_ok=True)

    rendered = {size: render(args.final, size) for size in PNG_SIZES}
    for size, image in rendered.items():
        (out / "png" / f"rdc-{size}.png").write_bytes(_png_bytes(image))

    ico = _ico_bytes([(size, _png_bytes(rendered[size])) for size in ICO_SIZES])
    (out / "rdc.ico").write_bytes(ico)
    icns = _icns_bytes({size: _png_bytes(rendered[size]) for size in (16, 32, 64, 128, 256, 512, 1024)})
    (out / "rdc.icns").write_bytes(icns)

    if args.android:
        count = _spread_android(rendered, Path(args.android))
        _emit(f"Android 位图: {count} 个密度 -> {args.android}")
    if args.linux:
        count = _spread_linux(rendered, Path(args.linux))
        _emit(f"Linux hicolor: {count} 个尺寸 -> {args.linux}")

    notes = {
        "a": "显示器轮廓 + 双向箭头: 16px 时退化为实心屏幕, 只保留最少笔画",
        "b": "R 由竖干/半圆碗/斜腿三段构成, 粗笔画保证 16px 可读",
        "c": "屏幕内叠加指针, 强调接管关系, 小尺寸用深蓝指针压白屏",
    }
    for key, title in (
        ("a", "方向 A · 显示器 + 连接箭头"),
        ("b", "方向 B · 字母 R 几何构成"),
        ("c", "方向 C · 屏幕 + 光标"),
    ):
        _sheet(key, title, notes[key]).save(str(out / f"preview-{key}.png"), "PNG")
    _compare_sheet().save(str(out / "preview-compare.png"), "PNG")

    _emit(f"输出目录: {out}")
    _emit(f"落地方向: {args.final.upper()}")
    _emit(f"ico 尺寸: {', '.join(str(item) for item in ICO_SIZES)}")
    _emit(f"ico 字节: {len(ico)}")
    _emit(f"icns 字节: {len(icns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
