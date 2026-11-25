"""Copy score directions (tempo, repeats, voltas, segno/coda) onto every voice.

The split engine rebuilds each voice measure-by-measure and previously kept
only clefs, key/time signatures, and metronome marks that happened to sit
on that staff's source bar. Tempo words, repeat barlines, 1st/2nd endings,
and segno/coda marks are collected from every source staff (and the score)
and written onto each exported voice, including rest-padded bars.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

from music21 import bar, expressions, repeat, spanner, stream, tempo


@dataclass
class BarForm:
    left_repeat: Any | None = None
    right_repeat: Any | None = None
    directions: list[tuple[float, Any]] = field(default_factory=list)
    #: Batch 11: light-heavy (final) / light-light (double) right barline.
    right_style: Any | None = None
    #: Batch 11: fermatas found on any staff, by offset inside the bar.
    #: Batch 12: entries are (offset, fermata, staff, source) - see FermataSource.
    fermatas: list[tuple] = field(default_factory=list)
    #: Batch 12: staves (omitted solo/top staves) whose fermatas are never
    #: copied onto other voices (only a voice routed FROM that staff keeps them).
    fermata_skip: frozenset = frozenset()


@dataclass(frozen=True)
class FermataSource:
    """What a fermata sat on in the source staff bar (Batch 12)."""
    is_rest: bool
    pitches: frozenset = frozenset()  # nameWithOctave of the carrying note/chord
    contested: bool = False  # another note of the SAME staff starts here without a fermata


@dataclass
class FormData:
    bars: list[BarForm] = field(default_factory=list)
    voltas: list[tuple[str, int, int, str]] = field(default_factory=list)


_TEMPO_WORD_RE = re.compile(
    r"(?i)\b("
    r"adagio|adagietto|andante|andantino|allegro|allegretto|"
    r"largo|larghetto|lento|lentamente|moderato|presto|prestissimo|"
    r"vivace|vivo|grave|maestoso|sostenuto|risoluto|cantabile|"
    r"a\s+tempo|tempo\s+primo|"
    r"rit\.?|ritard\.?|ritardando|rall\.?|rallentando|"
    r"accel\.?|accelerando|stringendo|rubato|"
    r"pi[uù]\s+mosso|meno\s+mosso|"
    r"d\.?\s*c\.?|d\.?\s*s\.?|dal\s+segno|da\s+capo|fine|coda"
    r")\b"
)
_METRONOME_TEXT_RE = re.compile(r"=\s*\d+")


def collect_score_form(
    parts: Iterable[stream.Part],
    n_bars: int,
    score: stream.Score | None = None,
    fermata_skip_staves: Iterable[int] = (),
) -> FormData:
    """Gather repeats, voltas, tempo, and segno/coda for ``n_bars`` bar slots."""
    parts = list(parts)
    skip = frozenset(fermata_skip_staves)
    bars = [BarForm(fermata_skip=skip) for _ in range(max(0, n_bars))]
    if n_bars <= 0:
        return FormData(bars=bars, voltas=[])

    for pi, part in enumerate(parts):
        measures = list(part.getElementsByClass(stream.Measure))
        for i, m in enumerate(measures[:n_bars]):
            _collect_barlines(bars[i], m)
            _collect_fermatas(bars[i], m, pi)
            _collect_directions(bars[i], m)
        _collect_loose_tempo(bars, part, n_bars)

    if score is not None:
        _collect_loose_tempo(bars, score, n_bars)

    return FormData(bars=bars, voltas=_collect_voltas(parts, n_bars, score))


def apply_bar_form(measure: stream.Measure, form: BarForm, own_staff: int | None = None) -> None:
    """Write collected repeats and directions onto an already-built measure."""
    if form.left_repeat is not None:
        measure.leftBarline = deepcopy(form.left_repeat)
    if form.right_repeat is not None:
        measure.rightBarline = deepcopy(form.right_repeat)
    elif form.right_style is not None and not isinstance(measure.rightBarline, bar.Repeat):
        measure.rightBarline = deepcopy(form.right_style)
    own = getattr(measure, "satbFermataStaves", None)  # Batch 13: routed bar built from 2 staves
    apply_fermatas(measure, form.fermatas, own if own else own_staff, getattr(form, "fermata_skip", ()))

    existing_keys = {
        _direction_key(el)
        for el in measure.recurse()
        if _is_copied_direction(el)
    }
    existing_texts = _existing_texts(measure)
    for offset, el in form.directions:
        if isinstance(el, expressions.TextExpression):
            text = str(el.content or "")
            if text in existing_texts:
                continue
            existing_texts.add(text)
        key = _direction_key(el)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        try:
            measure.insert(float(offset), deepcopy(el))
        except Exception:
            measure.insert(0, deepcopy(el))
        if isinstance(el, tempo.TempoIndication) and getattr(el, "text", None):
            existing_texts.add(str(el.text))


def attach_voltas(
    part: stream.Part,
    measures: list[stream.Measure],
    voltas: list[tuple[str, int, int, str]],
) -> None:
    """Attach 1st/2nd-ending brackets covering ``measures[start:end+1]``."""
    for rb in list(part.getElementsByClass(spanner.RepeatBracket)):
        part.remove(rb)
    n = len(measures)
    for number, start, end, ending_type in voltas:
        if start < 0 or start >= n:
            continue
        end = min(end, n - 1)
        spanned = measures[start : end + 1]
        if not spanned:
            continue
        try:
            rb = spanner.RepeatBracket(spanned, number=number)
            rb.editorial.satb_ending_type = ending_type
            part.insert(0, rb)
        except Exception:
            continue


def transfer_repeat_brackets(
    src_part: stream.Part,
    dest_part: stream.Part,
    old_to_new: dict[int, stream.Measure],
) -> None:
    """Recreate volta brackets after measures have been deep-copied."""
    for rb in src_part.getElementsByClass(spanner.RepeatBracket):
        spanned = [
            old_to_new[id(el)]
            for el in rb.getSpannedElements()
            if id(el) in old_to_new
        ]
        if not spanned:
            continue
        try:
            new_rb = spanner.RepeatBracket(spanned, number=rb.number)
            new_rb.editorial.satb_ending_type = _ending_type(rb)
            dest_part.insert(0, new_rb)
        except Exception:
            continue


def copy_measure_directions(src: stream.Measure, dest: stream.Measure) -> None:
    """Copy repeat barlines and tempo/segno marks from ``src`` onto ``dest``."""
    form = BarForm()
    _collect_barlines(form, src)
    _collect_fermatas(form, src, None)
    _collect_directions(form, src)
    apply_bar_form(dest, form)


def sync_voltas_from_ref(part: stream.Part, ref_part: stream.Part) -> None:
    """Rebuild this part's volta brackets to match ``ref_part`` measure indexes."""
    ref_ms = list(ref_part.getElementsByClass(stream.Measure))
    ms = list(part.getElementsByClass(stream.Measure))
    index_of = {id(m): i for i, m in enumerate(ref_ms)}
    voltas: list[tuple[str, int, int, str]] = []
    seen: set[tuple[str, int, int, str]] = set()
    for rb in ref_part.getElementsByClass(spanner.RepeatBracket):
        idxs = [index_of[id(el)] for el in rb.getSpannedElements() if id(el) in index_of]
        if not idxs:
            continue
        span = (str(rb.number), min(idxs), max(idxs), _ending_type(rb))
        if span not in seen:
            seen.add(span)
            voltas.append(span)
    attach_voltas(part, ms, voltas)


