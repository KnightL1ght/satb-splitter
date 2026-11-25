"""Regression tests for Orchestrator batch 6: flag cleanup after batch 5.

B5-1: omr_staff_shift text matches the written meter; 5/4 content still
gets meter_from_content; small clips are not flagged as staff shifts.
B5-2: overflow/clip flags from a discarded staff are dropped.
B5-3: unequal-duration divisi keeps the longer source note (no gap rest).
"""

from __future__ import annotations

from music21 import chord, clef, meter, note, stream

from satb_splitter.model.config import DivisiMode, SplitConfig
from satb_splitter.split.engine import split_score

from helpers import pitches_in
from test_p0_fixes import n, part_with, score_of, voiced_measure
from test_batch3_fixes import _bar
from test_batch5_fixes import _bar_lengths, _ts_list


def _stem(el, direction):
    el.stemDirection = direction
    return el


def test_five_four_content_flag_matches_written_meter():
    """B5-1: a 5-beat SA bar next to a 4-beat TB bar is 5/4, not a
    contradictory 'meter kept as 4/4' staff-shift flag."""
    sa = part_with(
        [
            _bar(1, n("C5", 4), ts="4/4"),
            _bar(2, n("C5", 1), n("C5", 1), n("C5", 1), n("C5", 1), n("C5", 1)),
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [
            _bar(1, n("C3", 4), ts="4/4"),
            _bar(2, n("C3", 1), n("C3", 1), n("C3", 1), n("C3", 1)),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    # Batch 13 item 5: declared 4/4 kept, bar 2 written overfull (5 q)
    assert _ts_list(res.scores["Soprano"]) == [(1, "4/4")]
    assert _bar_lengths(res.scores["Soprano"]) == [4.0, 5.0]
    assert _bar_lengths(res.scores["Bass"]) == [4.0, 5.0]
    assert any(
        f.reason == "bar_length_mismatch" and f.measure == 2 and "4/4" in f.detail and "5 q" in f.detail
        for f in res.flags
    ), res.flags
    assert not any(f.reason == "omr_staff_shift" and f.measure == 2 for f in res.flags)


def test_small_overhang_is_not_a_staff_shift_flag():
    """B5-1: a 4.25 clip next to a short partner stays 4/4 with no
    omr_staff_shift noise."""
    sa = part_with(
        [_bar(1, n("C5", 3), n("C5", 1.25), ts="4/4")],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [_bar(1, n("C3", 1), ts="4/4")],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert _bar_lengths(res.scores["Soprano"]) == [4.0]
    assert _ts_list(res.scores["Soprano"]) == [(1, "4/4")]
    assert not any(f.reason == "omr_staff_shift" for f in res.flags)


def test_discarded_staff_does_not_leave_overflow_flags():
    """B5-2: p14-like dumped D5 solo must not flag overflow/clip on S/A
    after the fitting choir staff is kept."""
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
            _bar(2, chord.Chord(["C5", "G4"], quarterLength=2), chord.Chord(["B4", "G4"], quarterLength=2), clef_obj=clef.TrebleClef()),
            _bar(3, chord.Chord(["C5", "A4"], quarterLength=2), chord.Chord(["B4", "G4"], quarterLength=2), clef_obj=clef.TrebleClef()),
        ],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert "D5" not in pitches_in(res.scores["Soprano"])
    assert "D5" not in pitches_in(res.scores["Alto"])
    leak = [
        f
        for f in res.flags
        if f.reason in ("overflow_dropped", "clipped_at_barline")
        and "D5" in (f.detail or "")
    ]
    assert leak == [], leak
    assert any(f.reason == "omr_staff_shift" for f in res.flags)
    for f in res.flags:
        if f.reason == "omr_staff_shift":
            assert "kept as 4/4" in f.detail, f


def test_unequal_divisi_keeps_longer_source_duration():
    """B5-3: C5/0.5 + B4/0.75 at the same onset stay sounding; no 16th rest."""
    c5 = _stem(n("C5", 0.5), "up")
    b4 = _stem(n("B4", 0.75), "up")
    g4 = _stem(n("G4", 0.75), "down")
    sa = part_with(
        [
            voiced_measure(
                1,
                {
                    "1": [(0.0, c5)],
                    "3": [(0.0, b4)],
                    "2": [(0.0, g4)],
                },
                ts="4/4",
            )
        ],
        clef.TrebleClef(),
        name="S,A",
    )
    tb = part_with(
        [_bar(1, n("C3", 4), ts="4/4")],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI), auto_layout=True)
    sop = list(res.scores["Soprano"].parts[0].getElementsByClass(stream.Measure))[0]
    events = [
        (
            round(float(el.offset), 4),
            round(float(el.quarterLength), 4),
            "r" if el.isRest else "+".join(
                p.nameWithOctave for p in sorted(el.pitches, key=lambda p: -p.ps)
            ),
        )
        for el in sop.notesAndRests
    ]
    assert not any(
        name == "r" and 0.0 < off < 0.75 - 1e-6 for off, _ql, name in events
    ), events

    def sounds(pitch_name: str, t: float) -> bool:
        for off, ql, name in events:
            if name == "r":
                continue
            if pitch_name in name.split("+") and off - 1e-6 <= t < off + ql - 1e-6:
                return True
        return False

    assert sounds("B4", 0.0), events
    assert sounds("B4", 0.6), events
    assert sounds("C5", 0.0), events
    b4_span = 0.0
    for off, ql, name in events:
        if "B4" in name.split("+"):
            b4_span += ql
    assert b4_span >= 0.75 - 1e-6, events
