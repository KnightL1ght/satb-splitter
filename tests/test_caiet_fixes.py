"""Batch 16 (Caiet): unit tests for C16_1..C16_4."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import fitz
import pytest
from music21 import clef, meter, note, stream

from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.omr.multirest import pdf_multirest_numbers
from satb_splitter.split.engine import split_file, split_score
from satb_splitter.split.layout import detect_layout


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _m(num: int, *els, ts: str | None = None, clef_obj=None) -> stream.Measure:
    m = stream.Measure(number=num)
    if clef_obj is not None:
        m.insert(0, clef_obj)
    if ts is not None:
        m.insert(0, meter.TimeSignature(ts))
    off = 0.0
    for el in els:
        m.insert(off, el)
        off += float(el.quarterLength)
    return m


def _n(pitch: str, ql: float, lyric: str | None = None) -> note.Note:
    nt = note.Note(pitch, quarterLength=ql)
    if lyric:
        nt.lyric = lyric
    return nt


# --------------------------------------------------------- C16_1: multirest rawdict
def test_c16_1_filters_single_span_stacked_ts_and_keeps_horizontal_mr(monkeypatch, tmp_path):
    class _FakePage:
        rect = type("R", (), {"width": 600.0})()

        def get_text(self, mode="dict"):
            assert mode == "rawdict"
            return {
                "blocks": [{
                    "lines": [
                        {"spans": [
                            # Stacked 4/4 merged in one span (y spread = 7.9 > 2.5)
                            {
                                "font": "OpusStd",
                                "size": 15.7,
                                "bbox": (90.0, 97.5, 102.0, 136.6),
                                "chars": [
                                    {"c": "4", "origin": (90.25, 116.99), "bbox": (90.0, 97.5, 102.0, 128.7)},
                                    {"c": "4", "origin": (90.25, 124.89), "bbox": (90.0, 105.4, 102.0, 136.6)},
                                ],
                            },
                            # Stacked 12/8 merged in one span (y spread = 9.48 > 2.5)
                            {
                                "font": "OpusStd",
                                "size": 18.9,
                                "bbox": (79.0, 111.3, 94.0, 158.3),
                                "chars": [
                                    {"c": "1", "origin": (79.21, 134.69), "bbox": (79.0, 111.3, 84.0, 148.8)},
                                    {"c": "2", "origin": (84.38, 134.69), "bbox": (84.0, 111.3, 94.0, 148.8)},
                                    {"c": "8", "origin": (81.98, 144.17), "bbox": (81.5, 120.8, 91.5, 158.3)},
                                ],
                            },
                            # Genuine horizontal 18-bar multirest (y spread = 0.0)
                            {
                                "font": "OpusStd",
                                "size": 16.3,
                                "bbox": (300.0, 200.0, 318.0, 232.0),
                                "chars": [
                                    {"c": "1", "origin": (300.0, 220.0), "bbox": (300.0, 200.0, 309.0, 232.0)},
                                    {"c": "8", "origin": (309.0, 220.0), "bbox": (309.0, 200.0, 318.0, 232.0)},
                                ],
                            },
                        ]}
                    ]
                }]
            }

    class _FakeDoc:
        page_count = 1

        def __getitem__(self, idx):
            return _FakePage()

        def close(self):
            pass

    monkeypatch.setattr(fitz, "open", lambda *a, **k: _FakeDoc())
    words, width, has_text = pdf_multirest_numbers(tmp_path / "dummy.pdf")
    assert width == 600.0
    assert has_text is True
    assert [w.text for w in words] == ["18"]


def test_c16_1_scanned_image_pdf_still_reports_no_text_layer(tmp_path):
    pdf = tmp_path / "scan.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.draw_line((50, 200), (500, 200))
    doc.save(str(pdf))
    doc.close()

    words, _, has_text = pdf_multirest_numbers(pdf)
    assert words == []
    assert has_text is False


# --------------------------------------------------------- C16_2: layout detection
def _make_part(pid: str, name: str, bars: list[stream.Measure]) -> stream.Part:
    p = stream.Part(id=pid)
    p.partName = name
    for m in bars:
        p.append(m)
    return p


def test_c16_2_prefers_cor_lyric_pair_over_piano_when_lower_clef_present():
    # Mirrors 124 page 1: P0='Voice'(G), P1='COR'(G dominant + F in last bar, lyrics), P2/P3='Piano'(G, F, 0 lyrics)
    p0 = _make_part("P1", "Voice", [_m(1, note.Rest(quarterLength=4), ts="4/4", clef_obj=clef.TrebleClef()),
                                    _m(2, note.Rest(quarterLength=4)),
                                    _m(3, _n("C5", 4, "hi"))])
    p1 = _make_part("P2", "COR", [_m(1, _n("G4", 4, "a"), ts="4/4", clef_obj=clef.TrebleClef()),
                                  _m(2, _n("A4", 4, "b")),
                                  _m(3, _n("C3", 4, "c"), clef_obj=clef.BassClef())])
    p2 = _make_part("P3", "Piano", [_m(i, _n("E4", 4), ts="4/4" if i == 1 else None, clef_obj=clef.TrebleClef() if i == 1 else None) for i in range(1, 4)])
    p3 = _make_part("P4", "Piano", [_m(i, _n("C3", 4), ts="4/4" if i == 1 else None, clef_obj=clef.BassClef() if i == 1 else None) for i in range(1, 4)])

    sc = stream.Score()
    for p in (p0, p1, p2, p3):
        sc.insert(0, p)

    det = detect_layout(sc)
    assert det.template.name == "choir_pair"
    assert [(sp.part_index, sp.upper, sp.lower) for sp in det.template.pairs] == [
        (0, "Soprano", "Alto"),
        (1, "Tenor", "Bass"),
    ]
    assert det.template.ignore_part_indices == [2, 3]


def test_c16_2_keeps_hard_p9_upper_duet_ignored_above_choir():
    # Two treble solo/duet parts above a dominant G/F choir pair: must keep (2, 3)
    p0 = _make_part("P1", "Voice", [_m(1, _n("C5", 4, "s1"), ts="4/4", clef_obj=clef.TrebleClef())])
    p1 = _make_part("P2", "Voice", [_m(1, _n("A4", 4, "s2"), ts="4/4", clef_obj=clef.TrebleClef())])
    p2 = _make_part("P3", "Voice", [_m(1, _n("G4", 4, "sa"), ts="4/4", clef_obj=clef.TrebleClef())])
    p3 = _make_part("P4", "Voice", [_m(1, _n("C3", 4), ts="4/4", clef_obj=clef.BassClef())])

    sc = stream.Score()
    for p in (p0, p1, p2, p3):
        sc.insert(0, p)

    det = detect_layout(sc)
    assert det.template.name == "choir_pair"
    assert [(sp.part_index, sp.upper, sp.lower) for sp in det.template.pairs] == [
        (2, "Soprano", "Alto"),
        (3, "Tenor", "Bass"),
    ]
    assert det.template.ignore_part_indices == [0, 1]


# --------------------------------------------------------- C16_3: dominant TS recovery on meterless pages
def _write_mxl_xml(path: Path, parts_bars: list[list[tuple[list[tuple[str, float]], tuple[int, int] | None]]]) -> Path:
    root = ET.Element("score-partwise", version="4.0")
    pl = ET.SubElement(root, "part-list")
    for pi in range(len(parts_bars)):
        sp = ET.SubElement(pl, "score-part", id=f"P{pi + 1}")
        ET.SubElement(sp, "part-name").text = "Voice"
    div = 4
    for pi, bars in enumerate(parts_bars):
        p = ET.SubElement(root, "part", id=f"P{pi + 1}")
        for mi, (notes, ts) in enumerate(bars, 1):
            m = ET.SubElement(p, "measure", number=str(mi))
            if mi == 1 or ts is not None:
                a = ET.SubElement(m, "attributes")
                if mi == 1:
                    ET.SubElement(a, "divisions").text = str(div)
                if ts is not None:
                    t = ET.SubElement(a, "time")
                    ET.SubElement(t, "beats").text = str(ts[0])
                    ET.SubElement(t, "beat-type").text = str(ts[1])
                if mi == 1:
                    c = ET.SubElement(a, "clef")
                    ET.SubElement(c, "sign").text = "G" if pi == 0 else "F"
                    ET.SubElement(c, "line").text = "2" if pi == 0 else "4"
            for pitch, ql in notes:
                n = ET.SubElement(m, "note")
                pe = ET.SubElement(n, "pitch")
                ET.SubElement(pe, "step").text = pitch[0]
                ET.SubElement(pe, "octave").text = pitch[1]
                ET.SubElement(n, "duration").text = str(int(round(ql * div)))
                ET.SubElement(n, "voice").text = "1"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_c16_3_reverts_poisoned_transient_5_4_to_dominant_4_4_on_meterless_page(tmp_path):
    # Page 1: m1..m2=4/4, m3=5/4 (5q), m4..m6=4q (missing return 4/4), m7=5/4 (5q), m8=4q
    p1_bars_sa = [
        ([("C5", 4.0)], (4, 4)),
        ([("D5", 4.0)], None),
        ([("E5", 5.0)], (5, 4)),
        ([("F5", 4.0)], None),
        ([("G5", 4.0)], None),
        ([("A5", 4.0)], None),
        ([("E5", 5.0)], (5, 4)),
        ([("C5", 4.0)], None),
    ]
    p1_bars_tb = [
        ([("C3", 4.0)], (4, 4)),
        ([("D3", 4.0)], None),
        ([("E3", 5.0)], (5, 4)),
        ([("F3", 4.0)], None),
        ([("G3", 4.0)], None),
        ([("A3", 4.0)], None),
        ([("E3", 5.0)], (5, 4)),
        ([("C3", 4.0)], None),
    ]
    # Page 2: meterless, 8 bars all holding 4q
    p2_bars_sa = [([("C5", 4.0)], None) for _ in range(8)]
    p2_bars_tb = [([("C3", 4.0)], None) for _ in range(8)]

    p1 = _write_mxl_xml(tmp_path / "page_01.musicxml", [p1_bars_sa, p1_bars_tb])
    p2 = _write_mxl_xml(tmp_path / "page_02.musicxml", [p2_bars_sa, p2_bars_tb])

    written, log, res = split_and_merge_mxl_files([p1, p2], tmp_path / "out", "song")
    assert any("meterless page matches dominant meter 4/4" in ln for ln in log)
    sop_ms = list(res.scores["Soprano"].parts[0].getElementsByClass(stream.Measure))
    assert len(sop_ms) == 16
    assert sop_ms[8].timeSignature is not None and sop_ms[8].timeSignature.ratioString == "4/4"
    for idx in range(8, 16):
        assert float(sop_ms[idx].duration.quarterLength) == 4.0
        ts = sop_ms[idx].timeSignature or sop_ms[idx].getContextByClass(meter.TimeSignature)
        assert ts is not None and ts.ratioString == "4/4"
    sop_xml = ET.parse(written["Soprano"]).getroot()
    m9 = sop_xml.find("./part/measure[@number='9']")
    assert m9 is not None
    assert m9.findtext("./attributes/time/beats") == "4"
    assert m9.findtext("./attributes/time/beat-type") == "4"
    assert not [f for f in res.flags if f.measure and f.measure >= 9 and f.reason == "bar_length_mismatch"]


def test_c16_3_preserves_genuine_meter_shift_onto_meterless_page(tmp_path):
    # Page 1: 4 bars of 4/4, then 2 bars of 3/4 -> Page 2 meterless 6 bars of 3q must stay 3/4
    p1_sa = [([("C5", 4.0)], (4, 4))] + [([("D5", 4.0)], None) for _ in range(3)] + [([("E5", 3.0)], (3, 4)), ([("F5", 3.0)], None)]
    p1_tb = [([("C3", 4.0)], (4, 4))] + [([("D3", 4.0)], None) for _ in range(3)] + [([("E3", 3.0)], (3, 4)), ([("F3", 3.0)], None)]
    p2_sa = [([("G4", 3.0)], None) for _ in range(6)]
    p2_tb = [([("C3", 3.0)], None) for _ in range(6)]

    p1 = _write_mxl_xml(tmp_path / "page_01.musicxml", [p1_sa, p1_tb])
    p2 = _write_mxl_xml(tmp_path / "page_02.musicxml", [p2_sa, p2_tb])

    _, log, res = split_and_merge_mxl_files([p1, p2], tmp_path / "out", "shift")
    assert not any("meterless page matches dominant meter" in ln for ln in log)
    sop_ms = list(res.scores["Soprano"].parts[0].getElementsByClass(stream.Measure))
    for idx in range(6, 12):
        assert float(sop_ms[idx].duration.quarterLength) == 3.0
        ts = sop_ms[idx].timeSignature or sop_ms[idx].getContextByClass(meter.TimeSignature)
        assert ts is not None and ts.ratioString == "3/4"


# --------------------------------------------------------- C16_4: staff_shift gate
def test_c16_4_underfull_partner_does_not_trigger_staff_shift_or_drop_notes():
    # Mirrors 125 p2 m9/m10: bar 2 P0=2.5q (underfull) & P1=8.0q; bar 3 P0=6.25q & P1=2.0q (underfull)
    sa = stream.Part(id="SA")
    sa.partName = "S,A"
    sa.append(_m(1, _n("C5", 4.0), ts="4/4", clef_obj=clef.TrebleClef()))
    sa.append(_m(2, _n("A4", 1.0), _n("A4", 1.0), _n("G4", 0.5)))
    sa.append(_m(3, _n("A4", 4.0), _n("D4", 0.5), _n("F#4", 1.0), _n("F#4", 0.25), _n("A4", 0.5)))

    tb = stream.Part(id="TB")
    tb.partName = "T,B"
    tb.append(_m(1, _n("C3", 4.0), ts="4/4", clef_obj=clef.BassClef()))
    tb.append(_m(2, _n("D4", 2.0), _n("D4", 2.0), _n("D4", 2.0), _n("D4", 2.0)))
    tb.append(_m(3, _n("B3", 0.5), _n("D4", 0.5), _n("B3", 0.5), _n("D4", 0.5)))

    sc = stream.Score()
    sc.insert(0, sa)
    sc.insert(0, tb)

    res = split_score(sc, auto_layout=True)
    assert not [f for f in res.flags if f.reason == "overflow_dropped"]
    assert not [f for f in res.flags if f.reason == "omr_staff_shift"]
    tb_b2 = list(res.scores["Bass"].parts[0].getElementsByClass(stream.Measure))[1]
    sa_b3 = list(res.scores["Soprano"].parts[0].getElementsByClass(stream.Measure))[2]
    assert len(list(tb_b2.recurse().notes)) == 4
    assert float(tb_b2.duration.quarterLength) == 8.0
    assert len(list(sa_b3.recurse().notes)) == 5
    assert float(sa_b3.duration.quarterLength) == 6.25


def test_c16_4_non_conflicting_exact_fit_plus_overfull_still_preserves_notes():
    # P0 fits 4/4 (G clef), P1 overfull 8.0q (F clef, no clef shift/role_conflict) -> keep all notes
    sa = stream.Part(id="SA")
    sa.partName = "S,A"
    sa.append(_m(1, _n("C5", 4.0), ts="4/4", clef_obj=clef.TrebleClef()))
    sa.append(_m(2, _n("E5", 4.0)))

    tb = stream.Part(id="TB")
    tb.partName = "T,B"
    tb.append(_m(1, _n("C3", 4.0), ts="4/4", clef_obj=clef.BassClef()))
    tb.append(_m(2, _n("D3", 2.0), _n("E3", 2.0), _n("F3", 2.0), _n("G3", 2.0)))

    sc = stream.Score()
    sc.insert(0, sa)
    sc.insert(0, tb)

    res = split_score(sc, auto_layout=True)
    assert not [f for f in res.flags if f.reason == "overflow_dropped"]
    tb_b2 = list(res.scores["Bass"].parts[0].getElementsByClass(stream.Measure))[1]
    assert len(list(tb_b2.recurse().notes)) == 4
