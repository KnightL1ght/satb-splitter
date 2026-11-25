"""Batch 9: all-staff forms, explicit PDF header tempo, open endings."""
from copy import deepcopy
import json
import xml.etree.ElementTree as ET

import fitz
import pytest
from music21 import clef, converter, note, spanner, stream

from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.export.writer import write_satb_files
from satb_splitter.omr.header_tempo import (
    parse_header_tempo, pdf_header_tempos, write_tempo_sidecars, tempo_sidecar_path,
)
from satb_splitter.omr.preprocess import whiteout_top_band
from satb_splitter.split.engine import split_score, split_file
from satb_splitter.split.directions import restore_ending_types
from test_batch8_fixes import _sa_tb_with_form, _volta_spans, _metros, _has_tempo_word
from test_batch3_fixes import _bar
from test_p0_fixes import n, part_with, score_of


def _top_score():
    solo = part_with([_bar(i, n("D5", 4), ts="4/4" if i == 1 else None)
                      for i in range(1, 8)], clef.TrebleClef(), name="Solo")
    ms = list(solo.getElementsByClass(stream.Measure))
    rb = spanner.RepeatBracket(ms[5:7], number=1)
    rb.editorial.satb_ending_type = "discontinue"
    solo.insert(0, rb)
    sa = part_with([_bar(i, n("G4", 4), ts="4/4" if i == 1 else None)
                    for i in range(1, 8)], clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(i, n("C3", 4), ts="4/4" if i == 1 else None)
                    for i in range(1, 8)], clef.BassClef(), name="T,B")
    return score_of(solo, sa, tb)


def test_solo_volta_on_every_voice_and_merged_export(tmp_path):
    source = _top_score()
    baseline = deepcopy(source)
    for p in baseline.parts:
        for rb in list(p.getElementsByClass(spanner.RepeatBracket)):
            p.remove(rb)
    expected = split_score(baseline, auto_layout=True)
    result = split_score(source, auto_layout=True)
    for role in result.scores:
        assert ("1", 6, 7) in _volta_spans(result.scores[role])
        assert [(str(n), n.quarterLength) for n in result.scores[role].flatten().notesAndRests] == [
            (str(n), n.quarterLength) for n in expected.scores[role].flatten().notesAndRests]
    written = write_satb_files(result, tmp_path, "top")
    for key, path in written.items():
        loaded = converter.parse(str(path))
        for part in loaded.parts:
            assert ("1", 6, 7) in _volta_spans(part)
        for xp in ET.parse(path).getroot().findall("part"):
            assert xp.findall("measure")[6].find("barline/ending").get("type") == "discontinue"
        restore_ending_types(loaded, path)
        for part in loaded.parts:
            assert all(rb.editorial.satb_ending_type == "discontinue"
                       for rb in part.getElementsByClass(spanner.RepeatBracket))
    # The actual file-to-split-to-merge path restores type lost by converter.
    source_path = tmp_path / "source.musicxml"
    source.write("musicxml", fp=str(source_path))
    from satb_splitter.split.directions import preserve_written_ending_types
    preserve_written_ending_types(source, source_path)
    paths, _, _ = split_and_merge_mxl_files([source_path], tmp_path / "merged", "form")
    for path in paths.values():
        for xp in ET.parse(path).getroot().findall("part"):
            assert xp.findall("measure")[6].find("barline/ending").get("type") == "discontinue"


@pytest.mark.parametrize("text,beat,dots,bpm", [
    ("Andante quarter=75", "quarter", 0, 75),
    ("Andante \u2669=75", "quarter", 0, 75),
    ("Moderato dotted quarter = 60", "quarter", 1, 60),
    ("Allegro eighth. = 120", "eighth", 1, 120),
    ("Largo half-note = 40", "half", 0, 40),
    ("quarter..=60", "quarter", 2, 60),
    ("\U0001d160 = 100", "eighth", 0, 100),
])
def test_explicit_tempo_parser(text, beat, dots, bpm):
    got = parse_header_tempo(text)[0]
    assert (got["beat_unit"], got["dots"], got["number"]) == (beat, dots, bpm)


