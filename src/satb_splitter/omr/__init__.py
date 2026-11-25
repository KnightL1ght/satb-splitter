"""OMR backends — Audiveris per-page pipeline with title-band white-out."""

from .pipeline import OmrResult, run_per_page_omr

__all__ = ["OmrResult", "run_per_page_omr"]
