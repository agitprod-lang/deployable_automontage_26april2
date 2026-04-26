#!/usr/bin/env python3
"""Rename insert_creator video outputs with timeline-based timestamps."""

from __future__ import annotations

import argparse
import csv
import filecmp
import json
import math
import os
import re
import subprocess
import shutil
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from processor import create_quote as cq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
TITLE_CREATOR_OUTPUT_DIR = PROJECT_ROOT / "program" / "title_creator" / "output"
APPROXIMATE_MATCHING_OUTPUT_DIR = CODE_BASE / "Comparser" / "Approximate_string_matching" / "output"
DEFAULT_INSERT_DIR = CODE_BASE / "swisser" / "Universal_pipe" / "Insert"
DEFAULT_NOUN_ARROW_SOURCE = (
    PROJECT_ROOT / "asset" / "arrows" / "circle_arrow_trans_centered__minus12db.mov"
)
DEFAULT_FRAME_RATE = 25.0
DEFAULT_TITLE_DELAY = 0.0
DEFAULT_3D_TRIM_START_SECONDS = 2
DEFAULT_3D_TRIM_END_SECONDS = 7
DEFAULT_INSERT_AUDIO_TARGET_PEAK_DB = -33.0
AUDIO_NORMALIZATION_TOLERANCE_DB = 1.0
FILENAME_AUDIO_EXTRA_GAIN_DB: dict[str, float] = {}
INSERT_TIMING_SIDECAR_DIRNAME = ".insert_timing"
COMMENT_TAG_VIDEO_PATH = (
    CODE_BASE / "swisser" / "Universal_pipe" / "asset" / "midjourney_animation" / "universal_fr" / "CTA" / "comment.mov"
)
SUBSCRIBE_TAG_VIDEO_PATH = (
    CODE_BASE / "swisser" / "Universal_pipe" / "asset" / "midjourney_animation" / "universal_fr" / "CTA" / "sabonner.mov"
)
TIPPEE_TAG_VIDEO_PATH = (
    CODE_BASE / "swisser" / "Universal_pipe" / "asset" / "midjourney_animation" / "universal_fr" / "CTA" / "money_give.mov"
)
INTRO_TAG_VIDEO_PATH = Path("/Users/mathieusandana/Desktop/AR/Génériques/Intro.mov")
VIDEO_LINK_TRANSITION_PATH = (
    CODE_BASE / "swisser" / "Universal_pipe" / "asset" / "transition" / "transitionfilburn.mov"
)
ILLUSTRATION_TIMING_HEADER = [
    "Asset Category",
    "Annotation Column",
    "Illustration Value",
    "Transcript #",
    "Row ID",
    "Edit Timestamp",
    "Edit Start Time",
    "Edit End Time",
    "Source Timestamp",
    "Source Start Time",
    "Source End Time",
    "Timing Source",
    "Timing Confidence",
    "Locator",
    "Asset Path",
    "Original Asset Path",
    "Manifest Path",
    "Entry ID",
    "Status",
]


@dataclass
class TimelineEntry:
    """Minimal metadata needed to rename a generated asset."""

    id: int
    value: str
    start_seconds: Optional[float]
    start_timecode: Optional[str]
    row_index: Optional[int]
    end_seconds: Optional[float] = None
    end_timecode: Optional[str] = None
    source_start_seconds: Optional[float] = None
    source_start_timecode: Optional[str] = None
    source_end_seconds: Optional[float] = None
    source_end_timecode: Optional[str] = None
    transcript_number: Optional[str] = None
    row_id: Optional[str] = None
    annotation_column: str = ""
    asset_category: str = ""
    locator: str = ""
    timing_source: str = "row_fallback"
    timing_confidence: str = "0.00"
    status: str = "row_fallback"
    text: str = ""
    reference_segment: str = ""


@dataclass
class TranscriptSegmentInfo:
    start_time: float
    end_time: float
    keep: bool


@dataclass
class PreciseTimingLookup:
    candidates: List[TimelineEntry]
    annotations: List[TimelineEntry]
    by_entry_id: Dict[Tuple[str, int], TimelineEntry]


