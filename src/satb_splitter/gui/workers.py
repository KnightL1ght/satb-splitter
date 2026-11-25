"""Background workers so the UI stays responsive during OMR/split."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, Signal, Slot


class CancelledError(Exception):
    """User cancelled the pipeline."""


class PipelineWorker(QObject):
    """Run OMR (optional) + split for one input file."""

    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        input_path: Path,
        work_dir: Path,
        *,
        is_pdf: bool,
        whiteout: float,
        whiteout_mode: str,
        template: str,
        unison: str,
        audiveris: str | None,
        divisi_mode: str = "monophonic_auto",
        divisi_overrides: dict[str, str] | None = None,
        mxl_paths: list[Path] | None = None,
        is_image: bool = False,
        image_paths: list[Path] | None = None,
        photo_enhance: str = "auto",
        deskew: bool = True,
        cancel_event: Event | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.input_path = Path(input_path)
        self.work_dir = Path(work_dir)
        self.is_pdf = is_pdf
        self.is_image = is_image
        self.image_paths = [Path(p) for p in image_paths] if image_paths is not None else None
        self.photo_enhance = photo_enhance
        self.deskew = deskew
        self.whiteout = whiteout
        self.whiteout_mode = whiteout_mode
        self.template = template
        self.unison = unison
        self.audiveris = audiveris
        self.divisi_mode = divisi_mode
        self.divisi_overrides = dict(divisi_overrides or {})
        self.mxl_paths = [Path(p) for p in mxl_paths] if mxl_paths is not None else None
        self.cancel_event = cancel_event or Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise CancelledError("Cancelled")

    @Slot()
    def run(self) -> None:
        try:
            result = self._run()
            self.finished.emit(result)
        except CancelledError:
            self.failed.emit("Cancelled")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))

    def _run(self) -> dict:
        from satb_splitter.model.config import (
            DivisiMode,
            LayoutTemplate,
            SplitConfig,
            UnisonMode,
        )
        from satb_splitter.export.merge import split_and_merge_mxl_files
        from satb_splitter.split.engine import split_file
        from satb_splitter.export.writer import write_satb_files

        self.work_dir.mkdir(parents=True, exist_ok=True)
        voices_dir = self.work_dir / "voices"
        basename = self.input_path.stem
        logs: list[str] = []
        mxl_paths: list[Path] = []
        combined_result = None
        omr_failures: list[str] = []

        is_omr = self.is_pdf or self.is_image or bool(self.image_paths)

        if self.mxl_paths is not None:
            mxl_paths = list(self.mxl_paths)
            self.progress.emit(
                f"Re-splitting {len(mxl_paths)} cached MusicXML file(s) with updated chord choices…"
            )
        elif is_omr:
            self.progress.emit("Starting per-page OMR (Audiveris)…")
            from satb_splitter.omr.pipeline import run_per_page_omr

            omr_src = list(self.image_paths) if self.image_paths else self.input_path
            omr = run_per_page_omr(
                omr_src,
                self.work_dir / "omr_work",
                whiteout_fraction=self.whiteout,
                whiteout_mode=self.whiteout_mode if self.whiteout > 0 else "none",
                audiveris=self.audiveris,
                progress=self.progress.emit,
                should_cancel=self.cancel_event.is_set,
                photo_enhance=self.photo_enhance,
                deskew=self.deskew,
            )
            self._check_cancel()
            for run in omr.page_runs:
                status = ("REVIEW" if getattr(run, "review_issues", []) else "OK") if run.success else "FAIL"
                names = ", ".join(p.name for p in run.mxl_paths) or "—"
                msg = f"  [{status}] {run.input_path.name} → {names}"
                logs.append(msg)
                self.progress.emit(msg)
                for issue in getattr(run, "review_issues", []):
                    warning = f"         WARNING: incomplete OMR: {issue}"
                    logs.append(warning)
                    self.progress.emit(warning)
                if not run.success and run.error:
                    self.progress.emit(f"         {run.error}")
            omr_failures = [r.input_path.name for r in omr.page_runs if not r.success]
            if omr_failures:
                warn = (
                    f"WARNING: {len(omr_failures)} page(s) failed OMR and are NOT in the "
                    f"merged voices: {', '.join(omr_failures)}"
                )
                logs.append(warn)
                self.progress.emit(warn)
            mxl_paths = list(omr.mxl_paths)
            if not mxl_paths:
                raise RuntimeError(
                    "OMR produced no MusicXML. Is Audiveris installed?\n"
                    "Set the Audiveris path, or add a MusicXML file instead."
                )
            self.progress.emit(
                f"OMR done: {len(mxl_paths)} MusicXML file(s). Splitting voices…"
            )
        else:
            mxl_paths = [self.input_path]
            self.progress.emit(f"Loading MusicXML: {self.input_path.name}")

        self._check_cancel()
        auto = self.template == "auto"
        eff_divisi_mode = "monophonic_auto" if self.divisi_mode == "ask" else self.divisi_mode
        if auto:
            cfg = SplitConfig(
                unison_mode=UnisonMode(self.unison),
                divisi_mode=DivisiMode(eff_divisi_mode),
                divisi_overrides=dict(self.divisi_overrides),
            )
        else:
            cfg = SplitConfig(
                template=LayoutTemplate.by_name(self.template),
                unison_mode=UnisonMode(self.unison),
                divisi_mode=DivisiMode(eff_divisi_mode),
                divisi_overrides=dict(self.divisi_overrides),
            )

        if len(mxl_paths) == 1 and not is_omr and not cfg.divisi_overrides:
            result = split_file(mxl_paths[0], cfg, auto_layout=auto)
            combined_result = result
            written = write_satb_files(result, voices_dir, basename, combined=True)
            for fl in result.flags:
                if fl.reason == "layout_auto":
                    logs.append(f"Layout: {fl.detail}")
                    self.progress.emit(f"Layout: {fl.detail}")
            flags = [
                f"m{fl.measure} part{fl.part_index}: {fl.reason} — {fl.detail}"
                for fl in result.flags
                if fl.reason != "layout_auto"
            ]
        else:
            written, merge_log, combined_result = split_and_merge_mxl_files(
                mxl_paths,
                voices_dir,
                basename,
                auto_layout=auto,
                config=cfg,
                omr_failures=omr_failures,
            )
            for line in merge_log:
                logs.append(line)
                self.progress.emit(line)
            flags = [
                f"m{fl.measure} part{fl.part_index}: {fl.reason} — {fl.detail}"
                for fl in (combined_result.flags if combined_result else [])
                if fl.reason != "layout_auto"
            ]

        self._check_cancel()
        satb_path = written.pop("SATB", None)
        voice_paths = {role: str(path) for role, path in written.items() if role != "SATB"}
        # pop already removed SATB; keep remaining
        if satb_path is None and combined_result is not None:
            from satb_splitter.export.writer import write_combined_satb

            satb_path = write_combined_satb(
                combined_result, voices_dir / f"{basename}_SATB.musicxml"
            )

        mscz_path = getattr(combined_result, "mscz_path", None) if combined_result is not None else None
        mscz_msg = getattr(combined_result, "mscz_message", "") if combined_result is not None else ""
        if mscz_msg:
            line = ("" if mscz_path else "WARNING: ") + mscz_msg
            if len(mxl_paths) == 1 and not is_omr:
                logs.append(line)
                self.progress.emit(line)
        self.progress.emit("Done.")
        return {
            "mscz": str(mscz_path) if mscz_path else None,
            "mscz_message": mscz_msg,
            "input": str(self.input_path),
            "work_dir": str(self.work_dir),
            "voices_dir": str(voices_dir),
            "voices": voice_paths,
            "satb": str(satb_path) if satb_path else None,
            "mxl": [str(p) for p in mxl_paths],
            "logs": logs,
            "flags": flags,
            "divisi_choices": list(getattr(combined_result, "divisi_choices", None) or []),
        }
