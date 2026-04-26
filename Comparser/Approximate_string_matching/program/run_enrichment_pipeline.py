#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

from lib.reference_features import run_stage_08
from lib.semantic_enrichment import run_stage_09


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run stages 08-09 starting from a stage-07 diagnostic CSV.")
    parser.add_argument("--input", type=Path, required=True, help="Stage-07 diagnostic CSV.")
    parser.add_argument("--html", type=Path, required=True, help="Reference HTML.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory. Defaults to the input CSV directory.",
    )
    parser.add_argument("--claude-api-key", help="Override ANTHROPIC_API_KEY.")
    parser.add_argument("--claude-model", help="Claude model for CTA/Zoom.")
    parser.add_argument("--claude-max-tokens", type=int, default=1200, help="Claude max tokens for CTA/Zoom.")
    parser.add_argument("--claude-batch-size", type=int, default=60, help="Claude batch size for CTA/Zoom.")
    parser.add_argument("--nouns-claude-model", help="Claude model for semantic extraction.")
    parser.add_argument("--nouns-claude-max-tokens", type=int, default=1500, help="Claude max tokens for semantics.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or args.input.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stage08_path = output_dir / "08_ref_features.csv"
    stage09_path = output_dir / "09_semantic_enriched.csv"

    summary = {
        "inputs": {
            "diagnostic_csv": str(args.input),
            "html": str(args.html),
        },
        "outputs": {
            "08": str(stage08_path),
            "09": str(stage09_path),
        },
        "stages": {},
    }
    summary["stages"]["08"] = run_stage_08(args.input, args.html, stage08_path)
    summary["stages"]["09"] = run_stage_09(
        input_path=stage08_path,
        html_path=args.html,
        output_path=stage09_path,
        claude_api_key=args.claude_api_key,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_batch_size=args.claude_batch_size,
        nouns_claude_model=args.nouns_claude_model,
        nouns_claude_max_tokens=args.nouns_claude_max_tokens,
    )
    summary_path = output_dir / "summary_enrichment.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
