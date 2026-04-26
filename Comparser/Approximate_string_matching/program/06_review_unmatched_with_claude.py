#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.constants import DEFAULT_CLAUDE_MODEL
from lib.stages import run_stage_06


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 06: review unmatched rows with Claude.")
    parser.add_argument("--input", type=Path, required=True, help="Input working CSV.")
    parser.add_argument("--html", type=Path, required=True, help="Reference HTML.")
    parser.add_argument("--output", type=Path, required=True, help="Output working CSV.")
    parser.add_argument("--model", default=DEFAULT_CLAUDE_MODEL, help="Claude model.")
    parser.add_argument("--max-tokens", type=int, default=1200, help="Claude max tokens.")
    parser.add_argument("--api-key", help="Override ANTHROPIC_API_KEY.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_06(
        args.input,
        args.html,
        args.output,
        model=args.model,
        max_tokens=args.max_tokens,
        api_key=args.api_key,
    )
    print(summary)


if __name__ == "__main__":
    main()
