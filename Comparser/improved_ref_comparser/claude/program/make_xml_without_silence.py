#!/usr/bin/env python3
"""
Build a proof Premiere XML from Stage 01 silence analysis.

The script starts from `01_marked_silence.csv`, removes the rows already marked
as silence/eliminated there, then optionally trims additional non-speech spans
inside the surviving speech rows. The default trim mode now uses a VAD-style
speech activity detector over the rush audio so cuts follow speech presence
rather than raw volume.
"""

from __future__ import annotations

import argparse
import array
import csv
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

THIS_FILE = Path(__file__).resolve()
PROGRAM_DIR = THIS_FILE.parent.parent
CODE_VIDEO_ROOT = THIS_FILE.parents[4]
XML_EDITOR_PROGRAM_DIR = CODE_VIDEO_ROOT / "xml_editor_after_comparser" / "program"

if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))
if str(XML_EDITOR_PROGRAM_DIR) not in sys.path:
    sys.path.append(str(XML_EDITOR_PROGRAM_DIR))

from generate_premiere_xml import (  # noqa: E402
    Segment,
    build_sequence_xml,
    extract_reference_media_info,
    load_reference_sequence,
    parse_timecode,
    require_int_timebase,
    write_xml,
)
from lib.text_utils import ends_with_terminal_punctuation  # noqa: E402


CSV_SAMPLE_BYTES = 2048
DEFAULT_VIDEO_WIDTH = 1920
DEFAULT_VIDEO_HEIGHT = 1080
DEFAULT_AUDIO_CHANNELS = 2
DEFAULT_AUDIO_SAMPLE_RATE = 48000
DEFAULT_PIXEL_ASPECT = "square"
REMOVED_MARKER = "x"

DEFAULT_AUDIO_TRIM_MODE = "vad"
NO_WORD_GAP_AUDIO_TRIM_MODE = "no_word_gap"
LEGACY_PERCENTILE_AUDIO_TRIM_MODE = "percentile"
LEGACY_AUDIO_TRIM_MODE = "silencedetect"
DEFAULT_VAD_TRIGGER_LEVEL = 5.0
DEFAULT_VAD_WINDOW_MS = 400
DEFAULT_VAD_HOP_MS = 50
DEFAULT_VAD_MIN_SPEECH_SECONDS = 0.18
DEFAULT_VAD_MERGE_GAP_SECONDS = 0.15
DEFAULT_NO_WORD_GAP_SECONDS = 0.20
DEFAULT_PUNCTUATION_NO_WORD_GAP_SECONDS = 0.12
DEFAULT_NO_WORD_PADDING_SECONDS = 0.04
DEFAULT_AUDIO_TRIM_PERCENTILE = 10.0
DEFAULT_AUDIO_TRIM_THRESHOLD_LIFT = 1.45
DEFAULT_AUDIO_TRIM_MIN_LOW_SPAN_SECONDS = 1.0
DEFAULT_AUDIO_TRIM_WINDOW_MS = 100
DEFAULT_AUDIO_TRIM_HOP_MS = 50
DEFAULT_AUDIO_TRIM_PADDING_SECONDS = DEFAULT_NO_WORD_PADDING_SECONDS
DEFAULT_AUDIO_TRIM_MIN_CHUNK_SECONDS = 0.35
DEFAULT_AUDIO_ANALYSIS_SAMPLE_RATE = 16000

DEFAULT_AUDIO_TRIM_RELATIVE_DB = 34.0
DEFAULT_AUDIO_TRIM_FLOOR_DB = -50.0
DEFAULT_AUDIO_TRIM_MIN_SILENCE_SECONDS = 0.55


@dataclass
class Stage01Extraction:
    segments: List[Segment]
    earliest_source_frame: int
    latest_source_frame: int
    earliest_edit_frame: int
    latest_edit_frame: int
    total_rows: int
    speech_rows: int
    removed_rows: int
    removed_intervals: int
    removed_frames: int
    audio_trimmed_intervals: int = 0
    audio_trimmed_frames: int = 0
    audio_analyzed_rows: int = 0
    audio_fallback_rows: int = 0
    audio_trim_mode: str = ""
    audio_global_threshold: Optional[float] = None
    audio_split_row_ids: List[str] = field(default_factory=list)
    audio_fallback_row_ids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AudioTrimConfig:
    mode: str
    padding_seconds: float
    min_chunk_seconds: float
    vad_trigger_level: float
    vad_window_ms: int
    vad_hop_ms: int
    vad_min_speech_seconds: float
    vad_merge_gap_seconds: float
    no_word_gap_seconds: float
    punctuation_gap_seconds: float
    percentile: float
    threshold_lift: float
    min_low_span_seconds: float
    window_ms: int
    hop_ms: int
    sample_rate: int
    relative_db: float
    floor_db: float
    min_silence_seconds: float


@dataclass(frozen=True)
class SpeechRow:
    row_id: str
    source_start: int
    source_end: int
    text: str


@dataclass(frozen=True)
class TimedWord:
    row_id: str
    source_start: int
    source_end: int
    token: str


