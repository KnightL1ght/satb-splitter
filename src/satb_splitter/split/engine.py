"""Core SATB voice-separation engine built on music21.



Every output voice is rebuilt measure by measure as a strictly *monophonic*

line:



* Notes keep their original onset inside the bar (simultaneous notes are never

  serialized into consecutive time).

* MusicXML voices are matched by **pitch within the staff/measure**: the

  voice with the higher average pitch becomes the upper role (S / T), the

  lower one the lower role (A / B). Audiveris voice numbers (1/2, 5/6, 3/4 ...)

  and their order are NOT trusted - Audiveris often numbers the lower line

  first. Stem direction breaks ties (unisons, one sounding voice). Extra

  voices are merged by pitch into whichever line is free at that beat.

* Single-staff sections (a treble-clef system Audiveris attached to the

  bass staff, or a one-staff page) are routed by their printed marker:

  ``unison`` -> all four voices (T/B an octave lower), ``S.A`` -> S/A,

  ``S.A.T.`` -> S/A/T, ``solo tenor`` -> Tenor, plain ``solo`` -> omitted

  (flagged). Unmarked single-staff treble systems default to S/A (flagged).

* Overlaps inside a line are trimmed, notes running past the barline are

  clipped, gaps are filled with rests - so every output bar has the source bar

  length and all four voices share the same bar count.

"""



from __future__ import annotations



import re

import unicodedata

from copy import deepcopy

from dataclasses import dataclass, field

from pathlib import Path



from music21 import (

    chord,

    clef,

    converter,

    expressions,

    instrument,

    key,

    meter,

    note,

    stream,

    tempo,

)



from satb_splitter.model.config import (

    DivisiChoice,

    DivisiMode,

    ReviewFlag,

    SplitConfig,

    SplitResult,

    StaffPair,

    UnisonMode,

    VoiceRole,

)

from satb_splitter.split.divisi import (

    PitchCandidate,

    VoiceLeadingState,

    anchor_pitch_distance,

    candidate_tie_type,

    chord_stem,

    no_cross_line_order,

    resolve_simultaneous_chord,

)

from satb_splitter.split.layout import (

    LayoutDetection,

    clef_kind,

    detect_layout,

    first_sounding_kind,

    measure_clef_kinds,

)

from satb_splitter.split.layout import staff_kind as _layout_staff_kind

from satb_splitter.split.directions import apply_bar_form, attach_voltas, collect_score_form



EPS = 1e-6

ROLES: tuple[VoiceRole, ...] = ("Soprano", "Alto", "Tenor", "Bass")

_CONTEXT_CLASSES = (clef.Clef, key.KeySignature, meter.TimeSignature, tempo.MetronomeMark)





# ---------------------------------------------------------------------------

# Public API

# ---------------------------------------------------------------------------



def split_file(

    path: Path | str,

    config: SplitConfig | None = None,

    *,

    auto_layout: bool = False,

    inherit_ts: str | None = None,

    skip_boundary_tie_trim: bool = False,

    inherited_top_solo_role: VoiceRole | None = None,

) -> SplitResult:

    """Load a MusicXML (or music21-readable) file and split into SATB scores.



    ``inherit_ts`` (e.g. ``"6/8"``) is the meter in effect at the end of the

    previous page; it is used when this file has no printed time signature.

    """

    from satb_splitter.musicxml_validation import load_validated_score, align_score_measures

    score, xml_flags = load_validated_score(path)

    if not isinstance(score, stream.Score):

        wrapper = stream.Score()

        if isinstance(score, stream.Part):

            wrapper.append(score)

        else:

            part = stream.Part()

            part.append(score)

            wrapper.append(part)

        score = wrapper

    from satb_splitter.split.directions import restore_ending_types

    from satb_splitter.omr.header_tempo import apply_tempo_sidecar

    restore_ending_types(score, Path(path))

    xml_flags.extend(f for f in align_score_measures(score) if f.reason == "measure_count_mismatch")

    apply_tempo_sidecar(score, Path(path))

    attached = _apply_marker_sidecar(score, Path(path))

    from satb_splitter.split.multirest import apply_multirests, source_measure_numbers

    from satb_splitter.split.graft import apply_grafts

    from satb_splitter.model.config import resolve_graft_config

    from satb_splitter.omr.header_tempo import tempo_sidecar_note

    source_nums = source_measure_numbers(score, Path(path))

    resolved_graft = resolve_graft_config(getattr(config, "graft", None))

    xml_flags.extend(apply_grafts(score, Path(path), resolved_graft, inherit_ts=inherit_ts))

    from satb_splitter.split.joinbars import merge_source_numbers, remap_measure

    has_overrides = bool(config and getattr(config, "divisi_overrides", None))
    score_snap = deepcopy(score) if has_overrides else None
    src_nums_snap = list(source_nums)
    xml_flags_snap = list(xml_flags)
    attached_snap = list(attached)

    def _finalize_split(run_score, run_cfg):
        res = split_score(
            run_score,
            run_cfg,
            auto_layout=auto_layout,
            inherit_ts=inherit_ts,
            skip_boundary_tie_trim=skip_boundary_tie_trim,
            inherited_top_solo_role=inherited_top_solo_role,
        )
        run_nums = list(src_nums_snap)
        run_xml_flags = list(xml_flags_snap)
        run_att = list(attached_snap)
        raw_secs = [j.raw_second for j in (getattr(res, "bar_joins", None) or [])]
        if raw_secs:
            for fl in run_xml_flags:
                if fl.measure is not None:
                    fl.measure = remap_measure(fl.measure, raw_secs)
            for ch in getattr(res, "divisi_choices", None) or []:
                ch.measure = remap_measure(ch.measure, raw_secs)
                ch.choice_key = ch.normalized_key()
            run_nums = merge_source_numbers(run_nums, raw_secs)
            run_att = [(remap_measure(idx + 1, raw_secs) - 1, text) for idx, text in run_att]
        res.flags.extend(run_xml_flags)
        res.source_measure_numbers = run_nums
        res.flags.extend(
            apply_multirests(
                res,
                Path(path),
                pos_map=lambda p: remap_measure(p, raw_secs),
                expand_mr1=resolved_graft.f7_pdf_multirest_mr1,
            )
        )
        mr_ins_l = getattr(res, "multirest_insertions", None) or []
        if mr_ins_l and run_att:
            run_att = [
                (
                    idx + sum(a for ins_base, a in mr_ins_l if (idx + 1) > ins_base),
                    text,
                )
                for idx, text in run_att
            ]
        if mr_ins_l:
            for ch in getattr(res, "divisi_choices", None) or []:
                ch.measure += sum(a for ins_base, a in mr_ins_l if ch.measure > ins_base)
                ch.choice_key = ch.normalized_key()
        for ch in getattr(res, "divisi_choices", None) or []:
            if 1 <= ch.measure <= len(run_nums) and run_nums[ch.measure - 1] not in (None, ""):
                ch.source_measure = str(run_nums[ch.measure - 1])
        return res, run_att

    result, attached = _finalize_split(score, config)
    if has_overrides and score_snap is not None and config is not None:
        orig_ov = config.divisi_overrides or {}
        extra_raw: dict[str, str] = {}
        for ch in getattr(result, "divisi_choices", None) or []:
            if ch.raw_measure is None or ch.measure == ch.raw_measure:
                continue
            val = orig_ov.get(ch.choice_key)
            if val is None and ch.source_measure:
                val = orig_ov.get(f"{ch.source_measure}|{ch.upper_role}|{round(float(ch.offset), 4)}")
            raw_k = f"{ch.raw_measure}|{ch.upper_role}|{round(float(ch.offset), 4)}"
            if val and val != "auto" and raw_k not in orig_ov:
                extra_raw[raw_k] = val
        if extra_raw:
            merged_ov = dict(orig_ov)
            merged_ov.update(extra_raw)
            re_cfg = SplitConfig(
                template=config.template,
                unison_mode=config.unison_mode,
                chord_mode=config.chord_mode,
                tenor_clef=config.tenor_clef,
                voice_upper_ids=config.voice_upper_ids,
                voice_lower_ids=config.voice_lower_ids,
                flag_middle_pitches=config.flag_middle_pitches,
                solo_policy=config.solo_policy,
                divisi_mode=config.divisi_mode,
                divisi_overrides=merged_ov,
                graft=config.graft,
            )
            result, attached = _finalize_split(score_snap, re_cfg)

    text_note = tempo_sidecar_note(Path(path))

    if text_note:

        result.flags.append(ReviewFlag(measure=None, part_index=-1, offset=None,

                                       reason="pdf_no_text_layer", detail=text_note))

    for idx, text in attached:

        result.flags.append(

            ReviewFlag(

                measure=idx + 1,

                part_index=-1,

                offset=None,

                reason="pdf_marker",

                detail=f"Marker '{text}' read from the PDF text layer (OMR missed it).",

            )

        )

    return result





def _apply_marker_sidecar(score: stream.Score, path: Path) -> list[tuple[int, str]]:

    """Insert markers from ``<file>.markers.json`` (PDF text layer, see

    omr/markers.py) as text expressions, so single-staff routing sees e.g. a

    ``unison`` that Audiveris did not read. Only bars without a marker of

    their own get one; when several land on one bar the first (leftmost) wins.

    """

    try:

        from satb_splitter.omr.markers import load_sidecar

    except Exception:  # pragma: no cover

        return []

    items = load_sidecar(path)

    if not items:

        return []

    parts = list(score.parts)

    if not parts:

        return []

    per_part = [list(p.getElementsByClass(stream.Measure)) for p in parts]

    existing = _markers_by_index(parts)

    done: list[tuple[int, str]] = []

    used: set[int] = set()

    for it in items:

        i = int(it["measure_index"])

        text = str(it.get("text", "")).strip()

        if not text or i in used or parse_section_marker(text) is None:

            continue

        if any(parse_section_marker(t) is not None for t in existing.get(i, [])):

            continue

        # Audiveris read the same marker on a neighbouring bar (PDF geometry

        # can be one bar off when a measure spans a system break): keep OMR's.

        mine = parse_section_marker(text)

        near = [parse_section_marker(t) for k in (i - 1, i + 1) for t in existing.get(k, [])]

        if any(x is not None and (x.kind, x.roles) == (mine.kind, mine.roles) for x in near):

            continue

        target = None

        for ms in per_part:

            if i < len(ms) and _has_notes(ms[i]):

                target = ms[i]

                break

        if target is None:

            target = next((ms[i] for ms in per_part if i < len(ms)), None)

        if target is None:

            continue

        target.insert(0, expressions.TextExpression(text))

        used.add(i)

        done.append((i, text))

    return done





def split_score(

    score: stream.Score,

    config: SplitConfig | None = None,

    *,

    auto_layout: bool = False,

    inherit_ts: str | None = None,

    skip_boundary_tie_trim: bool = False,

    inherited_top_solo_role: VoiceRole | None = None,

) -> SplitResult:

    """Split a multi-part Score into S/A/T/B single-voice Scores.



    All four returned scores always have the same number of measures with the

    same bar lengths (roles without source material are filled with rests and

    flagged).

    """

    from satb_splitter.musicxml_validation import align_score_measures

    alignment_flags = align_score_measures(score)

    cfg = config or SplitConfig()

    detection: LayoutDetection | None = None

    if auto_layout:

        detection = detect_layout(score)

        cfg = SplitConfig(

            template=detection.template,

            unison_mode=cfg.unison_mode,

            chord_mode=cfg.chord_mode,

            tenor_clef=cfg.tenor_clef,

            voice_upper_ids=cfg.voice_upper_ids,

            voice_lower_ids=cfg.voice_lower_ids,

            flag_middle_pitches=cfg.flag_middle_pitches,

            solo_policy=cfg.solo_policy,

            graft=cfg.graft,

            divisi_mode=cfg.divisi_mode,

            divisi_overrides=dict(cfg.divisi_overrides),

        )



    parts = [_ensure_measures(p) for p in score.parts]

    flags: list[ReviewFlag] = list(alignment_flags)

    if detection is not None:

        flags.append(

            ReviewFlag(

                measure=None,

                part_index=-1,

                offset=None,

                reason="layout_auto",

                detail=detection.reason,

            )

        )



    ignored = set(cfg.template.ignore_part_indices)

    active_pairs: list[StaffPair] = []

    for pair in cfg.template.pairs:

        if pair.part_index in ignored:

            continue

        if pair.part_index >= len(parts):

            flags.append(

                ReviewFlag(

                    measure=None,

                    part_index=pair.part_index,

                    offset=None,

                    reason="missing_part",

                    detail=f"Template expects part index {pair.part_index}, "

                    f"score has only {len(parts)} part(s).",

                )

            )

            continue

        active_pairs.append(pair)



    used_indices = {p.part_index for p in active_pairs}

    unused = [i for i in range(len(parts)) if i not in used_indices]

    if unused and parts:

        flags.append(

            ReviewFlag(

                measure=None,

                part_index=-1,

                offset=None,

                reason="parts_ignored",

                detail=(

                    f"Source staves {unused} not mapped to S/A/T/B "

                    "(solo/trio/accompaniment policy: excluded from voice files)."

                ),

            )

        )



    ref_parts = [parts[p.part_index] for p in active_pairs] or parts

    # Batch 13: one printed bar that Audiveris split across a system break

    # (two short measures adding up to the meter). Detected here on the

    # source; each half is split as usual and the rendered halves are joined

    # below (apply_page_joins) - no padding, no notes added.

    from satb_splitter.split.joinbars import UNJOINED_REASON, apply_page_joins, find_split_bars, page_edge_info

    split_bars, unjoined = find_split_bars(

        parts, ref_parts, inherit_ts=inherit_ts,

        fallback_ratio=lambda: _guess_nominal([list(p.getElementsByClass(stream.Measure)) for p in ref_parts]),

    )

    for idx, text in unjoined:

        flags.append(ReviewFlag(measure=idx + 1, part_index=-1, offset=None, reason=UNJOINED_REASON, detail=text))

    from satb_splitter.model.config import resolve_graft_config

    graft_cfg = resolve_graft_config(getattr(cfg, "graft", None))

    cfg = SplitConfig(

        template=cfg.template,

        unison_mode=cfg.unison_mode,

        chord_mode=cfg.chord_mode,

        tenor_clef=cfg.tenor_clef,

        voice_upper_ids=cfg.voice_upper_ids,

        voice_lower_ids=cfg.voice_lower_ids,

        flag_middle_pitches=cfg.flag_middle_pitches,

        solo_policy=cfg.solo_policy,

        graft=graft_cfg,

        divisi_mode=cfg.divisi_mode,

        divisi_overrides=dict(cfg.divisi_overrides),

    )

    grid = _build_grid(

        ref_parts,

        inherit_ts=inherit_ts,

        flags=flags,

        clamp_overfull=graft_cfg.b15_f3_meter_clamp,

    )

    edge_info = page_edge_info(parts, ref_parts, inherit_ts,

                               _guess_nominal([list(p.getElementsByClass(stream.Measure)) for p in ref_parts]))

    form_data = collect_score_form(parts, grid.count, score, fermata_skip_staves=ignored)

    grid.form = form_data.bars

    grid.voltas = form_data.voltas

    single_part = len(parts) == 1

    open_closed_bars, open_closed_roles = _find_open_to_closed_runs(parts, active_pairs, grid, cfg)
    shifted_3to2_bars, sa_pair_idx = _find_bottom_aligned_3to2_runs(
        parts, active_pairs, ignored, grid, cfg
    )
    sa_pair = next((p for p in active_pairs if p.upper == "Soprano" and p.lower == "Alto"), None)
    partner_sa_part = parts[sa_pair.part_index] if sa_pair is not None else None

    _flag_duplicate_staff_lines(parts, active_pairs, flags, skip_bars=shifted_3to2_bars)



    # Every staff may contribute measures to any role (single-staff sections

    # route e.g. a unison system on the TB staff into S/A/T/B). Per bar index

    # the owning staff wins; another staff's bar is used only when the owner's

    # bar has no notes. rank: 0 = first owner, 1 = routed from another staff,

    # 2 = duplicate owner (role already owned by an earlier staff).

    cands: dict[VoiceRole, list[list[tuple[int, int, stream.Measure]]]] = {

        r: [[] for _ in range(grid.count)] for r in ROLES

    }

    owned: set[VoiceRole] = set()

    owner_short: dict[VoiceRole, int] = {}

    divisi_choices: list[DivisiChoice] = []

    for pair in active_pairs:

        src = parts[pair.part_index]

        roles = pair.roles()

        dup = [r for r in roles if r in owned]

        if dup:

            flags.append(

                ReviewFlag(

                    measure=None,

                    part_index=pair.part_index,

                    offset=None,

                    reason="duplicate_role",

                    detail=f"Roles {dup} already filled by another staff; "

                    "used only where that staff is silent.",

                )

            )

        produced = _split_part(
            src, grid, pair, cfg, flags, parts=parts, single_part=single_part,
            divisi_choices=divisi_choices,
            partner_sa=(
                partner_sa_part
                if (pair.upper in ("Tenor", "Bass") or pair.lower in ("Tenor", "Bass"))
                else None
            ),
            shifted_3to2_bars=shifted_3to2_bars,
            sa_pair_index=sa_pair_idx,
            open_closed_bars=open_closed_bars,
            open_closed_roles=open_closed_roles.get(pair.part_index),
        )

        for role, ms in produced.items():

            if role in roles:

                rank = 2 if role in dup else 0

            else:

                rank = 1

            have = 0

            for i, m in enumerate(ms):

                if m is not None:

                    cands[role][i].append((rank, pair.part_index, m))

                    have += 1

            if rank == 0 and have < grid.count:

                owner_short[role] = have

        owned.update(roles)



    _route_top_staff_soprano(parts, active_pairs, ignored, grid, cfg, cands, flags)

    _route_top_staff_tenor(
        parts, active_pairs, ignored, grid, cfg, cands, flags,
        divisi_choices=divisi_choices,
        inherited_role=inherited_top_solo_role,
    )



    missing_source_bars = {

        pi: {i for i, m in enumerate(p.getElementsByClass(stream.Measure))

             if getattr(m.editorial, "satb_missing_source", False)}

        for pi, p in enumerate(parts)

    }

    out_parts: dict[VoiceRole, stream.Part] = {}

    for role in ROLES:

        part = stream.Part(id=role)

        part.partName = role

        _assign_instrument(part, role)

        ms: list[stream.Measure] = []

        any_src = False

        for i in range(grid.count):

            missing_owner = any(

                role in pair.roles()

                and i in missing_source_bars[pair.part_index]

                for pair in active_pairs

            )

            options = [] if missing_owner else sorted(cands[role][i], key=lambda t: t[0])

            sounding = [t for t in options if _has_notes(t[2])]

            if options:

                any_src = True

            picked = (

                _pick_conflict_candidate(sounding, grid.lengths[i])

                if sounding else None

            )

            if len(sounding) > 1 and sounding[0][1] != sounding[1][1] and sounding[0][0] >= 0:

                kept = picked[1] if picked is not None else sounding[0][1]

                default_kept = sounding[0][1]

                extra = (

                    " (preferred the staff whose bar fits the printed meter; "

                    "OMR staff shift - check PDF)"

                    if kept != default_kept else "."

                )

                flags.append(

                    ReviewFlag(

                        measure=i + 1,

                        part_index=sounding[1][1],

                        offset=None,

                        reason="role_conflict",

                        detail=f"{role}: staff {sounding[0][1]} and staff "

                        f"{sounding[1][1]} both have notes in this bar; kept staff "

                        f"{kept}{extra}",

                    )

                )

            if picked is not None:

                ms.append(picked[2])

                kept_idx = picked[1]

            elif options:

                ms.append(options[0][2])

                kept_idx = options[0][1]

            else:

                ms.append(_rest_measure(grid, i))

                kept_idx = None

            if i < len(grid.form):

                apply_bar_form(ms[-1], grid.form[i], kept_idx)

            # Overflow/clip flags were raised while rendering every candidate

            # staff. Drop those that belong to a staff this role discarded

            # (Hard p14: D5 solo on the losing staff).

            if kept_idx is not None and options:

                losers = {t[1] for t in options if t[1] != kept_idx}

                if losers:

                    drop = {"overflow_dropped", "clipped_at_barline"}

                    prefix = f"{role}:"

                    bar_no = i + 1

                    flags[:] = [

                        fl

                        for fl in flags

                        if not (

                            fl.measure == bar_no

                            and fl.part_index in losers

                            and fl.reason in drop

                            and (fl.detail or "").startswith(prefix)

                        )

                    ]

        if grid.count and (not any_src or not any(_has_notes(m) for m in ms)) and role not in owned:

            flags.append(

                ReviewFlag(

                    measure=None,

                    part_index=-1,

                    offset=None,

                    reason="role_padded",

                    detail=f"No source staff for {role} in this layout; "

                    f"filled {grid.count} bar(s) with rests to keep bar counts aligned.",

                )

            )

        elif role in owner_short:

            flags.append(

                ReviewFlag(

                    measure=None,

                    part_index=-1,

                    offset=None,

                    reason="measure_count_mismatch",

                    detail=f"{role} source staff has {owner_short[role]} bar(s), others "

                    f"{grid.count}; padded with rest bars.",

                )

            )

        for m in ms:

            part.append(m)

        _mark_short_bars(ms, grid)

        _mark_padding(ms, grid)

        attach_voltas(part, ms, grid.voltas)

        if ms and not grid.short_first:

            _mark_pickup(ms[0], grid.lengths[0])

            _ensure_clef(part, role, cfg)

        if not graft_cfg.b15_f6_tie_continuity:

            tie_bars: list[tuple[int, str]] = []

            fixed = sanitize_ties(part, tie_bars)

            if fixed:

                # Batch 11: one flag per bar (REVIEW.txt lists bar + voice + reason).

                for bar_i, what in tie_bars:

                    flags.append(

                        ReviewFlag(

                            measure=bar_i + 1,

                            part_index=-1,

                            offset=None,

                            reason="tie_removed",

                            detail=f"{role}: tie on {what} removed (next/previous note is a different pitch or a rest).",

                        )

                    )

        out_parts[role] = part



    # Batch 13: join the rendered halves of bars split by a system break

    bar_joins = apply_page_joins(out_parts, split_bars, flags, ReviewFlag) if split_bars else []

    if graft_cfg.b15_f6_tie_continuity and not skip_boundary_tie_trim:

        for role, part in out_parts.items():

            tie_bars = []

            if sanitize_ties(part, tie_bars, repair_continuity=True):

                for bar_i, what in tie_bars:

                    flags.append(

                        ReviewFlag(

                            measure=bar_i + 1,

                            part_index=-1,

                            offset=None,

                            reason="tie_removed",

                            detail=f"{role}: tie on {what} removed (next/previous note is a different pitch or a rest).",

                        )

                    )

    scores: dict[VoiceRole, stream.Score] = {}

    for role, part in out_parts.items():

        sc = stream.Score()

        sc.insert(0, part)

        sc.metadata = deepcopy(score.metadata) if score.metadata else None

        if sc.metadata is not None:

            try:

                sc.metadata.movementName = role

            except Exception:

                pass

        scores[role] = sc



    res = SplitResult(

        scores=scores,

        flags=flags,

        source_part_count=len(parts),

    )

    # Batch 13: joins done on this page + edge bars for cross-page joins

    res.bar_joins = bar_joins

    res.edge_info = edge_info

    res.divisi_choices = divisi_choices

    return res





