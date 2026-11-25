"""Batch 10: incomplete Audiveris XML, identity alignment, export preflight."""
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import os
import time
import xml.etree.ElementTree as ET

import pytest
from music21 import clef, converter, meter, note, stream

from satb_splitter.musicxml_validation import (
    align_score_measures, export_log_issues, load_validated_score,
    validate_musicxml, nearby_log_issues,
)
from satb_splitter.split.engine import split_file, split_score
from satb_splitter.export.writer import write_satb_files, MusicXMLPreflightError
from satb_splitter.model.config import ReviewFlag, SplitResult

FIXTURE = Path(__file__).parent / 'fixtures/batch10_page04_incomplete.musicxml'


def xml_page(path, *, missing_divisions=True, missing_first=True):
    root = ET.Element('score-partwise', version='4.0')
    pl = ET.SubElement(root, 'part-list')
    for i in range(2):
        sp = ET.SubElement(pl, 'score-part', id=f'P{i+1}')
        ET.SubElement(sp, 'part-name').text = ['SA', 'TB'][i]
    for i in range(2):
        p = ET.SubElement(root, 'part', id=f'P{i+1}')
        for k in range(1, 4):
            if i == 0 and k == 1 and missing_first:
                continue
            m = ET.SubElement(p, 'measure', number=str(k))
            if k == (2 if i == 0 and missing_first else 1):
                a = ET.SubElement(m, 'attributes')
                if i or not missing_divisions:
                    ET.SubElement(a, 'divisions').text = '12'
                t = ET.SubElement(a, 'time')
                ET.SubElement(t, 'beats').text = '2'
                ET.SubElement(t, 'beat-type').text = '4'
                c = ET.SubElement(a, 'clef')
                ET.SubElement(c, 'sign').text = 'G' if i == 0 else 'F'
                ET.SubElement(c, 'line').text = '2' if i == 0 else '4'
            n = ET.SubElement(m, 'note')
            pitch = ET.SubElement(n, 'pitch')
            ET.SubElement(pitch, 'step').text = ['C', 'D', 'E'][k-1] if i == 0 else 'G'
            ET.SubElement(pitch, 'octave').text = '5' if i == 0 else '2'
            ET.SubElement(n, 'duration').text = '6'
            ET.SubElement(n, 'type').text = 'eighth'
    ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
    return path


def test_verified_sibling_divisions_and_identity_alignment(tmp_path):
    path = xml_page(tmp_path / 'source.musicxml')
    original = path.read_bytes()
    score, flags = load_validated_score(path)
    assert any(f.reason == 'xml_missing_divisions' and 'divisions=12' in f.detail for f in flags)
    assert all(n.quarterLength == 0.5 for n in score.parts[0].recurse().notes)
    result = split_file(path)
    ms = list(result.scores['Soprano'].parts[0].getElementsByClass(stream.Measure))
    assert len(ms) == 3
    assert not list(ms[0].recurse().notes)  # missing owner must not steal TB pitches
    assert [n.nameWithOctave for n in ms[1].recurse().notes] == ['D5']
    assert [n.nameWithOctave for n in ms[2].recurse().notes] == ['E5']
    assert any(f.reason == 'xml_missing_measure' and f.measure == 1 for f in result.flags)
    assert path.read_bytes() == original
    assert len(write_satb_files(result, tmp_path / 'out', 'repaired')) == 5


def test_real_sanitized_audiveris_511_page(tmp_path):
    report = validate_musicxml(FIXTURE)
    assert not report.fatal
    assert [(f.part_index, f.measure) for f in report.flags if f.reason == 'xml_missing_measure'] == [(1, 1), (1, 7), (1, 8), (1, 9)]
    score, flags = load_validated_score(FIXTURE)
    m3 = next(m for m in score.parts[1].getElementsByClass(stream.Measure) if m.number == 3)
    assert [n.quarterLength for n in m3.recurse().notes] == [0.5]*4
    assert [tuple(p.nameWithOctave for p in n.pitches) for n in m3.recurse().notes] == [
        ('G#4', 'B4'), ('F##4', 'A#4'), ('G#4', 'B4'), ('A4', 'C#5')]
    align_score_measures(score)
    assert [m.number for m in score.parts[1].getElementsByClass(stream.Measure)] == list(range(1,17))
    result = split_file(FIXTURE, auto_layout=True)
    paths = write_satb_files(result, tmp_path, 'actual')
    assert len(paths) == 5
    assert all(len(list(sc.parts[0].getElementsByClass(stream.Measure))) == 16 for sc in result.scores.values())


