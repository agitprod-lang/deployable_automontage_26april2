#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.timeline_pipeline import run_stage_13


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 13: enrich the precise timeline and export final comparer.")
    parser.add_argument("--input", type=Path, required=True, help="Stage-09 semantic enriched CSV.")
    parser.add_argument("--timeline", type=Path, required=True, help="Stage-12 edit timeline CSV.")
    parser.add_argument("--output", type=Path, required=True, help="Final precise comparer CSV.")
    parser.add_argument("--annotations-output", type=Path, required=True, help="Precise annotations CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_13(
        enriched_input_path=args.input,
        timeline_input_path=args.timeline,
        output_path=args.output,
        annotations_output_path=args.annotations_output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