# ---------------------------------------------------------------------------

# Measure grid (shared bar count / bar lengths for all four voices)

# ---------------------------------------------------------------------------



@dataclass

class _Grid:

    numbers: list[int | None] = field(default_factory=list)

    lengths: list[float] = field(default_factory=list)

    context: list[list] = field(default_factory=list)

    # Batch 13 item 5: declared bar length (time signature in effect) per bar, None = no meter declared

    declared: list = field(default_factory=list)

    # Batch 13 item 5: bar 1 of a mid-score page is short and kept short (not a pickup, not padded)

    short_first: bool = False

    # Batch 13 item 5b: bar index -> OMR length, for bars padded to their declared meter

    padded: dict = field(default_factory=dict)

    form: list = field(default_factory=list)

    voltas: list = field(default_factory=list)



    @property

    def count(self) -> int:

        return len(self.lengths)





def _ensure_measures(part: stream.Part) -> stream.Part:

    if list(part.getElementsByClass(stream.Measure)):

        return part

    try:

        made = part.makeMeasures()

        if list(made.getElementsByClass(stream.Measure)):

            return made

    except Exception:

        pass

    return part





def _bar_length(m: stream.Measure) -> float:

    try:

        return float(m.barDuration.quarterLength)

    except Exception:

        return 4.0





def _content_length(m: stream.Measure) -> float:

    end = 0.0

    for el in m.recurse().notesAndRests:

        try:

            off = float(el.getOffsetInHierarchy(m))

        except Exception:

            off = float(el.offset)

        end = max(end, off + float(el.quarterLength))

    return end





# Meters accepted when a bar's notes do not fit the printed meter. Anything

# else (21/8, 17/16 ... from OMR rhythm errors) is not propagated: the bar is

# written in the smallest usual meter that holds its notes (rest padding) and

# flagged. Meters printed in the source are always accepted.

COMMON_METERS: tuple[str, ...] = ("4/4", "3/4", "2/4", "6/8", "9/8", "12/8", "3/8", "2/2", "3/2", "6/4", "5/4")





def _ratio_len(ratio: str) -> float:

    num, den = ratio.split("/")

    return int(num) * 4.0 / int(den)





def _ratio_den(ratio: str) -> int:

    return int(ratio.split("/")[1])





def _printed_ratio(m: stream.Measure) -> str | None:

    """Ratio of the time signature printed at/before this bar (None if none)."""

    own = sorted(m.getElementsByClass(meter.TimeSignature), key=lambda t: float(t.offset))

    if own:

        try:

            _ratio_len(own[0].ratioString)

            return own[0].ratioString

        except Exception:  # noqa: BLE001

            pass

    try:

        t = m.getContextByClass(meter.TimeSignature)

    except Exception:  # noqa: BLE001

        t = None

    if t is None:

        return None

    try:

        r = t.ratioString

        _ratio_len(r)

        return r

    except Exception:  # noqa: BLE001

        return None





def _last_onset(m: stream.Measure) -> float:

    """Start of the last sounding note in the bar (-1 if none)."""

    last = -1.0

    for el in m.recurse().notes:

        if not _is_sounding(el):

            continue

        try:

            off = float(el.getOffsetInHierarchy(m))

        except Exception:  # noqa: BLE001

            off = float(el.offset)

        last = max(last, off)

    return last





def _note_extent(m: stream.Measure) -> float:

    """End of the last sounding note in the bar (rests ignored)."""

    end = 0.0

    for el in m.recurse().notes:

        if not _is_sounding(el):

            continue

        try:

            off = float(el.getOffsetInHierarchy(m))

        except Exception:  # noqa: BLE001

            off = float(el.offset)

        end = max(end, off + float(el.quarterLength))

    return end





def _guess_nominal(per_part: list[list[stream.Measure]]) -> str | None:

    """Most common usual meter matching the bars' note lengths (no TS printed)."""

    counts: dict[str, int] = {}

    for ms in per_part:

        for m in ms:

            if _printed_ratio(m) is not None:

                continue  # meter already printed for this bar

            e = _note_extent(m)

            if e <= EPS:

                continue

            for r in COMMON_METERS:

                if abs(_ratio_len(r) - e) < EPS:

                    counts[r] = counts.get(r, 0) + 1

                    break

    if not counts:

        return None

    return max(counts.items(), key=lambda kv: (kv[1], -COMMON_METERS.index(kv[0])))[0]





def _derived_ratio(length: float) -> str | None:

    den = 4

    while den <= 64:

        num = length * den / 4.0

        if abs(num - round(num)) < EPS and round(num) > 0:

            return f"{int(round(num))}/{den}"

        den *= 2

    return None





def _choose_ratio(need: float, nominal: str, valid: list[str]) -> tuple[str, bool]:

    """Meter for a bar whose notes run to ``need`` > the nominal bar length.



    Returns (ratio, exact). ``exact`` = the notes fill a usual/printed meter

    exactly (e.g. a 9/8 phrase-end bar in 6/8); otherwise the smallest usual

    meter that holds the notes is used (rest padding, never cutting notes).

    """

    nd = _ratio_den(nominal)

    order = {r: i for i, r in enumerate(valid)}

    exact = [r for r in valid if abs(_ratio_len(r) - need) < EPS]

    if exact:

        return min(exact, key=lambda r: (_ratio_den(r) != nd, order[r])), True

    up = [r for r in valid if _ratio_len(r) > need + EPS]

    if up:

        return min(up, key=lambda r: (_ratio_len(r), _ratio_den(r) != nd, order[r])), False

    d = _derived_ratio(need) or _derived_ratio(int(need * 2 + 0.999) / 2.0) or nominal

    return d, False





def _try_compress_overfull_measures(cands: list[stream.Measure], target: float) -> bool:

    """Compress any overfull measures in ``cands`` into [0, target) with plain durations.



    Returns True only when EVERY overfull staff fits cleanly without tuplets

    or dropped pitches; otherwise leaves all measures untouched.

    """

    overfull = [m for m in cands if _note_extent(m) > target + EPS]

    if not overfull:

        return True

    plan: list[tuple[stream.Measure, float, list[tuple[object, object, float, object]]]] = []

    for m in overfull:

        ext = _note_extent(m)

        if ext <= EPS:

            return False

        scale = target / ext

        items: list[tuple[object, object, float, object]] = []

        containers: list[tuple[object, float]] = [

            (v, float(v.offset)) for v in m.voices

        ]

        containers.append((m, 0.0))

        for cont, base in containers:

            for el in list(cont.notesAndRests):

                if el.duration.isGrace:

                    continue

                if getattr(el.duration, "tuplets", None):

                    return False

                abs_off = base + float(cont.elementOffset(el))

                new_off = abs_off * scale

                new_ql = float(el.quarterLength) * scale

                if new_ql <= EPS or new_off >= target - EPS or new_off + new_ql > target + EPS:

                    return False

                nd = _plain_duration(new_ql)

                if nd is None:

                    return False

                items.append((cont, el, new_off - base * scale, nd))

        if not items:

            return False

        plan.append((m, scale, items))

    for m, scale, items in plan:

        for cont, el, rel_off, nd in items:

            el.duration = nd

            cont.setElementOffset(el, rel_off)

        for v in m.voices:

            m.setElementOffset(v, float(m.elementOffset(v)) * scale)

    return True





