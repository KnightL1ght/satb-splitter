"""Regression tests for Orchestrator batch 7: flag 4.5-beat 9/8 bars inside 4/4.

B6-1: a page with no printed time signature that inherits 4/4, but one
bar's own length is 4.5 (an extra OMR eighth; the last note starts
inside the bar), is written as 9/8 and flagged meter_from_content.
"""

from __future__ import annotations

from music21 import clef, note

from satb_splitter.split.engine import split_score

from test_p0_fixes import n, part_with, score_of
from test_batch3_fixes import _bar
from test_batch5_fixes import _bar_lengths, _ts_list


def test_four_and_a_half_beat_bar_inside_4_4_is_flagged():
    """B6-1: last note starts inside the 4/4 bar and runs to 4.5.

    Written as 9/8 with meter_from_content, not as a silent meter
    change and not as omr_staff_shift.
    """
    sa = part_with(
        [
            _bar(1, n("C5", 1), n("C5", 1), n("C5", 1), n("C5", 1)),
            _bar(2, n("C5", 1), n("C5", 1), n("C5", 1), n("C5", 1.5)),
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [
            _bar(1, n("C3", 1), n("C3", 1), n("C3", 1), n("C3", 1)),
            _bar(2, n("C3", 1), n("C3", 1), n("C3", 1), n("C3", 1)),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True, inherit_ts="4/4")
    # Batch 13 item 5: the carried 4/4 is kept (no 9/8); bar 2 keeps 4.5 q
    assert _ts_list(res.scores["Soprano"]) == [(1, "4/4")]
    assert _bar_lengths(res.scores["Soprano"]) == [4.0, 4.5]
    assert _bar_lengths(res.scores["Bass"]) == [4.0, 4.5]
    hits = [f for f in res.flags if f.reason == "bar_length_mismatch" and f.measure == 2
            and "4/4" in (f.detail or "") and "4.5 q" in (f.detail or "")]
    assert hits, res.flags
    assert "NOT changed" in hits[0].detail
    assert not any(f.reason in ("meter_from_content", "bar_length_mismatch") and f.measure == 1 for f in res.flags)
    assert not any(f.reason == "omr_staff_shift" and f.measure == 2 for f in res.flags)


def test_bar_duration_nine_eight_with_short_notes_is_flagged():
    """B6-1 also covers a measure whose own length is 4.5 even when the
    notes stop earlier (Hard page 16 bar 11 / merged 452)."""
    sa = part_with(
        [
            _bar(1, n("C5", 1), n("C5", 1), n("C5", 1), n("C5", 1)),
            _bar(2, n("G4", 2), note.Rest(quarterLength=2.5)),
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [
            _bar(1, n("C3", 1), n("C3", 1), n("C3", 1), n("C3", 1)),
            _bar(2, n("C3", 2), note.Rest(quarterLength=2.0)),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True, inherit_ts="4/4")
    assert _ts_list(res.scores["Soprano"]) == [(1, "4/4")]  # Batch 13 item 5
    assert _bar_lengths(res.scores["Soprano"]) == [4.0, 4.5]
    assert any(
        f.reason == "bar_length_mismatch" and f.measure == 2 and "4.5 q" in (f.detail or "")
        for f in res.flags
    ), res.flags
    assert not any(f.reason == "omr_staff_shift" and f.measure == 2 for f in res.flags)
