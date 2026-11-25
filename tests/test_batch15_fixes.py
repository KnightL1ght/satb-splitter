"""Batch 15: Tests for B15_F1..B15_F7 (config, lyric dedup, meter clamp, Type D gates, ties, 3-voice tie-split)."""
from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from music21 import chord, expressions, layout, meter, note, stream, tie

from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.export.writer import preflight_musicxml
from satb_splitter.model.config import GraftConfig, SplitConfig, resolve_graft_config
from satb_splitter.split.engine import sanitize_ties, split_file, split_score
from satb_splitter.split.graft import apply_grafts

ROLES = ("Soprano", "Alto", "Tenor", "Bass")
FIX14 = Path(__file__).parent / "fixtures" / "batch14"


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _bars(res, role: str) -> list[stream.Measure]:
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def _sig(m: stream.Measure) -> list[tuple[str, float, float]]:
    return [
        ("R" if e.isRest else "+".join(p.nameWithOctave for p in e.pitches), float(e.offset), float(e.quarterLength))
        for e in m.recurse().notesAndRests
        if not e.duration.isGrace
    ]


def _cfg_b15(**overrides) -> SplitConfig:
    kw = dict(
        f1_type_a_dot=True,
        f2_type_a_beam_meter=True,
        f3_type_b_shared_rest=True,
        f4_type_b_unprinted_rest=False,
        f5_type_c_voice_split=True,
        f6_type_c_split_dur=True,
        f7_pdf_multirest_mr1=False,
        b15_f2_lyric_dedup=False,
        b15_f3_meter_clamp=False,
        b15_f4_type_d_overfull=False,
        b15_f5_type_d_underfull=False,
        b15_f6_tie_continuity=False,
        b15_f7_chord_tie_split=False,
    )
    kw.update(overrides)
    return SplitConfig(graft=GraftConfig(**kw))


def _synth_xml(
    path: Path,
    staves: list[list[list[dict]]],
    *,
    beats: int = 4,
    beat_type: int = 4,
    sys_breaks: set[int] | None = None,
    bar_ts: dict[int, tuple[int, int]] | None = None,
) -> Path:
    root = ET.Element("score-partwise", version="4.0")
    pl = ET.SubElement(root, "part-list")
    for k in range(len(staves)):
        sp = ET.SubElement(pl, "score-part", id=f"P{k + 1}")
        ET.SubElement(sp, "part-name").text = "Voice"
    div = 16
    for pi, bars in enumerate(staves):
        p = ET.SubElement(root, "part", id=f"P{pi + 1}")
        for mi, bar_voices in enumerate(bars):
            m = ET.SubElement(p, "measure", number=str(mi + 1))
            if sys_breaks and (mi + 1) in sys_breaks:
                ET.SubElement(m, "print", **{"new-system": "yes"})
            if mi == 0 or (bar_ts and (mi + 1) in bar_ts):
                a = ET.SubElement(m, "attributes")
                if mi == 0:
                    ET.SubElement(a, "divisions").text = str(div)
                tb, tt = bar_ts[mi + 1] if (bar_ts and (mi + 1) in bar_ts) else (beats, beat_type)
                if mi == 0 or (bar_ts and (mi + 1) in bar_ts):
                    t = ET.SubElement(a, "time")
                    ET.SubElement(t, "beats").text = str(tb)
                    ET.SubElement(t, "beat-type").text = str(tt)
                if mi == 0:
                    c = ET.SubElement(a, "clef")
                    ET.SubElement(c, "sign").text = "G" if pi == 0 else "F"
                    ET.SubElement(c, "line").text = "2" if pi == 0 else "4"
            for vi, evs in enumerate(bar_voices):
                if vi > 0:
                    bk = ET.SubElement(m, "backup")
                    prev_tot = sum(int(round(e["q"] * div)) for e in bar_voices[vi - 1])
                    ET.SubElement(bk, "duration").text = str(prev_tot)
                for ev in evs:
                    if ev.get("fwd"):
                        fw = ET.SubElement(m, "forward")
                        ET.SubElement(fw, "duration").text = str(int(round(ev["q"] * div)))
                        continue
                    pitches = ev.get("pitches")
                    if pitches is None:
                        pitches = [ev["pitch"]] if ev.get("pitch") else []
                    for ci, pstr in enumerate(pitches or [None]):
                        n_attrib = {}
                        if ev.get("x") is not None:
                            n_attrib["default-x"] = str(ev["x"])
                        n = ET.SubElement(m, "note", n_attrib)
                        if ci > 0:
                            ET.SubElement(n, "chord")
                        if pstr is None:
                            ET.SubElement(n, "rest")
                        else:
                            pe = ET.SubElement(n, "pitch")
                            ET.SubElement(pe, "step").text = pstr[0]
                            if len(pstr) >= 3 and pstr[1] == "b":
                                ET.SubElement(pe, "alter").text = "-1"
                            elif len(pstr) >= 3 and pstr[1] == "#":
                                ET.SubElement(pe, "alter").text = "1"
                            ET.SubElement(pe, "octave").text = pstr[-1]
                        ET.SubElement(n, "duration").text = str(int(round(ev["q"] * div)))
                        ET.SubElement(n, "voice").text = str(ev.get("voice", vi + 1))
                        if ev.get("type"):
                            ET.SubElement(n, "type").text = ev["type"]
                        for _ in range(ev.get("dots", 0)):
                            ET.SubElement(n, "dot")
                        if ev.get("stem"):
                            ET.SubElement(n, "stem").text = ev["stem"]
                        if ev.get("fermata") and ci == 0:
                            nots = n.find("notations") or ET.SubElement(n, "notations")
                            ET.SubElement(nots, "fermata")
                        if ev.get("tie") and ci == 0:
                            ET.SubElement(n, "tie", type=ev["tie"])
                            nots = n.find("notations") or ET.SubElement(n, "notations")
                            ET.SubElement(nots, "tied", type=ev["tie"])
                        for v_no, l_txt in ev.get("lyrics", []) if ci == 0 else []:
                            ly = ET.SubElement(n, "lyric", number=str(v_no))
                            ET.SubElement(ly, "syllabic").text = "single"
                            ET.SubElement(ly, "text").text = l_txt
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


