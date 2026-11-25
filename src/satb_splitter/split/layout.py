"""Automatic staff-layout detection for SATB splitting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from music21 import chord, stream
from music21 import clef as m21clef

from satb_splitter.model.config import LayoutTemplate, StaffPair, VoiceRole


@dataclass(frozen=True)
class LayoutDetection:
    """Result of auto-detecting how source parts map to SATB."""

    template: LayoutTemplate
    reason: str
    part_count: int
    part_names: tuple[str, ...]
    clef_kinds: tuple[str | None, ...] = field(default_factory=tuple)


_VOICE_ALIASES: dict[str, VoiceRole] = {
    "s": "Soprano",
    "sop": "Soprano",
    "soprano": "Soprano",
    "a": "Alto",
    "alt": "Alto",
    "alto": "Alto",
    "t": "Tenor",
    "ten": "Tenor",
    "tenor": "Tenor",
    "b": "Bass",
    "bas": "Bass",
    "bass": "Bass",
    "basso": "Bass",
}

# Staff kinds that can carry the lower (T/B) staff of a choir pair.
_LOWER_KINDS = ("F", "G8")


# ---------------------------------------------------------------------------
# Clef helpers (also used by the split engine)
# ---------------------------------------------------------------------------

def clef_kind(c) -> str | None:
    """Classify a music21 clef: 'G' treble, 'G8' treble 8vb, 'F' bass, 'C', or None."""
    if c is None:
        return None
    sign = getattr(c, "sign", None)
    octave = getattr(c, "octaveChange", 0) or 0
    if sign == "F":
        return "F"
    if sign == "G":
        return "G8" if octave < 0 else "G"
    if sign == "C":
        return "C"
    return None


def measure_clef_kinds(part: stream.Part) -> list[str | None]:
    """Clef kind in effect at the start of each measure (None if never set)."""
    current = None
    for el in part.getElementsByClass(m21clef.Clef):
        current = clef_kind(el)
    out: list[str | None] = []
    for m in part.getElementsByClass(stream.Measure):
        clefs = sorted(m.getElementsByClass(m21clef.Clef), key=lambda c: float(c.offset))
        start = current
        for c in clefs:
            if float(c.offset) <= 1e-6:
                start = clef_kind(c)
        out.append(start)
        if clefs:
            current = clef_kind(clefs[-1])
    return out


def staff_kind(part: stream.Part) -> str | None:
    """Dominant clef kind of a staff, counted over measures that contain notes."""
    kinds = measure_clef_kinds(part)
    sounding: dict[str, int] = {}
    anybar: dict[str, int] = {}
    for m, k in zip(part.getElementsByClass(stream.Measure), kinds):
        if not k:
            continue
        anybar[k] = anybar.get(k, 0) + 1
        if any(True for _ in m.recurse().notes):
            sounding[k] = sounding.get(k, 0) + 1
    counts = sounding or anybar
    if not counts:
        # No measures: fall back to part-level clef
        for el in part.getElementsByClass(m21clef.Clef):
            return clef_kind(el)
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def first_sounding_kind(part: stream.Part) -> str | None:
    """Clef kind of the first measure that contains notes (None if none)."""
    for m, k in zip(part.getElementsByClass(stream.Measure), measure_clef_kinds(part)):
        if any(True for _ in m.recurse().notes):
            return k
    return None


def _lyric_count(part: stream.Part) -> int:
    return sum(1 for n in part.recurse().notes if getattr(n, "lyrics", None))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_layout(score: stream.Score) -> LayoutDetection:
    """Choose an SATB layout template from part names, clefs, and part count.

    Order of rules:

    1. Part names such as ``SA``, ``T,B``, ``Soprano`` (name-based mapping).
    2. Clef-based choir detection (when every staff has a clef):

       * ``[G, G8|F|C, F]``        -> SA / T / B
       * ``[G, G, G8|F|C, F]``     -> open score S / A / T / B
       * otherwise find the **choir pair** = a treble staff directly followed
         by a bass (or treble-8vb) staff -> SA / TB. Staves above it (solo /
         trio lines printed above the choir) and below it are **ignored** and
         reported. If several pairs exist, the one with the most lyrics wins
         (choir over piano), ties -> the lowest pair.
       * single staff: bass clef -> T/B, treble-8vb (at its first notes) ->
         Tenor, treble -> S/A. Printed markers on a single staff (``unison``,
         ``S.A.T.``, ``solo tenor`` ...) are applied per bar by the engine.

    3. Fallback by raw part count (1 -> S+A, 2 -> SA/TB, 3 -> SA/T/B, 4 -> open,
       5+ -> first four open).
    """
    parts = list(score.parts)
    n = len(parts)
    names = tuple(_part_label(p) for p in parts)
    kinds = tuple(staff_kind(p) for p in parts)

    named = _try_name_based(names, kinds)
    if named is not None:
        completed, why = _complete_name_based(named, parts, names, kinds)
        if completed is not None:
            return LayoutDetection(
                template=completed,
                reason=f"part names {list(names)} incomplete; {why}",
                part_count=n,
                part_names=names,
                clef_kinds=kinds,
            )
        return LayoutDetection(
            template=named,
            reason=f"part names {list(names)}",
            part_count=n,
            part_names=names,
            clef_kinds=kinds,
        )

    if n > 0 and all(k is not None for k in kinds):
        by_clef = _try_clef_based(parts, kinds, names)
        if by_clef is not None:
            tpl, why = by_clef
            return LayoutDetection(
                template=tpl,
                reason=f"clefs {list(kinds)} -> {why}",
                part_count=n,
                part_names=names,
                clef_kinds=kinds,
            )

    return _count_based(n, names, kinds)


def _has_lower_clef(part: stream.Part) -> bool:
    return any(k in _LOWER_KINDS for k in measure_clef_kinds(part))


def _try_clef_based(
    parts: list[stream.Part],
    kinds: tuple[str | None, ...],
    names: tuple[str, ...] = (),
) -> tuple[LayoutTemplate, str] | None:
    n = len(parts)
    names = names or tuple(_part_label(p) for p in parts)
    lowerish = ("G8", "F", "C")

    if n == 1:
        k = kinds[0]
        if first_sounding_kind(parts[0]) == "G8":
            # A tenor line (often a solo) whose later systems Audiveris read
            # as plain treble: still a Tenor staff, never S/A.
            k = "G8"
        if k == "F":
            return (
                LayoutTemplate(
                    name="single_staff_tb",
                    description="Single bass staff split into T+B",
                    pairs=[StaffPair(0, "Tenor", "Bass")],
                ),
                "single bass staff -> T/B (S/A padded with rests)",
            )
        if k == "G8":
            return (
                LayoutTemplate(
                    name="single_staff_tenor",
                    description="Single treble-8vb staff -> Tenor",
                    pairs=[StaffPair(0, "Tenor", None)],
                ),
                "single treble-8vb staff -> Tenor (others padded with rests)",
            )
        return (
            LayoutTemplate.single_staff_sa(),
            "single treble staff -> S/A (T/B padded with rests)",
        )

    if n == 2 and kinds[0] == "G" and kinds[1] == "G":
        two = _two_treble_staves(parts)
        if two is not None:
            return two

    if n == 3 and kinds[0] == "G" and kinds[1] in lowerish and kinds[2] == "F":
        return LayoutTemplate.three_staff_satb(), "three_staff_satb (SA / T / B)"

    if (
        n == 4
        and kinds[0] == "G"
        and kinds[1] == "G"
        and kinds[2] in lowerish
        and kinds[3] == "F"
        and not any(_is_instrument_name(nm) for nm in names)
    ):
        return LayoutTemplate.four_staff_open(), "four_staff_open (S / A / T / B)"

    candidates = [
        i for i in range(n - 1)
        if kinds[i] == "G" and (kinds[i + 1] in _LOWER_KINDS or _has_lower_clef(parts[i + 1]))
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        best = candidates[0]
    else:
        def _pair_score(i: int) -> tuple[int, int, int, int, int, int]:
            nm_u = names[i] if i < len(names) else ""
            nm_l = names[i + 1] if i + 1 < len(names) else ""
            non_instr = 0 if (_is_instrument_name(nm_u) or _is_instrument_name(nm_l)) else 1
            choir_named = 1 if (_is_choir_name(nm_u) or _is_choir_name(nm_l)) else 0
            lyrics = _lyric_count(parts[i]) + _lyric_count(parts[i + 1])
            has_lyrics = 1 if lyrics > 0 else 0
            dom_lower = 1 if kinds[i + 1] in _LOWER_KINDS else 0
            return (non_instr, choir_named, has_lyrics, dom_lower, lyrics, i)

        best = max(candidates, key=_pair_score)
    ignore = [i for i in range(n) if i not in (best, best + 1)]
    if n == 2 and best == 0:
        return LayoutTemplate.two_staff_satb(), "two_staff_satb (SA / TB)"
    tpl = LayoutTemplate(
        name="choir_pair",
        description=f"Choir SA=staff {best}, TB=staff {best + 1}; ignore {ignore}",
        pairs=[
            StaffPair(best, "Soprano", "Alto"),
            StaffPair(best + 1, "Tenor", "Bass"),
        ],
        ignore_part_indices=ignore,
    )
    above = [i for i in ignore if i < best]
    below = [i for i in ignore if i > best + 1]
    bits = [f"choir SA=staff {best}, TB=staff {best + 1}"]
    if above:
        bits.append(f"solo/trio staves {above} above choir ignored")
    if below:
        bits.append(f"staves {below} below choir ignored")
    return tpl, "; ".join(bits)


def _has_sounding(part: stream.Part) -> bool:
    return any(not n.duration.isGrace for n in part.recurse().notes)


def _two_treble_staves(parts: list[stream.Part]) -> tuple[LayoutTemplate, str] | None:
    """Two staves that are both (mostly) treble, e.g. Audiveris "Voice"/"Voice".

    * The lower staff has bass-clef systems with notes -> it IS the T/B staff
      (its treble runs are single-staff systems routed per bar by the engine).
    * Only one staff has notes -> that staff is a women's / single treble
      system -> S/A (never T/B because of its position).
    * Both have notes, no bass clef anywhere -> upper staff = Soprano,
      lower staff = Alto (women's open score).
    """
    lower_kinds = measure_clef_kinds(parts[1])
    lower_ms = list(parts[1].getElementsByClass(stream.Measure))
    if any(k in ("F", "G8") and any(True for _ in m.recurse().notes)
           for m, k in zip(lower_ms, lower_kinds)):
        return LayoutTemplate.two_staff_satb(), (
            "two_staff_satb (SA / TB): lower staff has bass-clef systems; "
            "its treble systems are routed per bar")
    has = [_has_sounding(p) for p in parts]
    if has[0] != has[1]:
        idx = 0 if has[0] else 1
        tpl = LayoutTemplate(
            name="treble_only_sa",
            description=f"Two treble staves, only staff {idx} has notes -> S/A",
            pairs=[StaffPair(idx, "Soprano", "Alto")],
            ignore_part_indices=[1 - idx],
        )
        return tpl, f"two treble staves, staff {1 - idx} empty -> staff {idx} = S/A (treble only, not T/B)"
    tpl = LayoutTemplate(
        name="treble_pair_sa",
        description="Two treble staves -> Soprano (upper) / Alto (lower)",
        pairs=[StaffPair(0, "Soprano", None), StaffPair(1, "Alto", None)],
    )
    return tpl, "two treble staves -> S (staff 0) / A (staff 1), no T/B staff"


def _count_based(n: int, names: tuple[str, ...], kinds: tuple) -> LayoutDetection:
    def det(tpl: LayoutTemplate, reason: str) -> LayoutDetection:
        return LayoutDetection(
            template=tpl, reason=reason, part_count=n, part_names=names, clef_kinds=kinds
        )

    if n <= 0:
        return det(LayoutTemplate.two_staff_satb(), "no parts; default two_staff_satb")
    if n == 1:
        return det(LayoutTemplate.single_staff_sa(), "1 part -> single_staff_sa")
    if n == 2:
        return det(LayoutTemplate.two_staff_satb(), "2 parts -> two_staff_satb (SA / TB)")
    if n == 3:
        return det(LayoutTemplate.three_staff_satb(), "3 parts -> three_staff_satb (SA / T / B)")
    if n == 4:
        return det(LayoutTemplate.four_staff_open(), "4 parts -> four_staff_open")
    tpl = LayoutTemplate.four_staff_open()
    tpl = LayoutTemplate(
        name="four_staff_open_ignore_extra",
        description=f"Four-staff open; ignore parts {list(range(4, n))}",
        pairs=list(tpl.pairs),
        ignore_part_indices=list(range(4, n)),
    )
    return det(tpl, f"{n} parts -> four_staff_open, ignore extras {list(range(4, n))}")


def _part_label(part: stream.Part) -> str:
    for attr in ("partName", "partAbbreviation", "id"):
        val = getattr(part, attr, None)
        if val:
            return str(val)
    return ""


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _tokens(name: str) -> list[str]:
    """Split a part name into voice-ish tokens: 'T,B' -> ['t','b']."""
    n = _normalize_name(name)
    n = n.replace("&", ",").replace("+", ",").replace("/", ",").replace("-", ",")
    raw = re.split(r"[\s,;|]+", n)
    return [t for t in raw if t]


def _roles_from_name(name: str) -> list[VoiceRole]:
    roles: list[VoiceRole] = []
    for tok in _tokens(name):
        key = re.sub(r"[^a-z]", "", tok)
        if key in _VOICE_ALIASES:
            role = _VOICE_ALIASES[key]
            if role not in roles:
                roles.append(role)
            continue
        if key in ("sa", "as"):
            for r in ("Soprano", "Alto"):
                if r not in roles:
                    roles.append(r)  # type: ignore[arg-type]
        elif key in ("tb", "bt"):
            for r in ("Tenor", "Bass"):
                if r not in roles:
                    roles.append(r)  # type: ignore[arg-type]
        elif key == "satb":
            for r in ("Soprano", "Alto", "Tenor", "Bass"):
                if r not in roles:
                    roles.append(r)  # type: ignore[arg-type]
    return roles


def _try_name_based(
    names: tuple[str, ...],
    kinds: tuple[str | None, ...] = (),
) -> LayoutTemplate | None:
    """Build a template from part names when they clearly encode SATB roles.

    Returns None if names are uninformative (e.g. all 'Piano' / 'Voice').
    """
    if not names or not any(names):
        return None

    per_part: list[list[VoiceRole]] = [_roles_from_name(n) for n in names]
    if not any(per_part):
        return None

    informative = sum(1 for r in per_part if r)
    if informative == 0:
        return None

    pairs: list[StaffPair] = []
    assigned_sa_from_full = False
    for idx, roles in enumerate(per_part):
        if not roles:
            continue
        if len(roles) == 1:
            pairs.append(StaffPair(idx, roles[0], None))
        elif len(roles) > 2:
            if idx < len(kinds) and kinds[idx] in _LOWER_KINDS:
                pairs.append(StaffPair(idx, "Tenor", "Bass"))
            elif assigned_sa_from_full and len(names) == 2:
                pairs.append(StaffPair(idx, "Tenor", "Bass"))
            else:
                pairs.append(StaffPair(idx, "Soprano", "Alto"))
                assigned_sa_from_full = True
        else:
            pairs.append(StaffPair(idx, roles[0], roles[1]))

    if not pairs:
        return None

    multi = any(len(r) >= 2 for r in per_part)
    if not multi and informative < 2:
        return None

    ignore = [i for i, r in enumerate(per_part) if not r]
    return LayoutTemplate(
        name="name_based",
        description=f"From part names: {list(names)}",
        pairs=pairs,
        ignore_part_indices=ignore,
    )


# Names that mark a staff as accompaniment (never a choir voice).
_INSTRUMENT_RE = re.compile(
    r"(pian|organ|orga|keyb|clav|harm|acc|pno|pf\b|guit|chitar|instr|bass ?line|cello|violin)"
)


_CHOIR_RE = re.compile(r"\b(?:cor|choir|chor|choral|satb)\b")

_SOLO_OR_CHILD_RE = re.compile(r"\b(?:solo|descant|child|children|copii|trio)\b")


def _is_instrument_name(name: str) -> bool:
    return bool(_INSTRUMENT_RE.search(_normalize_name(name)))


def _is_choir_name(name: str) -> bool:
    return bool(_CHOIR_RE.search(_normalize_name(name)))


def _is_solo_or_child_name(name: str) -> bool:
    return bool(_SOLO_OR_CHILD_RE.search(_normalize_name(name)))


def _template_roles(tpl: LayoutTemplate) -> set[str]:
    out: set[str] = set()
    for p in tpl.pairs:
        if p.part_index in tpl.ignore_part_indices:
            continue
        out.update(p.roles())
    return out


def _is_polyphonic_two_voice_staff(part: stream.Part) -> bool:
    """Return True if a staff frequently carries two simultaneous voices/pitches."""
    sounding_bars = 0
    poly_bars = 0
    for m in part.getElementsByClass(stream.Measure):
        notes = [
            el for el in m.recurse().notes
            if not el.duration.isGrace and getattr(el, "pitches", None)
        ]
        if not notes:
            continue
        sounding_bars += 1

        voices_with_notes = sum(
            1
            for v in m.getElementsByClass(stream.Voice)
            if any(
                not el.duration.isGrace and getattr(el, "pitches", None)
                for el in v.recurse().notes
            )
        )
        if voices_with_notes >= 2:
            poly_bars += 1
            continue

        if any(
            isinstance(el, chord.Chord) and len({round(p.ps, 2) for p in el.pitches}) >= 2
            for el in notes
        ):
            poly_bars += 1
            continue

        onsets: dict[float, set[float]] = {}
        for el in notes:
            off = round(float(el.getOffsetInHierarchy(m)), 4)
            ps_set = onsets.setdefault(off, set())
            for p in el.pitches:
                ps_set.add(round(p.ps, 2))
        if any(len(ps_set) >= 2 for ps_set in onsets.values()):
            poly_bars += 1

    return sounding_bars >= 3 and poly_bars >= 3 and (poly_bars / sounding_bars) >= 0.25


def _complete_name_based(
    named: LayoutTemplate,
    parts: list[stream.Part],
    names: tuple[str, ...],
    kinds: tuple[str | None, ...],
) -> tuple[LayoutTemplate | None, str]:
    """Fill SATB roles that part names left empty, using clef + position.

    Audiveris names staves from the printed label; when that label is lost
    (white-out, OCR miss) the staff becomes e.g. ``"Voice"`` while the staff
    below is still ``"T,B"``. Such an **unrecognised, non-instrument** staff is
    not ignored: a treble staff directly above a T/B staff becomes S/A, a
    bass / treble-8vb staff directly below an S/A staff becomes T/B. If that
    is not enough, clef-based detection is used when it covers more roles.

    Returns (template, reason) or (None, "") when the name-based template is
    already complete or nothing better is found.
    """
    if (
        len(parts) == 2
        and len(named.pairs) == 2
        and len(kinds) == 2
        and kinds[0] == "G"
        and (kinds[1] in _LOWER_KINDS or _has_lower_clef(parts[1]))
    ):
        pair_by_idx = {
            p.part_index: p
            for p in named.pairs
            if p.part_index not in named.ignore_part_indices
        }
        p0 = pair_by_idx.get(0)
        p1 = pair_by_idx.get(1)
        if (
            p0 is not None
            and p1 is not None
            and len(p0.roles()) == 1
            and len(p1.roles()) == 1
            and p0.roles()[0] in {"Soprano", "Alto"}
            and p1.roles()[0] in {"Tenor", "Bass"}
            and (_is_polyphonic_two_voice_staff(parts[0]) or _is_polyphonic_two_voice_staff(parts[1]))
        ):
            return (
                LayoutTemplate.two_staff_satb(),
                f"truncated single-voice names {list(names)} on polyphonic 2-staff G/F score -> two_staff_satb (SA / TB)",
            )

    have = _template_roles(named)
    assigned_role_count = sum(
        len(p.roles())
        for p in named.pairs
        if p.part_index not in named.ignore_part_indices
    )
    has_duplicates = assigned_role_count > len(have)
    missing = [r for r in ("Soprano", "Alto", "Tenor", "Bass") if r not in have]
    unnamed = [
        i for i in named.ignore_part_indices
        if i < len(names) and not _roles_from_name(names[i]) and not _is_instrument_name(names[i])
    ]
    if not missing and not has_duplicates:
        return None, ""
    if not unnamed and not has_duplicates:
        return None, ""

    by_index = {p.part_index: p for p in named.pairs}
    pairs = list(named.pairs)
    ignore = list(named.ignore_part_indices)
    filled: list[str] = []
    consumed: set[int] = set()
    for i in unnamed:
        if i in consumed:
            continue
        k = kinds[i] if i < len(kinds) else None
        if (
            i + 1 in unnamed
            and (i + 1) not in consumed
            and k == "G"
            and (kinds[i + 1] if i + 1 < len(kinds) else None) == "G"
            and not _is_solo_or_child_name(names[i])
            and not _is_solo_or_child_name(names[i + 1])
            and "Soprano" in missing
            and "Alto" in missing
        ):
            below2 = by_index.get(i + 2)
            # Only split two adjacent unnamed G staves into S / A when i+2 is
            # a single-voice Tenor staff (4-staff open [S, A, T, B]); never on
            # 3-staff [solo, SA, TB] where i+2 already holds Tenor+Bass.
            if below2 is not None and below2.roles() == ["Tenor"]:
                pairs.append(StaffPair(i, "Soprano", None))
                pairs.append(StaffPair(i + 1, "Alto", None))
                missing = [r for r in missing if r not in ("Soprano", "Alto")]
                filled.append(
                    f"staves {i},{i + 1} ('{names[i]}','{names[i + 1]}') "
                    "(open treble pair above Tenor) -> S / A"
                )
                ignore.remove(i)
                ignore.remove(i + 1)
                consumed.update((i, i + 1))
                continue
        below = by_index.get(i + 1)
        above = by_index.get(i - 1)
        if (
            k == "G"
            and below is not None
            and set(below.roles()) & {"Tenor", "Bass"}
            and "Soprano" in missing and "Alto" in missing
        ):
            pairs.append(StaffPair(i, "Soprano", "Alto"))
            missing = [r for r in missing if r not in ("Soprano", "Alto")]
            filled.append(f"staff {i} '{names[i]}' (treble above T/B) -> S/A")
        elif (
            k in _LOWER_KINDS
            and above is not None
            and set(above.roles()) & {"Soprano", "Alto"}
            and "Tenor" in missing and "Bass" in missing
        ):
            pairs.append(StaffPair(i, "Tenor", "Bass"))
            missing = [r for r in missing if r not in ("Tenor", "Bass")]
            filled.append(f"staff {i} '{names[i]}' ({k} below S/A) -> T/B")
        else:
            continue
        ignore.remove(i)
        consumed.add(i)

    if filled:
        pairs.sort(key=lambda p: p.part_index)
        tpl = LayoutTemplate(
            name="name_based_completed",
            description=f"From part names {list(names)} + clef/position for unnamed staves",
            pairs=pairs,
            ignore_part_indices=ignore,
        )
        return tpl, "clef/position fallback: " + "; ".join(filled)

    if parts and all(k is not None for k in kinds):
        by_clef = _try_clef_based(parts, kinds, names)
        if by_clef is not None and len(_template_roles(by_clef[0])) > len(have):
            return by_clef[0], f"clef fallback {list(kinds)} -> {by_clef[1]}"
    return None, ""
