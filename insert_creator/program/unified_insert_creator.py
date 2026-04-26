#!/usr/bin/env python3
"""Run the individual insert_creator generators in sequence."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Mapping, Sequence


PROJECT_DIR = Path(__file__).resolve().parent
CODE_BASE = PROJECT_DIR.parent.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"


def run_step(name: str, command: Sequence[str]) -> None:
    print(f"\n========== {name} ==========")
    try:
        subprocess.run(command, cwd=PROJECT_DIR, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"\n❌ {name} failed with exit code {exc.returncode}.")
        raise SystemExit(exc.returncode)


def find_latest_main_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [path for path in directory.rglob("*comparison.csv") if path.is_file()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No main *_comparison.csv files found in {directory}")
    return candidates[0]


def default_python_bin() -> str:
    preferred = shutil.which("python3.11")
    if preferred:
        return preferred
    return sys.executable


def load_csv(path: Path) -> tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise RuntimeError(f"{path} has no rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, name in enumerate(header):
        mapping[name.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' missing.")
    return header_map[key]


def split_titles(value: str | None) -> List[str]:
    if not value:
        return []
    return [fragment.strip().strip('"').strip() for fragment in value.split("|") if fragment.strip().strip('"').strip()]


def normalize_title_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = "".join(char for char in normalized if char.isascii())
    filtered = []
    for char in ascii_only:
        if char.isalnum() or char.isspace():
            filtered.append(char)
        else:
            filtered.append(" ")
    return " ".join("".join(filtered).split())


def collect_ransom_title_entries(rows: Sequence[Sequence[str]], header_map: Mapping[str, int]) -> List[dict[str, object]]:
    value_idx = require_column(header_map, "Titles")
    transcript_idx = require_column(header_map, "Transcript #")
    entries: List[dict[str, object]] = []
    next_id = 1
    for row_number, row in enumerate(rows, start=1):
        if value_idx >= len(row):
            continue
        transcript_number = row[transcript_idx].strip() if transcript_idx < len(row) else ""
        for raw_value in split_titles(row[value_idx]):
            sanitized = normalize_title_text(raw_value)
            if not sanitized:
                continue
            entries.append(
                {
                    "id": next_id,
                    "row_index": row_number,
                    "transcript_number": transcript_number or None,
                    "title": sanitized,
                }
            )
            next_id += 1
    return entries


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run all insert_creator generators in order.")
    parser.add_argument(
        "--python-bin",
        default=default_python_bin(),
        help="Python interpreter to use for the generator scripts (default: python3.11 when available).",
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Explicit comparer CSV to feed into all generator steps that support --input-csv.",
    )
    parser.add_argument(
        "--timing-manifest",
        type=Path,
        help="Canonical timed_AI_illustrator manifest CSV used as the precise timing source for final staging.",
    )
    parser.add_argument(
        "--downloader-metadata",
        type=Path,
        help="Insert_downloader metadata JSON used by the final stager for article/video/tweet assets.",
    )
    parser.add_argument(
        "--downloader-output-dir",
        type=Path,
        help="Insert_downloader output folder used by the final stager to locate raw assets.",
    )
    parser.add_argument(
        "--paper-output-dir",
        type=Path,
        help="Paper article animator output folder used by the final stager to locate animated title cards.",
    )
    parser.add_argument(
        "--clean-insert-dir",
        action="store_true",
        help="Clear the destination Insert directory before final staging.",
    )
    parser.add_argument(
        "--ransom-titles-only",
        action="store_true",
        help="Use transparent ransom-title videos for title inserts and skip standard title video staging.",
    )
    args = parser.parse_args(argv)

    python_bin = args.python_bin
    input_csv = (args.input_csv.expanduser() if args.input_csv else find_latest_main_comparison_csv(COMPARER_OUTPUT_DIR))
    input_csv = input_csv.resolve()
    print(f"Using comparer CSV: {input_csv}")
    header, rows = load_csv(input_csv)
    header_map = build_header_map(header)
    timing_manifest = args.timing_manifest.expanduser().resolve() if args.timing_manifest else None
    if timing_manifest is not None:
        print(f"Using timing manifest: {timing_manifest}")

    mentions_command = [python_bin, "number_social_punctuation_mention_illustration.py", "--input-csv", str(input_csv), "--no-move"]
    if timing_manifest is not None:
        mentions_command.extend(["--timing-manifest", str(timing_manifest)])

    bold_command = [python_bin, "bold_sentence.py", "--input-csv", str(input_csv)]
    if timing_manifest is not None:
        bold_command.extend(["--timing-manifest", str(timing_manifest)])

    list_command = [python_bin, "list_maker.py", "--input-csv", str(input_csv)]
    if timing_manifest is not None:
        list_command.extend(["--timing-manifest", str(timing_manifest)])

    renamer_command = [python_bin, "insert_created_renamer.py", "--input-csv", str(input_csv)]
    if timing_manifest is not None:
        renamer_command.extend(["--timing-manifest", str(timing_manifest)])
    if args.downloader_metadata:
        renamer_command.extend(["--downloader-metadata", str(args.downloader_metadata.expanduser().resolve())])
    if args.downloader_output_dir:
        renamer_command.extend(["--downloader-output-dir", str(args.downloader_output_dir.expanduser().resolve())])
    if args.paper_output_dir:
        renamer_command.extend(["--paper-output-dir", str(args.paper_output_dir.expanduser().resolve())])
    if args.clean_insert_dir:
        renamer_command.append("--clean-insert-dir")
    if args.ransom_titles_only:
        renamer_command.append("--skip-standard-title-videos")

    title_command = [python_bin, "create_title.py", "--input-csv", str(input_csv)]
    if timing_manifest is not None:
        title_command.extend(["--timing-manifest", str(timing_manifest)])

    ransom_entries = collect_ransom_title_entries(rows, header_map)

    steps: List[tuple[str, List[str]]] = []
    if not args.ransom_titles_only:
        steps.append(("create_title", title_command))
    steps.extend([
        ("create_noun", [python_bin, "create_noun.py", "--input-csv", str(input_csv)]),
        ("animate_noun", [python_bin, "animate_noun.py"]),
        ("create_institution", [python_bin, "create_institution.py", "--input-csv", str(input_csv)]),
        ("institution_animator", [python_bin, "institution_animator.py"]),
        ("create_calendar", [python_bin, "create_calendar.py", "--input-csv", str(input_csv)]),
        ("create_money", [python_bin, "create_money.py", "--input-csv", str(input_csv)]),
        ("percent_viewer", [python_bin, "percent_viewer.py", "--input-csv", str(input_csv)]),
        ("number_social_punctuation_mention_illustration", mentions_command),
        ("add_text_to_animation", [python_bin, "add_text_to_animation.py", "--input-csv", str(input_csv)]),
        ("list_maker", list_command),
        ("bold_sentence", bold_command),
        ("create_3d_location", [python_bin, "3D_location.py", "--input-csv", str(input_csv)]),
        ("quote_higlight", [python_bin, "quote_higlight.py", "--input-csv", str(input_csv)]),
    ])

    for name, command in steps:
        run_step(name, command)

    print(f"\n========== title_video_creator ==========")
    if not ransom_entries:
        print("No sanitized ransom titles found.")
    else:
        print(f"Generating transparent title MOVs per title: {len(ransom_entries)}")
        for entry in ransom_entries:
            command = [
                python_bin,
                "title_creator/program/title_video_creator.py",
                "--title-text",
                str(entry["title"]),
                "--source-csv",
                str(input_csv),
                "--title-id",
                str(entry["id"]),
                "--row-index",
                str(entry["row_index"]),
                "--append-manifest",
            ]
            transcript_number = entry.get("transcript_number")
            if transcript_number:
                command.extend(["--transcript-number", str(transcript_number)])
            run_step(f"title_video_creator #{entry['id']:03d}", command)

    run_step("insert_created_renamer", renamer_command)

    print("\n✅ unified_insert_creator.py completed successfully.")


if __name__ == "__main__":
    main()
