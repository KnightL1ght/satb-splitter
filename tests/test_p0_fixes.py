"""Regression tests for Orchestrator batch P0/P2 (BUG-1, BUG-5/6, BUG-7)."""

from __future__ import annotations

from pathlib import Path

from music21 import chord, clef, meter, note, stream

from satb_splitter.model.config import SplitConfig
from satb_splitter.split.engine import split_score
from satb_splitter.split.layout import detect_layout

from helpers import pitches_in


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def voiced_measure(number, voices: dict[str, list], ts: str | None = None):
    """voices: id -> list of (offset, element)."""
    m = stream.Measure(number=number)
    if ts:
        m.insert(0, meter.TimeSignature(ts))
    for vid, events in voices.items():
        v = stream.Voice(id=vid)
        for off, el in events:
            v.insert(off, el)
        m.insert(0, v)
    return m


def part_with(measures, clef_obj=None, name="Voice"):
    p = stream.Part()
    p.partName = name
    for i, m in enumerate(measures):
        if i == 0 and clef_obj is not None:
            m.insert(0, clef_obj)
        p.append(m)
    return p


def score_of(*parts):
    sc = stream.Score()
    for p in parts:
        sc.insert(0, p)
    return sc


def events(score):
    """[(measure, offset, ql, pitch|'r')] of the single part."""
    out = []
    for m in score.parts[0].getElementsByClass(stream.Measure):
        for el in m.notesAndRests:
            out.append(
                (m.number, float(el.offset), float(el.quarterLength),
                 "r" if el.isRest else el.pitch.nameWithOctave)
            )
    return out


def bar_fill(score):
    return [
        float(m.highestTime)
        for m in score.parts[0].getElementsByClass(stream.Measure)
    ]


def assert_monophonic(score):
    for m in score.parts[0].getElementsByClass(stream.Measure):
        assert not m.voices
        cursor = 0.0
        for el in sorted(m.notesAndRests, key=lambda e: float(e.offset)):
            assert float(el.offset) >= cursor - 1e-6, f"overlap in m{m.number}"
            cursor = float(el.offset) + float(el.quarterLength)


def n(p, ql):
    return note.Note(p, quarterLength=ql)


# ---------------------------------------------------------------------------
# BUG-1: voices matched by order, same-onset notes never serialized
# ---------------------------------------------------------------------------

