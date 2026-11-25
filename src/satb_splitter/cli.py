"""Command-line interface: satb-split."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from satb_splitter.export.writer import write_satb_files
from satb_splitter.model.config import (
    DivisiMode,
    LayoutTemplate,
    SplitConfig,
    UnisonMode,
    TenorClef,
)
from satb_splitter.split.engine import split_file


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="satb-split",
        description=(
            "Split a choral MusicXML score into separate "
            "Soprano, Alto, Tenor, and Bass MusicXML files."
        ),
    )
    p.add_argument(
        "input",
        type=Path,
        help="Input MusicXML file (.musicxml / .xml / .mxl)",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("satb_out"),
        help="Directory for the four voice files (default: satb_out)",
    )
    p.add_argument(
        "-t",
        "--template",
        default="auto",
        choices=[
            "auto",
            "two_staff_satb",
            "three_staff_satb",
            "four_staff_open",
            "single_staff_sa",
        ],
        help=(
            "Staff layout template (default: auto — detect from part count/names: "
            "2-staff SA/TB, 3-staff SA/T/B, 4-staff open)"
        ),
    )
    p.add_argument(
        "--template-json",
        type=Path,
        default=None,
        help="Custom layout template JSON (overrides --template)",
    )
    p.add_argument(
        "--unison",
        default="duplicate_both",
        choices=[m.value for m in UnisonMode],
        help="How to treat a single shared note on a two-voice staff",
    )
    p.add_argument(
        "--divisi",
        default=DivisiMode.MONOPHONIC_AUTO.value,
        choices=[m.value for m in DivisiMode],
        help=(
            "How to handle 3+ note simultaneous chords (default: monophonic_auto — "
            "1 note per voice; or keep_divisi, upper_only, lower_only)"
        ),
    )
    p.add_argument(
        "--divisi-overrides",
        type=Path,
        default=None,
        help="Optional JSON file mapping 'measure|upper_role|offset' keys to chord overrides",
    )
    p.add_argument(
        "--tenor-clef",
        default="treble_8vb",
        choices=[c.value for c in TenorClef],
        help="Clef to use for the Tenor part (default: treble_8vb)",
    )
    p.add_argument(
        "--basename",
        default=None,
        help="Filename stem (default: input file stem)",
    )
    p.add_argument(
        "--skip-empty",
        action="store_true",
        help="Do not write voice files that contain no notes",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print errors",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from satb_splitter.omr_cli import make_stdio_safe

    make_stdio_safe()
    args = build_parser().parse_args(argv)
    input_path: Path = args.input

    if not input_path.is_file():
        print(f"error: input not found: {input_path}", file=sys.stderr)
        return 1

    auto_layout = False
    if args.template_json is not None:
        template = LayoutTemplate.load_json(args.template_json)
    elif args.template == "auto":
        auto_layout = True
        template = LayoutTemplate.two_staff_satb()  # placeholder until detect
    else:
        template = LayoutTemplate.by_name(args.template)

    divisi_overrides: dict[str, str] = {}
    if args.divisi_overrides is not None:
        if not args.divisi_overrides.is_file():
            print(f"error: divisi overrides not found: {args.divisi_overrides}", file=sys.stderr)
            return 1
        divisi_overrides = {
            str(k): str(v)
            for k, v in json.loads(args.divisi_overrides.read_text(encoding="utf-8")).items()
        }

    config = SplitConfig(
        template=template,
        unison_mode=UnisonMode(args.unison),
        tenor_clef=TenorClef(args.tenor_clef),
        divisi_mode=DivisiMode(args.divisi),
        divisi_overrides=divisi_overrides,
    )

    try:
        result = split_file(input_path, config, auto_layout=auto_layout)
    except Exception as exc:  # noqa: BLE001 — surface parse errors to CLI
        print(f"error: failed to split {input_path}: {exc}", file=sys.stderr)
        return 2

    # Resolve displayed template name
    layout_detail = None
    for fl in result.flags:
        if fl.reason == "layout_auto":
            layout_detail = fl.detail
            break
    template_name = (
        f"auto ({layout_detail})" if layout_detail else config.template.name
    )

    basename = args.basename or input_path.stem
    written = write_satb_files(
        result,
        args.output_dir,
        basename,
        skip_empty=args.skip_empty,
    )

    if not args.quiet:
        print(f"Source parts: {result.source_part_count}")
        print(f"Template:     {template_name}")
        print(f"Unison mode:  {config.unison_mode.value}")
        print(f"Divisi mode:  {config.divisi_mode.value}")
        print(f"Wrote {len(written)} file(s) to {args.output_dir.resolve()}:")
        for role, path in written.items():
            print(f"  {role:8s}  {path}")
        other_flags = [f for f in result.flags if f.reason != "layout_auto"]
        if other_flags:
            print(f"\nReview flags ({len(other_flags)}):")
            for fl in other_flags:
                loc = f"m{fl.measure}" if fl.measure is not None else "m?"
                print(f"  [{loc} part{fl.part_index}] {fl.reason}: {fl.detail}")
        else:
            print("\nNo review flags.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
