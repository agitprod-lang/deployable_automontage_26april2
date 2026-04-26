#!/usr/bin/env python3
"""Groq pipeline variant that consumes Swisser HTML references for inserts."""

from __future__ import annotations

import argparse
import csv
import html as _html_module
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import json
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse, parse_qs, unquote


PYTHON_BIN = os.environ.get("PIPE_PYTHON_BIN", "python3.11")

# Groq transcription paths.
GROQ_VIDEO_TO_AUDIO_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/groq/program/video_to_audio.py")
GROQ_NOCLAP_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/groq/program/groq_noclap_csv_maker.py")
GROQ_CLAP_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/groq/program/groq_clap_csv_maker.py")
GROQ_VAD_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/groq/program/groq_vad_csv_maker.py")
GROQ_CLAP_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/groq/output/post_clap_output")
GROQ_NOCLAP_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/groq/output/no_clap_output")
GROQ_AUDIO_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/groq/output/audio")
GROQ_PRECISE_PLACER_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/precise_placer/program/groq_precise_placer.py")
APPROXIMATE_FULL_PIPELINE_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/Comparser/improved_ref_comparser/claude/program/run_full_pipeline.py"
)
TIMED_AI_ILLUSTRATOR_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/Comparser/timed_AI_illustrator/program/run_timed_ai_illustrator.py"
)
APPROXIMATE_STAGE1_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Comparser/improved_ref_comparser/claude/output")
TIMED_AI_ILLUSTRATOR_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Comparser/timed_AI_illustrator/output")
STEP1_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Comparser/output/first_comparser_output")

# Universal HTML + insert tooling.
UNIVERSAL_PIPE_DIR = Path("~/Desktop/code/deployable_auto-montage/swisser/Universal_pipe")
UNIVERSAL_RUSH_DIR = UNIVERSAL_PIPE_DIR / "Rush"
UNIVERSAL_INSERT_DIR = UNIVERSAL_PIPE_DIR / "Insert"
UNIVERSAL_HTML_DIR = UNIVERSAL_PIPE_DIR / "html"
DISPERSION_REFERENCE_DIR = Path("~/Downloads/dispersion")

INSERT_DOWNLOADER_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/program/unified_downloader.py")
CUT_VIDEO_WITH_TIMECODE_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/program/cut_video_with_timecode/cut_video_with_timecode_0.py")
URL_TO_VIDEO_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/program/screen_url/program/url_to_video_pipe.py")
WEBSITE_SCREENSHOT_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/program/tweet_screenshot_1.py")
MOSAIC_VIDEO_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/program/tweetcapture/tweet_screen_to_blur_mosaic_video.py")
INSERT_DOWNLOADER_INPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/input")
INSERT_DOWNLOADER_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Insert_downloader/output")
INSERT_EDITOR_OUTPUT_ROOT = Path("~/Desktop/code/deployable_auto-montage/Insert_editor/output")
PAPER_INSERT_ANIMATOR_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/Insert_editor/program/animator_for_paper_articles_left_side.py"
)

UNIVERSAL_GENERATOR_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/xml_editor_after_comparser/program/universal_generate_premiere_xml.py"
)
UNIVERSAL_GENERATOR_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/xml_editor_after_comparser/output")
UNIFIED_INSERT_CREATOR_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/insert_creator/program/unified_insert_creator.py")
PROGRAM6_UNIVERSAL_SCRIPT = Path("~/Desktop/code/deployable_auto-montage/xml_insertor/program/program6_universal.py")
XML_INSERTOR_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/xml_insertor/output")
COMPARER_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/Comparser/output/second_comparser_output")
FACE_ZOOM_REPLACEMENT_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/program/identify_face_and_zoom_on_segment.py"
)
PREMIERE_XML_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/premiere_automator/output/xml")
FFMPEG_PROGRAM_DIR = Path("~/Desktop/code/deployable_auto-montage/ffmpeger_otio_video_maker/program")
XML_TO_OTIO_SCRIPT = FFMPEG_PROGRAM_DIR / "xml_to_otio_converter.py"
FFMPEG_CREATOR_SCRIPT = FFMPEG_PROGRAM_DIR / "ffmpeg_video_creator.py"
CREATE_VIDEO_FROM_XML_SCRIPT = FFMPEG_PROGRAM_DIR / "create_video_from_xml.py"
FFMPEG_OUTPUT_DIR = Path("~/Desktop/code/deployable_auto-montage/ffmpeger_otio_video_maker/output")
ZOOM_DIR = UNIVERSAL_INSERT_DIR / "zoom"
ZOOM_FACE_REPLACEMENTS_DIR = ZOOM_DIR
ZOOM_INSERT_SHIFT_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/program/zoom_for_insert/zoom_insert_shift_creator_0.py"
)
ZOOM_INSERT_SHIFT_REPLACEMENTS_DIR = ZOOM_DIR
ZOOM_TYPE_RESOLVER_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/program/zoom_for_insert/zoom_type_resolver_0.py"
)
ZOOM_INTRO_INSERT_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/program/zoom_intro_insert_creator.py"
)
_ZOOM_INTRO_SFX_OPTIONS = [
    Path("~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/asset/cut_sfx_riser.mp3"),
    Path("~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/asset/deep_boom_intro_zoom.mp3"),
]
OUTRO_DIP_INSERT_SCRIPT = Path(
    "~/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/outro-dip-to-black/claude/program/outro_dip_insert_creator.py"
)

USE_CLAP_ENV = "GROQ_PIPE_USE_CLAP"

# All reference document formats the pipe can accept (in addition to HTML).
_REF_GLOB_PATTERNS = ("*.html", "*.htm", "*.docx", "*.rtf", "*.txt", "*.pdf", "*.pages")

RAW_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv"}
DOWNLOADER_VIDEO_EXTENSIONS = VIDEO_EXTENSIONS
PAPER_ANIMATOR_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".mov", ".mp4"}
# Extensions DaVinci Resolve cannot open natively on macOS
DAVINCI_INCOMPATIBLE_VIDEO_EXTENSIONS = {".webm", ".mkv"}
INSERT_TIME_PREFIX_PATTERN = re.compile(r"^(?:\d{2}h\d{2}m\d{2}s\d{3}ms|\d+m\d+)_", re.IGNORECASE)
INTERNAL_INSERT_METADATA_DIRS = {".insert_timing", "zoom_face_replacements"}

# Plain-text URL detection and timing extraction
DEFAULT_PLAIN_URL_VIDEO_DURATION_S = 5.0

# Matches absolute http(s) URLs and bare domains with a known TLD.
# Two alternatives:
#  1. Full https?:// URL (catches any domain including 1-char like x.com)
#  2. Bare domain — requires ≥2-char label to avoid false positives on prose words.
#     A path component is optional.
_PLAIN_URL_RE = re.compile(
    r'(?:https?://[^\s<>"\'\)\]]+)'
    r'|'
    r'(?<!["\'\w/@.=])(?:www\.)?[\w][\w\-]+'
    r'\.(?:com|org|net|io|fr|de|uk|info|edu|gov|ly|me|tv|cc|ai|app|news|media|link)'
    r'(?:/[^\s<>"\'\)\]]*)?',
    re.IGNORECASE,
)

_VIDEO_PLAIN_RE = re.compile(
    r'(?:youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|'
    r'twitter\.com|x\.com|facebook\.com|fb\.watch|'
    r'instagram\.com|tiktok\.com|vm\.tiktok\.com|'
    r'rumble\.com|odysee\.com)',
    re.IGNORECASE,
)
_TWEET_PLAIN_RE = re.compile(r'(?:twitter|x)\.com/.+/status/', re.IGNORECASE)
_IMAGE_EXT_PLAIN_RE = re.compile(
    r'\.(jpg|jpeg|png|gif|webp|tiff|bmp)(?:[?#]|$)', re.IGNORECASE
)

# Timecode patterns used near plain-text references
_TC_RANGE_PLAIN_RE = re.compile(
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(\d{1,2}:\d{2}(?::\d{2})?)'
)
_TC_END_ONLY_PLAIN_RE = re.compile(r'(?<![:\d])-(\d{1,2}:\d{2}(?::\d{2})?)\b')
_TC_PLAIN_SINGLE_RE = re.compile(r'\b(\d{1,2}:\d{2}(?::\d{2})?)\b')
_TC_HUMAN_MN_RE = re.compile(r'\b(\d+)m(\d{1,2})\b')


