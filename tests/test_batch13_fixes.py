"""Batch 13: bars split across system/page breaks joined, multirest end barline,
100 bar 86 Soprano lead-in, fermatas only from the voice's own staff.

Fixtures in fixtures/batch13 are dev3 Audiveris page MXLs (+ sidecars) copied
from test_runs/tester_dev3_20261116. No live Audiveris or MuseScore."""
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from music21 import expressions, note, stream

from satb_splitter.split.engine import split_file
from satb_splitter.split.directions import apply_fermatas, FermataSource
from satb_splitter.split.joinbars import (
    find_split_bars, merge_source_numbers, remap_measure, ratio_len,
)
from satb_splitter.split.topstaff import is_lead_in
from satb_splitter.export.merge import split_and_merge_mxl_files

FIX = Path(__file__).parent / 'fixtures' / 'batch13'
ROLES = ('Soprano', 'Alto', 'Tenor', 'Bass')


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv('SATB_MSCZ', '0')


def _fixture(tmp_path, stem, as_name=None):
    for p in FIX.glob(stem + '.*'):
        shutil.copy(p, tmp_path / p.name.replace(stem, as_name or stem))
    return tmp_path / f'{as_name or stem}.mxl'


def _bars(res, role):
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def _sig(m):
    return [('R' if e.isRest else '+'.join(p.nameWithOctave for p in e.pitches), float(e.offset), float(e.quarterLength))
            for e in m.recurse().notesAndRests]


def _fermatas(m):
    return [float(e.offset) for e in m.recurse().notesAndRests
            if any(isinstance(x, expressions.Fermata) for x in e.expressions)]


# ------------------------------------------------------------- synthetic MXL
def _xml(path, staves, *, systems=(), barlines=None, beats=4, beat_type=4):
    """staves: per part a list of bars; a bar is a list of (pitch|'R', quarters).
    systems: 1-based bar numbers starting a new system; barlines: {bar: style}."""
    root = ET.Element('score-partwise', version='4.0')
    pl = ET.SubElement(root, 'part-list')
    for k in range(len(staves)):
        sp = ET.SubElement(pl, 'score-part', id=f'P{k + 1}')
        ET.SubElement(sp, 'part-name').text = 'Voice'
    for pi, bars in enumerate(staves):
        p = ET.SubElement(root, 'part', id=f'P{pi + 1}')
        for k, b in enumerate(bars):
            m = ET.SubElement(p, 'measure', number=str(k + 1))
            if (k + 1) in systems:
                ET.SubElement(m, 'print', {'new-system': 'yes'})
            if k == 0:
                a = ET.SubElement(m, 'attributes')
                ET.SubElement(a, 'divisions').text = '2'
                t = ET.SubElement(a, 'time')
                ET.SubElement(t, 'beats').text = str(beats)
                ET.SubElement(t, 'beat-type').text = str(beat_type)
                c = ET.SubElement(a, 'clef')
                ET.SubElement(c, 'sign').text = 'G' if pi == 0 else 'F'
                ET.SubElement(c, 'line').text = '2' if pi == 0 else '4'
            for pitch, ql in b:
                n = ET.SubElement(m, 'note')
                if pitch == 'R':
                    ET.SubElement(n, 'rest')
                else:
                    pe = ET.SubElement(n, 'pitch')
                    ET.SubElement(pe, 'step').text = pitch[0]
                    ET.SubElement(pe, 'octave').text = pitch[1] if pi == 0 else str(int(pitch[1]) - 1)
                ET.SubElement(n, 'duration').text = str(int(ql * 2))
                ET.SubElement(n, 'type').text = {0.5: 'eighth', 1: 'quarter', 2: 'half', 4: 'whole'}.get(ql, 'quarter')
            if barlines and (k + 1) in barlines:
                bl = ET.SubElement(m, 'barline', location='right')
                ET.SubElement(bl, 'bar-style').text = barlines[k + 1]
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


FULL = [('C5', 4)]
HALF_A = [('D5', 1), ('E5', 1)]
HALF_B = [('F5', 1), ('G5', 1)]


def _score(bars_a, bars_b=None):
    return [bars_a, bars_b if bars_b is not None else bars_a]


# ------------------------------------------------- item 1: split bars joined
def test_synthetic_split_bar_at_system_break_is_joined(tmp_path):
    p = _xml(tmp_path / 'a.musicxml', _score([FULL, HALF_A, HALF_B, FULL]), systems=(3,))
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {3}
    sop = _bars(res, 'Soprano')[1]
    assert _sig(sop) == [('D5', 0.0, 1.0), ('E5', 1.0, 1.0), ('F5', 2.0, 1.0), ('G5', 3.0, 1.0)]  # no padding rest
    assert [(f.measure, f.reason) for f in res.flags if f.reason.startswith('split_bar')] == [(2, 'split_bar_joined')]
    assert res.source_measure_numbers == ['1', '2+3', '4']


