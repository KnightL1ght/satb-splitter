"""Studio Light & Studio Dark themes for the SATB Splitter desktop UI."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget


# Default Light brand constants (kept for backward compatibility)
BG = "#f1f5f9"
SURFACE = "#ffffff"
SURFACE_2 = "#f8fafc"
SURFACE_3 = "#eef2f7"
BORDER = "#e2e8f0"
BORDER_STRONG = "#cbd5e1"
TEXT = "#0f172a"
TEXT_MUTED = "#64748b"
PRIMARY = "#2563eb"
PRIMARY_HOVER = "#1d4ed8"
PRIMARY_SOFT = "#dbeafe"
SUCCESS = "#15803d"
SUCCESS_SOFT = "#dcfce7"
WARNING = "#b45309"
WARNING_SOFT = "#fef3c7"
DANGER = "#b91c1c"
DANGER_SOFT = "#fee2e2"
ACCENT_SA = "#7c3aed"
ACCENT_TB = "#0d9488"

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "name": "light",
        "bg": "#f1f5f9",
        "surface": "#ffffff",
        "surface_2": "#f8fafc",
        "surface_3": "#f1f5f9",
        "border": "#e2e8f0",
        "border_strong": "#cbd5e1",
        "text": "#0f172a",
        "text_secondary": "#334155",
        "text_muted": "#64748b",
        "disabled_text": "#94a3b8",
        "disabled_bg": "#f1f5f9",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "primary_soft": "#dbeafe",
        "primary_soft_text": "#1e40af",
        "success": "#15803d",
        "success_soft": "#dcfce7",
        "success_border": "#86efac",
        "warning": "#b45309",
        "warning_soft": "#fef3c7",
        "warning_border": "#fcd34d",
        "danger": "#b91c1c",
        "danger_soft": "#fee2e2",
        "danger_border": "#fca5a5",
        "hero_stop0": "#1e1b4b",
        "hero_stop1": "#1d4ed8",
        "hero_stop2": "#6d28d9",
        "hero_border": "#312e81",
        "row_bg": "#f8fafc",
        "row_hover": "#eff6ff",
        "row_selected": "#dbeafe",
        "drop_bg": "#f8fafc",
        "drop_border": "#94a3b8",
        "log_bg": "#0f172a",
        "log_text": "#e2e8f0",
        "log_border": "#1e293b",
        "scrollbar": "#cbd5e1",
        "scrollbar_hover": "#94a3b8",
    },
    "dark": {
        "name": "dark",
        "bg": "#0b1120",
        "surface": "#111827",
        "surface_2": "#1e293b",
        "surface_3": "#0f172a",
        "border": "#1f2937",
        "border_strong": "#334155",
        "text": "#f8fafc",
        "text_secondary": "#cbd5e1",
        "text_muted": "#94a3b8",
        "disabled_text": "#475569",
        "disabled_bg": "#0f172a",
        "primary": "#3b82f6",
        "primary_hover": "#2563eb",
        "primary_soft": "#1e3a8a",
        "primary_soft_text": "#bfdbfe",
        "success": "#22c55e",
        "success_soft": "#052e16",
        "success_border": "#166534",
        "warning": "#f59e0b",
        "warning_soft": "#451a03",
        "warning_border": "#92400e",
        "danger": "#ef4444",
        "danger_soft": "#450a0a",
        "danger_border": "#991b1b",
        "hero_stop0": "#0f172a",
        "hero_stop1": "#1e1b4b",
        "hero_stop2": "#4c1d95",
        "hero_border": "#312e81",
        "row_bg": "#1e293b",
        "row_hover": "#263449",
        "row_selected": "#1e3a8a",
        "drop_bg": "#0f172a",
        "drop_border": "#334155",
        "log_bg": "#090d16",
        "log_text": "#e2e8f0",
        "log_border": "#1e293b",
        "scrollbar": "#334155",
        "scrollbar_hover": "#475569",
    },
}


def get_theme_colors(theme: str = "light") -> dict[str, str]:
    """Return color token dictionary for ``'light'`` or ``'dark'``."""
    key = "dark" if str(theme).lower().strip() == "dark" else "light"
    return THEMES[key]


def build_stylesheet(theme: str = "light") -> str:
    """Compile the complete Qt stylesheet for the requested theme."""
    c = get_theme_colors(theme)
    return f"""
