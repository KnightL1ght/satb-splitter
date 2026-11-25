"""Batch 17 (Caiet): unit tests for intrinsic F5 upgrades B17_1..B17_3 and negative guards."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from music21 import note, stream

from satb_splitter.export.writer import preflight_musicxml
from satb_splitter.model.config import SplitConfig
from satb_splitter.split.engine import split_file

ROLES = ("Soprano", "Alto", "Tenor", "Bass")


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _write_poly_mxl(
    path: Path,
    staves: list[list[list[tuple[str, list[dict]]]]],
    *,
    beats: int = 4,
    beat_type: int = 4,
    clefs: tuple[tuple[str, str], ...] = (("G", "2"), ("F", "4")),
) -> Path:
    """Write multi-voice MusicXML preserving exact onset_q, duration, pitches, default-x, and lyrics."""
    root = ET.Element("score-partwise", version="4.0")
    pl = ET.SubElement(root, "part-list")
    for k in range(len(staves)):
        sp = ET.SubElement(pl, "score-part", id=f"P{k + 1}")
        ET.SubElement(sp, "part-name").text = "Voice"
    div = 4
    for pi, bars in enumerate(staves):
        p = ET.SubElement(root, "part", id=f"P{pi + 1}")
        sign, line = clefs[pi]
        for mi, bar_voices in enumerate(bars):
            m = ET.SubElement(p, "measure", number=str(mi + 1))
            if mi == 0:
                a = ET.SubElement(m, "attributes")
                ET.SubElement(a, "divisions").text = str(div)
                t = ET.SubElement(a, "time")
                ET.SubElement(t, "beats").text = str(beats)
                ET.SubElement(t, "beat-type").text = str(beat_type)
                c = ET.SubElement(a, "clef")
                ET.SubElement(c, "sign").text = sign
                ET.SubElement(c, "line").text = line
            cursor_ticks = 0
            for vid, evs in bar_voices:
                if cursor_ticks > 0:
                    bk = ET.SubElement(m, "backup")
                    ET.SubElement(bk, "duration").text = str(cursor_ticks)
                    cursor_ticks = 0
                for ev in evs:
                    on_ticks = int(round(float(Fraction(ev["on"])) * div))
                    if on_ticks > cursor_ticks:
                        fw = ET.SubElement(m, "forward")
                        ET.SubElement(fw, "duration").text = str(on_ticks - cursor_ticks)
                        cursor_ticks = on_ticks
                    dur_ticks = int(round(float(Fraction(ev["q"])) * div))
                    pitches = ev.get("pitches")
                    if pitches is None:
                        pitches = [ev["pitch"]] if ev.get("pitch") else []
                    for ci, pstr in enumerate(pitches or [None]):
                        attrib = {}
                        if ev.get("x") is not None:
                            attrib["default-x"] = str(ev["x"])
                        n = ET.SubElement(m, "note", attrib)
                        if ci > 0:
                            ET.SubElement(n, "chord")
                        if pstr is None:
                            ET.SubElement(n, "rest")
                        else:
                            pe = ET.SubElement(n, "pitch")
                            ET.SubElement(pe, "step").text = pstr[0]
                            if len(pstr) == 3 and pstr[1] == "b":
                                ET.SubElement(pe, "alter").text = "-1"
                            elif len(pstr) == 3 and pstr[1] == "#":
                                ET.SubElement(pe, "alter").text = "1"
                            ET.SubElement(pe, "octave").text = pstr[-1]
                        ET.SubElement(n, "duration").text = str(dur_ticks)
                        ET.SubElement(n, "voice").text = str(vid)
                        if ev.get("type"):
                            ET.SubElement(n, "type").text = ev["type"]
                        for _ in range(ev.get("dots", 0)):
                            ET.SubElement(n, "dot")
                        if ev.get("lyric") and ci == 0:
                            ly = ET.SubElement(n, "lyric", number="1")
                            ET.SubElement(ly, "syllabic").text = "single"
                            ET.SubElement(ly, "text").text = ev["lyric"]
                    cursor_ticks += dur_ticks
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _sig(m: stream.Measure) -> list[tuple[str, float, float]]:
    return [
        ("R" if e.isRest else "+".join(p.nameWithOctave for p in e.pitches), float(e.offset), float(e.quarterLength))
        for e in m.recurse().notesAndRests
        if not e.duration.isGrace
    ]


def _bars(res, role: str) -> list[stream.Measure]:
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def test_b17_1_held_note_beat_split_124_m39_style(tmp_path):
    p = _write_poly_mxl(
        tmp_path / "b17_1.musicxml",
        [
            [[
                ("1", [
                    {"on": "0", "q": "2", "pitch": "A4", "type": "half", "x": 62},
                    {"on": "2", "q": "1", "pitches": ["F#4", "A4"], "type": "quarter", "x": 209, "lyric": "na"},
                    {"on": "3", "q": "1", "pitches": ["E4", "G4"], "type": "quarter", "x": 283, "lyric": "va"},
                ]),
                ("2", [
                    {"on": "0", "q": "1", "pitch": "E4", "type": "quarter", "x": 59},
                    {"on": "1", "q": "1", "pitch": "F#4", "type": "quarter", "x": 109},
                ]),
            ]],
            [[("1", [{"on": "0", "q": "4", "pitches": ["D3", "A3"], "type": "whole", "x": 60}])]],
        ],
    )
    res = split_file(p, SplitConfig(), auto_layout=True)
    for role in ROLES:
        preflight_musicxml(res.scores[role], flags=res.flags, role=role)
    assert not [f for f in res.flags if f.reason == "underfull_measure"]
    assert _sig(_bars(res, "Soprano")[0]) == [("A4", 0.0, 2.0), ("A4", 2.0, 1.0), ("G4", 3.0, 1.0)]
    assert _sig(_bars(res, "Alto")[0]) == [("E4", 0.0, 1.0), ("F#4", 1.0, 1.0), ("F#4", 2.0, 1.0), ("E4", 3.0, 1.0)]
    sop_lyrics = [ly.text for n in _bars(res, "Soprano")[0].recurse().notes for ly in n.lyrics]
    assert sop_lyrics == ["na", "va"]


def test_b17_2_disjoint_multi_partial_merge_and_overlap_guard(tmp_path):
    # Disjoint partials v2=[0..1.5) and v3=[2..4) -> merge and fill [1.5..2) from v1
    p_ok = _write_poly_mxl(
        tmp_path / "b17_2_ok.musicxml",
        [
            [[("1", [{"on": "0", "q": "4", "pitches": ["G4", "Bb4"], "type": "whole", "x": 32}])]],
            [[
                ("1", [
                    {"on": "0", "q": "1.5", "pitch": "Bb3", "type": "quarter", "dots": 1, "x": 32},
                    {"on": "3/2", "q": "0.25", "pitch": "Ab3", "type": "16th", "x": 81},
                    {"on": "7/4", "q": "0.25", "pitch": "G3", "type": "16th", "x": 107},
                    {"on": "2", "q": "1.5", "pitch": "Ab3", "type": "quarter", "dots": 1, "x": 133},
                    {"on": "7/2", "q": "0.5", "pitches": ["Eb3", "G3"], "type": "eighth", "x": 199},
                ]),
                ("2", [{"on": "0", "q": "1.5", "pitch": "Bb3", "type": "quarter", "dots": 1, "x": 32}]),
                ("3", [{"on": "2", "q": "2", "pitch": "Bb2", "type": "half", "x": 133}]),
            ]],
        ],
    )
    res_ok = split_file(p_ok, SplitConfig(), auto_layout=True)
    for role in ROLES:
        preflight_musicxml(res_ok.scores[role], flags=res_ok.flags, role=role)
    assert not [f for f in res_ok.flags if f.reason == "underfull_measure"]
    assert _sig(_bars(res_ok, "Bass")[0]) == [
        ("B-3", 0.0, 1.5), ("A-3", 1.5, 0.25), ("G3", 1.75, 0.25), ("B-2", 2.0, 2.0)
    ]

    # Overlapping partials v2=[0..3) and v3=[1.5..2) must NOT trigger B17_2 (e.g. 126 m6)
    p_ov = _write_poly_mxl(
        tmp_path / "b17_2_overlap.musicxml",
        [
            [[
                ("1", [
                    {"on": "0", "q": "1.5", "pitch": "G4", "type": "quarter", "dots": 1, "x": 20},
                    {"on": "3/2", "q": "0.5", "pitch": "C5", "type": "eighth", "x": 110},
                    {"on": "2", "q": "2", "pitch": "A4", "type": "half", "x": 170},
                ]),
                ("2", [{"on": "0", "q": "3", "pitch": "D4", "type": "half", "dots": 1, "x": 20}]),
                ("3", [{"on": "3/2", "q": "0.5", "pitch": "C4", "type": "eighth", "x": 110}]),
            ]],
            [[("1", [{"on": "0", "q": "4", "pitches": ["C3", "G3"], "type": "whole", "x": 20}])]],
        ],
    )
    res_ov = split_file(p_ov, SplitConfig(), auto_layout=True)
    assert not [f for f in res_ov.flags if f.reason == "omr_graft_summary"]
    assert [f for f in res_ov.flags if f.reason == "underfull_measure"]


def test_b17_3_split_handoff_completion_126_style(tmp_path):
    p = _write_poly_mxl(
        tmp_path / "b17_3.musicxml",
        [
            [[("1", [{"on": "0", "q": "4", "pitches": ["F4", "A4"], "type": "whole", "x": 16}])]],
            [[
                ("1", [
                    {"on": "0", "q": "1", "pitches": ["C3", "C4"], "type": "quarter", "x": 16, "lyric": "do"},
                    {"on": "1", "q": "0.25", "pitch": "Bb3", "type": "16th", "x": 66, "lyric": "mi"},
                    {"on": "5/4", "q": "0.75", "pitch": "A3", "type": "eighth", "dots": 1, "x": 98},
                ]),
                ("2", [
                    {"on": "1", "q": "1", "pitch": "C3", "type": "quarter", "x": 66},
                    {"on": "2", "q": "1", "pitches": ["Bb3", "C3"], "type": "quarter", "x": 139},
                    {"on": "3", "q": "1", "pitches": ["C4", "C3"], "type": "quarter", "x": 210},
                ]),
            ]],
        ],
    )
    res = split_file(p, SplitConfig(), auto_layout=True)
    for role in ROLES:
        preflight_musicxml(res.scores[role], flags=res.flags, role=role)
    assert not [f for f in res.flags if f.reason == "underfull_measure"]
    assert any("intrinsic_b17_3_handoff" in f.detail for f in res.flags if f.reason == "omr_graft_summary")
    assert _sig(_bars(res, "Tenor")[0]) == [
        ("C4", 0.0, 1.0), ("B-3", 1.0, 0.25), ("A3", 1.25, 0.75), ("B-3", 2.0, 1.0), ("C4", 3.0, 1.0)
    ]
    assert _sig(_bars(res, "Bass")[0]) == [
        ("C3", 0.0, 1.0), ("C3", 1.0, 1.0), ("C3", 2.0, 1.0), ("C3", 3.0, 1.0)
    ]


def test_b17_negative_late_entry_pickup_does_not_trigger(tmp_path):
    p = _write_poly_mxl(
        tmp_path / "b17_neg_pickup.musicxml",
        [
            [[
                ("1", [
                    {"on": "0", "q": "2", "pitch": "G4", "type": "half", "x": 20},
                    {"on": "2", "q": "2", "pitch": "A4", "type": "half", "x": 120},
                ]),
                ("2", [{"on": "2", "q": "2", "pitch": "D4", "type": "half", "x": 120}]),
            ]],
            [[("1", [{"on": "0", "q": "4", "pitches": ["G2", "D3"], "type": "whole", "x": 20}])]],
        ],
    )
    res = split_file(p, SplitConfig(), auto_layout=True)
    assert not [f for f in res.flags if f.reason == "omr_graft_summary"]
    assert _sig(_bars(res, "Alto")[0]) == [("R", 0.0, 2.0), ("D4", 2.0, 2.0)]


def test_b17_negative_missing_rest_union_lt_expected_q_does_not_trigger(tmp_path):
    p = _write_poly_mxl(
        tmp_path / "b17_neg_short.musicxml",
        [
            [[("1", [{"on": "0", "q": "4", "pitches": ["F4", "A4"], "type": "whole", "x": 20}])]],
            [[
                ("1", [
                    {"on": "0", "q": "1", "pitch": "D4", "type": "quarter", "x": 20},
                    {"on": "1", "q": "1", "pitch": "E4", "type": "quarter", "x": 70},
                ]),
                ("2", [
                    {"on": "1", "q": "1", "pitch": "B3", "type": "quarter", "x": 70},
                    {"on": "2", "q": "1", "pitch": "C4", "type": "quarter", "x": 120},
                ]),
            ]],
        ],
    )
    res = split_file(p, SplitConfig(), auto_layout=True)
    assert not [f for f in res.flags if f.reason == "omr_graft_summary"]
    assert [f for f in res.flags if f.reason == "underfull_measure"]
