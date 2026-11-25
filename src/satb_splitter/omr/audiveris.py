"""Audiveris CLI discovery and per-input OMR runs."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_WINDOWS_PATHS = (
    Path.home() / "Audiveris-local-dev3l" / "Audiveris.exe",
    Path.home() / "Audiveris-local-dev3k" / "Audiveris.exe",
    Path.home() / "Audiveris-local-dev3j" / "Audiveris.exe",
    Path(r"C:\Program Files\Audiveris\Audiveris.exe"),
    Path(r"C:\Program Files (x86)\Audiveris\Audiveris.exe"),
)


def _bundled_audiveris_paths() -> tuple[Path, ...]:
    """Candidate paths for an Audiveris distribution bundled alongside the app."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "Audiveris" / "Audiveris.exe",
                exe_dir / "audiveris" / "Audiveris.exe",
            ]
        )
    repo_root = Path(__file__).resolve().parents[3]
    candidates.append(repo_root / "Audiveris" / "Audiveris.exe")
    return tuple(candidates)


@dataclass
class AudiverisRun:
    input_path: Path
    success: bool
    mxl_paths: list[Path] = field(default_factory=list)
    log: str = ""
    returncode: int | None = None
    error: str = ""
    retry_note: str = ""
    #: PDF text-layer markers attached / not attachable for this page.
    marker_log: list[str] = field(default_factory=list)
    #: Export exists but needs review; success does NOT mean clean OMR.
    review_issues: list[str] = field(default_factory=list)
    #: Fresh MXL files written by this attempt even when it was rejected
    #: (Batch 11: used to compare retries against the first read).
    candidate_mxl: list[Path] = field(default_factory=list)
    #: Bars in the exported MXL(s) (max over parts, summed over movements).
    bar_count: int = 0


def count_mxl_bars(paths) -> int:
    """Bars per page export: max measures over parts, summed over movements."""
    from satb_splitter.musicxml_validation import read_xml
    total = 0
    for path in paths or []:
        try:
            root = read_xml(path)
        except Exception:  # noqa: BLE001 - unreadable export counts as 0
            continue
        total += max((len(p.findall("measure")) for p in root.findall("part")), default=0)
    return total


def find_audiveris(explicit: Path | str | None = None) -> Path | None:
    """Locate Audiveris executable."""
    if explicit is not None:
        p = Path(explicit)
        return p if p.is_file() else None

    env = os.environ.get("SATB_AUDIVERIS") or os.environ.get("AUDIVERIS")
    if env:
        p = Path(env)
        if p.is_file():
            return p

    for candidate in _bundled_audiveris_paths():
        if candidate.is_file():
            return candidate

    which = shutil.which("Audiveris") or shutil.which("audiveris")
    if which:
        return Path(which)

    for candidate in DEFAULT_WINDOWS_PATHS:
        if candidate.is_file():
            return candidate
    return None


def run_audiveris_export(
    input_path: Path | str,
    output_dir: Path | str,
    *,
    audiveris: Path | str | None = None,
    timeout_s: int = 600,
) -> AudiverisRun:
    """Transcribe one PDF/image and export MusicXML (.mxl).

    Uses batch mode: ``-batch -transcribe -export -save``.
    """
    src = Path(input_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    exe = find_audiveris(audiveris)
    if exe is None:
        return AudiverisRun(
            input_path=src,
            success=False,
            error=(
                "Audiveris not found. Install it or set SATB_AUDIVERIS to the "
                "Audiveris.exe path."
            ),
        )

    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in out.glob("*.mxl")}
    started = time.time()
    cmd = [
        str(exe),
        "-batch",
        "-transcribe",
        "-export",
        "-save",
        "-output",
        str(out),
        "--",
        str(src),
    ]
    env = os.environ.copy()
    tessdata_dir = exe.parent / "tessdata"
    if not env.get("TESSDATA_PREFIX") and tessdata_dir.is_dir():
        env["TESSDATA_PREFIX"] = str(tessdata_dir)
    run_kwargs: dict = {}
    if sys.platform == "win32":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Audiveris logs are UTF-8; never decode with the Windows ANSI
            # code page (cp1252 raised UnicodeDecodeError on byte 0x81).
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
            env=env,
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        return AudiverisRun(
            input_path=src,
            success=False,
            error=f"Audiveris timed out after {timeout_s}s",
            log=_to_text(exc.stdout) + _to_text(exc.stderr),
        )
    except OSError as exc:
        return AudiverisRun(
            input_path=src,
            success=False,
            error=str(exc),
        )

    log = _to_text(proc.stdout) + "\n" + _to_text(proc.stderr)
    log += _read_log_files(out, since=started)
    from satb_splitter.musicxml_validation import validate_musicxml, export_log_issues
    after = set(out.glob("*.mxl"))
    new_files = sorted(p for p in after
                       if (p not in before or before[p] != (p.stat().st_mtime_ns, p.stat().st_size))
                       and p.stat().st_mtime >= started - 2)
    issues = export_log_issues(log)
    fatal = []
    for path in new_files:
        try:
            report = validate_musicxml(path)
            issues.extend(f"{path.name}: {fl.reason}: {fl.detail}" for fl in report.flags)
            fatal.extend(f"{path.name}: {x}" for x in report.fatal)
        except Exception as exc:
            fatal.append(f"{path.name}: invalid MusicXML: {exc}")
    success = bool(new_files) and proc.returncode == 0 and not fatal and "could not export" not in log.lower()
    error = ""
    if not success:
        error = "; ".join(fatal) or (f"Audiveris exit code {proc.returncode}; no valid fresh export (see log)")
    return AudiverisRun(
        input_path=src,
        success=success,
        mxl_paths=new_files if success else [],
        log=log,
        returncode=proc.returncode,
        error=error,
        review_issues=list(dict.fromkeys(issues)),
        candidate_mxl=list(new_files),
        bar_count=count_mxl_bars(new_files),
    )


def _to_text(data) -> str:
    """Decode subprocess output safely (bytes or str, never cp1252)."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _read_log_files(out_dir: Path, since: float = 0.0) -> str:
    """Append Audiveris *.log files written into the output dir (UTF-8, lenient)."""
    chunks: list[str] = []
    for log_path in sorted(Path(out_dir).glob("*.log")):
        try:
            if log_path.stat().st_mtime < since - 2:
                continue  # log from an earlier run
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                chunks.append(fh.read())
        except OSError:
            continue
    return ("\n" + "\n".join(chunks)) if chunks else ""