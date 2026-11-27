"""Unit, mocked GUI/worker, and real MuseScore 4 integration tests for Grand Piano MP3 export."""

from __future__ import annotations

import os
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

import satb_splitter.export.mp3 as mp3_mod
from satb_splitter.export.mp3 import (
    DEFAULT_MP3_STALL_S,
    DEFAULT_MP3_TIMEOUT_S,
    VALID_BITRATES,
    Mp3Result,
    _is_valid_mp3,
    discover_voice_musicxml_files,
    export_mp3_grand_piano,
    export_voices_mp3_grand_piano,
    rewrite_musicxml_as_grand_piano,
)
from satb_splitter.export.mscz import find_musescore
from satb_splitter.gui.main_window import MainWindow
from satb_splitter.gui.workers import Mp3ExportWorker


SAMPLE_XML = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise version="4.0">
  <!-- Top score comment -->
  <part-list>
    <score-part id="P1">
      <part-name>Soprano</part-name>
      <part-abbreviation>S.</part-abbreviation>
      <score-instrument id="P1-I99">
        <instrument-name>Choir Aahs</instrument-name>
      </score-instrument>
      <midi-device id="P1-I99" port="1" />
      <midi-instrument id="P1-I99">
        <midi-channel>4</midi-channel>
        <midi-program>53</midi-program>
      </midi-instrument>
    </score-part>
    <score-part id="P2">
      <part-name>Alto</part-name>
    </score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <note>
        <pitch><step>C</step><octave>5</octave></pitch>
        <duration>4</duration>
        <type>whole</type>
        <instrument id="P1-I99" />
      </note>
    </measure>
  </part>
  <part id="P2">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
      </attributes>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>4</duration>
        <type>whole</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""


def _strip_doctype(xml_text: str) -> ET.Element:
    import re

    clean = re.sub(r"<!DOCTYPE\b[^>]*>", "", xml_text, count=1)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    return ET.fromstring(clean, parser=parser)


# ---------------------------------------------------------------------------
# Unit tests: MusicXML Grand Piano rewriting
# ---------------------------------------------------------------------------


def test_rewrite_musicxml_as_grand_piano_plain_and_preserves_source(tmp_path: Path):
    src = tmp_path / "song_Soprano.musicxml"
    dest = tmp_path / "piano_out.musicxml"
    src.write_text(SAMPLE_XML, encoding="utf-8")
    before = src.read_text(encoding="utf-8")

    out = rewrite_musicxml_as_grand_piano(src, dest)
    assert out == dest and dest.is_file()
    assert src.read_text(encoding="utf-8") == before  # never mutated

    out_txt = dest.read_text(encoding="utf-8")
    assert out_txt.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert "<!DOCTYPE score-partwise" in out_txt
    assert "<!-- Top score comment -->" in out_txt

    root = _strip_doctype(out_txt)
    parts = root.findall("./part-list/score-part")
    assert len(parts) == 2

    for idx, (sp, expected_ch) in enumerate(zip(parts, ("1", "2")), start=1):
        pid = f"P{idx}"
        assert sp.attrib["id"] == pid
        assert sp.findtext("part-name") == "Grand Piano"
        assert sp.find("part-abbreviation") is None
        assert sp.find("midi-device") is None

        sis = sp.findall("score-instrument")
        assert len(sis) == 1
        assert sis[0].attrib["id"] == f"{pid}-I1"
        assert sis[0].findtext("instrument-name") == "Acoustic Grand Piano"
        assert sis[0].findtext("instrument-sound") == "keyboard.piano.grand"

        mis = sp.findall("midi-instrument")
        assert len(mis) == 1
        assert mis[0].attrib["id"] == f"{pid}-I1"
        assert mis[0].findtext("midi-channel") == expected_ch
        assert mis[0].findtext("midi-program") == "1"
        assert mis[0].findtext("volume") == "80"
        assert mis[0].findtext("pan") == "0"

    assert all(n.find("instrument") is None for n in root.iter("note"))


