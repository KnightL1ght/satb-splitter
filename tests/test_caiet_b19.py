"""Batch 19 (Caiet): unit tests for B19_1 and B19_2 layout auto-detection fixes."""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from music21 import chord, clef, converter, meter, note, stream

from satb_splitter.export.writer import write_combined_satb
from satb_splitter.split.engine import split_score
from satb_splitter.split.layout import _try_name_based, detect_layout


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _n(pitch: str, ql: float = 4.0, *, stem: str | None = None) -> note.Note:
    el = note.Note(pitch, quarterLength=ql)
    if stem:
        el.stemDirection = stem
    return el


def _make_bar(
    num: int,
    items: list[tuple[float, object]] | None = None,
    *,
    ts: str | None = None,
    clef_obj=None,
    voices: dict[str, list[tuple[float, object]]] | None = None,
) -> stream.Measure:
    m = stream.Measure(number=num)
    if ts:
        m.insert(0.0, meter.TimeSignature(ts))
    if clef_obj is not None:
        m.insert(0.0, clef_obj)
    if voices:
        for vid, evs in voices.items():
            v = stream.Voice(id=vid)
            for off, el in evs:
                v.insert(off, el)
            m.insert(0.0, v)
    elif items:
        for off, el in items:
            m.insert(off, el)
    return m


def _make_score(
    name0: str,
    bars0: list[stream.Measure],
    name1: str,
    bars1: list[stream.Measure],
) -> stream.Score:
    sc = stream.Score()
    p0 = stream.Part(id="P1")
    p0.partName = name0
    for b in bars0:
        p0.append(b)
    p1 = stream.Part(id="P2")
    p1.partName = name1
    for b in bars1:
        p1.append(b)
    sc.insert(0, p0)
    sc.insert(0, p1)
    return sc


def _pitches(score: stream.Score) -> list[str]:
    return [
        p.nameWithOctave
        for n in score.recurse().notes
        for p in n.pitches
    ]


def test_b19_1_duplicate_satb_two_staff_maps_sa_and_tb():
    # Case 1: ['SATB', 'SATB'] on 2-staff G+F score (mirrors 097 page_01)
    sa_bars = [
        _make_bar(
            i,
            ts="4/4" if i == 1 else None,
            clef_obj=clef.TrebleClef() if i == 1 else None,
            voices={"1": [(0.0, _n("E5", 4.0, stem="up"))], "2": [(0.0, _n("G4", 4.0, stem="down"))]},
        )
        for i in range(1, 5)
    ]
    tb_bars = [
        _make_bar(
            i,
            ts="4/4" if i == 1 else None,
            clef_obj=clef.BassClef() if i == 1 else None,
            voices={"1": [(0.0, _n("C4", 4.0, stem="up"))], "2": [(0.0, _n("C3", 4.0, stem="down"))]},
        )
        for i in range(1, 5)
    ]
    sc = _make_score("SATB", sa_bars, "SATB", tb_bars)
    det = detect_layout(sc)
    pairs = [(p.part_index, p.upper, p.lower) for p in det.template.pairs]
    assert pairs == [(0, "Soprano", "Alto"), (1, "Tenor", "Bass")]
    assert not det.template.ignore_part_indices

    res = split_score(sc, auto_layout=True)
    reasons = {f.reason for f in res.flags}
    assert not (reasons & {"duplicate_role", "role_conflict", "role_padded"})
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        assert len(_pitches(res.scores[role])) == 4

    # Direct _try_name_based without explicit kinds still maps 2x SATB -> SA / TB
    tpl_no_kinds = _try_name_based(("SATB", "SATB"))
    assert tpl_no_kinds is not None
    assert [(p.part_index, p.upper, p.lower) for p in tpl_no_kinds.pairs] == [
        (0, "Soprano", "Alto"),
        (1, "Tenor", "Bass"),
    ]

    # Duplicate role pair like ['SA', 'SA'] on G+F triggers clef fallback
    sc_dup_sa = _make_score("SA", sa_bars, "SA", tb_bars)
    det_dup_sa = detect_layout(sc_dup_sa)
    assert [(p.part_index, p.upper, p.lower) for p in det_dup_sa.template.pairs] == [
        (0, "Soprano", "Alto"),
        (1, "Tenor", "Bass"),
    ]


