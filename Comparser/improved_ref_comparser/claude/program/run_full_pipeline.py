#!/usr/bin/env python3
"""
run_full_pipeline.py  —  CLI wrapper called by GROQ_WITH_HTML_PIPE.py.

Drop-in replacement for:
    Approximate_string_matching/program/run_full_pipeline.py

Runs ONLY the semantic pipeline stages 00-13 via pipeline.run_pipeline()
and writes summary.json with the keys the pipe needs
(step1_xml_ready_csv, step1_diagnostic_csv, precise_comparer_csv,
precise_annotations_csv, illustration_candidates_csv).

No post-processing (stitch / trim / VAD), no intermediate XML generation.
Those produce visual-transform artifacts (wrong sequence width/height,
rotation metadata) that the pipe's downstream XML generator re-creates
from scratch anyway. Keep the wrapper to segmentation/elimination only.

Groq transcription is NOT run here; the pipe runs it upstream and passes
pre-computed --csv and --words.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pipeline import run_pipeline  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Improved comparser semantic pipeline (stages 00-13 only). "
                    "Drop-in replacement for run_full_pipeline.py called by GROQ_WITH_HTML_PIPE.py."
    )
    p.add_argument("--csv",        type=Path, required=True, help="Groq transcript CSV.")
    p.add_argument("--html",       type=Path, required=True, help="Reference HTML.")
    p.add_argument("--words",      type=Path, required=True, help="Word-level Groq CSV.")
    p.add_argument("--rush",       type=Path, required=True, help="Rush video (recorded in summary.json only).")
    p.add_argument("--output-dir", type=Path, required=True, help="Output root; timestamped subdir is created inside.")
    p.add_argument(
        "--align", action="store_true",
        help="Run wav2vec2 forced alignment on the rush and use its word CSV "
             "instead of --words (fixes gaps in Groq's word-level timing).",
    )
    p.add_argument(
        "--aligned-words-out", type=Path, default=None,
        help="Destination for the aligned words CSV. Defaults to <words stem>_aligned.csv "
             "next to the original --words file.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    words_path = args.words
    if args.align:
        from align_words import align_file
        aligned_out = args.aligned_words_out or (
            args.words.with_name(args.words.stem + "_aligned.csv")
        )
        align_file(args.csv, args.rush, aligned_out, groq_words_csv=args.words)
        words_path = aligned_out
    summary = run_pipeline(
        csv_path=args.csv,
        html_path=args.html,
        words_path=words_path,
        output_dir=args.output_dir,
    )
    run_dir = Path(summary["summary_path"]).parent
    print(f"  Stages 00-13 complete. Run dir: {run_dir}")


if __name__ == "__main__":
    main()
