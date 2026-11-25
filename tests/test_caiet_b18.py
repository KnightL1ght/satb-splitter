"""Batch 18 (Caiet): unit and headless PySide6 GUI tests for B18_1..B18_5."""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from music21 import chord, clef, expressions, meter, note, stream

from satb_splitter import cli
from satb_splitter.export.merge import split_and_merge_mxl_files
from satb_splitter.model.config import (
    DivisiChoice,
    DivisiMode,
    LayoutTemplate,
    SplitConfig,
    StaffPair,
)
from satb_splitter.omr.markers import _join_spans, pdf_marker_candidates
from satb_splitter.omr.multirest import _is_chord_symbol_digit, pdf_multirest_numbers
from satb_splitter.split.divisi import (
    PitchCandidate,
    VoiceLeadingState,
    chord_stem,
    resolve_simultaneous_chord,
)
from satb_splitter.split.engine import split_file, split_score
from satb_splitter.split.multirest import apply_multirests
from satb_splitter.split.topstaff import plan_top_solo_tenor

ROLES = ("Soprano", "Alto", "Tenor", "Bass")


@pytest.fixture(autouse=True)
def _no_musescore(monkeypatch):
    monkeypatch.setenv("SATB_MSCZ", "0")


def _n(pitch: str, ql: float = 1.0, *, stem: str | None = None, lyric: str | None = None, tie_type: str | None = None):
    el = note.Note(pitch, quarterLength=ql)
    if stem:
        el.stemDirection = stem
    if lyric:
        el.addLyric(lyric)
    if tie_type:
        from music21 import tie as m21tie
        el.tie = m21tie.Tie(tie_type)
    return el


def _c(pitches: list[str], ql: float = 1.0, *, stem: str | None = None, sub_stems: list[str] | None = None):
    el = chord.Chord(pitches, quarterLength=ql)
    if stem:
        el.stemDirection = stem
    if sub_stems:
        for sub, sd in zip(el.notes, sub_stems):
            sub.stemDirection = sd
    return el


def _bars(res, role: str) -> list[stream.Measure]:
    return list(res.scores[role].parts[0].getElementsByClass(stream.Measure))


def _bar_pitches(m: stream.Measure) -> list[str]:
    out = []
    for e in m.recurse().notesAndRests:
        if e.isRest:
            continue
        ps = sorted(e.pitches, key=lambda p: -p.ps)
        out.append("+".join(p.nameWithOctave for p in ps))
    return out


def _two_staff(sa_bars, tb_bars) -> stream.Score:
    sc = stream.Score()
    sa = stream.Part(id="SA")
    sa.partName = "Soprano, Alto"
    for idx, b in enumerate(sa_bars, 1):
        b.number = idx
        sa.append(b)
    tb = stream.Part(id="TB")
    tb.partName = "Tenor, Bass"
    for idx, b in enumerate(tb_bars, 1):
        b.number = idx
        tb.append(b)
    sc.insert(0, sa)
    sc.insert(0, tb)
    return sc


def _make_bar(num: int, items: list[tuple[float, object]], *, ts: str | None = None, clef_obj=None, voices: dict[str, list[tuple[float, object]]] | None = None) -> stream.Measure:
    m = stream.Measure(number=num)
    if ts:
        m.insert(0.0, meter.TimeSignature(ts))
    if clef_obj is not None:
        m.insert(0.0, clef_obj)
    if voices:
        for vid, evs in voices.items():
            v = stream.Voice(id=vid)
            for off, el in evs:
                v.insert(off, el)
            m.insert(0.0, v)
    else:
        for off, el in items:
            m.insert(off, el)
    return m


# ---------------------------------------------------------------------------
# B18_1: Smart middle-note routing, Option B default, snowball & crossing fixes
# ---------------------------------------------------------------------------


def test_b18_1_chord_stem_inspects_subnotes_and_option_b_monophonic():
    """Chord.stemDirection=='unspecified' with uniform sub-note stems still guides ownership."""
    up_ch = _c(["B-4", "D5"], 4.0, sub_stems=["up", "up"])
    dn_n = _n("G4", 4.0, stem="down")
    assert up_ch.stemDirection == "unspecified"
    assert chord_stem(up_ch) == "up"

    sa = [
        _make_bar(
            1, [], ts="4/4", clef_obj=clef.TrebleClef(),
            voices={"1": [(0.0, up_ch)], "2": [(0.0, dn_n)]},
        )
    ]
    tb = [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())]
    res_def = split_score(_two_staff(sa, tb), auto_layout=True)
    # Option B default: 1 pitch per voice, auto_owner='upper' due to stem cue
    assert _bar_pitches(_bars(res_def, "Soprano")[0]) == ["D5"]
    assert _bar_pitches(_bars(res_def, "Alto")[0]) == ["G4"]
    assert len(res_def.divisi_choices) == 1
    assert res_def.divisi_choices[0].auto_owner == "upper"
    assert not any(f.reason == "divisi_chord" for f in res_def.flags)

    # KEEP_DIVISI attaches Bb4 to Soprano (not Alto)
    sa_k = [_make_bar(1, [], ts="4/4", clef_obj=clef.TrebleClef(), voices={"1": [(0.0, _c(["B-4", "D5"], 4.0, sub_stems=["up", "up"]))], "2": [(0.0, _n("G4", 4.0, stem="down"))]})]
    res_keep = split_score(_two_staff(sa_k, [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())]),
                           SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI), auto_layout=True)
    assert _bar_pitches(_bars(res_keep, "Soprano")[0]) == ["D5+B-4"]
    assert _bar_pitches(_bars(res_keep, "Alto")[0]) == ["G4"]


