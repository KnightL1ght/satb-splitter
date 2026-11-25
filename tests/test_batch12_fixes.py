"""Batch 12: multi-bar rests, named-solo routing on a treble-8vb staff,
fermata copying, underfull top-staff bars, REVIEW labels/notes and score
metadata (.mscz inner name). No live Audiveris or MuseScore."""
import io
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from music21 import expressions, note, stream

from satb_splitter.split.engine import split_file
from satb_splitter.split.directions import apply_fermatas, FermataSource
from satb_splitter.split.multirest import scan_multirests, apply_multirests
from satb_splitter.export.writer import write_satb_files, write_review_report, set_musicxml_titles
from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.model.config import ReviewFlag, SplitResult
import satb_splitter.export.mscz as mz

FIX = Path(__file__).parent / 'fixtures' / 'batch12'
ROLES = ('Soprano', 'Alto', 'Tenor', 'Bass')


def _fixture(tmp_path, stem):
    for p in FIX.glob(stem + '.*'):
        shutil.copy(p, tmp_path / p.name)
    return tmp_path / f'{stem}.mxl'


def _bars(res, role):
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def _pos(res, mxl_number):
    return res.source_measure_numbers.index(str(mxl_number))  # 0-based bar position


def _has_fermata(el):
    return any(isinstance(x, expressions.Fermata) for x in el.expressions)


def _fermatas(m):
    return [(float(e.offset), e.isRest) for e in m.recurse().notesAndRests if _has_fermata(e)]


