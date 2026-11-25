"""Shared helpers for building choral MusicXML fixtures."""

from __future__ import annotations

from music21 import clef, key, meter, note, stream


def measure(
    number: int,
    *elements,
    time_sig: str | None = "4/4",
    insert_clef: clef.Clef | None = None,
) -> stream.Measure:
    m = stream.Measure(number=number)
    if time_sig and number == 1:
        m.insert(0, meter.TimeSignature(time_sig))
    if insert_clef is not None:
        m.insert(0, insert_clef)
    offset = 0.0
    for el in elements:
        m.insert(offset, el)
        offset += el.quarterLength
    return m


def two_voice_measure(
    number: int,
    upper_notes: list[note.GeneralNote],
    lower_notes: list[note.GeneralNote],
    *,
    time_sig: str | None = "4/4",
) -> stream.Measure:
    """Build a measure with explicit music21 Voices id 1 and 2."""
    m = stream.Measure(number=number)
    if time_sig and number == 1:
        m.insert(0, meter.TimeSignature(time_sig))

    v1 = stream.Voice(id="1")
    v2 = stream.Voice(id="2")
    off = 0.0
    for el in upper_notes:
        v1.insert(off, el)
        off += el.quarterLength
    off = 0.0
    for el in lower_notes:
        v2.insert(off, el)
        off += el.quarterLength
    m.insert(0, v1)
    m.insert(0, v2)
    return m


def make_two_staff_score(
    treble_measures: list[stream.Measure],
    bass_measures: list[stream.Measure],
) -> stream.Score:
    sa = stream.Part(id="SA")
    sa.partName = "Soprano Alto"
    sa.insert(0, clef.TrebleClef())
    sa.insert(0, key.KeySignature(0))
    for m in treble_measures:
        sa.append(m)

    tb = stream.Part(id="TB")
    tb.partName = "Tenor Bass"
    tb.insert(0, clef.BassClef())
    tb.insert(0, key.KeySignature(0))
    for m in bass_measures:
        tb.append(m)

    sc = stream.Score()
    sc.insert(0, sa)
    sc.insert(0, tb)
    return sc


def pitches_in(score: stream.Score) -> list[str]:
    """Return sounding pitch names (with octave) in order."""
    out = []
    for n in score.flatten().notes:
        if len(n.pitches) == 1:
            out.append(n.pitches[0].nameWithOctave)
        else:  # divisi chord: high -> low joined with '+'
            out.append("+".join(p.nameWithOctave for p in sorted(n.pitches, key=lambda p: -p.ps)))
    return out
