#!/usr/bin/env python3
"""Build a masking clip that reveals the paper transition only inside the noun silhouette."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Optional


DEFAULT_TRANSITION = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "Unfold Paper Transition green screen.mp4"
)
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
BLUE_HEX = "0x0044b9"
BLUE_SIMILARITY = 0.13
BLUE_BLEND = 0.07
GREEN_HEX = "0x00af3e"
GREEN_SIMILARITY = 0.24
GREEN_BLEND = 0.08
PLACEMENT_SCALE = 0.42
ANCHOR_X_RATIO = 0.25
ANCHOR_Y_RATIO = 0.78
PLACEMENT_WIDTH = int(round(VIDEO_WIDTH * PLACEMENT_SCALE))
PLACEMENT_HEIGHT = int(round(VIDEO_HEIGHT * PLACEMENT_SCALE))
ANCHOR_X = int(
    max(min(round(VIDEO_WIDTH * ANCHOR_X_RATIO - PLACEMENT_WIDTH / 2), VIDEO_WIDTH - PLACEMENT_WIDTH), 0)
)
ANCHOR_Y = int(
    max(min(round(VIDEO_HEIGHT * ANCHOR_Y_RATIO - PLACEMENT_HEIGHT / 2), VIDEO_HEIGHT - PLACEMENT_HEIGHT), 0)
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output"


def find_latest_textured_image(base_dir: Path = OUTPUT_DIR) -> Path:
    candidates = sorted(
        base_dir.glob("*_nouns_transitions/paper_textured_images/*.png"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError("No textured noun images found under output/*_nouns_transitions/paper_textured_images.")
    return candidates[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a transparent video where the transition is visible only within the noun mask."
    )
    parser.add_argument(
        "--image",
        type=Path,
        help="PNG with transparent background + noisy outline (defaults to latest under output/*_nouns_transitions/paper_textured_images).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination .mov file (defaults to alongside the image as <stem>_mask.mov).",
    )
    parser.add_argument(
        "--transition",
        type=Path,
        default=DEFAULT_TRANSITION,
        help="Paper transition clip (defaults to the Unfold Paper Transition asset).",
    )
    return parser.parse_args()


def render_masked_video(transition: Path, image: Path, output: Path) -> tuple[bool, str]:
    duration_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nk=1:nw=1",
        str(transition),
    ]
    duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
    try:
        transition_duration = float(duration_result.stdout.strip())
    except (ValueError, TypeError):
        transition_duration = 6.0
    else:
        transition_duration = max(min(transition_duration, 10.0), 0.5)
    filter_graph = (
        "[0:v]format=rgba,"
        f"colorkey={GREEN_HEX}:{GREEN_SIMILARITY}:{GREEN_BLEND},"
        "format=rgba[transition_keyed];"
        "[transition_keyed]scale="
        f"{VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:"
        "color=#00000000,setsar=1,format=rgba[transition_scaled];"
        "[1:v]scale="
        f"{VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:"
        "color=#00000000@0,setsar=1,format=rgba[photo];"
        "[photo]alphaextract,format=gray[photo_mask];"
        "[transition_scaled][photo_mask]alphamerge[masked];"
        f"[masked]scale=w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE})[scaled];"
        f"[scaled]pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{ANCHOR_X}:{ANCHOR_Y}:color=0x00000000,"
        "format=rgba[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(transition),
        "-loop",
        "1",
        "-i",
        str(image),
        "-filter_complex",
        filter_graph,
        "-t",
        f"{transition_duration}",
        "-map",
        "[out]",
        "-an",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-shortest",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def main() -> None:
    args = parse_args()
    try:
        image_path = args.image if args.image else find_latest_textured_image()
    except FileNotFoundError as exc:
        raise SystemExit(f"❌ {exc}") from exc
    if not image_path.exists():
        raise SystemExit(f"❌ Image not found: {image_path}")
    output_path = args.output if args.output else image_path.parent / f"{image_path.stem}_mask.mov"
    transition_clip = args.transition
    if not transition_clip.exists():
        raise SystemExit(f"❌ Transition clip not found: {transition_clip}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    success, stderr = render_masked_video(transition_clip, image_path, output_path)
    if not success:
        raise SystemExit(f"ffmpeg failed: {stderr.splitlines()[-1] if stderr else 'Unknown error'}")
    print(f"✅ Saved {output_path}")


if __name__ == "__main__":
    main()
