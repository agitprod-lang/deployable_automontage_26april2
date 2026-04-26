#!/usr/bin/env python3
"""Create glowing title videos from the comparser CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
FPS = 24
FONT_SIZE = 100
PADDING_X = 160
LINE_SPACING = 1.6

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90
FRAMES_FADE_OUT = 18

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)

LIT_COLOR = (0, 0, 0)
GLOW_COLOR = (255, 0, 0)

FONT_PATHS = [
    PROJECT_ROOT / "asset" / "fonts" / "Montserrat-Bold.ttf",
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]
EMOJI_FONT_PATH = Path("/System/Library/Fonts/Apple Color Emoji.ttc")
EMOJI_FONT_SIZES = (40, 48, 52, 64, 96)
EMOJI_POP_START = 0.35
EMOJI_POP_OVERSHOOT = 1.08

TITLE_BACKGROUND_DIR = PROJECT_ROOT / "asset" / "title_background"
INTRO_PATH = TITLE_BACKGROUND_DIR / "rgb_invert" / "intro_invert.mov"
MEDIUM_PATH = TITLE_BACKGROUND_DIR / "rgb_invert" / "medium_invert.mp4"
OUTRO_PATH = TITLE_BACKGROUND_DIR / "rgb_invert" / "outro_invert.mov"
AUDIO_PATH = TITLE_BACKGROUND_DIR / "ripped_trimed.m4a"


@dataclass
class TitleEntry:
    value: str
    row_index: int
    transcript_number: str | None
    start_timecode: str | None
    end_timecode: str | None
    start_seconds: float | None
    end_seconds: float | None
    text: str


@dataclass(frozen=True)
class FontBundle:
    text_font: ImageFont.FreeTypeFont
    emoji_font: ImageFont.FreeTypeFont
    emoji_scale: float


@dataclass(frozen=True)
class TokenSpec:
    text: str
    kind: str
    font: ImageFont.FreeTypeFont
    width: int
    height: int
    render_scale: float = 1.0


@dataclass(frozen=True)
class TokenPlacement:
    token: TokenSpec
    x: int
    y: int
    width: int
    height: int
    line_height: int


_glow_cache: dict[tuple[str, int, int], Image.Image] = {}
_emoji_cache: dict[tuple[str, int, float], Image.Image] = {}
_MEASURE_DRAW = ImageDraw.Draw(Image.new("RGBA", (1, 1), (0, 0, 0, 0)))
_EMOJI_HELPER_CHARS = {
    "\u200d",
    "\ufe0e",
    "\ufe0f",
    "\u20e3",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate videos from the 'Titles' column.")
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to *_comparison*.csv (defaults to newest file in Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override output directory (defaults to insert_creator/output).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=25.0,
        help="Frame rate for HH:MM:SS:FF parsing (default: 25).",
    )
    parser.add_argument(
        "--timing-manifest",
        type=Path,
        help="Timed insert manifest CSV used as a fallback source for title rows.",
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
        raise KeyError(f"Required column '{column_name}' missing.")
    return header_map[key]


def split_titles(value: str | None) -> List[str]:
    if not value:
        return []
    parts = value.split("|")
    cleaned: List[str] = []
    for fragment in parts:
        text = fragment.strip().strip('"').strip()
        if text:
            cleaned.append(text)
    return cleaned


def parse_timecode(value: str | None, frame_rate: float) -> float | None:
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


def parse_manifest_time(value: str | None) -> float | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        hours, minutes, seconds = raw.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def collect_entries(rows: Sequence[Sequence[str]], header_map: Mapping[str, int], frame_rate: float) -> List[TitleEntry]:
    value_idx = require_column(header_map, "Titles")
    start_idx = require_column(header_map, "Start Time")
    end_idx = require_column(header_map, "End Time")
    text_idx = require_column(header_map, "Text")
    transcript_idx = require_column(header_map, "Transcript #")
    entries: List[TitleEntry] = []
    for row_number, row in enumerate(rows, start=1):
        if value_idx >= len(row):
            continue
        values = split_titles(row[value_idx])
        if not values:
            continue
        start_time = row[start_idx] if start_idx < len(row) else ""
        end_time = row[end_idx] if end_idx < len(row) else ""
        start_seconds = parse_timecode(start_time, frame_rate)
        end_seconds = parse_timecode(end_time, frame_rate)
        transcript_number = row[transcript_idx] if transcript_idx < len(row) else ""
        text = row[text_idx] if text_idx < len(row) else ""
        for value in values:
            entries.append(
                TitleEntry(
                    value=value,
                    row_index=row_number,
                    transcript_number=transcript_number.strip() or None,
                    start_timecode=start_time.strip() or None,
                    end_timecode=end_time.strip() or None,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=text,
                )
            )
    return entries


def collect_entries_from_timing_manifest(path: Path) -> List[TitleEntry]:
    entries: List[TitleEntry] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            if (row.get("Asset Category") or "").strip() != "titles":
                continue
            value = str(row.get("Reference Word") or row.get("Illustration Value") or "").strip()
            if not value:
                continue
            transcript_number = str(row.get("Transcript #") or row.get("Row ID") or "").strip() or None
            row_index: int
            try:
                row_index = int(str(row.get("Row ID") or row.get("Transcript #") or "0") or 0)
            except ValueError:
                row_index = len(entries) + 1
            entries.append(
                TitleEntry(
                    value=value,
                    row_index=row_index,
                    transcript_number=transcript_number,
                    start_timecode=(row.get("Start Time") or "").strip() or None,
                    end_timecode=(row.get("End Time") or "").strip() or None,
                    start_seconds=parse_manifest_time(row.get("Start Time")),
                    end_seconds=parse_manifest_time(row.get("End Time")),
                    text="",
                )
            )
    return entries


def ffprobe_json(path: Path, entries: str, scope: str) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            f"{scope}={entries}",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def probe_video(path: Path) -> dict[str, float | int | str]:
    if not path.exists():
        raise FileNotFoundError(f"Required background asset not found: {path}")
    stream_data = ffprobe_json(path, "width,height,r_frame_rate", "stream")
    format_data = ffprobe_json(path, "duration", "format")
    streams = stream_data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in asset: {path}")
    stream = streams[0]
    num, den = str(stream["r_frame_rate"]).split("/")
    fps = float(num) / float(den)
    return {
        "path": str(path),
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps,
        "duration": float(format_data["format"]["duration"]),
    }


def probe_audio(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"Required audio asset not found: {path}")
    data = ffprobe_json(path, "codec_name,sample_rate,channels", "stream")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No audio stream found in asset: {path}")
    return streams[0]


def validate_assets() -> dict[str, dict[str, float | int | str]]:
    assets = {
        "intro": probe_video(INTRO_PATH),
        "medium": probe_video(MEDIUM_PATH),
        "outro": probe_video(OUTRO_PATH),
    }
    probe_audio(AUDIO_PATH)
    return assets


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise FileNotFoundError("No usable title font found.")


def load_fonts(size: int) -> FontBundle:
    text_font = load_font(size)
    if not EMOJI_FONT_PATH.exists():
        raise FileNotFoundError(f"Emoji font not found: {EMOJI_FONT_PATH}")

    emoji_size = min(EMOJI_FONT_SIZES, key=lambda candidate: abs(candidate - size))
    emoji_font = ImageFont.truetype(str(EMOJI_FONT_PATH), emoji_size)
    return FontBundle(
        text_font=text_font,
        emoji_font=emoji_font,
        emoji_scale=size / emoji_size,
    )


def is_emoji_char(char: str) -> bool:
    if not char:
        return False
    if char in _EMOJI_HELPER_CHARS:
        return True
    codepoint = ord(char)
    return (
        0x1F1E6 <= codepoint <= 0x1F1FF
        or 0x1F300 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0x1F900 <= codepoint <= 0x1F9FF
        or 0x1FA70 <= codepoint <= 0x1FAFF
        or 0x1F600 <= codepoint <= 0x1F64F
        or 0x1F680 <= codepoint <= 0x1F6FF
        or 0x1F700 <= codepoint <= 0x1F77F
        or 0x1F780 <= codepoint <= 0x1F7FF
        or 0x1F800 <= codepoint <= 0x1F8FF
        or 0x1F3FB <= codepoint <= 0x1F3FF
    )


def split_word_runs(word: str) -> list[tuple[str, str]]:
    if not word:
        return []
    runs: list[tuple[str, str]] = []
    current_kind = "emoji" if is_emoji_char(word[0]) else "text"
    current_chars = [word[0]]

    for char in word[1:]:
        kind = "emoji" if is_emoji_char(char) else "text"
        if kind == current_kind:
            current_chars.append(char)
            continue
        runs.append(("".join(current_chars), current_kind))
        current_kind = kind
        current_chars = [char]

    runs.append(("".join(current_chars), current_kind))
    return runs


def measure_text_token(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    bbox = _MEASURE_DRAW.textbbox((0, 0), text, font=font)
    return max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])


def measure_emoji_token(text: str, font: ImageFont.FreeTypeFont, scale: float) -> tuple[int, int]:
    bbox = _MEASURE_DRAW.textbbox((0, 0), text, font=font, embedded_color=True)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def build_token_specs(text: str, fonts: FontBundle) -> list[tuple[TokenSpec, bool]]:
    token_specs: list[tuple[TokenSpec, bool]] = []
    for word_index, raw_word in enumerate(re.findall(r"\S+", text)):
        for run_index, (run_text, kind) in enumerate(split_word_runs(raw_word)):
            if kind == "emoji":
                width, height = measure_emoji_token(run_text, fonts.emoji_font, fonts.emoji_scale)
                token = TokenSpec(
                    text=run_text,
                    kind="emoji",
                    font=fonts.emoji_font,
                    width=width,
                    height=height,
                    render_scale=fonts.emoji_scale,
                )
            else:
                width, height = measure_text_token(run_text, fonts.text_font)
                token = TokenSpec(
                    text=run_text,
                    kind="text",
                    font=fonts.text_font,
                    width=width,
                    height=height,
                )
            token_specs.append((token, word_index > 0 and run_index == 0))
    return token_specs


def layout_tokens(token_specs: Sequence[tuple[TokenSpec, bool]], text_font: ImageFont.FreeTypeFont) -> tuple[list[TokenPlacement], int, int]:
    max_w = VIDEO_WIDTH - 2 * PADDING_X
    space_w = int(round(_MEASURE_DRAW.textlength(" ", font=text_font)))
    lines: list[tuple[list[tuple[TokenSpec, int]], int]] = []
    line_tokens: list[tuple[TokenSpec, int]] = []
    line_x = 0
    line_h = 0

    for token, needs_space_before in token_specs:
        prefix = space_w if line_tokens and needs_space_before else 0
        token_x = line_x + prefix
        if line_tokens and token_x + token.width > max_w:
            lines.append((line_tokens, line_h))
            line_tokens = []
            line_x = 0
            line_h = 0
            prefix = 0
            token_x = 0
        line_tokens.append((token, token_x))
        line_x = token_x + token.width
        line_h = max(line_h, token.height)

    if line_tokens:
        lines.append((line_tokens, line_h))

    placements: list[TokenPlacement] = []
    line_y = 0
    last_line_height = 0
    for line_items, current_line_height in lines:
        last_line_height = current_line_height
        for token, token_x in line_items:
            placements.append(
                TokenPlacement(
                    token=token,
                    x=token_x,
                    y=line_y + max(0, (current_line_height - token.height) // 2),
                    width=token.width,
                    height=token.height,
                    line_height=current_line_height,
                )
            )
        line_y += int(current_line_height * LINE_SPACING)

    block_w = max((placement.x + placement.width for placement in placements), default=0)
    block_h = max(line_y - int(last_line_height * (LINE_SPACING - 1)), last_line_height)
    if not placements:
        block_h = 0
    return placements, block_w, block_h


def rgb_to_rgba(rgb_img: Image.Image) -> Image.Image:
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(token: TokenSpec, px: int, py: int) -> Image.Image:
    key = (token.text, px, py)
    if key not in _glow_cache:
        base = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), token.text, font=token.font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_text_fall_layer_rgba(token: TokenSpec, px: int, py: int, phase: int) -> Image.Image:
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)

    layer = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), token.text, font=token.font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def scale_alpha(img: Image.Image, alpha_scale: float) -> Image.Image:
    if alpha_scale >= 0.999:
        return img
    if alpha_scale <= 0.0:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b, a = img.split()
    a = ImageEnhance.Brightness(a).enhance(alpha_scale)
    return Image.merge("RGBA", (r, g, b, a))


def get_emoji_rgba(token: TokenSpec) -> Image.Image:
    key = (token.text, token.font.size, token.render_scale)
    if key in _emoji_cache:
        return _emoji_cache[key]

    bbox = _MEASURE_DRAW.textbbox((0, 0), token.text, font=token.font, embedded_color=True)
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])
    base = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    draw.text(
        (-bbox[0], -bbox[1]),
        token.text,
        font=token.font,
        embedded_color=True,
    )

    if not math.isclose(token.render_scale, 1.0):
        scaled_w = max(1, int(round(text_w * token.render_scale)))
        scaled_h = max(1, int(round(text_h * token.render_scale)))
        base = base.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

    _emoji_cache[key] = base
    return base


def draw_emoji_layer_rgba(token: TokenSpec, px: int, py: int, *, alpha: float = 1.0, scale: float = 1.0) -> Image.Image:
    base = get_emoji_rgba(token)
    if not math.isclose(scale, 1.0):
        scaled_w = max(1, int(round(base.width * scale)))
        scaled_h = max(1, int(round(base.height * scale)))
        rendered = base.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    else:
        rendered = base

    rendered = scale_alpha(rendered, alpha)
    layer = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    paste_x = px + (token.width - rendered.width) // 2
    paste_y = py + (token.height - rendered.height) // 2
    layer.alpha_composite(rendered, (paste_x, paste_y))
    return layer


def draw_text_layer_rgba(token: TokenSpec, px: int, py: int, fill: tuple[int, int, int, int]) -> Image.Image:
    layer = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((px, py), token.text, font=token.font, fill=fill)
    return layer


def make_emoji_intro_layer_rgba(token: TokenSpec, px: int, py: int, phase: int) -> Image.Image:
    intro_frames = FRAMES_FALL + FRAMES_GLOW_IN
    if intro_frames <= 1:
        return draw_emoji_layer_rgba(token, px, py)

    t = min(1.0, max(0.0, phase / (intro_frames - 1)))
    if t < 0.7:
        local_t = t / 0.7
        scale = EMOJI_POP_START + (EMOJI_POP_OVERSHOOT - EMOJI_POP_START) * local_t
    else:
        local_t = (t - 0.7) / 0.3
        scale = EMOJI_POP_OVERSHOOT + (1.0 - EMOJI_POP_OVERSHOOT) * local_t
    alpha = min(1.0, 0.25 + t * 1.05)
    return draw_emoji_layer_rgba(token, px, py, alpha=alpha, scale=scale)


def apply_zoom(img: Image.Image, zoom: float, cx: float, cy: float) -> Image.Image:
    inv = 1.0 / zoom
    a, b, c = inv, 0.0, cx * (1.0 - inv)
    d, e, f = 0.0, inv, cy * (1.0 - inv)
    return img.transform(
        (VIDEO_WIDTH, VIDEO_HEIGHT),
        Image.AFFINE,
        (a, b, c, d, e, f),
        resample=Image.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def render_frame(
    positions: Sequence[TokenPlacement],
    ox: int,
    oy: int,
    frame_num: int,
    n_tokens: int,
) -> Image.Image:
    total_word_frames = n_tokens * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_tokens, 0
        zoom = 1.12
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
        t_ease = t_global * t_global * (3 - 2 * t_global)
        zoom = 1.0 + (1.12 - 1.0) * t_ease

    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))

    for i, placement in enumerate(positions):
        px, py = ox + placement.x, oy + placement.y
        token = placement.token
        if all_lit or i < cur:
            if token.kind == "emoji":
                img = Image.alpha_composite(img, draw_emoji_layer_rgba(token, px, py))
            else:
                img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                img = Image.alpha_composite(img, draw_text_layer_rgba(token, px, py, (*LIT_COLOR, 255)))
        elif i == cur:
            if token.kind == "emoji":
                if phase < FRAMES_FALL + FRAMES_GLOW_IN:
                    img = Image.alpha_composite(img, make_emoji_intro_layer_rgba(token, px, py, phase))
                else:
                    img = Image.alpha_composite(img, draw_emoji_layer_rgba(token, px, py))
            else:
                if phase < FRAMES_FALL:
                    img = Image.alpha_composite(img, make_text_fall_layer_rgba(token, px, py, phase))
                elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                    t = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                    glow = get_glow_rgba(token, px, py)
                    if t < 1.0:
                        glow = scale_alpha(glow, t)
                    img = Image.alpha_composite(img, glow)
                    img = Image.alpha_composite(img, draw_text_layer_rgba(token, px, py, (*LIT_COLOR, 255)))
                else:
                    img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                    img = Image.alpha_composite(img, draw_text_layer_rgba(token, px, py, (*LIT_COLOR, 255)))

    return apply_zoom(img, zoom, VIDEO_WIDTH / 2, VIDEO_HEIGHT / 2)


def scale_image_alpha(img: Image.Image, alpha_scale: float) -> Image.Image:
    if alpha_scale >= 0.999:
        return img
    if alpha_scale <= 0.0:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b, a = img.split()
    a = ImageEnhance.Brightness(a).enhance(alpha_scale)
    return Image.merge("RGBA", (r, g, b, a))


def save_overlay_frames(
    frames_dir: Path,
    positions: Sequence[TokenPlacement],
    ox: int,
    oy: int,
    n_tokens: int,
    outro_frames: int,
) -> tuple[int, int]:
    medium_frames = n_tokens * FRAMES_PER_WORD + FRAMES_FINAL
    total_overlay_frames = medium_frames + outro_frames
    fade_start = max(0, medium_frames - FRAMES_FADE_OUT)

    for frame_num in range(total_overlay_frames):
        if frame_num < medium_frames:
            img = render_frame(positions, ox, oy, frame_num, n_tokens)
            if frame_num >= fade_start:
                fade_progress = (frame_num - fade_start + 1) / max(1, FRAMES_FADE_OUT)
                img = scale_image_alpha(img, 1.0 - fade_progress)
        else:
            img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
        img.save(frames_dir / f"frame_{frame_num:05d}.png")
    return medium_frames, total_overlay_frames


def build_final_video(
    assets: Mapping[str, Mapping[str, float | int | str]],
    medium_duration: float,
    intro_duration: float,
    overlay_dir: Path,
    out_path: Path,
) -> None:
    filter_graph = (
        f"[0:v]fps={FPS},scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuva444p10le[intro];"
        f"[1:v]trim=duration={medium_duration:.6f},setpts=PTS-STARTPTS,fps={FPS},"
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuva444p10le[medium];"
        f"[2:v]fps={FPS},scale={VIDEO_WIDTH}:{VIDEO_HEIGHT},format=yuva444p10le[outro];"
        "[intro][medium][outro]concat=n=3:v=1:a=0[bg];"
        f"[3:v]format=rgba,setpts=PTS+{intro_duration:.6f}/TB[title];"
        "[bg][title]overlay=eof_action=pass:format=auto[v];"
        "[4:a]asplit=2[intro_src][outro_src];"
        f"[intro_src]atrim=duration={intro_duration:.6f},asetpts=PTS-STARTPTS[intro_a];"
        f"[5:a]atrim=duration={medium_duration:.6f},asetpts=PTS-STARTPTS[medium_a];"
        f"[outro_src]atrim=duration={float(assets['outro']['duration']):.6f},asetpts=PTS-STARTPTS[outro_a];"
        "[intro_a][medium_a][outro_a]concat=n=3:v=0:a=1[a]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(INTRO_PATH),
            "-stream_loop",
            "-1",
            "-i",
            str(MEDIUM_PATH),
            "-i",
            str(OUTRO_PATH),
            "-framerate",
            str(FPS),
            "-i",
            str(overlay_dir / "frame_%05d.png"),
            "-i",
            str(AUDIO_PATH),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-r",
            str(FPS),
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-alpha_bits",
            "16",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out_path),
        ],
        check=True,
    )


def render_title_video(
    text: str,
    output_path: Path,
    assets: Mapping[str, Mapping[str, float | int | str]],
) -> float:
    token_texts = re.findall(r"\S+", text)
    if not token_texts:
        raise ValueError("Cannot render an empty title.")

    fonts = load_fonts(FONT_SIZE)
    token_specs = build_token_specs(text, fonts)
    positions, block_w, block_h = layout_tokens(token_specs, fonts.text_font)
    ox = (VIDEO_WIDTH - block_w) // 2
    oy = (VIDEO_HEIGHT - block_h) // 2

    n_tokens = len(token_specs)
    medium_frames = n_tokens * FRAMES_PER_WORD + FRAMES_FINAL
    medium_duration = medium_frames / FPS
    intro_duration = float(assets["intro"]["duration"])
    outro_duration = float(assets["outro"]["duration"])
    outro_frames = int(round(outro_duration * FPS))
    total_duration = intro_duration + medium_duration + outro_duration

    overlay_dir = Path(tempfile.mkdtemp(prefix="glow_title_overlay_"))
    try:
        save_overlay_frames(overlay_dir, positions, ox, oy, n_tokens, outro_frames)
        build_final_video(assets, medium_duration, intro_duration, overlay_dir, output_path)
    finally:
        shutil.rmtree(overlay_dir)
    return total_duration


def prepare_output_dirs(base_dir: Path, stem: str) -> tuple[Path, Path, Path, Path]:
    base_output = base_dir / f"{stem}_titles_media"
    text_dir = base_output / "textfiles"
    video_dir = base_output / "videos"
    manifest_path = base_output / f"{stem}_titles_manifest.json"
    base_output.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(exist_ok=True)
    video_dir.mkdir(exist_ok=True)
    return base_output, text_dir, video_dir, manifest_path


def create_videos(
    entries: Sequence[TitleEntry],
    text_dir: Path,
    video_dir: Path,
    assets: Mapping[str, Mapping[str, float | int | str]],
) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    for idx, entry in enumerate(entries, start=1):
        text_path = text_dir / f"title_{idx:03d}.txt"
        text_path.write_text(entry.value, encoding="utf-8")
        output_path = video_dir / f"title_{idx:03d}.mov"
        print(f"   🎬 title_{idx:03d}.mov ({entry.value})")
        try:
            duration = render_title_video(entry.value, output_path, assets)
            video_path = str(output_path)
        except Exception as exc:
            print(f"   ⚠️  Title render failed for {output_path.name}: {exc}")
            duration = None
            video_path = None
        results.append(
            {
                "id": idx,
                "title": entry.value,
                "duration": duration,
                "video_path": video_path,
                "text_path": str(text_path),
                "row_index": entry.row_index,
                "transcript_number": entry.transcript_number,
                "start_timecode": entry.start_timecode,
                "end_timecode": entry.end_timecode,
                "start_seconds": entry.start_seconds,
                "end_seconds": entry.end_seconds,
                "text": entry.text,
            }
        )
    return results


def write_manifest(manifest_path: Path, entries: Sequence[Dict[str, object]], source_csv: Path) -> None:
    data = {
        "source_csv": str(source_csv),
        "videos": entries,
    }
    manifest_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    output_dir = args.output_dir if args.output_dir else OUTPUT_DIR
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be greater than 0.")
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    entries: List[TitleEntry]
    try:
        entries = collect_entries(rows, header_map, args.frame_rate)
    except KeyError:
        entries = []
    if not entries and args.timing_manifest and args.timing_manifest.expanduser().exists():
        entries = collect_entries_from_timing_manifest(args.timing_manifest.expanduser())
    if not entries:
        print("No titles found.")
        return

    assets = validate_assets()
    _glow_cache.clear()

    base_output, text_dir, video_dir, manifest_path = prepare_output_dirs(output_dir, csv_path.stem)
    print(f"Generating {len(entries)} title videos...")
    video_records = create_videos(entries, text_dir, video_dir, assets)
    write_manifest(manifest_path, video_records, csv_path)
    print("\nSummary")
    print("=" * 40)
    print(f"Source CSV : {csv_path}")
    print(f"Videos dir : {video_dir}")
    print(f"Text dir   : {text_dir}")
    print(f"Manifest   : {manifest_path}")


if __name__ == "__main__":
    main()
