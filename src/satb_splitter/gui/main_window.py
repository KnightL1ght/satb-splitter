"""Main application window — Studio Edition (Light & Dark themes)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from threading import Event

from PySide6.QtCore import QSettings, QSize, Qt, QThread, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from satb_splitter.gui.divisi_dialog import DivisiResolverDialog
from satb_splitter.gui.icon import create_app_icon, render_icon_pixmap
from satb_splitter.gui.styles import apply_app_theme, apply_card_shadow, get_theme_colors
from satb_splitter.gui.workers import PipelineWorker
from satb_splitter.omr.audiveris import find_audiveris
from satb_splitter.omr.image_prep import IMAGE_EXTS, is_image_path


APP_NAME = "SATB Splitter"
ORG = "SATBSplitter"
SUPPORTED_EXTS = {".pdf", ".musicxml", ".xml", ".mxl"} | set(IMAGE_EXTS)

VOICE_COLORS = {
    "SATB": "#2563eb",
    "Soprano": "#7c3aed",
    "Alto": "#db2777",
    "Tenor": "#0d9488",
    "Bass": "#1e40af",
    "Review": "#d97706",
}


class DropList(QListWidget):
    """Drag-and-drop enabled score queue list."""

    def __init__(self, on_paths, parent=None):
        super().__init__(parent)
        self._on_paths = on_paths
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSpacing(4)
        self.setUniformItemSizes(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self._on_paths(paths)
        event.acceptProposedAction()


class DropZoneFrame(QFrame):
    """Illustrated empty drop-zone frame that also accepts file/folder drops."""

    def __init__(self, on_paths, on_browse_files, on_browse_folder, parent=None):
        super().__init__(parent)
        self.setObjectName("dropZone")
        self._on_paths = on_paths
        self.setAcceptDrops(True)
        self.setMinimumHeight(125)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(render_icon_pixmap(44))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon_lbl)

        title = QLabel("Drop Choral PDF, Photos, or MusicXML Scores Here")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-weight: 800; font-size: 14px; background: transparent;")
        lay.addWidget(title)

        sub = QLabel(
            "PDFs & photos (.png/.jpg/.webp/.tif) run per-page OMR  ·  MusicXML (.musicxml / .mxl / .xml) splits directly"
        )
        sub.setObjectName("muted")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        btn_files = QPushButton("＋ Browse Files…")
        btn_files.setObjectName("subtle")
        btn_files.clicked.connect(on_browse_files)
        btn_folder = QPushButton("Browse Folder…")
        btn_folder.setObjectName("subtle")
        btn_folder.clicked.connect(on_browse_folder)
        btn_row.addWidget(btn_files)
        btn_row.addWidget(btn_folder)
        btn_row.addStretch()
        lay.addLayout(btn_row)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.toLocalFile()]
        if paths:
            self._on_paths(paths)
        event.acceptProposedAction()


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


def _kpi_card(title: str, initial_value: str, subtitle: str, accent: str) -> tuple[QFrame, QLabel, QLabel]:
    card = QFrame()
    card.setObjectName("kpiCard")
    outer = QHBoxLayout(card)
    outer.setContentsMargins(12, 10, 12, 10)
    outer.setSpacing(10)

    bar = QFrame()
    bar.setFixedWidth(4)
    bar.setStyleSheet(f"background: {accent}; border-radius: 2px;")
    outer.addWidget(bar)

    col = QVBoxLayout()
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(1)
    t_lbl = QLabel(title)
    t_lbl.setObjectName("kpiTitle")
    v_lbl = QLabel(initial_value)
    v_lbl.setObjectName("kpiValue")
    s_lbl = QLabel(subtitle)
    s_lbl.setObjectName("kpiSub")
    col.addWidget(t_lbl)
    col.addWidget(v_lbl)
    col.addWidget(s_lbl)
    outer.addLayout(col, stretch=1)
    return card, v_lbl, s_lbl


def _row_widget(
    title: str,
    subtitle: str,
    accent: str,
    *,
    badge: str = "",
    status: str = "",
) -> QWidget:
    """Studio two-line card row with left accent bar and optional format/voice pill."""
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    outer = QHBoxLayout(w)
    outer.setContentsMargins(8, 7, 10, 7)
    outer.setSpacing(10)

    bar = QFrame()
    bar.setFixedWidth(4)
    bar.setStyleSheet(f"background: {accent}; border-radius: 2px;")
    outer.addWidget(bar)

    body = QVBoxLayout()
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(3)

    top_row = QHBoxLayout()
    top_row.setContentsMargins(0, 0, 0, 0)
    top_row.setSpacing(8)

    if badge:
        b_lbl = QLabel(badge)
        b_lbl.setStyleSheet(
            f"background: {accent}; color: #ffffff; font-weight: 800; font-size: 10px; "
            "padding: 2px 7px; border-radius: 6px;"
        )
        top_row.addWidget(b_lbl)

    t = QLabel(title)
    t.setStyleSheet(
        f"color: {accent}; font-weight: 700; font-size: 13px; background: transparent;"
    )
    t.setWordWrap(False)
    top_row.addWidget(t, stretch=1)

    if status:
        st_lbl = QLabel(status)
        st_lbl.setObjectName("muted")
        st_lbl.setStyleSheet("font-size: 11px; font-weight: 600; background: transparent;")
        top_row.addWidget(st_lbl)

    body.addLayout(top_row)

    s = QLabel(subtitle)
    s.setObjectName("muted")
    s.setStyleSheet("font-size: 11px; background: transparent;")
    s.setWordWrap(True)
    body.addWidget(s)

    outer.addLayout(body, stretch=1)
    w.setMinimumHeight(52)
    w.adjustSize()
    return w


def _add_row(
    listw: QListWidget,
    title: str,
    subtitle: str,
    data: str,
    accent: str = "#2563eb",
    *,
    badge: str = "",
    status: str = "",
    insert_at: int | None = None,
) -> QListWidgetItem:
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, data)
    item.setToolTip(data)
    w = _row_widget(title, subtitle, accent, badge=badge, status=status)
    width = max(listw.viewport().width() - 24, 240)
    item.setSizeHint(QSize(width, max(w.sizeHint().height(), 56)))
    if insert_at is None:
        listw.addItem(item)
    else:
        listw.insertItem(insert_at, item)
    listw.setItemWidget(item, w)
    return item


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(create_app_icon())
        self.resize(1200, 860)
        self.setMinimumSize(860, 620)
        self.settings = QSettings(ORG, APP_NAME)
        self._thread: QThread | None = None
        self._worker: PipelineWorker | None = None
        self._cancel = Event()
        self._last_result: dict | None = None
        self._queue: list[Path | list[Path]] = []
        self._queue_results: list[dict] = []
        self._reexporting_divisi = False
        self._last_failed = False
        self._all_flags_text: str = ""
        self._cards: list[QWidget] = []

        saved_theme = str(self.settings.value("theme", "light") or "light").lower().strip()
        self._theme: str = "dark" if saved_theme == "dark" else "light"

        app = QApplication.instance()
        if app is not None:
            apply_app_theme(app, self._theme)

        self._build_menu()
        self._build_ui()
        self._load_settings()
        self._apply_theme_to_widgets()
        self._refresh_audiveris_status()
        self._update_file_count()
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Add a PDF, sheet photo, or MusicXML file to begin.")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        act_add = QAction("Add files…", self)
        act_add.setShortcut("Ctrl+O")
        act_add.triggered.connect(self.browse_files)
        file_menu.addAction(act_add)

        act_folder = QAction("Add folder…", self)
        act_folder.setShortcut("Ctrl+Shift+O")
        act_folder.triggered.connect(self.browse_input_folder)
        file_menu.addAction(act_folder)

        act_out = QAction("Choose output folder…", self)
        act_out.triggered.connect(self.browse_output)
        file_menu.addAction(act_out)
        file_menu.addSeparator()
        act_quit = QAction("Exit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = self.menuBar().addMenu("&View")
        act_theme = QAction("Toggle Light / Dark Theme", self)
        act_theme.setShortcut("Ctrl+T")
        act_theme.triggered.connect(self.toggle_theme)
        view_menu.addAction(act_theme)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction("About", self.show_about)

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(14)

        # ---- Hero Header Banner ----
        hero = QFrame()
        hero.setObjectName("hero")
        hero_l = QHBoxLayout(hero)
        hero_l.setContentsMargins(20, 16, 20, 16)
        hero_l.setSpacing(16)

        emblem = QLabel()
        emblem.setPixmap(render_icon_pixmap(52))
        emblem.setFixedSize(56, 56)
        emblem.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_l.addWidget(emblem)

        hero_text = QVBoxLayout()
        hero_text.setSpacing(4)
        t = QLabel(APP_NAME)
        t.setObjectName("heroTitleOnDark")
        s = QLabel("Choral PDF, Photos & MusicXML  →  Soprano · Alto · Tenor · Bass  ·  MuseScore 4")
        s.setObjectName("heroSubOnDark")
        hero_text.addWidget(t)
        hero_text.addWidget(s)

        pills_row = QHBoxLayout()
        pills_row.setSpacing(8)
        self.hero_av_pill = QLabel("● Audiveris")
        self.hero_av_pill.setObjectName("heroPillOk")
        self.hero_ms_pill = QLabel("● MuseScore")
        self.hero_ms_pill.setObjectName("heroPillInfo")
        self.hero_queue_pill = QLabel("Queue: 0 scores")
        self.hero_queue_pill.setObjectName("heroPillInfo")
        pills_row.addWidget(self.hero_av_pill)
        pills_row.addWidget(self.hero_ms_pill)
        pills_row.addWidget(self.hero_queue_pill)
        pills_row.addStretch()
        hero_text.addLayout(pills_row)
        hero_l.addLayout(hero_text, stretch=1)

        run_col = QVBoxLayout()
        run_col.setSpacing(8)
        top_btns = QHBoxLayout()
        top_btns.setSpacing(8)
        top_btns.addStretch()
        self.btn_theme = QPushButton("🌙 Dark")
        self.btn_theme.setObjectName("ghost")
        self.btn_theme.setToolTip("Switch between Studio Light and Studio Dark themes (Ctrl+T)")
        self.btn_theme.clicked.connect(self.toggle_theme)
        top_btns.addWidget(self.btn_theme)

        self.btn_cancel = QPushButton("✕  Cancel")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setMinimumHeight(30)
        self.btn_cancel.clicked.connect(self.cancel_pipeline)
        self.btn_cancel.setVisible(False)
        top_btns.addWidget(self.btn_cancel)
        run_col.addLayout(top_btns)

        self.btn_run = QPushButton("▶  Convert && Split")
        self.btn_run.setObjectName("primary")
        self.btn_run.setMinimumHeight(44)
        self.btn_run.setMinimumWidth(196)
        self.btn_run.setToolTip("Run OMR (for PDFs) and split queued scores into SATB + 4 voice parts")
        self.btn_run.clicked.connect(self.start_pipeline)
        run_col.addWidget(self.btn_run)
        hero_l.addLayout(run_col)
        outer.addWidget(hero)

        # ---- Main Horizontal Splitter ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(12)
        splitter.setChildrenCollapsible(False)

        # ==== LEFT COLUMN ====
        left = QWidget()
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 0, 0)
        left_l.setSpacing(12)

        files_card = _card()
        self._cards.append(files_card)
        fl = QVBoxLayout(files_card)
        fl.setContentsMargins(16, 14, 16, 14)
        fl.setSpacing(10)
        files_head = QHBoxLayout()
        files_head.addWidget(self._section("Input Scores"))
        self.file_count_label = QLabel("0 files")
        self.file_count_label.setObjectName("countPill")
        files_head.addStretch()
        files_head.addWidget(self.file_count_label)
        fl.addLayout(files_head)

        self.file_stack = QStackedWidget()
        self.drop_zone = DropZoneFrame(
            self.add_paths,
            self.browse_files,
            self.browse_input_folder,
        )
        self.file_list = DropList(self.add_paths)
        self.file_list.setMinimumHeight(125)
        self.file_stack.addWidget(self.drop_zone)  # 0
        self.file_stack.addWidget(self.file_list)  # 1
        fl.addWidget(self.file_stack, stretch=1)

        file_btns = QHBoxLayout()
        file_btns.setSpacing(5)
        self.btn_add = QPushButton("＋ Add files…")
        self.btn_add.setStyleSheet("padding: 6px 10px;")
        self.btn_add.setToolTip("Add PDF, sheet photo, or MusicXML files to the queue (Ctrl+O)")
        self.btn_add.clicked.connect(self.browse_files)
        self.btn_add_folder = QPushButton("Add Folder…")
        self.btn_add_folder.setStyleSheet("padding: 6px 10px;")
        self.btn_add_folder.setToolTip("Add all PDF, image, and MusicXML scores from a folder (Ctrl+Shift+O)")
        self.btn_add_folder.clicked.connect(self.browse_input_folder)
        self.btn_move_up = QPushButton("▲")
        self.btn_move_up.setFixedWidth(30)
        self.btn_move_up.setStyleSheet("padding: 6px 0px;")
        self.btn_move_up.setToolTip("Move selected item up (reorder multi-page photo sheets)")
        self.btn_move_up.clicked.connect(self.move_selected_up)
        self.btn_move_down = QPushButton("▼")
        self.btn_move_down.setFixedWidth(30)
        self.btn_move_down.setStyleSheet("padding: 6px 0px;")
        self.btn_move_down.setToolTip("Move selected item down (reorder multi-page photo sheets)")
        self.btn_move_down.clicked.connect(self.move_selected_down)
        self.btn_remove = QPushButton("Remove")
        self.btn_remove.setStyleSheet("padding: 6px 10px;")
        self.btn_remove.setToolTip("Remove selected items from the queue")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.setStyleSheet("padding: 6px 10px;")
        self.btn_clear.setToolTip("Clear the entire input queue")
        self.btn_clear.clicked.connect(self.clear_files)
        file_btns.addWidget(self.btn_add)
        file_btns.addWidget(self.btn_add_folder)
        file_btns.addWidget(self.btn_move_up)
        file_btns.addWidget(self.btn_move_down)
        file_btns.addWidget(self.btn_remove)
        file_btns.addWidget(self.btn_clear)
        file_btns.addStretch()
        fl.addLayout(file_btns)
        left_l.addWidget(files_card, stretch=1)

        opts = _card()
        self._cards.append(opts)
        ol = QVBoxLayout(opts)
        ol.setContentsMargins(16, 12, 16, 12)
        ol.setSpacing(6)
        ol.addWidget(self._section("Pipeline Options"))

        form = QFormLayout()
        form.setVerticalSpacing(6)
        form.setHorizontalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        form.addRow(self._subsection("DESTINATION"))
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Folder for SATB files")
        self.out_edit.setToolTip("Root directory where per-score folders and voice parts will be written")
        out_row = QHBoxLayout()
        out_row.setSpacing(6)
        out_row.addWidget(self.out_edit, stretch=1)
        self.btn_out = QPushButton("Browse…")
        self.btn_out.setToolTip("Select destination folder")
        self.btn_out.clicked.connect(self.browse_output)
        self.btn_open_out_quick = QPushButton("Open")
        self.btn_open_out_quick.setObjectName("subtle")
        self.btn_open_out_quick.setToolTip("Open destination folder in Explorer")
        self.btn_open_out_quick.clicked.connect(self.open_output_folder)
        out_row.addWidget(self.btn_out)
        out_row.addWidget(self.btn_open_out_quick)
        form.addRow("Output", out_row)

        form.addRow(self._subsection("VOICE SPLITTING"))
        self.template_combo = QComboBox()
        self.template_combo.setToolTip("Score staff layout (Auto-detect handles 2-staff, 3-staff, and 4-staff SATB)")
        for label, data in [
            ("Auto-detect layout", "auto"),
            ("2-staff  ·  SA / TB", "two_staff_satb"),
            ("3-staff  ·  SA / T / B", "three_staff_satb"),
            ("4-staff  ·  open score", "four_staff_open"),
            ("1-staff  ·  S + A only", "single_staff_sa"),
        ]:
            self.template_combo.addItem(label, data)
        form.addRow("Layout", self.template_combo)

        self.unison_combo = QComboBox()
        self.unison_combo.setToolTip("How to assign a single shared unison note on a two-voice staff")
        for label, data in [
            ("Shared note → both voices", "duplicate_both"),
            ("Shared note → upper only", "upper_only"),
            ("Shared note → lower only", "lower_only"),
            ("Shared note → flag review", "flag_review"),
        ]:
            self.unison_combo.addItem(label, data)
        form.addRow("Unisons", self.unison_combo)

        self.divisi_combo = QComboBox()
        self.divisi_combo.setToolTip("How to route 3+ simultaneous notes on a 2-voice staff")
        for label, data in [
            ("1 note/voice · Auto (recommended)", "monophonic_auto"),
            ("1 note/voice · Ask for ambiguous chords", "ask"),
            ("Keep divisi chords (no collapse)", "keep_divisi"),
            ("1 note/voice · Bias to upper voice", "upper_only"),
            ("1 note/voice · Bias to lower voice", "lower_only"),
        ]:
            self.divisi_combo.addItem(label, data)
        form.addRow("3-note chords", self.divisi_combo)

        form.addRow(self._subsection("PDF & IMAGE OMR (AUDIVERIS)"))
        self.whiteout_spin = QDoubleSpinBox()
        self.whiteout_spin.setRange(0.0, 40.0)
        self.whiteout_spin.setSingleStep(1.0)
        self.whiteout_spin.setValue(12.0)
        self.whiteout_spin.setSuffix(" % of page")
        self.whiteout_spin.setDecimals(0)
        self.whiteout_spin.setToolTip("Top header margin masked before OMR to strip title/composer graphics")

        self.whiteout_mode = QComboBox()
        self.whiteout_mode.setToolTip("Which PDF/image pages receive top header white-out before OMR")
        self.whiteout_mode.addItem("All pages", "all")
        self.whiteout_mode.addItem("First page only", "first")
        self.whiteout_mode.addItem("None", "none")

        wo_row = QHBoxLayout()
        wo_row.setSpacing(8)
        wo_row.addWidget(self.whiteout_spin, stretch=1)
        wo_row.addWidget(self.whiteout_mode, stretch=1)
        form.addRow("PDF white-out", wo_row)

        self.photo_enhance_combo = QComboBox()
        self.photo_enhance_combo.setToolTip(
            "Automatically flatten uneven lighting/shadows and deskew camera photos before OMR"
        )
        self.photo_enhance_combo.addItem("Auto (detect camera photos)", "auto")
        self.photo_enhance_combo.addItem("On (always enhance photos)", "on")
        self.photo_enhance_combo.addItem("Off (raw image)", "off")
        form.addRow("Photo enhance", self.photo_enhance_combo)

        self.chk_merge_images = QCheckBox("Combine queued images into 1 multi-page score")
        self.chk_merge_images.setChecked(False)
        self.chk_merge_images.setToolTip(
            "Merge all queued sheet images (in list order) into one multi-page score before OMR & splitting"
        )
        form.addRow("", self.chk_merge_images)

        self.audiveris_edit = QLineEdit()
        self.audiveris_edit.setPlaceholderText("Auto-detect Audiveris.exe (dev3l prioritized)")
        self.audiveris_edit.setToolTip("Optional explicit path to Audiveris.exe (leave empty for auto-discovery)")
        self.audiveris_edit.editingFinished.connect(self._refresh_audiveris_status)
        av_row = QHBoxLayout()
        av_row.setSpacing(6)
        av_row.addWidget(self.audiveris_edit, stretch=1)
        self.btn_av = QPushButton("…")
        self.btn_av.setFixedWidth(40)
        self.btn_av.setToolTip("Browse for Audiveris.exe")
        self.btn_av.clicked.connect(self.browse_audiveris)
        av_row.addWidget(self.btn_av)
        form.addRow("Audiveris", av_row)
        ol.addLayout(form)

        self.audiveris_status = QLabel()
        self.audiveris_status.setWordWrap(True)
        ol.addWidget(self.audiveris_status)
        left_l.addWidget(opts, stretch=0)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setVisible(False)
        left_l.addWidget(self.progress)

        # ==== RIGHT COLUMN ====
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(12)

        # Top KPI Summary Bar
        kpi_row = QHBoxLayout()
        kpi_row.setSpacing(10)
        self.kpi_scores, self.kpi_scores_val, self.kpi_scores_sub = _kpi_card(
            "Scores", "0", "0 in queue", "#2563eb"
        )
        self.kpi_voices, self.kpi_voices_val, self.kpi_voices_sub = _kpi_card(
            "Voice Files", "0", "SATB + S/A/T/B", "#7c3aed"
        )
        self.kpi_divisi, self.kpi_divisi_val, self.kpi_divisi_sub = _kpi_card(
            "3+ Note Chords", "0", "0 ambiguous", "#0d9488"
        )
        self.kpi_flags, self.kpi_flags_val, self.kpi_flags_sub = _kpi_card(
            "Review Flags", "0", "Clean", "#d97706"
        )
        for kc in (self.kpi_scores, self.kpi_voices, self.kpi_divisi, self.kpi_flags):
            self._cards.append(kc)
            kpi_row.addWidget(kc, stretch=1)
        right_l.addLayout(kpi_row)

        # Workspace Tab Card
        right_card = _card()
        self._cards.append(right_card)
        rc = QVBoxLayout(right_card)
        rc.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()

        # ---- Tab 0: Pipeline Log ----
        log_tab = QWidget()
        log_l = QVBoxLayout(log_tab)
        log_l.setContentsMargins(8, 8, 8, 8)
        log_l.setSpacing(8)

        log_bar = QHBoxLayout()
        self.step_label = QLabel("Ready — add PDF, sheet photos, or MusicXML files to start.")
        self.step_label.setObjectName("muted")
        self.btn_copy_log = QPushButton("Copy log")
        self.btn_copy_log.setObjectName("subtle")
        self.btn_copy_log.clicked.connect(self._copy_log)
        self.btn_clear_log = QPushButton("Clear log")
        self.btn_clear_log.setObjectName("subtle")
        self.btn_clear_log.clicked.connect(self.log_view_clear)
        log_bar.addWidget(self.step_label, stretch=1)
        log_bar.addWidget(self.btn_copy_log)
        log_bar.addWidget(self.btn_clear_log)
        log_l.addLayout(log_bar)

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logConsole")
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Pipeline log will appear here…")
        log_l.addWidget(self.log_view, stretch=1)
        self.tabs.addTab(log_tab, "  Log  ")

        # ---- Tab 1: Voice Files ----
        results_w = QWidget()
        results_l = QVBoxLayout(results_w)
        results_l.setContentsMargins(8, 8, 8, 8)
        results_l.setSpacing(8)

        legend_row = QHBoxLayout()
        legend_row.setSpacing(6)
        hint = QLabel("Double-click any part to open in MuseScore:")
        hint.setObjectName("muted")
        legend_row.addWidget(hint)
        for role_name, hex_c in VOICE_COLORS.items():
            chip = QLabel(role_name)
            chip.setStyleSheet(
                f"background: {hex_c}; color: #ffffff; font-weight: 700; font-size: 10px; "
                "padding: 2px 8px; border-radius: 6px;"
            )
            legend_row.addWidget(chip)
        legend_row.addStretch()
        results_l.addLayout(legend_row)

        self.result_stack = QStackedWidget()
        empty_res = QLabel(
            "Voice files will appear here after a successful split.\n"
            "Combined SATB (.musicxml & .mscz) plus separate Soprano, Alto, Tenor, Bass parts."
        )
        empty_res.setObjectName("muted")
        empty_res.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results_list = QListWidget()
        self.results_list.setSpacing(4)
        self.results_list.setUniformItemSizes(False)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_list.itemDoubleClicked.connect(self._open_result_item)
        self.result_stack.addWidget(empty_res)
        self.result_stack.addWidget(self.results_list)
        results_l.addWidget(self.result_stack, stretch=1)

        res_btns = QHBoxLayout()
        res_btns.setSpacing(6)
        self.btn_open_satb = QPushButton("Open SATB in MuseScore")
        self.btn_open_satb.setObjectName("success")
        self.btn_open_satb.clicked.connect(self.open_satb_musescore)
        self.btn_open_satb.setEnabled(False)
        self.btn_open_voice = QPushButton("Open selected")
        self.btn_open_voice.clicked.connect(self.open_selected_result)
        self.btn_open_out = QPushButton("Open output folder")
        self.btn_open_out.clicked.connect(self.open_output_folder)
        self.btn_open_out.setEnabled(False)
        self.btn_resolve_divisi = QPushButton("Resolve 3-note chords…")
        self.btn_resolve_divisi.clicked.connect(self.open_divisi_resolver)
        self.btn_resolve_divisi.setEnabled(False)
        res_btns.addWidget(self.btn_open_satb)
        res_btns.addWidget(self.btn_open_voice)
        res_btns.addWidget(self.btn_open_out)
        res_btns.addWidget(self.btn_resolve_divisi)
        res_btns.addStretch()
        results_l.addLayout(res_btns)
        self.tabs.addTab(results_w, "  Voice files  ")

        # ---- Tab 2: Review Flags ----
        flags_tab = QWidget()
        flags_l = QVBoxLayout(flags_tab)
        flags_l.setContentsMargins(8, 8, 8, 8)
        flags_l.setSpacing(8)

        flags_bar = QHBoxLayout()
        flags_bar.setSpacing(8)
        self.flags_filter_edit = QLineEdit()
        self.flags_filter_edit.setPlaceholderText(
            "Filter review flags by measure (e.g. m14), reason, or keyword…"
        )
        self.flags_filter_edit.setClearButtonEnabled(True)
        self.flags_filter_edit.textChanged.connect(self._apply_flags_filter)
        self.flags_count_badge = QLabel("0 flags")
        self.flags_count_badge.setObjectName("countPill")
        flags_bar.addWidget(self.flags_filter_edit, stretch=1)
        flags_bar.addWidget(self.flags_count_badge)
        flags_l.addLayout(flags_bar)

        self.flags_view = QPlainTextEdit()
        self.flags_view.setReadOnly(True)
        self.flags_view.setPlaceholderText("Review flags (ambiguous measures) appear here.")
        flags_l.addWidget(self.flags_view, stretch=1)
        self.tabs.addTab(flags_tab, "  Review flags  ")

        rc.addWidget(self.tabs)
        right_l.addWidget(right_card, stretch=1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 7)
        outer.addWidget(splitter, stretch=1)

    def _section(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("sectionTitle")
        return lab

    def _subsection(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setObjectName("subSectionTitle")
        return lab

    def _apply_theme_to_widgets(self) -> None:
        is_dark = self._theme == "dark"
        self.btn_theme.setText("☀ Light" if is_dark else "🌙 Dark")
        for card in self._cards:
            apply_card_shadow(card, self._theme)

    def set_theme(self, theme: str) -> None:
        norm = "dark" if str(theme).lower().strip() == "dark" else "light"
        self._theme = norm
        self.settings.setValue("theme", norm)
        app = QApplication.instance()
        if app is not None:
            apply_app_theme(app, norm)
        self._apply_theme_to_widgets()
        self._refresh_audiveris_status()

    def toggle_theme(self) -> None:
        self.set_theme("dark" if self._theme == "light" else "light")

    def _copy_log(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.log_view.toPlainText())
            self.statusBar().showMessage("Copied pipeline log to clipboard.")

    def log_view_clear(self) -> None:
        self.log_view.clear()
        self.step_label.setText("Log cleared.")

    def _apply_flags_filter(self) -> None:
        raw = self._all_flags_text or self.flags_view.toPlainText()
        q = self.flags_filter_edit.text().strip().lower()
        if not q:
            if self.flags_view.toPlainText() != raw:
                self.flags_view.setPlainText(raw)
            return
        filtered: list[str] = []
        for line in raw.splitlines():
            if line.startswith("## ") or q in line.lower():
                filtered.append(line)
        self.flags_view.setPlainText("\n".join(filtered) if filtered else f"No flags matching '{q}'.")

    def _load_settings(self) -> None:
        default_out = str(Path.home() / "Documents" / "SATB_Splitter")
        self.out_edit.setText(str(self.settings.value("output_dir", default_out)))
        self.audiveris_edit.setText(str(self.settings.value("audiveris", "")))
        wo = float(self.settings.value("whiteout_pct", 12.0))
        if wo <= 1.0:
            wo *= 100.0
        self.whiteout_spin.setValue(wo)
        t = self.settings.value("template", "auto")
        idx = self.template_combo.findData(t)
        if idx >= 0:
            self.template_combo.setCurrentIndex(idx)
        dm = self.settings.value("divisi_mode", "monophonic_auto")
        dm_idx = self.divisi_combo.findData(dm)
        if dm_idx >= 0:
            self.divisi_combo.setCurrentIndex(dm_idx)
        pe = self.settings.value("photo_enhance", "auto")
        pe_idx = self.photo_enhance_combo.findData(pe)
        if pe_idx >= 0:
            self.photo_enhance_combo.setCurrentIndex(pe_idx)
        merge_val = self.settings.value("merge_images", False)
        is_merge = (
            merge_val.strip().lower() in ("true", "1", "yes")
            if isinstance(merge_val, str)
            else bool(merge_val)
        )
        self.chk_merge_images.setChecked(is_merge)

    def _save_settings(self) -> None:
        self.settings.setValue("output_dir", self.out_edit.text())
        self.settings.setValue("audiveris", self.audiveris_edit.text())
        self.settings.setValue("whiteout_pct", self.whiteout_spin.value())
        self.settings.setValue("template", self.template_combo.currentData())
        self.settings.setValue("divisi_mode", self.divisi_combo.currentData())
        self.settings.setValue("photo_enhance", self.photo_enhance_combo.currentData())
        self.settings.setValue("merge_images", self.chk_merge_images.isChecked())
        self.settings.setValue("theme", self._theme)

    def _overrides_path(self, work_dir: Path, stem: str) -> Path:
        return Path(work_dir) / f"{stem}.divisi_overrides.json"

    def _load_overrides(self, work_dir: Path, stem: str) -> dict[str, str]:
        p = self._overrides_path(work_dir, stem)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v and v != "auto"}

    def _save_overrides(self, work_dir: Path, stem: str, overrides: dict[str, str]) -> None:
        p = self._overrides_path(work_dir, stem)
        p.parent.mkdir(parents=True, exist_ok=True)
        clean = {str(k): str(v) for k, v in overrides.items() if v and v != "auto"}
        p.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")

    def _refresh_audiveris_status(self) -> None:
        found = find_audiveris(self.audiveris_edit.text().strip() or None)
        c = get_theme_colors(self._theme)
        if found:
            parent_tag = found.parent.name
            self.audiveris_status.setText(f"● Audiveris ready  ·  {found}")
            self.audiveris_status.setStyleSheet(
                f"background: {c['success_soft']}; color: {c['success']}; "
                f"border: 1px solid {c['success_border']}; border-radius: 8px; "
                "padding: 5px 10px; font-weight: 700; font-size: 11px;"
            )
            self.hero_av_pill.setText(f"● Audiveris ({parent_tag})")
            self.hero_av_pill.setObjectName("heroPillOk")
        else:
            self.audiveris_status.setText(
                "○ Audiveris not found — PDF OMR unavailable. MusicXML splitting still works."
            )
            self.audiveris_status.setStyleSheet(
                f"background: {c['danger_soft']}; color: {c['danger']}; "
                f"border: 1px solid {c['danger_border']}; border-radius: 8px; "
                "padding: 5px 10px; font-weight: 700; font-size: 11px;"
            )
            self.hero_av_pill.setText("○ Audiveris Missing")
            self.hero_av_pill.setObjectName("heroPillWarn")

        ms = self._find_musescore()
        if ms:
            self.hero_ms_pill.setText("● MuseScore 4 Ready")
            self.hero_ms_pill.setObjectName("heroPillOk")
        else:
            self.hero_ms_pill.setText("○ MuseScore Not Found")
            self.hero_ms_pill.setObjectName("heroPillInfo")

    def add_paths(self, paths: list[Path]) -> None:
        for p in paths:
            p_obj = Path(p)
            if p_obj.is_dir():
                for child in sorted(p_obj.iterdir()):
                    if child.is_file() and child.suffix.lower() in SUPPORTED_EXTS:
                        self.add_file(child)
            else:
                self.add_file(p_obj)

    def _add_file_row(
        self,
        path: Path,
        resolved: str,
        *,
        insert_at: int | None = None,
    ) -> QListWidgetItem:
        ext = path.suffix.lower()
        is_pdf = ext == ".pdf"
        is_img = is_image_path(path)
        if is_pdf:
            badge = "PDF · OMR"
            accent = "#7c3aed"
            kind = "Per-page OMR + split"
        elif is_img:
            badge = "IMAGE · OMR"
            accent = "#0284c7"
            kind = "Photo enhance + OMR + split"
        else:
            badge = "MusicXML"
            accent = "#0d9488"
            kind = "Direct voice split"
        return _add_row(
            self.file_list,
            path.name,
            f"{kind}  ·  {path.parent}",
            resolved,
            accent,
            badge=badge,
            status="Queued",
            insert_at=insert_at,
        )

    def add_file(self, path: Path) -> None:
        path = Path(path)
        if not path.is_file():
            return
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            QMessageBox.warning(
                self,
                APP_NAME,
                f"Unsupported type: {ext}\n\nUse PDF, sheet image (.png/.jpg/.webp/.tif/.bmp), or MusicXML (.musicxml / .mxl / .xml).",
            )
            return
        resolved = str(path.resolve())
        for i in range(self.file_list.count()):
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole) == resolved:
                return
        self._add_file_row(path, resolved)
        self._update_file_count()
        self.statusBar().showMessage(
            f"Added {path.name}  ({self.file_list.count()} in queue)"
        )

    def _update_file_count(self) -> None:
        n = self.file_list.count()
        label = f"{n} file" + ("" if n == 1 else "s")
        self.file_count_label.setText(label)
        self.hero_queue_pill.setText(f"Queue: {label}")
        self.file_stack.setCurrentIndex(1 if n else 0)
        done_n = len(self._queue_results)
        self.kpi_scores_val.setText(str(done_n if done_n else n))
        self.kpi_scores_sub.setText(
            f"{done_n} processed · {n} in queue" if done_n else f"{n} in queue"
        )

    def clear_files(self) -> None:
        self.file_list.clear()
        self._update_file_count()

    def browse_files(self) -> None:
        img_globs = " ".join(f"*{ext}" for ext in sorted(IMAGE_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add score files",
            str(self.settings.value("last_dir", str(Path.home()))),
            f"Scores (*.pdf {img_globs} *.musicxml *.xml *.mxl);;"
            f"PDF (*.pdf);;"
            f"Sheet Images ({img_globs});;"
            f"MusicXML (*.musicxml *.xml *.mxl);;"
            f"All (*.*)",
        )
        if paths:
            self.settings.setValue("last_dir", str(Path(paths[0]).parent))
            self.add_paths([Path(p) for p in paths])

    def browse_input_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self,
            "Add folder of scores",
            str(self.settings.value("last_dir", str(Path.home()))),
        )
        if d:
            self.settings.setValue("last_dir", d)
            self.add_paths([Path(d)])

    def _move_queue_rows(self, delta: int) -> None:
        count = self.file_list.count()
        if count < 2:
            return
        rows = sorted({self.file_list.row(it) for it in self.file_list.selectedItems()})
        if not rows:
            cur = self.file_list.currentRow()
            if cur < 0:
                return
            rows = [cur]
        if delta < 0 and rows[0] + delta < 0:
            return
        if delta > 0 and rows[-1] + delta >= count:
            return

        order = rows if delta < 0 else list(reversed(rows))
        new_rows: list[int] = []
        for r in order:
            item = self.file_list.item(r)
            if item is None:
                continue
            resolved = str(item.data(Qt.ItemDataRole.UserRole) or "")
            self.file_list.takeItem(r)
            target_r = r + delta
            self._add_file_row(Path(resolved), resolved, insert_at=target_r)
            new_rows.append(target_r)

        self.file_list.clearSelection()
        for r in new_rows:
            it = self.file_list.item(r)
            if it is not None:
                it.setSelected(True)
        if new_rows:
            self.file_list.setCurrentRow(new_rows[0])

    def move_selected_up(self) -> None:
        self._move_queue_rows(-1)

    def move_selected_down(self) -> None:
        self._move_queue_rows(1)

    def remove_selected(self) -> None:
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))
        self._update_file_count()

    def browse_output(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Output folder", self.out_edit.text() or str(Path.home())
        )
        if d:
            self.out_edit.setText(d)

    def browse_audiveris(self) -> None:
        found = find_audiveris(self.audiveris_edit.text().strip() or None)
        start_dir = str(found.parent) if found else r"C:\Program Files\Audiveris"
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Locate Audiveris.exe",
            start_dir,
            "Executable (*.exe);;All (*.*)",
        )
        if path:
            self.audiveris_edit.setText(path)
            self._refresh_audiveris_status()

    def start_pipeline(self) -> None:
        if self._thread is not None:
            return
        if self.file_list.count() == 0:
            QMessageBox.information(self, APP_NAME, "Add at least one PDF, sheet image, or MusicXML file first.")
            return
        out = self.out_edit.text().strip()
        if not out:
            QMessageBox.warning(self, APP_NAME, "Choose an output folder.")
            return
        self._save_settings()
        self._refresh_audiveris_status()

        items = self.file_list.selectedItems()
        if not items:
            items = [self.file_list.item(i) for i in range(self.file_list.count())]
        raw_paths: list[Path] = []
        for it in items:
            data = it.data(Qt.ItemDataRole.UserRole)
            if data:
                raw_paths.append(Path(str(data)))
        if not raw_paths:
            QMessageBox.warning(self, APP_NAME, "Could not read file paths from the list.")
            return

        self._queue = []
        if self.chk_merge_images.isChecked():
            img_group = [p for p in raw_paths if is_image_path(p)]
            inserted_group = False
            for p in raw_paths:
                if is_image_path(p):
                    if not inserted_group:
                        self._queue.append(img_group)
                        inserted_group = True
                else:
                    self._queue.append(p)
        else:
            self._queue = list(raw_paths)

        self._cancel.clear()
        self._queue_results = []
        self._reexporting_divisi = False
        self._last_failed = False
        self._all_flags_text = ""
        self.log_view.clear()
        self.results_list.clear()
        self.flags_view.clear()
        self.flags_filter_edit.clear()
        self.flags_count_badge.setText("0 flags")
        self.result_stack.setCurrentIndex(0)
        self._last_result = None
        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳  Working…")
        self.btn_cancel.setVisible(True)
        self.btn_open_out.setEnabled(False)
        self.btn_open_satb.setEnabled(False)
        self.btn_resolve_divisi.setEnabled(False)
        self.progress.setVisible(True)
        self._refresh_kpis()
        self.log(f"Queued {len(self._queue)} file(s)")
        self.log(f"Layout: {self.template_combo.currentText()}")
        self.tabs.setCurrentIndex(0)
        self._start_next_in_queue()

    def cancel_pipeline(self) -> None:
        self._cancel.set()
        self._queue.clear()
        if self._worker:
            self._worker.request_cancel()
        self.log("Cancel requested — waiting for current page to finish…")
        self.step_label.setText("Cancelling…")
        self.statusBar().showMessage("Cancelling…")

    def _start_worker(
        self,
        path: Path,
        work: Path,
        *,
        is_pdf: bool,
        divisi_overrides: dict[str, str],
        mxl_paths: list[Path] | None = None,
        is_image: bool = False,
        image_paths: list[Path] | None = None,
    ) -> None:
        whiteout_frac = self.whiteout_spin.value() / 100.0
        divisi_mode = str(self.divisi_combo.currentData() or "monophonic_auto")
        photo_enhance = str(self.photo_enhance_combo.currentData() or "auto")
        self._thread = QThread()
        self._worker = PipelineWorker(
            path,
            work,
            is_pdf=is_pdf,
            whiteout=whiteout_frac if self.whiteout_mode.currentData() != "none" else 0.0,
            whiteout_mode=str(self.whiteout_mode.currentData()),
            template=str(self.template_combo.currentData()),
            unison=str(self.unison_combo.currentData()),
            audiveris=self.audiveris_edit.text().strip() or None,
            divisi_mode=divisi_mode,
            divisi_overrides=divisi_overrides,
            mxl_paths=mxl_paths,
            is_image=is_image,
            image_paths=image_paths,
            photo_enhance=photo_enhance,
            deskew=True,
            cancel_event=self._cancel,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.log)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def _start_next_in_queue(self) -> None:
        if self._cancel.is_set() or not self._queue:
            self._finish_queue()
            return

        entry = self._queue.pop(0)
        out = self.out_edit.text().strip()
        if isinstance(entry, list):
            paths = [Path(p) for p in entry]
            path = paths[0]
            work = Path(out) / path.stem
            is_pdf = False
            is_image = True
            image_paths: list[Path] | None = paths
            label = f"{path.name} (+{len(paths) - 1} page(s))" if len(paths) > 1 else path.name
        else:
            path = Path(entry)
            work = Path(out) / path.stem
            is_pdf = path.suffix.lower() == ".pdf"
            is_image = is_image_path(path)
            image_paths = [path] if is_image else None
            label = path.name

        self.step_label.setText(f"Processing {label}…")
        self.statusBar().showMessage(f"Processing {label}…")
        self.log(f"\n── {label} ──")
        if image_paths and len(image_paths) > 1:
            self.log(f"Input:  {len(image_paths)} images -> {', '.join(p.name for p in image_paths)}")
        else:
            self.log(f"Input:  {path}")
        self.log(f"Output: {work}")
        if is_pdf or is_image:
            extra = (
                f" · photo={self.photo_enhance_combo.currentData()}"
                if is_image
                else ""
            )
            self.log(
                f"OMR: per-page · white-out {self.whiteout_spin.value():.0f}% "
                f"· {self.whiteout_mode.currentText()}{extra}"
            )

        self._start_worker(
            path,
            work,
            is_pdf=is_pdf,
            divisi_overrides=self._load_overrides(work, path.stem),
            is_image=is_image,
            image_paths=image_paths,
        )

    def _cleanup_thread(self) -> None:
        if self._worker:
            self._worker.deleteLater()
        if self._thread:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        if self._queue and not self._cancel.is_set():
            self._start_next_in_queue()
        else:
            self._idle_buttons()
            if not self._last_failed and not self._cancel.is_set():
                self._finish_queue()

    def _idle_buttons(self) -> None:
        self.btn_run.setEnabled(True)
        self.btn_run.setText("▶  Convert && Split")
        self.btn_cancel.setVisible(False)
        self.progress.setVisible(False)

    def _finish_queue(self) -> None:
        self._idle_buttons()
        self.btn_open_out.setEnabled(bool(self._last_result))
        was_reexport = self._reexporting_divisi
        self._reexporting_divisi = False
        if self._last_result:
            satb = self._last_result.get("satb")
            self.btn_open_satb.setEnabled(bool(satb))
            self.btn_resolve_divisi.setEnabled(bool(self._last_result.get("divisi_choices")))
            self.step_label.setText(f"Completed — {self._last_result.get('voices_dir')}")
            self.statusBar().showMessage(f"Done — {self._last_result.get('voices_dir')}")
            self.tabs.setCurrentIndex(1)
            choices = self._last_result.get("divisi_choices") or []
            if (
                not was_reexport
                and str(self.divisi_combo.currentData()) == "ask"
                and any(getattr(ch, "ambiguous", False) for ch in choices)
            ):
                self.open_divisi_resolver()

    def open_divisi_resolver(self) -> None:
        if not self._last_result:
            return
        choices = list(self._last_result.get("divisi_choices") or [])
        if not choices:
            QMessageBox.information(self, APP_NAME, "No 3+ note simultaneous chords in this score.")
            return
        input_path = Path(self._last_result["input"])
        work_dir = Path(self._last_result["work_dir"])
        saved = self._load_overrides(work_dir, input_path.stem)
        dlg = DivisiResolverDialog(choices, overrides=saved, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_overrides = dlg.selected_overrides()
        self._save_overrides(work_dir, input_path.stem, new_overrides)
        self._reexport_with_overrides(self._last_result, new_overrides)

    def _reexport_with_overrides(self, prev_result: dict, overrides: dict[str, str]) -> None:
        if self._thread is not None:
            return
        input_path = Path(prev_result["input"])
        work_dir = Path(prev_result["work_dir"])
        mxl_paths = [Path(p) for p in (prev_result.get("mxl") or [])]
        if not mxl_paths:
            return
        is_pdf = input_path.suffix.lower() == ".pdf"
        is_image = is_image_path(input_path)
        self._cancel.clear()
        self._reexporting_divisi = True
        self._last_failed = False
        self._all_flags_text = ""
        self.results_list.clear()
        self.flags_view.clear()
        self.btn_run.setEnabled(False)
        self.btn_run.setText("⏳  Working…")
        self.btn_resolve_divisi.setEnabled(False)
        self.progress.setVisible(True)
        self.step_label.setText(f"Re-exporting {input_path.name}…")
        self.statusBar().showMessage(f"Re-exporting {input_path.name} with updated chord choices…")
        self.log(f"\n── Re-exporting {input_path.name} ({len(overrides)} override(s)) ──")
        self._start_worker(
            input_path,
            work_dir,
            is_pdf=is_pdf,
            divisi_overrides=overrides,
            mxl_paths=mxl_paths,
            is_image=is_image,
        )

    def _refresh_kpis(self) -> None:
        n_queue = self.file_list.count()
        n_done = len(self._queue_results)
        self.kpi_scores_val.setText(str(n_done if n_done else n_queue))
        self.kpi_scores_sub.setText(
            f"{n_done} completed · {n_queue} queued" if n_done else f"{n_queue} in queue"
        )

        n_voices = self.results_list.count()
        self.kpi_voices_val.setText(str(n_voices))
        self.kpi_voices_sub.setText("Ready in MuseScore" if n_voices else "SATB + S/A/T/B")

        total_chords = 0
        ambig_chords = 0
        total_flags = 0
        for res in self._queue_results:
            chs = res.get("divisi_choices") or []
            total_chords += len(chs)
            ambig_chords += sum(1 for c in chs if getattr(c, "ambiguous", False))
            total_flags += len(res.get("flags") or [])

        self.kpi_divisi_val.setText(str(total_chords))
        self.kpi_divisi_sub.setText(f"{ambig_chords} ambiguous" if total_chords else "None detected")

        self.kpi_flags_val.setText(str(total_flags))
        self.kpi_flags_sub.setText("Needs review" if total_flags else "Clean split")
        self.flags_count_badge.setText(f"{total_flags} flag" + ("" if total_flags == 1 else "s"))

    def _on_finished(self, result: dict) -> None:
        self._last_failed = False
        self._last_result = result
        if self._reexporting_divisi and self._queue_results:
            self._queue_results[-1] = result
        else:
            self._queue_results.append(result)
        self.btn_open_out.setEnabled(True)
        self.btn_resolve_divisi.setEnabled(bool(result.get("divisi_choices")))
        self.result_stack.setCurrentIndex(1)

        stem = Path(result["input"]).stem
        satb = result.get("satb")
        if satb:
            _add_row(
                self.results_list,
                "SATB  —  Combined 4-voice proof score (open in MuseScore)",
                str(satb),
                satb,
                VOICE_COLORS["SATB"],
                badge="SATB · XML",
                status="Ready",
            )
            self.btn_open_satb.setEnabled(True)
        mscz = result.get("mscz")
        if mscz:
            _add_row(
                self.results_list,
                f"SATB MuseScore 4 file  —  {Path(mscz).name}",
                str(mscz),
                mscz,
                VOICE_COLORS["SATB"],
                badge="SATB · MSCZ",
                status="MuseScore 4",
            )

        for role in ("Soprano", "Alto", "Tenor", "Bass"):
            path = result.get("voices", {}).get(role)
            if not path:
                continue
            _add_row(
                self.results_list,
                f"{role}  —  {Path(path).name}",
                str(path),
                path,
                VOICE_COLORS.get(role, "#0f172a"),
                badge=role.upper(),
                status="Voice Part",
            )

        voices_dir = result.get("voices_dir")
        if voices_dir:
            review_file = Path(voices_dir) / f"{stem}_REVIEW.txt"
            if review_file.is_file():
                _add_row(
                    self.results_list,
                    f"Review Report  —  {review_file.name}",
                    str(review_file),
                    str(review_file),
                    VOICE_COLORS["Review"],
                    badge="REVIEW",
                    status=f"{len(result.get('flags') or [])} flags",
                )

        flags = result.get("flags") or []
        if flags:
            existing = self._all_flags_text.strip()
            block = f"## {Path(result['input']).name}\n" + "\n".join(flags)
            if existing and existing != "No review flags.":
                self._all_flags_text = existing + "\n\n" + block
            else:
                self._all_flags_text = block
        elif not self._all_flags_text.strip():
            self._all_flags_text = "No review flags."

        self.flags_view.setPlainText(self._all_flags_text)
        if self.flags_filter_edit.text().strip():
            self._apply_flags_filter()

        self._refresh_kpis()

        self.log(f"Done. Voices → {result.get('voices_dir')}")
        if satb:
            self.log(f"Combined SATB score → {satb}")
        if result.get("mscz_message"):
            self.log(("" if mscz else "WARNING: ") + str(result.get("mscz_message")))
        self.statusBar().showMessage(f"{stem}: SATB + 4 voice files ready")
        self.tabs.setCurrentIndex(1)

    def _on_failed(self, message: str) -> None:
        self._last_failed = True
        self.log(f"\nERROR: {message}")
        self.step_label.setText("Failed — see Log")
        self.statusBar().showMessage("Failed — see Log")
        if message != "Cancelled" and not self._queue:
            QMessageBox.critical(self, APP_NAME, message)

    def log(self, msg: str) -> None:
        self.log_view.appendPlainText(msg)
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)
        clean = msg.strip()
        if clean:
            self.step_label.setText(clean.splitlines()[-1][:110])

    def open_output_folder(self) -> None:
        path = None
        if self._last_result:
            path = self._last_result.get("voices_dir") or self._last_result.get("work_dir")
        if not path:
            path = self.out_edit.text()
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_result_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            prefer_ms = not str(path).lower().endswith(".txt")
            self._open_path(str(path), prefer_musescore=prefer_ms)

    def open_selected_result(self) -> None:
        items = self.results_list.selectedItems()
        if not items and self.results_list.count():
            items = [self.results_list.item(0)]
        for item in items:
            self._open_result_item(item)

    def open_satb_musescore(self) -> None:
        path = None
        if self._last_result:
            path = self._last_result.get("satb")
        if not path and self.results_list.count():
            path = self.results_list.item(0).data(Qt.ItemDataRole.UserRole)
        if not path:
            QMessageBox.information(self, APP_NAME, "No SATB score yet. Run Convert & split first.")
            return
        self._open_path(str(path), prefer_musescore=True)

    def _open_path(self, path: str, *, prefer_musescore: bool) -> None:
        if prefer_musescore:
            mscore = self._find_musescore()
            if mscore:
                try:
                    subprocess.Popen([mscore, path])
                    return
                except OSError:
                    pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @staticmethod
    def _find_musescore() -> str | None:
        candidates = [
            Path(r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe"),
            Path(r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe"),
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
        from shutil import which

        for name in ("MuseScore4", "mscore", "MuseScore3"):
            w = which(name)
            if w:
                return w
        return None

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            (
                f"<h2>{APP_NAME} · Studio Edition</h2>"
                "<p>Convert choral PDFs, sheet music photos, and MusicXML into Soprano, Alto, Tenor, Bass parts "
                "and MuseScore 4 (.mscz) proof scores.</p>"
                "<p>After a split, open <b>*_SATB.musicxml</b> or <b>*_SATB.mscz</b> in MuseScore "
                "to review all four voices together.</p>"
            ),
        )

    def closeEvent(self, event) -> None:
        self._save_settings()
        super().closeEvent(event)
