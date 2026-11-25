"""Per-page OMR pipeline (more reliable than full-book for large scores)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from satb_splitter.omr.audiveris import AudiverisRun, find_audiveris, run_audiveris_export
from satb_splitter.omr.preprocess import (
    prepare_pages_for_omr,
    safe_whiteout_fraction,
    whiteout_top_band,
)

#: Larger top white-out bands tried, in order, when a page fails OMR
#: (scripture boxes reach ~23% of page 1 on the Hard test score; Audiveris
#: then crashes with a TEXTS NPE and exports nothing).
DEFAULT_RETRY_WHITEOUTS: tuple[float, ...] = (0.22, 0.30)


@dataclass
class OmrResult:
    """Outcome of a full PDF -> MusicXML per-page OMR run."""

    pdf_path: Path
    work_dir: Path
    page_inputs: list[Path] = field(default_factory=list)
    page_runs: list[AudiverisRun] = field(default_factory=list)
    mxl_paths: list[Path] = field(default_factory=list)
    whiteout_fraction: float = 0.12
    whiteout_mode: str = "all"
    audiveris_path: Path | None = None
    #: Pages whose white-out band was clamped above the first staff.
    whiteout_notes: list[str] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.page_runs if r.success)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.page_runs if not r.success)

    @property
    def review_count(self) -> int:
        return sum(1 for r in self.page_runs if r.success and getattr(r, "review_issues", []))

    @property
    def ok(self) -> bool:
        return self.success_count > 0 and self.fail_count == 0 and self.review_count == 0


def run_per_page_omr(
    pdf_path: Path | str,
    work_dir: Path | str,
    *,
    whiteout_fraction: float = 0.12,
    whiteout_mode: str = "all",
    audiveris: Path | str | None = None,
    timeout_s: int = 600,
    pages: list[int] | None = None,
    progress: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    retry_whiteouts: tuple[float, ...] | list[float] | None = DEFAULT_RETRY_WHITEOUTS,
) -> OmrResult:
    """Run Audiveris **per page** (not full-book).

    Full-book transcription of large Romanian scores often fails when one
    sheet crashes (e.g. TEXTS/MetronomeInter) and then blocks MusicXML export
    for the entire book. Processing one page at a time isolates failures.

    Parameters
    ----------
    whiteout_fraction:
        Fraction of page height to white-out at the top (title / scripture).
        Default ``0.12`` avoids Audiveris metronome crashes. Use ``0`` to disable.
    whiteout_mode:
        ``all`` | ``none`` | ``first`` — which pages get the white-out band.
    pages:
        Optional 1-based page numbers to process; default = all pages.
    retry_whiteouts:
        When a page fails, it is re-run from the original page PDF with each
        larger white-out fraction in turn (fresh output folder per attempt,
        e.g. ``omr/page_01_wo22``) until one exports MusicXML. ``None`` or
        ``()`` disables retries. Pages that still fail stay in ``page_runs``
        with ``success=False`` so callers can warn about missing bars.
    """
    pdf = Path(pdf_path)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    omr_out = work / "omr"
    omr_out.mkdir(parents=True, exist_ok=True)

    exe = find_audiveris(audiveris)
    wo_notes: list[str] = []
    prepared = prepare_pages_for_omr(
        pdf,
        work,
        whiteout_fraction=whiteout_fraction,
        whiteout_mode=whiteout_mode if whiteout_fraction > 0 else "none",
        notes=wo_notes,
    )

    if pages is not None:
        want = set(pages)
        prepared = [p for i, p in enumerate(prepared, start=1) if i in want]

    result = OmrResult(
        pdf_path=pdf,
        work_dir=work,
        page_inputs=list(prepared),
        whiteout_fraction=whiteout_fraction,
        whiteout_mode=whiteout_mode,
        audiveris_path=exe,
        whiteout_notes=wo_notes,
    )

    if exe is None:
        result.page_runs.append(
            AudiverisRun(
                input_path=pdf,
                success=False,
                error=(
                    "Audiveris not found. Install Audiveris or set SATB_AUDIVERIS."
                ),
            )
        )
        return result

    total = len(prepared)
    for i, page_pdf in enumerate(prepared, start=1):
        if should_cancel and should_cancel():
            result.page_runs.append(
                AudiverisRun(
                    input_path=page_pdf,
                    success=False,
                    error="Cancelled",
                )
            )
            break
        if progress:
            progress(f"OMR page {i}/{total}: {page_pdf.name}")
        page_out = omr_out / page_pdf.stem
        page_out.mkdir(parents=True, exist_ok=True)
        run = run_audiveris_export(
            page_pdf,
            page_out,
            audiveris=exe,
            timeout_s=timeout_s,
        )
        if not run.success and retry_whiteouts:
            run = _retry_with_larger_whiteout(
                run,
                page_pdf,
                work,
                omr_out,
                exe=exe,
                timeout_s=timeout_s,
                current=whiteout_fraction if whiteout_mode != "none" else 0.0,
                fractions=retry_whiteouts,
                progress=progress,
                should_cancel=should_cancel,
            )
        if run.success and run.mxl_paths:
            _attach_pdf_markers(work, page_pdf, run, progress)
        if getattr(run, "review_issues", []) and progress:
            progress(f"  REVIEW {page_pdf.name}: incomplete OMR export")
            for issue in getattr(run, "review_issues", []):
                progress(f"    WARNING: {issue}")
        result.page_runs.append(run)
        result.mxl_paths.extend(run.mxl_paths)

    return result


def _attach_pdf_markers(work: Path, page_pdf: Path, run: AudiverisRun, progress) -> None:
    """Write ``.markers.json`` sidecars from the ORIGINAL page's text layer."""
    try:
        from satb_splitter.omr.markers import write_marker_sidecars
        from satb_splitter.omr.header_tempo import write_tempo_sidecars

        original = work / "pages" / page_pdf.name
        source = original if original.is_file() else page_pdf
        lines = write_tempo_sidecars(source, run.mxl_paths)
        lines += write_marker_sidecars(source, run.mxl_paths)
    except Exception as exc:  # noqa: BLE001 - markers are best effort
        lines = [f"{page_pdf.name}: PDF marker scan skipped ({exc})"]
    try:  # Batch 12: printed multi-bar rest counts (compared, never invented)
        from satb_splitter.omr.multirest import write_multirest_sidecars

        original = work / "pages" / page_pdf.name
        lines += write_multirest_sidecars(original if original.is_file() else page_pdf, run.mxl_paths)
    except Exception as exc:  # noqa: BLE001 - best effort
        lines.append(f"{page_pdf.name}: PDF multirest scan skipped ({exc})")
    if lines:
        run.marker_log = list(lines)
        if progress:
            for ln in lines:
                progress(f"  {ln}")


