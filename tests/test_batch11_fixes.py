"""Batch 11: duplicate measure numbers, fermatas, final barline, metronome,
underfull bars, Easy bar 7 Bass, per-bar REVIEW, .mscz export, top-staff
Soprano routing. No live Audiveris or MuseScore (subprocess is mocked)."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import xml.etree.ElementTree as ET
import zipfile

import pytest
from music21 import bar, clef, expressions, meter, note, chord, note as m21note, stream, tie

from satb_splitter.musicxml_validation import validate_musicxml, load_validated_score
from satb_splitter.split.engine import split_file, split_score, sanitize_ties
from satb_splitter.export.writer import write_satb_files, write_review_report
from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.model.config import GraftConfig, ReviewFlag, SplitConfig, SplitResult


# ---------------------------------------------------------------- helpers
def _xml(path, numbers_per_part, *, last_barline=None, metronome=None, sound_tempo=None,
         fermata_rest_bar=None, per_minute=None):
    """Two-part SA/TB page, 4/4, one whole note (or rest) per bar."""
    root = ET.Element('score-partwise', version='4.0')
    pl = ET.SubElement(root, 'part-list')
    for i in range(len(numbers_per_part)):
        sp = ET.SubElement(pl, 'score-part', id=f'P{i+1}')
        ET.SubElement(sp, 'part-name').text = ['SA', 'TB'][i]
    for i, numbers in enumerate(numbers_per_part):
        p = ET.SubElement(root, 'part', id=f'P{i+1}')
        for k, num in enumerate(numbers):
            m = ET.SubElement(p, 'measure', number=num)
            if k == 0:
                a = ET.SubElement(m, 'attributes')
                ET.SubElement(a, 'divisions').text = '1'
                t = ET.SubElement(a, 'time')
                ET.SubElement(t, 'beats').text = '4'
                ET.SubElement(t, 'beat-type').text = '4'
                c = ET.SubElement(a, 'clef')
                ET.SubElement(c, 'sign').text = 'G' if i == 0 else 'F'
                ET.SubElement(c, 'line').text = '2' if i == 0 else '4'
                if i == 0 and per_minute is not None:
                    d = ET.SubElement(m, 'direction', placement='above')
                    dt = ET.SubElement(d, 'direction-type')
                    ET.SubElement(dt, 'words').text = 'Andante '
                    dt2 = ET.SubElement(d, 'direction-type')
                    mt = ET.SubElement(dt2, 'metronome')
                    ET.SubElement(mt, 'beat-unit').text = 'quarter'
                    ET.SubElement(mt, 'per-minute').text = per_minute
                    if sound_tempo is not None:
                        ET.SubElement(d, 'sound', tempo=str(sound_tempo))
            n = ET.SubElement(m, 'note')
            if fermata_rest_bar is not None and k == fermata_rest_bar and i == 0:
                ET.SubElement(n, 'rest')
                ET.SubElement(n, 'duration').text = '4'
                ET.SubElement(n, 'type').text = 'whole'
                nt = ET.SubElement(n, 'notations')
                ET.SubElement(nt, 'fermata', type='upright')
            else:
                pitch = ET.SubElement(n, 'pitch')
                ET.SubElement(pitch, 'step').text = ['C', 'D', 'E', 'F', 'G', 'A'][k % 6]
                ET.SubElement(pitch, 'octave').text = '5' if i == 0 else '3'
                ET.SubElement(n, 'duration').text = '4'
                ET.SubElement(n, 'type').text = 'whole'
            if last_barline and k == len(numbers) - 1:
                bl = ET.SubElement(m, 'barline', location='right')
                ET.SubElement(bl, 'bar-style').text = last_barline
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


def _bars(result, role):
    return list(result.scores[role].parts[0].getElementsByClass(stream.Measure))


def _m(ts=None, clef_=None, *voices, number=1):
    m = stream.Measure(number=number)
    if ts:
        m.insert(0, meter.TimeSignature(ts))
    if clef_:
        m.insert(0, clef_)
    for vid, items in voices:
        v = stream.Voice(id=vid)
        for off, el in items:
            v.insert(off, el)
        m.insert(0, v)
    return m


def _n(name, ql, stem=None, lyric=None):
    n = note.Note(name, quarterLength=ql)
    if stem:
        n.stemDirection = stem
    if lyric:
        n.addLyric(lyric)
    return n


# ------------------------------------------------- 1) duplicate measure numbers
def test_duplicate_numbers_accepted_when_all_parts_agree(tmp_path):
    path = _xml(tmp_path / 'dup.musicxml', [['1', '1', '2', '3']] * 2)
    report = validate_musicxml(path)
    assert not report.fatal
    dups = [f for f in report.flags if f.reason == 'xml_duplicate_measure']
    assert len(dups) == 1 and dups[0].measure == 2
    assert [m.get('number') for m in report.root.find('part').findall('measure')] == ['1', '2', '3', '4']
    result = split_file(path)
    assert all(len(_bars(result, r)) == 4 for r in ('Soprano', 'Alto', 'Tenor', 'Bass'))
    assert any(f.reason == 'xml_duplicate_measure' and f.measure == 2 for f in result.flags)


def test_duplicate_numbers_still_fail_when_parts_disagree(tmp_path):
    path = _xml(tmp_path / 'dis.musicxml', [['1', '1', '2', '3'], ['1', '2', '3', '4']])
    report = validate_musicxml(path)
    assert any('disagree' in x for x in report.fatal)
    with pytest.raises(ValueError):
        split_file(path)


def test_retry_keeps_attempt_with_most_bars(monkeypatch, tmp_path):
    import satb_splitter.omr.pipeline as pl
    from satb_splitter.omr.audiveris import AudiverisRun
    page = tmp_path / 'pages' / 'page_05.pdf'
    page.parent.mkdir()
    page.write_bytes(b'%PDF')
    monkeypatch.setattr(pl, '_plan_retries', lambda o, c, f: [(0.2, True, ''), (0.3, False, 'unclamped')])
    monkeypatch.setattr(pl, 'whiteout_top_band', lambda src, dst, **k: dst)
    bars = iter([42, 45])

    def fake_run(pdf, out, **k):
        n = next(bars)
        mx = Path(out) / f'p{n}.mxl'
        return AudiverisRun(input_path=Path(pdf), success=True, mxl_paths=[mx], bar_count=n)
    monkeypatch.setattr(pl, 'run_audiveris_export', fake_run)
    failed = AudiverisRun(input_path=page, success=False, error='rejected', bar_count=49)
    best = pl._retry_with_larger_whiteout(failed, page, tmp_path, tmp_path / 'omr', exe=Path('x'),
                                          timeout_s=1, current=0.12, fractions=(0.2, 0.3),
                                          progress=None, should_cancel=None)
    assert best.success and best.bar_count == 45
    assert any('first read exported 49 bars' in x for x in best.review_issues)


def test_retry_stops_early_when_not_shorter(monkeypatch, tmp_path):
    import satb_splitter.omr.pipeline as pl
    from satb_splitter.omr.audiveris import AudiverisRun
    page = tmp_path / 'page_05.pdf'
    page.write_bytes(b'%PDF')
    monkeypatch.setattr(pl, '_plan_retries', lambda o, c, f: [(0.2, True, ''), (0.3, False, '')])
    monkeypatch.setattr(pl, 'whiteout_top_band', lambda src, dst, **k: dst)
    calls = []

    def fake_run(pdf, out, **k):
        calls.append(out)
        return AudiverisRun(input_path=Path(pdf), success=True, mxl_paths=[Path(out) / 'a.mxl'], bar_count=49)
    monkeypatch.setattr(pl, 'run_audiveris_export', fake_run)
    failed = AudiverisRun(input_path=page, success=False, error='crash', bar_count=0)
    best = pl._retry_with_larger_whiteout(failed, page, tmp_path, tmp_path / 'omr', exe=Path('x'),
                                          timeout_s=1, current=0.12, fractions=(0.2,),
                                          progress=None, should_cancel=None)
    assert best.success and len(calls) == 1 and not best.review_issues


# ------------------------------------------------------------- 2) fermatas
def test_rest_fermata_on_one_staff_reaches_every_voice(tmp_path):
    # Batch 13 (item 4): a fermata is kept only on the voices of the staff that
    # carries it in the MXL (SA staff here); it is no longer copied to T/B.
    path = _xml(tmp_path / 'ferm.musicxml', [['1', '2']] * 2, fermata_rest_bar=1)
    result = split_file(path)
    for role in ('Soprano', 'Alto', 'Tenor', 'Bass'):
        last = _bars(result, role)[1]
        has = any(isinstance(x, expressions.Fermata) for el in last.recurse().notesAndRests
                  for x in el.expressions)
        assert has == (role in ('Soprano', 'Alto')), role
    out = write_satb_files(result, tmp_path / 'out', 'ferm')
    for key in ('Soprano', 'SATB'):
        assert '<fermata' in out[key].read_text(encoding='utf-8')
    assert '<fermata' not in out['Tenor'].read_text(encoding='utf-8')


def test_own_staff_fermata_preferred_over_other_staff_offset():
    from satb_splitter.split.directions import apply_fermatas
    m = stream.Measure()
    m.insert(0, note.Note('C4', quarterLength=1.5))
    m.insert(1.5, note.Note('D4', quarterLength=0.5))
    m.insert(2, note.Note('E4', quarterLength=2))
    fs = [(1.5, expressions.Fermata(), 0), (2.0, expressions.Fermata(), 1)]
    assert apply_fermatas(m, fs, own_staff=1) == 1
    with_f = [float(el.offset) for el in m.notes if el.expressions]
    assert with_f == [2.0]


# ------------------------------------------------------- 3) final barline
def test_final_barline_kept_in_voices_satb_and_after_merge(tmp_path):
    p1 = _xml(tmp_path / 'page_01.musicxml', [['1', '2']] * 2)
    p2 = _xml(tmp_path / 'page_02.musicxml', [['1', '2']] * 2, last_barline='light-heavy')
    written, log, combined = split_and_merge_mxl_files([p1, p2], tmp_path / 'out', 'fin')
    for key in ('Soprano', 'Alto', 'Tenor', 'Bass', 'SATB'):
        text = written[key].read_text(encoding='utf-8')
        assert text.count('light-heavy') == (4 if key == 'SATB' else 1), key
    last = list(combined.scores['Bass'].parts[0].getElementsByClass(stream.Measure))[-1]
    assert isinstance(last.rightBarline, bar.Barline) and last.rightBarline.type == 'final'


# ----------------------------------------------------------- 4) metronome
def test_metronome_with_glued_ocr_text_is_kept(tmp_path):
    path = _xml(tmp_path / 'tempo.musicxml', [['1', '2']] * 2, per_minute='75 S.a\u20142\u2014a card',
                sound_tempo=75)
    result = split_file(path)
    assert any(f.reason == 'metronome_repaired' for f in result.flags)
    out = write_satb_files(result, tmp_path / 'out', 'tempo')
    for key in ('Soprano', 'Bass', 'SATB'):
        text = out[key].read_text(encoding='utf-8')
        assert '<per-minute>75</per-minute>' in text and '<beat-unit>quarter</beat-unit>' in text, key
        assert 'Andante' in text and 'card' not in text


def test_metronome_not_repaired_when_sound_tempo_disagrees(tmp_path):
    path = _xml(tmp_path / 'tempo2.musicxml', [['1', '2']] * 2, per_minute='75 junk', sound_tempo=90)
    report = validate_musicxml(path)
    assert any(f.reason == 'metronome_unreadable' for f in report.flags)
    assert not any(f.reason == 'metronome_repaired' for f in report.flags)


def test_header_tempo_reports_vector_only_pdf(tmp_path):
    import fitz
    from satb_splitter.omr.header_tempo import cache_header_tempos, write_tempo_sidecars, text_layer_note
    pdf = tmp_path / 'page_01.pdf'
    doc = fitz.open()
    page = doc.new_page()
    for y in (300, 310, 320, 330, 340):
        page.draw_line((50, y), (550, y))
    page.draw_circle((100, 100), 5)  # an outline 'glyph', not text
    doc.save(str(pdf))
    doc.close()
    assert text_layer_note(pdf).startswith('no PDF text layer')
    cache_header_tempos(pdf)
    data = json.loads(pdf.with_suffix('.header-tempo.json').read_text(encoding='utf-8'))
    assert data['tempos'] == [] and data['note'].startswith('no PDF text layer')
    lines = write_tempo_sidecars(pdf, [tmp_path / 'page_01.mxl'])
    assert lines and 'not readable' in lines[0]


# ---------------------------------------------------------- 5) underfull
def _two_staff_score(sa_measures, tb_measures):
    s = stream.Score()
    for ms, c in ((sa_measures, clef.TrebleClef()), (tb_measures, clef.BassClef())):
        p = stream.Part()
        for k, m in enumerate(ms):
            m.number = k + 1
            if k == 0:
                m.insert(0, clef.TrebleClef() if c.sign == 'G' else clef.BassClef())
                m.insert(0, meter.TimeSignature('4/4'))
            p.append(m)
        s.insert(0, p)
    return s


def test_underfull_bar_flagged_per_bar_not_silently_padded():
    full = lambda: _m(None, None, ('1', [(0, _n('C5', 4))]))
    short = _m(None, None, ('1', [(0, _n('C5', 0.5)), (0.5, _n('D5', 1)), (1.5, _n('E5', 2))]))
    tb = [_m(None, None, ('1', [(0, _n('C3', 4))])) for _ in range(3)]
    res = split_score(_two_staff_score([full(), short, full()], tb))
    under = [f for f in res.flags if f.reason == 'underfull_measure']
    assert [f.measure for f in under] == [2]
    assert '3.5 of 4' in under[0].detail and 'END' in under[0].detail
    sop = _bars(res, 'Soprano')[1]
    assert sum(float(el.quarterLength) for el in sop.notesAndRests) == pytest.approx(4)


def test_fragment_voice_entering_mid_bar_not_flagged():
    m = _m(None, None, ('1', [(0, _n('C5', 4, 'up'))]), ('2', [(2, _n('A4', 1, 'down'))]))
    tb = [_m(None, None, ('1', [(0, _n('C3', 4))]))]
    res = split_score(_two_staff_score([m], tb))
    assert not [f for f in res.flags if f.reason == 'underfull_measure']


# ------------------------------------------------------- 6) Easy bar 7 Bass
def test_easy_bar7_bass_d3_keeps_dotted_quarter():
    sa = [_m(None, None, ('1', [(0, chord.Chord(['A4', 'F#5'], quarterLength=1)),
                                (1, chord.Chord(['A4', 'D5'], quarterLength=1)),
                                (2, chord.Chord(['G4', 'C#5', 'E5'], quarterLength=1))]))]
    sa[0].insert(0, meter.TimeSignature('3/4'))
    v5 = [(0, chord.Chord(['D3', 'D4'], quarterLength=1.5))]
    a1 = chord.Chord(['A3', 'C#4'], quarterLength=0.5)
    a1.tie = tie.Tie('start')
    a2 = chord.Chord(['A3', 'C#4', 'E4'], quarterLength=0.5)
    a2.tie = tie.Tie('stop')
    v6 = [(0, note.Rest(quarterLength=0.5)), (0.5, _n('A3', 1, 'down')), (1.5, _n('A3', 0.5, 'down')),
          (2, a1), (2.5, a2)]
    tb = [_m('3/4', None, ('5', v5), ('6', v6))]
    s = stream.Score()
    p1 = stream.Part(); sa[0].insert(0, clef.TrebleClef()); p1.append(sa[0])
    p2 = stream.Part(); tb[0].insert(0, clef.BassClef()); p2.append(tb[0])
    s.insert(0, p1); s.insert(0, p2)
    res = split_score(s)
    bass = [el for el in _bars(res, 'Bass')[0].notes]
    assert bass[0].pitch.nameWithOctave == 'D3' and float(bass[0].quarterLength) == 1.5
    ten = [el for el in _bars(res, 'Tenor')[0].notes]
    # Batch 15 B15_F7 (default True): saves overlapping A3 by tie-splitting D4 (0.5 + 1.0)
    assert float(ten[0].quarterLength) == 0.5 and ten[0].pitch.nameWithOctave == 'D4'
    assert {p.nameWithOctave for p in ten[1].pitches} == {'A3', 'D4'}
    assert not [f for f in res.flags if f.reason == 'dropped_pitch']

    # Legacy behavior preserved when B15_F7 is disabled
    res_no_ts = split_score(s, SplitConfig(graft=GraftConfig(b15_f7_chord_tie_split=False)))
    ten_no = [el for el in _bars(res_no_ts, 'Tenor')[0].notes]
    assert ten_no[0].pitch.nameWithOctave == 'D4' and float(ten_no[0].quarterLength) == 1.5


def test_same_stem_voice_switch_still_trims_overlap():
    # Normal m21 TB: voice1 A3 quarter (OMR too long) then voice2 up-stem B3 at 2.5
    v1 = [(0, _n('B3', 2, 'up')), (2, _n('A3', 1, 'up'))]
    v2 = [(0, _n('G#3', 2, 'down')), (2.5, _n('B3', 0.75, 'up')), (3.25, _n('C#4', 0.25, 'up'))]
    tb = [_m(None, None, ('1', v1), ('2', v2))]
    sa = [_m(None, None, ('1', [(0, _n('E5', 4))]))]
    res = split_score(_two_staff_score(sa, tb))
    ten = [(float(el.offset), el.pitch.nameWithOctave, float(el.quarterLength)) for el in _bars(res, 'Tenor')[0].notes]
    assert (2.0, 'A3', 0.5) in ten and (2.5, 'B3', 0.75) in ten


# --------------------------------------------------------- 7) REVIEW.txt
def test_review_lists_per_bar_flags_and_tie_bars(tmp_path):
    p = stream.Part()
    m1, m2 = stream.Measure(number=1), stream.Measure(number=2)
    a = note.Note('C4', quarterLength=4); a.tie = tie.Tie('start')
    m1.append(a)
    m2.append(note.Note('D4', quarterLength=4))
    p.append([m1, m2])
    where = []
    assert sanitize_ties(p, where) == 1 and where == [(0, 'C4')]
    res = SplitResult(scores={}, flags=[
        ReviewFlag(12, -1, None, 'meter_from_content', 'Notes fill 9/8 while the meter is 4/4'),
        ReviewFlag(3, -1, None, 'tie_removed', 'Soprano: tie on C4 removed'),
        ReviewFlag(5, 1, None, 'underfull_measure', 'Staff 1 voice(s) 1: OMR bar holds 3.5 of 4'),
        ReviewFlag(7, -1, None, 'xml_duplicate_measure', 'XML measure number 1 reused'),
        ReviewFlag(None, -1, None, 'omr_export_incomplete', 'page_01.log: no correct rhythm'),
    ])
    path = write_review_report(res, tmp_path / 'x_REVIEW.txt', 'x')
    text = path.read_text(encoding='utf-8')
    assert 'PER-BAR FLAGS' in text and 'PAGE / SCORE-LEVEL WARNINGS' in text
    # Batch 12: a 'source (page, MXL measure)' column follows the merged bar
    assert '   3 | - | Soprano | tie_removed |' in text
    assert '   5 | - | staff 1 | underfull_measure |' in text
    assert '  12 | - | all | meter_from_content |' in text
    assert text.index('   3 |') < text.index('   5 |') < text.index('  12 |')


def test_split_flags_ties_per_bar():
    s = stream.Score()
    p = stream.Part()
    m1 = stream.Measure(number=1); m1.insert(0, meter.TimeSignature('4/4')); m1.insert(0, clef.TrebleClef())
    a = note.Note('C5', quarterLength=4); a.tie = tie.Tie('start'); m1.append(a)
    m2 = stream.Measure(number=2); m2.append(note.Note('E5', quarterLength=4))
    p.append([m1, m2]); s.insert(0, p)
    res = split_score(s)
    ties = [f for f in res.flags if f.reason == 'tie_removed']
    assert ties and all(f.measure == 1 for f in ties)


# ------------------------------------------------------------- 8) .mscz
def _fake_musescore(monkeypatch, tmp_path, *, returncode=0, write=True):
    import satb_splitter.export.mscz as mz
    exe = tmp_path / 'MuseScore4.exe'
    exe.write_text('')
    calls = []

    import io

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            if write:
                with zipfile.ZipFile(cmd[2], 'w') as z:
                    z.writestr('score.mscx', '<museScore/>')
            self.pid, self.returncode = 0, returncode
            self.stdout, self.stderr = io.BytesIO(b''), io.BytesIO(b'')

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass
    monkeypatch.setattr(mz.subprocess, 'Popen', FakePopen)
    monkeypatch.setattr(mz, '_kill_tree', lambda proc: None)
    monkeypatch.setenv('SATB_MSCZ', '1')
    monkeypatch.setenv('SATB_MUSESCORE', str(exe))
    return calls


def test_every_split_writes_satb_mscz_via_musescore_cli(monkeypatch, tmp_path):
    calls = _fake_musescore(monkeypatch, tmp_path)
    p1 = _xml(tmp_path / 'page_01.musicxml', [['1', '2']] * 2)
    written, log, combined = split_and_merge_mxl_files([p1], tmp_path / 'out', 'song')
    mscz = tmp_path / 'out' / 'song_SATB.mscz'
    assert mscz.is_file() and combined.mscz_path == mscz
    assert calls and calls[0][1] == '-o' and calls[0][-1].endswith('song_SATB.musicxml')
    assert any('MuseScore file written' in ln for ln in log)
    assert 'MuseScore file written' in (tmp_path / 'out' / 'song_REVIEW.txt').read_text(encoding='utf-8')
    # re-run replaces its own file
    split_and_merge_mxl_files([p1], tmp_path / 'out', 'song')
    assert mscz.is_file() and not (tmp_path / 'out' / 'song_SATB_splitter.mscz').exists()


def test_user_saved_mscz_is_never_overwritten(monkeypatch, tmp_path):
    _fake_musescore(monkeypatch, tmp_path)
    from satb_splitter.export.mscz import export_mscz
    src = tmp_path / 'song_SATB.musicxml'
    src.write_text('<score-partwise/>')
    user = tmp_path / 'song_SATB.mscz'
    user.write_bytes(b'user edits')
    res = export_mscz(src, user)
    assert user.read_bytes() == b'user edits'
    assert res.path == tmp_path / 'song_SATB_splitter.mscz' and 'NOT overwritten' in res.message


def test_missing_musescore_is_reported_not_silent(monkeypatch, tmp_path):
    import satb_splitter.export.mscz as mz
    monkeypatch.setenv('SATB_MSCZ', '1')
    monkeypatch.setattr(mz, 'find_musescore', lambda explicit=None: None)
    p1 = _xml(tmp_path / 'page_01.musicxml', [['1', '2']] * 2)
    written, log, combined = split_and_merge_mxl_files([p1], tmp_path / 'out', 'song')
    assert combined.mscz_path is None and 'MuseScore was not found' in combined.mscz_message
    assert any(ln.startswith('WARNING: .mscz not written') for ln in log)
    assert 'MuseScore was not found' in (tmp_path / 'out' / 'song_REVIEW.txt').read_text(encoding='utf-8')


def test_musescore_failure_reported(monkeypatch, tmp_path):
    _fake_musescore(monkeypatch, tmp_path, returncode=1, write=False)
    from satb_splitter.export.mscz import export_mscz
    src = tmp_path / 'a_SATB.musicxml'
    src.write_text('<score-partwise/>')
    res = export_mscz(src)
    assert res.path is None and 'exit code 1' in res.message


# -------------------------------------------------- 9) top staff -> Soprano
def _three_staff(top_bars, sa_bars, tb_bars):
    s = stream.Score()
    for bars_, c in ((top_bars, 'G'), (sa_bars, 'G'), (tb_bars, 'F')):
        p = stream.Part()
        for k, m in enumerate(bars_):
            m.number = k + 1
            if k == 0:
                m.insert(0, meter.TimeSignature('4/4'))
                m.insert(0, clef.TrebleClef() if c == 'G' else clef.BassClef())
            p.append(m)
        s.insert(0, p)
    return s


def test_labelled_sopran_top_staff_routed_to_soprano():
    top = [_m(None, None, ('1', [(0, note.Rest(quarterLength=4))])),
           _m(None, None, ('1', [(0, _n('B4', 2, lyric='Cand')), (2, _n('C#5', 2, lyric='va'))])),
           _m(None, None, ('1', [(0, _n('A4', 4, lyric='nunta'))]))]
    top[1].insert(0, expressions.TextExpression('Sopran'))
    sa = [_m(None, None, ('1', [(0, _n('E4', 4, lyric='du'))])),
          _m(None, None, ('1', [(0, _n('E4', 4, lyric='hm'))])),
          _m(None, None, ('1', [(0, _n('E4', 4))]))]
    tb = [_m(None, None, ('1', [(0, _n('E3', 4))])) for _ in range(3)]
    res = split_score(_three_staff(top, sa, tb), auto_layout=True)
    sop = [[n.pitch.nameWithOctave for n in m.notes] for m in _bars(res, 'Soprano')]
    alto = [[n.pitch.nameWithOctave for n in m.notes] for m in _bars(res, 'Alto')]
    assert sop[1] == ['B4', 'C#5'] and sop[2] == ['A4']
    assert alto[1] == ['E4'] and alto[2] == ['E4']
    assert sop[0] == ['E4']  # bar 1: top staff silent, SA staff unchanged
    flagged = sorted(f.measure for f in res.flags if f.reason == 'top_staff_soprano')
    assert flagged == [2, 3]


def test_melody_over_humming_choir_routed_without_label():
    top = [_m(None, None, ('1', [(0, note.Rest(quarterLength=3)), (3, _n('B4', 1, lyric='Cat'))])),
           _m(None, None, ('1', [(0, _n('G#4', 4, lyric='bate'))]))]
    sa = [_m(None, None, ('1', [(0, _n('E4', 3, lyric='m'))])),
          _m(None, None, ('1', [(0, _n('B3', 4, lyric='hm'))]))]
    tb = [_m(None, None, ('1', [(0, _n('E3', 4))])) for _ in range(2)]
    res = split_score(_three_staff(top, sa, tb), auto_layout=True)
    sop = [[(float(n.offset), n.pitch.nameWithOctave) for n in m.notes] for m in _bars(res, 'Soprano')]
    assert sop[0] == [(0.0, 'E4'), (3.0, 'B4')]  # SA note kept where the melody rests
    assert sop[1] == [(0.0, 'G#4')]


def test_plain_solo_over_singing_choir_still_omitted():
    top = [_m(None, None, ('1', [(0, _n('B4', 4, lyric='Ierusalime'))])),
           _m(None, None, ('1', [(0, _n('A4', 4, lyric='cetate'))]))]
    sa = [_m(None, None, ('1', [(0, _n('E4', 4, lyric='du'))])),
          _m(None, None, ('1', [(0, _n('E4', 4, lyric='du'))]))]
    tb = [_m(None, None, ('1', [(0, _n('E3', 4))])) for _ in range(2)]
    res = split_score(_three_staff(top, sa, tb), auto_layout=True)
    sop = [[n.pitch.nameWithOctave for n in m.notes] for m in _bars(res, 'Soprano')]
    assert sop == [['E4'], ['E4']]
    assert not [f for f in res.flags if f.reason == 'top_staff_soprano']
    assert any(f.reason == 'parts_ignored' for f in res.flags)


# ------------------------------------------- real Audiveris 5.12 page fixtures
FIX = Path(__file__).parent / 'fixtures' / 'batch11'


@pytest.mark.parametrize('name,bars,dups', [('b11_hard_page_05.mxl', 49, 1), ('b11_hard_page_15.mxl', 34, 2)])
def test_hard_multirest_pages_split_instead_of_retry(name, bars, dups):
    report = validate_musicxml(FIX / name)
    assert not report.fatal
    res = split_file(FIX / name, auto_layout=True)
    assert len(_bars(res, 'Soprano')) == bars
    assert len([f for f in res.flags if f.reason == 'xml_duplicate_measure']) == dups


def test_100_page1_metronome_and_andante_kept(tmp_path):
    res = split_file(FIX / 'b11_100_page_01.mxl', auto_layout=True)
    out = write_satb_files(res, tmp_path, 'p1')
    for key in ('Soprano', 'Alto', 'Tenor', 'Bass', 'SATB'):
        text = out[key].read_text(encoding='utf-8')
        assert '<per-minute>75</per-minute>' in text and 'Andante' in text, key


def test_100_page3_sopran_melody_routed_bars_16_to_24():
    res = split_file(FIX / 'b11_100_page_03.mxl', auto_layout=True)
    routed = sorted(f.measure for f in res.flags if f.reason == 'top_staff_soprano')
    assert routed == list(range(16, 25))
    sop = _bars(res, 'Soprano')
    assert [n.pitch.nameWithOctave for n in sop[16].notes][:4] == ['B4', 'A#4', 'B4', 'C#5']
    alto = _bars(res, 'Alto')
    assert [n.pitch.nameWithOctave for n in alto[16].notes] == ['E4']
    # bars 12-15: top staff 'du' over choir 'du' stays omitted
    assert not any(f.measure in (12, 13, 14, 15) for f in res.flags if f.reason == 'top_staff_soprano')


def test_100_page4_coda_fermata_final_barline_underfull(tmp_path):
    res = split_file(FIX / 'b11_100_page_04.mxl', auto_layout=True)
    routed = sorted(f.measure for f in res.flags if f.reason == 'top_staff_soprano')
    assert routed == [18, 19, 20]
    under = {f.measure for f in res.flags if f.reason == 'underfull_measure'}
    assert {4, 8, 12, 14, 20} <= under
    for role in ('Soprano', 'Alto', 'Tenor', 'Bass'):
        # Batch 13: p4 m18 fermata is on the SA staff only -> S/A only;
        # m24 rest fermata is on the TB staff only -> T/B only (no cross-staff copy)
        for idx, owners in ((17, ('Soprano', 'Alto')), (23, ('Tenor', 'Bass'))):
            bb = _bars(res, role)[idx]
            has = any(isinstance(x, expressions.Fermata) for el in bb.recurse().notesAndRests
                      for x in el.expressions)
            assert has == (role in owners), (role, idx + 1)
        last = _bars(res, role)[-1]
        assert last.rightBarline is not None and last.rightBarline.type == 'final', role


def test_easy_page_bar7_bass_d3_dotted_quarter():
    res = split_file(FIX / 'b11_easy_page_01.mxl', auto_layout=True)
    bass = list(_bars(res, 'Bass')[7].notes)
    assert bass[0].pitch.nameWithOctave == 'D3' and float(bass[0].quarterLength) == 1.5
