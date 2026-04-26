#!/usr/bin/env python3
"""Convert full video files to MP3 audio tracks kept alongside Groq outputs."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

DEFAULT_INPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush"
)
DEFAULT_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/groq/output/audio"
)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the complete audio track from a video into an MP3 file."
    )
    parser.add_argument(
        "video",
        type=Path,
        nargs="?",
        help=f"Video file to convert (default: newest video in {DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Destination MP3 path. Defaults to "
            f"{DEFAULT_OUTPUT_DIR}/<video_stem>.mp3."
        ),
    )
    return parser.parse_args()


def ensure_ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise RuntimeError("ffmpeg is required to extract audio.")
    return executable


def newest_video(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist.")
    candidates = [
        candidate
        for candidate in path.glob("*")
        if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not candidates:
        raise FileNotFoundError(f"No video files found in {path}")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def resolve_output_path(video: Path, desired: Path | None) -> Path:
    if desired:
        if desired.suffix.lower() == ".mp3":
            desired.parent.mkdir(parents=True, exist_ok=True)
            return desired
        desired.mkdir(parents=True, exist_ok=True)
        return desired / f"{video.stem}.mp3"
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_OUTPUT_DIR / f"{video.stem}.mp3"


def convert_video_to_mp3(video: Path, output: Path) -> None:
    ensure_ffmpeg()
    base_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(output),
    ]
    commands = [
        ["ffmpeg", "-y", "-c:a", "aac_at", "-i", str(video), "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(output)],
        base_cmd,
    ]
    errors: list[str] = []
    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="ignore") if exc.stderr else ""
            errors.append(stderr)
    raise RuntimeError("ffmpeg failed while extracting audio:\n" + "\n---\n".join(errors))


def main() -> int:
    args = parse_args()
    video = args.video
    if not video:
        video = newest_video(DEFAULT_INPUT_DIR)
    if not video.exists():
        raise FileNotFoundError(f"{video} does not exist.")
    output_path = resolve_output_path(video, args.output)
    convert_video_to_mp3(video, output_path)
    print(f"Extracted audio to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
