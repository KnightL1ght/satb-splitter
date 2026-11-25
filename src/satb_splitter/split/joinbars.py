"""Batch 13: join one printed bar that Audiveris split across a system/page break.

Audiveris sometimes exports ONE printed bar as TWO measures when a system (or
page) break falls inside it: the first half ends a system, the second half
starts the next one (Hard 30/31; the p6 9/8 bars read as 3/8 + 6/8; Hard
519/520 across the page_13 / page_14 break). The splitter used to pad both
halves to the full meter or write them as two short bars, which added bars
and broke the printed numbering.

A pair is joined only when ALL of these hold (nothing is padded or invented):

* the second half starts a new system or page (``<print new-system/new-page>``
  inside one page file, or the first bar of the next page file);
* the length of each half (longest choir staff; a staff that only holds a
  whole-bar rest says nothing) is shorter than the meter in effect, and the
  two lengths add up to that meter exactly;
* the second half prints no different time signature, there is no
  repeat / final / double barline between the halves, neither half is a bar
  Audiveris failed to export, and no voice has a note crossing the split point.

Each half is split as usual (its own staff routing, e.g. a women's passage on
the lower staff that ends at the system break - Hard p6), then the two
rendered voice bars are joined: the first half's notes up to its length, the
second half's notes after them. Padding rests of the halves are dropped.

A system-break pair whose halves are both short but add up to LESS than the
meter is flagged ``split_bar_unjoined`` (possible split bar, not joined).

Audiveris dev3b (c37da2a0c) marks the second half explicitly: an implicit
continuation measure numbered ``X<n>`` (``implicit="yes"``) right after
measure ``<n>`` (Hard p6 25+X25, 33+X33, 41+X41; p1 30+X30). Such a measure is
joined into the preceding one when the summed length fits the meter, even
without a system-break mark; when it does not fit it is flagged
``split_bar_unjoined``. Older exports (dev2/dev3, two plain measures) use the
system/page-break + duration rule above.
"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Iterable

from music21 import bar, clef, key, layout, meter, spanner, stream

EPS = 1e-6
JOIN_REASON = "split_bar_joined"
UNJOINED_REASON = "split_bar_unjoined"
#: flags about a half bar (padding / short meter) that the join makes obsolete
HALF_BAR_REASONS = frozenset({"meter_from_content", "meter_irregular", "underfull_measure", "bar_length_mismatch"})
#: the second half printed a key change that cannot be written mid-bar (MuseScore drops it)
KEY_DROPPED_REASON = "split_bar_key_change_dropped"


@dataclass
class BarJoin:
    first: int  # 0-based source position of the first half
    first_len: float
    second_len: float
    ratio: str
    first_num: str
    second_num: str
    where: str  # "system break" | "page break"
    index: int = -1  # 0-based position of the joined bar after all joins on the page
    implicit: bool = False  # second half is an Audiveris implicit X<n> continuation

    @property
    def raw_second(self) -> int:
        """1-based source position of the second half."""
        return self.first + 2

    def detail(self, first_label: str = "", second_label: str = "") -> str:
        a = f"{first_label} m{self.first_num}".strip()
        b = f"{second_label} m{self.second_num}".strip()
        how = " (Audiveris implicit continuation measure)" if self.implicit else ""
        return (f"One printed bar split by a {self.where}{how}: {a} ({self.first_len:g} q) + {b} "
                f"({self.second_len:g} q) joined into one {self.ratio} bar (no padding, no notes added). "
                "Check against the PDF.")


def ratio_len(ratio: str) -> float:
    num, den = str(ratio).split("/")
    return sum(int(x) for x in num.split("+")) * 4.0 / int(den)


def _valid_ratio(r) -> str | None:
    if not r:
        return None
    try:
        ratio_len(r)
        return str(r)
    except Exception:  # noqa: BLE001
        return None


def break_kind(m: stream.Measure | None) -> str | None:
    """'page break' / 'system break' when this measure starts a new page/system."""
    if m is None:
        return None
    kind = None
    for el in m.getElementsByClass(layout.LayoutBase):
        if isinstance(el, layout.PageLayout) and getattr(el, "isNew", False):
            return "page break"
        if isinstance(el, layout.SystemLayout) and getattr(el, "isNew", False):
            kind = "system break"
    return kind


_CONT_RE = re.compile(r"(?:\d+)?X(\d+)")


def is_continuation(m: stream.Measure | None) -> bool:
    """Audiveris implicit continuation measure ``X<n>`` (music21: suffix 'X<n>')."""
    if m is None:
        return False
    return bool(_CONT_RE.fullmatch(f"{getattr(m, 'numberSuffix', None) or ''}")) or \
        bool(_CONT_RE.fullmatch(str(getattr(m, "number", "") or "")))


def measure_label(m: stream.Measure) -> str:
    """MXL measure number as printed in the file ('30', 'X30', '12a')."""
    suffix = getattr(m, "numberSuffix", None) or ""
    if _CONT_RE.fullmatch(suffix):
        return "X" + _CONT_RE.fullmatch(suffix).group(1)
    return f"{m.number}{suffix}"


def _sounding(m: stream.Measure) -> bool:
    return any((not el.isRest) and not el.duration.isGrace for el in m.recurse().notes)


def _content(m: stream.Measure) -> float:
    end = 0.0
    for el in m.recurse().notesAndRests:
        if el.duration.isGrace:
            continue
        try:
            off = float(el.getOffsetInHierarchy(m))
        except Exception:  # noqa: BLE001
            off = float(el.offset)
        end = max(end, off + float(el.quarterLength))
    return end


def half_length(measures: Iterable[stream.Measure | None], bar_len: float) -> float | None:
    """Length of a (possible) half bar: the longest staff that says something."""
    vals = []
    for m in measures:
        if m is None:
            continue
        c = _content(m)
        if c <= EPS:
            continue
        if not _sounding(m) and c >= bar_len - EPS:
            continue  # a whole-bar rest (written with the FULL meter) tells nothing
        vals.append(c)
    return max(vals) if vals else None


def _own_ratio(m: stream.Measure | None) -> str | None:
    if m is None:
        return None
    own = sorted(m.getElementsByClass(meter.TimeSignature), key=lambda t: float(t.offset))
    return _valid_ratio(own[0].ratioString) if own else None


def carried_ratios(ms: list[stream.Measure]) -> list[str | None]:
    cur = None
    row = []
    for m in ms:
        r = _own_ratio(m)
        if r:
            cur = r
        row.append(cur)
    return row


def _plain(bl) -> bool:
    if bl is None:
        return True
    if isinstance(bl, bar.Repeat):
        return False
    return str(getattr(bl, "type", "regular") or "regular") in ("regular", "normal", "none")


def plain_right(m: stream.Measure | None) -> bool:
    return m is None or _plain(m.rightBarline)


def plain_left(m: stream.Measure | None) -> bool:
    return m is None or _plain(m.leftBarline)


def _missing(m: stream.Measure | None) -> bool:
    return m is not None and bool(getattr(m.editorial, "satb_missing_source", False))


def remap_measure(pos: int, raw_seconds: Iterable[int]) -> int:
    """1-based position before joins -> position after joins (2nd half -> joined bar)."""
    return pos - sum(1 for b in raw_seconds if b <= pos)


def merge_source_numbers(nums: list, raw_seconds: Iterable[int]) -> list:
    out = list(nums)
    for b in sorted(raw_seconds, reverse=True):
        if 2 <= b <= len(out):
            out[b - 2] = f"{out[b - 2]}+{out[b - 1]}"
            del out[b - 1]
    return out


def _measures(part: stream.Part) -> list[stream.Measure]:
    return list(part.getElementsByClass(stream.Measure))


# ---------------------------------------------------------------------------
# Detection inside one page (source score)
# ---------------------------------------------------------------------------

def find_split_bars(
    parts: list[stream.Part],
    ref_parts: list[stream.Part],
    *,
    inherit_ts: str | None = None,
    fallback_ratio: Callable[[], str | None] | None = None,
) -> tuple[list[BarJoin], list[tuple[int, str]]]:
    """Split-bar pairs of one page (nothing is changed). Returns (joins, unjoined)."""
    joins: list[BarJoin] = []
    unjoined: list[tuple[int, str]] = []
    if not parts or not ref_parts:
        return joins, unjoined
    per = [_measures(p) for p in ref_parts]
    alls = [_measures(p) for p in parts]
    n = min((len(ms) for ms in per), default=0)
    rows = [carried_ratios(ms) for ms in per]
    fallback_cache: list = []

    def fallback() -> str | None:
        if not fallback_cache:
            fallback_cache.append(_valid_ratio(fallback_ratio()) if fallback_ratio else None)
        return fallback_cache[0]

    i = 0
    while i + 1 < n:
        where = None
        for ms in alls:
            if i + 1 < len(ms):
                where = break_kind(ms[i + 1]) or where
                if where == "page break":
                    break
        cont = any(is_continuation(ms[i + 1]) for ms in alls if i + 1 < len(ms))
        if where is None and not cont:
            i += 1
            continue
        where = where or "system break"
        nominal = next((row[i] for row in rows if row[i]), None) or _valid_ratio(inherit_ts) or fallback()
        if nominal is None:
            i += 1
            continue
        bar_len = ratio_len(nominal)
        a_list = [ms[i] for ms in per]
        b_list = [ms[i + 1] for ms in per]
        b_own = {r for r in (_own_ratio(m) for m in b_list) if r}
        if (b_own and b_own != {nominal}) or any(_missing(m) for m in a_list + b_list) \
                or not all(plain_right(ms[i]) for ms in alls if i < len(ms)) \
                or not all(plain_left(ms[i + 1]) for ms in alls if i + 1 < len(ms)):
            i += 1
            continue
        la = half_length(a_list, bar_len)
        lb = half_length(b_list, bar_len)
        num_a = measure_label(a_list[0])
        num_b = measure_label(b_list[0])
        if la is None or lb is None or la >= bar_len - EPS or lb >= bar_len - EPS:
            if cont:
                unjoined.append((i, (
                    f"Audiveris implicit continuation m{num_b} after m{num_a} NOT joined: "
                    f"lengths {la if la is not None else 0:g} q + {lb if lb is not None else 0:g} q "
                    f"do not fit {nominal}. Check against the PDF.")))
            i += 1
            continue
        if abs(la + lb - bar_len) < EPS:
            joins.append(BarJoin(i, la, lb, nominal, num_a, num_b, where, implicit=cont))
            i += 2
            continue
        if cont and la + lb > bar_len + EPS:
            unjoined.append((i, (
                f"Audiveris implicit continuation m{num_b} after m{num_a} NOT joined: "
                f"{la:g} q + {lb:g} q is more than {nominal}. Check against the PDF.")))
        if la + lb < bar_len - EPS:
            unjoined.append((i, (
                f"Possible bar split by a {where}: MXL m{num_a} ({la:g} q) + m{num_b} ({lb:g} q) "
                f"add up to {la + lb:g} of {nominal}; NOT joined (lengths do not fill the meter). "
                "Check against the PDF.")))
        i += 1
    return joins, unjoined


# ---------------------------------------------------------------------------
# Joining rendered voice bars (same page or across page files)
# ---------------------------------------------------------------------------

def can_trim(m: stream.Measure, limit: float) -> bool:
    """No note of this rendered bar crosses ``limit``."""
    for el in m.recurse().notes:
        if el.duration.isGrace:
            continue
        off = float(el.getOffsetInHierarchy(m))
        if off < limit - EPS and off + float(el.quarterLength) > limit + EPS:
            return False
    return True


def _trim_container(container, limit: float, base: float = 0.0) -> None:
    """Drop notes/rests at/after ``limit``; shorten a rest running past it."""
    for el in list(container.notesAndRests):
        off = base + float(container.elementOffset(el))
        if off >= limit - EPS:
            container.remove(el)
        elif off + float(el.quarterLength) > limit + EPS and el.isRest:
            el.quarterLength = limit - off


def _in_bracket(container, m) -> bool:
    try:
        return any(sp.hasSpannedElement(m) for sp in container.recurse().getElementsByClass(spanner.RepeatBracket))
    except Exception:  # noqa: BLE001
        return False


def join_output_bars(a: stream.Measure, b: stream.Measure, la: float, lb: float, ratio: str) -> str | None:
    """Join two rendered single-voice bars: a[:la] + b[:lb].

    Returns the meter in effect at ``b`` when it differs from ``ratio`` (the
    caller re-states it on the bar after ``b``), else None.
    """
    a_own = _own_ratio(a)
    b_eff = _own_ratio(b) or a_own
    # Batch 13 add-on: a key change printed at the start of the second half.
    # A mid-bar <key> is dropped by MuseScore, so the caller re-states it on
    # the bar after the joined one and flags it (pitches are not affected).
    a.editorial.satb_dropped_key = None
    b_keys = [k for k in b.getElementsByClass(key.KeySignature) if float(b.elementOffset(k)) < lb - EPS]
    if b_keys:
        try:
            a_key = (list(a.getElementsByClass(key.KeySignature)) or [a.getContextByClass(key.KeySignature)])[-1]
        except Exception:  # noqa: BLE001
            a_key = None
        if a_key is None or getattr(a_key, "sharps", None) != getattr(b_keys[-1], "sharps", None):
            a.editorial.satb_dropped_key = deepcopy(b_keys[-1])
    _trim_container(a, la)
    for v in a.voices:
        _trim_container(v, la, float(a.elementOffset(v)))
    _trim_container(b, lb)
    for v in b.voices:
        _trim_container(v, lb, float(b.elementOffset(v)))
    if a_own is not None:
        for ts in list(a.getElementsByClass(meter.TimeSignature)):
            a.remove(ts)
        a.insert(0, meter.TimeSignature(ratio))
    for el in list(b):
        off = float(b.elementOffset(el))
        if isinstance(el, (layout.LayoutBase, meter.TimeSignature, bar.Barline, clef.Clef, key.KeySignature)):
            continue
        if off >= lb - EPS and not isinstance(el, stream.Voice):
            continue
        b.remove(el)
        a.insert(off + la, el)
    if b.rightBarline is not None:
        a.rightBarline = deepcopy(b.rightBarline)
    a.paddingLeft = 0.0
    a.paddingRight = 0.0
    a.showNumber = stream.enums.ShowNumber.DEFAULT
    a.editorial.satb_joined = True
    return b_eff if b_eff and b_eff != ratio else None


def joinable_rendered(parts: list[stream.Part], i: int, la: float, lb: float) -> bool:
    for part in parts:
        ms = _measures(part)
        if i + 1 >= len(ms):
            return False
        if not can_trim(ms[i], la) or not can_trim(ms[i + 1], lb):
            return False
        if _in_bracket(part, ms[i + 1]) and not _in_bracket(part, ms[i]):
            return False
    return True


def join_rendered_pair(part: stream.Part, i: int, la: float, lb: float, ratio: str) -> None:
    ms = _measures(part)
    a, b = ms[i], ms[i + 1]
    b_eff = join_output_bars(a, b, la, lb, ratio)
    for sp in list(part.recurse().getElementsByClass(spanner.RepeatBracket)):
        try:
            if sp.hasSpannedElement(b):
                sp.spannerStorage.remove(b)
        except Exception:  # noqa: BLE001
            pass
    part.remove(b)
    if b_eff and i + 1 < len(ms) - 1:
        nxt = ms[i + 2]
        if not list(nxt.getElementsByClass(meter.TimeSignature)):
            nxt.insert(0, meter.TimeSignature(b_eff))
    restate_key(a, ms[i + 2] if i + 2 < len(ms) else None)


def key_label(ks) -> str:
    try:
        n = int(ks.sharps)
    except Exception:  # noqa: BLE001
        return str(ks)
    return "no sharps/flats" if n == 0 else f"{abs(n)} {'sharp' if n > 0 else 'flat'}{'s' if abs(n) > 1 else ''}"


def restate_key(a: stream.Measure, nxt: stream.Measure | None) -> bool:
    """Put a key change dropped by a join on the next bar (if it has none)."""
    ks = getattr(a.editorial, "satb_dropped_key", None)
    if ks is None or nxt is None or list(nxt.getElementsByClass(key.KeySignature)):
        return False
    nxt.insert(0, deepcopy(ks))
    return True


def key_dropped_detail(ks, la: float, where: str, next_bar: int | None) -> str:
    moved = (f"re-stated at the start of bar {next_bar}" if next_bar else "not re-stated (no following bar)")
    return (f"Key change to {key_label(ks)} printed at the start of the second half of this joined bar "
            f"({where}, beat offset {la:g}) cannot be written mid-bar (MuseScore drops a mid-measure key); "
            f"joined bar keeps the previous key, key change {moved}. Pitches unchanged. Check against the PDF.")


def apply_page_joins(out_parts: dict, joins: list[BarJoin], flags: list, flag_cls) -> list[BarJoin]:
    """Join the rendered halves of every voice (split_score). Returns the joins done."""
    parts = [p for p in out_parts.values() if p is not None]
    done: list[BarJoin] = []
    skipped: list[BarJoin] = []
    for j in joins:
        (done if joinable_rendered(parts, j.first, j.first_len, j.second_len) else skipped).append(j)
    for j in sorted(done, key=lambda x: -x.first):
        for part in parts:
            join_rendered_pair(part, j.first, j.first_len, j.second_len, j.ratio)
    raw_seconds = [j.raw_second for j in done]
    halves = set()
    for j in done:
        halves.update({j.first + 1, j.first + 2})
    if done:
        flags[:] = [f for f in flags if not (f.measure in halves and f.reason in HALF_BAR_REASONS)]
        for f in flags:
            if f.measure is not None:
                f.measure = remap_measure(f.measure, raw_seconds)
    for j in done:
        j.index = remap_measure(j.first + 1, raw_seconds) - 1
        flags.append(flag_cls(measure=j.index + 1, part_index=-1, offset=None, reason=JOIN_REASON, detail=j.detail()))
        ms0 = _measures(parts[0]) if parts else []
        ks = getattr(ms0[j.index].editorial, "satb_dropped_key", None) if j.index < len(ms0) else None
        if ks is not None:
            nb = j.index + 2 if j.index + 1 < len(ms0) else None
            flags.append(flag_cls(measure=j.index + 1, part_index=-1, offset=None, reason=KEY_DROPPED_REASON,
                                  detail=key_dropped_detail(ks, j.first_len, "system break", nb)))
    for j in skipped:
        flags.append(flag_cls(
            measure=remap_measure(j.first + 1, raw_seconds), part_index=-1, offset=None, reason=UNJOINED_REASON,
            detail=(f"Bar split by a {j.where}: MXL m{j.first_num} ({j.first_len:g} q) + m{j.second_num} "
                    f"({j.second_len:g} q) = {j.ratio} NOT joined: a note crosses the split point or the "
                    "second half opens an ending bracket. Check against the PDF.")))
    return done


# ---------------------------------------------------------------------------
# Edge information for joins across page files (export/merge.py)
# ---------------------------------------------------------------------------

def page_edge_info(parts: list[stream.Part], ref_parts: list[stream.Part],
                   inherit_ts: str | None, fallback: str | None) -> dict:
    per = [_measures(p) for p in ref_parts]
    alls = [_measures(p) for p in parts]
    n = min((len(ms) for ms in per), default=0)
    if n == 0:
        return {}
    rows = [carried_ratios(ms) for ms in per]
    last_ratio = next((row[n - 1] for row in rows if row[n - 1]), None) or _valid_ratio(inherit_ts) or _valid_ratio(fallback)
    info: dict = {}
    if last_ratio:
        last = [ms[n - 1] for ms in per]
        info["last"] = {
            "content": half_length(last, ratio_len(last_ratio)),
            "ratio": last_ratio,
            "plain": all(plain_right(ms[n - 1]) for ms in alls if len(ms) >= n),
            "missing": any(_missing(m) for m in last),
            "number": str(last[0].number),
        }
    first = [ms[0] for ms in per]
    info["first"] = {
        "measures": first,
        "own_ratios": {r for r in (_own_ratio(m) for m in first) if r},
        "plain": all(plain_left(ms[0]) for ms in alls if ms),
        "missing": any(_missing(m) for m in first),
        "number": str(first[0].number),
    }
    return info


def first_content(info: dict, bar_len: float) -> float | None:
    ms = ((info or {}).get("first") or {}).get("measures")
    return half_length(ms, bar_len) if ms else None