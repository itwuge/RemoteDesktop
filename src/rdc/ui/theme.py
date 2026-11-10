# ==========================================================================================
# 文件        : theme.py
# 所属目录    : src/rdc/ui/
# 文件用途    : 深色 / 浅色两套视觉 token 与 QSS, 统一色阶、圆角、间距与字号阶梯
# 架构定位    : 界面层表现层; 不承载任何业务逻辑
# 核心功能    : 切换主题时整窗重绘; 为自绘组件提供当前色板
# 职责边界    : 不改业务状态; 不发命令; 不读写文件
# ==========================================================================================

"""界面主题."""

from __future__ import annotations

DARK = {
    "bg": "#070b12",
    "bg_2": "#0a1018",
    "panel": "#0c131d",
    "panel_2": "#101925",
    "card": "#111b27",
    "card_hover": "#16222f",
    "line": "#1e2b3b",
    "line_strong": "#2b3c50",
    "text": "#e9eff8",
    "muted": "#9aa9bd",
    "faint": "#66768c",
    "accent": "#4d8dff",
    "accent_2": "#7aaeff",
    "accent_soft": "rgba(77, 141, 255, 0.16)",
    "accent_soft_2": "rgba(77, 141, 255, 0.07)",
    "accent_border": "rgba(77, 141, 255, 0.34)",
    "accent_border_soft": "rgba(77, 141, 255, 0.22)",
    "ok": "#37d39a",
    "ok_soft": "rgba(55, 211, 154, 0.12)",
    "ok_border": "rgba(55, 211, 154, 0.34)",
    "warn": "#f0b45e",
    "warn_soft": "rgba(240, 180, 94, 0.12)",
    "warn_border": "rgba(240, 180, 94, 0.34)",
    "danger": "#ff7676",
    "danger_soft": "rgba(255, 118, 118, 0.12)",
    "danger_border": "rgba(255, 118, 118, 0.34)",
    "input": "#0a1119",
    "hover": "#172333",
    "hero_from": "#16325a",
    "hero_to": "#0d1725",
    "thumb_from": "#1b2c42",
    "thumb_to": "#0c1622",
    "white": "#ffffff",
    "shadow": "#02060c",
}

LIGHT = {
    "bg": "#eef2f7",
    "bg_2": "#e8edf4",
    "panel": "#ffffff",
    "panel_2": "#f5f8fc",
    "card": "#ffffff",
    "card_hover": "#f7faff",
    "line": "#e0e7f0",
    "line_strong": "#c9d4e2",
    "text": "#17202b",
    "muted": "#4a5b70",
    "faint": "#5f6d7e",
    "accent": "#2f6fe0",
    "accent_2": "#1f5ecb",
    "accent_soft": "rgba(47, 111, 224, 0.12)",
    "accent_soft_2": "rgba(47, 111, 224, 0.06)",
    "accent_border": "rgba(47, 111, 224, 0.34)",
    "accent_border_soft": "rgba(47, 111, 224, 0.22)",
    "ok": "#128a62",
    "ok_soft": "rgba(18, 138, 98, 0.10)",
    "ok_border": "rgba(18, 138, 98, 0.30)",
    "warn": "#9a640f",
    "warn_soft": "rgba(154, 100, 15, 0.10)",
    "warn_border": "rgba(154, 100, 15, 0.30)",
    "danger": "#c54343",
    "danger_soft": "rgba(197, 67, 67, 0.10)",
    "danger_border": "rgba(197, 67, 67, 0.30)",
    "input": "#fbfcfe",
    "hover": "#eef3fa",
    "hero_from": "#dbe8ff",
    "hero_to": "#f2f6fd",
    "thumb_from": "#dce6f4",
    "thumb_to": "#eef3fa",
    "white": "#ffffff",
    "shadow": "#b8c5d6",
}

_ACTIVE = DARK


def active() -> dict[str, str]:
    """返回当前生效的色板, 供自绘组件取色."""
    return _ACTIVE


