"""Merge multiple single-voice parts (e.g. per-page splits) into one score."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

from music21 import meter, note, stream

from satb_splitter.model.config import (
    DivisiChoice,
    DivisiMode,
    ReviewFlag,
    SplitConfig,
    SplitResult,
    VoiceRole,
    resolve_graft_config,
)
from satb_splitter.split.engine import sanitize_ties, split_file
from satb_splitter.split.directions import (
    copy_measure_directions,
    sync_voltas_from_ref,
    transfer_repeat_brackets,
)
from satb_splitter.export.writer import write_satb_files, VOICE_ORDER
from satb_splitter.split.joinbars import (
    HALF_BAR_REASONS,
    JOIN_REASON,
    KEY_DROPPED_REASON,
    key_dropped_detail,
    restate_key,
    UNJOINED_REASON,
    can_trim,
    first_content,
    join_output_bars,
    ratio_len,
)

EPS = 1e-6


def _page_id(path: Path) -> tuple[int | None, int]:
    """(page number, movement number) from 'page_07.mvt2.mxl' (None if no page name)."""
    m = re.match(r"page_(\d+)(?:\.mvt(\d+))?", path.stem, re.I)
    if not m:
        return None, 0
    return int(m.group(1)), int(m.group(2) or 0)


def _cross_page_candidate(prev_edge: dict, edge: dict):
    """Batch 13: last bar of the previous page + first bar of this page.

    Returns (la, lb, ratio) to join, ("unjoined", detail) to flag, or None."""
    last = (prev_edge or {}).get("last")
    first = (edge or {}).get("first")
    if not last or not first:
        return None
    if last.get("missing") or first.get("missing") or not last.get("plain") or not first.get("plain"):
        return None
    ratio = last.get("ratio")
    if not ratio:
        return None
    bl = ratio_len(ratio)
    own = first.get("own_ratios") or set()
    if own and own != {ratio}:
        return None
    la = last.get("content")
    lb = first_content(edge, bl)
    if la is None or lb is None or la >= bl - EPS or lb >= bl - EPS:
        return None
    if abs(la + lb - bl) < EPS:
        return la, lb, ratio
    if la + lb < bl - EPS:
        return ("unjoined", f"Possible bar split by a page break: MXL m{last.get('number')} ({la:g} q) + next page "
                f"m{first.get('number')} ({lb:g} q) add up to {la + lb:g} of {ratio}; NOT joined (lengths do not "
                "fill the meter). Check against the PDF.")
    return None


def _in_bracket(part: stream.Part, m: stream.Measure) -> bool:
    from music21 import spanner
    return any(sp.hasSpannedElement(m) for sp in part.recurse().getElementsByClass(spanner.RepeatBracket))


def _is_pickup(m: stream.Measure) -> bool:
    return float(getattr(m, "paddingLeft", 0.0) or 0.0) > 1e-6


def merge_parts(parts: list[stream.Part]) -> stream.Part:
    """Concatenate measures from several parts into one Part.

    * A pickup (anacrusis) as the very first bar is numbered 0 and written
      implicit, so MuseScore numbers the next bar 1. The first bar of a later
      page is never an implicit bar.
    * A time signature equal to the meter already in effect (repeated at the
      start of every page) is dropped, so MuseScore shows no fake meter change.
    """
    out = stream.Part()
    measure_num = 1
    current_ts: str | None = None
    first = True
    for part in parts:
        ms = list(part.getElementsByClass(stream.Measure))
        if ms:
            old_to_new: dict[int, stream.Measure] = {}
            for k, m in enumerate(ms):
                nm = deepcopy(m)
                if first and _is_pickup(nm):
                    nm.number = 0
                    nm.showNumber = stream.enums.ShowNumber.NEVER
                else:
                    nm.number = measure_num
                    measure_num += 1
                    if nm.showNumber == stream.enums.ShowNumber.NEVER:
                        nm.showNumber = stream.enums.ShowNumber.DEFAULT
                first = False
                for ts in list(nm.getElementsByClass(meter.TimeSignature)):
                    if ts.ratioString == current_ts:
                        nm.remove(ts)
                    else:
                        current_ts = ts.ratioString
                old_to_new[id(m)] = nm
                out.append(nm)
            transfer_repeat_brackets(part, out, old_to_new)
        else:
            for el in part.flatten().notesAndRests:
                out.append(deepcopy(el))
    return out


def _bar_lengths(part: stream.Part) -> list[float]:
    out = []
    for m in part.getElementsByClass(stream.Measure):
        out.append(float(m.highestTime) if m.highestTime else float(m.barDuration.quarterLength))
    return out


def _pad_page_parts(page_parts: dict[str, stream.Part], label: str, log: list[str]) -> None:
    """Make every role of one page have the same number of bars (rest padding)."""
    counts = {r: len(list(p.getElementsByClass(stream.Measure))) for r, p in page_parts.items()}
    if not counts:
        return
    target = max(counts.values())
    if all(c == target for c in counts.values()):
        return
    ref_role = max(counts, key=lambda r: counts[r])
    ref_ms = list(page_parts[ref_role].getElementsByClass(stream.Measure))
    for role, part in page_parts.items():
        have = counts[role]
        if have >= target:
            continue
        for i in range(have, target):
            ref = ref_ms[i]
            m = stream.Measure(number=ref.number)
            for ts in ref.getElementsByClass(meter.TimeSignature):
                m.insert(0, deepcopy(ts))
            length = float(ref.highestTime) or float(ref.barDuration.quarterLength)
            m.insert(0, note.Rest(quarterLength=length))
            copy_measure_directions(ref, m)
            part.append(m)
        log.append(
            f"  WARNING: {label}: {role} had {have} bar(s), padded to {target} with rests"
        )
    ref_part = page_parts[ref_role]
    for role, part in page_parts.items():
        sync_voltas_from_ref(part, ref_part)


def _mxl_meter_profile(path: Path) -> tuple[bool, list[str], list[float]]:
    """Return ``(has_time_sig, distinct_ratios, max_content_per_bar)`` from MXL/XML."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                root_name = None
                if "META-INF/container.xml" in zf.namelist():
                    c = ET.fromstring(zf.read("META-INF/container.xml"))
                    for rf in c.iter():
                        if rf.tag.endswith("rootfile") and rf.get("full-path"):
                            root_name = rf.get("full-path")
                            break
                if root_name is None:
                    xmls = [n for n in zf.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
                    if not xmls:
                        return False, [], []
                    root_name = next((n for n in xmls if "score" in n.lower()), xmls[0])
                raw = zf.read(root_name).decode("utf-8", errors="replace")
        elif path.is_file() and path.suffix.lower() in (".xml", ".musicxml"):
            raw = path.read_text(encoding="utf-8", errors="replace")
        else:
            return False, [], []
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", raw, count=1))
    except Exception:  # noqa: BLE001
        return False, [], []
    if root.tag != "score-partwise":
        return False, [], []

    ratios: list[str] = []
    bar_max: list[float] = []
    for part in root.findall("part"):
        div = 1.0
        for mi, m in enumerate(part.findall("measure")):
            for attrs in m.findall("attributes"):
                d_txt = attrs.findtext("divisions")
                if d_txt:
                    try:
                        d_val = float(d_txt)
                        if d_val > 0:
                            div = d_val
                    except ValueError:
                        pass
                for t in attrs.findall("time"):
                    b = t.findtext("beats")
                    bt = t.findtext("beat-type")
                    if b and bt:
                        r = f"{b.strip()}/{bt.strip()}"
                        try:
                            ratio_len(r)
                            if r not in ratios:
                                ratios.append(r)
                        except Exception:  # noqa: BLE001
                            pass
            voice_lens: dict[str, float] = {}
            for el in m:
                if el.tag == "note":
                    if el.find("chord") is not None or el.find("grace") is not None:
                        continue
                    rst = el.find("rest")
                    if rst is not None and rst.get("measure") == "yes":
                        continue
                    dur_txt = el.findtext("duration")
                    if dur_txt is None:
                        continue
                    try:
                        ql = float(dur_txt) / div
                    except ValueError:
                        continue
                    v = (el.findtext("voice") or "1").strip()
                    voice_lens[v] = voice_lens.get(v, 0.0) + ql
                elif el.tag == "forward":
                    dur_txt = el.findtext("duration")
                    if dur_txt is None:
                        continue
                    try:
                        ql = float(dur_txt) / div
                    except ValueError:
                        continue
                    v = (el.findtext("voice") or "1").strip()
                    voice_lens[v] = voice_lens.get(v, 0.0) + ql
            m_len = max(voice_lens.values(), default=0.0)
            if mi >= len(bar_max):
                bar_max.extend([0.0] * (mi + 1 - len(bar_max)))
            if m_len > bar_max[mi]:
                bar_max[mi] = m_len
    return bool(ratios), ratios, bar_max


def _pick_dominant_ts(counts: Counter[str], opening_ts: str | None) -> str | None:
    if not counts:
        return opening_ts
    top_count = max(counts.values())
    tied = [r for r, c in counts.items() if c == top_count]
    if opening_ts and opening_ts in tied:
        return opening_ts
    return tied[0]


def _record_page_meter_evidence(
    counts: Counter[str],
    seen_ts: list[str],
    page_ratios: list[str],
    page_lens: list[float],
    active_ts: str | None,
) -> None:
    for r in page_ratios:
        if r not in seen_ts:
            seen_ts.append(r)
    if active_ts and active_ts not in seen_ts:
        seen_ts.append(active_ts)
    if not seen_ts:
        return
    preferred = page_ratios[-1] if page_ratios else active_ts
    for x in page_lens:
        if x <= EPS:
            continue
        matches = []
        for r in seen_ts:
            try:
                if abs(x - ratio_len(r)) < EPS:
                    matches.append(r)
            except Exception:  # noqa: BLE001
                pass
        if len(matches) == 1:
            counts[matches[0]] += 1
        elif len(matches) > 1:
            if preferred in matches:
                counts[preferred] += 1
            else:
                counts[matches[0]] += 1


def split_and_merge_mxl_files(
    mxl_paths: list[Path | str],
    out_dir: Path | str,
    basename: str,
    *,
    auto_layout: bool = True,
    config: SplitConfig | None = None,
    omr_failures: list[str] | None = None,
) -> tuple[dict[VoiceRole, Path], list[str], SplitResult | None]:
    """Split each MusicXML (auto layout) and merge voices into final SATB files.

    All four merged voices are guaranteed to have the same bar count: each
    page is padded with rest bars where a role is short, and pages that fail
    to split (or failed OMR, passed via ``omr_failures``) are reported as
    WARNING lines + review flags instead of being dropped silently.

    Returns (written_paths, log_lines, combined_result).
    """
    base_cfg = config or SplitConfig()
    raw_overrides = dict(getattr(base_cfg, "divisi_overrides", {}) or {})
    cfg = SplitConfig(
        template=base_cfg.template,
        unison_mode=base_cfg.unison_mode,
        chord_mode=base_cfg.chord_mode,
        tenor_clef=base_cfg.tenor_clef,
        voice_upper_ids=base_cfg.voice_upper_ids,
        voice_lower_ids=base_cfg.voice_lower_ids,
        flag_middle_pitches=base_cfg.flag_middle_pitches,
        solo_policy=base_cfg.solo_policy,
        divisi_mode=getattr(base_cfg, "divisi_mode", DivisiMode.MONOPHONIC_AUTO),
        divisi_overrides={},
        graft=resolve_graft_config(getattr(base_cfg, "graft", None)),
    )
    voice_parts: dict[str, list[stream.Part]] = {r: [] for r in VOICE_ORDER}
    log: list[str] = []
    all_flags: list[ReviewFlag] = []
    all_divisi_choices: list[DivisiChoice] = []
    source_parts = 0
    bars_before = 0  # merged bars written by earlier pages
    last_ts: str | None = None  # meter at the end of the previous page
    seen_ts: list[str] = []
    opening_ts: str | None = None
    ts_content_counts: Counter[str] = Counter()
    pickup_shift = 0  # 1 when the merged file starts with a pickup numbered 0
    active_top_solo_role: VoiceRole | None = None
    # Batch 13: edge bars of the previous page for joins across page files
    prev_edge: dict | None = None
    prev_label = ""
    prev_page: int | None = None

    for failed in omr_failures or []:
        msg = (
            f"WARNING: OMR failed for {failed} - its bars are MISSING from all "
            "four voices (fix/re-run that page and re-merge)."
        )
        log.append(msg)
        all_flags.append(
            ReviewFlag(measure=None, part_index=-1, offset=None, reason="omr_page_failed", detail=msg)
        )

    for path in mxl_paths:
        path = Path(path)
        label = f"{path.parent.name}/{path.name}" if path.parent.name else path.name
        log.append(f"Splitting {label}...")
        page_no, mvt_no = _page_id(path)
        if mvt_no > 1:
            seen_ts.clear()
            ts_content_counts.clear()
            opening_ts = None
            active_top_solo_role = None
        has_ts, page_ratios, page_lens = _mxl_meter_profile(path)
        if page_ratios and opening_ts is None:
            opening_ts = page_ratios[0]
        effective_ts = last_ts
        if (
            bars_before > 0
            and last_ts is not None
            and mvt_no <= 1
            and not has_ts
            and page_lens
        ):
            dominant_ts = _pick_dominant_ts(ts_content_counts, opening_ts)
            if dominant_ts and dominant_ts != last_ts:
                try:
                    dom_q = ratio_len(dominant_ts)
                    last_q = ratio_len(last_ts)
                except Exception:  # noqa: BLE001
                    dom_q = last_q = None
                if dom_q is not None and last_q is not None:
                    dom_hits = sum(1 for x in page_lens if abs(x - dom_q) < EPS)
                    last_hits = sum(1 for x in page_lens if abs(x - last_q) < EPS)
                    if dom_hits / len(page_lens) >= 0.75 and dom_hits > last_hits:
                        effective_ts = dominant_ts
                        log.append(
                            f"  {label}: meterless page matches dominant meter {dominant_ts} "
                            f"({dom_hits}/{len(page_lens)} bars) instead of carried {last_ts}; "
                            f"inheriting {dominant_ts}"
                        )

        def _split_page(page_cfg: SplitConfig):
            if cfg.graft.b15_f6_tie_continuity:
                return split_file(
                    path,
                    page_cfg,
                    auto_layout=auto_layout,
                    inherit_ts=effective_ts,
                    skip_boundary_tie_trim=True,
                    inherited_top_solo_role=active_top_solo_role,
                )
            return split_file(
                path,
                page_cfg,
                auto_layout=auto_layout,
                inherit_ts=effective_ts,
                inherited_top_solo_role=active_top_solo_role,
            )

        try:
            result = _split_page(cfg)
        except Exception as exc:  # noqa: BLE001
            msg = (
                f"WARNING: could not split {label} ({exc}); its bars are MISSING "
                "from all four voices."
            )
            log.append(f"  {msg}")
            all_flags.append(
                ReviewFlag(measure=None, part_index=-1, offset=None, reason="page_split_failed", detail=msg)
            )
            prev_edge = None
            continue
        source_parts = max(source_parts, result.source_part_count)
        if bars_before == 0:
            sop = list(result.scores["Soprano"].parts)
            first_ms = list(sop[0].getElementsByClass(stream.Measure)) if sop else []
            pickup_shift = 1 if first_ms and float(getattr(first_ms[0], "paddingLeft", 0.0) or 0.0) > 1e-6 else 0

        def _prepare_page(res):
            parts_map: dict[str, stream.Part] = {}
            for role in VOICE_ORDER:
                ps = list(res.scores[role].parts)
                parts_map[role] = ps[0] if ps else stream.Part()
            _pad_page_parts(parts_map, label, log)
            e_info = getattr(res, "edge_info", None) or {}
            c_info = None
            if (bars_before > 0 and prev_edge and page_no is not None and prev_page is not None
                    and page_no == prev_page + 1 and mvt_no <= 1):
                c_info = _cross_page_candidate(prev_edge, e_info)
            return parts_map, e_info, c_info

        page_parts, edge, cand = _prepare_page(result)
        if raw_overrides and getattr(result, "divisi_choices", None):
            est_shift = 0
            if cand and cand[0] != "unjoined":
                la_, lb_, _ratio = cand
                est_ok = True
                for role in VOICE_ORDER:
                    prev_ms = list(voice_parts[role][-1].getElementsByClass(stream.Measure)) if voice_parts[role] else []
                    cur_ms = list(page_parts[role].getElementsByClass(stream.Measure))
                    if not (prev_ms and cur_ms and can_trim(prev_ms[-1], la_) and can_trim(cur_ms[0], lb_)
                            and not _in_bracket(page_parts[role], cur_ms[0])):
                        est_ok = False
                        break
                if est_ok:
                    est_shift = 1
            local_overrides: dict[str, str] = {}
            for ch in result.divisi_choices:
                merged_m = bars_before + ch.measure - pickup_shift - est_shift
                merged_key = f"{merged_m}|{ch.upper_role}|{round(float(ch.offset), 4)}"
                val = raw_overrides.get(merged_key)
                if val is None and ch.source_measure:
                    src_key = f"{label}|{ch.source_measure}|{ch.upper_role}|{round(float(ch.offset), 4)}"
                    val = raw_overrides.get(src_key)
                if val and val != "auto":
                    raw_key = f"{ch.raw_measure}|{ch.upper_role}|{round(float(ch.offset), 4)}"
                    local_overrides[raw_key] = val
            if local_overrides:
                page_cfg = SplitConfig(
                    template=cfg.template,
                    unison_mode=cfg.unison_mode,
                    chord_mode=cfg.chord_mode,
                    tenor_clef=cfg.tenor_clef,
                    voice_upper_ids=cfg.voice_upper_ids,
                    voice_lower_ids=cfg.voice_lower_ids,
                    flag_middle_pitches=cfg.flag_middle_pitches,
                    solo_policy=cfg.solo_policy,
                    divisi_mode=cfg.divisi_mode,
                    divisi_overrides=local_overrides,
                    graft=cfg.graft,
                )
                result = _split_page(page_cfg)
                page_parts, edge, cand = _prepare_page(result)

        # Batch 13: one printed bar split across the page break (last bar of
        # the previous page + first bar of this page, both short, adding up
        # to the meter) is written as ONE bar; nothing padded or invented.
        joined = None
        a_no = bars_before - pickup_shift  # merged number of the previous page's last bar
        if cand and cand[0] == "unjoined":
            all_flags.append(ReviewFlag(measure=a_no, part_index=-1, offset=None, reason=UNJOINED_REASON,
                                        detail=f"[{prev_label} + {label}] {cand[1]}",
                                        source_page=f"{prev_label} m{prev_edge['last'].get('number')} + {label}",
                                        source_measure=str(edge['first'].get('number'))))
        elif cand:
            la, lb, ratio = cand
            a_ms, b_ms = {}, {}
            for role in VOICE_ORDER:
                prev_ms = list(voice_parts[role][-1].getElementsByClass(stream.Measure)) if voice_parts[role] else []
                cur_ms = list(page_parts[role].getElementsByClass(stream.Measure))
                if prev_ms and cur_ms:
                    a_ms[role], b_ms[role] = prev_ms[-1], cur_ms[0]
            ok = (len(a_ms) == len(VOICE_ORDER)
                  and all(can_trim(a_ms[r], la) and can_trim(b_ms[r], lb) for r in a_ms)
                  and not any(_in_bracket(page_parts[r], b_ms[r]) for r in b_ms))
            if ok:
                dropped_key = None
                for role in VOICE_ORDER:
                    b_ts = join_output_bars(a_ms[role], b_ms[role], la, lb, ratio)
                    page_parts[role].remove(b_ms[role])
                    rest = list(page_parts[role].getElementsByClass(stream.Measure))
                    if b_ts and rest and not list(rest[0].getElementsByClass(meter.TimeSignature)):
                        rest[0].insert(0, meter.TimeSignature(b_ts))
                    restate_key(a_ms[role], rest[0] if rest else None)
                    dropped_key = dropped_key or getattr(a_ms[role].editorial, "satb_dropped_key", None)
                if dropped_key is not None:
                    has_next = bool(list(page_parts[VOICE_ORDER[0]].getElementsByClass(stream.Measure)))
                    all_flags.append(ReviewFlag(
                        measure=a_no, part_index=-1, offset=None, reason=KEY_DROPPED_REASON,
                        detail=f"[{prev_label} + {label}] " + key_dropped_detail(
                            dropped_key, la, f"page break, {label} m{edge['first'].get('number')}",
                            a_no + 1 if has_next else None),
                        source_page=f"{prev_label} m{prev_edge['last'].get('number')} + {label}",
                        source_measure=str(edge['first'].get('number'))))
                joined = (la, lb, ratio)
                log.append(f"  joined bar split across the page break: {prev_label} m{prev_edge['last'].get('number')} "
                           f"({la:g} q) + {label} m{edge['first'].get('number')} ({lb:g} q) -> one {ratio} bar "
                           f"(merged bar {a_no})")
            else:
                all_flags.append(ReviewFlag(
                    measure=a_no, part_index=-1, offset=None, reason=UNJOINED_REASON,
                    detail=(f"[{prev_label} + {label}] Bar split by a page break ({la:g} + {lb:g} q = {ratio}) NOT "
                            "joined: a note crosses the split point or the second half is inside an ending "
                            "bracket. Check against the PDF."),
                    source_page=f"{prev_label} m{prev_edge['last'].get('number')} + {label}",
                    source_measure=str(edge['first'].get('number'))))
        shift = 1 if joined else 0
        for fl in result.flags:
            if fl.reason == "layout_auto":
                log.append(f"  layout: {fl.detail}")
            else:
                # Flag measure = 1-based bar in the MERGED voice files (same
                # numbering MuseScore shows); the page-local bar is kept in
                # the detail prefix.
                if fl.measure is not None:
                    nums = getattr(result, "source_measure_numbers", None) or []
                    fl.source_page = label
                    if 1 <= fl.measure <= len(nums) and nums[fl.measure - 1] not in (None, ""):
                        fl.source_measure = str(nums[fl.measure - 1])
                        fl.detail = f"[{label} MXL m{fl.source_measure}] {fl.detail}"
                    else:
                        fl.detail = f"[{label} bar {fl.measure}] {fl.detail}"
                    # pickup numbered 0 -> MuseScore bar = position - 1
                    # (Batch 13: minus 1 when this page's bar 1 was joined
                    # onto the previous page's last bar)
                    fl.measure = bars_before + fl.measure - pickup_shift - shift
                else:
                    fl.source_page = label
                    fl.detail = f"[{label}] {fl.detail}"
                all_flags.append(fl)
                if fl.reason.startswith(("xml_", "omr_export")):
                    log.append(f"  WARNING: incomplete OMR: {fl.detail}")
        if any(fl.reason == "top_staff_tenor" for fl in result.flags):
            active_top_solo_role = "Tenor"
        elif result.source_part_count < 3 or any(fl.reason == "top_staff_soprano" for fl in result.flags):
            active_top_solo_role = None
        nums = getattr(result, "source_measure_numbers", None) or []
        for ch in getattr(result, "divisi_choices", None) or []:
            ch.source_page = label
            if not ch.source_measure and 1 <= ch.measure <= len(nums) and nums[ch.measure - 1] not in (None, ""):
                ch.source_measure = str(nums[ch.measure - 1])
            ch.measure = bars_before + ch.measure - pickup_shift - shift
            ch.choice_key = ch.normalized_key()
            all_divisi_choices.append(ch)
        if joined:
            la, lb, ratio = joined
            # the half-bar meter/underfull flags no longer apply to the joined bar
            all_flags[:] = [f for f in all_flags if not (f.measure == a_no and f.reason in HALF_BAR_REASONS)]
            all_flags.append(ReviewFlag(
                measure=a_no, part_index=-1, offset=None, reason=JOIN_REASON,
                detail=(f"[{prev_label} + {label}] One printed bar split by a page break: "
                        f"{prev_label} m{prev_edge['last'].get('number')} ({la:g} q) + {label} "
                        f"m{edge['first'].get('number')} ({lb:g} q) joined into one {ratio} bar "
                        "(no padding, no notes added). Check against the PDF."),
                source_page=f"{prev_label} m{prev_edge['last'].get('number')} + {label}",
                source_measure=str(edge['first'].get('number'))))
        bars_before += len(list(page_parts["Soprano"].getElementsByClass(stream.Measure)))
        if effective_ts is not None and effective_ts != last_ts:
            for role in VOICE_ORDER:
                role_ms = list(page_parts[role].getElementsByClass(stream.Measure))
                if role_ms and not list(role_ms[0].getElementsByClass(meter.TimeSignature)):
                    role_ms[0].insert(0, meter.TimeSignature(effective_ts))
        if joined and voice_parts["Soprano"]:
            for ts in voice_parts["Soprano"][-1].recurse().getElementsByClass(meter.TimeSignature):
                last_ts = ts.ratioString
        for ts in page_parts["Soprano"].recurse().getElementsByClass(meter.TimeSignature):
            last_ts = ts.ratioString
        # Batch 13: a short half bar at the end of the page does not set the
        # meter of the next page (it inherits the meter in effect, e.g. 4/4,
        # not the 2/4 of the half bar - Hard page_13 -> page_14).
        last_edge = edge.get("last") or {}
        if (last_edge.get("content") is not None and last_edge.get("ratio")
                and last_edge["content"] < ratio_len(last_edge["ratio"]) - EPS
                and last_ts is not None and last_ts != last_edge["ratio"]):
            log.append(f"  {label}: last bar is a short half bar ({last_edge['content']:g} q under "
                       f"{last_edge['ratio']}); next page inherits {last_edge['ratio']} (not {last_ts})")
            last_ts = last_edge["ratio"]
        _record_page_meter_evidence(ts_content_counts, seen_ts, page_ratios, page_lens, last_ts or effective_ts)
        prev_edge, prev_label, prev_page = edge, label, page_no
        for role in VOICE_ORDER:
            voice_parts[role].append(page_parts[role])
        log.append(
            "  notes: "
            + ", ".join(
                f"{r[0]}={len(list(page_parts[r].flatten().notes))}" for r in VOICE_ORDER
            )
            + "  bars: "
            + str(len(list(page_parts["Soprano"].getElementsByClass(stream.Measure))))
        )

    scores: dict[str, stream.Score] = {}
    bar_counts: dict[str, int] = {}
    for role in VOICE_ORDER:
        part = merge_parts(voice_parts[role]) if voice_parts[role] else stream.Part()
        part.partName = role
        part.id = role
        if cfg.graft.b15_f6_tie_continuity:
            tie_bars: list[tuple[int, str]] = []
            if sanitize_ties(part, tie_bars, repair_continuity=True):
                ms_list = list(part.getElementsByClass(stream.Measure))
                for bar_i, what in tie_bars:
                    m_no = (
                        ms_list[bar_i].number
                        if 0 <= bar_i < len(ms_list) and ms_list[bar_i].number is not None
                        else bar_i + 1
                    )
                    all_flags.append(
                        ReviewFlag(
                            measure=max(1, int(m_no)),
                            part_index=-1,
                            offset=None,
                            reason="tie_removed",
                            detail=f"{role}: tie on {what} removed (next/previous note is a different pitch or a rest).",
                        )
                    )
        bar_counts[role] = len(list(part.getElementsByClass(stream.Measure)))
        sc = stream.Score()
        sc.insert(0, part)
        scores[role] = sc

    summary = ", ".join(f"{r[0]}={bar_counts[r]}" for r in VOICE_ORDER)
    if len(set(bar_counts.values())) > 1:
        log.append(f"WARNING: merged bar counts differ: {summary}")
    else:
        log.append(f"Merged bars per voice: {summary}")

    combined = SplitResult(
        scores=scores,  # type: ignore[arg-type]
        flags=all_flags,
        source_part_count=source_parts,
        divisi_choices=all_divisi_choices,
    )
    written = write_satb_files(combined, out_dir, basename)
    log.append(f"Wrote {len(written)} voice file(s) to {Path(out_dir).resolve()}")
    if combined.mscz_message:
        log.append(("" if combined.mscz_path else "WARNING: ") + combined.mscz_message)
    return written, log, combined