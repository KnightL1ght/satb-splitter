"""White out top band of a single-page PDF to avoid Audiveris metronome NPE."""

from __future__ import annotations

import sys
from pathlib import Path

import fitz


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: whiteout_top.py input.pdf output.pdf [fraction]")
        return 1
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    frac = float(sys.argv[3]) if len(sys.argv) > 3 else 0.12
    doc = fitz.open(src)
    page = doc[0]
    r = page.rect
    band = fitz.Rect(r.x0, r.y0, r.x1, r.y0 + r.height * frac)
    page.draw_rect(band, color=(1, 1, 1), fill=(1, 1, 1))
    doc.save(str(dst))
    doc.close()
    print(f"wrote {dst} whiteout_frac={frac}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
