"""Unit tests for the SATB voice-split engine."""

from __future__ import annotations

from pathlib import Path

import pytest
from music21 import chord, clef, note, stream

from satb_splitter.export.writer import write_satb_files
from satb_splitter.model.config import (
    DivisiMode,
    LayoutTemplate,
    SplitConfig,
    UnisonMode,
)
from satb_splitter.split.engine import split_file, split_score

from helpers import (
    make_two_staff_score,
    measure,
    pitches_in,
    two_voice_measure,
)


# ---------------------------------------------------------------------------
# Explicit voices
# ---------------------------------------------------------------------------

def test_explicit_voices_split_to_satb():
    """Voice 1/2 on each staff map to S/A and T/B."""
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
    result = split_score(score)

    assert pitches_in(result.scores["Soprano"]) == ["C5"]
    assert pitches_in(result.scores["Alto"]) == ["A4"]
    assert pitches_in(result.scores["Tenor"]) == ["E3"]
    assert pitches_in(result.scores["Bass"]) == ["C3"]
    assert result.flags == []


# ---------------------------------------------------------------------------
# Chords (no voice tags) — pitch rank
# ---------------------------------------------------------------------------

def test_chord_pitch_rank_split():
    """Highest pitch → upper voice, lowest → lower voice."""
    treble = [
        measure(
            1,
            chord.Chord(["G4", "E4"], quarterLength=2),
            chord.Chord(["A4", "C4"], quarterLength=2),
            insert_clef=clef.TrebleClef(),
        )
    ]
    bass = [
        measure(
            1,
            chord.Chord(["D3", "G2"], quarterLength=4),
            insert_clef=clef.BassClef(),
        )
    ]
    score = make_two_staff_score(treble, bass)
    result = split_score(score)

    assert pitches_in(result.scores["Soprano"]) == ["G4", "A4"]
    assert pitches_in(result.scores["Alto"]) == ["E4", "C4"]
    assert pitches_in(result.scores["Tenor"]) == ["D3"]
    assert pitches_in(result.scores["Bass"]) == ["G2"]


def test_three_note_chord_keeps_middle_as_divisi():
    """Batch 3 (BUG-2 KEEP_DIVISI) + Batch 18 (Option B MONOPHONIC_AUTO default)."""
    treble = [
        measure(1, chord.Chord(["C5", "E4", "G4"], quarterLength=4))
    ]
    bass = [measure(1, note.Note("C3", quarterLength=4))]
    score = make_two_staff_score(treble, bass)

    result = split_score(score, SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI))
    assert pitches_in(result.scores["Soprano"]) == ["C5"]
    assert pitches_in(result.scores["Alto"]) == ["G4+E4"]
    assert any(f.reason == "divisi_chord" for f in result.flags)
    assert not any(f.reason in ("middle_pitches", "dropped_pitch") for f in result.flags)

    # Batch 18 Option B default: collapses to 1 pitch per voice cleanly
    mono_res = split_score(make_two_staff_score([measure(1, chord.Chord(["C5", "E4", "G4"], quarterLength=4))],
                                                [measure(1, note.Note("C3", quarterLength=4))]))
    assert pitches_in(mono_res.scores["Soprano"]) == ["C5"]
    assert pitches_in(mono_res.scores["Alto"]) == ["E4"]
    assert not any(f.reason == "divisi_chord" for f in mono_res.flags)
    assert len(mono_res.divisi_choices) == 1


# ---------------------------------------------------------------------------
# Unison (single note on shared staff)
# ---------------------------------------------------------------------------

def test_unison_duplicates_to_both_voices():
    treble = [measure(1, note.Note("G4", quarterLength=4))]
    bass = [measure(1, note.Note("C3", quarterLength=4))]
    score = make_two_staff_score(treble, bass)
    result = split_score(score, SplitConfig(unison_mode=UnisonMode.DUPLICATE_BOTH))

    assert pitches_in(result.scores["Soprano"]) == ["G4"]
    assert pitches_in(result.scores["Alto"]) == ["G4"]
    assert pitches_in(result.scores["Tenor"]) == ["C3"]
    assert pitches_in(result.scores["Bass"]) == ["C3"]


def test_unison_upper_only():
    treble = [measure(1, note.Note("G4", quarterLength=4))]
    bass = [measure(1, note.Rest(quarterLength=4))]
    score = make_two_staff_score(treble, bass)
    result = split_score(score, SplitConfig(unison_mode=UnisonMode.UPPER_ONLY))

    assert pitches_in(result.scores["Soprano"]) == ["G4"]
    assert pitches_in(result.scores["Alto"]) == []
    # Alto should have a rest (no pitches)
    alto_rests = list(result.scores["Alto"].flatten().getElementsByClass(note.Rest))
    assert len(alto_rests) >= 1


