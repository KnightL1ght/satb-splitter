"""Regression tests for Orchestrator batch 4: women's (treble-only) systems on
the T/B staff -> S/A (P0), 9/8 / 12/8 bars inside 6/8 not clipped (P1), no
garbage meters (P2), merged pickup written implicit (P3), duplicate staff
lines flagged (P3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from music21 import clef, converter, meter, note, stream

from satb_splitter.split.engine import parse_section_marker, split_score
from satb_splitter.split.layout import detect_layout

from helpers import pitches_in
from test_p0_fixes import n, part_with, score_of
from test_batch3_fixes import _bar


def _r(ql):
    return note.Rest(quarterLength=ql)


def _ts_list(score):
    return [(m.number, t.ratioString)
            for m in score.parts[0].getElementsByClass(stream.Measure)
            for t in m.getElementsByClass(meter.TimeSignature)]


def _bar_lengths(score):
    return [round(sum(float(e.quarterLength) for e in m.notesAndRests), 4)
            for m in score.parts[0].getElementsByClass(stream.Measure)]


# ---------------------------------------------------------------------------
# P0: treble-only / "femeile" sections -> S/A, never T/B at treble pitch
# ---------------------------------------------------------------------------

def test_two_voice_staves_first_empty_melody_on_second_goes_to_sa():
    """Audiveris: two parts named 'Voice', both treble, first one empty."""
    top = part_with([_bar(1, _r(3), ts="3/4"), _bar(2, _r(3))], clef.TrebleClef(), name="Voice")
    low = part_with([_bar(1, n("E-4", 3), ts="3/4"), _bar(2, n("C5", 3))], clef.TrebleClef(), name="Voice")
    sc = score_of(top, low)
    det = detect_layout(sc)
    roles = {p.part_index: p.roles() for p in det.template.pairs
             if p.part_index not in det.template.ignore_part_indices}
    assert {k: tuple(v) for k, v in roles.items()} == {1: ("Soprano", "Alto")}, det.reason
    res = split_score(sc, auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["E-4", "C5"]
    assert pitches_in(res.scores["Alto"]) == ["E-4", "C5"]
    assert pitches_in(res.scores["Tenor"]) == []
    assert pitches_in(res.scores["Bass"]) == []


def test_two_treble_staves_both_with_notes_are_s_and_a():
    top = part_with([_bar(1, n("C5", 4), ts="4/4")], clef.TrebleClef(), name="Voice")
    low = part_with([_bar(1, n("A4", 4), ts="4/4")], clef.TrebleClef(), name="Voice")
    res = split_score(score_of(top, low), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["C5"]
    assert pitches_in(res.scores["Alto"]) == ["A4"]
    assert pitches_in(res.scores["Tenor"]) == [] and pitches_in(res.scores["Bass"]) == []


def _p6_like(marker=None):
    """Hard p6: lower 'Voice' staff holds a women's treble system (more bars
    than its bass-clef choir bars); the upper staff is silent there."""
    top = part_with(
        [_bar(1, _r(3), ts="6/8"), _bar(2, _r(3)), _bar(3, _r(3)),
         _bar(4, n("C5", 3)), _bar(5, n("B-4", 3))],
        clef.TrebleClef(), name="Voice")
    low = part_with(
        [_bar(1, n("E-4", 3), ts="6/8", text=marker), _bar(2, n("C5", 3)), _bar(3, n("B-4", 3)),
         _bar(4, n("C4", 3), clef_obj=clef.BassClef()), _bar(5, n("E-3", 3))],
        clef.TrebleClef(), name="Voice")
    return score_of(top, low)


@pytest.mark.parametrize("marker", [None, "femeile"])
def test_treble_run_on_tb_staff_goes_to_sa_even_when_it_dominates(marker):
    res = split_score(_p6_like(marker), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["E-4", "C5", "B-4", "C5", "B-4"]
    assert pitches_in(res.scores["Alto"])[:3] == ["E-4", "C5", "B-4"]
    # T/B only sing their bass-clef bars, never the women's melody
    assert pitches_in(res.scores["Tenor"]) == ["C4", "E-3"]
    assert pitches_in(res.scores["Bass"]) == ["C4", "E-3"]


@pytest.mark.parametrize("text", ["femeile", "Femeile", "femei", "doar femeile"])
def test_women_marker_is_sa(text):
    sec = parse_section_marker(text)
    assert sec is not None and sec.roles == ("Soprano", "Alto")


@pytest.mark.parametrize("text", ["barbatii", "b\u0103rba\u021bii", "B\u0103rba\u0163ii", "barbati"])
def test_men_marker_is_tb(text):
    sec = parse_section_marker(text)
    assert sec is not None and sec.roles == ("Tenor", "Bass")


def test_women_word_inside_a_lyric_line_is_not_a_marker():
    assert parse_section_marker("si femeile au venit la mormant") is None


def test_barbatii_on_treble_run_goes_to_tb_an_octave_down():
    res = split_score(_p6_like("barbatii"), auto_layout=True)
    assert pitches_in(res.scores["Tenor"])[:3] == ["E-3", "C4", "B-3"]
    assert pitches_in(res.scores["Soprano"]) == ["C5", "B-4"]  # women only where the top staff sings
    assert any(f.reason == "octave_down_assumed" for f in res.flags)


# ---------------------------------------------------------------------------
# P1: 9/8 and 12/8 bars inside 6/8 keep all notes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra,ratio,length", [(1.5, "9/8", 4.5), (3.0, "12/8", 6.0)])
def test_long_bar_inside_6_8_is_not_clipped(extra, ratio, length):
    def staff(p1, p2, c):
        return part_with(
            [_bar(1, n(p1, 3), ts="6/8"),
             _bar(2, n(p1, 3), n(p2, extra)),
             _bar(3, n(p1, 3))], c, name="Voice")
    sa = staff("C5", "D5", clef.TrebleClef())
    tb = staff("C3", "D3", clef.BassClef())
    res = split_score(score_of(sa, tb), auto_layout=True)
    for role in ("Soprano", "Bass"):
        assert _bar_lengths(res.scores[role]) == [3.0, length, 3.0]
        # Batch 13 item 5: the declared 6/8 is kept; bar 2 is written overfull
        assert _ts_list(res.scores[role]) == [(1, "6/8")]
    assert pitches_in(res.scores["Soprano"]) == ["C5", "C5", "D5", "C5"]
    assert not any(f.reason == "overflow_dropped" for f in res.flags)
    assert not any(f.reason == "meter_from_content" for f in res.flags)
    hit = [f for f in res.flags if f.reason == "bar_length_mismatch" and f.measure == 2]
    assert hit and "6/8" in hit[0].detail and f"{length:g} q" in hit[0].detail


# ---------------------------------------------------------------------------
# P2: OMR garbage meters (21/8, 17/16 ...) are not written
# ---------------------------------------------------------------------------

def test_garbage_bar_length_uses_a_usual_meter_and_keeps_notes():
    # bar 2 holds 5.25 quarters (= 21/8): a note starts after the 4/4 barline
    def staff(p, c):
        return part_with(
            [_bar(1, n(p, 4), ts="4/4"),
             _bar(2, n(p, 4), n(p, 1.25)),
             _bar(3, n(p, 4))], c, name="Voice")
    res = split_score(score_of(staff("C5", clef.TrebleClef()), staff("C3", clef.BassClef())), auto_layout=True)
    # Batch 13 item 5: no meter is invented (21/8, 6/4 ...); 4/4 stays, bar 2
    # keeps its 5.25 quarters without rest padding
    assert _ts_list(res.scores["Soprano"]) == [(1, "4/4")]
    assert _bar_lengths(res.scores["Soprano"]) == [4.0, 5.25, 4.0]
    assert len(pitches_in(res.scores["Soprano"])) == 4  # nothing cut
    assert any(f.reason == "bar_length_mismatch" and f.measure == 2 and "5.25 q" in f.detail for f in res.flags)


def test_note_overhang_inside_bar_is_still_clipped_not_a_new_meter():
    def staff(p, c):
        return part_with([_bar(1, n(p, 2), n(p, 1.5), ts="3/4")], c, name="Voice")
    res = split_score(score_of(staff("C5", clef.TrebleClef()), staff("C3", clef.BassClef())), auto_layout=True)
    assert _ts_list(res.scores["Soprano"]) == [(1, "3/4")]
    assert _bar_lengths(res.scores["Soprano"]) == [3.0]


def test_page_without_time_signature_inherits_previous_meter(tmp_path: Path):
    from satb_splitter.export.merge import split_and_merge_mxl_files

    def page(path, ts):
        sa = part_with([_bar(1, n("C5", 3), ts=ts), _bar(2, n("D5", 2.5))], clef.TrebleClef(), name="S,A")
        tb = part_with([_bar(1, n("C3", 3), ts=ts), _bar(2, n("D3", 2.5))], clef.BassClef(), name="T,B")
        score_of(sa, tb).write("musicxml", fp=str(path))
        return path

    a = page(tmp_path / "a.musicxml", "6/8")
    b = page(tmp_path / "b.musicxml", None)  # OMR found no time signature
    _, _, res = split_and_merge_mxl_files([a, b], tmp_path / "out", "x")
    sop = res.scores["Soprano"]
    assert _ts_list(sop) == [(1, "6/8")]  # no 4/4 at the page start, no repeat
    assert _bar_lengths(sop) == [3.0, 3.0, 3.0, 3.0]


# ---------------------------------------------------------------------------
# P3: merged pickup is a real anacrusis; duplicate staff line flagged
# ---------------------------------------------------------------------------

def test_merged_pickup_is_implicit_bar_zero(tmp_path: Path):
    from satb_splitter.export.merge import split_and_merge_mxl_files

    sa = part_with([_bar(0, n("G4", 1), ts="3/4"), _bar(1, n("C5", 3)), _bar(2, n("D5", 3))],
                   clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(0, n("C3", 1), ts="3/4"), _bar(1, n("C3", 3)), _bar(2, n("D3", 3))],
                   clef.BassClef(), name="T,B")
    for part in (sa, tb):
        part.getElementsByClass(stream.Measure)[0].paddingLeft = 2.0  # 1-beat pickup in 3/4
    src = tmp_path / "p.musicxml"
    score_of(sa, tb).write("musicxml", fp=str(src))
    written, _, _ = split_and_merge_mxl_files([src], tmp_path / "out", "x")
    xml = Path(written["Soprano"]).read_text(encoding="utf-8")
    assert '<measure implicit="yes" number="0">' in xml
    assert '<measure implicit="no" number="1">' in xml
    first = list(converter.parse(str(written["Soprano"])).parts[0].getElementsByClass(stream.Measure))[0]
    assert [type(e).__name__ for e in first.notesAndRests] == ["Note"]


def test_same_line_on_both_staves_is_flagged():
    sa = part_with([_bar(1, n("B-3", 1), n("D-4", 1), n("C4", 2), ts="4/4")], clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(1, n("B-3", 1), n("D-4", 1), n("C4", 2), ts="4/4")], clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert any(f.reason == "duplicate_staff_line" and f.measure == 1 for f in res.flags)
