"""Batch 11: write ``*_SATB.mscz`` with the installed MuseScore command line.

The splitter itself writes MusicXML (music21). A MuseScore file is produced by
converting the finished ``*_SATB.musicxml`` with MuseScore's own converter
(``MuseScore4.exe -o out.mscz in.musicxml``). When MuseScore is missing, the
conversion fails or times out, the reason is returned so callers put it in the
log and REVIEW.txt - it is never silent. A ``.mscz`` the user saved/edited in
MuseScore is never overwritten (only files this module wrote, recorded in a
small ``.splitter.json`` sidecar, are replaced).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CANDIDATES = (
    r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
    r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
    r"C:\Program Files (x86)\MuseScore 3\bin\MuseScore3.exe",
    "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
    "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
)


# Batch 11b: MuseScore must never inherit headless/offscreen Qt overrides from
# the splitter's own environment (e.g. QT_QPA_PLATFORM=offscreen set for a test
# or by a PySide host). With them MuseScore4.exe shows a modal "no Qt platform
# plugin" box and hangs - so they are removed from the CHILD env only.
_QT_STRIP_EXACT = {
    "QT_PLUGIN_PATH", "QT_DEBUG_PLUGINS", "QT_QUICK_BACKEND", "QT_OPENGL",
    "QML2_IMPORT_PATH", "QML_IMPORT_PATH", "QSG_RHI_BACKEND", "QT_QUICK_CONTROLS_STYLE",
}
_QT_STRIP_PREFIXES = ("QT_QPA_",)  # QT_QPA_PLATFORM, _PLATFORM_PLUGIN_PATH, _PLATFORMTHEME, _FONTDIR, ...
_QT_ERROR_MARKERS = (
    "qt platform plugin", "qt.qpa.plugin", "could not find the qt platform plugin",
    "could not load the qt platform plugin", "no qt platform plugin",
    "this application failed to start because",
)
DEFAULT_TIMEOUT_S = 60
DEFAULT_STALL_S = 20.0
_POLL_S = 0.25


def musescore_child_env(base: dict | None = None) -> tuple[dict, list[str]]:
    """Copy of ``base`` (default ``os.environ``) without headless Qt overrides.

    Returns ``(env, removed_names)``. The parent environment is never modified.
    """
    env = dict(os.environ if base is None else base)
    removed = []
    for key in list(env):
        up = key.upper()
        if up in _QT_STRIP_EXACT or up.startswith(_QT_STRIP_PREFIXES):
            removed.append(key)
            del env[key]
    return env, sorted(removed)


def _env_number(name: str, default: float) -> float:
    try:
        val = float(os.environ.get(name, ""))
        return val if val > 0 else default
    except ValueError:
        return default


def _process_cpu_seconds(proc) -> float | None:
    """Total CPU time of the child (Windows only; None when unknown)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        ok = ctypes.windll.kernel32.GetProcessTimes(int(handle), *[ctypes.byref(t) for t in times])
        if not ok:
            return None
        k, u = times[2], times[3]
        return ((k.dwHighDateTime << 32 | k.dwLowDateTime) + (u.dwHighDateTime << 32 | u.dwLowDateTime)) / 1e7
    except Exception:
        return None


def _dialog_text(pid: int) -> str | None:
    """Text of a visible standard dialog box (#32770) owned by ``pid`` (Windows only)."""
    if sys.platform != "win32" or not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list[str] = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _text(hwnd) -> str:
            buf = ctypes.create_unicode_buffer(1024)
            user32.GetWindowTextW(hwnd, buf, 1024)
            return buf.value

        def _cls(hwnd) -> str:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            return buf.value

        def on_child(hwnd, _lp):
            t = _text(hwnd)
            if t and _cls(hwnd) == "Static":
                found.append(t)
            return True

        def on_top(hwnd, _lp):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd) and _cls(hwnd) == "#32770":
                found.append(_text(hwnd) or "dialog")
                user32.EnumChildWindows(hwnd, proto(on_child), 0)
                return False
            return True

        user32.EnumWindows(proto(on_top), 0)
        return " | ".join(" ".join(f.split()) for f in found) if found else None
    except Exception:
        return None


