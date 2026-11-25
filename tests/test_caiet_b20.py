"""Regression and behavioral tests for Batch 20 (Caiet layout/routing fixes)."""

from __future__ import annotations

from music21 import chord, clef, meter, note, stream

from satb_splitter.model.config import SplitConfig
from satb_splitter.split.engine import split_score
from satb_splitter.split.layout import detect_layout


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
            return [n.pitch.nameWithOctave for n in m.flatten().notes if isinstance(n, note.Note)]
    return []


def test_b20_1_four_staff_open_completion_voice_voice_t_b() -> None:
    """Two adjacent unnamed treble vocal staves above Tenor complete as Soprano + Alto."""
    # Positive: 050-style ['Voice', 'Voice', 'T]', 'B'] with G, G, G8, F clefs.
    for tenor_clef in (clef.Treble8vbClef(), clef.TrebleClef()):
        sc = stream.Score()
        sc.insert(0, _make_part("Voice", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]]))
        sc.insert(0, _make_part("Voice", clef.TrebleClef(), [[note.Note("G4", quarterLength=4.0)]]))
        sc.insert(0, _make_part("T]", tenor_clef, [[note.Note("E4", quarterLength=4.0)]]))
        sc.insert(0, _make_part("B", clef.BassClef(), [[note.Note("C3", quarterLength=4.0)]]))

        det = detect_layout(sc)
        assert det.template.ignore_part_indices == []
        roles_by_idx = {p.part_index: (p.upper, p.lower) for p in det.template.pairs}
        assert roles_by_idx == {
            0: ("Soprano", None),
            1: ("Alto", None),
            2: ("Tenor", None),
            3: ("Bass", None),
        }

    # Negative guard 1: 3-staff ['Voice', 'Voice', 'T,B'] (like song 026) keeps top staff ignored.
    sc_3 = stream.Score()
    sc_3.insert(0, _make_part("Voice", clef.TrebleClef(), [[note.Note("G5", quarterLength=4.0)]]))
    sc_3.insert(0, _make_part("Voice", clef.TrebleClef(), [[chord.Chord(["C5", "G4"], quarterLength=4.0)]]))
    sc_3.insert(0, _make_part("T,B", clef.BassClef(), [[chord.Chord(["E3", "C3"], quarterLength=4.0)]]))
    det_3 = detect_layout(sc_3)
    assert det_3.template.ignore_part_indices == [0]
    assert {p.part_index: (p.upper, p.lower) for p in det_3.template.pairs} == {
        1: ("Soprano", "Alto"),
        2: ("Tenor", "Bass"),
    }

    # Negative guard 2: instrument name on staff 0 ('Piano') must remain ignored.
    sc_inst = stream.Score()
    sc_inst.insert(0, _make_part("Piano", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]]))
    sc_inst.insert(0, _make_part("Voice", clef.TrebleClef(), [[chord.Chord(["A4", "E4"], quarterLength=4.0)]]))
    sc_inst.insert(0, _make_part("T", clef.Treble8vbClef(), [[note.Note("E4", quarterLength=4.0)]]))
    sc_inst.insert(0, _make_part("B", clef.BassClef(), [[note.Note("C3", quarterLength=4.0)]]))
    det_inst = detect_layout(sc_inst)
    assert 0 in det_inst.template.ignore_part_indices

    # Negative guard 3: explicit 'Solo' name on staff 0 must not trigger 4-staff S+A pair completion.
    sc_solo = stream.Score()
    sc_solo.insert(0, _make_part("Solo", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]]))
    sc_solo.insert(0, _make_part("Voice", clef.TrebleClef(), [[chord.Chord(["A4", "E4"], quarterLength=4.0)]]))
    sc_solo.insert(0, _make_part("T", clef.Treble8vbClef(), [[note.Note("E4", quarterLength=4.0)]]))
    sc_solo.insert(0, _make_part("B", clef.BassClef(), [[note.Note("C3", quarterLength=4.0)]]))
    det_solo = detect_layout(sc_solo)
    assert 0 in det_solo.template.ignore_part_indices

    # Negative guard 4: staff i+2 already has Soprano/Alto -> staves 0,1 must stay ignored.
    sc_sa_below = stream.Score()
    sc_sa_below.insert(0, _make_part("Voice", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)]]))
    sc_sa_below.insert(0, _make_part("Voice", clef.TrebleClef(), [[note.Note("G4", quarterLength=4.0)]]))
    sc_sa_below.insert(0, _make_part("S,A", clef.TrebleClef(), [[chord.Chord(["C5", "G4"], quarterLength=4.0)]]))
    sc_sa_below.insert(0, _make_part("T,B", clef.BassClef(), [[chord.Chord(["E3", "C3"], quarterLength=4.0)]]))
    det_sa = detect_layout(sc_sa_below)
    assert det_sa.template.ignore_part_indices == [0, 1]


