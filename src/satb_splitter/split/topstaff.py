"""Batch 11: route a labelled / melodic top staff above the choir to Soprano.

100.pdf p3 bars 16-24 print a 'Sopran' melody on the staff ABOVE the choir
SA/TB pair while the SA staff only hums ('hm'). The layout rule "staves
above the choir pair are ignored" dropped that melody and the Soprano voice
copied the Alto hum. This module decides, per bar, whether that top staff
carries the Soprano melody. It never changes the layout of other pages:

* a bar is routed only for the staff DIRECTLY above the SA staff;
* by label: a text 'Sopran' / 'Soprano' / 'S.' on the top staff starts a run
  that lasts while the top staff keeps sounding (an empty bar or any other
  section marker, e.g. 'solo', ends it);
* by humming: the top staff sings real words while the choir SA staff only
  hums ('hm' / 'm'), e.g. the 100.pdf coda p4 bars 18-20;
* a plain 'Solo' / 'Sopran Solo' staff singing over a choir that sings
  words or a vocalise ('du', 'dum') is still omitted (100.pdf p1, p3 12-15).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from music21 import chord, expressions, harmony, stream

from satb_splitter.split.layout import _roles_from_name

HUM_TOKENS = frozenset({"hm", "hmm", "hmmm", "m", "mm", "mmm", "hum", "mmh"})
VOCALISE_TOKENS = frozenset({"du", "dum", "duu", "da", "la", "lu", "lo", "na", "u", "o", "a",
                             "ah", "oh", "uh", "uu", "oo", "aa"}) | HUM_TOKENS
_SOPRANO_LABEL = re.compile(r"^(s|sop|sopr|sopran|soprano|soprane|sopranele|soprani|sopranos)\.?$")


@dataclass
class TopStaffPlan:
    staff: int
    bars: dict[int, str] = field(default_factory=dict)  # bar index -> why


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", t.lower()).strip()


def _tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", str(text)).lower()
    return ["".join(ch for ch in w if ch.isalpha()) for w in re.split(r"[\s\-_]+", t)]


def _sounding(el) -> bool:
    return (not el.isRest) and not el.duration.isGrace and float(el.quarterLength) > 1e-6


def _bar_texts(m: stream.Measure) -> list[str]:
    return [str(t.content) for t in m.recurse().getElementsByClass(expressions.TextExpression) if t.content]


def is_soprano_label(text: str) -> bool:
    t = _norm(text)
    return bool(_SOPRANO_LABEL.match(t)) and "solo" not in t


def _lyric_kind(el) -> str | None:
    """'hum', 'word' or None (no lyric / unreadable OCR junk)."""
    kinds = set()
    for ly in getattr(el, "lyrics", []) or []:
        for tok in _tokens(ly.text or ""):
            if not tok:
                continue
            if tok in HUM_TOKENS:
                kinds.add("hum")
            elif len(tok) >= 2:
                kinds.add("word")
    if "hum" in kinds:
        return "hum"
    if "word" in kinds:
        return "word"
    return None


def _has_real_words(m: stream.Measure) -> bool:
    for el in m.recurse().notes:
        for ly in getattr(el, "lyrics", []) or []:
            for tok in _tokens(ly.text or ""):
                if len(tok) >= 2 and tok not in VOCALISE_TOKENS:
                    return True
    return False


def humming_bars(sa: stream.Part, lengths: list[float]) -> list[bool]:
    """Per bar: does the choir SA staff only hum here?

    State carries over bars without lyrics (a held 'hm'). A word in the first
    half of a bar ends the hum for that bar; a hum must start in the first half
    of the bar to count for it (100 p1 m14 'm' on beat 3 does not).
    """
    out: list[bool] = []
    state = False
    for i, m in enumerate(sa.getElementsByClass(stream.Measure)):
        half = (lengths[i] if i < len(lengths) else 4.0) / 2.0
        events = []
        for el in m.recurse().notes:
            k = _lyric_kind(el)
            if k is not None:
                events.append((float(el.getOffsetInHierarchy(m)), k))
        events.sort()
        hum = state
        if events and events[0][1] == "hum" and events[0][0] < half - 1e-6:
            hum = True
        if any(k == "word" and off < half - 1e-6 for off, k in events):
            hum = False
        out.append(hum)
        if events:
            state = events[-1][1] == "hum"
    return out


def plan_top_staff(parts, sa_index: int, top_index: int, lengths: list[float],
                   section_parser) -> TopStaffPlan:
    """Bars where staff ``top_index`` (directly above SA) is the Soprano melody."""
    plan = TopStaffPlan(top_index)
    top = list(parts[top_index].getElementsByClass(stream.Measure))
    hums = humming_bars(parts[sa_index], lengths)
    active = False
    for i, m in enumerate(top[: len(lengths)]):
        texts = _bar_texts(m)
        sounding = any(_sounding(el) for el in m.recurse().notesAndRests)
        if any(is_soprano_label(t) for t in texts):
            active = True
        elif any(section_parser(t) is not None or "solo" in _norm(t) for t in texts):
            active = False
        if not sounding:
            active = False
            continue
        if active:
            plan.bars[i] = "labelled 'Sopran' on the top staff"
        elif i < len(hums) and hums[i] and _has_real_words(m):
            plan.bars[i] = "top staff sings words while the choir SA staff only hums"
    # Batch 13: a lead-in (pickup) on the top staff right before a routed run
    # (100 p4 m18 'Cat mai' B4-A4 into the coda melody) belongs to the same
    # Soprano melody. The dev3 MXL lost the 'm' hum lyric on that bar, so the
    # hum rule no longer covered it. Route it when the top staff sings only in
    # the second half of the bar, with real words, while the choir SA staff is
    # silent under it (the SA notes of the bar stay in the Soprano).
    sa_ms = list(parts[sa_index].getElementsByClass(stream.Measure))
    for i in sorted(plan.bars):
        j = i - 1
        if j < 0 or j in plan.bars or j >= len(top) or j >= len(lengths):
            continue
        if is_lead_in(top[j], sa_ms[j] if j < len(sa_ms) else None, lengths[j], section_parser):
            plan.bars[j] = "lead-in (pickup) of the Soprano melody routed from the next bar"
    return plan


def is_lead_in(m: stream.Measure, sa_m, length: float, section_parser) -> bool:
    """Top-staff bar = only a pickup into the next bar (second half, words, SA silent)."""
    texts = _bar_texts(m)
    if any(section_parser(t) is not None or "solo" in _norm(t) for t in texts):
        return False
    notes = [el for el in m.recurse().notes if _sounding(el)]
    if not notes:
        return False
    first = min(float(el.getOffsetInHierarchy(m)) for el in notes)
    if first < length / 2.0 - 1e-6:
        return False
    if not _has_real_words(m):
        return False
    if sa_m is not None:
        for el in sa_m.recurse().notes:
            if not _sounding(el):
                continue
            s = float(el.getOffsetInHierarchy(sa_m))
            e = s + float(el.quarterLength)
            if s < length - 1e-6 and e > first + 1e-6:
                return False
    return True


_COPYRIGHT_TOKENS = frozenset({"corul", "evanghelic", "turneu", "romania", "dominum", "laudate"})


def _real_lyric_count(m: stream.Measure) -> int:
    """Count non-footer lyric syllables in a measure."""
    count = 0
    for el in m.recurse().notes:
        if not _sounding(el):
            continue
        for ly in getattr(el, "lyrics", []) or []:
            raw = (ly.text or "").strip()
            if not raw or "©" in raw or "www." in raw.lower() or "http" in raw.lower():
                continue
            toks = [t for t in _tokens(raw) if t]
            if not toks:
                continue
            if any(t in _COPYRIGHT_TOKENS for t in toks):
                continue
            count += 1
    return count


def _has_accompaniment_cues(m: stream.Measure) -> bool:
    """True if measure holds keyboard/guitar accompaniment cues (3+ note chord or ChordSymbol)."""
    for el in m.recurse().notes:
        if _sounding(el) and isinstance(el, chord.Chord) and len(el.pitches) >= 3:
            return True
    if any(True for _ in m.recurse().getElementsByClass(harmony.ChordSymbol)):
        return True
    return False


@dataclass
class TopSoloTenorPlan:
    """Per-bar routing for a ``solo Tenor`` staff directly above SA."""

    staff: int
    bars: dict[int, tuple[str, str]] = field(default_factory=dict)  # bar idx -> (kind, reason)
    active: bool = False


def plan_top_solo_tenor(
    parts,
    sa_index: int,
    top_index: int,
    lengths: list[float],
    section_parser,
    *,
    inherited_role: str | None = None,
    solo_policy: str = "route_named",
) -> TopSoloTenorPlan:
    """Route a ``solo Tenor`` top staff into Tenor, masking intro/solo accompaniment to rests.

    Activates ONLY when:
    - ``solo_policy == "route_named"``, AND
    - ``top_index == sa_index - 1``, AND
    - ``top_index`` part name mentions Tenor, OR an explicit ``solo tenor`` /
      ``tenor solo`` marker appears on the score, OR ``inherited_role == "Tenor"``
      was carried from a prior consecutive 3-staff page without a conflicting label.

    Never activates on 100.pdf (``Sopran`` / ``Solo`` / generic ``Voice``).
    """
    plan = TopSoloTenorPlan(top_index)
    if solo_policy != "route_named" or top_index < 0 or top_index >= len(parts):
        return plan

    top_part = parts[top_index]
    raw_name = getattr(top_part, "partName", None)
    if not raw_name:
        raw_id = getattr(top_part, "id", "")
        raw_name = raw_id if isinstance(raw_id, str) else ""
    top_name = str(raw_name)
    named_tenor = "Tenor" in _roles_from_name(top_name)

    explicit_solo_tenor = False
    has_other_top_label = False
    for pi, p in enumerate(parts):
        for m in p.getElementsByClass(stream.Measure):
            for txt in _bar_texts(m):
                if pi == top_index and is_soprano_label(txt):
                    has_other_top_label = True
                sec = section_parser(txt)
                if sec is not None and getattr(sec, "solo", False):
                    if sec.roles == ("Tenor",):
                        explicit_solo_tenor = True
                    elif sec.roles and "Tenor" not in sec.roles:
                        has_other_top_label = True

    if not (named_tenor or explicit_solo_tenor or (inherited_role == "Tenor" and not has_other_top_label)):
        return plan

    plan.active = True
    top_ms = list(top_part.getElementsByClass(stream.Measure))
    sa_ms = list(parts[sa_index].getElementsByClass(stream.Measure))
    tb_ms = list(parts[sa_index + 1].getElementsByClass(stream.Measure)) if sa_index + 1 < len(parts) else []
    n_bars = min(len(lengths), len(top_ms))

    if inherited_role == "Tenor":
        first_choir_lyric = 0
    else:
        first_choir_lyric = n_bars
        for i in range(n_bars):
            sa_ly = _real_lyric_count(sa_ms[i]) if i < len(sa_ms) else 0
            tb_ly = _real_lyric_count(tb_ms[i]) if i < len(tb_ms) else 0
            if sa_ly >= 2 or tb_ly >= 2:
                first_choir_lyric = i
                break

    def _is_multi_bar_accompaniment_block(idx: int) -> bool:
        for nb in (idx - 1, idx + 1):
            if 0 <= nb < n_bars:
                nb_top_ly = _real_lyric_count(top_ms[nb])
                nb_sa_ly = _real_lyric_count(sa_ms[nb]) if nb < len(sa_ms) else 0
                nb_tb_ly = _real_lyric_count(tb_ms[nb]) if nb < len(tb_ms) else 0
                if nb_top_ly >= 2 and nb_sa_ly == 0 and nb_tb_ly == 0:
                    return True
        return False

    for i in range(n_bars):
        top_sounding = any(_sounding(el) for el in top_ms[i].recurse().notesAndRests)
        top_ly = _real_lyric_count(top_ms[i])
        sa_ly = _real_lyric_count(sa_ms[i]) if i < len(sa_ms) else 0
        tb_ly = _real_lyric_count(tb_ms[i]) if i < len(tb_ms) else 0
        sa_acc = _has_accompaniment_cues(sa_ms[i]) if i < len(sa_ms) else False

        if i < first_choir_lyric:
            if not top_sounding:
                plan.bars[i] = (
                    "intro_wait",
                    "intro accompaniment before choir/solo entry -> waiting-time rests",
                )
            else:
                plan.bars[i] = (
                    "solo_only",
                    "solo Tenor over keyboard accompaniment -> Tenor sings, S/A/B wait",
                )
        elif top_sounding:
            if top_ly >= 2 and (
                (sa_ly <= 1 and sa_acc)
                or (sa_ly == 0 and tb_ly == 0 and _is_multi_bar_accompaniment_block(i))
            ):
                plan.bars[i] = (
                    "solo_only",
                    "solo Tenor phrase over keyboard accompaniment -> Tenor sings, S/A/B wait",
                )
            else:
                plan.bars[i] = (
                    "overlay",
                    "solo Tenor + choir Tutti -> Tenor overlaid with choir Tenor",
                )

    return plan