_KEPT_BARLINE_TYPES = ("final", "double")


def _collect_barlines(form: BarForm, measure: stream.Measure) -> None:
    left = measure.leftBarline
    right = measure.rightBarline
    if isinstance(left, bar.Repeat) and form.left_repeat is None:
        form.left_repeat = deepcopy(left)
    if isinstance(right, bar.Repeat) and form.right_repeat is None:
        form.right_repeat = deepcopy(right)
    elif (
        isinstance(right, bar.Barline)
        and not isinstance(right, bar.Repeat)
        and getattr(right, "type", None) in _KEPT_BARLINE_TYPES
    ):
        # final (light-heavy) wins over double (light-light) across staves
        if form.right_style is None or (right.type == "final" and form.right_style.type != "final"):
            form.right_style = deepcopy(right)


def _collect_fermatas(form: BarForm, measure: stream.Measure, staff: int | None = None) -> None:
    """Fermatas on notes AND rests of one staff bar (Audiveris: 100 p4 m20 rest).

    Batch 12: each entry also records what carried it (rest / note pitches)
    and whether another note of the same staff starts at that offset without
    a fermata (two voices on one staff: only the voice holding the marked
    note may receive it - 100 p4 m14 Soprano E5 vs Alto E4).
    """
    els = list(measure.recurse().notesAndRests)
    starts = [(round(_offset_in_measure(el, measure), 4), el) for el in els]
    for off, el in starts:
        ferm = next((ex for ex in getattr(el, "expressions", []) or []
                     if isinstance(ex, expressions.Fermata)), None)
        if ferm is None:
            continue
        if any(abs(f[0] - off) < 1e-3 and f[2] == staff for f in form.fermatas):
            continue
        is_rest = bool(el.isRest)
        pitches = frozenset() if is_rest else frozenset(p.nameWithOctave for p in el.pitches)
        contested = (not is_rest) and any(
            o2 is not None and abs(o2 - off) < 1e-3 and other is not el and not other.isRest
            and not any(isinstance(x, expressions.Fermata) for x in other.expressions)
            and frozenset(p.nameWithOctave for p in other.pitches) != pitches
            for o2, other in starts
        )
        form.fermatas.append((off, deepcopy(ferm), staff, FermataSource(is_rest, pitches, contested)))


