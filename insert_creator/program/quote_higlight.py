#!/usr/bin/env python3
"""Generate static quote highlight videos using sequential yellow wipes."""

from __future__ import annotations

import argparse
import math
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - Pillow required
    raise SystemExit("Pillow is required. Install it via 'pip install pillow'.") from exc

from processor import create_quote as cq


WIDTH = 1920
HEIGHT = 1080
FPS = 30
INITIAL_HOLD = 1.3
TAIL_HOLD = 1.2
LINE_GAP = 0.2
BLANK_LINE_GAP = 0.15
LINE_MIN_DURATION = 0.9
LINE_DURATION_PER_CHAR = 0.04
LINE_SPACING_MULTIPLIER = 1.25
BASELINE_ASCENT_RATIO = 0.82
TEXT_FADE_DURATION = 0.4
TEXT_FADE_IN_DURATION = 0.3
MAINTAIN_REDUCTION = 1.0
MIN_MIDDLE_BACKGROUND_DURATION = 4.0
HIGHLIGHT_COLOR = (0xFF, 0xD3, 0x4A)
HIGHLIGHT_ALPHA = 0.7
HIGHLIGHT_PADDING_X = 45.0
HIGHLIGHT_PADDING_Y = 18.0
HIGHLIGHT_NOISE_STEP = 28.0
HIGHLIGHT_NOISE_AMPLITUDE = 7.0
TEXT_COLOR = (0, 0, 0, 255)
SHADOW_COLOR = (0, 0, 0, 0x55)
SHADOW_OFFSET = (2, 2)
QUOTE_BACKGROUND = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/title_background/rgb_background/medium_rgb_paper.mp4"
)
QUOTE_INTRO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/title_background/rgb_background/derushAvecInsert (Resolve)_6.mov"
)
QUOTE_OUTRO = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/title_background/rgb_background/outronew.mov"
)
PAPER_SOUND = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/papersound.mp3"
)
PAPER_SOUND_VOLUME = 0.4
ASSET_DURATION_CACHE: Dict[Path, float] = {}


def calculate_phase_timing(
    text: str,
    highlight_duration: float,
    intro_duration: float,
    outro_duration: float,
) -> Tuple[float, float, float, float]:
    visible_hold = max(0.0, cq.calculate_visible_hold(text) - MAINTAIN_REDUCTION)
    fade_start_target = max(highlight_duration, intro_duration + visible_hold)
    body_hold_duration = max(MIN_MIDDLE_BACKGROUND_DURATION, fade_start_target - intro_duration)
    fade_start = intro_duration + body_hold_duration
    fade_end = fade_start + TEXT_FADE_DURATION
    body_duration = fade_end - intro_duration
    clip_duration = fade_end + outro_duration
    return body_duration, fade_start, fade_end, clip_duration


def probe_asset_duration(path: Path) -> float:
    cached = ASSET_DURATION_CACHE.get(path)
    if cached is not None:
        return cached
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to probe duration for {path}: {result.stderr.strip()}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Unusable duration returned for {path}: {result.stdout!r}") from exc
    ASSET_DURATION_CACHE[path] = duration
    return duration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create static quote highlight clips from the latest universal comparser CSV output."
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Explicit path to a *_comparison*.csv file (defaults to the freshest file in Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for generated files (defaults to insert_creator/output).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=25.0,
        help="Frame rate used to convert HH:MM:SS:FF timecodes to seconds (default: 25).",
    )
    return parser.parse_args()


@dataclass
class LineSpec:
    text: str
    width: float
    text_x: float
    text_y: float
    box_x: float
    box_y: float
    box_w: float
    box_h: float
    start: float | None
    end: float | None
    duration: float | None
    top_offsets: List[float]
    bottom_offsets: List[float]


