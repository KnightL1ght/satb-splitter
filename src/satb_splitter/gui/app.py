"""Application entry point for the SATB Splitter desktop UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _set_windows_app_id() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SATBSplitter.Studio.Desktop.1"
            )
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    # Disable Windows 11 dark-mode override *before* QApplication exists
    # so our Studio Light / Studio Dark theme engine has full control.
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:darkmode=0")
    _set_windows_app_id()

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    raw_args = list(sys.argv[1:] if argv is None else argv)
    qt_args = [sys.argv[0] if sys.argv else "satb-app", *raw_args]

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(qt_args)
    app.setApplicationName("SATB Splitter")
    app.setOrganizationName("SATBSplitter")
    app.setStyle("Fusion")

    from satb_splitter.gui.icon import create_app_icon
    from satb_splitter.gui.main_window import SUPPORTED_EXTS, MainWindow
    from satb_splitter.gui.styles import apply_app_theme

    app.setWindowIcon(create_app_icon())
    apply_app_theme(app)

    win = MainWindow()

    startup_paths: list[Path] = []
    for arg in raw_args:
        if arg.startswith("-"):
            continue
        p = Path(arg)
        if p.is_dir() or (p.is_file() and p.suffix.lower() in SUPPORTED_EXTS):
            startup_paths.append(p)
    if startup_paths:
        win.add_paths(startup_paths)

    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
