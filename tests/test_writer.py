"""Regression tests for MusicXML writer complex duration handling."""

from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from music21 import meter, note, stream

from satb_splitter.export import writer as satb_writer


def test_write_musicxml_splits_full_measure_complex_rest_normal_score(tmp_path: Path):
    """Full-measure complex Rests (fullMeasure='auto') export without MusicXMLExportException."""
    sc = stream.Score()
    part = stream.Part(id="P1")
    m1 = stream.Measure(number=1)
    m1.append(meter.TimeSignature("11/16"))  # barDuration = 2.75 QL (complex)
    rest = note.Rest(quarterLength=2.75)
    assert rest.duration.type == "complex"
    assert rest.fullMeasure == "auto"
    m1.append(rest)
    part.append(m1)
    sc.insert(0, part)

    out = tmp_path / "complex_normal.musicxml"
    satb_writer.write_musicxml(sc, out)

    assert out.is_file()
    root = ET.parse(out).getroot()
    rests = root.findall(".//measure/note[rest]")
    assert len(rests) >= 2
    # Caller's score was not mutated in place
    assert rest.duration.type == "complex"
    assert rest.fullMeasure == "auto"


def test_write_musicxml_splits_complex_duration_in_overfull_score(tmp_path: Path):
    """Overfull bars with complex durations export cleanly via _NoRebarExporter."""
    sc = stream.Score()
    part = stream.Part(id="P1")
    m1 = stream.Measure(number=1)
    m1.editorial.satb_overfull = True
    m1.append(meter.TimeSignature("4/4"))
    m1.append(note.Note("C4", quarterLength=3.5))
    complex_rest = note.Rest(quarterLength=1.375)
    assert complex_rest.duration.type == "complex"
    m1.append(complex_rest)

    m2 = stream.Measure(number=2)
    m2.append(note.Note("D4", quarterLength=4.0))
    part.append([m1, m2])
    sc.insert(0, part)

    out = tmp_path / "complex_overfull.musicxml"
    satb_writer.write_musicxml(sc, out)

    assert out.is_file()
    root = ET.parse(out).getroot()
    measures = root.findall("part/measure")
    assert len(measures) == 2
    # Bar 2 must still hold only D4 (no cross-bar re-barring)
    m2_notes = measures[1].findall("note")
    assert len(m2_notes) == 1
    assert m2_notes[0].find("rest") is None