def test_no_invented_metronome():
    assert parse_header_tempo("Andante")[0]["number"] is None
    assert not parse_header_tempo("Psalm 75; unknown glyph =75; 75 BPM")
    assert not parse_header_tempo("?=75")


def _pdf(path, text="Andante quarter=75"):
    d = fitz.open()
    p = d.new_page(width=600, height=800)
    p.insert_text((50, 60), text)
    # First real staff below the header, used by the header bounding logic.
    for y in range(180, 221, 10):
        p.draw_line((50, y), (550, y), width=1)
    # A lyric with a tempo word under the first staff must not be promoted.
    p.insert_text((50, 260), "Allegro quarter=999")
    d.save(path)
    d.close()


@pytest.mark.parametrize("text,beat,dots,bpm", [
    ("Andante quarter=75", "quarter", 0, 75),
    ("Moderato dotted quarter=60", "quarter", 1, 60),
    ("Allegro eighth=120", "eighth", 0, 120),
])
def test_pdf_before_whiteout_through_file_split_merge(tmp_path, text, beat, dots, bpm):
    original = tmp_path / "original.pdf"
    _pdf(original, text)
    marks = pdf_header_tempos(original)
    assert len(marks) == 1
    assert (marks[0]["beat_unit"], marks[0]["dots"], marks[0]["number"]) == (beat, dots, bpm)
    whiteout_top_band(original, tmp_path / "clean.pdf")
    source = _sa_tb_with_form()
    for p in source.parts:
        for m in p.getElementsByClass(stream.Measure):
            from music21 import tempo
            for el in list(m.getElementsByClass(tempo.MetronomeMark)):
                m.remove(el)
    mxl = tmp_path / "page_01.musicxml"
    source.write("musicxml", fp=str(mxl))
    logs = write_tempo_sidecars(original, [mxl])
    assert logs and tempo_sidecar_path(mxl).is_file()
    paths, _, _ = split_and_merge_mxl_files([mxl], tmp_path / "out", "header")
    for path in paths.values():
        for xp in ET.parse(path).getroot().findall("part"):
            metro = xp.find("measure/direction/direction-type/metronome")
            assert metro.findtext("beat-unit") == beat
            assert len(metro.findall("beat-unit-dot")) == dots
            assert float(metro.findtext("per-minute")) == bpm
        for p in converter.parse(str(path)).parts:
            assert _has_tempo_word(p, text.split()[0])


def test_word_only_pdf_no_numeric_tempo(tmp_path):
    pdf = tmp_path / "word.pdf"
    _pdf(pdf, "Andante")
    assert pdf_header_tempos(pdf)[0]["number"] is None
    source = tmp_path / "page.musicxml"
    _sa_tb_with_form().write("musicxml", fp=str(source))
    write_tempo_sidecars(pdf, [source])
    assert json.loads(tempo_sidecar_path(source).read_text())["tempos"][0]["number"] is None


def test_pipeline_original_header_sidecar_without_omr_geometry(tmp_path):
    from satb_splitter.omr.pipeline import _attach_pdf_markers
    from satb_splitter.omr.audiveris import AudiverisRun
    (tmp_path / "pages").mkdir()
    original = tmp_path / "pages/page_01.pdf"
    _pdf(original)
    mxl = tmp_path / "page_01.musicxml"
    _sa_tb_with_form().write("musicxml", fp=str(mxl))
    run = AudiverisRun(input_path=original, success=True, mxl_paths=[mxl])
    _attach_pdf_markers(tmp_path, tmp_path / "pages_clean/page_01.pdf", run, None)
    assert tempo_sidecar_path(mxl).is_file()
    assert any("PDF header tempo" in x for x in run.marker_log)


def test_preprocess_caches_original_tempo_before_whiteout(tmp_path):
    from satb_splitter.omr.preprocess import prepare_pages_for_omr
    pdf = tmp_path / "score.pdf"
    _pdf(pdf)
    paths = prepare_pages_for_omr(pdf, tmp_path / "work")
    cache = tmp_path / "work/pages/page_01.header-tempo.json"
    assert paths[0].parent.name == "pages_clean"
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["tempos"][0]["text"] == "Andante"
    assert data["tempos"][0]["number"] == 75
