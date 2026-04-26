#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.timeline_pipeline import run_stage_10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 10: report unmatched reference spans.")
    parser.add_argument("--input", type=Path, required=True, help="Stage-06 reviewed working CSV or stage-07 diagnostic CSV.")
    parser.add_argument("--html", type=Path, required=True, help="Reference HTML.")
    parser.add_argument("--output", type=Path, required=True, help="Output CSV path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_10(args.input, args.html, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
