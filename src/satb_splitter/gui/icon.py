"""Procedural high-DPI application icon and multi-resolution Windows .ico writer."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

# SATB voice accent palette (high-contrast luminous shades for dark emblem background)
VOICE_DOT_COLORS: tuple[tuple[str, str], ...] = (
    ("S", "#c4b5fd"),  # Soprano - luminous violet
    ("A", "#f472b6"),  # Alto    - luminous rose
    ("T", "#2dd4bf"),  # Tenor   - luminous teal
    ("B", "#60a5fa"),  # Bass    - luminous sky blue
)


def _ensure_gui_app() -> None:
    win_fonts = Path(r"C:\Windows\Fonts")
    if win_fonts.is_dir():
        os.environ.setdefault("QT_QPA_FONTDIR", str(win_fonts))
    if QGuiApplication.instance() is None:
        from PySide6.QtWidgets import QApplication

        QApplication([])


def render_icon_pixmap(size: int = 256) -> QPixmap:
    """Paint the SATB Splitter studio icon at ``size`` x ``size`` pixels."""
    _ensure_gui_app()
    s = float(max(16, int(size)))
    img = QImage(int(s), int(s), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    pad = s * 0.045
    rect = QRectF(pad, pad, s - 2 * pad, s - 2 * pad)
    radius = s * 0.23

    # 1. Deep studio indigo -> royal blue -> violet squircle background
    bg_grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    bg_grad.setColorAt(0.0, QColor("#1e1b4b"))
    bg_grad.setColorAt(0.52, QColor("#2563eb"))
    bg_grad.setColorAt(1.0, QColor("#7c3aed"))

    squircle = QPainterPath()
    squircle.addRoundedRect(rect, radius, radius)
    p.fillPath(squircle, bg_grad)

    # 2. Subtle top-left ambient glow
    glow = QRadialGradient(
        QPointF(rect.left() + rect.width() * 0.28, rect.top() + rect.height() * 0.22),
        rect.width() * 0.72,
    )
    glow.setColorAt(0.0, QColor(255, 255, 255, 58))
    glow.setColorAt(0.55, QColor(255, 255, 255, 12))
    glow.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(squircle, glow)

    # 3. Crisp inner border
    border_pen = QPen(QColor(255, 255, 255, 72), max(1.0, s * 0.022))
    p.setPen(border_pen)
    p.drawRoundedRect(rect.adjusted(s * 0.01, s * 0.01, -s * 0.01, -s * 0.01), radius * 0.96, radius * 0.96)

    # 4. Five musical staff lines across the center band
    staff_left = rect.left() + rect.width() * 0.13
    staff_right = rect.right() - rect.width() * 0.13
    staff_top = rect.top() + rect.height() * 0.22
    staff_height = rect.height() * 0.36
    line_gap = staff_height / 4.0

    staff_pen = QPen(QColor(255, 255, 255, 78), max(1.0, s * 0.014))
    staff_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(staff_pen)
    for i in range(5):
        y = staff_top + i * line_gap
        p.drawLine(QPointF(staff_left, y), QPointF(staff_right, y))

    # Left & right barlines on the staff
    bar_pen = QPen(QColor(255, 255, 255, 115), max(1.0, s * 0.018))
    p.setPen(bar_pen)
    p.drawLine(QPointF(staff_left, staff_top), QPointF(staff_left, staff_top + staff_height))
    p.drawLine(QPointF(staff_right, staff_top), QPointF(staff_right, staff_top + staff_height))

    # 5. Four SATB voice notes & stems splitting across the staff
    # Staggered vertically (S highest -> B lowest) and horizontally left-to-right
    note_y_offsets = (0.12, 0.38, 0.64, 0.90)  # relative to staff_height
    col_span = staff_right - staff_left
    note_rx = max(1.8, s * 0.056)
    note_ry = max(1.4, s * 0.042)
    stem_len = staff_height * 0.62

    for idx, ((_letter, hex_col), rel_y) in enumerate(zip(VOICE_DOT_COLORS, note_y_offsets)):
        cx = staff_left + col_span * (0.20 + idx * 0.20)
        cy = staff_top + staff_height * rel_y
        col = QColor(hex_col)

        # Soft halo behind each notehead
        halo = QRadialGradient(QPointF(cx, cy), note_rx * 2.1)
        halo_c = QColor(col)
        halo_c.setAlpha(95)
        halo.setColorAt(0.0, halo_c)
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QPointF(cx, cy), note_rx * 2.0, note_rx * 2.0)

        # Stem (up for S/A, down for T/B)
        stem_up = idx < 2
        stem_x = cx + (note_rx * 0.78 if stem_up else -note_rx * 0.78)
        stem_y2 = cy - stem_len if stem_up else cy + stem_len * 0.75
        stem_pen = QPen(QColor("#ffffff"), max(1.1, s * 0.022))
        stem_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(stem_pen)
        p.drawLine(QPointF(stem_x, cy), QPointF(stem_x, stem_y2))

        # Notehead tilted ellipse
        p.save()
        p.translate(cx, cy)
        p.rotate(-18.0)
        p.setPen(QPen(QColor("#ffffff"), max(0.8, s * 0.012)))
        p.setBrush(col)
        p.drawEllipse(QPointF(0, 0), note_rx, note_ry)
        p.restore()

    # 6. Bottom SATB 4-pill studio badge (rendered clearly at >= 24px)
    if s >= 24:
        pill_top = rect.top() + rect.height() * 0.68
        pill_h = rect.height() * 0.19
        total_w = rect.width() * 0.76
        start_x = rect.left() + (rect.width() - total_w) / 2.0
        gap = s * 0.022
        pill_w = (total_w - 3 * gap) / 4.0
        pill_r = pill_h * 0.34

        font = QFont("Segoe UI", 1)
        font.setPixelSize(max(7, int(round(pill_h * 0.66))))
        font.setWeight(QFont.Weight.Black)
        p.setFont(font)

        for idx, (letter, hex_col) in enumerate(VOICE_DOT_COLORS):
            px = start_x + idx * (pill_w + gap)
            pr = QRectF(px, pill_top, pill_w, pill_h)
            col = QColor(hex_col)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(pr, pill_r, pill_r)

            if s >= 40:
                p.setPen(QColor("#0f172a"))
                p.drawText(pr, Qt.AlignmentFlag.AlignCenter, letter)

    p.end()
    return QPixmap.fromImage(img)


def create_app_icon() -> QIcon:
    """Create a multi-resolution ``QIcon`` for windows, dialogs, and taskbar."""
    _ensure_gui_app()
    icon = QIcon()
    for sz in ICO_SIZES:
        icon.addPixmap(render_icon_pixmap(sz))
    return icon


def _pixmap_to_png_bytes(pix: QPixmap) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    return data


def write_ico_file(path: Path | str, sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    """Write a valid multi-resolution Windows ``.ico`` file with PNG-encoded frames."""
    _ensure_gui_app()
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frames: list[tuple[int, bytes]] = []
    for sz in sizes:
        pix = render_icon_pixmap(sz)
        png = _pixmap_to_png_bytes(pix)
        frames.append((sz, png))

    # ICONDIR header: reserved (2B) = 0, type (2B) = 1 (icon), count (2B) = len(frames)
    header = struct.pack("<HHH", 0, 1, len(frames))
    dir_entries: list[bytes] = []
    payloads: list[bytes] = []
    offset = 6 + 16 * len(frames)

    for sz, png in frames:
        w_byte = 0 if sz >= 256 else sz
        h_byte = 0 if sz >= 256 else sz
        entry = struct.pack(
            "<BBBBHHII",
            w_byte,       # bWidth
            h_byte,       # bHeight
            0,            # bColorCount
            0,            # bReserved
            1,            # wPlanes
            32,           # wBitCount
            len(png),     # dwBytesInRes
            offset,       # dwImageOffset
        )
        dir_entries.append(entry)
        payloads.append(png)
        offset += len(png)

    out_path.write_bytes(header + b"".join(dir_entries) + b"".join(payloads))
    return out_path


if __name__ == "__main__":
    default_ico = Path(__file__).resolve().parents[3] / "assets" / "satb_splitter.ico"
    written = write_ico_file(default_ico)
    print(f"Wrote multi-resolution icon: {written} ({written.stat().st_size} bytes)")
