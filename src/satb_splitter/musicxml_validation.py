"""Validate OMR context before music21 can silently apply default divisions.

Repairs only verified sibling divisions; never reconstructs omitted music.
Original exports are never modified.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import math
import xml.etree.ElementTree as ET
import zipfile

from music21 import converter, stream
from satb_splitter.model.config import ReviewFlag


@dataclass
class XmlValidation:
    root: ET.Element
    flags: list[ReviewFlag] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    changed: bool = False


def read_xml(path):
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = next(e for e in container.iter() if e.tag.endswith("rootfile"))
            return ET.fromstring(z.read(rootfile.attrib["full-path"]))
    return ET.parse(path).getroot()


_CONT_ID = re.compile(r"(?:\d+)?X(\d+)")  # 'X25' (XML) or '25X25' (music21 number+suffix)


def _identity_grid(rows):
    """Use exact identities, filling integer gaps only in ordinary numeric runs.

    Page-local numbering may start above 1. Never infer a leading 1 without
    sibling evidence; preserve pickup 0. Reject duplicate/reordered identities
    rather than silently falling back to index alignment.
    """
    if any(len(row) != len(set(row)) or any(not x for x in row) for row in rows):
        raise ValueError("duplicate or missing measure identity; cannot align staves safely")
    ids = set(x for row in rows for x in row)
    if not ids:
        return []
    conts = {x: int(c.group(1)) for x in ids for c in [_CONT_ID.fullmatch(x)] if c}
    if conts and all(x in conts or re.fullmatch(r"\d+", x) for x in ids):
        # Batch 13: Audiveris dev3b implicit continuation 'X<n>' (second half of a
        # bar split by a system break) sits right after measure <n>; it is not a
        # gap or a duplicate. Plain numbers keep the integer-gap grid.
        nums = sorted(int(x) for x in ids if x not in conts)
        lo = min(nums + list(conts.values()))
        hi = max(nums + list(conts.values()))
        if hi - lo > 10000:
            raise ValueError("implausible measure-number gap; cannot align safely")
        grid = []
        for k in range(lo, hi + 1):
            if str(k) in ids or (nums and nums[0] <= k <= nums[-1]):
                grid.append(str(k))
            grid.extend(sorted(x for x, n in conts.items() if n == k))
    elif all(re.fullmatch(r"\d+", x) for x in ids):
        nums = sorted(int(x) for x in ids)
        if nums[-1] - nums[0] > 10000:
            raise ValueError("implausible measure-number gap; cannot align safely")
        grid = [str(x) for x in range(nums[0], nums[-1] + 1)]
    else:
        # Prefer a complete staff's exact sequence (including suffixes/pickups).
        grid = max(rows, key=len)
        if set(grid) != ids:
            raise ValueError("inconsistent nonnumeric measure identities; cannot align safely")
    positions = {x: i for i, x in enumerate(grid)}
    if any([positions[x] for x in row] != sorted(positions[x] for x in row) for row in rows):
        raise ValueError("reordered measure identities; cannot align staves safely")
    return grid


def _renumber_agreeing_duplicates(parts, rows, report) -> bool:
    """Batch 11: accept reused measure numbers when every part agrees.

    Audiveris 5.12 writes an expanded multi-measure rest as an extra
    ``<measure>`` that reuses the next bar's number (Hard p5 m1, p6 m9,
    p7 m18, p15 m1 x2, p18 m16). When every part has exactly the same number
    sequence (same count, same order) the bars are aligned by POSITION and
    renumbered sequentially; each reused number is flagged for review. When
    the parts disagree the Batch 10 hard failure is kept (no guessing).
    """
    if not rows or not any(len(r) != len(set(r)) for r in rows):
        return False
    if any(r != rows[0] for r in rows) or any(not x for x in rows[0]):
        report.fatal.append(
            "duplicate measure numbers and the parts disagree on bar order/count; cannot align staves safely")
        return False
    first = rows[0][0]
    start = int(first) if re.fullmatch(r"\d+", first) else 1
    seen: set[str] = set()
    dups = []
    for k, identity in enumerate(rows[0]):
        if identity in seen:
            dups.append((k, identity))
        seen.add(identity)
    for part in parts:
        for k, m in enumerate(part.findall("measure")):
            m.set("number", str(start + k))
    report.changed = True
    end = start + len(rows[0]) - 1
    for k, identity in dups:
        report.flags.append(ReviewFlag(k + 1, -1, None, "xml_duplicate_measure",
            f"XML measure number {identity} reused at bar position {k + 1} (e.g. expanded "
            f"multi-measure rest); all {len(parts)} part(s) agree, so bars were aligned by "
            f"position and renumbered {start}..{end}. Check this bar against the PDF."))
    return True


_PER_MINUTE_RE = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*(.*)", re.S)


def _repair_metronome_numbers(parts, report) -> None:
    """Batch 11: keep a metronome whose ``<per-minute>`` carries OMR junk.

    Audiveris 5.11/5.12 glued the neighbouring 'S.a-2-a oara' text onto the
    100.pdf mark: ``<per-minute>75 S.a—2—a card</per-minute>`` with
    ``<sound tempo="75">``. music21 then imports a MetronomeMark without a
    number and the mark is lost on export. The printed number is kept only
    when it is the leading number of ``per-minute`` AND agrees with the
    direction's ``<sound tempo>`` (if present). Nothing is inferred.
    """
    for pi, part in enumerate(parts):
        for k, m in enumerate(part.findall("measure")):
            for direction in m.findall("direction"):
                sound = direction.find("sound")
                for metro in direction.iter("metronome"):
                    pm = metro.find("per-minute")
                    if pm is None or pm.text is None:
                        continue
                    text = pm.text.strip()
                    if re.fullmatch(r"\d+(?:\.\d+)?", text):
                        continue
                    hit = _PER_MINUTE_RE.fullmatch(text)
                    label = part.get("id", str(pi))
                    if not hit:
                        report.flags.append(ReviewFlag(k + 1, pi, None, "metronome_unreadable",
                            f"Part {label}: metronome per-minute '{text}' has no readable number; "
                            "kept as OMR exported (no BPM inferred)."))
                        continue
                    number = hit.group(1).replace(",", ".")
                    tempo_attr = sound.get("tempo") if sound is not None else None
                    beat = metro.findtext("beat-unit", "")
                    dotted = metro.find("beat-unit-dot") is not None
                    if tempo_attr is not None and beat == "quarter" and not dotted:
                        try:
                            agrees = abs(float(tempo_attr) - float(number)) < 0.01
                        except ValueError:
                            agrees = False
                        if not agrees:
                            report.flags.append(ReviewFlag(k + 1, pi, None, "metronome_unreadable",
                                f"Part {label}: metronome per-minute '{text}' disagrees with "
                                f"sound tempo {tempo_attr}; kept as OMR exported."))
                            continue
                    pm.text = number
                    report.changed = True
                    report.flags.append(ReviewFlag(k + 1, pi, None, "metronome_repaired",
                        f"Part {label}: metronome {beat}{'.' if dotted else ''}={number} kept; OMR text "
                        f"'{hit.group(2).strip()}' glued to the number was removed from per-minute."))


def validate_musicxml(path, *, repair_divisions=False):
    root = read_xml(path)
    report = XmlValidation(root)
    if root.tag != "score-partwise":
        # Other music21 formats remain supported by split_file's old loader.
        return report
    parts = root.findall("part")
    if not parts:
        report.fatal.append("MusicXML has no parts")
        return report
    declared = {p.get("id") for p in root.findall("part-list/score-part")}
    actual = [p.get("id") for p in parts]
    if len(set(actual)) != len(actual) or any(not x for x in actual):
        report.fatal.append("duplicate or absent part identity")
    if declared - set(actual):
        report.fatal.append("declared parts absent from export: " + ", ".join(sorted(declared - set(actual))))
    rows = [[m.get("number", "") for m in p.findall("measure")] for p in parts]
    if _renumber_agreeing_duplicates(parts, rows, report):
        rows = [[m.get("number", "") for m in p.findall("measure")] for p in parts]
    _repair_metronome_numbers(parts, report)
    try:
        grid = _identity_grid(rows)
    except ValueError as exc:
        report.fatal.append(str(exc))
        grid = []
    for i, (part, row) in enumerate(zip(parts, rows)):
        label = part.get("id", str(i))
        if not row:
            report.fatal.append(f"part {label} has no measures")
        for k, identity in enumerate(grid):
            if identity not in row:
                report.flags.append(ReviewFlag(k + 1, i, None, "xml_missing_measure",
                    f"Part {label}: XML measure {identity} missing; no notes recovered. "
                    "Empty/rest placeholder only; later bars aligned by measure identity."))
        declarations = part.findall(".//divisions")
        try:
            if any(not math.isfinite(float(d.text or 0)) or float(d.text or 0) <= 0 for d in declarations):
                raise ValueError()
        except ValueError:
            report.fatal.append(f"part {label} has invalid divisions")
        # A declaration after notes cannot establish context for earlier notes.
        has_context = False
        missing = not declarations
        for m in part.findall("measure"):
            for el in m:
                if el.tag == "attributes" and el.find("divisions") is not None:
                    has_context = True
                if el.tag in ("note", "backup", "forward") and el.find("duration") is not None and not has_context:
                    missing = True
        if missing:
            sibling = {d.text.strip() for j, p in enumerate(parts) if j != i
                       for d in p.findall(".//divisions") if d.text}
            verified = None
            if len(sibling) == 1:
                candidate = next(iter(sibling))
                try:
                    if math.isfinite(float(candidate)) and float(candidate) > 0:
                        verified = candidate
                except ValueError:
                    pass
            detail = f"Part {label}: missing divisions context before timed music. "
            if verified and row:
                detail += f"Verified consistent sibling divisions={verified} applied before import; review original OMR."
                if repair_divisions:
                    m = part.find("measure")
                    attrs = m.find("attributes")
                    if attrs is None:
                        attrs = ET.Element("attributes")
                        m.insert(0, attrs)
                    divisions = ET.Element("divisions")
                    divisions.text = verified
                    attrs.insert(0, divisions)
                    report.changed = True
            else:
                detail += "No unambiguous sibling divisions; import blocked (10080 default is unsafe)."
                report.fatal.append(detail)
            report.flags.append(ReviewFlag(None, i, None, "xml_missing_divisions", detail))
    return report


def export_log_issues(log):
    """Deduplicated diagnostic families with an original log example each."""
    patterns = {
        "export exception": r"NullPointerException|\bNPE\b|(?:exception.*export|export.*exception)|error\s+exporting|could not export",
        "missing timeOffset": r"no\s+timeOffset",
        "no correct rhythm": r"no\s+correct\s+rhythm",
        "abnormal measure": r"abnormal.*measure|measure.*abnormal",
    }
    issues = []
    for label, pattern in patterns.items():
        line = next((ln.strip() for ln in log.splitlines() if re.search(pattern, ln, re.I)), None)
        if line:
            issues.append(f"{label}: {line}")
    return issues


def nearby_log_issues(path):
    """Only the latest matching page log, never all historical logs in a folder."""
    path = Path(path)
    candidates = list(path.parent.glob(path.stem + "*.log"))
    if not candidates:
        return []
    latest = max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))
    return [f"{latest.name}: {x}" for x in export_log_issues(latest.read_text(encoding="utf-8", errors="replace"))]


def load_validated_score(path):
    path = Path(path)
    if path.suffix.lower() not in (".mxl", ".xml", ".musicxml"):
        return converter.parse(str(path)), []
    report = validate_musicxml(path, repair_divisions=True)
    if report.fatal:
        raise ValueError("Incomplete MusicXML: " + "; ".join(report.fatal))
    if report.changed:
        score = converter.parseData(ET.tostring(report.root, encoding="unicode"), format="musicxml")
    else:
        score = converter.parse(str(path))
    flags = report.flags
    flags.extend(ReviewFlag(None, -1, None, "omr_export_incomplete", x) for x in nearby_log_issues(path))
    return score, flags


def align_score_measures(score):
    """Insert empty bars for omitted identities, retaining notes/spanner objects.

    Runs before routing/layout. Empty bars are marked so missing owned voices
    cannot acquire another staff's pitches through single-staff fallback.
    """
    parts = list(score.parts)
    rows = [list(p.getElementsByClass(stream.Measure)) for p in parts]
    if not rows or any(not row for row in rows):
        return []  # unmeasured streams use the existing makeMeasures path
    ids = [[str(m.number) + (m.numberSuffix or "") for m in row] for row in rows]
    if all(row == ids[0] for row in ids):
        # Batch 11: identical identity sequences (incl. agreed reused numbers)
        # need no padding; bars align by position.
        return []
    grid = _identity_grid(ids)
    flags = []
    for i, (part, measures, identities) in enumerate(zip(parts, rows, ids)):
        missing = set(grid) - set(identities)
        if not missing:
            continue
        flags.append(ReviewFlag(None, i, None, "measure_count_mismatch",
            f"Part {part.id} has {len(measures)} source bars, identity grid has {len(grid)}; "
            "missing source measures padded with rests, not recovered music."))
        lookup = dict(zip(identities, measures))
        for m in measures:
            part.remove(m)
        offset = 0.0
        for k, identity in enumerate(grid):
            m = lookup.get(identity)
            if m is None:
                m = stream.Measure(number=identity)
                m.editorial.satb_missing_source = True
                flags.append(ReviewFlag(k + 1, i, None, "xml_missing_measure",
                    f"Part {part.id}: XML measure {identity} missing; empty/rest placeholder only, no notes recovered."))
            part.insert(offset, m)
            try:
                length = float(m.barDuration.quarterLength)
            except Exception:
                length = 4.0
            offset += max(float(m.highestTime), length)
    return flags