def test_b20_2_four_staff_open_to_two_staff_closed_reduction() -> None:
    """Silent upper S/A staves + bottom-aligned closed [G poly, F poly] route SA+TB on lower staves."""
    p_s = _make_part(
        "Voice",
        clef.TrebleClef(),
        [
            [note.Note("C5", quarterLength=4.0)],
            [note.Note("D5", quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
        ],
    )
    p_a = _make_part(
        "Voice",
        clef.TrebleClef(),
        [
            [note.Note("G4", quarterLength=4.0)],
            [note.Note("A4", quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
        ],
    )
    p_t = _make_part(
        "T]",
        clef.Treble8vbClef(),
        [
            [note.Note("E4", quarterLength=4.0)],
            [note.Note("F4", quarterLength=4.0)],
            # Bar 3: switches from G8 to plain G clef and holds closed-score SA polyphony.
            [(0.0, clef.TrebleClef()), (0.0, chord.Chord(["D5", "A4"], quarterLength=2.0)), (2.0, chord.Chord(["C5", "G4"], quarterLength=2.0))],
            [chord.Chord(["D5", "A4"], quarterLength=2.0), chord.Chord(["C5", "G4"], quarterLength=2.0)],
            [chord.Chord(["B4", "F4"], quarterLength=4.0)],
        ],
    )
    p_b = _make_part(
        "B",
        clef.BassClef(),
        [
            [note.Note("C3", quarterLength=4.0)],
            [note.Note("D3", quarterLength=4.0)],
            [chord.Chord(["F3", "B-2"], quarterLength=2.0), chord.Chord(["E3", "C3"], quarterLength=2.0)],
            [chord.Chord(["F3", "B-2"], quarterLength=2.0), chord.Chord(["E3", "C3"], quarterLength=2.0)],
            [chord.Chord(["D3", "G2"], quarterLength=4.0)],
        ],
    )
    sc = stream.Score([p_s, p_a, p_t, p_b])

    res = split_score(sc, SplitConfig(), auto_layout=True)
    # Bars 3..5 must populate Soprano/Alto from p_t and Tenor/Bass from p_b without dropped/divisi flags.
    for m_num in (3, 4):
        assert _voice_measure_pitches(res.scores["Soprano"], m_num) == ["D5", "C5"]
        assert _voice_measure_pitches(res.scores["Alto"], m_num) == ["A4", "G4"]
        assert _voice_measure_pitches(res.scores["Tenor"], m_num) == ["F3", "E3"]
        assert _voice_measure_pitches(res.scores["Bass"], m_num) == ["B-2", "C3"]
    assert _voice_measure_pitches(res.scores["Soprano"], 5) == ["B4"]
    assert _voice_measure_pitches(res.scores["Alto"], 5) == ["F4"]
    bar345_flags = [f for f in res.flags if f.measure in (3, 4, 5) and f.reason in ("dropped_pitch", "divisi_chord", "role_conflict")]
    assert bar345_flags == []

    # Negative guard 1: open-score T+B duet where Tenor stays G8 must NOT shift into Soprano/Alto.
    p_t_duet = _make_part(
        "T]",
        clef.Treble8vbClef(),
        [
            [note.Note("E4", quarterLength=4.0)],
            [note.Note("F4", quarterLength=4.0)],
            [chord.Chord(["G4", "E4"], quarterLength=4.0)],
            [chord.Chord(["G4", "E4"], quarterLength=4.0)],
        ],
    )
    p_s_duet = _make_part(
        "Voice",
        clef.TrebleClef(),
        [
            [note.Note("C5", quarterLength=4.0)],
            [note.Note("D5", quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
        ],
    )
    p_a_duet = _make_part(
        "Voice",
        clef.TrebleClef(),
        [
            [note.Note("G4", quarterLength=4.0)],
            [note.Note("A4", quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
        ],
    )
    p_b_duet = _make_part(
        "B",
        clef.BassClef(),
        [
            [note.Note("C3", quarterLength=4.0)],
            [note.Note("D3", quarterLength=4.0)],
            [note.Note("C3", quarterLength=4.0)],
            [note.Note("C3", quarterLength=4.0)],
        ],
    )
    res_duet = split_score(stream.Score([p_s_duet, p_a_duet, p_t_duet, p_b_duet]), SplitConfig(), auto_layout=True)
    assert _voice_measure_pitches(res_duet.scores["Soprano"], 3) == []
    assert _voice_measure_pitches(res_duet.scores["Alto"], 3) == []

    # Negative guard 2: plain G clef on Tenor that is monophonic (unmarked G8 duet) keeps S/A resting.
    p_t_mono_g = _make_part(
        "T]",
        clef.TrebleClef(),
        [
            [note.Note("E4", quarterLength=4.0)],
            [note.Note("F4", quarterLength=4.0)],
            [note.Note("G4", quarterLength=4.0)],
            [note.Note("A4", quarterLength=4.0)],
        ],
    )
    res_mono = split_score(
        stream.Score([
            _make_part("Voice", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)], [note.Note("D5", quarterLength=4.0)], [note.Rest(quarterLength=4.0)], [note.Rest(quarterLength=4.0)]]),
            _make_part("Voice", clef.TrebleClef(), [[note.Note("G4", quarterLength=4.0)], [note.Note("A4", quarterLength=4.0)], [note.Rest(quarterLength=4.0)], [note.Rest(quarterLength=4.0)]]),
            p_t_mono_g,
            _make_part("B", clef.BassClef(), [[note.Note("C3", quarterLength=4.0)], [note.Note("D3", quarterLength=4.0)], [note.Note("C3", quarterLength=4.0)], [note.Note("D3", quarterLength=4.0)]]),
        ]),
        SplitConfig(),
        auto_layout=True,
    )
    assert _voice_measure_pitches(res_mono.scores["Soprano"], 3) == []
    assert _voice_measure_pitches(res_mono.scores["Tenor"], 3) == ["G4"]


def test_b20_3_bottom_aligned_3to2_shift_resolves_086_without_role_conflict() -> None:
    """3->2 bottom-aligned shift ([ignored, SA, TB] -> [rest, child, Choir SA]) routes SA from TB."""
    p_child = _make_part(
        "Cor de Copii",
        clef.TrebleClef(),
        [
            [note.Note("G5", quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Rest(quarterLength=4.0)],
            [note.Note("G5", quarterLength=4.0)],
        ],
    )
    p_sa = _make_part(
        "Cor S.A.",
        clef.TrebleClef(),
        [
            [chord.Chord(["C5", "G4"], quarterLength=4.0)],
            # Bars 2-4 actually hold the children's staff when shifted onto staves 1 & 2.
            [note.Note("A4", quarterLength=4.0)],
            [note.Note("B4", quarterLength=4.0)],
            [note.Note("C5", quarterLength=4.0)],
            [chord.Chord(["D5", "A4"], quarterLength=4.0)],
        ],
    )
    p_tb = _make_part(
        "Cor T.B.",
        clef.BassClef(),
        [
            [chord.Chord(["E3", "C3"], quarterLength=4.0)],
            [(0.0, clef.TrebleClef()), (0.0, chord.Chord(["E5", "C5"], quarterLength=4.0))],
            [chord.Chord(["F5", "D5"], quarterLength=4.0)],
            [chord.Chord(["E5", "C5"], quarterLength=4.0)],
            [(0.0, clef.BassClef()), (0.0, chord.Chord(["F3", "D3"], quarterLength=4.0))],
        ],
    )
    sc = stream.Score([p_child, p_sa, p_tb])
    res = split_score(sc, SplitConfig(), auto_layout=True)

    assert [f for f in res.flags if f.reason in ("role_conflict", "duplicate_staff_line")] == []
    # Bars 2-4 Soprano/Alto come from p_tb (Choir S.A.), NOT p_sa (child line).
    assert _voice_measure_pitches(res.scores["Soprano"], 2) == ["E5"]
    assert _voice_measure_pitches(res.scores["Alto"], 2) == ["C5"]
    assert _voice_measure_pitches(res.scores["Soprano"], 3) == ["F5"]
    assert _voice_measure_pitches(res.scores["Alto"], 3) == ["D5"]
    assert _voice_measure_pitches(res.scores["Tenor"], 2) == []
    assert _voice_measure_pitches(res.scores["Bass"], 2) == []
    # Normal bars 1 & 5 remain intact.
    assert _voice_measure_pitches(res.scores["Soprano"], 1) == ["C5"]
    assert _voice_measure_pitches(res.scores["Tenor"], 1) == ["E3"]
    assert _voice_measure_pitches(res.scores["Soprano"], 5) == ["D5"]
    assert _voice_measure_pitches(res.scores["Tenor"], 5) == ["F3"]

    # Negative guard: if staff 0 rests while TB stays in F clef, no shift triggers.
    p_tb_f = _make_part(
        "Cor T.B.",
        clef.BassClef(),
        [
            [chord.Chord(["E3", "C3"], quarterLength=4.0)],
            [chord.Chord(["E3", "C3"], quarterLength=4.0)],
            [chord.Chord(["F3", "D3"], quarterLength=4.0)],
        ],
    )
    res_f = split_score(
        stream.Score([
            _make_part("Cor de Copii", clef.TrebleClef(), [[note.Note("G5", quarterLength=4.0)], [note.Rest(quarterLength=4.0)], [note.Rest(quarterLength=4.0)]]),
            _make_part("Cor S.A.", clef.TrebleClef(), [[chord.Chord(["C5", "G4"], quarterLength=4.0)], [chord.Chord(["D5", "A4"], quarterLength=4.0)], [chord.Chord(["E5", "B4"], quarterLength=4.0)]]),
            p_tb_f,
        ]),
        SplitConfig(),
        auto_layout=True,
    )
    assert _voice_measure_pitches(res_f.scores["Soprano"], 2) == ["D5"]
    assert _voice_measure_pitches(res_f.scores["Tenor"], 2) == ["E3"]


def test_b20_3_two_staff_tb_treble_while_sa_sounds_keeps_tb_105() -> None:
    """Mid-bar G->F clef transition on TB while SA already sounds must not assume S/A on TB."""
    p_sa = _make_part(
        "S, A",
        clef.TrebleClef(),
        [
            # Bar 1: SA rests while TB sings treble cue/unison (like 105 m1).
            [note.Rest(quarterLength=4.0)],
            # Bar 2: SA starts singing S+A (like 105 m2) while TB has mid-bar G->F clef change.
            [chord.Chord(["D5", "A4"], quarterLength=4.0)],
            [chord.Chord(["E5", "B4"], quarterLength=4.0)],
            [chord.Chord(["F5", "C5"], quarterLength=4.0)],
        ],
    )
    p_tb = _make_part(
        "T, B",
        clef.BassClef(),
        [
            [(0.0, clef.TrebleClef()), (0.0, chord.Chord(["C5", "G4"], quarterLength=4.0))],
            # Bar 2 starts in G clef (measure_clef_kinds == 'G') with mid-bar change to F clef.
            [(0.0, note.Note("F3", quarterLength=2.0)), (2.0, clef.BassClef()), (2.0, chord.Chord(["E3", "C3"], quarterLength=2.0))],
            [chord.Chord(["G3", "D3"], quarterLength=4.0)],
            [chord.Chord(["A3", "E3"], quarterLength=4.0)],
        ],
    )
    res = split_score(stream.Score([p_sa, p_tb]), SplitConfig(), auto_layout=True)

    assert [f for f in res.flags if f.reason == "role_conflict"] == []
    # Bar 1: TB treble routes cleanly to S/A while SA rested.
    assert _voice_measure_pitches(res.scores["Soprano"], 1) == ["C5"]
    assert _voice_measure_pitches(res.scores["Alto"], 1) == ["G4"]
    # Bar 2: SA keeps S/A and TB keeps T/B without fighting over Soprano/Alto.
    assert _voice_measure_pitches(res.scores["Soprano"], 2) == ["D5"]
    assert _voice_measure_pitches(res.scores["Alto"], 2) == ["A4"]
    assert _voice_measure_pitches(res.scores["Tenor"], 2) == ["F3", "E3"]
    assert _voice_measure_pitches(res.scores["Bass"], 2) == ["F3", "C3"]

    # Negative guard: full G-clef bar on TB without mid-bar F clef is NOT sent to Tenor/Bass.
    p_tb_full_g = _make_part(
        "T, B",
        clef.BassClef(),
        [
            [chord.Chord(["C3", "G2"], quarterLength=4.0)],
            [(0.0, clef.TrebleClef()), (0.0, note.Note("E4", quarterLength=4.0))],
            [(0.0, clef.BassClef()), (0.0, chord.Chord(["D3", "A2"], quarterLength=4.0))],
        ],
    )
    res_full_g = split_score(
        stream.Score([
            _make_part("S, A", clef.TrebleClef(), [[note.Note("C5", quarterLength=4.0)], [note.Note("D5", quarterLength=4.0)], [note.Note("E5", quarterLength=4.0)]]),
            p_tb_full_g,
        ]),
        SplitConfig(),
        auto_layout=True,
    )
    assert _voice_measure_pitches(res_full_g.scores["Tenor"], 2) == []

    # Positive guard (105 dev3k bar 1): when SA already sounds in bar 1 (k == 0) and TB starts
    # with G clef in bar 1 switching to F clef in bar 2 (j == 1, kinds[1] == "F"), keep TB on Tenor/Bass.
    res_bar1_gf = split_score(
        stream.Score([
            _make_part(
                "S, A",
                clef.TrebleClef(),
                [
                    [chord.Chord(["E5", "B4"], quarterLength=4.0)],
                    [chord.Chord(["D5", "A4"], quarterLength=4.0)],
                    [chord.Chord(["C5", "G4"], quarterLength=4.0)],
                ],
            ),
            _make_part(
                "T, B",
                clef.TrebleClef(),
                [
                    [chord.Chord(["D5", "G4"], quarterLength=4.0)],
                    [(0.0, clef.BassClef()), (0.0, chord.Chord(["F3", "C3"], quarterLength=4.0))],
                    [chord.Chord(["E3", "B2"], quarterLength=4.0)],
                ],
            ),
        ]),
        SplitConfig(),
        auto_layout=True,
    )
    assert [f for f in res_bar1_gf.flags if f.reason == "role_conflict"] == []
    assert _voice_measure_pitches(res_bar1_gf.scores["Soprano"], 1) == ["E5"]
    assert _voice_measure_pitches(res_bar1_gf.scores["Alto"], 1) == ["B4"]
    assert _voice_measure_pitches(res_bar1_gf.scores["Tenor"], 1) == ["D5"]
    assert _voice_measure_pitches(res_bar1_gf.scores["Bass"], 1) == ["G4"]
