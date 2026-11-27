"""Studio Light & Studio Dark themes for the SATB Splitter desktop UI."""

from __future__ import annotations

import struct
import tempfile
import zlib
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget


def _encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode raw RGBA bytes as a minimal PNG without requiring QApplication."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    raw_rows = b"".join(
        b"\x00" + rgba[y * width * 4 : (y + 1) * width * 4] for y in range(height)
    )
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw_rows, 9))
        + chunk(b"IEND", b"")
    )


def _dist_seg(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / denom))
    nx, ny = x1 + t * dx, y1 + t * dy
    return ((px - nx) ** 2 + (py - ny) ** 2) ** 0.5


def _ensure_ui_icons(theme: str, color_hex: str) -> dict[str, str]:
    """Write tiny cached PNG indicators for checkboxes, combos, and spinboxes."""
    out_dir = Path(tempfile.gettempdir()) / "satb_splitter_ui_icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    h = color_hex.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def render_segs(size: int, segs: list[tuple[float, float, float, float]], rgb: tuple[int, int, int], stroke: float) -> bytes:
        buf = bytearray(size * size * 4)
        for y in range(size):
            for x in range(size):
                d = min(_dist_seg(x + 0.5, y + 0.5, *s) for s in segs)
                alpha = max(0.0, min(1.0, stroke + 0.6 - d))
                idx = (y * size + x) * 4
                buf[idx] = rgb[0]
                buf[idx + 1] = rgb[1]
                buf[idx + 2] = rgb[2]
                buf[idx + 3] = int(round(alpha * 255))
        return _encode_rgba_png(size, size, bytes(buf))

    specs = {
        "check": (
            out_dir / "check_white.png",
            14,
            [(3.0, 7.2, 5.8, 10.2), (5.8, 10.2, 11.2, 4.0)],
            (255, 255, 255),
            1.15,
        ),
        "chev_down": (
            out_dir / f"chev_down_{theme}.png",
            12,
            [(2.5, 4.2, 6.0, 7.8), (6.0, 7.8, 9.5, 4.2)],
            (r, g, b),
            1.0,
        ),
        "chev_up": (
            out_dir / f"chev_up_{theme}.png",
            12,
            [(2.5, 7.8, 6.0, 4.2), (6.0, 4.2, 9.5, 7.8)],
            (r, g, b),
            1.0,
        ),
    }
    urls: dict[str, str] = {}
    for key, (path, size, segs, rgb, stroke) in specs.items():
        if not path.is_file():
            path.write_bytes(render_segs(size, segs, rgb, stroke))
        urls[key] = path.as_posix()
    return urls


# Default Light brand constants (kept for backward compatibility)
BG = "#f4f4f6"
SURFACE = "#ffffff"
SURFACE_2 = "#f8f8fa"
SURFACE_3 = "#f0f0f3"
BORDER = "#e4e4e7"
BORDER_STRONG = "#d4d4d8"
TEXT = "#09090b"
TEXT_MUTED = "#71717a"
PRIMARY = "#2563eb"
PRIMARY_HOVER = "#1d4ed8"
PRIMARY_SOFT = "#eff6ff"
SUCCESS = "#059669"
SUCCESS_SOFT = "#ecfdf5"
WARNING = "#b45309"
WARNING_SOFT = "#fffbeb"
DANGER = "#dc2626"
DANGER_SOFT = "#fef2f2"
ACCENT_SA = "#8b5cf6"
ACCENT_TB = "#14b8a6"

