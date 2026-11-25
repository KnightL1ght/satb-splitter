"""Recover printed section markers (``unison``, ``S.A``, ``solo tenor`` ...)
from the PDF text layer and attach them to OMR bars (Batch 3, P1).

Audiveris often misses short text above a staff (or it was erased by the top
white-out band). Vector PDFs (Finale / Sibelius / MuseScore exports) still
carry that text, so we read it with PyMuPDF and map each marker word to a
bar using the system/measure geometry stored in the Audiveris ``.omr`` book:

* PDF point coordinates are scaled to the sheet image (``picture`` width).
* A marker belongs to the system whose top staff line lies just below it
  (within a few interlines), or whose staves enclose it vertically.
* The bar is the measure *stack* under the marker's x position.

Result: a sidecar ``<mxl stem>.markers.json`` next to each MusicXML, read by
:func:`satb_splitter.split.engine.split_file`. Markers that cannot be placed
on an OMR system (e.g. the system itself was lost) are reported, never
guessed onto another bar.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SIDECAR_SUFFIX = ".markers.json"
_SYLLABLE_RE = re.compile(r"^[A-Za-zăâîșțşţĂÂÎȘȚŞŢ,.;:!?\"'„“”’\-]+$")


@dataclass
class PdfWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class OmrSystem:
    top: float  # px, first staff line of the top staff
    bottom: float  # px, last staff line of the bottom staff
    stacks: list[tuple[float, float]] = field(default_factory=list)  # (left, right) px


@dataclass
class OmrSheet:
    width: float
    height: float
    interline: float
    systems: list[OmrSystem]


@dataclass
class PlacedMarker:
    text: str
    measure_index: int  # 0-based bar index on the page (across movements), by stack count
    system_index: int
    stack_index: int = 0  # bar inside the system


# ---------------------------------------------------------------------------
# PDF text
# ---------------------------------------------------------------------------

def _join_spans(items: list) -> str:
    """Join spans of one PDF line; touching spans form one word
    (Finale writes "toţi" as "to" + "ţ" + "i" in two fonts).

    Preserves word boundaries when a span carries trailing/leading whitespace
    even if its bounding box touches the next span (e.g. ``"solo "`` + ``"Tenor"``).
    """
    out = ""
    prev_x1 = None
    prev_trail = False
    for it in sorted(items, key=lambda x: x[2][0]):
        t, _f, bb = it[0], it[1], it[2]
        lead = bool(it[3]) if len(it) > 3 else False
        trail = bool(it[4]) if len(it) > 4 else False
        if prev_x1 is not None and (bb[0] - prev_x1 > 1.0 or prev_trail or lead):
            if not out.endswith(" ") and not t.startswith(" "):
                out += " "
        out += t
        prev_x1 = bb[2]
        prev_trail = trail
    return out.strip()


def pdf_marker_candidates(pdf_path: Path | str) -> tuple[list[PdfWord], float, float]:
    """Short text on page 1 that parses as a section marker.

    Lyrics are excluded: a candidate in the dominant lyric font/size that sits
    on a row with other lyric syllables (e.g. Romanian "toţi" = "all" inside
    a lyric line) is ignored. Returns (markers, page_width_pt, page_height_pt);
    empty for scanned PDFs (no text layer) or when PyMuPDF is missing.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover
        return [], 0.0, 0.0
    from satb_splitter.split.engine import parse_section_marker

    doc = fitz.open(str(pdf_path))
    try:
        if doc.page_count == 0:
            return [], 0.0, 0.0
        page = doc[0]
        w, h = float(page.rect.width), float(page.rect.height)
        data = page.get_text("dict")
    finally:
        doc.close()

    spans: list[tuple[str, tuple, tuple, bool, bool]] = []  # text, (font, size), bbox, lead_ws, trail_ws
    line_groups: list[list[int]] = []
    for blk in data.get("blocks", []):
        for ln in blk.get("lines", []):
            idx = []
            for sp in ln.get("spans", []):
                raw = sp.get("text", "")
                t = raw.strip()
                if not t:
                    continue
                idx.append(len(spans))
                spans.append((
                    t,
                    (sp.get("font"), round(float(sp.get("size", 0)), 1)),
                    tuple(sp["bbox"]),
                    raw != raw.lstrip(),
                    raw != raw.rstrip(),
                ))
            if idx:
                line_groups.append(idx)

    # dominant "lyric" text font = most frequent font among letter spans
    freq: dict[tuple, int] = {}
    for t, f, _, _, _ in spans:
        # a lyric syllable: Latin letters (+ Romanian diacritics / punctuation)
        # with a vowel - excludes music-font glyph runs such as "bbb", "jœ"
        if _SYLLABLE_RE.match(t) and re.search(r"[aeiouyAEIOUYăâîĂÂÎ]", t):
            freq[f] = freq.get(f, 0) + 1
    lyric_font = max(freq.items(), key=lambda kv: kv[1])[0] if freq else None

    def lyric_row_count(bbox) -> int:
        yc = (bbox[1] + bbox[3]) / 2
        return sum(
            1 for t, f, bb, _, _ in spans
            if f == lyric_font and abs((bb[1] + bb[3]) / 2 - yc) < 2.5
        )

    # Merge adjacent same-baseline spans when 'solo' is split across separate lines/blocks
    # from a following voice word (e.g. 'solo' + 'Tenor').
    _VOICE_WORD_RE = re.compile(r"^(s|sop|sopr|sopran|soprano|soprana|a|alt|alto|contralto|t|ten|tenor|tenore|b|bas|bass|basso|bar|bariton|baritone)\.?$", re.I)
    merged_groups: list[list[int]] = []
    used_idx: set[int] = set()
    for gi, idx in enumerate(line_groups):
        if any(i in used_idx for i in idx):
            continue
        cur = list(idx)
        cur_txt = _join_spans([spans[i] for i in cur])
        if cur_txt.lower() in ("solo", "soli"):
            bb_cur = (
                min(spans[i][2][0] for i in cur), min(spans[i][2][1] for i in cur),
                max(spans[i][2][2] for i in cur), max(spans[i][2][3] for i in cur),
            )
            yc_cur = (bb_cur[1] + bb_cur[3]) / 2.0
            for gj in range(len(line_groups)):
                if gj == gi or any(i in used_idx for i in line_groups[gj]):
                    continue
                nxt = line_groups[gj]
                nxt_txt = _join_spans([spans[i] for i in nxt])
                if not _VOICE_WORD_RE.match(nxt_txt):
                    continue
                bb_nxt = (
                    min(spans[i][2][0] for i in nxt), min(spans[i][2][1] for i in nxt),
                    max(spans[i][2][2] for i in nxt), max(spans[i][2][3] for i in nxt),
                )
                yc_nxt = (bb_nxt[1] + bb_nxt[3]) / 2.0
                if abs(yc_nxt - yc_cur) <= 4.0 and -2.0 <= bb_nxt[0] - bb_cur[2] <= 18.0:
                    cur = cur + nxt
                    used_idx.update(nxt)
                    break
        used_idx.update(cur)
        merged_groups.append(cur)

    out: list[PdfWord] = []
    seen: set[tuple] = set()
    for idx in merged_groups:
        groups = [idx] + ([[i] for i in idx] if len(idx) > 1 else [])
        for g in groups:
            text = _join_spans([spans[i] for i in g])
            if not text or len(text) > 40:
                continue
            if parse_section_marker(text) is None:
                continue
            bb = (min(spans[i][2][0] for i in g), min(spans[i][2][1] for i in g),
                  max(spans[i][2][2] for i in g), max(spans[i][2][3] for i in g))
            # (a word may mix fonts: "to"+"ţ"+"i", so ANY lyric-font span counts)
            if any(spans[i][1] == lyric_font for i in g) and lyric_row_count(bb) >= 3:
                continue  # a word inside a lyric line
            key = tuple(round(v) for v in bb)
            if any(abs(key[1] - k[1]) < 3 and key[0] < k[2] and k[0] < key[2] for k in seen):
                continue  # already covered by the whole-line candidate
            seen.add(key)
            out.append(PdfWord(text=text, x0=bb[0], y0=bb[1], x1=bb[2], y1=bb[3]))
            break
    return out, w, h


