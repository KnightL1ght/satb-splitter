"""Regression tests for Orchestrator batch 5: OMR staff-shift role_conflict
prefers the staff that fits the printed meter (P0 / Hard p14 bars 4-5), and
a short first bar of a mid-score page is padded + flagged rather than treated
as a pickup (P1 / merged bar 382)."""

from __future__ import annotations

from music21 import chord, clef, meter, note, stream

from satb_splitter.split.engine import split_score

from helpers import pitches_in
from test_p0_fixes import n, part_with, score_of
from test_batch3_fixes import _bar


def _r(ql):
    return note.Rest(quarterLength=ql)


def _ch(pitches, ql):
    return chord.Chord(pitches, quarterLength=ql)


def _bar_lengths(score):
    return [
        round(sum(float(e.quarterLength) for e in m.notesAndRests), 4)
        for m in score.parts[0].getElementsByClass(stream.Measure)
    ]


def _ts_list(score):
    return [
        (idx + 1, t.ratioString)
        for idx, m in enumerate(score.parts[0].getElementsByClass(stream.Measure))
        for t in m.getElementsByClass(meter.TimeSignature)
    ]


def test_role_conflict_keeps_staff_that_fits_printed_meter():
    """Hard p14-like: SA staff holds a dumped solo (7.5 then 9 quarters);
    TB staff (bass staff, treble clef in these bars) holds the real SA chords
    that fit 4/4. Keep the chords, 4/4, not the solo and not 15/8 or 9/4."""
    sa = part_with(
        [
            _bar(1, n("C5", 4), ts="4/4"),
            _bar(2, *[n("D5", 1) for _ in range(7)], n("D5", 0.5)),
            _bar(3, *[n("D5", 1) for _ in range(9)]),
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [
            _bar(1, n("C3", 4), ts="4/4", clef_obj=clef.BassClef()),
            _bar(2, _ch(["C5", "G4"], 2), _ch(["B4", "G4"], 2), clef_obj=clef.TrebleClef()),
            _bar(3, _ch(["C5", "A4"], 2), _ch(["B4", "G4"], 2), clef_obj=clef.TrebleClef()),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    sop = pitches_in(res.scores["Soprano"])
    alto = pitches_in(res.scores["Alto"])
    assert "D5" not in sop and "D5" not in alto, (sop, alto)
    assert "C5+G4" in sop or "C5" in sop or any("C5" in x for x in sop), sop
    assert "G4" in alto or any("G4" in x for x in alto), alto
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        lengths = _bar_lengths(res.scores[role])
        assert lengths == [4.0, 4.0, 4.0], (role, lengths)
        ratios = [r for _, r in _ts_list(res.scores[role])]
        assert "15/8" not in ratios and "9/4" not in ratios, (role, ratios)
    details = " ".join(f.detail for f in res.flags)
    assert "OMR staff shift" in details
    assert any(f.reason == "role_conflict" for f in res.flags)
    assert any(f.reason == "omr_staff_shift" for f in res.flags)


def test_midscore_short_first_bar_is_padded_and_flagged():
    """Hard p13 bar 1 / merged 382: inherit_ts means this is not a pickup.

    Batch 13 item 5: no longer padded either - kept at its OMR length under the
    declared 4/4 and flagged bar_length_mismatch (was: padded, meter_irregular)."""
    sa = part_with(
        [
            _bar(1, n("C5", 1), n("C5", 0.25), n("C5", 1), n("C5", 1), ts="4/4"),
            _bar(2, n("D5", 4)),
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [
            _bar(1, n("C3", 1), n("C3", 0.25), n("C3", 1), n("C3", 1), ts="4/4"),
            _bar(2, n("D3", 4)),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True, inherit_ts="4/4")
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        assert _bar_lengths(res.scores[role]) == [3.25, 4.0], role
        first = res.scores[role].parts[0].getElementsByClass(stream.Measure).first()
        assert not float(getattr(first, "paddingLeft", 0.0) or 0.0), role  # not a pickup
        assert _ts_list(res.scores[role]) == [(1, "4/4")], role
    assert any(
        f.reason == "bar_length_mismatch" and f.measure == 1
        and "first bar of a mid-score page" in f.detail and "3.25 q" in f.detail
        for f in res.flags
    ), res.flags
    assert not any(f.reason == "meter_irregular" for f in res.flags)


def test_work_start_short_bar_is_still_a_pickup():
    """Easy pickup: no inherit_ts, short first bar stays a pickup."""
    sa = part_with(
        [_bar(0, n("G4", 1), ts="3/4"), _bar(1, n("C5", 3))],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [_bar(0, n("C3", 1), ts="3/4"), _bar(1, n("C3", 3))],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert _bar_lengths(res.scores["Soprano"]) == [1.0, 3.0]
    assert not any(f.reason == "meter_irregular" for f in res.flags)