def test_no_join_without_system_break_or_across_a_final_barline(tmp_path):
    p = _xml(tmp_path / 'b.musicxml', _score([FULL, HALF_A, HALF_B, FULL]))
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {4}
    assert not [f for f in res.flags if f.reason.startswith('split_bar')]
    p2 = _xml(tmp_path / 'c.musicxml', _score([FULL, HALF_A, HALF_B, FULL]), systems=(3,), barlines={2: 'light-light'})
    res2 = split_file(p2, auto_layout=True)
    assert {len(_bars(res2, r)) for r in ROLES} == {4}


def test_halves_not_filling_the_meter_are_flagged_not_joined(tmp_path):
    p = _xml(tmp_path / 'd.musicxml', _score([FULL, [('D5', 1)], HALF_B, FULL]), systems=(3,))
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {4}
    fl = [f for f in res.flags if f.reason.startswith('split_bar')]
    assert [(f.measure, f.reason) for f in fl] == [(2, 'split_bar_unjoined')]
    assert 'add up to 3 of 4/4' in fl[0].detail


def test_join_helpers():
    assert remap_measure(5, [3]) == 4 and remap_measure(3, [3]) == 2 and remap_measure(2, [3]) == 2
    assert merge_source_numbers(['1', '2', '3', '4'], [3]) == ['1', '2+3', '4']
    assert ratio_len('9/8') == 4.5 and ratio_len('3+2/4') == 5.0


def test_hard_p1_bar_30_31_joined(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_hard_page_01'), auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {61}  # 62 MXL measures - 1 join
    assert [(f.measure, f.reason) for f in res.flags if f.reason.startswith('split_bar')] == [(30, 'split_bar_joined')]
    assert res.source_measure_numbers[29] == '30+31'
    sop = _bars(res, 'Soprano')[29]
    assert [n for n, _, _ in _sig(sop)] == ['A-4'] * 5 + ['G4', 'F4', 'G4']
    assert float(sop.duration.quarterLength) == 4.0 and not any(n == 'R' for n, _, _ in _sig(sop))


def test_hard_p6_98_bars_split_3_plus_6_joined(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_hard_page_06'), auto_layout=True)
    joined = [f.measure for f in res.flags if f.reason == 'split_bar_joined']
    assert [res.source_measure_numbers[m - 1] for m in joined] == ['25+26', '34+35', '43+44']
    assert {len(_bars(res, r)) for r in ROLES} == {76}  # 79 - 3
    for m in joined:
        for r in ROLES:
            assert float(_bars(res, r)[m - 1].duration.quarterLength) == 4.5, (r, m)
    s = _bars(res, 'Soprano')[joined[1] - 1]  # m34+35: tied A-4 ends, C5 on the 3rd beat
    assert _sig(s) == [('A-4', 0.0, 1.5), ('R', 1.5, 1.5), ('C5', 3.0, 1.5)]
    assert s.recurse().notes[0].tie.type == 'stop'
    t = _bars(res, 'Tenor')[joined[1] - 1]
    assert [x for x in _sig(t) if x[0] != 'R'] == [('C4', 3.0, 1.5)]


def test_hard_page13_14_bar_split_across_page_files_joined(tmp_path):
    a = _fixture(tmp_path, 'b13_hard_page_13', 'page_13')
    b = _fixture(tmp_path, 'b13_hard_page_14', 'page_14')
    _, log, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 'x')
    assert any('joined bar split across the page break' in line for line in log)
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == 25  # 12 + 14 - 1
        assert float(ms[11].duration.quarterLength) == 4.0
        assert ms[11].timeSignature is None  # 4/4 continues - no 2/4 half bar
    assert _sig(_bars(res, 'Soprano')[11])[-1] == ('C5', 2.0, 2.0)
    fl = [f for f in res.flags if f.reason.startswith('split_bar')]
    assert [(f.measure, f.reason) for f in fl] == [(12, 'split_bar_joined')]
    assert fl[0].source_page.endswith('page_13.mxl m12 + ' + tmp_path.name + '/page_14.mxl') and fl[0].source_measure == '1'
    assert not [f for f in res.flags if f.measure == 12 and f.reason in ('meter_from_content', 'meter_irregular')]
    text = (tmp_path / 'out' / 'x_REVIEW.txt').read_text(encoding='utf-8')
    assert 'split_bar_joined' in text


def test_no_cross_page_join_into_a_new_movement(tmp_path):
    a = _fixture(tmp_path, 'b13_hard_page_13', 'page_13')
    b = _fixture(tmp_path, 'b13_hard_page_14', 'page_14.mvt2')
    _, _, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 'x')
    assert len(_bars(res, 'Soprano')) == 26
    assert not [f for f in res.flags if f.reason == 'split_bar_joined']


