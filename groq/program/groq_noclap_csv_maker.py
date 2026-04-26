#!/usr/bin/env python3
"""Generate Premiere-style transcript CSVs from Groq transcriptions."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Iterable, Iterator, Sequence

DEFAULT_MODEL = "whisper-large-v3"
DEFAULT_FRAME_RATE = 30.0
DEFAULT_INPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/groq/output/audio"
)
DEFAULT_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/groq/output/no_clap_output"
)
MAX_UPLOAD_BYTES = 23 * 1024 * 1024
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".webm",
    ".mpg",
    ".mpeg",
}


def prefer_audio_source(path: Path | None) -> Path | None:
    if not path:
        return None
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        candidate = DEFAULT_INPUT_DIR / f"{path.stem}.mp3"
        if candidate.exists():
            return candidate
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with Groq and emit a Premiere-compatible CSV."
    )
    parser.add_argument(
        "audio",
        type=Path,
        nargs="?",
        help=f"Audio/video file to transcribe (default: newest file in {DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Destination CSV path (defaults to {DEFAULT_OUTPUT_DIR}/<timestamp>.csv).",
    )
    parser.add_argument(
        "--speaker",
        default="Unknown",
        help='Speaker name to use for every row (default: "Unknown").',
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=DEFAULT_FRAME_RATE,
        help=f"Timecode frame rate (default: {DEFAULT_FRAME_RATE}).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Groq Whisper model to use (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--api-key",
        help="Override the GROQ_API_KEY environment variable.",
    )
    parser.add_argument(
        "--json-input",
        type=Path,
        help="Optional raw Groq JSON response (skips live transcription).",
    )
    parser.add_argument(
        "--no-transcode",
        action="store_true",
        help="Disable automatic ffmpeg transcoding for large/video files.",
    )
    return parser.parse_args()


def resolve_api_key(candidate: str | None) -> str:
    api_key = candidate or os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Set GROQ_API_KEY or pass --api-key to reach Groq.")
    return api_key


def ensure_output_path(candidate: Path | None) -> Path:
    if candidate:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%d_%H_%M_%S")
    return DEFAULT_OUTPUT_DIR / f"{timestamp}.csv"


def latest_input_file(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [
        path
        for path in directory.glob("*")
        if path.suffix.lower()
        in AUDIO_EXTENSIONS.union(VIDEO_EXTENSIONS)
    ]
    if not candidates:
        raise FileNotFoundError(f"No supported media files found in {directory}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_groq_client(api_key: str):
    try:
        from groq import Groq  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "groq package is missing. Install it with `pip install groq`."
        ) from exc
    return Groq(api_key=api_key)


def transcribe_audio(client: Any, audio_path: Path, model: str) -> dict[str, Any]:
    if not audio_path or not audio_path.exists():
        raise FileNotFoundError(f"Audio file {audio_path} does not exist.")
    with audio_path.open("rb") as handle:
        response = client.audio.transcriptions.create(
            file=handle,
            model=model,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )
    return normalize_response(response)


def normalize_response(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    for attr in ("model_dump", "dict"):
        loader = getattr(payload, attr, None)
        if callable(loader):
            return loader()
    dump_json = getattr(payload, "model_dump_json", None)
    if callable(dump_json):
        return json.loads(dump_json())
    return json.loads(json.dumps(payload, default=_fallback_to_dict))


def _fallback_to_dict(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def iter_segments(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    segments = data.get("segments")
    if isinstance(segments, list):
        return segments
    alt = data.get("results") or data.get("data")
    if isinstance(alt, dict):
        candidate = alt.get("segments")
        if isinstance(candidate, list):
            return candidate
    return []


def seconds_to_timecode(value: float, frame_rate: float) -> str:
    total_frames = max(0, int(round(value * frame_rate)))
    fps = max(1, int(round(frame_rate)))
    frames = total_frames % fps
    total_seconds = total_frames // fps
    seconds = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split()).strip()


def build_rows(
    data: dict[str, Any],
    speaker: str,
    frame_rate: float,
) -> list[list[str]]:
    rows: list[list[str]] = [["Speaker Name", "Start Time", "End Time", "Text"]]
    segments = list(iter_segments(data))
    if not segments and data.get("text"):
        text = clean_text(data["text"])
        if text:
            rows.append([speaker, "00:00:00:00", "00:00:00:00", text])
        return rows
    for segment in segments:
        text = clean_text(segment.get("text"))
        if not text:
            continue
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        rows.append(
            [
                speaker,
                seconds_to_timecode(start, frame_rate),
                seconds_to_timecode(end, frame_rate),
                text,
            ]
        )
    return rows


def iter_word_entries(data: dict[str, Any]) -> Iterable[tuple[str, float, float]]:
    top_level_words = data.get("words")
    if isinstance(top_level_words, list):
        for word in top_level_words:
            if not isinstance(word, dict):
                continue
            token = clean_text(word.get("word"))
            if not token:
                continue
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start))
            yield (token, start, end)
        return

    segments = list(iter_segments(data))
    for segment in segments:
        segment_start = float(segment.get("start", 0.0))
        words = segment.get("words")
        if not isinstance(words, list):
            continue
        for word in words:
            if not isinstance(word, dict):
                continue
            token = clean_text(word.get("word"))
            if not token:
                continue
            start = float(word.get("start", segment_start))
            end = float(word.get("end", start))
            yield (token, start, end)


def build_word_rows(entries: Sequence[tuple[str, float, float]], frame_rate: float) -> list[list[str]]:
    rows: list[list[str]] = [["Word", "Start Time", "End Time"]]
    for token, start, end in entries:
        rows.append(
            [
                token,
                seconds_to_timecode(start, frame_rate),
                seconds_to_timecode(end, frame_rate),
            ]
        )
    return rows


def needs_transcode(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return True
    if suffix not in AUDIO_EXTENSIONS:
        return True
    return path.stat().st_size >= MAX_UPLOAD_BYTES


def ensure_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required for automatic transcoding. Install it first.")
    return exe


def _run_ffmpeg(cmd: list[str], context: str) -> None:
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
        raise RuntimeError(f"ffmpeg failed while {context}:\n{stderr}") from exc


def _convert_to_light_mp3(source: Path, target: Path) -> Path:
    ensure_ffmpeg()
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(target),
    ]
    _run_ffmpeg(cmd, f"transcoding {source.name}")
    return target


def _convert_to_reference_mp3(source: Path, target: Path) -> Path:
    ensure_ffmpeg()
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(target),
    ]
    _run_ffmpeg(cmd, f"exporting reference audio from {source.name}")
    return target


def transcode_audio_file(source: Path) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp_path = Path(tmp.name)
    tmp.close()
    return _convert_to_light_mp3(source, tmp_path)


def export_reference_mp3(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{source.stem}_audio.mp3"
    return _convert_to_reference_mp3(source, target)


@contextmanager
def prepare_audio_file(source: Path, allow_transcode: bool) -> Iterator[Path]:
    temp_path: Path | None = None
    try:
        candidate = source
        if allow_transcode and needs_transcode(source):
            temp_path = transcode_audio_file(source)
            candidate = temp_path
        yield candidate
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        for row in rows:
            writer.writerow(row)


def write_word_level_csv(base_path: Path, data: dict[str, Any], frame_rate: float) -> Path | None:
    entries = list(iter_word_entries(data))
    if not entries:
        return None
    target = base_path.with_name(f"{base_path.stem}_words.csv")
    rows = build_word_rows(entries, frame_rate)
    write_csv(target, rows)
    return target


def write_raw_json(base_path: Path, data: dict[str, Any]) -> Path:
    target = base_path.with_suffix(".json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main() -> int:
    args = parse_args()
    requested_media: Path | None = args.audio
    if not requested_media and not args.json_input:
        requested_media = latest_input_file(DEFAULT_INPUT_DIR)
    preferred_source = prefer_audio_source(requested_media)
    original_video = (
        requested_media if requested_media and requested_media.suffix.lower() in VIDEO_EXTENSIONS else None
    )
    output_path = ensure_output_path(args.output)
    reference_mp3: Path | None = None
    if original_video and (not preferred_source or preferred_source.suffix.lower() in VIDEO_EXTENSIONS):
        reference_mp3 = export_reference_mp3(original_video, output_path.parent)
    transcription_data: dict[str, Any]
    if args.json_input:
        with args.json_input.open("r", encoding="utf-8") as handle:
            transcription_data = json.load(handle)
    else:
        if not preferred_source:
            print("Error: provide an audio path or use --json-input.", file=sys.stderr)
            return 2
        api_key = resolve_api_key(args.api_key)
        client = load_groq_client(api_key)
        allow_transcode = not args.no_transcode
        transcription_source = reference_mp3 or preferred_source
        with prepare_audio_file(transcription_source, allow_transcode) as media_path:
            transcription_data = transcribe_audio(client, media_path, args.model)
    rows = build_rows(transcription_data, args.speaker, args.frame_rate)
    if len(rows) == 1:
        raise RuntimeError("No transcript segments were returned by Groq.")
    write_csv(output_path, rows)
    raw_json = write_raw_json(output_path, transcription_data)
    word_csv = write_word_level_csv(output_path, transcription_data, args.frame_rate)
    if word_csv is None:
        raise RuntimeError(
            "Groq transcription did not include word timestamps. "
            "A *_words.csv artifact is now required for no-word-gap trimming."
        )
    if reference_mp3:
        print(f"Audio reference exported to {reference_mp3}")
    print(f"Wrote {len(rows) - 1} rows to {output_path}")
    print(f"Wrote raw Groq JSON to {raw_json}")
    print(f"Wrote word-level timings to {word_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
