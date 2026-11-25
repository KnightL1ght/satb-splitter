"""Regression tests for Orchestrator batch 3: unnamed "Voice" staff (P0),
unison marker from the PDF text layer + staff-safe white-out (P1), chord /
divisi handling (P2, BUG-2) and 1-based flag bar numbers (P3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from music21 import chord, clef, converter, expressions, meter, note, stream

from satb_splitter.model.config import DivisiMode, GraftConfig, LayoutTemplate, SplitConfig
from satb_splitter.split.engine import parse_section_marker, split_file, split_score
from satb_splitter.split.layout import detect_layout

from helpers import pitches_in
from test_p0_fixes import n, part_with, score_of, voiced_measure


def _stem(el, d):
    el.stemDirection = d
    return el


def _bar(number, *els, ts=None, clef_obj=None, text=None):
    m = stream.Measure(number=number)
    if ts:
        m.insert(0, meter.TimeSignature(ts))
    if clef_obj is not None:
        m.insert(0, clef_obj)
    if text:
        m.insert(0, expressions.TextExpression(text))
    off = 0.0
    for el in els:
        m.insert(off, el)
        off += el.quarterLength
    return m


def _rest_bar(number, ts=None):
    return _bar(number, note.Rest(quarterLength=4), ts=ts)


def _sa_tb(sa_name="Voice", tb_name="T,B", bars=2):
    sa = part_with([_bar(i + 1, n("E5", 4), ts="4/4" if i == 0 else None) for i in range(bars)],
                   clef.TrebleClef(), name=sa_name)
    tb = part_with([_bar(i + 1, n("C3", 4), ts="4/4" if i == 0 else None) for i in range(bars)],
                   clef.BassClef(), name=tb_name)
    return score_of(sa, tb)


# ---------------------------------------------------------------------------
# P0: staff named "Voice" above "T,B" -> S/A (clef/position fallback)
# ---------------------------------------------------------------------------

def test_voice_named_treble_above_tb_becomes_sa():
    sc = _sa_tb("Voice", "T,B")
    det = detect_layout(sc)
    roles = {p.part_index: p.roles() for p in det.template.pairs}
    assert roles[0] == ["Soprano", "Alto"]
    assert roles[1] == ["Tenor", "Bass"]
    assert 0 not in det.template.ignore_part_indices
    assert "fallback" in det.reason
    res = split_score(sc, auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["E5", "E5"]
    assert pitches_in(res.scores["Alto"]) == ["E5", "E5"]
    assert not any(f.reason == "parts_ignored" for f in res.flags)


def test_unnamed_bass_below_sa_becomes_tb():
    det = detect_layout(_sa_tb("S,A", "Voice"))
    roles = {p.part_index: p.roles() for p in det.template.pairs}
    assert roles == {0: ["Soprano", "Alto"], 1: ["Tenor", "Bass"]}


def test_piano_named_staff_stays_ignored():
    det = detect_layout(_sa_tb("Piano", "T,B"))
    assert 0 in det.template.ignore_part_indices


def test_named_complete_layout_unchanged():
    det = detect_layout(_sa_tb("S,A", "T,B"))
    assert det.template.name == "name_based"
    assert det.reason.startswith("part names")


def test_solo_voice_above_choir_named_voice():
    """['Voice'(solo G), 'Voice'(G), 'T,B'] -> the staff directly above T,B is S/A."""
    solo = part_with([_bar(1, n("A4", 4), ts="4/4")], clef.TrebleClef(), name="Voice")
    sa = part_with([_bar(1, n("E5", 4), ts="4/4")], clef.TrebleClef(), name="Voice")
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    det = detect_layout(score_of(solo, sa, tb))
    roles = {p.part_index: p.roles() for p in det.template.pairs}
    assert roles.get(1) == ["Soprano", "Alto"]
    assert 0 in det.template.ignore_part_indices


# ---------------------------------------------------------------------------
# P1: unison marker (XML or PDF sidecar) on a single-staff system
# ---------------------------------------------------------------------------

def _unison_page(text_in_xml: str | None = None):
    """SA staff rests in bars 1-2 (single-staff system attached to the TB staff,
    treble clef), then a normal 2-staff choir bar 3."""
    sa = part_with([_rest_bar(1, ts="4/4"), _rest_bar(2)] + [_bar(i, n("E5", 4)) for i in (3, 4, 5)],
                   clef.TrebleClef(), name="Voice")
    tb = part_with(
        [
            _bar(1, n("G4", 2), n("A4", 2), ts="4/4", clef_obj=clef.TrebleClef(), text=text_in_xml),
            _bar(2, n("B4", 4)),
            _bar(3, n("C3", 4), clef_obj=clef.BassClef()),
            _bar(4, n("C3", 4)),
            _bar(5, n("C3", 4)),
        ],
        None, name="Voice",
    )
    return score_of(sa, tb)


def test_unmarked_single_staff_system_goes_to_sa_only():
    res = split_score(_unison_page(None), auto_layout=True)
    assert pitches_in(res.scores["Soprano"])[:3] == ["G4", "A4", "B4"]
    assert pitches_in(res.scores["Tenor"]) == ["C3", "C3", "C3"]


def test_xml_unison_marker_routes_all_bars_of_system_to_tb():
    res = split_score(_unison_page("unison"), auto_layout=True)
    assert pitches_in(res.scores["Soprano"])[:3] == ["G4", "A4", "B4"]
    # marker on bar 1 stays active for the whole single-staff run (bar 2 too)
    assert pitches_in(res.scores["Tenor"])[:3] == ["G3", "A3", "B3"]
    assert pitches_in(res.scores["Bass"])[:3] == ["G3", "A3", "B3"]


def _write(sc, tmp_path: Path, name="page_16.musicxml") -> Path:
    path = tmp_path / name
    sc.write("musicxml", fp=str(path))
    return path


def test_pdf_sidecar_unison_marker_applied(tmp_path: Path):
    path = _write(_unison_page(None), tmp_path)
    (tmp_path / "page_16.markers.json").write_text(
        json.dumps({"markers": [{"measure_index": 0, "text": "unison"}]}), encoding="utf-8"
    )
    res = split_file(path, auto_layout=True)
    assert pitches_in(res.scores["Tenor"])[:3] == ["G3", "A3", "B3"]
    assert any(f.reason == "pdf_marker" and f.measure == 1 for f in res.flags)


def test_pdf_sidecar_does_not_override_xml_marker(tmp_path: Path):
    path = _write(_unison_page("S,A"), tmp_path)
    (tmp_path / "page_16.markers.json").write_text(
        json.dumps({"markers": [{"measure_index": 0, "text": "unison"}]}), encoding="utf-8"
    )
    res = split_file(path, auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["C3", "C3", "C3"]  # S,A section: T/B rest
    assert not any(f.reason == "pdf_marker" for f in res.flags)


def test_solo_tutti_is_not_a_routing_marker():
    assert parse_section_marker("solo - tuti") is None
    assert parse_section_marker("solo · tutti") is None
    assert parse_section_marker("solo tenor").roles == ("Tenor",)
    assert parse_section_marker("unison").kind == "unison"


# ---------------------------------------------------------------------------
# P1: PDF text-layer marker extraction + placement on .omr geometry
# ---------------------------------------------------------------------------

fitz = pytest.importorskip("fitz")


def _staff_pdf(path: Path, staff_top_pt: float, *, marker: str | None = "unison",
               lyric_row: bool = True) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    for k in range(5):
        y = staff_top_pt + 4 * k
        page.draw_line((60, y), (560, y), color=(0, 0, 0), width=0.8)
    if marker:
        page.insert_text((150, staff_top_pt - 3), marker, fontsize=9.5, fontname="tiro")
    if lyric_row:
        x = 70
        for syl in ["Es", "te", "toţi", "pa", "ce", "toţi"]:
            page.insert_text((x, staff_top_pt + 35), syl, fontsize=10.2, fontname="tiro")
            x += 60
    doc.save(str(path))
    doc.close()
    return path


def test_pdf_marker_candidates_skip_lyrics(tmp_path: Path):
    from satb_splitter.omr.markers import pdf_marker_candidates

    words, w, h = pdf_marker_candidates(_staff_pdf(tmp_path / "p.pdf", 64))
    assert [x.text for x in words] == ["unison"]
    assert w == pytest.approx(595)


def test_place_marker_on_system_and_bar():
    from satb_splitter.omr.markers import OmrSheet, OmrSystem, PdfWord, place_markers

    scale = 2479 / 595
    sys1 = OmrSystem(top=64 * scale, bottom=84 * scale,
                     stacks=[(300, 700), (700, 1200), (1200, 1800), (1800, 2330)])
    sys2 = OmrSystem(top=150 * scale, bottom=200 * scale, stacks=[(300, 1200), (1200, 2330)])
    sheet = OmrSheet(width=2479, height=3508, interline=17, systems=[sys1, sys2])
    words = [
        PdfWord("unison", 144, 54, 169, 64),     # above system 1, bar 1
        PdfWord("S,A", 400, 140, 415, 149),      # above system 2, 2nd stack -> page bar 6
        PdfWord("T,B", 38, 160, 54, 170),        # staff label in the left margin: skipped
        PdfWord("solo", 300, 20, 320, 30),       # far above any system: unplaced
    ]
    placed, unplaced = place_markers(words, 595, sheet)
    got = [(p.text, p.measure_index) for p in placed]
    assert ("unison", 0) in got
    assert ("S,A", 5) in got
    assert all(t != "T,B" for t, _ in got)
    assert unplaced == ["solo"]


# ---------------------------------------------------------------------------
# P1 root cause: top white-out must never cover the first staff
# ---------------------------------------------------------------------------

def test_whiteout_clamped_above_first_staff(tmp_path: Path):
    from satb_splitter.omr.preprocess import first_staff_position, safe_whiteout_fraction

    pdf = _staff_pdf(tmp_path / "top.pdf", 64, marker=None, lyric_row=False)  # staff at ~7.6%
    pos = first_staff_position(pdf)
    assert pos is not None and pos[0] == pytest.approx(64 / 842, abs=0.004)
    eff, note_ = safe_whiteout_fraction(pdf, 0.12)
    assert eff < 64 / 842 and "clamped" in note_
    low = _staff_pdf(tmp_path / "low.pdf", 300, marker=None, lyric_row=False)
    assert safe_whiteout_fraction(low, 0.12) == (0.12, "")


def test_retry_plan_protected_then_tight_then_unclamped(tmp_path: Path):
    from satb_splitter.omr.pipeline import _plan_retries

    pdf = _staff_pdf(tmp_path / "p1.pdf", 211, marker=None, lyric_row=False)  # like Hard p1 (0.251)
    plan = _plan_retries(pdf, 0.12, (0.22, 0.30))
    fracs = [round(f, 3) for f, _, _ in plan]
    protects = [p for _, p, _ in plan]
    assert fracs[0] == 0.22 and protects[0]
    assert all(f < 211 / 842 for f, p, _ in plan if p)           # protected never reach the staff
    assert any("tight" in note_ for _, _, note_ in plan)
    assert plan[-1][1] is False and "WARNING" in plan[-1][2]     # last resort is flagged


# ---------------------------------------------------------------------------
# P2: BUG-2 chords kept (divisi), dropped only when truly unassignable
# ---------------------------------------------------------------------------

def test_divisi_chord_in_lower_voice_kept_whole():
    """Hard p8 b5-7: S=G4 (up), A = B3+D4 chord (down). Keep under KEEP_DIVISI; 1 note under Option B."""
    sa = part_with(
        [voiced_measure(1, {"1": [(0, _stem(n("G4", 4), "up"))],
                            "2": [(0, _stem(chord.Chord(["B3", "D4"], quarterLength=4), "down"))]},
                        ts="4/4")],
        clef.TrebleClef(), name="S,A",
    )
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["G4"]
    assert pitches_in(res.scores["Alto"]) == ["D4+B3"]
    assert not any(f.reason == "dropped_pitch" for f in res.flags)
    assert any(f.reason == "divisi_chord" for f in res.flags)

    res_b18 = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res_b18.scores["Soprano"]) == ["G4"]
    assert pitches_in(res_b18.scores["Alto"]) == ["B3"]


def test_chord_in_upper_voice_when_lower_busy_is_divisi_of_upper():
    sa = part_with(
        [voiced_measure(1, {"1": [(0, _stem(chord.Chord(["C5", "F5"], quarterLength=4), "up"))],
                            "2": [(0, _stem(n("F4", 4), "down"))]}, ts="4/4")],
        clef.TrebleClef(), name="S,A",
    )
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["F5+C5"]
    assert pitches_in(res.scores["Alto"]) == ["F4"]

    res_b18 = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res_b18.scores["Soprano"]) == ["F5"]
    assert pitches_in(res_b18.scores["Alto"]) == ["F4"]


def test_third_line_over_sustained_notes_still_flagged():
    """Easy bar 7: both lines hold long notes, a 3rd voice starts mid-note ->
    dropped + flagged when B15_F7 tie-split is disabled or sliver < 0.125q."""
    sa = part_with(
        [voiced_measure(1, {"1": [(0, _stem(n("C5", 4), "up"))],
                            "2": [(0, _stem(n("G4", 4), "down"))],
                            "3": [(0, note.Rest(quarterLength=1)), (1, n("E4", 1)),
                                  (2, note.Rest(quarterLength=2))]}, ts="4/4")],
        clef.TrebleClef(), name="S,A",
    )
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    res_off = split_score(score_of(sa, tb), SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)), auto_layout=True)
    assert any(f.reason == "dropped_pitch" and "E4" in f.detail for f in res_off.flags)

    # Sub-32nd sliver (offset 0.0625 < 0.125) still dropped + flagged even with B15_F7=True
    sa_sl = part_with(
        [voiced_measure(1, {"1": [(0, _stem(n("C5", 4), "up"))],
                            "2": [(0, _stem(n("G4", 4), "down"))],
                            "3": [(0, note.Rest(quarterLength=0.0625)), (0.0625, n("E4", 0.5)),
                                  (0.5625, note.Rest(quarterLength=3.4375))]}, ts="4/4")],
        clef.TrebleClef(), name="S,A",
    )
    res_sl = split_score(score_of(sa_sl, tb), auto_layout=True)
    assert any(f.reason == "dropped_pitch" and "E4" in f.detail for f in res_sl.flags)


def test_divisi_chord_written_to_musicxml(tmp_path: Path):
    from satb_splitter.export.writer import write_satb_files

    sa = part_with(
        [voiced_measure(1, {"1": [(0, _stem(n("G4", 4), "up"))],
                            "2": [(0, _stem(chord.Chord(["B3", "D4"], quarterLength=4), "down"))]},
                        ts="4/4")],
        clef.TrebleClef(), name="S,A",
    )
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI), auto_layout=True)
    written = write_satb_files(res, tmp_path, "t")
    alto = converter.parse(str(written["Alto"]))
    ch = [x for x in alto.flatten().notes]
    assert len(ch) == 1 and sorted(p.nameWithOctave for p in ch[0].pitches) == ["B3", "D4"]


# ---------------------------------------------------------------------------
# P3: flag bar numbers are 1-based positions (pickup numbered 0 by Audiveris)
# ---------------------------------------------------------------------------

def test_flag_measure_is_one_based_position_with_pickup():
    sa_ms = [
        voiced_measure(0, {"1": [(0, n("G4", 1))]}, ts="4/4"),
        _bar(1, n("C5", 4)),
        voiced_measure(2, {"1": [(0, _stem(n("G4", 4), "up"))],
                           "2": [(0, _stem(n("E4", 4), "down"))],
                           "3": [(0, note.Rest(quarterLength=1)), (1, n("C4", 1)),
                                 (2, note.Rest(quarterLength=2))]}),
    ]
    sa = part_with(sa_ms, clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(0, n("C3", 1), ts="4/4"), _bar(1, n("C3", 4)), _bar(2, n("C3", 4))],
                   clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)), auto_layout=True)
    drops = [f for f in res.flags if f.reason == "dropped_pitch"]
    assert drops and all(f.measure == 3 for f in drops)  # 3rd bar of the file, not "2"


def test_merge_flag_measures_are_merged_bar_numbers(tmp_path: Path):
    from satb_splitter.export.merge import split_and_merge_mxl_files

    def page(path):
        sa = part_with(
            [_bar(1, n("E5", 4), ts="4/4"),
             voiced_measure(2, {"1": [(0, _stem(n("G4", 4), "up"))],
                                "2": [(0, _stem(n("E4", 4), "down"))],
                                "3": [(0, note.Rest(quarterLength=1)), (1, n("C4", 1)),
                                      (2, note.Rest(quarterLength=2))]})],
            clef.TrebleClef(), name="S,A")
        tb = part_with([_bar(1, n("C3", 4), ts="4/4"), _bar(2, n("C3", 4))],
                       clef.BassClef(), name="T,B")
        score_of(sa, tb).write("musicxml", fp=str(path))
        return path

    a = page(tmp_path / "a.musicxml")
    b = page(tmp_path / "b.musicxml")
    _, _, res = split_and_merge_mxl_files([a, b], tmp_path / "out", "x", config=SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)))
    drops = sorted(f.measure for f in res.flags if f.reason == "dropped_pitch")
    assert drops == [2, 4]
    # Batch 12: the page-local label is the MXL measure number ("MXL m2"), not "bar 2"
    assert any("MXL m2]" in f.detail for f in res.flags if f.reason == "dropped_pitch")


def test_pickup_bar_written_without_trailing_rest(tmp_path: Path):
    from satb_splitter.export.writer import write_satb_files

    sa = part_with([_bar(0, n("G4", 1), ts="3/4"), _bar(1, n("C5", 3))], clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(0, n("C3", 1), ts="3/4"), _bar(1, n("C3", 3))], clef.BassClef(), name="T,B")
    res = split_score(score_of(sa, tb), auto_layout=True)
    written = write_satb_files(res, tmp_path, "p")
    for role in ("Soprano", "Bass"):
        first = list(converter.parse(str(written[role])).parts[0].getElementsByClass(stream.Measure))[0]
        assert [type(e).__name__ for e in first.notesAndRests] == ["Note"]
        assert first.paddingLeft == pytest.approx(2.0)


def test_mxl_measure_count_ignores_measure_numbering(tmp_path: Path):
    from satb_splitter.omr.markers import _mxl_measure_count

    xml = ('<score-partwise><part id="P1"><measure number="1"><print><measure-numbering>system'
           '</measure-numbering></print></measure><measure number="2"><measure-style/></measure>'
           '</part></score-partwise>')
    p = tmp_path / "x.musicxml"
    p.write_text(xml, encoding="utf-8")
    assert _mxl_measure_count(p) == 2


def test_plain_solo_on_treble8_single_staff_goes_to_tenor():
    """Hard p10 mvt2: 'solo' printed over a treble-8vb one-staff page."""
    p = part_with([_bar(1, n("A3", 4), ts="4/4", clef_obj=clef.Treble8vbClef(), text="solo"),
                   _bar(2, n("B3", 4))], None, name="Voice")
    res = split_score(score_of(p), auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["A3", "B3"]
    assert not any(f.reason == "solo_omitted" for f in res.flags)


def test_split_span_word_joined(tmp_path: Path):
    """'toţi' written as 'to' + 'ţ' + 'i' spans must still read as a marker."""
    from satb_splitter.omr.markers import _join_spans

    spans = [("to", ("Times", 8.9), (384.5, 59, 391.3, 69)),
             ("ţ", ("Music", 8.9), (391.3, 59, 393.8, 69)),
             ("i", ("Times", 8.9), (393.8, 59, 396.3, 69))]
    assert _join_spans(spans) == "toţi"
    assert parse_section_marker(_join_spans(spans)).kind == "unison"


def test_pdf_sidecar_skipped_when_omr_has_same_marker_next_bar(tmp_path: Path):
    path = _write(_unison_page("unison"), tmp_path)
    (tmp_path / "page_16.markers.json").write_text(
        json.dumps({"markers": [{"measure_index": 1, "text": "unison"}]}), encoding="utf-8"
    )
    res = split_file(path, auto_layout=True)
    assert not any(f.reason == "pdf_marker" for f in res.flags)
    assert pitches_in(res.scores["Tenor"])[:3] == ["G3", "A3", "B3"]


def test_mxl_system_starts(tmp_path: Path):
    from satb_splitter.omr.markers import _mxl_system_starts

    xml = ('<score-partwise><part id="P1"><measure number="1"><print><measure-numbering>system'
           '</measure-numbering></print></measure><measure number="2"></measure>'
           '<measure number="3"><print new-system="yes"/></measure></part></score-partwise>')
    p = tmp_path / "s.musicxml"
    p.write_text(xml, encoding="utf-8")
    assert _mxl_system_starts(p) == [0, 2]
