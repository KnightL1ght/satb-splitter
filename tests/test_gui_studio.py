"""Headless PySide6 tests for the Studio UI redesign, icon generator, exe & shortcut."""

from __future__ import annotations

import os
import struct
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from satb_splitter.gui.divisi_dialog import DivisiResolverDialog
from satb_splitter.gui.icon import ICO_SIZES, create_app_icon, render_icon_pixmap, write_ico_file
from satb_splitter.gui.main_window import MainWindow
from satb_splitter.gui.styles import apply_app_theme, get_theme_colors
from satb_splitter.model.config import DivisiChoice
from satb_splitter.omr import audiveris as audiveris_mod
from satb_splitter.omr.audiveris import DEFAULT_WINDOWS_PATHS, find_audiveris, run_audiveris_export


def test_studio_icon_rendering_and_ico_writer(tmp_path: Path):
    """create_app_icon and write_ico_file produce a valid 7-frame Windows .ico."""
    app = QApplication.instance() or QApplication([])
    assert app is not None

    pix = render_icon_pixmap(64)
    assert not pix.isNull()
    assert pix.width() == 64 and pix.height() == 64

    icon = create_app_icon()
    assert not icon.isNull()

    ico_path = write_ico_file(tmp_path / "test.ico")
    assert ico_path.is_file()
    raw = ico_path.read_bytes()
    assert raw[:4] == b"\x00\x00\x01\x00"
    reserved, ico_type, count = struct.unpack("<HHH", raw[:6])
    assert (reserved, ico_type, count) == (0, 1, len(ICO_SIZES))
    assert count == 7


def test_audiveris_default_paths_prioritize_dev3l(monkeypatch):
    """DEFAULT_WINDOWS_PATHS prioritizes Audiveris-local-dev3l over dev3k/dev3j/Program Files."""
    assert any("Audiveris-local-dev3l" in str(p) for p in DEFAULT_WINDOWS_PATHS)
    first_str = str(DEFAULT_WINDOWS_PATHS[0])
    assert "Audiveris-local-dev3l" in first_str

    monkeypatch.delenv("SATB_AUDIVERIS", raising=False)
    monkeypatch.delenv("AUDIVERIS", raising=False)
    found = find_audiveris()
    if (Path.home() / "Audiveris-local-dev3l" / "Audiveris.exe").is_file():
        assert found is not None
        assert "Audiveris-local-dev3l" in str(found)


def test_find_audiveris_bundled_precedence(monkeypatch, tmp_path: Path):
    """Bundled Audiveris/Audiveris.exe takes precedence over PATH and DEFAULT_WINDOWS_PATHS."""
    monkeypatch.delenv("SATB_AUDIVERIS", raising=False)
    monkeypatch.delenv("AUDIVERIS", raising=False)

    fake_app_dir = tmp_path / "portable"
    fake_exe = fake_app_dir / "SATB Splitter.exe"
    fake_app_dir.mkdir(parents=True)
    fake_exe.write_bytes(b"MZ")

    bundled = fake_app_dir / "Audiveris" / "Audiveris.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"MZ")

    path_exe = tmp_path / "on_path" / "Audiveris.exe"
    path_exe.parent.mkdir(parents=True)
    path_exe.write_bytes(b"MZ")

    monkeypatch.setattr(audiveris_mod.sys, "frozen", True, raising=False)
    monkeypatch.setattr(audiveris_mod.sys, "executable", str(fake_exe), raising=False)
    monkeypatch.setattr(audiveris_mod.shutil, "which", lambda _name: str(path_exe))

    assert bundled in audiveris_mod._bundled_audiveris_paths()
    assert find_audiveris() == bundled.resolve()

    # Env var and explicit still outrank bundled
    env_exe = tmp_path / "env" / "Audiveris.exe"
    env_exe.parent.mkdir(parents=True)
    env_exe.write_bytes(b"MZ")
    monkeypatch.setenv("SATB_AUDIVERIS", str(env_exe))
    assert find_audiveris() == env_exe


