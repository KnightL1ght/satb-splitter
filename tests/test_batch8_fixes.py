"""Regression tests for Orchestrator batch 8: keep tempo and repeats.

Tempo words, metronome marks (beat-unit = BPM), repeat barlines, 1st/2nd
endings, and segno marks present in the source MusicXML must appear on
every exported voice and on the combined SATB score. Notes and meters
are unchanged.
"""

from __future__ import annotations

from music21 import bar, clef, converter, expressions, note, repeat, spanner, stream, tempo

from satb_splitter.export.merge import merge_parts, split_and_merge_mxl_files
from satb_splitter.export.writer import write_satb_files
from satb_splitter.split.engine import split_score

from test_batch3_fixes import _bar
from test_p0_fixes import n, part_with, score_of


def _metro(number: int, referent_ql: float = 1.0, text: str | None = None):
    if abs(referent_ql - 1.5) < 1e-6:
        referent = note.Note(type="quarter", dots=1)
    elif abs(referent_ql - 0.5) < 1e-6:
        referent = note.Note(type="eighth")
    else:
        referent = note.Note(type="quarter")
    return tempo.MetronomeMark(number=number, referent=referent, text=text)


def _metros(score_or_part):
    parts = list(score_or_part.parts) if hasattr(score_or_part, "parts") and score_or_part.parts else [score_or_part]
    out = []
    for part in parts:
        for el in part.recurse().getElementsByClass(tempo.MetronomeMark):
            ref = None
            try:
                ref = float(el.referent.quarterLength) if el.referent is not None else None
            except Exception:
                ref = None
            out.append((el.number, ref, getattr(el, "text", None)))
    return out


def _has_tempo_word(score_or_part, word: str) -> bool:
    word_l = word.lower()
    parts = list(score_or_part.parts) if hasattr(score_or_part, "parts") and score_or_part.parts else [score_or_part]
    for part in parts:
        for el in part.recurse().getElementsByClass(tempo.TempoIndication):
            if word_l in str(getattr(el, "text", "") or "").lower():
                return True
        for el in part.recurse().getElementsByClass(expressions.TextExpression):
            if word_l in str(el.content or "").lower():
                return True
    return False


def _repeat_dirs(score_or_part):
    part = score_or_part.parts[0] if hasattr(score_or_part, "parts") and score_or_part.parts else score_or_part
    out = {}
    for i, m in enumerate(part.getElementsByClass(stream.Measure), 1):
        left = m.leftBarline
        right = m.rightBarline
        ld = left.direction if isinstance(left, bar.Repeat) else None
        rd = right.direction if isinstance(right, bar.Repeat) else None
        if ld or rd:
            out[i] = (ld, rd)
    return out


def _volta_spans(score_or_part):
    part = score_or_part.parts[0] if hasattr(score_or_part, "parts") and score_or_part.parts else score_or_part
    ms = list(part.getElementsByClass(stream.Measure))
    spans = []
    for rb in part.getElementsByClass(spanner.RepeatBracket):
        idxs = [ms.index(el) + 1 for el in rb.getSpannedElements() if el in ms]
        if idxs:
            spans.append((str(rb.number), min(idxs), max(idxs)))
    return sorted(spans)


def _sa_tb_with_form():
    """Two-staff score: tempo + start repeat on bar 1 of the treble only."""
    sa1 = _bar(1, n("G4", 4), ts="4/4")
    sa1.insert(0, _metro(75, 1.0, "Andante"))
    sa1.leftBarline = bar.Repeat(direction="start")
    sa2 = _bar(2, n("A4", 4))
    sa2.rightBarline = bar.Repeat(direction="end")
    sa3 = _bar(3, n("B4", 4))
    sa3.insert(0, repeat.Segno())
    sa = part_with([sa1, sa2, sa3], clef.TrebleClef(), name="S,A")
    ms = list(sa.getElementsByClass(stream.Measure))
    sa.insert(0, spanner.RepeatBracket([ms[1]], number=1))
    sa.insert(0, spanner.RepeatBracket([ms[2]], number=2))

    tb = part_with(
        [
            _bar(1, n("C3", 4), ts="4/4"),
            _bar(2, n("D3", 4)),
            _bar(3, n("E3", 4)),
        ],
        clef.BassClef(),
        name="T,B",
    )
    return score_of(sa, tb)


def test_tempo_and_metronome_copied_to_all_voices_and_satb(tmp_path):
    res = split_score(_sa_tb_with_form(), auto_layout=True)
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        metros = _metros(res.scores[role])
        assert any(num == 75 and ref == 1.0 for num, ref, _txt in metros), (role, metros)
        assert _has_tempo_word(res.scores[role], "Andante"), role

    written = write_satb_files(res, tmp_path, "tempo")
    for key in ("Soprano", "Alto", "Tenor", "Bass", "SATB"):
        sc = converter.parse(str(written[key]))
        targets = list(sc.parts) if key == "SATB" else [sc]
        assert targets
        for part in targets:
            metros = _metros(part)
            assert any(num == 75 and ref == 1.0 for num, ref, _txt in metros), (key, metros)
            assert _has_tempo_word(part, "Andante"), key