def prepare_lines(display_text: str, font: ImageFont.FreeTypeFont, font_size: int) -> Tuple[List[LineSpec], float]:
    raw_lines = display_text.splitlines()
    if not raw_lines:
        raw_lines = [""]
    line_spacing = font_size * LINE_SPACING_MULTIPLIER
    text_block_height = font_size + (len(raw_lines) - 1) * line_spacing
    first_baseline = (HEIGHT - text_block_height) / 2 + font_size * BASELINE_ASCENT_RATIO
    cursor = INITIAL_HOLD
    specs: List[LineSpec] = []
    ascent, _ = font.getmetrics()
    for idx, raw_line in enumerate(raw_lines):
        line_text = raw_line.rstrip()
        baseline = first_baseline + idx * line_spacing
        if line_text:
            duration = max(LINE_MIN_DURATION, len(line_text) * LINE_DURATION_PER_CHAR)
            start = cursor
            end = start + duration
            cursor = end + LINE_GAP
            bbox = font.getbbox(line_text)
            width = max(float(bbox[2] - bbox[0]), 1.0)
            box_h = font_size + HIGHLIGHT_PADDING_Y * 2
            box_y = max(baseline - font_size * BASELINE_ASCENT_RATIO - HIGHLIGHT_PADDING_Y, 0.0)
            text_left = (WIDTH - width) / 2
            box_x = max(text_left - HIGHLIGHT_PADDING_X, 0.0)
            box_w = min(width + HIGHLIGHT_PADDING_X * 2, WIDTH - box_x)
            text_y = baseline - ascent
            sample_count = max(2, int(math.ceil(box_w / HIGHLIGHT_NOISE_STEP)) + 1)
            rng = random.Random(hash((line_text, idx, font_size)))
            top_offsets = [rng.uniform(-HIGHLIGHT_NOISE_AMPLITUDE, HIGHLIGHT_NOISE_AMPLITUDE) for _ in range(sample_count)]
            bottom_offsets = [rng.uniform(-HIGHLIGHT_NOISE_AMPLITUDE, HIGHLIGHT_NOISE_AMPLITUDE) for _ in range(sample_count)]
        else:
            duration = None
            start = None
            end = None
            cursor += BLANK_LINE_GAP
            width = 0.0
            box_y = 0.0
            box_h = 0.0
            box_x = 0.0
            box_w = 0.0
            text_y = 0.0
            top_offsets = []
            bottom_offsets = []
        specs.append(
            LineSpec(
                text=line_text,
                width=round(width, 3),
                text_x=round((WIDTH - width) / 2, 3),
                text_y=round(text_y, 3),
                box_x=round(box_x, 3),
                box_y=round(box_y, 3),
                box_w=round(box_w, 3),
                box_h=round(box_h, 3),
                start=start,
                end=end,
                duration=duration,
                top_offsets=top_offsets,
                bottom_offsets=bottom_offsets,
            )
        )
    total_duration = max(cursor + TAIL_HOLD, INITIAL_HOLD + TEXT_FADE_DURATION)
    return specs, total_duration


def render_highlight_videos(
    quotes: Sequence[Dict[str, object]],
    base_name: str,
    output_dir: Path,
) -> Tuple[Path, int, int]:
    video_dir = output_dir / f"{base_name}_highlight_quotes_videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    font_path = Path(cq.find_system_font())
    intro_duration = probe_asset_duration(QUOTE_INTRO)
    outro_duration = probe_asset_duration(QUOTE_OUTRO)
    if outro_duration <= 0:
        print(f"Unexpected outro duration for {QUOTE_OUTRO}: {outro_duration}")
        return video_dir, 0, len(quotes)
    success = 0
    failures = 0
    for quote in quotes:
        quote_text = str(quote.get("text", "")).strip()
        display_text = cq.build_display_text(quote_text, quote.get("author"))
        if not display_text:
            continue
        font_size = int(quote.get("suggested_font_size") or 54)
        font = ImageFont.truetype(str(font_path), font_size)
        line_specs, highlight_duration = prepare_lines(display_text, font, font_size)
        body_duration, fade_start, fade_end, clip_duration = calculate_phase_timing(
            quote_text,
            highlight_duration,
            intro_duration,
            outro_duration,
        )
        final_path = video_dir / f"quote_{quote['id']:03d}.mov"
        with tempfile.TemporaryDirectory(prefix="quote_highlight_") as tmp_dir:
            overlay_path = Path(tmp_dir) / f"quote_{quote['id']:03d}_overlay.mov"
            overlay_ok = render_highlight_clip(
                line_specs,
                font,
                clip_duration,
                fade_start,
                fade_end,
                overlay_path,
            )
            final_ok = overlay_ok and composite_quote_layers(
                overlay_path,
                clip_duration,
                intro_duration,
                body_duration,
                fade_end,
                final_path,
            )
        if final_ok:
            success += 1
        else:
            failures += 1
    return video_dir, success, failures