def _build_grid(

    ref_parts: list[stream.Part],

    *,

    inherit_ts: str | None = None,

    flags: list[ReviewFlag] | None = None,

    clamp_overfull: bool = False,

) -> _Grid:

    """One bar length + time signature per bar index, shared by all voices.



    Meter policy (Batch 4):



    * The **printed** meter (time signature in the source, or ``inherit_ts``

      from the previous page when this page has none, else the most common

      usual meter of the page) is the bar length. Short OMR bars are padded

      (a short first bar of a mid-score page, identified by ``inherit_ts``,

      is kept short and flagged ``bar_length_mismatch`` (Batch 13 item 5;

      was: padded, ``meter_irregular``) rather than treated as a

      pickup). If some sounding staves fit that meter and another overshoots

      by a wide, non-usual amount (an OMR dump such as 7.5 or 9 quarters

      against 4/4), the overlay is ignored for meter choice (flag

      ``omr_staff_shift``, which names the meter actually written). A

      partner that fills a usual meter (5/4, 9/8, 12/8) or overruns by two

      beats or less is not treated as an overlay.

    * Batch 13 item 5 (replaces the Batch 4/6/7 content-derived meters): the

      time signature written is ALWAYS the declared one - printed in this

      page's MXL, or carried from the previous page when this page prints

      none. It is never changed to absorb a duration mismatch and never

      guessed from the bar contents. A bar whose notes (starting at/after

      the barline) run past the declared length keeps all of them and is

      written overfull; a bar on a page without a printed meter whose OMR

      length is another usual meter keeps that length (under- or overfull,

      no padding, no trimming). Both are flagged ``bar_length_mismatch``

      (declared meter, actual length). With no meter declared anywhere

      before the bar, none is written (flag ``meter_missing``).

    """

    grid = _Grid()

    per_part = [list(p.getElementsByClass(stream.Measure)) for p in ref_parts]

    part_clef_rows = [measure_clef_kinds(p) for p in ref_parts]

    part_first_kinds = [first_sounding_kind(p) for p in ref_parts]

    n = max((len(ms) for ms in per_part), default=0)

    printed: list[str] = []

    for p in ref_parts:

        for t in p.recurse().getElementsByClass(meter.TimeSignature):

            try:

                r = t.ratioString

                _ratio_len(r)

            except Exception:  # noqa: BLE001

                continue

            if r not in printed:

                printed.append(r)

    inherited = None

    if inherit_ts:

        try:

            _ratio_len(inherit_ts)

            inherited = inherit_ts

        except Exception:  # noqa: BLE001

            inherited = None

    valid = list(dict.fromkeys(list(COMMON_METERS) + printed + ([inherited] if inherited else [])))

    # Meter for bars before the first printed time signature: the most common

    # usual bar length of the page; the previous page's meter is used when it

    # has that length (3/4 vs 6/8) or when the page gives no evidence.

    fallback_nominal = None



    def flag(i: int, reason: str, detail: str) -> None:

        if flags is not None:

            flags.append(ReviewFlag(measure=i + 1, part_index=-1, offset=None, reason=reason, detail=detail))



    # Printed meter in effect per staff and bar (carried forward bar by bar;

    # music21's context search misses a TS inside a bar in some streams).

    carried: list[list[str | None]] = []

    for ms in per_part:

        cur: str | None = None

        row: list[str | None] = []

        for m in ms:

            own = sorted(m.getElementsByClass(meter.TimeSignature), key=lambda t: float(t.offset))

            if own:

                try:

                    _ratio_len(own[0].ratioString)

                    cur = own[0].ratioString

                except Exception:  # noqa: BLE001

                    pass

            row.append(cur or _printed_ratio(m))

            # a TS later in the bar takes effect from the next bar

            if len(own) > 1 or (own and float(own[-1].offset) > EPS):

                try:

                    _ratio_len(own[-1].ratioString)

                    cur = own[-1].ratioString

                except Exception:  # noqa: BLE001

                    pass

        carried.append(row)

    prev_ratio: str | None = None

    missing_flagged = False

    for i in range(n):

        cands = [ms[i] for ms in per_part if i < len(ms)]

        m0 = cands[0]

        nominal = next((row[i] for row in carried if i < len(row) and row[i]), None)

        if nominal is None:

            # Batch 13 item 5: only a declared meter - the previous page's,

            # carried across the page files - never one guessed from contents.

            nominal = inherited

            from_fallback = True

        else:

            from_fallback = False

        if nominal is not None:

            bar = _ratio_len(nominal)

        else:

            bar = max([_content_length(m) for m in cands] + [_note_extent(m) for m in cands] + [0.0])

            if bar <= EPS:

                bar = max([_bar_length(m) for m in cands] + [0.0]) or 1.0

            if not missing_flagged:

                missing_flagged = True

                flag(i, "meter_missing",

                     "No time signature in this page's MXL before this bar and none carried from a previous "

                     "page: no meter written (none invented); bars keep their OMR length. Check the printed meter.")

        ratio = nominal

        declared_len = bar if nominal is not None else None

        extents = [_note_extent(m) for m in cands]

        onsets = [_last_onset(m) for m in cands]

        sounding_pairs = [(e, on) for e, on in zip(extents, onsets) if e > EPS]

        fitting_pairs = [(e, on) for e, on in sounding_pairs if e <= bar + EPS]

        exact_fitting = [
            (idx, e, on)
            for idx, (e, on) in enumerate(zip(extents, onsets))
            if e > EPS and abs(e - bar) < EPS
        ]

        need_max = max((e for e, _ in sounding_pairs), default=0.0)

        last_on_max = max(onsets, default=-1.0)

        # Hard p14: one staff holds a dumped solo (7.5 / 9 quarters) while

        # another staff's choir music fits the printed meter. Do not let the

        # overlay rewrite the meter (15/8, 9/4). Resting staves are not

        # treated as "fitting" (a real 9/8 bar with T/B rests must still

        # become 9/8). A staff that fills a usual meter (5/4, 9/8, 12/8)

        # or overruns by two beats or less is content or a small clip, not

        # an overlay - otherwise meter_from_content is lost and the flag

        # can contradict the written meter.

        # Batch 16 (C16_4): require an *exact* meter fit on a shifted/conflicting

        # staff so an underfull choir staff (e.g. 125 p2 m9/m10: 2.5q/2.0q vs

        # 8.0q/6.25q) never triggers staff_shift and truncates real notes.

        content_like = any(abs(_ratio_len(r) - need_max) < EPS for r in COMMON_METERS)

        shifted_fit = any(
            (
                part_first_kinds[idx] is not None
                and i < len(part_clef_rows[idx])
                and part_clef_rows[idx][i] is not None
                and part_clef_rows[idx][i] != part_first_kinds[idx]
            )
            or any(
                j != idx
                and extents[j] > bar + 2.0 + EPS
                and i < len(part_clef_rows[j])
                and part_clef_rows[j][i] is not None
                and i < len(part_clef_rows[idx])
                and part_clef_rows[j][i] == part_clef_rows[idx][i]
                for j in range(len(ref_parts))
            )
            for idx, _, _ in exact_fitting
        )

        staff_shift = bool(

            exact_fitting

            and shifted_fit

            and need_max > bar + 2.0 + EPS

            and not content_like

        )

        if staff_shift:

            need = max(e for _, e, _ in exact_fitting)

            last_on = max(on for _, _, on in exact_fitting)

        else:

            need = need_max

            last_on = last_on_max

        # Only notes that START at/after the barline extend the bar (9/8 bar

        # in 6/8). A note that starts inside and merely runs long (OMR

        # duration error) is clipped at the barline as before (BUG-3).

        if nominal is not None and need > bar + EPS and last_on >= bar - EPS:

            own_ts_at_zero = any(

                any(float(t.offset) <= EPS for t in m.getElementsByClass(meter.TimeSignature))

                for m in cands

            )

            clamped = False

            if clamp_overfull and fitting_pairs and not own_ts_at_zero:

                clamped = _try_compress_overfull_measures(cands, bar)

            if not clamped:

                # Batch 13 item 5: keep the declared meter; the bar is written

                # overfull with all its notes (no padding, no trimming).

                flag(i, BAR_LENGTH_MISMATCH,

                     f"Declared meter {nominal} ({bar:g} q); OMR bar holds {need:g} q - overfull by "

                     f"{need - bar:g} q. Meter NOT changed: bar written with all its notes, no padding, "

                     "no trimming. Check this bar against the PDF.")

                bar = need

        elif nominal is not None and from_fallback and i > 0:

            # No printed meter here: keep a bar whose own length is a usual

            # meter (e.g. a 6/8 bar between 9/8 bars, as Batch 3 did), written

            # with the nominal's note value (6/8, not 3/4). If that length is a different usual meter than the inherited/guessed nominal (a 9/8 bar inside 4/4), flag meter_from_content. Garbage lengths

            # (5/8 = 2.5, 32/3 ...) fall back to the nominal meter.

            bd = max((_bar_length(m) for m in cands), default=bar)

            if EPS < bd and abs(bd - bar) > EPS and bd >= need - EPS:

                same = [r for r in valid if abs(_ratio_len(r) - bd) < EPS]

                if same:

                    # Batch 13 item 5: the OMR length is kept as is (under- or

                    # overfull), the declared meter is NOT rewritten to fit it.

                    flag(i, BAR_LENGTH_MISMATCH,

                         f"Declared meter {nominal} ({bar:g} q, carried from the previous page); OMR bar is "

                         f"{bd:g} q ({'under' if bd < bar else 'over'}full by {abs(bd - bar):g} q). Meter NOT "

                         "changed: bar kept at its OMR length, no padding, no trimming. Check this bar against the PDF.")

                    bar = bd

        if i > 0 and nominal is not None and declared_len is not None and abs(bar - declared_len) < EPS:

            # Batch 13 item 5b (Orchestrator): EVERY staff holds less than the

            # declared meter. The Batch 11 end padding is kept (meter unchanged),

            # but the bar is listed in REVIEW and its padding rests are marked.

            # Not for a page's first bar (i == 0) or a bar Audiveris marks implicit.

            held = max([_content_length(m) for m in cands] + [need_max])

            implicit = any(getattr(m, "showNumber", None) == stream.enums.ShowNumber.NEVER for m in cands)

            if EPS < held < bar - EPS and not implicit:

                flag(i, BAR_LENGTH_MISMATCH,

                     f"Declared meter {nominal} ({bar:g} q); OMR bar holds {held:g} q - underfull by "

                     f"{bar - held:g} q in every staff. Meter NOT changed: padded with {bar - held:g} q of rest "

                     "at the end of the bar (visible rest, marked as padding in the MusicXML; no note "

                     "invented). Check this bar against the PDF.")

                grid.padded[i] = held

        length = bar

        if i == 0:

            mref = max(cands, key=_bar_length)

            pad_left = float(getattr(mref, "paddingLeft", 0.0) or 0.0)

            content = max(_content_length(m) for m in cands)

            if inherited is None and EPS < pad_left < bar - EPS and (bar - pad_left) >= content - EPS:

                length = bar - pad_left

            elif (

                inherited is None

                and EPS < content < bar - EPS

                and need <= content + EPS

            ):

                # Pickup / anacrusis only at the start of a work, not the

                # first bar of a mid-score page (Hard p13 bar 1 / merged 382).

                length = content

            elif EPS < max(need, content) < bar - EPS:

                # Batch 13 item 5: first bar of a mid-score page holding less

                # than the declared meter - neither re-metered nor padded: kept

                # at its OMR length under the declared meter (not a pickup).

                length = max(need, content)

                grid.short_first = True

                flag(

                    i,

                    BAR_LENGTH_MISMATCH,

                    f"Declared meter {ratio} ({bar:g} q); OMR bar holds {length:g} q - underfull by "

                    f"{bar - length:g} q (first bar of a mid-score page, not a pickup). Meter NOT changed: "

                    "bar kept at its OMR length, no padding, no trimming. Check this bar against the PDF.",

                )

        if staff_shift:

            flag(

                i,

                "omr_staff_shift",

                "OMR staff shift - check PDF: some staves fit "

                f"{nominal} while another holds {need_max:g} quarters; "

                f"meter kept as {ratio}.",

            )

        ctx_els: list = []

        if ratio is not None and ratio != prev_ratio:

            ctx_els.append(meter.TimeSignature(ratio))

            prev_ratio = ratio

        for m in cands:

            ks = list(m.getElementsByClass(key.KeySignature))

            if ks:

                ctx_els.append(deepcopy(ks[0]))

                break

        grid.numbers.append(m0.number)

        grid.lengths.append(length)

        grid.context.append(ctx_els)

        grid.declared.append(declared_len)

    return grid





def _flag_duplicate_staff_lines(

    parts: list[stream.Part], pairs: list[StaffPair], flags: list[ReviewFlag],

    skip_bars: set[int] | frozenset[int] = frozenset(),

) -> None:

    """Flag (info) bars where both choir staves carry the very same line.



    Hard p5 bars 1/7: checked against the PDF - Alto and Tenor really sing

    the same notes there (S/B rest), so the split is correct. An OMR

    duplicate looks identical in the MusicXML, so it is reported for a

    check, never "fixed" by guessing."""

    two = [p for p in pairs if p.lower is not None]

    if len(two) < 2:

        return

    a, b = two[0], two[1]

    ma = list(parts[a.part_index].getElementsByClass(stream.Measure))

    mb = list(parts[b.part_index].getElementsByClass(stream.Measure))



    def seq(m):

        out = []

        for el in m.recurse().notes:

            if not _is_sounding(el):

                continue

            off = round(float(el.getOffsetInHierarchy(m)), 3)

            out.append((off, tuple(sorted(round(x.ps, 1) for x in el.pitches))))

        return sorted(out)



    for i in range(min(len(ma), len(mb))):

        if i in skip_bars:

            continue

        sa, sb = seq(ma[i]), seq(mb[i])

        if len(sa) >= 2 and sa == sb:

            names = " ".join(

                "+".join(x.nameWithOctave for x in el.pitches)

                for el in ma[i].recurse().notes if _is_sounding(el)

            )

            flags.append(ReviewFlag(

                measure=i + 1, part_index=b.part_index, offset=None,

                reason="duplicate_staff_line",

                detail=(f"Same notes on staff {a.part_index} and staff {b.part_index} ({names}): "

                        f"{a.lower} = {b.upper}. Often printed that way (A/T unison; Hard p5 bars 1/7 "

                        "match the PDF) but can be an OMR duplicate - check against the PDF if "

                        f"{a.upper}/{b.lower} look wrongly empty."),

            ))





def _mark_pickup(m: stream.Measure, length: float) -> None:

    """Mark a short first bar as an anacrusis (``paddingLeft``) so the

    MusicXML exporter writes it as an implicit pickup instead of padding it

    with a trailing rest to the full time signature (Easy bar 1)."""

    full = None

    try:

        own = list(m.getElementsByClass(meter.TimeSignature))

        ts = own[0] if own else m.getContextByClass(meter.TimeSignature)

        full = float(ts.barDuration.quarterLength) if ts is not None else None

    except Exception:  # noqa: BLE001

        full = None

    if full is not None and length < full - EPS:

        m.paddingLeft = full - length

        # implicit="yes" in MusicXML: MuseScore treats the bar as a pickup

        # (anacrusis) and does not count it as bar 1.

        m.showNumber = stream.enums.ShowNumber.NEVER





BAR_LENGTH_MISMATCH = "bar_length_mismatch"





def _mark_short_bars(ms: list[stream.Measure], grid: _Grid) -> None:
    """Batch 13 item 5 + B19.3: keep bars whose OMR length differs from the
    declared (or implicit 4.0q) meter at their exact grid length on export.

    A bar kept SHORTER than its declared meter gets ``paddingRight`` so music21
    does not fill it with a hidden rest on export (bar 1 of a page is left to
    ``_mark_pickup`` unless ``grid.short_first`` is set). A bar kept LONGER is
    marked ``editorial.satb_overfull`` so ``write_musicxml`` uses
    ``_no_rebar_exporter`` instead of splitting notes/rests across the barline.

    When ``declared is None`` (``meter_missing``), music21's implicit default
    bar duration on MusicXML export is ``4.0q`` (4/4) and ``_mark_pickup`` does
    not set ``paddingLeft``, so both underfull (``< 4.0q``) and overfull
    (``> 4.0q``) meterless bars are marked against ``4.0``."""
    for i, m in enumerate(ms):
        declared = grid.declared[i] if i < len(grid.declared) else None
        if declared is not None:
            if i == 0 and declared - grid.lengths[i] > EPS and not grid.short_first:
                continue
            gap = declared - grid.lengths[i]
        else:
            gap = 4.0 - grid.lengths[i]
        if gap > EPS:
            m.paddingRight = gap
        elif gap < -EPS:
            # overfull: writer.py keeps music21 from re-barring it on export
            m.editorial.satb_overfull = True





def _mark_padding(ms: list[stream.Measure], grid: _Grid) -> None:

    """Batch 13 item 5b: mark the end-padding rests of a padded bar

    (``grid.padded``) with ``editorial.satb_padding`` - the writer puts an XML

    comment before them. A rest that straddles the OMR length is split there

    so only the added part is marked. The rests stay visible."""

    for i, held in grid.padded.items():

        if i >= len(ms) or ms[i] is None:

            continue

        m = ms[i]

        for c in [m, *m.voices]:

            for r in list(c.getElementsByClass(note.Rest)):

                a = float(r.offset)

                e = a + float(r.quarterLength)

                if e <= held + EPS:

                    continue

                if a < held - EPS:

                    r.quarterLength = held - a

                    pad = note.Rest(quarterLength=e - held)

                    pad.editorial.satb_padding = True

                    c.insert(held, pad)

                else:

                    r.editorial.satb_padding = True

def _fallback_bar(cands: list[stream.Measure], bar: float) -> float:

    for m in cands:

        try:

            t = m.getContextByClass(meter.TimeSignature)

            if t is not None:

                ql = float(t.barDuration.quarterLength)

                if ql > EPS and _effective_ts(m, ql) is not None:

                    return ql

        except Exception:

            continue

    return max(0.5, int(bar * 2) / 2.0)





def _effective_ts(m: stream.Measure, bar: float) -> meter.TimeSignature | None:

    try:

        t = m.getContextByClass(meter.TimeSignature)

    except Exception:

        t = None

    if t is not None:

        try:

            if abs(float(t.barDuration.quarterLength) - bar) < EPS:

                return meter.TimeSignature(t.ratioString)

        except Exception:

            pass

    # Derive an x/2^k signature from the bar length

    den = 4

    while den <= 64:

        num = bar * den / 4.0

        if abs(num - round(num)) < EPS and round(num) > 0:

            try:

                return meter.TimeSignature(f"{int(round(num))}/{den}")

            except Exception:

                return None

        den *= 2

    return None



def _rest_measure(grid: _Grid, i: int) -> stream.Measure:

    m = stream.Measure(number=grid.numbers[i] if grid.numbers[i] is not None else i + 1)

    for el in grid.context[i]:

        m.insert(0, deepcopy(el))

    m.insert(0, note.Rest(quarterLength=grid.lengths[i]))

    return m





# ---------------------------------------------------------------------------

# Instruments / clefs

# ---------------------------------------------------------------------------



def _assign_instrument(part: stream.Part, role: VoiceRole) -> None:

    mapping = {

        "Soprano": instrument.Soprano(),

        "Alto": instrument.Alto(),

        "Tenor": instrument.Tenor(),

        "Bass": instrument.Bass(),

    }

    inst = mapping[role]

    inst.instrumentName = role

    part.insert(0, inst)





def _ensure_clef(part: stream.Part, role: VoiceRole, cfg: SplitConfig) -> None:

    flat = part.flatten()

    if flat.getElementsByClass(clef.Clef):

        return

    c = _default_clef(role, cfg)

    measures = list(part.getElementsByClass(stream.Measure))

    if measures:

        measures[0].insert(0, c)

    else:

        part.insert(0, c)





def _default_clef(role: VoiceRole, cfg: SplitConfig) -> clef.Clef:

    if role in ("Soprano", "Alto"):

        return clef.TrebleClef()

    if role == "Bass":

        return clef.BassClef()

    if cfg.tenor_clef.value == "bass":

        return clef.BassClef()

    if cfg.tenor_clef.value == "as_source":

        return clef.TrebleClef()

    return clef.Treble8vbClef()





# ---------------------------------------------------------------------------

# Monophonic line accumulator

# ---------------------------------------------------------------------------



def _el_pitches(n) -> list:

    """Pitches of a placed line item (Note or divisi Chord)."""

    if isinstance(n, chord.Chord):

        return list(n.pitches)

    return [n.pitch]





def _el_name(n) -> str:

    return "+".join(p.nameWithOctave for p in sorted(_el_pitches(n), key=lambda p: -p.ps))





class _Line:

    """Notes placed at absolute offsets inside one measure for one role.



    An item is normally a Note; when a pitch could not get a line of its own

    (divisi / 3+ note chord on a two-voice staff) it is added to the item at

    the same onset, which then becomes a Chord - the voice stays one rhythmic

    line and no pitch is lost (BUG-2).

    """



    def __init__(self) -> None:

        self.items: list[tuple[float, float, note.NotRest]] = []



    def is_free(self, start: float, end: float) -> bool:

        for s, e, _ in self.items:

            if s < end - EPS and start < e - EPS:

                return False

        return True



    def sounds(self, pitch, t: float) -> bool:

        for s, e, n in self.items:

            if s - EPS <= t < e - EPS and any(abs(q.ps - pitch.ps) < 0.01 for q in _el_pitches(n)):

                return True

        return False



    def add_divisi(self, item, pitch, src_el=None) -> None:

        """Add ``pitch`` to an existing item (Note -> Chord).



        When the incoming note is longer than the item already on the line

        (Hard p14 bar 397: C5/0.5 plus B4/0.75 at the same onset), keep the

        longer duration so a rest is not inserted through a pitch that is

        still sounding.

        """

        s_, e_, n_ = item

        incoming_end = e_

        if src_el is not None:

            try:

                incoming_end = s_ + float(src_el.quarterLength)

            except Exception:  # noqa: BLE001

                incoming_end = e_

        new_end = max(e_, incoming_end)

        if any(abs(q.ps - pitch.ps) < 0.01 for q in _el_pitches(n_)):

            if new_end > e_ + EPS:

                n_.quarterLength = new_end - s_

                idx = self.items.index(item)

                self.items[idx] = (s_, new_end, n_)

            return

        pitches = sorted(_el_pitches(n_) + [deepcopy(pitch)], key=lambda p: p.ps)

        c = chord.Chord(pitches)

        c.duration = deepcopy(n_.duration)

        c.quarterLength = new_end - s_

        c.articulations = deepcopy(n_.articulations)

        c.expressions = deepcopy(n_.expressions)

        c.lyrics = deepcopy(n_.lyrics)

        if n_.tie is not None:

            c.tie = deepcopy(n_.tie)

        idx = self.items.index(item)

        self.items[idx] = (s_, new_end, c)



    def starting_at(self, t: float):

        for item in self.items:

            if abs(item[0] - t) < EPS:

                return item

        return None



    def place(self, offset: float, src_el: note.GeneralNote, pitch) -> None:

        n = _note_from_pitch(pitch, src_el)

        self.items.append((offset, offset + float(n.quarterLength), n))





