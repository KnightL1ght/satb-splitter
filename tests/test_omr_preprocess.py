"""Tests for PDF preprocess helpers (split + white-out)."""

from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from satb_splitter.omr.preprocess import (  # noqa: E402
    prepare_pages_for_omr,
    split_pdf_pages,
    whiteout_top_band,
)


def _tiny_pdf(path: Path, pages: int = 2) -> Path:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=200, height=300)
        page.insert_text((20, 40), f"Title page {i + 1}")
        page.insert_text((20, 150), "Staff music area")
    doc.save(str(path))
    doc.close()
    return path


def test_split_pdf_pages(tmp_path: Path):
    src = _tiny_pdf(tmp_path / "score.pdf", pages=3)
    pages = split_pdf_pages(src, tmp_path / "pages")
    assert len(pages) == 3
    assert all(p.is_file() for p in pages)
    assert pages[0].name == "page_01.pdf"


def test_whiteout_top_band(tmp_path: Path):
    src = _tiny_pdf(tmp_path / "one.pdf", pages=1)
    out = tmp_path / "clean.pdf"
    whiteout_top_band(src, out, fraction=0.2)
    assert out.is_file()
    assert out.stat().st_size > 0


def test_prepare_pages_all_whiteout(tmp_path: Path):
    src = _tiny_pdf(tmp_path / "score.pdf", pages=2)
    prepared = prepare_pages_for_omr(
        src,
        tmp_path / "work",
        whiteout_fraction=0.12,
        whiteout_mode="all",
    )
    assert len(prepared) == 2
    assert all(p.is_file() for p in prepared)


def test_prepare_pages_no_whiteout(tmp_path: Path):
    src = _tiny_pdf(tmp_path / "score.pdf", pages=2)
    prepared = prepare_pages_for_omr(
        src,
        tmp_path / "work",
        whiteout_fraction=0.12,
        whiteout_mode="none",
    )
    assert len(prepared) == 2
    # without white-out, paths live under pages/
    assert "pages" in str(prepared[0])