# ---------------------------------------------------------------------------
# Audiveris .omr geometry
# ---------------------------------------------------------------------------

def read_omr_sheet(omr_path: Path | str) -> OmrSheet | None:
    """Systems (staff y-span + measure stacks) of the first sheet in a book."""
    try:
        with zipfile.ZipFile(str(omr_path)) as z:
            names = [n for n in z.namelist() if re.search(r"sheet#\d+/sheet#\d+\.xml$", n)]
            if not names:
                return None
            names.sort(key=lambda n: int(re.search(r"sheet#(\d+)\.xml$", n).group(1)))
            xml = z.read(names[0]).decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    pic = re.search(r'<picture width="(\d+)" height="(\d+)"', xml)
    if not pic:
        return None
    il = re.search(r'<interline[^>]*main="([\d.]+)"', xml)
    interline = float(il.group(1)) if il else 20.0
    systems: list[OmrSystem] = []
    for sm in re.finditer(r"<system id=\"\d+\"[^>]*>(.*?)</system>", xml, re.S):
        body = sm.group(1)
        stacks = [
            (float(a), float(b))
            for a, b in re.findall(r'<stack id="\d+" left="(-?[\d.]+)" right="(-?[\d.]+)"', body)
        ]
        ys: list[float] = []
        for lines in re.findall(r"<lines>(.*?)</lines>", body, re.S):
            ys.extend(float(y) for y in re.findall(r'<point x="[-\d.]+" y="(-?[\d.]+)"', lines))
        if not ys:
            continue
        systems.append(OmrSystem(top=min(ys), bottom=max(ys), stacks=stacks))
    systems.sort(key=lambda s: s.top)
    return OmrSheet(width=float(pic.group(1)), height=float(pic.group(2)),
                    interline=interline, systems=systems)


