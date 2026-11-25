"""Batch 18: smart 3+ note simultaneous chord routing & Option B monophonic collapse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from music21 import chord, note, pitch as m21pitch

from satb_splitter.model.config import DivisiChoice, DivisiMode, SplitConfig

EPS = 1e-6

# Comfortable choir vocal ranges (MIDI note numbers).
COMFORT_RANGES: dict[str, tuple[int, int]] = {
    "Soprano": (60, 79),  # C4 .. G5
    "Alto": (55, 72),     # G3 .. C5
    "Tenor": (48, 67),    # C3 .. G4
    "Bass": (40, 60),     # E2 .. C4
}


def chord_stem(el: note.GeneralNote) -> str | None:
    """Return 'up'/'down' stem direction, inspecting sub-notes on Chords.

    music21 leaves ``Chord.stemDirection == 'unspecified'`` on many MusicXML
    chords even when every ``<note>`` inside the chord carries ``<stem>up</stem>``.
    """
    sd = getattr(el, "stemDirection", None)
    if sd in ("up", "down"):
        return sd
    if isinstance(el, chord.Chord):
        dirs = {
            getattr(sub, "stemDirection", None)
            for sub in getattr(el, "notes", ())
            if getattr(sub, "stemDirection", None) in ("up", "down")
        }
        if len(dirs) == 1:
            return next(iter(dirs))
    return None


def _ps_label(ps_val: float | None) -> str | None:
    if ps_val is None:
        return None
    try:
        return m21pitch.Pitch(ps=round(float(ps_val), 2)).nameWithOctave
    except Exception:  # noqa: BLE001
        return None


def _out_of_range(role: str, ps_val: float) -> float:
    lo, hi = COMFORT_RANGES.get(role, (40, 84))
    if ps_val < lo:
        return lo - ps_val
    if ps_val > hi:
        return ps_val - hi
    return 0.0


@dataclass
class PitchCandidate:
    """One pitch participating in a simultaneous onset group."""

    pitch: m21pitch.Pitch
    src_el: note.GeneralNote
    stem: str | None = None
    group_target: str | None = None  # "U", "L", or None
    tie_type: str | None = None      # "start", "continue", "stop", or None


@dataclass
class VoiceLeadingState:
    """Running melodic & tie context along one two-voice staff across bars."""

    prev_upper: float | None = None
    prev_lower: float | None = None
    prev_upper_tie_out: bool = False
    prev_lower_tie_out: bool = False
    last_divisi_owner: str | None = None  # "upper" | "lower" | None

    def update(
        self,
        upper_pitches: Iterable[m21pitch.Pitch],
        lower_pitches: Iterable[m21pitch.Pitch],
        *,
        upper_tie_out: bool = False,
        lower_tie_out: bool = False,
    ) -> None:
        ups = list(upper_pitches)
        los = list(lower_pitches)
        if ups:
            self.prev_upper = max(p.ps for p in ups)
            self.prev_upper_tie_out = upper_tie_out
        if los:
            self.prev_lower = min(p.ps for p in los)
            self.prev_lower_tie_out = lower_tie_out


def candidate_tie_type(src_el: note.GeneralNote, p_obj: m21pitch.Pitch) -> str | None:
    """Return tie type ('start'/'continue'/'stop'/None) for ``p_obj`` on ``src_el``."""
    t = getattr(src_el, "tie", None)
    if isinstance(src_el, chord.Chord):
        try:
            t = src_el.getTie(p_obj) or t
        except Exception:  # noqa: BLE001
            pass
    return getattr(t, "type", None) if t is not None else None


def _avg_dist(ps_val: float, prev_ps: float | None, next_ps: float | None) -> float | None:
    vals = [abs(ps_val - x) for x in (prev_ps, next_ps) if x is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _select_tied(
    pool: list[PitchCandidate],
    prev_ps: float | None,
    prev_tie_out: bool,
) -> PitchCandidate | None:
    if prev_ps is None:
        return None
    for cand in pool:
        if abs(cand.pitch.ps - prev_ps) < 0.05 and (
            prev_tie_out or cand.tie_type in ("stop", "continue")
        ):
            return cand
    return None


def _collapse_upper(
    pool: list[PitchCandidate],
    upper_role: str,
    state: VoiceLeadingState,
) -> tuple[list[PitchCandidate], bool]:
    """Option B monophonic collapse for upper voice holding ``pool`` (high->low)."""
    if len(pool) <= 1:
        return list(pool), False
    tied = _select_tied(pool, state.prev_upper, state.prev_upper_tie_out)
    if tied is not None:
        return [tied], False
    ceil = COMFORT_RANGES.get(upper_role, (40, 84))[1]
    outer = pool[0]
    if outer.pitch.ps > ceil + 0.01:
        for cand in pool[1:]:
            if cand.pitch.ps <= ceil + 0.01:
                return [cand], True
        return [pool[-1]], True
    return [outer], False


def _collapse_lower(
    pool: list[PitchCandidate],
    lower_role: str,
    state: VoiceLeadingState,
) -> tuple[list[PitchCandidate], bool]:
    """Option B monophonic collapse for lower voice holding ``pool`` (high->low)."""
    if len(pool) <= 1:
        return list(pool), False
    tied = _select_tied(pool, state.prev_lower, state.prev_lower_tie_out)
    if tied is not None:
        return [tied], False
    floor = COMFORT_RANGES.get(lower_role, (36, 72))[0]
    outer = pool[-1]
    alt = pool[0]
    if outer.pitch.ps < floor - 0.01:
        for cand in reversed(pool[:-1]):
            if cand.pitch.ps >= floor - 0.01:
                return [cand], True
        return [alt], True
    if state.prev_lower is not None:
        d_alt = abs(state.prev_lower - alt.pitch.ps)
        d_out = abs(state.prev_lower - outer.pitch.ps)
        if d_alt + 2.0 <= d_out:
            return [alt], False
    return [outer], False


def _lookup_override(
    overrides: dict[str, str],
    measure: int,
    upper_role: str,
    offset: float,
    source_measure_label: str | None = None,
) -> str | None:
    if not overrides:
        return None
    off_r = round(float(offset), 4)
    keys = [f"{measure}|{upper_role}|{off_r}"]
    if source_measure_label:
        keys.append(f"{source_measure_label}|{upper_role}|{off_r}")
    for k in keys:
        val = overrides.get(k)
        if val and val != "auto":
            return val
    return None


def _split_4plus_keep(cands: list[PitchCandidate]) -> tuple[list[PitchCandidate], list[PitchCandidate], bool]:
    """Split 4+ note chord cleanly into upper half and lower half (no 3+1 snowballing)."""
    # If explicit U vs L voice/stem split exists with no crossing, preserve it.
    up_group = [c for c in cands if c.stem == "up" or (c.stem is None and c.group_target == "U")]
    dn_group = [c for c in cands if c.stem == "down" or (c.stem is None and c.group_target == "L")]
    if (
        up_group
        and dn_group
        and len(up_group) + len(dn_group) == len(cands)
        and min(c.pitch.ps for c in up_group) >= max(c.pitch.ps for c in dn_group) - 0.01
    ):
        return up_group, dn_group, False
    crossing = bool(
        up_group
        and dn_group
        and min(c.pitch.ps for c in up_group) < max(c.pitch.ps for c in dn_group) - 0.01
    )
    mid = (len(cands) + 1) // 2
    return cands[:mid], cands[mid:], crossing


def resolve_simultaneous_chord(
    candidates_desc: list[PitchCandidate],
    *,
    measure: int,
    part_index: int,
    offset: float,
    upper_role: str,
    lower_role: str,
    state: VoiceLeadingState,
    next_upper_ps: float | None = None,
    next_lower_ps: float | None = None,
    cfg: SplitConfig,
    source_measure_label: str | None = None,
) -> tuple[list[PitchCandidate], list[PitchCandidate], DivisiChoice]:
    """Score middle-note ownership and realize 3+ note simultaneous chord per DivisiMode."""
    assert len(candidates_desc) >= 3
    hi = candidates_desc[0]
    lo = candidates_desc[-1]
    mids = candidates_desc[1:-1]
    m_rep = mids[len(mids) // 2]

    # 1) Tie cue (±10.0)
    tie_cue = 0.0
    for mc in mids:
        if state.prev_upper is not None and abs(mc.pitch.ps - state.prev_upper) < 0.05 and (
            state.prev_upper_tie_out or mc.tie_type in ("stop", "continue")
        ):
            tie_cue = 10.0
            break
        if state.prev_lower is not None and abs(mc.pitch.ps - state.prev_lower) < 0.05 and (
            state.prev_lower_tie_out or mc.tie_type in ("stop", "continue")
        ):
            tie_cue = -10.0
            break

    # 2) Stem / voice distinction cue (±6.0)
    def _side(c: PitchCandidate) -> str | None:
        if c.stem == "up":
            return "U"
        if c.stem == "down":
            return "L"
        return c.group_target

    sides = {_side(c) for c in candidates_desc if _side(c) is not None}
    has_stem_distinction = sides == {"U", "L"}
    stem_cue = 0.0
    source_crossing = False
    if has_stem_distinction:
        u_ps = [c.pitch.ps for c in candidates_desc if _side(c) == "U"]
        l_ps = [c.pitch.ps for c in candidates_desc if _side(c) == "L"]
        if u_ps and l_ps and min(u_ps) < max(l_ps) - 0.01:
            source_crossing = True
        mid_sides = {_side(mc) for mc in mids if _side(mc) is not None}
        if mid_sides == {"U"} and _side(lo) == "L":
            stem_cue = 6.0
        elif mid_sides == {"L"} and _side(hi) == "U":
            stem_cue = -6.0

    # 3) Voice-leading continuity cue (±3.0)
    vl_cue = 0.0
    d_u = _avg_dist(m_rep.pitch.ps, state.prev_upper, next_upper_ps)
    d_l = _avg_dist(m_rep.pitch.ps, state.prev_lower, next_lower_ps)
    if d_u is not None and d_l is not None:
        diff = d_l - d_u
        if abs(diff) > 0.25:
            vl_cue = max(-3.0, min(3.0, diff * 0.75))

    # 4) SATB vocal comfort ranges (±2.0)
    comfort_cue = 0.0
    pen_u = _out_of_range(upper_role, m_rep.pitch.ps)
    pen_l = _out_of_range(lower_role, m_rep.pitch.ps)
    if abs(pen_l - pen_u) > 0.01:
        comfort_cue += max(-2.0, min(2.0, (pen_l - pen_u) * 0.5))
    u_ceil = COMFORT_RANGES.get(upper_role, (40, 84))[1]
    l_floor = COMFORT_RANGES.get(lower_role, (36, 72))[0]
    if hi.pitch.ps > u_ceil + 0.01 and lo.pitch.ps >= l_floor - 0.01:
        comfort_cue = min(2.0, comfort_cue + 1.5)
    elif lo.pitch.ps < l_floor - 0.01 and hi.pitch.ps <= u_ceil + 0.01:
        comfort_cue = max(-2.0, comfort_cue - 1.5)

    # 5) Passage consistency (±1.5)
    passage_cue = 0.0
    if state.last_divisi_owner == "upper":
        passage_cue = 1.5
    elif state.last_divisi_owner == "lower":
        passage_cue = -1.5

    # 6) Anchor semitone proximity tie-breaker (±0.5)
    dist_hi = abs(hi.pitch.ps - m_rep.pitch.ps)
    dist_lo = abs(m_rep.pitch.ps - lo.pitch.ps)
    anchor_cue = 0.5 if dist_hi < dist_lo - 0.01 else (-0.5 if dist_lo < dist_hi - 0.01 else 0.0)

    total_score = tie_cue + stem_cue + vl_cue + comfort_cue + passage_cue + anchor_cue
    auto_owner = "upper" if total_score >= 0.0 else "lower"

    if cfg.divisi_mode == DivisiMode.UPPER_ONLY:
        eff_owner = "upper"
    elif cfg.divisi_mode == DivisiMode.LOWER_ONLY:
        eff_owner = "lower"
    else:
        eff_owner = auto_owner

    override = _lookup_override(
        getattr(cfg, "divisi_overrides", {}) or {},
        measure,
        upper_role,
        offset,
        source_measure_label=source_measure_label,
    )

    comfort_override = False
    if override in ("upper_high", "lower_low"):
        chosen_u, chosen_l = [hi], [lo]
    elif override == "upper_mid":
        chosen_u, chosen_l = [candidates_desc[1]], [lo]
    elif override == "lower_mid":
        chosen_u, chosen_l = [hi], [candidates_desc[-2]]
    elif override == "keep_upper":
        chosen_u, chosen_l = candidates_desc[:-1], [lo]
    elif override == "keep_lower":
        chosen_u, chosen_l = [hi], candidates_desc[1:]
    elif override == "keep_both":
        chosen_u, chosen_l, _ = _split_4plus_keep(candidates_desc)
    elif cfg.divisi_mode == DivisiMode.KEEP_DIVISI:
        if len(candidates_desc) >= 4:
            chosen_u, chosen_l, _ = _split_4plus_keep(candidates_desc)
            auto_owner = "both"
        elif eff_owner == "upper":
            chosen_u, chosen_l = candidates_desc[:-1], [lo]
        else:
            chosen_u, chosen_l = [hi], candidates_desc[1:]
    else:
        # Option B monophonic collapse (MONOPHONIC_AUTO / UPPER_ONLY / LOWER_ONLY)
        if len(candidates_desc) >= 4:
            u_pool, l_pool, _ = _split_4plus_keep(candidates_desc)
            auto_owner = "both"
        elif eff_owner == "upper":
            u_pool, l_pool = candidates_desc[:-1], [lo]
        else:
            u_pool, l_pool = [hi], candidates_desc[1:]
        chosen_u, c_u_over = _collapse_upper(u_pool, upper_role, state)
        chosen_l, c_l_over = _collapse_lower(l_pool, lower_role, state)
        comfort_override = c_u_over or c_l_over

    # Enforce zero output crossings regardless of mode.
    if chosen_u and chosen_l:
        min_u = min(c.pitch.ps for c in chosen_u)
        max_l = max(c.pitch.ps for c in chosen_l)
        if min_u < max_l - 0.01:
            chosen_u = [hi]
            chosen_l = [lo]

    reasons: list[str] = []
    if abs(tie_cue) >= 5.0:
        reasons.append("tie continuity")
    if has_stem_distinction and abs(stem_cue) > 0:
        reasons.append("distinct stems/voices")
    elif not has_stem_distinction:
        reasons.append("shared/no stem distinction")
    if source_crossing:
        reasons.append("source crossing avoided")
    if comfort_override:
        reasons.append("comfort range override")
    if abs(vl_cue) >= 0.5:
        reasons.append("voice-leading")
    if abs(passage_cue) > 0 and not has_stem_distinction:
        reasons.append("passage consistency")
    if override:
        reasons.append(f"override={override}")
    if not reasons:
        reasons.append("pitch proximity")

    ambiguous = (
        (not has_stem_distinction and abs(tie_cue) < 5.0)
        or comfort_override
        or source_crossing
        or (len(candidates_desc) >= 4 and not has_stem_distinction)
    )
    confidence = round(min(1.0, abs(total_score) / 10.0), 2)
    if ambiguous and confidence > 0.75:
        confidence = 0.65

    if override is None and eff_owner in ("upper", "lower"):
        state.last_divisi_owner = eff_owner

    choice = DivisiChoice(
        measure=measure,
        part_index=part_index,
        offset=round(float(offset), 4),
        upper_role=upper_role,
        lower_role=lower_role,
        pitches=[c.pitch.nameWithOctave for c in candidates_desc],
        auto_owner=auto_owner,
        chosen_upper=[c.pitch.nameWithOctave for c in chosen_u],
        chosen_lower=[c.pitch.nameWithOctave for c in chosen_l],
        ambiguous=ambiguous,
        confidence=confidence,
        reason="; ".join(reasons),
        prev_upper=_ps_label(state.prev_upper),
        next_upper=_ps_label(next_upper_ps),
        prev_lower=_ps_label(state.prev_lower),
        next_lower=_ps_label(next_lower_ps),
        choice_key=f"{measure}|{upper_role}|{round(float(offset), 4)}",
        raw_measure=measure,
    )
    return chosen_u, chosen_l, choice


def no_cross_line_order(
    pitch: m21pitch.Pitch,
    off: float,
    upper,
    lower,
    prefer: str | None,
) -> list[tuple[str, object]]:
    """Order candidate lines for ``pitch`` without forcing a voice crossing."""
    u_item = upper.starting_at(off)
    l_item = lower.starting_at(off)
    if u_item is not None and l_item is not None:
        u_min = min(p.ps for p in _item_pitches(u_item[2]))
        l_max = max(p.ps for p in _item_pitches(l_item[2]))
        if prefer == "U" and pitch.ps < l_max - 0.01:
            return [("lower", lower)]
        if prefer == "L" and pitch.ps > u_min + 0.01:
            return [("upper", upper)]
    if prefer == "U":
        return [("upper", upper), ("lower", lower)]
    if prefer == "L":
        return [("lower", lower), ("upper", upper)]
    return [("upper", upper), ("lower", lower)]


def _item_pitches(n) -> list:
    if isinstance(n, chord.Chord):
        return list(n.pitches)
    return [n.pitch]


def anchor_pitch_distance(item_el, pitch: m21pitch.Pitch, label: str) -> float:
    """Measure distance against outer anchor pitch (never snowballing onto middle divisi)."""
    ps = _item_pitches(item_el)
    if label == "upper":
        anchor = max(ps, key=lambda p: p.ps)
    elif label == "lower":
        anchor = min(ps, key=lambda p: p.ps)
    else:
        anchor = ps[0]
    return abs(anchor.ps - pitch.ps)