THEMES: dict[str, dict[str, str]] = {
    "light": {
        "name": "light",
        "bg": "#f4f4f6",
        "surface": "#ffffff",
        "surface_2": "#f8f8fa",
        "surface_3": "#f0f0f3",
        "border": "#e4e4e7",
        "border_strong": "#d4d4d8",
        "text": "#09090b",
        "text_secondary": "#3f3f46",
        "text_muted": "#71717a",
        "disabled_text": "#a1a1aa",
        "disabled_bg": "#f4f4f5",
        "primary": "#2563eb",
        "primary_hover": "#1d4ed8",
        "primary_soft": "#eff6ff",
        "primary_soft_text": "#1d4ed8",
        "success": "#059669",
        "success_soft": "#ecfdf5",
        "success_border": "#a7f3d0",
        "warning": "#b45309",
        "warning_soft": "#fffbeb",
        "warning_border": "#fde68a",
        "danger": "#dc2626",
        "danger_soft": "#fef2f2",
        "danger_border": "#fecaca",
        "hero_stop0": "#ffffff",
        "hero_stop1": "#ffffff",
        "hero_stop2": "#ffffff",
        "hero_border": "#e4e4e7",
        "row_bg": "#fafafa",
        "row_hover": "#f4f4f5",
        "row_selected": "#eff6ff",
        "drop_bg": "#fafafa",
        "drop_border": "#d4d4d8",
        "log_bg": "#fafafa",
        "log_text": "#18181b",
        "log_border": "#e4e4e7",
        "scrollbar": "#d4d4d8",
        "scrollbar_hover": "#a1a1aa",
    },
    "dark": {
        "name": "dark",
        "bg": "#09090b",
        "surface": "#111114",
        "surface_2": "#18181b",
        "surface_3": "#202024",
        "border": "#27272a",
        "border_strong": "#3f3f46",
        "text": "#fafafa",
        "text_secondary": "#d4d4d8",
        "text_muted": "#a1a1aa",
        "disabled_text": "#52525b",
        "disabled_bg": "#141417",
        "primary": "#3b82f6",
        "primary_hover": "#2563eb",
        "primary_soft": "#172554",
        "primary_soft_text": "#93c5fd",
        "success": "#10b981",
        "success_soft": "#052e16",
        "success_border": "#14532d",
        "warning": "#f59e0b",
        "warning_soft": "#422006",
        "warning_border": "#713f12",
        "danger": "#f87171",
        "danger_soft": "#450a0a",
        "danger_border": "#7f1d1d",
        "hero_stop0": "#111114",
        "hero_stop1": "#111114",
        "hero_stop2": "#111114",
        "hero_border": "#27272a",
        "row_bg": "#18181b",
        "row_hover": "#202026",
        "row_selected": "#172554",
        "drop_bg": "#141417",
        "drop_border": "#3f3f46",
        "log_bg": "#0a0a0c",
        "log_text": "#d4d4d8",
        "log_border": "#27272a",
        "scrollbar": "#3f3f46",
        "scrollbar_hover": "#52525b",
    },
}


def get_theme_colors(theme: str = "light") -> dict[str, str]:
    """Return color token dictionary for ``'light'`` or ``'dark'``."""
    key = "dark" if str(theme).lower().strip() == "dark" else "light"
    return THEMES[key]