@dataclass

class _Ctx:

    measure: int | None

    part_index: int

    cfg: SplitConfig

    flags: list[ReviewFlag]

    divisi: list[str] = field(default_factory=list)

    upper_role: str = "Soprano"

    lower_role: str = "Alto"

    vl_state: VoiceLeadingState | None = None

    bar_next_upper: float | None = None

    bar_next_lower: float | None = None

    divisi_choices: list[DivisiChoice] | None = None

    divisi_ambig: list[str] = field(default_factory=list)

    target: float | None = None



    def flag(self, offset: float | None, reason: str, detail: str) -> None:

        self.flags.append(

            ReviewFlag(

                measure=self.measure,

                part_index=self.part_index,

                offset=offset,

                reason=reason,

                detail=detail,

            )

        )





def _is_sounding(el: note.GeneralNote) -> bool:

    if isinstance(el, note.Rest) or not isinstance(el, (note.Note, chord.Chord)):

        return False

    if el.duration.isGrace or float(el.quarterLength) <= EPS:

        return False

    return True





def _pitches_desc(el: note.GeneralNote) -> list:

    if isinstance(el, chord.Chord):

        return sorted(el.pitches, key=lambda p: p.ps, reverse=True)

    return [el.pitch]





def _voice_sort_key(vid) -> tuple:

    try:

        return (0, int(str(vid)), "")

    except (TypeError, ValueError):

        return (1, 0, str(vid))





def _measure_groups(m: stream.Measure) -> tuple[list[list[tuple[float, note.GeneralNote]]], list[tuple[float, note.GeneralNote]]]:

    """Return (voice groups ordered by voice number/appearance, loose top-level events)."""

    voices = list(m.voices)

    ordered = sorted(enumerate(voices), key=lambda iv: (_voice_sort_key(iv[1].id), iv[0]))

    groups: list[list[tuple[float, note.GeneralNote]]] = []

    for _, v in ordered:

        base = float(v.offset)

        groups.append([(base + float(el.offset), el) for el in v.notesAndRests])

    loose = [(float(el.offset), el) for el in m.notesAndRests]

    return groups, loose





# ---------------------------------------------------------------------------

# Placement rules

# ---------------------------------------------------------------------------



def _place_unison(pitch, el, off, upper: _Line, lower: _Line, ctx: _Ctx) -> None:

    mode = ctx.cfg.unison_mode

    if mode in (UnisonMode.DUPLICATE_BOTH, UnisonMode.FLAG_REVIEW):

        upper.place(off, el, pitch)

        lower.place(off, el, pitch)

        if mode == UnisonMode.FLAG_REVIEW:

            ctx.flag(off, "unison", f"Unison {pitch.nameWithOctave} duplicated for review")

    elif mode == UnisonMode.UPPER_ONLY:

        upper.place(off, el, pitch)

    elif mode == UnisonMode.LOWER_ONLY:

        lower.place(off, el, pitch)





def _divisi_into(off: float, pitch, lines: list[tuple[str, _Line]], ctx: _Ctx, src_el=None) -> bool:

    """Attach ``pitch`` as a divisi note to the nearest line that has a note

    starting at ``off``, measuring distance against the outer anchor pitch

    (prevents snowball chaining) and avoiding forced voice crossings."""

    best = None

    for label, line in lines:

        item = line.starting_at(off)

        if item is None:

            continue

        dist = anchor_pitch_distance(item[2], pitch, label)

        if best is None or dist < best[0] - 0.01:

            best = (dist, label, line, item)

    if best is None:

        return False

    _, label, line, item = best

    line.add_divisi(item, pitch, src_el)

    ctx.divisi.append(f"{pitch.nameWithOctave}->{label}")

    return True





def _tie_split_into(

    off: float,

    el: note.GeneralNote,

    pitch,

    lines: list[tuple[str, _Line]],

    ctx: _Ctx,

    *,

    prefer: str | None = None,

) -> bool:

    """B15_F7: split a spanning note on a busy line to insert a 3rd-voice pitch via ties."""

    from music21 import tie as m21tie



    ql = float(el.quarterLength)

    if ql < 0.125 - EPS or getattr(el.duration, "tuplets", None):

        return False

    if ctx.target is not None and (off >= ctx.target - EPS or off + ql > ctx.target + EPS):

        return False

    d_mid = _plain_duration(ql)

    if d_mid is None:

        return False



    pref_label = {"U": "upper", "L": "lower"}.get(prefer or "")

    cands = []

    for label, line in lines:

        for idx, (s, e, n) in enumerate(line.items):

            eff_e = min(e, ctx.target) if ctx.target is not None else e

            if not (s < off - EPS and off + ql <= eff_e + EPS):

                continue

            if getattr(n.duration, "tuplets", None):

                continue

            left_ql = off - s

            tail_ql = eff_e - (off + ql)

            if left_ql < 0.125 - EPS:

                continue

            if tail_ql > EPS and tail_ql < 0.125 - EPS:

                continue

            d_left = _plain_duration(left_ql)

            if d_left is None:

                continue

            d_tail = _plain_duration(tail_ql) if tail_ql > EPS else None

            if tail_ql > EPS and d_tail is None:

                continue

            n_ps = _el_pitches(n)

            if any(abs(q.ps - pitch.ps) < 0.01 for q in n_ps):

                continue

            dist = min(abs(q.ps - pitch.ps) for q in n_ps)

            pref_rank = 0 if (pref_label and label == pref_label) else 1

            cands.append((dist, pref_rank, label, line, idx, s, e, eff_e, n, d_left, d_tail))



    if not cands:

        return False

    cands.sort(key=lambda t: (t[0], t[1]))

    _, _, label, line, idx, s, e, eff_e, n, d_left, d_tail = cands[0]

    ne = off + ql

    tail_ql = eff_e - ne

    n_ps = _el_pitches(n)



    def _orig_tie(src_n, p_obj):

        if isinstance(src_n, chord.Chord):

            return src_n.getTie(p_obj) or getattr(src_n, "tie", None)

        return getattr(src_n, "tie", None)



    # Left head keeps original lyrics, expressions, articulations

    n_left = deepcopy(n)

    n_left.duration = d_left

    if isinstance(n_left, chord.Chord):

        for p_obj in n_left.pitches:

            ot = _orig_tie(n, p_obj)

            lt = "continue" if (ot is not None and ot.type in ("stop", "continue")) else "start"

            n_left.setTie(m21tie.Tie(lt), p_obj)

    else:

        ot = getattr(n, "tie", None)

        lt = "continue" if (ot is not None and ot.type in ("stop", "continue")) else "start"

        n_left.tie = m21tie.Tie(lt)



    # Middle chord: strictly 0 lyric/expression/articulation duplication from spanning note

    mid_pitches = sorted([deepcopy(p) for p in n_ps] + [deepcopy(pitch)], key=lambda p: p.ps)

    c_mid = chord.Chord(mid_pitches)

    c_mid.duration = d_mid

    c_mid.lyrics = []

    c_mid.expressions = []

    c_mid.articulations = []

    for mp in c_mid.pitches:

        if abs(mp.ps - pitch.ps) < 0.01:

            et = getattr(el, "tie", None)

            if tail_ql <= EPS and et is not None and et.type in ("start", "continue"):

                c_mid.setTie(deepcopy(et), mp)

            else:

                c_mid.setTie(None, mp)

        else:

            ot = _orig_tie(n, mp)

            if tail_ql > EPS or (ot is not None and ot.type in ("start", "continue")):

                c_mid.setTie(m21tie.Tie("continue"), mp)

            else:

                c_mid.setTie(m21tie.Tie("stop"), mp)



    new_items = [(s, off, n_left), (off, ne, c_mid)]

    if tail_ql > EPS and d_tail is not None:

        n_tail = deepcopy(n)

        n_tail.duration = d_tail

        n_tail.lyrics = []

        n_tail.expressions = []

        n_tail.articulations = []

        if isinstance(n_tail, chord.Chord):

            for p_obj in n_tail.pitches:

                ot = _orig_tie(n, p_obj)

                tt = "continue" if (ot is not None and ot.type in ("start", "continue")) else "stop"

                n_tail.setTie(m21tie.Tie(tt), p_obj)

        else:

            ot = getattr(n, "tie", None)

            tt = "continue" if (ot is not None and ot.type in ("start", "continue")) else "stop"

            n_tail.tie = m21tie.Tie(tt)

        new_items.append((ne, eff_e, n_tail))



    line.items[idx:idx + 1] = new_items

    if e > eff_e + EPS:

        role = {"upper": ctx.upper_role, "lower": ctx.lower_role}.get(label, ctx.upper_role)

        ctx.flag(s, "clipped_at_barline", f"{role}: {_el_name(n)} shortened to fit bar")

    ctx.divisi.append(f"{pitch.nameWithOctave}->{label}")

    return True





def _place_flexible(

    off: float,

    el: note.GeneralNote,

    pitches: list,

    upper: _Line,

    lower: _Line,

    ctx: _Ctx,

    *,

    origin: str,

    prefer: str | None = None,

    placed_group: dict[int, int] | None = None,

    placed_stem: dict[int, str | None] | None = None,

    group_of: dict[int, int] | None = None,

) -> None:

    """Place pitches (high->low) into whichever line(s) are free at this beat.



    Used for unvoiced staves, chords and extra (3rd+) voices. A pitch with no

    free line becomes a divisi note of the line that has a note at the same

    onset (``prefer`` = "U"/"L": the voice the chord was written in). Only

    pitches with no line sounding a note at that onset are dropped + flagged.

    """

    if not pitches:

        return

    def _rec(line: _Line) -> None:
        if placed_group is not None and group_of is not None and id(el) in group_of and line.items:
            placed_group[id(line.items[-1][2])] = group_of[id(el)]
        if placed_stem is not None and line.items:
            placed_stem[id(line.items[-1][2])] = _stem_of(el)

    end = off + float(el.quarterLength)

    u_free = upper.is_free(off, end)

    l_free = lower.is_free(off, end)

    remaining = list(pitches)

    both = [("upper", upper), ("lower", lower)]



    if u_free and l_free:

        if len(remaining) == 1:

            _place_unison(remaining[0], el, off, upper, lower, ctx)

            return

        if len(remaining) >= 3:

            cands = [
                PitchCandidate(
                    pitch=p,
                    src_el=el,
                    stem=chord_stem(el),
                    group_target=prefer,
                    tie_type=candidate_tie_type(el, p),
                )
                for p in remaining
            ]

            _apply_resolved_chord(
                cands,
                off,
                upper,
                lower,
                ctx,
                next_upper_ps=ctx.bar_next_upper,
                next_lower_ps=ctx.bar_next_lower,
                placed_group=placed_group,
                placed_stem=placed_stem,
                group_of=group_of,
            )

            return

        upper.place(off, el, remaining[0])

        _rec(upper)

        lower.place(off, el, remaining[-1])

        _rec(lower)

        return



    if u_free:

        upper.place(off, el, remaining[0])

        _rec(upper)

        remaining = remaining[1:]

    elif l_free:

        lower.place(off, el, remaining[-1])

        _rec(lower)

        remaining = remaining[:-1]



    remaining = [p for p in remaining if not upper.sounds(p, off) and not lower.sounds(p, off)]

    monophonic_collapse = getattr(ctx.cfg, "divisi_mode", DivisiMode.MONOPHONIC_AUTO) != DivisiMode.KEEP_DIVISI

    both_started = upper.starting_at(off) is not None and lower.starting_at(off) is not None

    if monophonic_collapse and both_started:

        remaining = []

    if prefer is not None and remaining:

        # Leftover of a chord written in one voice: divisi of that voice, unless it would cross.

        kept_rem = []

        for p in remaining:

            order = no_cross_line_order(p, off, upper, lower, prefer)

            if not _divisi_into(off, p, order, ctx, el):

                kept_rem.append(p)

        remaining = kept_rem

    if remaining:

        kept_rem = []

        for p in remaining:

            order = no_cross_line_order(p, off, upper, lower, None)

            if not _divisi_into(off, p, order, ctx, el):

                kept_rem.append(p)

        remaining = kept_rem

    if remaining and getattr(getattr(ctx.cfg, "graft", None), "b15_f7_chord_tie_split", False):

        remaining = [p for p in remaining if not _tie_split_into(off, el, p, both, ctx, prefer=prefer)]

    dropped = remaining

    if dropped:

        ctx.flag(

            off,

            "dropped_pitch",

            f"{origin}: no voice has a note at this onset for "

            + ", ".join(p.nameWithOctave for p in dropped),

        )





def _group_stats(group: list[tuple[float, note.GeneralNote]]) -> tuple[float | None, int]:

    """(duration-weighted mean pitch of sounding notes or None, stem score up>0 / down<0)."""

    tot = 0.0

    wsum = 0.0

    stem = 0

    for _, el in group:

        if not _is_sounding(el):

            continue

        ql = max(float(el.quarterLength), 0.125)

        ps = [p.ps for p in el.pitches]

        tot += ql * sum(ps) / len(ps)

        wsum += ql

        sd = getattr(el, "stemDirection", None)

        if sd == "up":

            stem += 1

        elif sd == "down":

            stem -= 1

    return (tot / wsum if wsum else None), stem





def _pair_pitch_order(ga, gb, sa, sb) -> tuple[float, float]:

    """Score two voices for 'which is higher': compare pitches where both

    sound at the same onset; fall back to mean pitch, then stems, then order."""

    def onsets(g):

        d: dict[float, float] = {}

        for off, el in g:

            if _is_sounding(el):

                ps = [p.ps for p in el.pitches]

                d[round(off, 4)] = sum(ps) / len(ps)

        return d

    oa, ob = onsets(ga), onsets(gb)

    common = set(oa) & set(ob)

    up = sum(1 for k in common if oa[k] > ob[k] + 0.01)

    down = sum(1 for k in common if ob[k] > oa[k] + 0.01)

    if up != down:

        return (1.0, 0.0) if up > down else (0.0, 1.0)

    if sa[0] is not None and sb[0] is not None and abs(sa[0] - sb[0]) > 0.01:

        return sa[0], sb[0]

    if sa[1] != sb[1]:

        return (1.0, 0.0) if sa[1] > sb[1] else (0.0, 1.0)

    return 1.0, 0.0





def _order_groups(

    groups: list[list[tuple[float, note.GeneralNote]]],

) -> tuple[list, list, list] | None:

    """Pick (upper, lower, extras) voice groups by PITCH, not voice number/order.



    Returns None when the bar has fewer than two voice groups (caller treats

    the staff as unvoiced). Sounding groups are ranked by mean pitch (higher =

    upper); exact ties fall back to stem direction, then original order. When

    only one group sounds and the partner holds only rests, stem direction

    decides whether the sounding line is the upper or the lower voice.

    """

    if len(groups) < 2:

        return None

    stats = [_group_stats(g) for g in groups]

    sounding = [i for i, (avg, _) in enumerate(stats) if avg is not None]

    silent = [i for i, (avg, _) in enumerate(stats) if avg is None]

    if len(sounding) >= 2:

        # The first two sounding voices are the main pair (Audiveris puts the

        # continuous lines first); which one is UPPER is decided by pitch.

        a, b = sounding[0], sounding[1]

        pa, pb = _pair_pitch_order(groups[a], groups[b], stats[a], stats[b])

        upper_i, lower_i = (a, b) if pa >= pb else (b, a)

        extras = [groups[i] for i in sounding[2:]]

        return groups[upper_i], groups[lower_i], extras

    if len(sounding) == 1 and silent:

        si = sounding[0]

        partner = silent[0]

        stem = stats[si][1]

        if stem < 0:

            upper_is_sounding = False

        elif stem > 0:

            upper_is_sounding = True

        else:

            upper_is_sounding = si < partner

        if upper_is_sounding:

            return groups[si], groups[partner], []

        return groups[partner], groups[si], []

    return None





def _stem_of(el: note.GeneralNote) -> str | None:

    return chord_stem(el)





def _dedup_candidates(raw_cands: list[PitchCandidate]) -> list[PitchCandidate]:

    """Deduplicate simultaneous pitches by semitone (high->low), preferring lyric/single-note sources."""

    best_by_ps: dict[float, tuple[tuple, PitchCandidate]] = {}

    for c in raw_cands:

        k = round(float(c.pitch.ps), 2)

        rank = (
            1 if getattr(c.src_el, "lyrics", None) else 0,
            1 if (not isinstance(c.src_el, chord.Chord) or len(c.src_el.pitches) == 1) else 0,
            1 if c.stem in ("up", "down") else 0,
            1 if c.tie_type in ("start", "continue", "stop") else 0,
            1 if c.group_target in ("U", "L") else 0,
        )

        prev = best_by_ps.get(k)

        if prev is None or rank > prev[0]:

            best_by_ps[k] = (rank, c)

    return [t[1] for t in sorted(best_by_ps.values(), key=lambda kv: -kv[1].pitch.ps)]


