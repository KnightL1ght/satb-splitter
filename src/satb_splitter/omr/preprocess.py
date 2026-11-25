"""PDF preprocess: split pages and white-out top title/scripture band."""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Sequence

AUDIVERIS_SAFE_MAX_PIXELS = 18_000_000
OMR_TARGET_DPI = 300


def _require_fitz():
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Per-page OMR requires PyMuPDF. Install with: pip install pymupdf"
        ) from exc
    return fitz


def split_pdf_pages(pdf_path: Path | str, out_dir: Path | str) -> list[Path]:
    """Write one single-page PDF per page. Returns ordered page paths."""
    fitz = _require_fitz()
    src = Path(pdf_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(src)
    paths: list[Path] = []
    try:
        for i in range(doc.page_count):
            one = fitz.open()
            one.insert_pdf(doc, from_page=i, to_page=i)
            path = out / f"page_{i + 1:02d}.pdf"
            one.save(str(path))
            one.close()
            paths.append(path)
    finally:
        doc.close()
    return paths


def first_staff_position(pdf_path: Path | str, *, dpi: int = 150) -> tuple[float, float] | None:
    """Locate the first (top-most) 5-line staff on page 1 of ``pdf_path``.

    Returns ``(top, height)`` as fractions of the page height, or None when
    no staff is found (e.g. a text-only page or a very skewed scan). Works on
    vector and scanned PDFs: the page is rendered and rows that are dark over
    most of the width are grouped into evenly spaced 5-line staves.
    """
    fitz = _require_fitz()
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy ships with music21
        return None
    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count == 0:
            return None
        pix = doc[0].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    finally:
        doc.close()
    if pix.height <= 0 or pix.width <= 0:
        return None
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    dark = (a < 170).mean(axis=1)
    rows = np.where(dark > 0.30)[0]
    lines: list[list[int]] = []
    for r in rows:
        if lines and r - lines[-1][-1] <= 2:
            lines[-1].append(int(r))
        else:
            lines.append([int(r)])
    ys = [sum(l) / len(l) for l in lines]
    max_gap = dpi * 0.12  # ~3 mm: wider gaps are not staff-line spacing
    i = 0
    while i + 4 < len(ys):
        g = ys[i : i + 5]
        gaps = [g[k + 1] - g[k] for k in range(4)]
        if min(gaps) > 2 and max(gaps) < 1.5 * min(gaps) + 2 and max(gaps) < max_gap:
            return g[0] / pix.height, (g[4] - g[0]) / pix.height
        i += 1
    return None


def safe_whiteout_fraction(
    pdf_path: Path | str,
    fraction: float,
    *,
    staff_margin: float = 1.0,
) -> tuple[float, str]:
    """Clamp a white-out band so it never covers the first staff.

    The band stops ``staff_margin`` staff-heights above the first staff, so
    text printed right above it (``unison``, ``S.A``, ``solo``, tempo, bar
    numbers) and the staff label at its left survive. A plain 12% band used
    to erase the whole top system (or its S/A staff) on most continuation
    pages, e.g. Hard p16 bars 54-57 + their ``unison`` marker, and p1's
    ``S,A`` staff at 0.30 (Batch 3).

    Returns (effective_fraction, note); note is "" when nothing was clamped.
    """
    if fraction <= 0:
        return 0.0, ""
    try:
        pos = first_staff_position(pdf_path)
    except Exception:  # noqa: BLE001
        pos = None
    if pos is None:
        return fraction, ""
    top, height = pos
    limit = max(0.0, top - staff_margin * height)
    if fraction <= limit + 1e-9:
        return fraction, ""
    eff = round(limit, 3)
    return eff, (
        f"white-out {fraction:.2f} clamped to {eff:.3f} (first staff starts at {top:.3f} of page height)"
    )


def whiteout_top_band(
    pdf_path: Path | str,
    out_path: Path | str,
    *,
    fraction: float = 0.12,
    protect_staves: bool = True,
    staff_margin: float = 1.0,
) -> Path:
    """White out the top ``fraction`` of the first page (title / scripture).

    Audiveris 5.5 can crash in TEXTS when Romanian scripture lines are
    misclassified as metronome marks (``MetronomeInter`` NPE). Covering the
    top band avoids that crash on large choral scores.

    With ``protect_staves`` (default) the band is clamped above the first
    staff (see :func:`safe_whiteout_fraction`) so no music is erased.
    """
    fitz = _require_fitz()
    src = Path(pdf_path)
    dst = Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if protect_staves and fraction > 0:
        fraction, _ = safe_whiteout_fraction(src, fraction, staff_margin=staff_margin)

    if fraction <= 0.005:
        # no-op copy
        if src.resolve() != dst.resolve():
            dst.write_bytes(src.read_bytes())
        return dst

    fraction = min(fraction, 0.5)
    doc = fitz.open(src)
    try:
        page = doc[0]
        r = page.rect
        band = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * fraction)
        page.draw_rect(band, color=(1, 1, 1), fill=(1, 1, 1))
        doc.save(str(dst))
    finally:
        doc.close()
    return dst


