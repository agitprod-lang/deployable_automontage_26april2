#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.stages import run_stage_00


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 00: segment Groq transcript and insert silence rows.")
    parser.add_argument("--csv", type=Path, required=True, help="Groq transcript CSV.")
    parser.add_argument("--words", type=Path, help="Optional Groq word-level CSV.")
    parser.add_argument("--output", type=Path, required=True, help="Output working CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_00(args.csv, args.output, args.words)
    print(summary)


if __name__ == "__main__":
    main()