# ------------------------------------------- B15 config & env override tests
def test_b15_config_env_and_b14_compat(monkeypatch):
    # Committed Batch 15 X* = (1, 1, 0, 1, 1, 1, 1) defaults on GraftConfig()
    committed = GraftConfig()
    assert (
        committed.f4_type_b_unprinted_rest,
        committed.b15_f2_lyric_dedup,
        committed.b15_f3_meter_clamp,
        committed.b15_f4_type_d_overfull,
        committed.b15_f5_type_d_underfull,
        committed.b15_f6_tie_continuity,
        committed.b15_f7_chord_tie_split,
    ) == (True, True, False, True, True, True, True)

    base = GraftConfig(
        f4_type_b_unprinted_rest=False,
        b15_f2_lyric_dedup=False,
        b15_f3_meter_clamp=False,
        b15_f4_type_d_overfull=False,
        b15_f5_type_d_underfull=False,
        b15_f6_tie_continuity=False,
        b15_f7_chord_tie_split=False,
    )
    monkeypatch.setenv("SATB_GRAFT_FLAGS", "1110110")
    monkeypatch.setenv("SATB_B15_FLAGS", "1010101")
    r = resolve_graft_config(base)
    assert (r.f1_type_a_dot, r.f3_type_b_shared_rest, r.f5_type_c_voice_split, r.f7_pdf_multirest_mr1) == (True, True, True, False)
    assert (
        r.f4_type_b_unprinted_rest, r.b15_f2_lyric_dedup, r.b15_f3_meter_clamp,
        r.b15_f4_type_d_overfull, r.b15_f5_type_d_underfull, r.b15_f6_tie_continuity, r.b15_f7_chord_tie_split,
    ) == (True, False, True, False, True, False, True)

    monkeypatch.setenv("SATB_B15_FLAGS", "B15_F2,B15_F6")
    r2 = resolve_graft_config(base)
    assert r2.b15_f2_lyric_dedup and r2.b15_f6_tie_continuity
    assert not r2.b15_f3_meter_clamp and not r2.f4_type_b_unprinted_rest

    monkeypatch.delenv("SATB_B15_FLAGS", raising=False)
    monkeypatch.setenv("SATB_GRAFT_FLAGS", "1110110,b15_f7_chord_tie_split=1")
    r3 = resolve_graft_config(base)
    assert r3.f1_type_a_dot and not r3.f4_type_b_unprinted_rest
    assert r3.b15_f7_chord_tie_split and not r3.b15_f2_lyric_dedup