def test_rewrite_skips_midi_channel_10(tmp_path: Path):
    # 11 parts -> channels 1..9, 11, 12 (skipping percussion channel 10)
    sp_xml = "\n".join(
        f'<score-part id="P{i}"><part-name>Part {i}</part-name></score-part>'
        for i in range(1, 12)
    )
    p_xml = "\n".join(
        f'<part id="P{i}"><measure number="1"><note><rest/><duration>4</duration></note></measure></part>'
        for i in range(1, 12)
    )
    src = tmp_path / "many.musicxml"
    dest = tmp_path / "many_piano.musicxml"
    src.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><score-partwise version="4.0">'
        f"<part-list>{sp_xml}</part-list>{p_xml}</score-partwise>",
        encoding="utf-8",
    )
    rewrite_musicxml_as_grand_piano(src, dest)
    root = _strip_doctype(dest.read_text(encoding="utf-8"))
    channels = [
        sp.findtext("./midi-instrument/midi-channel")
        for sp in root.findall("./part-list/score-part")
    ]
    assert channels == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "11", "12"]
    assert "10" not in channels


def test_rewrite_tempo_inserts_when_missing_and_updates_when_present(tmp_path: Path):
    # Case 1: no tempo in measure 1 -> inserts <direction> immediately after <attributes>
    src1 = tmp_path / "no_tempo.musicxml"
    dest1 = tmp_path / "no_tempo_96.musicxml"
    src1.write_text(SAMPLE_XML, encoding="utf-8")
    rewrite_musicxml_as_grand_piano(src1, dest1, tempo_bpm=96)
    root1 = _strip_doctype(dest1.read_text(encoding="utf-8"))
    m1 = root1.find("./part/measure")
    assert m1 is not None
    tags = [el.tag for el in list(m1)]
    assert tags[:3] == ["attributes", "direction", "note"]
    assert m1.findtext("./direction/direction-type/metronome/beat-unit") == "quarter"
    assert m1.findtext("./direction/direction-type/metronome/per-minute") == "96"
    snd = m1.find("./direction/sound")
    assert snd is not None and snd.attrib.get("tempo") == "96"

    # Case 2: existing tempo -> updates existing <sound tempo> and <per-minute>, no duplicate direction
    dest2 = tmp_path / "updated_120.musicxml"
    rewrite_musicxml_as_grand_piano(dest1, dest2, tempo_bpm=120)
    root2 = _strip_doctype(dest2.read_text(encoding="utf-8"))
    m1_2 = root2.find("./part/measure")
    assert len(m1_2.findall("direction")) == 1
    assert m1_2.findtext("./direction/direction-type/metronome/per-minute") == "120"
    assert m1_2.find("./direction/sound").attrib["tempo"] == "120"


def test_rewrite_tempo_inserts_at_index_0_if_no_attributes(tmp_path: Path):
    src = tmp_path / "no_attr.musicxml"
    dest = tmp_path / "no_attr_out.musicxml"
    src.write_text(
        '<?xml version="1.0" encoding="utf-8"?><score-partwise version="4.0">'
        '<part-list><score-part id="P1"><part-name>S</part-name></score-part></part-list>'
        '<part id="P1"><measure number="1"><note><rest/><duration>4</duration></note></measure></part>'
        "</score-partwise>",
        encoding="utf-8",
    )
    rewrite_musicxml_as_grand_piano(src, dest, tempo_bpm=84)
    root = _strip_doctype(dest.read_text(encoding="utf-8"))
    m1 = root.find("./part/measure")
    assert [el.tag for el in list(m1)][0] == "direction"
    assert m1.find("./direction/sound").attrib["tempo"] == "84"


def test_rewrite_reads_compressed_mxl_container_and_fallback(tmp_path: Path):
    mxl_container = tmp_path / "with_container.mxl"
    with zipfile.ZipFile(mxl_container, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<container><rootfiles>"
            '<rootfile full-path="scores/main.musicxml"/>'
            "</rootfiles></container>",
        )
        zf.writestr("scores/main.musicxml", SAMPLE_XML)

    out1 = rewrite_musicxml_as_grand_piano(mxl_container, tmp_path / "out1.musicxml")
    r1 = _strip_doctype(out1.read_text(encoding="utf-8"))
    assert r1.findtext("./part-list/score-part/part-name") == "Grand Piano"

    # Fallback without container.xml
    mxl_fallback = tmp_path / "no_container.mxl"
    with zipfile.ZipFile(mxl_fallback, "w") as zf:
        zf.writestr("score.xml", SAMPLE_XML)

    out2 = rewrite_musicxml_as_grand_piano(mxl_fallback, tmp_path / "out2.musicxml")
    r2 = _strip_doctype(out2.read_text(encoding="utf-8"))
    assert r2.findtext("./part-list/score-part/midi-instrument/midi-program") == "1"


