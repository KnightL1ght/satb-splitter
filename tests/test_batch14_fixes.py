"""Batch 14: Taguchi-tunable OMR repair grafts (F1..F6) with strict gates + F7 PDF MR1 recovery."""
from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from music21 import expressions, meter, note, stream, tie

from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.export.writer import preflight_musicxml, write_review_report
from satb_splitter.model.config import DivisiMode, GraftConfig, SplitConfig, resolve_graft_config
from satb_splitter.split.engine import split_file
from satb_splitter.split.graft import apply_grafts
from satb_splitter.split.multirest import scan_multirests

FIX = Path(__file__).parent / "fixtures" / "batch14"
ROLES = ("Soprano", "Alto", "Tenor", "Bass")


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _fixture(tmp_path: Path, stem: str, *, with_graft: bool = True, as_name: str | None = None) -> Path:
    for p in FIX.glob(stem + ".*"):
        if not with_graft and p.name.endswith(".graft.json"):
            continue
        dst_name = p.name.replace(stem, as_name) if as_name else p.name
        dst = tmp_path / dst_name
        if p.name.endswith(".graft.json") and as_name:
            data = json.loads(p.read_text(encoding="utf-8"))
            for r in data.get("records", []):
                if "bar" in r:
                    r["bar"]["mxl_file"] = f"{as_name}.mxl"
            dst.write_text(json.dumps(data), encoding="utf-8")
        else:
            shutil.copy(p, dst)
    return tmp_path / f"{as_name or stem}.mxl"


def _bars(res, role: str) -> list[stream.Measure]:
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def _sig(m: stream.Measure) -> list[tuple[str, float, float]]:
    return [
        ("R" if e.isRest else "+".join(p.nameWithOctave for p in e.pitches), float(e.offset), float(e.quarterLength))
        for e in m.recurse().notesAndRests
        if not e.duration.isGrace
    ]


def _cfg_off() -> SplitConfig:
    return SplitConfig(graft=GraftConfig(
        f1_type_a_dot=False,
        f2_type_a_beam_meter=False,
        f3_type_b_shared_rest=False,
        f4_type_b_unprinted_rest=False,
        f5_type_c_voice_split=False,
        f6_type_c_split_dur=False,
        f7_pdf_multirest_mr1=False,
    ))


def _cfg_all(**overrides) -> SplitConfig:
    kw = dict(
        f1_type_a_dot=True,
        f2_type_a_beam_meter=True,
        f3_type_b_shared_rest=True,
        f4_type_b_unprinted_rest=True,
        f5_type_c_voice_split=True,
        f6_type_c_split_dur=True,
        f7_pdf_multirest_mr1=True,
        b15_f2_lyric_dedup=False,
        b15_f3_meter_clamp=False,
        b15_f4_type_d_overfull=False,
        b15_f5_type_d_underfull=False,
        b15_f6_tie_continuity=False,
        b15_f7_chord_tie_split=False,
    )
    kw.update(overrides)
    return SplitConfig(graft=GraftConfig(**kw))