def _bool_env(var_name: str) -> bool:
    raw = os.environ.get(var_name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _read_wait_seconds(default: int = 10) -> int:
    raw_value = os.environ.get("SWISSER_TRANSCRIPTION_WAIT_SECONDS")
    if not raw_value:
        return default
    try:
        return max(0, int(raw_value))
    except ValueError:
        return default


def resolve_path(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"Missing path: {resolved}")
    return resolved


def run_python_script(step_name: str, script: Path, extra_args: Sequence[str] | None = None) -> None:
    script_path = resolve_path(script)
    args = [PYTHON_BIN, str(script_path)]
    if extra_args:
        args.extend(extra_args)
    print(f"\n==> {step_name}")
    print("    Running:", " ".join(args))
    subprocess.run(args, check=True)


def load_json_file(path: Path) -> dict:
    resolved = resolve_path(path)
    with resolved.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _json_has_entries_list(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("entries"), list)


def resolve_downloader_metadata_path(output_dir: Path) -> Path:
    resolved = resolve_path(output_dir)
    preferred = sorted(
        (path for path in resolved.glob("*_metadata.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in preferred:
        if _json_has_entries_list(candidate):
            return candidate

    fallback = sorted(
        (path for path in resolved.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in fallback:
        if _json_has_entries_list(candidate):
            return candidate

    raise FileNotFoundError(f"No valid downloader metadata JSON with an entries list found in {resolved}")


def _safe_int(value: object) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _count_data_rows(csv_path: Path | None) -> int:
    if csv_path is None or not csv_path.exists():
        return 0
    with csv_path.open("r", encoding="utf-8", errors="ignore") as handle:
        next(handle, None)
        return sum(1 for line in handle if line.strip())


def _count_precise_zoom_rows(csv_path: Path | None) -> int:
    if csv_path is None or not csv_path.exists():
        return 0
    total = 0
    with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            if (row.get("Annotation Column") or "").strip() != "Zoom":
                continue
            if not (row.get("Annotation Value") or "").strip():
                continue
            total += 1
    return total


def _timed_ai_generated_count(summary: dict) -> int:
    counts = summary.get("counts") or {}
    total = 0
    if not isinstance(counts, dict):
        return total
    for bucket in counts.values():
        if isinstance(bucket, dict):
            total += sum(_safe_int(value) for value in bucket.values())
    return total


def _timed_ai_no_span_count(summary: dict) -> int:
    skipped = summary.get("skipped") or {}
    total = 0
    if not isinstance(skipped, dict):
        return total
    for bucket in skipped.values():
        if isinstance(bucket, dict):
            total += _safe_int(bucket.get("no_span"))
    return total


def _timed_manifest_link_counts(csv_path: Path | None) -> dict[str, int]:
    counts = {"article_links": 0, "image_links": 0, "video_links": 0, "tweet_links": 0, "website_links": 0}
    if csv_path is None or not csv_path.exists():
        return counts
    with csv_path.open("r", encoding="utf-8", errors="ignore") as handle:
        header_line = handle.readline()
        if not header_line:
            return counts
        header = [cell.strip() for cell in header_line.rstrip("\n").split(";")]
        try:
            category_index = header.index("Asset Category")
        except ValueError:
            return counts
        for line in handle:
            if not line.strip():
                continue
            columns = line.rstrip("\n").split(";")
            category = columns[category_index].strip() if category_index < len(columns) else ""
            if category in counts:
                counts[category] += 1
    return counts


def _timed_manifest_bold_count(csv_path: Path | None) -> int:
    if csv_path is None or not csv_path.exists():
        return 0
    total = 0
    with csv_path.open("r", encoding="utf-8", errors="ignore", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            if (row.get("Asset Category") or "").strip() != "social_ranking_punctuation":
                continue
            if (row.get("Illustration Type") or "").strip() != "bold":
                continue
            total += 1
    return total


def _validate_bold_insert_assets(insert_dir: Path, expected_count: int) -> list[Path]:
    resolved = resolve_path(insert_dir)
    bold_assets = sorted(
        path
        for path in resolved.iterdir()
        if path.is_file() and path.suffix.lower() == ".mov" and path.name.endswith("_BLD.mov")
    )
    if len(bold_assets) < expected_count:
        raise RuntimeError(
            "Bold insert generation is incomplete: "
            f"expected at least {expected_count} staged BLD clip(s) in {resolved}, "
            f"found {len(bold_assets)}."
        )
    return bold_assets


def validate_precise_pipeline_outputs(
    approximate_run_dir: Path,
    final_comparer_path: Path,
    timed_ai_summary_path: Path | None,
    timed_manifest_csv: Path | None = None,
) -> None:
    run_summary_path = resolve_path(approximate_run_dir) / "summary.json"
    run_summary = load_json_file(run_summary_path)
    stage13 = ((run_summary.get("stages") or {}).get("13") or {}) if isinstance(run_summary, dict) else {}
    precise_summary_rows = _safe_int(stage13.get("precise_comparer_rows"))
    precise_file_rows = _count_data_rows(final_comparer_path)
    illustration_candidates = _safe_int(stage13.get("illustration_candidates"))
    precise_annotations = _safe_int(stage13.get("precise_annotations"))
    if precise_summary_rows <= 0 or precise_file_rows <= 0:
        raise RuntimeError(
            "Approximate full pipeline produced no usable precise comparer rows "
            f"(summary precise_comparer_rows={precise_summary_rows}, "
            f"staged_precise_rows={precise_file_rows}, "
            f"illustration_candidates={illustration_candidates}, "
            f"precise_annotations={precise_annotations})."
        )

    if timed_ai_summary_path is None or not timed_ai_summary_path.exists():
        raise RuntimeError("timed_AI_illustrator summary is missing; cannot validate timed insert output.")
    timed_summary = load_json_file(timed_ai_summary_path)
    kept_rows = _safe_int(timed_summary.get("kept_rows"))
    generated_records = _timed_ai_generated_count(timed_summary)
    no_span = _timed_ai_no_span_count(timed_summary)
    manifest_rows = _count_data_rows(timed_manifest_csv)
    if kept_rows <= 0:
        raise RuntimeError(
            "timed_AI_illustrator has no kept comparer rows to inspect "
            f"(kept_rows={kept_rows}, generated_records={generated_records}, "
            f"no_span={no_span}, manifest_rows={manifest_rows})."
        )
    if generated_records <= 0 or manifest_rows <= 0:
        print(
            "    Warning: timed_AI_illustrator produced no usable timed inserts "
            f"(kept_rows={kept_rows}, generated_records={generated_records}, "
            f"no_span={no_span}, manifest_rows={manifest_rows})."
        )


def run_approximate_full_pipeline(
    transcript_csv: Path,
    words_csv: Path | None,
    rush_video: Path,
    html_path: Path | None,
) -> tuple[Path, Path, Path, Path | None, Path | None, Path]:
    output_root = APPROXIMATE_STAGE1_OUTPUT_DIR.expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    pipeline_args = [
        "--csv",
        str(transcript_csv),
        "--rush",
        str(rush_video),
        "--output-dir",
        str(output_root),
    ]
    if words_csv is not None:
        pipeline_args.extend(["--words", str(words_csv)])
    if html_path is not None:
        pipeline_args.extend(["--html", str(html_path)])
    run_python_script(
        "Comparser full pipeline (approximate matching + precise timeline)",
        APPROXIMATE_FULL_PIPELINE_SCRIPT,
        tuple(pipeline_args),
    )
    summary_path = latest_file(output_root, ("*/summary.json",))
    summary = load_json_file(summary_path)
    xml_ready_csv = Path(summary["step1_xml_ready_csv"]).expanduser()
    diagnostic_csv = Path(summary["step1_diagnostic_csv"]).expanduser()
    precise_comparer_raw = summary.get("precise_comparer_csv")
    if not precise_comparer_raw:
        raise KeyError(f"Approximate full-pipeline summary missing precise_comparer_csv: {summary_path}")
    precise_comparer_csv = Path(precise_comparer_raw).expanduser()
    precise_annotations_csv = None
    if summary.get("precise_annotations_csv"):
        precise_annotations_csv = Path(summary["precise_annotations_csv"]).expanduser()
    illustration_candidates_csv = None
    if summary.get("illustration_candidates_csv"):
        illustration_candidates_csv = Path(summary["illustration_candidates_csv"]).expanduser()
    for candidate in (xml_ready_csv, diagnostic_csv, precise_comparer_csv):
        if not candidate.exists():
            raise FileNotFoundError(f"Approximate full-pipeline output missing: {candidate}")
    print(f"    Stage-1 XML-ready CSV (x = keep): {xml_ready_csv}")
    print(f"    Stage-1 diagnostic CSV: {diagnostic_csv}")
    print(f"    Precise comparer CSV: {precise_comparer_csv}")
    if precise_annotations_csv is not None and precise_annotations_csv.exists():
        print(f"    Precise annotations CSV: {precise_annotations_csv}")
    if illustration_candidates_csv is not None and illustration_candidates_csv.exists():
        print(f"    Illustration candidates CSV: {illustration_candidates_csv}")
    return (
        precise_comparer_csv,
        xml_ready_csv,
        diagnostic_csv,
        precise_annotations_csv,
        illustration_candidates_csv,
        summary_path.parent,
    )


def run_timed_ai_illustrator(
    approximate_run_dir: Path,
    html_path: Path | None,
    final_comparer_path: Path,
) -> tuple[Path | None, Path | None, Path | None, Path | None, Path | None]:
    output_root = TIMED_AI_ILLUSTRATOR_OUTPUT_DIR.expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    script_path = resolve_path(TIMED_AI_ILLUSTRATOR_SCRIPT)
    args = [
        PYTHON_BIN,
        str(script_path),
        "--run-dir",
        str(approximate_run_dir),
        "--output-dir",
        str(output_root),
    ]
    if html_path is not None:
        args.extend(["--html", str(html_path)])
    print("\n==> Timed AI illustrator")
    print("    Running:", " ".join(args))
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.strip())

    summary_path = latest_file(output_root, ("*/summary.json",))
    summary = load_json_file(summary_path)
    outputs = summary.get("outputs", {}) if isinstance(summary, dict) else {}
    format_csv = Path(str(outputs.get("format_csv") or "")).expanduser() if outputs.get("format_csv") else None
    ai_csv = Path(str(outputs.get("ai_csv") or "")).expanduser() if outputs.get("ai_csv") else None
    manifest_csv = Path(str(outputs.get("timing_manifest_csv") or "")).expanduser() if outputs.get("timing_manifest_csv") else None
    manifest_json = Path(str(outputs.get("timing_manifest_json") or "")).expanduser() if outputs.get("timing_manifest_json") else None
    if format_csv is not None and format_csv.exists():
        staged_format_csv = final_comparer_path.with_name(f"{final_comparer_path.stem}_timed_format_illustrations.csv")
        shutil.copy2(format_csv, staged_format_csv)
        format_csv = staged_format_csv
    if ai_csv is not None and ai_csv.exists():
        staged_ai_csv = final_comparer_path.with_name(f"{final_comparer_path.stem}_timed_ai_illustrations.csv")
        shutil.copy2(ai_csv, staged_ai_csv)
        ai_csv = staged_ai_csv
    if manifest_csv is not None and manifest_csv.exists():
        staged_manifest_csv = final_comparer_path.with_name(f"{final_comparer_path.stem}_timed_insert_timing_manifest.csv")
        shutil.copy2(manifest_csv, staged_manifest_csv)
        manifest_csv = staged_manifest_csv
    if manifest_json is not None and manifest_json.exists():
        staged_manifest_json = final_comparer_path.with_name(f"{final_comparer_path.stem}_timed_insert_timing_manifest.json")
        shutil.copy2(manifest_json, staged_manifest_json)
        manifest_json = staged_manifest_json
    staged_summary_path = final_comparer_path.with_name(f"{final_comparer_path.stem}_timed_ai_illustrator_summary.json")
    shutil.copy2(summary_path, staged_summary_path)

    if format_csv is not None and format_csv.exists():
        print(f"    Timed format illustrations retained at: {format_csv}")
    if ai_csv is not None and ai_csv.exists():
        print(f"    Timed AI illustrations retained at: {ai_csv}")
    if manifest_csv is not None and manifest_csv.exists():
        print(f"    Timed insert timing manifest retained at: {manifest_csv}")
    print(f"    Timed AI illustrator summary retained at: {staged_summary_path}")
    return format_csv, ai_csv, manifest_csv, manifest_json, staged_summary_path


def run_face_zoom_replacements(
    precise_annotations_csv: Path,
    final_comparer_path: Path,
    media_timebase: int,
) -> Path | None:
    zoom_row_count = _count_precise_zoom_rows(precise_annotations_csv)
    if zoom_row_count <= 0:
        print("    Precise annotations contain no Zoom rows; skipping face-aware zoom replacement stage.")
        return None

    output_dir = (ZOOM_FACE_REPLACEMENTS_DIR.expanduser() / final_comparer_path.stem).resolve()
    manifest_path = final_comparer_path.with_name(f"{final_comparer_path.stem}_zoom_face_manifest.json")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_python_script(
        "Render face-aware zoom replacements",
        FACE_ZOOM_REPLACEMENT_SCRIPT,
        (
            "--fps",
            str(media_timebase),
            "--output-dir",
            str(output_dir),
            "--manifest-json",
            str(manifest_path),
            "--overwrite",
            str(precise_annotations_csv),
        ),
    )
    if not manifest_path.exists():
        raise FileNotFoundError(f"Expected zoom face manifest not found: {manifest_path}")
    print(f"    Face-aware zoom clips written to: {output_dir}")
    print(f"    Face-aware zoom manifest retained at: {manifest_path}")
    return manifest_path


def run_insert_shift_zooms(
    rush_path: Path,
    insert_dir: Path,
    annotations_csv: Path | None,
    media_timebase: int,
    rush_xml: Path | None = None,
) -> Path | None:
    """Render pan-right zoom clips for insert-heavy timeline windows.

    Scans the Insert folder for triggering inserts (emoji, CTA, portraits, etc.),
    groups consecutive ones, and produces a shift-zoom clip per group.  Returns
    the manifest JSON path, or None when no triggering inserts are found.
    """
    output_dir = (ZOOM_INSERT_SHIFT_REPLACEMENTS_DIR.expanduser()).resolve()
    manifest_path = output_dir / "zoom_insert_shift_manifest.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    extra_args: list[str] = [
        "--rush", str(rush_path),
        "--insert-dir", str(insert_dir.expanduser()),
        "--output-dir", str(output_dir),
        "--manifest-json", str(manifest_path),
        "--fps", str(media_timebase),
        "--overwrite",
    ]
    if annotations_csv is not None and annotations_csv.exists():
        extra_args.extend(["--annotations", str(annotations_csv)])
    if rush_xml is not None and rush_xml.exists():
        extra_args.extend(["--rush-xml", str(rush_xml)])

    run_python_script(
        "Render insert-shift zoom clips",
        ZOOM_INSERT_SHIFT_SCRIPT,
        tuple(extra_args),
    )
    if not manifest_path.exists():
        print("    No insert-shift manifest produced (no triggering inserts found).")
        return None
    print(f"    Insert-shift zoom manifest: {manifest_path}")
    return manifest_path


def run_zoom_type_resolver(
    comparer_csv: Path,
    annotations_csv: Path | None,
    insert_dir: Path,
    media_timebase: float,
) -> None:
    """Resolve unified Zoom types in comparer CSV after Insert folder is fully populated."""
    if not comparer_csv.exists():
        print("    Zoom type resolver: comparer CSV not found; skipping.")
        return
    extra_args = [
        "--csv", str(comparer_csv),
        "--insert-dir", str(insert_dir.expanduser()),
        "--fps", str(media_timebase),
        "--transition-s", "1.0",
        "--gap-tolerance", "1.0",
    ]
    if annotations_csv is not None and annotations_csv.exists():
        extra_args.extend(["--annotations-csv", str(annotations_csv)])
    run_python_script(
        "Resolve unified Zoom types in comparer CSV",
        ZOOM_TYPE_RESOLVER_SCRIPT,
        tuple(extra_args),
    )


def _merge_zoom_manifests(
    face_manifest: Path | None,
    shift_manifest: Path | None,
    merged_output: Path,
) -> Path | None:
    """Merge face-zoom and insert-shift manifests into one file for program6.

    Shift window entries take priority: any face-zoom entry whose row_id appears
    in a shift entry's overridden_comparser_rows list is excluded from the merge.
    """
    face_entries: list[dict] = []
    shift_entries: list[dict] = []
    fps: float = 30.0

    if face_manifest and face_manifest.exists():
        with face_manifest.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        face_entries = payload.get("entries") or []
        fps = float(payload.get("sequence_fps") or fps)

    if shift_manifest and shift_manifest.exists():
        with shift_manifest.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        shift_entries = payload.get("entries") or []
        fps = float(payload.get("sequence_fps") or fps)

    if not face_entries and not shift_entries:
        return None

    # Collect all row_ids overridden by shift windows
    overridden_ids: set[str] = set()
    for entry in shift_entries:
        for rid in entry.get("overridden_comparser_rows") or []:
            overridden_ids.add(str(rid))

    # Keep face-zoom entries that are NOT overridden
    kept_face = [e for e in face_entries if str(e.get("row_id") or "") not in overridden_ids]

    combined = kept_face + shift_entries
    combined.sort(key=lambda e: (int(e.get("timeline_start_frames") or 0),
                                  int(e.get("timeline_end_frames") or 0)))

    merged_output.parent.mkdir(parents=True, exist_ok=True)
    with merged_output.open("w", encoding="utf-8") as fh:
        json.dump({"sequence_fps": fps, "entries": combined}, fh, indent=2)
    return merged_output


def run_intro_zoom(
    rush_path: Path,
    insert_dir: Path,
    reference_xml: Path | None = None,
) -> Path | None:
    """
    Apply spin-zoom intro effect to the first kept segment of the edited
    sequence and stage the result as 00h00m00s000ms_0_intro_zoom.mp4 in the
    Insert folder so xml_insertor places it at t=0 on top of the rush.

    If reference_xml is provided, the source in-point of the first clip in
    video track 1 is used so the zoom targets the actual opening frame of the
    edit, not necessarily the very start of the rush file.

    Failure is soft — a crash prints a warning but does not abort the pipeline.
    """
    print("\n==> Intro zoom insert")
    _sfx = random.choice(_ZOOM_INTRO_SFX_OPTIONS)
    print(f"    Intro SFX: {_sfx.name}")
    zoom_dir = (insert_dir / "zoom").expanduser()
    zoom_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON_BIN,
        str(ZOOM_INTRO_INSERT_SCRIPT.expanduser()),
        "--rush", str(rush_path),
        "--insert-dir", str(zoom_dir),
        "--sfx", str(_sfx.expanduser()),
        "--overwrite",
    ]
    if reference_xml is not None and reference_xml.exists():
        cmd += ["--xml", str(reference_xml)]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("    WARNING: intro zoom step failed — continuing without it.")
        return None
    dest = zoom_dir / "00h00m00s000ms_0_intro_zoom.mp4"
    print(f"    Staged intro zoom insert → {dest}")
    return dest


def run_outro_dip(
    rush_path: Path,
    insert_dir: Path,
    reference_xml: Path,
) -> Path | None:
    """
    Render the outro dip-to-black effect on the last kept rush segment and
    stage it in the Insert folder. program6 picks it up and swaps it onto V1/A1
    of the final XML. Soft-fails on error.
    """
    print("\n==> Outro dip-to-black")
    if not reference_xml.exists():
        print(f"    WARNING: reference XML missing ({reference_xml}); skipping outro dip.")
        return None
    zoom_dir = (insert_dir / "zoom").expanduser()
    zoom_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        PYTHON_BIN,
        str(OUTRO_DIP_INSERT_SCRIPT.expanduser()),
        "--rush", str(rush_path),
        "--insert-dir", str(zoom_dir),
        "--xml", str(reference_xml),
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("    WARNING: outro dip step failed — continuing without it.")
        return None
    staged = sorted(zoom_dir.glob("*_outro_dip.mp4"))
    if not staged:
        print("    WARNING: outro dip stage produced no file; continuing without it.")
        return None
    print(f"    Staged outro dip insert → {staged[-1]}")
    return staged[-1]


def wait_with_updates(step_name: str, total_seconds: int, tick: int = 30) -> None:
    if total_seconds <= 0:
        return
    print(f"\n==> {step_name}")
    remaining = total_seconds
    while remaining > 0:
        step = min(remaining, tick)
        time.sleep(step)
        remaining -= step
        print(f"    ...{remaining} seconds remaining")


def latest_file(directory_hint: Path, patterns: Iterable[str]) -> Path:
    directory = resolve_path(directory_hint)
    if not directory.is_dir():
        raise FileNotFoundError(f"Not a directory: {directory}")
    candidates: list[tuple[float, Path]] = []
    for pattern in patterns:
        for file_path in directory.glob(pattern):
            try:
                candidates.append((file_path.stat().st_mtime, file_path))
            except OSError:
                continue
    if not candidates:
        raise FileNotFoundError(f"No files matching {list(patterns)} in {directory}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def latest_video_file(directory_hint: Path) -> Path:
    directory = resolve_path(directory_hint)
    if not directory.is_dir():
        raise FileNotFoundError(f"Not a directory: {directory}")
    candidates = [
        path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in DOWNLOADER_VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise FileNotFoundError(f"No supported video files found in {directory}")
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def _ensure_directory(path: Path) -> Path:
    resolved = path.expanduser()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def stage_html_for_downloader(html_path: Path) -> Path:
    resolved_html = resolve_path(html_path)
    input_dir = _ensure_directory(INSERT_DOWNLOADER_INPUT_DIR)
    destination = input_dir / resolved_html.name
    if destination.exists():
        try:
            if destination.resolve() == resolved_html.resolve():
                destination.touch()
                return destination
        except FileNotFoundError:
            pass
    shutil.copy2(resolved_html, destination)
    destination.touch()
    return destination


def downloader_output_dir_for_html(html_path: Path) -> Path:
    base = _ensure_directory(INSERT_DOWNLOADER_OUTPUT_DIR)
    return base / html_path.stem


def paper_animator_input_dir_for_html(html_path: Path) -> Path:
    return downloader_output_dir_for_html(html_path) / "title_cards"


def paper_assets_exist_for_html(html_path: Path) -> bool:
    input_dir = paper_animator_input_dir_for_html(html_path)
    if not input_dir.exists():
        return False
    return any(
        path.is_file() and path.suffix.lower() in PAPER_ANIMATOR_EXTENSIONS
        for path in input_dir.rglob("*")
    )


def _reset_directory(path: Path) -> Path:
    resolved = path.expanduser()
    if resolved.exists():
        for child in list(resolved.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _downloader_asset_pairs(output_dir: Path) -> list[tuple[Path, Path]]:
    asset_pairs: list[tuple[Path, Path]] = []
    for src in output_dir.rglob("*"):
        if not src.is_file() or src.suffix.lower() not in DOWNLOADER_VIDEO_EXTENSIONS:
            continue
        relative = src.relative_to(output_dir)
        if relative.parts and relative.parts[0].lower() == "title_cards":
            relative = Path(relative.name)
        asset_pairs.append((src, relative))
    return asset_pairs


def _canonical_insert_name(name: str) -> str:
    normalized = name.lower().replace("@", "_")
    normalized = INSERT_TIME_PREFIX_PATTERN.sub("", normalized, count=1)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _existing_insert_index(insert_dir: Path) -> dict[str, Path]:
    resolved = insert_dir.expanduser()
    if not resolved.exists():
        return {}
    index: dict[str, Path] = {}
    for path in resolved.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in RAW_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            continue
        canonical = _canonical_insert_name(path.name)
        if canonical:
            index.setdefault(canonical, path)
    return index


def _is_plain_video_link_asset(path: Path) -> bool:
    stem = path.stem.lower()
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and "@" in stem
        and "extract" not in stem
        and "extrait" not in stem
        and "titre@" not in stem
        and "title_" not in stem
        and "title@" not in stem
    )


def _asset_has_alpha(path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=pix_fmt",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    pix_fmt = (result.stdout or "").strip().lower()
    return "a" in pix_fmt if pix_fmt else False


def _parse_fraction(raw_value: str) -> float | None:
    value = (raw_value or "").strip()
    if not value or value == "0/0":
        return None
    if "/" in value:
        left, right = value.split("/", 1)
        try:
            numerator = float(left)
            denominator = float(right)
        except ValueError:
            return None
        if denominator == 0:
            return None
        return numerator / denominator
    try:
        return float(value)
    except ValueError:
        return None


def probe_media_timebase(media_path: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "default=noprint_wrappers=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Unable to probe frame rate for {media_path}") from exc
    rates: dict[str, float] = {}
    for line in (result.stdout or "").splitlines():
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        parsed = _parse_fraction(raw_value)
        if parsed is not None and parsed > 0:
            rates[key.strip()] = parsed
    candidate = rates.get("avg_frame_rate") or rates.get("r_frame_rate")
    if not candidate:
        return 30
    return max(1, int(round(candidate)))


def insert_assets_ready_for_html(source_html: Path, staged_html: Path, insert_dir: Path) -> bool:
    existing_inserts = _existing_insert_index(insert_dir)
    if not existing_inserts:
        return False
    try:
        html_mtime = resolve_path(source_html).stat().st_mtime
    except (FileNotFoundError, OSError):
        html_mtime = 0.0
    output_dir = downloader_output_dir_for_html(staged_html)
    if not output_dir.exists():
        return False
    asset_pairs = _downloader_asset_pairs(output_dir)
    if not asset_pairs:
        return False
    try:
        newest_output_mtime = max((src.stat().st_mtime for src, _ in asset_pairs), default=0.0)
    except OSError:
        newest_output_mtime = 0.0
    if newest_output_mtime < html_mtime:
        return False
    for _, relative in asset_pairs:
        target = insert_dir.expanduser() / relative
        if target.is_file():
            if _is_plain_video_link_asset(target) and _asset_has_alpha(target):
                return False
            continue
        canonical = _canonical_insert_name(relative.name)
        match = existing_inserts.get(canonical)
        if not match or not match.is_file():
            return False
        if _is_plain_video_link_asset(match) and _asset_has_alpha(match):
            return False
    return True


def sync_downloader_assets_into_universal_insert(staged_html: Path, target_dir: Path) -> list[Path]:
    output_dir = downloader_output_dir_for_html(staged_html)
    if not output_dir.exists():
        raise FileNotFoundError(f"Insert downloader output missing: {output_dir}")
    asset_pairs = _downloader_asset_pairs(output_dir)
    if not asset_pairs:
        return []
    target = _reset_directory(target_dir)
    copied: list[Path] = []
    for src, relative in asset_pairs:
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if src.suffix.lower() in DOWNLOADER_VIDEO_EXTENSIONS:
            copied.append(dest)
    return copied


def normalize_inserts_for_davinci(insert_dir: Path) -> int:
    """Convert DaVinci-incompatible video files to H264 MP4 in-place.

    Walks *insert_dir* recursively and re-encodes every file whose extension
    is listed in DAVINCI_INCOMPATIBLE_VIDEO_EXTENSIONS.  The converted file
    replaces the original so downstream steps (program6, xml_to_otio) always
    see a DaVinci-readable path.
    """
    resolved = insert_dir.expanduser()
    if not resolved.exists():
        return 0
    converted = 0
    for src in sorted(resolved.rglob("*")):
        if not src.is_file():
            continue
        if src.suffix.lower() not in DAVINCI_INCOMPATIBLE_VIDEO_EXTENSIONS:
            continue
        dst = src.with_suffix(".mp4")
        print(f"    Converting {src.name} → {dst.name}")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k",
                    str(dst),
                ],
                check=True,
                capture_output=True,
            )
            src.unlink()
            converted += 1
            print(f"    Done → {dst.name}")
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            print(f"    WARNING: Could not convert {src.name}: {stderr[:200]}")
    return converted


TIMECODE_STRIPPABLE_EXTENSIONS = {".mov", ".mp4", ".m4v"}


def _insert_has_nonzero_timecode(path: Path) -> bool:
    """Return True if *path* carries a non-zero timecode tag (format or stream)."""
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format_tags=timecode:stream_tags=timecode",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return False
    for line in probe.stdout.splitlines():
        tc = line.strip()
        if tc and tc != "00:00:00:00":
            return True
    return False


def strip_insert_timecodes(insert_dir: Path) -> int:
    """Zero the embedded timecode on DaVinci-relevant files inside *insert_dir*.

    DaVinci Resolve treats an OTIO clip's `start_time` as an absolute source
    timecode. When a file embeds a non-zero TC (e.g. 00:02:03:15), DaVinci
    looks for frame 0 inside a file whose first frame is at 02:03:15 and
    marks the clip as missing. Rewriting every `.mov`/`.mp4`/`.m4v` in place
    with a zero timecode makes `start_time=0` line up with the first frame.
    """
    resolved = insert_dir.expanduser()
    if not resolved.exists():
        return 0
    stripped = 0
    for src in sorted(resolved.rglob("*")):
        if not src.is_file():
            continue
        if src.suffix.lower() not in TIMECODE_STRIPPABLE_EXTENSIONS:
            continue
        if not _insert_has_nonzero_timecode(src):
            continue
        tmp = src.with_name(f".{src.stem}.tc_strip{src.suffix}")
        print(f"    Stripping timecode on {src.name}")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(src),
                    "-map", "0:v", "-map", "0:a?",
                    "-c", "copy",
                    "-map_metadata", "-1",
                    "-map_metadata:s:v", "-1",
                    "-map_metadata:s:a", "-1",
                    "-timecode", "00:00:00:00",
                    str(tmp),
                ],
                check=True,
                capture_output=True,
            )
            tmp.replace(src)
            stripped += 1
        except subprocess.CalledProcessError as exc:
            if tmp.exists():
                tmp.unlink()
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            print(f"    WARNING: Could not strip timecode from {src.name}: {stderr[:200]}")
    return stripped


def validate_precise_insert_dir(insert_dir: Path) -> None:
    resolved = resolve_path(insert_dir)
    invalid_paths: list[Path] = []
    for path in resolved.iterdir():
        if path.name == ".DS_Store":
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if path.is_dir() and path.name in INTERNAL_INSERT_METADATA_DIRS:
            continue
        if not path.is_file():
            invalid_paths.append(path)
            continue
        if path.name == "list.txt":
            continue
        if path.suffix.lower() not in RAW_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {".gif", ".webm"}:
            invalid_paths.append(path)
            continue
        if not INSERT_TIME_PREFIX_PATTERN.match(path.name):
            invalid_paths.append(path)
    if invalid_paths:
        raise RuntimeError(
            "Active Insert folder contains non-staged assets: "
            + ", ".join(path.name for path in sorted(invalid_paths))
        )


def _start_time_to_prefix(start_time: str) -> str:
    """Convert 'HH:MM:SS.mmm' (or ',mmm') to the insert-dir prefix 'HHhMMmSSsNNNms'."""
    cleaned = start_time.replace(",", ".")
    m = re.match(r"(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", cleaned)
    if not m:
        return "00h00m00s000ms"
    h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    ms = int((m.group(4) or "0").ljust(3, "0")[:3])
    return f"{h:02d}h{mn:02d}m{s:02d}s{ms:03d}ms"


def _extract_url_from_segment(segment: str) -> str:
    """Extract the first URL-like token from a reference segment string.

    Word-level alignment sometimes inserts spaces before TLDs, e.g.
    'nicolas-cage. com.' — this function strips those spaces so the
    returned value is a usable domain name or URL.
    Returns an empty string when no URL-like pattern is found.
    """
    if not segment:
        return ""
    m = re.search(r'https?://\S+', segment)
    if m:
        return m.group(0).rstrip(".,;)")
    # Domain with optional intra-word spaces before TLD (tokenization artifact)
    m = re.search(r'\b((?:www\.\s*)?[\w][\w-]*(?:\.\s*[\w]{2,})+)\b', segment, re.IGNORECASE)
    if m:
        return re.sub(r'[ \t]+', '', m.group(1)).rstrip(".")
    return ""


def _read_url_tasks_from_csv(csv_path: Path) -> list[tuple[str, str]]:
    """Return a list of (time_prefix, url) for every 'Spoken URL' entry in the CSV.

    The HTML reference segment is preferred over the spoken transcription because
    the spoken version may use phonetic spellings (e.g. 'NicolasKage.com') that
    differ from the real domain in the prepared HTML ('nicolas-cage.com').
    """
    tasks: list[tuple[str, str]] = []
    seen: set[str] = set()
    with csv_path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            raw = (row.get("Spoken URL") or "").strip()
            if not raw:
                continue
            start_time = (row.get("Start Time") or "00:00:00.000").strip()
            prefix = _start_time_to_prefix(start_time)
            ref_segment = (row.get("Reference Segment") or "").strip()
            ref_url = _extract_url_from_segment(ref_segment)
            for fragment in raw.split(" | "):
                spoken = fragment.strip()
                if not spoken:
                    continue
                url = ref_url if ref_url else spoken
                if _canonical_url(url) in seen:
                    continue
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                url = _unwrap_url(url)
                key = _canonical_url(url)
                if key in seen:
                    continue
                seen.add(key)
                tasks.append((prefix, url))
    return tasks


def _unwrap_url(url: str) -> str:
    """Unwrap Google redirect URLs (google.com/url?q=...) to their real target.

    Also handles HTML-entity-encoded variants (e.g. &amp;sa=D appearing in
    raw href attributes from Google Drive exports).
    """
    url = _html_module.unescape(url)  # &amp; → &, etc.
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [None])[0]
        if target:
            return unquote(target)
    return url


def _canonical_url(url: str) -> str:
    """Normalize a URL for deduplication: lowercase scheme+host, strip trailing root slash.

    Treats http://foo.com and http://foo.com/ as the same URL.
    """
    parsed = urlparse(url)
    return parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
    ).geturl()


def _run_url_anim_pipeline(url: str, prefix: str, insert_dir: Path, label: str) -> bool:
    """
    Animated-screenshot pipeline for a single website URL:
      url_to_video_pipe.py <url> -o <insert_dir/prefix_slug_url_screen.mov>
    Produces a proper webpage screenshot animation (not a tweet mosaic blur).
    Returns True on success.
    """
    script = URL_TO_VIDEO_SCRIPT.expanduser()
    if not script.exists():
        print(f"    url_to_video_pipe.py not found at {script}; skipping {label}.")
        return False

    # Clean Google redirect URLs before slugifying so filenames are readable
    clean = _unwrap_url(url)
    insert_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", clean.replace("https://", "").replace("http://", "")).strip("_").lower()[:40]
    out_mov = insert_dir / f"{prefix}_{slug}_url_screen.mov"

    try:
        result = subprocess.run(
            [PYTHON_BIN, str(script), clean, "-o", str(out_mov)],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip(), file=__import__("sys").stderr)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        print(f"    Warning: screenshot animation failed for {clean}: {exc}")
        if stderr:
            print(f"    stderr: {stderr}")
        if stdout:
            print(f"    stdout: {stdout}")
        return False

    print(f"    Staged: {out_mov.name}")
    return True


def run_url_screenshot_inserts(comparer_csv: Path, insert_dir: Path) -> int:
    """Generate an animated screenshot .mov for every hard-written URL found in the
    comparer CSV and stage it in *insert_dir* with the correct timing prefix."""
    try:
        tasks = _read_url_tasks_from_csv(comparer_csv)
    except Exception as exc:
        print(f"    Could not read comparer CSV for URL tasks: {exc}; skipping.")
        return 0

    if not tasks:
        print("    No plain-text URLs found in comparer CSV; skipping URL screenshot inserts.")
        return 0

    insert_dir_resolved = insert_dir.expanduser()
    print(f"\n==> URL screenshot inserts (hard-written, {len(tasks)} URL(s))")
    generated = 0
    for prefix, url in tasks:
        print(f"    {url}")
        if _run_url_anim_pipeline(url, prefix, insert_dir_resolved, url):
            generated += 1

    print(f"    {generated}/{len(tasks)} hard-written URL insert(s) staged.")
    return generated


def run_website_link_manifest_inserts(timed_manifest_csv: Path, insert_dir: Path) -> int:
    """Generate an animated screenshot .mov for every website_links entry in the timed
    insert manifest and stage it in *insert_dir* with the correct timing prefix."""
    if not timed_manifest_csv.exists():
        print("    Timed manifest not found; skipping website_links inserts.")
        return 0

    tasks: list[tuple[str, str]] = []
    seen: set[str] = set()
    with timed_manifest_csv.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            if (row.get("Asset Category") or "").strip() != "website_links":
                continue
            url = _unwrap_url((row.get("Link URL") or "").strip())
            if not url:
                continue
            key = _canonical_url(url)
            if key in seen:
                continue
            seen.add(key)
            start_time = (row.get("Start Time") or "00:00:00.000").strip()
            prefix = _start_time_to_prefix(start_time)
            tasks.append((prefix, url))

    if not tasks:
        print("    No website_links entries in timed manifest; skipping.")
        return 0

    insert_dir_resolved = insert_dir.expanduser()
    print(f"\n==> Website link screenshot inserts ({len(tasks)} URL(s))")
    generated = 0
    for prefix, url in tasks:
        print(f"    {url}")
        if _run_url_anim_pipeline(url, prefix, insert_dir_resolved, url):
            generated += 1

    print(f"    {generated}/{len(tasks)} website_links insert(s) staged.")
    return generated


def _tc_to_seconds(tc: str) -> float | None:
    """Convert MM:SS or HH:MM:SS string to total seconds."""
    parts = tc.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except (ValueError, IndexError):
        pass
    return None


def _seconds_to_hms_prefix(seconds: float) -> str:
    """Convert seconds to 'HHhMMmSSsNNNms' insert filename prefix."""
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}h{m:02d}m{s:02d}s{ms:03d}ms"


