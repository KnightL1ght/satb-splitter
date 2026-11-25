"""Batch 21 (Caiet): unit tests for B21.1 and B21.2 fixes and negative guards."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
import pytest
from music21 import chord, clef, expressions, meter, note, stream

from satb_splitter.model.config import GraftConfig, SplitConfig
from satb_splitter.omr.markers import pdf_marker_candidates
from satb_splitter.split.engine import parse_section_marker, split_score

ROLES = ("Soprano", "Alto", "Tenor", "Bass")


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _make_part(
    name: str,
    part_clef: clef.Clef,
    measures: list[list[object]],
    *,
    time_sig: str = "4/4",
) -> stream.Part:
    part = stream.Part(id=name.replace(" ", "_"))
    part.partName = name
    for idx, items in enumerate(measures, start=1):
        m = stream.Measure(number=idx)
        if idx == 1:
            m.insert(0.0, part_clef)
            m.insert(0.0, meter.TimeSignature(time_sig))
        for item in items:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], (int, float)):
                off, el = item
                m.insert(float(off), el)
            else:
                m.append(item)
        part.append(m)
    return part


def _voice_measure_pitches(score: stream.Score, m_num: int) -> list[str]:
    part = next(iter(score.parts))
    for m in part.getElementsByClass(stream.Measure):
        if int(m.number) == m_num:
            return [p.nameWithOctave for n in m.flatten().notes for p in n.pitches]
    return []


# ---------------------------------------------------------------------------
# B21.1: parse_section_marker additions + negative guards
# ---------------------------------------------------------------------------

def test_b21_1_parse_section_marker_positive_and_negative_guards() -> None:
    # Guitar accompaniment -> omit (both English and Romanian)
    g1 = parse_section_marker("Ac. Guitar Leading Opt.")
    assert g1 is not None and g1.kind == "omit"
    g2 = parse_section_marker("Chitara")
    assert g2 is not None and g2.kind == "omit"

    # Negative guards: never omit piano, band, or instrumental
    for non_omit in ("Piano Only", "Band", "Instrumental"):
        assert parse_section_marker(non_omit) is None

    # Duet markers: explicit letter pairs take priority over generic duet -> S/A
    dt_st = parse_section_marker("Duet S.T.(melody at the bottom)")
    assert dt_st is not None and dt_st.kind == "roles" and dt_st.roles == ("Soprano", "Tenor")

    dt_ocr = parse_section_marker("Duet S. at batty/n)")
    assert dt_ocr is not None and dt_ocr.kind == "roles" and dt_ocr.roles == ("Soprano", "Alto")

    dt_plain = parse_section_marker("Duet")
    assert dt_plain is not None and dt_plain.kind == "roles" and dt_plain.roles == ("Soprano", "Alto")

    # Pre-solo voice word ("Soprano solo") vs plain "Solo"
    pre_s = parse_section_marker("Soprano solo")
    assert pre_s is not None and pre_s.kind == "roles" and pre_s.roles == ("Soprano",) and pre_s.solo

    plain_solo = parse_section_marker("Solo")
    assert plain_solo is not None and plain_solo.kind == "omit"

    # "Corunison" / "Cor unison" -> unison
    cu1 = parse_section_marker("Corunison")
    assert cu1 is not None and cu1.kind == "unison" and cu1.roles == ROLES
    cu2 = parse_section_marker("Cor unison")
    assert cu2 is not None and cu2.kind == "unison" and cu2.roles == ROLES
    assert parse_section_marker("solo - corunison") is None


# ---------------------------------------------------------------------------
# B21.1: _section_plan pending_other across 2-staff -> single-staff bars (091)
# ---------------------------------------------------------------------------

def test_b21_1_091_style_pending_other_and_duet_routing() -> None:
    # 7-bar score mirroring 091 page_01:
    # m1..3: SA rests, TB (G clef) plays 'Ac. Guitar Leading Opt.' (omitted)
    # m4:    SA sings 'Solo Sopran Opt.' (2-staff bar) while TB finishes guitar intro
    # m5..6: SA rests again -> TB adopts pending 'Solo Sopran Opt.' (Soprano only)
    # m7:    SA rests, TB has 'Duet S. at batty/n)' with 2-note chord -> splits S+A
    # m8:    F clef resumes normal T+B
    sa = _make_part(
        "S, A",
        clef.TrebleClef(),
        [
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [(0.0, expressions.TextExpression("Solo Sopran Opt.")), (0.0, note.Note("C5", quarterLength=4.0))],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [chord.Chord(["E5", "C5"], quarterLength=4.0)],
        ],
    )
    tb = _make_part(
        "T, B",
        clef.TrebleClef(),
        [
            [(0.0, expressions.TextExpression("Ac. Guitar Leading Opt.")), (0.0, chord.Chord(["E4", "C4", "G3"], quarterLength=4.0))],
            [chord.Chord(["F4", "D4", "A3"], quarterLength=4.0)],
            [chord.Chord(["G4", "E4", "C4"], quarterLength=4.0)],
            [chord.Chord(["C5", "G4", "E4"], quarterLength=4.0)],
            [note.Note("D5", quarterLength=4.0)],
            [note.Note("E5", quarterLength=4.0)],
            [(0.0, expressions.TextExpression("Duet S. at batty/n)")), (0.0, chord.Chord(["F5", "A4"], quarterLength=4.0))],
            [(0.0, clef.BassClef()), (0.0, chord.Chord(["C4", "C3"], quarterLength=4.0))],
        ],
    )
    res = split_score(stream.Score([sa, tb]), SplitConfig(), auto_layout=True)

    assert [f for f in res.flags if f.reason in ("role_conflict", "dropped_pitch")] == []
    # Bars 1..4 omitted (0 foreign_clef_measure); bars 5..6 solo_routed; bar 7 'duet' -> S/A on F-staff
    assert [f.measure for f in res.flags if f.reason == "foreign_clef_measure"] == [7]
    # Bars 1..3 omitted
    for m_num in (1, 2, 3):
        for role in ROLES:
            assert _voice_measure_pitches(res.scores[role], m_num) == []
    # Bar 4: SA keeps Solo Sopran Opt. (rescued on both S/A via unison); TB guitar tail stays omitted -> 0 role_conflict, 0 dropped_pitch
    assert _voice_measure_pitches(res.scores["Soprano"], 4) == ["C5"]
    assert _voice_measure_pitches(res.scores["Alto"], 4) == ["C5"]
    assert _voice_measure_pitches(res.scores["Tenor"], 4) == []
    assert _voice_measure_pitches(res.scores["Bass"], 4) == []
    # Bars 5..6: SA rests, so pending 'Solo Sopran Opt.' applies to TB treble melody
    assert _voice_measure_pitches(res.scores["Soprano"], 5) == ["D5"]
    assert _voice_measure_pitches(res.scores["Alto"], 5) == []
    assert _voice_measure_pitches(res.scores["Soprano"], 6) == ["E5"]
    assert _voice_measure_pitches(res.scores["Alto"], 6) == []
    # Bar 7: 'Duet S. at batty/n)' splits 2 voices across Soprano + Alto
    assert _voice_measure_pitches(res.scores["Soprano"], 7) == ["F5"]
    assert _voice_measure_pitches(res.scores["Alto"], 7) == ["A4"]
    # Bar 8: F clef resumes normal SATB
    assert _voice_measure_pitches(res.scores["Soprano"], 8) == ["E5"]
    assert _voice_measure_pitches(res.scores["Alto"], 8) == ["C5"]
    assert _voice_measure_pitches(res.scores["Tenor"], 8) == ["C4"]
    assert _voice_measure_pitches(res.scores["Bass"], 8) == ["C3"]


def test_b21_1_negative_guard_res_full_g_two_staff_without_omit() -> None:
    """2-staff full G-clef bar on TB without 'omit' must NOT route to Tenor/Bass."""
    sa = _make_part(
        "S, A",
        clef.TrebleClef(),
        [
            [note.Note("C5", quarterLength=4.0)],
            [note.Note("D5", quarterLength=4.0)],
            [note.Note("E5", quarterLength=4.0)],
        ],
    )
    tb = _make_part(
        "T, B",
        clef.BassClef(),
        [
            [chord.Chord(["C3", "G2"], quarterLength=4.0)],
            [(0.0, clef.TrebleClef()), (0.0, note.Note("E4", quarterLength=4.0))],
            [(0.0, clef.BassClef()), (0.0, chord.Chord(["D3", "A2"], quarterLength=4.0))],
        ],
    )
    res_full_g = split_score(stream.Score([sa, tb]), SplitConfig(), auto_layout=True)
    assert _voice_measure_pitches(res_full_g.scores["Tenor"], 2) == []


# ---------------------------------------------------------------------------
# B21.2: _fill_mono B15_F7 tie-split on overlapping durations (094 / 050 m7)
# ---------------------------------------------------------------------------

def _four_staff_with_overlap_in_bass() -> stream.Score:
    s = _make_part("Soprano", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]])
    a = _make_part("Alto", clef.TrebleClef(), [[note.Note("G4", quarterLength=4.0)]])
    t = _make_part("Tenor", clef.Treble8vbClef(), [[note.Note("E4", quarterLength=4.0)]])

    b = stream.Part(id="Bass")
    b.partName = "Bass"
    m = stream.Measure(number=1)
    m.insert(0.0, clef.BassClef())
    m.insert(0.0, meter.TimeSignature("4/4"))
    v1 = stream.Voice(id="1")
    v1.insert(0.0, note.Note("G3", quarterLength=2.0))
    v1.insert(2.0, note.Note("F3", quarterLength=2.0))
    v2 = stream.Voice(id="2")
    v2.insert(3.0, note.Note("E3", quarterLength=0.5))
    m.insert(0.0, v1)
    m.insert(0.0, v2)
    b.append(m)
    return stream.Score([s, a, t, b])


def test_b21_2_fill_mono_tie_split_094_and_050_m7_style() -> None:
    # 094 m9 style: 2.0q F3 spanning [2.0, 4.0) overlapped by 0.5q E3 at beat 4 [3.0, 3.5)
    sc_094 = _four_staff_with_overlap_in_bass()
    res_on = split_score(sc_094, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=True)), auto_layout=True)
    assert [f for f in res_on.flags if f.reason == "dropped_pitch"] == []
    assert sorted(set(_voice_measure_pitches(res_on.scores["Bass"], 1))) == ["E3", "F3", "G3"]

    res_off = split_score(sc_094, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)), auto_layout=True)
    assert [f.reason for f in res_off.flags if f.reason == "dropped_pitch"] == ["dropped_pitch"]
    assert sorted(set(_voice_measure_pitches(res_off.scores["Bass"], 1))) == ["F3", "G3"]

    # 050 m7 style: Soprano voice 1 A-5 (2.0q) overlapped by voice 2 E-5 (0.5q) at 1.0 & D-5 (0.5q) at 1.5
    s_050 = stream.Part(id="Soprano")
    s_050.partName = "Soprano"
    m = stream.Measure(number=1)
    m.insert(0.0, clef.TrebleClef())
    m.insert(0.0, meter.TimeSignature("4/4"))
    v1 = stream.Voice(id="1")
    v1.insert(0.0, note.Note("A-5", quarterLength=2.0))
    v1.insert(2.0, note.Note("A-5", quarterLength=1.0))
    v1.insert(3.0, note.Note("D-4", quarterLength=1.0))
    v2 = stream.Voice(id="2")
    v2.insert(0.0, note.Note("F5", quarterLength=0.5))
    v2.insert(0.5, note.Note("F5", quarterLength=0.5))
    v2.insert(1.0, note.Note("E-5", quarterLength=0.5))
    v2.insert(1.5, note.Note("D-5", quarterLength=0.5))
    v2.insert(2.0, note.Note("E-5", quarterLength=1.0))
    m.insert(0.0, v1)
    m.insert(0.0, v2)
    s_050.append(m)

    sc_050 = stream.Score([
        s_050,
        _make_part("Alto", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]]),
        _make_part("Tenor", clef.Treble8vbClef(), [[note.Note("E-4", quarterLength=4.0)]]),
        _make_part("Bass", clef.BassClef(), [[note.Note("A-3", quarterLength=4.0)]]),
    ])
    res_050_on = split_score(sc_050, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=True)), auto_layout=True)
    assert [f for f in res_050_on.flags if f.reason == "dropped_pitch"] == []
    sop_pitches = set(_voice_measure_pitches(res_050_on.scores["Soprano"], 1))
    assert {"A-5", "F5", "E-5", "D-5", "D-4"} <= sop_pitches

    res_050_off = split_score(sc_050, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)), auto_layout=True)
    dropped_050 = [f for f in res_050_off.flags if f.reason == "dropped_pitch"]
    assert len(dropped_050) == 2
    assert "D-5" not in _voice_measure_pitches(res_050_off.scores["Soprano"], 1)


def test_b21_2_tie_split_barline_clamp_094_m9_overfull() -> None:
    """094 m9: overfull F3 [4.5, 6.5) + E3 [5.5, 6.0) in 12/8 (6.0q) clamps at barline without overflow."""
    b = stream.Part(id="Bass")
    b.partName = "Bass"
    m = stream.Measure(number=1)
    m.insert(0.0, clef.BassClef())
    m.insert(0.0, meter.TimeSignature("12/8"))
    v1 = stream.Voice(id="1")
    v1.insert(0.0, note.Note("B-3", quarterLength=1.5))
    v1.insert(1.5, note.Note("A3", quarterLength=1.0))
    v1.insert(2.5, note.Note("G3", quarterLength=0.5))
    v1.insert(3.0, note.Note("G3", quarterLength=1.5))
    v1.insert(4.5, note.Note("F3", quarterLength=2.0))
    v2 = stream.Voice(id="2")
    v2.insert(5.5, note.Note("E3", quarterLength=0.5))
    m.insert(0.0, v1)
    m.insert(0.0, v2)
    b.append(m)

    sc = stream.Score([
        _make_part("Soprano", clef.TrebleClef(), [[note.Note("C5", quarterLength=6.0)]], time_sig="12/8"),
        _make_part("Alto", clef.TrebleClef(), [[note.Note("G4", quarterLength=6.0)]], time_sig="12/8"),
        _make_part("Tenor", clef.Treble8vbClef(), [[note.Note("E4", quarterLength=6.0)]], time_sig="12/8"),
        b,
    ])
    res = split_score(sc, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=True)), auto_layout=True)

    assert [f for f in res.flags if f.reason == "dropped_pitch"] == []
    assert [f for f in res.flags if f.reason == "overflow_dropped"] == []
    assert any(f.reason == "clipped_at_barline" for f in res.flags)
    bass_pitches = _voice_measure_pitches(res.scores["Bass"], 1)
    assert "E3" in bass_pitches and "F3" in bass_pitches
    bass_m = next(iter(res.scores["Bass"].parts)).getElementsByClass(stream.Measure)[0]
    assert float(bass_m.duration.quarterLength) == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# B21.1: omr/markers.py length limit relaxed to 40 chars
# ---------------------------------------------------------------------------

def test_b21_1_markers_accepts_31_char_duet_direction_and_rejects_over_40(tmp_path: Path) -> None:
    direction_31 = "Duet S.T.(melody at the bottom)"
    assert len(direction_31) == 31
    direction_45 = "Duet S.T.(melody at the bottom of everything)"
    assert len(direction_45) > 40

    def _pdf_with_text(name: str, text: str) -> Path:
        p = tmp_path / name
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        for k in range(5):
            y = 100 + 4 * k
            page.draw_line((60, y), (560, y), color=(0, 0, 0), width=0.8)
        page.insert_text((120, 94), text, fontsize=9.5, fontname="tiro")
        x = 70
        for syl in ("Es", "te", "pa", "ce", "azi"):
            page.insert_text((x, 140), syl, fontsize=10.2, fontname="tiro")
            x += 60
        doc.save(str(p))
        doc.close()
        return p

    words_ok, _, _ = pdf_marker_candidates(_pdf_with_text("ok.pdf", direction_31))
    assert [w.text for w in words_ok] == [direction_31]

    words_too_long, _, _ = pdf_marker_candidates(_pdf_with_text("long.pdf", direction_45))
    assert words_too_long == []