# ------------------------------------------- B15_F1 unprinted rest test
def test_b15_f1_unprinted_rest_repairs_100_p3(tmp_path):
    # Copy fixture page_03 from batch14
    import shutil
    for fp in FIX14.glob("b14_100_page_03.*"):
        shutil.copy(fp, tmp_path / fp.name)
    mxl = tmp_path / "b14_100_page_03.mxl"
    res_off = split_file(mxl, _cfg_b15(f4_type_b_unprinted_rest=False), auto_layout=True, inherit_ts="4/4")
    assert any(f.reason == "underfull_measure" and f.measure in (20, 24) for f in res_off.flags)

    res_on = split_file(mxl, _cfg_b15(f4_type_b_unprinted_rest=True), auto_layout=True, inherit_ts="4/4")
    assert not [f for f in res_on.flags if f.reason == "underfull_measure" and f.measure in (20, 24)]


# ------------------------------------------- B15_F2 lyric dedup & multiverse
def test_b15_f2_lyric_dedup_normal_b50_and_multiverse_preserve(tmp_path):
    # v2 C5@'ce' + v3 C5@'ar' at dx=183 + genuine multiverse (1,'A'),(2,'B') at dx=18
    p = _synth_xml(tmp_path / "b50.musicxml", [
        [[
            [{"pitch": "C5", "q": 2.0, "type": "half", "x": 18},
             {"pitch": None, "q": 0.5, "type": "eighth", "x": 100},
             {"pitch": "Bb4", "q": 1.0, "type": "quarter", "x": 133}],
            [{"pitch": "F4", "q": 2.0, "type": "half", "x": 18, "lyrics": [(1, "A"), (2, "B")]},
             {"fwd": True, "q": 0.5},
             {"pitch": "Bb4", "q": 0.5, "type": "eighth", "x": 133, "lyrics": [(1, "ori")]},
             {"pitch": "C5", "q": 0.75, "type": "eighth", "dots": 1, "x": 183, "lyrics": [(1, "ce")]},
             {"pitch": "D5", "q": 0.25, "type": "16th", "x": 238, "lyrics": [(1, "ve")]}],
            [{"fwd": True, "q": 3.0},
             {"pitch": "C5", "q": 0.75, "type": "eighth", "dots": 1, "x": 183, "lyrics": [(1, "ar")]},
             {"pitch": "D5", "q": 0.25, "type": "16th", "x": 238}],
        ]],
        [[[{"pitch": "C3", "q": 4.0, "type": "whole", "x": 18}]]],
    ])
    (tmp_path / "b50.graft.json").write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "b50.musicxml", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "voice_split_with_duration_graft",
        "graft_payload": {
            "kind": "shared_voice_split",
            "rust_voices_target": {
                "1": [
                    {"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["C5"], "type": "half"},
                    {"onset_q": "2", "dur_q": "1/2", "is_rest": True, "pitches": [], "type": "eighth"},
                    {"onset_q": "5/2", "dur_q": "1/2", "is_rest": False, "pitches": ["Bb4"], "type": "eighth"},
                    {"onset_q": "3", "dur_q": "3/4", "is_rest": False, "pitches": ["C5"], "type": "eighth", "dots": 1},
                    {"onset_q": "15/4", "dur_q": "1/4", "is_rest": False, "pitches": ["D5"], "type": "16th"},
                ],
                "2": [
                    {"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["F4"], "type": "half"},
                    {"onset_q": "2", "dur_q": "1/2", "is_rest": True, "pitches": [], "type": "eighth"},
                    {"onset_q": "5/2", "dur_q": "1/2", "is_rest": False, "pitches": ["Bb4"], "type": "eighth"},
                    {"onset_q": "3", "dur_q": "3/4", "is_rest": False, "pitches": ["C5"], "type": "eighth", "dots": 1},
                    {"onset_q": "15/4", "dur_q": "1/4", "is_rest": False, "pitches": ["D5"], "type": "16th"},
                ],
            },
        },
    }]}))

    res_off = split_file(p, _cfg_b15(b15_f2_lyric_dedup=False), auto_layout=True)
    assert any(f.reason == "underfull_measure" for f in res_off.flags)

    res_on = split_file(p, _cfg_b15(b15_f2_lyric_dedup=True), auto_layout=True)
    assert not [f for f in res_on.flags if f.reason == "underfull_measure"]
    all_ly = {
        (ly.number or 1, ly.text)
        for r in ("Soprano", "Alto")
        for n in _bars(res_on, r)[0].recurse().notes
        for ly in n.lyrics
    }
    assert {(1, "A"), (2, "B"), (1, "ori"), (1, "ce"), (1, "ar"), (1, "ve")} <= all_ly