/* ---- Global ---- */
QMainWindow, QDialog {{
    background: {c['bg']};
    color: {c['text']};
}}
QWidget {{
    color: {c['text']};
    font-size: 13px;
}}
QLabel {{
    color: {c['text']};
    background: transparent;
}}
QLabel#heroTitle {{
    font-size: 22px;
    font-weight: 800;
    color: {c['text']};
    letter-spacing: -0.4px;
}}
QLabel#heroSub {{
    font-size: 13px;
    color: {c['text_muted']};
}}
QLabel#muted {{
    color: {c['text_muted']};
    font-size: 12px;
}}
QLabel#badgeOk {{
    color: {c['success']};
    font-weight: 700;
    font-size: 12px;
}}
QLabel#badgeBad {{
    color: {c['danger']};
    font-weight: 700;
    font-size: 12px;
}}
QLabel#sectionTitle {{
    font-size: 12px;
    font-weight: 800;
    color: {c['text']};
    text-transform: uppercase;
    letter-spacing: 0.7px;
}}
QLabel#subSectionTitle {{
    font-size: 10px;
    font-weight: 800;
    color: {c['text_muted']};
    text-transform: uppercase;
    letter-spacing: 0.9px;
    padding-top: 4px;
}}
QLabel#countPill {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
    font-weight: 700;
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 10px;
}}
QLabel#statusPillOk {{
    background: {c['success_soft']};
    color: {c['success']};
    border: 1px solid {c['success_border']};
    font-weight: 700;
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 8px;
}}
QLabel#statusPillBad {{
    background: {c['danger_soft']};
    color: {c['danger']};
    border: 1px solid {c['danger_border']};
    font-weight: 700;
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 8px;
}}
QLabel#statusPillWarn {{
    background: {c['warning_soft']};
    color: {c['warning']};
    border: 1px solid {c['warning_border']};
    font-weight: 700;
    font-size: 11px;
    padding: 5px 10px;
    border-radius: 8px;
}}

/* ---- Cards & Hero ---- */
QFrame#card {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 14px;
}}
QFrame#kpiCard {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 12px;
}}
QLabel#kpiTitle {{
    color: {c['text_muted']};
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
}}
QLabel#kpiValue {{
    color: {c['text']};
    font-size: 20px;
    font-weight: 800;
}}
QLabel#kpiSub {{
    color: {c['text_muted']};
    font-size: 11px;
}}
QFrame#dropZone {{
    background: {c['drop_bg']};
    border: 2px dashed {c['drop_border']};
    border-radius: 12px;
}}
QFrame#hero {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 {c['hero_stop0']}, stop:0.52 {c['hero_stop1']}, stop:1 {c['hero_stop2']});
    border: 1px solid {c['hero_border']};
    border-radius: 16px;
}}
QLabel#heroTitleOnDark {{
    color: #ffffff;
    background: transparent;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.3px;
}}
QLabel#heroSubOnDark {{
    color: rgba(255, 255, 255, 0.88);
    background: transparent;
    font-size: 12px;
    font-weight: 500;
}}
QLabel#heroPillOk {{
    background: rgba(34, 197, 94, 0.22);
    color: #bbf7d0;
    border: 1px solid rgba(134, 239, 172, 0.45);
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 700;
}}
QLabel#heroPillWarn {{
    background: rgba(245, 158, 11, 0.24);
    color: #fde68a;
    border: 1px solid rgba(252, 211, 77, 0.45);
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 700;
}}
QLabel#heroPillInfo {{
    background: rgba(255, 255, 255, 0.14);
    color: #f8fafc;
    border: 1px solid rgba(255, 255, 255, 0.28);
    border-radius: 10px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 700;
}}

/* ---- Buttons ---- */
QPushButton {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 9px;
    padding: 7px 14px;
    font-weight: 600;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {c['surface_2']};
    border-color: {c['primary']};
}}
QPushButton:pressed {{
    background: {c['surface_3']};
}}
QPushButton:disabled {{
    color: {c['disabled_text']};
    background: {c['disabled_bg']};
    border-color: {c['border']};
}}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2563eb, stop:1 #4f46e5);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 10px;
    font-size: 14px;
    font-weight: 700;
    padding: 10px 20px;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1d4ed8, stop:1 #4338ca);
}}
QPushButton#primary:disabled {{
    background: #64748b;
    color: #cbd5e1;
    border-color: transparent;
}}
QPushButton#ghost {{
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid rgba(255, 255, 255, 0.35);
    color: #ffffff;
    border-radius: 9px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 700;
}}
QPushButton#ghost:hover {{
    background: rgba(255, 255, 255, 0.22);
    border-color: rgba(255, 255, 255, 0.65);
}}
QPushButton#danger {{
    background: rgba(239, 68, 68, 0.22);
    border: 1px solid rgba(252, 165, 165, 0.55);
    color: #fecaca;
    border-radius: 9px;
    padding: 6px 12px;
    font-weight: 700;
}}
QPushButton#danger:hover {{
    background: rgba(239, 68, 68, 0.38);
}}
QPushButton#success {{
    background: {c['success']};
    color: #ffffff;
    border: none;
    font-weight: 700;
}}
QPushButton#success:hover {{
    background: #166534;
}}
QPushButton#success:disabled {{
    background: {c['disabled_bg']};
    color: {c['disabled_text']};
    border: 1px solid {c['border']};
}}
QPushButton#subtle {{
    background: {c['surface_2']};
    color: {c['text_secondary']};
    border: 1px solid {c['border']};
    border-radius: 8px;
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#subtle:hover {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
    border-color: {c['primary']};
}}