# ---------------------------------------------------------------------------
# Unit tests: _is_valid_mp3 & discover_voice_musicxml_files
# ---------------------------------------------------------------------------


def test_is_valid_mp3_checks_size_and_headers(tmp_path: Path):
    missing = tmp_path / "missing.mp3"
    assert not _is_valid_mp3(missing)

    too_small = tmp_path / "small.mp3"
    too_small.write_bytes(b"ID3" + b"\x00" * 100)
    assert not _is_valid_mp3(too_small)

    bad_hdr = tmp_path / "bad.mp3"
    bad_hdr.write_bytes(b"RIFF" + b"\x00" * 512)
    assert not _is_valid_mp3(bad_hdr)

    id3 = tmp_path / "ok_id3.mp3"
    id3.write_bytes(b"ID3" + b"\x03\x00" + b"\x00" * 400)
    assert _is_valid_mp3(id3)

    sync = tmp_path / "ok_sync.mp3"
    sync.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 400)
    assert _is_valid_mp3(sync)


def test_discover_voice_musicxml_files_canonical_order_and_extras(tmp_path: Path):
    d = tmp_path / "voices"
    d.mkdir()
    for name in (
        "Hymn_SATB.musicxml",
        "Hymn_Bass.musicxml",
        "Hymn_Tenor.musicxml",
        "Hymn_Alto.musicxml",
        "Hymn_soprano.xml",
        "Solo_Violin.mxl",
        "Extra_Organ.musicxml",
        "notes.txt",
    ):
        (d / name).write_text("<score-partwise/>", encoding="utf-8")

    found = discover_voice_musicxml_files([d])
    assert list(found.keys()) == [
        "Soprano",
        "Alto",
        "Tenor",
        "Bass",
        "SATB",
        "Extra_Organ",
        "Solo_Violin",
    ]
    assert found["Soprano"].name == "Hymn_soprano.xml"
    assert found["SATB"].name == "Hymn_SATB.musicxml"


# ---------------------------------------------------------------------------
# Unit / Mocked tests: export_mp3_grand_piano & export_voices_mp3_grand_piano
# ---------------------------------------------------------------------------


def test_export_mp3_constants_and_validation(tmp_path: Path, monkeypatch):
    assert DEFAULT_MP3_TIMEOUT_S == 180
    assert DEFAULT_MP3_STALL_S == 45.0
    assert VALID_BITRATES == (128, 192, 320)

    src = tmp_path / "Hymn_Soprano.musicxml"
    src.write_text(SAMPLE_XML, encoding="utf-8")

    with pytest.raises(ValueError):
        export_mp3_grand_piano(src, bitrate=0)
    with pytest.raises(ValueError):
        export_mp3_grand_piano(src, bitrate=-128)

    missing_res = export_mp3_grand_piano(tmp_path / "missing.musicxml")
    assert not missing_res.ok and "does not exist" in missing_res.message

    monkeypatch.setattr(mp3_mod, "find_musescore", lambda _explicit=None: None)
    no_ms = export_mp3_grand_piano(src)
    assert not no_ms.ok and "MuseScore was not found" in no_ms.message