def render_highlight_clip(
    line_specs: Sequence[LineSpec],
    font: ImageFont.FreeTypeFont,
    duration: float,
    fade_start: float,
    fade_end: float,
    output_path: Path,
) -> bool:
    total_frames = max(1, math.ceil(duration * FPS))
    highlight_rgba = (
        HIGHLIGHT_COLOR[0],
        HIGHLIGHT_COLOR[1],
        HIGHLIGHT_COLOR[2],
        int(HIGHLIGHT_ALPHA * 255),
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "qtrle",
        "-pix_fmt",
        "argb",
        str(output_path),
    ]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for frame_idx in range(total_frames):
            t = frame_idx / FPS
            overlay_alpha = compute_overlay_alpha(t, fade_start, fade_end)
            text_fade_in = compute_fade_in_alpha(t, INITIAL_HOLD, TEXT_FADE_IN_DURATION)
            text_visibility = min(overlay_alpha, text_fade_in)
            image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            for spec in line_specs:
                if spec.start is None or spec.duration is None or spec.box_w <= 0:
                    continue
                if t < spec.start:
                    progress = 0.0
                elif t >= spec.end:
                    progress = 1.0
                else:
                    progress = (t - spec.start) / spec.duration
                progress = max(0.0, min(progress, 1.0))
                current_w = spec.box_w * progress
                draw_noisy_bar(draw, spec, current_w, highlight_rgba, overlay_alpha)
            for spec in line_specs:
                if not spec.text:
                    continue
                text_alpha = int(round(TEXT_COLOR[3] * text_visibility))
                shadow_alpha = int(round(SHADOW_COLOR[3] * text_visibility))
                shadow_pos = (spec.text_x + SHADOW_OFFSET[0], spec.text_y + SHADOW_OFFSET[1])
                if shadow_alpha > 0:
                    draw.text(
                        shadow_pos,
                        spec.text,
                        font=font,
                        fill=(SHADOW_COLOR[0], SHADOW_COLOR[1], SHADOW_COLOR[2], shadow_alpha),
                    )
                draw.text(
                    (spec.text_x, spec.text_y),
                    spec.text,
                    font=font,
                    fill=(TEXT_COLOR[0], TEXT_COLOR[1], TEXT_COLOR[2], text_alpha),
                )
            process.stdin.write(image.tobytes())
    finally:
        process.stdin.close()
    return process.wait() == 0


def draw_noisy_bar(
    draw: ImageDraw.ImageDraw,
    spec: LineSpec,
    width: float,
    color: Tuple[int, int, int, int],
    alpha_scale: float,
) -> None:
    if width <= 0 or not spec.top_offsets:
        return
    width = min(width, spec.box_w)
    step = max(1.0, HIGHLIGHT_NOISE_STEP)
    segments = max(1, int(math.ceil(width / step)))
    points_top: List[Tuple[float, float]] = []
    points_bottom: List[Tuple[float, float]] = []
    for i in range(segments + 1):
        x = spec.box_x + min(width, i * step)
        idx = min(i, len(spec.top_offsets) - 1)
        top_offset = spec.top_offsets[idx]
        bottom_offset = spec.bottom_offsets[idx]
        points_top.append((x, spec.box_y + top_offset))
        points_bottom.append((x, spec.box_y + spec.box_h + bottom_offset))
    polygon = points_top + points_bottom[::-1]
    adjusted_color = (
        color[0],
        color[1],
        color[2],
        int(round(color[3] * alpha_scale)),
    )
    draw.polygon(polygon, fill=adjusted_color)


def compute_overlay_alpha(t: float, fade_start: float, fade_end: float) -> float:
    if fade_end <= fade_start:
        return 0.0 if t >= fade_start else 1.0
    if t <= fade_start:
        return 1.0
    if t >= fade_end:
        return 0.0
    progress = (t - fade_start) / (fade_end - fade_start)
    return max(0.0, min(1.0 - progress, 1.0))


def compute_fade_in_alpha(t: float, start: float, duration: float) -> float:
    if duration <= 0:
        return 0.0 if t < start else 1.0
    if t <= start:
        return 0.0
    if t >= start + duration:
        return 1.0
    progress = (t - start) / duration
    return max(0.0, min(progress, 1.0))