def _synth_mxl(
    path: Path,
    staves: list[list[list[dict]]],
    *,
    beats: int = 4,
    beat_type: int = 4,
    barlines: dict[int, str] | None = None,
    mr: dict[int, int] | None = None,
) -> Path:
    """Create a synthetic MusicXML with explicit voices, lyrics, fermatas, ties, default-x."""
    root = ET.Element("score-partwise", version="4.0")
    pl = ET.SubElement(root, "part-list")
    for k in range(len(staves)):
        sp = ET.SubElement(pl, "score-part", id=f"P{k + 1}")
        ET.SubElement(sp, "part-name").text = "Voice"
    div = 4
    for pi, bars in enumerate(staves):
        p = ET.SubElement(root, "part", id=f"P{pi + 1}")
        for mi, bar_voices in enumerate(bars):
            m = ET.SubElement(p, "measure", number=str(mi + 1))
            if mi == 0 or (mr and (mi + 1) in mr):
                a = ET.SubElement(m, "attributes")
                if mi == 0:
                    ET.SubElement(a, "divisions").text = str(div)
                    t = ET.SubElement(a, "time")
                    ET.SubElement(t, "beats").text = str(beats)
                    ET.SubElement(t, "beat-type").text = str(beat_type)
                    c = ET.SubElement(a, "clef")
                    ET.SubElement(c, "sign").text = "G" if pi == 0 else "F"
                    ET.SubElement(c, "line").text = "2" if pi == 0 else "4"
                if mr and (mi + 1) in mr:
                    ms = ET.SubElement(a, "measure-style")
                    ET.SubElement(ms, "multiple-rest").text = str(mr[mi + 1])
            for vi, evs in enumerate(bar_voices):
                if vi > 0:
                    bk = ET.SubElement(m, "backup")
                    prev_tot = sum(int(round(e["q"] * div)) for e in bar_voices[vi - 1])
                    ET.SubElement(bk, "duration").text = str(prev_tot)
                for ev in evs:
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
                            if ev.get("measure_rest"):
                                ET.SubElement(n, "rest", measure="yes")
                            else:
                                ET.SubElement(n, "rest")
                        else:
                            pe = ET.SubElement(n, "pitch")
                            ET.SubElement(pe, "step").text = pstr[0]
                            if len(pstr) == 3 and pstr[1] == "b":
                                ET.SubElement(pe, "alter").text = "-1"
                            elif len(pstr) == 3 and pstr[1] == "#":
                                ET.SubElement(pe, "alter").text = "1"
                            ET.SubElement(pe, "octave").text = pstr[-1]
                        ET.SubElement(n, "duration").text = str(int(round(ev["q"] * div)))
                        ET.SubElement(n, "voice").text = str(ev.get("voice", vi + 1))
                        if ev.get("type"):
                            ET.SubElement(n, "type").text = ev["type"]
                        for _ in range(ev.get("dots", 0)):
                            ET.SubElement(n, "dot")
                        if ev.get("fermata") and ci == 0:
                            nots = n.find("notations")
                            if nots is None:
                                nots = ET.SubElement(n, "notations")
                            ET.SubElement(nots, "fermata")
                        if ev.get("tie") and ci == 0:
                            ET.SubElement(n, "tie", type=ev["tie"])
                            nots = n.find("notations")
                            if nots is None:
                                nots = ET.SubElement(n, "notations")
                            ET.SubElement(nots, "tied", type=ev["tie"])
                        if ev.get("lyric") and ci == 0:
                            ly = ET.SubElement(n, "lyric", number="1")
                            ET.SubElement(ly, "syllabic").text = "single"
                            ET.SubElement(ly, "text").text = ev["lyric"]
            if barlines and (mi + 1) in barlines:
                bl = ET.SubElement(m, "barline", location="right")
                ET.SubElement(bl, "bar-style").text = barlines[mi + 1]
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


# ------------------------------------------------ config & env override tests
def test_graft_config_defaults_and_env_overrides(monkeypatch, tmp_path):
    base = GraftConfig()
    # Committed Batch 15 X* sets B15_F1 (f4_type_b_unprinted_rest) = True on top of Batch 14 1110110
    assert (
        base.f1_type_a_dot,
        base.f2_type_a_beam_meter,
        base.f3_type_b_shared_rest,
        base.f4_type_b_unprinted_rest,
        base.f5_type_c_voice_split,
        base.f6_type_c_split_dur,
        base.f7_pdf_multirest_mr1,
    ) == (True, True, True, True, True, True, False)
    assert base.any_graft_enabled()

    # Batch 14 7-bit SATB_GRAFT_FLAGS override remains 100% compatible
    monkeypatch.setenv("SATB_GRAFT_FLAGS", "1110110")
    r_b14 = resolve_graft_config(base)
    assert (
        r_b14.f1_type_a_dot,
        r_b14.f2_type_a_beam_meter,
        r_b14.f3_type_b_shared_rest,
        r_b14.f4_type_b_unprinted_rest,
        r_b14.f5_type_c_voice_split,
        r_b14.f6_type_c_split_dur,
        r_b14.f7_pdf_multirest_mr1,
    ) == (True, True, True, False, True, True, False)

    monkeypatch.setenv("SATB_GRAFT_FLAGS", "none")
    r_off = resolve_graft_config(base)
    assert not r_off.any_graft_enabled() and not r_off.f7_pdf_multirest_mr1

    monkeypatch.setenv("SATB_GRAFT_FLAGS", "1,0,1,0,1,0,1")
    gj = tmp_path / "g.json"
    monkeypatch.setenv("SATB_GRAFT_JSON", str(gj))
    r = resolve_graft_config(base)
    assert (r.f1_type_a_dot, r.f2_type_a_beam_meter, r.f3_type_b_shared_rest,
            r.f4_type_b_unprinted_rest, r.f5_type_c_voice_split,
            r.f6_type_c_split_dur, r.f7_pdf_multirest_mr1) == (True, False, True, False, True, False, True)
    assert r.graft_json_path == gj
    monkeypatch.setenv("SATB_GRAFT_FLAGS", "F2,F7")
    r2 = resolve_graft_config(base)
    assert r2.f2_type_a_beam_meter and r2.f7_pdf_multirest_mr1 and not r2.f1_type_a_dot