def is_raster_scan_page(page) -> bool:
    """Return True when a PDF page is a pure raster scan (no text or vector drawings)."""
    return (
        len(page.get_text("text").strip()) == 0
        and len(page.get_drawings()) == 0
        and len(page.get_images(full=True)) >= 1
    )


def _extract_raster_page_image(doc, page):
    """Extract the underlying raster scan image or render safely under Audiveris's pixel cap."""
    from PIL import Image

    fitz = _require_fitz()
    imgs = page.get_images(full=True)
    if len(imgs) == 1:
        xref = imgs[0][0]
        rects = page.get_image_rects(xref)
        page_area = abs(page.rect.get_area())
        if rects and page_area > 0.0:
            cov_area = sum(abs(r.get_area()) for r in rects)
            if cov_area >= 0.80 * page_area:
                extracted = doc.extract_image(xref)
                with Image.open(io.BytesIO(extracted["image"])) as im:
                    im.load()
                    img = im.copy()
                rot = int(page.rotation) % 360
                if rot in (90, 180, 270):
                    img = img.rotate(-rot, expand=True)
                return img

    w_pt = max(1.0, float(page.rect.width))
    h_pt = max(1.0, float(page.rect.height))
    short_pt = min(w_pt, h_pt)
    dpi_for_short = 2400.0 * 72.0 / short_pt
    max_dpi_for_px = math.sqrt(float(AUDIVERIS_SAFE_MAX_PIXELS) / (w_pt * h_pt)) * 72.0
    render_dpi = int(round(max(72.0, min(float(OMR_TARGET_DPI), dpi_for_short, max_dpi_for_px))))
    pix = page.get_pixmap(dpi=render_dpi, colorspace=fitz.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def normalize_pdf_page_for_omr(
    page_pdf: Path | str,
    *,
    photo_enhance: str = "auto",
    deskew: bool = True,
    notes: list[str] | None = None,
) -> bool:
    """Normalize a single-page raster-scan or oversized vector PDF for Audiveris."""
    from satb_splitter.omr.image_prep import preprocess_image_for_omr

    fitz = _require_fitz()
    path = Path(page_pdf)
    doc = fitz.open(str(path))
    try:
        if doc.page_count == 0:
            return False
        page = doc[0]
        rect = page.rect
        orig_w_pt = float(rect.width)
        orig_h_pt = float(rect.height)

        if is_raster_scan_page(page):
            img = _extract_raster_page_image(doc, page)
            doc.close()
            doc = None

            proc, img_notes = preprocess_image_for_omr(
                img,
                photo_enhance=photo_enhance,
                deskew=deskew,
            )
            buf = io.BytesIO()
            proc.save(buf, format="PNG")
            pw = proc.width * 72.0 / float(OMR_TARGET_DPI)
            ph = proc.height * 72.0 / float(OMR_TARGET_DPI)

            out_doc = fitz.open()
            try:
                out_page = out_doc.new_page(width=pw, height=ph)
                out_page.insert_image(out_page.rect, stream=buf.getvalue())
                tmp_pdf = path.with_name(f".{path.stem}.norm.pdf")
                out_doc.save(str(tmp_pdf))
            finally:
                out_doc.close()
            tmp_pdf.replace(path)

            if notes is not None:
                for ln in img_notes:
                    notes.append(f"{path.name}: {ln}")
                if abs(orig_w_pt - pw) > 1.0 or abs(orig_h_pt - ph) > 1.0:
                    notes.append(
                        f"{path.name}: Normalized scanned PDF page "
                        f"({orig_w_pt:.0f}x{orig_h_pt:.0f}pt -> {pw:.0f}x{ph:.0f}pt @ {OMR_TARGET_DPI} DPI)"
                    )
            return True

        est_px = (orig_w_pt * float(OMR_TARGET_DPI) / 72.0) * (
            orig_h_pt * float(OMR_TARGET_DPI) / 72.0
        )
        if est_px > float(AUDIVERIS_SAFE_MAX_PIXELS):
            scale = math.sqrt(16_000_000.0 / est_px)
            new_w = orig_w_pt * scale
            new_h = orig_h_pt * scale
            out_doc = fitz.open()
            try:
                out_page = out_doc.new_page(width=new_w, height=new_h)
                out_page.show_pdf_page(out_page.rect, doc, 0)
                tmp_pdf = path.with_name(f".{path.stem}.norm.pdf")
                out_doc.save(str(tmp_pdf))
            finally:
                out_doc.close()
            doc.close()
            doc = None
            tmp_pdf.replace(path)
            if notes is not None:
                notes.append(
                    f"{path.name}: Scaled oversized vector PDF "
                    f"({orig_w_pt:.0f}x{orig_h_pt:.0f}pt -> {new_w:.0f}x{new_h:.0f}pt)"
                )
            return True

        return False
    finally:
        if doc is not None:
            doc.close()


def prepare_pages_for_omr(
    pdf_path: Path | str | Sequence[Path | str],
    work_dir: Path | str,
    *,
    whiteout_fraction: float = 0.12,
    whiteout_mode: str = "all",
    notes: list[str] | None = None,
    photo_enhance: str = "auto",
    deskew: bool = True,
) -> list[Path]:
    """Split PDF (or sheet music images) into pages and optionally white-out top bands.

    Parameters
    ----------
    pdf_path:
        Either a single PDF path, a single image path, or a sequence of image paths.
    whiteout_mode:
        ``all`` — white-out every page (recommended for large Romanian scores).
        ``none`` — no white-out.
        ``first`` — only page 1.
    notes:
        Optional list that receives image preprocessing notes and one line per
        page whose band had to be clamped above the first staff.
    photo_enhance:
        ``auto`` | ``on`` | ``off`` — camera photo shadow-flattening & stretch.
    deskew:
        Automatically estimate and correct small page skew on image inputs.
    """
    from satb_splitter.omr.image_prep import images_to_pdf, is_image_path

    work = Path(work_dir)
    pages_dir = work / "pages"
    clean_dir = work / "pages_clean"

    is_seq = not isinstance(pdf_path, (str, Path))
    from_images = is_seq or is_image_path(pdf_path)
    if from_images:
        img_paths = [Path(p) for p in pdf_path] if is_seq else [Path(pdf_path)]
        pdf_src: Path | str = images_to_pdf(
            img_paths,
            work / "images_input.pdf",
            photo_enhance=photo_enhance,
            deskew=deskew,
            notes=notes,
        )
    else:
        pdf_src = pdf_path

    pages = split_pdf_pages(pdf_src, pages_dir)
    if not from_images:
        # Recover explicit header tempo from original text BEFORE hiding the header.
        # No changes to the band size, staff protection, or retry policy.
        from satb_splitter.omr.header_tempo import cache_header_tempos
        for page in pages:
            try:
                cache_header_tempos(page)
            except Exception as exc:  # best effort, like section-marker recovery
                if notes is not None:
                    notes.append(f"{page.name}: PDF header tempo scan skipped ({exc})")
            normalize_pdf_page_for_omr(
                page,
                photo_enhance=photo_enhance,
                deskew=deskew,
                notes=notes,
            )

    if whiteout_mode == "none" or whiteout_fraction <= 0:
        return pages

    clean_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[Path] = []
    for i, page in enumerate(pages):
        apply = whiteout_mode == "all" or (whiteout_mode == "first" and i == 0)
        if apply:
            out = clean_dir / page.name
            eff, note = safe_whiteout_fraction(page, whiteout_fraction)
            if note and notes is not None:
                notes.append(f"{page.name}: {note}")
            prepared.append(
                whiteout_top_band(page, out, fraction=eff, protect_staves=False)
            )
        else:
            prepared.append(page)
    return prepared