# ------------------------------------------- B15_F3 meter clamp + safety
def test_b15_f3_cross_staff_meter_clamp_and_overflow_guard(tmp_path):
    # Case A: bar 2 (not own_ts): st1=4.0q fits 4/4, st2=6.0q (4 dotted quarters -> compresses cleanly by 4/6 = 2/3 into quarters)
    p = _synth_xml(tmp_path / "p14m2.musicxml", [
        [[[{"pitch": "C5", "q": 4.0, "type": "whole"}]],
         [[{"pitch": "C5", "q": 4.0, "type": "whole"}]]],
        [[[{"pitch": "C3", "q": 4.0, "type": "whole"}]],
         [[{"pitch": "G3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "A3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "B3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "C4", "q": 1.5, "type": "quarter", "dots": 1}]]],
    ], beats=4, beat_type=4)
    res_off = split_file(p, _cfg_b15(b15_f3_meter_clamp=False), auto_layout=True)
    assert float(_bars(res_off, "Soprano")[1].highestTime) == 6.0

    res_on = split_file(p, _cfg_b15(b15_f3_meter_clamp=True), auto_layout=True)
    for role in ROLES:
        preflight_musicxml(res_on.scores[role], flags=res_on.flags, role=role)
        assert float(_bars(res_on, role)[1].highestTime) == 4.0
    assert _sig(_bars(res_on, "Tenor")[1]) == [("G3", 0.0, 1.0), ("A3", 1.0, 1.0), ("B3", 2.0, 1.0), ("C4", 3.0, 1.0)]
    assert not [f for f in res_on.flags if f.reason == "overflow_dropped"]

    # Negative guard: explicit own_ts on bar MUST NOT clamp
    p_own = _synth_xml(tmp_path / "own_ts.musicxml", [
        [[[{"pitch": "C5", "q": 4.0, "type": "whole"}]]],
        [[[{"pitch": "G3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "A3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "B3", "q": 1.5, "type": "quarter", "dots": 1},
           {"pitch": "C4", "q": 1.5, "type": "quarter", "dots": 1}]]],
    ], beats=4, beat_type=4)
    res_own = split_file(p_own, _cfg_b15(b15_f3_meter_clamp=True), auto_layout=True)
    assert float(_bars(res_own, "Soprano")[0].highestTime) == 6.0

    # Case B: uncompressible 4.5q (scale=8/9 non-plain) MUST NOT drop notes!
    p_bad = _synth_xml(tmp_path / "ovf45.musicxml", [
        [[[{"pitch": "C5", "q": 4.0, "type": "whole"}]]],
        [[[{"pitch": "B2", "q": 3.0, "type": "half", "dots": 1},
           {"pitch": "G3", "q": 0.5, "type": "eighth"},
           {"pitch": "F#3", "q": 0.5, "type": "eighth"},
           {"pitch": "E3", "q": 0.5, "type": "eighth"}]]],
    ], beats=4, beat_type=4)
    res_bad = split_file(p_bad, _cfg_b15(b15_f3_meter_clamp=True), auto_layout=True)
    assert not [f for f in res_bad.flags if f.reason == "overflow_dropped"]
    assert any(p.nameWithOctave == "E3" for p in _bars(res_bad, "Tenor")[0].recurse().notes[3].pitches)


# ------------------------------------------- B15_F4 / B15_F5 Type D & split half
def test_b15_f4_f5_type_d_gates_and_split_half(tmp_path):
    # 1. Negative gates: print_faithful, anchor < 0.70, head_delta > 4, aud_heads == 0, lyric loss
    sc = stream.Score()
    pt = stream.Part()
    m = stream.Measure(number=1)
    m.append(meter.TimeSignature("4/4"))
    n1 = note.Note("C5", quarterLength=2.0); n1.lyric = "keep"
    n2 = note.Note("D5", quarterLength=1.0)
    m.append([n1, n2])
    pt.append(m); sc.append(pt)

    good_tgt = {
        "1": [
            {"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["C#5"], "type": "half"},
            {"onset_q": "2", "dur_q": "2", "is_rest": False, "pitches": ["D5"], "type": "half"},
        ]
    }
    bad_cases = [
        {"rust_strict_label": "print_faithful", "tgt": good_tgt},  # print_faithful rejected
        {"rust_strict_label": "exact_all_voices", "tgt": {  # anchor ratio 0.0 < 0.70 rejected
            "1": [{"onset_q": "0", "dur_q": "4", "is_rest": False, "pitches": ["G4"], "type": "whole"}]
        }},
        {"rust_strict_label": "exact_all_voices", "tgt": {  # head delta = 6 > 4 rejected
            "1": [{"onset_q": float(k) / 2, "dur_q": "1/2", "is_rest": False, "pitches": ["C5"], "type": "eighth"} for k in range(8)]
        }},
        {"rust_strict_label": "exact_all_voices", "tgt": {  # bad meter sum 3.0 != 4.0 rejected
            "1": [{"onset_q": "0", "dur_q": "3", "is_rest": False, "pitches": ["C5"], "type": "half", "dots": 1}]
        }},
    ]
    for i, bc in enumerate(bad_cases):
        gj = tmp_path / f"neg_{i}.json"
        gj.write_text(json.dumps({"records": [{
            "bar": {"mxl_file": f"neg_{i}.mxl", "mxl_idx": 1},
            "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
            "sub_cause": "dot_or_accidental_cluster",
            "rust_strict_label": bc["rust_strict_label"],
            "graft_payload": {"kind": "type_d_geometry_graft", "rust_voices_target": bc["tgt"]},
        }]}))
        sc_c = stream.Score()
        pt_c = stream.Part()
        mc = stream.Measure(number=1); mc.append(meter.TimeSignature("4/4"))
        nc1 = note.Note("C5", quarterLength=2.0); nc1.lyric = "keep"
        nc2 = note.Note("D5", quarterLength=1.0)
        mc.append([nc1, nc2]); pt_c.append(mc); sc_c.append(pt_c)
        fl = apply_grafts(sc_c, tmp_path / f"neg_{i}.mxl", GraftConfig(b15_f5_type_d_underfull=True, graft_json_path=gj))
        assert fl and "rejected 1" in fl[0].detail

    # Empty aud_heads == 0 rejected
    sc_empty = stream.Score()
    pe = stream.Part(); me = stream.Measure(number=1); me.append(meter.TimeSignature("4/4"))
    me.append(note.Rest(quarterLength=1.0)); pe.append(me); sc_empty.append(pe)
    gj_e = tmp_path / "empty.json"
    gj_e.write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "empty.mxl", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "rust_strict_label": "exact_all_voices",
        "graft_payload": {"kind": "type_d_geometry_graft", "rust_voices_target": good_tgt},
    }]}))
    fl_e = apply_grafts(sc_empty, tmp_path / "empty.mxl", GraftConfig(b15_f5_type_d_underfull=True, graft_json_path=gj_e))
    assert fl_e and "rejected 1" in fl_e[0].detail

    # Valid B15_F5 accidental+dot repair preserves lyric
    gj_ok = tmp_path / "ok.json"
    gj_ok.write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "ok.mxl", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "dot_or_accidental_cluster",
        "rust_strict_label": "exact_all_voices",
        "graft_payload": {"kind": "type_d_geometry_graft", "rust_voices_target": good_tgt},
    }]}))
    fl_ok = apply_grafts(sc, tmp_path / "ok.mxl", GraftConfig(b15_f5_type_d_underfull=True, graft_json_path=gj_ok))
    assert fl_ok and "B15_F5:dot_or_accidental_cluster" in fl_ok[0].detail
    notes_out = list(m.recurse().notes)
    assert [n.pitch.nameWithOctave for n in notes_out] == ["C#5", "D5"]
    assert notes_out[0].lyric == "keep"

    # 2. B15_F4 overfull + split_half cross-page join restoration
    p1_dir = tmp_path / "page_01"; p2_dir = tmp_path / "page_02"
    p1_dir.mkdir(); p2_dir.mkdir()
    p1 = _synth_xml(p1_dir / "page_01.musicxml", [
        [[[{"pitch": "A4", "q": 4.0, "type": "whole"}]], [[{"pitch": "A4", "q": 1.75, "type": "quarter", "dots": 1}]]],
        [[[{"pitch": "A3", "q": 4.0, "type": "whole"}]], [[{"pitch": "A3", "q": 1.75, "type": "quarter", "dots": 1}]]],
    ])
    p2 = _synth_xml(p2_dir / "page_02.musicxml", [
        [[[{"pitch": "C5", "q": 2.0, "type": "half"}]], [[{"pitch": "C5", "q": 4.0, "type": "whole"}]]],
        [[[{"pitch": "C4", "q": 2.0, "type": "half"}]], [[{"pitch": "C4", "q": 4.0, "type": "whole"}]]],
    ])
    (p1_dir / "page_01.graft.json").write_text(json.dumps({"split_half_records": [
        {
            "bar": {"mxl_file": "page_01.musicxml", "mxl_idx": 2},
            "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
            "is_split_half": True, "split_half_expected_q": "2",
            "rust_strict_label": "exact_all_voices",
            "graft_payload": {"kind": "type_d_geometry_graft", "rust_voices_target": {
                "1": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["A4"], "type": "half"}]
            }},
        },
        {
            "bar": {"mxl_file": "page_01.musicxml", "mxl_idx": 2},
            "staff": {"active_staff_idx": 2, "aud_part": 2, "aud_staff": 1},
            "is_split_half": True, "split_half_expected_q": "2",
            "rust_strict_label": "exact_all_voices",
            "graft_payload": {"kind": "type_d_geometry_graft", "rust_voices_target": {
                "1": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["A3"], "type": "half"}]
            }},
        },
    ]}))
    _, _, r_un = split_and_merge_mxl_files([p1, p2], tmp_path / "out_off", "sp", auto_layout=True, config=_cfg_b15(b15_f5_type_d_underfull=False))
    assert len(_bars(r_un, "Soprano")) == 4  # unjoined
    _, _, r_jn = split_and_merge_mxl_files([p1, p2], tmp_path / "out_on", "sp", auto_layout=True, config=_cfg_b15(b15_f5_type_d_underfull=True))
    assert len(_bars(r_jn, "Soprano")) == 3  # joined 2+2=4/4!