def test_unison_flag_review_mode():
    treble = [measure(1, note.Note("A4", quarterLength=4))]
    bass = [measure(1, note.Note("D3", quarterLength=4))]
    score = make_two_staff_score(treble, bass)
    result = split_score(score, SplitConfig(unison_mode=UnisonMode.FLAG_REVIEW))

    assert pitches_in(result.scores["Soprano"]) == ["A4"]
    assert pitches_in(result.scores["Alto"]) == ["A4"]
    assert any(f.reason == "unison" for f in result.flags)


# ---------------------------------------------------------------------------
# Four-staff open score
# ---------------------------------------------------------------------------

def test_four_staff_open_template():
    parts = []
    pitches = ["C5", "A4", "E3", "C3"]
    names = ["S", "A", "T", "B"]
    for i, (p, name) in enumerate(zip(pitches, names)):
        part = stream.Part(id=name)
        part.append(measure(1, note.Note(p, quarterLength=4)))
        parts.append(part)
    score = stream.Score()
    for part in parts:
        score.insert(0, part)

    cfg = SplitConfig(template=LayoutTemplate.four_staff_open())
    result = split_score(score, cfg)

    assert pitches_in(result.scores["Soprano"]) == ["C5"]
    assert pitches_in(result.scores["Alto"]) == ["A4"]
    assert pitches_in(result.scores["Tenor"]) == ["E3"]
    assert pitches_in(result.scores["Bass"]) == ["C3"]


# ---------------------------------------------------------------------------
# Export + round-trip via MusicXML
# ---------------------------------------------------------------------------

def test_write_musicxml_and_reload(tmp_path: Path):
    treble = [
        two_voice_measure(
            1,
            [note.Note("D5", quarterLength=2), note.Note("E5", quarterLength=2)],
            [note.Note("B4", quarterLength=4)],
        )
    ]
    bass = [
        two_voice_measure(
            1,
            [note.Note("G3", quarterLength=4)],
            [note.Note("G2", quarterLength=4)],
        )
    ]
    score = make_two_staff_score(treble, bass)
    # Persist source
    src = tmp_path / "source.musicxml"
    score.write("musicxml", fp=str(src))

    result = split_file(src)
    written = write_satb_files(result, tmp_path / "out", "Hymn")

    assert {"Soprano", "Alto", "Tenor", "Bass", "SATB"} <= set(written)
    for role, path in written.items():
        assert path.is_file()
        reloaded = split_file(path)  # monophonic; just ensure parse works
        assert reloaded.source_part_count >= 1

    # Spot-check pitches after round-trip write
    from music21 import converter

    sop = converter.parse(str(written["Soprano"]))
    names = [n.pitch.nameWithOctave for n in sop.flatten().notes]
    assert names == ["D5", "E5"]


def test_cli_main(tmp_path: Path):
    treble = [measure(1, chord.Chord(["C5", "A4"], quarterLength=4))]
    bass = [measure(1, chord.Chord(["E3", "C3"], quarterLength=4))]
    score = make_two_staff_score(treble, bass)
    src = tmp_path / "cli_in.musicxml"
    score.write("musicxml", fp=str(src))

    from satb_splitter.cli import main

    out = tmp_path / "cli_out"
    rc = main([str(src), "-o", str(out), "-q"])
    assert rc == 0
    assert (out / "cli_in_Soprano.musicxml").is_file()
    assert (out / "cli_in_Alto.musicxml").is_file()
    assert (out / "cli_in_Tenor.musicxml").is_file()
    assert (out / "cli_in_Bass.musicxml").is_file()


def test_missing_input_cli():
    from satb_splitter.cli import main

    rc = main(["Z:/no/such/file.musicxml", "-q"])
    assert rc == 1


def test_template_json_load():
    t = LayoutTemplate.by_name("two_staff_satb")
    assert len(t.pairs) == 2
    assert t.pairs[0].upper == "Soprano"
    assert t.pairs[0].lower == "Alto"

    custom = LayoutTemplate.from_dict(t.to_dict())
    assert custom.name == t.name


def test_combined_satb_has_four_parts(tmp_path: Path):
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
    result = split_score(score)
    from satb_splitter.export.writer import write_combined_satb
    from music21 import converter

    path = write_combined_satb(result, tmp_path / "all.musicxml")
    loaded = converter.parse(str(path))
    names = [p.partName for p in loaded.parts]
    assert names == ["Soprano", "Alto", "Tenor", "Bass"]
    assert [n.pitch.nameWithOctave for n in loaded.parts[0].flatten().notes] == ["C5"]
    assert [n.pitch.nameWithOctave for n in loaded.parts[3].flatten().notes] == ["C3"]