def test_export_mp3_grand_piano_mocked_musescore(tmp_path: Path, monkeypatch):
    exe = tmp_path / "MuseScore4.exe"
    exe.write_bytes(b"MZ")
    src = tmp_path / "Hymn_Soprano.musicxml"
    src.write_text(SAMPLE_XML, encoding="utf-8")

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("SATB_MP3_TIMEOUT", "95")
    monkeypatch.setenv("SATB_MP3_STALL", "15")

    captured: dict = {}

    def fake_run(cmd, tmp_mp3: Path, env: dict, timeout_s: float, stall_s: float, kwargs: dict):
        captured["cmd"] = cmd
        captured["env"] = env
        captured["timeout_s"] = timeout_s
        captured["stall_s"] = stall_s
        captured["kwargs"] = kwargs
        captured["input_xml_text"] = Path(cmd[-1]).read_text(encoding="utf-8")
        tmp_mp3.write_bytes(b"ID3" + b"\x03\x00" + b"\x00" * 512)
        return 0, "", "", None

    monkeypatch.setattr(mp3_mod, "_run_musescore", fake_run)

    res = export_mp3_grand_piano(
        src,
        musescore=exe,
        bitrate=320,
        tempo_bpm=108,
        role="Soprano",
    )
    assert res.ok
    assert res.role == "Soprano"
    assert res.path == tmp_path / "Hymn_Soprano_Piano.mp3"
    assert _is_valid_mp3(res.path)
    assert "320 kbps" in res.message

    assert captured["cmd"][0] == str(exe)
    assert captured["cmd"][1:3] == ["-b", "320"]
    assert captured["cmd"][3] == "-o"
    assert captured["timeout_s"] == 95.0
    assert captured["stall_s"] == 15.0
    assert "QT_QPA_PLATFORM" not in captured["env"]
    if os.name == "nt":
        assert captured["kwargs"].get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)

    xml_root = _strip_doctype(captured["input_xml_text"])
    assert xml_root.findtext("./part-list/score-part/part-name") == "Grand Piano"
    assert xml_root.find("./part/measure/direction/sound").attrib["tempo"] == "108"
    assert not (tmp_path / "Hymn_Soprano_Piano.mp3-tmp").exists()


def test_export_voices_mp3_grand_piano_progress_and_cancel(tmp_path: Path, monkeypatch):
    exe = tmp_path / "MuseScore4.exe"
    exe.write_bytes(b"MZ")
    s_xml = tmp_path / "Piece_Soprano.musicxml"
    a_xml = tmp_path / "Piece_Alto.musicxml"
    t_xml = tmp_path / "Piece_Tenor.musicxml"
    for p in (s_xml, a_xml, t_xml):
        p.write_text(SAMPLE_XML, encoding="utf-8")

    calls: list[str] = []
    progress_msgs: list[str] = []

    def fake_run(cmd, tmp_mp3: Path, env, timeout_s, stall_s, kwargs):
        calls.append(tmp_mp3.name)
        tmp_mp3.write_bytes(b"ID3" + b"\x00" * 512)
        return 0, "", "", None

    monkeypatch.setattr(mp3_mod, "_run_musescore", fake_run)

    cancel_after_two = [False]

    def on_prog(msg: str):
        progress_msgs.append(msg)
        if len(progress_msgs) >= 2:
            cancel_after_two[0] = True

    res_map = export_voices_mp3_grand_piano(
        {"Soprano": s_xml, "Alto": a_xml, "Tenor": t_xml},
        tmp_path / "mp3_out",
        musescore=exe,
        progress=on_prog,
        should_cancel=lambda: cancel_after_two[0],
    )
    assert list(res_map.keys()) == ["Soprano", "Alto"]
    assert all(r.ok for r in res_map.values())
    assert "Rendering Soprano (1/3)" in progress_msgs[0]
    assert "Rendering Alto (2/3)" in progress_msgs[1]
    assert not (tmp_path / "mp3_out" / "Piece_Tenor_Piano.mp3").exists()


# ---------------------------------------------------------------------------
# Mocked GUI & Worker tests
# ---------------------------------------------------------------------------