def _fermata_source(entry) -> "FermataSource | None":
    return entry[3] if len(entry) > 3 and isinstance(entry[3], FermataSource) else None


def apply_fermatas(
    measure: stream.Measure,
    fermatas: list[tuple],
    own_staff: int | None = None,
    skip_staves: Iterable[int] = (),
) -> int:
    """Attach collected fermatas to this voice (Batch 12 rules).

    * Batch 13: only the voice's own source staff is used; fermatas of other
      staves are never copied (the voice must have a note/rest starting at
      the same onset AND the fermata must be on its own staff in the MXL).
    * A fermata goes only onto an element STARTING at its offset (no
      "sounding through" fallback).
    * A fermata that sat on a note never lands on a rest (Hard 372/390: solo
      staff fermata on choir rests).
    * Own staff, contested offset (two voices start there and only one note
      carries the fermata): only the voice whose pitch matches gets it.
    Elements that already carry a fermata are skipped. Returns the count added.
    """
    added = 0
    if not fermatas:
        return 0
    skip = set(skip_staves)
    if isinstance(own_staff, (tuple, list, set, frozenset)):
        own_set = set(own_staff)
    else:
        own_set = set() if own_staff is None else {own_staff}
    own = [f for f in fermatas if f[2] in own_set and f[2] is not None]
    # Batch 13: a fermata is copied ONLY from the voice's own source staff
    # (Hard 127/134: an SA-staff fermata was copied onto T/B, whose staff has
    # none). A voice built from no staff (own_staff None) only takes
    # staff-less entries (copy_measure_directions).
    if own_staff is None:
        chosen = [f for f in fermatas if f[2] is None]
    else:
        chosen = own
    els = list(measure.recurse().notesAndRests)
    done: list[float] = []
    for entry in sorted(chosen, key=lambda f: f[0]):
        off, ferm = entry[0], entry[1]
        src = _fermata_source(entry)
        target = next((el for el in els if abs(_offset_in_measure(el, measure) - off) < 1e-3), None)
        if target is None:
            continue
        if src is not None:
            if target.isRest and not src.is_rest:
                continue
            if own and src.contested:
                mine = frozenset(p.nameWithOctave for p in target.pitches) if not target.isRest else frozenset()
                if not (mine & src.pitches):
                    continue
        if any(abs(off - d) < 1e-3 for d in done):
            continue
        if any(isinstance(x, expressions.Fermata) for x in target.expressions):
            continue
        done.append(off)
        target.expressions.append(deepcopy(ferm))
        added += 1
    return added