def test_metronome_keeps_non_quarter_beat_unit(tmp_path):
    sa1 = _bar(1, n("G4", 4), ts="4/4")
    sa1.insert(0, _metro(60, 1.5))
    sa2 = _bar(2, n("A4", 4))
    sa2.insert(0, _metro(120, 0.5))
    sa = part_with([sa1, sa2], clef.TrebleClef(), name="S,A")
    tb = part_with(
        [_bar(1, n("C3", 4), ts="4/4"), _bar(2, n("D3", 4))],
        clef.BassClef(),
        name="T,B",
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    sop = _metros(res.scores["Bass"])
    assert any(num == 60 and ref == 1.5 for num, ref, _t in sop), sop
    assert any(num == 120 and ref == 0.5 for num, ref, _t in sop), sop

    written = write_satb_files(res, tmp_path, "beatunit")
    bass = converter.parse(str(written["Bass"]))
    got = _metros(bass)
    assert any(num == 60 and ref == 1.5 for num, ref, _t in got), got
    assert any(num == 120 and ref == 0.5 for num, ref, _t in got), got


def test_repeats_voltas_and_segno_copied_to_all_voices_and_satb(tmp_path):
    res = split_score(_sa_tb_with_form(), auto_layout=True)
    for role in ("Soprano", "Alto", "Tenor", "Bass"):
        assert _repeat_dirs(res.scores[role]) == {1: ("start", None), 2: (None, "end")}, role
        assert _volta_spans(res.scores[role]) == [("1", 2, 2), ("2", 3, 3)], role
        assert list(res.scores[role].recurse().getElementsByClass(repeat.Segno)), role
        # notes themselves stay put
        pitches = [el.pitch.nameWithOctave for el in res.scores[role].flatten().notes]
        if role == "Soprano":
            assert pitches == ["G4", "A4", "B4"]
        elif role == "Bass":
            assert pitches == ["C3", "D3", "E3"]

    written = write_satb_files(res, tmp_path, "repeats")
    for key in ("Soprano", "Alto", "Tenor", "Bass", "SATB"):
        sc = converter.parse(str(written[key]))
        targets = list(sc.parts) if key == "SATB" else [sc]
        for part in targets:
            assert _repeat_dirs(part) == {1: ("start", None), 2: (None, "end")}, key
            assert _volta_spans(part) == [("1", 2, 2), ("2", 3, 3)], key
            assert list(part.recurse().getElementsByClass(repeat.Segno)), key


def test_merge_parts_keeps_repeats_and_tempo():
    """Page concatenation must not drop volta spanners or in-measure tempo."""
    def page(start_num: int, with_form: bool) -> stream.Part:
        m1 = _bar(start_num, n("C4", 4), ts="4/4")
        m2 = _bar(start_num + 1, n("D4", 4))
        if with_form:
            m1.insert(0, _metro(75, 1.0, "Andante"))
            m1.leftBarline = bar.Repeat(direction="start")
            m2.rightBarline = bar.Repeat(direction="end")
        p = stream.Part()
        p.append(m1)
        p.append(m2)
        if with_form:
            ms = list(p.getElementsByClass(stream.Measure))
            p.insert(0, spanner.RepeatBracket([ms[1]], number=1))
        return p

    merged = merge_parts([page(1, True), page(3, False)])
    ms = list(merged.getElementsByClass(stream.Measure))
    assert len(ms) == 4
    assert isinstance(ms[0].leftBarline, bar.Repeat) and ms[0].leftBarline.direction == "start"
    assert isinstance(ms[1].rightBarline, bar.Repeat) and ms[1].rightBarline.direction == "end"
    assert any(el.number == 75 for el in merged.recurse().getElementsByClass(tempo.MetronomeMark))
    assert _volta_spans(merged) == [("1", 2, 2)]


def test_split_and_merge_files_keep_tempo_and_repeats(tmp_path):
    """The GUI / omr_cli --split merge path keeps markings across pages."""
    page1 = _sa_tb_with_form()
    p1 = tmp_path / "page_01.musicxml"
    page1.write("musicxml", fp=str(p1))

    sa = part_with([_bar(1, n("C5", 4), ts="4/4")], clef.TrebleClef(), name="S,A")
    tb = part_with([_bar(1, n("C3", 4), ts="4/4")], clef.BassClef(), name="T,B")
    p2 = tmp_path / "page_02.musicxml"
    score_of(sa, tb).write("musicxml", fp=str(p2))

    written, log, combined = split_and_merge_mxl_files([p1, p2], tmp_path / "out", "merged")
    assert combined is not None
    sop = converter.parse(str(written["Soprano"]))
    assert any(num == 75 and ref == 1.0 for num, ref, _t in _metros(sop))
    assert _has_tempo_word(sop, "Andante")
    assert _repeat_dirs(sop)[1] == ("start", None)
    assert _repeat_dirs(sop)[2] == (None, "end")
    assert ("1", 2, 2) in _volta_spans(sop)
    assert ("2", 3, 3) in _volta_spans(sop)
    satb = converter.parse(str(written["SATB"]))
    assert len(satb.parts) == 4
    for part in satb.parts:
        assert any(num == 75 for num, _r, _t in _metros(part)), part.id
        assert _repeat_dirs(part)[1] == ("start", None)