# ----------------------------------------------------------- strict gate tests
def test_gates_reject_bad_pitch_bad_lyric_and_bad_meter(tmp_path):
    p = _synth_mxl(tmp_path / "g.musicxml", [
        [
            [[{"pitch": "E5", "q": 4.0, "type": "whole"}]],
            [[{"pitch": "C5", "q": 2.0, "type": "half", "lyric": "la", "fermata": True},
              {"pitch": "D5", "q": 1.0, "type": "quarter", "lyric": "do"}]],
        ],
        [
            [[{"pitch": "C3", "q": 4.0, "type": "whole"}]],
            [[{"pitch": "C3", "q": 4.0, "type": "whole"}]],
        ],
    ])
    # 1. Pitch Gate rejects F1 duration_graft on bar 2 when pitch doesn't match
    (tmp_path / "g.graft.json").write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "g.musicxml", "mxl_idx": 2},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "missed_augmentation_dot",
        "graft_payload": {"kind": "duration_graft", "edits": [{
            "voice": "1", "event_idx": 0, "is_rest": False, "pitches": ["E5"],
            "aud_dur_q": "2", "rust_dur_q": "3", "rust_type": "half", "rust_dots": 1,
            "sub_cause": "missed_augmentation_dot",
        }]},
    }]}))
    res = split_file(p, _cfg_all(f2_type_a_beam_meter=False, f3_type_b_shared_rest=False,
                                f4_type_b_unprinted_rest=False, f5_type_c_voice_split=False,
                                f6_type_c_split_dur=False), auto_layout=True)
    # Bar 2 remained 3q underfull -> pitch gate rejected
    assert any(f.reason == "underfull_measure" and f.measure == 2 for f in res.flags)
    assert _sig(_bars(res, "Soprano")[1])[:2] == [("C5", 0.0, 2.0), ("D5", 2.0, 1.0)]
    assert any("rejected 1" in f.detail for f in res.flags if f.reason == "omr_graft_summary")

    # 2. Meter Balance Gate rejects F1 when sum != declared 4/4
    (tmp_path / "g.graft.json").write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "g.musicxml", "mxl_idx": 2},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "missed_augmentation_dot",
        "graft_payload": {"kind": "duration_graft", "edits": [{
            "voice": "1", "event_idx": 0, "is_rest": False, "pitches": ["C5"],
            "aud_dur_q": "2", "rust_dur_q": "2.5", "rust_type": "half", "rust_dots": 0,
            "sub_cause": "missed_augmentation_dot",
        }]},
    }]}))
    res2 = split_file(p, _cfg_all(), auto_layout=True)
    assert any(f.reason == "underfull_measure" and f.measure == 2 for f in res2.flags)
    assert _sig(_bars(res2, "Soprano")[1])[:2] == [("C5", 0.0, 2.0), ("D5", 2.0, 1.0)]

    # 3. Pitch Gate & Lyric Gate reject F5 TYPE_C when target drops a pitch or drops a syllable
    p2 = _synth_mxl(tmp_path / "g2.musicxml", [
        [[[{"pitch": "C5", "q": 2.0, "type": "half", "lyric": "one", "x": 10},
           {"pitch": "D5", "q": 2.0, "type": "half", "lyric": "two", "x": 50}],
          [{"pitch": "A4", "q": 2.0, "type": "half", "x": 10}]]],
        [[[{"pitch": "C3", "q": 4.0, "type": "whole"}]]],
    ])
    # Target invents E5 outside source columns -> Pitch Gate rejects
    (tmp_path / "g2.graft.json").write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "g2.musicxml", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "pure_voice_split",
        "graft_payload": {"kind": "shared_voice_split", "rust_voices_target": {
            "1": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["C5"], "type": "half"},
                  {"onset_q": "2", "dur_q": "2", "is_rest": False, "pitches": ["D5"], "type": "half"}],
            "2": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["A4"], "type": "half"},
                  {"onset_q": "2", "dur_q": "2", "is_rest": False, "pitches": ["E5"], "type": "half"}],
        }},
    }]}))
    res3 = split_file(p2, _cfg_all(f5_type_c_voice_split=True), auto_layout=True)
    # Without F5 applying (rejected), Alto only had A4 2q + padding rest
    assert _sig(_bars(res3, "Alto")[0])[0] == ("A4", 0.0, 2.0)

    # 4. Lyric Gate direct test on music21 Score: dropping a syllable must reject
    sc_ly = stream.Score()
    pt = stream.Part()
    m = stream.Measure(number=1)
    m.append(meter.TimeSignature("4/4"))
    v1 = stream.Voice(id="1")
    n1 = note.Note("C5", quarterLength=2.0); n1.lyric = "keep"
    n2 = note.Note("D5", quarterLength=2.0); n2.lyric = "lost"
    v1.insert(0.0, n1); v1.insert(2.0, n2)
    v2 = stream.Voice(id="2")
    v2.insert(0.0, note.Note("A4", quarterLength=2.0))
    m.insert(0.0, v1); m.insert(0.0, v2)
    pt.append(m); sc_ly.append(pt)
    bad_ly_json = tmp_path / "bad_ly.json"
    bad_ly_json.write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "ly.musicxml", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "pure_voice_split",
        "graft_payload": {"kind": "shared_voice_split", "rust_voices_target": {
            "1": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["C5"], "type": "half"},
                  {"onset_q": "2", "dur_q": "2", "is_rest": False, "pitches": ["C5"], "type": "half"}],
            "2": [{"onset_q": "0", "dur_q": "2", "is_rest": False, "pitches": ["A4"], "type": "half"},
                  {"onset_q": "2", "dur_q": "2", "is_rest": False, "pitches": ["D5"], "type": "half"}],
        }},
    }]}))
    fl = apply_grafts(sc_ly, tmp_path / "ly.musicxml", GraftConfig(f5_type_c_voice_split=True, graft_json_path=bad_ly_json))
    assert fl and "rejected 1" in fl[0].detail