KEEP_TRUE_MARKERS = {"x", "✓", "✔", "keep", "1", "yes"}
KEEP_FALSE_MARKERS = {"✗", "✕", "delete", "drop", "cut", "no", "0"}
MS_TIMESTAMP_PREFIX_RE = re.compile(
    r"^(?P<hours>\d{2})h(?P<minutes>\d{2})m(?P<seconds>\d{2})s(?P<millis>\d{3})ms",
    re.IGNORECASE,
)
LEGACY_TIMESTAMP_PREFIX_RE = re.compile(r"^(?P<minutes>\d+)m(?P<seconds>\d{2})", re.IGNORECASE)
RUN_STAMP_RE = re.compile(r"(?P<stamp>\d{8}_\d{6})")
PRECISION_CATEGORY_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "titles": ("Titles",),
    "ransom_gifs": ("Titles",),
    "quotes": ("Quote Extracted",),
    "quote_highlights": ("Quote Extracted",),
    "nouns": ("Person Mention",),
    "institution_images": ("Gov Institution",),
    "institution_transitions": ("Gov Institution",),
    "locations_3d": ("Location Mention",),
    "city_country": ("City Mention", "Country Mention"),
    "money": ("Money Mention",),
    "percent": ("Percent Mention",),
    "numbers": ("Number Mention", "Date Mention"),
    "calendar": ("Date Mention", "Number Mention"),
    "social_ranking_punctuation": (
        "Social Network Mention",
        "Ranking Mention",
        "Punctuation Signal",
    ),
}
USED_ILLUSTRATION_ROWS: List[Dict[str, str]] = []
ILLUSTRATION_ISSUES: List[Dict[str, str]] = []
MEDIA_DURATION_CACHE: Dict[Path, Optional[float]] = {}
DEFAULT_QUOTE_HIGHLIGHT_SHIFT = -1.2  # was -3.0; +1.8s (1s+20f at 25fps) delay added
DEFAULT_BOLD_SHIFT = 0.88  # 22 frames at 25fps
AUDIO_PEAK_CACHE: Dict[Path, Optional[float]] = {}
EDIT_TIMELINE_ROWS_CACHE: Dict[Path, Dict[str, List[Dict[str, str]]]] = {}
SINGLE_TITLE_MOV_ASSET_MODE = "single_title_mov"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename insert_creator video outputs (titles, numbers/dates, quotes) "
            "using transcript timestamps so clips stay in sync with precise_placer."
        )
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to a *_comparison*.csv file (defaults to the newest file in Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override insert_creator/output (defaults to the repo's insert_creator/output folder).",
    )
    parser.add_argument(
        "--insert-dir",
        type=Path,
        default=DEFAULT_INSERT_DIR,
        help="Folder where renamed video/GIF assets are moved (defaults to swisser/Universal_pipe/Insert).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=DEFAULT_FRAME_RATE,
        help="Frame rate used to interpret the HH:MM:SS:FF timestamps (default: 25).",
    )
    parser.add_argument(
        "--title-delay-seconds",
        type=float,
        default=DEFAULT_TITLE_DELAY,
        help="Optional global seconds to add to renamed asset timestamps (default: 0).",
    )
    parser.add_argument(
        "--quote-highlight-shift-seconds",
        type=float,
        default=DEFAULT_QUOTE_HIGHLIGHT_SHIFT,
        help="Seconds to shift quote highlight inserts relative to their detected time (default: -1.2).",
    )
    parser.add_argument(
        "--bold-shift-seconds",
        type=float,
        default=DEFAULT_BOLD_SHIFT,
        help="Seconds to add to bold (BLD) insert timestamps relative to their detected time (default: 0.88 = 22 frames at 25fps).",
    )
    parser.add_argument(
        "--timing-manifest",
        type=Path,
        help="Canonical timed_AI_illustrator manifest CSV used as the primary precise timing source.",
    )
    parser.add_argument(
        "--downloader-metadata",
        type=Path,
        help="Insert_downloader metadata JSON used to stage article/video/tweet assets.",
    )
    parser.add_argument(
        "--downloader-output-dir",
        type=Path,
        help="Insert_downloader output directory used to resolve downloaded assets.",
    )
    parser.add_argument(
        "--paper-output-dir",
        type=Path,
        help="Paper article animator output directory used to resolve animated title cards.",
    )
    parser.add_argument(
        "--clean-insert-dir",
        action="store_true",
        help="Delete existing Insert contents before final staging.",
    )
    parser.add_argument(
        "--skip-standard-title-videos",
        action="store_true",
        help="Do not stage normal title .mov assets; keep transparent ransom-title staging enabled.",
    )
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [
        path
        for path in directory.rglob("*comparison.csv")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No *_comparison.csv file found in {directory}")
    return candidates[0]


def load_csv(path: Path) -> tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise RuntimeError(f"{path} has no rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, name in enumerate(header):
        mapping[name.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' missing from CSV.")
    return header_map[key]


def split_pipe_values(value: str | None) -> List[str]:
    if not value:
        return []
    fragments = value.split("|")
    cleaned: List[str] = []
    for fragment in fragments:
        text = fragment.strip().strip('"').strip()
        if text:
            cleaned.append(text)
    return cleaned


def parse_timecode(value: str | None, frame_rate: float) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 4:
            hours, minutes, seconds, frames = map(int, parts)
            base = hours * 3600 + minutes * 60 + seconds
            return base + (frames / frame_rate if frame_rate > 0 else 0.0)
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


def format_timecode_from_seconds(seconds: Optional[float], frame_rate: float) -> Optional[str]:
    if seconds is None:
        return None
    total_frames = max(0, int(round(seconds * frame_rate)))
    fps = max(1, int(round(frame_rate)))
    total_seconds = total_frames // fps
    frames = total_frames % fps
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02}:{minutes:02}:{secs:02}:{frames:02}"


def human_timestamp_from_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    total_millis = max(0, int(round(seconds * 1000)))
    hours = total_millis // 3_600_000
    minutes = (total_millis % 3_600_000) // 60_000
    secs = (total_millis % 60_000) // 1000
    millis = total_millis % 1000
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def parse_human_timestamp(value: object) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    match = re.match(r"^(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<millis>\d{3})$", raw)
    if not match:
        return None
    return (
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + int(match.group("seconds"))
        + int(match.group("millis")) / 1000.0
    )


def parse_filename_timestamp_seconds(name: str) -> Optional[float]:
    ms_match = MS_TIMESTAMP_PREFIX_RE.search(name)
    if ms_match:
        hours = int(ms_match.group("hours"))
        minutes = int(ms_match.group("minutes"))
        seconds = int(ms_match.group("seconds"))
        millis = int(ms_match.group("millis"))
        return hours * 3600 + minutes * 60 + seconds + millis / 1000.0
    legacy_match = LEGACY_TIMESTAMP_PREFIX_RE.search(name)
    if legacy_match:
        minutes = int(legacy_match.group("minutes"))
        seconds = int(legacy_match.group("seconds"))
        return minutes * 60 + seconds
    return None


def build_row_id_map(rows: Sequence[Sequence[str]], header_map: Mapping[str, int]) -> Dict[int, str]:
    transcript_idx = header_map.get("transcript #")
    if transcript_idx is None:
        return {}
    mapping: Dict[int, str] = {}
    for row_number, row in enumerate(rows, start=1):
        if transcript_idx < len(row):
            mapping[row_number] = str(row[transcript_idx]).strip()
    return mapping


def load_dict_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        return [{key: value or "" for key, value in row.items()} for row in reader]


def _extract_run_stamp(value: object) -> Optional[str]:
    match = RUN_STAMP_RE.search(str(value or ""))
    if match is None:
        return None
    return match.group("stamp")


def _resolve_edit_timeline_path_for_comparison_csv(csv_path: Optional[Path]) -> Optional[Path]:
    if csv_path is None:
        return None
    run_stamp = _extract_run_stamp(csv_path.stem)
    if not run_stamp:
        return None
    candidate = APPROXIMATE_MATCHING_OUTPUT_DIR / run_stamp / "12_edit_timeline.csv"
    if candidate.exists():
        return candidate
    return None


def _load_edit_timeline_rows_by_row(path: Optional[Path]) -> Dict[str, List[Dict[str, str]]]:
    if path is None or not path.exists():
        return {}
    cached = EDIT_TIMELINE_ROWS_CACHE.get(path)
    if cached is not None:
        return cached
    rows_by_row: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in load_dict_rows(path):
        row_key = str(row.get("Row ID") or row.get("Transcript #") or "").strip()
        if row_key:
            rows_by_row[row_key].append(row)
    result = dict(rows_by_row)
    EDIT_TIMELINE_ROWS_CACHE[path] = result
    return result


def _precise_timing_entry_from_row(row: Mapping[str, str], asset_category: str = "") -> TimelineEntry:
    start_timecode = (row.get("Edit Start Time") or row.get("Start Time") or "").strip() or None
    end_timecode = (row.get("Edit End Time") or row.get("End Time") or "").strip() or None
    source_start_timecode = (row.get("Source Start Time") or "").strip() or None
    source_end_timecode = (row.get("Source End Time") or "").strip() or None
    asset_category_value = (row.get("Asset Category") or asset_category or "").strip()
    start_seconds = (
        parse_human_timestamp(row.get("Edit Timestamp"))
        or parse_human_timestamp(row.get("Start Time"))
        or parse_timecode(start_timecode, DEFAULT_FRAME_RATE)
    )
    end_seconds = (
        parse_human_timestamp(row.get("Edit End Timestamp"))
        or parse_human_timestamp(row.get("End Time"))
        or parse_timecode(end_timecode, DEFAULT_FRAME_RATE)
    )
    return TimelineEntry(
        id=int(str(row.get("Entry ID") or "0") or 0),
        value=str(
            row.get("Illustration Value")
            or row.get("Annotation Value")
            or row.get("Reference Word")
            or row.get("Transcript Word")
            or ""
        ).strip(),
        start_seconds=start_seconds,
        start_timecode=start_timecode,
        row_index=None,
        end_seconds=end_seconds,
        end_timecode=end_timecode,
        source_start_seconds=parse_human_timestamp(row.get("Source Timestamp")) or parse_timecode(source_start_timecode, DEFAULT_FRAME_RATE),
        source_start_timecode=source_start_timecode,
        source_end_seconds=parse_human_timestamp(row.get("Source End Timestamp")) or parse_timecode(source_end_timecode, DEFAULT_FRAME_RATE),
        source_end_timecode=source_end_timecode,
        transcript_number=(row.get("Transcript #") or "").strip() or None,
        row_id=(row.get("Row ID") or row.get("Transcript #") or "").strip() or None,
        annotation_column=(row.get("Annotation Column") or "").strip(),
        asset_category=asset_category_value,
        locator=(row.get("Locator") or "").strip(),
        timing_source=(row.get("Timing Source") or row.get("Timing Basis") or "row_fallback").strip() or "row_fallback",
        timing_confidence=(row.get("Timing Confidence") or row.get("Confidence") or "0.00").strip() or "0.00",
        status=(row.get("Status") or "").strip() or ("timed_manifest" if asset_category_value else "precise_candidate"),
        text=(row.get("Transcript Word") or row.get("Text") or "").strip(),
        reference_segment=(row.get("Reference Word") or row.get("Reference Segment") or "").strip(),
    )


def _build_precise_timing_lookup(csv_path: Path, timing_manifest_path: Optional[Path] = None) -> PreciseTimingLookup:
    candidates_path = csv_path.with_name(f"{csv_path.stem}_illustration_candidates.csv")
    annotations_path = csv_path.with_name(f"{csv_path.stem}_precise_annotations.csv")
    candidates: List[TimelineEntry] = []
    annotations: List[TimelineEntry] = []
    by_entry_id: Dict[Tuple[str, int], TimelineEntry] = {}
    if timing_manifest_path is not None and timing_manifest_path.exists():
        manifest_candidates = [
            _precise_timing_entry_from_row(row)
            for row in load_dict_rows(timing_manifest_path)
            if (row.get("Asset Category") or "").strip()
        ]
        for entry in manifest_candidates:
            candidates.append(entry)
            if entry.asset_category and entry.id:
                by_entry_id[(entry.asset_category, entry.id)] = entry
    if candidates_path.exists():
        candidates.extend(
            [
            _precise_timing_entry_from_row(row)
            for row in load_dict_rows(candidates_path)
            if (row.get("Illustration Value") or "").strip()
            ]
        )
    if annotations_path.exists():
        annotations = [
            _precise_timing_entry_from_row(row)
            for row in load_dict_rows(annotations_path)
            if (row.get("Annotation Value") or "").strip()
        ]
    return PreciseTimingLookup(candidates=candidates, annotations=annotations, by_entry_id=by_entry_id)


def _sorted_precise_entries(entries: Sequence[TimelineEntry]) -> List[TimelineEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            float(entry.timing_confidence or "0") * -1,
            entry.start_seconds if entry.start_seconds is not None else float("inf"),
            entry.row_id or "",
            normalize_text_key(entry.value),
        ),
    )


def _overlay_timing(target: TimelineEntry, precise: TimelineEntry) -> TimelineEntry:
    return TimelineEntry(
        id=target.id,
        value=target.value,
        start_seconds=precise.start_seconds if precise.start_seconds is not None else target.start_seconds,
        start_timecode=precise.start_timecode or target.start_timecode,
        row_index=target.row_index,
        end_seconds=precise.end_seconds,
        end_timecode=precise.end_timecode,
        source_start_seconds=precise.source_start_seconds,
        source_start_timecode=precise.source_start_timecode,
        source_end_seconds=precise.source_end_seconds,
        source_end_timecode=precise.source_end_timecode,
        transcript_number=target.transcript_number or precise.transcript_number,
        row_id=target.row_id or precise.row_id,
        annotation_column=precise.annotation_column or target.annotation_column,
        asset_category=precise.asset_category or target.asset_category,
        locator=precise.locator or target.locator,
        timing_source=precise.timing_source or target.timing_source,
        timing_confidence=precise.timing_confidence or target.timing_confidence,
        status=precise.status or "precise_candidate",
        text=precise.text or target.text,
        reference_segment=precise.reference_segment or target.reference_segment,
    )


def resolve_precise_timing(
    lookup: Optional[PreciseTimingLookup],
    asset_category: str,
    entry: TimelineEntry,
) -> TimelineEntry:
    if lookup is None:
        entry.status = "fallback_row_timing"
        entry.asset_category = asset_category or entry.asset_category
        return entry

    entry_key = (asset_category or entry.asset_category, entry.id)
    exact_entry = lookup.by_entry_id.get(entry_key)
    if exact_entry is not None:
        resolved = _overlay_timing(entry, exact_entry)
        resolved.status = "timed_manifest"
        return resolved

    normalized_value = normalize_text_key(entry.value)
    row_id = (entry.row_id or entry.transcript_number or "").strip()
    candidate_pool = [item for item in lookup.candidates if item.asset_category == asset_category]
    title_manifest_pool = [
        item
        for item in lookup.candidates
        if item.asset_category == "titles" and item.status == "timed_manifest"
    ]
    annotation_columns = PRECISION_CATEGORY_COLUMNS.get(asset_category, ())
    annotation_pool = [
        item
        for item in lookup.annotations
        if item.annotation_column in annotation_columns
    ]

    def exact_match(pool: Sequence[TimelineEntry], require_row: bool) -> Optional[TimelineEntry]:
        if not normalized_value:
            return None
        filtered = [
            item for item in pool
            if normalize_text_key(item.value) == normalized_value
            and (not require_row or (item.row_id or item.transcript_number or "") == row_id)
        ]
        if not filtered:
            return None
        return _sorted_precise_entries(filtered)[0]

    def row_category_match(pool: Sequence[TimelineEntry]) -> Optional[TimelineEntry]:
        if not row_id:
            return None
        filtered = [item for item in pool if (item.row_id or item.transcript_number or "") == row_id]
        if not filtered:
            return None
        return _sorted_precise_entries(filtered)[0]

    matched = (
        (
            exact_match(title_manifest_pool, True)
            or exact_match(title_manifest_pool, False)
            or exact_match(candidate_pool, True)
            or exact_match(candidate_pool, False)
            or row_category_match(candidate_pool)
            or exact_match(annotation_pool, True)
            or exact_match(annotation_pool, False)
            or row_category_match(annotation_pool)
        )
        if asset_category == "ransom_gifs"
        else (
            exact_match(candidate_pool, True)
            or exact_match(candidate_pool, False)
            or row_category_match(candidate_pool)
            or exact_match(annotation_pool, True)
            or exact_match(annotation_pool, False)
            or row_category_match(annotation_pool)
        )
    )
    if matched is None:
        entry.asset_category = asset_category or entry.asset_category
        entry.status = "fallback_row_timing"
        return entry
    resolved = _overlay_timing(entry, matched)
    resolved.asset_category = asset_category or resolved.asset_category
    resolved.status = "precise_candidate"
    if matched.status == "timed_manifest":
        resolved.status = "timed_manifest"
    return resolved


def resolve_value_column(header_map: Mapping[str, int], candidates: Sequence[str]) -> int:
    last_error: Optional[KeyError] = None
    for name in candidates:
        try:
            return require_column(header_map, name)
        except KeyError as exc:
            last_error = exc
    if last_error:
        raise last_error
    raise KeyError("No matching column name provided.")


def build_feature_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    column_names: Sequence[str],
    frame_rate: float,
    row_start_map: Optional[Mapping[int, Optional[float]]] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
    asset_category: str = "",
) -> List[TimelineEntry]:
    value_idx = resolve_value_column(header_map, column_names)
    start_idx = require_column(header_map, "Start Time")
    entries: List[TimelineEntry] = []
    for row_number, row in enumerate(rows, start=1):
        if value_idx >= len(row):
            continue
        values = split_pipe_values(row[value_idx])
        if not values:
            continue
        start_time = row[start_idx] if start_idx < len(row) else ""
        start_seconds = parse_timecode(start_time, frame_rate)
        if row_start_map is not None:
            mapped = row_start_map.get(row_number)
            if mapped is not None:
                start_seconds = mapped
        for value in values:
            entries.append(
                resolve_precise_timing(
                    precise_lookup,
                    asset_category,
                    TimelineEntry(
                    id=len(entries) + 1,
                    value=value,
                    start_seconds=start_seconds,
                    start_timecode=start_time.strip() or None,
                    row_index=row_number,
                    row_id=row_id_map.get(row_number) if row_id_map else None,
                    transcript_number=row_id_map.get(row_number) if row_id_map else None,
                    annotation_column=column_names[0] if column_names else "",
                    asset_category=asset_category,
                    status="row_fallback",
                    ),
                )
            )
    return entries


def interpret_keep_value(value: object) -> bool:
    marker = str(value or "").strip().lower()
    if not marker:
        return False
    if marker in KEEP_TRUE_MARKERS:
        return True
    if marker in KEEP_FALSE_MARKERS:
        return False
    return False


def build_transcript_segments(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
) -> Tuple[List[TranscriptSegmentInfo], Dict[int, float], Optional[float], bool]:
    start_idx = require_column(header_map, "Start Time")
    end_idx = require_column(header_map, "End Time")
    keep_idx = header_map.get("keep")
    segments: List[TranscriptSegmentInfo] = []
    row_times: Dict[int, float] = {}
    earliest: Optional[float] = None
    has_keep_column = keep_idx is not None

    for row_number, row in enumerate(rows, start=1):
        if start_idx >= len(row):
            continue
        start_seconds = parse_timecode(row[start_idx], frame_rate)
        end_seconds = parse_timecode(row[end_idx], frame_rate) if end_idx < len(row) else None
        if start_seconds is None:
            continue
        if end_seconds is None or end_seconds < start_seconds:
            end_seconds = start_seconds
        keep = True
        if keep_idx is not None and keep_idx < len(row):
            keep = interpret_keep_value(row[keep_idx])
        segments.append(TranscriptSegmentInfo(start_seconds, end_seconds, keep))
        row_times[row_number] = start_seconds
        if earliest is None or start_seconds < earliest:
            earliest = start_seconds

    return segments, row_times, earliest, has_keep_column


def compute_removed_intervals(
    segments: Sequence[TranscriptSegmentInfo],
) -> Tuple[List[Tuple[float, float]], float, float, float]:
    if not segments:
        return [], 0.0, 0.0, 0.0

    earliest = min(segment.start_time for segment in segments)
    cursor = earliest if earliest < 0 else 0.0
    intervals: List[Tuple[float, float]] = []
    drop_duration = 0.0
    silent_duration = 0.0

    for segment in segments:
        seg_start = segment.start_time
        seg_end = segment.end_time
        if seg_start > cursor:
            intervals.append((cursor, seg_start))
            silent_duration += seg_start - cursor
            cursor = seg_start
        if not segment.keep:
            intervals.append((seg_start, seg_end))
            drop_duration += max(0.0, seg_end - seg_start)
        cursor = max(cursor, seg_end)

    if not intervals:
        return [], drop_duration, silent_duration, 0.0

    intervals.sort()
    merged: List[Tuple[float, float]] = []
    for start, end in intervals:
        if not merged:
            merged.append((start, end))
            continue
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    merged = [(start, end) for start, end in merged if end > start]
    total_removed = sum(end - start for start, end in merged)
    return merged, drop_duration, silent_duration, total_removed


def close_removed_gaps(timestamp: float, removed_intervals: Sequence[Tuple[float, float]]) -> float:
    if not removed_intervals:
        return max(0.0, timestamp)

    removed = 0.0
    for start, end in removed_intervals:
        if timestamp >= end:
            removed += end - start
            continue
        if timestamp <= start:
            break
        removed += timestamp - start
        break
    return max(0.0, timestamp - removed)


def build_row_start_map(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
) -> Dict[int, Optional[float]]:
    segments, row_times, earliest, has_keep_column = build_transcript_segments(
        rows, header_map, frame_rate
    )
    if not row_times:
        return {}
    base_offset = 0.0
    if earliest is not None and earliest >= 3600:
        base_offset = math.floor(earliest / 3600) * 3600
    adjusted_segments = [
        TranscriptSegmentInfo(seg.start_time - base_offset, seg.end_time - base_offset, seg.keep)
        for seg in segments
    ]
    removed_intervals, drop_duration, silent_duration, total_removed = compute_removed_intervals(
        adjusted_segments
    )
    if total_removed > 0:
        details: List[str] = []
        if drop_duration > 0:
            details.append(f"{drop_duration:.2f}s dropped via Keep column")
        if silent_duration > 0:
            details.append(f"{silent_duration:.2f}s of silent gaps")
        suffix = f" ({', '.join(details)})" if details else ""
        print(f"Closing transcript gaps; removing {total_removed:.2f}s{suffix}.")
    else:
        if has_keep_column:
            print(
                "Keep column detected but no deletions or timing gaps were found; "
                "timestamps will preserve original spacing."
            )
        else:
            print("No deletions or timing gaps detected; timestamps will preserve original spacing.")
    row_map: Dict[int, Optional[float]] = {}
    min_adjusted: Optional[float] = None
    for row_number, raw_start in row_times.items():
        adjusted = close_removed_gaps(raw_start - base_offset, removed_intervals)
        row_map[row_number] = adjusted
        if adjusted is not None:
            min_adjusted = adjusted if min_adjusted is None else min(min_adjusted, adjusted)
    if min_adjusted is not None and min_adjusted > 0:
        for key, value in row_map.items():
            if value is not None:
                row_map[key] = max(0.0, value - min_adjusted)
        print(
            f"Normalized earliest transcript timestamp by subtracting {min_adjusted:.2f}s so assets start at 0m00."
        )
    if row_map:
        last_known: Optional[float] = None
        total_rows = len(rows)
        for row_number in range(1, total_rows + 1):
            if row_number in row_map:
                value = row_map[row_number]
                if value is not None:
                    last_known = value
            elif last_known is not None:
                row_map[row_number] = last_known
    return row_map


def coerce_row_indices(raw_rows: Iterable[object]) -> List[int]:
    result: List[int] = []
    for value in raw_rows:
        if isinstance(value, int):
            result.append(value)
            continue
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            continue
        result.append(number)
    return result


def first_valid_row_seconds(rows: Sequence[int], row_start_map: Mapping[int, Optional[float]]) -> Optional[float]:
    for row in rows:
        seconds = row_start_map.get(row)
        if seconds is not None:
            return seconds
    return None


def seconds_to_label(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours = total_millis // 3_600_000
    minutes = (total_millis % 3_600_000) // 60_000
    secs = (total_millis % 60_000) // 1000
    millis = total_millis % 1000
    return f"{hours:02d}h{minutes:02d}m{secs:02d}s{millis:03d}ms"


def seconds_to_trim_suffix(seconds: int) -> str:
    total_seconds = max(0, int(seconds))
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes:02d}:{secs:02d}"


def build_trim_suffix(start_seconds: int, end_seconds: int) -> str:
    return f"{seconds_to_trim_suffix(start_seconds)}-{seconds_to_trim_suffix(end_seconds)}"


def normalize_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"\s+", "_", ascii_name.strip())
    ascii_name = re.sub(r"_+", "_", ascii_name)
    return ascii_name or "asset"


