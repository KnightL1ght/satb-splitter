"""Batch 12: printed multi-bar rest counts from the PDF text layer.

Finale-style PDFs draw the number above a multi-bar rest with the music
font (the same font as the time-signature digits). Each such number is
placed on a page bar with the Audiveris ``.omr`` geometry (system + measure
stack) and written to ``<mxl>.multirest.json``:

    {"source_pdf": ..., "printed": [{"measure_index": 0, "count": 18, "system": 0}]}

The split engine (``split/multirest.py``) only COMPARES these counts with
the MXL's ``<multiple-rest>``; it never invents bars from them. Nothing is
written for a PDF without a text layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from satb_splitter.omr.markers import PdfWord, read_omr_sheet, _mxl_system_starts
from satb_splitter.split.multirest import read_mxl_text, sidecar_path, _PART_RE, _measures

#: music-font digits are drawn with a tall glyph box; the visible digit's
#: bottom sits about this fraction of the font size above the box bottom.
_GLYPH_DROP = 0.74


_CHORD_LEFT_RE = re.compile(
    r"(?:^[A-Ga-g](?:[#b♯♭]|add|maj|min|dim|aug|sus|m)*$|(?:add|maj|min|dim|aug|sus)$)"
)
_CHORD_ACC_TOKENS = frozenset({"#", "b", "♯", "♭", "+", "°", "ø", "/"})


def _is_chord_symbol_digit(bb: tuple, size: float, all_spans: list[tuple]) -> bool:
    """True when a digit span sits adjacent horizontally on the same y-band to a
    chord-symbol root/suffix/accidental (e.g. ``Badd2``, ``F#add4``, ``C#7``)."""
    yc = (bb[1] + bb[3]) / 2.0
    for t2, _f2, s2, b2 in all_spans:
        if b2 == bb:
            continue
        yc2 = (b2[1] + b2[3]) / 2.0
        band = max(size, s2) * 0.9
        v_overlap = min(bb[3], b2[3]) > max(bb[1], b2[1]) - max(size, s2) * 0.35
        if abs(yc2 - yc) > band and not v_overlap:
            continue
        gap_left = bb[0] - b2[2]
        if -3.0 <= gap_left <= max(size, s2) * 1.1:
            if _CHORD_LEFT_RE.search(t2) or t2 in _CHORD_ACC_TOKENS:
                return True
        gap_right = b2[0] - bb[2]
        if -2.0 <= gap_right <= max(size, s2) * 0.6 and t2.startswith(("/", "maj", "sus", "add")):
            return True
    return False


def pdf_multirest_numbers(pdf_path: Path | str) -> tuple[list[PdfWord], float, bool]:
    """(numbers, page_width_pt, has_text_layer). A number = an integer span
    in a large (music) font that is NOT half of a stacked time signature or a
    chord-symbol digit (``Badd2``, ``Badd4``, ``F#add4``, ``C#7`` ...)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover
        return [], 0.0, False
    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count == 0:
            return [], 0.0, False
        page = doc[0]
        w = float(page.rect.width)
        data = page.get_text("rawdict")
    finally:
        doc.close()
    spans = []
    all_spans: list[tuple[str, str | None, float, tuple]] = []
    any_text = False
    for blk in data.get("blocks", []):
        for ln in blk.get("lines", []):
            for sp in ln.get("spans", []):
                chars = sp.get("chars")
                if chars is not None:
                    non_ws = [ch for ch in chars if (ch.get("c") or "").strip()]
                    t = "".join(ch.get("c", "") for ch in non_ws).strip()
                    if non_ws:
                        any_text = True
                else:
                    non_ws = []
                    t = (sp.get("text") or "").strip()
                    if t:
                        any_text = True
                if not t:
                    continue
                sz = float(sp.get("size", 0))
                bb = tuple(sp["bbox"])
                all_spans.append((t, sp.get("font"), sz, bb))
                if re.fullmatch(r"\d{1,3}", t) and sz >= 10:
                    if non_ws:
                        ys = [
                            float(ch["origin"][1])
                            if "origin" in ch
                            else (float(ch["bbox"][1]) + float(ch["bbox"][3])) / 2.0
                            for ch in non_ws
                        ]
                        if max(ys) - min(ys) > 2.5:
                            continue  # vertically stacked time-signature digits merged in one span
                    spans.append((t, sp.get("font"), sz, bb))
    out: list[PdfWord] = []
    for i, (t, font, size, bb) in enumerate(spans):
        xc = (bb[0] + bb[2]) / 2
        paired = any(
            j != i and f2 == font and abs((b2[0] + b2[2]) / 2 - xc) < 3 and 3 < abs(b2[1] - bb[1]) < size * 1.5
            for j, (t2, f2, s2, b2) in enumerate(spans)
        )
        if paired:
            continue  # numerator/denominator of a time signature
        if _is_chord_symbol_digit(bb, size, all_spans):
            continue  # chord symbol suffix digit (Badd2, F#add4, C#7, ...)
        drop = size * _GLYPH_DROP
        out.append(PdfWord(text=t, x0=bb[0], y0=bb[3] - drop - size * 0.7, x1=bb[2], y1=bb[3] - drop))
    return out, w, any_text


def _page_measures(mxls: list[Path]) -> tuple[list[tuple[Path, int, int]], list[int]] | None:
    """[(file, local_index, multirest_count)] for every bar of the page, and
    global indices of system starts."""
    bars: list[tuple[Path, int, int]] = []
    starts: list[int] = []
    for m in mxls:
        text = read_mxl_text(m)
        pm = _PART_RE.search(text or "")
        if not pm:
            return None
        base = len(bars)
        st = _mxl_system_starts(m) or [0]
        starts.extend(base + x for x in st)
        for k, (_n, body) in enumerate(_measures(pm.group(1))):
            hit = re.search(r"<multiple-rest[^>]*>\s*(\d+)", body)
            bars.append((m, k, int(hit.group(1)) if hit else 1))
    return bars, starts


def write_multirest_sidecars(page_pdf: Path | str, mxl_paths, omr_path: Path | str | None = None) -> list[str]:
    mxls = sorted(Path(m) for m in mxl_paths)
    for m in mxls:
        sidecar_path(m).unlink(missing_ok=True)
    if not mxls or not Path(page_pdf).is_file():
        return []
    words, pw, has_text = pdf_multirest_numbers(page_pdf)
    if not has_text:
        return []
    if omr_path is None:
        books = sorted(mxls[0].parent.glob("*.omr"))
        omr_path = books[0] if books else None
    sheet = read_omr_sheet(omr_path) if omr_path else None
    info = _page_measures(mxls)
    per_file: dict[Path, list[dict]] = {m: [] for m in mxls}
    log: list[str] = []
    if words and (sheet is None or info is None):
        log.append(f"{Path(page_pdf).name}: printed multirest counts {[w.text for w in words]} not placed "
                   "(no .omr geometry / unreadable MusicXML)")
        words = []
    systems = [s for s in sheet.systems if s.stacks] if sheet else []
    scale = sheet.width / pw if sheet and pw else 1.0
    seen = set()
    if words and info is not None:
        bars, starts = info
        if len(starts) != len(systems):
            log.append(f"{Path(page_pdf).name}: printed multirest counts not placed (OMR systems "
                       f"{len(starts)} != {len(systems)})")
            words = []
        for w in words:
            yb, xc = w.y1 * scale, (w.x0 + w.x1) / 2 * scale
            il = sheet.interline
            best = None
            for si, s in enumerate(systems):
                if s.top - il <= yb <= s.top + 4 * il or s.top <= yb <= s.bottom:
                    d = abs(s.top - yb)
                    if best is None or d < best[0]:
                        best = (d, si)
            if best is None:
                continue
            si = best[1]
            sts = systems[si].stacks
            stack = next((k for k, (a, b) in enumerate(sts) if a <= xc < b), None)
            if stack is None and sts[-1][1] <= xc <= sts[-1][1] + 4 * il:
                stack = len(sts) - 1  # number centred over a bar at the right edge
            if stack is None:
                continue
            # walk the system's bars: one stack per printed bar, a multirest
            # stack stands for its <multiple-rest> count of MXL bars
            g = starts[si]
            end = starts[si + 1] if si + 1 < len(starts) else len(bars)
            for _ in range(stack):
                if g >= end:
                    break
                g += max(1, bars[g][2])
            beyond = g >= end
            if beyond:
                g = end - 1  # the MXL has no bar under this number: report at the system's last bar
            if g < 0 or (si, stack) in seen:
                continue
            seen.add((si, stack))
            f, local, _c = bars[g]
            item = {"measure_index": local, "count": int(w.text), "system": si}
            if beyond:
                item["beyond"] = True
            per_file[f].append(item)
            log.append(f"{f.name}: printed multirest {w.text} -> bar {local + 1}"
                       + (" (the MXL has no bar under it; reported at the system's last bar)" if beyond else ""))
    for m, items in per_file.items():
        sidecar_path(m).write_text(json.dumps({"source_pdf": str(page_pdf), "printed": items}, indent=1),
                                   encoding="utf-8")
    return log