def _parse_plain_ref_context(context_text: str) -> tuple[str, float, float | None]:
    """
    Extract (start_prefix, clip_start_s, clip_end_s) from text surrounding a
    plain-text URL reference.

    clip_start_s: offset in the linked video to begin playback (seconds).
    clip_end_s:   offset to end playback, or None to play to the video's end.

    Timecode formats:
    - '01:10-2:05'  → start_prefix=01h10m, clip 0 → 55 s (range duration)
    - '-01:10'      → start_prefix=00h00m, clip 0 → 70 s (end-only duration)
    - '2m50'        → start_prefix=02h50m, clip 2:50 → end of linked video
    - '01:10'       → start_prefix=01h10m, clip 0 → DEFAULT duration
    - (none)        → start_prefix=00h00m, clip 0 → DEFAULT duration
    """
    m = _TC_RANGE_PLAIN_RE.search(context_text)
    if m:
        t1 = _tc_to_seconds(m.group(1))
        t2 = _tc_to_seconds(m.group(2))
        if t1 is not None and t2 is not None and t2 > t1:
            return _seconds_to_hms_prefix(t1), 0.0, t2 - t1

    m = _TC_END_ONLY_PLAIN_RE.search(context_text)
    if m:
        t = _tc_to_seconds(m.group(1))
        if t is not None:
            return "00h00m00s000ms", 0.0, t

    m = _TC_HUMAN_MN_RE.search(context_text)
    if m:
        offset_s = int(m.group(1)) * 60 + int(m.group(2))
        return _seconds_to_hms_prefix(float(offset_s)), float(offset_s), None

    m = _TC_PLAIN_SINGLE_RE.search(context_text)
    if m:
        t = _tc_to_seconds(m.group(1))
        if t is not None:
            return _seconds_to_hms_prefix(t), 0.0, DEFAULT_PLAIN_URL_VIDEO_DURATION_S

    return "00h00m00s000ms", 0.0, DEFAULT_PLAIN_URL_VIDEO_DURATION_S


