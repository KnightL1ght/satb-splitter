"""Process test OMR outputs and write a quality report."""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy

from music21 import chord, converter, note, stream

from satb_splitter.export.writer import write_satb_files
from satb_splitter.model.config import LayoutTemplate, SplitConfig, StaffPair
from satb_splitter.split.engine import split_file, split_score

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "test_runs"


def note_count(score: stream.Score) -> int:
    return len(list(score.flatten().notes))


def summarize_score(path: Path) -> dict:
    s = converter.parse(str(path))
    if not isinstance(s, stream.Score):
        sc = stream.Score()
        sc.append(s)
        s = sc
    parts = list(s.parts)
    info = {
        "path": str(path),
        "parts": len(parts),
        "part_details": [],
        "total_notes": 0,
        "multi_voice_measures": 0,
        "chord_events": 0,
    }
    for i, p in enumerate(parts):
        notes = list(p.flatten().notes)
        ms = list(p.getElementsByClass(stream.Measure))
        mv = sum(1 for m in ms if m.hasVoices())
        ch = sum(1 for n in notes if isinstance(n, chord.Chord))
        info["part_details"].append(
            {
                "index": i,
                "name": p.partName,
                "measures": len(ms),
                "notes": len(notes),
                "multi_voice_measures": mv,
                "chords": ch,
            }
        )
        info["total_notes"] += len(notes)
        info["multi_voice_measures"] += mv
        info["chord_events"] += ch
    return info


def split_and_count(path: Path, out_dir: Path, basename: str, config: SplitConfig | None = None):
    result = split_file(path, config)
    written = write_satb_files(result, out_dir, basename)
    counts = {role: note_count(result.scores[role]) for role in result.scores}
    return result, written, counts


def hard_page_files() -> list[Path]:
    d = RUNS / "hard" / "page_pdf_omr"
    # Prefer ordered page sequence; include mvt splits in name order
    files = sorted(d.glob("page_*.mxl"))
    # page_01_clean before page_02; page_04.mvt1 before mvt2 — lexicographic works
    # except page_01_clean vs page_01 — only clean exists
    return files


def merge_voice_parts(parts: list[stream.Part]) -> stream.Part:
    """Concatenate parts end-to-end into one Part."""
    out = stream.Part()
    measure_num = 1
    for part in parts:
        ms = list(part.getElementsByClass(stream.Measure))
        if ms:
            for m in ms:
                nm = deepcopy(m)
                nm.number = measure_num
                measure_num += 1
                out.append(nm)
        else:
            # flat notes
            for el in part.flatten().notesAndRests:
                out.append(deepcopy(el))
    return out