# ------------------------------------------- B15_F6 tie continuity & lyric guard
def test_b15_f6_tie_continuity_split_bar_page_break_and_lyric_guard(tmp_path):
    # 1. Unit test on sanitize_ties: start+None, start+start, None+stop, lyric guard, diff-pitch strip
    part = stream.Part()
    m1 = stream.Measure(number=1); m1.append(meter.TimeSignature("4/4"))
    a = note.Note("G4", quarterLength=2.0); a.tie = tie.Tie("start")
    b = note.Note("G4", quarterLength=2.0)  # missing stop, no lyric
    m1.append([a, b])
    m2 = stream.Measure(number=2)
    c = note.Note("A4", quarterLength=2.0); c.tie = tie.Tie("start")
    d = note.Note("A4", quarterLength=2.0); d.tie = tie.Tie("start")  # bad_start, no lyric
    m2.append([c, d])
    m3 = stream.Measure(number=3)
    e = note.Note("B4", quarterLength=2.0)  # None+stop, no lyric
    f = note.Note("B4", quarterLength=2.0); f.tie = tie.Tie("stop")
    m3.append([e, f])
    m4 = stream.Measure(number=4)
    g = note.Note("C5", quarterLength=2.0); g.tie = tie.Tie("start")
    h = note.Note("C5", quarterLength=2.0); h.lyric = "i"  # lyric re-articulation! MUST NOT tie!
    m4.append([g, h])
    m5 = stream.Measure(number=5)
    k = note.Note("D5", quarterLength=2.0); k.tie = tie.Tie("start")
    r = note.Rest(quarterLength=2.0)  # rest -> MUST strip!
    m5.append([k, r])
    part.append([m1, m2, m3, m4, m5])

    where = []
    fixed = sanitize_ties(part, where, repair_continuity=True)
    assert (a.tie.type, b.tie.type) == ("start", "stop")
    assert (c.tie.type, d.tie.type) == ("start", "stop")
    assert (e.tie.type, f.tie.type) == ("start", "stop")
    assert g.tie is None and h.tie is None
    assert k.tie is None
    assert fixed == 2  # only g and k stripped!

    # 2. Cross-page tie preservation in split_and_merge_mxl_files
    p1 = _synth_xml(tmp_path / "tp1.musicxml", [
        [[[{"pitch": "E5", "q": 4.0, "type": "whole", "tie": "start"}]]],
        [[[{"pitch": "E3", "q": 4.0, "type": "whole"}]]],
    ])
    p2 = _synth_xml(tmp_path / "tp2.musicxml", [
        [[[{"pitch": "E5", "q": 4.0, "type": "whole", "tie": "stop"}]]],
        [[[{"pitch": "E3", "q": 4.0, "type": "whole"}]]],
    ])
    _, _, r_off = split_and_merge_mxl_files([p1, p2], tmp_path / "t_off", "t", auto_layout=True, config=_cfg_b15(b15_f6_tie_continuity=False))
    assert _bars(r_off, "Soprano")[0].notes[0].tie is None
    _, _, r_on = split_and_merge_mxl_files([p1, p2], tmp_path / "t_on", "t", auto_layout=True, config=_cfg_b15(b15_f6_tie_continuity=True))
    assert _bars(r_on, "Soprano")[0].notes[0].tie.type == "start"
    assert _bars(r_on, "Soprano")[1].notes[0].tie.type == "stop"


