"""Regression tests for Orchestrator batch 2 (R1 voice swap, R2 single-staff
unison/S.A, R3 solo tenor / trio, R4 omr_cli cp1252, BUG-3/4/8)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from music21 import clef, expressions, meter, note, stream, tie

from satb_splitter.split.engine import parse_section_marker, sanitize_ties, split_score

from helpers import pitches_in
from test_p0_fixes import events, n, part_with, score_of, voiced_measure


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


# ---------------------------------------------------------------------------
# R1: upper/lower decided by pitch / stems, not Audiveris voice order
# ---------------------------------------------------------------------------

def test_voice1_lower_line_goes_to_bass_not_tenor():
    """Audiveris voice 1 = LOWER line (hard p11 b3): Tenor must get the upper."""
    sa = part_with([voiced_measure(1, {"1": [(0, n("E5", 4))], "2": [(0, n("C5", 4))]}, ts="4/4")],
                   clef.TrebleClef())
    tb = part_with(
        [voiced_measure(1, {"1": [(0, n("C3", 2)), (2, n("D3", 2))],
                            "2": [(0, n("G3", 2)), (2, n("A3", 2))]}, ts="4/4")],
        clef.BassClef(),
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["G3", "A3"]
    assert pitches_in(res.scores["Bass"]) == ["C3", "D3"]


def test_voice1_lower_on_sa_staff_goes_to_alto():
    sa = part_with(
        [voiced_measure(1, {"5": [(0, n("A4", 4))], "6": [(0, n("D5", 4))]}, ts="4/4")],
        clef.TrebleClef(),
    )
    tb = part_with([voiced_measure(1, {"1": [(0, n("G3", 4))], "2": [(0, n("C3", 4))]}, ts="4/4")],
                   clef.BassClef())
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["D5"]
    assert pitches_in(res.scores["Alto"]) == ["A4"]


def test_stems_override_voice_numbers_when_audiveris_switches_mid_bar():
    """Normal p1 b20: voice 1 carries the BASS (stems down) in beat 3-4 while
    voices 2/3 carry the tenor (stems up)."""
    sa = part_with([voiced_measure(1, {"1": [(0, n("E5", 4))], "2": [(0, n("C5", 4))]}, ts="4/4")],
                   clef.TrebleClef())
    tb = part_with(
        [voiced_measure(1, {
            "1": [(0, note.Rest(quarterLength=2.5)), (2.5, _stem(n("E3", 0.5), "down")),
                  (3, _stem(n("A3", 1), "down"))],
            "2": [(2.5, _stem(n("E3", 0.5), "up"))],
            "3": [(3, _stem(n("D4", 1), "up"))],
        }, ts="4/4")],
        clef.BassClef(),
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == ["E3", "D4"]
    assert pitches_in(res.scores["Bass"]) == ["E3", "A3"]


def test_single_sounding_voice_with_stem_down_is_lower():
    sa = part_with([voiced_measure(1, {"1": [(0, n("E5", 4))], "2": [(0, n("C5", 4))]}, ts="4/4")],
                   clef.TrebleClef())
    tb = part_with(
        [voiced_measure(1, {"1": [(0, note.Rest(quarterLength=4))],
                            "2": [(0, _stem(n("C3", 4), "down"))]}, ts="4/4")],
        clef.BassClef(),
    )
    res = split_score(score_of(sa, tb), auto_layout=True)
    assert pitches_in(res.scores["Tenor"]) == []
    assert pitches_in(res.scores["Bass"]) == ["C3"]


# ---------------------------------------------------------------------------
# R2: single-staff unison / S.A systems keep their notes
# ---------------------------------------------------------------------------

def _choir_with_single_staff_system(text_bar2=None, text_bar3=None):
    """Bar 1 = 2-staff choir; bars 2-3 = one treble staff Audiveris attached
    to the TB part (SA part empty); bar 4 = choir again."""
    sa = part_with(
        [_bar(1, n("E5", 4), ts="4/4"), _rest_bar(2), _rest_bar(3), _bar(4, n("D5", 4))],
        clef.TrebleClef(),
    )
    tb = part_with(
        [
            _bar(1, n("C3", 4), ts="4/4"),
            _bar(2, n("G4", 2), n("A4", 2), clef_obj=clef.TrebleClef(), text=text_bar2),
            _bar(3, n("B4", 4), text=text_bar3),
            _bar(4, n("G3", 4), clef_obj=clef.BassClef()),
        ],
        clef.BassClef(),
    )
    return score_of(sa, tb)


def test_unmarked_single_staff_treble_system_goes_to_sa():
    res = split_score(_choir_with_single_staff_system(), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["E5", "G4", "A4", "B4", "D5"]
    assert pitches_in(res.scores["Alto"]) == ["E5", "G4", "A4", "B4", "D5"]
    assert pitches_in(res.scores["Tenor"]) == ["C3", "G3"]
    assert any(f.reason == "foreign_clef_measure" and "S/A" in f.detail for f in res.flags)
    counts = {r: len(res.scores[r].parts[0].getElementsByClass(stream.Measure)) for r in res.scores}
    assert set(counts.values()) == {4}


def test_unison_marker_duplicates_to_all_four_tb_octave_down():
    res = split_score(_choir_with_single_staff_system(text_bar2="unison"), auto_layout=True)
    assert pitches_in(res.scores["Soprano"])[1:4] == ["G4", "A4", "B4"]
    assert pitches_in(res.scores["Alto"])[1:4] == ["G4", "A4", "B4"]
    assert pitches_in(res.scores["Tenor"]) == ["C3", "G3", "A3", "B3", "G3"]
    assert pitches_in(res.scores["Bass"]) == ["C3", "G3", "A3", "B3", "G3"]


def test_solo_then_sa_marker_in_one_single_staff_run():
    """Hard p16: 'solo' system (omitted) followed by 'S.A' system (kept)."""
    res = split_score(
        _choir_with_single_staff_system(text_bar2="solo", text_bar3="S.A"), auto_layout=True
    )
    assert pitches_in(res.scores["Soprano"]) == ["E5", "B4", "D5"]
    assert pitches_in(res.scores["Alto"]) == ["E5", "B4", "D5"]
    assert any(f.reason == "solo_omitted" for f in res.flags)


# ---------------------------------------------------------------------------
# R3: solo tenor / trio S.A.T. on a single staff
# ---------------------------------------------------------------------------

def test_single_staff_trio_sat_then_solo_tenor():
    """Hard p7 mvt2: 'trio S.A.T.' chords, then 'solo tenor' line."""
    from music21 import chord

    p = part_with(
        [
            _bar(1, chord.Chord(["B3", "D4", "G4"], quarterLength=4), ts="4/4", text="trio S.A.T."),
            _bar(2, chord.Chord(["C4", "E4", "G4"], quarterLength=4)),
            _bar(3, n("B3", 2), n("C4", 2), text="solo tenor"),
        ],
        clef.TrebleClef(),
    )
    res = split_score(score_of(p), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == ["G4", "G4"]
    assert pitches_in(res.scores["Alto"]) == ["D4", "E4"]
    assert pitches_in(res.scores["Tenor"]) == ["B3", "C4", "B3", "C4"]
    assert pitches_in(res.scores["Bass"]) == []


def test_single_staff_treble8_solo_goes_to_tenor_and_lost_8vb_fixed():
    """Hard p10 mvt2: tenor solo in treble-8vb; later systems read as plain
    treble (an octave too high) - must be Tenor, never S/A."""
    p = part_with(
        [
            _bar(1, n("A3", 4), ts="4/4"),
            _bar(2, n("G3", 4)),
            _bar(3, n("A4", 2), n("B-4", 2), clef_obj=clef.TrebleClef()),
        ],
        clef.Treble8vbClef(),
    )
    res = split_score(score_of(p), auto_layout=True)
    assert pitches_in(res.scores["Soprano"]) == []
    assert pitches_in(res.scores["Alto"]) == []
    assert pitches_in(res.scores["Tenor"]) == ["A3", "G3", "A3", "B-3"]
    assert any(f.reason == "octave_down_assumed" for f in res.flags)


@pytest.mark.parametrize(
    "text,kind,roles",
    [
        ("unison", "unison", ("Soprano", "Alto", "Tenor", "Bass")),
        ("S,A", "roles", ("Soprano", "Alto")),
        ("mo S.A.T.", "roles", ("Soprano", "Alto", "Tenor")),
        ("solo tenor", "roles", ("Tenor",)),
        ("solo alto", "roles", ("Alto",)),
        ("solo", "omit", ()),
        ('solo tun"', "omit", ()),
    ],
)
def test_parse_section_marker(text, kind, roles):
    sec = parse_section_marker(text)
    assert sec is not None and sec.kind == kind and sec.roles == roles


@pytest.mark.parametrize("text", ["Sé-L - sa—I can - tam in", "Partea VI din cantata", "Randy Vader", ""])
def test_lyric_or_title_text_is_not_a_marker(text):
    assert parse_section_marker(text) is None


def test_solo_policy_omit_drops_named_solos():
    sec = parse_section_marker("solo tenor", solo_policy="omit")
    assert sec.kind == "omit"


# ---------------------------------------------------------------------------
# BUG-3: no ties between different pitches
# ---------------------------------------------------------------------------

def test_sanitize_ties_removes_cross_pitch_ties():
    p = stream.Part()
    m = stream.Measure(number=1)
    a = n("G3", 1); a.tie = tie.Tie("start")
    b = n("B3", 1); b.tie = tie.Tie("stop")
    c = n("C4", 1); c.tie = tie.Tie("start")
    d = n("C4", 1); d.tie = tie.Tie("stop")
    for i, el in enumerate((a, b, c, d)):
        m.insert(i, el)
    p.append(m)
    assert sanitize_ties(p) == 2
    assert a.tie is None and b.tie is None
    assert c.tie.type == "start" and d.tie.type == "stop"


# ---------------------------------------------------------------------------
# R4 + BUG-8: omr_cli never crashes on cp1252 and --split merges like the GUI
# ---------------------------------------------------------------------------

def _cp1252_stdout():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_make_stdio_safe_on_cp1252(monkeypatch):
    from satb_splitter.omr_cli import make_stdio_safe

    out, err = _cp1252_stdout(), _cp1252_stdout()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    make_stdio_safe()
    print("page_01.pdf → page_01.mxl … — ă î ș ț")
    sys.stdout.flush()


def test_omr_cli_split_runs_and_merges_on_cp1252(monkeypatch, tmp_path: Path):
    import satb_splitter.omr_cli as omr_cli
    from test_p0_fixes import _simple_part

    pdf = tmp_path / "My Song.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    mxls = []
    for i in (1, 2):
        d = tmp_path / "work" / "omr" / f"page_0{i}"
        d.mkdir(parents=True)
        f = d / f"page_0{i}.musicxml"
        score_of(
            _simple_part(clef.TrebleClef(), "E5", bars=2),
            _simple_part(clef.BassClef(), "C3", bars=2),
        ).write("musicxml", fp=str(f))
        mxls.append(f)
    runs = [
        SimpleNamespace(input_path=Path("page_01.pdf"), success=True, mxl_paths=[mxls[0]], error="", retry_note=""),
        SimpleNamespace(input_path=Path("page_02.pdf"), success=True, mxl_paths=[mxls[1]], error="",
                        retry_note="recovered with white-out 0.22 after: white-out 0.12: failed"),
        SimpleNamespace(input_path=Path("page_03.pdf"), success=False, mxl_paths=[], error="No MusicXML",
                        retry_note=""),
    ]
    fake = SimpleNamespace(
        audiveris_path=Path("Audiveris.exe"), fail_count=1, success_count=2,
        page_inputs=[Path("a"), Path("b"), Path("c")], page_runs=runs, mxl_paths=mxls,
    )
    monkeypatch.setattr(omr_cli, "run_per_page_omr", lambda *a, **k: fake)
    monkeypatch.setattr(sys, "stdout", _cp1252_stdout())
    rc = omr_cli.main([str(pdf), "-o", str(tmp_path / "work"), "--split"])
    assert rc == 0
    voices = tmp_path / "work" / "voices"
    for role in ("Soprano", "Alto", "Tenor", "Bass", "SATB"):
        assert (voices / f"My Song_{role}.musicxml").is_file()
    assert not list(voices.glob("page_0*_Soprano.musicxml"))


# ---------------------------------------------------------------------------
# BUG-4: failed page retried with a larger white-out
# ---------------------------------------------------------------------------

def test_failed_page_retried_with_larger_whiteout(monkeypatch, tmp_path: Path):
    import satb_splitter.omr.pipeline as pl
    from satb_splitter.omr.audiveris import AudiverisRun

    pages = tmp_path / "work" / "pages"
    clean = tmp_path / "work" / "pages_clean"
    pages.mkdir(parents=True)
    clean.mkdir(parents=True)
    for d in (pages, clean):
        (d / "page_01.pdf").write_bytes(b"x")
        (d / "page_02.pdf").write_bytes(b"x")
    monkeypatch.setattr(pl, "find_audiveris", lambda a=None: Path("Audiveris.exe"))
    monkeypatch.setattr(
        pl, "prepare_pages_for_omr",
        lambda *a, **k: [clean / "page_01.pdf", clean / "page_02.pdf"],
    )
    used = []

    def fake_whiteout(src, dst, fraction):
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"x")
        used.append(fraction)
        return Path(dst)

    def fake_run(src, out, audiveris=None, timeout_s=600):
        src = Path(src)
        ok = not (src.name == "page_01.pdf" and src.parent.name == "pages_clean")
        mxl = Path(out) / f"{src.stem}.mxl"
        if ok:
            mxl.write_bytes(b"x")
        return AudiverisRun(input_path=src, success=ok, mxl_paths=[mxl] if ok else [],
                            error="" if ok else "No MusicXML export produced")

    monkeypatch.setattr(pl, "whiteout_top_band", fake_whiteout)
    monkeypatch.setattr(pl, "run_audiveris_export", fake_run)
    res = pl.run_per_page_omr(tmp_path / "x.pdf", tmp_path / "work")
    assert res.fail_count == 0 and res.success_count == 2
    assert used == [0.22]
    assert res.page_runs[0].input_path.name == "page_01.pdf"
    assert "0.22" in res.page_runs[0].retry_note
    assert res.mxl_paths[0].parent.name == "page_01_wo22"