def _classify_plain_url(url: str) -> str:
    """Return 'tweet', 'video', 'image', or 'website' for the given URL."""
    if _TWEET_PLAIN_RE.search(url):
        return 'tweet'
    if _VIDEO_PLAIN_RE.search(url):
        return 'video'
    path = urlparse(url).path
    if _IMAGE_EXT_PLAIN_RE.search(path):
        return 'image'
    return 'website'


def _extract_plain_text_refs_from_html(html_path: Path) -> list[tuple[str, str, str]]:
    """
    Scan *html_path* for plain-text URLs that are NOT inside <a> tags.

    Returns list of (context_text, raw_url, full_url) where context_text is
    the ~600-char window around the URL used for timecode extraction and
    full_url always starts with http:// or https://.
    """
    raw = html_path.read_text(encoding='utf-8', errors='ignore')

    linked_lower: set[str] = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', raw, re.IGNORECASE):
        linked_lower.add(_unwrap_url(m.group(1)).lower().rstrip('/'))

    anchor_stash: list[str] = []

    def _stash_anchor(m: re.Match) -> str:
        anchor_stash.append(m.group(0))
        return f'\x00A{len(anchor_stash) - 1}\x00'

    no_anchors = re.sub(
        r'<a\b[^>]*>.*?</a\s*>', _stash_anchor, raw,
        flags=re.IGNORECASE | re.DOTALL,
    )

    tag_stash: list[str] = []

    def _stash_tag(m: re.Match) -> str:
        tag_stash.append(m.group(0))
        return f'\x00T{len(tag_stash) - 1}\x00'

    text_only = re.sub(r'<[^>]+>', _stash_tag, no_anchors)

    results: list[tuple[str, str, str]] = []
    seen_norm: set[str] = set()

    for m in _PLAIN_URL_RE.finditer(text_only):
        raw_url = m.group(0).rstrip('.,;:)')
        full_url = raw_url if raw_url.startswith(('http://', 'https://')) else 'https://' + raw_url
        full_url = _unwrap_url(full_url)
        norm = full_url.lower().rstrip('/')
        if norm in linked_lower or norm in seen_norm:
            continue
        seen_norm.add(norm)
        ctx_start = max(0, m.start() - 300)
        ctx_end = min(len(text_only), m.end() + 300)
        context = text_only[ctx_start:ctx_end]
        results.append((context, raw_url, full_url))

    return results