def build_stylesheet(theme: str = "light") -> str:
    """Compile the complete Qt stylesheet for the requested theme."""
    c = get_theme_colors(theme)
    icons = _ensure_ui_icons(c["name"], c["text_muted"])
    return f"""
/* ---- Global ---- */
QMainWindow, QDialog {{
    background: {c['bg']};
    color: {c['text']};
}}
QWidget {{
    color: {c['text']};
    font-size: 12.5px;
}}
QLabel {{
    color: {c['text']};
    background: transparent;
}}
QLabel#heroTitle {{
    font-size: 16px;
    font-weight: 700;
    color: {c['text']};
    letter-spacing: -0.2px;
}}
QLabel#heroSub {{
    font-size: 12px;
    color: {c['text_muted']};
}}
QLabel#muted {{
    color: {c['text_muted']};
    font-size: 12px;
}}
QLabel#badgeOk {{
    color: {c['success']};
    font-weight: 600;
    font-size: 12px;
}}
QLabel#badgeBad {{
    color: {c['danger']};
    font-weight: 600;
    font-size: 12px;
}}
QLabel#sectionTitle {{
    font-size: 11px;
    font-weight: 700;
    color: {c['text']};
    text-transform: uppercase;
    letter-spacing: 0.7px;
}}
QLabel#subSectionTitle {{
    font-size: 10px;
    font-weight: 700;
    color: {c['text_muted']};
    text-transform: uppercase;
    letter-spacing: 0.7px;
    padding-top: 2px;
}}
QLabel#countPill {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
    font-weight: 600;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
}}
QLabel#statusPillOk {{
    background: {c['success_soft']};
    color: {c['success']};
    border: 1px solid {c['success_border']};
    font-weight: 600;
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
}}
QLabel#statusPillBad {{
    background: {c['danger_soft']};
    color: {c['danger']};
    border: 1px solid {c['danger_border']};
    font-weight: 600;
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
}}
QLabel#statusPillWarn {{
    background: {c['warning_soft']};
    color: {c['warning']};
    border: 1px solid {c['warning_border']};
    font-weight: 600;
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
}}

/* ---- Cards, Metrics & Hero ---- */
QFrame#card {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QFrame#metricsBar {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QFrame#kpiCard {{
    background: transparent;
    border: none;
    border-radius: 0px;
}}
QFrame#vDivider {{
    background: {c['border']};
    max-width: 1px;
    min-width: 1px;
}}
QFrame#hDivider {{
    background: {c['border']};
    max-height: 1px;
    min-height: 1px;
}}
QLabel#kpiTitle {{
    color: {c['text_muted']};
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.7px;
}}
QLabel#kpiValue {{
    color: {c['text']};
    font-size: 18px;
    font-weight: 700;
}}
QLabel#kpiSub {{
    color: {c['text_muted']};
    font-size: 11px;
}}
QFrame#dropZone {{
    background: {c['drop_bg']};
    border: 1px dashed {c['drop_border']};
    border-radius: 8px;
}}
QFrame#hero {{
    background: {c['surface']};
    border: 1px solid {c['border']};
    border-radius: 8px;
}}
QLabel#heroTitleOnDark {{
    color: {c['text']};
    background: transparent;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: -0.2px;
}}
QLabel#heroSubOnDark {{
    color: {c['text_muted']};
    background: transparent;
    font-size: 12px;
    font-weight: 400;
}}
QLabel#heroPillOk {{
    background: {c['success_soft']};
    color: {c['success']};
    border: 1px solid {c['success_border']};
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#heroPillWarn {{
    background: {c['warning_soft']};
    color: {c['warning']};
    border: 1px solid {c['warning_border']};
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#heroPillInfo {{
    background: {c['surface_2']};
    color: {c['text_secondary']};
    border: 1px solid {c['border']};
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 600;
}}

/* ---- Buttons ---- */
QPushButton {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
    min-height: 18px;
}}
QPushButton:hover {{
    background: {c['surface_3']};
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
    background: {c['primary']};
    color: #ffffff;
    border: 1px solid {c['primary']};
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 12.5px;
    font-weight: 600;
}}
QPushButton#primary:hover {{
    background: {c['primary_hover']};
    border-color: {c['primary_hover']};
}}
QPushButton#primary:disabled {{
    background: {c['disabled_bg']};
    color: {c['disabled_text']};
    border-color: {c['border']};
}}
QPushButton#ghost {{
    background: {c['surface_2']};
    color: {c['text_secondary']};
    border: 1px solid {c['border_strong']};
    border-radius: 6px;
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#ghost:hover {{
    background: {c['surface_3']};
    color: {c['text']};
    border-color: {c['primary']};
}}
QPushButton#danger {{
    background: {c['danger_soft']};
    color: {c['danger']};
    border: 1px solid {c['danger_border']};
    border-radius: 6px;
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#danger:hover {{
    border-color: {c['danger']};
}}
QPushButton#success {{
    background: #059669;
    color: #ffffff;
    border: 1px solid #059669;
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#success:hover {{
    background: #047857;
    border-color: #047857;
}}
QPushButton#success:disabled {{
    background: {c['disabled_bg']};
    color: {c['disabled_text']};
    border: 1px solid {c['border']};
}}
QPushButton#subtle {{
    background: transparent;
    color: {c['text_secondary']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton#subtle:hover {{
    background: {c['surface_2']};
    color: {c['text']};
    border-color: {c['border_strong']};
}}

/* ---- Inputs & Checkboxes ---- */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 6px;
    padding: 4px 9px;
    font-size: 12px;
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
    width: 24px;
}}
QComboBox::down-arrow {{
    image: url({icons['chev_down']});
    width: 11px;
    height: 11px;
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
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background: transparent;
    border: none;
    border-left: 1px solid {c['border']};
    width: 18px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {c['surface_3']};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url({icons['chev_up']});
    width: 9px;
    height: 9px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url({icons['chev_down']});
    width: 9px;
    height: 9px;
}}
QCheckBox {{
    spacing: 7px;
    font-size: 12px;
    color: {c['text']};
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid {c['border_strong']};
    background: {c['surface_2']};
}}
QCheckBox::indicator:hover {{
    border-color: {c['primary']};
}}
QCheckBox::indicator:checked {{
    background: {c['primary']};
    border: 1px solid {c['primary']};
    image: url({icons['check']});
}}

/* ---- Lists / Tables / Text ---- */
QListWidget {{
    background: transparent;
    color: {c['text']};
    border: none;
    padding: 0px;
    outline: none;
}}
QTreeWidget, QTableWidget {{
    background: {c['surface_2']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    background: {c['row_bg']};
    color: {c['text']};
    border: 1px solid {c['border']};
    border-radius: 6px;
    margin: 2px 0px;
    padding: 1px;
    min-height: 46px;
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
    border-radius: 6px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: 12px;
    padding: 10px;
}}
QPlainTextEdit#logConsole {{
    background: {c['log_bg']};
    color: {c['log_text']};
    border: 1px solid {c['log_border']};
    border-radius: 6px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    font-size: 12px;
    padding: 10px;
}}
QHeaderView::section {{
    background: {c['surface_3']};
    color: {c['text_secondary']};
    font-weight: 700;
    font-size: 11.5px;
    padding: 7px 10px;
    border: none;
    border-bottom: 1px solid {c['border_strong']};
    border-right: 1px solid {c['border']};
}}

/* ---- Tabs ---- */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {c['border']};
    background: transparent;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {c['text_muted']};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 14px;
    margin-right: 4px;
    font-weight: 600;
    font-size: 12.5px;
}}
QTabBar::tab:selected {{
    color: {c['text']};
    border-bottom: 2px solid {c['primary']};
}}
QTabBar::tab:hover:!selected {{
    color: {c['text_secondary']};
}}

/* ---- Progress / Status / Menus ---- */
QProgressBar {{
    border: none;
    border-radius: 3px;
    background: {c['border']};
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    border-radius: 3px;
    background: {c['primary']};
}}
QStatusBar {{
    background: {c['surface']};
    color: {c['text_muted']};
    border-top: 1px solid {c['border']};
    font-size: 11.5px;
}}
QMenuBar {{
    background: {c['surface']};
    color: {c['text']};
    border-bottom: 1px solid {c['border']};
    padding: 2px 6px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
}}
QMenu {{
    background: {c['surface']};
    color: {c['text']};
    border: 1px solid {c['border_strong']};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 20px 5px 10px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {c['primary_soft']};
    color: {c['primary_soft_text']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 9px;
    margin: 2px 1px;
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
    pal.setColor(QPalette.ColorRole.Button, QColor(c["surface_2"]))
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
    font.setPointSizeF(9.5)
    app.setFont(font)

    app.setStyleSheet(DARK_STYLESHEET if c["name"] == "dark" else LIGHT_STYLESHEET)
    return c["name"]


def apply_card_shadow(widget: QWidget, theme: str = "light") -> None:
    """Clear elevation shadows so panels rely cleanly on crisp 1px borders."""
    widget.setGraphicsEffect(None)
