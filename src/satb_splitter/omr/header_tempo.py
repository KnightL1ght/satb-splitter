"""Recover explicit header tempos from ORIGINAL PDF text, never infer BPM.

Separate .tempo.json sidecars do not depend on Audiveris system geometry.
Only text above the first staff is eligible; missing/ambiguous note glyphs
are not interpreted as quarters. Scanned PDFs need a usable text layer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from music21 import expressions, note, stream, tempo

_WORDS = re.compile(
    r"(?i)\b(?:adagio|adagietto|andante|andantino|allegro|allegretto|largo|"
    r"larghetto|lento|moderato|presto|prestissimo|vivace|grave|maestoso|"
    r"sostenuto|cantabile)(?:\s+(?:molto|con\s+moto|non\s+troppo|assai|"
    r"cantabile|maestoso|moderato|espressivo))?\b"
)
_BEATS = {"whole": "whole", "half": "half", "quarter": "quarter",
          "eighth": "eighth", "8th": "eighth", "16th": "16th", "sixteenth": "16th",
          "32nd": "32nd", "thirty-second": "32nd",
          "\u2669": "quarter", "\u266a": "eighth", "\U0001d15e": "half", "\U0001d15d": "whole",
          "\U0001d15f": "quarter", "\U0001d160": "eighth", "\U0001d161": "16th"}
_METRO = re.compile(
    r"(?i)(?<![\w])(?:(dotted|double-dotted)\s+)?("
    + "|".join(re.escape(x) for x in sorted(_BEATS, key=len, reverse=True))
    + r")(?:[- ]note)?\s*(\.{1,2}|[\u00b7\u2022])?\s*=\s*(\d+(?:\.\d+)?)"
)


def parse_header_tempo(text: str) -> list[dict]:
    """Parse explicit words and note=value; word-only tempos have no BPM."""
    text = text.replace("\u00a0", " ")
    words = list(_WORDS.finditer(text))
    marks = []
    for m in _METRO.finditer(text):
        bpm = float(m.group(4))
        if not 0 < bpm <= 1000:
            continue
        prefix, beat, suffix, _ = m.groups()
        dots = (2 if prefix and prefix.lower() == "double-dotted" else 1 if prefix else
                len(suffix) if suffix and suffix.startswith(".") else 1 if suffix else 0)
        # Associate only nearby printed tempo words, not a title/lyric far away.
        nearby = [w for w in words if 0 <= m.start() - w.end() <= 80]
        word = nearby[-1].group(0) if nearby else ""
        marks.append({"text": word, "beat_unit": _BEATS[beat.lower()],
                      "dots": dots, "number": bpm, "raw": m.group(0)})
    used = {m["text"] for m in marks}
    for w in words:
        if w.group(0) not in used:
            marks.append({"text": w.group(0), "beat_unit": None, "dots": 0,
                          "number": None, "raw": w.group(0)})
    return marks


def pdf_header_tempos(pdf_path: Path | str) -> list[dict]:
    """Header of the first page of a per-page PDF, read before white-out."""
    import fitz
    from satb_splitter.omr.preprocess import first_staff_position
    pos = first_staff_position(pdf_path)
    with fitz.open(str(pdf_path)) as doc:
        if not len(doc):
            return []
        page = doc[0]
        limit = page.rect.height * (pos[0] if pos else 0.30)
        # Layout order matters: fonts frequently put a note and '=75' in
        # different PDF lines/blocks. Cluster by vertical centres, then x.
        spans = []
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                for s in ln.get("spans", []):
                    if s["bbox"][3] <= limit and s["text"].strip():
                        spans.append(s)
        rows = []
        for s in sorted(spans, key=lambda s: (sum(s["bbox"][1::2])/2, s["bbox"][0])):
            yc = (s["bbox"][1] + s["bbox"][3]) / 2
            row = next((r for r in rows if abs(r[0]-yc) <= 8), None)
            if row is None:
                rows.append([yc, [s]])
            else:
                row[1].append(s)
        text = "\n".join(" ".join(s["text"] for s in sorted(r[1], key=lambda s: s["bbox"][0]))
                         for r in rows)
    return parse_header_tempo(text)


def text_layer_note(pdf_path: Path | str) -> str:
    """Why a page has no readable header text ('' when it has a text layer).

    Batch 11: 100.pdf ('Microsoft: Print To PDF') has no fonts and no text at
    all - every glyph, including 'Andante' and the note of '=75', is a vector
    outline, so no PDF text extraction can read it. Audiveris' own OCR is
    then the only source (its metronome is kept by musicxml_validation).
    """
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        if not len(doc):
            return "empty PDF"
        page = doc[0]
        if page.get_text("text").strip():
            return ""
        fonts = len(page.get_fonts())
        drawings = len(page.get_drawings())
        images = len(page.get_images())
    return (f"no PDF text layer ({fonts} fonts, 0 text characters, {drawings} vector drawings, "
            f"{images} images): printed tempo glyphs are outlines/pixels, not text - header tempo "
            "cannot be read from the PDF; only Audiveris' OCR of the score can supply it")


def cache_header_tempos(page_pdf: Path | str) -> None:
    """Persist extraction before any white-out, beside the original page PDF."""
    p = Path(page_pdf)
    marks = pdf_header_tempos(p)
    data = {"source_pdf": str(p), "tempos": marks}
    if not marks:
        try:
            note = text_layer_note(p)
        except Exception as exc:  # noqa: BLE001
            note = f"text layer check failed ({exc})"
        data["note"] = note or "text layer present, but no tempo word or 'note = number' above the first staff"
    p.with_suffix(".header-tempo.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def tempo_sidecar_path(mxl: Path | str) -> Path:
    p = Path(mxl)
    return p.with_name(p.stem + ".tempo.json")


def write_tempo_sidecars(page_pdf, mxl_paths) -> list[str]:
    """Attach page-header tempo to the first movement only, independently of .omr."""
    paths = sorted(Path(p) for p in mxl_paths)
    if not paths:
        return []
    for p in paths:
        tempo_sidecar_path(p).unlink(missing_ok=True)
    cache = Path(page_pdf).with_suffix(".header-tempo.json")
    note = ""
    if cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
        marks = data.get("tempos", [])
        note = data.get("note", "")
    else:
        marks = pdf_header_tempos(page_pdf)
    if not marks:
        if note and note.startswith("no PDF text layer"):
            # Batch 12: keep the note next to the MXL so the split REVIEW.txt
            # can report it (no tempo is attached).
            tempo_sidecar_path(paths[0]).write_text(json.dumps(
                {"source_pdf": str(page_pdf), "tempos": [], "note": note}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return [f"{Path(page_pdf).name}: PDF header tempo not readable - {note}"]
        return []
    tempo_sidecar_path(paths[0]).write_text(json.dumps(
        {"source_pdf": str(page_pdf), "tempos": marks}, ensure_ascii=False, indent=2), encoding="utf-8")
    return [f"{paths[0].name}: PDF header tempo {m} -> bar 1" for m in marks]


def tempo_sidecar_note(mxl_path) -> str:
    """Batch 12: the 'no PDF text layer' note stored next to an MXL ('' if none)."""
    p = tempo_sidecar_path(mxl_path)
    if not p.is_file():
        return ""
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("note", "") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def apply_tempo_sidecar(score: stream.Score, mxl_path) -> None:
    p = tempo_sidecar_path(mxl_path)
    if not p.is_file() or not score.parts:
        return
    try:
        marks = json.loads(p.read_text(encoding="utf-8")).get("tempos", [])
    except (OSError, ValueError):
        return
    ms = list(score.parts[0].getElementsByClass(stream.Measure))
    if not ms:
        return
    target = ms[0]
    for m in marks:
        text, beat, number = m.get("text", ""), m.get("beat_unit"), m.get("number")
        if beat in set(_BEATS.values()) and isinstance(number, (float, int)) and 0 < number <= 1000:
            ref = note.Note(type=beat, dots=int(m.get("dots", 0)))
            existing = list(target.recurse().getElementsByClass(tempo.MetronomeMark))
            if not any(x.number == number and x.referent is not None and
                       x.referent.quarterLength == ref.quarterLength for x in existing):
                # No text on MM: word-only metronomes may infer a number, and
                # separate words make both PDF recovery and XML reload explicit.
                target.insert(0, tempo.MetronomeMark(number=number, referent=ref))
        if text and not any(str(x.content) == text for x in
                            target.recurse().getElementsByClass(expressions.TextExpression)):
            target.insert(0, expressions.TextExpression(text))
