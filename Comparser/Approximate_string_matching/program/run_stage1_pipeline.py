#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.constants import DEFAULT_CLAUDE_MODEL, DEFAULT_OUTPUT_ROOT
from lib.discovery import (
    discover_latest_groq_csv,
    discover_latest_html,
    discover_latest_rush,
    discover_words_for_csv,
)
from lib.stages import run_full_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Approximate String Matching v1 stage-1 pipeline.")
    parser.add_argument("--csv", type=Path, help="Groq transcript CSV. Defaults to latest Groq output CSV.")
    parser.add_argument("--html", type=Path, help="Reference HTML. Defaults to latest Swisser HTML.")
    parser.add_argument("--words", type=Path, help="Optional word-level Groq CSV.")
    parser.add_argument(
        "--allow-segment-fallback",
        action="store_true",
        help="Allow running without *_words.csv and fall back to coarse segment timing.",
    )
    parser.add_argument(
        "--rush",
        type=Path,
        help="Rush video used only for default discovery/reporting. Defaults to latest Swisser Rush file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Output root directory; a timestamped run subdirectory will be created inside it.",
    )
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL, help="Claude model.")
    parser.add_argument("--claude-max-tokens", type=int, default=1200, help="Claude max tokens.")
    parser.add_argument("--claude-api-key", help="Override ANTHROPIC_API_KEY.")
    return parser.parse_args()


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, Path, Path | None, Path]:
    rush_path = args.rush or discover_latest_rush()
    html_path = args.html or discover_latest_html()
    csv_path = args.csv or discover_latest_groq_csv()
    words_path = args.words if args.words else discover_words_for_csv(csv_path)
    return csv_path, html_path, words_path, rush_path


def main() -> None:
    args = parse_args()
    csv_path, html_path, words_path, rush_path = resolve_inputs(args)
    print(
        json.dumps(
            {
                "resolved_inputs": {
                    "rush": str(rush_path),
                    "html": str(html_path),
                    "csv": str(csv_path),
                    "words": str(words_path) if words_path else "",
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    summary = run_full_pipeline(
        csv_path=csv_path,
        html_path=html_path,
        words_path=words_path,
        output_dir=args.output_dir,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_api_key=args.claude_api_key,
        allow_segment_fallback=args.allow_segment_fallback,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
