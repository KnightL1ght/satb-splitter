"""Rasterize a PDF to per-page PNG files for Audiveris."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: rasterize_pdf.py input.pdf out_dir [zoom]")
        return 1
    pdf = Path(sys.argv[1])
    out = Path(sys.argv[2])
    zoom = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf)
    print(f"pages={doc.page_count} zoom={zoom}")
    for i in range(doc.page_count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        path = out / f"page_{i + 1:02d}.png"
        pix.save(str(path))
        print(f"wrote {path.name} {pix.width}x{pix.height}")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
