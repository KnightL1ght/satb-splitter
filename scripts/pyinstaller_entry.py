"""PyInstaller entrypoint for SATB Splitter Studio (Windows portable bundle)."""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _run_smoke_test(musicxml_arg: str, out_arg: str) -> int:
    """Headless portable verification: Audiveris discovery + SATB split + XML validation."""
    os.environ["SATB_MSCZ"] = "0"

    from satb_splitter.export.writer import VOICE_ORDER, write_satb_files
    from satb_splitter.model.config import SplitConfig
    from satb_splitter.musicxml_validation import validate_musicxml
    from satb_splitter.omr.audiveris import find_audiveris
    from satb_splitter.omr_cli import make_stdio_safe
    from satb_splitter.split.engine import split_file

    make_stdio_safe()

    src = Path(musicxml_arg)
    out_dir = Path(out_arg)
    if not src.is_file():
        print(f"ERROR: input MusicXML not found: {src}", file=sys.stderr)
        return 2

    audiveris_exe = find_audiveris()
    if audiveris_exe is None:
        print("ERROR: Audiveris.exe not found via find_audiveris()", file=sys.stderr)
        return 3
    tessdata_dir = audiveris_exe.parent / "tessdata"
    print(f"Audiveris: {audiveris_exe}")
    print(f"Tessdata:  {tessdata_dir} (exists={tessdata_dir.is_dir()})")

    result = split_file(src, SplitConfig(), auto_layout=True)
    written = write_satb_files(result, out_dir, src.stem, mscz=False)

    expected_keys = (*VOICE_ORDER, "SATB")
    missing = [k for k in expected_keys if k not in written]
    if missing:
        print(f"ERROR: missing expected outputs: {missing}", file=sys.stderr)
        return 4

    for key in expected_keys:
        path = written[key]
        if not path.is_file() or path.stat().st_size == 0:
            print(f"ERROR: output missing or empty for {key}: {path}", file=sys.stderr)
            return 5
        ET.parse(path)
        report = validate_musicxml(path)
        if report.fatal:
            print(f"ERROR: validation fatal for {path.name}: {report.fatal}", file=sys.stderr)
            return 6
        print(f"OK [{key:7s}] {path.name} ({path.stat().st_size} bytes)")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 1 and args[0] == "--smoke-test":
        if len(args) != 3:
            print(
                "Usage: SATB Splitter.exe --smoke-test <musicxml_path> <out_dir>",
                file=sys.stderr,
            )
            return 1
        try:
            return _run_smoke_test(args[1], args[2])
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc(file=sys.stderr)
            return 1

    from satb_splitter.gui.app import main as gui_main

    return gui_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