def place_markers(
    words: list[PdfWord], page_w: float, sheet: OmrSheet, *, max_gap_interlines: float = 6.0
) -> tuple[list[PlacedMarker], list[str]]:
    """Map PDF marker words to (page bar index). Returns (placed, unplaced_texts)."""
    if page_w <= 0 or not sheet.systems:
        return [], [w.text for w in words]
    scale = sheet.width / page_w
    offsets: list[int] = []
    acc = 0
    for s in sheet.systems:
        offsets.append(acc)
        acc += len(s.stacks)
    placed: list[PlacedMarker] = []
    unplaced: list[str] = []
    for w in words:
        y_bot = w.y1 * scale
        y_mid = (w.y0 + w.y1) / 2 * scale
        x_mid = (w.x0 + w.x1) / 2 * scale
        x_left = w.x0 * scale
        best = None
        for si, s in enumerate(sheet.systems):
            gap = s.top - y_bot
            if -sheet.interline <= gap <= max_gap_interlines * sheet.interline:
                score = gap
            elif s.top <= y_mid <= s.bottom:
                score = 0.0
            else:
                continue
            if best is None or score < best[0]:
                best = (score, si)
        if best is None or not sheet.systems[best[1]].stacks:
            unplaced.append(w.text)
            continue
        si = best[1]
        stacks = sheet.systems[si].stacks
        if w.x1 * scale < stacks[0][0] - sheet.interline:
            continue  # staff label in the left margin ("S,A", "T,B"), not a marker
        k = 0
        for j, (left, right) in enumerate(stacks):
            # a marker printed at the start of a bar may begin left of it
            if x_left < right and x_mid >= left - 4 * sheet.interline:
                k = j
                break
        else:
            k = len(stacks) - 1
        placed.append(PlacedMarker(text=w.text, measure_index=offsets[si] + k, system_index=si,
                                   stack_index=k))
    return placed, unplaced


# ---------------------------------------------------------------------------
# Sidecars
# ---------------------------------------------------------------------------

def _mxl_measure_count(mxl: Path) -> int | None:
    try:
        with zipfile.ZipFile(str(mxl)) as z:
            names = [n for n in z.namelist() if n.endswith(".xml") and not n.startswith("META-INF")]
            if not names:
                return None
            xml = z.read(names[0]).decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile):
        try:
            xml = Path(mxl).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    first = re.search(r"<part id=[^>]*>(.*?)</part>", xml, re.S)
    if not first:
        return None
    return len(re.findall(r"<measure[\s>]", first.group(1)))  # not <measure-numbering>


def _mxl_first_part_xml(mxl: Path) -> str | None:
    try:
        with zipfile.ZipFile(str(mxl)) as z:
            names = [n for n in z.namelist() if n.endswith(".xml") and not n.startswith("META-INF")]
            xml = z.read(names[0]).decode("utf-8", errors="replace") if names else None
    except (OSError, zipfile.BadZipFile):
        try:
            xml = Path(mxl).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    if not xml:
        return None
    first = re.search(r"<part id=[^>]*>(.*?)</part>", xml, re.S)
    return first.group(1) if first else None


def _mxl_system_starts(mxl: Path) -> list[int] | None:
    """0-based measure indices where a new system starts (bar 0 included)."""
    body = _mxl_first_part_xml(mxl)
    if body is None:
        return None
    starts: list[int] = []
    for i, m in enumerate(re.finditer(r"<measure[\s>](.*?)</measure>", body, re.S)):
        if i == 0 or re.search(r'<print[^>]*new-(system|page)="yes"', m.group(1)):
            starts.append(i)
    return starts