def strip_existing_suffix(stem: str, has_suffix: bool) -> str:
    if not stem or not has_suffix:
        return stem
    cleaned = re.sub(
        r"(?:[_\-\s]+)?(INSERT|EXTRAIT|EXTRACT|EXCERPT|DIRECT)"
        r"(?:[_\-\s]+[0-9:.,\-]+)?$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.rstrip("_- ")
    return cleaned if cleaned else stem


def normalize_text_key(value: object) -> str:
    text = str(value or "")
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
    return cleaned


def _normalize_word_tokens(value: object) -> List[str]:
    normalized = normalize_text_key(value)
    if not normalized:
        return []
    return [token for token in normalized.split() if token]


def build_target_name(
    original_name: str,
    timestamp_label: str,
    index: int,
    suffix_text: Optional[str],
) -> str:
    prefix = f"{timestamp_label}_{index}"
    stem, ext = os.path.splitext(original_name)
    remainder = re.sub(
        r"^(?:(?:\d{2}h\d{2}m\d{2}s\d{3}ms)|(?:\d+m\d{2}))[_\s]+(?:\d+_)?",
        "",
        stem,
        count=1,
    )
    remainder = remainder or stem
    remainder = strip_existing_suffix(remainder, bool(suffix_text))
    if not remainder:
        remainder = "asset"
    parts = [prefix, remainder]
    base = "_".join(part for part in parts if part)
    if suffix_text:
        base = f"{base}_{suffix_text}"
    return normalize_filename(f"{base}{ext}")


def build_insert_timing_sidecar_path(path: Path) -> Path:
    return path.parent / INSERT_TIMING_SIDECAR_DIRNAME / f"{path.name}.json"


def clear_insert_timing_sidecar(path: Path) -> None:
    build_insert_timing_sidecar_path(path).unlink(missing_ok=True)


def timeline_span_duration_seconds(entry: TimelineEntry) -> Optional[float]:
    if entry.start_seconds is None or entry.end_seconds is None:
        return None
    duration = entry.end_seconds - entry.start_seconds
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def write_insert_timing_sidecar(
    path: Path,
    entry: TimelineEntry,
    requested_duration_seconds: Optional[float] = None,
    extra_fields: Optional[Mapping[str, object]] = None,
) -> None:
    sidecar_path = build_insert_timing_sidecar_path(path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entry_id": entry.id,
        "asset_category": entry.asset_category,
        "start_seconds": entry.start_seconds,
        "end_seconds": entry.end_seconds,
    }
    if requested_duration_seconds is not None and requested_duration_seconds > 0:
        payload["requested_duration_seconds"] = requested_duration_seconds
    if extra_fields:
        payload.update(extra_fields)
    sidecar_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_manifest(path: Path) -> MutableMapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_manifest(path: Path, data: Mapping[str, object]) -> None:
    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(serialized, encoding="utf-8")


def apply_delay(seconds: Optional[float], delay: float) -> Optional[float]:
    if seconds is None:
        return None
    return max(0.0, seconds + delay)


def parse_seconds_value(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def probe_media_duration(path: Path) -> Optional[float]:
    resolved = path.expanduser().resolve()
    if resolved in MEDIA_DURATION_CACHE:
        return MEDIA_DURATION_CACHE[resolved]
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(resolved),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        value = float((result.stdout or "").strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        value = None
    if value is not None and (not math.isfinite(value) or value <= 0):
        value = None
    MEDIA_DURATION_CACHE[resolved] = value
    return value


def analyze_audio_peak_db(path: Path) -> Optional[float]:
    resolved = path.resolve()
    if resolved in AUDIO_PEAK_CACHE:
        return AUDIO_PEAK_CACHE[resolved]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(resolved),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        AUDIO_PEAK_CACHE[resolved] = None
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?)|inf|-inf)\s*dB", output, re.IGNORECASE)
    if not match:
        AUDIO_PEAK_CACHE[resolved] = None
        return None
    token = match.group(1).lower()
    if token in {"inf", "-inf"}:
        peak_db = None
    else:
        try:
            peak_db = float(token)
        except ValueError:
            peak_db = None
    AUDIO_PEAK_CACHE[resolved] = peak_db
    return peak_db


def build_normalized_audio_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}__peaknorm{path.suffix}")