# ------------------------------------------ synthetic F1/F2/F3 ornament & intrinsic tests
def test_f1_preserves_fermata_lyric_tie_and_intrinsic_f2_f3_f5(tmp_path):
    # F1 preserves fermata, lyric, tie when dot restored
    p1 = _synth_mxl(tmp_path / "f1.musicxml", [
        [[[{"pitch": "C5", "q": 2.0, "type": "half", "lyric": "Amen", "fermata": True, "tie": "start"},
           {"pitch": "C5", "q": 1.0, "type": "quarter", "tie": "stop"}]]],
        [[[{"pitch": "C3", "q": 4.0, "type": "whole"}]]],
    ])
    (tmp_path / "f1.graft.json").write_text(json.dumps({"records": [{
        "bar": {"mxl_file": "f1.musicxml", "mxl_idx": 1},
        "staff": {"active_staff_idx": 1, "aud_part": 1, "aud_staff": 1},
        "sub_cause": "missed_augmentation_dot",
        "graft_payload": {"kind": "duration_graft", "edits": [{
            "voice": "1", "event_idx": 0, "is_rest": False, "pitches": ["C5"],
            "aud_dur_q": "2", "rust_dur_q": "3", "rust_type": "half", "rust_dots": 1,
            "sub_cause": "missed_augmentation_dot",
        }]},
    }]}))
    res1 = split_file(p1, _cfg_all(), auto_layout=True)
    sop_m = _bars(res1, "Soprano")[0]
    assert _sig(sop_m) == [("C5", 0.0, 3.0), ("C5", 3.0, 1.0)]
    n0 = list(sop_m.recurse().notes)[0]
    assert any(isinstance(x, expressions.Fermata) for x in n0.expressions)
    assert [ly.text for ly in n0.lyrics] == ["Amen"]
    assert n0.tie is not None and n0.tie.type == "start"

    # Intrinsic F2 (measure-rest normalization on Score), F3 (shared mid-staff rest), F5 (default-x column split)
    sc_f2 = stream.Score()
    pt_f2 = stream.Part()
    m_f2 = stream.Measure(number=1)
    m_f2.append(meter.TimeSignature("4/4"))
    r_bad = note.Rest(quarterLength=3.5)
    r_bad.fullMeasure = True
    m_f2.append(r_bad)
    pt_f2.append(m_f2)
    sc_f2.append(pt_f2)
    fl_f2 = apply_grafts(sc_f2, tmp_path / "none.mxl", GraftConfig(f2_type_a_beam_meter=True))
    assert fl_f2 and "F2:intrinsic_measure_rest" in fl_f2[0].detail
    assert float(list(m_f2.notesAndRests)[0].quarterLength) == 4.0

    # Intrinsic F3 without JSON sidecar: voice 2 missing eighth rest at beat 1 (x-aligned)
    p3 = _synth_mxl(tmp_path / "f3.musicxml", [
        [[[{"pitch": None, "q": 1.0, "type": "quarter", "x": 10},
           {"pitch": "G4", "q": 3.0, "type": "half", "dots": 1, "x": 60}],
          [{"pitch": "D4", "q": 3.0, "type": "half", "dots": 1, "x": 60}]]],
        [[[{"pitch": "G2", "q": 4.0, "type": "whole"}]]],
    ])
    res3 = split_file(p3, _cfg_all(), auto_layout=True)
    assert _sig(_bars(res3, "Alto")[0]) == [("R", 0.0, 1.0), ("D4", 1.0, 3.0)]
    assert not [f for f in res3.flags if f.reason == "underfull_measure"]


