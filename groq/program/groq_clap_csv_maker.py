#!/usr/bin/env python3
"""Groq transcription pipeline with clap-based take filtering."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Iterable, Iterator, List

import groq_noclap_csv_maker as base

POST_CLAP_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/groq/output/post_clap_output"
)
base.DEFAULT_OUTPUT_DIR = POST_CLAP_OUTPUT_DIR

CLAP_PROCESSOR_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/clap_editor/program/processor")
if str(CLAP_PROCESSOR_DIR) not in map(str, sys.path):
    sys.path.insert(0, str(CLAP_PROCESSOR_DIR))

try:
    from program_1 import detect_relaxed_claps
except ImportError as exc:  # pragma: no cover - env specific
    raise RuntimeError(
        "Unable to import clap detector from clap_editor/program/processor. "
        "Ensure that repository is available at the expected path."
    ) from exc

POST_DROP_SECONDS = 0.35
POST_CLAP_PADDING = 0.05
MIN_SEGMENT_DURATION = 0.3


def sanitize_output_path(path: Path) -> Path:
    """Replace spaces in the auto-generated filename to keep Premiere happy."""
    if " " not in path.name:
        return path
    sanitized = path.with_name(path.name.replace(" ", "_"))
    sanitized.parent.mkdir(parents=True, exist_ok=True)
    return sanitized


@dataclass
class TranscriptPiece:
    start: float
    end: float
    text: str


@dataclass
class TakeSegment:
    start: float
    end: float
    keep: bool
    marker_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with Groq and drop bad takes based on clap markers."
    )
    parser.add_argument(
        "audio",
        type=Path,
        nargs="?",
        help=f"Audio/video file to process (default: newest file in {base.DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Destination CSV path (defaults to {base.DEFAULT_OUTPUT_DIR}/<timestamp>.csv).",
    )
    parser.add_argument(
        "--speaker",
        default="Unknown",
        help='Speaker name to use for every row (default: "Unknown").',
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=base.DEFAULT_FRAME_RATE,
        help=f"Timecode frame rate (default: {base.DEFAULT_FRAME_RATE}).",
    )
    parser.add_argument(
        "--model",
        default=base.DEFAULT_MODEL,
        help=f"Groq Whisper model to use (default: {base.DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--api-key",
        help="Override the GROQ_API_KEY environment variable.",
    )
    parser.add_argument(
        "--json-input",
        type=Path,
        help="Optional raw Groq JSON response (skips live transcription; still needs --audio for clap detection).",
    )
    parser.add_argument(
        "--no-transcode",
        action="store_true",
        help="Disable automatic ffmpeg transcoding for large/video files.",
    )
    return parser.parse_args()


def ensure_ffprobe() -> str:
    exe = shutil.which("ffprobe")  # type: ignore[name-defined]
    if not exe:
        raise RuntimeError("ffprobe is required to probe media duration. Install ffmpeg/ffprobe first.")
    return exe


def probe_media_duration(path: Path) -> float:
    ffprobe = ensure_ffprobe()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffprobe failed while reading {path.name}: {exc.stderr}") from exc
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


@contextmanager
def clap_detection_wav(source: Path) -> Iterator[Path]:
    base.ensure_ffmpeg()
    temp_dir = tempfile.TemporaryDirectory()
    wav_path = Path(temp_dir.name) / "clap_detection.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-acodec",
        "pcm_s16le",
        str(wav_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        yield wav_path
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        raise RuntimeError(f"ffmpeg failed while preparing clap detection audio:\n{stderr}") from exc
    finally:
        temp_dir.cleanup()


def collect_transcript_pieces(data: dict) -> List[TranscriptPiece]:
    pieces: List[TranscriptPiece] = []
    segments = list(base.iter_segments(data))
    if not segments and data.get("text"):
        text = base.clean_text(data["text"])
        if text:
            pieces.append(TranscriptPiece(start=0.0, end=0.0, text=text))
        return pieces
    for segment in segments:
        text = base.clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        if end < start:
            end = start
        pieces.append(TranscriptPiece(start=start, end=end, text=text))
    return pieces


def build_take_segments(groups: Iterable[dict], duration: float) -> List[TakeSegment]:
    if duration <= 0:
        raise RuntimeError("Unable to determine media duration for clap filtering.")
    segments: List[TakeSegment] = []
    cursor = 0.0
    for group in groups:
        clap_start = group.get("tight_start", group.get("start", 0.0))
        clap_end = group.get("tight_end", group.get("end", clap_start))
        take_end = max(cursor, clap_start - POST_DROP_SECONDS)
        keep = group.get("type") == "triple"
        marker_type = group.get("type", "single")
        if take_end - cursor >= MIN_SEGMENT_DURATION:
            segments.append(
                TakeSegment(
                    start=cursor,
                    end=take_end,
                    keep=keep,
                    marker_type=marker_type,
                )
            )
        cursor = min(duration, clap_end + POST_CLAP_PADDING)
    if duration - cursor >= MIN_SEGMENT_DURATION:
        segments.append(TakeSegment(start=cursor, end=duration, keep=True, marker_type="tail"))
    return segments


def filter_kept_pieces(pieces: List[TranscriptPiece], segments: List[TakeSegment]) -> List[TranscriptPiece]:
    if not segments:
        return pieces
    kept: List[TranscriptPiece] = []
    seg_idx = 0
    for piece in pieces:
        while seg_idx < len(segments) and piece.start >= segments[seg_idx].end:
            seg_idx += 1
        if seg_idx >= len(segments):
            break
        segment = segments[seg_idx]
        overlaps = piece.end > segment.start and piece.start < segment.end
        if not overlaps:
            continue
        if not segment.keep:
            continue
        kept.append(piece)
    return kept


def build_rows(pieces: List[TranscriptPiece], speaker: str, frame_rate: float) -> list[list[str]]:
    rows: list[list[str]] = [["Keep", "Speaker Name", "Start Time", "End Time", "Text"]]
    for piece in pieces:
        rows.append(
            [
                "x",
                speaker,
                base.seconds_to_timecode(piece.start, frame_rate),
                base.seconds_to_timecode(piece.end, frame_rate),
                piece.text,
            ]
        )
    return rows


def main() -> int:
    args = parse_args()
    requested_media: Path | None = args.audio
    if not requested_media and not args.json_input:
        requested_media = base.latest_input_file(base.DEFAULT_INPUT_DIR)
    if args.json_input and not requested_media:
        print("Error: --json-input still requires --audio for clap detection.", file=sys.stderr)
        return 2
    preferred_source = base.prefer_audio_source(requested_media)
    reference_video = (
        requested_media
        if requested_media and requested_media.suffix.lower() in base.VIDEO_EXTENSIONS
        else None
    )
    output_path = sanitize_output_path(base.ensure_output_path(args.output))
    reference_mp3: Path | None = None
    if reference_video and (not preferred_source or preferred_source.suffix.lower() in base.VIDEO_EXTENSIONS):
        reference_mp3 = base.export_reference_mp3(reference_video, output_path.parent)
    transcription_data: dict
    if args.json_input:
        with args.json_input.open("r", encoding="utf-8") as handle:
            transcription_data = json.load(handle)
    else:
        if not preferred_source:
            print("Error: provide an audio path or use --json-input.", file=sys.stderr)
            return 2
        api_key = base.resolve_api_key(args.api_key)
        client = base.load_groq_client(api_key)
        allow_transcode = not args.no_transcode
        transcription_source = reference_mp3 or preferred_source
        with base.prepare_audio_file(transcription_source, allow_transcode) as media_path:
            transcription_data = base.transcribe_audio(client, media_path, args.model)
    pieces = collect_transcript_pieces(transcription_data)
    if not pieces:
        raise RuntimeError("No transcript segments were returned by Groq.")
    probe_source = preferred_source or reference_mp3
    if not probe_source:
        raise RuntimeError("Unable to determine clap reference media.")
    duration = probe_media_duration(probe_source)
    if duration <= 0:
        duration = max((piece.end for piece in pieces), default=0.0)
    with clap_detection_wav(probe_source) as wav_path:
        groups = detect_relaxed_claps(wav_path)
    segments = build_take_segments(groups, duration) if groups else [TakeSegment(0.0, duration, True, "tail")]
    kept_pieces = filter_kept_pieces(pieces, segments)
    if not kept_pieces:
        raise RuntimeError("All transcript segments were filtered out by clap detection.")
    rows = build_rows(kept_pieces, args.speaker, args.frame_rate)
    base.write_csv(output_path, rows)
    raw_json = base.write_raw_json(output_path, transcription_data)
    word_csv = base.write_word_level_csv(output_path, transcription_data, args.frame_rate)
    if word_csv is None:
        raise RuntimeError(
            "Groq transcription did not include word timestamps. "
            "A *_words.csv artifact is now required for no-word-gap trimming."
        )
    if reference_mp3:
        print(f"Audio reference exported to {reference_mp3}")
    print(f"Detected {len(groups)} clap groups; kept {len(kept_pieces)} transcript rows.")
    print(f"Wrote {len(kept_pieces)} rows to {output_path}")
    print(f"Wrote raw Groq JSON to {raw_json}")
    print(f"Wrote word-level timings to {word_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