def _run_video_insert_for_plain_url(
    url: str,
    prefix: str,
    clip_start_s: float,
    clip_end_s: float | None,
    insert_dir: Path,
) -> bool:
    """
    Download *url* with yt-dlp and cut it with ffmpeg.

    clip_start_s: seconds into the linked video where playback begins.
    clip_end_s:   seconds where playback ends, or None to play to end.

    Stages the result as an H264 MP4 in *insert_dir* named with *prefix*.
    Returns True on success.
    """
    yt_dlp_bin = shutil.which('yt-dlp')
    if not yt_dlp_bin:
        print(f"    yt-dlp not found in PATH; cannot download video for {url}")
        return False

    slug = re.sub(
        r'[^a-zA-Z0-9]+', '_',
        url.replace('https://', '').replace('http://', ''),
    ).strip('_').lower()[:40]
    out_path = insert_dir / f"{prefix}_{slug}_video.mp4"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_base = Path(tmpdir) / 'dl'
        dl_cmd = [
            yt_dlp_bin,
            '--format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            '--output', f'{tmp_base}.%(ext)s',
            '--no-playlist',
            '--quiet',
            url,
        ]
        try:
            subprocess.run(dl_cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b'').decode(errors='replace')[:200]
            print(f"    yt-dlp failed for {url}: {stderr}")
            return False

        candidates = sorted(Path(tmpdir).glob('dl.*'))
        if not candidates:
            print(f"    yt-dlp produced no output file for {url}")
            return False
        src = candidates[0]

        ffmpeg_cmd = ['ffmpeg', '-y', '-ss', str(clip_start_s), '-i', str(src)]
        if clip_end_s is not None:
            ffmpeg_cmd += ['-t', str(max(0.1, clip_end_s - clip_start_s))]
        ffmpeg_cmd += [
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
            '-c:a', 'aac', '-b:a', '192k',
            str(out_path),
        ]
        try:
            subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b'').decode(errors='replace')[:200]
            print(f"    ffmpeg clip failed for {url}: {stderr}")
            return False

    print(f"    Staged video insert: {out_path.name}")
    return True


