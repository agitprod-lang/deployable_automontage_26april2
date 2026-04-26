#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.stages import run_stage_07


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 07: export compatibility CSV.")
    parser.add_argument("--input", type=Path, required=True, help="Input working CSV.")
    parser.add_argument("--output", type=Path, required=True, help="Output compatibility CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_07(args.input, args.output)
    print(summary)


if __name__ == "__main__":
    main()
