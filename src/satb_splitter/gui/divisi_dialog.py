"""Interactive studio dialog for reviewing and overriding 3+ note chord routing."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from satb_splitter.gui.icon import create_app_icon
from satb_splitter.model.config import DivisiChoice


def _choice_key(ch: DivisiChoice) -> str:
    return ch.choice_key or ch.normalized_key()


def _normalize_override(val: str | None) -> str:
    if not val or val == "auto":
        return "auto"
    if val == "lower_low":
        return "upper_high"
    return val


def _decision_options(ch: DivisiChoice) -> list[tuple[str, str]]:
    """Build explicit pitch-labelled decision options for ``ch``."""
    pitches = list(ch.pitches)
    if len(pitches) < 3:
        return [("Auto", "auto")]
    hi_p = pitches[0]
    lo_p = pitches[-1]
    mid_u = pitches[1]
    mid_l = pitches[-2]
    auto_u = "+".join(ch.chosen_upper) or "—"
    auto_l = "+".join(ch.chosen_lower) or "—"
    opts: list[tuple[str, str]] = [
        (f"Auto ({ch.upper_role}={auto_u}, {ch.lower_role}={auto_l})", "auto"),
        (f"Outer notes ({ch.upper_role}={hi_p}, {ch.lower_role}={lo_p})", "upper_high"),
        (f"{ch.upper_role} takes middle ({ch.upper_role}={mid_u}, {ch.lower_role}={lo_p})", "upper_mid"),
        (f"{ch.lower_role} takes middle ({ch.upper_role}={hi_p}, {ch.lower_role}={mid_l})", "lower_mid"),
        (
            f"Keep divisi in {ch.upper_role} ({ch.upper_role}={'+'.join(pitches[:-1])}, {ch.lower_role}={lo_p})",
            "keep_upper",
        ),
        (
            f"Keep divisi in {ch.lower_role} ({ch.upper_role}={hi_p}, {ch.lower_role}={'+'.join(pitches[1:])})",
            "keep_lower",
        ),
    ]
    if len(pitches) >= 4:
        mid_idx = (len(pitches) + 1) // 2
        opts.append(
            (
                f"Keep divisi in both ({ch.upper_role}={'+'.join(pitches[:mid_idx])}, "
                f"{ch.lower_role}={'+'.join(pitches[mid_idx:])})",
                "keep_both",
            )
        )
    return opts


class DivisiResolverDialog(QDialog):
    """Modal table allowing singers/conductors to inspect & override 3+ note chords."""

    COLUMNS = [
        "Bar",
        "Staff",
        "Chord (High → Low)",
        "Context (Prev → Next)",
        "Status / Reason",
        "Decision",
    ]

    def __init__(
        self,
        choices: list[DivisiChoice],
        overrides: dict[str, str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Resolve 3-Note Chords · SATB Splitter")
        self.setWindowIcon(create_app_icon())
        self.resize(1060, 580)
        self.setMinimumSize(820, 440)

        self._choices = list(choices)
        self._overrides: dict[str, str] = {}
        for k, v in (overrides or {}).items():
            norm = _normalize_override(v)
            if norm != "auto":
                self._overrides[k] = norm

        self._visible_indices: list[int] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # ---- Studio Header Card with Summary Badges ----
        header_card = QFrame()
        header_card.setObjectName("card")
        hl = QVBoxLayout(header_card)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(10)

        top_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_lbl = QLabel("3+ Note Chord & Divisi Resolver")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: 800;")
        sub_lbl = QLabel(
            "Default routing keeps 1 monophonic line per voice using stem & voice-leading cues. "
            "Override individual chords or apply a rule across an entire staff pair."
        )
        sub_lbl.setObjectName("muted")
        sub_lbl.setWordWrap(True)
        title_col.addWidget(title_lbl)
        title_col.addWidget(sub_lbl)
        top_row.addLayout(title_col, stretch=1)

        ambig_count = sum(1 for c in self._choices if c.ambiguous)
        conf_count = len(self._choices) - ambig_count

        badges_row = QHBoxLayout()
        badges_row.setSpacing(8)
        self.badge_total = QLabel(f"Total Chords: {len(self._choices)}")
        self.badge_total.setObjectName("countPill")
        self.badge_ambig = QLabel(f"Ambiguous: {ambig_count}")
        self.badge_ambig.setObjectName("statusPillWarn" if ambig_count else "statusPillOk")
        self.badge_conf = QLabel(f"Confident: {conf_count}")
        self.badge_conf.setObjectName("statusPillOk")
        self.badge_overrides = QLabel(f"Active Overrides: {len(self._overrides)}")
        self.badge_overrides.setObjectName("countPill")

        badges_row.addWidget(self.badge_total)
        badges_row.addWidget(self.badge_ambig)
        badges_row.addWidget(self.badge_conf)
        badges_row.addWidget(self.badge_overrides)
        top_row.addLayout(badges_row)
        hl.addLayout(top_row)

        # Filter & search bar inside header card
        filter_row = QHBoxLayout()
        filter_row.setSpacing(12)
        self.chk_ambiguous_only = QCheckBox("Show ambiguous chords only")
        self.chk_ambiguous_only.setChecked(ambig_count > 0)
        self.chk_ambiguous_only.toggled.connect(self._populate_table)
        filter_row.addWidget(self.chk_ambiguous_only)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by bar (e.g. m12), staff (Soprano), pitch (G4), or reason…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._populate_table)
        filter_row.addWidget(self.search_edit, stretch=1)

        self.row_count_label = QLabel("")
        self.row_count_label.setObjectName("muted")
        filter_row.addWidget(self.row_count_label)
        hl.addLayout(filter_row)
        root.addWidget(header_card)

        # ---- Chords Table ----
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, stretch=1)

        # ---- Footer Action Bar ----
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self.btn_apply_staff = QPushButton("Apply current row's choice to same staff")
        self.btn_apply_staff.setToolTip(
            "Copy the selected row's decision to all 3+ note chords on the same staff pair"
        )
        self.btn_apply_staff.clicked.connect(self._apply_to_same_staff)
        self.btn_reset = QPushButton("Reset all to Auto")
        self.btn_reset.setToolTip("Clear all custom chord overrides and revert to automatic routing")
        self.btn_reset.clicked.connect(self._reset_all)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_apply = QPushButton("Apply && Re-export")
        self.btn_apply.setObjectName("primary")
        self.btn_apply.clicked.connect(self.accept)

        btns.addWidget(self.btn_apply_staff)
        btns.addWidget(self.btn_reset)
        btns.addStretch()
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_apply)
        root.addLayout(btns)

        self._populate_table()

    def _update_override_badge(self) -> None:
        active = len(self.selected_overrides())
        self.badge_overrides.setText(f"Active Overrides: {active}")

    def _populate_table(self) -> None:
        ambig_only = self.chk_ambiguous_only.isChecked()
        query = self.search_edit.text().strip().lower() if hasattr(self, "search_edit") else ""

        visible: list[int] = []
        for idx, ch in enumerate(self._choices):
            if ambig_only and not ch.ambiguous:
                continue
            if query:
                haystack = " ".join(
                    [
                        f"m{ch.measure}",
                        str(ch.source_page or ""),
                        str(ch.source_measure or ""),
                        ch.upper_role,
                        ch.lower_role,
                        " ".join(ch.pitches),
                        str(ch.prev_upper or ""),
                        str(ch.next_upper or ""),
                        str(ch.prev_lower or ""),
                        str(ch.next_lower or ""),
                        ch.reason,
                        "ambiguous" if ch.ambiguous else "confident",
                    ]
                ).lower()
                if query not in haystack:
                    continue
            visible.append(idx)

        self._visible_indices = visible
        self.table.setRowCount(len(self._visible_indices))
        self.row_count_label.setText(
            f"Showing {len(self._visible_indices)} of {len(self._choices)}"
        )

        bold_font = QFont()
        bold_font.setWeight(QFont.Weight.Bold)

        for row, ch_idx in enumerate(self._visible_indices):
            ch = self._choices[ch_idx]
            key = _choice_key(ch)
            self.table.setRowHeight(row, 38)

            loc = f"m{ch.measure}"
            if ch.source_page and ch.source_measure:
                loc += f" ({ch.source_page} m{ch.source_measure})"
            elif ch.source_measure:
                loc += f" (MXL m{ch.source_measure})"
            if ch.offset:
                loc += f" @{ch.offset:g}"

            ctx_txt = (
                f"{ch.upper_role[0]}: {ch.prev_upper or '—'} → {ch.next_upper or '—'}  |  "
                f"{ch.lower_role[0]}: {ch.prev_lower or '—'} → {ch.next_lower or '—'}"
            )
            status = "Ambiguous" if ch.ambiguous else "Confident"
            reason_txt = f"{status} ({int(round(ch.confidence * 100))}%) — {ch.reason}"

            cells = [
                loc,
                f"{ch.upper_role} / {ch.lower_role}",
                ", ".join(ch.pitches),
                ctx_txt,
                reason_txt,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col in (0, 2):
                    item.setFont(bold_font)
                elif col == 4:
                    item.setFont(bold_font)
                    if ch.ambiguous:
                        item.setForeground(QColor("#d97706"))
                    else:
                        item.setForeground(QColor("#16a34a"))
                self.table.setItem(row, col, item)

            combo = QComboBox()
            for label, data in _decision_options(ch):
                combo.addItem(label, data)
            saved_val = _normalize_override(self._overrides.get(key))
            idx = combo.findData(saved_val)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(
                lambda _i, k=key, cb=combo: self._on_combo_changed(k, cb)
            )
            self.table.setCellWidget(row, 5, combo)

        self.btn_apply_staff.setEnabled(bool(self._visible_indices))
        self._update_override_badge()

    def _on_combo_changed(self, key: str, combo: QComboBox) -> None:
        val = str(combo.currentData() or "auto")
        if val == "auto":
            self._overrides.pop(key, None)
        else:
            self._overrides[key] = val
        self._update_override_badge()

    def _apply_to_same_staff(self) -> None:
        row = self.table.currentRow()
        if row < 0 and self._visible_indices:
            row = 0
        if row < 0 or row >= len(self._visible_indices):
            return
        src_ch = self._choices[self._visible_indices[row]]
        combo = self.table.cellWidget(row, 5)
        if not isinstance(combo, QComboBox):
            return
        token = str(combo.currentData() or "auto")
        staff_pair = (src_ch.upper_role, src_ch.lower_role)
        for ch in self._choices:
            if (ch.upper_role, ch.lower_role) != staff_pair:
                continue
            k = _choice_key(ch)
            if token == "auto":
                self._overrides.pop(k, None)
            elif token == "keep_both" and len(ch.pitches) < 4:
                self._overrides[k] = "keep_upper"
            else:
                self._overrides[k] = token
        self._populate_table()
        if 0 <= row < self.table.rowCount():
            self.table.selectRow(row)

    def _reset_all(self) -> None:
        self._overrides.clear()
        self._populate_table()

    def selected_overrides(self) -> dict[str, str]:
        """Return non-auto ``choice_key -> override`` mapping."""
        return {k: v for k, v in self._overrides.items() if v and v != "auto"}