def _kill_tree(proc) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _run_musescore(cmd, tmp: Path, env: dict, timeout_s: float, stall_s: float, popen_kwargs: dict):
    """Run MuseScore, watching for hangs. Returns ``(returncode, stdout, stderr, abort_reason)``.

    Progress = new stdout/stderr bytes, the output file growing, or the child's
    CPU time increasing. No progress for ``stall_s`` seconds, a visible dialog
    box, Qt-plugin error text, or ``timeout_s`` overall -> the process tree is
    killed and ``abort_reason`` says why.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, **popen_kwargs)
    chunks = {"out": [], "err": []}
    activity = [time.monotonic()]

    def pump(stream, key):
        try:
            for raw in iter(lambda: stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096), b""):
                if not raw:
                    break
                chunks[key].append(raw if isinstance(raw, bytes) else raw.encode("utf-8", "replace"))
                activity[0] = time.monotonic()
        except Exception:
            pass

    readers = []
    for stream, key in ((proc.stdout, "out"), (proc.stderr, "err")):
        if stream is not None:
            th = threading.Thread(target=pump, args=(stream, key), daemon=True)
            th.start()
            readers.append(th)

    def text(key):
        return b"".join(chunks[key]).decode("utf-8", "replace")

    started = time.monotonic()
    last_size = -1
    last_cpu = _process_cpu_seconds(proc)
    watch_stall = last_cpu is not None
    reason = None
    while proc.poll() is None:
        time.sleep(_POLL_S)
        now = time.monotonic()
        try:
            size = tmp.stat().st_size if tmp.exists() else -1
        except OSError:
            size = last_size
        if size != last_size:
            last_size = size
            if size >= 0:
                activity[0] = now
        cpu = _process_cpu_seconds(proc)
        if cpu is not None and last_cpu is not None and cpu - last_cpu > 0.05:
            activity[0] = now
        if cpu is not None:
            last_cpu = cpu
            watch_stall = True
        err_low = text("err").lower() + " " + text("out").lower()
        if any(m in err_low for m in _QT_ERROR_MARKERS):
            reason = "MuseScore reported a Qt platform-plugin error"
        elif (dlg := _dialog_text(proc.pid)):
            reason = f"MuseScore showed a dialog instead of converting: {dlg[:200]}"
        elif now - started > timeout_s:
            reason = f"MuseScore timed out after {timeout_s:g}s"
        elif watch_stall and stall_s > 0 and now - activity[0] > stall_s:
            reason = (f"MuseScore stalled: no output, no .mscz progress and no CPU activity for "
                      f"{stall_s:g}s (probably waiting on a hidden dialog)")
        if reason:
            _kill_tree(proc)
            break
    for th in readers:
        th.join(timeout=2)
    return proc.returncode, text("out"), text("err"), reason


@dataclass
class MsczResult:
    path: Path | None
    message: str

    @property
    def ok(self) -> bool:
        return self.path is not None


def mscz_disabled() -> bool:
    return os.environ.get("SATB_MSCZ", "1").strip().lower() in ("0", "off", "no", "false")


def find_musescore(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    for var in ("SATB_MUSESCORE", "MUSESCORE"):
        val = os.environ.get(var)
        if val and Path(val).is_file():
            return Path(val)
    for name in ("MuseScore4", "MuseScore3", "mscore4portable", "mscore", "musescore"):
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    local = os.environ.get("LOCALAPPDATA")
    extra = [str(Path(local) / "Programs" / "MuseScore 4" / "bin" / "MuseScore4.exe")] if local else []
    for cand in list(DEFAULT_CANDIDATES) + extra:
        if Path(cand).is_file():
            return Path(cand)
    return None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(dest: Path) -> Path:
    return dest.with_name(dest.name + ".splitter.json")


def _ours(dest: Path) -> bool:
    side = _sidecar(dest)
    try:
        return json.loads(side.read_text(encoding="utf-8")).get("sha256") == _sha(dest)
    except (OSError, ValueError):
        return False


def export_mscz(
    musicxml: str | Path,
    dest: str | Path | None = None,
    *,
    musescore: str | Path | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> MsczResult:
    src = Path(musicxml)
    target = Path(dest) if dest else src.with_suffix(".mscz")
    if mscz_disabled():
        return MsczResult(None, "MuseScore .mscz export disabled (SATB_MSCZ=0).")
    if not src.is_file():
        return MsczResult(None, f".mscz not written: {src.name} does not exist.")
    exe = find_musescore(musescore)
    if exe is None:
        return MsczResult(None, (
            ".mscz not written: MuseScore was not found (looked for SATB_MUSESCORE, PATH and "
            r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe). Open "
            f"{src.name} in MuseScore and save it, or install MuseScore 4."))
    note = ""
    if target.exists() and not _ours(target):
        kept = target.name
        base = target.stem
        k = 1
        target = target.with_name(f"{base}_splitter.mscz")
        while target.exists() and not _ours(target):
            k += 1
            target = target.with_name(f"{base}_splitter{k}.mscz")
        note = (f" (existing {kept} was saved outside the splitter and was NOT overwritten; "
                f"new file is {target.name})")
    # Batch 12: MuseScore names the inner .mscx (and container rootfile) after
    # the OUTPUT file, so export into a private temp folder under the FINAL
    # file name, then os.replace it into place. Legacy *.tmp-export.mscz
    # leftovers from older versions are removed.
    target.with_name(target.stem + ".tmp-export.mscz").unlink(missing_ok=True)
    tmp_dir = target.with_name(target.stem + ".splitter-tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / target.name
    tmp.unlink(missing_ok=True)
    try:
        return _export_into(exe, src, target, tmp, timeout_s, note)
    finally:
        tmp.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def _export_into(exe: Path, src: Path, target: Path, tmp: Path, timeout_s, note: str) -> MsczResult:
    cmd = [str(exe), "-o", str(tmp), str(src)]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if timeout_s == DEFAULT_TIMEOUT_S:
        timeout_s = _env_number("SATB_MSCZ_TIMEOUT", DEFAULT_TIMEOUT_S)
    stall_s = _env_number("SATB_MSCZ_STALL", DEFAULT_STALL_S)
    env, stripped = musescore_child_env()
    started = time.time()
    try:
        returncode, out, err, reason = _run_musescore(cmd, tmp, env, timeout_s, stall_s, kwargs)
    except OSError as exc:
        return MsczResult(None, f".mscz not written: could not start MuseScore ({exe}): {exc}")
    tail = " ".join(((err or "") + " " + (out or "")).split())[-300:]
    if reason:
        tmp.unlink(missing_ok=True)
        hint = f" Removed from MuseScore's environment: {', '.join(stripped)}." if stripped else ""
        return MsczResult(None, f".mscz not written: {reason}; process killed.{hint} {tail}".strip())
    valid = False
    if tmp.is_file() and tmp.stat().st_size > 0 and tmp.stat().st_mtime >= started - 2:
        try:
            with zipfile.ZipFile(tmp) as z:
                valid = any(n.endswith(".mscx") for n in z.namelist())
        except zipfile.BadZipFile:
            valid = False
    if returncode != 0 or not valid:
        tmp.unlink(missing_ok=True)
        return MsczResult(None, (f".mscz not written: MuseScore exit code {returncode}"
                                 f"{'' if valid else ', no valid .mscz produced'}. {tail}").strip())
    os.replace(tmp, target)
    _sidecar(target).write_text(json.dumps({"sha256": _sha(target), "source": src.name,
                                            "musescore": str(exe)}, indent=2), encoding="utf-8")
    return MsczResult(target, f"MuseScore file written: {target.name} (via {exe.name}){note}")