def test_mp3_export_worker_success_partial_and_all_failed(tmp_path: Path, monkeypatch):
    s_xml = tmp_path / "Song_Soprano.musicxml"
    a_xml = tmp_path / "Song_Alto.musicxml"
    s_xml.write_text(SAMPLE_XML, encoding="utf-8")
    a_xml.write_text(SAMPLE_XML, encoding="utf-8")

    # 1. Partial success (Soprano OK, Alto fails) -> emits finished with errors
    def fake_export(musicxml, dest=None, **kw):
        role = kw.get("role", "")
        if role == "Alto":
            return Mp3Result(None, "Alto failed", role="Alto")
        p = Path(dest)
        p.write_bytes(b"ID3" + b"\x00" * 512)
        return Mp3Result(p, "OK", role=role)

    monkeypatch.setattr(mp3_mod, "export_mp3_grand_piano", fake_export)

    done_events = []
    finished_events = []
    failed_events = []

    w1 = Mp3ExportWorker({"Soprano": s_xml, "Alto": a_xml}, tmp_path / "out", bitrate=192)
    w1.file_done.connect(lambda r, p, m: done_events.append((r, p, m)))
    w1.finished.connect(finished_events.append)
    w1.failed.connect(failed_events.append)
    w1.run()

    assert len(done_events) == 1 and done_events[0][0] == "Soprano"
    assert not failed_events
    assert len(finished_events) == 1
    assert "Soprano" in finished_events[0]["mp3_files"]
    assert len(finished_events[0]["errors"]) == 1

    # 2. All fail -> emits failed
    monkeypatch.setattr(
        mp3_mod,
        "export_mp3_grand_piano",
        lambda *a, **kw: Mp3Result(None, "MuseScore missing", role=kw.get("role", "")),
    )
    failed_events.clear()
    finished_events.clear()
    w2 = Mp3ExportWorker({"Soprano": s_xml}, tmp_path / "out")
    w2.finished.connect(finished_events.append)
    w2.failed.connect(failed_events.append)
    w2.run()
    assert not finished_events
    assert len(failed_events) == 1 and "MuseScore missing" in failed_events[0]

    # 3. Cancelled -> emits failed("Cancelled")
    ev = Event()
    ev.set()
    failed_events.clear()
    w3 = Mp3ExportWorker({"Soprano": s_xml}, tmp_path / "out", cancel_event=ev)
    w3.failed.connect(failed_events.append)
    w3.run()
    assert failed_events == ["Cancelled"]