# ----------------------------------------- real-score Batch 14 integration tests
def test_hard_p1_m19_mr1_to_mr8_insertion_mid_page(tmp_path):
    p = _fixture(tmp_path, "b14_hard_page_01", with_graft=True)
    res_off = split_file(p, _cfg_off(), auto_layout=True)
    assert len(_bars(res_off, "Soprano")) == 54
    assert [(f.measure, f.reason) for f in res_off.flags if f.reason == "multirest_count_mismatch"] == [(19, "multirest_count_mismatch")]

    res_on = split_file(p, _cfg_all(), auto_layout=True)
    for role in ROLES:
        ms = _bars(res_on, role)
        assert len(ms) == 61, role  # 54 + 7 recovered bars
        for idx in range(18, 26):
            assert not list(ms[idx].recurse().notes), (role, idx + 1)
            assert float(ms[idx].duration.quarterLength) == 4.0
        # bar 27 (old m20) keeps forward repeat after the 8-bar multirest
        assert ms[26].leftBarline is not None
    # Subsequent split_bar_joined shifted from 23 -> 30, MR10 shifted from 39 -> 46
    assert [(f.measure, f.reason) for f in res_on.flags if f.reason == "split_bar_joined"] == [(30, "split_bar_joined")]
    assert [(f.measure, f.reason) for f in res_on.flags if f.reason.startswith("multirest")] == [
        (1, "multirest"), (7, "multirest"), (19, "multirest_pdf_recovered"), (46, "multirest")
    ]
    assert not [f for f in res_on.flags if f.reason == "multirest_count_mismatch"]
    assert res_on.source_measure_numbers[18:27] == ["19", "19+1", "19+2", "19+3", "19+4", "19+5", "19+6", "19+7", "20"]
    assert res_on.source_measure_numbers[29] == "23+X23"

    # Synthetic F7 with light-heavy barline on MR1 bar moves final barline to end of recovered run
    p_mr_bl = _synth_mxl(tmp_path / "mr_bl.musicxml", [
        [[[{"pitch": "C5", "q": 4.0, "type": "whole"}]],
         [[{"pitch": None, "q": 4.0, "measure_rest": True}]],
         [[{"pitch": "D5", "q": 4.0, "type": "whole"}]]],
        [[[{"pitch": "C3", "q": 4.0, "type": "whole"}]],
         [[{"pitch": None, "q": 4.0, "measure_rest": True}]],
         [[{"pitch": "G2", "q": 4.0, "type": "whole"}]]],
    ], mr={2: 1}, barlines={2: "light-heavy"})
    (tmp_path / "mr_bl.multirest.json").write_text(json.dumps({"printed": [{"measure_index": 1, "count": 4}]}))
    res_bl = split_file(p_mr_bl, _cfg_all(), auto_layout=True)
    for role in ROLES:
        ms = _bars(res_bl, role)
        assert len(ms) == 6
        assert ms[1].rightBarline is None or ms[1].rightBarline.type in ("regular", "normal")
        assert ms[4].rightBarline is not None and ms[4].rightBarline.type == "final"

    # Negative F7 gate: if measure 19 contains a sounding note, MR1 must NOT expand
    p_busy = _fixture(tmp_path, "b14_hard_page_01", with_graft=False, as_name="busy")
    # Inject a note into measure 19 of busy.mxl
    import zipfile
    with zipfile.ZipFile(p_busy) as z:
        xml_name = [n for n in z.namelist() if n.endswith(".xml")][0]
        xml_bytes = z.read(xml_name)
    xml_txt = xml_bytes.decode("utf-8").replace(
        '<measure number="19" width="124">',
        '<measure number="19" width="124"><!-- keep -->', 1
    )
    root = ET.fromstring(xml_txt)
    m19 = root.find("part").findall("measure")[18]
    note_el = m19.find("note")
    note_el.attrib.pop("print-object", None)
    note_el.remove(note_el.find("rest"))
    pe = ET.SubElement(note_el, "pitch")
    ET.SubElement(pe, "step").text = "C"
    ET.SubElement(pe, "octave").text = "5"
    p_busy_xml = tmp_path / "busy.musicxml"
    ET.ElementTree(root).write(p_busy_xml, encoding="utf-8", xml_declaration=True)
    res_busy = split_file(p_busy_xml, _cfg_all(), auto_layout=True)
    assert len(_bars(res_busy, "Soprano")) == 54
    assert any(f.reason == "multirest_count_mismatch" and f.measure == 19 for f in res_busy.flags)