def test_audiveris_voice_numbers_5_6_map_by_order():
    """Lower staff uses voices 5/6 (Audiveris) -> Tenor/Bass, bars stay 3/4."""
    sa = part_with(
        [voiced_measure(1, {"1": [(0, n("E5", 3))], "2": [(0, n("C5", 3))]}, ts="3/4")],
        clef.TrebleClef(),
    )
    tb = part_with(
        [
            voiced_measure(
                1,
                {
                    "5": [(0, n("G3", 2)), (2, n("A3", 1))],
                    "6": [(0, n("D3", 1)), (1, n("C#3", 1)), (2, n("A2", 1))],
                },
                ts="3/4",
            )
        ],
        clef.BassClef(),
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert events(res.scores["Tenor"]) == [(1, 0.0, 2.0, "G3"), (1, 2.0, 1.0, "A3")]
    assert events(res.scores["Bass"]) == [
        (1, 0.0, 1.0, "D3"), (1, 1.0, 1.0, "C#3"), (1, 2.0, 1.0, "A2"),
    ]
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        assert bar_fill(res.scores[role]) == [3.0]
        assert_monophonic(res.scores[role])


def test_three_voices_extra_merged_by_free_line_not_serialized():
    """Voice 7 fills the line that is free at that beat; clashes are flagged."""
    tb = part_with(
        [
            voiced_measure(
                1,
                {
                    "5": [(0, n("C4", 1))],               # upper only on beat 1
                    "6": [(0, n("C3", 2))],               # lower beats 1-2
                    "7": [(1, n("E3", 1)), (2, n("G3", 2))],  # beat 2 (upper free) + beats 3-4
                },
                ts="4/4",
            )
        ],
        clef.BassClef(),
    )
    sa = part_with([voiced_measure(1, {"1": [(0, n("C5", 4))], "2": [(0, n("A4", 4))]}, ts="4/4")], clef.TrebleClef())
    res = split_score(score_of(sa, tb), auto_layout=True)
    ten = events(res.scores["Tenor"])
    bas = events(res.scores["Bass"])
    assert ten == [(1, 0.0, 1.0, "C4"), (1, 1.0, 1.0, "E3"), (1, 2.0, 2.0, "G3")]
    assert bas[0] == (1, 0.0, 2.0, "C3")
    assert bar_fill(res.scores["Tenor"]) == [4.0]
    assert bar_fill(res.scores["Bass"]) == [4.0]


def test_voice_ids_3_4_not_copied_to_both_lines():
    """Normal-score case: voices 3/4 are the two lines -> not duplicated."""
    sa = part_with(
        [voiced_measure(1, {"3": [(0, n("D5", 4))], "4": [(0, n("B4", 4))]}, ts="4/4")],
        clef.TrebleClef(),
    )
    tb = part_with(
        [voiced_measure(1, {"3": [(0, n("G3", 4))], "4": [(0, n("G2", 4))]}, ts="4/4")],
        clef.BassClef(),
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["D5"]
    assert pitches_in(res.scores["Alto"]) == ["B4"]
    assert pitches_in(res.scores["Tenor"]) == ["G3"]
    assert pitches_in(res.scores["Bass"]) == ["G2"]


def test_chord_in_upper_voice_gives_bottom_to_free_lower():
    sa = part_with(
        [
            voiced_measure(
                1,
                {"1": [(0, chord.Chord(["G4", "B4"], quarterLength=2)), (2, n("C5", 2))],
                 "2": [(0, note.Rest(quarterLength=2)), (2, n("A4", 2))]},
                ts="4/4",
            )
        ],
        clef.TrebleClef(),
    )
    tb = part_with([stream.Measure(number=1)], clef.BassClef())
    tb.getElementsByClass(stream.Measure)[0].insert(0, note.Rest(quarterLength=4))
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["B4", "C5"]
    assert pitches_in(res.scores["Alto"]) == ["G4", "A4"]


def test_note_running_past_barline_is_clipped_and_flagged():
    sa = part_with(
        [voiced_measure(1, {"1": [(0, n("C5", 2)), (2.5, n("D5", 1))], "2": [(0, n("A4", 3))]}, ts="3/4")],
        clef.TrebleClef(),
    )
    tb = part_with([voiced_measure(1, {"1": [(0, n("C3", 3))]}, ts="3/4")], clef.BassClef())
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert bar_fill(res.scores["Soprano"]) == [3.0]
    assert events(res.scores["Soprano"])[-1] == (1, 2.5, 0.5, "D5")
    assert any(f.reason == "clipped_at_barline" for f in res.flags)


# ---------------------------------------------------------------------------
# BUG-5/6: layout by clef/position + aligned bar counts
# ---------------------------------------------------------------------------

def _simple_part(clef_obj, pitch, bars=2, name="Voice"):
    ms = []
    for i in range(bars):
        m = stream.Measure(number=i + 1)
        if i == 0:
            m.insert(0, meter.TimeSignature("4/4"))
        m.insert(0, n(pitch, 4))
        ms.append(m)
    return part_with(ms, clef_obj, name=name)


def test_solo_staff_above_choir_is_ignored():
    """[trio G, choir G, choir F] -> SA from staff 1, TB from staff 2."""
    sc = score_of(
        _simple_part(clef.TrebleClef(), "G5"),
        _simple_part(clef.TrebleClef(), "E5"),
        _simple_part(clef.BassClef(), "C3"),
    )
    det = detect_layout(sc)
    pairs = {(p.part_index, p.upper, p.lower) for p in det.template.pairs}
    assert pairs == {(1, "Soprano", "Alto"), (2, "Tenor", "Bass")}
    assert det.template.ignore_part_indices == [0]
    res = split_score(sc, auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["E5", "E5"]
    assert pitches_in(res.scores["Tenor"]) == ["C3", "C3"]
    assert any(f.reason == "parts_ignored" for f in res.flags)


def test_solo_tenor_treble8_above_choir():
    sc = score_of(
        _simple_part(clef.Treble8vbClef(), "C4"),
        _simple_part(clef.TrebleClef(), "E5"),
        _simple_part(clef.BassClef(), "C3"),
    )
    det = detect_layout(sc)
    assert det.template.pairs[0].part_index == 1
    assert det.template.pairs[1].part_index == 2


def test_two_solos_above_choir_four_staves():
    sc = score_of(
        _simple_part(clef.TrebleClef(), "G5"),
        _simple_part(clef.TrebleClef(), "A5"),
        _simple_part(clef.TrebleClef(), "E5"),
        _simple_part(clef.BassClef(), "C3"),
    )
    det = detect_layout(sc)
    assert {p.part_index for p in det.template.pairs} == {2, 3}


def test_true_open_score_still_detected():
    sc = score_of(
        _simple_part(clef.TrebleClef(), "C5"),
        _simple_part(clef.TrebleClef(), "A4"),
        _simple_part(clef.Treble8vbClef(), "E3"),
        _simple_part(clef.BassClef(), "C3"),
    )
    assert detect_layout(sc).template.name == "four_staff_open"


def test_single_treble_staff_pads_tb_with_rests():
    sc = score_of(_simple_part(clef.TrebleClef(), "E5", bars=3))
    res = split_score(sc, auto_layout=True)
    counts = {r: len(res.scores[r].parts[0].getElementsByClass(stream.Measure)) for r in res.scores}
    assert set(counts.values()) == {3}
    assert pitches_in(res.scores["Tenor"]) == []
    assert any(f.reason == "role_padded" for f in res.flags)


def test_single_bass_staff_goes_to_tb():
    sc = score_of(_simple_part(clef.BassClef(), "C3", bars=2))
    res = split_score(sc, auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["C3", "C3"]
    assert pitches_in(res.scores["Soprano"]) == []


def test_short_staff_padded_to_same_bar_count():
    sc = score_of(
        _simple_part(clef.TrebleClef(), "E5", bars=4),
        _simple_part(clef.BassClef(), "C3", bars=3),
    )
    res = split_score(sc, auto_layout=True)
    counts = {r: len(res.scores[r].parts[0].getElementsByClass(stream.Measure)) for r in res.scores}
    assert set(counts.values()) == {4}
    assert any(f.reason == "measure_count_mismatch" for f in res.flags)


def test_treble_bar_on_bass_staff_is_not_given_to_tb():
    """Solo system Audiveris attached to the TB staff (hard p8/p16)."""
    tb = _simple_part(clef.BassClef(), "C3", bars=3)
    m2 = tb.getElementsByClass(stream.Measure)[1]
    m2.insert(0, clef.TrebleClef())
    m3 = tb.getElementsByClass(stream.Measure)[2]
    m3.insert(0, clef.BassClef())
    sa = _simple_part(clef.TrebleClef(), "E5", bars=3)
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["C3", "C3"]
    assert any(f.reason == "foreign_clef_measure" for f in res.flags)
    assert len(res.scores["Tenor"].parts[0].getElementsByClass(stream.Measure)) == 3


def test_merge_keeps_bar_counts_aligned_and_warns(tmp_path: Path):
    from satb_splitter.export.merge import split_and_merge_mxl_files

    p1 = tmp_path / "p1.musicxml"
    score_of(
        _simple_part(clef.TrebleClef(), "E5", bars=2),
        _simple_part(clef.BassClef(), "C3", bars=2),
    ).write("musicxml", fp=str(p1))
    p2 = tmp_path / "p2.musicxml"
    score_of(_simple_part(clef.TrebleClef(), "G5", bars=3)).write("musicxml", fp=str(p2))

    written, log, res = split_and_merge_mxl_files(
        [p1, p2], tmp_path / "out", "Song", omr_failures=["page_03.pdf"]
    )
    counts = {
        r: len(res.scores[r].parts[0].getElementsByClass(stream.Measure))
        for r in ("Soprano", "Alto", "Tenor", "Bass")
    }
    assert set(counts.values()) == {5}
    assert any("page_03.pdf" in line and "WARNING" in line for line in log)
    assert any(f.reason == "omr_page_failed" for f in res.flags)


# ---------------------------------------------------------------------------
# BUG-7: Audiveris output decoding
# ---------------------------------------------------------------------------

def test_audiveris_log_decoding_handles_0x81(tmp_path: Path):
    from satb_splitter.omr.audiveris import _read_log_files, _to_text

    assert "abc" in _to_text(b"abc\x81def")
    (tmp_path / "page_01-x.log").write_bytes(b"Could not export \x81 sheet")
    text = _read_log_files(tmp_path)
    assert "Could not export" in text