def test_main_window_mp3_tab_widgets_settings_load_and_export(tmp_path: Path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    win = MainWindow()

    # Tab 3 exists right after Review flags
    assert win.tabs.count() == 4
    assert "MP3 Audio" in win.tabs.tabText(3)

    # Exact widget names & defaults
    assert set(win.mp3_voice_checks.keys()) == {"Soprano", "Alto", "Tenor", "Bass", "SATB"}
    for r in ("Soprano", "Alto", "Tenor", "Bass"):
        assert win.mp3_voice_checks[r].isChecked()
    assert not win.mp3_voice_checks["SATB"].isChecked()
    assert win.mp3_voice_checks["SATB"].text() == "SATB (Full)"

    assert [win.mp3_bitrate_combo.itemData(i) for i in range(win.mp3_bitrate_combo.count())] == [
        192,
        128,
        320,
    ]
    assert win.mp3_tempo_spin.minimum() == 0
    assert win.mp3_tempo_spin.maximum() == 200
    assert win.mp3_tempo_spin.specialValueText() == "Score tempo"
    assert win.mp3_tempo_spin.suffix() == " BPM"
    assert win.btn_export_mp3.objectName() == "primary"
    assert win.btn_mp3_load.objectName() == "subtle"
    assert win.btn_mp3_cancel.objectName() == "danger"
    assert not win.btn_mp3_cancel.isVisible()
    assert not win.mp3_progress.isVisible()
    assert win.btn_play_mp3.objectName() == "success"
    assert not win.btn_export_mp3.isEnabled()

    # Settings persistence
    win.mp3_bitrate_combo.setCurrentIndex(2)  # 320
    win.mp3_tempo_spin.setValue(104)
    win._save_settings()
    win.mp3_bitrate_combo.setCurrentIndex(0)
    win.mp3_tempo_spin.setValue(0)
    win._load_settings()
    assert win.mp3_bitrate_combo.currentData() == 320
    assert win.mp3_tempo_spin.value() == 104

    # Prepare synthetic split voice files
    vdir = tmp_path / "voices"
    vdir.mkdir()
    s_xml = vdir / "Ave_Soprano.musicxml"
    a_xml = vdir / "Ave_Alto.musicxml"
    t_xml = vdir / "Ave_Tenor.musicxml"
    b_xml = vdir / "Ave_Bass.musicxml"
    satb_xml = vdir / "Ave_SATB.musicxml"
    for p in (s_xml, a_xml, t_xml, b_xml, satb_xml):
        p.write_text(SAMPLE_XML, encoding="utf-8")

    # _on_finished populates _mp3_sources
    win._on_finished(
        {
            "input": str(tmp_path / "Ave.musicxml"),
            "work_dir": str(tmp_path),
            "voices_dir": str(vdir),
            "satb": str(satb_xml),
            "mscz": None,
            "mscz_message": "",
            "voices": {
                "Soprano": str(s_xml),
                "Alto": str(a_xml),
                "Tenor": str(t_xml),
                "Bass": str(b_xml),
            },
            "mxl": [str(tmp_path / "Ave.musicxml")],
            "logs": [],
            "flags": [],
            "divisi_choices": [],
        }
    )
    assert set(win._mp3_sources.keys()) == {"Soprano", "Alto", "Tenor", "Bass", "SATB"}
    assert win.btn_export_mp3.isEnabled()

    # load_mp3_sources with 1 file auto-discovers siblings and switches to Tab 3
    win._mp3_sources.clear()
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileNames",
        lambda *a, **kw: ([str(s_xml)], "MusicXML (*.musicxml *.xml *.mxl)"),
    )
    win.load_mp3_sources()
    assert win.tabs.currentIndex() == 3
    assert set(win._mp3_sources.keys()) == {"Soprano", "Alto", "Tenor", "Bass", "SATB"}

    # Early guard when MuseScore 4 is not found -> warns and does not start thread
    warnings_shown: list[str] = []
    monkeypatch.setattr(win, "_find_musescore", lambda: None)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, text, *a, **kw: warnings_shown.append(str(text)),
    )
    win.start_mp3_export()
    assert win._mp3_thread is None
    assert len(warnings_shown) == 1 and "MuseScore 4 was not found" in warnings_shown[0]

    # Mock export_mp3_grand_piano and run start_mp3_export through QThread
    def fake_export(musicxml, dest=None, **kw):
        p = Path(dest)
        p.write_bytes(b"ID3" + b"\x00" * 512)
        return Mp3Result(p, f"Grand Piano MP3 written: {p.name}", role=kw.get("role", ""))

    monkeypatch.setattr(win, "_find_musescore", lambda: str(tmp_path / "MuseScore4.exe"))
    monkeypatch.setattr(mp3_mod, "export_mp3_grand_piano", fake_export)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: None)

    loop = QEventLoop()
    win.start_mp3_export()
    assert win._mp3_thread is not None
    win._mp3_thread.finished.connect(loop.quit)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    app.processEvents()

    assert win._mp3_thread is None
    assert win.mp3_list.count() == 4  # S/A/T/B checked, SATB unchecked
    assert win.mp3_stack.currentIndex() == 1
    assert win.btn_play_mp3.isEnabled()
    assert win.btn_open_mp3_folder.isEnabled()
    assert (vdir / "Ave_Soprano_Piano.mp3").is_file()

    win.close()
    app.processEvents()


# ---------------------------------------------------------------------------
# Real MuseScore 4 integration test
# ---------------------------------------------------------------------------


def test_real_musescore4_grand_piano_mp3_export(tmp_path: Path):
    exe = find_musescore()
    if exe is None:
        pytest.skip("MuseScore 4 not installed on this machine")

    repo_root = Path(__file__).resolve().parents[1]
    demo = repo_root / "demo_score.musicxml"
    assert demo.is_file()
    before_bytes = demo.read_bytes()

    out_mp3 = tmp_path / "demo_score_Piano.mp3"
    t0 = time.monotonic()
    res = export_mp3_grand_piano(
        demo,
        out_mp3,
        musescore=exe,
        bitrate=192,
        tempo_bpm=96,
        role="SATB",
    )
    elapsed = time.monotonic() - t0

    assert demo.read_bytes() == before_bytes  # original untouched
    assert res.ok, f"Real MuseScore MP3 export failed: {res.message}"
    assert res.path == out_mp3
    assert _is_valid_mp3(out_mp3)
    assert out_mp3.stat().st_size > 1024
    assert elapsed < DEFAULT_MP3_TIMEOUT_S
    assert not (tmp_path / "demo_score_Piano.mp3-tmp").exists()
