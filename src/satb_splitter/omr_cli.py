"""CLI: satb-omr - per-page Audiveris OMR with top-band white-out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from satb_splitter.omr.image_prep import is_image_path
from satb_splitter.omr.pipeline import DEFAULT_RETRY_WHITEOUTS, run_per_page_omr


def make_stdio_safe() -> None:
    """Never crash on a character the console/pipe encoding cannot show.

    On Windows a redirected stdout is cp1252; printing e.g. an arrow raised
    UnicodeEncodeError *after* OMR, so ``--split`` never ran (R4). Prefer
    UTF-8; otherwise keep the encoding but replace unencodable characters.
    """
    for stream_ in (sys.stdout, sys.stderr):
        reconf = getattr(stream_, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            try:
                reconf(errors="replace")
            except Exception:  # noqa: BLE001
                pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="satb-omr",
        description=(
            "Convert a PDF or sheet-music image(s) to MusicXML using Audiveris **per page** "
            "(more reliable than full-book for large scores). "
            "Optionally white-out the top title/scripture band to avoid "
            "Audiveris metronome crashes on Romanian text."
        ),
    )
    p.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input PDF score or one/more sheet music image files (.png/.jpg/.webp/.tif/.bmp)",
    )
    p.add_argument(
        "-o",
        "--work-dir",
        type=Path,
        default=None,
        help="Working directory for pages + OMR output (default: <input_stem>_omr)",
    )
    p.add_argument(
        "--photo-enhance",
        choices=["auto", "on", "off"],
        default="auto",
        help="Camera photo shadow-flattening & contrast stretch (default: auto)",
    )
    p.add_argument(
        "--no-deskew",
        action="store_true",
        help="Disable automatic skew estimation & straightening on image inputs",
    )
    p.add_argument(
        "--whiteout",
        type=float,
        default=0.12,
        metavar="FRAC",
        help=(
            "Fraction of page height to white-out at the top "
            "(default: 0.12). Use 0 to disable."
        ),
    )
    p.add_argument(
        "--whiteout-mode",
        choices=["all", "none", "first"],
        default="all",
        help="Which pages get the white-out band (default: all)",
    )
    p.add_argument(
        "--audiveris",
        type=Path,
        default=None,
        help="Path to Audiveris.exe (or set SATB_AUDIVERIS)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Per-page Audiveris timeout in seconds (default: 600)",
    )
    p.add_argument(
        "--pages",
        type=str,
        default=None,
        help="Comma-separated 1-based page numbers (e.g. 1,2,5-7)",
    )
    p.add_argument(
        "--split",
        action="store_true",
        help=(
            "After OMR, split every page (auto layout) and merge into ONE set of "
            "<pdf_stem>_Soprano/Alto/Tenor/Bass/SATB.musicxml files (same as the GUI)"
        ),
    )
    p.add_argument(
        "--per-page-split",
        action="store_true",
        help="With --split: also write per-page voice files (old behaviour)",
    )
    p.add_argument(
        "--retry-whiteout",
        type=str,
        default=",".join(str(f) for f in DEFAULT_RETRY_WHITEOUTS),
        metavar="F1,F2",
        help=(
            "Larger white-out fractions to retry a failed page with "
            "(default: %(default)s). Use 'none' to disable retries."
        ),
    )
    p.add_argument(
        "--split-dir",
        type=Path,
        default=None,
        help="Directory for SATB voice files when using --split (default: <work>/voices)",
    )
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def _parse_pages(spec: str) -> list[int]:
    """Parse '1,2,5-7' into [1,2,5,6,7]."""
    out: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(chunk))
    return sorted(set(out))


def _parse_fracs(spec: str) -> tuple[float, ...]:
    if not spec or spec.strip().lower() in ("none", "off", "0"):
        return ()
    return tuple(float(x) for x in spec.split(",") if x.strip())


def main(argv: list[str] | None = None) -> int:
    make_stdio_safe()
    args = build_parser().parse_args(argv)
    inputs: list[Path] = list(args.inputs)
    for p in inputs:
        if not p.is_file():
            print(f"error: input file not found: {p}", file=sys.stderr)
            return 1

    img_flags = [is_image_path(p) for p in inputs]
    if any(img_flags) and not all(img_flags):
        print(
            "error: cannot mix PDF and image files in a single satb-omr invocation.",
            file=sys.stderr,
        )
        return 1
    if not all(img_flags) and len(inputs) > 1:
        print(
            "error: pass a single PDF or one/more image files.",
            file=sys.stderr,
        )
        return 1

    primary = inputs[0]
    from_images = all(img_flags)
    omr_source: Path | list[Path] = inputs if from_images else primary
    work = args.work_dir or Path(f"{primary.stem}_omr")
    page_list = _parse_pages(args.pages) if args.pages else None
    whiteout_mode = args.whiteout_mode
    if args.whiteout <= 0:
        whiteout_mode = "none"

    if not args.quiet:
        if from_images:
            print(f"Images:    {len(inputs)} file(s) ({', '.join(p.name for p in inputs)})")
            print(f"Photo prep: enhance={args.photo_enhance} deskew={not args.no_deskew}")
        else:
            print(f"PDF:       {primary}")
        print(f"Work dir:  {work.resolve()}")
        print(f"White-out: fraction={args.whiteout} mode={whiteout_mode}")
        print("Mode:      per-page OMR (not full-book)")
        print("Running Audiveris...")

    result = run_per_page_omr(
        omr_source,
        work,
        whiteout_fraction=max(0.0, args.whiteout),
        whiteout_mode=whiteout_mode,
        audiveris=args.audiveris,
        timeout_s=args.timeout,
        pages=page_list,
        retry_whiteouts=_parse_fracs(args.retry_whiteout),
        photo_enhance=args.photo_enhance,
        deskew=not args.no_deskew,
    )

    if result.audiveris_path is None and result.fail_count:
        print(result.page_runs[0].error, file=sys.stderr)
        return 3

    if not args.quiet:
        print(
            f"\nPages: {len(result.page_inputs)}  "
            f"OK: {result.success_count - getattr(result, 'review_count', 0)}  REVIEW: {getattr(result, 'review_count', 0)}  FAIL: {result.fail_count}"
        )
        for run in result.page_runs:
            status = ("REVIEW" if getattr(run, "review_issues", []) else "OK") if run.success else "FAIL"
            mxl = ", ".join(p.name for p in run.mxl_paths) or "-"
            print(f"  [{status}] {run.input_path.name} -> {mxl}")
            for issue in getattr(run, "review_issues", []):
                print(f"         WARNING: incomplete OMR: {issue}")
            if not run.success and run.error:
                print(f"         {run.error}")
            if run.retry_note:
                print(f"         {run.retry_note}")
            for ln in getattr(run, "marker_log", []) or []:
                print(f"         {ln}")
        failed = [r.input_path.name for r in result.page_runs if not r.success]
        if failed:
            print(
                f"\nWARNING: {len(failed)} page(s) failed OMR (after automatic "
                f"white-out retries) and are MISSING from the split output: "
                f"{', '.join(failed)}. Try e.g. --whiteout 0.30 --pages ... and re-merge."
            )
        notes = getattr(result, "whiteout_notes", []) or []
        if notes:
            wo_clamp = [ln for ln in notes if "clamped to" in ln]
            prep_notes = [ln for ln in notes if "clamped to" not in ln]
            if prep_notes:
                print("\nImage preprocessing:")
                for ln in prep_notes:
                    print(f"  {ln}")
            if wo_clamp:
                print(f"\nWhite-out clamped above the first staff on {len(wo_clamp)} page(s) (no music erased):")
                for ln in wo_clamp:
                    print(f"  {ln}")
        print(f"\nMusicXML files ({len(result.mxl_paths)}):")
        for p in result.mxl_paths:
            print(f"  {p}")

    if args.split and result.mxl_paths:
        from satb_splitter.export.merge import split_and_merge_mxl_files
        from satb_splitter.export.writer import write_satb_files
        from satb_splitter.model.config import SplitConfig
        from satb_splitter.split.engine import split_file

        split_dir = args.split_dir or (work / "voices")
        split_dir.mkdir(parents=True, exist_ok=True)
        failed = [r.input_path.name for r in result.page_runs if not r.success]
        if not args.quiet:
            print(f"\nSplitting + merging (layout auto-detect) -> {split_dir}")
        written, log, combined = split_and_merge_mxl_files(
            result.mxl_paths,
            split_dir,
            primary.stem,
            auto_layout=True,
            omr_failures=failed,
        )
        if not args.quiet:
            for line in log:
                print(f"  {line}")
            for role, path in written.items():
                print(f"    {role}: {path.name}")
            flags = [f for f in (combined.flags if combined else []) if f.reason != "layout_auto"]
            if flags:
                from collections import Counter

                counts = Counter(f.reason for f in flags)
                print(f"  Review flags: {dict(counts)}")
        if args.per_page_split:
            page_dir = split_dir / "per_page"
            page_dir.mkdir(parents=True, exist_ok=True)
            for mxl in result.mxl_paths:
                res = split_file(mxl, SplitConfig(), auto_layout=True)
                write_satb_files(res, page_dir, mxl.stem, mscz=False)

    return 0 if result.success_count > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
