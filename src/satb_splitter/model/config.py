"""Configuration and layout templates for SATB splitting."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal
import json


class UnisonMode(str, Enum):
    """How to treat a single note on a staff shared by two voices."""

    DUPLICATE_BOTH = "duplicate_both"
    UPPER_ONLY = "upper_only"
    LOWER_ONLY = "lower_only"
    FLAG_REVIEW = "flag_review"


class ChordMode(str, Enum):
    """How to split simultaneous pitches encoded as a chord."""

    PITCH_RANK = "pitch_rank"  # highest -> upper, lowest -> lower


class DivisiMode(str, Enum):
    """How to handle 3+ note simultaneous chords on a two-voice staff."""

    MONOPHONIC_AUTO = "monophonic_auto"  # Option B default: 1 pitch per voice
    KEEP_DIVISI = "keep_divisi"          # Keep optional pitches as divisi Chord
    UPPER_ONLY = "upper_only"            # Assign middle pitch ownership to upper voice
    LOWER_ONLY = "lower_only"            # Assign middle pitch ownership to lower voice


@dataclass
class DivisiChoice:
    """Metadata and realization record for one 3+ note simultaneous chord."""

    measure: int
    part_index: int
    offset: float
    upper_role: str
    lower_role: str
    pitches: list[str]
    auto_owner: str
    chosen_upper: list[str]
    chosen_lower: list[str]
    ambiguous: bool
    confidence: float
    reason: str
    prev_upper: str | None = None
    next_upper: str | None = None
    prev_lower: str | None = None
    next_lower: str | None = None
    source_page: str | None = None
    source_measure: str | None = None
    choice_key: str = ""
    raw_measure: int | None = None

    def normalized_key(self) -> str:
        """Return canonical ``measure|upper_role|offset`` key."""
        return f"{self.measure}|{self.upper_role}|{round(float(self.offset), 4)}"


class TenorClef(str, Enum):
    TREBLE_8VB = "treble_8vb"
    BASS = "bass"
    AS_SOURCE = "as_source"


VoiceRole = Literal["Soprano", "Alto", "Tenor", "Bass"]


@dataclass(frozen=True)
class StaffPair:
    """Maps one source part/staff to an upper and/or lower SATB role.

    If only ``upper`` is set (and ``lower`` is None), the staff is monophonic
    and goes entirely to that voice (four-staff open score).
    """

    part_index: int
    upper: VoiceRole | None
    lower: VoiceRole | None = None

    def roles(self) -> list[VoiceRole]:
        out: list[VoiceRole] = []
        if self.upper is not None:
            out.append(self.upper)
        if self.lower is not None:
            out.append(self.lower)
        return out


@dataclass
class LayoutTemplate:
    name: str
    description: str
    pairs: list[StaffPair]
    ignore_part_indices: list[int] = field(default_factory=list)

    @staticmethod
    def two_staff_satb() -> LayoutTemplate:
        """Classic choral: treble = S+A, bass = T+B."""
        return LayoutTemplate(
            name="two_staff_satb",
            description="Two staves: treble SA, bass TB",
            pairs=[
                StaffPair(0, "Soprano", "Alto"),
                StaffPair(1, "Tenor", "Bass"),
            ],
        )

    @staticmethod
    def four_staff_open() -> LayoutTemplate:
        """Open score: one staff per voice."""
        return LayoutTemplate(
            name="four_staff_open",
            description="Four staves: S, A, T, B each alone",
            pairs=[
                StaffPair(0, "Soprano", None),
                StaffPair(1, "Alto", None),
                StaffPair(2, "Tenor", None),
                StaffPair(3, "Bass", None),
            ],
        )

    @staticmethod
    def three_staff_satb() -> LayoutTemplate:
        """Three staves: SA combined + monophonic T + monophonic B.

        Common when Audiveris/open scores use a shared treble for S+A
        and separate lower staves (seen on hard test pages 8, 11–14).
        """
        return LayoutTemplate(
            name="three_staff_satb",
            description="Three staves: SA / T / B",
            pairs=[
                StaffPair(0, "Soprano", "Alto"),
                StaffPair(1, "Tenor", None),
                StaffPair(2, "Bass", None),
            ],
        )

    @staticmethod
    def single_staff_sa() -> LayoutTemplate:
        """One staff only — split as Soprano + Alto (no T/B)."""
        return LayoutTemplate(
            name="single_staff_sa",
            description="Single staff split into S+A",
            pairs=[
                StaffPair(0, "Soprano", "Alto"),
            ],
        )

    @classmethod
    def by_name(cls, name: str) -> LayoutTemplate:
        presets = {
            "two_staff_satb": cls.two_staff_satb,
            "three_staff_satb": cls.three_staff_satb,
            "four_staff_open": cls.four_staff_open,
            "single_staff_sa": cls.single_staff_sa,
            "auto": cls.two_staff_satb,  # placeholder; use detect_layout() for real auto
        }
        if name not in presets:
            known = ", ".join(k for k in presets if k != "auto")
            raise ValueError(f"Unknown template '{name}'. Known: {known}, auto")
        return presets[name]()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "ignore_part_indices": list(self.ignore_part_indices),
            "pairs": [
                {
                    "part_index": p.part_index,
                    "upper": p.upper,
                    "lower": p.lower,
                }
                for p in self.pairs
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> LayoutTemplate:
        pairs = [
            StaffPair(
                part_index=int(p["part_index"]),
                upper=p.get("upper"),
                lower=p.get("lower"),
            )
            for p in data["pairs"]
        ]
        return cls(
            name=data.get("name", "custom"),
            description=data.get("description", ""),
            pairs=pairs,
            ignore_part_indices=list(data.get("ignore_part_indices", [])),
        )

    @classmethod
    def load_json(cls, path: Path | str) -> LayoutTemplate:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


@dataclass
class GraftConfig:
    """Batch 14/15: OMR repair graft, recovery, and post-split continuity flags.

    Committed Batch 15 L8 Confirmation Gate X* = (1, 1, 0, 1, 1, 1, 1) on top of
    Batch 14 (1, 1, 1, 0, 1, 1, 0): ``B15_F1`` (``f4_type_b_unprinted_rest``),
    ``B15_F2`` (lyric dedup), ``B15_F4`` (Type D overfull), ``B15_F5`` (Type D
    underfull/clipped + split-half), ``B15_F6`` (tie continuity), and ``B15_F7``
    (3-voice chord tie split) enabled; ``B15_F3`` (cross-staff meter clamp) off.
    """

    f1_type_a_dot: bool = True
    f2_type_a_beam_meter: bool = True
    f3_type_b_shared_rest: bool = True
    f4_type_b_unprinted_rest: bool = True
    f5_type_c_voice_split: bool = True
    f6_type_c_split_dur: bool = True
    f7_pdf_multirest_mr1: bool = False
    b15_f2_lyric_dedup: bool = True
    b15_f3_meter_clamp: bool = False
    b15_f4_type_d_overfull: bool = True
    b15_f5_type_d_underfull: bool = True
    b15_f6_tie_continuity: bool = True
    b15_f7_chord_tie_split: bool = True
    graft_json_path: Path | None = None

    def any_graft_enabled(self) -> bool:
        return any((
            self.f1_type_a_dot,
            self.f2_type_a_beam_meter,
            self.f3_type_b_shared_rest,
            self.f4_type_b_unprinted_rest,
            self.f5_type_c_voice_split,
            self.f6_type_c_split_dur,
            self.b15_f4_type_d_overfull,
            self.b15_f5_type_d_underfull,
        ))


_GRAFT_FLAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("F1", "f1_type_a_dot"),
    ("F2", "f2_type_a_beam_meter"),
    ("F3", "f3_type_b_shared_rest"),
    ("F4", "f4_type_b_unprinted_rest"),
    ("F5", "f5_type_c_voice_split"),
    ("F6", "f6_type_c_split_dur"),
    ("F7", "f7_pdf_multirest_mr1"),
)

_B15_FLAG_FIELDS: tuple[tuple[str, str], ...] = (
    ("B15_F1", "f4_type_b_unprinted_rest"),
    ("B15_F2", "b15_f2_lyric_dedup"),
    ("B15_F3", "b15_f3_meter_clamp"),
    ("B15_F4", "b15_f4_type_d_overfull"),
    ("B15_F5", "b15_f5_type_d_underfull"),
    ("B15_F6", "b15_f6_tie_continuity"),
    ("B15_F7", "b15_f7_chord_tie_split"),
)


def _apply_flag_env(
    resolved: GraftConfig,
    env_flags: str,
    fields: tuple[tuple[str, str], ...],
    *,
    extra_fields: tuple[tuple[str, str], ...] = (),
    short_aliases: dict[str, str] | None = None,
) -> bool:
    """Parse env flag specification onto ``resolved``. Returns True if kill-switch off."""
    low = env_flags.lower()
    if low in ("all", "on", "true"):
        for _, attr in fields:
            setattr(resolved, attr, True)
        return False
    if low in ("none", "off", "false", "0"):
        for _, attr in fields:
            setattr(resolved, attr, False)
        return True
    if len(env_flags) == 7 and set(env_flags) <= {"0", "1"}:
        for ch, (_, attr) in zip(env_flags, fields):
            setattr(resolved, attr, ch == "1")
        return False

    tokens = [t.strip() for t in env_flags.split(",") if t.strip()]
    if len(tokens) == 7 and all(t in ("0", "1") for t in tokens):
        for tok, (_, attr) in zip(tokens, fields):
            setattr(resolved, attr, tok == "1")
        return False

    by_short = {short.lower(): attr for short, attr in fields + extra_fields}
    by_full = {attr.lower(): attr for _, attr in fields + extra_fields}
    if short_aliases:
        by_short.update({k.lower(): v for k, v in short_aliases.items()})

    if tokens and all("=" not in t for t in tokens):
        for _, attr in fields:
            setattr(resolved, attr, False)
        for tok in tokens:
            key = tok.lower()
            attr = by_short.get(key) or by_full.get(key)
            if attr:
                setattr(resolved, attr, True)
    else:
        for tok in tokens:
            if "=" not in tok:
                if len(tok) == len(fields) and set(tok) <= {"0", "1"}:
                    for ch, (_, attr) in zip(tok, fields):
                        setattr(resolved, attr, ch == "1")
                continue
            k, v = tok.split("=", 1)
            attr = by_short.get(k.strip().lower()) or by_full.get(k.strip().lower())
            if attr:
                setattr(resolved, attr, v.strip().lower() in ("1", "true", "on", "yes"))
    return False


def resolve_graft_config(base: GraftConfig | None = None) -> GraftConfig:
    """Return a resolved ``GraftConfig`` applying ``SATB_GRAFT_JSON``, ``SATB_GRAFT_FLAGS``, and ``SATB_B15_FLAGS``."""
    import os

    src = base or GraftConfig()
    resolved = GraftConfig(
        f1_type_a_dot=bool(src.f1_type_a_dot),
        f2_type_a_beam_meter=bool(src.f2_type_a_beam_meter),
        f3_type_b_shared_rest=bool(src.f3_type_b_shared_rest),
        f4_type_b_unprinted_rest=bool(src.f4_type_b_unprinted_rest),
        f5_type_c_voice_split=bool(src.f5_type_c_voice_split),
        f6_type_c_split_dur=bool(src.f6_type_c_split_dur),
        f7_pdf_multirest_mr1=bool(src.f7_pdf_multirest_mr1),
        b15_f2_lyric_dedup=bool(src.b15_f2_lyric_dedup),
        b15_f3_meter_clamp=bool(src.b15_f3_meter_clamp),
        b15_f4_type_d_overfull=bool(src.b15_f4_type_d_overfull),
        b15_f5_type_d_underfull=bool(src.b15_f5_type_d_underfull),
        b15_f6_tie_continuity=bool(src.b15_f6_tie_continuity),
        b15_f7_chord_tie_split=bool(src.b15_f7_chord_tie_split),
        graft_json_path=Path(src.graft_json_path) if src.graft_json_path else None,
    )
    env_json = os.environ.get("SATB_GRAFT_JSON", "").strip()
    if env_json:
        resolved.graft_json_path = Path(env_json)

    env_flags = os.environ.get("SATB_GRAFT_FLAGS", "").strip()
    if env_flags:
        killed = _apply_flag_env(resolved, env_flags, _GRAFT_FLAG_FIELDS, extra_fields=_B15_FLAG_FIELDS)
        if killed:
            for _, attr in _B15_FLAG_FIELDS:
                setattr(resolved, attr, False)

    env_b15 = os.environ.get("SATB_B15_FLAGS", "").strip()
    if env_b15:
        _apply_flag_env(
            resolved,
            env_b15,
            _B15_FLAG_FIELDS,
            short_aliases={f"F{i + 1}": attr for i, (_, attr) in enumerate(_B15_FLAG_FIELDS)},
        )
    return resolved


@dataclass
class SplitConfig:
    """Runtime options for the voice splitter."""

    template: LayoutTemplate = field(default_factory=LayoutTemplate.two_staff_satb)
    unison_mode: UnisonMode = UnisonMode.DUPLICATE_BOTH
    chord_mode: ChordMode = ChordMode.PITCH_RANK
    tenor_clef: TenorClef = TenorClef.TREBLE_8VB
    # Legacy: kept for API/config compatibility only. The splitter now matches
    # voices by ORDER within each staff bar (1st -> upper, 2nd -> lower), because
    # Audiveris numbers lower-staff voices 5, 6, 7... (BUG-1).
    voice_upper_ids: tuple[str, ...] = ("1",)
    voice_lower_ids: tuple[str, ...] = ("2",)
    # When True, middle pitches of 3+ note chords are flagged
    flag_middle_pitches: bool = True
    # Single-staff "solo" systems: "route_named" sends "solo tenor"/"solo alto"
    # ... to that voice and omits a plain "solo"; "omit" leaves every solo out.
    solo_policy: str = "route_named"
    # Batch 14: deterministic OMR bar-repair graft & recovery options.
    graft: GraftConfig = field(default_factory=GraftConfig)
    # Batch 18: 3+ note simultaneous chord routing (Option B monophonic default) + user overrides.
    divisi_mode: DivisiMode = DivisiMode.MONOPHONIC_AUTO
    divisi_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class ReviewFlag:
    """An ambiguous situation the user may want to inspect."""

    measure: int | None
    part_index: int
    offset: float | None
    reason: str
    detail: str = ""
    #: Batch 12: source page label (e.g. "page_03/page_03.mxl") and the MXL
    #: measure number (as written in the file) for flags merged from pages.
    source_page: str | None = None
    source_measure: str | None = None


@dataclass
class SplitResult:
    """Four single-voice music21 Scores plus review flags."""

    scores: dict[VoiceRole, object]  # music21.stream.Score
    flags: list[ReviewFlag] = field(default_factory=list)
    source_part_count: int = 0
    #: Batch 11: MuseScore file written next to *_SATB.musicxml (or None).
    mscz_path: object = None
    #: Batch 11: what happened with the .mscz export (always set by the writer).
    mscz_message: str = ""
    #: Batch 12: MXL measure number (string, as written) per bar position of
    #: the split page (index 0 = bar 1); used for REVIEW labels.
    source_measure_numbers: list = field(default_factory=list)
    #: Batch 13: bars joined inside this page (split by a system break), see
    #: split/joinbars.py, and the page's first/last bar info for joins across
    #: page files (export/merge.py).
    bar_joins: list = field(default_factory=list)
    edge_info: dict = field(default_factory=dict)
    #: Batch 18: 3+ note simultaneous chord routing choices (for GUI resolver & review).
    divisi_choices: list[DivisiChoice] = field(default_factory=list)