def _run_image_insert_for_plain_url(url: str, prefix: str, insert_dir: Path) -> bool:
    """Download a direct image URL and stage it as an insert."""
    import urllib.request as _url_req
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}:
        ext = '.jpg'
    slug = re.sub(
        r'[^a-zA-Z0-9]+', '_',
        url.replace('https://', '').replace('http://', ''),
    ).strip('_').lower()[:40]
    out_path = insert_dir / f"{prefix}_{slug}{ext}"
    try:
        _url_req.urlretrieve(url, str(out_path))
        print(f"    Staged image insert: {out_path.name}")
        return True
    except Exception as exc:
        print(f"    Image download failed for {url}: {exc}")
        return False


def run_plain_text_ref_inserts(html_path: Path, insert_dir: Path) -> int:
    """
    Generate inserts for plain-text URLs found in *html_path* that are not
    wrapped in <a> tags.  These URLs are not expected to appear in the
    transcript; their timing is read directly from explicit timecodes in
    the surrounding text.

    Insert types:
    - video  → yt-dlp download + ffmpeg clip to the computed duration
    - image  → direct image download
    - tweet / website → animated screenshot via url_to_video_pipe

    Video duration fallback / override:
    - Range  '01:10-2:05'  → 55 s (the range duration)
    - End    '-01:10'       → 70 s
    - Offset '2m50'         → clip from 2:50 to end of linked video
    - Plain  '01:10'        → DEFAULT_PLAIN_URL_VIDEO_DURATION_S (5 s)
    - None                  → DEFAULT_PLAIN_URL_VIDEO_DURATION_S (5 s)

    Returns the count of inserts successfully staged.
    """
    plain_refs = _extract_plain_text_refs_from_html(html_path)
    if not plain_refs:
        print("    No plain-text reference URLs found; skipping.")
        return 0

    insert_dir_resolved = insert_dir.expanduser()
    insert_dir_resolved.mkdir(parents=True, exist_ok=True)
    print(f"\n==> Plain-text reference URL inserts ({len(plain_refs)} URL(s))")
    generated = 0
    for context, _raw_url, full_url in plain_refs:
        print(f"    {full_url}")
        url_type = _classify_plain_url(full_url)
        prefix, clip_start_s, clip_end_s = _parse_plain_ref_context(context)

        if url_type == 'video':
            ok = _run_video_insert_for_plain_url(
                full_url, prefix, clip_start_s, clip_end_s, insert_dir_resolved,
            )
        elif url_type == 'image':
            ok = _run_image_insert_for_plain_url(full_url, prefix, insert_dir_resolved)
        else:
            ok = _run_url_anim_pipeline(full_url, prefix, insert_dir_resolved, full_url)

        if ok:
            generated += 1

    print(f"    {generated}/{len(plain_refs)} plain-text URL insert(s) staged.")
    return generated


def _count_staged_insert_assets(insert_dir: Path) -> int:
    resolved = resolve_path(insert_dir)
    total = 0
    for path in resolved.iterdir():
        if not path.is_file():
            continue
        if path.name == "list.txt":
            continue
        if path.suffix.lower() not in RAW_IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {".gif", ".webm"}:
            continue
        if not INSERT_TIME_PREFIX_PATTERN.match(path.name):
            continue
        total += 1
    return total


def _stage_fallback_final_xml(source_xml: Path) -> Path:
    resolved_source = resolve_path(source_xml)
    output_dir = _ensure_directory(XML_INSERTOR_OUTPUT_DIR)
    destination = output_dir / resolved_source.name
    shutil.copy2(resolved_source, destination)
    return destination