def _onset_extremes(events: list[tuple[float, note.GeneralNote]]) -> dict[float, tuple[float | None, float | None]]:

    """Map rounded onset -> (max_ps, min_ps) across sounding events in a measure."""

    by_off: dict[float, list[float]] = {}

    for off, el in events:

        if not _is_sounding(el):

            continue

        k = round(float(off), 4)

        by_off.setdefault(k, []).extend(p.ps for p in _pitches_desc(el))

    return {k: (max(ps), min(ps)) for k, ps in by_off.items() if ps}


def _update_vl_at_off(off: float, upper: _Line, lower: _Line, ctx: _Ctx) -> None:

    if ctx.vl_state is None:

        return

    u_it = upper.starting_at(off)

    l_it = lower.starting_at(off)

    u_ps = _el_pitches(u_it[2]) if u_it is not None else []

    l_ps = _el_pitches(l_it[2]) if l_it is not None else []

    u_tie = any(candidate_tie_type(u_it[2], p) in ("start", "continue") for p in u_ps) if u_it else False

    l_tie = any(candidate_tie_type(l_it[2], p) in ("start", "continue") for p in l_ps) if l_it else False

    ctx.vl_state.update(u_ps, l_ps, upper_tie_out=u_tie, lower_tie_out=l_tie)


def _apply_resolved_chord(

    raw_cands: list[PitchCandidate],

    off: float,

    upper: _Line,

    lower: _Line,

    ctx: _Ctx,

    *,

    next_upper_ps: float | None = None,

    next_lower_ps: float | None = None,

    placed_group: dict[int, int] | None = None,

    placed_stem: dict[int, str | None] | None = None,

    group_of: dict[int, int] | None = None,

) -> None:

    deduped = _dedup_candidates(raw_cands)

    def _record_placed(line: _Line, cand: PitchCandidate) -> None:
        if placed_group is not None and group_of is not None and id(cand.src_el) in group_of:
            placed_group[id(line.items[-1][2])] = group_of[id(cand.src_el)]
        if placed_stem is not None:
            placed_stem[id(line.items[-1][2])] = cand.stem

    if not deduped:
        return

    if len(deduped) == 1:
        c0 = deduped[0]
        _place_unison(c0.pitch, c0.src_el, off, upper, lower, ctx)
        for line in (upper, lower):
            if line.items and abs(line.items[-1][0] - off) < EPS:
                _record_placed(line, c0)
        _update_vl_at_off(off, upper, lower, ctx)
        return

    if len(deduped) == 2:
        c_u, c_l = deduped[0], deduped[1]
        upper.place(off, c_u.src_el, c_u.pitch)
        _record_placed(upper, c_u)
        lower.place(off, c_l.src_el, c_l.pitch)
        _record_placed(lower, c_l)
        _update_vl_at_off(off, upper, lower, ctx)
        return

    vl_state = ctx.vl_state if ctx.vl_state is not None else VoiceLeadingState()

    chosen_u, chosen_l, choice = resolve_simultaneous_chord(
        deduped,
        measure=ctx.measure or 1,
        part_index=ctx.part_index,
        offset=off,
        upper_role=ctx.upper_role,
        lower_role=ctx.lower_role,
        state=vl_state,
        next_upper_ps=next_upper_ps,
        next_lower_ps=next_lower_ps,
        cfg=ctx.cfg,
    )

    for idx_c, cand in enumerate(chosen_u):

        if idx_c == 0:

            upper.place(off, cand.src_el, cand.pitch)

            if placed_group is not None and group_of is not None and id(cand.src_el) in group_of:

                placed_group[id(upper.items[-1][2])] = group_of[id(cand.src_el)]

            if placed_stem is not None:

                placed_stem[id(upper.items[-1][2])] = cand.stem

        else:

            upper.add_divisi(upper.items[-1], cand.pitch, cand.src_el)

            ctx.divisi.append(f"{cand.pitch.nameWithOctave}->upper")

    for idx_c, cand in enumerate(chosen_l):

        if idx_c == 0:

            lower.place(off, cand.src_el, cand.pitch)

            if placed_group is not None and group_of is not None and id(cand.src_el) in group_of:

                placed_group[id(lower.items[-1][2])] = group_of[id(cand.src_el)]

            if placed_stem is not None:

                placed_stem[id(lower.items[-1][2])] = cand.stem

        else:

            lower.add_divisi(lower.items[-1], cand.pitch, cand.src_el)

            ctx.divisi.append(f"{cand.pitch.nameWithOctave}->lower")

    if ctx.divisi_choices is not None:

        ctx.divisi_choices.append(choice)

    if choice.ambiguous:

        ctx.divisi_ambig.append(
            f"{choice.choice_key}:{','.join(choice.chosen_upper)}/{','.join(choice.chosen_lower)}"
        )

    if ctx.vl_state is not None:

        u_tie = any(candidate_tie_type(c.src_el, c.pitch) in ("start", "continue") for c in chosen_u)

        l_tie = any(candidate_tie_type(c.src_el, c.pitch) in ("start", "continue") for c in chosen_l)

        ctx.vl_state.update(
            [c.pitch for c in chosen_u],
            [c.pitch for c in chosen_l],
            upper_tie_out=u_tie,
            lower_tie_out=l_tie,
        )


def _fill_pair(m: stream.Measure, upper: _Line, lower: _Line, ctx: _Ctx) -> None:

    """Split one staff bar into an upper and a lower monophonic line.



    With 2+ notated voices, each note goes to the line given by its **stem

    direction** (up = upper voice, down = lower voice - the engraving

    convention for two voices on one staff, and robust to Audiveris switching

    voice numbers mid-bar). Notes without a stem (whole notes) follow their

    voice, whose upper/lower role is decided by pitch (_order_groups).

    Simultaneous 3+ note chords are routed by ``split/divisi.py`` using

    multi-cue ownership + Option B monophonic collapse (unless KEEP_DIVISI).

    """

    groups, loose = _measure_groups(m)

    ordered = _order_groups(groups)

    all_ev = [ev for g in groups for ev in g] + loose

    extremes = _onset_extremes(all_ev)

    sorted_keys = sorted(extremes)

    next_map: dict[float, tuple[float | None, float | None]] = {}

    for idx_k, ok in enumerate(sorted_keys):

        if idx_k + 1 < len(sorted_keys):

            next_map[ok] = extremes[sorted_keys[idx_k + 1]]

        else:

            next_map[ok] = (ctx.bar_next_upper, ctx.bar_next_lower)

    if ordered is None:

        by_off: dict[float, list[tuple[float, note.GeneralNote]]] = {}

        for off, el in sorted(all_ev, key=lambda t: t[0]):

            if _is_sounding(el):

                by_off.setdefault(round(float(off), 4), []).append((off, el))

        for ok in sorted(by_off):

            evs = by_off[ok]

            off0 = evs[0][0]

            lines_free = (
                not any(s_ - EPS <= off0 and e_ > off0 + EPS for s_, e_, _ in upper.items)
                and not any(s_ - EPS <= off0 and e_ > off0 + EPS for s_, e_, _ in lower.items)
                and upper.starting_at(off0) is None
                and lower.starting_at(off0) is None
            )

            raw_cands = [
                PitchCandidate(
                    pitch=p,
                    src_el=el,
                    stem=_stem_of(el),
                    group_target=None,
                    tie_type=candidate_tie_type(el, p),
                )
                for _o, el in evs
                for p in _pitches_desc(el)
            ]

            dedup = _dedup_candidates(raw_cands)

            if lines_free and len(dedup) >= 3:

                nx_u, nx_l = next_map.get(ok, (ctx.bar_next_upper, ctx.bar_next_lower))

                _apply_resolved_chord(dedup, off0, upper, lower, ctx, next_upper_ps=nx_u, next_lower_ps=nx_l)

            else:

                for off, el in evs:

                    _place_flexible(off, el, _pitches_desc(el), upper, lower, ctx, origin="chord")

                _update_vl_at_off(off0, upper, lower, ctx)

        return



    primary, secondary, extra_groups = ordered

    n_sounding = sum(1 for g in (primary, secondary) if any(_is_sounding(el) for _, el in g))

    use_stems = n_sounding >= 2

    targeted_by_off: dict[float, list[tuple[float, int, note.GeneralNote, str]]] = {}

    free_by_off: dict[float, list[tuple[float, note.GeneralNote]]] = {}

    group_of: dict[int, int] = {}

    placed_group: dict[int, int] = {}

    placed_stem: dict[int, str | None] = {}

    for gi, g in enumerate([primary, secondary] + list(extra_groups)):

        for _, el in g:

            group_of[id(el)] = gi

    for g, default in [(primary, "U"), (secondary, "L")] + [(g, None) for g in extra_groups]:

        for off, el in g:

            if not _is_sounding(el):

                continue

            stem = _stem_of(el) if use_stems else None

            target = {"up": "U", "down": "L"}.get(stem or "", default)

            ok = round(float(off), 4)

            if target is None:

                free_by_off.setdefault(ok, []).append((off, el))

            else:

                targeted_by_off.setdefault(ok, []).append((off, 0 if target == "U" else 1, el, target))

    for off, el in loose:

        if _is_sounding(el):

            free_by_off.setdefault(round(float(off), 4), []).append((off, el))



    all_keys = sorted(set(targeted_by_off) | set(free_by_off))

    for ok in all_keys:

        t_evs = sorted(targeted_by_off.get(ok, []), key=lambda t: (t[0], t[1]))

        f_evs = free_by_off.get(ok, [])

        off0 = t_evs[0][0] if t_evs else f_evs[0][0]

        lines_free = (
            not any(s_ - EPS <= off0 and e_ > off0 + EPS for s_, e_, _ in upper.items)
            and not any(s_ - EPS <= off0 and e_ > off0 + EPS for s_, e_, _ in lower.items)
            and upper.starting_at(off0) is None
            and lower.starting_at(off0) is None
        )

        raw_cands: list[PitchCandidate] = []

        for _off, _pr, el, tgt in t_evs:

            for p in _pitches_desc(el):

                raw_cands.append(
                    PitchCandidate(
                        pitch=p,
                        src_el=el,
                        stem=_stem_of(el),
                        group_target=tgt,
                        tie_type=candidate_tie_type(el, p),
                    )
                )

        for _off, el in f_evs:

            for p in _pitches_desc(el):

                raw_cands.append(
                    PitchCandidate(
                        pitch=p,
                        src_el=el,
                        stem=_stem_of(el),
                        group_target=None,
                        tie_type=candidate_tie_type(el, p),
                    )
                )

        dedup = _dedup_candidates(raw_cands)

        if lines_free and len(dedup) >= 3:

            nx_u, nx_l = next_map.get(ok, (ctx.bar_next_upper, ctx.bar_next_lower))

            _apply_resolved_chord(
                dedup,
                off0,
                upper,
                lower,
                ctx,
                next_upper_ps=nx_u,
                next_lower_ps=nx_l,
                placed_group=placed_group,
                placed_stem=placed_stem,
                group_of=group_of,
            )

            continue

        leftovers: list[tuple[float, note.GeneralNote, list, str | None]] = []

        for off, _, el, target in t_evs:

            ps = _pitches_desc(el)

            line = upper if target == "U" else lower

            mine, rest = (ps[0], ps[1:]) if target == "U" else (ps[-1], ps[:-1])

            gid = group_of.get(id(el))

            my_stem = _stem_of(el)

            if gid in (0, 1) and any(
                s_ < off - EPS and e_ > off + EPS
                and placed_group.get(id(n_)) in (0, 1)
                and placed_group.get(id(n_)) != gid
                and placed_stem.get(id(n_)) != my_stem
                for s_, e_, n_ in line.items
            ):

                leftovers.append((off, el, ps, target))

                continue

            clash = line.starting_at(off)

            if clash is not None:

                c_s, c_e, c_n = clash

                c_ps = _el_pitches(c_n)

                c_out = max(c_ps, key=lambda p: p.ps) if target == "U" else min(c_ps, key=lambda p: p.ps)

                better = mine.ps > c_out.ps if target == "U" else mine.ps < c_out.ps

                if not better:

                    leftovers.append((off, el, ps, target))

                    continue

                line.items.remove(clash)

                leftovers.append((c_s, c_n, sorted(c_ps, key=lambda p: -p.ps), target))

            line.place(off, el, mine)

            if gid is not None:

                placed_group[id(line.items[-1][2])] = gid

                placed_stem[id(line.items[-1][2])] = _stem_of(el)

            if rest:

                leftovers.append((off, el, rest, target))

        for off, el, rem, owner in leftovers:

            _place_flexible(
                off, el, rem, upper, lower, ctx,
                origin="chord in voice", prefer=owner,
                placed_group=placed_group, placed_stem=placed_stem, group_of=group_of,
            )

        for off, el in f_evs:

            _place_flexible(
                off, el, _pitches_desc(el), upper, lower, ctx,
                origin="extra voice",
                placed_group=placed_group, placed_stem=placed_stem, group_of=group_of,
            )

        _update_vl_at_off(off0, upper, lower, ctx)





def _fill_mono(m: stream.Measure, line: _Line, prefer_low: bool, ctx: _Ctx) -> None:

    groups, loose = _measure_groups(m)

    events: list[tuple[float, int, note.GeneralNote]] = []

    for gi, g in enumerate(groups):

        events.extend((off, gi, el) for off, el in g)

    events.extend((off, len(groups), el) for off, el in loose)

    for off, _, el in sorted(events, key=lambda t: (t[0], t[1])):

        if not _is_sounding(el):

            continue

        ps = _pitches_desc(el)

        if prefer_low:

            ps = list(reversed(ps))

        end = off + float(el.quarterLength)

        rest = ps

        if line.is_free(off, end):

            line.place(off, el, ps[0])

            rest = ps[1:]

        rest = [p for p in rest if not line.sounds(p, off)]

        dropped = [p for p in rest if not _divisi_into(off, p, [("line", line)], ctx, el)]

        if dropped and getattr(getattr(ctx.cfg, "graft", None), "b15_f7_chord_tie_split", False):

            dropped = [p for p in dropped if not _tie_split_into(off, el, p, [("line", line)], ctx)]

        if dropped:

            ctx.flag(

                off,

                "dropped_pitch",

                "single-voice staff: not kept " + ", ".join(p.nameWithOctave for p in dropped),

            )





# ---------------------------------------------------------------------------

# Rendering a line into a full, gap-free measure

# ---------------------------------------------------------------------------



def _render_measure(

    src_m: stream.Measure | None,

    line: _Line,

    target: float,

    number: int | None,

    extra_context: list,

    ctx: _Ctx,

    role: str,

) -> stream.Measure:

    new_m = stream.Measure(number=number)

    present: set[type] = set()

    if src_m is not None:

        for el in src_m.getElementsByClass(_CONTEXT_CLASSES):

            if isinstance(el, meter.TimeSignature):

                continue  # time signatures come from the shared grid

            new_m.insert(el.offset, deepcopy(el))

            present.add(_ctx_family(el))

    for el in extra_context:

        fam = _ctx_family(el)

        if fam not in present:

            new_m.insert(0, deepcopy(el))

            present.add(fam)



    placed: list[tuple[float, note.GeneralNote]] = []

    cursor = 0.0

    for s, e, n in sorted(line.items, key=lambda t: t[0]):

        if s >= target - EPS:

            ctx.flag(s, "overflow_dropped", f"{role}: {_el_name(n)} starts after barline")

            continue

        if s < cursor - EPS and placed:

            ps, pn = placed[-1]

            new_len = s - ps

            if new_len <= EPS:

                placed.pop()

            else:

                pn.quarterLength = new_len

            cursor = s

        if s > cursor + EPS:

            placed.append((cursor, note.Rest(quarterLength=s - cursor)))

        if e > target + EPS:

            n.quarterLength = target - s

            e = target

            ctx.flag(s, "clipped_at_barline", f"{role}: {_el_name(n)} shortened to fit bar")

        placed.append((s, n))

        cursor = e

    if cursor < target - EPS:

        placed.append((cursor, note.Rest(quarterLength=target - cursor)))

    placed = _merge_tied_tuplet_fragments(placed)

    for s, el in placed:

        new_m.insert(s, el)

    return new_m





def _plain_duration(ql: float):

    """A tuplet-free Duration for ``ql`` or None (e.g. 1.0 -> quarter)."""

    from music21 import duration as m21dur



    try:

        d = m21dur.Duration(ql)

    except Exception:  # noqa: BLE001

        return None

    if d.tuplets or d.type in ("complex", "inexpressible", "zero"):

        return None

    return d





def _merge_tied_tuplet_fragments(placed: list) -> list:

    """Join a tied same-pitch pair of *tuplet* notes into one plain note when

    their sum is a plain value (OMR triplet 1/3 + 2/3 tied = one quarter).



    An incomplete tuplet group (its other notes went to another voice) is

    re-barred by the MusicXML exporter into a tie that ends on a rest / the

    next bar's different pitch (Hard merged bar 315 -> 316, BUG-3 leftover).

    """

    from music21 import tie as m21tie



    out = list(placed)

    i = 0

    while i + 1 < len(out):

        s1, a = out[i]

        s2, b = out[i + 1]

        if (

            isinstance(a, note.Note) and isinstance(b, note.Note)

            and a.tie is not None and a.tie.type in ("start", "continue")

            and b.tie is not None and b.tie.type in ("stop", "continue")

            and abs(a.pitch.ps - b.pitch.ps) < 0.01

            and abs(s1 + float(a.quarterLength) - s2) < EPS

            and (a.duration.tuplets or b.duration.tuplets)

        ):

            d = _plain_duration(float(a.quarterLength) + float(b.quarterLength))

            if d is not None:

                a.duration = d

                starts = a.tie.type == "continue"

                goes_on = b.tie.type == "continue"

                a.tie = (m21tie.Tie("continue") if starts and goes_on else

                         m21tie.Tie("start") if goes_on else

                         m21tie.Tie("stop") if starts else None)

                out.pop(i + 1)

                continue

        i += 1

    return out





