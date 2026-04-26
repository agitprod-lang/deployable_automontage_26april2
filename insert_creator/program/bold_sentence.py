#!/usr/bin/env python3
"""Render glowing transparent clips for bold timeline entries."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1920, 1080
FPS = 30
FONT_SIZE = 100
PADDING_X = 160
PADDING_BOTTOM = 80
LINE_SPACING = 1.6

FRAMES_FALL = 2
FRAMES_GLOW_IN = 2
FRAMES_HOLD = 1
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 11

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)
LIT_COLOR = (230, 230, 230)
GLOW_COLOR = (0, 179, 255)
ZOOM_START = 2.5


@dataclass(frozen=True)
class BoldEntry:
    entry_id: int
    seq_num: int
    value: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render glowing bold mention clips.")
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to *_comparison.csv. Defaults to the latest comparer output.",
    )
    parser.add_argument(
        "--timing-manifest",
        type=Path,
        help="Path to *_timed_insert_timing_manifest.csv. Defaults to the one next to the input CSV.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override insert_creator output directory.",
    )
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    candidates = [path for path in directory.rglob("*comparison.csv") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No *comparison.csv found in {directory}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def resolve_timing_manifest(input_csv: Path, explicit_manifest: Optional[Path]) -> Path:
    if explicit_manifest is not None:
        manifest_path = explicit_manifest.expanduser().resolve()
        if not manifest_path.exists():
            raise FileNotFoundError(f"Timing manifest not found: {manifest_path}")
        return manifest_path

    sibling = input_csv.with_name(f"{input_csv.stem}_timed_insert_timing_manifest.csv")
    if sibling.exists():
        return sibling

    candidates = [path for path in input_csv.parent.glob("*_timed_insert_timing_manifest.csv") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No timed insert timing manifest found near {input_csv}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def clean_text(value: str) -> str:
    text = re.sub(r"</?b[^>]*>", "", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = " ".join(text.replace("\n", " ").split())
    return text.strip()


def load_bold_entries(path: Path) -> List[BoldEntry]:
    entries: List[BoldEntry] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            if (row.get("Asset Category") or "").strip() != "social_ranking_punctuation":
                continue
            if (row.get("Illustration Type") or "").strip() != "bold":
                continue
            value = clean_text(row.get("Reference Word") or row.get("Transcript Word") or "")
            if not value:
                continue
            try:
                entry_id = int((row.get("Entry ID") or "").strip())
            except ValueError:
                entry_id = len(entries) + 1
            entries.append(BoldEntry(entry_id=entry_id, seq_num=len(entries) + 1, value=value))
    return entries


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def layout_words(words, font):
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    max_w = W - 2 * PADDING_X
    space_w = draw.textlength(" ", font=font)

    positions, line_x, line_y, line_h = [], 0, 0, 0
    for word in words:
        bbox = draw.textbbox((0, 0), word, font=font)
        word_w, word_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        line_h = max(line_h, word_h)
        if line_x > 0 and line_x + word_w > max_w:
            line_x = 0
            line_y += int(line_h * LINE_SPACING)
            line_h = word_h
        positions.append((word, line_x, line_y, word_w, word_h))
        line_x += word_w + space_w

    block_w = max(x + word_w for _, x, _, word_w, _ in positions)
    block_h = line_y + int(line_h * LINE_SPACING)
    return positions, block_w, block_h


_glow_cache = {}


def rgb_to_rgba(rgb_img):
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(word, px, py, font):
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), word, font=font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_fall_layer_rgba(word, px, py, font, phase):
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)
    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), word, font=font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def apply_zoom(img, zoom, cx, cy):
    inv = 1.0 / zoom
    a, b, c = inv, 0.0, cx * (1.0 - inv)
    d, e, f = 0.0, inv, cy * (1.0 - inv)
    return img.transform(
        (W, H),
        Image.AFFINE,
        (a, b, c, d, e, f),
        resample=Image.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def render_frame(positions, ox, oy, frame_num, n_words, font, first_cx, first_cy):
    total_word_frames = n_words * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_words, 0
        t_ease = 1.0
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
        t_ease = 1 - (1 - t_global) ** 3

    zoom = ZOOM_START - (ZOOM_START - 1.0) * t_ease
    cx = first_cx + (W / 2 - first_cx) * t_ease
    cy = first_cy + (H / 2 - first_cy) * t_ease

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for index, (word, word_x, word_y, _, _) in enumerate(positions):
        px, py = ox + word_x, oy + word_y
        if all_lit or index < cur:
            img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
            ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))
        elif index == cur:
            if phase < FRAMES_FALL:
                img = Image.alpha_composite(img, make_fall_layer_rgba(word, px, py, font, phase))
            elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                t = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                glow = get_glow_rgba(word, px, py, font)
                if t < 1.0:
                    r, g, b, a = glow.split()
                    a = ImageEnhance.Brightness(a).enhance(t)
                    glow = Image.merge("RGBA", (r, g, b, a))
                img = Image.alpha_composite(img, glow)
                ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))
            else:
                img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
                ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))
    return apply_zoom(img, zoom, cx, cy)


def encode_clip(text: str, output_path: Path) -> None:
    words = text.split()
    if not words:
        raise ValueError("Cannot render empty bold text.")

    font = load_font(FONT_SIZE)
    positions, block_w, block_h = layout_words(words, font)
    ox = (W - block_w) // 2
    oy = H - PADDING_BOTTOM - block_h

    _, wx0, wy0, ww0, wh0 = positions[0]
    first_cx = float(ox + wx0 + ww0 / 2)
    first_cy = float(oy + wy0 + wh0 / 2)

    total_frames = len(words) * FRAMES_PER_WORD + FRAMES_FINAL
    frames_dir = Path(tempfile.mkdtemp(prefix="glow_zoom_alpha_"))
    try:
        for frame_num in range(total_frames):
            frame = render_frame(positions, ox, oy, frame_num, len(words), font, first_cx, first_cy)
            frame.save(frames_dir / f"frame_{frame_num:05d}.png")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(FPS),
                "-i",
                str(frames_dir / "frame_%05d.png"),
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4444",
                "-pix_fmt",
                "yuva444p10le",
                "-alpha_bits",
                "16",
                str(output_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        shutil.rmtree(frames_dir)


def render_entries(entries: List[BoldEntry], video_dir: Path) -> int:
    rendered = 0
    for entry in entries:
        output_path = video_dir / f"bold_{entry.seq_num:03d}_BLD.mov"
        print(f"  [BLD] #{entry.seq_num:03d} — {entry.value}")
        encode_clip(entry.value, output_path)
        print(f"       → {output_path}")
        rendered += 1
    return rendered


def main() -> None:
    args = parse_args()
    input_csv = args.input_csv.expanduser().resolve() if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    timing_manifest = resolve_timing_manifest(input_csv, args.timing_manifest)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else OUTPUT_DIR

    entries = load_bold_entries(timing_manifest)
    if not entries:
        print(f"No bold entries found in {timing_manifest}.")
        return

    video_dir = output_dir / f"{input_csv.stem}_mentions_media" / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    print(f"Input CSV       : {input_csv}")
    print(f"Timing manifest : {timing_manifest}")
    print(f"Video output    : {video_dir}")
    print(f"Bold entries    : {len(entries)}")

    rendered = render_entries(entries, video_dir)
    print(f"\nRendered {rendered} bold clip(s).")


if __name__ == "__main__":
    main()