def process_hard() -> dict:
    pages = hard_page_files()
    page_reports = []
    voice_parts: dict[str, list[stream.Part]] = {
        "Soprano": [],
        "Alto": [],
        "Tenor": [],
        "Bass": [],
    }
    total_flags = 0

    for path in pages:
        s = converter.parse(str(path))
        if not isinstance(s, stream.Score):
            sc = stream.Score()
            sc.append(s)
            s = sc
        n_parts = len(list(s.parts))

        # Choose template by part count
        if n_parts >= 4:
            cfg = SplitConfig(template=LayoutTemplate.four_staff_open())
        elif n_parts == 3:
            # Best-effort: top staff SA, middle ignored or T, bottom B — treat as
            # staff0=SA, staff1=TB-ish using only first two for SATB pair + 3rd as bass mono
            cfg = SplitConfig(
                template=LayoutTemplate(
                    name="three_staff_guess",
                    description="3-staff guess: SA / T mono / B mono",
                    pairs=[
                        StaffPair(0, "Soprano", "Alto"),
                        StaffPair(1, "Tenor", None),
                        StaffPair(2, "Bass", None),
                    ],
                )
            )
        else:
            cfg = SplitConfig(template=LayoutTemplate.two_staff_satb())

        try:
            result = split_score(s, cfg)
            total_flags += len(result.flags)
            for role in voice_parts:
                # extract part from score
                ps = list(result.scores[role].parts)
                if ps:
                    voice_parts[role].append(ps[0])
            page_reports.append(
                {
                    "file": path.name,
                    "parts": n_parts,
                    "template": cfg.template.name,
                    "counts": {r: note_count(result.scores[r]) for r in voice_parts},
                    "flags": len(result.flags),
                    "ok": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            page_reports.append(
                {
                    "file": path.name,
                    "parts": n_parts,
                    "ok": False,
                    "error": str(exc),
                }
            )

    # Write merged voices
    out_dir = RUNS / "hard" / "voices"
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_counts = {}
    for role, plist in voice_parts.items():
        if not plist:
            continue
        part = merge_voice_parts(plist)
        sc = stream.Score()
        sc.insert(0, part)
        fp = out_dir / f"PacePePamant_{role}.musicxml"
        sc.write("musicxml", fp=str(fp))
        merged_counts[role] = note_count(sc)

    return {
        "pages": page_reports,
        "merged_counts": merged_counts,
        "total_flags": total_flags,
        "mxl_count": len(pages),
    }


def main() -> None:
    lines: list[str] = []
    lines.append("# SATB pipeline test report")
    lines.append("")
    lines.append("Pipeline: PDF → Audiveris OMR → MusicXML → `satb-split`")
    lines.append("")

    # --- EASY ---
    lines.append("## 1. Easy — Happy Birthday (piano)")
    lines.append("")
    easy_mxl = RUNS / "easy" / "omr" / "HAPPY-BIRTHDAY_piano_intermediate2.mxl"
    easy = summarize_score(easy_mxl)
    lines.append(f"- Source PDF: `samples/easy/HAPPY-BIRTHDAY_piano_intermediate2.pdf`")
    lines.append(f"- OMR MusicXML: `{easy_mxl}`")
    lines.append(f"- Parts detected: **{easy['parts']}** (grand staff piano)")
    lines.append(f"- Total notes: **{easy['total_notes']}**")
    lines.append(f"- Multi-voice measures: {easy['multi_voice_measures']}")
    lines.append(f"- Chord events: {easy['chord_events']}")
    for p in easy["part_details"]:
        lines.append(
            f"  - part[{p['index']}] {p['name']!r}: {p['measures']} measures, "
            f"{p['notes']} notes, {p['chords']} chords"
        )

    res, written, counts = split_and_count(
        easy_mxl, RUNS / "easy" / "voices", "HappyBirthday"
    )
    lines.append(f"- Split flags: **{len(res.flags)}** (mostly 3+ pitch piano chords → middle flagged)")
    lines.append("- Voice note counts after split:")
    for role, n in counts.items():
        lines.append(f"  - {role}: {n} notes → `{written[role].name}`")
    lines.append("")
    lines.append(
        "**Assessment:** OMR succeeded cleanly (1 page). This is a **piano** score, not SATB. "
        "Treating RH/LH as SA/TB is a structural mismatch — S/A get melody-ish RH split, "
        "T/B get accompaniment LH. Usable for pipeline smoke-test only, not as real choral parts."
    )
    lines.append("")

    # --- NORMAL ---
    lines.append("## 2. Normal — Iubirea Ta e tot ce vor")
    lines.append("")
    normal_mxl = RUNS / "normal" / "omr" / "Iubirea Ta e tot ce vor.mxl"
    normal = summarize_score(normal_mxl)
    lines.append(f"- Source PDF: `samples/normal/Iubirea Ta e tot ce vor.pdf` (2 pages)")
    lines.append(f"- OMR MusicXML: `{normal_mxl}`")
    lines.append(f"- Parts detected: **{normal['parts']}**")
    lines.append(f"- Total notes: **{normal['total_notes']}**")
    lines.append(f"- Multi-voice measures: {normal['multi_voice_measures']}")
    lines.append(f"- Chord events: {normal['chord_events']}")
    for p in normal["part_details"]:
        lines.append(
            f"  - part[{p['index']}] {p['name']!r}: {p['measures']} measures, "
            f"{p['notes']} notes, {p['chords']} chords, multi-voice m={p['multi_voice_measures']}"
        )

    res, written, counts = split_and_count(
        normal_mxl, RUNS / "normal" / "voices", "IubireaTa"
    )
    lines.append(f"- Split flags: **{len(res.flags)}**")
    lines.append("- Voice note counts after split:")
    for role, n in counts.items():
        lines.append(f"  - {role}: {n} notes → `{written[role].name}`")
    lines.append("")
    lines.append(
        "**Assessment:** Full-book OMR export succeeded. 2 parts → classic SA/TB template applies. "
        "Open the four MusicXML files in MuseScore and compare against the PDF for pitch/rhythm "
        "cleanup (Audiveris had some rhythm warnings on sheet 1)."
    )
    lines.append("")

    # --- HARD ---
    lines.append("## 3. Hard — Pace pe pământ (18 pages)")
    lines.append("")
    lines.append(
        "- Source PDF: `samples/hard/Pace pe pamant_merge.pdf` (**18 pages**)"
    )
    lines.append(
        "- Full-book Audiveris export **failed**: sheets 1 and 6 crashed in TEXTS "
        "(`MetronomeInter` NPE on Romanian scripture lines misread as metronome marks)."
    )
    lines.append(
        "- Workaround: split PDF into per-page PDFs; white-out top band on pages 1 & 6; "
        "OMR each page; merge voice streams."
    )

    hard = process_hard()
    lines.append(f"- Per-page MusicXML files produced: **{hard['mxl_count']}**")
    lines.append(f"- Aggregate review flags across pages: **{hard['total_flags']}**")
    lines.append("- Merged voice note counts:")
    for role, n in hard["merged_counts"].items():
        lines.append(f"  - {role}: {n} notes → `PacePePamant_{role}.musicxml`")
    lines.append("")
    lines.append("### Per-page split summary")
    lines.append("")
    lines.append("| File | Parts | Template | S | A | T | B | Flags | OK |")
    lines.append("|------|------:|----------|--:|--:|--:|--:|------:|:--:|")
    for p in hard["pages"]:
        if not p.get("ok"):
            lines.append(
                f"| {p['file']} | {p.get('parts','?')} | — | — | — | — | — | — | FAIL |"
            )
            continue
        c = p["counts"]
        lines.append(
            f"| {p['file']} | {p['parts']} | {p['template']} | "
            f"{c.get('Soprano',0)} | {c.get('Alto',0)} | {c.get('Tenor',0)} | {c.get('Bass',0)} | "
            f"{p['flags']} | yes |"
        )
    lines.append("")
    lines.append(
        "**Assessment:** Hard score is multi-system choral with dense engraving. OMR quality "
        "will need heavy MuseScore cleanup. Per-page export is more robust than full-book. "
        "Pages with 3 parts used a guess template (SA / T / B). Pages with mvt1/mvt2 were "
        "exported as separate movements by Audiveris (layout discontinuity)."
    )
    lines.append("")

    # --- Conclusions ---
    lines.append("## Conclusions & next steps")
    lines.append("")
    lines.append("| Level | OMR | Split | Ready for MuseScore? |")
    lines.append("|-------|-----|-------|----------------------|")
    lines.append("| Easy | OK | OK (but piano≠SATB) | Smoke test only |")
    lines.append("| Normal | OK | OK | Yes — verify & edit |")
    lines.append("| Hard | Partial/workaround | OK per page | Yes with heavy cleanup |")
    lines.append("")
    lines.append("### Recommended product improvements")
    lines.append("")
    lines.append("1. **OMR wrapper**: per-page PDF OMR + auto white-out of top scripture/title band")
    lines.append("2. **Layout detector**: auto-pick 2-staff SA/TB vs 3/4-staff templates from OMR parts")
    lines.append("3. **Piano-vocal mode**: ignore piano staves (this easy sample is piano, not choir)")
    lines.append("4. **GUI review**: side-by-side PDF vs voice; jump to flagged measures")
    lines.append("5. **Optional better OMR path**: import MusicXML from Soundslice/PlayScore for hard scores")
    lines.append("")
    lines.append("### Output locations")
    lines.append("")
    lines.append(f"- Easy voices: `{RUNS / 'easy' / 'voices'}`")
    lines.append(f"- Normal voices: `{RUNS / 'normal' / 'voices'}`")
    lines.append(f"- Hard voices: `{RUNS / 'hard' / 'voices'}`")
    lines.append(f"- Hard page OMR: `{RUNS / 'hard' / 'page_pdf_omr'}`")

    report_path = RUNS / "REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {report_path}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
