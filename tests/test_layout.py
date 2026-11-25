"""Tests for automatic layout detection."""

from __future__ import annotations

from music21 import note, stream

from satb_splitter.model.config import LayoutTemplate
from satb_splitter.split.engine import split_score
from satb_splitter.split.layout import detect_layout
from helpers import make_two_staff_score, measure, pitches_in, two_voice_measure


def test_detect_two_staff():
    score = make_two_staff_score(
        [measure(1, note.Note("C5", quarterLength=4))],
        [measure(1, note.Note("C3", quarterLength=4))],
    )
    # helpers set part names "Soprano Alto" / "Tenor Bass" → name-based
    det = detect_layout(score)
    assert det.part_count == 2
    assert det.template.name in ("two_staff_satb", "name_based")
    assert {p.upper for p in det.template.pairs} >= {"Soprano", "Tenor"}


def test_detect_two_staff_anonymous():
    sc = stream.Score()
    for i, pitch in enumerate(["C5", "C3"]):
        p = stream.Part(id=f"P{i}")
        p.partName = f"Part {i + 1}"
        p.append(measure(1, note.Note(pitch, quarterLength=4)))
        sc.insert(0, p)
    det = detect_layout(sc)
    assert det.template.name == "two_staff_satb"


def test_detect_four_staff():
    sc = stream.Score()
    for name, pitch in [
        ("S", "C5"),
        ("A", "A4"),
        ("T", "E3"),
        ("B", "C3"),
    ]:
        p = stream.Part(id=name)
        p.partName = name
        p.append(measure(1, note.Note(pitch, quarterLength=4)))
        sc.insert(0, p)
    det = detect_layout(sc)
    assert det.template.name in ("four_staff_open", "name_based")
    assert det.part_count == 4


def test_detect_three_staff():
    sc = stream.Score()
    for i in range(3):
        p = stream.Part(id=f"P{i}")
        p.partName = f"Part {i}"
        p.append(measure(1, note.Note("C4", quarterLength=4)))
        sc.insert(0, p)
    det = detect_layout(sc)
    assert det.template.name == "three_staff_satb"
    assert len(det.template.pairs) == 3


def test_detect_single_staff():
    sc = stream.Score()
    p = stream.Part()
    p.append(measure(1, note.Note("G4", quarterLength=4)))
    sc.insert(0, p)
    det = detect_layout(sc)
    assert det.template.name == "single_staff_sa"


def test_detect_by_part_names_sa_tb():
    score = make_two_staff_score(
        [measure(1, note.Note("C5", quarterLength=4))],
        [measure(1, note.Note("C3", quarterLength=4))],
    )
    score.parts[0].partName = "SA"
    score.parts[1].partName = "T,B"
    det = detect_layout(score)
    assert det.template.name == "name_based"
    assert det.template.pairs[0].upper == "Soprano"
    assert det.template.pairs[0].lower == "Alto"
    assert det.template.pairs[1].upper == "Tenor"
    assert det.template.pairs[1].lower == "Bass"


def test_auto_layout_split():
    treble = [
        two_voice_measure(
            1,
            [note.Note("C5", quarterLength=4)],
            [note.Note("A4", quarterLength=4)],
        )
    ]
    bass = [
        two_voice_measure(
            1,
            [note.Note("E3", quarterLength=4)],
            [note.Note("C3", quarterLength=4)],
        )
    ]
    score = make_two_staff_score(treble, bass)
    result = split_score(score, auto_layout=True)
    assert pitches_in(result.scores["Soprano"]) == ["C5"]
    assert pitches_in(result.scores["Alto"]) == ["A4"]
    assert any(f.reason == "layout_auto" for f in result.flags)


def test_template_presets():
    assert LayoutTemplate.three_staff_satb().name == "three_staff_satb"
    assert LayoutTemplate.single_staff_sa().name == "single_staff_sa"
    assert LayoutTemplate.by_name("three_staff_satb").pairs[1].upper == "Tenor"