def test_easy_m3_m6_m7_grafts_and_print_faithful_untouched(tmp_path):
    # Intrinsic F5 (without .graft.json) + full F5 (with .graft.json)
    p_no = _fixture(tmp_path, "b14_easy_page_01", with_graft=False, as_name="easy_no")
    res_intr = split_file(p_no, _cfg_all(), auto_layout=True)
    # m3 (index 3) is fixed intrinsically; T3 = G3(0..2), G3(2..2.5), C#3(2.5..3)
    assert _sig(_bars(res_intr, "Tenor")[3]) == [("G3", 0.0, 2.0), ("G3", 2.0, 0.5), ("C#3", 2.5, 0.5)]

    p = _fixture(tmp_path, "b14_easy_page_01", with_graft=True)
    res = split_file(p, _cfg_all(), auto_layout=True)
    for role in ROLES:
        preflight_musicxml(res.scores[role], flags=res.flags, role=role)
    # m3 (bar 4): Tenor + Bass clean 3q
    assert _sig(_bars(res, "Tenor")[3]) == [("G3", 0.0, 2.0), ("G3", 2.0, 0.5), ("C#3", 2.5, 0.5)]
    assert _sig(_bars(res, "Bass")[3]) == [("D3", 0.0, 1.0), ("C#3", 1.0, 1.0), ("A2", 2.0, 0.5), ("C#3", 2.5, 0.5)]
    # m6 (bar 7): all 4 voices exact 3q with 0 dropped_pitch flags on bar 7
    assert not [f for f in res.flags if f.measure == 7 and f.reason == "dropped_pitch"]
    assert _sig(_bars(res, "Soprano")[6]) == [("C#5", 0.0, 1.0), ("B4", 1.0, 1.0), ("G5", 2.0, 0.75), ("G5", 2.75, 0.25)]
    assert _sig(_bars(res, "Alto")[6]) == [("A4", 0.0, 1.0), ("G4", 1.0, 1.0), ("B4", 2.0, 0.75), ("C#5", 2.75, 0.25)]
    # m7 (bar 8): AGREE_PRINT_FAITHFUL remains untouched (not fabricated)
    assert _sig(_bars(res, "Tenor")[7])[:2] == [("D4", 0.0, 1.5), ("R", 1.5, 0.5)]