def _ctx_family(el) -> type:

    for cls in _CONTEXT_CLASSES:

        if isinstance(el, cls):

            return cls

    return type(el)





# ---------------------------------------------------------------------------

# Single-staff sections (unison / S.A / trio / solo systems)

# ---------------------------------------------------------------------------



@dataclass(frozen=True)

class _Section:

    """How one bar of a single-staff section is routed.



    kind: "omit" (solo -> rests everywhere), "roles" (split into ``roles``,

    high -> low) or "unison" (S/A as written, T/B an octave lower when the

    bar is in treble clef).

    """



    kind: str

    roles: tuple[VoiceRole, ...]

    label: str

    octave_down_if_treble: bool = False

    #: Batch 12: a solo routed to its named voice (flag ``solo_routed`` per bar).

    solo: bool = False





_VOICE_WORDS: dict[str, VoiceRole] = {

    "s": "Soprano", "sop": "Soprano", "sopr": "Soprano", "sopran": "Soprano",

    "soprano": "Soprano", "soprana": "Soprano",

    "a": "Alto", "alt": "Alto", "alto": "Alto", "contralto": "Alto",

    "t": "Tenor", "ten": "Tenor", "tenor": "Tenor", "tenore": "Tenor",

    "b": "Bass", "bas": "Bass", "bass": "Bass", "basso": "Bass",

    "bar": "Bass", "bariton": "Bass", "baritone": "Bass",

}

_LETTER_ROLE: dict[str, VoiceRole] = {"s": "Soprano", "a": "Alto", "t": "Tenor", "b": "Bass"}

_UNISON_RE = re.compile(r"\b(unison|unisono|unis|tutti|toti|all voices)\b")

_SOLO_RE = re.compile(r"\bsol[oi]\b\W*([a-z]*)")

_PRE_SOLO_RE = re.compile(r"\b([a-z]+)\s+sol[oi]\b")

_GUITAR_RE = re.compile(r"\b(?:ac\.?\s*)?(?:guitar|chitara)\b")

# Romanian / English cue words: "femeile" (the women) -> S/A,

# "barbatii" (the men; diacritics are stripped first, so all spellings match) -> T/B.

_WOMEN_RE = re.compile(r"\b(femei|femeile|femeilor|femeii|women|ladies|sopranele|altistele)\b")

_MEN_RE = re.compile(r"\b(barbati|barbatii|barbatilor|men|gentlemen|tenorii|basii)\b")

_LETTERS_RE = re.compile(

    r"(?<![a-z])([satb])\s*[.,]\s*([satb])(?:\s*[.,]\s*([satb]))?(?:\s*[.,]\s*([satb]))?\s*\.?(?![a-z])"

)





def _norm_text(text: str) -> str:

    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii")

    return re.sub(r"\s+", " ", t.lower()).strip()





def parse_section_marker(text: str, solo_policy: str = "route_named") -> _Section | None:

    """Recognise a printed section marker ('unison', 'S.A', 'solo tenor' ...)."""

    t = _norm_text(text)

    if not t:

        return None

    m = _SOLO_RE.search(t)

    is_unison = bool(_UNISON_RE.search(t) or re.search(r"\bcor\s*unison\b", t))

    if m and (is_unison or re.search(r"\btuti\b", t)):

        return None  # "solo - tutti": alternation instruction, not a routing marker

    if m:

        voice = _VOICE_WORDS.get(m.group(1)) if m.group(1) else None

        if voice is None:

            pre = _PRE_SOLO_RE.search(t)

            if pre:

                voice = _VOICE_WORDS.get(pre.group(1))

        if voice is not None and solo_policy == "route_named":

            return _Section("roles", (voice,), f"solo {voice.lower()} -> {voice}", solo=True)

        return _Section("omit", (), "solo (not a choir part) -> omitted")

    if is_unison:

        return _Section("unison", ROLES, "unison -> S/A/T/B (T/B 8vb)")

    if len(re.findall(r"[a-z]+", t)) <= 3:  # a cue word, not a lyric line

        women, men = _WOMEN_RE.search(t), _MEN_RE.search(t)

        if women and men:

            return _Section("unison", ROLES, "women + men -> S/A/T/B (T/B 8vb)")

        if women:

            return _Section("roles", ("Soprano", "Alto"), f"'{women.group(1)}' (women) -> S/A")

        if men:

            return _Section("roles", ("Tenor", "Bass"), f"'{men.group(1)}' (men) -> T/B",

                            octave_down_if_treble=True)

    m = _LETTERS_RE.search(t)

    if m:

        letters = {g for g in m.groups() if g}

        roles = tuple(r for r in ROLES if any(_LETTER_ROLE[l] == r for l in letters))

        if len(roles) >= 2:

            return _Section("roles", roles, "/".join(r[0] for r in roles) + " section")

    if re.search(r"\btrio\b", t):

        return _Section("roles", ("Soprano", "Alto", "Tenor"), "trio -> S/A/T")

    if re.search(r"\bduet\b", t):

        return _Section("roles", ("Soprano", "Alto"), "duet -> S/A (assumed)")

    if _GUITAR_RE.search(t):

        return _Section("omit", (), "accompaniment (guitar) -> omitted")

    return None





def _markers_by_index(parts: list[stream.Part]) -> dict[int, list[str]]:

    out: dict[int, list[str]] = {}

    for p in parts:

        for i, m in enumerate(p.getElementsByClass(stream.Measure)):

            for te in m.recurse().getElementsByClass(expressions.TextExpression):

                if te.content:

                    out.setdefault(i, []).append(str(te.content))

    return out





def _section_plan(

    src: stream.Part,

    kinds: list[str | None],

    staff_kind: str | None,

    pair: StaffPair,

    parts: list[stream.Part],

    single_part: bool,

    cfg: SplitConfig,

    *,

    sa_part: stream.Part | None = None,

    shifted_3to2_bars: set[int] | frozenset[int] = frozenset(),

) -> list[_Section | None]:

    """Per-bar routing for single-staff sections of this staff (None = normal)."""

    n = len(kinds)

    plan: list[_Section | None] = [None] * n

    own = _markers_by_index([src])

    others = _markers_by_index([p for p in parts if p is not src]) if not single_part else {}

    src_ms = list(src.getElementsByClass(stream.Measure))

    sa_ms = list(sa_part.getElementsByClass(stream.Measure)) if sa_part is not None else None



    def _scan_markers(bucket: dict[int, list[str]], i: int) -> _Section | None:

        found = None

        for txt in bucket.get(i, []):

            sec = parse_section_marker(txt, cfg.solo_policy)

            if sec is not None:

                found = sec

        return found

    def own_marker_at(i: int) -> _Section | None:

        return _scan_markers(own, i)

    def other_marker_at(i: int) -> _Section | None:

        return _scan_markers(others, i)

    def marker_at(i: int) -> _Section | None:

        found = None

        for txt in own.get(i, []) + others.get(i, []):

            sec = parse_section_marker(txt, cfg.solo_policy)

            if sec is not None:

                found = sec

        return found



    if single_part:

        # One-staff page: markers stay active until the next marker.

        active: _Section | None = None

        for i in range(n):

            sec = marker_at(i)

            if sec is not None:

                active = sec

            plan[i] = active

        if first_sounding_kind(src) == "G8":

            # A plain "solo" over a treble-8vb staff is a tenor solo (Hard

            # p10 mvt2): route it to Tenor instead of omitting it.

            tenor_solo = _Section("roles", ("Tenor",), "solo on treble-8vb staff -> Tenor", solo=True)

            plan = [tenor_solo if (p is not None and p.kind == "omit") else p for p in plan]

        return plan



    if staff_kind != "F":

        return plan

    # Treble-clef runs on a bass-clef choir staff = a single-staff system

    # (unison / S.A / solo) that Audiveris attached to the TB staff.

    i = 0

    while i < n:

        if kinds[i] != "G":

            i += 1

            continue

        j = i

        while j < n and kinds[j] == "G":

            j += 1

        active = _Section("roles", ("Soprano", "Alto"), "unmarked single-staff treble system -> S/A (assumed)")
        explicit = False
        pending_other: _Section | None = None

        for k in range(i, j):
            sa_sounds = sa_ms is not None and k < len(sa_ms) and _has_notes(sa_ms[k])
            own_sec = own_marker_at(k)
            other_sec = other_marker_at(k)
            if own_sec is not None:
                active = own_sec
                explicit = True
                pending_other = None
            elif other_sec is not None:
                if sa_sounds and k not in shifted_3to2_bars:
                    pending_other = other_sec
                else:
                    active = other_sec
                    explicit = True
                    pending_other = None
            elif not sa_sounds and pending_other is not None:
                active = pending_other
                explicit = True
                pending_other = None

            if k in shifted_3to2_bars:
                plan[k] = _Section(
                    "roles", ("Soprano", "Alto"),
                    "bottom-aligned 3->2 choir SA on lower staff",
                )
                continue

            if not explicit and sa_sounds:
                tb_m = src_ms[k] if k < len(src_ms) else None
                has_midbar_f = tb_m is not None and any(
                    clef_kind(c) == "F" and float(c.offset) > EPS
                    for c in tb_m.getElementsByClass(clef.Clef)
                )
                if has_midbar_f or (k == 0 and j < n and kinds[j] == "F"):
                    plan[k] = None
                    continue

            plan[k] = active

        i = j

    # Batch 12: a treble-8vb run on the bass-clef choir staff is a single-staff

    # tenor system (Hard p11 printed 38-46 "solo"). Only a printed marker

    # routes it: plain "solo" -> Tenor only, "solo bass" -> Bass ...; the other

    # voice of the staff rests. Unmarked runs keep the normal T/B split.

    i = 0

    while i < n:

        if kinds[i] != "G8":

            i += 1

            continue

        j = i

        while j < n and kinds[j] == "G8":

            j += 1

        active = None

        for k in range(i, j):

            sec = marker_at(k)

            if sec is not None:

                if sec.kind == "omit":

                    sec = _Section("roles", ("Tenor",), "solo on treble-8vb staff -> Tenor", solo=True)

                active = sec

            plan[k] = active

        i = j

    return plan





def _all_events(m: stream.Measure) -> list[tuple[float, note.GeneralNote]]:

    groups, loose = _measure_groups(m)

    return sorted([ev for g in groups for ev in g] + loose, key=lambda t: t[0])





def _fill_multi(m: stream.Measure, roles: tuple[VoiceRole, ...], lines: dict, ctx: _Ctx) -> None:

    """Distribute simultaneous pitches over 3+ roles (high -> low)."""

    by_onset: dict[float, list[tuple[object, note.GeneralNote]]] = {}

    exact: dict[float, float] = {}

    for off, el in _all_events(m):

        if not _is_sounding(el):

            continue

        key_ = round(off, 4)

        exact.setdefault(key_, off)

        for p in _pitches_desc(el):

            by_onset.setdefault(key_, []).append((p, el))

    k = len(roles)

    for key_ in sorted(by_onset):

        off = exact[key_]

        seen: set[float] = set()

        plist = []

        for p, el in sorted(by_onset[key_], key=lambda t: -t[0].ps):

            if round(p.ps, 2) in seen:

                continue

            seen.add(round(p.ps, 2))

            plist.append((p, el))

        npit = len(plist)

        used: set[int] = set()

        for j, role in enumerate(roles):

            idx = 0 if npit == 1 else round(j * (npit - 1) / (k - 1)) if k > 1 else 0

            p, el = plist[idx]

            end = off + float(el.quarterLength)

            if lines[role].is_free(off, end):

                lines[role].place(off, el, p)

                used.add(idx)

        cand = [(r, lines[r]) for r in roles]

        dropped = [

            plist[i][0] for i in range(npit)

            if i not in used

            and not any(lines[r].sounds(plist[i][0], off) for r in roles)

            and not _divisi_into(off, plist[i][0], cand, ctx, plist[i][1])

        ]

        if dropped:

            ctx.flag(off, "dropped_pitch", f"{'/'.join(r[0] for r in roles)} section: not kept "

                     + ", ".join(p.nameWithOctave for p in dropped))





def _shifted(line: _Line, semitones: int) -> _Line:

    out = _Line()

    for s_, e_, n_ in line.items:

        c = deepcopy(n_)

        if isinstance(c, chord.Chord):

            c.transpose(semitones, inPlace=True)

        else:

            c.pitch = c.pitch.transpose(semitones)

        out.items.append((s_, e_, c))

    return out





def _fill_section(

    m: stream.Measure, sec: _Section, bar_kind: str | None, ctx: _Ctx

) -> dict[VoiceRole, _Line]:

    lines: dict[VoiceRole, _Line] = {}

    if sec.kind == "omit":

        return lines

    if sec.kind == "unison":

        s_line, a_line = _Line(), _Line()

        _fill_pair(m, s_line, a_line, ctx)

        shift = -12 if bar_kind in ("G", None) else 0

        lines["Soprano"], lines["Alto"] = s_line, a_line

        lines["Tenor"], lines["Bass"] = _shifted(s_line, shift), _shifted(a_line, shift)

        return lines

    roles = sec.roles

    for r in roles:

        lines[r] = _Line()

    if len(roles) == 1:

        _fill_mono(m, lines[roles[0]], prefer_low=(roles[0] == "Bass"), ctx=ctx)

    elif len(roles) == 2:

        _fill_pair(m, lines[roles[0]], lines[roles[1]], ctx)

    else:

        _fill_multi(m, roles, lines, ctx)

    if sec.octave_down_if_treble and bar_kind == "G" and any(line.items for line in lines.values()):

        lines = {r: _shifted(line, -12) for r, line in lines.items()}

        ctx.flag(None, "octave_down_assumed",

                 "CHECK BY EAR/SCORE: men's section printed on a plain treble staff; moved down an "

                 "octave for T/B (assumption, not printed).")

    return lines





def _source_extent(m: stream.Measure) -> float:

    extra = getattr(m, "satbSourceExtent", None)

    if extra is not None:

        try:

            return float(extra)

        except (TypeError, ValueError):

            pass

    return _note_extent(m)





def _pick_conflict_candidate(

    sounding: list[tuple[int, int, stream.Measure]],

    expected_len: float,

) -> tuple[int, int, stream.Measure]:

    """Choose among staff candidates for one role in one bar.



    Prefer a bar whose *source* notes fit the expected/printed meter. A

    solo overlay that overshoots (Hard p14: melody dumped on the SA staff)

    loses to a staff that fits. Owner-vs-routed rank is the tie-breaker

    only when meter-fit is equal. Rendered measures may already be clipped

    to the grid length, so the original extent is read from

    ``satbSourceExtent`` when present.

    """



    def key(t: tuple[int, int, stream.Measure]) -> tuple:

        rank, _idx, meas = t

        extent = _source_extent(meas)

        if expected_len > EPS:

            fits = extent <= expected_len + EPS

            overshoot = max(0.0, extent - expected_len)

        else:

            fits = True

            overshoot = 0.0

        return (0 if fits else 1, overshoot, rank)



    return min(sounding, key=key)





def _has_notes(m: stream.Measure | None) -> bool:

    return m is not None and any(_is_sounding(el) for el in m.recurse().notesAndRests)





def _is_polyphonic_bar(m: stream.Measure) -> bool:
    """Return True if a measure carries 2+ simultaneous pitches or voices."""
    voices_with_notes = sum(
        1
        for v in m.getElementsByClass(stream.Voice)
        if any(_is_sounding(el) and getattr(el, "pitches", None) for el in v.recurse().notes)
    )
    if voices_with_notes >= 2:
        return True

    notes = [
        el for el in m.recurse().notes
        if _is_sounding(el) and getattr(el, "pitches", None)
    ]
    if any(
        isinstance(el, chord.Chord) and len({round(p.ps, 2) for p in el.pitches}) >= 2
        for el in notes
    ):
        return True

    onsets: dict[float, set[float]] = {}
    for el in notes:
        off = round(float(el.getOffsetInHierarchy(m)), 4)
        ps_set = onsets.setdefault(off, set())
        for p in el.pitches:
            ps_set.add(round(p.ps, 2))
    return any(len(ps_set) >= 2 for ps_set in onsets.values())