def test_run_audiveris_export_sets_tessdata_prefix(monkeypatch, tmp_path: Path):
    """run_audiveris_export injects TESSDATA_PREFIX=<exe.parent>/tessdata when dir exists."""
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    aud_dir = tmp_path / "Audiveris"
    tess_dir = aud_dir / "tessdata"
    tess_dir.mkdir(parents=True)
    exe = aud_dir / "Audiveris.exe"
    exe.write_bytes(b"MZ")

    src_pdf = tmp_path / "score.pdf"
    src_pdf.write_bytes(b"%PDF-1.4\n")
    out_dir = tmp_path / "out"

    captured: dict = {}

    class DummyProc:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return DummyProc()

    monkeypatch.setattr(audiveris_mod.subprocess, "run", fake_run)
    run_audiveris_export(src_pdf, out_dir, audiveris=exe)

    assert captured["kwargs"]["env"]["TESSDATA_PREFIX"] == str(tess_dir)
    if audiveris_mod.sys.platform == "win32":
        assert captured["kwargs"].get("creationflags") == getattr(
            audiveris_mod.subprocess, "CREATE_NO_WINDOW", 0
        )

    # Existing TESSDATA_PREFIX is respected
    monkeypatch.setenv("TESSDATA_PREFIX", str(tmp_path / "custom_tess"))
    run_audiveris_export(src_pdf, out_dir, audiveris=exe)
    assert captured["kwargs"]["env"]["TESSDATA_PREFIX"] == str(tmp_path / "custom_tess")