def test_b18_1_comfort_collapse_avoid_crossing_and_4note_no_snowball():
    """Comfort ceiling collapses Bb5+F5 -> F5; forced crossings eliminated; 4-note never snowballs."""
    # Comfort ceiling: Bb5 (>G5 ceiling) with F5 + Bb4 -> S=F5, A=Bb4, ambiguous=True
    sa = [
        _make_bar(
            1, [], ts="4/4", clef_obj=clef.TrebleClef(),
            voices={"1": [(0.0, _c(["F5", "B-5"], 4.0, stem="up"))], "2": [(0.0, _n("B-4", 4.0, stem="down"))]},
        ),
        # Source voice 1 lower than voice 2 in 3-note onset (forced crossing avoided in output)
        _make_bar(
            2, [], ts="4/4", clef_obj=clef.TrebleClef(),
            voices={"1": [(0.0, _c(["F4", "A4"], 4.0, stem="up"))], "2": [(0.0, _n("C5", 4.0, stem="down"))]},
        ),
        # 4-note chord
        _make_bar(3, [(0.0, _c(["C4", "F4", "A4", "C5"], 4.0))]),
    ]
    tb = [_make_bar(i, [(0.0, _n("C3", 4.0))], ts="4/4" if i == 1 else None, clef_obj=clef.BassClef() if i == 1 else None) for i in range(1, 4)]

    res_mono = split_score(_two_staff(sa, tb), auto_layout=True)
    assert _bar_pitches(_bars(res_mono, "Soprano")[0]) == ["F5"]
    assert _bar_pitches(_bars(res_mono, "Alto")[0]) == ["B-4"]
    assert res_mono.divisi_choices[0].ambiguous is True
    assert any(f.reason == "divisi_resolved" and f.measure == 1 for f in res_mono.flags)

    # Bar 2: output never crosses (Soprano >= Alto)
    assert _bar_pitches(_bars(res_mono, "Soprano")[1]) == ["C5"]
    assert _bar_pitches(_bars(res_mono, "Alto")[1]) == ["F4"]
    assert res_mono.divisi_choices[1].ambiguous is True

    # Bar 3: 4-note chord collapses cleanly to 1 note per voice (smoothly keeping F4 from bar 2!)
    assert _bar_pitches(_bars(res_mono, "Soprano")[2]) == ["C5"]
    assert _bar_pitches(_bars(res_mono, "Alto")[2]) == ["F4"]

    # Standalone 4-note chord (no prior voice-leading state) picks outer notes C5 / C4
    sa_4 = [_make_bar(1, [(0.0, _c(["C4", "F4", "A4", "C5"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef())]
    res_4_mono = split_score(_two_staff(sa_4, [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())]), auto_layout=True)
    assert _bar_pitches(_bars(res_4_mono, "Soprano")[0]) == ["C5"]
    assert _bar_pitches(_bars(res_4_mono, "Alto")[0]) == ["C4"]

    # KEEP_DIVISI on 4-note chord splits cleanly 2+2 (never 3+1 snowballing)
    sa_keep = [
        _make_bar(1, [(0.0, _c(["C4", "F4", "A4", "C5"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef())
    ]
    res_keep = split_score(
        _two_staff(sa_keep, [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())]),
        SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI),
        auto_layout=True,
    )
    assert _bar_pitches(_bars(res_keep, "Soprano")[0]) == ["C5+A4"]
    assert _bar_pitches(_bars(res_keep, "Alto")[0]) == ["F4+C4"]


def test_b18_1_tie_voice_leading_and_all_override_modes():
    """Tie continuity, UPPER_ONLY/LOWER_ONLY biases, and explicit overrides work cleanly."""
    # Bar 1: S=G4 tied into middle G4 of bar 2 chord [C5, G4, E4]
    sa = [
        _make_bar(1, [], ts="4/4", clef_obj=clef.TrebleClef(),
                  voices={"1": [(0.0, _n("G4", 4.0, stem="up", tie_type="start"))],
                          "2": [(0.0, _n("D4", 4.0, stem="down"))]}),
        _make_bar(2, [(0.0, _c(["E4", "G4", "C5"], 4.0))]),
    ]
    tb = [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef()),
          _make_bar(2, [(0.0, _n("C3", 4.0))])]
    res_tie = split_score(_two_staff(sa, tb), auto_layout=True)
    # Tie into G4 routes middle G4 to Soprano & Option B collapse picks tied G4!
    assert res_tie.divisi_choices[0].auto_owner == "upper"
    assert _bar_pitches(_bars(res_tie, "Soprano")[1]) == ["G4"]
    assert _bar_pitches(_bars(res_tie, "Alto")[1]) == ["E4"]

    # Direct unit test of DivisiModes & explicit overrides via resolve_simultaneous_chord
    cands = [
        PitchCandidate(_n("C5").pitch, _n("C5")),
        PitchCandidate(_n("G4").pitch, _n("G4")),
        PitchCandidate(_n("E4").pitch, _n("E4")),
    ]
    u_u, l_u, _ = resolve_simultaneous_chord(
        cands, measure=1, part_index=0, offset=0.0,
        upper_role="Soprano", lower_role="Alto", state=VoiceLeadingState(),
        cfg=SplitConfig(divisi_mode=DivisiMode.KEEP_DIVISI, divisi_overrides={"1|Soprano|0.0": "upper_mid"}),
    )
    assert [c.pitch.nameWithOctave for c in u_u] == ["G4"]
    assert [c.pitch.nameWithOctave for c in l_u] == ["E4"]

    u_l, l_l, _ = resolve_simultaneous_chord(
        cands, measure=1, part_index=0, offset=0.0,
        upper_role="Soprano", lower_role="Alto", state=VoiceLeadingState(),
        cfg=SplitConfig(divisi_mode=DivisiMode.MONOPHONIC_AUTO, divisi_overrides={"1|Soprano|0.0": "lower_mid"}),
    )
    assert [c.pitch.nameWithOctave for c in u_l] == ["C5"]
    assert [c.pitch.nameWithOctave for c in l_l] == ["G4"]

    # UPPER_ONLY & LOWER_ONLY bias auto-keep cleanly
    st = VoiceLeadingState()
    u_k, l_k, ch_u = resolve_simultaneous_chord(
        cands, measure=2, part_index=0, offset=0.0,
        upper_role="Soprano", lower_role="Alto", state=st,
        cfg=SplitConfig(divisi_mode=DivisiMode.UPPER_ONLY),
    )
    assert len(u_k) == 1 and len(l_k) == 1 and ch_u.ambiguous is True


def test_b18_1_multipage_merge_override_translation(tmp_path: Path):
    """Merged override keys translate accurately back to page-local bars on multi-page merge."""
    p1 = tmp_path / "page_01.musicxml"
    p2 = tmp_path / "page_02.musicxml"
    sc1 = _two_staff(
        [_make_bar(1, [(0.0, _n("C5", 4.0))], ts="4/4", clef_obj=clef.TrebleClef()),
         _make_bar(2, [(0.0, _n("D5", 4.0))])],
        [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef()),
         _make_bar(2, [(0.0, _n("C3", 4.0))])],
    )
    sc2 = _two_staff(
        [_make_bar(1, [(0.0, _c(["E4", "G4", "C5"], 4.0))], clef_obj=clef.TrebleClef())],
        [_make_bar(1, [(0.0, _n("C3", 4.0))], clef_obj=clef.BassClef())],
    )
    sc1.write("musicxml", fp=str(p1))
    sc2.write("musicxml", fp=str(p2))

    # Without override: page 2 bar 1 is merged bar 3
    _, _, res0 = split_and_merge_mxl_files([p1, p2], tmp_path / "out0", "t", auto_layout=True)
    assert len(res0.divisi_choices) == 1
    assert res0.divisi_choices[0].choice_key == "3|Soprano|0.0"

    # Override at merged bar 3 ("3|Soprano|0.0": "upper_mid") forces Soprano=G4 on page 2 bar 1!
    cfg_ov = SplitConfig(divisi_overrides={"3|Soprano|0.0": "upper_mid"})
    _, _, res1 = split_and_merge_mxl_files([p1, p2], tmp_path / "out1", "t", auto_layout=True, config=cfg_ov)
    assert _bar_pitches(_bars(res1, "Soprano")[2]) == ["G4"]
    assert res1.divisi_choices[0].chosen_upper == ["G4"]


# ---------------------------------------------------------------------------
# B18_2: Interactive GUI DivisiResolverDialog, PipelineWorker, MainWindow, CLI
# ---------------------------------------------------------------------------


def test_b18_2_gui_divisi_dialog_filtering_bulk_and_worker_reexport(tmp_path: Path):
    """Headless PySide6 tests for DivisiResolverDialog, JSON overrides, PipelineWorker & MainWindow."""
    from PySide6.QtWidgets import QApplication, QComboBox
    from satb_splitter.gui.divisi_dialog import DivisiResolverDialog
    from satb_splitter.gui.main_window import MainWindow
    from satb_splitter.gui.workers import PipelineWorker

    app = QApplication.instance() or QApplication([])

    c1 = DivisiChoice(
        measure=3, part_index=0, offset=0.0, upper_role="Soprano", lower_role="Alto",
        pitches=["C5", "G4", "E4"], auto_owner="upper", chosen_upper=["C5"], chosen_lower=["E4"],
        ambiguous=True, confidence=0.55, reason="shared/no stem distinction",
        prev_upper="A4", next_upper="C5", prev_lower="F4", next_lower="E4",
        choice_key="3|Soprano|0.0",
    )
    c2 = DivisiChoice(
        measure=5, part_index=0, offset=0.0, upper_role="Soprano", lower_role="Alto",
        pitches=["D5", "B4", "G4"], auto_owner="upper", chosen_upper=["D5"], chosen_lower=["G4"],
        ambiguous=False, confidence=0.9, reason="distinct stems",
        choice_key="5|Soprano|0.0",
    )
    c3 = DivisiChoice(
        measure=6, part_index=1, offset=0.0, upper_role="Tenor", lower_role="Bass",
        pitches=["C4", "G3", "C3"], auto_owner="lower", chosen_upper=["C4"], chosen_lower=["C3"],
        ambiguous=True, confidence=0.5, reason="shared/no stem distinction",
        choice_key="6|Tenor|0.0",
    )

    dlg = DivisiResolverDialog([c1, c2, c3])
    assert dlg.chk_ambiguous_only.isChecked() is True
    assert dlg.table.rowCount() == 2  # c1 and c3 only
    dlg.chk_ambiguous_only.setChecked(False)
    assert dlg.table.rowCount() == 3

    # Select row 0 (Soprano/Alto) -> change to "upper_mid", apply to same staff (should touch c1 & c2, NOT c3!)
    dlg.table.selectRow(0)
    cb0 = dlg.table.cellWidget(0, 5)
    assert isinstance(cb0, QComboBox)
    idx_mid = cb0.findData("upper_mid")
    assert idx_mid >= 0
    cb0.setCurrentIndex(idx_mid)
    dlg._apply_to_same_staff()
    ov = dlg.selected_overrides()
    assert ov == {"3|Soprano|0.0": "upper_mid", "5|Soprano|0.0": "upper_mid"}

    dlg._reset_all()
    assert dlg.selected_overrides() == {}
    dlg.close()

    # MainWindow controls, settings, override JSON persistence & PipelineWorker re-export
    win = MainWindow()
    assert win.divisi_combo.count() == 5
    assert win.btn_resolve_divisi.isEnabled() is False
    idx_ask = win.divisi_combo.findData("ask")
    assert idx_ask >= 0
    win.divisi_combo.setCurrentIndex(idx_ask)
    win._save_overrides(tmp_path / "work", "piece", {"1|Soprano|0.0": "upper_mid"})
    assert win._load_overrides(tmp_path / "work", "piece") == {"1|Soprano|0.0": "upper_mid"}

    # PipelineWorker fast cached-MXL re-export (no Audiveris)
    mxl = tmp_path / "piece.musicxml"
    sc = _two_staff(
        [_make_bar(1, [(0.0, _c(["E4", "G4", "C5"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef())],
        [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())],
    )
    sc.write("musicxml", fp=str(mxl))
    worker = PipelineWorker(
        mxl, tmp_path / "wout",
        is_pdf=False, whiteout=0.0, whiteout_mode="none", template="auto",
        unison="duplicate_both", audiveris=None,
        divisi_mode="monophonic_auto", divisi_overrides={"1|Soprano|0.0": "upper_mid"},
        mxl_paths=[mxl],
    )
    res_dict = worker._run()
    assert len(res_dict["divisi_choices"]) == 1
    assert res_dict["divisi_choices"][0].chosen_upper == ["G4"]
    win.close()
    app.processEvents()


def test_b18_2_cli_divisi_and_overrides_flags(tmp_path: Path):
    """CLI --divisi and --divisi-overrides roundtrip accurately."""
    mxl = tmp_path / "song.musicxml"
    ov_json = tmp_path / "ov.json"
    out_dir = tmp_path / "cli_out"
    sc = _two_staff(
        [_make_bar(1, [(0.0, _c(["E4", "G4", "C5"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef())],
        [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())],
    )
    sc.write("musicxml", fp=str(mxl))
    ov_json.write_text(json.dumps({"1|Soprano|0.0": "upper_mid"}), encoding="utf-8")

    rc = cli.main(["-q", "--divisi", "monophonic_auto", "--divisi-overrides", str(ov_json), str(mxl), "-o", str(out_dir)])
    assert rc == 0
    from music21 import converter
    sop = converter.parse(str(out_dir / "song_Soprano.musicxml"))
    pitches = [n.pitch.nameWithOctave for n in sop.flatten().notes]
    assert pitches == ["G4"]


# ---------------------------------------------------------------------------
# B18_3: solo Tenor marker joining, routing, waiting-time rests & 100.pdf guard
# ---------------------------------------------------------------------------


def test_b18_3_join_spans_preserves_solo_tenor_space_keeps_toti(tmp_path: Path):
    """Touching span trailing space gives 'solo Tenor' while font-split 'to'+'ţ'+'i' remains joined."""
    assert _join_spans([("solo", "Arial", (10, 10, 40, 20), False, True), ("Tenor", "Arial", (40, 10, 80, 20), False, False)]) == "solo Tenor"
    assert _join_spans([("solo ", "Arial", (10, 10, 40, 20), False, True), ("Tenor", "Arial", (40, 10, 80, 20), False, False)]) == "solo Tenor"
    assert _join_spans([("to", "Arial", (10, 10, 20, 20)), ("ţ", "Arial2", (20, 10, 25, 20)), ("i", "Arial", (25, 10, 28, 20))]) == "toţi"


def test_b18_3_solo_tenor_intro_wait_solo_only_overlay_and_multipage(tmp_path: Path):
    """solo Tenor goes to Tenor (8vb), S/A/B get waiting-time rests on intro/solo bars, Tutti overlays."""
    # Page 1: 3 bars -> bar 1 intro_wait (top silent, SA keyboard triad + no real words),
    # bar 2 solo_only (top sings in treble G above choir accompaniment triad with 0 lyrics),
    # bar 3 Tutti (SA & TB sing real words -> overlay)
    def _build_3staff(with_label: bool):
        sc = stream.Score()
        top = stream.Part(id="Solo")
        top.partName = "Voice"
        sa = stream.Part(id="SA")
        sa.partName = "Voice 2"
        tb = stream.Part(id="TB")
        tb.partName = "Voice 3"

        t1 = _make_bar(1, [(0.0, note.Rest(quarterLength=4.0))], ts="4/4", clef_obj=clef.TrebleClef())
        t2 = _make_bar(2, [(0.0, _n("C5", 2.0, lyric="Doam")), (2.0, _n("D5", 2.0, lyric="ne"))])
        if with_label:
            t2.insert(0.0, expressions.TextExpression("solo Tenor"))
        t3 = _make_bar(3, [(0.0, _n("E5", 4.0, lyric="ma"))])
        for b in (t1, t2, t3):
            top.append(b)

        # Bar 1 & 2 SA/TB hold keyboard accompaniment chords (3-note chord -> would trigger divisi_choices if unmasked!)
        sa1 = _make_bar(1, [(0.0, _c(["C4", "E4", "G4"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef())
        sa2 = _make_bar(2, [(0.0, _c(["D4", "F4", "A4"], 4.0))])
        sa3 = _make_bar(3, [], voices={
            "1": [(0.0, _n("C5", 2.0, stem="up", lyric="A")), (2.0, _n("B4", 2.0, stem="up", lyric="min"))],
            "2": [(0.0, _n("G4", 2.0, stem="down")), (2.0, _n("G4", 2.0, stem="down"))],
        })
        for b in (sa1, sa2, sa3):
            sa.append(b)

        tb1 = _make_bar(1, [(0.0, _c(["C3", "E3", "G3"], 4.0))], ts="4/4", clef_obj=clef.BassClef())
        tb2 = _make_bar(2, [(0.0, _c(["D3", "F3", "A3"], 4.0))])
        tb3 = _make_bar(3, [], voices={
            "1": [(0.0, _n("E3", 4.0, stem="up"))],
            "2": [(0.0, _n("C3", 4.0, stem="down"))],
        })
        for b in (tb1, tb2, tb3):
            tb.append(b)

        sc.insert(0, top)
        sc.insert(0, sa)
        sc.insert(0, tb)
        return sc

    res = split_score(_build_3staff(with_label=True), auto_layout=True)
    # Bar 1 (intro_wait): ALL 4 voices are pure rests
    for r in ROLES:
        assert _bar_pitches(_bars(res, r)[0]) == []
    # Bar 2 (solo_only): S/A/B are waiting-time rests, Tenor has C4 D4 (transposed -12 from C5 D5!)
    for r in ("Soprano", "Alto", "Bass"):
        assert _bar_pitches(_bars(res, r)[1]) == []
    assert _bar_pitches(_bars(res, "Tenor")[1]) == ["C4", "D4"]
    # Bar 3 (overlay Tutti): choir S/A/B sing normally; Tenor gets E4 (-12 from E5)
    assert _bar_pitches(_bars(res, "Soprano")[2]) == ["C5", "B4"]
    assert _bar_pitches(_bars(res, "Alto")[2]) == ["G4", "G4"]
    assert _bar_pitches(_bars(res, "Tenor")[2]) == ["E4"]
    assert _bar_pitches(_bars(res, "Bass")[2]) == ["C3"]
    # Stale accompaniment divisi_choices on masked bars 1 & 2 suppressed!
    assert all(ch.measure not in (1, 2) for ch in res.divisi_choices)
    assert any(f.reason == "top_staff_tenor" for f in res.flags)

    # Multi-page merge carry-over: page 2 lacks explicit "solo Tenor" text yet still inherits Tenor solo!
    p1 = tmp_path / "page_01.musicxml"
    p2 = tmp_path / "page_02.musicxml"
    _build_3staff(with_label=True).write("musicxml", fp=str(p1))
    sc2 = stream.Score()
    top2 = stream.Part(id="Solo")
    top2.partName = "Voice"
    top2.append(_make_bar(1, [(0.0, _n("F5", 2.0, lyric="Si")), (2.0, _n("E5", 2.0, lyric="domn"))], ts="4/4", clef_obj=clef.TrebleClef()))
    sa2 = stream.Part(id="SA")
    sa2.partName = "Voice 2"
    sa2.append(_make_bar(1, [(0.0, _c(["F4", "A4", "C5"], 4.0))], ts="4/4", clef_obj=clef.TrebleClef()))
    tb2 = stream.Part(id="TB")
    tb2.partName = "Voice 3"
    tb2.append(_make_bar(1, [(0.0, _c(["F3", "A3", "C4"], 4.0))], ts="4/4", clef_obj=clef.BassClef()))
    sc2.insert(0, top2)
    sc2.insert(0, sa2)
    sc2.insert(0, tb2)
    sc2.write("musicxml", fp=str(p2))

    _, _, mres = split_and_merge_mxl_files([p1, p2], tmp_path / "merged", "m", auto_layout=True)
    # Bar 4 (= p2 bar 1): Tenor has F4 E4, S/A/B are waiting-time rests
    assert _bar_pitches(_bars(mres, "Tenor")[3]) == ["F4", "E4"]
    assert _bar_pitches(_bars(mres, "Soprano")[3]) == []


def test_b18_3_100_pdf_sopran_negative_guard():
    """'Sopran' / plain 'Solo' top staff NEVER activates solo Tenor routing."""
    sc = stream.Score()
    top = stream.Part(id="Sop")
    top.partName = "Voice"
    t1 = _make_bar(1, [(0.0, _n("B4", 2.0, lyric="Cand")), (2.0, _n("C#5", 2.0, lyric="va"))], ts="4/4", clef_obj=clef.TrebleClef())
    t1.insert(0.0, expressions.TextExpression("Sopran"))
    top.append(t1)
    sa = stream.Part(id="SA")
    sa.partName = "Voice 2"
    sa.append(_make_bar(1, [(0.0, _n("E4", 4.0, lyric="hm"))], ts="4/4", clef_obj=clef.TrebleClef()))
    tb = stream.Part(id="TB")
    tb.partName = "Voice 3"
    tb.append(_make_bar(1, [(0.0, _n("E3", 4.0))], ts="4/4", clef_obj=clef.BassClef()))
    sc.insert(0, top)
    sc.insert(0, sa)
    sc.insert(0, tb)

    plan = plan_top_solo_tenor(list(sc.parts), 1, 0, [4.0], lambda _t: None)
    assert plan.active is False
    res = split_score(sc, auto_layout=True)
    assert not any(f.reason == "top_staff_tenor" for f in res.flags)
    assert _bar_pitches(_bars(res, "Soprano")[0]) == ["B4", "C#5"]


# ---------------------------------------------------------------------------
# B18_4: 034 m117 _build_grid bogus paddingLeft >= bar collapse fix
# ---------------------------------------------------------------------------


def test_b18_4_bogus_padding_left_ge_bar_does_not_collapse_bar():
    """Mid-score measure with full 4.0q content & bogus paddingLeft==4.0 stays 4.0q (no collapse)."""
    m1 = _make_bar(1, [(0.0, _n("C5", 4.0))], ts="4/4", clef_obj=clef.TrebleClef())
    m2 = _make_bar(2, [(0.0, _n("D5", 2.0)), (2.0, _n("E5", 2.0))])
    m2.paddingLeft = 4.0  # bogus OMR paddingLeft == full bar!
    tb1 = _make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())
    tb2 = _make_bar(2, [(0.0, _n("G2", 4.0))])
    tb2.paddingLeft = 4.0
    res = split_score(_two_staff([m1, m2], [tb1, tb2]), auto_layout=True)
    b2 = _bars(res, "Soprano")[1]
    assert float(b2.quarterLength) == 4.0
    assert _bar_pitches(b2) == ["D5", "E5"]

    # Real bar-1 pickup (1.0q in 4/4, paddingLeft=3.0) stays 1.0q!
    p_m1 = _make_bar(0, [(0.0, _n("G4", 1.0))], ts="4/4", clef_obj=clef.TrebleClef())
    p_m1.paddingLeft = 3.0
    p_m2 = _make_bar(1, [(0.0, _n("C5", 4.0))])
    p_tb1 = _make_bar(0, [(0.0, _n("G2", 1.0))], ts="4/4", clef_obj=clef.BassClef())
    p_tb1.paddingLeft = 3.0
    p_tb2 = _make_bar(1, [(0.0, _n("C3", 4.0))])
    res_pu = split_score(_two_staff([p_m1, p_m2], [p_tb1, p_tb2]), auto_layout=True)
    assert float(_bars(res_pu, "Soprano")[0].quarterLength) == 1.0


# ---------------------------------------------------------------------------
# B18_5: Chord-symbol digits & note-bearing bars never trigger multirest_missing
# ---------------------------------------------------------------------------


def test_b18_5_chord_symbol_digit_filter_and_note_bearing_suppresses_mr(tmp_path: Path):
    """Chord symbols (Badd2, F#add4) never become multirest counts, and note-bearing bars never flag multirest_missing."""
    spans = [
        ("B", "Arial", 10.0, (100.0, 40.0, 108.0, 52.0)),
        ("add", "Arial", 8.0, (108.0, 42.0, 122.0, 52.0)),
        ("2", "EngraverFontSet", 10.0, (122.0, 40.0, 128.0, 52.0)),
        ("4", "EngraverFontSet", 20.0, (250.0, 40.0, 260.0, 62.0)),
    ]
    assert _is_chord_symbol_digit((122.0, 40.0, 128.0, 52.0), 10.0, spans) is True
    assert _is_chord_symbol_digit((250.0, 40.0, 260.0, 62.0), 20.0, spans) is False

    # Note-bearing bar at idx 0: apply_multirests skips multirest_missing when notes sound!
    mxl = tmp_path / "song.mxl"
    sc = _two_staff(
        [_make_bar(1, [(0.0, _n("C5", 4.0))], ts="4/4", clef_obj=clef.TrebleClef()),
         _make_bar(2, [(0.0, note.Rest(quarterLength=4.0))])],
        [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef()),
         _make_bar(2, [(0.0, note.Rest(quarterLength=4.0))])],
    )
    sc.write("musicxml", fp=str(mxl))
    sidecar = tmp_path / "song.multirest.json"
    sidecar.write_text(json.dumps({
        "printed": [
            {"measure_index": 0, "count": 2, "system": 0},  # note-bearing bar -> NOT multirest_missing!
            {"measure_index": 1, "count": 4, "system": 0},  # genuine all-rest bar -> emits multirest_missing!
        ],
        "has_text_layer": True,
    }), encoding="utf-8")

    res = split_file(mxl, auto_layout=True)
    mr_flags = [f for f in res.flags if f.reason == "multirest_missing"]
    assert len(mr_flags) == 1
    assert mr_flags[0].measure == 2


def test_b18_4_sanitize_xml_tuplets_fixes_unbalanced_and_exotic_rest_ratios(tmp_path: Path):
    """Unbalanced OMR tuplets (start/stop mismatch) & exotic 12/7 rests are cleanly repaired in writer."""
    from satb_splitter.export.writer import _sanitize_xml_tuplets

    xml_in = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>S</part-name></score-part></part-list>'
        '<part id="P1"><measure number="1">'
        '<attributes><divisions>10080</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>'
        '<note><pitch><step>C</step><octave>5</octave></pitch><duration>30240</duration><type>half</type><dot/></note>'
        '<note><pitch><step>D</step><octave>5</octave></pitch><duration>3360</duration><type>eighth</type>'
        '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        '<notations><tuplet type="start" number="1"/></notations></note>'
        '<note><pitch><step>E</step><octave>5</octave></pitch><duration>3360</duration><type>eighth</type>'
        '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        '<notations><tuplet type="stop" number="1"/></notations></note>'
        '<note><pitch><step>F</step><octave>5</octave></pitch><duration>3360</duration><type>eighth</type>'
        '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification></note>'
        '</measure></part></score-partwise>'
    )
    p = tmp_path / "broken_tuplet.musicxml"
    p.write_text(xml_in, encoding="utf-8")
    assert _sanitize_xml_tuplets(p) is True
    root = ET.fromstring(p.read_text(encoding="utf-8"))
    tups = [[t.attrib.get("type") for t in n.findall("notations/tuplet")] for n in root.findall(".//note")]
    assert tups == [[], ["start"], [], ["stop"]]
    assert _sanitize_xml_tuplets(p) is False


def test_b18_1_duplicate_pitch_3note_chord_two_distinct():
    """3-note chord with duplicate pitches (2 distinct) never triggers divisi assert."""
    from satb_splitter.split.engine import _Ctx, _Line, _apply_resolved_chord

    # Verify music21 Chord retains all 3 duplicate pitches so _place_flexible reaches _apply_resolved_chord
    dup_chord = _c(["A2", "A2", "C#4"], 4.0)
    assert len(dup_chord.pitches) == 3

    sa = [_make_bar(1, [(0.0, _n("C5", 4.0))], ts="4/4", clef_obj=clef.TrebleClef())]
    tb = [_make_bar(1, [(0.0, dup_chord)], ts="4/4", clef_obj=clef.BassClef())]
    res = split_score(_two_staff(sa, tb), SplitConfig())
    assert res.flags == []
    assert len(res.divisi_choices) == 0
    assert _bar_pitches(_bars(res, "Tenor")[0]) == ["C#4"]
    assert _bar_pitches(_bars(res, "Bass")[0]) == ["A2"]

    # Also cover ordered multi-voice extra-voice path (free_by_off -> _place_flexible -> _apply_resolved_chord)
    tb_v = [
        _make_bar(
            1, [], ts="4/4", clef_obj=clef.BassClef(),
            voices={
                "1": [(0.0, note.Rest(quarterLength=4.0))],
                "2": [(0.0, note.Rest(quarterLength=4.0))],
                "3": [(0.0, _c(["A2", "A2", "C#4"], 4.0))],
            },
        )
    ]
    res_v = split_score(_two_staff([_make_bar(1, [(0.0, _n("C5", 4.0))], ts="4/4", clef_obj=clef.TrebleClef())], tb_v), SplitConfig())
    assert res_v.flags == []
    assert len(res_v.divisi_choices) == 0
    assert _bar_pitches(_bars(res_v, "Tenor")[0]) == ["C#4"]
    assert _bar_pitches(_bars(res_v, "Bass")[0]) == ["A2"]

    # Direct _apply_resolved_chord check with two simultaneous elements sharing A2 + C#4
    n_a2_up = _n("A2", 4.0, stem="down")
    n_a2_dup = _n("A2", 4.0, stem="down")
    n_cs4 = _n("C#4", 4.0, stem="up")
    raw_cands = [
        PitchCandidate(n_a2_up.pitch, n_a2_up, stem="down"),
        PitchCandidate(n_a2_dup.pitch, n_a2_dup, stem="down"),
        PitchCandidate(n_cs4.pitch, n_cs4, stem="up"),
    ]
    upper, lower = _Line(), _Line()
    placed_group: dict[int, int] = {}
    placed_stem: dict[int, str | None] = {}
    group_of = {id(n_cs4): 0, id(n_a2_up): 1, id(n_a2_dup): 1}
    ctx = _Ctx(
        measure=1, part_index=1, upper_role="Tenor", lower_role="Bass",
        cfg=SplitConfig(), flags=[], divisi=[], vl_state=VoiceLeadingState(),
        divisi_choices=[],
    )
    _apply_resolved_chord(
        raw_cands, 0.0, upper, lower, ctx,
        placed_group=placed_group, placed_stem=placed_stem, group_of=group_of,
    )
    assert len(upper.items) == 1 and upper.items[0][2].pitch.nameWithOctave == "C#4"
    assert len(lower.items) == 1 and lower.items[0][2].pitch.nameWithOctave == "A2"
    assert placed_group[id(upper.items[0][2])] == 0
    assert placed_group[id(lower.items[0][2])] == 1
    assert placed_stem[id(upper.items[0][2])] == "up"
    assert placed_stem[id(lower.items[0][2])] == "down"
    assert ctx.divisi_choices == []

    # Empty candidates list is a safe no-op
    _apply_resolved_chord([], 0.0, _Line(), _Line(), ctx)


def test_b18_1_duplicate_pitch_3note_chord_unison():
    """3-note chord with 1 distinct pitch duplicates cleanly to Soprano & Alto without assert."""
    from satb_splitter.split.engine import _Ctx, _Line, _apply_resolved_chord

    dup_unison = _c(["G4", "G4", "G4"], 4.0)
    assert len(dup_unison.pitches) == 3

    sa = [_make_bar(1, [(0.0, dup_unison)], ts="4/4", clef_obj=clef.TrebleClef())]
    tb = [_make_bar(1, [(0.0, _n("C3", 4.0))], ts="4/4", clef_obj=clef.BassClef())]
    res = split_score(_two_staff(sa, tb), SplitConfig())
    assert res.flags == []
    assert len(res.divisi_choices) == 0
    assert _bar_pitches(_bars(res, "Soprano")[0]) == ["G4"]
    assert _bar_pitches(_bars(res, "Alto")[0]) == ["G4"]

    # Direct _apply_resolved_chord check with 3 duplicate G4 candidates + placed_group/placed_stem
    n1, n2, n3 = _n("G4", 4.0, stem="up"), _n("G4", 4.0, stem="down"), _n("G4", 4.0)
    raw_cands = [
        PitchCandidate(n1.pitch, n1, stem="up"),
        PitchCandidate(n2.pitch, n2, stem="down"),
        PitchCandidate(n3.pitch, n3),
    ]
    upper, lower = _Line(), _Line()
    placed_group: dict[int, int] = {}
    placed_stem: dict[int, str | None] = {}
    group_of = {id(n1): 0, id(n2): 1, id(n3): 2}
    ctx = _Ctx(
        measure=1, part_index=0, upper_role="Soprano", lower_role="Alto",
        cfg=SplitConfig(), flags=[], divisi=[], vl_state=VoiceLeadingState(),
        divisi_choices=[],
    )
    _apply_resolved_chord(
        raw_cands, 0.0, upper, lower, ctx,
        placed_group=placed_group, placed_stem=placed_stem, group_of=group_of,
    )
    assert len(upper.items) == 1 and upper.items[0][2].pitch.nameWithOctave == "G4"
    assert len(lower.items) == 1 and lower.items[0][2].pitch.nameWithOctave == "G4"
    assert placed_group[id(upper.items[0][2])] == 0
    assert placed_group[id(lower.items[0][2])] == 0
    assert placed_stem[id(upper.items[0][2])] == "up"
    assert placed_stem[id(lower.items[0][2])] == "up"
    assert ctx.divisi_choices == []