def test_missing_divisions_without_verified_sibling_blocked(tmp_path):
    path = xml_page(tmp_path / 'ambiguous.musicxml')
    r = ET.parse(path)
    a = r.getroot().findall('part')[1].find('measure/attributes')
    a.remove(a.find('divisions'))
    r.write(path)
    with pytest.raises(ValueError, match='Incomplete MusicXML.*missing divisions'):
        split_file(path)


def test_conflicting_sibling_divisions_not_guessed(tmp_path):
    path = xml_page(tmp_path / 'conflict.musicxml')
    r = ET.parse(path)
    p = r.getroot().findall('part')[1]
    a = ET.SubElement(p.findall('measure')[1], 'attributes')
    ET.SubElement(a, 'divisions').text = '24'
    r.write(path)
    assert validate_musicxml(path).fatal


def test_padding_rest_preflight_reports_part_measure_and_completeness(tmp_path):
    # The problematic generated rest: nearly three quarters, not a tiny note.
    s = stream.Score(); p = stream.Part(id='Soprano'); m = stream.Measure(number=63)
    m.append(note.Rest(quarterLength=Fraction(3)-Fraction(1,420)))
    p.append(m); s.append(p)
    assert m.notesAndRests[0].duration.tuplets[0].durationNormal.type == '2048th'
    flags = [ReviewFlag(None, 1, None, 'xml_missing_divisions', 'P2 missing divisions; sibling 12'),
             ReviewFlag(62, 1, None, 'xml_missing_measure', 'P2 missing XML measure 1')]
    result = SplitResult(scores={'Soprano': s}, flags=flags)
    with pytest.raises(MusicXMLPreflightError) as e:
        write_satb_files(result, tmp_path, 'unsafe')
    text = str(e.value)
    assert 'part Soprano, measure 63' in text
    assert 'xml_missing_divisions' in text and 'xml_missing_measure' in text
    assert 'Cannot convert' not in text
    assert not list(tmp_path.glob('*.musicxml'))


@pytest.mark.parametrize('ql,blocked', [(Fraction(1,512),True),(Fraction(1,256),False)])
def test_shortest_serializable_duration_boundary(tmp_path, ql, blocked):
    s = stream.Score(); p = stream.Part(); m = stream.Measure(number=1)
    m.append(note.Note('C5',quarterLength=ql));p.append(m);s.append(p)
    from satb_splitter.export.writer import preflight_musicxml
    if blocked:
        with pytest.raises(MusicXMLPreflightError, match='measure 1'):
            preflight_musicxml(s)
    else:
        preflight_musicxml(s)


@pytest.mark.parametrize('line', [
    'PartwiseBuilder WARN Error exporting measure 7 java.lang.NullPointerException',
    'NPE during measure export', 'No timeOffset for HeadChordInter',
    'MeasureStack#17 no correct rhythm', 'abnormal measure #4',
    'Measure #2 is abnormal', 'export exception in measure 1', 'Could not export score',
])
def test_incomplete_export_log_phrases(line):
    assert export_log_issues(line)
    assert not export_log_issues('INFO Export complete; correct rhythm; normal measure')


def test_complete_score_unchanged_and_no_false_flags(tmp_path):
    path = xml_page(tmp_path / 'good.musicxml',missing_divisions=False,missing_first=False)
    report = validate_musicxml(path)
    assert not report.flags and not report.fatal and not report.changed
    source = converter.parse(path)
    expected = split_score(source)
    actual = split_file(path)
    for role in actual.scores:
        assert [(n.nameWithOctave if n.isNote else str(n), n.quarterLength) for n in actual.scores[role].flatten().notesAndRests] == [
            (n.nameWithOctave if n.isNote else str(n), n.quarterLength) for n in expected.scores[role].flatten().notesAndRests]
    assert not any(f.reason.startswith('xml_') for f in actual.flags)


def test_internal_gap_alignment_for_direct_score():
    s = stream.Score()
    for pi in range(2):
        p=stream.Part(id=str(pi))
        for i in ([1,3] if pi == 0 else [1,2,3]):
            m=stream.Measure(number=i);m.insert(0,meter.TimeSignature('4/4'))
            m.append(note.Note('C5' if pi == 0 else 'C3',quarterLength=4));p.append(m)
        s.append(p)
    result=split_score(s)
    ms=list(result.scores['Soprano'].parts[0].getElementsByClass(stream.Measure))
    assert not list(ms[1].recurse().notes) and list(ms[2].recurse().notes)
    assert any(f.reason == 'xml_missing_measure' and f.measure == 2 for f in result.flags)


