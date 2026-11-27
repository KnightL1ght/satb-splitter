"""Export MusicXML / MXL voice parts as Grand Piano MP3 files via MuseScore."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from satb_splitter.export.mscz import (
    _env_number,
    _run_musescore,
    find_musescore,
    musescore_child_env,
)

DEFAULT_MP3_TIMEOUT_S = 180
DEFAULT_MP3_STALL_S = 45.0
VALID_BITRATES = (128, 192, 320)

_CANONICAL_ROLES: tuple[str, ...] = ("Soprano", "Alto", "Tenor", "Bass", "SATB")
_MUSICXML_EXTS = {".musicxml", ".xml", ".mxl"}
_REMOVE_SCORE_PART_TAGS = {
    "part-name",
    "part-abbreviation",
    "score-instrument",
    "midi-device",
    "midi-instrument",
}


@dataclass
class Mp3Result:
    """Result of exporting one MusicXML score to a Grand Piano MP3."""

    path: Path | None
    message: str
    role: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


def _read_musicxml_text(src: Path) -> str:
    """Return UTF-8 MusicXML text from a plain ``.musicxml``/``.xml`` or compressed ``.mxl``."""
    if src.suffix.lower() == ".mxl":
        with zipfile.ZipFile(src, "r") as zf:
            names = zf.namelist()
            rootfile: str | None = None
            container_name = next(
                (n for n in names if n.replace("\\", "/").lower() == "meta-inf/container.xml"),
                None,
            )
            if container_name is not None:
                try:
                    c_root = ET.fromstring(zf.read(container_name))
                    for elem in c_root.iter():
                        if elem.tag == "rootfile" or elem.tag.endswith("}rootfile"):
                            fp = elem.attrib.get("full-path")
                            if fp and fp in names:
                                rootfile = fp
                                break
                except ET.ParseError:
                    rootfile = None
            if rootfile is None:
                for n in names:
                    norm = n.replace("\\", "/").lower()
                    if norm.startswith("meta-inf/") or norm.endswith("/"):
                        continue
                    if norm.endswith((".musicxml", ".xml")):
                        rootfile = n
                        break
            if rootfile is None:
                raise ValueError(f"No MusicXML rootfile found inside {src.name}")
            return zf.read(rootfile).decode("utf-8-sig")
    return src.read_text(encoding="utf-8-sig")


def _next_midi_channel(ch: int) -> int:
    """Return the next non-percussion MIDI channel (1..16, skipping channel 10)."""
    nxt = ch + 1
    if nxt == 10:
        nxt = 11
    if nxt > 16:
        nxt = 1
    return nxt


def rewrite_musicxml_as_grand_piano(
    src: str | Path,
    dest: str | Path,
    *,
    tempo_bpm: int | None = None,
) -> Path:
    """Write a copy of ``src`` with every part mapped to Acoustic Grand Piano (MIDI program 1).

    Never mutates ``src``. Optionally overrides all tempo markings (or inserts a
    metronome/sound direction into measure 1 of part 1 when none exists).
    """
    src_path = Path(src)
    dest_path = Path(dest)

    txt = _read_musicxml_text(src_path)
    header_m = re.match(r"^(.*?)(?=<score-partwise\b)", txt, re.S)
    header = header_m.group(1) if header_m else ""
    if not header.strip():
        header = '<?xml version="1.0" encoding="utf-8"?>\n'
    elif "<?xml" not in header[:120]:
        header = '<?xml version="1.0" encoding="utf-8"?>\n' + header
    else:
        header = re.sub(
            r'(<\?xml\b[^>]*?\bencoding=)(["\'])[^"\']+\2',
            r'\1\2utf-8\2',
            header,
            count=1,
            flags=re.IGNORECASE,
        )

    clean_txt = re.sub(
        r"<!DOCTYPE\b[^>\[]*(?:\[[^\]]*\])?\s*>",
        "",
        txt,
        count=1,
        flags=re.S,
    )
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(clean_txt, parser=parser)

    part_list = root.find("part-list")
    if part_list is not None:
        ch = 1
        for sp in part_list.findall("score-part"):
            pid = sp.attrib.get("id", "P1")
            for child in list(sp):
                if child.tag in _REMOVE_SCORE_PART_TAGS:
                    sp.remove(child)

            pn = ET.Element("part-name")
            pn.text = "Grand Piano"

            inst_id = f"{pid}-I1"
            si = ET.Element("score-instrument", {"id": inst_id})
            iname = ET.SubElement(si, "instrument-name")
            iname.text = "Acoustic Grand Piano"
            isound = ET.SubElement(si, "instrument-sound")
            isound.text = "keyboard.piano.grand"

            mi = ET.Element("midi-instrument", {"id": inst_id})
            mch = ET.SubElement(mi, "midi-channel")
            mch.text = str(ch)
            mprog = ET.SubElement(mi, "midi-program")
            mprog.text = "1"
            mvol = ET.SubElement(mi, "volume")
            mvol.text = "80"
            mpan = ET.SubElement(mi, "pan")
            mpan.text = "0"

            insert_idx = 0
            for idx, existing in enumerate(list(sp)):
                if existing.tag in ("identification", "part-link"):
                    insert_idx = idx + 1
            sp.insert(insert_idx, pn)
            sp.append(si)
            sp.append(mi)

            ch = _next_midi_channel(ch)

    for note_el in root.iter("note"):
        for inst in list(note_el.findall("instrument")):
            note_el.remove(inst)

    if tempo_bpm is not None and int(tempo_bpm) > 0:
        bpm_str = str(int(tempo_bpm))
        for sound_el in root.iter("sound"):
            if "tempo" in sound_el.attrib:
                sound_el.attrib["tempo"] = bpm_str
        for met_el in root.iter("metronome"):
            for pm_el in met_el.findall("per-minute"):
                pm_el.text = bpm_str

        first_part = root.find("part")
        if first_part is not None:
            first_measure = first_part.find("measure")
            if first_measure is not None:
                has_tempo = any("tempo" in s.attrib for s in first_measure.iter("sound"))
                if not has_tempo:
                    direction = ET.Element("direction", {"placement": "above"})
                    dtype = ET.SubElement(direction, "direction-type")
                    met = ET.SubElement(dtype, "metronome")
                    bu = ET.SubElement(met, "beat-unit")
                    bu.text = "quarter"
                    pm = ET.SubElement(met, "per-minute")
                    pm.text = bpm_str
                    ET.SubElement(direction, "sound", {"tempo": bpm_str})

                    children = list(first_measure)
                    insert_idx = 0
                    for idx, ch_el in enumerate(children):
                        if ch_el.tag == "attributes":
                            insert_idx = idx + 1
                            break
                    first_measure.insert(insert_idx, direction)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    body = ET.tostring(root, encoding="unicode")
    dest_path.write_text(f"{header}{body}", encoding="utf-8")
    return dest_path


def _is_valid_mp3(path: Path) -> bool:
    """Return True if ``path`` looks like a non-trivial MP3 (ID3 tag or MPEG frame sync)."""
    try:
        if not path.is_file() or path.stat().st_size <= 256:
            return False
        with path.open("rb") as f:
            hdr = f.read(3)
        if len(hdr) < 2:
            return False
        if hdr[:3] == b"ID3":
            return True
        return hdr[0] == 0xFF and (hdr[1] & 0xE0) == 0xE0
    except OSError:
        return False


def _validate_bitrate(bitrate: int) -> int:
    try:
        br = int(bitrate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid MP3 bitrate: {bitrate!r}") from exc
    if br <= 0:
        raise ValueError(f"MP3 bitrate must be positive, got {br}")
    return br


def export_mp3_grand_piano(
    musicxml: str | Path,
    dest: str | Path | None = None,
    *,
    musescore: str | Path | None = None,
    bitrate: int = 192,
    tempo_bpm: int | None = None,
    timeout_s: int = DEFAULT_MP3_TIMEOUT_S,
    stall_s: float = DEFAULT_MP3_STALL_S,
    role: str = "",
) -> Mp3Result:
    """Render ``musicxml`` as a Grand Piano MP3 using MuseScore."""
    br = _validate_bitrate(bitrate)
    src = Path(musicxml)
    if not src.is_file():
        return Mp3Result(None, f"MP3 not written: {src.name} does not exist.", role=role)

    target = Path(dest) if dest is not None else src.with_name(f"{src.stem}_Piano.mp3")
    target.parent.mkdir(parents=True, exist_ok=True)

    exe = find_musescore(musescore)
    if exe is None:
        return Mp3Result(
            None,
            (
                "MP3 not written: MuseScore was not found (looked for SATB_MUSESCORE, PATH and "
                r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe). Install MuseScore 4 to export MP3s."
            ),
            role=role,
        )

    if timeout_s == DEFAULT_MP3_TIMEOUT_S:
        timeout_s = _env_number("SATB_MP3_TIMEOUT", DEFAULT_MP3_TIMEOUT_S)
    if stall_s == DEFAULT_MP3_STALL_S:
        stall_s = _env_number("SATB_MP3_STALL", DEFAULT_MP3_STALL_S)

    tmp_dir = target.with_name(target.stem + ".mp3-tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_xml = tmp_dir / "piano_input.musicxml"
    tmp_mp3 = tmp_dir / target.name
    tmp_mp3.unlink(missing_ok=True)

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env, stripped = musescore_child_env()

    try:
        try:
            rewrite_musicxml_as_grand_piano(src, tmp_xml, tempo_bpm=tempo_bpm)
        except Exception as exc:  # noqa: BLE001
            return Mp3Result(None, f"MP3 not written: failed to prepare piano MusicXML ({exc}).", role=role)

        cmd = [str(exe), "-b", str(br), "-o", str(tmp_mp3), str(tmp_xml)]
        try:
            returncode, out, err, reason = _run_musescore(cmd, tmp_mp3, env, timeout_s, stall_s, kwargs)
        except OSError as exc:
            return Mp3Result(None, f"MP3 not written: could not start MuseScore ({exe}): {exc}", role=role)

        tail = " ".join(((err or "") + " " + (out or "")).split())[-300:]
        if reason:
            tmp_mp3.unlink(missing_ok=True)
            hint = f" Removed from MuseScore's environment: {', '.join(stripped)}." if stripped else ""
            return Mp3Result(None, f"MP3 not written: {reason}; process killed.{hint} {tail}".strip(), role=role)

        valid = _is_valid_mp3(tmp_mp3)
        if returncode != 0 or not valid:
            tmp_mp3.unlink(missing_ok=True)
            return Mp3Result(
                None,
                (
                    f"MP3 not written: MuseScore exit code {returncode}"
                    f"{'' if valid else ', no valid MP3 produced'}. {tail}"
                ).strip(),
                role=role,
            )

        os.replace(tmp_mp3, target)
        return Mp3Result(
            target,
            f"Grand Piano MP3 written: {target.name} ({br} kbps via {exe.name})",
            role=role,
        )
    finally:
        tmp_xml.unlink(missing_ok=True)
        tmp_mp3.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def export_voices_mp3_grand_piano(
    voice_files: dict[str, str | Path],
    out_dir: str | Path | None = None,
    *,
    musescore: str | Path | None = None,
    bitrate: int = 192,
    tempo_bpm: int | None = None,
    timeout_s: int = DEFAULT_MP3_TIMEOUT_S,
    stall_s: float = DEFAULT_MP3_STALL_S,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Mp3Result]:
    """Export a mapping of ``role -> MusicXML path`` sequentially as Grand Piano MP3s."""
    results: dict[str, Mp3Result] = {}
    items = list(voice_files.items())
    total = len(items)
    out_root = Path(out_dir) if out_dir is not None else None
    if out_root is not None:
        out_root.mkdir(parents=True, exist_ok=True)

    for idx, (role, src_val) in enumerate(items, start=1):
        if should_cancel is not None and should_cancel():
            break
        if progress is not None:
            progress(f"Rendering {role} ({idx}/{total}) as Grand Piano MP3…")
        src_path = Path(src_val)
        dest_path = (
            out_root / f"{src_path.stem}_Piano.mp3"
            if out_root is not None
            else src_path.with_name(f"{src_path.stem}_Piano.mp3")
        )
        results[role] = export_mp3_grand_piano(
            src_path,
            dest_path,
            musescore=musescore,
            bitrate=bitrate,
            tempo_bpm=tempo_bpm,
            timeout_s=timeout_s,
            stall_s=stall_s,
            role=role,
        )
    return results


def discover_voice_musicxml_files(paths: Sequence[Path | str]) -> dict[str, Path]:
    """Discover MusicXML files from files or directories, ordering canonical SATB roles first."""
    candidates: list[Path] = []
    seen_resolved: set[Path] = set()

    def _add(p: Path) -> None:
        if not p.is_file() or p.suffix.lower() not in _MUSICXML_EXTS:
            return
        res = p.resolve()
        if res not in seen_resolved:
            seen_resolved.add(res)
            candidates.append(p)

    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for child in sorted(p.iterdir()):
                _add(child)
        else:
            _add(p)

    canonical_map: dict[str, Path] = {}
    extras: list[Path] = []

    for f in candidates:
        stem_low = f.stem.lower()
        matched_role: str | None = None
        for role in _CANONICAL_ROLES:
            r_low = role.lower()
            if stem_low == r_low or stem_low.endswith(f"_{r_low}"):
                matched_role = role
                break
        if matched_role is not None and matched_role not in canonical_map:
            canonical_map[matched_role] = f
        else:
            extras.append(f)

    ordered: dict[str, Path] = {}
    for role in _CANONICAL_ROLES:
        if role in canonical_map:
            ordered[role] = canonical_map[role]

    for extra in sorted(extras, key=lambda p: p.stem.lower()):
        key = extra.stem
        if key in ordered:
            idx = 2
            while f"{key}_{idx}" in ordered:
                idx += 1
            key = f"{key}_{idx}"
        ordered[key] = extra

    return ordered