@pytest.mark.parametrize(
    "names,poly_mode",
    [
        (("Alto", "Bass"), "voices"),
        (("A.", "B."), "chords"),
        (("Alto", "Bass"), "same_onset"),
    ],
)
def test_b19_2_truncated_single_voice_names_on_polyphonic_gf_upgraded(names, poly_mode):
    sa_bars = []
    tb_bars = []
    for i in range(1, 5):
        ts = "4/4" if i == 1 else None
        c_sa = clef.TrebleClef() if i == 1 else None
        c_tb = clef.BassClef() if i == 1 else None
        if poly_mode == "voices":
            sa_bars.append(_make_bar(
                i, ts=ts, clef_obj=c_sa,
                voices={"1": [(0.0, _n("C5", 4.0, stem="up"))], "2": [(0.0, _n("A4", 4.0, stem="down"))]},
            ))
            tb_bars.append(_make_bar(
                i, ts=ts, clef_obj=c_tb,
                voices={"1": [(0.0, _n("E3", 4.0, stem="up"))], "2": [(0.0, _n("C3", 4.0, stem="down"))]},
            ))
        elif poly_mode == "chords":
            sa_bars.append(_make_bar(
                i, [(0.0, chord.Chord(["A4", "C5"], quarterLength=4.0))],
                ts=ts, clef_obj=c_sa,
            ))
            tb_bars.append(_make_bar(
                i, [(0.0, chord.Chord(["C3", "E3"], quarterLength=4.0))],
                ts=ts, clef_obj=c_tb,
            ))
        else:
            sa_bars.append(_make_bar(
                i, [(0.0, _n("C5", 4.0)), (0.0, _n("A4", 4.0))],
                ts=ts, clef_obj=c_sa,
            ))
            tb_bars.append(_make_bar(
                i, [(0.0, _n("E3", 4.0)), (0.0, _n("C3", 4.0))],
                ts=ts, clef_obj=c_tb,
            ))

    sc = _make_score(names[0], sa_bars, names[1], tb_bars)
    det = detect_layout(sc)
    assert det.template.name == "two_staff_satb"
    assert [(p.part_index, p.upper, p.lower) for p in det.template.pairs] == [
        (0, "Soprano", "Alto"),
        (1, "Tenor", "Bass"),
    ]
    assert "truncated single-voice names" in det.reason

    res = split_score(sc, auto_layout=True)
    assert not any(f.reason == "role_padded" for f in res.flags)
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        assert len(_pitches(res.scores[role])) == 4


def test_b19_2_true_monophonic_duet_alto_bass_preserved():
    alto_bars = [
        _make_bar(
            i,
            [(0.0, _n("A4", 2.0)), (2.0, _n("G4", 2.0))],
            ts="4/4" if i == 1 else None,
            clef_obj=clef.TrebleClef() if i == 1 else None,
        )
        for i in range(1, 5)
    ]
    bass_bars = [
        _make_bar(
            i,
            [(0.0, _n("C3", 2.0)), (2.0, _n("G2", 2.0))],
            ts="4/4" if i == 1 else None,
            clef_obj=clef.BassClef() if i == 1 else None,
        )
        for i in range(1, 5)
    ]
    sc = _make_score("Alto", alto_bars, "Bass", bass_bars)
    det = detect_layout(sc)
    assert det.template.name == "name_based"
    assert [(p.part_index, p.upper, p.lower) for p in det.template.pairs] == [
        (0, "Alto", None),
        (1, "Bass", None),
    ]

    res = split_score(sc, auto_layout=True)
    assert any(f.reason == "role_padded" for f in res.flags)
    assert _pitches(res.scores["Soprano"]) == []
    assert _pitches(res.scores["Tenor"]) == []
    assert len(_pitches(res.scores["Alto"])) == 8
    assert len(_pitches(res.scores["Bass"])) == 8


def _raw_xml_part_bar_durations(xml_path) -> dict[str, list[float]]:
    root = ET.parse(xml_path).getroot()
    names = {
        sp.get("id", ""): (sp.findtext("part-name") or sp.get("id", ""))
        for sp in root.findall("part-list/score-part")
    }
    out: dict[str, list[float]] = {}
    for part in root.findall("part"):
        pid = part.get("id", "")
        name = names.get(pid, pid)
        div = 1
        bars: list[float] = []
        for meas in part.findall("measure"):
            div_el = meas.find("attributes/divisions")
            if div_el is not None and div_el.text:
                div = int(div_el.text)
            total_ticks = 0
            for child in meas:
                if child.tag == "note":
                    if child.find("chord") is not None:
                        continue
                    dur_el = child.find("duration")
                    if dur_el is not None and dur_el.text:
                        total_ticks += int(dur_el.text)
                elif child.tag == "forward":
                    dur_el = child.find("duration")
                    if dur_el is not None and dur_el.text:
                        total_ticks += int(dur_el.text)
                elif child.tag == "backup":
                    dur_el = child.find("duration")
                    if dur_el is not None and dur_el.text:
                        total_ticks -= int(dur_el.text)
            bars.append(total_ticks / div)
        out[name] = bars
    return out