@dataclass
class ReferenceMetadata:
    media_path: Optional[Path]
    source_base_frame: Optional[int]
    sequence_start_frame: Optional[int]
    sequence_name: str
    video_width: int
    video_height: int
    audio_channels: int
    audio_sample_rate: int
    pixel_aspect: str


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a proof Premiere XML from 01_marked_silence.csv, with "
            "optional extra trimming of low-level internal silence from the rush audio."
        )
    )
    parser.add_argument(
        "--silence-csv",
        required=True,
        type=Path,
        help="Path to 01_marked_silence.csv.",
    )
    parser.add_argument(
        "--reference-xml",
        type=Path,
        help="Optional reference XML used only for media/sequence metadata.",
    )
    parser.add_argument(
        "--rush",
        type=Path,
        help="Rush media path. Overrides the media path found in --reference-xml.",
    )
    parser.add_argument(
        "--word-timeline",
        type=Path,
        help=(
            "Optional 11_word_timeline.csv used by legacy percentile mode."
        ),
    )
    parser.add_argument(
        "--word-csv",
        type=Path,
        help=(
            "Optional raw Groq *_words.csv used by vad/no_word_gap modes. "
            "When omitted, the script tries to resolve it from summary.json."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination XML. Defaults next to the silence CSV.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Frame rate used by the CSV timecodes. Defaults to summary.json final.frame_rate, otherwise 25.",
    )
    parser.add_argument(
        "--audio-trim",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Trim low-level spans inside kept speech rows (default: enabled).",
    )
    parser.add_argument(
        "--audio-trim-mode",
        choices=(
            DEFAULT_AUDIO_TRIM_MODE,
            NO_WORD_GAP_AUDIO_TRIM_MODE,
            LEGACY_PERCENTILE_AUDIO_TRIM_MODE,
            LEGACY_AUDIO_TRIM_MODE,
        ),
        default=DEFAULT_AUDIO_TRIM_MODE,
        help=(
            "Trim detector to use: vad speech activity from audio, no_word_gap "
            "from Groq word timestamps, legacy percentile RMS analysis, or "
            "legacy ffmpeg silencedetect "
            f"(default: {DEFAULT_AUDIO_TRIM_MODE})."
        ),
    )
    parser.add_argument(
        "--vad-trigger-level",
        type=float,
        default=DEFAULT_VAD_TRIGGER_LEVEL,
        help=(
            "VAD mode: trigger level used by torchaudio SoX-style voice activity detection "
            f"(default: {DEFAULT_VAD_TRIGGER_LEVEL})."
        ),
    )
    parser.add_argument(
        "--vad-window-ms",
        type=int,
        default=DEFAULT_VAD_WINDOW_MS,
        help=(
            "VAD mode: analysis window size in milliseconds "
            f"(default: {DEFAULT_VAD_WINDOW_MS})."
        ),
    )
    parser.add_argument(
        "--vad-hop-ms",
        type=int,
        default=DEFAULT_VAD_HOP_MS,
        help=(
            "VAD mode: hop size between analysis windows in milliseconds "
            f"(default: {DEFAULT_VAD_HOP_MS})."
        ),
    )
    parser.add_argument(
        "--vad-min-speech-seconds",
        type=float,
        default=DEFAULT_VAD_MIN_SPEECH_SECONDS,
        help=(
            "VAD mode: minimum speech interval duration kept after merging "
            f"(default: {DEFAULT_VAD_MIN_SPEECH_SECONDS})."
        ),
    )
    parser.add_argument(
        "--vad-merge-gap-seconds",
        type=float,
        default=DEFAULT_VAD_MERGE_GAP_SECONDS,
        help=(
            "VAD mode: merge neighboring detected speech intervals when the gap between "
            f"them is below this duration (default: {DEFAULT_VAD_MERGE_GAP_SECONDS})."
        ),
    )
    parser.add_argument(
        "--no-word-gap-seconds",
        type=float,
        default=DEFAULT_NO_WORD_GAP_SECONDS,
        help=(
            "Trim no-word gaps at or above this duration in seconds "
            f"(default: {DEFAULT_NO_WORD_GAP_SECONDS})."
        ),
    )
    parser.add_argument(
        "--punctuation-no-word-gap-seconds",
        type=float,
        default=DEFAULT_PUNCTUATION_NO_WORD_GAP_SECONDS,
        help=(
            "Trim shorter no-word gaps after terminal punctuation at or above this duration "
            f"(default: {DEFAULT_PUNCTUATION_NO_WORD_GAP_SECONDS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-percentile",
        type=float,
        default=DEFAULT_AUDIO_TRIM_PERCENTILE,
        help=(
            "Legacy percentile mode: percentile threshold "
            f"(default: {DEFAULT_AUDIO_TRIM_PERCENTILE})."
        ),
    )
    parser.add_argument(
        "--audio-trim-min-low-span",
        type=float,
        default=DEFAULT_AUDIO_TRIM_MIN_LOW_SPAN_SECONDS,
        help=(
            "Legacy percentile mode: minimum duration in seconds for a contiguous low-level span "
            "in "
            f"percentile mode (default: {DEFAULT_AUDIO_TRIM_MIN_LOW_SPAN_SECONDS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-threshold-lift",
        type=float,
        default=DEFAULT_AUDIO_TRIM_THRESHOLD_LIFT,
        help=(
            "Legacy percentile mode: multiplier applied to the low-level threshold so "
            "near-silent plateaus slightly above the raw cutoff are still removed "
            f"(default: {DEFAULT_AUDIO_TRIM_THRESHOLD_LIFT})."
        ),
    )
    parser.add_argument(
        "--audio-trim-window-ms",
        type=int,
        default=DEFAULT_AUDIO_TRIM_WINDOW_MS,
        help=(
            "Legacy percentile mode: RMS window size in milliseconds "
            f"(default: {DEFAULT_AUDIO_TRIM_WINDOW_MS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-hop-ms",
        type=int,
        default=DEFAULT_AUDIO_TRIM_HOP_MS,
        help=(
            "Legacy percentile mode: RMS hop size in milliseconds "
            f"(default: {DEFAULT_AUDIO_TRIM_HOP_MS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-padding",
        type=float,
        default=DEFAULT_AUDIO_TRIM_PADDING_SECONDS,
        help=(
            "Word-anchored safety padding in seconds around removed no-word spans, "
            "and legacy audio trim padding "
            f"(default: {DEFAULT_AUDIO_TRIM_PADDING_SECONDS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-min-chunk",
        type=float,
        default=DEFAULT_AUDIO_TRIM_MIN_CHUNK_SECONDS,
        help=(
            "Minimum surviving speech chunk duration in seconds "
            f"(default: {DEFAULT_AUDIO_TRIM_MIN_CHUNK_SECONDS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-relative-db",
        type=float,
        default=DEFAULT_AUDIO_TRIM_RELATIVE_DB,
        help=(
            "Legacy silencedetect mode: relative dB below row peak used as "
            f"the silence threshold (default: {DEFAULT_AUDIO_TRIM_RELATIVE_DB})."
        ),
    )
    parser.add_argument(
        "--audio-trim-floor-db",
        type=float,
        default=DEFAULT_AUDIO_TRIM_FLOOR_DB,
        help=(
            "Legacy silencedetect mode: lower bound for the silence threshold "
            f"in dB (default: {DEFAULT_AUDIO_TRIM_FLOOR_DB})."
        ),
    )
    parser.add_argument(
        "--audio-trim-min-silence",
        type=float,
        default=DEFAULT_AUDIO_TRIM_MIN_SILENCE_SECONDS,
        help=(
            "Legacy silencedetect mode: minimum silence duration in seconds "
            f"(default: {DEFAULT_AUDIO_TRIM_MIN_SILENCE_SECONDS})."
        ),
    )
    parser.add_argument(
        "--audio-trim-debug",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Print row IDs that were split or preserved via fallback.",
    )
    return parser.parse_args(argv)


def _canonicalize(label: str) -> str:
    return label.strip().lower().replace(" ", "").replace("#", "")


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


def _load_rows(csv_path: Path) -> Tuple[List[dict], List[str]]:
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
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


def _parse_row_bounds(
    row: dict,
    row_index: int,
    fps: int,
    start_field: str,
    end_field: str,
) -> Tuple[int, int]:
    start_value = _value(row, start_field)
    end_value = _value(row, end_field)
    if not start_value or not end_value:
        raise ValueError(f"Row {row_index} is missing Start/End timecodes.")
    try:
        start_frame = parse_timecode(start_value, fps)
        end_frame = parse_timecode(end_value, fps)
    except ValueError as exc:
        raise ValueError(
            f"Row {row_index} has invalid timecodes '{start_value}' -> '{end_value}'."
        ) from exc
    if end_frame <= start_frame:
        raise ValueError(
            f"Row {row_index} has non-positive duration '{start_value}' -> '{end_value}'."
        )
    return start_frame, end_frame


def _frames_to_seconds(frames: int, fps: int) -> float:
    return max(0.0, frames / float(fps))


def _seconds_to_frames(seconds: float, fps: int) -> int:
    return max(0, int(round(seconds * float(fps))))


def _is_removed_row(row: dict, kind_field: Optional[str], eliminate_field: Optional[str]) -> bool:
    kind = _value(row, kind_field).lower()
    eliminate = _value(row, eliminate_field).lower()
    return kind == "silence" or eliminate == REMOVED_MARKER


def _is_speech_row(row: dict, kind_field: Optional[str]) -> bool:
    return _value(row, kind_field).lower() == "speech"


def _merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: List[Tuple[int, int]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _collapse_frame(frame: int, removed_intervals: Sequence[Tuple[int, int]]) -> int:
    collapsed = frame
    for start, end in removed_intervals:
        if frame <= start:
            break
        collapsed -= min(frame, end) - start
    return max(0, collapsed)


def _extract_stage01_speech_rows(
    rows: Sequence[dict],
    fieldnames: Sequence[str],
    fps: int,
) -> Tuple[List[SpeechRow], List[Tuple[int, int]], int, int, int, int]:
    start_field = _resolve_field(fieldnames, ("Start Time", "Start"))
    end_field = _resolve_field(fieldnames, ("End Time", "End"))
    kind_field = _resolve_field(fieldnames, ("Kind",))
    eliminate_field = _resolve_field(fieldnames, ("Eliminate",))
    text_field = _resolve_field(fieldnames, ("Text", "Dialogue"))
    row_id_field = _resolve_field(fieldnames, ("Row ID",))

    if not start_field or not end_field:
        raise ValueError("Stage 01 CSV must contain Start Time and End Time columns.")
    if not kind_field:
        raise ValueError("Stage 01 CSV must contain a Kind column.")

    speech_rows: List[SpeechRow] = []
    removed_rows = 0
    removed_row_intervals: List[Tuple[int, int]] = []

    for row_index, row in enumerate(rows, start=2):
        start_frame, end_frame = _parse_row_bounds(row, row_index, fps, start_field, end_field)
        if _is_removed_row(row, kind_field, eliminate_field):
            removed_row_intervals.append((start_frame, end_frame))
            removed_rows += 1
            continue
        if not _is_speech_row(row, kind_field):
            continue
        speech_rows.append(
            SpeechRow(
                row_id=_value(row, row_id_field) or str(len(speech_rows) + 1),
                source_start=start_frame,
                source_end=end_frame,
                text=_value(row, text_field),
            )
        )

    if not speech_rows:
        raise ValueError("Stage 01 CSV did not contain any surviving speech rows.")

    removed_intervals = _merge_intervals(removed_row_intervals)
    removed_frames = sum(end - start for start, end in removed_intervals)
    return (
        speech_rows,
        removed_intervals,
        removed_rows,
        len(removed_intervals),
        removed_frames,
        len(rows),
    )


def _build_segments_from_speech_rows(speech_rows: Sequence[SpeechRow]) -> List[Segment]:
    ordered = sorted(speech_rows, key=lambda row: row.source_start)
    timeline_cursor = 0
    segments: List[Segment] = []
    for row in ordered:
        duration = row.source_end - row.source_start
        if duration <= 0:
            continue
        segments.append(
            Segment(
                timeline_start=timeline_cursor,
                timeline_end=timeline_cursor + duration,
                source_in=row.source_start,
                source_out=row.source_end,
                text=row.text,
            )
        )
        timeline_cursor += duration
    return segments


def build_stage01_segments_from_rows(
    rows: Sequence[dict],
    fieldnames: Sequence[str],
    fps: int,
) -> Stage01Extraction:
    speech_rows, removed_intervals, removed_rows, removed_interval_count, removed_frames, total_rows = (
        _extract_stage01_speech_rows(rows, fieldnames, fps)
    )
    raw_segments: List[Tuple[int, int, int, int, str]] = []
    for row in sorted(speech_rows, key=lambda entry: entry.source_start):
        edit_start = _collapse_frame(row.source_start, removed_intervals)
        edit_end = _collapse_frame(row.source_end, removed_intervals)
        if edit_end <= edit_start:
            raise ValueError(
                "A kept speech row collapsed to zero duration. "
                f"Source bounds: {row.source_start} -> {row.source_end}."
            )
        raw_segments.append((edit_start, edit_end, row.source_start, row.source_end, row.text))

    if not raw_segments:
        raise ValueError("Stage 01 CSV did not contain any surviving segments.")

    timeline_base = min(
        edit_start for edit_start, _edit_end, _source_start, _source_end, _text in raw_segments
    )
    segments = [
        Segment(
            timeline_start=edit_start - timeline_base,
            timeline_end=edit_end - timeline_base,
            source_in=source_start,
            source_out=source_end,
            text=text,
        )
        for edit_start, edit_end, source_start, source_end, text in raw_segments
    ]

    earliest_source_frame = min(
        source_start
        for _edit_start, _edit_end, source_start, _source_end, _text in raw_segments
    )
    latest_source_frame = max(
        source_end for _edit_start, _edit_end, _source_start, source_end, _text in raw_segments
    )
    latest_edit_frame = max(segment.timeline_end for segment in segments)
    return Stage01Extraction(
        segments=segments,
        earliest_source_frame=earliest_source_frame,
        latest_source_frame=latest_source_frame,
        earliest_edit_frame=0,
        latest_edit_frame=latest_edit_frame,
        total_rows=total_rows,
        speech_rows=len(speech_rows),
        removed_rows=removed_rows,
        removed_intervals=removed_interval_count,
        removed_frames=removed_frames,
    )


def _run_ffmpeg_text(cmd: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for audio-based silence trimming.") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {message}")
    return f"{result.stdout}\n{result.stderr}"


def _run_ffmpeg_bytes(cmd: Sequence[str]) -> bytes:
    try:
        result = subprocess.run(
            list(cmd),
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for audio-based silence trimming.") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() or "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {message}")
    return result.stdout


def _load_torchaudio_vad() -> Tuple[object, object]:
    try:
        import torch
        import torchaudio.functional as torchaudio_functional
    except ImportError as exc:
        raise RuntimeError("VAD mode requires torch and torchaudio to be installed.") from exc
    return torch, torchaudio_functional


def _decode_media_audio_tensor(
    media_path: Path,
    sample_rate: int,
):
    torch, _torchaudio_functional = _load_torchaudio_vad()
    raw_bytes = _run_ffmpeg_bytes(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = array.array("f")
    if raw_bytes:
        samples.frombytes(raw_bytes)
        if sys.byteorder != "little":
            samples.byteswap()
    if not samples:
        return torch.zeros((1, 0), dtype=torch.float32)
    return torch.tensor(samples, dtype=torch.float32).unsqueeze(0)


def _parse_peak_db(output: str) -> Optional[float]:
    match = re.search(
        r"max_volume:\s*(-?(?:\d+(?:\.\d+)?)|inf|-inf)\s*dB",
        output,
        re.IGNORECASE,
    )
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"inf", "-inf"}:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _analyze_clip_peak_db(
    media_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> Optional[float]:
    if duration_seconds <= 0:
        return None
    output = _run_ffmpeg_text(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{start_seconds:.6f}",
            "-t",
            f"{duration_seconds:.6f}",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
    )
    return _parse_peak_db(output)


def _parse_silence_intervals(output: str, clip_duration_seconds: float) -> List[Tuple[float, float]]:
    intervals: List[Tuple[float, float]] = []
    current_start: Optional[float] = None
    for line in output.splitlines():
        start_match = re.search(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)", line, re.IGNORECASE)
        if start_match:
            current_start = float(start_match.group(1))
            continue
        end_match = re.search(
            r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)",
            line,
            re.IGNORECASE,
        )
        if end_match and current_start is not None:
            intervals.append((current_start, float(end_match.group(1))))
            current_start = None
    if current_start is not None:
        intervals.append((current_start, clip_duration_seconds))
    return intervals


def _merge_second_intervals(intervals: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: List[Tuple[float, float]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _merge_second_intervals_with_gap(
    intervals: Sequence[Tuple[float, float]],
    max_gap_seconds: float,
) -> List[Tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: List[Tuple[float, float]] = []
    current_start, current_end = ordered[0]
    max_gap_seconds = max(0.0, max_gap_seconds)
    for start, end in ordered[1:]:
        if start <= current_end + max_gap_seconds:
            current_end = max(current_end, end)
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _trim_silence_intervals_to_speech_chunks(
    row: SpeechRow,
    silence_intervals: Sequence[Tuple[float, float]],
    fps: int,
    padding_seconds: float,
    min_chunk_seconds: float,
) -> Tuple[List[SpeechRow], int]:
    clip_duration_seconds = _frames_to_seconds(row.source_end - row.source_start, fps)
    if clip_duration_seconds <= 0:
        return [], 0

    merged_silence = _merge_second_intervals(silence_intervals)
    chunks: List[SpeechRow] = []
    removed_frames = 0
    cursor_seconds = 0.0
    padding_seconds = max(0.0, padding_seconds)
    min_chunk_seconds = max(0.0, min_chunk_seconds)

    for silence_start, silence_end in merged_silence:
        silence_start = max(0.0, min(clip_duration_seconds, silence_start))
        silence_end = max(0.0, min(clip_duration_seconds, silence_end))
        keep_end_seconds = max(cursor_seconds, silence_start - padding_seconds)
        if keep_end_seconds - cursor_seconds >= min_chunk_seconds:
            chunk_start_frame = row.source_start + _seconds_to_frames(cursor_seconds, fps)
            chunk_end_frame = min(
                row.source_end,
                row.source_start + _seconds_to_frames(keep_end_seconds, fps),
            )
            if chunk_end_frame > chunk_start_frame:
                chunks.append(
                    SpeechRow(
                        row_id=row.row_id,
                        source_start=chunk_start_frame,
                        source_end=chunk_end_frame,
                        text=row.text,
                    )
                )
        remove_start_seconds = max(cursor_seconds, silence_start + padding_seconds)
        remove_end_seconds = max(remove_start_seconds, silence_end - padding_seconds)
        removed_frames += max(
            0,
            _seconds_to_frames(remove_end_seconds, fps)
            - _seconds_to_frames(remove_start_seconds, fps),
        )
        cursor_seconds = max(cursor_seconds, silence_end + padding_seconds)

    if clip_duration_seconds - cursor_seconds >= min_chunk_seconds:
        chunk_start_frame = row.source_start + _seconds_to_frames(cursor_seconds, fps)
        if row.source_end > chunk_start_frame:
            chunks.append(
                SpeechRow(
                    row_id=row.row_id,
                    source_start=chunk_start_frame,
                    source_end=row.source_end,
                    text=row.text,
                )
            )
    return chunks, removed_frames


def _decode_media_audio_samples(
    media_path: Path,
    sample_rate: int,
) -> array.array:
    raw_bytes = _run_ffmpeg_bytes(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = array.array("f")
    if raw_bytes:
        samples.frombytes(raw_bytes)
        if sys.byteorder != "little":
            samples.byteswap()
    return samples


def _vad_parameters(audio_config: AudioTrimConfig) -> Dict[str, float]:
    return {
        "trigger_level": float(audio_config.vad_trigger_level),
        "trigger_time": 0.10,
        "search_time": 0.20,
        "allowed_gap": 0.12,
        "pre_trigger_time": 0.0,
        "boot_time": 0.10,
        "noise_up_time": 0.05,
        "noise_down_time": 0.01,
        "noise_reduction_amount": 1.20,
        "measure_freq": 20.0,
        "measure_duration": None,
        "measure_smooth_time": 0.10,
        "hp_filter_freq": 50.0,
        "lp_filter_freq": 6000.0,
        "hp_lifter_freq": 150.0,
        "lp_lifter_freq": 2000.0,
    }


def _detect_speech_interval_in_chunk(
    waveform,
    sample_rate: int,
    audio_config: AudioTrimConfig,
) -> Optional[Tuple[int, int]]:
    if waveform.numel() == 0:
        return None
    torch, torchaudio_functional = _load_torchaudio_vad()
    params = _vad_parameters(audio_config)
    front = torchaudio_functional.vad(waveform, sample_rate, **params)
    if front.numel() == 0 or front.shape[-1] == 0:
        return None
    lead_trim = waveform.shape[-1] - front.shape[-1]
    reverse_front = torch.flip(front, dims=[-1])
    back = torchaudio_functional.vad(reverse_front, sample_rate, **params)
    if back.numel() == 0 or back.shape[-1] == 0:
        return None
    tail_trim = front.shape[-1] - back.shape[-1]
    speech_start = max(0, int(lead_trim))
    speech_end = max(speech_start, int(waveform.shape[-1] - tail_trim))
    if speech_end <= speech_start:
        return None
    return speech_start, speech_end


def _analysis_window_starts(total_samples: int, window_samples: int, hop_samples: int) -> List[int]:
    if total_samples <= 0:
        return []
    if total_samples <= window_samples:
        return [0]
    starts = list(range(0, total_samples - window_samples + 1, max(1, hop_samples)))
    final_start = total_samples - window_samples
    if starts[-1] != final_start:
        starts.append(final_start)
    return starts


def _compute_global_vad_speech_intervals(
    media_path: Path,
    audio_config: AudioTrimConfig,
) -> List[Tuple[float, float]]:
    waveform = _decode_media_audio_tensor(media_path, audio_config.sample_rate)
    total_samples = int(waveform.shape[-1])
    if total_samples <= 0:
        return []

    window_samples = max(1, int(round(audio_config.sample_rate * audio_config.vad_window_ms / 1000.0)))
    hop_samples = max(1, int(round(audio_config.sample_rate * audio_config.vad_hop_ms / 1000.0)))
    intervals: List[Tuple[float, float]] = []

    for chunk_start in _analysis_window_starts(total_samples, window_samples, hop_samples):
        chunk_end = min(total_samples, chunk_start + window_samples)
        chunk = waveform[:, chunk_start:chunk_end]
        speech_bounds = _detect_speech_interval_in_chunk(chunk, audio_config.sample_rate, audio_config)
        if speech_bounds is None:
            continue
        speech_start, speech_end = speech_bounds
        absolute_start = (chunk_start + speech_start) / float(audio_config.sample_rate)
        absolute_end = (chunk_start + speech_end) / float(audio_config.sample_rate)
        if absolute_end <= absolute_start:
            continue
        intervals.append((absolute_start, absolute_end))

    merged = _merge_second_intervals_with_gap(intervals, audio_config.vad_merge_gap_seconds)
    return [
        (start, end)
        for start, end in merged
        if end - start >= audio_config.vad_min_speech_seconds
    ]


def _slice_audio_samples_for_row(
    media_samples: array.array,
    sample_rate: int,
    row: SpeechRow,
    fps: int,
) -> array.array:
    start_index = max(0, int(round(_frames_to_seconds(row.source_start, fps) * sample_rate)))
    end_index = max(start_index, int(round(_frames_to_seconds(row.source_end, fps) * sample_rate)))
    end_index = min(end_index, len(media_samples))
    if start_index >= len(media_samples) or end_index <= start_index:
        return array.array("f")
    return media_samples[start_index:end_index]


def _compute_window_levels(
    samples: array.array,
    sample_rate: int,
    window_ms: int,
    hop_ms: int,
) -> List[Tuple[float, float, float]]:
    if not samples:
        return []

    window_size = max(1, int(round(sample_rate * window_ms / 1000.0)))
    hop_size = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    sample_count = len(samples)

    sumsq_prefix: List[float] = [0.0]
    total = 0.0
    for sample in samples:
        total += float(sample) * float(sample)
        sumsq_prefix.append(total)

    windows: List[Tuple[float, float, float]] = []
    start = 0
    while start < sample_count:
        end = min(sample_count, start + window_size)
        span = end - start
        if span <= 0:
            break
        sumsq = sumsq_prefix[end] - sumsq_prefix[start]
        rms = math.sqrt(max(0.0, sumsq / span))
        windows.append((start / sample_rate, end / sample_rate, rms))
        if start + hop_size >= sample_count:
            break
        start += hop_size
    return windows


def _percentile_value(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    percentile = max(0.0, min(100.0, percentile))
    position = (len(ordered) - 1) * (percentile / 100.0)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return ordered[lower_index]
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight


def _compute_span_average_levels(
    window_levels: Sequence[Tuple[float, float, float]],
    min_low_span_seconds: float,
) -> List[Tuple[float, float, float]]:
    if not window_levels:
        return []
    if min_low_span_seconds <= 0:
        return list(window_levels)

    level_prefix: List[float] = [0.0]
    total = 0.0
    for _start, _end, level in window_levels:
        total += level
        level_prefix.append(total)

    span_levels: List[Tuple[float, float, float]] = []
    end_index = 0
    for start_index, (span_start, _span_window_end, _level) in enumerate(window_levels):
        if end_index < start_index:
            end_index = start_index
        while (
            end_index < len(window_levels)
            and window_levels[end_index][1] - span_start < min_low_span_seconds
        ):
            end_index += 1
        if end_index >= len(window_levels):
            break
        span_end = window_levels[end_index][1]
        count = end_index + 1 - start_index
        average_level = (level_prefix[end_index + 1] - level_prefix[start_index]) / count
        span_levels.append((span_start, span_end, average_level))
    return span_levels


def _compute_low_level_threshold_from_span_levels(
    span_levels: Sequence[Tuple[float, float, float]],
    percentile: float,
    threshold_lift: float,
) -> float:
    if not span_levels:
        return 0.0
    base_threshold = _percentile_value([level for _start, _end, level in span_levels], percentile)
    return base_threshold * max(0.0, threshold_lift)


def _detect_low_level_intervals_from_span_levels(
    span_levels: Sequence[Tuple[float, float, float]],
    threshold: float,
    min_low_span_seconds: float,
) -> List[Tuple[float, float]]:
    if not span_levels:
        return []

    low_spans: List[Tuple[float, float]] = []
    current_start: Optional[float] = None
    current_end: float = 0.0

    for start, end, level in span_levels:
        if level <= threshold:
            if current_start is None:
                current_start = start
                current_end = end
            else:
                current_end = max(current_end, end)
            continue
        if current_start is not None:
            low_spans.append((current_start, current_end))
        current_start = None
        current_end = 0.0

    if current_start is not None:
        low_spans.append((current_start, current_end))
    merged_spans = _merge_second_intervals(low_spans)
    return [
        (start, end)
        for start, end in merged_spans
        if end - start >= min_low_span_seconds
    ]


def _detect_low_level_intervals_from_window_levels(
    window_levels: Sequence[Tuple[float, float, float]],
    percentile: float,
    threshold_lift: float,
    min_low_span_seconds: float,
) -> Tuple[List[Tuple[float, float]], float]:
    if not window_levels:
        return [], 0.0

    span_levels = _compute_span_average_levels(window_levels, min_low_span_seconds)
    if not span_levels:
        return [], 0.0

    threshold = _compute_low_level_threshold_from_span_levels(
        span_levels,
        percentile,
        threshold_lift,
    )
    return (
        _detect_low_level_intervals_from_span_levels(
            span_levels,
            threshold,
            min_low_span_seconds,
        ),
        threshold,
    )


def _compute_global_percentile_threshold(
    media_samples: array.array,
    audio_config: AudioTrimConfig,
) -> float:
    window_levels = _compute_window_levels(
        media_samples,
        audio_config.sample_rate,
        audio_config.window_ms,
        audio_config.hop_ms,
    )
    span_levels = _compute_span_average_levels(
        window_levels,
        audio_config.min_low_span_seconds,
    )
    return _compute_low_level_threshold_from_span_levels(
        span_levels,
        audio_config.percentile,
        audio_config.threshold_lift,
    )


def _compute_global_percentile_intervals(
    media_samples: array.array,
    audio_config: AudioTrimConfig,
) -> Tuple[List[Tuple[float, float]], float]:
    window_levels = _compute_window_levels(
        media_samples,
        audio_config.sample_rate,
        audio_config.window_ms,
        audio_config.hop_ms,
    )
    span_levels = _compute_span_average_levels(
        window_levels,
        audio_config.min_low_span_seconds,
    )
    threshold = _compute_low_level_threshold_from_span_levels(
        span_levels,
        audio_config.percentile,
        audio_config.threshold_lift,
    )
    intervals = _detect_low_level_intervals_from_span_levels(
        span_levels,
        threshold,
        audio_config.min_low_span_seconds,
    )
    return intervals, threshold


def _detect_percentile_low_level_intervals_for_row(
    media_samples: array.array,
    row: SpeechRow,
    fps: int,
    audio_config: AudioTrimConfig,
    global_intervals: Sequence[Tuple[float, float]],
) -> Tuple[List[Tuple[float, float]], float]:
    row_start_seconds = _frames_to_seconds(row.source_start, fps)
    row_end_seconds = _frames_to_seconds(row.source_end, fps)
    row_intervals: List[Tuple[float, float]] = []
    for start, end in global_intervals:
        if end <= row_start_seconds:
            continue
        if start >= row_end_seconds:
            break
        row_intervals.append(
            (
                max(0.0, start - row_start_seconds),
                min(row_end_seconds - row_start_seconds, end - row_start_seconds),
            )
        )
    return _merge_second_intervals(row_intervals), 0.0


def _row_relative_seconds_to_source_frames(
    row: SpeechRow,
    interval: Tuple[float, float],
    fps: int,
) -> Optional[Tuple[int, int]]:
    start_seconds, end_seconds = interval
    start_frame = row.source_start + max(0, int(math.floor(start_seconds * float(fps))))
    end_frame = row.source_start + max(0, int(math.ceil(end_seconds * float(fps))))
    start_frame = max(row.source_start, min(row.source_end, start_frame))
    end_frame = max(row.source_start, min(row.source_end, end_frame))
    if end_frame <= start_frame:
        return None
    return start_frame, end_frame


def _silence_intervals_to_source_frames(
    row: SpeechRow,
    silence_intervals: Sequence[Tuple[float, float]],
    fps: int,
) -> List[Tuple[int, int]]:
    frame_intervals = [
        frame_interval
        for frame_interval in (
            _row_relative_seconds_to_source_frames(row, interval, fps)
            for interval in silence_intervals
        )
        if frame_interval is not None
    ]
    return _merge_intervals(frame_intervals)


def _interval_fully_contains_word(
    word: TimedWord,
    silence_intervals: Sequence[Tuple[int, int]],
) -> bool:
    return any(
        silence_start <= word.source_start and word.source_end <= silence_end
        for silence_start, silence_end in silence_intervals
    )


def _interval_splits_between_words(
    previous_word: TimedWord,
    current_word: TimedWord,
    silence_intervals: Sequence[Tuple[int, int]],
) -> bool:
    return any(
        silence_start < current_word.source_start and silence_end > previous_word.source_end
        for silence_start, silence_end in silence_intervals
    )


def _chunk_text_from_words(words: Sequence[TimedWord], fallback_text: str) -> str:
    text = " ".join(word.token.strip() for word in words if word.token.strip()).strip()
    return text or fallback_text


def _words_for_row(raw_words: Sequence[TimedWord], row: SpeechRow) -> List[TimedWord]:
    words = [
        TimedWord(
            row_id=row.row_id,
            source_start=max(row.source_start, word.source_start),
            source_end=min(row.source_end, word.source_end),
            token=word.token,
        )
        for word in raw_words
        if word.source_end > row.source_start and word.source_start < row.source_end
    ]
    return [word for word in words if word.source_end > word.source_start]


def _effective_gap_threshold(previous_word: TimedWord, audio_config: AudioTrimConfig) -> float:
    base_threshold = audio_config.no_word_gap_seconds
    if ends_with_terminal_punctuation(previous_word.token):
        base_threshold = min(base_threshold, audio_config.punctuation_gap_seconds)
    return max(0.0, base_threshold)


def _trim_row_to_vad_chunks(
    row: SpeechRow,
    speech_intervals: Sequence[Tuple[float, float]],
    fps: int,
    audio_config: AudioTrimConfig,
    raw_words: Optional[Sequence[TimedWord]] = None,
) -> Tuple[List[SpeechRow], int]:
    clip_duration_seconds = _frames_to_seconds(row.source_end - row.source_start, fps)
    if clip_duration_seconds <= 0:
        return [], 0

    row_start_seconds = _frames_to_seconds(row.source_start, fps)
    row_end_seconds = _frames_to_seconds(row.source_end, fps)
    local_intervals: List[Tuple[float, float]] = []
    for start, end in speech_intervals:
        if end <= row_start_seconds:
            continue
        if start >= row_end_seconds:
            break
        local_intervals.append(
            (
                max(0.0, start - row_start_seconds),
                min(clip_duration_seconds, end - row_start_seconds),
            )
        )
    if not local_intervals:
        return [], 0

    merged_local = _merge_second_intervals_with_gap(local_intervals, audio_config.vad_merge_gap_seconds)
    padding_seconds = max(0.0, audio_config.padding_seconds)
    min_chunk_frames = max(0, _seconds_to_frames(audio_config.min_chunk_seconds, fps))
    row_words = _words_for_row(raw_words or [], row) if raw_words else []
    chunks: List[SpeechRow] = []
    kept_frames = 0

    for start_seconds, end_seconds in merged_local:
        keep_start_seconds = max(0.0, start_seconds - padding_seconds)
        keep_end_seconds = min(clip_duration_seconds, end_seconds + padding_seconds)
        if keep_end_seconds <= keep_start_seconds:
            continue
        chunk_start_frame = max(row.source_start, row.source_start + _seconds_to_frames(keep_start_seconds, fps))
        chunk_end_frame = min(row.source_end, row.source_start + _seconds_to_frames(keep_end_seconds, fps))
        if chunk_end_frame <= chunk_start_frame:
            continue
        if chunk_end_frame - chunk_start_frame < min_chunk_frames:
            continue
        chunk_words = [
            word
            for word in row_words
            if word.source_end > chunk_start_frame and word.source_start < chunk_end_frame
        ]
        kept_frames += chunk_end_frame - chunk_start_frame
        chunks.append(
            SpeechRow(
                row_id=row.row_id,
                source_start=chunk_start_frame,
                source_end=chunk_end_frame,
                text=_chunk_text_from_words(chunk_words, row.text),
            )
        )

    if not chunks:
        return [], 0
    return chunks, max(0, (row.source_end - row.source_start) - kept_frames)


def _trim_row_to_no_word_gap_chunks(
    row: SpeechRow,
    raw_words: Sequence[TimedWord],
    fps: int,
    audio_config: AudioTrimConfig,
) -> Tuple[List[SpeechRow], int]:
    row_words = _words_for_row(raw_words, row)
    if not row_words:
        return [], 0

    padding_frames = _seconds_to_frames(audio_config.padding_seconds, fps)
    min_chunk_frames = max(0, _seconds_to_frames(audio_config.min_chunk_seconds, fps))
    chunks: List[SpeechRow] = []
    current_group: List[TimedWord] = [row_words[0]]

    for word in row_words[1:]:
        previous_word = current_group[-1]
        gap_frames = max(0, word.source_start - previous_word.source_end)
        gap_seconds = _frames_to_seconds(gap_frames, fps)
        if gap_seconds >= _effective_gap_threshold(previous_word, audio_config):
            group_start = max(row.source_start, current_group[0].source_start - padding_frames)
            group_end = min(row.source_end, current_group[-1].source_end + padding_frames)
            if group_end - group_start >= min_chunk_frames:
                chunks.append(
                    SpeechRow(
                        row_id=row.row_id,
                        source_start=group_start,
                        source_end=group_end,
                        text=_chunk_text_from_words(current_group, row.text),
                    )
                )
            current_group = [word]
            continue
        current_group.append(word)

    group_start = max(row.source_start, current_group[0].source_start - padding_frames)
    group_end = min(row.source_end, current_group[-1].source_end + padding_frames)
    if group_end - group_start >= min_chunk_frames:
        chunks.append(
            SpeechRow(
                row_id=row.row_id,
                source_start=group_start,
                source_end=group_end,
                text=_chunk_text_from_words(current_group, row.text),
            )
        )

    if not chunks:
        return [], 0
    kept_frames = sum(chunk.source_end - chunk.source_start for chunk in chunks)
    return chunks, max(0, (row.source_end - row.source_start) - kept_frames)


def _trim_row_to_word_chunks(
    row: SpeechRow,
    row_words: Sequence[TimedWord],
    silence_intervals: Sequence[Tuple[float, float]],
    fps: int,
    min_chunk_seconds: float,
) -> Tuple[List[SpeechRow], int]:
    clipped_words = [
        TimedWord(
            row_id=word.row_id,
            source_start=max(row.source_start, word.source_start),
            source_end=min(row.source_end, word.source_end),
            token=word.token,
        )
        for word in row_words
        if word.source_end > row.source_start and word.source_start < row.source_end
    ]
    clipped_words = [word for word in clipped_words if word.source_end > word.source_start]
    if not clipped_words:
        return [], 0

    frame_intervals = _silence_intervals_to_source_frames(row, silence_intervals, fps)
    if not frame_intervals:
        return [row], 0

    kept_words = [
        word for word in clipped_words if not _interval_fully_contains_word(word, frame_intervals)
    ]
    if not kept_words:
        return [], 0

    word_groups: List[List[TimedWord]] = []
    current_group: List[TimedWord] = []
    for word in kept_words:
        if not current_group:
            current_group = [word]
            continue
        if _interval_splits_between_words(current_group[-1], word, frame_intervals):
            word_groups.append(current_group)
            current_group = [word]
            continue
        current_group.append(word)
    if current_group:
        word_groups.append(current_group)

    min_chunk_frames = max(0, _seconds_to_frames(min_chunk_seconds, fps))
    chunks: List[SpeechRow] = []
    kept_frames = 0
    for group in word_groups:
        chunk_start = max(row.source_start, group[0].source_start)
        chunk_end = min(row.source_end, group[-1].source_end)
        if chunk_end <= chunk_start:
            continue
        if chunk_end - chunk_start < min_chunk_frames:
            continue
        kept_frames += chunk_end - chunk_start
        chunks.append(
            SpeechRow(
                row_id=row.row_id,
                source_start=chunk_start,
                source_end=chunk_end,
                text=_chunk_text_from_words(group, row.text),
            )
        )

    if not chunks:
        return [], 0
    return chunks, max(0, (row.source_end - row.source_start) - kept_frames)


def _detect_silencedetect_intervals_for_row(
    media_path: Path,
    row: SpeechRow,
    fps: int,
    audio_config: AudioTrimConfig,
) -> List[Tuple[float, float]]:
    clip_duration_seconds = _frames_to_seconds(row.source_end - row.source_start, fps)
    clip_start_seconds = _frames_to_seconds(row.source_start, fps)
    peak_db = _analyze_clip_peak_db(media_path, clip_start_seconds, clip_duration_seconds)
    threshold_db = max(
        audio_config.floor_db,
        (peak_db if peak_db is not None else audio_config.floor_db) - audio_config.relative_db,
    )
    output = _run_ffmpeg_text(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            f"{clip_start_seconds:.6f}",
            "-t",
            f"{clip_duration_seconds:.6f}",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-af",
            f"silencedetect=n={threshold_db:.2f}dB:d={audio_config.min_silence_seconds:.3f}",
            "-f",
            "null",
            "-",
        ]
    )
    return _parse_silence_intervals(output, clip_duration_seconds)


def _detect_audio_trim_intervals_for_row(
    media_source: Path | array.array,
    row: SpeechRow,
    fps: int,
    audio_config: AudioTrimConfig,
    global_intervals: Optional[Sequence[Tuple[float, float]]] = None,
) -> Tuple[List[Tuple[float, float]], Optional[float]]:
    if audio_config.mode == LEGACY_AUDIO_TRIM_MODE:
        return _detect_silencedetect_intervals_for_row(
            media_source,
            row,
            fps,
            audio_config,
        ), None
    if audio_config.mode != LEGACY_PERCENTILE_AUDIO_TRIM_MODE:
        raise ValueError(f"Unsupported detector for row-level audio intervals: {audio_config.mode}")
    if global_intervals is None:
        raise ValueError("Percentile mode requires whole-video silence intervals.")
    return _detect_percentile_low_level_intervals_for_row(
        media_source,
        row,
        fps,
        audio_config,
        global_intervals,
    )


def build_audio_trimmed_segments_from_rows(
    rows: Sequence[dict],
    fieldnames: Sequence[str],
    fps: int,
    media_path: Path,
    audio_config: AudioTrimConfig,
    raw_words: Optional[Sequence[TimedWord]] = None,
    word_timings_by_row: Optional[Dict[str, List[TimedWord]]] = None,
) -> Stage01Extraction:
    speech_rows, _removed_intervals, removed_rows, removed_interval_count, removed_frames, total_rows = (
        _extract_stage01_speech_rows(rows, fieldnames, fps)
    )

    trimmed_rows: List[SpeechRow] = []
    audio_trimmed_intervals = 0
    audio_trimmed_frames = 0
    audio_fallback_rows = 0
    audio_split_row_ids: List[str] = []
    audio_fallback_row_ids: List[str] = []
    audio_global_threshold: Optional[float] = None
    audio_global_intervals: Optional[List[Tuple[float, float]]] = None
    media_source: Path | array.array = media_path

    if audio_config.mode == DEFAULT_AUDIO_TRIM_MODE:
        audio_global_intervals = _compute_global_vad_speech_intervals(media_path, audio_config)
    elif audio_config.mode == NO_WORD_GAP_AUDIO_TRIM_MODE:
        if raw_words is None:
            raise ValueError(
                "no_word_gap mode requires raw Groq *_words.csv timings."
            )
    elif audio_config.mode == LEGACY_PERCENTILE_AUDIO_TRIM_MODE:
        if word_timings_by_row is None:
            raise ValueError(
                "Percentile mode requires a word timeline so cut boundaries can snap to Groq words."
            )
        media_source = _decode_media_audio_samples(media_path, audio_config.sample_rate)
        audio_global_intervals, audio_global_threshold = _compute_global_percentile_intervals(
            media_source,
            audio_config,
        )

    for row in speech_rows:
        if audio_config.mode == DEFAULT_AUDIO_TRIM_MODE:
            row_chunks, row_removed_frames = _trim_row_to_vad_chunks(
                row,
                audio_global_intervals or [],
                fps,
                audio_config,
                raw_words=raw_words,
            )
            if not row_chunks:
                trimmed_rows.append(row)
                audio_fallback_rows += 1
                audio_fallback_row_ids.append(row.row_id)
                continue
            if row_removed_frames > 0 or len(row_chunks) != 1 or row_chunks[0] != row:
                audio_split_row_ids.append(row.row_id)
            trimmed_rows.extend(row_chunks)
            audio_trimmed_intervals += max(0, len(row_chunks) - 1)
            audio_trimmed_frames += row_removed_frames
            continue

        if audio_config.mode == NO_WORD_GAP_AUDIO_TRIM_MODE:
            row_chunks, row_removed_frames = _trim_row_to_no_word_gap_chunks(
                row,
                raw_words or [],
                fps,
                audio_config,
            )
            if not row_chunks:
                trimmed_rows.append(row)
                audio_fallback_rows += 1
                audio_fallback_row_ids.append(row.row_id)
                continue
            if row_removed_frames > 0 or len(row_chunks) != 1 or row_chunks[0] != row:
                audio_split_row_ids.append(row.row_id)
            trimmed_rows.extend(row_chunks)
            audio_trimmed_intervals += max(0, len(row_chunks) - 1)
            audio_trimmed_frames += row_removed_frames
            continue

        try:
            trim_intervals, _row_threshold = _detect_audio_trim_intervals_for_row(
                media_source,
                row,
                fps,
                audio_config,
                audio_global_intervals,
            )
        except RuntimeError:
            raise
        except Exception:
            trim_intervals = []
            audio_fallback_rows += 1
            audio_fallback_row_ids.append(row.row_id)

        if not trim_intervals:
            trimmed_rows.append(row)
            continue

        if audio_config.mode == LEGACY_PERCENTILE_AUDIO_TRIM_MODE:
            row_words = word_timings_by_row.get(row.row_id, []) if word_timings_by_row else []
            if not row_words:
                trimmed_rows.append(row)
                audio_fallback_rows += 1
                audio_fallback_row_ids.append(row.row_id)
                continue
            row_chunks, row_removed_frames = _trim_row_to_word_chunks(
                row,
                row_words,
                trim_intervals,
                fps,
                audio_config.min_chunk_seconds,
            )
        else:
            row_chunks, row_removed_frames = _trim_silence_intervals_to_speech_chunks(
                row,
                trim_intervals,
                fps,
                audio_config.padding_seconds,
                audio_config.min_chunk_seconds,
            )
        if not row_chunks:
            trimmed_rows.append(row)
            audio_fallback_rows += 1
            audio_fallback_row_ids.append(row.row_id)
            continue

        if row_removed_frames > 0 or len(row_chunks) != 1 or row_chunks[0] != row:
            audio_split_row_ids.append(row.row_id)
        trimmed_rows.extend(row_chunks)
        audio_trimmed_intervals += len(trim_intervals)
        audio_trimmed_frames += row_removed_frames

    segments = _build_segments_from_speech_rows(trimmed_rows)
    if not segments:
        raise ValueError("Audio-trimmed extraction did not produce any surviving segments.")

    return Stage01Extraction(
        segments=segments,
        earliest_source_frame=min(segment.source_in for segment in segments),
        latest_source_frame=max(segment.source_out for segment in segments),
        earliest_edit_frame=0,
        latest_edit_frame=max(segment.timeline_end for segment in segments),
        total_rows=total_rows,
        speech_rows=len(speech_rows),
        removed_rows=removed_rows,
        removed_intervals=removed_interval_count,
        removed_frames=removed_frames,
        audio_trimmed_intervals=audio_trimmed_intervals,
        audio_trimmed_frames=audio_trimmed_frames,
        audio_analyzed_rows=len(speech_rows),
        audio_fallback_rows=audio_fallback_rows,
        audio_trim_mode=audio_config.mode,
        audio_global_threshold=audio_global_threshold,
        audio_split_row_ids=audio_split_row_ids,
        audio_fallback_row_ids=audio_fallback_row_ids,
    )


def _parse_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip()) if value is not None and str(value).strip() else default
    except ValueError:
        return default


def load_reference_metadata(reference_xml: Path) -> ReferenceMetadata:
    _tree, sequence_el = load_reference_sequence(reference_xml, None)
    ref_info = extract_reference_media_info(sequence_el)

    return ReferenceMetadata(
        media_path=ref_info.media_path,
        source_base_frame=ref_info.source_base_frame,
        sequence_start_frame=ref_info.sequence_start_frame,
        sequence_name=(sequence_el.findtext("name") or reference_xml.stem).strip(),
        video_width=_parse_int(
            sequence_el.findtext("./media/video/format/samplecharacteristics/width"),
            DEFAULT_VIDEO_WIDTH,
        ),
        video_height=_parse_int(
            sequence_el.findtext("./media/video/format/samplecharacteristics/height"),
            DEFAULT_VIDEO_HEIGHT,
        ),
        audio_channels=_parse_int(
            sequence_el.findtext("./media/audio/format/samplecharacteristics/channelcount"),
            DEFAULT_AUDIO_CHANNELS,
        ),
        audio_sample_rate=_parse_int(
            sequence_el.findtext("./media/audio/format/samplecharacteristics/samplerate"),
            DEFAULT_AUDIO_SAMPLE_RATE,
        ),
        pixel_aspect=(
            sequence_el.findtext("./media/video/format/samplecharacteristics/pixelaspectratio")
            or DEFAULT_PIXEL_ASPECT
        ).strip(),
    )


def _default_output_path(silence_csv: Path) -> Path:
    return silence_csv.with_name(f"{silence_csv.stem}_without_silence.xml")


def _default_word_timeline_path(silence_csv: Path) -> Path:
    return silence_csv.with_name("11_word_timeline.csv")


def _default_word_csv_path(silence_csv: Path) -> Optional[Path]:
    summary_path = silence_csv.with_name("summary.json")
    if not summary_path.is_file():
        return None
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if not isinstance(inputs, dict):
        return None
    explicit_words = str(inputs.get("words") or "").strip()
    if explicit_words:
        candidate = Path(explicit_words).expanduser()
        return candidate if candidate.exists() else None
    input_csv = str(inputs.get("csv") or "").strip()
    if not input_csv:
        return None
    candidate = Path(input_csv).expanduser().with_name(f"{Path(input_csv).stem}_words.csv")
    return candidate if candidate.exists() else None


def _default_fps_from_summary(silence_csv: Path) -> Optional[float]:
    summary_path = silence_csv.with_name("summary.json")
    if not summary_path.is_file():
        return None
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    final = payload.get("final") if isinstance(payload, dict) else None
    if not isinstance(final, dict):
        return None
    try:
        return float(final.get("frame_rate"))
    except (TypeError, ValueError):
        return None


def _build_audio_trim_config(args: argparse.Namespace) -> AudioTrimConfig:
    return AudioTrimConfig(
        mode=args.audio_trim_mode,
        padding_seconds=max(0.0, args.audio_trim_padding),
        min_chunk_seconds=max(0.0, args.audio_trim_min_chunk),
        vad_trigger_level=max(0.0, args.vad_trigger_level),
        vad_window_ms=max(1, args.vad_window_ms),
        vad_hop_ms=max(1, args.vad_hop_ms),
        vad_min_speech_seconds=max(0.0, args.vad_min_speech_seconds),
        vad_merge_gap_seconds=max(0.0, args.vad_merge_gap_seconds),
        no_word_gap_seconds=max(0.0, args.no_word_gap_seconds),
        punctuation_gap_seconds=max(0.0, args.punctuation_no_word_gap_seconds),
        percentile=max(0.0, min(100.0, args.audio_trim_percentile)),
        threshold_lift=max(0.0, args.audio_trim_threshold_lift),
        min_low_span_seconds=max(0.0, args.audio_trim_min_low_span),
        window_ms=max(1, args.audio_trim_window_ms),
        hop_ms=max(1, args.audio_trim_hop_ms),
        sample_rate=DEFAULT_AUDIO_ANALYSIS_SAMPLE_RATE,
        relative_db=args.audio_trim_relative_db,
        floor_db=args.audio_trim_floor_db,
        min_silence_seconds=max(0.0, args.audio_trim_min_silence),
    )


def _load_word_timeline_map(word_timeline_csv: Path, fps: int) -> Dict[str, List[TimedWord]]:
    rows, fieldnames = _load_rows(word_timeline_csv)
    row_id_field = _resolve_field(fieldnames, ("Row ID",))
    source_start_field = _resolve_field(fieldnames, ("Source Start Time",))
    source_end_field = _resolve_field(fieldnames, ("Source End Time",))
    transcript_token_field = _resolve_field(fieldnames, ("Transcript Token", "Word", "Token"))
    reference_token_field = _resolve_field(fieldnames, ("Reference Token",))

    if not row_id_field or not source_start_field or not source_end_field:
        raise ValueError(
            f"Word timeline must contain Row ID, Source Start Time, and Source End Time: {word_timeline_csv}"
        )

    grouped: Dict[str, List[TimedWord]] = {}
    for row_index, row in enumerate(rows, start=2):
        source_start_value = _value(row, source_start_field)
        source_end_value = _value(row, source_end_field)
        if not source_start_value or not source_end_value:
            continue
        try:
            source_start = parse_timecode(source_start_value, fps)
            source_end = parse_timecode(source_end_value, fps)
        except ValueError as exc:
            raise ValueError(
                f"Word timeline row {row_index} has invalid source timing "
                f"'{source_start_value}' -> '{source_end_value}'."
            ) from exc
        if source_end <= source_start:
            continue
        row_id = _value(row, row_id_field)
        if not row_id:
            continue
        token = _value(row, transcript_token_field) or _value(row, reference_token_field)
        grouped.setdefault(row_id, []).append(
            TimedWord(
                row_id=row_id,
                source_start=source_start,
                source_end=source_end,
                token=token,
            )
        )

    for row_words in grouped.values():
        row_words.sort(key=lambda word: (word.source_start, word.source_end, word.token))
    return grouped


def _load_raw_word_csv(word_csv: Path, fps: int) -> List[TimedWord]:
    rows, fieldnames = _load_rows(word_csv)
    token_field = _resolve_field(fieldnames, ("Word", "Transcript Token", "Token"))
    start_field = _resolve_field(fieldnames, ("Start Time",))
    end_field = _resolve_field(fieldnames, ("End Time",))
    if not token_field or not start_field or not end_field:
        raise ValueError(f"Word CSV must contain Word, Start Time and End Time: {word_csv}")

    words: List[TimedWord] = []
    for row_index, row in enumerate(rows, start=2):
        token = _value(row, token_field)
        start_value = _value(row, start_field)
        end_value = _value(row, end_field)
        if not token or not start_value or not end_value:
            continue
        try:
            start_frame = parse_timecode(start_value, fps)
            end_frame = parse_timecode(end_value, fps)
        except ValueError as exc:
            raise ValueError(
                f"Word CSV row {row_index} has invalid timing '{start_value}' -> '{end_value}'."
            ) from exc
        if end_frame <= start_frame:
            continue
        words.append(TimedWord(row_id="", source_start=start_frame, source_end=end_frame, token=token))
    words.sort(key=lambda word: (word.source_start, word.source_end, word.token))
    return words


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    silence_csv = args.silence_csv.expanduser()
    if not silence_csv.is_file():
        raise FileNotFoundError(f"Silence CSV not found: {silence_csv}")

    fps_value = args.fps if args.fps is not None else (_default_fps_from_summary(silence_csv) or 25.0)
    timebase = require_int_timebase(fps_value)

    rows, fieldnames = _load_rows(silence_csv)

    reference: Optional[ReferenceMetadata] = None
    if args.reference_xml:
        reference_xml = args.reference_xml.expanduser()
        if not reference_xml.is_file():
            raise FileNotFoundError(f"Reference XML not found: {reference_xml}")
        reference = load_reference_metadata(reference_xml)

    media_path = args.rush.expanduser() if args.rush else (reference.media_path if reference else None)
    if media_path is None:
        raise FileNotFoundError(
            "Could not determine rush media. Pass --rush or provide --reference-xml with a valid media path."
        )
    if not media_path.is_file():
        raise FileNotFoundError(f"Rush media not found: {media_path}")

    audio_config = _build_audio_trim_config(args)
    word_csv_path: Optional[Path] = None
    raw_words: Optional[List[TimedWord]] = None
    word_timeline_path: Optional[Path] = None
    word_timings_by_row: Optional[Dict[str, List[TimedWord]]] = None

    if args.audio_trim and audio_config.mode in (DEFAULT_AUDIO_TRIM_MODE, NO_WORD_GAP_AUDIO_TRIM_MODE):
        word_csv_path = args.word_csv.expanduser() if args.word_csv else _default_word_csv_path(silence_csv)
        if word_csv_path is not None and word_csv_path.is_file():
            raw_words = _load_raw_word_csv(word_csv_path, timebase)
            if not raw_words and audio_config.mode == NO_WORD_GAP_AUDIO_TRIM_MODE:
                raise ValueError(f"Word CSV contains no timed words: {word_csv_path}")
        elif audio_config.mode == NO_WORD_GAP_AUDIO_TRIM_MODE:
            raise FileNotFoundError(
                "no_word_gap mode requires Groq *_words.csv timings. "
                "Pass --word-csv or rerun Groq transcription so summary.json resolves a real words artifact."
            )
    elif args.audio_trim and audio_config.mode == LEGACY_PERCENTILE_AUDIO_TRIM_MODE:
        word_timeline_path = (
            args.word_timeline.expanduser()
            if args.word_timeline
            else _default_word_timeline_path(silence_csv)
        )
        if not word_timeline_path.is_file():
            raise FileNotFoundError(
                "Percentile mode requires 11_word_timeline.csv for Groq-word boundary snapping. "
                f"Pass --word-timeline or place it here: {word_timeline_path}"
            )
        word_timings_by_row = _load_word_timeline_map(word_timeline_path, timebase)

    if args.audio_trim:
        extraction = build_audio_trimmed_segments_from_rows(
            rows,
            fieldnames,
            timebase,
            media_path,
            audio_config,
            raw_words=raw_words,
            word_timings_by_row=word_timings_by_row,
        )
    else:
        extraction = build_stage01_segments_from_rows(rows, fieldnames, timebase)

    output_path = args.output.expanduser() if args.output else _default_output_path(silence_csv)
    sequence_name = (
        reference.sequence_name if reference else f"{silence_csv.parent.name}_stage01_without_silence"
    )
    source_base_frame = (
        reference.source_base_frame
        if reference and reference.source_base_frame is not None
        else extraction.earliest_source_frame
    )
    sequence_start_frame = (
        reference.sequence_start_frame
        if reference and reference.sequence_start_frame is not None
        else extraction.earliest_edit_frame
    )

    root = build_sequence_xml(
        sequence_name=sequence_name,
        media_path=media_path,
        segments=extraction.segments,
        timebase=timebase,
        source_base_frame=source_base_frame,
        sequence_start_frame=sequence_start_frame,
        video_width=reference.video_width if reference else DEFAULT_VIDEO_WIDTH,
        video_height=reference.video_height if reference else DEFAULT_VIDEO_HEIGHT,
        audio_channels=reference.audio_channels if reference else DEFAULT_AUDIO_CHANNELS,
        audio_sample_rate=reference.audio_sample_rate if reference else DEFAULT_AUDIO_SAMPLE_RATE,
        pixel_aspect=reference.pixel_aspect if reference else DEFAULT_PIXEL_ASPECT,
    )
    write_xml(root, output_path)

    print(f"Stage 01 CSV: {silence_csv}")
    print(f"Output XML: {output_path}")
    print(f"Rush media: {media_path}")
    print(
        f"Rows: {extraction.total_rows} total, {extraction.speech_rows} kept speech, "
        f"{extraction.removed_rows} removed row(s) merged into {extraction.removed_intervals} interval(s)."
    )
    print(
        f"Stage-01 removed duration: {extraction.removed_frames} frame(s). "
        f"Proof timeline duration: {extraction.latest_edit_frame} frame(s)."
    )
    print(f"Output clip count: {len(extraction.segments)}")

    if args.audio_trim:
        if audio_config.mode == DEFAULT_AUDIO_TRIM_MODE:
            if word_csv_path is not None and word_csv_path.is_file():
                print(f"Word CSV: {word_csv_path}")
            print(
                "Audio trim settings: "
                f"mode={audio_config.mode}, "
                f"trigger={audio_config.vad_trigger_level:.2f}, "
                f"window={audio_config.vad_window_ms}ms, "
                f"hop={audio_config.vad_hop_ms}ms, "
                f"min_speech={audio_config.vad_min_speech_seconds:.2f}s, "
                f"merge_gap={audio_config.vad_merge_gap_seconds:.2f}s, "
                f"padding={audio_config.padding_seconds:.2f}s, "
                f"min_chunk={audio_config.min_chunk_seconds:.2f}s."
            )
        elif audio_config.mode == NO_WORD_GAP_AUDIO_TRIM_MODE:
            if word_csv_path is not None and word_csv_path.is_file():
                print(f"Word CSV: {word_csv_path}")
            print(
                "Audio trim settings: "
                f"mode={audio_config.mode}, "
                f"no_word_gap={audio_config.no_word_gap_seconds:.2f}s, "
                f"punctuation_gap={audio_config.punctuation_gap_seconds:.2f}s, "
                f"padding={audio_config.padding_seconds:.2f}s, "
                f"min_chunk={audio_config.min_chunk_seconds:.2f}s."
            )
        elif audio_config.mode == LEGACY_PERCENTILE_AUDIO_TRIM_MODE:
            if word_timeline_path is not None:
                print(f"Word timeline: {word_timeline_path}")
            print(
                "Audio trim settings: "
                f"mode={audio_config.mode}, "
                f"percentile={audio_config.percentile:.2f}, "
                f"threshold_lift={audio_config.threshold_lift:.2f}, "
                "threshold_scope=whole_video, "
                f"min_low_span={audio_config.min_low_span_seconds:.2f}s, "
                f"window={audio_config.window_ms}ms, "
                f"hop={audio_config.hop_ms}ms, "
                f"min_chunk={audio_config.min_chunk_seconds:.2f}s."
            )
            if extraction.audio_global_threshold is not None:
                print(f"Audio trim whole-video threshold: {extraction.audio_global_threshold:.9f}")
        else:
            print(
                "Audio trim settings: "
                f"mode={audio_config.mode}, "
                f"relative={audio_config.relative_db:.2f}dB, "
                f"floor={audio_config.floor_db:.2f}dB, "
                f"min_silence={audio_config.min_silence_seconds:.2f}s, "
                f"padding={audio_config.padding_seconds:.2f}s, "
                f"min_chunk={audio_config.min_chunk_seconds:.2f}s."
            )
        print(
            "Audio trim: "
            f"analyzed {extraction.audio_analyzed_rows} speech row(s), "
            f"trimmed {extraction.audio_trimmed_intervals} internal interval(s), "
            f"removed {extraction.audio_trimmed_frames} additional frame(s), "
            f"fallback rows {extraction.audio_fallback_rows}."
        )
        if args.audio_trim_debug:
            if extraction.audio_split_row_ids:
                print("Audio trim split rows: " + ", ".join(extraction.audio_split_row_ids))
            if extraction.audio_fallback_row_ids:
                print("Audio trim fallback rows: " + ", ".join(extraction.audio_fallback_row_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