def _plan_retries(
    original: Path, current: float, fractions
) -> list[tuple[float, bool, str]]:
    """Ordered (effective_fraction, protect_staves, note) retry attempts.

    1. each requested fraction, clamped above the first staff;
    2. a *tight* band ending just above the first staff (erases all header
       text incl. markers above the staff, keeps every staff line);
    3. last resort: the unclamped requested fractions (old behaviour) - these
       can erase the first staff, so their note carries a WARNING.
    Attempts not larger than the previous band (+0.01) are skipped.
    """
    cur_eff, _ = safe_whiteout_fraction(original, current) if current > 0 else (0.0, "")
    plan: list[tuple[float, bool, str]] = []
    tried = [cur_eff]

    def add(eff: float, protect: bool, note: str) -> None:
        if eff <= max(tried) + 0.01:
            return
        tried.append(eff)
        plan.append((eff, protect, note))

    for frac in fractions:
        eff, note = safe_whiteout_fraction(original, frac)
        add(eff, True, note)
    biggest = max([current, *fractions] or [0.0])
    if biggest > 0:
        eff, note = safe_whiteout_fraction(original, max(biggest, 0.5), staff_margin=0.25)
        if note:
            add(eff, True, "tight band just above the first staff (" + note + ")")
        for frac in fractions:
            add(frac, False, (
                f"WARNING: unclamped white-out {frac:.2f} may have erased (part of) the first "
                "staff - check the first system's S/A bars"
            ))
    return plan


