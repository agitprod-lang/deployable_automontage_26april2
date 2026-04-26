#!/usr/bin/env python3
"""Render transparent animated list clips from the timed insert manifest."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"

W, H = 1920, 1080
FPS = 24
FONT_SIZE = 88
PADDING_X = 180
LINE_SPACING = 1.45
ITEM_GAP = 28
PREFIX_GAP = 22
MARKER_W = 56
MARKER_H = 76

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90
FRAMES_FADE_OUT = 18

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)
EMOJI_POP_START = 0.12
EMOJI_POP_OVERSHOOT = 1.18

LIT_COLOR = (0, 0, 0)
GLOW_COLOR = (255, 0, 0)
FONT_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/"
    "Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"
)
ZOOM_START = 1.0
ZOOM_END = 1.12

INTRO_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "title_background/rgb_invert/intro_invert.mov"
)
MEDIUM_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "title_background/rgb_invert/medium_invert.mp4"
)
OUTRO_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "title_background/rgb_invert/outro_invert.mov"
)
AUDIO_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "title_background/ripped_trimed.m4a"
)
LIST_SOUND_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/"
    "sounds/confetti.mp3"
)
LIST_SOUND_VOLUME = 0.8

LIST_TYPES: Dict[str, str] = {
    "list_bullet_group": "list_bullet",
    "list_dash_group": "list_dash",
    "list_number_group": "list_number",
    "list_check_group": "list_check",
}


@dataclass(frozen=True)
class ListEntry:
    entry_id: int
    seq_num: int
    type_name: str
    illustration_type: str
    items: Tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render transparent animated list clips.")
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


def normalize_items(reference_word: str, transcript_word: str) -> Tuple[str, ...]:
    source = reference_word.strip() or transcript_word.strip()
    fragments = [fragment.strip() for fragment in source.split("|") if fragment.strip()]
    return tuple(fragments)


def load_list_entries(path: Path) -> List[ListEntry]:
    entries: List[ListEntry] = []
    counters: Dict[str, int] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        for row in reader:
            if (row.get("Asset Category") or "").strip() != "social_ranking_punctuation":
                continue
            illustration_type = (row.get("Illustration Type") or "").strip()
            type_name = LIST_TYPES.get(illustration_type)
            if type_name is None:
                continue
            items = normalize_items(row.get("Reference Word") or "", row.get("Transcript Word") or "")
            if not items:
                continue
            try:
                entry_id = int((row.get("Entry ID") or "").strip())
            except ValueError:
                entry_id = len(entries) + 1
            counters[type_name] = counters.get(type_name, 0) + 1
            entries.append(
                ListEntry(
                    entry_id=entry_id,
                    seq_num=counters[type_name],
                    type_name=type_name,
                    illustration_type=illustration_type,
                    items=items,
                )
            )
    return entries


def load_font(size):
    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(f"Required font not found: {FONT_PATH}")
    return ImageFont.truetype(FONT_PATH, size)


def draw_text(draw, pos, text, font, fill, *, embedded_color=False):
    kwargs = {"font": font, "fill": fill}
    if embedded_color:
        kwargs["embedded_color"] = True
    draw.text(pos, text, **kwargs)


def marker_for_item(entry: ListEntry, index: int) -> Tuple[str, str]:
    return "marker", "📍"


def layout_items(entry: ListEntry, font):
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    max_w = W - 2 * PADDING_X
    space_w = draw.textlength(" ", font=font)

    tokens = []
    block_w = 0
    y = 0
    for item_index, item in enumerate(entry.items):
        words = item.split()
        if not words:
            continue
        marker_kind, marker_text = marker_for_item(entry, item_index)
        marker_font = font if marker_kind == "text_marker" else None
        if marker_kind == "marker":
            prefix_w = MARKER_W
            prefix_h = MARKER_H
        else:
            marker_bbox = draw.textbbox((0, 0), marker_text, font=font)
            prefix_w = marker_bbox[2] - marker_bbox[0]
            prefix_h = marker_bbox[3] - marker_bbox[1]

        line_x = prefix_w + PREFIX_GAP
        line_h = prefix_h
        line_tokens = [
            {
                "text": marker_text,
                "x": 0,
                "y": y,
                "kind": marker_kind,
                "font": marker_font,
                "w": prefix_w,
                "h": prefix_h,
            }
        ]
        for word in words:
            bbox = draw.textbbox((0, 0), word, font=font)
            word_w = bbox[2] - bbox[0]
            word_h = bbox[3] - bbox[1]
            if line_x > prefix_w + PREFIX_GAP and line_x + word_w > max_w:
                line_x = prefix_w + PREFIX_GAP
                y += int(line_h * LINE_SPACING)
                line_h = word_h
            line_h = max(line_h, word_h, prefix_h)
            line_tokens.append(
                {
                    "text": word,
                    "x": line_x,
                    "y": y,
                    "kind": "word",
                    "font": font,
                    "w": word_w,
                    "h": word_h,
                }
            )
            line_x += word_w + space_w
        line_width = max(token["x"] + token["w"] for token in line_tokens)
        block_w = max(block_w, line_width)
        tokens.extend(line_tokens)
        y += int(line_h * LINE_SPACING)
        if item_index < len(entry.items) - 1:
            y += ITEM_GAP

    block_h = max(y - int(prefix_h * (LINE_SPACING - 1)), prefix_h)
    return tokens, block_w, block_h


_glow_cache = {}


def rgb_to_rgba(rgb_img):
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def scale_rgba_alpha(img, alpha_scale):
    if alpha_scale >= 0.999:
        return img
    if alpha_scale <= 0.0:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b, a = img.split()
    a = ImageEnhance.Brightness(a).enhance(alpha_scale)
    return Image.merge("RGBA", (r, g, b, a))


def make_marker_rgba(width, height, fill, hole_fill=(0, 0, 0, 0)):
    icon = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    cx = width / 2
    circle_d = int(width * 0.82)
    circle_left = int((width - circle_d) / 2)
    circle_top = 0
    circle_right = circle_left + circle_d
    circle_bottom = circle_top + circle_d
    tail_top = int(circle_d * 0.58)
    tail = [
        (int(cx), height - 1),
        (circle_left + int(circle_d * 0.18), tail_top),
        (circle_right - int(circle_d * 0.18), tail_top),
    ]
    draw.polygon(tail, fill=fill)
    draw.ellipse((circle_left, circle_top, circle_right, circle_bottom), fill=fill)
    hole_d = int(circle_d * 0.38)
    hole_left = int(cx - hole_d / 2)
    hole_top = int(circle_d * 0.22)
    draw.ellipse(
        (hole_left, hole_top, hole_left + hole_d, hole_top + hole_d),
        fill=hole_fill,
    )
    return icon


def draw_marker_layer(px, py, scale=1.0):
    marker = make_marker_rgba(MARKER_W, MARKER_H, (0, 0, 0, 255))
    px = int(round(px))
    py = int(round(py))
    if scale != 1.0:
        scaled_w = max(1, int(round(MARKER_W * scale)))
        scaled_h = max(1, int(round(MARKER_H * scale)))
        marker = marker.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        paste_x = int(round(px + (MARKER_W - scaled_w) / 2))
        paste_y = int(round(py + (MARKER_H - scaled_h) / 2))
    else:
        paste_x = px
        paste_y = py
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(marker, (paste_x, paste_y))
    return layer


def marker_glow_rgba(px, py):
    px = int(round(px))
    py = int(round(py))
    base = Image.new("RGB", (W, H), (0, 0, 0))
    marker = make_marker_rgba(MARKER_W, MARKER_H, GLOW_COLOR, hole_fill=(0, 0, 0, 0))
    base.paste(marker.convert("RGB"), (px, py), marker)
    result = base.copy()
    for radius in [3, 6, 12, 22, 40]:
        result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
    return rgb_to_rgba(result)


def get_glow_rgba(token, px, py):
    key = (token["text"], px, py, token["kind"])
    if key not in _glow_cache:
        if token["kind"] == "marker":
            _glow_cache[key] = marker_glow_rgba(px, py)
        else:
            base = Image.new("RGB", (W, H), (0, 0, 0))
            draw = ImageDraw.Draw(base)
            draw_text(draw, (px, py), token["text"], token["font"], GLOW_COLOR)
            result = base.copy()
            for radius in [3, 6, 12, 22, 40]:
                result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
            _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_fall_layer_rgba(token, px, py, phase):
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)

    layer = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw_text(draw, (px, py - y_off), token["text"], token["font"], FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def draw_scaled_token_rgba(token, px, py, scale, fill):
    if token["kind"] == "marker":
        return draw_marker_layer(px, py, scale)
    scale = max(scale, 0.01)
    bbox = token["font"].getbbox(token["text"])
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])
    base = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    draw_text(
        draw,
        (-bbox[0], -bbox[1]),
        token["text"],
        token["font"],
        fill,
        embedded_color=(token["kind"] == "emoji"),
    )
    scaled_w = max(1, int(round(text_w * scale)))
    scaled_h = max(1, int(round(text_h * scale)))
    scaled = base.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    paste_x = int(round(px + (text_w - scaled_w) / 2))
    paste_y = int(round(py + (text_h - scaled_h) / 2))
    layer.alpha_composite(scaled, (paste_x, paste_y))
    return layer


def draw_word_layer_rgba(token, px, py, fill):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw_text(draw, (px, py), token["text"], token["font"], fill)
    return layer


def make_emoji_pop_layer_rgba(token, px, py, phase):
    pop_frames = FRAMES_FALL + FRAMES_GLOW_IN
    if pop_frames <= 1:
        scale = 1.0
    else:
        t = phase / (pop_frames - 1)
        if t < 0.7:
            local_t = t / 0.7
            scale = EMOJI_POP_START + (EMOJI_POP_OVERSHOOT - EMOJI_POP_START) * local_t
        else:
            local_t = (t - 0.7) / 0.3
            scale = EMOJI_POP_OVERSHOOT + (1.0 - EMOJI_POP_OVERSHOOT) * local_t
    return draw_scaled_token_rgba(token, px, py, scale, (255, 255, 255, 255))


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


def render_frame(tokens, ox, oy, frame_num, n_tokens):
    total_word_frames = n_tokens * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_tokens, 0
        zoom = ZOOM_END
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
        t_ease = t_global * t_global * (3 - 2 * t_global)
        zoom = ZOOM_START + (ZOOM_END - ZOOM_START) * t_ease

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    for index, token in enumerate(tokens):
        px, py = ox + token["x"], oy + token["y"]

        if all_lit or index < cur:
            img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
            if token["kind"] == "marker":
                img = Image.alpha_composite(img, draw_marker_layer(px, py))
            else:
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )
        elif index == cur:
            if token["kind"] == "marker":
                if phase < FRAMES_FALL + FRAMES_GLOW_IN:
                    img = Image.alpha_composite(
                        img, make_emoji_pop_layer_rgba(token, px, py, phase)
                    )
                else:
                    img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                    img = Image.alpha_composite(img, draw_marker_layer(px, py))
            elif phase < FRAMES_FALL:
                img = Image.alpha_composite(img, make_fall_layer_rgba(token, px, py, phase))
            elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                t = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                glow = get_glow_rgba(token, px, py)
                if t < 1.0:
                    r, g, b, a = glow.split()
                    a = ImageEnhance.Brightness(a).enhance(t)
                    glow = Image.merge("RGBA", (r, g, b, a))
                img = Image.alpha_composite(img, glow)
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )
            else:
                img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )

    return apply_zoom(img, zoom, W / 2, H / 2)


def scale_image_alpha(img, alpha_scale):
    return scale_rgba_alpha(img, alpha_scale)


def ffprobe_json(path, entries, scope):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            f"{scope}={entries}",
            "-of",
            "json",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def probe_video(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required background asset not found: {path}")
    stream_data = ffprobe_json(path, "width,height,r_frame_rate", "stream")
    format_data = ffprobe_json(path, "duration", "format")
    streams = stream_data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No video stream found in asset: {path}")
    stream = streams[0]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    return {
        "path": path,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "fps": fps,
        "duration": float(format_data["format"]["duration"]),
    }


def validate_assets():
    assets = {
        "intro": probe_video(INTRO_PATH),
        "medium": probe_video(MEDIUM_PATH),
        "outro": probe_video(OUTRO_PATH),
    }
    for name, data in assets.items():
        if data["width"] != W or data["height"] != H:
            raise RuntimeError(
                f"{name} asset has unexpected size {data['width']}x{data['height']}; expected {W}x{H}"
            )
        if abs(data["fps"] - FPS) > 0.01:
            raise RuntimeError(
                f"{name} asset has unexpected fps {data['fps']:.3f}; expected {FPS}"
            )
    return assets


def validate_audio():
    if not os.path.exists(AUDIO_PATH):
        raise FileNotFoundError(f"Required audio asset not found: {AUDIO_PATH}")
    data = ffprobe_json(AUDIO_PATH, "codec_name,sample_rate,channels", "stream")
    if not (data.get("streams") or []):
        raise RuntimeError(f"No audio stream found in asset: {AUDIO_PATH}")
    if not os.path.exists(LIST_SOUND_PATH):
        raise FileNotFoundError(f"Required list sound asset not found: {LIST_SOUND_PATH}")
    sound_data = ffprobe_json(LIST_SOUND_PATH, "codec_name,sample_rate,channels", "stream")
    if not (sound_data.get("streams") or []):
        raise RuntimeError(f"No audio stream found in asset: {LIST_SOUND_PATH}")


def marker_sound_times(tokens):
    cue_times = []
    for index, token in enumerate(tokens):
        if token["x"] != 0:
            continue
        if token["kind"] not in {"marker", "text_marker"}:
            continue
        cue_times.append(index * FRAMES_PER_WORD / FPS)
    return cue_times


def save_overlay_frames(frames_dir, tokens, ox, oy, n_tokens, outro_frames):
    medium_frames = n_tokens * FRAMES_PER_WORD + FRAMES_FINAL
    total_overlay_frames = medium_frames + outro_frames
    fade_start = max(0, medium_frames - FRAMES_FADE_OUT)

    for frame_num in range(total_overlay_frames):
        if frame_num < medium_frames:
            img = render_frame(tokens, ox, oy, frame_num, n_tokens)
            if frame_num >= fade_start:
                fade_progress = (frame_num - fade_start + 1) / max(1, FRAMES_FADE_OUT)
                img = scale_image_alpha(img, 1.0 - fade_progress)
        else:
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        img.save(os.path.join(frames_dir, f"frame_{frame_num:05d}.png"))
    return medium_frames, total_overlay_frames


def build_final_video(assets, medium_duration, intro_duration, overlay_dir, out_path, marker_times):
    temp_out_path = os.path.join(overlay_dir, "encoded_output.mov")
    base_graph = (
        "[0:v]fps=24,format=yuva444p10le[intro];"
        f"[1:v]trim=duration={medium_duration:.6f},setpts=PTS-STARTPTS,fps=24,format=yuva444p10le[medium];"
        "[2:v]fps=24,format=yuva444p10le[outro];"
        "[intro][medium][outro]concat=n=3:v=1:a=0[bg];"
        f"[3:v]format=rgba,setpts=PTS+{intro_duration:.6f}/TB[title];"
        "[bg][title]overlay=eof_action=pass:format=auto[v];"
        "[4:a]asplit=2[intro_src][outro_src];"
        f"[intro_src]atrim=duration={intro_duration:.6f},asetpts=PTS-STARTPTS[intro_a];"
        f"[6:a]atrim=duration={medium_duration:.6f},asetpts=PTS-STARTPTS[medium_a];"
        f"[outro_src]atrim=duration={assets['outro']['duration']:.6f},asetpts=PTS-STARTPTS[outro_a];"
        "[intro_a][medium_a][outro_a]concat=n=3:v=0:a=1[a];"
    )
    effect_times = [intro_duration + marker_time for marker_time in marker_times]
    filter_parts = [base_graph]
    audio_output_label = "[a]"

    if effect_times:
        split_labels = "".join(f"[sfx_src_{idx}]" for idx in range(len(effect_times)))
        filter_parts.append(f"[5:a]asplit={len(effect_times)}{split_labels};")
        mixed_inputs = ["[a]"]
        for idx, cue_time in enumerate(effect_times):
            delay_ms = max(0, int(round(cue_time * 1000)))
            out_label = f"[sfx_mix_{idx}]"
            filter_parts.append(
                f"[sfx_src_{idx}]"
                f"volume={LIST_SOUND_VOLUME:.3f},"
                f"adelay={delay_ms}|{delay_ms}"
                f"{out_label};"
            )
            mixed_inputs.append(out_label)
        audio_output_label = "[a_with_fx]"
        filter_parts.append(
            "".join(mixed_inputs)
            + f"amix=inputs={len(mixed_inputs)}:normalize=0{audio_output_label}"
        )

    filter_graph = "".join(filter_parts)

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            assets["intro"]["path"],
            "-stream_loop",
            "-1",
            "-i",
            assets["medium"]["path"],
            "-i",
            assets["outro"]["path"],
            "-framerate",
            str(FPS),
            "-i",
            os.path.join(overlay_dir, "frame_%05d.png"),
            "-i",
            AUDIO_PATH,
            "-i",
            LIST_SOUND_PATH,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            audio_output_label,
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
            temp_out_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.move(temp_out_path, out_path)


def encode_clip(entry: ListEntry, output_path: Path) -> None:
    assets = validate_assets()
    validate_audio()

    font = load_font(FONT_SIZE)
    tokens, block_w, block_h = layout_items(entry, font)
    ox = (W - block_w) // 2
    oy = (H - block_h) // 2

    n_tokens = len(tokens)
    marker_times = marker_sound_times(tokens)
    medium_frames = n_tokens * FRAMES_PER_WORD + FRAMES_FINAL
    medium_duration = medium_frames / FPS
    intro_duration = assets["intro"]["duration"]
    outro_frames = int(round(assets["outro"]["duration"] * FPS))

    frames_dir = Path(tempfile.mkdtemp(prefix="glow_list_bg_overlay_"))
    try:
        save_overlay_frames(str(frames_dir), tokens, ox, oy, n_tokens, outro_frames)
        build_final_video(
            assets,
            medium_duration,
            intro_duration,
            str(frames_dir),
            str(output_path),
            marker_times,
        )
    finally:
        shutil.rmtree(frames_dir)


def render_entries(entries: List[ListEntry], video_dir: Path) -> int:
    rendered = 0
    for entry in entries:
        output_path = video_dir / f"{entry.type_name}_{entry.seq_num:03d}_LST.mov"
        print(f"  [LST] #{entry.seq_num:03d} {entry.illustration_type} -> {' | '.join(entry.items)}")
        encode_clip(entry, output_path)
        print(f"       -> {output_path}")
        rendered += 1
    return rendered


def main() -> None:
    args = parse_args()
    input_csv = args.input_csv.expanduser().resolve() if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    timing_manifest = resolve_timing_manifest(input_csv, args.timing_manifest)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else OUTPUT_DIR

    entries = load_list_entries(timing_manifest)
    if not entries:
        print(f"No list entries found in {timing_manifest}.")
        return

    video_dir = output_dir / f"{input_csv.stem}_mentions_media" / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)

    print(f"Input CSV       : {input_csv}")
    print(f"Timing manifest : {timing_manifest}")
    print(f"Video output    : {video_dir}")
    print(f"List entries    : {len(entries)}")

    rendered = render_entries(entries, video_dir)
    print(f"\nRendered {rendered} list clip(s).")


if __name__ == "__main__":
    main()