def _collect_directions(form: BarForm, measure: stream.Measure) -> None:
    seen = {_direction_key(el) for _, el in form.directions}
    queued_texts: set[str] = set()
    for _, el in form.directions:
        queued_texts.update(_texts_of(el))

    has_metro = bool(list(measure.getElementsByClass(tempo.MetronomeMark)))
    if not has_metro:
        has_metro = bool(list(measure.recurse().getElementsByClass(tempo.MetronomeMark)))

    def add(el: Any, offset: float) -> None:
        key = _direction_key(el)
        if key in seen:
            return
        if isinstance(el, expressions.TextExpression):
            text = str(el.content or "")
            if text in queued_texts:
                return
            queued_texts.add(text)
        seen.add(key)
        form.directions.append((float(offset), deepcopy(el)))
        queued_texts.update(_texts_of(el))

    for el in measure.recurse().getElementsByClass(tempo.TempoIndication):
        add(el, _offset_in_measure(el, measure))

    for el in measure.recurse().getElementsByClass(expressions.TextExpression):
        text = str(el.content or "")
        if not text or _is_section_marker_text(text):
            continue
        if _looks_like_tempo_text(text) or has_metro:
            add(el, _offset_in_measure(el, measure))

    for el in measure.recurse().getElementsByClass(repeat.RepeatExpression):
        add(el, _offset_in_measure(el, measure))


def _collect_loose_tempo(bars: list[BarForm], container: stream.Stream, n_bars: int) -> None:
    """Tempo marks sitting on a Part or Score (not inside a measure) go on bar 1."""
    if not bars:
        return
    direct = list(container.getElementsByClass(tempo.TempoIndication))
    for el in direct:
        site = el.activeSite
        if isinstance(site, stream.Measure):
            continue
        _collect_one_direction(bars[0], el, 0.0)


def _collect_one_direction(form: BarForm, el: Any, offset: float) -> None:
    key = _direction_key(el)
    if any(_direction_key(existing) == key for _, existing in form.directions):
        return
    form.directions.append((float(offset), deepcopy(el)))


def _collect_voltas(
    parts: list[stream.Part],
    n_bars: int,
    score: stream.Score | None,
) -> list[tuple[str, int, int, str]]:
    spans: list[tuple[str, int, int, str]] = []
    seen: set[tuple[str, int, int, str]] = set()

    def consider(container: stream.Stream, index_of: dict[int, int]) -> None:
        for rb in container.getElementsByClass(spanner.RepeatBracket):
            idxs = [
                index_of[id(el)]
                for el in rb.getSpannedElements()
                if id(el) in index_of and index_of[id(el)] < n_bars
            ]
            if not idxs:
                continue
            span = (str(rb.number), min(idxs), max(idxs), _ending_type(rb))
            if span not in seen:
                seen.add(span)
                spans.append(span)

    for part in parts:
        measures = list(part.getElementsByClass(stream.Measure))
        consider(part, {id(m): i for i, m in enumerate(measures)})
    if score is not None:
        # Score-level brackets (rare): map spanned measures through every part.
        for part in parts:
            measures = list(part.getElementsByClass(stream.Measure))
            consider(score, {id(m): i for i, m in enumerate(measures)})

    return _merge_volta_spans(spans)


def _merge_volta_spans(
    spans: list[tuple[str, int, int, str]],
) -> list[tuple[str, int, int, str]]:
    """Keep distinct endings; merge same-number spans that overlap."""
    merged: list[tuple[str, int, int, str]] = []
    for number, start, end, ending_type in sorted(spans, key=lambda s: (s[1], s[2], s[0])):
        overlapped = False
        for i, (n2, s2, e2, t2) in enumerate(merged):
            if n2 == number and not (end < s2 or start > e2):
                merged[i] = (number, min(start, s2), max(end, e2), ending_type if end >= e2 else t2)
                overlapped = True
                break
        if not overlapped:
            merged.append((number, start, end, ending_type))
    return merged


def _is_copied_direction(el: Any) -> bool:
    return isinstance(el, (tempo.TempoIndication, expressions.TextExpression, repeat.RepeatExpression))


def _direction_key(el: Any) -> tuple:
    if isinstance(el, tempo.MetronomeMark):
        ref = None
        try:
            if el.referent is not None:
                ref = float(el.referent.quarterLength)
        except Exception:
            ref = None
        text = getattr(el, "text", None) or None
        return ("metro", el.number, ref, text)
    if isinstance(el, tempo.TempoText):
        return ("tempotext", str(getattr(el, "text", None) or el))
    if isinstance(el, expressions.TextExpression):
        return ("text", str(el.content or ""))
    return (type(el).__name__, str(el))