def apply_extra_gain(path: Path, gain_db: float) -> Path:
    """Return a copy of *path* with *gain_db* applied; caches result alongside source."""
    sign = "plus" if gain_db >= 0 else "minus"
    tag = f"__gain{sign}{abs(int(gain_db))}db"
    out = path.with_name(f"{path.stem}{tag}{path.suffix}")
    try:
        if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
            return out
    except OSError:
        pass
    tmp = out.with_name(f"{out.stem}.tmp{out.suffix}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path),
        "-map", "0", "-c:v", "copy", "-c:s", "copy",
        "-af", f"volume={gain_db:.2f}dB",
        "-c:a", "pcm_s16le",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        tmp.replace(out)
        AUDIO_PEAK_CACHE.pop(out.resolve(), None)
        print(f"  🔊 Extra gain {gain_db:+.1f} dB applied -> {out.name}")
        return out
    except (subprocess.CalledProcessError, OSError):
        tmp.unlink(missing_ok=True)
        return path


def should_reduce_insert_audio(path: Path) -> bool:
    return path.suffix.lower() in {".mov", ".mp4", ".m4v", ".webm"}


def prepare_reduced_audio_source(
    path: Path,
    *,
    target_peak_db: float = DEFAULT_INSERT_AUDIO_TARGET_PEAK_DB,
) -> Path:
    if not path.exists() or not should_reduce_insert_audio(path):
        return path
    peak_db = analyze_audio_peak_db(path)
    if peak_db is None or not math.isfinite(peak_db):
        return path
    required_delta_db = target_peak_db - peak_db
    if required_delta_db >= -AUDIO_NORMALIZATION_TOLERANCE_DB:
        return path
    normalized_path = build_normalized_audio_path(path)
    try:
        if normalized_path.exists() and normalized_path.stat().st_mtime >= path.stat().st_mtime:
            return normalized_path
    except OSError:
        pass
    temp_path = normalized_path.with_name(f"{normalized_path.stem}.tmp{normalized_path.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-af",
        f"volume={required_delta_db:.2f}dB",
        "-c:a",
        "pcm_s16le",
        str(temp_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        temp_path.replace(normalized_path)
        AUDIO_PEAK_CACHE.pop(normalized_path.resolve(), None)
        print(
            f"  🔉 Reduced audio peak for {path.name} by {abs(required_delta_db):.1f} dB "
            f"-> {normalized_path.name}"
        )
        return normalized_path
    except (subprocess.CalledProcessError, OSError):
        temp_path.unlink(missing_ok=True)
        return path


def rename_path_for_entry(
    path: Path,
    entry: TimelineEntry,
    delay_seconds: float,
    suffix_text: Optional[str] = None,
    destination_dir: Optional[Path] = None,
) -> Optional[Path]:
    source_path = path  # normalization ceiling disabled; source audio passes through as-is
    adjusted_seconds = apply_delay(entry.start_seconds, delay_seconds)
    if adjusted_seconds is None:
        context = f"row {entry.row_index}" if entry.row_index is not None else "unknown row"
        print(f"  ⚠️  Missing timing for entry #{entry.id} ({context}); {path.name} left untouched.")
        return None
    if not source_path.exists():
        print(f"  ⚠️  Path not found for entry #{entry.id}: {path}")
        return None
    timestamp_label = seconds_to_label(adjusted_seconds)
    target_name = build_target_name(path.name, timestamp_label, entry.id, suffix_text)
    if destination_dir is not None:
        destination_dir.mkdir(parents=True, exist_ok=True)
        new_path = destination_dir / target_name
        if source_path == new_path:
            print(f"  ⏭  {source_path.name} already aligned to {timestamp_label}.")
            return source_path
        if new_path.exists():
            try:
                if filecmp.cmp(source_path, new_path, shallow=False):
                    print(
                        f"  ⏭  Destination already has {new_path.name}; source matches so it is left as-is."
                    )
                    return new_path
            except OSError:
                pass
            raise FileExistsError(f"Cannot copy {source_path} -> {new_path}: target exists.")
        shutil.copy2(source_path, new_path)
        print(f"  ✅ {source_path.name} copied to {new_path.name} ({timestamp_label})")
        return new_path
    new_path = path.with_name(target_name)
    if source_path == new_path:
        print(f"  ⏭  {source_path.name} already aligned to {timestamp_label}.")
        return source_path
    if new_path.exists():
        raise FileExistsError(f"Cannot rename {source_path} -> {new_path}: target exists.")
    source_path.rename(new_path)
    print(f"  ✅ {source_path.name} -> {new_path.name} ({timestamp_label})")
    return new_path


def reset_illustration_tracking() -> None:
    USED_ILLUSTRATION_ROWS.clear()
    ILLUSTRATION_ISSUES.clear()


def _timecode_or_formatted(value: Optional[str], seconds: Optional[float], frame_rate: float) -> str:
    if value:
        return value
    return format_timecode_from_seconds(seconds, frame_rate) or ""


def _used_asset_row(
    entry: TimelineEntry,
    asset_path: Optional[Path],
    original_path: Optional[Path],
    manifest_path: Optional[Path],
    frame_rate: float,
) -> Dict[str, str]:
    edit_start_time = _timecode_or_formatted(entry.start_timecode, entry.start_seconds, frame_rate)
    edit_end_time = _timecode_or_formatted(entry.end_timecode, entry.end_seconds, frame_rate)
    source_start_time = _timecode_or_formatted(entry.source_start_timecode, entry.source_start_seconds, frame_rate)
    source_end_time = _timecode_or_formatted(entry.source_end_timecode, entry.source_end_seconds, frame_rate)
    return {
        "Asset Category": entry.asset_category,
        "Annotation Column": entry.annotation_column,
        "Illustration Value": entry.value,
        "Transcript #": entry.transcript_number or entry.row_id or "",
        "Row ID": entry.row_id or entry.transcript_number or "",
        "Edit Timestamp": human_timestamp_from_seconds(entry.start_seconds),
        "Edit Start Time": edit_start_time,
        "Edit End Time": edit_end_time,
        "Source Timestamp": human_timestamp_from_seconds(entry.source_start_seconds),
        "Source Start Time": source_start_time,
        "Source End Time": source_end_time,
        "Timing Source": entry.timing_source,
        "Timing Confidence": entry.timing_confidence,
        "Locator": entry.locator,
        "Asset Path": str(asset_path) if asset_path else "",
        "Original Asset Path": str(original_path) if original_path else "",
        "Manifest Path": str(manifest_path) if manifest_path else "",
        "Entry ID": str(entry.id),
        "Status": entry.status or "precise_candidate",
    }


def record_issue(
    entry: TimelineEntry,
    manifest_path: Optional[Path],
    frame_rate: float,
    status: str,
    original_path: Optional[Path] = None,
    asset_path: Optional[Path] = None,
) -> None:
    row = _used_asset_row(
        entry=entry,
        asset_path=asset_path,
        original_path=original_path,
        manifest_path=manifest_path,
        frame_rate=frame_rate,
    )
    row["Status"] = status
    ILLUSTRATION_ISSUES.append(row)


def rename_and_record(
    asset_path: Path,
    entry: TimelineEntry,
    delay_seconds: float,
    frame_rate: float,
    manifest_path: Optional[Path],
    suffix_text: Optional[str] = None,
    destination_dir: Optional[Path] = None,
    requested_duration_seconds: Optional[float] = None,
    sidecar_extra_fields: Optional[Mapping[str, object]] = None,
) -> Optional[Path]:
    original_path = asset_path
    renamed_path = rename_path_for_entry(
        asset_path,
        entry,
        delay_seconds,
        suffix_text=suffix_text,
        destination_dir=destination_dir,
    )
    if renamed_path is None:
        status = "missing_timing" if entry.start_seconds is None else "missing_asset" if not asset_path.exists() else "rename_failed"
        record_issue(
            entry=entry,
            manifest_path=manifest_path,
            frame_rate=frame_rate,
            status=status,
            original_path=original_path,
            asset_path=asset_path if asset_path.exists() else None,
        )
        return None
    asset_mode = ""
    if sidecar_extra_fields:
        asset_mode = str(sidecar_extra_fields.get("asset_mode") or "").strip().lower()
    should_write_sidecar = (
        (
            asset_mode == SINGLE_TITLE_MOV_ASSET_MODE
            and entry.start_seconds is not None
        )
        or
        (requested_duration_seconds is not None and requested_duration_seconds > 0)
        or (
            entry.asset_category in {"titles", "ransom_gifs"}
            and entry.start_seconds is not None
            and entry.end_seconds is not None
            and entry.end_seconds > entry.start_seconds
        )
    )
    if should_write_sidecar:
        write_insert_timing_sidecar(
            renamed_path,
            entry,
            requested_duration_seconds,
            extra_fields=sidecar_extra_fields,
        )
    else:
        clear_insert_timing_sidecar(renamed_path)
    USED_ILLUSTRATION_ROWS.append(
        _used_asset_row(
            entry=entry,
            asset_path=renamed_path,
            original_path=original_path,
            manifest_path=manifest_path,
            frame_rate=frame_rate,
        )
    )
    if entry.status == "fallback_row_timing":
        record_issue(
            entry=entry,
            manifest_path=manifest_path,
            frame_rate=frame_rate,
            status="fallback_row_timing",
            original_path=original_path,
            asset_path=renamed_path,
        )
    return renamed_path


def write_illustration_timing_reports(csv_path: Path, output_dir: Path) -> tuple[Path, Path]:
    rows = sorted(
        USED_ILLUSTRATION_ROWS,
        key=lambda row: (
            parse_human_timestamp(row.get("Edit Timestamp")) or float("inf"),
            int(str(row.get("Entry ID") or "0") or 0),
            row.get("Asset Path", ""),
        ),
    )
    canonical_csv = csv_path.with_name(f"{csv_path.stem}_illustration_timing.csv")
    canonical_json = csv_path.with_name(f"{csv_path.stem}_illustration_timing.json")
    output_dir.mkdir(parents=True, exist_ok=True)
    local_csv = output_dir / canonical_csv.name
    local_json = output_dir / canonical_json.name
    for target in (canonical_csv, local_csv):
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ILLUSTRATION_TIMING_HEADER, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
    payload = {
        "rows": rows,
        "issues": ILLUSTRATION_ISSUES,
        "summary": {
            "used_assets": len(rows),
            "issues": len(ILLUSTRATION_ISSUES),
        },
    }
    for target in (canonical_json, local_json):
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return canonical_csv, canonical_json


def clear_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    for child in list(path.iterdir()):
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)


def write_insert_list(insert_dir: Path) -> Path:
    entries = [
        path.name
        for path in insert_dir.iterdir()
        if path.is_file() and path.name != "list.txt" and parse_filename_timestamp_seconds(path.name) is not None
    ]
    entries.sort(key=lambda name: (parse_filename_timestamp_seconds(name) or 0.0, name.lower()))
    list_path = insert_dir / "list.txt"
    list_path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return list_path


def load_timing_manifest_rows(path: Optional[Path]) -> List[Dict[str, str]]:
    if path is None or not path.exists():
        return []
    return load_dict_rows(path)


def _title_entries_from_timing_manifest(manifest_rows: Sequence[Mapping[str, str]]) -> List[TimelineEntry]:
    entries: List[TimelineEntry] = []
    for local_id, row in enumerate(manifest_rows, start=1):
        if (row.get("Asset Category") or "").strip() != "titles":
            continue
        value = str(row.get("Reference Word") or row.get("Illustration Value") or "").strip()
        if not value:
            continue
        entries.append(
            TimelineEntry(
                id=len(entries) + 1,
                value=value,
                start_seconds=parse_human_timestamp(row.get("Start Time")) or parse_timecode(row.get("Start Time"), DEFAULT_FRAME_RATE),
                start_timecode=(row.get("Start Time") or "").strip() or None,
                row_index=local_id,
                end_seconds=parse_human_timestamp(row.get("End Time")) or parse_timecode(row.get("End Time"), DEFAULT_FRAME_RATE),
                end_timecode=(row.get("End Time") or "").strip() or None,
                transcript_number=(row.get("Transcript #") or "").strip() or None,
                row_id=(row.get("Row ID") or row.get("Transcript #") or "").strip() or None,
                annotation_column="Titles",
                asset_category="titles",
                locator=(row.get("Locator") or "").strip(),
                timing_source=(row.get("Timing Basis") or "timed_manifest").strip() or "timed_manifest",
                timing_confidence="1.00",
                status="timed_manifest",
            )
        )
    return entries


def _timing_entry_from_manifest_row(row: Mapping[str, str]) -> TimelineEntry:
    return _precise_timing_entry_from_row(row)


def _match_reference_segment_rows(
    edit_rows: Sequence[Mapping[str, str]],
    reference_segment: str,
) -> List[Mapping[str, str]]:
    reference_tokens = _normalize_word_tokens(reference_segment)
    if not reference_tokens:
        return []
    matched_rows: List[Mapping[str, str]] = []
    token_index = 0
    for row in edit_rows:
        if token_index >= len(reference_tokens):
            break
        row_tokens = _normalize_word_tokens(row.get("Reference Token"))
        if not row_tokens:
            continue
        next_tokens = reference_tokens[token_index: token_index + len(row_tokens)]
        if next_tokens != row_tokens:
            continue
        matched_rows.append(row)
        token_index += len(row_tokens)
        if token_index >= len(reference_tokens):
            break
    if token_index != len(reference_tokens):
        return []
    return matched_rows


def _excerpt_sentence_anchor_seconds(
    manifest_row: Mapping[str, str],
    frame_rate: float,
    edit_timeline_rows_by_row: Mapping[str, Sequence[Mapping[str, str]]],
) -> Optional[float]:
    link_kind = (manifest_row.get("Link Kind") or "").strip().lower()
    illustration_type = (manifest_row.get("Illustration Type") or "").strip().lower()
    if link_kind != "video_excerpt" and illustration_type != "video_link_excerpt":
        return None
    row_key = str(manifest_row.get("Row ID") or manifest_row.get("Transcript #") or "").strip()
    if not row_key:
        return None
    reference_segment = str(
        manifest_row.get("Reference Word")
        or manifest_row.get("Reference Segment")
        or ""
    ).strip()
    if not reference_segment:
        return None
    edit_rows = edit_timeline_rows_by_row.get(row_key)
    if not edit_rows:
        return None
    matched_rows = _match_reference_segment_rows(edit_rows, reference_segment)
    if not matched_rows:
        return None
    first_ref_only_index: Optional[int] = None
    for idx, row in enumerate(matched_rows):
        if (row.get("Alignment Type") or "").strip().upper() == "REF_ONLY":
            first_ref_only_index = idx
            break
    if first_ref_only_index is None or first_ref_only_index <= 0:
        return None
    trailing_rows = matched_rows[first_ref_only_index:]
    if any((row.get("Alignment Type") or "").strip().upper() != "REF_ONLY" for row in trailing_rows):
        return None
    for row in reversed(matched_rows[:first_ref_only_index]):
        anchor_seconds = parse_timecode((row.get("Edit End Time") or "").strip(), frame_rate)
        if anchor_seconds is not None:
            return anchor_seconds
    return None


def _refine_video_link_timing(
    manifest_row: Mapping[str, str],
    entry: TimelineEntry,
    frame_rate: float,
    edit_timeline_rows_by_row: Mapping[str, Sequence[Mapping[str, str]]],
) -> TimelineEntry:
    anchor_seconds = _excerpt_sentence_anchor_seconds(
        manifest_row,
        frame_rate,
        edit_timeline_rows_by_row,
    )
    if anchor_seconds is None:
        return entry
    span_duration = timeline_span_duration_seconds(entry)
    refined_end_seconds = (
        anchor_seconds + span_duration if span_duration is not None else entry.end_seconds
    )
    refined_locator = entry.locator or "reference_sentence_end"
    if entry.locator and "reference_sentence_end" not in entry.locator:
        refined_locator = f"{entry.locator}+reference_sentence_end"
    refined_timing_source = entry.timing_source or "excerpt_sentence_anchor"
    if "excerpt_sentence_anchor" not in refined_timing_source:
        refined_timing_source = f"{refined_timing_source}+excerpt_sentence_anchor"
    return replace(
        entry,
        start_seconds=anchor_seconds,
        start_timecode=format_timecode_from_seconds(anchor_seconds, frame_rate),
        end_seconds=refined_end_seconds,
        end_timecode=(
            format_timecode_from_seconds(refined_end_seconds, frame_rate)
            if refined_end_seconds is not None
            else entry.end_timecode
        ),
        locator=refined_locator,
        timing_source=refined_timing_source,
    )


def _match_downloader_entry(
    manifest_row: Mapping[str, str],
    metadata_entries: Sequence[Mapping[str, object]],
) -> Optional[Mapping[str, object]]:
    html_insert_index = (manifest_row.get("HTML Insert Index") or "").strip()
    link_url = (manifest_row.get("Link URL") or "").strip()
    if html_insert_index:
        for entry in metadata_entries:
            if str(entry.get("insert_index") or "").strip() == html_insert_index:
                return entry
    if link_url:
        link_url_lower = link_url.casefold()
        for entry in metadata_entries:
            source_url = str(entry.get("source_url") or "").strip()
            if source_url and source_url.casefold() == link_url_lower:
                return entry
    return None


def _resolve_article_asset_path(
    metadata_entry: Mapping[str, object],
    downloader_output_dir: Optional[Path],
    paper_output_dir: Optional[Path],
) -> Optional[Path]:
    title_card_value = str(metadata_entry.get("title_card_video") or "").strip()
    if not title_card_value:
        return None
    raw_name = Path(title_card_value).name
    candidates: List[Path] = []
    if paper_output_dir is not None:
        candidates.append(paper_output_dir / raw_name)
    if downloader_output_dir is not None:
        candidates.extend(_downloader_asset_candidates(downloader_output_dir, title_card_value, raw_name))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _downloader_asset_candidates(
    downloader_output_dir: Path,
    metadata_value: str,
    raw_name: Optional[str] = None,
) -> List[Path]:
    value = metadata_value.strip()
    if not value:
        return []

    normalized = value.replace("\\", "/").lstrip("/")
    candidates: List[Path] = []

    # Some metadata is rooted at output/rtf/... while newer records are output/<html_stem>/...
    for prefix in ("output/rtf/", "output/"):
        if normalized.startswith(prefix):
            relative = normalized[len(prefix):]
            candidates.append(downloader_output_dir / relative)

    parts = Path(normalized).parts
    if parts:
        if parts[0] == "output":
            parts = parts[1:]
        if parts and parts[0] == downloader_output_dir.name:
            parts = parts[1:]
        if parts:
            candidates.append(downloader_output_dir.joinpath(*parts))

    candidates.append(downloader_output_dir / normalized)
    if raw_name:
        candidates.append(downloader_output_dir / raw_name)

    deduped: List[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def _resolve_downloaded_media_path(
    metadata_entry: Mapping[str, object],
    downloader_output_dir: Optional[Path],
    field_name: str = "downloaded_file",
) -> Optional[Path]:
    if downloader_output_dir is None:
        return None
    value = str(metadata_entry.get(field_name) or "").strip()
    if not value:
        return None
    raw_name = Path(value).name
    for candidate in _downloader_asset_candidates(downloader_output_dir, value, raw_name):
        if candidate.exists():
            return candidate
    return None


def _resolve_link_asset_path(
    category: str,
    metadata_entry: Mapping[str, object],
    downloader_output_dir: Optional[Path],
    paper_output_dir: Optional[Path],
) -> Optional[Path]:
    if category == "article_links":
        asset_path = _resolve_article_asset_path(metadata_entry, downloader_output_dir, paper_output_dir)
        if asset_path is not None:
            return asset_path
    else:
        asset_path = _resolve_downloaded_media_path(metadata_entry, downloader_output_dir)
        if asset_path is not None:
            return asset_path

    metadata_type = str(metadata_entry.get("type") or "").strip().lower()
    if metadata_type == "article":
        return _resolve_article_asset_path(metadata_entry, downloader_output_dir, paper_output_dir)
    if metadata_type in {"image", "video", "tweet"}:
        return _resolve_downloaded_media_path(metadata_entry, downloader_output_dir)
    return None


def _link_suffix_for_manifest_row(row: Mapping[str, str]) -> Optional[str]:
    link_kind = (row.get("Link Kind") or "").strip().lower()
    illustration_type = (row.get("Illustration Type") or "").strip().lower()
    if link_kind == "video_excerpt" or illustration_type == "video_link_excerpt":
        return "EXTRACT"
    if link_kind == "video_direct" or illustration_type == "video_link_direct":
        return "DIRECT"
    return None


def _requested_duration_for_video_link_row(
    row: Mapping[str, str],
    entry: TimelineEntry,
) -> Optional[float]:
    link_kind = (row.get("Link Kind") or "").strip().lower()
    illustration_type = (row.get("Illustration Type") or "").strip().lower()
    if link_kind in {"video_direct", "video_excerpt"} or illustration_type in {
        "video_link_direct",
        "video_link_excerpt",
    }:
        return timeline_span_duration_seconds(entry)
    return None


def build_transition_entry_for_video_link(
    entry: TimelineEntry,
    transition_path: Path,
    frame_rate: float,
    *,
    anchor_seconds: Optional[float],
    timing_suffix: str,
) -> Optional[TimelineEntry]:
    duration_seconds = probe_media_duration(transition_path)
    if duration_seconds is None or anchor_seconds is None:
        return None
    shifted_start = max(0.0, anchor_seconds - (duration_seconds / 2.0))
    return TimelineEntry(
        id=entry.id,
        value=entry.value,
        start_seconds=shifted_start,
        start_timecode=format_timecode_from_seconds(shifted_start, frame_rate),
        row_index=entry.row_index,
        end_seconds=shifted_start + duration_seconds,
        end_timecode=format_timecode_from_seconds(shifted_start + duration_seconds, frame_rate),
        source_start_seconds=entry.source_start_seconds,
        source_start_timecode=entry.source_start_timecode,
        source_end_seconds=entry.source_end_seconds,
        source_end_timecode=entry.source_end_timecode,
        transcript_number=entry.transcript_number,
        row_id=entry.row_id,
        annotation_column=entry.annotation_column,
        asset_category="video_link_transitions",
        locator=entry.locator,
        timing_source=f"{entry.timing_source}+{timing_suffix}",
        timing_confidence=entry.timing_confidence,
        status=entry.status,
        text=entry.text,
        reference_segment=entry.reference_segment,
    )


def _video_link_end_anchor_seconds(
    entry: TimelineEntry,
    asset_path: Path,
) -> Optional[float]:
    # Anchor on actual clip end, not the transcript span end.
    # The excerpt may be shorter than the spoken segment; using entry.end_seconds
    # would place the END transition too late.
    if entry.start_seconds is not None:
        asset_duration = probe_media_duration(asset_path)
        if asset_duration is not None and asset_duration > 0:
            return entry.start_seconds + asset_duration
    # Fallback: use timeline span end when asset duration is unavailable
    if entry.end_seconds is not None and entry.start_seconds is not None and entry.end_seconds > entry.start_seconds:
        return entry.end_seconds
    return entry.end_seconds


def stage_link_inserts_from_manifest(
    manifest_rows: Sequence[Mapping[str, str]],
    downloader_metadata_path: Optional[Path],
    downloader_output_dir: Optional[Path],
    paper_output_dir: Optional[Path],
    destination_dir: Path,
    frame_rate: float,
    delay_seconds: float,
    comparison_csv_path: Optional[Path] = None,
) -> Dict[str, int]:
    totals = {
        "article_links": 0,
        "image_links": 0,
        "video_links": 0,
        "tweet_links": 0,
        "video_link_transitions": 0,
    }
    if downloader_metadata_path is None or not downloader_metadata_path.exists():
        return totals
    metadata = load_manifest(downloader_metadata_path)
    metadata_entries = metadata.get("entries", [])
    if not isinstance(metadata_entries, list):
        return totals
    edit_timeline_rows_by_row = _load_edit_timeline_rows_by_row(
        _resolve_edit_timeline_path_for_comparison_csv(comparison_csv_path)
    )
    for row in manifest_rows:
        category = (row.get("Asset Category") or "").strip()
        if category not in totals:
            continue
        metadata_entry = _match_downloader_entry(row, metadata_entries)
        if metadata_entry is None:
            continue
        asset_path = _resolve_link_asset_path(
            category,
            metadata_entry,
            downloader_output_dir,
            paper_output_dir,
        )
        entry = _timing_entry_from_manifest_row(row)
        if category == "video_links":
            entry = _refine_video_link_timing(
                row,
                entry,
                frame_rate,
                edit_timeline_rows_by_row,
            )
            suffix_text = _link_suffix_for_manifest_row(row)
            requested_duration_seconds = _requested_duration_for_video_link_row(
                row,
                entry,
            )
        else:
            suffix_text = None
            requested_duration_seconds = None
        if asset_path is None or not asset_path.exists():
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            downloader_metadata_path,
            suffix_text=suffix_text,
            destination_dir=destination_dir,
            requested_duration_seconds=requested_duration_seconds,
        )
        if renamed_path is not None:
            totals[category] += 1
            if category == "video_links" and VIDEO_LINK_TRANSITION_PATH.exists():
                transition_specs = (
                    (
                        build_transition_entry_for_video_link(
                            entry,
                            VIDEO_LINK_TRANSITION_PATH,
                            frame_rate,
                            anchor_seconds=entry.start_seconds,
                            timing_suffix="transition_midpoint",
                        ),
                        None,
                    ),
                    (
                        build_transition_entry_for_video_link(
                            entry,
                            VIDEO_LINK_TRANSITION_PATH,
                            frame_rate,
                            anchor_seconds=_video_link_end_anchor_seconds(entry, asset_path),
                            timing_suffix="transition_out_midpoint",
                        ),
                        "END",
                    ),
                )
                for transition_entry, transition_suffix in transition_specs:
                    if transition_entry is None:
                        continue
                    transition_path = rename_and_record(
                        VIDEO_LINK_TRANSITION_PATH,
                        transition_entry,
                        0.0,
                        frame_rate,
                        downloader_metadata_path,
                        suffix_text=transition_suffix,
                        destination_dir=destination_dir,
                    )
                    if transition_path is not None:
                        totals["video_link_transitions"] += 1
    return totals


def stage_cta_inserts_from_manifest(
    manifest_rows: Sequence[Mapping[str, str]],
    destination_dir: Path,
    frame_rate: float,
    delay_seconds: float,
) -> int:
    source_lookup = {
        "cta_intro": INTRO_TAG_VIDEO_PATH,
        "cta_comment": COMMENT_TAG_VIDEO_PATH,
        "cta_subscribe": SUBSCRIBE_TAG_VIDEO_PATH,
        "cta_tippee": TIPPEE_TAG_VIDEO_PATH,
    }
    total = 0
    for row in manifest_rows:
        if (row.get("Asset Category") or "").strip() != "cta":
            continue
        illustration_type = (row.get("Illustration Type") or "").strip()
        source_path = source_lookup.get(illustration_type)
        if source_path is None or not source_path.exists():
            continue
        entry = _timing_entry_from_manifest_row(row)
        renamed_path = rename_and_record(
            source_path,
            entry,
            delay_seconds,
            frame_rate,
            None,
            suffix_text="CTA",
            destination_dir=destination_dir,
        )
        if renamed_path is not None:
            total += 1
    return total


def stage_animated_local_inserts_from_manifest(
    manifest_rows: Sequence[Mapping[str, str]],
    destination_dir: Path,
    frame_rate: float,
    delay_seconds: float,
) -> Dict[str, int]:
    """
    Copy animated emoji and flag .mov files from their local source paths
    (stored in the 'Link URL' column) into *destination_dir* with the
    correct timing-prefix filename.

    Handles Asset Category values: 'animated_emoji', 'animated_flag'.
    """
    totals: Dict[str, int] = {"animated_emoji": 0, "animated_flag": 0}
    for row in manifest_rows:
        category = (row.get("Asset Category") or "").strip()
        if category not in totals:
            continue
        link_url = (row.get("Link URL") or "").strip()
        if not link_url:
            continue
        source_path = Path(link_url)
        if not source_path.exists():
            print(f"  ⚠️  Animated insert source not found: {source_path}")
            continue
        entry = _timing_entry_from_manifest_row(row)
        suffix = "EMOJI" if category == "animated_emoji" else "FLAG"
        renamed_path = rename_and_record(
            source_path,
            entry,
            delay_seconds,
            frame_rate,
            None,
            suffix_text=suffix,
            destination_dir=destination_dir,
        )
        if renamed_path is not None:
            totals[category] += 1
    return totals


def copy_noun_arrow_overlay(
    noun: object,
    entry: TimelineEntry,
    delay_seconds: float,
    destination_dir: Optional[Path],
    source_path: Path = DEFAULT_NOUN_ARROW_SOURCE,
    frame_rate: float = DEFAULT_FRAME_RATE,
    manifest_path: Optional[Path] = None,
) -> Optional[Path]:
    if destination_dir is None:
        print("  ℹ️  No destination dir configured for noun arrow overlay; skipping arrow copy.")
        return None
    if not source_path.exists():
        print(f"  ⚠️  Noun arrow source not found: {source_path}")
        return None
    adjusted_seconds = apply_delay(entry.start_seconds, delay_seconds)
    if adjusted_seconds is None:
        context = f"row {entry.row_index}" if entry.row_index is not None else "unknown row"
        print(f"  ⚠️  Missing timing for noun arrow #{entry.id} ({context}); arrow left untouched.")
        return None

    noun_key = normalize_text_key(noun)
    noun_slug = normalize_filename(noun_key.replace(" ", "_")) if noun_key else "noun"
    target_name = normalize_filename(
        f"{seconds_to_label(adjusted_seconds)}_{entry.id}_image@circle_arrow_trans_{noun_slug}{source_path.suffix}"
    )
    destination_dir.mkdir(parents=True, exist_ok=True)
    new_path = destination_dir / target_name
    if new_path.exists():
        try:
            if filecmp.cmp(source_path, new_path, shallow=False):
                print(
                    f"  ⏭  Destination already has {new_path.name}; arrow source matches so it is left as-is."
                )
                USED_ILLUSTRATION_ROWS.append(
                    _used_asset_row(
                        entry=entry,
                        asset_path=new_path,
                        original_path=source_path,
                        manifest_path=manifest_path,
                        frame_rate=frame_rate,
                    )
                )
                return new_path
        except OSError:
            pass
        raise FileExistsError(f"Cannot copy {source_path} -> {new_path}: target exists.")
    shutil.copy2(source_path, new_path)
    print(f"  ✅ {source_path.name} copied to {new_path.name} ({seconds_to_label(adjusted_seconds)})")
    USED_ILLUSTRATION_ROWS.append(
        _used_asset_row(
            entry=entry,
            asset_path=new_path,
            original_path=source_path,
            manifest_path=manifest_path,
            frame_rate=frame_rate,
        )
    )
    if entry.status == "fallback_row_timing":
        record_issue(
            entry=entry,
            manifest_path=manifest_path,
            frame_rate=frame_rate,
            status="fallback_row_timing",
            original_path=source_path,
            asset_path=new_path,
        )
    return new_path


def find_candidate_by_index(directory: Path, entry_id: int) -> Optional[Path]:
    padded = f"{entry_id:03d}"
    strict_matches: List[Path] = []
    loose_matches: List[Path] = []
    pattern = re.compile(rf"(?:^|[^0-9]){padded}(?:[^0-9]|$)")
    try:
        for item in directory.iterdir():
            if not item.is_file():
                continue
            stem = item.stem
            if pattern.search(stem):
                strict_matches.append(item)
            elif padded in stem:
                loose_matches.append(item)
    except FileNotFoundError:
        return None
    for candidates in (strict_matches, loose_matches):
        if candidates:
            candidates.sort(key=lambda path: path.name)
            return candidates[0]
    return None


def locate_entry_asset(
    path_value: Optional[str],
    fallback_dirs: Sequence[Path],
    entry_id: int,
) -> Optional[Path]:
    attempted: List[Path] = []
    if path_value:
        direct = Path(path_value)
        if direct.exists():
            return direct
        attempted.append(direct.parent)
    for directory in fallback_dirs:
        directory = directory.expanduser()
        if directory not in attempted:
            attempted.append(directory)
    for directory in attempted:
        if not directory.exists() or not directory.is_dir():
            continue
        candidate = find_candidate_by_index(directory, entry_id)
        if candidate:
            return candidate
    return None


def rename_manifest_assets(
    manifest_path: Path,
    manifest_key: str,
    path_field: str,
    timeline_entries: Mapping[int, TimelineEntry],
    delay_seconds: float,
    frame_rate: float,
    destination_dir: Optional[Path] = None,
    fallback_dirs: Optional[Sequence[Path]] = None,
    manifest_label: Optional[str] = None,
) -> int:
    if not manifest_path.exists():
        return 0
    data = load_manifest(manifest_path)
    manifest_items = data.get(manifest_key)
    if not isinstance(manifest_items, list):
        return 0
    renamed = 0
    for entry in manifest_items:
        if not isinstance(entry, MutableMapping):
            continue
        entry_id = entry.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            print(f"  ⚠️  Skipping entry without numeric id in {manifest_path.name}: {entry!r}")
            continue
        timeline_entry = timeline_entries.get(entry_id)
        if not timeline_entry:
            print(f"  ⚠️  No timeline data for id {entry_id} in {manifest_path.name}.")
            continue
        path_value = entry.get(path_field)
        asset_path = locate_entry_asset(path_value, fallback_dirs or [], entry_id)
        if not asset_path:
            label = manifest_label or manifest_path.name
            print(f"  ⚠️  Could not locate asset for entry #{entry_id} in {label}.")
            record_issue(
                entry=timeline_entry,
                manifest_path=manifest_path,
                frame_rate=frame_rate,
                status="missing_asset",
            )
            continue
        renamed_path = rename_and_record(
            asset_path,
            timeline_entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            destination_dir=destination_dir,
        )
        if renamed_path:
            entry[path_field] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def build_quote_entries(
    manifest_quotes: Sequence[Mapping[str, object]],
    row_start_map: Mapping[int, Optional[float]],
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> Dict[int, TimelineEntry]:
    entries: Dict[int, TimelineEntry] = {}
    for quote in manifest_quotes:
        entry_id = quote.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            continue
        text_value = str(quote.get("text") or "").strip()
        start_seconds = quote.get("start_seconds")
        seconds_value: Optional[float]
        if start_seconds is None:
            seconds_value = None
        else:
            try:
                seconds_value = float(start_seconds)
            except (TypeError, ValueError):
                seconds_value = None
        clip_row = quote.get("clip_row")
        row_index: Optional[int] = None
        if isinstance(clip_row, int):
            row_index = clip_row
        else:
            try:
                row_index = int(str(clip_row)) if clip_row is not None else None
            except (TypeError, ValueError):
                row_index = None
        if seconds_value is None and row_index is not None:
            seconds_value = row_start_map.get(row_index)
        entries[entry_id] = resolve_precise_timing(
            precise_lookup,
            "quotes",
            TimelineEntry(
                id=entry_id,
                value=text_value,
                start_seconds=seconds_value,
                start_timecode=None,
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Quote Extracted",
                asset_category="quotes",
                status="row_fallback",
            ),
        )
    return entries


def rename_quote_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_path = output_dir / f"{base_name}_video_quotes.json"
    video_dir = output_dir / f"{base_name}_video_quotes_videos"
    if not manifest_path.exists():
        print(f"\nℹ️  Quote manifest not found at {manifest_path}; skipping quotes.")
        return 0
    if not video_dir.exists():
        print(f"\nℹ️  Quote video folder not found at {video_dir}; skipping quotes.")
        return 0
    data = load_manifest(manifest_path)
    quotes_data = data.get("quotes", [])
    if not isinstance(quotes_data, list):
        print(f"\nℹ️  quotes field missing from {manifest_path}; skipping quotes.")
        return 0
    entry_map = build_quote_entries(quotes_data, row_start_map, row_id_map, precise_lookup)
    renamed = 0
    for quote in quotes_data:
        if not isinstance(quote, MutableMapping):
            continue
        entry_id = quote.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            print(f"  ⚠️  Quote entry with invalid id: {quote!r}")
            continue
        timeline_entry = entry_map.get(entry_id)
        if not timeline_entry:
            print(f"  ⚠️  No timing for quote #{entry_id}; skipping.")
            continue
        video_path = video_dir / f"quote_{entry_id:03d}.mov"
        renamed_path = rename_and_record(
            video_path,
            timeline_entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            destination_dir=destination_dir,
        )
        if not renamed_path:
            continue
        quote["video_path"] = str(renamed_path)
        renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_money_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_money_media"
    manifest_path = manifest_dir / f"{base_name}_money_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Money manifest not found at {manifest_path}; skipping money inserts.")
        return 0
    data = load_manifest(manifest_path)
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        print(f"\nℹ️  videos field missing from {manifest_path}; skipping money inserts.")
        return 0
    video_dir = manifest_dir / "videos"
    video_dir = manifest_dir / "videos"
    renamed = 0
    for idx, item in enumerate(videos, start=1):
        if not isinstance(item, MutableMapping):
            continue
        entry_id = item.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            entry_id = idx
        seconds = parse_seconds_value(item.get("start_seconds"))
        row_index = item.get("row_index")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        if row_index is not None:
            mapped_seconds = row_start_map.get(row_index)
            if mapped_seconds is not None:
                seconds = mapped_seconds
        entry = resolve_precise_timing(
            precise_lookup,
            "money",
            TimelineEntry(
                id=entry_id,
                value=str(item.get("value") or item.get("display_text") or ""),
                start_seconds=seconds,
                start_timecode=item.get("start_timecode")
                if isinstance(item.get("start_timecode"), str)
                else None,
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Money Mention",
                asset_category="money",
                status="row_fallback",
            ),
        )
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate money video for entry #{entry.id}.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_percent_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_percent_media"
    manifest_path = manifest_dir / f"{base_name}_percent_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Percent manifest not found at {manifest_path}; skipping percent inserts.")
        return 0
    data = load_manifest(manifest_path)
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        print(f"\nℹ️  videos field missing from {manifest_path}; skipping percent inserts.")
        return 0
    video_dir = manifest_dir / "videos"
    renamed = 0
    for idx, item in enumerate(videos, start=1):
        if not isinstance(item, MutableMapping):
            continue
        entry_id = item.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            entry_id = idx
        seconds = parse_seconds_value(item.get("start_seconds"))
        row_index = item.get("row_index")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        if row_index is not None:
            mapped_seconds = row_start_map.get(row_index)
            if mapped_seconds is not None:
                seconds = mapped_seconds
        entry = resolve_precise_timing(
            precise_lookup,
            "percent",
            TimelineEntry(
                id=entry_id,
                value=str(item.get("display_text") or item.get("value") or "percent"),
                start_seconds=seconds,
                start_timecode=item.get("start_timecode")
                if isinstance(item.get("start_timecode"), str)
                else None,
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Percent Mention",
                asset_category="percent",
                status="row_fallback",
            ),
        )
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate percent video for entry #{entry.id}.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            suffix_text="PCT",
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_3d_location_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_3d_locations"
    manifest_path = manifest_dir / f"{base_name}_3d_location_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  3D location manifest not found at {manifest_path}; skipping 3D inserts.")
        return 0
    data = load_manifest(manifest_path)
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        print(f"\nℹ️  videos field missing from {manifest_path}; skipping 3D inserts.")
        return 0
    video_dir = manifest_dir / "videos"
    trim_suffix = build_trim_suffix(
        DEFAULT_3D_TRIM_START_SECONDS,
        DEFAULT_3D_TRIM_END_SECONDS,
    )
    renamed = 0
    for idx, item in enumerate(videos, start=1):
        if not isinstance(item, MutableMapping):
            continue
        rows = coerce_row_indices(item.get("rows") or [])
        seconds = first_valid_row_seconds(rows, row_start_map)
        entry = resolve_precise_timing(
            precise_lookup,
            "locations_3d",
            TimelineEntry(
                id=idx,
                value=str(item.get("name") or item.get("type") or ""),
                start_seconds=seconds,
                start_timecode=None,
                row_index=rows[0] if rows else None,
                row_id=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                transcript_number=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                annotation_column="Location Mention",
                asset_category="locations_3d",
                status="row_fallback",
            ),
        )
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate 3D video for entry #{entry.id}.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        target_dir: Optional[Path] = destination_dir
        if destination_dir is not None and asset_path.parent == destination_dir:
            # Avoid duplicate 3D inserts on reruns when the asset is already in the delivery folder.
            target_dir = None
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            suffix_text=trim_suffix,
            destination_dir=target_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            item["trim_start_seconds"] = DEFAULT_3D_TRIM_START_SECONDS
            item["trim_end_seconds"] = DEFAULT_3D_TRIM_END_SECONDS
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def build_noun_timeline_data(
    entries: Sequence[Mapping[str, object]],
    row_start_map: Mapping[int, Optional[float]],
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> tuple[List[TimelineEntry], Dict[str, List[TimelineEntry]]]:
    timeline_entries: List[TimelineEntry] = []
    grouped: Dict[str, List[TimelineEntry]] = {}
    for idx, item in enumerate(entries, start=1):
        rows = coerce_row_indices(item.get("rows") or []) if isinstance(item, Mapping) else []
        seconds = first_valid_row_seconds(rows, row_start_map)
        entry = resolve_precise_timing(
            precise_lookup,
            "nouns",
            TimelineEntry(
                id=idx,
                value=str(item.get("noun") if isinstance(item, Mapping) else ""),
                start_seconds=seconds,
                start_timecode=None,
                row_index=rows[0] if rows else None,
                row_id=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                transcript_number=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                annotation_column="Person Mention",
                asset_category="nouns",
                status="row_fallback",
            ),
        )
        timeline_entries.append(entry)
        key = normalize_text_key(entry.value)
        if key:
            grouped.setdefault(key, []).append(entry)
    return timeline_entries, grouped


def rename_noun_images(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_path = output_dir / f"{base_name}_nouns_images.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Noun manifest not found at {manifest_path}; skipping nouns.")
        return 0
    data = load_manifest(manifest_path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        print(f"\nℹ️  entries field missing from {manifest_path}; skipping nouns.")
        return 0
    renamed = 0
    timeline_entries, _ = build_noun_timeline_data(entries, row_start_map, row_id_map, precise_lookup)
    for item, entry in zip(entries, timeline_entries):
        if not isinstance(item, MutableMapping):
            continue
        path_value = item.get("image_path")
        if not path_value:
            continue
        renamed_path = rename_and_record(
            Path(path_value),
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
        )
        if renamed_path:
            item["image_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def load_noun_lookup(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> Dict[str, List[TimelineEntry]]:
    manifest_path = output_dir / f"{base_name}_nouns_images.json"
    if not manifest_path.exists():
        return {}
    data = load_manifest(manifest_path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        return {}
    _, grouped = build_noun_timeline_data(entries, row_start_map, row_id_map, precise_lookup)
    return {key: list(value) for key, value in grouped.items()}


def rename_noun_transition_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_nouns_transitions"
    manifest_path = manifest_dir / f"{base_name}_nouns_transition_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Noun transition manifest not found at {manifest_path}; skipping noun transitions.")
        return 0
    noun_lookup = load_noun_lookup(base_name, output_dir, row_start_map, row_id_map, precise_lookup)
    if not noun_lookup:
        print(
            f"\nℹ️  Could not derive noun timing data for {base_name}; skipping noun transitions."
        )
        return 0
    data = load_manifest(manifest_path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        print(f"\nℹ️  entries field missing from {manifest_path}; skipping noun transitions.")
        return 0
    renamed = 0
    video_dir = manifest_dir / "videos"
    for item in entries:
        if not isinstance(item, MutableMapping):
            continue
        noun_key = normalize_text_key(item.get("noun"))
        bucket = noun_lookup.get(noun_key)
        if not bucket:
            print(f"  ⚠️  No timing data for noun transition '{item.get('noun')}'.")
            continue
        entry = bucket.pop(0)
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate noun transition for '{item.get('noun')}'.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            copy_noun_arrow_overlay(
                item.get("noun"),
                entry,
                delay_seconds,
                destination_dir=destination_dir,
                frame_rate=frame_rate,
                manifest_path=manifest_path,
            )
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_calendar_videos(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_calendar_media"
    manifest_path = manifest_dir / f"{base_name}_calendar_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Calendar manifest not found at {manifest_path}; skipping calendars.")
        return 0
    data = load_manifest(manifest_path)
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        print(f"\nℹ️  videos field missing from {manifest_path}; skipping calendars.")
        return 0
    renamed = 0
    video_dir = manifest_dir / "videos"
    for idx, item in enumerate(videos, start=1):
        if not isinstance(item, MutableMapping):
            continue
        entry_id = item.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            entry_id = idx
        row_index = item.get("row_index")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        seconds = row_start_map.get(row_index) if row_index is not None else None
        if seconds is None:
            seconds = parse_seconds_value(item.get("start_seconds"))
        if seconds is None:
            seconds = parse_timecode(item.get("start_timecode"), frame_rate)
        entry = resolve_precise_timing(
            precise_lookup,
            "calendar",
            TimelineEntry(
                id=entry_id,
                value=str(item.get("text") or ""),
                start_seconds=seconds,
                start_timecode=item.get("start_timecode"),
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Date Mention",
                asset_category="calendar",
                status="row_fallback",
            ),
        )
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate calendar video for entry #{entry.id}.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            suffix_text="CAL",
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_social_ranking_punctuation_videos(
    base_name: str,
    output_dir: Path,
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_mentions_media"
    manifest_path = manifest_dir / f"{base_name}_mentions_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Mention manifest not found at {manifest_path}; skipping social/ranking/punctuation clips.")
        return 0
    data = load_manifest(manifest_path)
    clips = data.get("clips", [])
    if not isinstance(clips, list):
        print(f"\nℹ️  clips field missing from {manifest_path}; skipping social/ranking/punctuation clips.")
        return 0
    video_dir = manifest_dir / "videos"
    renamed = 0
    for idx, item in enumerate(clips, start=1):
        if not isinstance(item, MutableMapping):
            continue
        if not item.get("success"):
            continue
        clip_tag = str(item.get("tag") or "").strip().upper()
        if clip_tag == "PNC":
            continue
        entry_id = item.get("entry_id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            entry_id = idx
        row_index = item.get("row_index")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        entry = resolve_precise_timing(
            precise_lookup,
            "social_ranking_punctuation",
            TimelineEntry(
                id=entry_id,
                value=str(item.get("value") or item.get("illustration_type") or item.get("type_name") or ""),
                start_seconds=parse_seconds_value(item.get("start_seconds")),
                start_timecode=item.get("start_timecode") if isinstance(item.get("start_timecode"), str) else None,
                row_index=row_index,
                row_id=(item.get("transcript_number") or item.get("row_index") or "") if item.get("transcript_number") or item.get("row_index") else None,
                transcript_number=str(item.get("transcript_number") or "").strip() or None,
                annotation_column="Timed AI Manifest",
                asset_category="social_ranking_punctuation",
                status="row_fallback",
            ),
        )
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Could not locate social/ranking/punctuation asset for entry #{entry.id}.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def _build_quote_timeline_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
    row_start_map: Mapping[int, Optional[float]],
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> Dict[int, TimelineEntry]:
    if precise_lookup is not None:
        precise_quotes = [
            item
            for item in _sorted_precise_entries(precise_lookup.candidates)
            if item.asset_category == "quote_highlights"
        ]
        if precise_quotes:
            return {
                entry_id: TimelineEntry(
                    id=entry_id,
                    value=precise.value,
                    start_seconds=precise.start_seconds,
                    start_timecode=precise.start_timecode,
                    row_index=None,
                    end_seconds=precise.end_seconds,
                    end_timecode=precise.end_timecode,
                    source_start_seconds=precise.source_start_seconds,
                    source_start_timecode=precise.source_start_timecode,
                    source_end_seconds=precise.source_end_seconds,
                    source_end_timecode=precise.source_end_timecode,
                    transcript_number=precise.transcript_number,
                    row_id=precise.row_id,
                    annotation_column=precise.annotation_column or "Quote Extracted",
                    asset_category="quote_highlights",
                    locator=precise.locator,
                    timing_source=precise.timing_source,
                    timing_confidence=precise.timing_confidence,
                    status=precise.status or "precise_candidate",
                    text=precise.text,
                    reference_segment=precise.reference_segment,
                )
                for entry_id, precise in enumerate(precise_quotes, start=1)
            }
    quotes, _ = cq.build_video_quotes(rows, dict(header_map), frame_rate)
    timeline_entries: Dict[int, TimelineEntry] = {}
    for quote in quotes:
        entry_id = quote.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            continue
        row_index = quote.get("clip_row")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        seconds = row_start_map.get(row_index) if row_index is not None else None
        if seconds is None:
            seconds = parse_seconds_value(quote.get("start_seconds"))
        if seconds is None:
            seconds = parse_timecode(quote.get("start_timecode"), frame_rate)
        timeline_entries[entry_id] = resolve_precise_timing(
            precise_lookup,
            "quote_highlights",
            TimelineEntry(
                id=entry_id,
                value=str(quote.get("text") or ""),
                start_seconds=seconds,
                start_timecode=quote.get("start_timecode"),
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Quote Extracted",
                asset_category="quote_highlights",
                status="row_fallback",
            ),
        )
    return timeline_entries


def rename_highlight_quote_videos(
    base_name: str,
    output_dir: Path,
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    video_dir = output_dir / f"{base_name}_highlight_quotes_videos"
    if not video_dir.exists():
        print(f"\nℹ️  Highlight quote folder not found at {video_dir}; skipping highlights.")
        return 0
    entry_map = _build_quote_timeline_entries(rows, header_map, frame_rate, row_start_map, row_id_map, precise_lookup)
    if not entry_map:
        print("\nℹ️  No quote timing data available; skipping highlight quotes.")
        return 0
    renamed = 0
    for entry_id, entry in entry_map.items():
        asset_path = video_dir / f"quote_{entry_id:03d}.mov"
        if not asset_path.exists():
            print(f"  ⚠️  Missing highlight video for quote #{entry_id}.")
            record_issue(entry, None, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            None,
            suffix_text="QH",
            destination_dir=destination_dir,
        )
        if renamed_path:
            renamed += 1
    return renamed


def _load_institution_entries(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> tuple[Dict[int, TimelineEntry], List[MutableMapping[str, object]], Optional[dict]]:
    manifest_path = output_dir / f"{base_name}_institutions_images.json"
    if not manifest_path.exists():
        return {}, [], None
    data = load_manifest(manifest_path)
    entries = data.get("entries")
    if not isinstance(entries, list):
        return {}, [], data
    result: Dict[int, TimelineEntry] = {}
    for idx, item in enumerate(entries, start=1):
        if not isinstance(item, MutableMapping):
            continue
        rows = coerce_row_indices(item.get("rows") or [])
        seconds = first_valid_row_seconds(rows, row_start_map)
        entry = resolve_precise_timing(
            precise_lookup,
            "institution_images",
            TimelineEntry(
                id=idx,
                value=str(item.get("institution") or ""),
                start_seconds=seconds,
                start_timecode=None,
                row_index=rows[0] if rows else None,
                row_id=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                transcript_number=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                annotation_column="Gov Institution",
                asset_category="institution_images",
                status="row_fallback",
            ),
        )
        result[idx] = entry
    return result, entries, data


def rename_institution_images(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_path = output_dir / f"{base_name}_institutions_images.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Institution image manifest not found at {manifest_path}; skipping images.")
        return 0
    entry_map, entries, manifest_data = _load_institution_entries(
        base_name, output_dir, row_start_map, row_id_map, precise_lookup
    )
    if not entry_map or not entries:
        print("\nℹ️  No institution timing data available; skipping images.")
        return 0
    renamed = 0
    for idx, item in enumerate(entries, start=1):
        entry = entry_map.get(idx)
        if entry is None:
            continue
        path_value = item.get("image_path")
        if not path_value:
            continue
        asset_path = Path(path_value)
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            suffix_text="INST",
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["image_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        if manifest_data is None:
            manifest_data = {}
        manifest_data["entries"] = entries
        save_manifest(manifest_path, manifest_data)
    return renamed


def rename_institution_transitions(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_institution_transitions"
    manifest_path = manifest_dir / f"{base_name}_institution_transition_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  Institution transition manifest not found at {manifest_path}; skipping transitions.")
        return 0
    entry_map, _, _ = _load_institution_entries(base_name, output_dir, row_start_map, row_id_map, precise_lookup)
    if not entry_map:
        print("\nℹ️  No institution timing data available; skipping transitions.")
        return 0
    data = load_manifest(manifest_path)
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        print(f"\nℹ️  videos field missing from {manifest_path}; skipping transitions.")
        return 0
    renamed = 0
    video_dir = manifest_dir / "videos"
    for item in videos:
        if not isinstance(item, MutableMapping):
            continue
        entry_index = item.get("entry_index")
        try:
            entry_index = int(entry_index)
        except (TypeError, ValueError):
            continue
        entry = entry_map.get(entry_index)
        if entry is None:
            continue
        asset_path = locate_entry_asset(item.get("video_path"), [video_dir], entry.id)
        if not asset_path:
            print(f"  ⚠️  Missing institution transition for '{entry.value}'.")
            record_issue(entry, manifest_path, frame_rate, "missing_asset")
            continue
        renamed_path = rename_and_record(
            asset_path,
            entry,
            delay_seconds,
            frame_rate,
            manifest_path,
            suffix_text="INST",
            destination_dir=destination_dir,
        )
        if renamed_path:
            item["video_path"] = str(renamed_path)
            renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def rename_city_country_images(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    manifest_dir = output_dir / f"{base_name}_city_country_media"
    manifest_path = manifest_dir / f"{base_name}_city_country_manifest.json"
    if not manifest_path.exists():
        print(f"\nℹ️  City/Country manifest not found at {manifest_path}; skipping city/country assets.")
        return 0
    data = load_manifest(manifest_path)
    entries = data.get("entries", [])
    if not isinstance(entries, list):
        print(f"\nℹ️  entries field missing from {manifest_path}; skipping city/country assets.")
        return 0
    renamed = 0
    for idx, item in enumerate(entries, start=1):
        if not isinstance(item, MutableMapping):
            continue
        rows = coerce_row_indices(item.get("rows") or [])
        seconds = first_valid_row_seconds(rows, row_start_map)
        asset_category = "city_country"
        entry = resolve_precise_timing(
            precise_lookup,
            asset_category,
            TimelineEntry(
                id=idx,
                value=str(item.get("name") or item.get("type") or ""),
                start_seconds=seconds,
                start_timecode=None,
                row_index=rows[0] if rows else None,
                row_id=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                transcript_number=row_id_map.get(rows[0]) if (row_id_map and rows) else None,
                annotation_column="City Mention",
                asset_category=asset_category,
                status="row_fallback",
            ),
        )
        asset_type = str(item.get("type") or "").lower()
        if asset_type == "city":
            path_value = item.get("image_path")
            if not path_value:
                continue
            renamed_path = rename_and_record(
                Path(path_value),
                entry,
                delay_seconds,
                frame_rate,
                manifest_path,
            )
            if renamed_path:
                item["image_path"] = str(renamed_path)
                renamed += 1
        else:
            for field, label in (("flag_path", "FLAG"), ("map_path", "MAP")):
                path_value = item.get(field)
                if not path_value:
                    continue
                renamed_path = rename_and_record(
                    Path(path_value),
                    entry,
                    delay_seconds,
                    frame_rate,
                    manifest_path,
                    label,
                )
                if renamed_path:
                    item[field] = str(renamed_path)
                    renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def _parse_manifest_float(value: object) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _single_title_mov_sidecar_fields(title: Mapping[str, object]) -> Dict[str, object]:
    show_from_seconds = _parse_manifest_float(title.get("show_from_seconds"))
    show_until_seconds = _parse_manifest_float(title.get("show_until_seconds"))
    visible_duration_seconds = _parse_manifest_float(title.get("visible_duration_seconds"))
    if (
        visible_duration_seconds is None
        and show_from_seconds is not None
        and show_until_seconds is not None
        and show_until_seconds > show_from_seconds
    ):
        visible_duration_seconds = show_until_seconds - show_from_seconds
    payload: Dict[str, object] = {
        "asset_mode": SINGLE_TITLE_MOV_ASSET_MODE,
    }
    if show_from_seconds is not None:
        payload["show_from_seconds"] = show_from_seconds
    if show_until_seconds is not None:
        payload["show_until_seconds"] = show_until_seconds
    if visible_duration_seconds is not None and visible_duration_seconds > 0:
        payload["visible_duration_seconds"] = visible_duration_seconds
    return payload


def rename_ransom_title_gifs(
    base_name: str,
    output_dir: Path,
    row_start_map: Mapping[int, Optional[float]],
    frame_rate: float,
    delay_seconds: float,
    destination_dir: Optional[Path] = None,
    title_entries: Optional[Mapping[int, TimelineEntry]] = None,
    row_id_map: Optional[Mapping[int, str]] = None,
    precise_lookup: Optional[PreciseTimingLookup] = None,
) -> int:
    title_creator_manifest_dir = TITLE_CREATOR_OUTPUT_DIR / f"{base_name}_ransom_titles"
    title_creator_manifest_path = title_creator_manifest_dir / f"{base_name}_ransom_titles_manifest.json"
    legacy_manifest_dir = output_dir / f"{base_name}_ransom_titles"
    legacy_manifest_path = legacy_manifest_dir / f"{base_name}_ransom_titles_manifest.json"

    manifest_dir: Path
    manifest_path: Path
    if title_creator_manifest_path.exists():
        manifest_dir = title_creator_manifest_dir
        manifest_path = title_creator_manifest_path
    elif legacy_manifest_path.exists():
        manifest_dir = legacy_manifest_dir
        manifest_path = legacy_manifest_path
    else:
        print(
            f"\nℹ️  Ransom title manifest not found at {title_creator_manifest_path} "
            f"or {legacy_manifest_path}; skipping ransom title assets."
        )
        return 0

    data = load_manifest(manifest_path)
    titles = data.get("titles", [])
    if not isinstance(titles, list):
        print(f"\nℹ️  titles field missing from {manifest_path}; skipping ransom title assets.")
        return 0

    video_dir = manifest_dir / "videos"
    gif_dir = manifest_dir / "gifs"
    renamed = 0
    for idx, title in enumerate(titles, start=1):
        if not isinstance(title, MutableMapping):
            continue
        entry_id = title.get("id")
        try:
            entry_id = int(entry_id)
        except (TypeError, ValueError):
            entry_id = None
        row_index = title.get("row_index")
        try:
            row_index = int(row_index) if row_index is not None else None
        except (TypeError, ValueError):
            row_index = None
        seconds = row_start_map.get(row_index) if row_index is not None else None
        entry: Optional[TimelineEntry]
        if title_entries and entry_id is not None:
            entry = title_entries.get(entry_id)
        else:
            entry = None
        if entry is None:
            entry = TimelineEntry(
                id=entry_id if entry_id is not None else idx,
                value=str(title.get("title") or ""),
                start_seconds=seconds,
                start_timecode=None,
                row_index=row_index,
                row_id=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                transcript_number=row_id_map.get(row_index) if (row_id_map and row_index is not None) else None,
                annotation_column="Titles",
                asset_category="ransom_gifs",
                status="row_fallback",
            )
        entry = resolve_precise_timing(precise_lookup, "ransom_gifs", entry)
        if entry.start_seconds is None:
            print(f"  ⚠️  Missing timing for ransom title '{entry.value}'; skipping title asset.")
            continue

        video_path_value = title.get("video_path")
        if video_path_value:
            asset_path = locate_entry_asset(video_path_value, [video_dir], entry.id)
            if not asset_path:
                print(f"  ⚠️  Missing title MOV for '{entry.value}'.")
                record_issue(entry, manifest_path, frame_rate, "missing_asset")
                continue
            renamed_path = rename_and_record(
                asset_path,
                entry,
                delay_seconds,
                frame_rate,
                manifest_path,
                destination_dir=destination_dir,
                sidecar_extra_fields=_single_title_mov_sidecar_fields(title),
            )
            if renamed_path:
                title["video_path"] = str(renamed_path)
                renamed += 1
            continue

        lines = title.get("lines", [])
        if isinstance(lines, list):
            for line in lines:
                if not isinstance(line, MutableMapping):
                    continue
                asset_path = locate_entry_asset(line.get("gif_path"), [gif_dir], entry.id)
                if not asset_path:
                    print(f"  ⚠️  Missing GIF asset for ransom title '{entry.value}'.")
                    record_issue(entry, manifest_path, frame_rate, "missing_asset")
                    continue
                renamed_path = rename_and_record(
                    asset_path,
                    entry,
                    delay_seconds,
                    frame_rate,
                    manifest_path,
                    destination_dir=destination_dir,
                )
                if renamed_path:
                    line["gif_path"] = str(renamed_path)
                    renamed += 1

        combined_path_value = title.get("combined_gif_path")
        if combined_path_value:
            combined_asset = locate_entry_asset(combined_path_value, [gif_dir], entry.id)
            if not combined_asset:
                print(f"  ⚠️  Missing combined GIF for ransom title '{entry.value}'.")
                record_issue(entry, manifest_path, frame_rate, "missing_asset")
            else:
                renamed_path = rename_and_record(
                    combined_asset,
                    entry,
                    delay_seconds,
                    frame_rate,
                    manifest_path,
                    destination_dir=destination_dir,
                )
                if renamed_path:
                    title["combined_gif_path"] = str(renamed_path)
                    renamed += 1
    if renamed:
        save_manifest(manifest_path, data)
    return renamed


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be greater than 0.")
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    csv_path = csv_path.expanduser()
    output_dir = (args.output_dir if args.output_dir else OUTPUT_DIR).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    insert_dir = Path(args.insert_dir).expanduser()
    insert_dir.mkdir(parents=True, exist_ok=True)
    if args.clean_insert_dir:
        clear_directory_contents(insert_dir)
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    row_start_map = build_row_start_map(rows, header_map, args.frame_rate)
    row_id_map = build_row_id_map(rows, header_map)
    timing_manifest_path = args.timing_manifest.expanduser().resolve() if args.timing_manifest else None
    manifest_rows = load_timing_manifest_rows(timing_manifest_path)
    precise_lookup = _build_precise_timing_lookup(csv_path, timing_manifest_path)
    downloader_metadata_path = args.downloader_metadata.expanduser().resolve() if args.downloader_metadata else None
    downloader_output_dir = args.downloader_output_dir.expanduser().resolve() if args.downloader_output_dir else None
    paper_output_dir = args.paper_output_dir.expanduser().resolve() if args.paper_output_dir else None
    reset_illustration_tracking()
    base_name = csv_path.stem

    totals: Dict[str, int] = {}

    # Titles
    titles_manifest = output_dir / f"{base_name}_titles_media" / f"{base_name}_titles_manifest.json"
    try:
        title_entries = build_feature_entries(
            rows,
            header_map,
            ["Titles"],
            args.frame_rate,
            row_start_map,
            row_id_map=row_id_map,
            precise_lookup=precise_lookup,
            asset_category="titles",
        )
    except KeyError as exc:
        print(f"\n⚠️  {exc}; skipping title-based renames.")
        title_entries = []
    if not title_entries and manifest_rows:
        title_entries = _title_entries_from_timing_manifest(manifest_rows)
    title_map = {entry.id: entry for entry in title_entries}
    totals["titles"] = 0
    if args.skip_standard_title_videos:
        print("\nℹ️  Skipping standard title videos; transparent ransom-title assets remain enabled.")
    elif titles_manifest.exists() and title_entries:
        print(f"\nRenaming title videos via {titles_manifest}:")
        totals["titles"] = rename_manifest_assets(
            titles_manifest,
            "videos",
            "video_path",
            title_map,
            args.title_delay_seconds,
            args.frame_rate,
            destination_dir=insert_dir,
            fallback_dirs=[titles_manifest.parent / "videos"],
            manifest_label=titles_manifest.name,
        )
    else:
        print(f"\nℹ️  Title manifest or entries missing; skipping titles.")

    print("\nRenaming ransom title assets:")
    totals["ransom_gifs"] = rename_ransom_title_gifs(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        title_entries=title_map,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    # Numbers / dates
    numbers_manifest = output_dir / f"{base_name}_numbers_media" / f"{base_name}_numbers_manifest.json"
    try:
        number_entries = build_feature_entries(
            rows,
            header_map,
            ["Number or Date", "Number Mention", "Date Mention"],
            args.frame_rate,
            row_start_map,
            row_id_map=row_id_map,
            precise_lookup=precise_lookup,
            asset_category="numbers",
        )
    except KeyError as exc:
        print(f"\n⚠️  {exc}; skipping number/date renames.")
        number_entries = []
    totals["numbers"] = 0
    if numbers_manifest.exists() and number_entries:
        print(f"\nRenaming number/date videos via {numbers_manifest}:")
        number_map = {entry.id: entry for entry in number_entries}
        totals["numbers"] = rename_manifest_assets(
            numbers_manifest,
            "videos",
            "video_path",
            number_map,
            args.title_delay_seconds,
            args.frame_rate,
            destination_dir=insert_dir,
            fallback_dirs=[numbers_manifest.parent / "videos"],
            manifest_label=numbers_manifest.name,
        )
    else:
        print(f"\nℹ️  Number/date manifest or entries missing; skipping numbers.")

    print("\nRenaming money videos:")
    totals["money"] = rename_money_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming percent videos:")
    totals["percent"] = rename_percent_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming social/ranking/punctuation videos:")
    totals["social_ranking_punctuation"] = rename_social_ranking_punctuation_videos(
        base_name,
        output_dir,
        args.frame_rate,
        args.title_delay_seconds + args.bold_shift_seconds,
        destination_dir=insert_dir,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming 3D location videos:")
    totals["locations_3d"] = rename_3d_location_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming calendar videos:")
    totals["calendar"] = rename_calendar_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    # Quotes
    print("\nRenaming quote videos:")
    totals["quotes"] = rename_quote_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming highlight quote videos:")
    totals["quote_highlights"] = rename_highlight_quote_videos(
        base_name,
        output_dir,
        rows,
        header_map,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds + args.quote_highlight_shift_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    # Noun imagery
    print("\nRenaming noun imagery:")
    totals["nouns"] = rename_noun_images(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming noun transition videos:")
    totals["noun_transitions"] = rename_noun_transition_videos(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming institution imagery:")
    totals["institution_images"] = rename_institution_images(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nRenaming institution transitions:")
    totals["institution_transitions"] = rename_institution_transitions(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        destination_dir=insert_dir,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    # City / Country imagery
    print("\nRenaming city/country imagery:")
    totals["city_country"] = rename_city_country_images(
        base_name,
        output_dir,
        row_start_map,
        args.frame_rate,
        args.title_delay_seconds,
        row_id_map=row_id_map,
        precise_lookup=precise_lookup,
    )

    print("\nStaging article/image/video/tweet downloader assets:")
    downloader_totals = stage_link_inserts_from_manifest(
        manifest_rows,
        downloader_metadata_path,
        downloader_output_dir,
        paper_output_dir,
        insert_dir,
        args.frame_rate,
        args.title_delay_seconds,
        comparison_csv_path=csv_path,
    )
    totals.update(downloader_totals)

    print("\nStaging CTA assets:")
    totals["cta"] = stage_cta_inserts_from_manifest(
        manifest_rows,
        insert_dir,
        args.frame_rate,
        args.title_delay_seconds,
    )

    print("\nStaging animated emoji and flag inserts:")
    animated_totals = stage_animated_local_inserts_from_manifest(
        manifest_rows,
        insert_dir,
        args.frame_rate,
        args.title_delay_seconds,
    )
    totals.update(animated_totals)

    print("\nSummary")
    print("=" * 32)
    labels = [
        ("titles", "Title videos"),
        ("ransom_gifs", "Ransom Title Assets"),
        ("numbers", "Number/date videos"),
        ("money", "Money videos"),
        ("percent", "Percent videos"),
        ("social_ranking_punctuation", "Social/ranking/punct"),
        ("locations_3d", "3D location videos"),
        ("calendar", "Calendar videos"),
        ("quotes", "Quote videos"),
        ("quote_highlights", "Quote highlight videos"),
        ("nouns", "Noun images"),
        ("noun_transitions", "Noun transition videos"),
        ("institution_images", "Institution images"),
        ("institution_transitions", "Institution transitions"),
        ("city_country", "City/country images"),
        ("article_links", "Article inserts"),
        ("image_links", "Image inserts"),
        ("video_links", "Video inserts"),
        ("video_link_transitions", "Video transitions"),
        ("tweet_links", "Tweet inserts"),
        ("cta", "CTA inserts"),
        ("animated_emoji", "Animated emoji inserts"),
        ("animated_flag", "Animated flag inserts"),
    ]
    printed = False
    for key, label in labels:
        if key in totals:
            print(f"{label:<22}: {totals[key]}")
            printed = True
    if not printed:
        print("No manifests were found; nothing renamed.")

    illustration_timing_csv, illustration_timing_json = write_illustration_timing_reports(csv_path, output_dir)
    print(f"\nIllustration timing CSV: {illustration_timing_csv}")
    print(f"Illustration timing JSON: {illustration_timing_json}")
    list_path = write_insert_list(insert_dir)
    print(f"Insert list: {list_path}")


if __name__ == "__main__":
    main()