def test_duplicate_id_rejected_instead_of_index_fallback(tmp_path):
    path=xml_page(tmp_path/'duplicate.musicxml',missing_divisions=False,missing_first=False)
    r=ET.parse(path); r.getroot().find('part').findall('measure')[1].set('number','1');r.write(path)
    with pytest.raises(ValueError,match='duplicate.*measure identity'):
        split_file(path)


def test_latest_log_only_not_historical_error(tmp_path):
    path=xml_page(tmp_path/'page_01.musicxml',missing_divisions=False,missing_first=False)
    old=tmp_path/'page_01-old.log'; old.write_text('NullPointerException')
    os.utime(old,(1,1))
    new=tmp_path/'page_01-new.log'; new.write_text('INFO Export complete')
    assert not nearby_log_issues(path)
    new.write_text('No timeOffset for measure 4')
    _,flags=load_validated_score(path)
    assert any(f.reason=='omr_export_incomplete' for f in flags)


@pytest.mark.parametrize('mode', ['stale','nonzero','review','good'])
def test_audiveris_freshness_returncode_and_review(monkeypatch,tmp_path,mode):
    import satb_splitter.omr.audiveris as a
    out=tmp_path/'out';out.mkdir();mxl=out/'page_01.mxl'
    # Uncompressed XML renamed .mxl is valid to our raw inspector.
    if mode=='stale':
        xml_page(mxl,missing_divisions=False,missing_first=False)
        os.utime(mxl,(1,1))
    monkeypatch.setattr(a,'find_audiveris',lambda explicit: tmp_path/'Audiveris.exe')
    def run(*args,**kwargs):
        if mode!='stale':
            xml_page(mxl,missing_divisions=mode=='review',missing_first=mode=='review')
        return SimpleNamespace(returncode=1 if mode=='nonzero' else 0,
                               stdout='No timeOffset measure 1' if mode=='review' else '',stderr='')
    monkeypatch.setattr(a.subprocess,'run',run)
    result=a.run_audiveris_export(tmp_path/'page_01.pdf',out)
    if mode in ('stale','nonzero'):
        assert not result.success and not result.mxl_paths
    elif mode=='review':
        assert result.success and result.review_issues
        assert any('missing divisions' in x for x in result.review_issues)
        from satb_splitter.omr.pipeline import OmrResult
        omr=OmrResult(tmp_path/'in.pdf',tmp_path,page_runs=[result])
        assert not omr.ok and omr.review_count==1
    else:
        assert result.success and not result.review_issues


def test_absent_declared_part_is_incomplete(tmp_path):
    path=xml_page(tmp_path/'missing_part.musicxml',missing_divisions=False,missing_first=False)
    r=ET.parse(path);root=r.getroot();root.remove(root.findall('part')[1]);r.write(path)
    with pytest.raises(ValueError,match='declared parts absent'):
        split_file(path)


def test_grace_duration_does_not_trigger_preflight():
    from satb_splitter.export.writer import preflight_musicxml
    s=stream.Score();p=stream.Part();m=stream.Measure(number=1)
    m.append(note.Note('C5').getGrace());p.append(m);s.append(p)
    preflight_musicxml(s)


def test_complete_export_with_log_exception_requires_review(monkeypatch,tmp_path):
    import satb_splitter.omr.audiveris as a
    out=tmp_path/'out';out.mkdir()
    monkeypatch.setattr(a,'find_audiveris',lambda explicit:tmp_path/'Audiveris.exe')
    def run(*args,**kwargs):
        xml_page(out/'page_01.mxl',missing_divisions=False,missing_first=False)
        return SimpleNamespace(returncode=0,stdout='WARN Error exporting measure 7\njava.lang.NullPointerException',stderr='')
    monkeypatch.setattr(a.subprocess,'run',run)
    result=a.run_audiveris_export(tmp_path/'page_01.pdf',out)
    assert result.success and result.review_issues
    assert 'export exception' in result.review_issues[0]


def test_review_report_accompanies_recovered_context_outputs(tmp_path):
    path=xml_page(tmp_path/'source.musicxml')
    result=split_file(path)
    write_satb_files(result,tmp_path/'out','reviewed')
    text=(tmp_path/'out/reviewed_REVIEW.txt').read_text(encoding='utf-8')
    assert 'INCOMPLETE OMR' in text and 'xml_missing_divisions' in text and 'xml_missing_measure' in text