def _retry_with_larger_whiteout(
    failed: AudiverisRun,
    page_pdf: Path,
    work: Path,
    omr_out: Path,
    *,
    exe: Path,
    timeout_s: int,
    current: float,
    fractions,
    progress: Callable[[str], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> AudiverisRun:
    """Re-run one failed page with larger top white-out bands (BUG-4).

    Bands are clamped above the first staff first (Batch 3: an unclamped
    0.30 band erased p1's S,A staff); see :func:`_plan_retries`.
    """
    original = work / "pages" / page_pdf.name
    if not original.is_file():
        original = page_pdf
    attempts: list[str] = [f"white-out {current:.2f}: {failed.error or 'failed'}"]
    # Batch 11: never let a retry silently lose bars. The first read may have
    # exported MXL that was rejected; a larger white-out can erase system 1
    # (Hard p5/p6/p15 lost 7-12 bars that way). Keep the successful attempt
    # with the most bars and stop early only when it is not shorter than the
    # first read.
    first_bars = int(getattr(failed, "bar_count", 0) or 0)
    best: AudiverisRun | None = None
    best_note = ""
    for frac, protect, note in _plan_retries(original, current, fractions):
        if should_cancel and should_cancel():
            break
        if not protect:
            tag = f"wo{int(round(frac * 100)):02d}u"
        elif abs(frac * 100 - round(frac * 100)) < 1e-6:
            tag = f"wo{int(round(frac * 100)):02d}"
        else:
            tag = f"wo{int(round(frac * 1000)):03d}"
        if progress:
            progress(f"  retry {page_pdf.name} with white-out {frac:.3f}" + (f" ({note})" if note else ""))
        dst = work / f"pages_{tag}" / page_pdf.name
        if protect:
            retry_pdf = whiteout_top_band(original, dst, fraction=frac)
        else:
            retry_pdf = whiteout_top_band(original, dst, fraction=frac, protect_staves=False)
        # Fresh output folder per attempt (re-using one raised Audiveris
        # FileSystemAlreadyExistsException on the .omr book).
        out = omr_out / f"{page_pdf.stem}_{tag}"
        out.mkdir(parents=True, exist_ok=True)
        run = run_audiveris_export(retry_pdf, out, audiveris=exe, timeout_s=timeout_s)
        if run.success:
            run.input_path = page_pdf
            bars = int(getattr(run, "bar_count", 0) or 0)
            if best is None or bars > int(getattr(best, "bar_count", 0) or 0):
                best = run
                best_note = (
                    f"recovered with white-out {frac:.3f}"
                    + (f" [{note}]" if note else "")
                    + f" ({bars} bars) after: " + "; ".join(attempts)
                )
            if bars >= first_bars:
                break
            attempts.append(
                f"white-out {frac:.3f}: exported only {bars} bars (< {first_bars} in the first read); "
                "trying the next band, best attempt kept")
            continue
        attempts.append(f"white-out {frac:.3f}: {run.error or 'failed'}")
    if best is not None:
        best.retry_note = best_note
        bars = int(getattr(best, "bar_count", 0) or 0)
        if bars < first_bars:
            best.review_issues = list(getattr(best, "review_issues", []) or []) + [
                f"retry kept the best attempt with {bars} bars, but the first read exported "
                f"{first_bars} bars and was rejected ({failed.error or 'failed'}); "
                f"{first_bars - bars} bar(s) may be missing - check the first system against the PDF"
            ]
        return best
    failed.retry_note = "all retries failed: " + "; ".join(attempts)
    if len(attempts) > 1:
        failed.error = (failed.error or "OMR failed") + f" (retried {len(attempts) - 1}x with larger white-out)"
    return failed
