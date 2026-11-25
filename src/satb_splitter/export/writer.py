"""Write split SATB scores to MusicXML files."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from music21 import duration, layout, note, stream

from satb_splitter.model.config import SplitResult, VoiceRole
from satb_splitter.split.directions import preserve_written_ending_types


VOICE_ORDER: tuple[VoiceRole, ...] = ("Soprano", "Alto", "Tenor", "Bass")


def _has_overfull_bar(score) -> bool:
    return any(getattr(m.editorial, "satb_overfull", False)
               for m in score.recurse().getElementsByClass(stream.Measure))


def _wide_meter_stream(score):
    """A meter stream longer than any bar of ``score`` (see write_musicxml)."""
    from music21 import meter

    longest = max((float(m.duration.quarterLength)
                   for m in score.recurse().getElementsByClass(stream.Measure)), default=4.0)
    ms = stream.Stream()
    ms.insert(0, meter.TimeSignature(f"{int(longest) + 8}/4"))
    return ms


def _no_rebar_exporter(score):
    """music21's GeneralObjectExporter for ONE call, whose makeNotation does not
    re-bar overfull measures (makeTies gets a meter longer than every bar).
    Nothing global is changed: the override lives on this exporter instance."""
    from music21.musicxml.m21ToXml import GeneralObjectExporter

    wide = _wide_meter_stream(score)

    class _NoRebarExporter(GeneralObjectExporter):
        def fromGeneralObject(self, obj):
            # music21 copies the score with makeRests(), which ends with
            # makeTies(Rest) on every part - that would split an overfull
            # bar's trailing rest into the next bar. Same work, measure by
            # measure (no cross-bar re-barring), then the usual fromScore.
            if not (isinstance(obj, stream.Score) and self.makeNotation):
                return super().fromGeneralObject(obj)
            from music21.common.numberTools import opFrac

            sc = obj.coreCopyAsDerivation("makeRests")
            sc.streamStatus.tuplets = None
            for p in sc.iter().parts:
                p.streamStatus.tuplets = None
                for m in p.getElementsByClass(stream.Measure):
                    m.makeRests(inPlace=True, fillGaps=True, hideRests=True, timeRangeFromBarDuration=True)
                at = 0.0
                for m in p.getElementsByClass(stream.Measure):
                    p.setElementOffset(m, at)
                    at = opFrac(at + m.highestTime)
            return self.fromScore(sc)

        def fromScore(self, sc):
            if self.makeNotation:
                sc.makeNotation(inPlace=True, meterStream=wide)
                self.makeNotation = False  # already done: skip the default (re-barring) call
                try:
                    return super().fromScore(sc)
                finally:
                    self.makeNotation = True  # the part exporters still run splitAtDurations/fixups
            return super().fromScore(sc)

    return _NoRebarExporter(score)


def _needs_tuplet_fix(m: ET.Element, div: int) -> bool:
    """True if any voice in ``m`` has unbalanced/exotic or non-beat-aligned tuplets."""
    half_beat = div // 2 if div > 1 and div % 2 == 0 else max(1, div)
    by_voice: dict[str, tuple[int, int]] = {}
    for n in m.findall("note"):
        if n.find("chord") is not None or n.find("grace") is not None:
            continue
        vid = n.findtext("voice") or "1"
        depth, grp_dur = by_voice.get(vid, (0, 0))
        dur = int(n.findtext("duration") or 0)
        tm = n.find("time-modification")
        if tm is not None:
            act = int(tm.findtext("actual-notes") or 1)
            if act > 5 or (n.find("rest") is not None and dur >= 2 * div):
                return True
        tups = [t.attrib.get("type") for t in n.findall("notations/tuplet")]
        starts = tups.count("start")
        stops = tups.count("stop")
        if starts:
            depth += starts
        if depth > 0:
            grp_dur += dur
        if stops:
            depth -= stops
            if depth < 0:
                return True
            if depth == 0:
                if grp_dur % half_beat != 0:
                    return True
                grp_dur = 0
        if tm is not None and not tups and depth == 0:
            return True
        by_voice[vid] = (depth, grp_dur)
    return any(d != 0 for d, _ in by_voice.values())


def _fix_tuplet_measure(m: ET.Element, div: int) -> None:
    """Repair unbalanced OMR tuplets & exotic auto-rest ratios (e.g. 12/7, 48/29)."""
    half_beat = div // 2 if div > 1 and div % 2 == 0 else max(1, div)
    unit64 = max(1, div // 16)
    mapping = [
        (4 * div, "whole", 0),
        (3 * div, "half", 1),
        (2 * div, "half", 0),
        (3 * div // 2, "quarter", 1),
        (div, "quarter", 0),
        (3 * div // 4, "eighth", 1),
        (div // 2, "eighth", 0),
        (3 * div // 8, "16th", 1),
        (div // 4, "16th", 0),
        (div // 8, "32nd", 0),
        (unit64, "64th", 0),
    ]
    mapping = [t for t in mapping if t[0] > 0]

    def set_plain_type(n: ET.Element, dur: int) -> int:
        for c in list(n.findall("dot")):
            n.remove(c)
        tm = n.find("time-modification")
        if tm is not None:
            n.remove(tm)
        best = min(mapping, key=lambda t: abs(t[0] - dur))
        d_el = n.find("duration")
        if d_el is not None:
            d_el.text = str(best[0])
        tp = n.find("type")
        if tp is not None:
            tp.text = best[1]
            idx = list(n).index(tp) + 1
            for _ in range(best[2]):
                n.insert(idx, ET.Element("dot"))
        return best[0]

    for n in m.findall("note"):
        for notat in list(n.findall("notations")):
            for tup in list(notat.findall("tuplet")):
                notat.remove(tup)
            if len(notat) == 0:
                n.remove(notat)

    notes = [n for n in m.findall("note") if n.find("chord") is None and n.find("grace") is None]
    orig_total = sum(int(n.findtext("duration") or 0) for n in notes)
    broken_indices: list[int] = []
    i = 0
    while i < len(notes):
        tm = notes[i].find("time-modification")
        if tm is not None:
            act = int(tm.findtext("actual-notes") or 1)
            norm = int(tm.findtext("normal-notes") or 1)
            dur_i = int(notes[i].findtext("duration") or 0)
            if (act, norm) == (3, 2) and not (notes[i].find("rest") is not None and dur_i >= 2 * div):
                j = i
                s = 0
                last_good = i - 1
                v_i = notes[i].findtext("voice")
                while j < len(notes):
                    tmj = notes[j].find("time-modification")
                    if tmj is None or notes[j].findtext("voice") != v_i:
                        break
                    if (int(tmj.findtext("actual-notes") or 1), int(tmj.findtext("normal-notes") or 1)) != (3, 2):
                        break
                    dj = int(notes[j].findtext("duration") or 0)
                    if notes[j].find("rest") is not None and dj >= 2 * div:
                        break
                    s += dj
                    if s % half_beat == 0:
                        last_good = j
                    j += 1
                if last_good >= i + 1:
                    n0 = notes[i].find("notations")
                    if n0 is None:
                        n0 = ET.SubElement(notes[i], "notations")
                    ET.SubElement(n0, "tuplet", {"type": "start", "number": "1"})
                    n1 = notes[last_good].find("notations")
                    if n1 is None:
                        n1 = ET.SubElement(notes[last_good], "notations")
                    ET.SubElement(n1, "tuplet", {"type": "stop", "number": "1"})
                    for k in range(last_good + 1, j):
                        set_plain_type(notes[k], int(notes[k].findtext("duration") or 0))
                        broken_indices.append(k)
                else:
                    for k in range(i, j):
                        set_plain_type(notes[k], int(notes[k].findtext("duration") or 0))
                        broken_indices.append(k)
                i = j
                continue
        if tm is not None:
            set_plain_type(notes[i], int(notes[i].findtext("duration") or 0))
            broken_indices.append(i)
        i += 1

    new_total = sum(int(n.findtext("duration") or 0) for n in notes)
    diff_d = orig_total - new_total
    if diff_d != 0:
        if notes and notes[-1].find("rest") is not None and notes[-1].find("time-modification") is None:
            d_last = int(notes[-1].findtext("duration") or 0) + diff_d
            if d_last > 0:
                rem = d_last
                rest_parts = []
                for u, typ, dots in sorted(mapping, key=lambda t: -t[0]):
                    while rem >= u:
                        rest_parts.append((u, typ, dots))
                        rem -= u
                if rem == 0 and rest_parts:
                    ins_pos = list(m).index(notes[-1])
                    m.remove(notes[-1])
                    for off_i, (u, typ, dots) in enumerate(rest_parts):
                        rn = ET.Element("note")
                        ET.SubElement(rn, "rest")
                        ET.SubElement(rn, "duration").text = str(u)
                        ET.SubElement(rn, "type").text = typ
                        for _ in range(dots):
                            ET.SubElement(rn, "dot")
                        m.insert(ins_pos + off_i, rn)
                    return
        for bi_idx in reversed(broken_indices):
            cand_d = int(notes[bi_idx].findtext("duration") or 0) + diff_d
            if any(u == cand_d for u, _, _ in mapping):
                set_plain_type(notes[bi_idx], cand_d)
                break


def _sanitize_xml_tuplets(path: Path) -> bool:
    """Repair unbalanced OMR tuplets & exotic auto-rest ratios in written MusicXML."""
    txt = path.read_text(encoding="utf-8")
    m_hdr = re.match(r"^(.*?)(?=<score-partwise\b)", txt, re.S)
    hdr = m_hdr.group(1) if m_hdr else '<?xml version="1.0" encoding="utf-8"?>\n'
    root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", txt, count=1))
    changed = False
    for part in root.findall("part"):
        div = 10080
        for m in part.findall("measure"):
            d_el = m.find("attributes/divisions")
            if d_el is not None and d_el.text:
                div = int(d_el.text)
            if _needs_tuplet_fix(m, div):
                _fix_tuplet_measure(m, div)
                changed = True
    if changed:
        path.write_text(hdr + ET.tostring(root, encoding="unicode"), encoding="utf-8")
    return changed


def write_musicxml(score, path) -> Path:
    """Write ``score`` as MusicXML (Batch 13 item 5 / 5b).

    A bar whose OMR content is longer than its declared meter keeps that meter
    and is marked ``editorial.satb_overfull``. music21's default export
    (makeNotation -> makeTies) would split the last notes of such a bar at the
    meter length and push the remainder into the NEXT bar. For a score with an
    overfull bar this call uses an exporter whose makeTies sees a meter longer
    than every bar (the engine already pads/clips every bar to the grid, so
    makeTies has nothing legitimate to do). The setting is per call: no music21
    module attribute is patched. Scores without an overfull bar go through the
    plain ``score.write`` exactly as before.
    """
    path = Path(path)
    if not _has_overfull_bar(score):
        score.write("musicxml", fp=str(path))
    else:
        data = _no_rebar_exporter(score).parse()
        path.write_bytes(data)
    _sanitize_xml_tuplets(path)
    return path


PADDING_COMMENT = "<!-- satb-splitter: padding rest (OMR bar shorter than its meter; no note invented) -->"


def _padding_spans(score) -> list[list[list[tuple[float, float]]]]:
    rows_by_part = []
    for part in score.parts:
        rows = []
        for m in part.getElementsByClass(stream.Measure):
            spans = []
            for r in m.recurse().getElementsByClass(note.Rest):
                if getattr(r.editorial, "satb_padding", False):
                    a = float(r.getOffsetInHierarchy(m))
                    spans.append((a, a + float(r.quarterLength)))
            rows.append(spans)
        rows_by_part.append(rows)
    return rows_by_part


def mark_padding_rests(score, path) -> int:
    """Batch 13 item 5b: put an XML comment before every written padding rest.

    Padding rests (``editorial.satb_padding``, set by the engine for the end
    padding of a bar shorter than its declared meter) stay ordinary VISIBLE
    rests - no print-object="no". MuseScore ignores XML comments, so the
    comment is only a marker for people/tools reading the MusicXML. Returns
    the number of <note> elements marked.
    """
    import re

    spans = _padding_spans(score)
    if not any(s for rows in spans for s in rows):
        return 0
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    token_re = re.compile(r"<note\b[^>]*>.*?</note>|<backup>.*?</backup>|<forward>.*?</forward>"
                          r"|<divisions>\s*(\d+)\s*</divisions>", re.S)
    dur_re = re.compile(r"<duration>\s*(\d+)\s*</duration>")
    count = 0
    part_index = 0

    def do_part(pm):
        nonlocal part_index, count
        rows = spans[part_index] if part_index < len(spans) else []
        part_index += 1
        state = {"div": 1, "mi": 0}

        def do_measure(mm):
            nonlocal count
            sp = rows[state["mi"]] if state["mi"] < len(rows) else []
            state["mi"] += 1
            body = mm.group(0)
            cur = 0
            last_start = 0
            inserts = []
            for t in token_re.finditer(body):
                tok = t.group(0)
                if t.group(1):
                    state["div"] = int(t.group(1)) or 1
                    continue
                d = dur_re.search(tok)
                dur = int(d.group(1)) if d else 0
                if tok.startswith("<backup"):
                    cur -= dur
                    continue
                if tok.startswith("<forward"):
                    cur += dur
                    continue
                if "<grace" in tok:
                    continue
                if "<chord" in tok:
                    start = last_start
                else:
                    start = cur
                    last_start = cur
                    cur += dur
                if sp and "<rest" in tok and 'print-object="no"' not in tok:
                    q = start / state["div"]
                    if any(a - 1e-6 <= q < b - 1e-6 for a, b in sp):
                        inserts.append(t.start())
            if not inserts:
                return body
            for at in reversed(inserts):
                line_start = body.rfind("\n", 0, at) + 1
                indent = body[line_start:at] if body[line_start:at].strip() == "" else ""
                body = body[:at] + PADDING_COMMENT + "\n" + indent + body[at:]
            count += len(inserts)
            return body

        return pm.group(1) + re.sub(r"<measure\b[^>]*>.*?</measure>", do_measure, pm.group(2), flags=re.S) + pm.group(3)

    text = re.sub(r"(<part\s[^>]*>)(.*?)(</part>)", do_part, text, flags=re.S)
    if count:
        path.write_text(text, encoding="utf-8")
    return count

class MusicXMLPreflightError(ValueError):
    """Actionable failure before writing any unsupported MusicXML durations."""


def preflight_musicxml(score, *, flags=(), role=None):
    """Inspect notated components AND tuplet normal/actual durations.

    A long padding rest can contain an unsupported 2048th tuplet normal
    duration, even though its total quarterLength is nearly a whole bar.
    No quantization, clamping or exception swallowing is performed.
    """
    context = [f"{fl.reason}: {fl.detail}" for fl in flags
               if fl.reason.startswith(("xml_", "omr_export"))]
    for pi, part in enumerate(score.parts):
        label = role or part.partName or part.id or str(pi)
        for measure in part.getElementsByClass(stream.Measure):
            for el in measure.recurse().notesAndRests:
                d = el.duration
                if d.isGrace:
                    continue
                types = [c.type for c in d.components]
                for tuplet in d.tuplets:
                    for td in (tuplet.durationActual, tuplet.durationNormal):
                        if td is not None:
                            types.append(td.type)
                unsupported = [t for t in types if t in duration.typeToDuration
                               and 0 < duration.typeToDuration[t] < 1 / 256]
                if unsupported or (0 < float(d.quarterLength) < 1 / 256):
                    detail = "; ".join(context) or (
                        "No XML completeness context recorded; check source divisions, "
                        "missing measures and OMR rhythm/export diagnostics."
                    )
                    kind = "rest" if el.isRest else "note/chord"
                    raise MusicXMLPreflightError(
                        f"MusicXML preflight blocked: part {label}, measure {measure.number}, "
                        f"offset {el.getOffsetInHierarchy(measure)}: {kind} has unsupported "
                        f"duration below 1024th (QL={d.quarterLength}, types={types}). "
                        f"Underlying completeness/review context: {detail} "
                        "No notes or durations were clamped; repair/re-run the source OMR."
                    )


def write_satb_files(
    result: SplitResult,
    out_dir: Path | str,
    basename: str,
    *,
    skip_empty: bool = False,
    combined: bool = True,
    mscz: bool = True,
    title: str | None = None,
) -> dict[str, Path]:
    """Write one MusicXML file per voice, plus an optional 4-staff SATB score.

    The combined file (``{basename}_SATB.musicxml``) opens in MuseScore as a
    full choir score so all voices can be checked together.

    Batch 12: every file carries the score title (``title``, default
    ``basename`` - the PDF file name stem for --split runs) instead of
    music21's "Music21 Fragment"/"Music21" placeholders; the composer is left
    empty because the splitter does not know it.

    Returns
    -------
    Mapping role (and ``"SATB"``) -> written path.
    """
    title = (title if title is not None else basename) or basename
    for role, score in result.scores.items():
        preflight_musicxml(score, flags=result.flags, role=role)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for role in VOICE_ORDER:
        score = result.scores[role]
        if skip_empty and not _has_notes(score):
            continue
        path = out / f"{basename}_{role}.musicxml"
        write_musicxml(score, path)
        mark_padding_rests(score, path)
        preserve_written_ending_types(score, path)
        set_musicxml_titles(path, title, f"{title} - {role}")
        written[role] = path

    if combined:
        combo_path = out / f"{basename}_SATB.musicxml"
        write_combined_satb(result, combo_path, skip_empty=skip_empty)
        set_musicxml_titles(combo_path, title, title)
        written["SATB"] = combo_path
        if mscz:
            from satb_splitter.export.mscz import export_mscz
            res = export_mscz(combo_path, out / f"{basename}_SATB.mscz")
            result.mscz_path = res.path
            result.mscz_message = res.message

    write_review_report(result, out / f"{basename}_REVIEW.txt", basename)
    return written


_ROLE_NAMES = ("Soprano", "Alto", "Tenor", "Bass")


_PLACEHOLDER_COMPOSERS = ("Music21",)


def set_musicxml_titles(path: Path | str, work_title: str, movement_title: str | None = None) -> None:
    """Batch 12: real title + no placeholder composer in a written MusicXML.

    music21 always writes ``<movement-title>Music21 Fragment</movement-title>``
    and ``<creator type="composer">Music21</creator>`` when the score has no
    metadata; MuseScore imports those as the title/composer texts. The title
    becomes ``work_title`` (``<work><work-title>``) and ``movement_title``;
    the placeholder composer is removed (a real composer is kept).
    """
    import re
    from xml.sax.saxutils import escape

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    movement_title = movement_title or work_title
    for name in _PLACEHOLDER_COMPOSERS:
        text = re.sub(r'\n?[ \t]*<creator type="composer">' + re.escape(name) + r"</creator>", "", text)
    text = re.sub(r"\n?[ \t]*<work>.*?</work>", "", text, flags=re.S)
    text = re.sub(r"\n?[ \t]*<movement-title>.*?</movement-title>", "", text, flags=re.S)
    block = (f"\n  <work>\n    <work-title>{escape(work_title)}</work-title>\n  </work>"
             f"\n  <movement-title>{escape(movement_title)}</movement-title>")
    text, n = re.subn(r"(<score-partwise\b[^>]*>)", lambda m: m.group(1) + block, text, count=1)
    if n:
        path.write_text(text, encoding="utf-8")


def _flag_voice(fl) -> str:
    detail = fl.detail or ""
    body = detail.split("] ", 1)[1] if detail.startswith("[") and "] " in detail else detail
    for role in _ROLE_NAMES:
        if body.startswith(role + ":") or body.startswith(role + " "):
            return role
    return f"staff {fl.part_index}" if fl.part_index is not None and fl.part_index >= 0 else "all"


def _flag_source(fl) -> str:
    page = getattr(fl, "source_page", None)
    num = getattr(fl, "source_measure", None)
    if page and num is not None:
        return f"{page} m{num}"
    return page or "-"


def write_review_report(result: SplitResult, path: Path | str, basename: str = "") -> Path | None:
    """Batch 11: REVIEW.txt with page-level warnings AND one line per flagged bar.

    Bar numbers are the merged bar numbers MuseScore shows. Written whenever
    there is anything to review (or a .mscz message); stale reports are removed.
    """
    path = Path(path)
    flags = [f for f in result.flags if f.reason != "layout_auto"]
    mscz_msg = getattr(result, "mscz_message", "") or ""
    if not flags and not mscz_msg:
        return None
    from collections import Counter
    incomplete = [f for f in flags if f.reason.startswith(("xml_", "omr_export", "omr_page", "page_split"))]
    lines = [f"SATB SPLITTER REVIEW{(' - ' + basename) if basename else ''}", ""]
    if incomplete:
        lines += ["INCOMPLETE OMR EXPORT - REVIEW REQUIRED",
                  "Missing source music is not recovered; omitted bars contain rest placeholders only.", ""]
    if mscz_msg:
        lines += [f"MuseScore: {mscz_msg}", ""]
    counts = Counter(f.reason for f in flags)
    if counts:
        lines.append("Flag counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        lines.append("")
    page = [f for f in flags if f.measure is None]
    if page:
        lines.append("PAGE / SCORE-LEVEL WARNINGS")
        lines.extend(f"measure={f.measure} part_index={f.part_index} {f.reason}: {f.detail}" for f in page)
        lines.append("")
    bars = sorted((f for f in flags if f.measure is not None), key=lambda f: (f.measure, f.reason))
    if bars:
        lines.append("PER-BAR FLAGS (bar = merged bar number as shown in MuseScore; "
                     "source = page file + measure number as written in that page's MXL)")
        lines.append("bar  | source (page, MXL measure) | voice/staff | reason | detail")
        for f in bars:
            lines.append(f"{f.measure:>4} | {_flag_source(f)} | {_flag_voice(f)} | {f.reason} | {f.detail}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_combined_satb(
    result: SplitResult,
    path: Path | str,
    *,
    skip_empty: bool = False,
) -> Path:
    """Write a single MusicXML score with S, A, T, B as simultaneous parts."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    combined = stream.Score()
    if result.scores:
        any_score = next(iter(result.scores.values()))
        if getattr(any_score, "metadata", None) is not None:
            combined.metadata = deepcopy(any_score.metadata)

    parts: list[stream.Part] = []
    for role in VOICE_ORDER:
        src = result.scores.get(role)
        if src is None:
            continue
        if skip_empty and not _has_notes(src):
            continue
        src_parts = list(src.parts)
        if src_parts:
            part = deepcopy(src_parts[0])
        else:
            part = stream.Part()
        part.id = role
        part.partName = role
        part.partAbbreviation = role[0]
        combined.insert(0, part)
        parts.append(part)

    if len(parts) >= 2:
        try:
            group = layout.StaffGroup(
                parts,
                name="Choir",
                abbreviation="SATB",
                symbol="bracket",
            )
            combined.insert(0, group)
        except Exception:
            pass

    preflight_musicxml(combined, flags=result.flags)
    write_musicxml(combined, dest)
    mark_padding_rests(combined, dest)
    preserve_written_ending_types(combined, dest)
    return dest


def _has_notes(score: stream.Score) -> bool:
    return any(True for _ in score.flatten().notes)
