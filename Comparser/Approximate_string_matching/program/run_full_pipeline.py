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
from lib.reference_features import run_stage_08
from lib.semantic_enrichment import run_stage_09
from lib.stages import run_full_pipeline as run_stage1_full_pipeline
from lib.timeline_pipeline import run_stage_10, run_stage_11, run_stage_12, run_stage_13


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Approximate String Matching full pipeline (00-13).")
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
    parser.add_argument("--claude-max-tokens", type=int, default=1200, help="Claude max tokens for stage 06 and CTA/Zoom.")
    parser.add_argument("--claude-batch-size", type=int, default=60, help="Claude batch size for CTA/Zoom.")
    parser.add_argument("--claude-api-key", help="Override ANTHROPIC_API_KEY.")
    parser.add_argument("--nouns-claude-model", help="Claude model for semantic extraction.")
    parser.add_argument("--nouns-claude-max-tokens", type=int, default=1500, help="Claude max tokens for semantics.")
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
    stage1_summary = run_stage1_full_pipeline(
        csv_path=csv_path,
        html_path=html_path,
        words_path=words_path,
        output_dir=args.output_dir,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_api_key=args.claude_api_key,
        allow_segment_fallback=args.allow_segment_fallback,
    )
    run_dir = Path(stage1_summary["summary_path"]).parent
    stage08_path = run_dir / "08_ref_features.csv"
    stage09_path = run_dir / "09_semantic_enriched.csv"
    stage10_path = run_dir / "10_ref_leftovers.csv"
    stage11_path = run_dir / "11_word_timeline.csv"
    stage12_path = run_dir / "12_edit_timeline.csv"
    stage13_annotations_path = run_dir / "13_precise_annotations.csv"
    stage13_candidates_path = run_dir / "13_illustration_candidates.csv"
    stage13_comparer_path = run_dir / "13_precise_comparer.csv"

    summary = dict(stage1_summary)
    summary["inputs"]["rush"] = str(rush_path)
    summary["outputs"]["08"] = str(stage08_path)
    summary["outputs"]["09"] = str(stage09_path)
    summary["outputs"]["10"] = str(stage10_path)
    summary["outputs"]["11"] = str(stage11_path)
    summary["outputs"]["12"] = str(stage12_path)
    summary["outputs"]["13_annotations"] = str(stage13_annotations_path)
    summary["outputs"]["13_candidates"] = str(stage13_candidates_path)
    summary["outputs"]["13"] = str(stage13_comparer_path)
    summary["stages"]["08"] = run_stage_08(Path(stage1_summary["step1_diagnostic_csv"]), html_path, stage08_path)
    summary["stages"]["09"] = run_stage_09(
        input_path=stage08_path,
        html_path=html_path,
        output_path=stage09_path,
        claude_api_key=args.claude_api_key,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_batch_size=args.claude_batch_size,
        nouns_claude_model=args.nouns_claude_model,
        nouns_claude_max_tokens=args.nouns_claude_max_tokens,
    )
    summary["stages"]["10"] = run_stage_10(Path(stage1_summary["outputs"]["06"]), html_path, stage10_path)
    summary["stages"]["11"] = run_stage_11(Path(stage1_summary["outputs"]["06"]), stage11_path, words_path=words_path)
    summary["stages"]["12"] = run_stage_12(stage11_path, stage12_path)
    summary["stages"]["13"] = run_stage_13(
        enriched_input_path=stage09_path,
        timeline_input_path=stage12_path,
        output_path=stage13_comparer_path,
        annotations_output_path=stage13_annotations_path,
        illustration_candidates_output_path=stage13_candidates_path,
    )
    summary["enriched_csv"] = str(stage09_path)
    summary["ref_leftovers_csv"] = str(stage10_path)
    summary["word_timeline_csv"] = str(stage11_path)
    summary["word_timeline_json"] = str(stage11_path.with_suffix(".json"))
    summary["edit_timeline_csv"] = str(stage12_path)
    summary["edit_timeline_json"] = str(stage12_path.with_suffix(".json"))
    summary["precise_annotations_csv"] = str(stage13_annotations_path)
    summary["illustration_candidates_csv"] = str(stage13_candidates_path)
    summary["precise_comparer_csv"] = str(stage13_comparer_path)
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
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
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