/* ---- Inputs ---- */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 8px;
    padding: 5px 10px;
    min-height: 18px;
    selection-background-color: {c['primary']};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border: 1.5px solid {c['primary']};
    background: {c['surface']};
}}
QComboBox::drop-down {{
    border: none;
    width: 26px;
}}
QComboBox QAbstractItemView {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    selection-background-color: {c['primary_soft']};
    selection-color: {c['primary_soft_text']};
    outline: none;
    padding: 4px;
}}

/* ---- Lists / Tables / Text ---- */
QListWidget, QTreeWidget, QTableWidget {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    padding: 6px;
    outline: none;
}}
QListWidget::item {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    margin: 3px 2px;
    padding: 2px;
    min-height: 54px;
}}
QListWidget::item:selected {{
    background: {c['row_selected']};
    border: 1.5px solid {c['primary']};
    color: {c['text']};
}}
QListWidget::item:hover:!selected {{
    background: {c['row_hover']};
    border-color: {c['border_strong']};
}}
QListWidget QLabel {{
    background: transparent;
}}
QPlainTextEdit, QTextEdit {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 10px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: 12px;
    padding: 10px;
}}
QPlainTextEdit#logConsole {{
    background: {c['log_bg']};
    color: {c['log_text']};
    border: 1px solid {c['log_border']};
    border-radius: 10px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: 12px;
    padding: 12px;
}}
QHeaderView::section {{
    background: {c['surface_3']};
    color: {c['text_secondary']};
    font-weight: 700;
    font-size: 12px;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid {c['border_strong']};
    border-right: 1px solid {c['border']};
}}

/* ---- Tabs ---- */
QTabWidget::pane {{
    border: 1px solid {c['border']};
    border-radius: 12px;
    background: {c['surface']};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {c['text_muted']};
    padding: 10px 18px;
    margin-right: 4px;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    font-weight: 700;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    color: {c['primary']};
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-bottom: 2px solid {c['primary']};
}}
QTabBar::tab:hover:!selected {{
    color: {c['text']};
    background: {c['surface_2']};
}}

/* ---- Progress / Status / Menus ---- */
QProgressBar {{
    border: none;
    border-radius: 5px;
    background: {c['border']};
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 {c['primary']}, stop:0.5 #6366f1, stop:1 #0ea5e9);
}}
QStatusBar {{
    background: {c['surface']};
    color: {c['text_muted']};
    border-top: 1px solid {c['border']};
    font-size: 12px;
}}
QMenuBar {{
    background: {c['surface']};
    color: {c['text']};
    border-bottom: 1px solid {c['border']};
    padding: 2px 6px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 6px;
}}
QMenuBar::item:selected {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
}}
QMenu {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 22px 6px 12px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}}
QScrollBar::handle:vertical {{
    background: {c['scrollbar']};
    border-radius: 4px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c['scrollbar_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QSplitter::handle {{
    background: transparent;
    width: 10px;
}}
"""


LIGHT_STYLESHEET = build_stylesheet("light")
DARK_STYLESHEET = build_stylesheet("dark")
STYLESHEET = LIGHT_STYLESHEET


def apply_app_theme(app: QApplication, theme: str = "light") -> str:
    """Apply Fusion style, explicit QPalette, typography, and studio stylesheet.

    Returns the normalized theme name (``'light'`` or ``'dark'``).
    """
    c = get_theme_colors(theme)
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(c["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(c["surface"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(c["surface_2"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["text_muted"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(c["surface"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(c["primary"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["surface"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(c["text"]))

    pal.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
        QColor(c["disabled_text"]),
    )
    pal.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(c["disabled_text"]),
    )
    pal.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(c["disabled_text"]),
    )
    app.setPalette(pal)

    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Arial"])
    font.setPointSize(10)
    app.setFont(font)

    app.setStyleSheet(DARK_STYLESHEET if c["name"] == "dark" else LIGHT_STYLESHEET)
    return c["name"]


def apply_card_shadow(widget: QWidget, theme: str = "light") -> None:
    """Attach a soft studio elevation shadow to a card frame."""
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 4)
    if str(theme).lower().strip() == "dark":
        shadow.setColor(QColor(0, 0, 0, 110))
    else:
        shadow.setColor(QColor(15, 23, 42, 22))
    widget.setGraphicsEffect(shadow)