def _find_open_to_closed_runs(
    parts: list[stream.Part],
    active_pairs: list[StaffPair],
    grid: _Grid,
    cfg: SplitConfig,
) -> tuple[set[int], dict[int, tuple[VoiceRole, VoiceRole]]]:
    """Detect 4-staff open [S, A, T, B] bars bottom-aligned into 2-staff closed [SA, TB].

    Returns ``(bar_indices, {part_index: (upper_role, lower_role)})``.
    Guarded against normal open-score T+B duets where Tenor stays G8 or mono.
    """
    if len(active_pairs) != 4 or any(p.lower is not None for p in active_pairs):
        return set(), {}
    by_role = {p.upper: p for p in active_pairs if p.upper is not None}
    if set(by_role) != set(ROLES):
        return set(), {}
    p_s, p_a, p_t, p_b = by_role["Soprano"], by_role["Alto"], by_role["Tenor"], by_role["Bass"]
    if not (p_s.part_index < p_t.part_index and p_a.part_index < p_t.part_index and p_t.part_index < p_b.part_index):
        return set(), {}

    ms_s = list(parts[p_s.part_index].getElementsByClass(stream.Measure))
    ms_a = list(parts[p_a.part_index].getElementsByClass(stream.Measure))
    ms_t = list(parts[p_t.part_index].getElementsByClass(stream.Measure))
    ms_b = list(parts[p_b.part_index].getElementsByClass(stream.Measure))
    kinds_t = measure_clef_kinds(parts[p_t.part_index])
    kinds_b = measure_clef_kinds(parts[p_b.part_index])
    markers_t = _markers_by_index([parts[p_t.part_index]])
    markers_b = _markers_by_index([parts[p_b.part_index]])

    eligible: list[bool] = []
    for k in range(grid.count):
        ok = (
            k < len(ms_s) and not _has_notes(ms_s[k])
            and k < len(ms_a) and not _has_notes(ms_a[k])
            and k < len(ms_t) and k < len(kinds_t) and kinds_t[k] in ("G", None)
            and k < len(ms_b) and k < len(kinds_b) and kinds_b[k] == "F"
        )
        eligible.append(ok)

    bars: set[int] = set()
    i = 0
    while i < len(eligible):
        if not eligible[i]:
            i += 1
            continue
        j = i
        while j < len(eligible) and eligible[j]:
            j += 1
        sounding_t = sum(1 for k in range(i, j) if _has_notes(ms_t[k]))
        sounding_b = sum(1 for k in range(i, j) if _has_notes(ms_b[k]))
        if sounding_t > 0 or sounding_b > 0:
            poly_t = sum(1 for k in range(i, j) if _is_polyphonic_bar(ms_t[k]))
            poly_b = sum(1 for k in range(i, j) if _is_polyphonic_bar(ms_b[k]))
            has_closed_marker = False
            for k in range(i, j):
                for txt in markers_t.get(k, []):
                    sec = parse_section_marker(txt, cfg.solo_policy)
                    if sec is not None and sec.roles == ("Soprano", "Alto"):
                        has_closed_marker = True
                for txt in markers_b.get(k, []):
                    sec = parse_section_marker(txt, cfg.solo_policy)
                    if sec is not None and sec.roles == ("Tenor", "Bass"):
                        has_closed_marker = True
            if has_closed_marker or (poly_t >= 1 and poly_b >= 1 and (poly_t >= 2 or sounding_t <= 1)):
                bars.update(range(i, j))
        i = j

    if not bars:
        return set(), {}
    return bars, {
        p_t.part_index: ("Soprano", "Alto"),
        p_b.part_index: ("Tenor", "Bass"),
    }


def _find_bottom_aligned_3to2_runs(
    parts: list[stream.Part],
    active_pairs: list[StaffPair],
    ignored: set[int],
    grid: _Grid,
    cfg: SplitConfig,
) -> tuple[set[int], int | None]:
    """Detect 3->2 bottom-aligned shifts where ignored top rests and TB carries G-clef SA.

    Returns ``(bar_indices, sa_part_index)``.
    """
    sa = next((p for p in active_pairs if p.upper == "Soprano" and p.lower == "Alto"), None)
    tb = next((p for p in active_pairs if p.upper == "Tenor" and p.lower == "Bass"), None)
    if sa is None or tb is None or sa.part_index == 0 or tb.part_index <= sa.part_index:
        return set(), None
    ignored_above = [idx for idx in sorted(ignored) if idx < sa.part_index]
    if not ignored_above:
        return set(), None

    kinds_tb = measure_clef_kinds(parts[tb.part_index])
    if _plan_staff_kind(parts[tb.part_index], kinds_tb, tb, first_sounding_kind(parts[tb.part_index])) != "F":
        return set(), None

    ms_above = [list(parts[idx].getElementsByClass(stream.Measure)) for idx in ignored_above]
    ms_sa = list(parts[sa.part_index].getElementsByClass(stream.Measure))
    ms_tb = list(parts[tb.part_index].getElementsByClass(stream.Measure))
    kinds_sa = measure_clef_kinds(parts[sa.part_index])
    markers_tb = _markers_by_index([parts[tb.part_index]])

    eligible: list[bool] = []
    for k in range(grid.count):
        above_silent = all(k >= len(ms) or not _has_notes(ms[k]) for ms in ms_above)
        sa_g = k < len(ms_sa) and k < len(kinds_sa) and kinds_sa[k] in ("G", None)
        tb_g = k < len(ms_tb) and k < len(kinds_tb) and kinds_tb[k] == "G"
        any_sound = (k < len(ms_sa) and _has_notes(ms_sa[k])) or (k < len(ms_tb) and _has_notes(ms_tb[k]))
        tb_non_sa_marker = False
        for txt in markers_tb.get(k, []):
            sec = parse_section_marker(txt, cfg.solo_policy)
            if sec is not None and (sec.kind == "unison" or any(r in ("Tenor", "Bass") for r in sec.roles)):
                tb_non_sa_marker = True
        eligible.append(above_silent and sa_g and tb_g and any_sound and not tb_non_sa_marker)

    bars: set[int] = set()
    i = 0
    while i < len(eligible):
        if not eligible[i]:
            i += 1
            continue
        j = i
        while j < len(eligible) and eligible[j]:
            j += 1
        both = sum(1 for k in range(i, j) if _has_notes(ms_sa[k]) and _has_notes(ms_tb[k]))
        if both >= 2 and both / (j - i) >= 0.5:
            bars.update(range(i, j))
        i = j

    if not bars:
        return set(), None
    return bars, sa.part_index


# ---------------------------------------------------------------------------

# Per-staff split

# ---------------------------------------------------------------------------



def _split_part(

    src: stream.Part,

    grid: _Grid,

    pair: StaffPair,

    cfg: SplitConfig,

    flags: list[ReviewFlag],

    *,

    parts: list[stream.Part] | None = None,

    single_part: bool = False,

    divisi_choices: list[DivisiChoice] | None = None,

    partner_sa: stream.Part | None = None,

    shifted_3to2_bars: set[int] | frozenset[int] = frozenset(),

    sa_pair_index: int | None = None,

    open_closed_bars: set[int] | frozenset[int] = frozenset(),

    open_closed_roles: tuple[VoiceRole, VoiceRole] | None = None,

) -> dict[VoiceRole, list[stream.Measure | None]]:

    """Split one staff. Returns role -> per-bar measures (None = no material).



    The staff's own roles always get a measure for every source bar; other

    roles only get measures for bars routed to them by a single-staff

    section (unison, S.A.T., solo tenor ...).

    """

    measures = list(src.getElementsByClass(stream.Measure))

    part_ctx = [

        deepcopy(el)

        for el in src.getElementsByClass(_CONTEXT_CLASSES)

        if not isinstance(el, meter.TimeSignature)

    ]

    kinds = measure_clef_kinds(src)

    first_kind = first_sounding_kind(src)

    staff_kind = _plan_staff_kind(src, kinds, pair, first_kind)

    mono = pair.lower is None

    own_roles = pair.roles()

    out: dict[VoiceRole, list[stream.Measure | None]] = {r: [None] * grid.count for r in ROLES}

    plan = _section_plan(
        src, kinds, staff_kind, pair, parts or [src], single_part, cfg,
        sa_part=partner_sa,
        shifted_3to2_bars=shifted_3to2_bars,
    )

    prev_label: str | None = None

    printed_len: float | None = None

    bar_first_ext: list[tuple[float | None, float | None]] = []

    for m_look in measures[: grid.count]:

        gs_l, ls_l = _measure_groups(m_look)

        evs_l = [ev for g in gs_l for ev in g] + ls_l

        ext_l = _onset_extremes(evs_l)

        bar_first_ext.append(ext_l[min(ext_l)] if ext_l else (None, None))

    vl_state = VoiceLeadingState()
    oc_vl_state = VoiceLeadingState()



    for i, m in enumerate(measures[: grid.count]):

        # Flags use the 1-based bar POSITION in this file (bar 1 = first bar,

        # pickup included), the same numbering as the output voice files.

        # Audiveris numbers a pickup bar "0", so m.number is not used.

        next_u: float | None = None

        next_l: float | None = None

        for j_look in range(i + 1, len(bar_first_ext)):

            if bar_first_ext[j_look] != (None, None):

                next_u, next_l = bar_first_ext[j_look]

                break

        target = grid.lengths[i]

        ctx = _Ctx(

            measure=i + 1,

            part_index=pair.part_index,

            cfg=cfg,

            flags=flags,

            upper_role=pair.upper or "Soprano",

            lower_role=pair.lower or "Alto",

            vl_state=vl_state,

            bar_next_upper=next_u,

            bar_next_lower=next_l,

            divisi_choices=divisi_choices,

            target=target,

        )

        # Part-level context on bar 1, plus the grid's time/key signature so a

        # TS change printed on only one staff still reaches every voice.

        extra = (part_ctx if i == 0 else []) + list(grid.context[i])

        bar_kind = kinds[i] if i < len(kinds) else None

        sec = plan[i] if i < len(plan) else None

        sounding = any(_is_sounding(el) for el in m.recurse().notesAndRests)

        own_ts = sorted(m.getElementsByClass(meter.TimeSignature), key=lambda t: float(t.offset))

        if own_ts and float(own_ts[0].offset) <= EPS:

            try:

                printed_len = _ratio_len(own_ts[0].ratioString)

            except Exception:  # noqa: BLE001

                pass

        if i in open_closed_bars:
            if open_closed_roles is not None:
                prev_label = None
                oc_u, oc_l = open_closed_roles
                ctx_oc = _Ctx(
                    measure=i + 1,
                    part_index=pair.part_index,
                    cfg=cfg,
                    flags=flags,
                    upper_role=oc_u,
                    lower_role=oc_l,
                    vl_state=oc_vl_state,
                    bar_next_upper=next_u,
                    bar_next_lower=next_l,
                    divisi_choices=divisi_choices,
                    target=target,
                )
                if sounding:
                    _flag_underfull(m, _underfull_ref(printed_len, target), ctx_oc)
                u_line, l_line = _Line(), _Line()
                _fill_pair(m, u_line, l_line, ctx_oc)
                if ctx_oc.divisi:
                    ctx_oc.flag(
                        None,
                        "divisi_chord",
                        "Chord with more pitches than free voices: kept as divisi chord "
                        "(no pitch dropped): " + ", ".join(dict.fromkeys(ctx_oc.divisi)),
                    )
                if ctx_oc.divisi_ambig and cfg.divisi_mode != DivisiMode.KEEP_DIVISI:
                    ctx_oc.flag(
                        None,
                        "divisi_resolved",
                        "Ambiguous 3+ note chord resolved to 1 note per voice "
                        "(adjust in GUI 'Resolve 3-note chords' if needed): "
                        + ", ".join(dict.fromkeys(ctx_oc.divisi_ambig)),
                    )
                src_ext = _note_extent(m)
                for r in own_roles:
                    if r not in open_closed_roles:
                        out[r][i] = _rest_measure(grid, i)
                for r, line in ((oc_u, u_line), (oc_l, l_line)):
                    rm = _render_measure(m, line, target, m.number, extra, ctx_oc, r)
                    if r not in own_roles:
                        for c in list(rm.getElementsByClass(clef.Clef)):
                            rm.remove(c)
                    rm.satbSourceExtent = src_ext
                    out[r][i] = rm
            else:
                prev_label = None
                for r in own_roles:
                    out[r][i] = _rest_measure(grid, i)
            continue

        if i in shifted_3to2_bars and sa_pair_index is not None and pair.part_index == sa_pair_index:
            prev_label = None
            for r in own_roles:
                out[r][i] = _rest_measure(grid, i)
            continue

        if sounding:

            _flag_underfull(m, _underfull_ref(printed_len, target), ctx)



        if sec is not None:

            lines = _fill_section(m, sec, bar_kind, ctx)

            if sounding:

                is_foreign = staff_kind == "F" and bar_kind == "G" and not single_part

                reason = "foreign_clef_measure" if is_foreign else "single_staff_section"

                if sec.kind == "omit":

                    reason = "solo_omitted"

                elif sec.solo and sec.kind == "roles":

                    reason = "solo_routed"  # Batch 12: every bar, named voice only

                if sec.label != prev_label or reason in ("solo_omitted", "solo_routed") or is_foreign:

                    routed = "/".join(r for r in lines) or "no voice (rests)"

                    ctx.flag(None, reason, f"Single-staff system: {sec.label}; bar -> {routed}.")

            prev_label = sec.label

        else:

            prev_label = None

            upper, lower = _Line(), _Line()

            if mono:

                _fill_mono(m, upper, prefer_low=(pair.upper == "Bass"), ctx=ctx)

            else:

                _fill_pair(m, upper, lower, ctx)

            lines = {}

            if pair.upper is not None:

                lines[pair.upper] = upper

            if pair.lower is not None:

                lines[pair.lower] = lower



        # Tenor line printed in treble-8vb whose later systems Audiveris read

        # as plain treble (8vb lost): bring those bars back down an octave.

        if (

            set(lines) == {"Tenor"}

            and first_kind == "G8"

            and bar_kind == "G"

            and sounding

        ):

            lines["Tenor"] = _shifted(lines["Tenor"], -12)

            ctx.flag(None, "octave_down_assumed",

                     "CHECK BY EAR/SCORE: Tenor line started in treble-8vb; OMR read this bar as plain "

                     "treble, so it was moved down an octave (assumption, not printed).")



        if sec is not None or mono:

            u_line = lines.get(pair.upper) if pair.upper else None

            l_line = lines.get(pair.lower) if pair.lower else None

            u_last = [el for _, _, el in (u_line.items if u_line else []) if _is_sounding(el)]

            l_last = [el for _, _, el in (l_line.items if l_line else []) if _is_sounding(el)]

            if u_last or l_last:

                vl_state.update(

                    _el_pitches(u_last[-1]) if u_last else [],

                    _el_pitches(l_last[-1]) if l_last else [],

                )

        if ctx.divisi:

            ctx.flag(None, "divisi_chord",

                     "Chord with more pitches than free voices: kept as divisi chord "

                     "(no pitch dropped): " + ", ".join(dict.fromkeys(ctx.divisi)))

        if ctx.divisi_ambig and cfg.divisi_mode != DivisiMode.KEEP_DIVISI:

            ctx.flag(

                None,

                "divisi_resolved",

                "Ambiguous 3+ note chord resolved to 1 note per voice "

                "(adjust in GUI 'Resolve 3-note chords' if needed): "

                + ", ".join(dict.fromkeys(ctx.divisi_ambig)),

            )

        src_ext = _note_extent(m)

        for role in own_roles:

            line = lines.get(role, _Line())

            rm = _render_measure(m, line, target, m.number, extra, ctx, role)

            rm.satbSourceExtent = src_ext

            out[role][i] = rm

        for role, line in lines.items():

            if role in own_roles:

                continue

            rm = _render_measure(m, line, target, m.number, extra, ctx, role)

            for c in list(rm.getElementsByClass(clef.Clef)):

                rm.remove(c)

            rm.satbSourceExtent = src_ext

            out[role][i] = rm

    return out





def _flag_underfull(m: stream.Measure, ref: float, ctx: _Ctx) -> None:

    """Batch 11: flag a staff bar whose voice is shorter than the bar.



    Audiveris often drops an eighth rest at the start/middle of a bar (100.pdf

    p4 m4/m8/m12/m14/m20). The renderer pads the gap with a rest at the END

    (no notes are invented), so the following notes may sit early. Only

    voices that start on beat 1 and run without gaps are checked (a 3rd-voice

    fragment entering mid-bar is normal Audiveris output, not a lost rest).

    """

    if ref <= EPS:

        return

    groups: list[tuple[str, list[tuple[float, note.GeneralNote]]]] = []

    for v in m.voices:

        base = float(v.offset)

        groups.append((str(v.id), [(base + float(el.offset), el) for el in v.notesAndRests]))

    loose = [(float(el.offset), el) for el in m.notesAndRests]

    if loose:

        groups.append(("-", loose))

    short: list[tuple[str, float]] = []

    for vid, items in groups:

        if not items or not any(_is_sounding(el) for _, el in items):

            continue

        if any(el.isRest and getattr(el, "fullMeasure", False) in (True, "always") for _, el in items):

            continue

        items = sorted(items, key=lambda t: t[0])

        if items[0][0] > EPS:

            continue

        end = 0.0

        contiguous = True

        for off, el in items:

            if el.duration.isGrace:

                continue

            if off > end + EPS:

                contiguous = False

                break

            end = max(end, off + float(el.quarterLength))

        if contiguous and end < ref - EPS:

            short.append((vid, end))

    if short:

        held = max(e for _, e in short)

        gap = ref - min(e for _, e in short)

        ctx.flag(None, "underfull_measure",

                 f"Staff {ctx.part_index} voice(s) {', '.join(v for v, _ in short)}: OMR bar holds "

                 f"{', '.join(f'{e:g}' for _, e in short)} of {ref:g} quarters - a rest is probably "

                 f"missing. Padded with a rest at the END of the bar (no notes invented); if the lost "

                 f"rest belongs at the start/middle, the following notes sit up to {gap:g} quarter(s) "

                 "early. Check this bar against the PDF.")





def _underfull_ref(printed: float | None, target: float) -> float:

    """Bar length to check against: the grid bar, or the staff's printed meter

    when the grid bar was widened by another staff (9/8 content bar in 4/4)."""

    if printed is not None and EPS < printed < target - EPS:

        return printed

    return target





def _overlay_measure(

    primary: stream.Measure,

    secondary: stream.Measure | None,

    target: float,

    number,

    extra: list,

    ctx: _Ctx,

    role: VoiceRole = "Soprano",

) -> stream.Measure:

    """Primary voice bar = top-staff melody + choir notes only where the melody rests."""

    line = _Line()

    prim: list[tuple[float, float]] = []

    for el in primary.recurse().notes:

        if not _is_sounding(el):

            continue

        s = float(el.getOffsetInHierarchy(primary))

        e = s + float(el.quarterLength)

        prim.append((s, e))

        line.items.append((s, e, deepcopy(el)))

    if secondary is not None:

        for el in secondary.recurse().notes:

            if not _is_sounding(el):

                continue

            s = float(el.getOffsetInHierarchy(secondary))

            e = s + float(el.quarterLength)

            if all(e <= ps + EPS or s >= pe - EPS for ps, pe in prim):

                line.items.append((s, e, deepcopy(el)))

    rm = _render_measure(primary, line, target, number, extra, ctx, role)

    for c in list(rm.getElementsByClass(clef.Clef)):

        rm.remove(c)

    return rm