def _texts_of(el: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(el, expressions.TextExpression) and el.content:
        out.add(str(el.content))
    text = getattr(el, "text", None)
    if text:
        out.add(str(text))
    return out


def _existing_texts(measure: stream.Measure) -> set[str]:
    out: set[str] = set()
    for el in measure.recurse().getElementsByClass(expressions.TextExpression):
        if el.content:
            out.add(str(el.content))
    for el in measure.recurse().getElementsByClass(tempo.TempoIndication):
        if getattr(el, "text", None):
            out.add(str(el.text))
    return out


def _offset_in_measure(el: Any, measure: stream.Measure) -> float:
    try:
        return float(el.getOffsetInHierarchy(measure))
    except Exception:
        try:
            return float(el.offset)
        except Exception:
            return 0.0


def _looks_like_tempo_text(text: str) -> bool:
    return bool(_TEMPO_WORD_RE.search(text) or _METRONOME_TEXT_RE.search(text))


def _is_section_marker_text(text: str) -> bool:
    from satb_splitter.split.engine import parse_section_marker

    try:
        return parse_section_marker(text) is not None
    except Exception:
        return False


def _ending_type(rb) -> str:
    return getattr(rb.editorial, "satb_ending_type", "stop")


def restore_ending_types(score, path) -> None:
    """music21 discards stop/discontinue on import. Restore only matched ends.

    Do not reconstruct malformed starts, change bracket spans, or guess numbers.
    """
    from pathlib import Path
    import xml.etree.ElementTree as ET
    import zipfile
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = next(el for el in container.iter() if el.tag.endswith("rootfile"))
            root = ET.fromstring(z.read(rootfile.attrib["full-path"]))
    else:
        root = ET.parse(path).getroot()
    for part, xp in zip(score.parts, root.findall("part")):
        ms = list(part.getElementsByClass(stream.Measure))
        raw_ms = xp.findall("measure")
        for rb in part.getElementsByClass(spanner.RepeatBracket):
            ends = [i for i, m in enumerate(ms) if rb.hasSpannedElement(m)]
            if not ends or max(ends) >= len(raw_ms):
                continue
            matches = [e for e in raw_ms[max(ends)].findall("barline/ending")
                       if e.get("type") in ("stop", "discontinue")
                       and e.get("number", "") == str(rb.number)]
            if matches:
                rb.editorial.satb_ending_type = matches[-1].get("type")


def preserve_written_ending_types(score, path) -> None:
    """Repair music21's hardcoded stop export, without rewriting other XML.

    Only the right barline ending matching a known bracket end is changed.
    """
    from pathlib import Path
    p = Path(path)
    xml = p.read_text(encoding="utf-8")
    part_index = -1
    def fix_part(match):
        nonlocal part_index
        part_index += 1
        if part_index >= len(score.parts):
            return match.group(0)
        part = score.parts[part_index]
        ms = list(part.getElementsByClass(stream.Measure))
        ends = set()
        for rb in part.getElementsByClass(spanner.RepeatBracket):
            if _ending_type(rb) != "discontinue":
                continue
            idxs = [i for i, m in enumerate(ms) if rb.hasSpannedElement(m)]
            if idxs:
                ends.add((max(idxs), str(rb.number)))
        i = -1
        def fix_measure(mm):
            nonlocal i
            i += 1
            def fix_bar(bb):
                if 'location="right"' not in bb.group(0):
                    return bb.group(0)
                def fix_ending(ee):
                    text = ee.group(0)
                    num = re.search(r'number="([^"]*)"', text)
                    if num and (i, num.group(1)) in ends:
                        text = text.replace('type="stop"', 'type="discontinue"')
                    return text
                return re.sub(r'<ending\b[^>]*>', fix_ending, bb.group(0))
            return re.sub(r'<barline\b.*?</barline>', fix_bar, mm.group(0), flags=re.S)
        return re.sub(r'<measure\b.*?</measure>', fix_measure, match.group(0), flags=re.S)
    xml = re.sub(r'<part\s[^>]*>.*?</part>', fix_part, xml, flags=re.S)
    p.write_text(xml, encoding="utf-8")
