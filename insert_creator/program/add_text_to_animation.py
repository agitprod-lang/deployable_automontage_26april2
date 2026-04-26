#!/usr/bin/env python3
"""Add glowing bottom-left animated text as a separate layer over generated clips."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from collections.abc import MutableMapping
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROJECT_DIR.parent
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1920, 1080
FPS = 30
FONT_SIZE = 100
PADDING_X = 160
PADDING_BOTTOM = 120
LINE_SPACING = 1.6
MONEY_TEXT_TOP_Y = 370

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)
LIT_COLOR = (0, 0, 0)
GLOW_COLOR = (0, 100, 255)
ZOOM_START = 1.0
ZOOM_END = 1.12

FONT_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"
)

TAG_FONT_OVERRIDES: Dict[str, Path] = {
    "DUR": Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/typefont/Time Normal.ttf"),
}

_glow_cache: Dict[tuple[str, int, int], Image.Image] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply glowing animated text overlays to generated money and mention clips."
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to *_comparison*.csv (defaults to the most recent file under Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="Directory containing the generated manifest folders (default: insert_creator/output).",
    )
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [path for path in directory.rglob("*comparison.csv") if path.is_file()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No *_comparison.csv files found in {directory}")
    return candidates[0]


def load_font(size: int, font_path: Path | None = None) -> ImageFont.FreeTypeFont:
    path = font_path if font_path is not None else FONT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Required font not found: {path}")
    return ImageFont.truetype(str(path), size)


def layout_words(words: Sequence[str], font: ImageFont.FreeTypeFont) -> tuple[list[tuple[str, int, int, int, int]], int, int]:
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    max_w = W - 2 * PADDING_X
    space_w = int(draw.textlength(" ", font=font))

    positions: list[tuple[str, int, int, int, int]] = []
    line_x = 0
    line_y = 0
    line_h = 0
    for word in words:
        bb = draw.textbbox((0, 0), word, font=font)
        ww, wh = bb[2] - bb[0], bb[3] - bb[1]
        line_h = max(line_h, wh)
        if line_x > 0 and line_x + ww > max_w:
            line_x = 0
            line_y += int(line_h * LINE_SPACING)
            line_h = wh
        positions.append((word, line_x, line_y, ww, wh))
        line_x += ww + space_w

    block_w = max((x + ww for _, x, _, ww, _ in positions), default=0)
    block_h = line_y + int(line_h * LINE_SPACING)
    return positions, block_w, block_h


def rgb_to_rgba(rgb_img: Image.Image) -> Image.Image:
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(word: str, px: int, py: int, font: ImageFont.FreeTypeFont) -> Image.Image:
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), word, font=font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_fall_layer_rgba(word: str, px: int, py: int, font: ImageFont.FreeTypeFont, phase: int) -> Image.Image:
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)

    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), word, font=font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def apply_zoom(img: Image.Image, zoom: float, cx: float, cy: float) -> Image.Image:
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


def render_frame(
    positions: Sequence[tuple[str, int, int, int, int]],
    ox: int,
    oy: int,
    frame_num: int,
    n_words: int,
    font: ImageFont.FreeTypeFont,
    zoom_cx: float,
    zoom_cy: float,
) -> Image.Image:
    total_word_frames = n_words * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_words, 0
        zoom = ZOOM_END
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / max(total_word_frames, 1)
        t_ease = t_global * t_global * (3 - 2 * t_global)
        zoom = ZOOM_START + (ZOOM_END - ZOOM_START) * t_ease

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    for i, (word, wx, wy, _ww, _wh) in enumerate(positions):
        px, py = ox + wx, oy + wy
        if all_lit or i < cur:
            img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
            ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))
        elif i == cur:
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

    return apply_zoom(img, zoom, zoom_cx, zoom_cy)


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        try:
            value = float(proc.stdout.strip())
            if value > 0:
                return value
        except ValueError:
            pass
    return 4.0


def render_text_overlay(text: str, destination: Path, duration: float, font_path: Path | None = None, center_vertically: bool = False, fixed_y: int | None = None) -> None:
    words = text.split()
    if not words:
        raise ValueError("Cannot render an empty text overlay.")

    font = load_font(FONT_SIZE, font_path)
    positions, block_w, block_h = layout_words(words, font)
    ox = PADDING_X
    if fixed_y is not None:
        oy = fixed_y
    else:
        oy = (H - block_h) // 2 if center_vertically else H - block_h - PADDING_BOTTOM
    zoom_cx = ox + block_w / 2
    zoom_cy = oy + block_h / 2

    minimum_frames = len(words) * FRAMES_PER_WORD + FRAMES_FINAL
    total_frames = max(int(round(duration * FPS)), minimum_frames)

    frames_dir = Path(tempfile.mkdtemp(prefix="insert_text_frames_"))
    try:
        for frame_num in range(total_frames):
            render_frame(positions, ox, oy, frame_num, len(words), font, zoom_cx, zoom_cy).save(
                frames_dir / f"frame_{frame_num:05d}.png"
            )
        cmd = [
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
            str(destination),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


def composite_text_over_video(base_video: Path, text: str, output_path: Path, font_path: Path | None = None, center_vertically: bool = False, fixed_y: int | None = None) -> None:
    duration = probe_duration(base_video)
    temp_dir = Path(tempfile.mkdtemp(prefix="insert_text_overlay_"))
    overlay_path = temp_dir / "overlay.mov"
    rendered_path = temp_dir / "rendered.mov"
    try:
        render_text_overlay(text, overlay_path, duration, font_path, center_vertically, fixed_y)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(base_video),
            "-i",
            str(overlay_path),
            "-filter_complex",
            "[0:v]format=rgba[base];[1:v]format=rgba[text];[base][text]overlay=0:0:format=auto[out]",
            "-map",
            "[out]",
            "-map",
            "0:a?",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-c:a",
            "copy",
            "-shortest",
            str(rendered_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(rendered_path), str(output_path))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def load_manifest(path: Path) -> MutableMapping[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, data: Mapping[str, object]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def iter_records(manifest_data: MutableMapping[str, object], field: str) -> Iterable[MutableMapping[str, object]]:
    raw = manifest_data.get(field, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, MutableMapping)]


def process_record(record: MutableMapping[str, object], label: str, is_money: bool = False) -> bool:
    if not record.get("success", True):
        return False
    if not record.get("needs_text_layer"):
        return False

    text = str(record.get("overlay_text") or record.get("display_text") or record.get("value") or "").strip()
    if not text:
        return False

    base_video_path = record.get("base_video_path") or record.get("video_path")
    output_video_path = record.get("video_path") or record.get("base_video_path")
    if not isinstance(base_video_path, str) or not isinstance(output_video_path, str):
        print(f"  ⚠️  {label}: missing video path metadata.")
        record["success"] = False
        return False

    base_video = Path(base_video_path)
    output_video = Path(output_video_path)
    if not base_video.exists():
        print(f"  ⚠️  {label}: base video not found at {base_video}.")
        record["success"] = False
        return False

    tag = str(record.get("tag") or "").upper()
    font_override = TAG_FONT_OVERRIDES.get(tag)
    center_vertically = tag not in ("", "TMP")
    fixed_y: int | None = MONEY_TEXT_TOP_Y if is_money else None
    try:
        composite_text_over_video(base_video, text, output_video, font_override, center_vertically, fixed_y)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as exc:
        print(f"  ⚠️  {label}: could not add text layer ({exc}).")
        record["success"] = False
        return False

    record["video_path"] = str(output_video)
    record["text_layer_applied"] = True
    return True


def process_manifest(manifest_path: Path, field: str, label_prefix: str, is_money: bool = False) -> int:
    if not manifest_path.exists():
        return 0
    data = load_manifest(manifest_path)
    processed = 0
    for idx, record in enumerate(iter_records(data, field), start=1):
        label = f"{label_prefix} #{idx:03d}"
        if process_record(record, label, is_money=is_money):
            processed += 1
            print(f"  ✨ {label}")
    save_manifest(manifest_path, data)
    return processed


def main() -> None:
    args = parse_args()
    csv_path = args.input_csv.expanduser().resolve() if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    output_dir = args.output_dir.expanduser().resolve()
    stem = csv_path.stem

    money_manifest = output_dir / f"{stem}_money_media" / f"{stem}_money_manifest.json"
    mentions_manifest = output_dir / f"{stem}_mentions_media" / f"{stem}_mentions_manifest.json"

    print(f"Applying animated text layers for {stem}...")
    money_count = process_manifest(money_manifest, "videos", "money", is_money=True)
    mentions_count = process_manifest(mentions_manifest, "clips", "mention")
    print(f"Text layers applied: money={money_count}, mentions={mentions_count}")


if __name__ == "__main__":
    main()
