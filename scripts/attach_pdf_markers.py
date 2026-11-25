"""Attach PDF text-layer markers to an EXISTING OMR work dir and re-merge.

Usage (from the project root):
    .venv\\Scripts\\python.exe scripts\\attach_pdf_markers.py <work_dir> [--merge-out DIR] [--name STEM]

<work_dir> is a folder written by `satb-omr` / the GUI (it has pages\\ and
omr\\page_NN*\\). The script WRITES `<page>.markers.json` next to each .mxl,
so run it on your own copy / scratch run, not on someone else's artifacts.
New OMR runs do this automatically (Batch 3); this is for re-using old runs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from satb_splitter.omr_cli import make_stdio_safe
    from satb_splitter.omr.markers import write_marker_sidecars

    make_stdio_safe()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("work_dir", type=Path)
    ap.add_argument("--merge-out", type=Path, default=None)
    ap.add_argument("--name", default=None)
    args = ap.parse_args(argv)
    work: Path = args.work_dir
    mxls: list[Path] = []
    for folder in sorted((work / "omr").glob("page_*")):
        page_mxls = sorted(folder.glob("*.mxl"))
        if not page_mxls:
            continue
        m = re.match(r"(page_\d+)", folder.name)
        pdf = work / "pages" / f"{m.group(1)}.pdf" if m else None
        if pdf is not None and pdf.is_file():
            for line in write_marker_sidecars(pdf, page_mxls):
                print(f"  {line}")
        mxls.extend(page_mxls)
    if args.merge_out:
        from satb_splitter.export.merge import split_and_merge_mxl_files

        key = lambda p: (int(re.search(r"page_(\d+)", p.parent.name).group(1)), p.name)
        written, log, _ = split_and_merge_mxl_files(sorted(mxls, key=key), args.merge_out,
                                                    args.name or work.name)
        for line in log:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
