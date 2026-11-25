"""Batch 12: multi-bar rests (``<multiple-rest>``) - detect, honour, flag.

Audiveris writes a multi-bar rest as ``<measure-style><multiple-rest>N``
on the first bar, followed (MusicXML convention) by the N-1 bars it stands
for. The splitter keeps exactly what the MXL carries:

* every multirest is flagged for review (merged bar, count used, source of
  the count);
* a count the file does not fully expand (the page ENDS before the N bars)
  is honoured by appending whole-bar rests to every voice;
* a count that collides with music or another multirest is NOT expanded
  (flagged ``multirest_conflict``);
* printed counts read from the PDF text layer (``<mxl>.multirest.json``,
  written by ``omr/multirest.py``) are only compared, never used to invent
  bars: a disagreement is flagged ``multirest_count_mismatch``; a printed
  count over a bar without ``<multiple-rest>`` is ``multirest_missing``.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from copy import deepcopy

from music21 import bar, note, spanner, stream

from satb_splitter.model.config import ReviewFlag

SIDECAR_SUFFIX = ".multirest.json"


def read_mxl_text(path: Path | str) -> str | None:
    p = Path(path)
    try:
        with zipfile.ZipFile(str(p)) as z:
            names = [n for n in z.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
            return z.read(names[0]).decode("utf-8", errors="replace") if names else None
    except (OSError, zipfile.BadZipFile):
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


_PART_RE = re.compile(r"<part\s+id=\"[^\"]*\"[^>]*>(.*?)</part>", re.S)
_MEASURE_RE = re.compile(r"<measure\b([^>]*)>(.*?)</measure>", re.S)


def _measures(part_body: str) -> list[tuple[str, str]]:
    out = []
    for attrs, body in _MEASURE_RE.findall(part_body):
        num = re.search(r'\bnumber="([^"]*)"', attrs)
        out.append((num.group(1) if num else "", body))
    return out


def mxl_measure_numbers(path: Path | str) -> list[str]:
    """Measure numbers as WRITTEN in the file (first part, by position)."""
    text = read_mxl_text(path)
    if not text:
        return []
    m = _PART_RE.search(text)
    return [n for n, _ in _measures(m.group(1))] if m else []


@dataclass
class Multirest:
    position: int  # 1-based bar position on the page
    number: str  # MXL measure number
    count: int  # <multiple-rest> value
    present: int  # bars of the run present in the file (incl. the first)
    stop: str  # "" (complete) | "end" | "music" | "multirest"
    parts: list = field(default_factory=list)


def scan_multirests(path: Path | str) -> list[Multirest]:
    """Every ``<multiple-rest>`` start (union over parts, by position)."""
    text = read_mxl_text(path)
    if not text:
        return []
    found: dict[int, Multirest] = {}
    for pi, body in enumerate(_PART_RE.findall(text)):
        ms = _measures(body)
        for k, (num, mb) in enumerate(ms):
            hit = re.search(r"<multiple-rest[^>]*>\s*(\d+)\s*</multiple-rest>", mb)
            if not hit:
                continue
            count = int(hit.group(1))
            present, stop = 1, ""
            for j in range(k + 1, k + count):
                if j >= len(ms):
                    stop = "end"
                    break
                nb = ms[j][1]
                if "<multiple-rest" in nb:
                    stop = "multirest"
                    break
                if "<pitch>" in nb or "<unpitched>" in nb:
                    stop = "music"
                    break
                present += 1
            mr = found.get(k + 1)
            if mr is None:
                found[k + 1] = Multirest(k + 1, num, count, present, stop, [pi])
            else:
                mr.parts.append(pi)
                if (mr.count, mr.present) != (count, present):
                    # parts disagree: keep the smallest complete reading
                    if present < mr.present:
                        mr.present, mr.stop = present, stop
                    mr.count = max(mr.count, count)
    return [found[k] for k in sorted(found)]


def sidecar_path(mxl: Path | str) -> Path:
    p = Path(mxl)
    return p.with_name(p.stem + SIDECAR_SUFFIX)


def load_printed(mxl: Path | str) -> tuple[bool, dict[int, int]]:
    """(text layer checked?, {0-based bar index: printed count}).

    Entries marked ``beyond`` (printed over a bar the MXL lacks) are keyed
    by the NEGATIVE ``-(index + 1)`` so they are never matched to a
    multirest start."""
    sc = sidecar_path(mxl)
    if not sc.is_file():
        return False, {}
    try:
        data = json.loads(sc.read_text(encoding="utf-8"))
        out = {}
        for it in data.get("printed", []):
            if isinstance(it.get("measure_index"), int) and isinstance(it.get("count"), int):
                key = -(it["measure_index"] + 1) if it.get("beyond") else it["measure_index"]
                out.setdefault(key, it["count"])
        return True, out
    except (OSError, ValueError, AttributeError):
        return False, {}


def _append_rest_bars(result, n: int) -> None:
    for role, sc in result.scores.items():
        parts = list(sc.parts)
        if not parts:
            continue
        ms = list(parts[0].getElementsByClass(stream.Measure))
        if not ms:
            continue
        last = ms[-1]
        ql = float(last.barDuration.quarterLength)
        num = int(last.number or len(ms))
        for i in range(n):
            m = stream.Measure(number=num + i + 1)
            r = note.Rest(quarterLength=ql)
            r.fullMeasure = True
            m.append(r)
            parts[0].append(m)


def _insert_rest_bars(result, pos: int, n: int) -> None:
    """Batch 14 (F7): insert ``n`` full-measure rest bars right after 1-based bar ``pos``."""
    if n <= 0 or pos < 1:
        return
    for role, sc in result.scores.items():
        parts = list(sc.parts)
        if not parts:
            continue
        part = parts[0]
        ms = list(part.getElementsByClass(stream.Measure))
        if pos > len(ms):
            continue
        anchor = ms[pos - 1]
        ql = float(anchor.barDuration.quarterLength) if anchor.barDuration else float(anchor.duration.quarterLength or 4.0)
        anchor_off = float(part.elementOffset(anchor))
        anchor_num = int(anchor.number or pos)
        shift_q = n * ql
        for m in reversed(ms[pos:]):
            part.setElementOffset(m, float(part.elementOffset(m)) + shift_q)
            if isinstance(m.number, int):
                m.number += n
        for i in range(n):
            m = stream.Measure(number=anchor_num + i + 1)
            r = note.Rest(quarterLength=ql)
            r.fullMeasure = True
            m.append(r)
            part.insert(anchor_off + (i + 1) * ql, m)


def _all_voices_and_source_rest_only(result, pos: int, path: Path, mxl_pos_1based: int) -> bool:
    """Verify every output voice and every raw XML part is rest-only at this bar."""
    for sc in result.scores.values():
        parts = list(sc.parts)
        if not parts:
            continue
        ms = list(parts[0].getElementsByClass(stream.Measure))
        if pos < 1 or pos > len(ms):
            return False
        if any(True for _ in ms[pos - 1].recurse().notes):
            return False
    text = read_mxl_text(path)
    if text:
        for body in _PART_RE.findall(text):
            ms = _measures(body)
            if 1 <= mxl_pos_1based <= len(ms):
                mb = ms[mxl_pos_1based - 1][1]
                if "<pitch>" in mb or "<unpitched>" in mb:
                    return False
    return True


def _plain(rb) -> bool:
    if rb is None:
        return True
    if isinstance(rb, bar.Repeat):
        return False
    return str(getattr(rb, "type", "regular") or "regular") in ("regular", "normal", "none")


def move_run_end_form(result, first: int, last: int) -> bool:
    """Batch 13: barline / repeat end / ending end written on the multirest bar
    belongs to the LAST bar of the run (MusicXML puts it on the one bar that
    carries ``<multiple-rest>``; Normal m31 MR2 light-heavy -> bar 60, Hard p2
    MR20). Start-side repeats/endings stay on the first bar."""
    moved = False
    if last <= first or first < 1:
        return False
    for sc in result.scores.values():
        parts = list(sc.parts)
        if not parts:
            continue
        part = parts[0]
        ms = list(part.getElementsByClass(stream.Measure))
        if last > len(ms):
            continue
        a, z = ms[first - 1], ms[last - 1]
        if not _plain(a.rightBarline) and _plain(z.rightBarline):
            z.rightBarline = deepcopy(a.rightBarline)
            a.rightBarline = None
            moved = True
        for sp in part.recurse().getElementsByClass(spanner.RepeatBracket):
            els = sp.getSpannedElements()
            if els and els[-1] is a:
                sp.addSpannedElements([m for m in ms[first:last] if not sp.hasSpannedElement(m)])
                moved = True
    return moved


def apply_multirests(
    result,
    path: Path | str,
    pos_map=None,
    *,
    expand_mr1: bool = False,
) -> list[ReviewFlag]:
    """Flag every multirest of one page; append bars for a truncated count.

    ``pos_map`` (Batch 13) maps a 1-based bar position of the MXL to the
    position after split bars were joined.
    ``expand_mr1`` (Batch 14 F7): ONLY when True, recover a verified PDF
    count ``printed > 1`` when MXL has ``<multiple-rest>1</multiple-rest>``
    and all voices/staves on that measure are rest-only.
    """
    pm = pos_map or (lambda p: p)
    path = Path(path)
    runs = scan_multirests(path)
    checked, printed = load_printed(path)
    flags: list[ReviewFlag] = []
    nums = list(getattr(result, "source_measure_numbers", []) or [])
    starts = {mr.position for mr in runs}
    insertions: list[tuple[int, int]] = []

    def _shifted_pos(raw_mxl_pos: int) -> int:
        base_pos = pm(raw_mxl_pos)
        return base_pos + sum(a for ins_base, a in insertions if base_pos > ins_base)

    for mr in runs:
        p = printed.get(mr.position - 1)
        if not checked:
            pdf = "no printed count available (no PDF text layer / no sidecar)"
        elif p is None:
            pdf = "no printed count found above this bar in the PDF text layer"
        else:
            pdf = f"PDF text layer prints {p}"
        base_pos = pm(mr.position)
        pos = _shifted_pos(mr.position)
        added = 0
        recovered_mr1 = False
        if mr.stop == "end":
            added = mr.count - mr.present
            _append_rest_bars(result, added)
            nums.extend(f"{mr.number}+{i + 1}" for i in range(added))
        elif (
            expand_mr1
            and mr.count == 1
            and p is not None
            and p > 1
            and _all_voices_and_source_rest_only(result, pos, path, mr.position)
        ):
            added = p - 1
            _insert_rest_bars(result, pos, added)
            nums[pos:pos] = [f"{mr.number}+{i + 1}" for i in range(added)]
            for fl in result.flags:
                if fl.measure is not None and fl.measure > pos:
                    fl.measure += added
            for fl in flags:
                if fl.measure is not None and fl.measure > pos:
                    fl.measure += added
            insertions.append((base_pos, added))
            recovered_mr1 = True

        effective_count = p if recovered_mr1 else mr.count
        if mr.stop in ("music", "multirest"):
            reason = "multirest_conflict"
            what = (f"only {mr.present} of {mr.count} bar(s) follow before "
                    f"{'notes' if mr.stop == 'music' else 'another multirest'}; NOT expanded "
                    f"({mr.present} bar(s) kept per voice)")
        elif recovered_mr1:
            reason = "multirest_pdf_recovered"
            what = (f"recovered {p} bar(s) from PDF text layer (MXL had <multiple-rest>1</multiple-rest>; "
                    f"{added} rest bar(s) inserted per voice)")
        elif p is not None and p != mr.count:
            reason = "multirest_count_mismatch"
            what = (f"count used {mr.count} but the PDF prints {p}: "
                    f"{abs(p - mr.count)} bar(s) {'missing' if p > mr.count else 'extra'} - check against the PDF; "
                    "no bars invented")
        else:
            reason = "multirest"
            what = f"count used {mr.count}"
        tail = (
            f" ({added} rest bar(s) appended per voice: the page ended inside the run)"
            if (added and not recovered_mr1) else ""
        )
        if (mr.stop in ("", "end") or recovered_mr1) and effective_count > 1 and move_run_end_form(result, pos, pos + effective_count - 1):
            tail += (f" Barline/ending end on the multirest bar moved to its last bar "
                     f"(page bar {pos + effective_count - 1}).")
        flags.append(ReviewFlag(pos, -1, None, reason,
            f"Multi-bar rest at MXL m{mr.number}: {what}; count source: {'PDF text layer' if recovered_mr1 else f'MXL <multiple-rest>{mr.count}'}; "
            f"{effective_count if (mr.stop in ('', 'end') or recovered_mr1) else mr.present} bar(s) per voice from this bar; {pdf}.{tail}"))
    for idx, count in sorted(printed.items(), key=lambda kv: abs(kv[0])):
        if idx >= 0 and (idx + 1 in starts or count <= 1):
            continue
        if idx < 0:
            flags.append(ReviewFlag(_shifted_pos(-idx), -1, None, "multirest_missing",
                f"PDF text layer prints a {count}-bar rest AFTER this bar (end of the system) but the MXL "
                f"has no bar there at all; nothing added (count not invented) - {count} bar(s) missing."))
            continue
        pos = _shifted_pos(idx + 1)
        if not _all_voices_and_source_rest_only(result, pos, path, idx + 1):
            continue
        flags.append(ReviewFlag(pos, -1, None, "multirest_missing",
            f"PDF text layer prints a {count}-bar rest over this bar but the MXL has no <multiple-rest> here; "
            f"kept the bar(s) as exported (count not invented) - up to {count - 1} bar(s) may be missing."))
    if nums:
        result.source_measure_numbers = nums
    if insertions:
        result.multirest_insertions = insertions
        edge = getattr(result, "edge_info", None)
        if isinstance(edge, dict) and "last" in edge:
            last_info = edge["last"]
            sop_parts = list((result.scores.get("Soprano") or next(iter(result.scores.values()))).parts)
            sop_ms = list(sop_parts[0].getElementsByClass(stream.Measure)) if sop_parts else []
            if sop_ms:
                last_m = sop_ms[-1]
                if not any(True for _ in last_m.recurse().notes):
                    last_info = dict(last_info)
                    last_info["content"] = None
                    last_info["number"] = str(nums[-1] if nums else last_m.number)
                    edge["last"] = last_info
    return flags


def source_measure_numbers(score, path: Path | str) -> list[str]:
    """MXL measure number per bar position of a loaded page (for REVIEW).

    The numbers as written in the file are used when the file's first part
    has exactly as many bars as the loaded score (this also survives the
    Batch 11 renumbering of reused numbers); otherwise music21's numbers
    (the XML identities after alignment) are used.
    """
    parts = list(getattr(score, "parts", []) or [])
    ms = list(parts[0].getElementsByClass(stream.Measure)) if parts else []
    raw = mxl_measure_numbers(path)
    if raw and len(raw) == len(ms):
        return raw
    out = []
    for m in ms:
        suffix = getattr(m, "numberSuffix", None) or ""
        out.append(suffix if re.fullmatch(r"X\d+", suffix) else f"{m.number}{suffix}")
    return out