def composite_quote_layers(
    overlay_path: Path,
    duration: float,
    intro_duration: float,
    body_duration: float,
    outro_start: float,
    output_path: Path,
) -> bool:
    missing_assets = [
        path for path in (QUOTE_BACKGROUND, QUOTE_INTRO, QUOTE_OUTRO) if not path.exists()
    ]
    if missing_assets:
        print(
            "Failed to build quote background layers: missing "
            + ", ".join(str(path) for path in missing_assets)
        )
        return False
    paper_sound_duration = None
    if PAPER_SOUND.exists():
        try:
            paper_sound_duration = probe_asset_duration(PAPER_SOUND)
        except RuntimeError as exc:
            print(f"⚠️  Failed to probe paper sound duration: {exc}")
    filter_complex = (
        f"color=c=black@0.0:size={WIDTH}x{HEIGHT}:rate={FPS}:d={duration:.6f},format=rgba[base];"
        f"[0:v:0]scale={WIDTH}:{HEIGHT},trim=0:{body_duration:.6f},setpts=PTS-STARTPTS+{intro_duration:.6f}/TB[body];"
        f"[1:v]format=rgba,setpts=PTS-STARTPTS[intro];"
        f"[2:v]format=rgba,setpts=PTS-STARTPTS+{outro_start:.6f}/TB[outro];"
        f"[3:v]format=rgba,setpts=PTS-STARTPTS[quote];"
        "[base][body]overlay=eof_action=pass:repeatlast=0[bg0];"
        "[bg0][intro]overlay=eof_action=pass:repeatlast=0[bg1];"
        "[bg1][outro]overlay=eof_action=pass:repeatlast=0[bg2];"
        "[bg2][quote]overlay=format=auto:eof_action=pass:repeatlast=0[vout]"
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(QUOTE_BACKGROUND),
        "-i",
        str(QUOTE_INTRO),
        "-i",
        str(QUOTE_OUTRO),
        "-i",
        str(overlay_path),
    ]
    if paper_sound_duration and paper_sound_duration > 0:
        end_delay_ms = max(0, int(round((duration - paper_sound_duration) * 1000.0)))
        cmd.extend(
            [
                "-i",
                str(PAPER_SOUND),
                "-i",
                str(PAPER_SOUND),
            ]
        )
        filter_complex += (
            f";[4:a]volume={PAPER_SOUND_VOLUME},atrim=0:{paper_sound_duration:.6f},asetpts=PTS-STARTPTS[a0]"
            f";[5:a]volume={PAPER_SOUND_VOLUME},atrim=0:{paper_sound_duration:.6f},adelay={end_delay_ms}|{end_delay_ms}[a1]"
            f";[a0][a1]amix=inputs=2:duration=longest[aout]"
        )
    cmd.extend(
        [
            "-filter_complex",
            filter_complex,
        ]
    )
    if paper_sound_duration and paper_sound_duration > 0:
        cmd.extend(
            [
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        )
    else:
        print(f"⚠️  Paper sound effect missing for quote highlight clips: {PAPER_SOUND}")
        cmd.extend(
            [
                "-map",
                "[vout]",
                "-an",
            ]
        )
    cmd.extend(
        [
            "-r",
            str(FPS),
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4",
            "-pix_fmt",
            "yuva444p10le",
            str(output_path),
        ]
    )
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        print(f"Failed to merge quote layers for {output_path.name} (exit code {result.returncode})")
        return False
    return True


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be greater than 0.")
    csv_path = args.input_csv if args.input_csv else cq.find_latest_comparison_csv(cq.COMPARER_OUTPUT_DIR)
    output_dir = args.output_dir if args.output_dir else cq.OUTPUT_DIR
    header, rows = cq.load_csv(csv_path)
    header_map = cq.build_header_map(header)
    quotes, _ = cq.build_video_quotes(rows, header_map, frame_rate=args.frame_rate)
    if not quotes:
        print(f"No quotes found in {csv_path}.")
        return
    video_dir, success, failures = render_highlight_videos(quotes, csv_path.stem, output_dir)
    print(f"Source CSV      : {csv_path}")
    print(f"Quotes processed: {len(quotes)}")
    print(f"Highlight videos: {video_dir} (ok/fail={success}/{failures})")


if __name__ == "__main__":
    main()