def stylesheet(light: bool = False) -> str:
    """按主题生成整窗 QSS."""
    token = LIGHT if light else DARK
    return f"""
QWidget {{
    color: {token["text"]};
    font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {token["bg"]}; }}
QWidget#central {{ background: {token["bg"]}; }}
QFrame#titleBar {{ background: {token["accent"]}; border-radius: 2px; }}
QToolTip {{
    background: {token["panel_2"]};
    color: {token["text"]};
    border: 1px solid {token["line_strong"]};
    border-radius: 6px;
    padding: 5px 8px;
}}

/* ---------------------------------------------------------------- 侧栏 */
QFrame#sidebar {{
    background: {token["panel"]};
    border-right: 1px solid {token["line"]};
}}
QLabel#brand {{ font-size: 16px; font-weight: 700; letter-spacing: 0.2px; }}
QLabel#brandSub {{ color: {token["muted"]}; font-size: 11px; }}
QLabel#navSection {{
    color: {token["faint"]};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1.6px;
}}
QLabel#sidebarFoot {{ color: {token["faint"]}; font-size: 11px; }}

/* ---------------------------------------------------------------- 文字阶梯 */
QLabel#pageTitle {{ font-size: 24px; font-weight: 700; }}
QLabel#pageSub {{ color: {token["muted"]}; font-size: 12px; }}
QLabel#eyebrow {{
    color: {token["accent"]};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.8px;
}}
QLabel#sectionTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#sectionSub {{ color: {token["muted"]}; font-size: 12px; }}
QLabel#cardTitle {{ font-size: 13px; font-weight: 600; }}
QLabel#cardMeta {{ color: {token["muted"]}; font-size: 11px; }}
QLabel#emptyTitle {{ font-size: 16px; font-weight: 600; }}
QLabel#emptyDesc {{ color: {token["muted"]}; font-size: 12px; }}
QLabel#metricValue {{ font-size: 22px; font-weight: 700; }}
QLabel#metricLabel {{ color: {token["muted"]}; font-size: 11px; }}
QLabel#heroCode {{
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 28px;
    font-weight: 600;
    letter-spacing: 3px;
}}
QLabel#heroLabel {{ color: {token["muted"]}; font-size: 11px; }}
QLabel#heroCode[compact="true"] {{
    font-size: 24px;
    letter-spacing: 2px;
}}
QLabel#bigCode {{
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 18px;
    letter-spacing: 2px;
}}
QLabel#mono {{ font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }}
QLabel#muted, QLabel[class="muted"] {{ color: {token["muted"]}; font-size: 12px; }}
QLabel#badgeOk {{ color: {token["ok"]}; }}
QLabel#badgeWarn {{ color: {token["warn"]}; }}
QLabel#badgeDanger {{ color: {token["danger"]}; }}

/* ---------------------------------------------------------------- 胶囊 */
QLabel#chip {{
    background: {token["panel_2"]};
    border: 1px solid {token["line"]};
    border-radius: 9px;
    padding: 3px 9px;
    color: {token["muted"]};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#chip[tone="ok"] {{
    background: {token["ok_soft"]};
    border-color: {token["ok_border"]};
    color: {token["ok"]};
}}
QLabel#chip[tone="warn"] {{
    background: {token["warn_soft"]};
    border-color: {token["warn_border"]};
    color: {token["warn"]};
}}
QLabel#chip[tone="danger"] {{
    background: {token["danger_soft"]};
    border-color: {token["danger_border"]};
    color: {token["danger"]};
}}
QLabel#chip[tone="accent"] {{
    background: {token["accent_soft"]};
    border-color: {token["accent_border"]};
    color: {token["accent"]};
}}

/* ---------------------------------------------------------------- 容器 */
QFrame#card, QFrame#statCard {{
    background: {token["card"]};
    border: 1px solid {token["line"]};
    border-radius: 14px;
}}
QFrame#statCard {{ border-radius: 12px; }}
QFrame#hero {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {token["hero_from"]}, stop:1 {token["hero_to"]});
    border: 1px solid {token["accent_border"]};
    border-radius: 18px;
}}
QFrame#heroPanel {{
    background: {token["accent_soft_2"]};
    border: 1px solid {token["accent_border_soft"]};
    border-radius: 14px;
}}
QFrame#deviceCard {{
    background: {token["card"]};
    border: 1px solid {token["line"]};
    border-radius: 14px;
}}
QFrame#deviceCard:hover {{ border-color: {token["line_strong"]}; background: {token["card_hover"]}; }}
QFrame#deviceCard[selected="true"] {{
    border: 1px solid {token["accent"]};
    background: {token["accent_soft_2"]};
}}
QFrame#toolbar {{
    background: {token["panel_2"]};
    border: 1px solid {token["line"]};
    border-radius: 12px;
}}
QFrame#statusStrip {{
    background: {token["panel_2"]};
    border: 1px solid {token["line"]};
    border-radius: 12px;
}}
QFrame#wallSurface {{
    background: {token["panel_2"]};
    border: 1px solid {token["line"]};
    border-radius: 16px;
}}
QFrame#logPanel {{
    background: {token["panel_2"]};
    border-top: 1px solid {token["line"]};
}}
QFrame#dropTarget {{
    background: {token["panel_2"]};
    border: 1px dashed {token["line_strong"]};
    border-radius: 16px;
}}
QFrame#dropTarget[dragging="true"] {{
    border: 2px solid {token["accent"]};
    background: {token["accent_soft_2"]};
}}
QFrame#wallCell {{
    background: {token["thumb_from"]};
    border: 1px solid {token["line"]};
    border-radius: 12px;
}}
QFrame#wallCell[focused="true"] {{ border: 2px solid {token["accent"]}; }}

/* ---------------------------------------------------------------- 按钮 */
QPushButton {{
    background: {token["card"]};
    border: 1px solid {token["line_strong"]};
    border-radius: 9px;
    padding: 8px 14px;
    min-height: 20px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {token["hover"]}; border-color: {token["accent_border"]}; }}
QPushButton:pressed {{ background: {token["accent_soft"]}; border-color: {token["accent_border"]}; }}
QPushButton:disabled {{
    color: {token["faint"]};
    background: {token["panel_2"]};
    border-color: {token["line"]};
}}
QPushButton[primary="true"] {{
    background: {token["accent"]};
    border-color: {token["accent"]};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[primary="true"]:hover {{ background: {token["accent_2"]}; border-color: {token["accent_2"]}; }}
QPushButton[primary="true"]:disabled {{
    background: {token["line"]};
    border-color: {token["line"]};
    color: {token["faint"]};
}}
QPushButton[ghost="true"] {{
    background: transparent;
    border: 1px solid {token["line_strong"]};
}}
QPushButton[ghost="true"]:hover {{ background: {token["hover"]}; border-color: {token["accent_border"]}; }}
QPushButton[ghost="true"]:pressed {{ background: {token["accent_soft"]}; border-color: {token["accent_border"]}; }}
QPushButton[nav="true"] {{
    text-align: left;
    padding: 10px 12px;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: {token["muted"]};
    font-weight: 500;
}}
QPushButton[nav="true"]:hover {{
    background: {token["hover"]};
    color: {token["text"]};
}}
QPushButton[nav="true"]:checked {{
    background: {token["accent_soft"]};
    border-color: {token["accent_border_soft"]};
    color: {token["accent"]};
    font-weight: 600;
}}
QPushButton[seg="true"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: {token["muted"]};
    padding: 6px 12px;
    font-size: 12px;
}}
QPushButton[seg="true"]:hover {{ background: {token["hover"]}; color: {token["text"]}; }}
QPushButton[seg="true"]:pressed {{ background: {token["accent_soft"]}; color: {token["text"]}; }}
QPushButton[seg="true"]:disabled {{
    color: {token["faint"]};
    background: transparent;
    border-color: transparent;
}}
QPushButton[seg="true"]:checked {{
    background: {token["accent"]};
    border-color: {token["accent"]};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[link="true"] {{
    background: transparent;
    border: 0;
    color: {token["accent"]};
    padding: 2px 4px;
}}
QPushButton[link="true"]:hover {{ color: {token["accent_2"]}; }}
QPushButton[link="true"]:pressed {{ color: {token["accent"]}; }}
QPushButton[link="true"]:disabled {{ color: {token["faint"]}; }}
QPushButton#logToggle {{
    background: transparent;
    border: 0;
    color: {token["muted"]};
    padding: 6px 16px;
    text-align: left;
    font-size: 11px;
}}

/* ---------------------------------------------------------------- 表单 */
QGroupBox {{
    background: {token["card"]};
    border: 1px solid {token["line"]};
    border-radius: 14px;
    margin-top: 20px;
    padding: 16px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 6px;
    color: {token["muted"]};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.6px;
}}
QGroupBox QLabel {{ color: {token["muted"]}; font-size: 12px; }}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {token["input"]};
    border: 1px solid {token["line_strong"]};
    border-radius: 9px;
    padding: 7px 10px;
    min-height: 20px;
    selection-background-color: {token["accent"]};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {token["accent"]};
    background: {token["bg_2"]};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {token["faint"]};
    background: {token["panel_2"]};
}}
QComboBox::drop-down {{ border: 0; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {token["card"]};
    border: 1px solid {token["line_strong"]};
    border-radius: 8px;
    selection-background-color: {token["accent_soft"]};
    selection-color: {token["text"]};
    padding: 4px;
}}
QCheckBox {{ spacing: 9px; color: {token["muted"]}; }}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid {token["line_strong"]};
    background: {token["input"]};
}}
QCheckBox::indicator:checked {{
    background: {token["accent"]};
    border-color: {token["accent"]};
}}
QLabel#formLabel {{ color: {token["muted"]}; font-size: 12px; }}

/* ---------------------------------------------------------------- 数据 */
QTableWidget {{
    background: {token["card"]};
    alternate-background-color: {token["panel_2"]};
    border: 1px solid {token["line"]};
    border-radius: 12px;
    gridline-color: {token["line"]};
    selection-background-color: {token["accent_soft"]};
    selection-color: {token["text"]};
}}
QHeaderView::section {{
    background: {token["panel_2"]};
    color: {token["muted"]};
    border: 0;
    border-bottom: 1px solid {token["line"]};
    padding: 8px;
    font-size: 11px;
    font-weight: 600;
}}
QTableWidget::item {{ padding: 6px; }}
QListWidget {{
    background: transparent;
    border: 0;
    padding: 2px;
}}
QListWidget::item {{
    background: {token["card"]};
    border: 1px solid {token["line"]};
    border-radius: 10px;
    padding: 10px 12px;
    margin: 3px 0;
    color: {token["text"]};
}}
QListWidget::item:hover {{ background: {token["card_hover"]}; border-color: {token["line_strong"]}; }}
QListWidget::item:selected {{
    background: {token["accent_soft"]};
    border-color: {token["accent_border"]};
    color: {token["text"]};
}}
QProgressBar {{
    background: {token["input"]};
    border: 1px solid {token["line"]};
    border-radius: 6px;
    height: 8px;
    text-align: center;
    font-size: 10px;
    color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {token["accent"]}, stop:1 {token["accent_2"]});
    border-radius: 5px;
}}
QScrollArea {{ border: 0; background: transparent; }}
QWidget#scrollBody {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: {token["line_strong"]};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {token["faint"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

/* ---------------------------------------------------------------- 标签页 */
QTabWidget::pane {{
    border: 1px solid {token["line"]};
    border-radius: 12px;
    background: {token["card"]};
    top: -1px;
}}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {token["muted"]};
    padding: 7px 16px;
    border: 0;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {token["text"]}; border-bottom-color: {token["line_strong"]}; }}
QTabBar::tab:selected {{
    color: {token["accent"]};
    border-bottom-color: {token["accent_border"]};
    font-weight: 600;
}}

/* ---------------------------------------------------------------- 状态栏 */
QStatusBar {{
    background: {token["panel"]};
    color: {token["muted"]};
    border-top: 1px solid {token["line"]};
    font-size: 11px;
}}
QStatusBar::item {{ border: 0; }}
"""


def apply_theme(app: object, light: bool = False) -> None:
    """给 QApplication 应用主题并记录当前色板; 参数保持 object 以免测试环境强依赖 Qt 类型."""
    global _ACTIVE
    _ACTIVE = LIGHT if light else DARK
    setter = getattr(app, "setStyleSheet", None)
    if callable(setter):
        setter(stylesheet(light))