def test_studio_main_window_theme_kpis_queue_and_flags_filter(tmp_path: Path):
    """MainWindow supports light/dark theme toggle, KPI cards, folder queue & flag filtering."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()

    # Default or loaded theme can be toggled cleanly between light and dark
    win.set_theme("light")
    assert win._theme == "light"
    assert "Dark" in win.btn_theme.text()
    assert get_theme_colors("light")["name"] == "light"

    win.toggle_theme()
    assert win._theme == "dark"
    assert "Light" in win.btn_theme.text()
    assert get_theme_colors("dark")["name"] == "dark"

    win.toggle_theme()
    assert win._theme == "light"
    apply_app_theme(app, "light")

    # Queue file and folder addition
    score_dir = tmp_path / "scores"
    score_dir.mkdir()
    f1 = score_dir / "01_Hymn.pdf"
    f2 = score_dir / "02_Motet.musicxml"
    f1.write_bytes(b"%PDF-1.4\n")
    f2.write_text("<score-partwise/>", encoding="utf-8")

    win.add_paths([score_dir])
    assert win.file_list.count() == 2
    assert win.file_count_label.text() == "2 files"
    assert win.kpi_scores_val.text() == "2"

    # Simulated pipeline completion updates KPIs, voice list, and review flags filter
    c1 = DivisiChoice(
        measure=14,
        part_index=0,
        offset=0.0,
        upper_role="Soprano",
        lower_role="Alto",
        pitches=["D5", "A4", "F4"],
        auto_owner="upper",
        chosen_upper=["D5"],
        chosen_lower=["F4"],
        ambiguous=True,
        confidence=0.52,
        reason="no stem distinction",
        choice_key="14|Soprano|0.0",
    )
    voices_dir = tmp_path / "out" / "voices"
    voices_dir.mkdir(parents=True)
    review_txt = voices_dir / "01_Hymn_REVIEW.txt"
    review_txt.write_text("Review summary", encoding="utf-8")

    win._on_finished(
        {
            "input": str(f1),
            "work_dir": str(tmp_path / "out"),
            "voices_dir": str(voices_dir),
            "satb": str(voices_dir / "01_Hymn_SATB.musicxml"),
            "mscz": str(voices_dir / "01_Hymn_SATB.mscz"),
            "mscz_message": "MuseScore 4 file ready",
            "voices": {
                "Soprano": str(voices_dir / "01_Hymn_Soprano.musicxml"),
                "Alto": str(voices_dir / "01_Hymn_Alto.musicxml"),
                "Tenor": str(voices_dir / "01_Hymn_Tenor.musicxml"),
                "Bass": str(voices_dir / "01_Hymn_Bass.musicxml"),
            },
            "mxl": [str(f2)],
            "logs": ["Done"],
            "flags": [
                "m14 part0: divisi_resolved — Soprano=D5 Alto=F4",
                "m28 part1: voice_crossing — Tenor below Bass",
            ],
            "divisi_choices": [c1],
        }
    )

    assert win.kpi_voices_val.text() == "7"  # SATB xml + mscz + 4 voices + REVIEW.txt
    assert win.kpi_divisi_val.text() == "1"
    assert "1 ambiguous" in win.kpi_divisi_sub.text()
    assert win.kpi_flags_val.text() == "2"
    assert "m14 part0" in win.flags_view.toPlainText()
    assert "m28 part1" in win.flags_view.toPlainText()

    # Filter review flags by keyword
    win.flags_filter_edit.setText("m28")
    assert "m28 part1" in win.flags_view.toPlainText()
    assert "m14 part0" not in win.flags_view.toPlainText()

    # Clearing filter restores full flags text
    win.flags_filter_edit.clear()
    assert "m14 part0" in win.flags_view.toPlainText()
    assert "m28 part1" in win.flags_view.toPlainText()

    # Log clear button
    win.log("Test log line")
    assert "Test log line" in win.log_view.toPlainText()
    win.log_view_clear()
    assert win.log_view.toPlainText() == ""

    win.close()
    app.processEvents()


def test_studio_divisi_dialog_search_and_badges():
    """DivisiResolverDialog search box filters rows and updates active override badges."""
    app = QApplication.instance() or QApplication([])
    c1 = DivisiChoice(
        measure=4,
        part_index=0,
        offset=0.0,
        upper_role="Soprano",
        lower_role="Alto",
        pitches=["E5", "C5", "G4"],
        auto_owner="upper",
        chosen_upper=["E5"],
        chosen_lower=["G4"],
        ambiguous=True,
        confidence=0.5,
        reason="shared stem",
        choice_key="4|Soprano|0.0",
    )
    c2 = DivisiChoice(
        measure=19,
        part_index=1,
        offset=0.0,
        upper_role="Tenor",
        lower_role="Bass",
        pitches=["D4", "A3", "D3"],
        auto_owner="lower",
        chosen_upper=["D4"],
        chosen_lower=["D3"],
        ambiguous=True,
        confidence=0.55,
        reason="shared stem",
        choice_key="19|Tenor|0.0",
    )
    dlg = DivisiResolverDialog([c1, c2])
    assert dlg.table.rowCount() == 2
    assert "Total Chords: 2" in dlg.badge_total.text()
    assert "Ambiguous: 2" in dlg.badge_ambig.text()

    dlg.search_edit.setText("m19")
    assert dlg.table.rowCount() == 1
    assert "m19" in dlg.table.item(0, 0).text()

    dlg.search_edit.clear()
    assert dlg.table.rowCount() == 2
    dlg.close()
    app.processEvents()


def test_packaged_exe_icon_and_desktop_shortcut_exist():
    """Verify SATB Splitter.exe, assets/satb_splitter.ico, and Desktop shortcut exist."""
    root = Path(__file__).resolve().parents[1]
    ico_path = root / "assets" / "satb_splitter.ico"
    exe_path = root / "SATB Splitter.exe"
    lnk_path = Path.home() / "Desktop" / "SATB Splitter.lnk"

    assert ico_path.is_file() and ico_path.stat().st_size > 1024
    assert exe_path.is_file() and exe_path.stat().st_size > 4096
    assert exe_path.read_bytes()[:2] == b"MZ"
    assert lnk_path.is_file() and lnk_path.stat().st_size > 100