# ------------------------------------------ item 2: multirest end barline
def test_normal_final_barline_moves_to_last_bar_of_multirest(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_normal_page_02'), auto_layout=True)
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == 32
        assert ms[-1].rightBarline is not None and ms[-1].rightBarline.type == 'final', r
        assert ms[-2].rightBarline is None or ms[-2].rightBarline.type in ('regular', 'normal'), r


def test_hard_p2_mvt_end_barline_on_last_bar_of_mr20(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_hard_page_02'), auto_layout=True)
    ms = _bars(res, 'Bass')
    assert [(i + 1, m.rightBarline.type) for i, m in enumerate(ms) if m.rightBarline is not None] == [(52, 'final')]


def test_truncated_multirest_barline_goes_to_appended_bar(tmp_path):
    p = _xml(tmp_path / 'e.musicxml', _score([FULL, [('R', 4)]]), barlines={2: 'light-heavy'})
    t = p.read_text(encoding='utf-8').replace(
        '<measure number="2">', '<measure number="2"><attributes><measure-style><multiple-rest>3</multiple-rest>'
        '</measure-style></attributes>')
    p.write_text(t, encoding='utf-8')
    res = split_file(p, auto_layout=True)
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == 4
        assert ms[-1].rightBarline is not None and ms[-1].rightBarline.type == 'final'
        assert ms[1].rightBarline is None or ms[1].rightBarline.type in ('regular', 'normal')


# ------------------------------------------------ item 3: 100 bar 86 pickup
def test_100_bar86_soprano_pickup_restored_without_alto_extra(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_100_page_04'), auto_layout=True)
    routed = {f.measure: f.detail for f in res.flags if f.reason == 'top_staff_soprano'}
    assert sorted(routed) == [18, 19, 20] and 'lead-in' in routed[18]
    assert _sig(_bars(res, 'Soprano')[17]) == [('E4', 0.0, 3.0), ('B4', 3.0, 0.5), ('A4', 3.5, 0.5)]
    assert _sig(_bars(res, 'Alto')[17]) == [('B3', 0.0, 3.0), ('R', 3.0, 1.0)]
    # Batch 12 fix kept: no Alto fermata at offset 0 of bar 82 (p4 m14)
    assert _fermatas(_bars(res, 'Alto')[13]) == [1.0]
    # routed bar 88 (p4 m20): the SA-staff rest fermata stays on the Soprano
    assert _fermatas(_bars(res, 'Soprano')[19]) == [2.5]


def test_lead_in_rule_needs_second_half_words_and_silent_sa():
    from music21 import note as n21

    def bar(*els):
        m = stream.Measure(number=1)
        for off, el in els:
            m.insert(off, el)
        return m

    def sung(p, ql, word):
        x = n21.Note(p, quarterLength=ql)
        x.lyric = word
        return x

    parser = lambda t: None  # noqa: E731
    top = bar((0, n21.Rest(quarterLength=3)), (3, sung('B4', 0.5, 'Cat')), (3.5, sung('A4', 0.5, 'mai')))
    sa_ok = bar((0, n21.Note('E4', quarterLength=3)), (3, n21.Rest(quarterLength=1)))
    sa_busy = bar((0, n21.Note('E4', quarterLength=4)))
    assert is_lead_in(top, sa_ok, 4.0, parser)
    assert not is_lead_in(top, sa_busy, 4.0, parser)
    early = bar((0, n21.Rest(quarterLength=1)), (1, sung('B4', 3, 'Cat')))
    assert not is_lead_in(early, sa_ok, 4.0, parser)
    hum = bar((0, n21.Rest(quarterLength=3)), (3, sung('B4', 1, 'hm')))
    assert not is_lead_in(hum, sa_ok, 4.0, parser)


# -------------------------------------------- item 4: fermatas own staff only
def test_hard_p3_sa_fermata_not_copied_to_tenor_bass(tmp_path):
    res = split_file(_fixture(tmp_path, 'b13_hard_page_03'), auto_layout=True)
    for mxl in ('13', '20'):
        i = res.source_measure_numbers.index(mxl)
        assert _fermatas(_bars(res, 'Tenor')[i]) == [] and _fermatas(_bars(res, 'Bass')[i]) == [], mxl
        assert _fermatas(_bars(res, 'Alto')[i]), mxl  # legit fermata on its own staff kept


def test_apply_fermatas_own_staff_only_and_routed_two_staff_bar():
    src = FermataSource(is_rest=False, pitches=frozenset({'C4'}), contested=False)
    rest_src = FermataSource(is_rest=True)
    m = stream.Measure(number=1)
    m.insert(0, note.Note('A3', quarterLength=4))
    assert apply_fermatas(m, [(0.0, expressions.Fermata(), 0, src)], own_staff=1) == 0
    assert apply_fermatas(m, [(0.0, expressions.Fermata(), 1, src)], own_staff=1) == 1
    r = stream.Measure(number=1)
    r.insert(0, note.Note('B4', quarterLength=2))
    r.insert(2, note.Rest(quarterLength=2))
    # Soprano bar routed from the top staff (0) + SA fill (1): SA rest fermata applies
    assert apply_fermatas(r, [(2.0, expressions.Fermata(), 1, rest_src)], own_staff=(0, 1)) == 1
    assert _fermatas(r) == [2.0]
    n = stream.Measure(number=1)
    n.insert(0, note.Rest(quarterLength=4))
    assert apply_fermatas(n, [(0.0, expressions.Fermata(), 2, rest_src)], own_staff=None) == 0

# ------------------------------------- dev3b: implicit X<n> continuation measures
from satb_splitter.musicxml_validation import _identity_grid, validate_musicxml  # noqa: E402


def _x_continuation(path, first, second_label, *, keep_print=True):
    """Rewrite measure ``second`` (1-based) of every part as Audiveris dev3b does."""
    t = path.read_text(encoding='utf-8')
    t = t.replace(f'<measure number="{first + 1}">', f'<measure number="{second_label}" implicit="yes">')
    if not keep_print:
        t = t.replace('<print new-system="yes" />', '').replace('<print new-system="yes"/>', '')
    path.write_text(t, encoding='utf-8')
    return path


@pytest.mark.parametrize('stem, joins, bars', [
    ('b13_hard3b_page_01', ['30+X30'], 61),
    ('b13_hard3b_page_06', ['25+X25', '33+X33', '41+X41'], 76),
])
def test_dev3b_implicit_continuation_measures_joined(tmp_path, stem, joins, bars):
    res = split_file(_fixture(tmp_path, stem), auto_layout=True)
    fl = [f for f in res.flags if f.reason == 'split_bar_joined']
    assert [res.source_measure_numbers[f.measure - 1] for f in fl] == joins
    assert all('implicit continuation' in f.detail for f in fl)
    assert not [f for f in res.flags if f.reason == 'split_bar_unjoined']
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == bars
        for f in fl:
            m = ms[f.measure - 1]
            assert float(m.duration.quarterLength) == (4.0 if stem.endswith('01') else 4.5)
    assert not [f for f in res.flags if f.reason in ('xml_missing_measure', 'xml_duplicate_measure', 'measure_count_mismatch')]


def test_dev3b_validation_accepts_x_identities(tmp_path):
    rep = validate_musicxml(_fixture(tmp_path, 'b13_hard3b_page_06'))
    assert not rep.fatal
    assert not [f for f in rep.flags if f.reason in ('xml_missing_measure', 'xml_duplicate_measure')]
    assert _identity_grid([['24', '25', 'X25', '26']] * 2) == ['24', '25', 'X25', '26']
    # a real gap is still a gap; the continuation is not
    assert _identity_grid([['24', '25', 'X25', '26'], ['24', 'X25', '26']]) == ['24', '25', 'X25', '26']
    assert _identity_grid([['25', '25X25', '26']]) == ['25', '25X25', '26']
    with pytest.raises(ValueError):
        _identity_grid([['25', 'X25', 'X25']])


def test_x_continuation_joined_without_system_break_mark(tmp_path):
    p = _xml(tmp_path / 'x.musicxml', _score([FULL, HALF_A, HALF_B, FULL]))
    _x_continuation(p, 2, 'X2', keep_print=False)
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {3}
    assert _sig(_bars(res, 'Soprano')[1]) == [('D5', 0.0, 1.0), ('E5', 1.0, 1.0), ('F5', 2.0, 1.0), ('G5', 3.0, 1.0)]
    assert res.source_measure_numbers == ['1', '2+X2', '4']  # file keeps number 4 for the next bar


def test_x_continuation_not_fitting_is_flagged_never_padded(tmp_path):
    p = _xml(tmp_path / 'y.musicxml', _score([FULL, HALF_A, [('F5', 1), ('G5', 1), ('A5', 1)], FULL]))
    _x_continuation(p, 2, 'X2', keep_print=False)
    res = split_file(p, auto_layout=True)
    assert {len(_bars(res, r)) for r in ROLES} == {4}
    fl = [f for f in res.flags if f.reason.startswith('split_bar')]
    assert [(f.measure, f.reason) for f in fl] == [(2, 'split_bar_unjoined')]
    assert 'mX2' in fl[0].detail and 'more than 4/4' in fl[0].detail


def test_dev3b_normal_final_barline_already_on_60_not_moved(tmp_path):
    """dev3b writes the light-heavy on m60 (after the MR2 on m59): keep it there."""
    res = split_file(_fixture(tmp_path, 'b13_normal3b_full'), auto_layout=True)
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == 60
        assert ms[59].rightBarline is not None and ms[59].rightBarline.type == 'final', r
        assert ms[58].rightBarline is None or ms[58].rightBarline.type in ('regular', 'normal'), r
    mr = [f for f in res.flags if f.reason.startswith('multirest')]
    assert mr and not any('moved to its last bar' in f.detail for f in mr)

# ------------------------------------- item 5: declared meter never rewritten
from music21 import key as m21key, meter as m21meter  # noqa: E402

from satb_splitter.export.writer import write_satb_files  # noqa: E402


def _ts(res, role='Soprano'):
    return [(i + 1, t.ratioString) for i, m in enumerate(_bars(res, role))
            for t in m.getElementsByClass(m21meter.TimeSignature)]


def _edit_xml(path, *, drop_time=False, time_at=None, key_at=None):
    """Post-edit a synthetic MXL: remove every <time>; add <time> (bar, beats, type)
    or <key> (bar, fifths) to every part's attributes of that bar."""
    tree = ET.parse(path)
    root = tree.getroot()
    for part in root.findall('part'):
        for m in part.findall('measure'):
            a = m.find('attributes')
            if drop_time and a is not None:
                for t in a.findall('time'):
                    a.remove(t)
            for spec, tag in ((time_at, 'time'), (key_at, 'key')):
                if spec and m.get('number') == str(spec[0]):
                    if a is None:
                        a = ET.Element('attributes')
                        m.insert(0, a)
                    el = ET.Element(tag)
                    if tag == 'time':
                        ET.SubElement(el, 'beats').text = str(spec[1])
                        ET.SubElement(el, 'beat-type').text = str(spec[2])
                        a.append(el)
                    else:
                        ET.SubElement(el, 'fifths').text = str(spec[1])
                        a.insert(1 if a.find('divisions') is not None else 0, el)
    tree.write(path, encoding='utf-8', xml_declaration=True)
    return path


def _written(out_dir, role):
    root = ET.parse(next(Path(out_dir).glob(f'*_{role}.musicxml'))).getroot()
    return root.find('part').findall('measure')


def _hidden_rests(measure):
    return [n for n in measure.findall('note') if n.find('rest') is not None and n.get('print-object') == 'no']


OVER_SA = [('C5', 1)] * 4 + [('D5', 0.5)]                               # last note starts ON the barline
OVER_TB = [('C5', 0.5), ('C5', 1), ('C5', 1), ('C5', 1), ('D5', 1)]   # last note crosses beat 4


def test_item5_overfull_bar_keeps_declared_meter_and_all_notes(tmp_path):
    p = _xml(tmp_path / 'o.musicxml', [[FULL, OVER_SA, FULL], [FULL, OVER_TB, FULL]])
    res = split_file(p, auto_layout=True)
    for r in ROLES:
        assert _ts(res, r) == [(1, '4/4')], r                       # no 9/8 written
        assert float(_bars(res, r)[1].duration.quarterLength) == 4.5, r
    assert [n for n, _, _ in _sig(_bars(res, 'Soprano')[1])] == ['C5'] * 4 + ['D5']   # nothing trimmed
    assert _sig(_bars(res, 'Bass')[1])[-1] == ('D4', 3.5, 1.0)       # not clipped, not split
    fl = [f for f in res.flags if f.reason == 'bar_length_mismatch']
    assert [f.measure for f in fl] == [2]
    assert 'Declared meter 4/4' in fl[0].detail and '4.5 q' in fl[0].detail and 'NOT changed' in fl[0].detail
    assert not [f for f in res.flags if f.reason in ('meter_from_content', 'meter_irregular')]
    out = tmp_path / 'out'
    write_satb_files(res, out, 'o', mscz=False)
    for r in ROLES:
        ms = _written(out, r)
        assert len(ms) == 3 and len(ET.parse(next(out.glob(f'*_{r}.musicxml'))).getroot().findall('.//time')) == 1
        # music21 must not split the overfull bar's last note into the next bar
        assert not [n for n in ms[1].findall('note') if n.find('tie') is not None], r
        assert [n.find('duration').text for n in ms[2].findall('note')] == [ms[2].findall('note')[0].find('duration').text]


def test_item5_carried_meter_underfull_bar_kept_short_without_hidden_rest(tmp_path):
    a = _xml(tmp_path / 'page_01.musicxml', [[FULL, FULL], [FULL, FULL]])
    b = _edit_xml(_xml(tmp_path / 'page_02.musicxml',
                       [[FULL, [('D5', 1), ('E5', 2)], FULL], [FULL, [('D5', 1), ('E5', 2)], FULL]]), drop_time=True)
    _, _, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 'u')
    for r in ROLES:
        assert _ts(res, r) == [(1, '4/4')], r                       # no 3/4 invented
        assert [float(m.duration.quarterLength) for m in _bars(res, r)] == [4.0, 4.0, 4.0, 3.0, 4.0], r
    fl = [f for f in res.flags if f.reason == 'bar_length_mismatch']
    assert [f.measure for f in fl] == [4] and 'carried from the previous page' in fl[0].detail and '3 q' in fl[0].detail
    for r in ROLES:
        ms = _written(tmp_path / 'out', r)
        assert len(ms) == 5 and not _hidden_rests(ms[3]), r       # no padding (visible or hidden)
        assert [n.find('rest') is None for n in ms[3].findall('note')] == [True, True]
        assert sum(len(m.findall('.//time')) for m in ms) == 1


def test_item5_genuine_meter_change_in_the_mxl_passes_through(tmp_path):
    bars = [FULL, FULL, [('D5', 1), ('E5', 2)], [('F5', 1), ('G5', 2)]]
    p = _edit_xml(_xml(tmp_path / 'g.musicxml', [bars, bars]), time_at=(3, 3, 4))
    res = split_file(p, auto_layout=True)
    for r in ROLES:
        assert _ts(res, r) == [(1, '4/4'), (3, '3/4')], r
        assert [float(m.duration.quarterLength) for m in _bars(res, r)] == [4.0, 4.0, 3.0, 3.0]
    assert not [f for f in res.flags if f.reason in ('bar_length_mismatch', 'meter_from_content', 'meter_missing')]


def test_item5_no_meter_anywhere_is_flagged_not_invented(tmp_path):
    bars = [[('D5', 1), ('E5', 2)], [('F5', 1), ('G5', 2)]]
    p = _edit_xml(_xml(tmp_path / 'n.musicxml', [bars, bars]), drop_time=True)
    res = split_file(p, auto_layout=True)
    for r in ROLES:
        assert _ts(res, r) == [], r
        assert [float(m.duration.quarterLength) for m in _bars(res, r)] == [3.0, 3.0]
    assert [f.reason for f in res.flags if f.reason.startswith('meter_')] == ['meter_missing']
    out = tmp_path / 'out'
    write_satb_files(res, out, 'n', mscz=False)
    for r in ROLES:
        ms = _written(out, r)
        assert sum(len(m.findall('.//time')) for m in ms) == 0 and not any(_hidden_rests(m) for m in ms), r


def test_item5_short_first_bar_of_a_later_page_kept_short_not_a_pickup(tmp_path):
    a = _xml(tmp_path / 'page_01.musicxml', [[FULL, FULL], [FULL, FULL]])
    b = _edit_xml(_xml(tmp_path / 'page_02.musicxml', [[[('D5', 1), ('E5', 2)], FULL], [[('D5', 1), ('E5', 2)], FULL]]),
                  drop_time=True)  # meter carried from page 1 (no printed TS on this page)
    _, _, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 's')
    for r in ROLES:
        ms = _bars(res, r)
        assert [float(m.duration.quarterLength) for m in ms] == [4.0, 4.0, 3.0, 4.0], r
        assert not float(getattr(ms[2], 'paddingLeft', 0.0) or 0.0)
        assert not [x for x in _sig(ms[2]) if x[0] == 'R']
    fl = [f for f in res.flags if f.reason == 'bar_length_mismatch']
    assert [f.measure for f in fl] == [3] and 'first bar of a mid-score page' in fl[0].detail
    assert not [f for f in res.flags if f.reason == 'meter_irregular']
    ms = _written(tmp_path / 'out', 'Soprano')
    assert ms[2].get('implicit') != 'yes' and not _hidden_rests(ms[2])


def test_item5_key_change_on_second_half_of_cross_page_join_is_flagged_and_restated(tmp_path):
    a = _xml(tmp_path / 'page_01.musicxml', _score([FULL, FULL, HALF_A]))
    b = _edit_xml(_xml(tmp_path / 'page_02.musicxml', _score([HALF_B, FULL])), key_at=(1, 2))
    _, log, res = split_and_merge_mxl_files([a, b], tmp_path / 'out', 'k')
    assert any('joined bar split across the page break' in line for line in log)
    for r in ROLES:
        ms = _bars(res, r)
        assert len(ms) == 4 and float(ms[2].duration.quarterLength) == 4.0
        ks = list(ms[3].getElementsByClass(m21key.KeySignature))
        assert ks and ks[0].sharps == 2, r                          # re-stated on the next bar
        assert not [k for k in ms[2].recurse().getElementsByClass(m21key.KeySignature) if k.sharps == 2]
    fl = [f for f in res.flags if f.reason == 'split_bar_key_change_dropped']
    assert [f.measure for f in fl] == [3]
    assert '2 sharps' in fl[0].detail and 're-stated at the start of bar 4' in fl[0].detail
    assert 'split_bar_key_change_dropped' in (tmp_path / 'out' / 'k_REVIEW.txt').read_text(encoding='utf-8')

def test_item5_normal_p2_bars_10_18_29_keep_carried_4_4(tmp_path):
    """Crosscheck positions 38/46/57 (Normal p2 m10/m18/m29): Tester's run wrote 9/8 there."""
    res = split_file(_fixture(tmp_path, 'b13_normal_page_02'), auto_layout=True, inherit_ts='4/4')
    for r in ROLES:
        assert _ts(res, r) == [(1, '4/4')], r
        assert [float(_bars(res, r)[i - 1].duration.quarterLength) for i in (10, 18, 29)] == [4.5] * 3
    assert sorted(f.measure for f in res.flags if f.reason == 'bar_length_mismatch') == [10, 18, 29]
    assert not [f for f in res.flags if f.reason == 'meter_from_content']

# ------------------------------- item 5b: padded underfull bars made visible
from satb_splitter.export import writer as satb_writer  # noqa: E402

SHORT = [('D5', 1), ('E5', 2)]   # 3 q under 4/4


def test_item5b_padded_bar_gets_review_row_and_a_marked_visible_rest(tmp_path):
    p = _xml(tmp_path / 'page_01.musicxml', _score([FULL, SHORT, FULL]))
    _, _, res = split_and_merge_mxl_files([p], tmp_path / 'out', 'pd')
    for r in ROLES:
        assert _ts(res, r) == [(1, '4/4')], r                      # meter unchanged
        assert [float(m.duration.quarterLength) for m in _bars(res, r)] == [4.0, 4.0, 4.0], r  # Batch 11 padding kept
        pads = [e for e in _bars(res, r)[1].recurse().notesAndRests if getattr(e.editorial, 'satb_padding', False)]
        assert [(float(e.offset), float(e.quarterLength), e.isRest) for e in pads] == [(3.0, 1.0, True)], r
    fl = [f for f in res.flags if f.reason == 'bar_length_mismatch']
    assert [f.measure for f in fl] == [2]
    assert ('Declared meter 4/4' in fl[0].detail and 'underfull by 1 q' in fl[0].detail
            and 'padded with 1 q of rest' in fl[0].detail)
    review = (tmp_path / 'out' / 'pd_REVIEW.txt').read_text(encoding='utf-8')
    assert 'bar_length_mismatch' in review and 'padded with 1 q of rest' in review
    for r in ROLES + ('SATB',):
        text = next((tmp_path / 'out').glob(f'*_{r}.musicxml')).read_text(encoding='utf-8')
        assert text.count(satb_writer.PADDING_COMMENT) == (4 if r == 'SATB' else 1), r
        after = text.split(satb_writer.PADDING_COMMENT, 1)[1].lstrip()
        note_xml = after[:after.index('</note>')]
        assert note_xml.startswith('<note') and '<rest' in note_xml and 'print-object' not in note_xml, r
        assert 'print-object="no"' not in text, r                    # no hidden rests anywhere
    ms = _written(tmp_path / 'out', 'Soprano')
    assert [n.find('rest') is not None for n in ms[1].findall('note')] == [False, False, True]


def test_item5b_rest_straddling_the_omr_length_is_split_so_only_the_padding_is_marked(tmp_path):
    bars = [FULL, [('D5', 1), ('R', 1), ('E5', 1)], FULL]           # last note at 2, OMR bar = 3 q
    p = _xml(tmp_path / 's.musicxml', [bars, [FULL, [('D5', 1), ('E5', 1)], FULL]])
    res = split_file(p, auto_layout=True)
    b = _bars(res, 'Bass')[1]                                        # T/B staff holds 2 q -> rest 2..4
    rests = [(float(e.offset), float(e.quarterLength), bool(getattr(e.editorial, 'satb_padding', False)))
             for e in b.recurse().notesAndRests if e.isRest]
    assert rests == [(2.0, 1.0, False), (3.0, 1.0, True)]


def test_item5b_pickup_and_implicit_bar_are_not_flagged(tmp_path):
    p = _xml(tmp_path / 'k.musicxml', _score([[('G5', 1)], FULL, FULL]))   # page 1, bar 1 = pickup
    res = split_file(p, auto_layout=True)
    assert not [f for f in res.flags if f.reason == 'bar_length_mismatch']
    assert float(getattr(_bars(res, 'Soprano')[0], 'paddingLeft', 0.0) or 0.0) == 3.0
    q = _xml(tmp_path / 'i.musicxml', _score([FULL, FULL, SHORT, FULL]))
    tree = ET.parse(q)
    for part in tree.getroot().findall('part'):
        part.findall('measure')[2].set('implicit', 'yes')            # Audiveris: implicit bar
    tree.write(q, encoding='utf-8', xml_declaration=True)
    res2 = split_file(q, auto_layout=True)
    assert not [f for f in res2.flags if f.reason == 'bar_length_mismatch' and f.measure == 3]


def _overfull_score():
    from music21 import meter as mm
    part = stream.Part()
    m1 = stream.Measure(number=1)
    m1.append([mm.TimeSignature('4/4'), note.Note('C4', quarterLength=3), note.Rest(quarterLength=1.5)])
    m1.editorial.satb_overfull = True
    m2 = stream.Measure(number=2)
    m2.append(note.Note('D4', quarterLength=4))
    part.append([m1, m2])
    sc = stream.Score()
    sc.insert(0, part)
    return sc


def _crossing_score():
    from music21 import meter as mm
    part = stream.Part()
    m1 = stream.Measure(number=1)
    m1.append([mm.TimeSignature('4/4'), note.Note('C4', quarterLength=3), note.Note('E4', quarterLength=2)])
    m2 = stream.Measure(number=2)
    m2.append(note.Note('D4', quarterLength=3))
    part.append([m1, m2])
    sc = stream.Score()
    sc.insert(0, part)
    return sc


def _snapshot_music21():
    from music21.musicxml import m21ToXml
    from music21.stream import makeNotation as mn
    return (mn.makeTies, mn.makeRests, m21ToXml.GeneralObjectExporter.fromScore,
            m21ToXml.GeneralObjectExporter.fromGeneralObject, stream.Stream.makeTies, stream.Stream.makeRests)


def test_item5b_writer_no_rebar_is_per_call(tmp_path):
    before = _snapshot_music21()
    sc = _overfull_score()
    satb_writer.write_musicxml(sc, tmp_path / 'o.musicxml')
    assert _snapshot_music21() == before                             # nothing global patched
    root = ET.parse(tmp_path / 'o.musicxml').getroot()
    ms = root.find('part').findall('measure')
    assert [n.find('duration').text for n in ms[0].findall('note')] == ['30240', '15120'] or \
        len(ms[0].findall('note')) == 2                              # trailing 1.5 q rest stays in bar 1
    assert len(ms[1].findall('note')) == 1 and ms[1].find('note').find('rest') is None
    # the input score is not modified by the export
    assert [float(e.quarterLength) for e in sc.parts[0].getElementsByClass(stream.Measure)[0].notesAndRests] == [3.0, 1.5]
    # an ordinary score in the same process still gets music21's default re-barring (tie)
    satb_writer.write_musicxml(_crossing_score(), tmp_path / 'c.musicxml')
    cm = ET.parse(tmp_path / 'c.musicxml').getroot().find('part').findall('measure')
    assert cm[0].findall('note')[-1].find('tie').get('type') == 'start'
    assert any(t.get('type') == 'stop' for t in cm[1].iter('tie'))   # music21 default re-barring intact


def test_item5b_writer_leaves_no_global_state_after_an_exception(tmp_path, monkeypatch):
    before = _snapshot_music21()

    def boom(self, *a, **k):
        raise RuntimeError('export failed mid-way')

    monkeypatch.setattr(stream.Measure, 'makeRests', boom)           # fails inside the per-call exporter
    with pytest.raises(RuntimeError):
        satb_writer.write_musicxml(_overfull_score(), tmp_path / 'x.musicxml')
    monkeypatch.undo()
    with pytest.raises(OSError):                                     # fails after the export, on write
        satb_writer.write_musicxml(_overfull_score(), tmp_path / 'missing_dir' / 'y.musicxml')
    assert _snapshot_music21() == before
    satb_writer.write_musicxml(_crossing_score(), tmp_path / 'c.musicxml')
    cm = ET.parse(tmp_path / 'c.musicxml').getroot().find('part').findall('measure')
    assert any(t.get('type') == 'stop' for t in cm[1].iter('tie'))   # music21 default re-barring intact