def sidecar_path(mxl: Path | str) -> Path:
    p = Path(mxl)
    return p.with_name(p.stem + SIDECAR_SUFFIX)


def write_marker_sidecars(
    page_pdf: Path | str, mxl_paths: list[Path], omr_path: Path | str | None = None
) -> list[str]:
    """Write ``<mxl>.markers.json`` for each MusicXML of one OMR page.

    ``page_pdf`` should be the ORIGINAL page (before white-out). Returns log
    lines (what was attached / what could not be placed).
    """
    log: list[str] = []
    mxls = sorted(Path(m) for m in mxl_paths)
    if not mxls:
        return log
    for m in mxls:  # re-runs start clean (no stale sidecar from an older scan)
        try:
            sidecar_path(m).unlink()
        except (FileNotFoundError, OSError):
            pass
    if omr_path is None:
        books = sorted(mxls[0].parent.glob("*.omr"))
        omr_path = books[0] if books else None
    if omr_path is None or not Path(page_pdf).is_file():
        return log
    words, pw, _ = pdf_marker_candidates(page_pdf)
    if not words:
        return log
    sheet = read_omr_sheet(omr_path)
    if sheet is None:
        return [f"{Path(page_pdf).name}: PDF markers {[w.text for w in words]} not attached (no .omr geometry)"]
    placed, unplaced = place_markers(words, pw, sheet)
    counts = [_mxl_measure_count(m) for m in mxls]
    if any(c is None for c in counts):
        return [f"{Path(page_pdf).name}: PDF markers not attached (unreadable MusicXML)"]
    # Preferred mapping: the MusicXML's own system breaks (robust when
    # Audiveris merges/splits a measure stack). Fallback: stack counting,
    # only when the totals agree.
    sys_starts: list[int] = []
    base = 0
    for m, c in zip(mxls, counts):
        st = _mxl_system_starts(m) or [0]
        sys_starts.extend(base + x for x in st)
        base += c  # type: ignore[operator]
    total_bars = base
    systems_with_bars = [i for i, s_ in enumerate(sheet.systems) if s_.stacks]
    if len(sys_starts) == len(systems_with_bars):
        ends = sys_starts[1:] + [total_bars]
        pos = {si: j for j, si in enumerate(systems_with_bars)}
        for pm in placed:
            j = pos.get(pm.system_index)
            if j is not None:
                pm.measure_index = min(sys_starts[j] + pm.stack_index, ends[j] - 1)
    else:
        total_stacks = sum(len(s_.stacks) for s_ in sheet.systems)
        if total_bars != total_stacks:
            return [f"{Path(page_pdf).name}: PDF markers {[w.text for w in words]} not attached "
                    f"(OMR systems {len(sys_starts)} != {len(systems_with_bars)}, bars {counts} != "
                    f"{total_stacks} measure stacks)"]
    per_file: dict[Path, list[dict]] = {m: [] for m in mxls}
    start = 0
    bounds = []
    for m, c in zip(mxls, counts):
        bounds.append((m, start, start + c))  # type: ignore[operator]
        start += c  # type: ignore[operator]
    for pm in placed:
        for m, a, b in bounds:
            if a <= pm.measure_index < b:
                per_file[m].append({"measure_index": pm.measure_index - a, "text": pm.text,
                                    "system": pm.system_index})
                log.append(f"{m.name}: PDF marker '{pm.text}' -> bar {pm.measure_index - a + 1}")
                break
    for t in unplaced:
        log.append(f"{Path(page_pdf).name}: PDF marker '{t}' is above no OMR system "
                   "(system lost by OMR?) - not attached")
    for m, items in per_file.items():
        sc = sidecar_path(m)
        if items:
            sc.write_text(json.dumps({"source_pdf": str(page_pdf), "markers": items}, indent=1),
                          encoding="utf-8")
    return log


def load_sidecar(mxl: Path | str) -> list[dict]:
    sc = sidecar_path(mxl)
    if not sc.is_file():
        return []
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
        return [m for m in data.get("markers", []) if isinstance(m.get("measure_index"), int)]
    except (OSError, ValueError, AttributeError):
        return []


__all__ = [
    "pdf_marker_candidates", "read_omr_sheet", "place_markers",
    "write_marker_sidecars", "load_sidecar", "sidecar_path",
]