def _latest_csv(directory: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in directory.glob("*.csv")
            if not path.name.endswith("_words.csv") and not path.name.endswith("_vad_segments.csv")
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No CSV files found in {directory}")
    return candidates[0]


def _derive_audio_path(rush_video: Path) -> Path:
    resolved = GROQ_AUDIO_OUTPUT_DIR.expanduser()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved / f"{rush_video.stem}.mp3"


def _run_groq_transcription(rush_video: Path, frame_rate: int) -> tuple[Path, Path, Path | None]:
    audio_path = _derive_audio_path(rush_video)
    run_python_script(
        "Extract rush audio for Groq",
        GROQ_VIDEO_TO_AUDIO_SCRIPT,
        (str(rush_video),),
    )
    use_clap = _bool_env(USE_CLAP_ENV)
    if use_clap:
        transcript_script = GROQ_CLAP_SCRIPT
        transcript_dir = GROQ_CLAP_OUTPUT_DIR
        step_name = "Groq clap transcription"
        try:
            run_python_script(step_name, transcript_script, (str(audio_path), "--frame-rate", str(frame_rate)))
        except subprocess.CalledProcessError as exc:
            transcript_root = transcript_dir.expanduser()
            try:
                fallback_csv = _latest_csv(transcript_root)
            except FileNotFoundError:
                raise RuntimeError(
                    f"{step_name} failed and no cached transcript CSV was found in {transcript_root}."
                ) from exc
            print(f"    ⚠️  {step_name} failed ({exc}). Reusing cached transcript CSV: {fallback_csv}")
            fallback_words = fallback_csv.with_name(f"{fallback_csv.stem}_words.csv")
            return fallback_csv, fallback_words if fallback_words.exists() else None, fallback_csv.with_suffix(".json")
        transcript_csv = _latest_csv(transcript_dir.expanduser())
    else:
        run_python_script(
            "Groq VAD-first transcription",
            GROQ_VAD_SCRIPT,
            (str(audio_path), "--frame-rate", str(frame_rate)),
        )
        transcript_csv = _latest_csv(GROQ_NOCLAP_OUTPUT_DIR.expanduser())
    words_csv = transcript_csv.with_name(f"{transcript_csv.stem}_words.csv")
    if not words_csv.exists():
        raise FileNotFoundError(f"Expected Groq word timings not found: {words_csv}")
    raw_json = transcript_csv.with_suffix(".json")
    return transcript_csv, words_csv, raw_json if raw_json.exists() else None


def _fix_google_redirects_in_html(html_text: str) -> str:
    """Rewrite every href that is a Google redirect (google.com/url?q=...) to the real URL."""
    def _replace(m: re.Match) -> str:
        cleaned = _unwrap_url(m.group(1))  # _unwrap_url already calls html.unescape
        return f'href="{_html_module.escape(cleaned)}"'
    return re.sub(r'href="([^"]+)"', _replace, html_text)


def _text_to_html(text: str) -> str:
    """Wrap plain text in minimal HTML, splitting on blank lines into <p> blocks."""
    paragraphs = []
    for block in re.split(r'\n{2,}', text):
        block = block.strip()
        if not block:
            continue
        lines = [_html_module.escape(line) for line in block.split('\n')]
        paragraphs.append('<p>' + '<br>\n'.join(lines) + '</p>')
    return '<html><body>\n' + '\n'.join(paragraphs) + '\n</body></html>'


def _textutil_to_html(input_path: Path) -> str:
    """Convert RTF (or any textutil-supported format) to HTML via macOS textutil."""
    with tempfile.NamedTemporaryFile(suffix='.html', delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ['textutil', '-convert', 'html', str(input_path), '-output', str(tmp_path)],
            check=True, capture_output=True,
        )
        html_text = tmp_path.read_text(encoding='utf-8', errors='ignore')
    finally:
        tmp_path.unlink(missing_ok=True)
    return _fix_google_redirects_in_html(html_text)


def _docx_to_html(docx_path: Path) -> str:
    """Parse DOCX XML and produce HTML that preserves hyperlinks, bold, and headings.

    Handles both relationship-based <w:hyperlink r:id="..."> and field-code-based
    HYPERLINK instructions (the form Google Drive / Pages use when exporting to DOCX).
    """
    W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
    R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    HEADING_MAP = {
        'heading1': 'h1', 'heading2': 'h2', 'heading3': 'h3',
        'heading4': 'h4', 'heading5': 'h5', 'heading6': 'h6',
        'title': 'h1',
    }

    with zipfile.ZipFile(docx_path) as z:
        doc_xml = z.read('word/document.xml')
        try:
            rels_xml = z.read('word/_rels/document.xml.rels')
            rels_root = ET.fromstring(rels_xml)
            rel_map: dict[str, str] = {
                rel.get('Id', ''): rel.get('Target', '')
                for rel in rels_root
                if 'hyperlink' in rel.get('Type', '').lower()
            }
        except (KeyError, ET.ParseError):
            rel_map = {}

    root = ET.fromstring(doc_xml)
    body = root.find(f'{{{W}}}body')
    if body is None:
        return '<html><body></body></html>'

    html_parts = ['<html><body>']

    for para in body:
        ptag = para.tag.split('}')[-1] if '}' in para.tag else para.tag
        if ptag != 'p':
            continue

        para_html: list[str] = []
        field_state: str | None = None  # None → 'begin' → 'url' → 'display'
        current_url = ''
        display_parts: list[str] = []

        ppr = para.find(f'{{{W}}}pPr')
        p_tag = 'p'
        if ppr is not None:
            pstyle = ppr.find(f'{{{W}}}pStyle')
            if pstyle is not None:
                style_val = (pstyle.get(f'{{{W}}}val') or '').lower().replace(' ', '')
                p_tag = HEADING_MAP.get(style_val, 'p')

        for child in para:
            ctag = child.tag.split('}')[-1] if '}' in child.tag else child.tag

            if ctag == 'hyperlink':
                rid = child.get(f'{{{R_NS}}}id') or ''
                href = _unwrap_url(rel_map.get(rid, ''))
                texts = [t.text or '' for t in child.findall(f'.//{{{W}}}t')]
                text = _html_module.escape(''.join(texts))
                if href:
                    para_html.append(f'<a href="{_html_module.escape(href)}">{text}</a>')
                else:
                    para_html.append(text)
                continue

            if ctag != 'r':
                continue

            fldchar = child.find(f'{{{W}}}fldChar')
            instr_els = child.findall(f'{{{W}}}instrText')
            t_els = child.findall(f'{{{W}}}t')

            if fldchar is not None:
                ftype = fldchar.get(f'{{{W}}}fldCharType') or ''
                if ftype == 'begin':
                    field_state = 'begin'
                    current_url = ''
                    display_parts = []
                elif ftype == 'separate':
                    if field_state in ('begin', 'url'):
                        field_state = 'display'
                elif ftype == 'end':
                    if display_parts:
                        text = _html_module.escape(''.join(display_parts))
                        if current_url:
                            clean = _html_module.escape(_unwrap_url(current_url))
                            para_html.append(f'<a href="{clean}">{text}</a>')
                        else:
                            para_html.append(text)
                    field_state = None
                    current_url = ''
                    display_parts = []

            elif instr_els and field_state in ('begin', None):
                full_instr = ''.join(el.text or '' for el in instr_els)
                m = re.search(r'HYPERLINK\s+"?([^"\s]+)"?', full_instr, re.IGNORECASE)
                if m:
                    current_url = m.group(1).rstrip('"')
                    field_state = 'url'

            elif t_els:
                text = ''.join(el.text or '' for el in t_els)
                if field_state == 'display':
                    display_parts.append(text)
                elif field_state is None:
                    escaped = _html_module.escape(text)
                    rpr = child.find(f'{{{W}}}rPr')
                    if rpr is not None:
                        b_el = rpr.find(f'{{{W}}}b')
                        if b_el is not None:
                            b_val = b_el.get(f'{{{W}}}val', '1')
                            if b_val not in ('0', 'false', 'off'):
                                escaped = f'<strong>{escaped}</strong>'
                    para_html.append(escaped)

        content = ''.join(para_html)
        html_parts.append(f'<{p_tag}>{content}</{p_tag}>')

    html_parts.append('</body></html>')
    return _fix_google_redirects_in_html('\n'.join(html_parts))


def _pdf_to_html_via_osascript(input_path: Path) -> str:
    """Extract plain text from a PDF using macOS PDFKit (via osascript) and wrap as HTML."""
    script = (
        'use framework "PDFKit"\n'
        'use scripting additions\n'
        f'set pdfURL to current application\'s NSURL\'s fileURLWithPath_("{str(input_path)}")\n'
        'set pdfDoc to current application\'s PDFDocument\'s alloc()\'s initWithURL_(pdfURL)\n'
        'if pdfDoc is not missing value then\n'
        '    return (pdfDoc\'s |string|()) as text\n'
        'end if\n'
        'return ""\n'
    )
    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return _text_to_html(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"PDF text extraction via osascript failed for {input_path}: {exc}") from exc


def _pages_to_html_via_applescript(input_path: Path) -> str:
    """Export an Apple Pages document to DOCX via AppleScript, then parse to HTML."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_docx = Path(tmpdir) / 'exported.docx'
        script = (
            'tell application "Pages"\n'
            f'    set theDoc to open POSIX file "{str(input_path)}"\n'
            '    delay 1\n'
            f'    export theDoc to POSIX file "{str(out_docx)}" as Microsoft Word\n'
            '    close theDoc saving no\n'
            'end tell\n'
        )
        try:
            subprocess.run(
                ['osascript', '-e', script],
                capture_output=True, text=True, timeout=60, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"Apple Pages export failed for {input_path}: {exc}\n"
                "Make sure Apple Pages is installed and the file is a valid Pages document."
            ) from exc
        if not out_docx.exists():
            raise RuntimeError(f"Apple Pages export produced no file (expected at {out_docx})")
        return _docx_to_html(out_docx)


def normalize_ref_to_html(input_path: Path) -> Path:
    """Convert any supported reference format to an HTML file.

    Accepted formats: .html / .htm / .docx / .rtf / .txt / .pdf / .pages

    For non-HTML inputs the converted HTML is written alongside the source:
        input.docx  →  input.html  (same directory)

    The conversion is skipped when the .html output already exists and is
    newer than the source file.  For .html inputs, Google redirect hrefs are
    fixed in-place when present.

    Returns the path to the ready-to-use .html file.
    """
    input_path = input_path.expanduser().resolve()
    ext = input_path.suffix.lower()

    if ext in ('.html', '.htm'):
        raw = input_path.read_text(encoding='utf-8', errors='ignore')
        fixed = _fix_google_redirects_in_html(raw)
        if fixed != raw:
            input_path.write_text(fixed, encoding='utf-8')
            print(f"    Fixed Google redirect URLs in {input_path.name}")
        return input_path

    out_path = input_path.with_suffix('.html')

    if out_path.exists() and out_path.stat().st_mtime >= input_path.stat().st_mtime:
        print(f"    Normalized HTML already up-to-date: {out_path.name}")
        return out_path

    print(f"    Converting {ext} reference → HTML …")
    if ext == '.txt':
        raw = input_path.read_text(encoding='utf-8', errors='ignore')
        html_text = _text_to_html(raw)
    elif ext == '.rtf':
        html_text = _textutil_to_html(input_path)
    elif ext == '.docx':
        html_text = _docx_to_html(input_path)
    elif ext == '.pdf':
        html_text = _pdf_to_html_via_osascript(input_path)
    elif ext == '.pages':
        html_text = _pages_to_html_via_applescript(input_path)
    else:
        raise ValueError(
            f"Unsupported reference format: {ext}. "
            f"Supported: {', '.join(sorted({p.lstrip('*.') for p in _REF_GLOB_PATTERNS}))}"
        )

    out_path.write_text(html_text, encoding='utf-8')
    print(f"    Converted {input_path.name} → {out_path.name}")
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rush-video",
        help="Explicit rush video path. Defaults to the latest file in Universal_pipe/Rush.",
    )
    parser.add_argument(
        "--html",
        help=(
            "Explicit reference document path. Accepts .html, .htm, .docx, .rtf, .txt, .pdf, .pages. "
            "Defaults to the latest supported file in Universal_pipe/html."
        ),
    )
    parser.add_argument(
        "--comparser-only",
        action="store_true",
        help=(
            "Stop after the approximate/full comparser pipeline. "
            "Useful when debugging transcript alignment without generating inserts or XML bake steps."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    wait_seconds = _read_wait_seconds()
    rush_video = (
        resolve_path(Path(args.rush_video))
        if args.rush_video
        else latest_video_file(UNIVERSAL_RUSH_DIR)
    )
    media_timebase = probe_media_timebase(rush_video)
    print(f"\n==> Groq rush selection\n    Using rush video: {rush_video}")
    print(f"    Using media timebase: {media_timebase} fps")

    raw_ref = (
        resolve_path(Path(args.html))
        if args.html
        else latest_file(UNIVERSAL_HTML_DIR, _REF_GLOB_PATTERNS)
    )
    latest_html = normalize_ref_to_html(raw_ref)
    staged_html = stage_html_for_downloader(latest_html)
    downloader_output_dir = downloader_output_dir_for_html(staged_html)
    downloader_metadata_path: Path | None = None
    paper_output_dir = (INSERT_EDITOR_OUTPUT_ROOT.expanduser() / staged_html.stem / "paper_articles").resolve()
    print("\n==> Prepare insert downloads")
    print(f"    Reference document (raw): {raw_ref}")
    print(f"    Reference document (HTML): {latest_html}")
    print(f"    Downloader input: {staged_html}")
    print(f"    Downloader output folder: {downloader_output_dir}")
    if insert_assets_ready_for_html(latest_html, staged_html, UNIVERSAL_INSERT_DIR):
        print("    Insert assets already synchronized; skipping downloader.")
    else:
        run_python_script("Download insert references", INSERT_DOWNLOADER_SCRIPT)
        if not downloader_output_dir.exists():
            raise SystemExit("Insert downloader produced no output directory; aborting.")
        run_python_script("Cut videos with timecodes", CUT_VIDEO_WITH_TIMECODE_SCRIPT, ("--html", str(staged_html)))
        if paper_assets_exist_for_html(staged_html):
            paper_output_dir.mkdir(parents=True, exist_ok=True)
            run_python_script(
                "Animate paper inserts",
                PAPER_INSERT_ANIMATOR_SCRIPT,
                (
                    "--input-dir",
                    str(paper_animator_input_dir_for_html(staged_html)),
                    "--output-dir",
                    str(paper_output_dir),
                ),
            )
            print(f"    Paper article animations retained at: {paper_output_dir}")
        else:
            print("    No paper article inserts detected; skipping paper animation.")
        print(f"    Downloader output retained at: {downloader_output_dir}")
    downloader_metadata_path = resolve_downloader_metadata_path(downloader_output_dir)
    print(f"    Downloader metadata selected: {downloader_metadata_path}")

    try:
        reference_xml = latest_file(PREMIERE_XML_OUTPUT_DIR, ("*.xml",))
        print(f"\n==> Premiere reference\n    Latest XML detected: {reference_xml}")
    except FileNotFoundError:
        reference_xml = None
        print("\n==> Premiere reference\n    No XML detected; universal generator will rely on rush metadata.")

    wait_with_updates("Premiere processing pause", wait_seconds)

    transcript_csv, transcript_words_csv, transcript_raw_json = _run_groq_transcription(rush_video, media_timebase)
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"    Transcript CSV: {transcript_csv}")
    print(f"    Transcript words CSV: {transcript_words_csv}")
    if transcript_raw_json is not None:
        print(f"    Transcript raw JSON: {transcript_raw_json}")

    # Full comparser pipeline: approximate alignment + enrichment + precise timeline.
    (
        precise_comparer_csv,
        step1_xml_ready_csv,
        step1_diagnostic_csv,
        precise_annotations_csv,
        illustration_candidates_csv,
        approximate_run_dir,
    ) = run_approximate_full_pipeline(
        transcript_csv,
        transcript_words_csv,
        rush_video,
        latest_html,
    )

    final_comparer_path = (
        COMPARER_OUTPUT_DIR / f"{rush_video.stem}_{run_timestamp}_groq_html_comparison.csv"
    ).expanduser()
    final_comparer_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(precise_comparer_csv, final_comparer_path)
    final_precise_annotations_path = final_comparer_path.with_name(
        f"{final_comparer_path.stem}_precise_annotations.csv"
    )
    final_illustration_candidates_path = final_comparer_path.with_name(
        f"{final_comparer_path.stem}_illustration_candidates.csv"
    )
    if precise_annotations_csv is not None and precise_annotations_csv.exists():
        shutil.copy2(precise_annotations_csv, final_precise_annotations_path)
    if illustration_candidates_csv is not None and illustration_candidates_csv.exists():
        shutil.copy2(illustration_candidates_csv, final_illustration_candidates_path)
    print(f"    XML-ready stage-1 CSV retained at: {step1_xml_ready_csv}")
    print(f"    Diagnostic stage-1 CSV retained at: {step1_diagnostic_csv}")
    if precise_annotations_csv is not None and precise_annotations_csv.exists():
        print(f"    Precise annotations retained at: {final_precise_annotations_path}")
    if illustration_candidates_csv is not None and illustration_candidates_csv.exists():
        print(f"    Illustration candidates retained at: {final_illustration_candidates_path}")
    print(f"    Final precise comparer staged at: {final_comparer_path}")

    if args.comparser_only:
        print("\nComparser-only mode completed successfully.")
        print(f"    Approximate run dir: {approximate_run_dir}")
        print(f"    XML-ready stage-1 CSV: {step1_xml_ready_csv}")
        print(f"    Diagnostic stage-1 CSV: {step1_diagnostic_csv}")
        print(f"    Precise comparer CSV: {final_comparer_path}")
        return

    timed_format_csv, timed_ai_csv, timed_manifest_csv, timed_manifest_json, timed_ai_summary_path = run_timed_ai_illustrator(
        approximate_run_dir,
        latest_html,
        final_comparer_path,
    )
    if timed_manifest_csv is None or not timed_manifest_csv.exists():
        raise RuntimeError("timed_AI_illustrator did not produce the canonical insert timing manifest.")
    validate_precise_pipeline_outputs(
        approximate_run_dir,
        final_comparer_path,
        timed_ai_summary_path,
        timed_manifest_csv,
    )
    link_counts = _timed_manifest_link_counts(timed_manifest_csv)
    bold_insert_count = _timed_manifest_bold_count(timed_manifest_csv)
    total_link_assets = sum(link_counts.values())
    if total_link_assets > 0:
        print(
            "    Timed manifest link assets: "
            f"image_links={link_counts['image_links']}, "
            f"video_links={link_counts['video_links']}, "
            f"article_links={link_counts['article_links']}, "
            f"tweet_links={link_counts['tweet_links']}, "
            f"website_links={link_counts['website_links']}"
        )
        print("    Downloader link assets will be staged into Universal_pipe/Insert via insert_creator.")
    else:
        print("    Timed manifest contains no downloader link assets; final staging will skip them.")
    if bold_insert_count > 0:
        print(f"    Timed manifest bold inserts: {bold_insert_count}")
    else:
        print("    Timed manifest contains no bold inserts.")

    run_python_script(
        "Align inserts via groq_precise_placer",
        GROQ_PRECISE_PLACER_SCRIPT,
        ("--transcript", str(final_comparer_path), "--doc", str(latest_html), "--require-precise-comparer"),
    )

    generator_args = [
        "--csv",
        str(final_comparer_path),
        "--media",
        str(rush_video),
        "--fps",
        str(media_timebase),
    ]
    if reference_xml is not None:
        generator_args.extend(["--reference-xml", str(reference_xml)])
    run_python_script("Generate universal Premiere XML", UNIVERSAL_GENERATOR_SCRIPT, tuple(generator_args))
    latest_universal_xml = latest_file(
        UNIVERSAL_GENERATOR_OUTPUT_DIR,
        ("*_premiere.xml", "*.xml"),
    )
    print(f"    Latest universal XML detected: {latest_universal_xml}")

    insert_creator_args = [
        "--input-csv",
        str(final_comparer_path),
        "--timing-manifest",
        str(timed_manifest_csv) if timed_manifest_csv is not None else "",
        "--downloader-metadata",
        str(downloader_metadata_path) if downloader_metadata_path is not None else "",
        "--downloader-output-dir",
        str(downloader_output_dir),
        "--paper-output-dir",
        str(paper_output_dir),
        "--clean-insert-dir",
        "--ransom-titles-only",
    ]
    insert_creator_args = [arg for arg in insert_creator_args if arg != ""]
    run_python_script(
        "Generate Swisser inserts (including bold_sentence bold clips)",
        UNIFIED_INSERT_CREATOR_SCRIPT,
        tuple(insert_creator_args),
    )
    illustration_timing_csv = final_comparer_path.with_name(f"{final_comparer_path.stem}_illustration_timing.csv")
    if illustration_timing_csv.exists():
        print(f"    Illustration timing CSV: {illustration_timing_csv}")
    if timed_format_csv is not None and timed_format_csv.exists():
        print(f"    Timed format illustration CSV: {timed_format_csv}")
    if timed_ai_csv is not None and timed_ai_csv.exists():
        print(f"    Timed AI illustration CSV: {timed_ai_csv}")
    if timed_manifest_csv is not None and timed_manifest_csv.exists():
        print(f"    Timed insert timing manifest CSV: {timed_manifest_csv}")
    if timed_manifest_json is not None and timed_manifest_json.exists():
        print(f"    Timed insert timing manifest JSON: {timed_manifest_json}")
    if timed_ai_summary_path is not None and timed_ai_summary_path.exists():
        print(f"    Timed AI illustrator summary: {timed_ai_summary_path}")
    run_url_screenshot_inserts(final_comparer_path, UNIVERSAL_INSERT_DIR.expanduser())
    if timed_manifest_csv is not None:
        run_website_link_manifest_inserts(timed_manifest_csv, UNIVERSAL_INSERT_DIR.expanduser())
    run_plain_text_ref_inserts(latest_html, UNIVERSAL_INSERT_DIR.expanduser())
    validate_precise_insert_dir(UNIVERSAL_INSERT_DIR)
    print("\n==> Normalize insert formats for DaVinci compatibility")
    converted_count = normalize_inserts_for_davinci(UNIVERSAL_INSERT_DIR)
    if converted_count:
        print(f"    Converted {converted_count} incompatible file(s) to H264 MP4.")
    else:
        print("    All insert files are already DaVinci-compatible.")
    staged_insert_assets = _count_staged_insert_assets(UNIVERSAL_INSERT_DIR)
    zoom_manifest_path: Path | None = None
    if final_precise_annotations_path.exists():
        zoom_manifest_path = run_face_zoom_replacements(
            final_precise_annotations_path,
            final_comparer_path,
            media_timebase,
        )
    else:
        print("    No staged precise annotations CSV available; skipping face-aware zoom replacement stage.")

    print("\n==> Insert-shift zoom replacements")
    insert_shift_manifest_path = run_insert_shift_zooms(
        rush_path=rush_video,
        insert_dir=UNIVERSAL_INSERT_DIR,
        annotations_csv=final_precise_annotations_path if final_precise_annotations_path.exists() else None,
        media_timebase=media_timebase,
        rush_xml=latest_universal_xml,
    )

    print("\n==> Resolve unified Zoom types in comparer CSV")
    run_zoom_type_resolver(
        comparer_csv=final_comparer_path,
        annotations_csv=final_precise_annotations_path if final_precise_annotations_path.exists() else None,
        insert_dir=UNIVERSAL_INSERT_DIR,
        media_timebase=media_timebase,
    )

    render_source_xml = latest_universal_xml
    if bold_insert_count > 0:
        staged_bold_assets = _validate_bold_insert_assets(UNIVERSAL_INSERT_DIR, bold_insert_count)
        print(
            "    Staged bold insert clips: "
            + ", ".join(path.name for path in staged_bold_assets[:bold_insert_count])
        )
    # Stage intro zoom LAST — after insert_created_renamer's --clean-insert-dir wipe,
    # so it is present when program6_universal.py builds the XML.
    run_intro_zoom(
        rush_path=rush_video,
        insert_dir=UNIVERSAL_INSERT_DIR,
        reference_xml=latest_universal_xml,
    )
    run_outro_dip(
        rush_path=rush_video,
        insert_dir=UNIVERSAL_INSERT_DIR.expanduser(),
        reference_xml=latest_universal_xml,
    )

    print("\n==> Strip embedded timecodes from inserts")
    stripped_count = strip_insert_timecodes(UNIVERSAL_INSERT_DIR)
    if stripped_count:
        print(f"    Reset timecode on {stripped_count} insert file(s).")
    else:
        print("    No inserts carried a non-zero embedded timecode.")

    has_face_zoom = zoom_manifest_path is not None and zoom_manifest_path.exists()
    has_insert_shift = insert_shift_manifest_path is not None and insert_shift_manifest_path.exists()
    has_zoom_replacements = has_face_zoom or has_insert_shift

    # Merge face-zoom and insert-shift manifests so program6 receives a single file
    merged_zoom_manifest: Path | None = None
    if has_zoom_replacements:
        merged_zoom_manifest = (ZOOM_INSERT_SHIFT_REPLACEMENTS_DIR.expanduser() / "zoom_merged_manifest.json").resolve()
        merged_zoom_manifest = _merge_zoom_manifests(
            zoom_manifest_path,
            insert_shift_manifest_path,
            merged_zoom_manifest,
        )
        if merged_zoom_manifest and merged_zoom_manifest.exists():
            print(f"    Merged zoom manifest: {merged_zoom_manifest}")

    if staged_insert_assets <= 0 and not has_zoom_replacements:
        print("    No staged insert assets were generated; using no-insert fallback render path.")
        render_source_xml = _stage_fallback_final_xml(latest_universal_xml)
        print(f"    Fallback final XML staged at: {render_source_xml}")
    else:
        program6_args = [
            "--reference-xml",
            str(latest_universal_xml),
            "--comparison-csv",
            str(final_comparer_path),
            "--disable-cta-materialization",
        ]
        if merged_zoom_manifest and merged_zoom_manifest.exists():
            program6_args.extend(["--zoom-replacements-manifest", str(merged_zoom_manifest)])
        run_python_script(
            "Bake inserts into duplicate sequence",
            PROGRAM6_UNIVERSAL_SCRIPT,
            tuple(program6_args),
        )
        render_source_xml = latest_file(XML_INSERTOR_OUTPUT_DIR, ("*.xml",))
        print(f"    Latest insertor XML detected: {render_source_xml}")

    print("\n==> OTIO conversion + XML-driven FFmpeg render")
    run_python_script(
        "Convert insertor XML to OTIO",
        XML_TO_OTIO_SCRIPT,
        ("--xml", str(render_source_xml)),
    )
    otio_path = (FFMPEG_OUTPUT_DIR / f"{render_source_xml.stem}.otio").expanduser()
    if not otio_path.exists():
        raise FileNotFoundError(f"Expected OTIO not found: {otio_path}")
    mp4_destination = (FFMPEG_OUTPUT_DIR / f"{render_source_xml.stem}.mp4").expanduser()
    run_python_script(
        "Render normalized MP4 from insertor XML via ffmpeg",
        CREATE_VIDEO_FROM_XML_SCRIPT,
        (
            "--xml",
            str(render_source_xml),
            "--mp4",
            str(mp4_destination),
            "--keep-otio",
        ),
    )
    print(f"    Rendered MP4: {mp4_destination}")

    print("\nGroq + HTML pipeline completed successfully (Premiere import skipped by request).")


if __name__ == "__main__":
    main()
