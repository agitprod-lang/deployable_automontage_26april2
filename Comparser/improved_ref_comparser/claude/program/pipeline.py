#!/usr/bin/env python3
"""
pipeline.py  —  improved comparser core

Drop-in replacement for:
  Approximate_string_matching/program/run_full_pipeline.py

Same CLI interface, same summary.json keys — so GROQ_WITH_HTML_PIPE.py needs
only one path change to use this improved version:

    APPROXIMATE_FULL_PIPELINE_SCRIPT = Path(
        "~/Desktop/code/deployable_auto-montage/Comparser/improved_ref_comparser/program/pipeline.py"
    )

Importable function for ref_comparser.py:

    from pipeline import run_pipeline
    summary = run_pipeline(csv_path, html_path, words_path, output_dir)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Inject the comparser lib into sys.path.
# When improving the comparser, copy lib/ into this directory and it will
# shadow the original automatically (local path is inserted first).
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_ORIGINAL_LIB = (
    Path("~/Desktop/code/deployable_auto-montage/Comparser/Approximate_string_matching/program")
    .expanduser()
    .resolve()
)

for _p in (_ORIGINAL_LIB, _HERE):
    s = str(_p)
    if s in sys.path:
        sys.path.remove(s)
    sys.path.insert(0, s)

from lib.constants import DEFAULT_CLAUDE_MODEL, DEFAULT_OUTPUT_ROOT
from lib.discovery import (
    discover_latest_groq_csv,
    discover_latest_html,
    discover_latest_rush,
    discover_words_for_csv,
)
from lib.reference_features import run_stage_08
from lib.semantic_enrichment import run_stage_09
from lib.stages import run_full_pipeline as _run_stage1
from lib.timeline_pipeline import run_stage_10, run_stage_11, run_stage_12, run_stage_13

# Default output goes to this project's output/, not the original's
_DEFAULT_OUTPUT_ROOT = _HERE.parent / "output"


# ---------------------------------------------------------------------------
# Core pipeline function (importable)
# ---------------------------------------------------------------------------

def run_pipeline(
    csv_path: Path,
    html_path: Path,
    words_path: Path | None,
    output_dir: Path,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    claude_max_tokens: int = 1200,
    claude_api_key: str | None = None,
    claude_batch_size: int = 60,
    nouns_claude_model: str | None = None,
    nouns_claude_max_tokens: int = 1500,
    allow_segment_fallback: bool = False,
) -> dict:
    """
    Run the full comparser pipeline (stages 00-13).

    Returns a summary dict with keys compatible with GROQ_WITH_HTML_PIPE.py:
      step1_xml_ready_csv, step1_diagnostic_csv, precise_comparer_csv,
      precise_annotations_csv, illustration_candidates_csv, summary_path, …
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    stage1_summary = _run_stage1(
        csv_path=csv_path,
        html_path=html_path,
        words_path=words_path,
        output_dir=output_dir,
        claude_model=claude_model,
        claude_max_tokens=claude_max_tokens,
        claude_api_key=claude_api_key,
        allow_segment_fallback=allow_segment_fallback,
    )
    run_dir = Path(stage1_summary["summary_path"]).parent

    stage08_path              = run_dir / "08_ref_features.csv"
    stage09_path              = run_dir / "09_semantic_enriched.csv"
    stage10_path              = run_dir / "10_ref_leftovers.csv"
    stage11_path              = run_dir / "11_word_timeline.csv"
    stage12_path              = run_dir / "12_edit_timeline.csv"
    stage13_annotations_path  = run_dir / "13_precise_annotations.csv"
    stage13_candidates_path   = run_dir / "13_illustration_candidates.csv"
    stage13_comparer_path     = run_dir / "13_precise_comparer.csv"

    summary = dict(stage1_summary)
    summary["outputs"]["08"] = str(stage08_path)
    summary["outputs"]["09"] = str(stage09_path)
    summary["outputs"]["10"] = str(stage10_path)
    summary["outputs"]["11"] = str(stage11_path)
    summary["outputs"]["12"] = str(stage12_path)
    summary["outputs"]["13_annotations"] = str(stage13_annotations_path)
    summary["outputs"]["13_candidates"]  = str(stage13_candidates_path)
    summary["outputs"]["13"]             = str(stage13_comparer_path)

    summary["stages"]["08"] = run_stage_08(
        Path(stage1_summary["step1_diagnostic_csv"]), html_path, stage08_path
    )
    summary["stages"]["09"] = run_stage_09(
        input_path=stage08_path,
        html_path=html_path,
        output_path=stage09_path,
        claude_api_key=claude_api_key,
        claude_model=claude_model,
        claude_max_tokens=claude_max_tokens,
        claude_batch_size=claude_batch_size,
        nouns_claude_model=nouns_claude_model,
        nouns_claude_max_tokens=nouns_claude_max_tokens,
    )
    summary["stages"]["10"] = run_stage_10(
        Path(stage1_summary["outputs"]["06"]), html_path, stage10_path
    )
    summary["stages"]["11"] = run_stage_11(
        Path(stage1_summary["outputs"]["06"]), stage11_path, words_path=words_path
    )
    summary["stages"]["12"] = run_stage_12(stage11_path, stage12_path)
    summary["stages"]["13"] = run_stage_13(
        enriched_input_path=stage09_path,
        timeline_input_path=stage12_path,
        output_path=stage13_comparer_path,
        annotations_output_path=stage13_annotations_path,
        illustration_candidates_output_path=stage13_candidates_path,
    )

    # Keys required by GROQ_WITH_HTML_PIPE.py
    summary["precise_comparer_csv"]       = str(stage13_comparer_path)
    summary["precise_annotations_csv"]    = str(stage13_annotations_path)
    summary["illustration_candidates_csv"] = str(stage13_candidates_path)
    summary["enriched_csv"]               = str(stage09_path)
    summary["ref_leftovers_csv"]          = str(stage10_path)
    summary["word_timeline_csv"]          = str(stage11_path)
    summary["word_timeline_json"]         = str(stage11_path.with_suffix(".json"))
    summary["edit_timeline_csv"]          = str(stage12_path)
    summary["edit_timeline_json"]         = str(stage12_path.with_suffix(".json"))
    summary["precise_annotations_csv"]   = str(stage13_annotations_path)
    summary["illustration_candidates_csv"] = str(stage13_candidates_path)
    summary["precise_comparer_csv"]       = str(stage13_comparer_path)

    summary_path = run_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI  (same interface as run_full_pipeline.py — drop-in for GROQ_WITH_HTML_PIPE)
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Improved comparser pipeline (stages 00-13). "
                    "Drop-in replacement for run_full_pipeline.py."
    )
    parser.add_argument("--csv",   type=Path, help="Groq transcript CSV.")
    parser.add_argument("--html",  type=Path, help="Reference HTML.")
    parser.add_argument("--words", type=Path, help="Word-level Groq CSV.")
    parser.add_argument(
        "--allow-segment-fallback", action="store_true",
        help="Run without *_words.csv using coarse segment timing.",
    )
    parser.add_argument("--rush",  type=Path, help="Rush video (metadata only).")
    parser.add_argument(
        "--output-dir", type=Path, default=_DEFAULT_OUTPUT_ROOT,
        help="Output root; a timestamped subdirectory is created inside.",
    )
    parser.add_argument("--claude-model",       default=DEFAULT_CLAUDE_MODEL)
    parser.add_argument("--claude-max-tokens",  type=int, default=1200)
    parser.add_argument("--claude-batch-size",  type=int, default=60)
    parser.add_argument("--claude-api-key",     default=None)
    parser.add_argument("--nouns-claude-model", default=None)
    parser.add_argument("--nouns-claude-max-tokens", type=int, default=1500)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    rush_path  = args.rush  or discover_latest_rush()
    html_path  = args.html  or discover_latest_html()
    csv_path   = args.csv   or discover_latest_groq_csv()
    words_path = args.words if args.words else discover_words_for_csv(csv_path)

    summary = run_pipeline(
        csv_path=csv_path,
        html_path=html_path,
        words_path=words_path,
        output_dir=args.output_dir,
        claude_model=args.claude_model,
        claude_max_tokens=args.claude_max_tokens,
        claude_api_key=args.claude_api_key,
        claude_batch_size=args.claude_batch_size,
        nouns_claude_model=args.nouns_claude_model,
        nouns_claude_max_tokens=args.nouns_claude_max_tokens,
        allow_segment_fallback=args.allow_segment_fallback,
    )
    summary["inputs"] = summary.get("inputs", {})
    summary["inputs"]["rush"] = str(rush_path)

    print(json.dumps({"resolved_inputs": {
        "rush":  str(rush_path),
        "html":  str(html_path),
        "csv":   str(csv_path),
        "words": str(words_path) if words_path else "",
    }}, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