# ------------------------------------------- B15_F7 three-voice chord tie split
def test_b15_f7_three_voice_chord_tie_split_and_sliver_guard(tmp_path):
    # Easy b7 pattern: D4/D3 0..1.5 (with lyric+fermata on D4) + A3 0.5..1.5
    p = _synth_xml(tmp_path / "eb7.musicxml", [
        [[[{"pitch": "G4", "q": 3.0, "type": "half", "dots": 1}]]],
        [[
            [{"pitch": "D4", "q": 1.5, "type": "quarter", "dots": 1, "stem": "up", "lyrics": [(1, "hold")], "fermata": True},
             {"pitch": "C#4", "q": 1.5, "type": "quarter", "dots": 1, "stem": "up"}],
            [{"pitch": "D3", "q": 1.5, "type": "quarter", "dots": 1, "stem": "down"},
             {"pitch": "A3", "q": 1.5, "type": "quarter", "dots": 1, "stem": "down"}],  # at 1.5, same voice
        ]],
    ], beats=3, beat_type=4)
    # Build exact 3-voice overlap: D4+D3 0..1.5 + rest(0..0.5)+A3(0.5..1.5)+rest(1.5..3.0)
    p2 = _synth_xml(tmp_path / "eb7_exact.musicxml", [
        [[[{"pitch": "G4", "q": 3.0, "type": "half", "dots": 1}]]],
        [[
            [{"pitch": "D4", "q": 1.5, "type": "quarter", "dots": 1, "stem": "up", "lyrics": [(1, "hold")], "fermata": True},
             {"pitch": None, "q": 1.5, "type": "quarter", "dots": 1}],
            [{"pitch": "D3", "q": 1.5, "type": "quarter", "dots": 1, "stem": "down"},
             {"pitch": None, "q": 1.5, "type": "quarter", "dots": 1}],
            [{"pitch": None, "q": 0.5, "type": "eighth"},
             {"pitch": "A3", "q": 1.0, "type": "quarter"},
             {"pitch": None, "q": 1.5, "type": "quarter", "dots": 1}],
        ]],
    ], beats=3, beat_type=4)
    res_off = split_file(p2, _cfg_b15(b15_f7_chord_tie_split=False), auto_layout=True)
    assert any(f.reason == "dropped_pitch" for f in res_off.flags)

    res_on = split_file(p2, _cfg_b15(b15_f7_chord_tie_split=True, b15_f6_tie_continuity=True), auto_layout=True)
    assert not [f for f in res_on.flags if f.reason == "dropped_pitch"]
    ten = _bars(res_on, "Tenor")[0]
    preflight_musicxml(res_on.scores["Tenor"], flags=res_on.flags, role="Tenor")
    assert _sig(ten) == [("D4", 0.0, 0.5), ("A3+D4", 0.5, 1.0), ("R", 1.5, 1.5)]
    el0, el1 = list(ten.recurse().notes)[:2]
    assert el0.tie.type == "start"
    assert [ly.text for ly in el0.lyrics] == ["hold"]
    assert any(isinstance(x, expressions.Fermata) for x in el0.expressions)
    assert el1.lyrics == [] and el1.expressions == []
    assert el1.getTie(el1.pitches[1]).type == "stop" and el1.getTie(el1.pitches[0]) is None

    # 3-segment split (D4 0..2.0 + A3 0.5..1.5 -> 0.5 + 1.0 + 0.5)
    p3 = _synth_xml(tmp_path / "three_seg.musicxml", [
        [[[{"pitch": "G4", "q": 3.0, "type": "half", "dots": 1}]]],
        [[
            [{"pitch": "D4", "q": 2.0, "type": "half", "stem": "up", "lyrics": [(1, "la")]},
             {"pitch": None, "q": 1.0, "type": "quarter"}],
            [{"pitch": "D3", "q": 2.0, "type": "half", "stem": "down"},
             {"pitch": None, "q": 1.0, "type": "quarter"}],
            [{"pitch": None, "q": 0.5, "type": "eighth"},
             {"pitch": "A3", "q": 1.0, "type": "quarter"},
             {"pitch": None, "q": 1.5, "type": "quarter", "dots": 1}],
        ]],
    ], beats=3, beat_type=4)
    res3 = split_file(p3, _cfg_b15(b15_f7_chord_tie_split=True, b15_f6_tie_continuity=True), auto_layout=True)
    assert not [f for f in res3.flags if f.reason == "dropped_pitch"]
    assert _sig(_bars(res3, "Tenor")[0]) == [("D4", 0.0, 0.5), ("A3+D4", 0.5, 1.0), ("D4", 1.5, 0.5), ("R", 2.0, 1.0)]
    ns3 = list(_bars(res3, "Tenor")[0].recurse().notes)
    assert [ly.text for ly in ns3[0].lyrics] == ["la"]
    assert ns3[1].lyrics == [] and ns3[2].lyrics == []

    # Negative guard: < 32nd sliver (offset 0.0625 < 0.125) MUST NOT split
    p_sliver = _synth_xml(tmp_path / "sliver.musicxml", [
        [[[{"pitch": "G4", "q": 3.0, "type": "half", "dots": 1}]]],
        [[
            [{"pitch": "D4", "q": 2.0, "type": "half", "stem": "up"}, {"pitch": None, "q": 1.0, "type": "quarter"}],
            [{"pitch": "D3", "q": 2.0, "type": "half", "stem": "down"}, {"pitch": None, "q": 1.0, "type": "quarter"}],
            [{"pitch": None, "q": 0.0625, "type": "64th"}, {"pitch": "A3", "q": 0.5, "type": "eighth"}, {"pitch": None, "q": 2.4375}],
        ]],
    ], beats=3, beat_type=4)
    res_sl = split_file(p_sliver, _cfg_b15(b15_f7_chord_tie_split=True), auto_layout=True)
    assert any(f.reason == "dropped_pitch" for f in res_sl.flags)
