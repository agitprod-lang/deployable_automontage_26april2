#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from timed_ai_illustrator import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate timed deterministic and AI illustration tables from the latest Approximate_string_matching run."
    )
    parser.add_argument("--run-dir", type=Path, help="Approximate_string_matching run directory to read.")
    parser.add_argument("--comparer", type=Path, help="Explicit comparer CSV override.")
    parser.add_argument("--edit-timeline", type=Path, help="Explicit 12_edit_timeline.csv override.")
    parser.add_argument("--html", type=Path, help="Explicit HTML reference override.")
    parser.add_argument("--output-dir", type=Path, help="Root output directory for timed_AI_illustrator runs.")
    parser.add_argument(
        "--keep-mode",
        choices=("auto", "xml_ready", "legacy_step2"),
        default="auto",
        help="Interpretation mode for the comparer Keep column.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_pipeline(
        run_dir=args.run_dir,
        comparer_path=args.comparer,
        edit_timeline_path=args.edit_timeline,
        html_path=args.html,
        output_dir=args.output_dir,
        keep_mode=args.keep_mode,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