# ------------------------------------------------------- item 2: solo routing
def test_hard_p11_treble8vb_solo_goes_to_tenor_only(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_hard_page_11'), auto_layout=True)
    routed = sorted(f.measure for f in res.flags if f.reason == 'solo_routed')
    first, last = _pos(res, 13) + 1, _pos(res, 21) + 1
    assert routed == list(range(first, last + 1))  # flagged on EVERY solo bar
    assert all('-> Tenor' in f.detail for f in res.flags if f.reason == 'solo_routed')
    tenor, bass = _bars(res, 'Tenor'), _bars(res, 'Bass')
    for i in range(first - 1, last):
        assert not list(bass[i].recurse().notes), f'bar {i + 1}: Bass must rest'
    assert sum(len(list(tenor[i].recurse().notes)) for i in range(first - 1, last)) > 20


def test_hard_p8_solo_alto_goes_to_alto_only(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_hard_page_08'), auto_layout=True)
    routed = sorted(f.measure for f in res.flags if f.reason == 'solo_routed')
    first, last = _pos(res, 10) + 1, _pos(res, 13) + 1
    assert routed == list(range(first, last + 1))
    for i in range(first - 1, last):
        assert list(_bars(res, 'Alto')[i].recurse().notes)
        for role in ('Soprano', 'Tenor', 'Bass'):
            assert not list(_bars(res, role)[i].recurse().notes), (role, i + 1)


# ----------------------------------------------------------- item 3: fermatas
def test_omitted_solo_fermata_not_copied_onto_choir_rests(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_hard_page_10_mvt2'), auto_layout=True)
    i = _pos(res, 9)
    for role in ROLES:
        assert _fermatas(_bars(res, role)[i]) == [], role


def test_p11_solo_bar_fermata_only_on_tenor(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_hard_page_11'), auto_layout=True)
    i = _pos(res, 16)
    assert _fermatas(_bars(res, 'Tenor')[i]) == [(0.0, False)]
    for role in ('Soprano', 'Alto', 'Bass'):
        assert _fermatas(_bars(res, role)[i]) == [], role


def test_100_bar82_alto_no_fermata_at_offset0(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_100_page_04'), auto_layout=True)
    i = _pos(res, 14)
    assert _fermatas(_bars(res, 'Alto')[i]) == [(1.0, False)]
    assert _fermatas(_bars(res, 'Soprano')[i]) == [(0.0, False)]
    assert _fermatas(_bars(res, 'Tenor')[i]) == [(1.0, False)]


def _m(*els):
    m = stream.Measure(number=1)
    for off, el in els:
        m.insert(off, el)
    return m


def test_apply_fermatas_needs_element_starting_at_offset():
    m = _m((0, note.Note('A4', quarterLength=2)), (2, note.Note('B4', quarterLength=2)))
    src = FermataSource(is_rest=False, pitches=frozenset({'C5'}), contested=False)
    apply_fermatas(m, [(1.0, expressions.Fermata(), 0, src)], own_staff=0)
    assert _fermatas(m) == []  # no 'covering' fallback any more


def test_note_fermata_never_lands_on_rest_and_skip_staff_ignored():
    src = FermataSource(is_rest=False, pitches=frozenset({'C4'}), contested=False)
    m = _m((0, note.Rest(quarterLength=4)))
    apply_fermatas(m, [(0.0, expressions.Fermata(), 1, src)], own_staff=0)
    assert _fermatas(m) == []
    m2 = _m((0, note.Note('E4', quarterLength=4)))
    apply_fermatas(m2, [(0.0, expressions.Fermata(), 2, src)], own_staff=0, skip_staves=(2,))
    assert _fermatas(m2) == []
    m3 = _m((0, note.Note('E4', quarterLength=4)))
    apply_fermatas(m3, [(0.0, expressions.Fermata(), 1, src)], own_staff=0)
    assert _fermatas(m3) == []  # Batch 13: never copied from another staff (was kept in Batch 12)


# ------------------------------------------------- item 4: top-staff underfull
def test_top_staff_soprano_bar_is_checked_for_underfull(tmp_path):
    res = split_file(_fixture(tmp_path, 'b12_100_page_03'), auto_layout=True)
    i = _pos(res, 20) + 1
    reasons = {f.reason for f in res.flags if f.measure == i}
    assert {'top_staff_soprano', 'underfull_measure'} <= reasons
    uf = [f for f in res.flags if f.measure == i and f.reason == 'underfull_measure']
    assert any('3.5 of 4' in f.detail for f in uf)


# ----------------------------------------------------- item 1: multi-bar rests
def _mr_xml(path, bars, *, mr=None, two_parts=True):
    """bars: list of 'R' (whole rest) or pitch names; mr: {position: count}."""
    root = ET.Element('score-partwise', version='4.0')
    pl = ET.SubElement(root, 'part-list')
    parts = ['P1', 'P2'] if two_parts else ['P1']
    for pid in parts:
        sp = ET.SubElement(pl, 'score-part', id=pid)
        ET.SubElement(sp, 'part-name').text = 'Voice'
    for pi, pid in enumerate(parts):
        p = ET.SubElement(root, 'part', id=pid)
        for k, b in enumerate(bars):
            m = ET.SubElement(p, 'measure', number=str(k + 1))
            a = ET.SubElement(m, 'attributes')
            if k == 0:
                ET.SubElement(a, 'divisions').text = '1'
                t = ET.SubElement(a, 'time')
                ET.SubElement(t, 'beats').text = '4'
                ET.SubElement(t, 'beat-type').text = '4'
                c = ET.SubElement(a, 'clef')
                ET.SubElement(c, 'sign').text = 'G' if pi == 0 else 'F'
                ET.SubElement(c, 'line').text = '2' if pi == 0 else '4'
            if mr and (k + 1) in mr:
                ms = ET.SubElement(a, 'measure-style')
                ET.SubElement(ms, 'multiple-rest').text = str(mr[k + 1])
            n = ET.SubElement(m, 'note')
            if b == 'R':
                ET.SubElement(n, 'rest', measure='yes')
            else:
                pitch = ET.SubElement(n, 'pitch')
                ET.SubElement(pitch, 'step').text = b[0]
                ET.SubElement(pitch, 'octave').text = b[1] if pi == 0 else str(int(b[1]) - 1)
            ET.SubElement(n, 'duration').text = '4'
            ET.SubElement(n, 'type').text = 'whole'
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


def test_complete_multirest_flagged_with_count_and_source(tmp_path):
    p = _mr_xml(tmp_path / 'a.musicxml', ['R', 'R', 'R', 'C5', 'D5'], mr={1: 3})
    res = split_file(p, auto_layout=True)
    fl = [f for f in res.flags if f.reason.startswith('multirest')]
    assert len(fl) == 1 and fl[0].reason == 'multirest' and fl[0].measure == 1
    assert 'count used 3' in fl[0].detail and 'count source: MXL <multiple-rest>3' in fl[0].detail
    assert 'no PDF text layer' in fl[0].detail
    assert all(len(_bars(res, r)) == 5 for r in ROLES)  # nothing added


def test_truncated_multirest_at_page_end_is_expanded_in_every_voice(tmp_path):
    p = _mr_xml(tmp_path / 'b.musicxml', ['C5', 'D5', 'R'], mr={3: 4})
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {6}  # 3 + 3 appended
    for r in ROLES:
        assert not list(_bars(res, r)[5].recurse().notes)
        assert float(_bars(res, r)[5].duration.quarterLength) == 4.0
    fl = [f for f in res.flags if f.reason == 'multirest']
    assert fl and '3 rest bar(s) appended per voice' in fl[0].detail
    assert res.source_measure_numbers[-3:] == ['3+1', '3+2', '3+3']


def test_multirest_colliding_with_music_is_not_expanded(tmp_path):
    p = _mr_xml(tmp_path / 'c.musicxml', ['R', 'R', 'E5', 'F5'], mr={1: 4})
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {4}
    assert [f.reason for f in res.flags if f.reason.startswith('multirest')] == ['multirest_conflict']


def test_printed_count_mismatch_and_missing_are_flagged_not_invented(tmp_path):
    p = _mr_xml(tmp_path / 'd.musicxml', ['R', 'R', 'R', 'C5', 'R', 'D5'], mr={1: 3})
    (tmp_path / 'd.multirest.json').write_text(json.dumps({'printed': [
        {'measure_index': 0, 'count': 13}, {'measure_index': 4, 'count': 6}]}))
    res = split_file(p, auto_layout=True)
    by = {f.reason: f for f in res.flags if f.reason.startswith('multirest')}
    assert by['multirest_count_mismatch'].measure == 1
    assert 'count used 3 but the PDF prints 13' in by['multirest_count_mismatch'].detail
    assert by['multirest_missing'].measure == 5
    assert {len(_bars(res, r)) for r in ROLES} == {6}


def test_real_hard_p7_mvt2_multirests(tmp_path):
    p = _fixture(tmp_path, 'b12_hard_page_07_mvt2')
    runs = scan_multirests(p)
    assert [(r.position, r.count, r.present, r.stop) for r in runs] == [(1, 3, 3, ''), (5, 3, 3, '')]
    res = split_file(p, auto_layout=True)
    fl = sorted((f.measure, f.reason) for f in res.flags if f.reason.startswith('multirest'))
    assert fl == [(1, 'multirest'), (5, 'multirest_count_mismatch')]


# ----------------------------------------------------------- item 5: REVIEW
def test_review_shows_page_and_mxl_measure_number(tmp_path):
    a = _mr_xml(tmp_path / 'p1.musicxml', ['C5', 'D5'])
    b = _mr_xml(tmp_path / 'p2.musicxml', ['R', 'R', 'R', 'E5'], mr={1: 3})
    # page 2 numbers its bars from 7 in the MXL
    t = b.read_text(encoding='utf-8')
    for k in range(4, 0, -1):
        t = t.replace(f'number="{k}"', f'number="{k + 6}"')
    b.write_text(t, encoding='utf-8')
    (tmp_path / 'p2.tempo.json').write_text(json.dumps({'tempos': [], 'note':
        'no PDF text layer (0 fonts, 0 text characters, 9 vector drawings, 0 images)'}))
    import os
    os.environ['SATB_MSCZ'] = '0'
    try:
        _, _, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 'song')
    finally:
        os.environ.pop('SATB_MSCZ', None)
    text = (tmp_path / 'out' / 'song_REVIEW.txt').read_text(encoding='utf-8')
    assert 'bar  | source (page, MXL measure) | voice/staff | reason | detail' in text
    assert '   3 | ' + str(tmp_path.name) + '/p2.musicxml m7 | all | multirest |' in text
    assert 'pdf_no_text_layer' in text and 'no PDF text layer' in text
    fl = [f for f in res.flags if f.reason == 'multirest'][0]
    assert fl.measure == 3 and fl.source_measure == '7' and '[' in fl.detail and 'MXL m7]' in fl.detail


def test_header_tempo_sidecar_keeps_no_text_layer_note(tmp_path):
    from satb_splitter.omr.header_tempo import write_tempo_sidecars, tempo_sidecar_note
    pdf = tmp_path / 'page_01.pdf'
    pdf.write_bytes(b'%PDF-1.4')
    pdf.with_suffix('.header-tempo.json').write_text(json.dumps(
        {'tempos': [], 'note': 'no PDF text layer (0 fonts)'}))
    mxl = tmp_path / 'page_01.mxl'
    mxl.write_text('x')
    lines = write_tempo_sidecars(pdf, [mxl])
    assert lines and 'not readable' in lines[0]
    assert tempo_sidecar_note(mxl).startswith('no PDF text layer')


# --------------------------------------------------------- item 6: metadata
def test_titles_and_no_placeholder_composer(tmp_path, monkeypatch):
    monkeypatch.setenv('SATB_MSCZ', '0')
    p = _mr_xml(tmp_path / 'x.musicxml', ['C5', 'D5'])
    res = split_file(p, auto_layout=True)
    written = write_satb_files(res, tmp_path / 'out', 'Pace pe pamant_merge')
    for key, path in written.items():
        t = path.read_text(encoding='utf-8')
        assert 'Music21' not in t and '.tmp-export' not in t
        assert '<work-title>Pace pe pamant_merge</work-title>' in t
        want = 'Pace pe pamant_merge' if key == 'SATB' else f'Pace pe pamant_merge - {key}'
        assert f'<movement-title>{want}</movement-title>' in t
        ET.parse(path)  # still well-formed


def test_set_titles_keeps_real_composer_and_escapes(tmp_path):
    f = tmp_path / 'a.musicxml'
    f.write_text('<?xml version="1.0"?>\n<score-partwise version="4.0">\n  <movement-title>Music21 Fragment</movement-title>\n'
                 '  <identification>\n    <creator type="composer">J. S. Bach</creator>\n  </identification>\n</score-partwise>\n')
    set_musicxml_titles(f, 'A & B <1>')
    t = f.read_text()
    assert 'J. S. Bach' in t and 'Music21 Fragment' not in t
    assert '<work-title>A &amp; B &lt;1&gt;</work-title>' in t
    ET.parse(f)


class _FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd, self.pid, self.returncode = cmd, 0, 0
        out = Path(cmd[2])
        # like MuseScore: the inner .mscx is named after the OUTPUT file
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr(out.stem + '.mscx', '<museScore/>')
        self.stdout, self.stderr = io.BytesIO(b''), io.BytesIO(b'')

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_mscz_exported_under_final_name(tmp_path, monkeypatch):
    exe = tmp_path / 'MuseScore4.exe'
    exe.write_text('')
    monkeypatch.setenv('SATB_MSCZ', '1')
    monkeypatch.setenv('SATB_MUSESCORE', str(exe))
    seen = []
    monkeypatch.setattr(mz.subprocess, 'Popen', lambda cmd, **kw: (seen.append(cmd), _FakeProc(cmd))[1])
    src = tmp_path / 'song_SATB.musicxml'
    src.write_text('<score-partwise/>')
    (tmp_path / 'song_SATB.tmp-export.mscz').write_bytes(b'old leftover')
    res = mz.export_mscz(src)
    assert res.path == tmp_path / 'song_SATB.mscz'
    assert Path(seen[0][2]).name == 'song_SATB.mscz' and Path(seen[0][2]).parent != tmp_path
    with zipfile.ZipFile(res.path) as z:
        assert 'song_SATB.mscx' in z.namelist()
        assert not any('tmp-export' in n for n in z.namelist())
    assert not (tmp_path / 'song_SATB.tmp-export.mscz').exists()
    assert not (tmp_path / 'song_SATB.splitter-tmp').exists()
