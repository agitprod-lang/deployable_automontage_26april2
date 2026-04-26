#!/usr/bin/env python3
"""
Build a Premiere-compatible XML timeline directly from a Comparser CSV.

This script focuses on the "universal" workflow where the CSV in /Comparser/output is
considered the sole source of truth. Only the rows explicitly marked as keepers (default:
Keep column contains "x") and tied to a Transcript # are turned into clipitems, removing
the guesswork that previously flipped the semantics in generate_premiere_xml.py.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from generate_premiere_xml import (
    DEFAULT_COMPARER_OUTPUT,
    DEFAULT_MEDIA_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_PREMIERE_XML_DIR,
    Segment,
    VIDEO_EXTENSIONS,
    build_sequence_xml,
    extract_reference_media_info,
    load_reference_sequence,
    parse_timecode,
    renumber_clip_indexes,
    require_int_timebase,
    write_xml,
)


CSV_SAMPLE_BYTES = 2048


@dataclass
class ExtractionResult:
    segments: List[Segment]
    earliest_source_frame: int
    latest_source_frame: int
    earliest_edit_frame: int
    latest_edit_frame: int
    stats: Dict[str, int]
    warnings: List[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Premiere-compatible XML from a universal Comparser CSV. "
            "Only rows whose Keep column matches --keep-marker are included."
        )
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        help=f"Explicit CSV path (defaults to the latest CSV inside {DEFAULT_COMPARER_OUTPUT}).",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        help=f"Destination XML (defaults to {DEFAULT_OUTPUT_DIR}/<csv_name>_premiere.xml).",
    )
    parser.add_argument(
        "--media",
        dest="media_path",
        help=(
            "Rush used for the clips. By default the latest media file inside "
            f"{DEFAULT_MEDIA_DIR} is used or the first clip found in --reference-xml."
        ),
    )
    parser.add_argument(
        "--sequence-name",
        help="Optional custom sequence name (defaults to the CSV filename).",
    )
    parser.add_argument(
        "--keep-marker",
        default="x",
        help="Marker in the Keep column that denotes a keeper row (default: x).",
    )
    parser.add_argument(
        "--include-empty-transcript",
        action="store_true",
        help="Also keep rows even if the Transcript # column is empty (default: skip them).",
    )
    parser.add_argument(
        "--preserve-gaps",
        action="store_true",
        help="Preserve the original gaps between clips instead of condensing them.",
    )
    parser.add_argument(
        "--trim-start",
        type=int,
        default=0,
        help="Frames to trim from the start of each kept clip.",
    )
    parser.add_argument(
        "--trim-end",
        type=int,
        default=0,
        help="Frames to trim from the end of each kept clip.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=25.0,
        help="Frame rate of the CSV timecodes (integer frame rates only, default: 25).",
    )
    parser.add_argument(
        "--timecode-start",
        help="Optional sequence start timecode (HH:MM:SS:FF). Defaults to the earliest kept start.",
    )
    parser.add_argument(
        "--media-timecode-start",
        help="Optional rush start timecode (HH:MM:SS:FF). Defaults to the earliest kept start.",
    )
    parser.add_argument(
        "--video-width",
        type=int,
        default=1920,
        help="Video width metadata to embed (default: 1920).",
    )
    parser.add_argument(
        "--video-height",
        type=int,
        default=1080,
        help="Video height metadata to embed (default: 1080).",
    )
    parser.add_argument(
        "--audio-channels",
        type=int,
        default=2,
        help="Audio channel count metadata (default: 2).",
    )
    parser.add_argument(
        "--audio-sample-rate",
        type=int,
        default=48000,
        help="Audio sample rate metadata (default: 48000).",
    )
    parser.add_argument(
        "--pixel-aspect",
        default="square",
        help="Pixel aspect ratio string (default: square).",
    )
    parser.add_argument(
        "--reference-xml",
        dest="reference_xml",
        help=(
            "Optional Premiere XML to copy media metadata from. Defaults to the latest XML in "
            f"{DEFAULT_PREMIERE_XML_DIR}."
        ),
    )
    parser.add_argument(
        "--reference-sequence",
        dest="reference_sequence",
        help="Sequence name inside --reference-xml to read metadata from (defaults to the first).",
    )
    return parser.parse_args()


def _canonicalize(label: str) -> str:
    return label.strip().lower().replace(" ", "").replace("#", "")


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    if not directory.exists():
        return None
    candidates = [path for path in directory.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _latest_media_file(directory: Path) -> Optional[Path]:
    if not directory.exists():
        return None
    try:
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _load_rows(csv_path: Path) -> Tuple[List[dict], List[str]]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        sample = handle.read(CSV_SAMPLE_BYTES)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            if getattr(dialect, "delimiter", ",") == "," and ";" in sample:
                dialect.delimiter = ";"
            reader = csv.DictReader(handle, dialect=dialect)
        except csv.Error:
            reader = csv.DictReader(handle, delimiter=";")
        rows = list(reader)
        fieldnames = reader.fieldnames or []
    return rows, fieldnames


def _resolve_field(fieldnames: Iterable[str], candidates: Tuple[str, ...]) -> Optional[str]:
    normalized_map: Dict[str, str] = {}
    for name in fieldnames:
        if not name:
            continue
        normalized_map.setdefault(_canonicalize(name), name)
    for candidate in candidates:
        canonical = _canonicalize(candidate)
        if canonical in normalized_map:
            return normalized_map[canonical]
    return None


def _value(row: dict, field: Optional[str]) -> str:
    if not field:
        return ""
    raw = row.get(field)
    return raw.strip() if isinstance(raw, str) else ""


def _trimmed_bounds(start_frame: int, end_frame: int, trim_start: int, trim_end: int) -> Optional[Tuple[int, int]]:
    trimmed_start = start_frame + max(0, trim_start)
    trimmed_end = end_frame - max(0, trim_end)
    if trimmed_end <= trimmed_start:
        return None
    return trimmed_start, trimmed_end


def _summarize_stats(csv_name: str, keep_marker: str, stats: Dict[str, int]) -> None:
    total = stats["total_rows"]
    usable = stats["rows_with_timecode"]
    kept = stats["kept_rows"]
    print(
        f"{csv_name}: kept {kept} clip(s) marked '{keep_marker}' out of "
        f"{usable} usable timecoded rows ({total} total rows)."
    )
    if stats["skipped_keep_marker"]:
        print(f"  - Skipped {stats['skipped_keep_marker']} row(s) whose Keep value did not match.")
    if stats["skipped_transcript"]:
        print(f"  - Skipped {stats['skipped_transcript']} row(s) without a Transcript #.")
    if stats["skipped_timecode"]:
        print(f"  - Skipped {stats['skipped_timecode']} row(s) because their timecodes could not be parsed.")
    if stats["skipped_trimmed"]:
        print(
            f"  - Skipped {stats['skipped_trimmed']} row(s) after trimming because the resulting duration <= 0."
        )
    if stats.get("source_fallback_rows"):
        print(
            "  - Fell back to edit time bounds for "
            f"{stats['source_fallback_rows']} row(s) whose source bounds were missing or unusable."
        )
    if stats.get("source_duration_mismatch_rows"):
        print(
            "  - Detected source/edit duration mismatches on "
            f"{stats['source_duration_mismatch_rows']} row(s); inspect the emitted warnings."
        )


def extract_kept_segments(
    csv_path: Path,
    fps: int,
    keep_marker: str,
    include_empty_transcript: bool,
    trim_start: int,
    trim_end: int,
    preserve_gaps: bool,
) -> ExtractionResult:
    rows, fieldnames = _load_rows(csv_path)
    if not rows:
        raise ValueError(f"{csv_path} did not contain any rows.")

    start_field = _resolve_field(fieldnames, ("Start Time", "Start"))
    end_field = _resolve_field(fieldnames, ("End Time", "End"))
    source_start_field = _resolve_field(fieldnames, ("Source Start Time", "Source Start"))
    source_end_field = _resolve_field(fieldnames, ("Source End Time", "Source End"))
    keep_field = _resolve_field(fieldnames, ("Keep",))
    transcript_field = _resolve_field(fieldnames, ("Transcript #", "Transcript"))
    text_field = _resolve_field(fieldnames, ("Text", "Dialogue"))

    if not start_field or not end_field:
        raise ValueError("CSV must contain Start/End columns.")
    if keep_marker and not keep_field:
        raise ValueError("CSV did not contain a Keep column, cannot honor --keep-marker.")
    if not include_empty_transcript and not transcript_field:
        raise ValueError("CSV did not contain a Transcript column and --include-empty-transcript was not set.")

    keep_marker_lower = keep_marker.strip().lower()
    stats = {
        "total_rows": len(rows),
        "rows_with_timecode": 0,
        "kept_rows": 0,
        "skipped_keep_marker": 0,
        "skipped_transcript": 0,
        "skipped_timecode": 0,
        "skipped_trimmed": 0,
        "source_fallback_rows": 0,
        "source_duration_mismatch_rows": 0,
    }

    segments: List[Segment] = []
    timeline_cursor = 0
    base_offset: Optional[int] = None
    earliest_source_frame: Optional[int] = None
    latest_source_frame: Optional[int] = None
    earliest_edit_frame: Optional[int] = None
    latest_edit_frame: Optional[int] = None
    warnings: List[str] = []

    for row in rows:
        edit_start_value = _value(row, start_field)
        edit_end_value = _value(row, end_field)
        if not edit_start_value or not edit_end_value:
            continue
        try:
            edit_start_frame = parse_timecode(edit_start_value, fps)
            edit_end_frame = parse_timecode(edit_end_value, fps)
        except ValueError:
            stats["skipped_timecode"] += 1
            continue
        if edit_end_frame <= edit_start_frame:
            continue

        stats["rows_with_timecode"] += 1

        transcript_id = _value(row, transcript_field)
        if not include_empty_transcript and not transcript_id:
            stats["skipped_transcript"] += 1
            continue

        keep_value = _value(row, keep_field).lower()
        if keep_marker_lower and keep_value != keep_marker_lower:
            stats["skipped_keep_marker"] += 1
            continue

        edit_bounds = _trimmed_bounds(edit_start_frame, edit_end_frame, trim_start, trim_end)
        if edit_bounds is None:
            stats["skipped_trimmed"] += 1
            continue
        trimmed_edit_start, trimmed_edit_end = edit_bounds

        csv_duration = trimmed_edit_end - trimmed_edit_start
        if preserve_gaps:
            if base_offset is None:
                base_offset = trimmed_edit_start
            timeline_start = trimmed_edit_start - base_offset
        else:
            timeline_start = timeline_cursor
            timeline_cursor += csv_duration
        timeline_end = timeline_start + csv_duration

        source_in = trimmed_edit_start
        source_out = trimmed_edit_end
        source_start_value = _value(row, source_start_field)
        source_end_value = _value(row, source_end_field)
        if source_start_value and source_end_value:
            try:
                source_start_frame = parse_timecode(source_start_value, fps)
                source_end_frame = parse_timecode(source_end_value, fps)
            except ValueError:
                stats["source_fallback_rows"] += 1
                warnings.append(
                    "Transcript "
                    f"{transcript_id or '?'} has invalid source timecodes "
                    f"('{source_start_value}' -> '{source_end_value}'); using edit time bounds instead."
                )
            else:
                source_bounds = _trimmed_bounds(source_start_frame, source_end_frame, trim_start, trim_end)
                if source_bounds is None:
                    stats["source_fallback_rows"] += 1
                    warnings.append(
                        "Transcript "
                        f"{transcript_id or '?'} has source bounds that collapse after trimming "
                        f"('{source_start_value}' -> '{source_end_value}'); using edit time bounds instead."
                    )
                else:
                    candidate_source_in, candidate_source_out = source_bounds
                    source_duration = candidate_source_out - candidate_source_in
                    if source_duration != csv_duration:
                        stats["source_duration_mismatch_rows"] += 1
                        warnings.append(
                            "Transcript "
                            f"{transcript_id or '?'} source duration "
                            f"{source_duration} frame(s) does not match edit duration {csv_duration} frame(s); "
                            "keeping source bounds and edit timeline placement."
                        )
                    source_in = candidate_source_in
                    source_out = candidate_source_out
        elif source_start_value or source_end_value:
            stats["source_fallback_rows"] += 1
            warnings.append(
                "Transcript "
                f"{transcript_id or '?'} has incomplete source bounds "
                f"('{source_start_value}' -> '{source_end_value}'); using edit time bounds instead."
            )

        text_value = _value(row, text_field)
        segment = Segment(
            timeline_start=timeline_start,
            timeline_end=timeline_end,
            source_in=source_in,
            source_out=source_out,
            text=text_value,
        )
        segments.append(segment)
        stats["kept_rows"] += 1

        if earliest_source_frame is None or source_in < earliest_source_frame:
            earliest_source_frame = source_in
        if latest_source_frame is None or source_out > latest_source_frame:
            latest_source_frame = source_out
        if earliest_edit_frame is None or trimmed_edit_start < earliest_edit_frame:
            earliest_edit_frame = trimmed_edit_start
        if latest_edit_frame is None or trimmed_edit_end > latest_edit_frame:
            latest_edit_frame = trimmed_edit_end

    if (
        not segments
        or earliest_source_frame is None
        or latest_source_frame is None
        or earliest_edit_frame is None
        or latest_edit_frame is None
    ):
        raise ValueError(
            "No keepable segments were found. Double-check that the CSV contains rows where "
            f"Keep == '{keep_marker}' and Transcript # is filled."
        )

    return ExtractionResult(
        segments=segments,
        earliest_source_frame=earliest_source_frame,
        latest_source_frame=latest_source_frame,
        earliest_edit_frame=earliest_edit_frame,
        latest_edit_frame=latest_edit_frame,
        stats=stats,
        warnings=warnings,
    )


def main() -> None:
    args = parse_args()
    timebase = require_int_timebase(args.fps)

    if args.csv_path:
        csv_path = Path(args.csv_path).expanduser()
    else:
        csv_candidate = _latest_file(DEFAULT_COMPARER_OUTPUT, "*.csv")
        if csv_candidate is None:
            raise FileNotFoundError(
                f"No CSV provided and none found inside {DEFAULT_COMPARER_OUTPUT}. Pass --csv explicitly."
            )
        csv_path = csv_candidate
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    if args.output_path:
        output_path = Path(args.output_path).expanduser()
    else:
        output_path = DEFAULT_OUTPUT_DIR / f"{csv_path.stem}_premiere.xml"

    reference_path: Optional[Path] = None
    reference_info_media_path: Optional[Path] = None
    reference_sequence_start: Optional[int] = None
    reference_source_start: Optional[int] = None

    if args.reference_xml:
        reference_path = Path(args.reference_xml).expanduser()
    else:
        reference_candidate = _latest_file(DEFAULT_PREMIERE_XML_DIR, "*.xml")
        if reference_candidate is not None:
            reference_path = reference_candidate

    if reference_path and reference_path.is_file():
        try:
            _, sequence_el = load_reference_sequence(reference_path, args.reference_sequence)
            ref_info = extract_reference_media_info(sequence_el)
            reference_info_media_path = ref_info.media_path
            reference_sequence_start = ref_info.sequence_start_frame
            reference_source_start = ref_info.source_base_frame
        except Exception as exc:  # noqa: BLE001 - provide context but continue
            print(f"Warning: could not read reference XML {reference_path}: {exc}")

    extraction = extract_kept_segments(
        csv_path=csv_path,
        fps=timebase,
        keep_marker=args.keep_marker,
        include_empty_transcript=args.include_empty_transcript,
        trim_start=args.trim_start,
        trim_end=args.trim_end,
        preserve_gaps=args.preserve_gaps,
    )

    if args.media_path:
        media_path = Path(args.media_path).expanduser()
    elif reference_info_media_path:
        media_path = reference_info_media_path
    else:
        media_candidate = _latest_media_file(DEFAULT_MEDIA_DIR)
        if media_candidate is None:
            raise FileNotFoundError(
                "Could not determine media automatically. Provide --media or pass --reference-xml "
                "with a sequence that points to the correct rush, or add files to "
                f"{DEFAULT_MEDIA_DIR}."
            )
        media_path = media_candidate
    if not media_path.is_file():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    if args.media_timecode_start:
        source_base_frame = parse_timecode(args.media_timecode_start, timebase)
    elif reference_source_start is not None:
        source_base_frame = reference_source_start
    else:
        source_base_frame = extraction.earliest_source_frame

    if args.timecode_start:
        sequence_start_frame = parse_timecode(args.timecode_start, timebase)
    elif reference_sequence_start is not None:
        sequence_start_frame = reference_sequence_start
    else:
        sequence_start_frame = extraction.earliest_edit_frame

    sequence_name = args.sequence_name or csv_path.stem

    root = build_sequence_xml(
        sequence_name=sequence_name,
        media_path=media_path,
        segments=extraction.segments,
        timebase=timebase,
        source_base_frame=source_base_frame,
        sequence_start_frame=sequence_start_frame,
        video_width=args.video_width,
        video_height=args.video_height,
        audio_channels=args.audio_channels,
        audio_sample_rate=args.audio_sample_rate,
        pixel_aspect=args.pixel_aspect,
    )

    sequence_el = root.find(".//sequence")
    if sequence_el is not None:
        renumber_clip_indexes(sequence_el)

    write_xml(root, output_path)
    for warning in extraction.warnings:
        print(f"Warning: {warning}")
    _summarize_stats(csv_path.name, args.keep_marker, extraction.stats)
    print(f"XML written to {output_path}")


if __name__ == "__main__":
    main()