def test_100_m13_m35_m64_m68_f4_f5_repairs(tmp_path):
    p1 = _fixture(tmp_path, "b14_100_page_01", with_graft=True)
    p2 = _fixture(tmp_path, "b14_100_page_02", with_graft=True)
    p3 = _fixture(tmp_path, "b14_100_page_03", with_graft=True)
    _, _, res = split_and_merge_mxl_files([p1, p2, p3], tmp_path / "out", "100", auto_layout=True, config=_cfg_all())
    # Bar 13: F4 unprinted_rest on Alto (rest @0.0, D#4 shifted to 0.5) + F5 on Tenor/Bass
    assert _sig(_bars(res, "Alto")[12])[:2] == [("R", 0.0, 0.5), ("D#4", 0.5, 0.5)]
    assert _sig(_bars(res, "Bass")[12])[0] == ("B2", 0.0, 0.5)
    # Bar 35 (p2 m21): F5 pure_voice_split on S/A
    assert sum(q for _, _, q in _sig(_bars(res, "Alto")[34])) == 4.0
    assert not [f for f in res.flags if f.measure in (13, 35, 64, 68) and f.reason == "underfull_measure"]
    # Bar 64 (p3 m20) and Bar 68 (p3 m24): F4 unprinted_rest restored; Soprano gets B4 at @2.5 after P2 shifts
    assert ("R", 2.0, 0.5) in _sig(_bars(res, "Soprano")[63])
    assert _sig(_bars(res, "Soprano")[67])[-3:] == [("B4", 2.5, 0.5), ("A4", 3.0, 0.5), ("G#4", 3.5, 0.5)]
    assert ("R", 2.0, 0.5) in _sig(_bars(res, "Alto")[67])
    assert ("R", 2.0, 0.5) in _sig(_bars(res, "Bass")[67])


def test_normal_m9_m38_m57_and_hard_p2_m3_p6_repairs(tmp_path):
    np1 = _fixture(tmp_path, "b14_normal_page_01", with_graft=True)
    np2 = _fixture(tmp_path, "b14_normal_page_02", with_graft=True)
    _, _, nres = split_and_merge_mxl_files([np1, np2], tmp_path / "norm_out", "norm", auto_layout=True, config=_cfg_all())
    assert len(_bars(nres, "Soprano")) == 60
    # m9 (bar 9), m38 (bar 38), m57 (bar 57): exact 4.0q bars, no bar_length_mismatch
    for b_no in (9, 38, 57):
        for role in ROLES:
            assert float(_bars(nres, role)[b_no - 1].duration.quarterLength) == 4.0, (role, b_no)
        assert not [f for f in nres.flags if f.measure == b_no and f.reason in ("bar_length_mismatch", "underfull_measure")]

    # hard p2 m3 (F1 missed_augmentation_dot): Option B gives monophonic dotted quarter,
    # KEEP_DIVISI preserves Bb4+D5 / F4 dotted quarter.
    hp2 = _fixture(tmp_path, "b14_hard_page_02", with_graft=True)
    hres2 = split_file(hp2, _cfg_all(), auto_layout=True, inherit_ts="4/4")
    assert _sig(_bars(hres2, "Soprano")[2]) == [("C5", 0.0, 2.0), ("D5", 2.0, 1.5), ("E-5", 3.5, 0.5)]
    assert _sig(_bars(hres2, "Alto")[2]) == [("E-4", 0.0, 2.0), ("F4", 2.0, 1.5), ("G4", 3.5, 0.5)]
    assert not [f for f in hres2.flags if f.measure == 3 and f.reason in ("underfull_measure", "bar_length_mismatch")]
    cfg_div = SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI, graft=_cfg_all().graft)
    hres2_div = split_file(hp2, cfg_div, auto_layout=True, inherit_ts="4/4")
    assert _sig(_bars(hres2_div, "Soprano")[2]) == [("A-4+C5", 0.0, 2.0), ("B-4+D5", 2.0, 1.5), ("C5+E-5", 3.5, 0.5)]

    # hard p6: keeps 76 bars (3 split_bar_joined), reduces per-bar flags cleanly
    hp6 = _fixture(tmp_path, "b14_hard_page_06", with_graft=True)
    hres6_off = split_file(hp6, _cfg_off(), auto_layout=True, inherit_ts="9/8")
    hres6_on = split_file(hp6, _cfg_all(), auto_layout=True, inherit_ts="9/8")
    assert len(_bars(hres6_on, "Soprano")) == 76
    assert [f.measure for f in hres6_on.flags if f.reason == "split_bar_joined"] == [
        f.measure for f in hres6_off.flags if f.reason == "split_bar_joined"
    ]
    assert len([f for f in hres6_on.flags if f.measure is not None]) < len([f for f in hres6_off.flags if f.measure is not None])

    # Audit trail check in REVIEW.txt
    rv = write_review_report(hres6_on, tmp_path / "hard_p6_REVIEW.txt", "hp6")
    txt = rv.read_text(encoding="utf-8")
    assert "omr_graft_summary" in txt