def test_b19_3_meter_missing_overfull_and_underfull_bars_preserve_lengths(tmp_path):
    # 2-staff score with NO TimeSignature (meter_missing), mirroring 072:
    # Bar 1: 4.0q, Bar 2: 3.5q (underfull), Bar 3: 5.0q (overfull, Alto ends with Rest(2.5)), Bar 4: 4.0q.
    sa_bars = [
        _make_bar(
            1,
            clef_obj=clef.TrebleClef(),
            voices={
                "1": [(0.0, _n("C5", 4.0, stem="up"))],
                "2": [(0.0, _n("A4", 4.0, stem="down"))],
            },
        ),
        _make_bar(
            2,
            voices={
                "1": [(0.0, _n("C5", 2.0, stem="up")), (2.0, _n("D5", 1.5, stem="up"))],
                "2": [(0.0, _n("A4", 2.0, stem="down")), (2.0, _n("B4", 1.5, stem="down"))],
            },
        ),
        _make_bar(
            3,
            voices={
                "1": [
                    (0.0, note.Rest(quarterLength=1.0)),
                    (1.0, _n("E5", 1.0, stem="up")),
                    (2.0, _n("C5", 1.0, stem="up")),
                    (3.0, _n("D5", 1.0, stem="up")),
                    (4.0, _n("E5", 1.0, stem="up")),
                ],
                "2": [
                    (0.0, note.Rest(quarterLength=1.0)),
                    (1.0, _n("E4", 1.0, stem="down")),
                    (2.0, _n("C#4", 0.5, stem="down")),
                ],
            },
        ),
        _make_bar(
            4,
            voices={
                "1": [(0.0, _n("D5", 4.0, stem="up"))],
                "2": [(0.0, _n("D4", 4.0, stem="down"))],
            },
        ),
    ]
    tb_bars = [
        _make_bar(
            1,
            clef_obj=clef.BassClef(),
            voices={
                "1": [(0.0, _n("E3", 4.0, stem="up"))],
                "2": [(0.0, _n("C3", 4.0, stem="down"))],
            },
        ),
        _make_bar(
            2,
            voices={
                "1": [(0.0, _n("E3", 3.5, stem="up"))],
                "2": [(0.0, _n("C3", 3.5, stem="down"))],
            },
        ),
        _make_bar(
            3,
            voices={
                "1": [(0.0, _n("G3", 3.0, stem="up")), (3.0, _n("E3", 2.0, stem="up"))],
                "2": [(0.0, _n("E3", 3.0, stem="down")), (3.0, _n("C3", 2.0, stem="down"))],
            },
        ),
        _make_bar(
            4,
            voices={
                "1": [(0.0, _n("G3", 4.0, stem="up"))],
                "2": [(0.0, _n("G2", 4.0, stem="down"))],
            },
        ),
    ]
    sc = _make_score("SATB", sa_bars, "SATB", tb_bars)
    res = split_score(sc, auto_layout=True)

    assert any(f.reason == "meter_missing" for f in res.flags)
    alto_bars = list(res.scores["Alto"].parts[0].getElementsByClass(stream.Measure))
    assert float(getattr(alto_bars[1], "paddingRight", 0.0) or 0.0) == pytest.approx(0.5)
    assert bool(getattr(alto_bars[2].editorial, "satb_overfull", False)) is True
    # Confirm Alto bar 3 actually ends with the rendered trailing Rest(2.5) crossing beat 4.0
    alto_b3_els = list(alto_bars[2].recurse().notesAndRests)
    assert alto_b3_els[-1].isRest and float(alto_b3_els[-1].quarterLength) == pytest.approx(2.5)

    out_xml = tmp_path / "meter_missing_b19_3.musicxml"
    write_combined_satb(res, out_xml)

    # Direct XML inspection: all 4 parts must have exact per-bar durations [4.0, 3.5, 5.0, 4.0]
    raw_durs = _raw_xml_part_bar_durations(out_xml)
    assert list(raw_durs.keys()) == ["Soprano", "Alto", "Tenor", "Bass"]
    for role, bars in raw_durs.items():
        assert bars == pytest.approx([4.0, 3.5, 5.0, 4.0]), f"{role}: {bars}"

    # Verify zero hidden rests added to bar 2 and zero rest spilled from bar 3 into bar 4
    root = ET.parse(out_xml).getroot()
    for part in root.findall("part"):
        ms = part.findall("measure")
        assert len(ms) == 4
        for n_el in ms[1].findall("note"):
            assert n_el.get("print-object") != "no"
        b4_notes = ms[3].findall("note")
        assert len(b4_notes) == 1 and b4_notes[0].find("rest") is None

    # Re-parse via music21 converter and confirm bar lengths & elements match across all 4 parts
    parsed = converter.parse(str(out_xml))
    assert len(parsed.parts) == 4
    for p in parsed.parts:
        p_bars = list(p.getElementsByClass(stream.Measure))
        assert [sum(float(el.quarterLength) for el in m.notesAndRests) for m in p_bars] == pytest.approx(
            [4.0, 3.5, 5.0, 4.0]
        )
        assert [float(m.duration.quarterLength) for m in p_bars] == pytest.approx([4.0, 3.5, 5.0, 4.0])
