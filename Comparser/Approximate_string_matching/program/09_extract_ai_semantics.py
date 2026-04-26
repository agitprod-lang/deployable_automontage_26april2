#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.semantic_enrichment import run_stage_09


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 09: AI semantic enrichment.")
    parser.add_argument("--input", type=Path, required=True, help="Stage-08 CSV.")
    parser.add_argument("--html", type=Path, help="Reference HTML.")
    parser.add_argument("--output", type=Path, required=True, help="Output CSV path.")
    parser.add_argument("--claude-api-key", help="Override ANTHROPIC_API_KEY.")
    parser.add_argument("--claude-model", help="Claude model for CTA/Zoom.")
    parser.add_argument("--claude-max-tokens", type=int, default=1200, help="Claude max tokens for CTA/Zoom.")
    parser.add_argument("--claude-batch-size", type=int, default=60, help="Claude batch size for CTA/Zoom.")
    parser.add_argument("--nouns-claude-model", help="Claude model for semantic extraction.")
    parser.add_argument("--nouns-claude-max-tokens", type=int, default=1500, help="Claude max tokens for semantics.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_stage_09(
        input_path=args.input,
        html_path=args.html,
        output_path=args.output,
        claude_api_key=args.claude_api_key,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_batch_size=args.claude_batch_size,
        nouns_claude_model=args.nouns_claude_model,
        nouns_claude_max_tokens=args.nouns_claude_max_tokens,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
