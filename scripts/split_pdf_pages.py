"""Split a multi-page PDF into one PDF per page (vector-preserving)."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: split_pdf_pages.py input.pdf out_dir")
        return 1
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(src)
    print(f"pages={doc.page_count}")
    for i in range(doc.page_count):
        one = fitz.open()
        one.insert_pdf(doc, from_page=i, to_page=i)
        path = out / f"page_{i + 1:02d}.pdf"
        one.save(str(path))
        one.close()
        print(f"wrote {path.name}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
