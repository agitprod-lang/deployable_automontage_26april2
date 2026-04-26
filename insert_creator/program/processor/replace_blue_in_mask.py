#!/usr/bin/env python3
"""Replace the keyed-blue area of a mask clip with the noun image."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


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
        description="Fill the blue regions of a mask clip with the noun image while keeping transparency elsewhere."
    )
    parser.add_argument(
        "--mask-video",
        type=Path,
        help="Transparent video from create_mask.py (defaults to <image_dir>/<image_stem>_mask.mov).",
    )
    parser.add_argument(
        "--image",
        type=Path,
        help="PNG with transparent background + noisy outline (defaults to latest under output/*_nouns_transitions/paper_textured_images).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination .mov (defaults to alongside the mask as <stem>_filled.mov).",
    )
    parser.add_argument(
        "--photo-key-color",
        default="0x00af3e",
        help="Hex color to key from the photo (default: 0x00af3e for green).",
    )
    parser.add_argument(
        "--photo-key-similarity",
        type=float,
        default=0.24,
        help="Similarity threshold for the photo colorkey filter (default: 0.24).",
    )
    parser.add_argument(
        "--photo-key-blend",
        type=float,
        default=0.08,
        help="Blend value for the photo colorkey filter (default: 0.08).",
    )
    parser.add_argument(
        "--skip-photo-key",
        action="store_true",
        help="Skip removing a background color from the photo (use when the image already has transparency).",
    )
    return parser.parse_args()


def render_replacement(
    mask_clip: Path,
    image: Path,
    output: Path,
    *,
    photo_key_color: str | None,
    photo_key_similarity: float,
    photo_key_blend: float,
) -> tuple[bool, str]:
    photo_chain = "[1:v]format=rgba"
    if photo_key_color:
        photo_chain += (
            f",colorkey={photo_key_color}:{photo_key_similarity}:{photo_key_blend},"
            "format=rgba"
        )
    photo_chain += (
        f",scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease"
        f",pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:color=#00000000@0"
        ",setsar=1,format=rgba[photo_full];"
    )
    filter_graph = (
        "[0:v]format=rgba,"
        f"colorkey={GREEN_HEX}:{GREEN_SIMILARITY}:{GREEN_BLEND},"
        "format=rgba[green_keyed];"
        "[green_keyed]format=rgba,"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:({VIDEO_WIDTH}-iw)/2:({VIDEO_HEIGHT}-ih)/2:color=#00000000,"
        "setsar=1,format=rgba[mask_base];"
        "[mask_base]format=rgba,"
        f"colorkey={BLUE_HEX}:{BLUE_SIMILARITY}:{BLUE_BLEND},"
        "format=rgba[video_without_blue];"
        "[video_without_blue]alphaextract,format=gray[alpha_keep];"
        "[alpha_keep]geq=lum='255-lum(X,Y)'[blue_mask];"
        f"{photo_chain}"
        "[blue_mask]format=rgba[mask_rgba];"
        "[photo_full][mask_rgba]alphamerge[photo_blue_only];"
        "[video_without_blue][photo_blue_only]overlay=format=auto[combined];"
        "[combined]scale="
        f"w=round(iw*{PLACEMENT_SCALE}):h=round(ih*{PLACEMENT_SCALE})[scaled];"
        f"[scaled]pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}:{ANCHOR_X}:{ANCHOR_Y}:color=0x00000000,"
        "format=rgba[out]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mask_clip),
        "-loop",
        "1",
        "-i",
        str(image),
        "-filter_complex",
        filter_graph,
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
    result = subprocess.run(cmd)
    return result.returncode == 0, ""


def main() -> None:
    args = parse_args()
    try:
        image_path = args.image if args.image else find_latest_textured_image()
    except FileNotFoundError as exc:
        raise SystemExit(f"❌ {exc}") from exc
    if not image_path.exists():
        raise SystemExit(f"❌ Image not found: {image_path}")
    mask_path = args.mask_video if args.mask_video else image_path.parent / f"{image_path.stem}_mask.mov"
    if not mask_path.exists():
        raise SystemExit(f"❌ Mask video not found: {mask_path} (run create_mask.py first).")
    output_path = args.output if args.output else mask_path.with_name(f"{mask_path.stem}_filled.mov")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    key_color = None if args.skip_photo_key else args.photo_key_color
    success, stderr = render_replacement(
        mask_path,
        image_path,
        output_path,
        photo_key_color=key_color,
        photo_key_similarity=args.photo_key_similarity,
        photo_key_blend=args.photo_key_blend,
    )
    if not success:
        raise SystemExit("ffmpeg failed. See above for details.")
    print(f"✅ Saved {output_path}")


if __name__ == "__main__":
    main()
