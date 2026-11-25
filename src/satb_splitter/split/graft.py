"""Batch 14: deterministic OMR repair grafts (F1..F6) with strict safety gates.

All repairs run on the validated source Score in ``split_file`` right before
``split_score``. Every candidate measure repair is validated in memory against:

* **Pitch Gate**: F1..F4 preserve the exact per-staff pitch multiset; F5..F6
  preserve exact horizontal column pitch sets (0 invented or dropped pitches).
* **Lyric Gate**: 100% of source lyrics are preserved (0 dropped syllables).
* **Exact Meter Balance Gate**: every repaired voice in the measure sums to
  the exact declared meter (``expected_q``), with no rewritten time signatures
  and no unsupported (<1024th) durations.
* **Ornament/Expression Preservation**: existing ``expressions`` (including
  ``Fermata``), ``articulations``, ``lyrics``, and ``tie`` objects stay
  attached.

If ANY gate fails, the graft for that measure/staff is rejected and the source
measure is left untouched. Repaired bars are recorded in a single page-level
``omr_graft_summary`` review flag (``measure=None``) for auditability.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from music21 import chord, duration as m21dur, meter, note, pitch as m21pitch, stream

from satb_splitter.model.config import GraftConfig, ReviewFlag, resolve_graft_config

EPS = 1e-6
GRAFT_SIDECAR_SUFFIX = ".graft.json"
TYPE_D_ANCHOR_MIN = 0.70
TYPE_D_MAX_HEAD_DELTA = 4

F1_CAUSES = frozenset({"missed_augmentation_dot"})
F2_CAUSES = frozenset({
    "quarter_vs_eighth_half_mismatch",
    "wrong_beam_flag_reduction",
    "measure_rest_duration_mismatch",
    "duration_rounding",
})
F3_CAUSES = frozenset({"shared_mid_staff_rest"})
F4_CAUSES = frozenset({"unprinted_rest"})
F5_CAUSES = frozenset({"pure_voice_split", "pure_voice_split_and_reassign", "shared_rest_duplication"})
F6_CAUSES = frozenset({"voice_split_with_duration_graft"})

_PITCH_RE = re.compile(r"^([A-Ga-g])(bb|b|x|##|#|--|-|)(-?\d+)$")
_ALTER_MAP = {"": 0, "b": -1, "-": -1, "bb": -2, "--": -2, "#": 1, "x": 2, "##": 2}
_CANON_ALTER = {-2: "bb", -1: "b", 0: "", 1: "#", 2: "x"}


def _is_genuine_pickup(m: stream.Measure, mi: int, inherit_ts: str | None) -> bool:
    if mi != 0 or inherit_ts is not None:
        return False
    if m.number == 0 or getattr(m, "showNumber", None) == stream.enums.ShowNumber.NEVER:
        return True
    return False


def _canon_pitch_str(raw: str) -> str:
    m = _PITCH_RE.match(raw.strip())
    if m:
        step = m.group(1).upper()
        alt = _ALTER_MAP.get(m.group(2), 0)
        return f"{step}{_CANON_ALTER.get(alt, m.group(2))}{m.group(3)}"
    p = m21pitch.Pitch(raw)
    alt = int(round(p.accidental.alter)) if p.accidental else 0
    return f"{p.step}{_CANON_ALTER.get(alt, '')}{p.octave}"


def _m21_pitch_canon(p: m21pitch.Pitch) -> str:
    alt = int(round(p.accidental.alter)) if p.accidental else 0
    return f"{p.step}{_CANON_ALTER.get(alt, '')}{p.octave}"


def _to_m21_pitch(canon: str) -> m21pitch.Pitch:
    m = _PITCH_RE.match(canon.strip())
    if m:
        step = m.group(1).upper()
        acc = {"": "", "b": "-", "-": "-", "bb": "--", "--": "--", "#": "#", "x": "##", "##": "##"}.get(m.group(2), "")
        return m21pitch.Pitch(f"{step}{acc}{m.group(3)}")
    return m21pitch.Pitch(canon)


def _ev_pitch_list(el: note.GeneralNote) -> list[str]:
    if el.isRest:
        return []
    if isinstance(el, chord.Chord):
        return sorted(_m21_pitch_canon(p) for p in el.pitches)
    return [_m21_pitch_canon(el.pitch)]


def _norm_voice(vid: str, aud_staff: int) -> str:
    s = str(vid)
    if aud_staff > 1 and s.isdigit() and int(s) > 4 * (aud_staff - 1):
        return str(int(s) - 4 * (aud_staff - 1))
    return s


def _denorm_voice(norm_vid: str, aud_staff: int) -> str:
    if aud_staff > 1 and norm_vid.isdigit():
        return str(int(norm_vid) + 4 * (aud_staff - 1))
    return norm_vid


def _frac(val) -> Fraction:
    if isinstance(val, Fraction):
        return val
    if isinstance(val, str):
        return Fraction(val)
    return Fraction(val).limit_denominator(256)


def _build_duration(
    dur_q: Fraction,
    note_type: str | None = None,
    dots: int = 0,
    time_mod: tuple[int, int] | list[int] | None = None,
    *,
    is_measure_rest: bool = False,
) -> m21dur.Duration:
    if is_measure_rest:
        return m21dur.Duration(float(dur_q))
    if time_mod and len(time_mod) == 2 and note_type:
        try:
            d = m21dur.Duration(type=note_type, dots=int(dots or 0))
            d.appendTuplet(m21dur.Tuplet(numberNotesActual=int(time_mod[0]), numberNotesNormal=int(time_mod[1])))
            if abs(float(d.quarterLength) - float(dur_q)) < EPS:
                return d
        except Exception:
            pass
    d = m21dur.Duration(dur_q)
    if d.type in ("complex", "inexpressible", "zero") and note_type:
        try:
            d2 = m21dur.Duration(type=note_type, dots=int(dots or 0))
            if abs(float(d2.quarterLength) - float(dur_q)) < EPS:
                return d2
        except Exception:
            pass
    return d


def _valid_duration_for_preflight(dur: m21dur.Duration, *, is_measure_rest: bool = False) -> bool:
    if dur is None or dur.isGrace:
        return True
    if is_measure_rest and float(dur.quarterLength) >= 1.0 / 256.0:
        return True
    types = [c.type for c in dur.components]
    for tup in dur.tuplets:
        for td in (tup.durationActual, tup.durationNormal):
            if td is not None:
                types.append(td.type)
    for t in types:
        if t in m21dur.typeToDuration and 0 < m21dur.typeToDuration[t] < 1.0 / 256.0:
            return False
    if 0 < float(dur.quarterLength) < 1.0 / 256.0:
        return False
    if dur.type in ("inexpressible", "zero"):
        return False
    if not is_measure_rest and dur.type == "complex":
        return False
    return True


def _measure_pitch_multiset(voices: dict[str, list[tuple[Fraction, note.GeneralNote]]]) -> Counter:
    c: Counter = Counter()
    for evs in voices.values():
        for _, el in evs:
            for p in _ev_pitch_list(el):
                c[p] += 1
    return c


def _measure_lyric_multiset(voices: dict[str, list[tuple[Fraction, note.GeneralNote]]]) -> Counter:
    c: Counter = Counter()
    for evs in voices.values():
        for _, el in evs:
            for ly in getattr(el, "lyrics", None) or ():
                txt = (getattr(ly, "text", None) or "").strip()
                if txt:
                    num = getattr(ly, "number", 1) or 1
                    c[(num, txt)] += 1
    return c


def _voice_exact_meter_ok(
    evs: list[tuple[Fraction, note.GeneralNote]],
    expected_q: Fraction,
) -> bool:
    if not evs:
        return False
    ordered = sorted(evs, key=lambda it: (it[0], 0 if not it[1].isRest else 1))
    total = Fraction(0)
    cursor = Fraction(0)
    for off, el in ordered:
        is_mr = bool(el.isRest and getattr(el, "fullMeasure", False) in (True, "always"))
        if not _valid_duration_for_preflight(el.duration, is_measure_rest=is_mr):
            return False
        dq = _frac(el.quarterLength)
        if dq <= 0:
            return False
        if off < cursor - Fraction(1, 1024):
            return False
        if off > cursor + Fraction(1, 1024):
            return False
        total += dq
        cursor = off + dq
    return total == expected_q and cursor == expected_q


def _xml_meta(path: Path) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int, int, str, int], float]]:
    """Read XML <part>/<staves> mapping and per-event default-x from MXL/XML."""
    text: str | None = None
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.endswith((".xml", ".musicxml")) and not n.startswith("META-INF")]
                if names:
                    target = next((n for n in names if "score" in n.lower()), names[0])
                    text = z.read(target).decode("utf-8", errors="replace")
        elif path.is_file() and path.suffix.lower() in (".xml", ".musicxml"):
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, {}
    if not text:
        return {}, {}
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", text, count=1))
    except ET.ParseError:
        return {}, {}
    if root.tag != "score-partwise":
        return {}, {}

    part_staff_map: dict[tuple[int, int], int] = {}
    dx_map: dict[tuple[int, int, int, str, int], float] = {}
    m21_idx = 0
    for pi, part in enumerate(root.findall("part"), 1):
        staves = max([int(s.text) for s in part.findall(".//attributes/staves") if s.text] + [1])
        for st in range(1, staves + 1):
            part_staff_map[(pi, st)] = m21_idx
            m21_idx += 1
        for mi, m in enumerate(part.findall("measure"), 1):
            counts: dict[tuple[int, str], int] = defaultdict(int)
            for el in m:
                if el.tag != "note" or el.find("grace") is not None or el.find("chord") is not None:
                    continue
                st = int((el.findtext("staff") or "1").strip())
                v = _norm_voice((el.findtext("voice") or "1").strip(), st)
                ev_i = counts[(st, v)]
                counts[(st, v)] += 1
                dx = el.attrib.get("default-x")
                if dx is not None:
                    try:
                        dx_map[(pi, st, mi, v, ev_i)] = float(dx)
                    except ValueError:
                        pass
    return part_staff_map, dx_map


def _piece_from_path(path: Path, pieces: dict) -> str | None:
    parts_low = [p.lower() for p in path.resolve().parts]
    for name in pieces:
        if name.lower() in parts_low:
            return name
    return None


def _load_graft_records(
    mxl_path: Path,
    cfg: GraftConfig,
) -> dict[tuple[int, int, int], list[dict]]:
    """Load repair records for ``mxl_path`` keyed by ``(mxl_idx, aud_part, aud_staff)``."""
    sources: list[Path] = []
    sidecar = mxl_path.with_name(mxl_path.stem + GRAFT_SIDECAR_SUFFIX)
    if sidecar.is_file():
        sources.append(sidecar)
    if cfg.graft_json_path and Path(cfg.graft_json_path).is_file():
        p = Path(cfg.graft_json_path)
        if p not in sources:
            sources.append(p)
    if not sources:
        return {}

    by_key: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    seen_sigs: set[tuple[int, int, int, str]] = set()
    mxl_name = mxl_path.name
    for src in sources:
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records: list[dict] = []
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            if "records" in data and isinstance(data["records"], list):
                records.extend(data["records"])
            if "split_half_records" in data and isinstance(data["split_half_records"], list):
                records.extend(data["split_half_records"])
            if not records and "runs" in data and isinstance(data["runs"], dict):
                run_obj = data["runs"].get("dev3e") or next(iter(data["runs"].values()), None) or {}
                pieces = run_obj.get("pieces", {})
                matched_piece = _piece_from_path(mxl_path, pieces)
                if matched_piece is not None:
                    pdata = pieces[matched_piece]
                    records = list(pdata.get("records", [])) + list(pdata.get("split_half_records", []))
                else:
                    for pdata in pieces.values():
                        records.extend(pdata.get("records", []))
                        records.extend(pdata.get("split_half_records", []))

        for rec in records:
            if not isinstance(rec, dict) or not rec.get("graft_payload"):
                continue
            bar = rec.get("bar") or {}
            rec_file = bar.get("mxl_file") or rec.get("mxl_file")
            if rec_file and rec_file != mxl_name and src != sidecar:
                continue
            mxl_idx = int(bar.get("mxl_idx") or rec.get("mxl_idx") or 0)
            if mxl_idx <= 0:
                continue
            st = rec.get("staff") or {}
            ap = int(st.get("aud_part") or rec.get("aud_part") or st.get("active_staff_idx") or rec.get("active_staff_idx") or 1)
            ast = int(st.get("aud_staff") or rec.get("aud_staff") or 1)
            sig = (mxl_idx, ap, ast, json.dumps(rec.get("graft_payload"), sort_keys=True))
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            by_key[(mxl_idx, ap, ast)].append(rec)
    return by_key


def _declared_meters(parts: list[stream.Part], inherit_ts: str | None) -> list[Fraction | None]:
    n = max((len(list(p.getElementsByClass(stream.Measure))) for p in parts), default=0)
    per_part = [list(p.getElementsByClass(stream.Measure)) for p in parts]
    cur: Fraction | None = None
    if inherit_ts:
        try:
            num, den = str(inherit_ts).split("/")
            cur = Fraction(sum(int(x) for x in num.split("+")) * 4, int(den))
        except Exception:
            cur = None
    out: list[Fraction | None] = []
    for i in range(n):
        for ms in per_part:
            if i >= len(ms):
                continue
            tss = sorted(ms[i].getElementsByClass(meter.TimeSignature), key=lambda t: float(t.offset))
            if tss and float(tss[0].offset) <= EPS:
                try:
                    cur = Fraction(int(round(float(tss[0].barDuration.quarterLength) * 256)), 256)
                    # Exact rational beats * 4 / beat-type when available
                    cur = Fraction(tss[0].numerator * 4, tss[0].denominator)
                except Exception:
                    pass
                break
        out.append(cur)
    return out


def _extract_measure_voices(
    m: stream.Measure,
    aud_staff: int,
    dx_map: dict[tuple[int, int, int, str, int], float],
    aud_part: int,
    mxl_idx: int,
) -> tuple[dict[str, list[tuple[Fraction, note.GeneralNote]]], dict[int, float | None], bool]:
    """Extract normalized voice_id -> [(offset_frac, el)] and element id -> default-x."""
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]] = {}
    el_dx: dict[int, float | None] = {}
    had_explicit_voices = bool(m.voices)
    if m.voices:
        for v in m.voices:
            nv = _norm_voice(str(v.id), aud_staff)
            base = _frac(float(v.offset))
            evs = []
            ordered = sorted(
                (e for e in v.notesAndRests if not e.duration.isGrace),
                key=lambda e: (float(e.offset), 0 if not e.isRest else 1),
            )
            for idx, el in enumerate(ordered):
                off = base + _frac(float(el.offset))
                evs.append((off, el))
                ax = getattr(el.style, "absoluteX", None) if el.hasStyleInformation else None
                if ax is None:
                    ax = dx_map.get((aud_part, aud_staff, mxl_idx, nv, idx))
                el_dx[id(el)] = float(ax) if ax is not None else None
            if evs:
                voices.setdefault(nv, []).extend(evs)
        for nv in voices:
            voices[nv].sort(key=lambda it: (it[0], 0 if not it[1].isRest else 1))
    else:
        ordered = sorted(
            (e for e in m.notesAndRests if not e.duration.isGrace),
            key=lambda e: (float(e.offset), 0 if not e.isRest else 1),
        )
        if ordered:
            evs = []
            for idx, el in enumerate(ordered):
                off = _frac(float(el.offset))
                evs.append((off, el))
                ax = getattr(el.style, "absoluteX", None) if el.hasStyleInformation else None
                if ax is None:
                    ax = dx_map.get((aud_part, aud_staff, mxl_idx, "1", idx))
                el_dx[id(el)] = float(ax) if ax is not None else None
            voices["1"] = evs
    return voices, el_dx, had_explicit_voices


def _sounding_columns(
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    tol_tenths: float = 16.0,
) -> list[dict]:
    items: list[tuple[str, Fraction, note.GeneralNote, float | None]] = []
    for nv, evs in voices.items():
        for off, el in evs:
            if not el.isRest:
                items.append((nv, off, el, el_dx.get(id(el))))
    if not items:
        return []
    if all(dx is not None for _, _, _, dx in items):
        items.sort(key=lambda t: (t[3], t[1], t[0]))  # type: ignore[arg-type]
        clusters: list[list[tuple[str, Fraction, note.GeneralNote, float | None]]] = [[items[0]]]
        for it in items[1:]:
            mean_x = sum(x[3] for x in clusters[-1]) / len(clusters[-1])  # type: ignore[misc]
            if abs(it[3] - mean_x) <= tol_tenths:  # type: ignore[operator]
                clusters[-1].append(it)
            else:
                clusters.append([it])
        return [
            {
                "mean_x": sum(x[3] for x in cl) / len(cl),  # type: ignore[misc]
                "pitches": frozenset(p for _, _, el, _ in cl for p in _ev_pitch_list(el)),
                "items": cl,
            }
            for cl in clusters
        ]
    by_on: dict[Fraction, list[tuple[str, Fraction, note.GeneralNote, float | None]]] = defaultdict(list)
    for it in items:
        by_on[it[1]].append(it)
    return [
        {
            "mean_x": None,
            "pitches": frozenset(p for _, _, el, _ in by_on[on] for p in _ev_pitch_list(el)),
            "items": by_on[on],
        }
        for on in sorted(by_on)
    ]


def _copy_ornaments(
    src_el: note.GeneralNote,
    dst_el: note.GeneralNote,
    pitch_str: str | None = None,
    *,
    copy_non_lyric: bool = True,
) -> None:
    if isinstance(src_el, (note.Note, chord.Chord)) and isinstance(dst_el, (note.Note, chord.Chord)):
        if copy_non_lyric:
            dst_el.articulations = deepcopy(src_el.articulations)
            dst_el.expressions = deepcopy(src_el.expressions)
        dst_el.lyrics = deepcopy(src_el.lyrics)
    tie = getattr(src_el, "tie", None)
    if isinstance(src_el, chord.Chord) and pitch_str is not None:
        try:
            tie = src_el.getTie(_to_m21_pitch(pitch_str)) or tie
        except Exception:
            pass
    if tie is not None and not dst_el.isRest:
        dst_el.tie = deepcopy(tie)


def _el_lyric_keys(el: note.GeneralNote) -> list[tuple[int, int | str, str]]:
    out: list[tuple[int, int | str, str]] = []
    for ly in getattr(el, "lyrics", None) or ():
        txt = (getattr(ly, "text", None) or "").strip()
        if txt:
            num = getattr(ly, "number", 1) or 1
            out.append((id(el), num, txt))
    return out


def _mark_el_lyrics_consumed(el: note.GeneralNote, consumed: set[tuple[int, int | str]]) -> None:
    for eid, num, _ in _el_lyric_keys(el):
        consumed.add((eid, num))


def _unconsumed_lyric_count(el: note.GeneralNote, consumed: set[tuple[int, int | str]]) -> int:
    return sum(1 for eid, num, _ in _el_lyric_keys(el) if (eid, num) not in consumed)


def _dedup_col_src_lyrics(
    src_cols: list[dict],
    tgt_onsets: list[Fraction],
    tgt_by_on: dict[Fraction, list[tuple[str, dict]]],
    col_to_on: dict[int, Fraction],
    src_lyrics: Counter,
) -> Counter:
    """Dedup ONLY redundant same-column/same-pitch exact (verse, text) OMR collisions."""
    adj = Counter(src_lyrics)
    for col_idx, col in enumerate(src_cols):
        on = col_to_on.get(col_idx)
        if on is None:
            continue
        tgt_pitch_cnt: Counter = Counter()
        for _, tev in tgt_by_on.get(on, []):
            for p in tev.get("pitches") or ():
                tgt_pitch_cnt[p] += 1
        col_pitch_ly: dict[tuple[str, int | str, str], int] = Counter()
        for _, _, sel, _ in col.get("items", []):
            ps = _ev_pitch_list(sel)
            for _, num, txt in _el_lyric_keys(sel):
                for p in ps:
                    col_pitch_ly[(p, num, txt)] += 1
        for (p, num, txt), cnt in col_pitch_ly.items():
            cap = tgt_pitch_cnt.get(p, 0)
            if cnt > 1 and 0 < cap < cnt:
                adj[(num, txt)] -= (cnt - cap)
    return Counter({k: v for k, v in adj.items() if v > 0})


def _apply_duration_graft_json(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    aud_staff: int,
    expected_q: Fraction,
    payload: dict,
    record_sub_cause: str,
    cfg: GraftConfig,
) -> tuple[bool, str | None]:
    edits = payload.get("edits") or []
    enabled_edits = []
    for ed in edits:
        sc = ed.get("sub_cause") or record_sub_cause
        if sc in F1_CAUSES and cfg.f1_type_a_dot:
            enabled_edits.append(ed)
        elif sc in F2_CAUSES and cfg.f2_type_a_beam_meter:
            enabled_edits.append(ed)
    if not enabled_edits:
        return False, None

    src_pitches = _measure_pitch_multiset(voices)
    src_lyrics = _measure_lyric_multiset(voices)

    # Plan mutations on copies/specs first; commit only after ALL gates pass.
    planned_voices: dict[str, list[tuple[Fraction, note.GeneralNote, m21dur.Duration, bool]]] = {}
    repaired_vids: set[str] = set()
    by_v: dict[str, list[dict]] = defaultdict(list)
    for ed in enabled_edits:
        by_v[str(ed["voice"])].append(ed)

    for nv, v_edits in by_v.items():
        if nv not in voices:
            return False, None
        evs = voices[nv]
        # Verify each edit target matches pitches & is_rest
        edit_by_idx: dict[int, dict] = {}
        for ed in v_edits:
            idx = int(ed["event_idx"])
            if idx < 0 or idx >= len(evs) or idx in edit_by_idx:
                return False, None
            _, el = evs[idx]
            if bool(el.isRest) != bool(ed.get("is_rest", False)):
                return False, None
            want_ps = sorted(_canon_pitch_str(p) for p in (ed.get("pitches") or []))
            if _ev_pitch_list(el) != want_ps:
                return False, None
            edit_by_idx[idx] = ed

        shift = Fraction(0)
        new_evs: list[tuple[Fraction, note.GeneralNote, m21dur.Duration, bool]] = []
        for idx, (off, el) in enumerate(evs):
            cur_off = off + shift
            if idx in edit_by_idx:
                ed = edit_by_idx[idx]
                old_dq = _frac(el.quarterLength)
                new_dq = _frac(ed["rust_dur_q"])
                is_mr = bool(el.isRest and (ed.get("rust_type") == "measure-rest" or getattr(el, "fullMeasure", False) in (True, "always")))
                new_dur = _build_duration(
                    new_dq,
                    ed.get("rust_type"),
                    int(ed.get("rust_dots") or 0),
                    ed.get("rust_tuplet"),
                    is_measure_rest=is_mr,
                )
                shift += new_dq - old_dq
                new_evs.append((cur_off, el, new_dur, is_mr))
            else:
                is_mr = bool(el.isRest and getattr(el, "fullMeasure", False) in (True, "always"))
                new_evs.append((cur_off, el, el.duration, is_mr))
        planned_voices[nv] = new_evs
        repaired_vids.add(nv)

    # Exact Meter Balance Gate on every repaired voice
    for nv in repaired_vids:
        check_evs = []
        for off, el, ndur, is_mr in planned_voices[nv]:
            if not _valid_duration_for_preflight(ndur, is_measure_rest=is_mr):
                return False, None
            clone = deepcopy(el)
            clone.duration = ndur
            if is_mr:
                clone.fullMeasure = True
            check_evs.append((off, clone))
        if not _voice_exact_meter_ok(check_evs, expected_q):
            return False, None

    # Pitch & Lyric Gates across entire staff bar
    cand_voices = dict(voices)
    for nv in repaired_vids:
        cand_voices[nv] = [(off, deepcopy(el)) for off, el, _, _ in planned_voices[nv]]
        for (_, cel), (_, _, ndur, is_mr) in zip(cand_voices[nv], planned_voices[nv]):
            cel.duration = ndur
            if is_mr:
                cel.fullMeasure = True
    if _measure_pitch_multiset(cand_voices) != src_pitches:
        return False, None
    if _measure_lyric_multiset(cand_voices) != src_lyrics:
        return False, None

    # Commit in-place preserving expressions/articulations/lyrics/ties/spanners
    for nv in repaired_vids:
        container = None
        if m.voices:
            for v in m.voices:
                if _norm_voice(str(v.id), aud_staff) == nv:
                    container = v
                    break
        else:
            container = m
        if container is None:
            return False, None
        base = _frac(float(container.offset)) if container is not m else Fraction(0)
        for off, el, ndur, is_mr in planned_voices[nv]:
            el.duration = ndur
            if is_mr:
                el.fullMeasure = True
            container.setElementOffset(el, float(off - base))

    used_causes = sorted({ed.get("sub_cause") or record_sub_cause for ed in enabled_edits})
    tag = "F1:" + "+".join(used_causes) if all(c in F1_CAUSES for c in used_causes) else "F2:" + "+".join(used_causes)
    return True, tag


def _apply_intrinsic_f2_measure_rest(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    aud_staff: int,
    expected_q: Fraction,
) -> tuple[bool, str | None]:
    if len(voices) != 1:
        return False, None
    nv, evs = next(iter(voices.items()))
    if len(evs) != 1:
        return False, None
    off, el = evs[0]
    if not el.isRest:
        return False, None
    is_mr = getattr(el, "fullMeasure", False) in (True, "always")
    if not is_mr:
        return False, None
    cur_q = _frac(el.quarterLength)
    if cur_q == expected_q or off != 0:
        return False, None
    new_dur = m21dur.Duration(float(expected_q))
    if not _valid_duration_for_preflight(new_dur, is_measure_rest=True):
        return False, None
    container = m
    if m.voices:
        for v in m.voices:
            if _norm_voice(str(v.id), aud_staff) == nv:
                container = v
                break
    el.duration = new_dur
    el.fullMeasure = True
    container.setElementOffset(el, 0.0)
    return True, "F2:intrinsic_measure_rest"


def _apply_rest_splice_plan(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    aud_staff: int,
    expected_q: Fraction,
    splices: list[dict],
    tag: str,
) -> tuple[bool, str | None]:
    if not splices:
        return False, None
    src_pitches = _measure_pitch_multiset(voices)
    src_lyrics = _measure_lyric_multiset(voices)

    by_v: dict[str, list[dict]] = defaultdict(list)
    for sp in splices:
        by_v[str(sp["voice"])].append(sp)

    # Plan per-voice new event list [(off, el, is_new_rest)]
    planned: dict[str, list[tuple[Fraction, note.GeneralNote, bool]]] = {}
    for nv, v_splices in by_v.items():
        cur_evs: list[tuple[Fraction, note.GeneralNote, bool]] = [
            (off, el, False) for off, el in voices.get(nv, [])
        ]
        for sp in sorted(v_splices, key=lambda s: _frac(s["gap_onset_q"])):
            on = _frac(sp["gap_onset_q"])
            dq = _frac(sp["dur_q"])
            if dq <= 0:
                return False, None
            is_mr = sp.get("rest_type") == "measure-rest" or dq == expected_q
            new_dur = _build_duration(dq, sp.get("rest_type"), 0, None, is_measure_rest=is_mr)
            if not _valid_duration_for_preflight(new_dur, is_measure_rest=is_mr):
                return False, None
            r = note.Rest(duration=new_dur)
            if is_mr:
                r.fullMeasure = True

            overlaps = any(
                off < on + dq - Fraction(1, 1024) and off + _frac(el.quarterLength) > on + Fraction(1, 1024)
                for off, el, _ in cur_evs
            )
            next_evs: list[tuple[Fraction, note.GeneralNote, bool]] = []
            if overlaps:
                for off, el, is_new in cur_evs:
                    if off >= on - Fraction(1, 1024):
                        next_evs.append((off + dq, el, is_new))
                    else:
                        next_evs.append((off, el, is_new))
            else:
                next_evs.extend(cur_evs)
            next_evs.append((on, r, True))
            next_evs.sort(key=lambda t: (t[0], 0 if not t[1].isRest else 1))
            cur_evs = next_evs

        # Exact Meter Balance Gate on repaired voice
        if not _voice_exact_meter_ok([(off, el) for off, el, _ in cur_evs], expected_q):
            return False, None
        planned[nv] = cur_evs

    cand_voices = dict(voices)
    for nv, pevs in planned.items():
        cand_voices[nv] = [(off, el) for off, el, _ in pevs]
    if _measure_pitch_multiset(cand_voices) != src_pitches:
        return False, None
    if _measure_lyric_multiset(cand_voices) != src_lyrics:
        return False, None

    # Commit into m
    existing_v_objs = {_norm_voice(str(v.id), aud_staff): v for v in m.voices}
    for nv, pevs in planned.items():
        if m.voices:
            v_obj = existing_v_objs.get(nv)
            if v_obj is None:
                v_obj = stream.Voice(id=_denorm_voice(nv, aud_staff))
                m.insert(0.0, v_obj)
            base = _frac(float(v_obj.offset))
            for off, el, is_new in pevs:
                if is_new:
                    v_obj.insert(float(off - base), el)
                else:
                    v_obj.setElementOffset(el, float(off - base))
        else:
            if nv == "1":
                for off, el, is_new in pevs:
                    if is_new:
                        m.insert(float(off), el)
                    else:
                        m.setElementOffset(el, float(off))
            else:
                # Wrap loose elements into voice 1 and add new voice
                loose = [e for e in list(m.notesAndRests) if not e.duration.isGrace]
                v1 = stream.Voice(id=_denorm_voice("1", aud_staff))
                for le in loose:
                    loff = float(m.elementOffset(le))
                    m.remove(le)
                    v1.insert(loff, le)
                m.insert(0.0, v1)
                v_obj = stream.Voice(id=_denorm_voice(nv, aud_staff))
                for off, el, _ in pevs:
                    v_obj.insert(float(off), el)
                m.insert(0.0, v_obj)
    return True, tag


def _apply_rest_splice_json(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    aud_staff: int,
    expected_q: Fraction,
    payload: dict,
    cfg: GraftConfig,
) -> tuple[bool, str | None]:
    splices = payload.get("rest_splices") or []
    enabled = []
    for sp in splices:
        st = sp.get("sub_type") or "shared_mid_staff_rest"
        if st in F3_CAUSES and cfg.f3_type_b_shared_rest:
            enabled.append(sp)
        elif st in F4_CAUSES and cfg.f4_type_b_unprinted_rest:
            enabled.append(sp)
    if not enabled:
        return False, None
    types = sorted({sp.get("sub_type", "shared_mid_staff_rest") for sp in enabled})
    prefix = "F4:" if any(t in F4_CAUSES for t in types) and not any(t in F3_CAUSES for t in types) else "F3:"
    return _apply_rest_splice_plan(m, voices, aud_staff, expected_q, enabled, prefix + "+".join(types))


def _apply_intrinsic_f3(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
) -> tuple[bool, str | None]:
    if len(voices) < 2:
        return False, None
    full_vids = [nv for nv, evs in voices.items() if _voice_exact_meter_ok(evs, expected_q)]
    if not full_vids:
        return False, None
    sib_evs = voices[full_vids[0]]
    sib_rests = [(off, _frac(el.quarterLength)) for off, el in sib_evs if el.isRest]
    if not sib_rests:
        return False, None

    splices: list[dict] = []
    for nv, evs in sorted(voices.items()):
        if nv in full_vids:
            continue
        cur_sum = sum((_frac(el.quarterLength) for _, el in evs), Fraction(0))
        deficit = expected_q - cur_sum
        if deficit <= 0:
            continue
        # Find sibling rests not covered by existing rests in nv whose durations equal deficit
        # Case A: open gaps in nv matching sibling rests
        open_cands: list[tuple[Fraction, Fraction]] = []
        for r_on, r_dq in sib_rests:
            overlaps = any(
                off < r_on + r_dq - Fraction(1, 1024) and off + _frac(el.quarterLength) > r_on + Fraction(1, 1024)
                for off, el in evs
            )
            if not overlaps:
                open_cands.append((r_on, r_dq))
        if open_cands and sum((d for _, d in open_cands), Fraction(0)) == deficit:
            # Verify default-x alignment on sounding notes at matching offsets (if available)
            sib_x_by_on = {off: el_dx.get(id(el)) for off, el in sib_evs if not el.isRest}
            aligned = True
            for off, el in evs:
                if el.isRest:
                    continue
                sx = sib_x_by_on.get(off)
                nx = el_dx.get(id(el))
                if sx is not None and nx is not None and abs(sx - nx) > 25.0:
                    aligned = False
                    break
            if aligned:
                for r_on, r_dq in open_cands:
                    splices.append({"voice": nv, "gap_onset_q": str(r_on), "dur_q": str(r_dq), "rest_type": ""})
                continue
        # Case B: single contiguous rest in sib_rests matching deficit, where shifting notes >= r_on aligns default-x
        matching_single = [(r_on, r_dq) for r_on, r_dq in sib_rests if r_dq == deficit]
        for r_on, r_dq in matching_single:
            sib_x_by_on = {off: el_dx.get(id(el)) for off, el in sib_evs if not el.isRest}
            checked = 0
            aligned = True
            for off, el in evs:
                if el.isRest:
                    continue
                target_off = off + r_dq if off >= r_on - Fraction(1, 1024) else off
                sx = sib_x_by_on.get(target_off)
                nx = el_dx.get(id(el))
                if sx is not None and nx is not None:
                    checked += 1
                    if abs(sx - nx) > 25.0:
                        aligned = False
                        break
            if aligned and checked > 0:
                splices.append({"voice": nv, "gap_onset_q": str(r_on), "dur_q": str(r_dq), "rest_type": ""})
                break

    if not splices:
        return False, None
    return _apply_rest_splice_plan(m, voices, aud_staff, expected_q, splices, "F3:intrinsic_shared_mid_staff_rest")


def _build_reconstructed_voices(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
    target_map: dict[str, list[dict]],
    tag: str,
    *,
    lyric_dedup: bool = False,
) -> tuple[bool, str | None]:
    if not target_map:
        return False, None
    src_cols = _sounding_columns(voices, el_dx, tol_tenths=16.0)
    src_all_pitches = {p for col in src_cols for p in col["pitches"]}
    src_lyrics = _measure_lyric_multiset(voices)

    # Check Pitch Gate on target columns vs source columns
    tgt_by_on: dict[Fraction, list[tuple[str, dict]]] = defaultdict(list)
    tgt_all_pitches: set[str] = set()
    for nv, evs in target_map.items():
        for ev in evs:
            if not ev.get("is_rest") and ev.get("pitches"):
                on = _frac(ev["onset_q"])
                ps = [_canon_pitch_str(p) for p in ev["pitches"]]
                tgt_all_pitches.update(ps)
                tgt_by_on[on].append((nv, {**ev, "pitches": ps}))

    if tgt_all_pitches != src_all_pitches:
        return False, None
    sorted_onsets = sorted(tgt_by_on)
    tgt_cols = [
        frozenset(p for _, ev in tgt_by_on[on] for p in ev["pitches"])
        for on in sorted_onsets
    ]
    if tgt_cols != [col["pitches"] for col in src_cols]:
        return False, None

    # Map each column onset -> source column items
    col_map = {on: src_cols[i] for i, on in enumerate(sorted_onsets)}
    col_to_on = {i: on for i, on in enumerate(sorted_onsets)}
    consumed_lyrics: set[tuple[int, int | str]] | None = set() if lyric_dedup else None

    new_voices: dict[str, list[tuple[Fraction, note.GeneralNote]]] = {}
    for nv in sorted(target_map):
        evs = target_map[nv]
        built: list[tuple[Fraction, note.GeneralNote]] = []
        for ev in sorted(evs, key=lambda e: (_frac(e["onset_q"]), 0 if not e.get("is_rest") else 1)):
            on = _frac(ev["onset_q"])
            dq = _frac(ev["dur_q"])
            is_rest = bool(ev.get("is_rest"))
            is_mr = bool(ev.get("is_measure_rest")) or (is_rest and ev.get("type") == "measure-rest")
            dur = _build_duration(
                dq,
                ev.get("type"),
                int(ev.get("dots") or 0),
                ev.get("time_mod"),
                is_measure_rest=is_mr,
            )
            if not _valid_duration_for_preflight(dur, is_measure_rest=is_mr):
                return False, None
            if is_rest or not ev.get("pitches"):
                el: note.GeneralNote = note.Rest(duration=dur)
                if is_mr:
                    el.fullMeasure = True
                # Preserve any rest fermata from same onset/voice if present
                for src_off, src_el in voices.get(nv, []):
                    if src_el.isRest and abs(float(src_off - on)) <= EPS:
                        if getattr(src_el, "expressions", None):
                            el.expressions = deepcopy(src_el.expressions)
                        break
            else:
                ps = [_canon_pitch_str(p) for p in ev["pitches"]]
                col_items = col_map.get(on, {}).get("items", [])
                if len(ps) == 1:
                    el = note.Note(_to_m21_pitch(ps[0]), duration=dur)
                    src_match = _find_source_for_pitch(
                        col_items, ps[0], nv, consumed_lyrics=consumed_lyrics
                    )
                    if src_match is not None:
                        _copy_ornaments(src_match, el, ps[0])
                        if consumed_lyrics is not None:
                            _mark_el_lyrics_consumed(src_match, consumed_lyrics)
                else:
                    el = chord.Chord([_to_m21_pitch(p) for p in ps], duration=dur)
                    for p in ps:
                        src_match = _find_source_for_pitch(
                            col_items, p, nv, consumed_lyrics=consumed_lyrics
                        )
                        if src_match is not None:
                            _copy_ornaments(src_match, el, p)
                            if consumed_lyrics is not None:
                                _mark_el_lyrics_consumed(src_match, consumed_lyrics)
                            break
                if nv == "1":
                    el.stemDirection = "up"
                elif nv == "2":
                    el.stemDirection = "down"
            built.append((on, el))
        # Exact Meter Balance Gate on every target voice
        if not _voice_exact_meter_ok(built, expected_q):
            return False, None
        new_voices[nv] = built

    if lyric_dedup and consumed_lyrics is not None:
        # Transfer any unconsumed distinct-verse syllables onto a matching target note in the same column
        for on, col in col_map.items():
            for _, _, sel, _ in col.get("items", []):
                sel_ps = set(_ev_pitch_list(sel))
                for eid, num, txt in _el_lyric_keys(sel):
                    if (eid, num) in consumed_lyrics:
                        continue
                    for _, el in (it for evs in new_voices.values() for it in evs if it[0] == on and not it[1].isRest):
                        if set(_ev_pitch_list(el)) & sel_ps:
                            have_nums = {getattr(ly, "number", 1) or 1 for ly in getattr(el, "lyrics", None) or ()}
                            if num not in have_nums:
                                for ly in sel.lyrics:
                                    if (getattr(ly, "number", 1) or 1) == num:
                                        el.lyrics.append(deepcopy(ly))
                                        consumed_lyrics.add((eid, num))
                                        break
                            if (eid, num) in consumed_lyrics:
                                break

    req_lyrics = (
        _dedup_col_src_lyrics(src_cols, sorted_onsets, tgt_by_on, col_to_on, src_lyrics)
        if lyric_dedup
        else src_lyrics
    )
    # Lyric Gate: 100% of required source syllables must appear in reconstructed measure
    dst_lyrics = _measure_lyric_multiset(new_voices)
    for k, cnt in req_lyrics.items():
        if dst_lyrics[k] < cnt:
            return False, None

    # Commit: replace voices/notesAndRests in m while keeping all non-note elements
    for v in list(m.voices):
        m.remove(v)
    for le in list(m.notesAndRests):
        if not le.duration.isGrace:
            m.remove(le)
    for nv, built in new_voices.items():
        v_obj = stream.Voice(id=_denorm_voice(nv, aud_staff))
        for off, el in built:
            v_obj.insert(float(off), el)
        m.insert(0.0, v_obj)
    return True, tag


def _find_source_for_pitch(
    col_items: list[tuple[str, Fraction, note.GeneralNote, float | None]],
    target_pitch: str,
    prefer_voice: str,
    *,
    consumed_lyrics: set[tuple[int, int | str]] | None = None,
) -> note.GeneralNote | None:
    cands = [(sv, sel) for sv, _, sel, _ in col_items if target_pitch in _ev_pitch_list(sel)]
    if not cands:
        return None
    if consumed_lyrics is not None:
        cands.sort(key=lambda it: (
            0 if _unconsumed_lyric_count(it[1], consumed_lyrics) > 0 else 1,
            0 if it[0] == prefer_voice else 1,
            0 if getattr(it[1], "lyrics", None) else 1,
            0 if getattr(it[1], "expressions", None) else 1,
            0 if getattr(it[1], "tie", None) is not None else 1,
        ))
    else:
        cands.sort(key=lambda it: (
            0 if it[0] == prefer_voice else 1,
            0 if getattr(it[1], "lyrics", None) else 1,
            0 if getattr(it[1], "expressions", None) else 1,
            0 if getattr(it[1], "tie", None) is not None else 1,
        ))
    return cands[0][1]


def _step_oct(canon: str) -> tuple[str, str]:
    m = _PITCH_RE.match(canon.strip())
    return (m.group(1).upper(), m.group(3)) if m else (canon, "")


def _lcs_len(a: list[tuple[str, str]], b: list[tuple[str, str]]) -> int:
    dp = [0] * (len(b) + 1)
    for ca in a:
        ndp = [0] * (len(b) + 1)
        for j, cb in enumerate(b):
            ndp[j + 1] = dp[j] + 1 if ca == cb else max(dp[j + 1], ndp[j])
        dp = ndp
    return dp[-1]


def _align_type_d_columns(
    src_cols: list[dict],
    tgt_onsets: list[Fraction],
    tgt_by_on: dict[Fraction, list[tuple[str, dict]]],
) -> tuple[dict[Fraction, int], dict[int, Fraction]]:
    """Monotonically align source columns to target onsets by (step, octave) overlap."""
    s_len, t_len = len(src_cols), len(tgt_onsets)
    if not s_len or not t_len:
        return {}, {}
    scores: list[list[int]] = [[0] * t_len for _ in range(s_len)]
    for i, sc in enumerate(src_cols):
        s_so = Counter(_step_oct(p) for p in sc["pitches"])
        for j, on in enumerate(tgt_onsets):
            tps = [p for _, tev in tgt_by_on[on] for p in tev["pitches"]]
            t_so = Counter(_step_oct(p) for p in tps)
            ov = sum((s_so & t_so).values())
            ex = len(sc["pitches"] & set(tps))
            scores[i][j] = (100 * ov + 10 * ex) if ov > 0 else -50

    dp = [[0] * (t_len + 1) for _ in range(s_len + 1)]
    choice = [[0] * (t_len + 1) for _ in range(s_len + 1)]  # 1=match, 2=skip_s, 3=skip_t
    for i in range(s_len - 1, -1, -1):
        for j in range(t_len - 1, -1, -1):
            best = dp[i + 1][j]
            ch = 2
            if dp[i][j + 1] > best:
                best = dp[i][j + 1]
                ch = 3
            cand = scores[i][j] + dp[i + 1][j + 1]
            if cand > best:
                best = cand
                ch = 1
            dp[i][j] = best
            choice[i][j] = ch

    on_to_col: dict[Fraction, int] = {}
    col_to_on: dict[int, Fraction] = {}
    i = j = 0
    while i < s_len and j < t_len:
        ch = choice[i][j]
        if ch == 1:
            on_to_col[tgt_onsets[j]] = i
            col_to_on[i] = tgt_onsets[j]
            i += 1
            j += 1
        elif ch == 2:
            i += 1
        else:
            j += 1
    return on_to_col, col_to_on


def _find_type_d_source(
    col_items: list[tuple[str, Fraction, note.GeneralNote, float | None]],
    target_pitches: list[str],
    prefer_voice: str,
    consumed_lyrics: set[tuple[int, int | str]],
) -> note.GeneralNote | None:
    if not col_items:
        return None
    tgt_set = set(target_pitches)
    tgt_so = {_step_oct(p) for p in target_pitches}
    ranked: list[tuple[tuple[int, int, int, int, int, int], note.GeneralNote]] = []
    for sv, _, sel, _ in col_items:
        eps = _ev_pitch_list(sel)
        exact = 0 if (tgt_set & set(eps)) else 1
        so_ok = 0 if (tgt_so & {_step_oct(p) for p in eps}) else 1
        if exact == 1 and so_ok == 1 and not getattr(sel, "lyrics", None):
            continue
        uncons = 0 if _unconsumed_lyric_count(sel, consumed_lyrics) > 0 else 1
        pref = 0 if sv == prefer_voice else 1
        has_expr = 0 if getattr(sel, "expressions", None) else 1
        has_tie = 0 if getattr(sel, "tie", None) is not None else 1
        ranked.append(((exact, so_ok, uncons, pref, has_expr, has_tie), sel))
    if not ranked:
        return None
    ranked.sort(key=lambda it: it[0])
    return ranked[0][1]


def _apply_type_d_geometry_graft(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
    rec: dict,
    cfg: GraftConfig,
) -> tuple[bool, str | None]:
    """B15_F4 (overfull) and B15_F5 (underfull/clipped) Type D reconstruction with strict gates."""
    payload = rec.get("graft_payload") or {}
    target_map = payload.get("rust_voices_target")
    if not target_map:
        return False, None

    eff_exp_q = _frac(rec["split_half_expected_q"]) if rec.get("split_half_expected_q") else expected_q
    aud_lens = [sum((_frac(el.quarterLength) for _, el in evs), Fraction(0)) for evs in voices.values()]
    aud_ends = [off + _frac(el.quarterLength) for evs in voices.values() for off, el in evs]
    aud_max = max(aud_lens, default=Fraction(0))
    aud_end = max(aud_ends, default=Fraction(0))
    aud_min = min(aud_lens, default=Fraction(0))

    is_overfull = (aud_max > eff_exp_q) or (aud_end > eff_exp_q)
    is_underfull = (not is_overfull) and (aud_min < eff_exp_q)
    if is_overfull and not cfg.b15_f4_type_d_overfull:
        return False, None
    if is_underfull and not cfg.b15_f5_type_d_underfull:
        return False, None
    if not is_overfull and not is_underfull:
        return False, None

    # Gate 1: never overwrite print_faithful bars
    if rec.get("rust_strict_label") == "print_faithful":
        return False, None

    # Gate 2: live head count & delta gate (aud_heads >= 1, abs(rust_heads - aud_heads) <= 4)
    src_cols = _sounding_columns(voices, el_dx, tol_tenths=16.0)
    aud_heads = sum(len(_ev_pitch_list(el)) for evs in voices.values() for _, el in evs if not el.isRest)
    if aud_heads < 1 or not src_cols:
        return False, None

    tgt_by_on: dict[Fraction, list[tuple[str, dict]]] = defaultdict(list)
    rust_heads = 0
    for nv, evs in target_map.items():
        for ev in evs:
            if not ev.get("is_rest") and ev.get("pitches"):
                on = _frac(ev["onset_q"])
                ps = [_canon_pitch_str(p) for p in ev["pitches"]]
                rust_heads += len(ps)
                tgt_by_on[on].append((nv, {**ev, "pitches": ps}))
    if rust_heads == 0 or abs(rust_heads - aud_heads) > TYPE_D_MAX_HEAD_DELTA:
        return False, None

    # Gate 3: column-ordered (step, octave) anchor match ratio >= 0.70
    sorted_onsets = sorted(tgt_by_on)
    src_anchor_seq = [_step_oct(p) for col in src_cols for p in sorted(col["pitches"])]
    tgt_anchor_seq = [
        _step_oct(p)
        for on in sorted_onsets
        for p in sorted({pp for _, ev in tgt_by_on[on] for pp in ev["pitches"]})
    ]
    if not src_anchor_seq:
        return False, None
    anchor_ratio = _lcs_len(src_anchor_seq, tgt_anchor_seq) / len(src_anchor_seq)
    if anchor_ratio < TYPE_D_ANCHOR_MIN - 1e-9:
        return False, None

    on_to_col, col_to_on = _align_type_d_columns(src_cols, sorted_onsets, tgt_by_on)
    consumed_lyrics: set[tuple[int, int | str]] = set()
    consumed_ornaments: set[int] = set()
    src_lyrics = _measure_lyric_multiset(voices)

    new_voices: dict[str, list[tuple[Fraction, note.GeneralNote]]] = {}
    for nv in sorted(target_map):
        evs = target_map[nv]
        built: list[tuple[Fraction, note.GeneralNote]] = []
        for ev in sorted(evs, key=lambda e: (_frac(e["onset_q"]), 0 if not e.get("is_rest") else 1)):
            on = _frac(ev["onset_q"])
            dq = _frac(ev["dur_q"])
            is_rest = bool(ev.get("is_rest"))
            is_mr = bool(ev.get("is_measure_rest")) or (is_rest and ev.get("type") == "measure-rest")
            dur = _build_duration(
                dq,
                ev.get("type"),
                int(ev.get("dots") or 0),
                ev.get("time_mod"),
                is_measure_rest=is_mr,
            )
            if not _valid_duration_for_preflight(dur, is_measure_rest=is_mr):
                return False, None
            if is_rest or not ev.get("pitches"):
                el: note.GeneralNote = note.Rest(duration=dur)
                if is_mr:
                    el.fullMeasure = True
                for src_off, src_el in voices.get(nv, []):
                    if src_el.isRest and abs(float(src_off - on)) <= EPS:
                        if getattr(src_el, "expressions", None):
                            el.expressions = deepcopy(src_el.expressions)
                        break
            else:
                ps = [_canon_pitch_str(p) for p in ev["pitches"]]
                c_idx = on_to_col.get(on)
                col_items = src_cols[c_idx]["items"] if c_idx is not None else []
                el = note.Note(_to_m21_pitch(ps[0]), duration=dur) if len(ps) == 1 else chord.Chord(
                    [_to_m21_pitch(p) for p in ps], duration=dur
                )
                src_match = _find_type_d_source(col_items, ps, nv, consumed_lyrics)
                if src_match is not None:
                    copy_orn = id(src_match) not in consumed_ornaments
                    _copy_ornaments(src_match, el, ps[0], copy_non_lyric=copy_orn)
                    if copy_orn:
                        consumed_ornaments.add(id(src_match))
                    _mark_el_lyrics_consumed(src_match, consumed_lyrics)
                if nv == "1":
                    el.stemDirection = "up"
                elif nv == "2":
                    el.stemDirection = "down"
            built.append((on, el))

        # Gate 4: Exact Meter Balance Gate on every target voice
        if not _voice_exact_meter_ok(built, eff_exp_q):
            return False, None
        new_voices[nv] = built

    # Transfer any unconsumed source lyrics from aligned/unaligned columns onto matching target notes
    for col_idx, col in enumerate(src_cols):
        mapped_on = col_to_on.get(col_idx)
        for sv, _, sel, _ in col.get("items", []):
            for eid, num, txt in _el_lyric_keys(sel):
                if (eid, num) in consumed_lyrics:
                    continue
                cand_ons = [mapped_on] if mapped_on is not None else sorted_onsets
                placed_ly = False
                for try_on in cand_ons:
                    if try_on is None:
                        continue
                    cands_el = sorted(
                        [(v_k, it_el) for v_k, evs_b in new_voices.items() for off_b, it_el in evs_b if off_b == try_on and not it_el.isRest],
                        key=lambda kv: (0 if kv[0] == sv else 1, kv[0]),
                    )
                    for _, dst_el in cands_el:
                        have_nums = {getattr(ly, "number", 1) or 1 for ly in getattr(dst_el, "lyrics", None) or ()}
                        if num not in have_nums:
                            for ly in sel.lyrics:
                                if (getattr(ly, "number", 1) or 1) == num:
                                    dst_el.lyrics.append(deepcopy(ly))
                                    consumed_lyrics.add((eid, num))
                                    placed_ly = True
                                    break
                        if placed_ly:
                            break
                    if placed_ly:
                        break

    # Transfer any unconsumed expressions (e.g. fermatas) on non-rest source items
    for col_idx, col in enumerate(src_cols):
        mapped_on = col_to_on.get(col_idx)
        for sv, _, sel, _ in col.get("items", []):
            if id(sel) in consumed_ornaments or not getattr(sel, "expressions", None):
                continue
            try_on = mapped_on if mapped_on is not None else (sorted_onsets[-1] if sorted_onsets else None)
            if try_on is not None:
                for v_k in sorted(new_voices, key=lambda k: (0 if k == sv else 1, k)):
                    for off_b, dst_el in new_voices[v_k]:
                        if off_b == try_on and not dst_el.isRest:
                            dst_el.expressions.extend(deepcopy(sel.expressions))
                            consumed_ornaments.add(id(sel))
                            break
                    if id(sel) in consumed_ornaments:
                        break

    # Gate 5: Lyric Gate (0 dropped syllables)
    req_lyrics = (
        _dedup_col_src_lyrics(src_cols, sorted_onsets, tgt_by_on, col_to_on, src_lyrics)
        if cfg.b15_f2_lyric_dedup
        else src_lyrics
    )
    dst_lyrics = _measure_lyric_multiset(new_voices)
    for k, cnt in req_lyrics.items():
        if dst_lyrics[k] < cnt:
            return False, None

    for v in list(m.voices):
        m.remove(v)
    for le in list(m.notesAndRests):
        if not le.duration.isGrace:
            m.remove(le)
    for nv, built in new_voices.items():
        v_obj = stream.Voice(id=_denorm_voice(nv, aud_staff))
        for off, el in built:
            v_obj.insert(float(off), el)
        m.insert(0.0, v_obj)

    sub = rec.get("sub_cause") or ("type_d_overfull" if is_overfull else "type_d_underfull")
    tag = f"B15_F4:{sub}" if is_overfull else f"B15_F5:{sub}"
    return True, tag


def _apply_voice_split_json(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
    payload: dict,
    sub_cause: str,
    cfg: GraftConfig,
) -> tuple[bool, str | None]:
    if sub_cause in F5_CAUSES and not cfg.f5_type_c_voice_split:
        return False, None
    if sub_cause in F6_CAUSES and not cfg.f6_type_c_split_dur:
        return False, None
    if sub_cause not in F5_CAUSES and sub_cause not in F6_CAUSES:
        return False, None
    tgt = payload.get("rust_voices_target")
    if not tgt:
        return False, None
    prefix = "F5:" if sub_cause in F5_CAUSES else "F6:"
    return _build_reconstructed_voices(
        m, voices, el_dx, aud_staff, expected_q, tgt, prefix + sub_cause,
        lyric_dedup=cfg.b15_f2_lyric_dedup,
    )


def _ev_to_target(
    off: Fraction,
    dq: Fraction,
    el: note.GeneralNote,
    pitches: list[str],
) -> dict:
    is_rest = not pitches or bool(el.isRest)
    tmod = None
    try:
        tup = el.duration.tuplets
        if tup:
            tmod = (int(tup[0].numberNotesActual), int(tup[0].numberNotesNormal))
    except Exception:  # noqa: BLE001
        tmod = None
    return {
        "onset_q": str(off),
        "dur_q": str(dq),
        "is_rest": is_rest,
        "is_measure_rest": bool(is_rest and getattr(el, "fullMeasure", False) in (True, "always")),
        "pitches": list(pitches),
        "type": el.duration.type,
        "dots": el.duration.dots,
        "time_mod": tmod,
    }


def _apply_intrinsic_f5_pair(
    m: stream.Measure,
    orig_voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    two_voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
    *,
    tag: str,
) -> tuple[bool, str | None]:
    full_vids = [nv for nv, evs in two_voices.items() if _voice_exact_meter_ok(evs, expected_q)]
    if len(full_vids) != 1:
        return False, None
    v_full = full_vids[0]
    v_short = next(nv for nv in two_voices if nv != v_full)
    full_evs = two_voices[v_full]
    short_evs = two_voices[v_short]
    if not short_evs or short_evs[0][0] != 0:
        # Never fabricate notes before a late-entry partial voice (print_faithful pickup)
        return False, None
    short_sum = sum((_frac(el.quarterLength) for _, el in short_evs), Fraction(0))
    if short_sum >= expected_q:
        return False, None

    # Require default-x columns across all sounding notes
    cols = _sounding_columns(two_voices, el_dx, tol_tenths=16.0)
    if not cols or any(c["mean_x"] is None for c in cols):
        return False, None
    full_by_id = {id(el): off for off, el in full_evs}
    full_intervals = sorted(
        (off, off + _frac(el.quarterLength), el) for off, el in full_evs if not el.isRest
    )
    # Track occupied intervals in v_short so a sustained/beat-split span in v_short is preserved
    short_intervals = sorted(
        (off, off + _frac(el.quarterLength), el) for off, el in short_evs
    )

    def _short_covers(on: Fraction, dq: Fraction) -> note.GeneralNote | None:
        for s0, s1, sel in short_intervals:
            if s0 <= on and s1 >= on + dq:
                return sel
        return None

    def _short_covers_span(start_q: Fraction, end_q: Fraction) -> bool:
        cur = start_q
        for s0, s1, _ in short_intervals:
            if s1 <= cur:
                continue
            if s0 > cur + Fraction(1, 1024):
                return False
            if s1 > cur:
                cur = s1
            if cur >= end_q - Fraction(1, 1024):
                return True
        return cur >= end_q - Fraction(1, 1024)

    col_entries: list[tuple[str, Fraction, dict, note.GeneralNote | None, list[tuple[str, Fraction, note.GeneralNote]]]] = []
    for c in cols:
        f_items = [(sv, off, el) for sv, off, el, _ in c["items"] if sv == v_full]
        s_items = [(sv, off, el) for sv, off, el, _ in c["items"] if sv == v_short]
        if len(f_items) == 1:
            col_entries.append(("full", full_by_id[id(f_items[0][2])], c, f_items[0][2], s_items))
        elif len(f_items) == 0 and len(s_items) == 1 and len(c["items"]) == 1:
            # B17_1: v_short attacks a subdivision beat while v_full holds a longer sounding note across it
            _, s_off, s_el = s_items[0]
            s_dq = _frac(s_el.quarterLength)
            if not any(
                f0 < s_off < f1
                and s_off + s_dq <= f1 + Fraction(1, 1024)
                and _short_covers_span(f0, f1)
                for f0, f1, _ in full_intervals
            ):
                return False, None
            col_entries.append(("held_split", s_off, c, None, s_items))
        else:
            return False, None

    target_map: dict[str, list[dict]] = {v_full: [], v_short: []}
    for off, el in full_evs:
        if el.isRest:
            dq = _frac(el.quarterLength)
            target_map[v_full].append(_ev_to_target(off, dq, el, []))
            if _short_covers(off, dq) is None and not any(
                s0 < off + dq and s1 > off for s0, s1, _ in short_intervals
            ):
                target_map[v_short].append(_ev_to_target(off, dq, el, []))

    for s0, s1, sel in short_intervals:
        target_map[v_short].append(_ev_to_target(s0, s1 - s0, sel, _ev_pitch_list(sel)))

    for kind, on, _, f_el, s_items in col_entries:
        if kind == "held_split" or f_el is None:
            continue
        dq = _frac(f_el.quarterLength)
        f_ps = _ev_pitch_list(f_el)
        if not s_items:
            if _short_covers(on, dq) is not None or any(
                s0 < on + dq and s1 > on for s0, s1, _ in short_intervals
            ):
                # Sustained note in v_short already spans this column; keep v_full intact
                target_map[v_full].append(_ev_to_target(on, dq, f_el, f_ps))
                continue
            if len(f_ps) == 1:
                for nv in (v_full, v_short):
                    target_map[nv].append(_ev_to_target(on, dq, f_el, [f_ps[0]]))
            elif len(f_ps) == 2:
                ordered_ps = sorted(f_ps, key=lambda ps: _to_m21_pitch(ps).ps, reverse=True)
                v_hi, v_lo = sorted((v_full, v_short))
                p_map = {v_hi: ordered_ps[0], v_lo: ordered_ps[1]}
                for nv in (v_full, v_short):
                    target_map[nv].append(_ev_to_target(on, dq, f_el, [p_map[nv]]))
            else:
                return False, None
        elif len(s_items) == 1:
            _, _, s_el = s_items[0]
            s_dq = _frac(s_el.quarterLength)
            if s_dq < dq and not _short_covers_span(on, on + dq):
                return False, None
            # Sustained or beat-split events in v_short already added above; preserve v_full intact
            target_map[v_full].append(_ev_to_target(on, dq, f_el, f_ps))
        else:
            return False, None

    return _build_reconstructed_voices(m, orig_voices, el_dx, aud_staff, expected_q, target_map, tag)


def _apply_intrinsic_b17_2_disjoint(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    v_full: str,
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
) -> tuple[bool, str | None]:
    """B17_2: merge strictly pairwise time-disjoint non-full voices (< expected_q) and apply F5."""
    partials = [(nv, evs) for nv, evs in sorted(voices.items()) if nv != v_full]
    if len(partials) < 2 or any(not evs for _, evs in partials):
        return False, None

    all_items: list[tuple[Fraction, Fraction, note.GeneralNote]] = []
    for _, evs in partials:
        for off, el in evs:
            dq = _frac(el.quarterLength)
            if dq <= 0:
                return False, None
            all_items.append((off, off + dq, el))

    all_items.sort(key=lambda it: (it[0], it[1]))
    if all_items[0][0] != 0:
        return False, None
    for idx in range(len(all_items) - 1):
        if all_items[idx][1] > all_items[idx + 1][0] + Fraction(1, 1024):
            return False, None
        if all_items[idx][1] > all_items[idx + 1][0]:
            return False, None

    total_partial_q = sum((s1 - s0 for s0, s1, _ in all_items), Fraction(0))
    if total_partial_q >= expected_q:
        return False, None

    v_short_id = partials[0][0]
    two_voices = {
        v_full: voices[v_full],
        v_short_id: [(s0, el) for s0, _, el in all_items],
    }
    return _apply_intrinsic_f5_pair(
        m,
        voices,
        two_voices,
        el_dx,
        aud_staff,
        expected_q,
        tag="F5:intrinsic_b17_2_disjoint_merge",
    )


def _contiguous_span(
    evs: list[tuple[Fraction, note.GeneralNote]],
) -> tuple[Fraction, Fraction, list[tuple[Fraction, note.GeneralNote]]] | None:
    if not evs:
        return None
    ordered = sorted(evs, key=lambda it: (it[0], 0 if not it[1].isRest else 1))
    start = ordered[0][0]
    cur = start
    for off, el in ordered:
        is_mr = bool(el.isRest and getattr(el, "fullMeasure", False) in (True, "always"))
        if not _valid_duration_for_preflight(el.duration, is_measure_rest=is_mr):
            return None
        dq = _frac(el.quarterLength)
        if dq <= 0 or abs(off - cur) > Fraction(1, 1024):
            return None
        cur = off + dq
    return start, cur, ordered


def _apply_intrinsic_b17_3_handoff(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
) -> tuple[bool, str | None]:
    """B17_3: complete 2 overlapping partial voices [0..t] + [s..expected_q] across [0..s] and [t..expected_q]."""
    vids = sorted(voices)
    if len(vids) != 2:
        return False, None

    for v_a, v_b in ((vids[0], vids[1]), (vids[1], vids[0])):
        sp_a = _contiguous_span(voices[v_a])
        sp_b = _contiguous_span(voices[v_b])
        if sp_a is None or sp_b is None:
            continue
        start_a, t, ord_a = sp_a
        s, end_b, ord_b = sp_b
        if start_a != 0 or end_b != expected_q:
            continue
        if not (Fraction(0) < s <= t < expected_q):
            continue

        prefix_a = [
            (off, _frac(el.quarterLength), el)
            for off, el in ord_a
            if off + _frac(el.quarterLength) <= s
        ]
        mid_a = [
            (off, _frac(el.quarterLength), el)
            for off, el in ord_a
            if off >= s and off + _frac(el.quarterLength) <= t
        ]
        if not prefix_a or len(prefix_a) + len(mid_a) != len(ord_a):
            continue

        mid_b = [
            (off, _frac(el.quarterLength), el)
            for off, el in ord_b
            if off >= s and off + _frac(el.quarterLength) <= t
        ]
        suffix_b = [
            (off, _frac(el.quarterLength), el)
            for off, el in ord_b
            if off >= t
        ]
        if not suffix_b or len(mid_b) + len(suffix_b) != len(ord_b):
            continue

        v_hi, v_lo = sorted((v_a, v_b))
        target_map: dict[str, list[dict]] = {v_a: [], v_b: []}
        valid = True

        def _append_shared(off: Fraction, dq: Fraction, el: note.GeneralNote) -> bool:
            if el.isRest:
                for nv in (v_a, v_b):
                    target_map[nv].append(_ev_to_target(off, dq, el, []))
                return True
            ps = _ev_pitch_list(el)
            if len(ps) == 1:
                for nv in (v_a, v_b):
                    target_map[nv].append(_ev_to_target(off, dq, el, [ps[0]]))
                return True
            if len(ps) == 2:
                ordered_ps = sorted(ps, key=lambda ps_s: _to_m21_pitch(ps_s).ps, reverse=True)
                target_map[v_hi].append(_ev_to_target(off, dq, el, [ordered_ps[0]]))
                target_map[v_lo].append(_ev_to_target(off, dq, el, [ordered_ps[1]]))
                return True
            return False

        for off, dq, el in prefix_a:
            if not _append_shared(off, dq, el):
                valid = False
                break
        if not valid:
            continue

        for off, dq, el in mid_a:
            target_map[v_a].append(_ev_to_target(off, dq, el, [] if el.isRest else _ev_pitch_list(el)))
        for off, dq, el in mid_b:
            target_map[v_b].append(_ev_to_target(off, dq, el, [] if el.isRest else _ev_pitch_list(el)))

        for off, dq, el in suffix_b:
            if not _append_shared(off, dq, el):
                valid = False
                break
        if not valid:
            continue

        ok, tag = _build_reconstructed_voices(
            m, voices, el_dx, aud_staff, expected_q, target_map, "F5:intrinsic_b17_3_handoff"
        )
        if ok:
            return True, tag

    return False, None


def _apply_intrinsic_f5(
    m: stream.Measure,
    voices: dict[str, list[tuple[Fraction, note.GeneralNote]]],
    el_dx: dict[int, float | None],
    aud_staff: int,
    expected_q: Fraction,
) -> tuple[bool, str | None]:
    """Intrinsic F5 (+B17_1..B17_3): complete partial staff voices strictly within meter & pitch gates."""
    full_vids = [nv for nv, evs in voices.items() if _voice_exact_meter_ok(evs, expected_q)]
    if len(voices) == 2:
        if len(full_vids) == 1:
            return _apply_intrinsic_f5_pair(
                m, voices, voices, el_dx, aud_staff, expected_q, tag="F5:intrinsic_voice_split"
            )
        if len(full_vids) == 0:
            return _apply_intrinsic_b17_3_handoff(m, voices, el_dx, aud_staff, expected_q)
        return False, None
    if len(voices) >= 3 and len(full_vids) == 1:
        return _apply_intrinsic_b17_2_disjoint(
            m, voices, full_vids[0], el_dx, aud_staff, expected_q
        )
    return False, None


def apply_grafts(
    score: stream.Score,
    mxl_path: Path | str,
    config: GraftConfig | None = None,
    *,
    inherit_ts: str | None = None,
) -> list[ReviewFlag]:
    """Apply enabled Batch 14 grafts (F1..F6) onto ``score`` before ``split_score``.

    Returns page-level ``omr_graft_summary`` ``ReviewFlag``s (``measure=None``).
    Bars that fail any strict gate are left untouched.
    """
    cfg = resolve_graft_config(config)
    if not cfg.any_graft_enabled():
        return []

    path = Path(mxl_path)
    parts = list(score.parts)
    if not parts:
        return []

    part_staff_map, dx_map = _xml_meta(path)
    rev_part_staff = {idx: key for key, idx in part_staff_map.items()}
    if not rev_part_staff:
        for idx in range(len(parts)):
            rev_part_staff[idx] = (idx + 1, 1)

    records_by_key = _load_graft_records(path, cfg)
    meters = _declared_meters(parts, inherit_ts)
    per_part_ms = [list(p.getElementsByClass(stream.Measure)) for p in parts]

    repaired_entries: list[str] = []
    rejected_count = 0

    for pidx, ms in enumerate(per_part_ms):
        aud_part, aud_staff = rev_part_staff.get(pidx, (pidx + 1, 1))
        for mi, m in enumerate(ms):
            mxl_idx = mi + 1
            if getattr(m.editorial, "satb_missing_source", False):
                continue
            if _is_genuine_pickup(m, mi, inherit_ts):
                continue
            exp_q = meters[mi] if mi < len(meters) else None
            if exp_q is None or exp_q <= 0:
                continue

            voices, el_dx, _ = _extract_measure_voices(m, aud_staff, dx_map, aud_part, mxl_idx)
            if not voices:
                continue

            recs = records_by_key.get((mxl_idx, aud_part, aud_staff), [])
            applied = False
            attempted_json = False
            tag: str | None = None

            for rec in recs:
                gp = rec.get("graft_payload") or {}
                kind = gp.get("kind")
                sub = rec.get("sub_cause") or ""
                is_type_d_active = False
                if kind == "duration_graft":
                    attempted_json = attempted_json or cfg.f1_type_a_dot or cfg.f2_type_a_beam_meter
                    applied, tag = _apply_duration_graft_json(m, voices, aud_staff, exp_q, gp, sub, cfg)
                elif kind == "missing_rest_splice":
                    attempted_json = attempted_json or cfg.f3_type_b_shared_rest or cfg.f4_type_b_unprinted_rest
                    applied, tag = _apply_rest_splice_json(m, voices, aud_staff, exp_q, gp, cfg)
                elif kind == "shared_voice_split":
                    attempted_json = attempted_json or cfg.f5_type_c_voice_split or cfg.f6_type_c_split_dur
                    applied, tag = _apply_voice_split_json(m, voices, el_dx, aud_staff, exp_q, gp, sub, cfg)
                elif kind == "type_d_geometry_graft":
                    eff_q = _frac(rec["split_half_expected_q"]) if rec.get("split_half_expected_q") else exp_q
                    v_lens = [sum((_frac(el.quarterLength) for _, el in evs), Fraction(0)) for evs in voices.values()]
                    v_ends = [off + _frac(el.quarterLength) for evs in voices.values() for off, el in evs]
                    over = max(v_lens, default=Fraction(0)) > eff_q or max(v_ends, default=Fraction(0)) > eff_q
                    under = (not over) and min(v_lens, default=Fraction(0)) < eff_q
                    is_type_d_active = (over and cfg.b15_f4_type_d_overfull) or (under and cfg.b15_f5_type_d_underfull)
                    if is_type_d_active:
                        attempted_json = True
                        applied, tag = _apply_type_d_geometry_graft(m, voices, el_dx, aud_staff, exp_q, rec, cfg)
                if applied:
                    break
                if kind in ("duration_graft", "missing_rest_splice", "shared_voice_split", "type_d_geometry_graft"):
                    if (
                        (kind == "duration_graft" and (cfg.f1_type_a_dot or cfg.f2_type_a_beam_meter))
                        or (kind == "missing_rest_splice" and (cfg.f3_type_b_shared_rest or cfg.f4_type_b_unprinted_rest))
                        or (kind == "shared_voice_split" and (cfg.f5_type_c_voice_split or cfg.f6_type_c_split_dur))
                        or (kind == "type_d_geometry_graft" and is_type_d_active)
                    ):
                        rejected_count += 1

            # Intrinsic MXL fallbacks ONLY when no JSON repair for this bar was attempted/rejected
            if not applied and not attempted_json:
                if cfg.f2_type_a_beam_meter:
                    applied, tag = _apply_intrinsic_f2_measure_rest(m, voices, aud_staff, exp_q)
                if not applied and cfg.f3_type_b_shared_rest:
                    applied, tag = _apply_intrinsic_f3(m, voices, el_dx, aud_staff, exp_q)
                if not applied and cfg.f5_type_c_voice_split:
                    applied, tag = _apply_intrinsic_f5(m, voices, el_dx, aud_staff, exp_q)

            if applied and tag:
                m.paddingLeft = 0.0
                m.paddingRight = 0.0
                m_num = str(getattr(m, "number", None) or mxl_idx)
                repaired_entries.append(f"m{m_num}(st{pidx}:{tag})")

    if not repaired_entries and not rejected_count:
        return []
    detail = (
        f"Applied {len(repaired_entries)} OMR graft(s) (rejected {rejected_count}): "
        + ", ".join(repaired_entries)
    )
    return [ReviewFlag(measure=None, part_index=-1, offset=None, reason="omr_graft_summary", detail=detail)]