def _route_top_staff_soprano(

    parts: list[stream.Part],

    active_pairs: list[StaffPair],

    ignored: set[int],

    grid: _Grid,

    cfg: SplitConfig,

    cands: dict,

    flags: list[ReviewFlag],

) -> None:

    """Batch 11 item 9: labelled 'Sopran' / melody-over-humming top staff -> Soprano.



    Only the staff DIRECTLY above the choir SA staff, only if the layout

    ignores it. Routed bars win over the SA staff's Soprano line; SA notes

    are kept where the melody rests. Each routed bar is flagged.

    """

    from satb_splitter.split.topstaff import plan_top_staff



    sa = next((p for p in active_pairs if p.upper == "Soprano" and p.lower == "Alto"), None)

    if sa is None or sa.part_index == 0:

        return

    top = sa.part_index - 1

    if top not in ignored or top >= len(parts):

        return

    if _layout_staff_kind(parts[top]) not in ("G", None):

        return

    plan = plan_top_staff(parts, sa.part_index, top, grid.lengths,

                          lambda t: parse_section_marker(t, cfg.solo_policy))

    if not plan.bars:

        return

    scratch: list[ReviewFlag] = []

    produced = _split_part(parts[top], grid, StaffPair(top, "Soprano", None), cfg, scratch,

                           parts=parts, single_part=False)

    top_ms = produced.get("Soprano", [])

    for i, why in sorted(plan.bars.items()):

        if i >= grid.count or i >= len(top_ms) or top_ms[i] is None:

            continue

        # Batch 12: the routed melody bar gets the same underfull check as a

        # choir bar (100 p3 m20 = merged 64 holds 3.5 of 4).

        flags.extend(f for f in scratch if f.reason == "underfull_measure" and f.measure == i + 1)

        owner = next((t for t in cands["Soprano"][i] if t[0] == 0), None)

        ctx = _Ctx(measure=i + 1, part_index=top, cfg=cfg, flags=flags)

        merged = _overlay_measure(top_ms[i], owner[2] if owner else None, grid.lengths[i],

                                  top_ms[i].number, list(grid.context[i]), ctx)

        merged.satbSourceExtent = min(_source_extent(top_ms[i]), grid.lengths[i])

        # Batch 13: this Soprano bar is built from the top staff AND the SA

        # staff (fill where the melody rests): fermatas of both staves are

        # its own (100 bar 88: SA-staff fermata on the shared rest).

        merged.satbFermataStaves = (top, sa.part_index)

        cands["Soprano"][i].insert(0, (-1, top, merged))

        flags.append(ReviewFlag(

            measure=i + 1, part_index=top, offset=None, reason="top_staff_soprano",

            detail=(f"Top staff {top} above the choir carries the Soprano melody ({why}); routed to "

                    "Soprano. Choir SA notes fill the Soprano only where that melody rests; Alto "

                    "unchanged. Check against the PDF."),

        ))





def _shift_measure_octave(meas: stream.Measure, semitones: int) -> None:

    for el in meas.recurse().notes:

        if isinstance(el, chord.Chord):

            el.transpose(semitones, inPlace=True)

        elif isinstance(el, note.Note):

            el.pitch = el.pitch.transpose(semitones)





def _route_top_staff_tenor(

    parts: list[stream.Part],

    active_pairs: list[StaffPair],

    ignored: set[int],

    grid: _Grid,

    cfg: SplitConfig,

    cands: dict,

    flags: list[ReviewFlag],

    *,

    divisi_choices: list[DivisiChoice] | None = None,

    inherited_role: VoiceRole | None = None,

) -> None:

    """Batch 18 (B18_3): route a ``solo Tenor`` top staff into Tenor.



    On intro/solo-over-accompaniment bars (``intro_wait`` / ``solo_only``),

    non-solo voices receive clean waiting-time whole rests and stale flags or

    divisi choices emitted by the masked accompaniment staves are suppressed.

    On Tutti bars (``overlay``), Tenor overlays the solo line with choir Tenor.

    """

    from satb_splitter.split.topstaff import plan_top_solo_tenor



    sa = next((p for p in active_pairs if p.upper == "Soprano" and p.lower == "Alto"), None)

    if sa is None or sa.part_index == 0:

        return

    top = sa.part_index - 1

    if top not in ignored or top >= len(parts):

        return

    staff_k = _layout_staff_kind(parts[top])

    if staff_k not in ("G", "G8", None):

        return

    plan = plan_top_solo_tenor(

        parts,

        sa.part_index,

        top,

        grid.lengths,

        lambda t: parse_section_marker(t, cfg.solo_policy),

        inherited_role=inherited_role,

        solo_policy=cfg.solo_policy,

    )

    if not plan.active or not plan.bars:

        return



    first_kind = first_sounding_kind(parts[top])

    midi_vals: list[float] = []

    for pm in parts[top].getElementsByClass(stream.Measure):

        for el in pm.recurse().notes:

            if _is_sounding(el):

                midi_vals.extend(float(p.ps) for p in _pitches_desc(el))

    should_shift_8vb = False

    if staff_k in ("G", None) and first_kind != "G8" and midi_vals:

        sorted_m = sorted(midi_vals)

        med_m = sorted_m[len(sorted_m) // 2]

        if med_m >= 60.0 or max(sorted_m) >= 64.0:

            should_shift_8vb = True



    scratch: list[ReviewFlag] = []

    produced = _split_part(

        parts[top], grid, StaffPair(top, "Tenor", None), cfg, scratch,

        parts=parts, single_part=False,

    )

    top_ms = produced.get("Tenor", [])

    tb = next((p for p in active_pairs if p.upper == "Tenor" and p.lower == "Bass"), None)

    masked_bars: set[int] = set()



    for i, (kind, why) in sorted(plan.bars.items()):

        if i >= grid.count:

            continue

        if kind == "intro_wait":

            for r in ROLES:

                cands[r][i] = [(-1, top, _rest_measure(grid, i))]

            masked_bars.add(i + 1)

            flags.append(ReviewFlag(

                measure=i + 1, part_index=top, offset=None, reason="top_staff_tenor",

                detail=f"Top staff {top} ({why}); S/A/T/B set to waiting-time rests.",

            ))

            continue



        if i >= len(top_ms) or top_ms[i] is None:

            continue

        flags.extend(f for f in scratch if f.reason == "underfull_measure" and f.measure == i + 1)

        t_m = deepcopy(top_ms[i])

        if should_shift_8vb:

            _shift_measure_octave(t_m, -12)

        if cfg.tenor_clef.value != "as_source":

            for c in list(t_m.getElementsByClass(clef.Clef)):

                t_m.remove(c)



        if kind == "solo_only":

            for r in ("Soprano", "Alto", "Bass"):

                cands[r][i] = [(-1, top, _rest_measure(grid, i))]

            t_m.satbSourceExtent = min(_source_extent(t_m), grid.lengths[i])

            t_m.satbFermataStaves = (top,)

            cands["Tenor"][i] = [(-1, top, t_m)]

            masked_bars.add(i + 1)

            flags.append(ReviewFlag(

                measure=i + 1, part_index=top, offset=None, reason="top_staff_tenor",

                detail=(f"Top staff {top} above the choir carries solo Tenor ({why}); "

                        "routed to Tenor (8vb), S/A/B set to waiting-time rests."),

            ))

        else:

            owner = next((t for t in cands["Tenor"][i] if t[0] == 0), None)

            ctx = _Ctx(measure=i + 1, part_index=top, cfg=cfg, flags=flags,

                       upper_role="Tenor", lower_role="Tenor")

            merged = _overlay_measure(

                t_m, owner[2] if owner else None, grid.lengths[i],

                t_m.number, list(grid.context[i]), ctx, role="Tenor",

            )

            merged.satbSourceExtent = min(_source_extent(t_m), grid.lengths[i])

            merged.satbFermataStaves = (top, tb.part_index) if tb is not None else (top,)

            cands["Tenor"][i].insert(0, (-1, top, merged))

            flags.append(ReviewFlag(

                measure=i + 1, part_index=top, offset=None, reason="top_staff_tenor",

                detail=(f"Top staff {top} above the choir carries solo Tenor ({why}); "

                        "routed to Tenor and overlaid with choir Tenor where solo rests."),

            ))



    masked_parts = {p.part_index for p in (sa, tb) if p is not None}

    if masked_bars and masked_parts:

        stale_reasons = {

            "overflow_dropped",

            "clipped_at_barline",

            "divisi_chord",

            "divisi_resolved",

            "underfull_measure",

        }

        flags[:] = [

            fl for fl in flags

            if not (

                fl.measure in masked_bars

                and fl.part_index in masked_parts

                and fl.reason in stale_reasons

            )

        ]

        if divisi_choices is not None:

            divisi_choices[:] = [

                ch for ch in divisi_choices

                if not (ch.measure in masked_bars and ch.part_index in masked_parts)

            ]





def sanitize_ties(

    part: stream.Part,

    where: list | None = None,

    *,

    repair_continuity: bool = False,

) -> int:

    """Remove ties that do not join two consecutive notes of the SAME pitch.



    Splitting a tied chord / arpeggio (or OMR noise) can leave a tie from

    e.g. G3 to B3, which MusicXML/MuseScore treat as invalid (BUG-3).

    When ``repair_continuity`` is True (B15_F6), dangling same-pitch pairs

    (``start+None``, ``start+bad_start``, ``None+stop``) are repaired to

    ``start+stop`` (or ``continue``) ONLY when ``nxt`` carries no lyrics.

    Returns the number of tie ends changed. ``where`` (optional) receives

    ``(bar_index, pitch_name)`` for every change (Batch 11 per-bar REVIEW).

    """

    from music21 import tie as m21tie



    seq = [el for el in part.flatten().notesAndRests]

    bar_of: dict[int, int] = {}

    for bi, mm in enumerate(part.getElementsByClass(stream.Measure)):

        for el in mm.recurse().notesAndRests:

            bar_of[id(el)] = bi

    fixed = 0



    def _note_where(el) -> None:

        if where is not None and id(el) in bar_of:

            try:

                what = "+".join(p.nameWithOctave for p in el.pitches)

            except Exception:  # noqa: BLE001

                what = "note"

            where.append((bar_of[id(el)], what))



    def _ps(x) -> set:

        return {round(p.ps, 2) for p in x.pitches} if isinstance(x, (note.Note, chord.Chord)) else set()



    def _el_tie_for_ps(x, ps_val: float) -> str | None:

        if isinstance(x, note.Note):

            return x.tie.type if (abs(x.pitch.ps - ps_val) < 0.01 and x.tie is not None) else None

        if isinstance(x, chord.Chord):

            for q in x.pitches:

                if abs(q.ps - ps_val) < 0.01:

                    qt = x.getTie(q) or x.tie

                    return qt.type if qt is not None else None

        return None



    if repair_continuity:

        for i, el in enumerate(seq):

            if not isinstance(el, note.Note):

                continue

            nxt = seq[i + 1] if i + 1 < len(seq) else None

            if not isinstance(nxt, note.Note) or abs(nxt.pitch.ps - el.pitch.ps) >= 0.01:

                continue

            if getattr(nxt, "lyrics", None):

                continue

            et = el.tie.type if el.tie is not None else None

            nt = nxt.tie.type if nxt.tie is not None else None

            if et in ("start", "continue") and nt in (None, "start"):

                nxt2 = seq[i + 2] if i + 2 < len(seq) else None

                continues = (

                    nt == "start"

                    and isinstance(nxt2, note.Note)

                    and abs(nxt2.pitch.ps - nxt.pitch.ps) < 0.01

                    and (

                        (nxt2.tie is not None and nxt2.tie.type in ("stop", "continue"))

                        or not getattr(nxt2, "lyrics", None)

                    )

                )

                nxt.tie = m21tie.Tie("continue" if continues else "stop")

            elif et in (None, "stop") and nt in ("stop", "continue"):

                prev = seq[i - 1] if i > 0 else None

                has_prev = (

                    et == "stop"

                    and isinstance(prev, note.Note)

                    and abs(prev.pitch.ps - el.pitch.ps) < 0.01

                    and prev.tie is not None

                    and prev.tie.type in ("start", "continue")

                )

                el.tie = m21tie.Tie("continue" if has_prev else "start")



    for i, el in enumerate(seq):

        if isinstance(el, chord.Chord):

            ties = [el.tie] + [nn.tie for nn in el.notes]

            if not any(t is not None for t in ties):

                continue

            prev = seq[i - 1] if i > 0 else None

            nxt = seq[i + 1] if i + 1 < len(seq) else None

            # Check per-pitch ties when individual chord notes carry distinct ties (B15_F7)

            per_note_ties = [nn.tie for nn in el.notes]

            distinct = any(t is not None for t in per_note_ties) and any(t is None for t in per_note_ties)

            if distinct:

                changed_chord = False

                for nn in el.notes:

                    if nn.tie is None:

                        continue

                    nt_type = nn.tie.type

                    pt_nxt = _el_tie_for_ps(nxt, nn.pitch.ps)

                    pt_prev = _el_tie_for_ps(prev, nn.pitch.ps)

                    jn = (pt_nxt in ("stop", "continue")) if nt_type in ("start", "continue") else False

                    jp = (pt_prev in ("start", "continue")) if nt_type in ("stop", "continue") else False

                    want_n = {(True, True): "continue", (True, False): "start",

                              (False, True): "stop", (False, False): None}[(jn, jp)]

                    if want_n != nt_type:

                        nn.tie = m21tie.Tie(want_n) if want_n else None

                        changed_chord = True

                if changed_chord:

                    active_t = next((nn.tie.type for nn in el.notes if nn.tie is not None), None)

                    el.tie = m21tie.Tie(active_t) if active_t else None

                    fixed += 1

                    _note_where(el)

                continue

            t = next((x.type for x in ties if x is not None), None)

            j_next = bool(_ps(el) & _ps(nxt)) and t in ("start", "continue")

            j_prev = bool(_ps(el) & _ps(prev)) and t in ("stop", "continue")

            want = {(True, True): "continue", (True, False): "start",

                    (False, True): "stop", (False, False): None}[(j_next, j_prev)]

            if want != t:

                el.tie = m21tie.Tie(want) if want else None

                for nn in el.notes:

                    nn.tie = m21tie.Tie(want) if want else None

                fixed += 1

                _note_where(el)

            continue

        if not isinstance(el, note.Note) or el.tie is None:

            continue

        prev = seq[i - 1] if i > 0 else None

        nxt = seq[i + 1] if i + 1 < len(seq) else None

        nxt_t = _el_tie_for_ps(nxt, el.pitch.ps)

        prev_t = _el_tie_for_ps(prev, el.pitch.ps)

        joins_next = nxt_t in ("stop", "continue")

        joins_prev = prev_t in ("start", "continue")

        t = el.tie.type

        want = {

            (True, True): "continue",

            (True, False): "start",

            (False, True): "stop",

            (False, False): None,

        }[(joins_next if t in ("start", "continue") else False,

           joins_prev if t in ("stop", "continue") else False)]

        if want != t:

            el.tie = m21tie.Tie(want) if want else None

            fixed += 1

            _note_where(el)

    return fixed





def _plan_staff_kind(

    src: stream.Part, kinds: list[str | None], pair: StaffPair, first_kind: str | None

) -> str | None:

    """Clef kind that decides single-staff routing for this staff.



    A choir T/B staff is treated as a bass staff ("F") even when its

    treble-clef single-staff systems (e.g. Hard p6 "femeile") outnumber its

    bass-clef bars, so those treble runs are routed (S/A by default) instead

    of being sung by T/B at treble pitch. Counting all bars (empty multi-rest

    bars included) made p6's T/B staff look "treble" in Batch 3.

    """

    dom = _dominant(kinds)

    if dom == "F" or _layout_staff_kind(src) == "F":

        return "F"

    lower_pair = pair.upper in ("Tenor", "Bass") or pair.lower in ("Tenor", "Bass")

    if lower_pair and "F" in kinds and first_kind != "G8" and "G8" not in kinds:

        return "F"

    return dom





def _dominant(kinds: list[str | None]) -> str | None:

    counts: dict[str, int] = {}

    for k in kinds:

        if k:

            counts[k] = counts.get(k, 0) + 1

    if not counts:

        return None

    return max(counts.items(), key=lambda kv: kv[1])[0]





# ---------------------------------------------------------------------------

# Note helpers

# ---------------------------------------------------------------------------



def _note_from_pitch(pitch, source_el: note.GeneralNote) -> note.Note:

    n = note.Note()

    n.pitch = deepcopy(pitch)

    if source_el.duration is not None:

        n.duration = deepcopy(source_el.duration)

    else:

        n.quarterLength = source_el.quarterLength

    if isinstance(source_el, (note.Note, chord.Chord)):

        n.articulations = deepcopy(source_el.articulations)

        n.expressions = deepcopy(source_el.expressions)

        n.lyrics = deepcopy(source_el.lyrics)

    tie = getattr(source_el, "tie", None)

    if isinstance(source_el, chord.Chord):

        try:

            tie = source_el.getTie(pitch) or tie

        except Exception:

            pass

    n.tie = deepcopy(tie) if tie else None

    return n





__all__ = ["split_file", "split_score", "clef_kind", "sanitize_ties", "parse_section_marker"]