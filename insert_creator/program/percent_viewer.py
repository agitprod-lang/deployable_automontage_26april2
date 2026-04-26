#!/usr/bin/env python3
"""Create transparent percent pie overlays from the universal comparser CSV output."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Set


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
PERCENT_SOUND_PATH = PROJECT_ROOT / "asset" / "sounds" / "soundeffectforpercent.mp3"
PERCENT_SOUND_VOLUME_DB = -10.0

VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080
DEFAULT_FRAME_RATE = 25.0
DEFAULT_DURATION = 5.0
DEFAULT_FILL_DURATION = 1.5
DEFAULT_PIE_RADIUS = 150
DEFAULT_OFFSET_X = 250
DEFAULT_OFFSET_Y = 540
DEFAULT_FONT_SIZE = 135
DEFAULT_FONT_GAP = 30
DEFAULT_NUMBER_HOLD_DURATION = 3.0
DEFAULT_NUMBER_FADE_DURATION = 0.35
DEFAULT_SHADOW_OFFSET_X = 20
DEFAULT_SHADOW_OFFSET_Y = 22
DEFAULT_SHADOW_BLUR = 26
DEFAULT_SHADOW_ALPHA = 72
DEFAULT_GLOW_SPREAD = 48
DEFAULT_GLOW_ALPHA = 150
DEFAULT_FILL_COLOR = (246, 181, 50, 205)
DEFAULT_BG_COLOR = (30, 35, 40, 44)
DEFAULT_RING_COLOR = (255, 255, 255, 150)
DEFAULT_TEXT_COLOR = "0xffffff"
DEFAULT_TEXT_SHADOW = "0x000000d0"
DEFAULT_TEXT_BORDER = "0x00000050"

IMPACT_PATHS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Impact.ttf",
]
POPPINS_FALLBACK = "/Users/mathieusandana/Library/Fonts/Poppins-Bold.ttf"
SYSTEM_FALLBACKS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/Library/Fonts/Arial.ttf",
]
PERCENT_PATTERN = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*%")


@dataclass
class PercentEntry:
    raw_value: str
    normalized_percent: float
    display_text: str
    row_index: int
    transcript_number: str | None
    start_timecode: str | None
    end_timecode: str | None
    start_seconds: float | None
    end_seconds: float | None
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render transparent percent pie overlays from the Percent Mention column "
            "in the latest universal comparser CSV."
        )
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to *_comparison*.csv (defaults to the most recent file under Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for generated percent videos (defaults to insert_creator/output).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=DEFAULT_FRAME_RATE,
        help=f"Frame rate for parsing HH:MM:SS:FF timecodes and encoding the result (default: {DEFAULT_FRAME_RATE}).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help=f"Total clip duration in seconds (default: {DEFAULT_DURATION}).",
    )
    parser.add_argument(
        "--fill-duration",
        type=float,
        default=DEFAULT_FILL_DURATION,
        help=f"Seconds spent filling the pie before the hold (default: {DEFAULT_FILL_DURATION}).",
    )
    parser.add_argument(
        "--pie-radius",
        type=int,
        default=DEFAULT_PIE_RADIUS,
        help=f"Pie radius in pixels (default: {DEFAULT_PIE_RADIUS}).",
    )
    parser.add_argument(
        "--offset-x",
        type=int,
        default=DEFAULT_OFFSET_X,
        help=f"Horizontal inset from the left for the pie center (default: {DEFAULT_OFFSET_X}).",
    )
    parser.add_argument(
        "--offset-y",
        type=int,
        default=DEFAULT_OFFSET_Y,
        help=f"Vertical inset from the bottom for the pie center (default: {DEFAULT_OFFSET_Y}).",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=DEFAULT_FONT_SIZE,
        help=f"Font size for the percent label (default: {DEFAULT_FONT_SIZE}).",
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
            raise RuntimeError(f"{path} does not contain any rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, column in enumerate(header):
        mapping[column.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' is missing from the CSV.")
    return header_map[key]


def split_multi_value(value: str | None) -> List[str]:
    if not value:
        return []
    values: List[str] = []
    for fragment in value.split("|"):
        cleaned = fragment.strip().strip('"').strip()
        if cleaned:
            values.append(cleaned)
    return values


def parse_timecode(value: str | None, frame_rate: float) -> float | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    parts = candidate.split(":")
    try:
        if len(parts) == 4:
            hours, minutes, seconds, frames = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds + frames / frame_rate
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


def find_font_file() -> str:
    for path in IMPACT_PATHS:
        if Path(path).exists():
            return path
    if Path(POPPINS_FALLBACK).exists():
        return POPPINS_FALLBACK
    for path in SYSTEM_FALLBACKS:
        if Path(path).exists():
            return path
    return "/System/Library/Fonts/Helvetica.ttc"


def prepare_output_dirs(base_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    base_output = base_dir / f"{stem}_percent_media"
    video_dir = base_output / "videos"
    manifest_path = base_output / f"{stem}_percent_manifest.json"
    base_output.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    return base_output, video_dir, manifest_path


def normalize_percent_value(value: str) -> float | None:
    match = PERCENT_PATTERN.search(value)
    if not match:
        return None
    raw = match.group(1).replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return None
    return max(0.0, min(100.0, number))


def format_percent_display(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return f"{int(rounded)}%"
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


def collect_percent_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
) -> List[PercentEntry]:
    percent_idx = require_column(header_map, "Percent Mention")
    start_idx = require_column(header_map, "Start Time")
    end_idx = require_column(header_map, "End Time")
    text_idx = require_column(header_map, "Text")
    transcript_idx = require_column(header_map, "Transcript #")
    entries: List[PercentEntry] = []
    for row_number, row in enumerate(rows, start=1):
        percent_cell = row[percent_idx] if percent_idx < len(row) else ""
        values = split_multi_value(percent_cell)
        if not values:
            continue
        start_time = row[start_idx] if start_idx < len(row) else ""
        end_time = row[end_idx] if end_idx < len(row) else ""
        text_value = row[text_idx] if text_idx < len(row) else ""
        transcript_number = row[transcript_idx] if transcript_idx < len(row) else ""
        seen_values: Set[float] = set()
        for value in values:
            normalized = normalize_percent_value(value)
            if normalized is None:
                continue
            rounded_key = round(normalized, 3)
            if rounded_key in seen_values:
                continue
            seen_values.add(rounded_key)
            entries.append(
                PercentEntry(
                    raw_value=value,
                    normalized_percent=normalized,
                    display_text=format_percent_display(normalized),
                    row_index=row_number,
                    transcript_number=transcript_number.strip() or None,
                    start_timecode=start_time.strip() or None,
                    end_timecode=end_time.strip() or None,
                    start_seconds=parse_timecode(start_time, frame_rate),
                    end_seconds=parse_timecode(end_time, frame_rate),
                    text=text_value,
                )
            )
    return entries


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def escape_filter_text(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    return escaped


def escape_filter_expr(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:")
    escaped = escaped.replace("'", "\\'")
    escaped = escaped.replace(",", "\\,")
    escaped = escaped.replace("%", "\\%")
    return escaped


def fill_ratio_at_time(time_seconds: float, target_percent: float, fill_duration: float) -> float:
    if target_percent <= 0:
        return 0.0
    progress = 1.0 if fill_duration <= 0 else min(1.0, max(0.0, time_seconds / fill_duration))
    return (target_percent / 100.0) * progress


def _set_pixel(buffer: bytearray, width: int, x: int, y: int, rgba: tuple[int, int, int, int]) -> None:
    idx = (y * width + x) * 4
    buffer[idx] = rgba[0]
    buffer[idx + 1] = rgba[1]
    buffer[idx + 2] = rgba[2]
    buffer[idx + 3] = rgba[3]


def _blend_pixel(buffer: bytearray, width: int, x: int, y: int, rgba: tuple[int, int, int, int]) -> None:
    idx = (y * width + x) * 4
    src_alpha = rgba[3] / 255.0
    if src_alpha <= 0.0:
        return
    dst_alpha = buffer[idx + 3] / 255.0
    out_alpha = src_alpha + dst_alpha * (1.0 - src_alpha)
    if out_alpha <= 0.0:
        return
    for channel in range(3):
        src = rgba[channel] / 255.0
        dst = buffer[idx + channel] / 255.0
        out = (src * src_alpha + dst * dst_alpha * (1.0 - src_alpha)) / out_alpha
        buffer[idx + channel] = round(out * 255.0)
    buffer[idx + 3] = round(out_alpha * 255.0)


def _lighten_color(rgba: tuple[int, int, int, int], amount: float) -> tuple[int, int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return (
        round(rgba[0] + (255 - rgba[0]) * amount),
        round(rgba[1] + (255 - rgba[1]) * amount),
        round(rgba[2] + (255 - rgba[2]) * amount),
        rgba[3],
    )


def _draw_radial_glow(
    buffer: bytearray,
    width: int,
    height: int,
    center_x: int,
    center_y: int,
    inner_radius: int,
    glow_spread: int,
    rgba: tuple[int, int, int, int],
    alpha_scale: float,
) -> None:
    if alpha_scale <= 0.0 or glow_spread <= 0:
        return

    outer_radius = inner_radius + glow_spread
    inner_squared = inner_radius * inner_radius
    outer_squared = outer_radius * outer_radius
    x_min = clamp(center_x - outer_radius, 0, width - 1)
    x_max = clamp(center_x + outer_radius, 0, width - 1)
    y_min = clamp(center_y - outer_radius, 0, height - 1)
    y_max = clamp(center_y + outer_radius, 0, height - 1)

    for y in range(y_min, y_max + 1):
        dy = y - center_y
        dy_squared = dy * dy
        for x in range(x_min, x_max + 1):
            dx = x - center_x
            dist_squared = dx * dx + dy_squared
            if dist_squared > outer_squared:
                continue

            if dist_squared <= inner_squared:
                falloff = 1.0
            else:
                dist = math.sqrt(dist_squared)
                edge = max(0.0, 1.0 - ((dist - inner_radius) / glow_spread))
                falloff = edge ** 1.6

            local_alpha = round(rgba[3] * alpha_scale * falloff)
            if local_alpha <= 0:
                continue
            _blend_pixel(buffer, width, x, y, (rgba[0], rgba[1], rgba[2], local_alpha))


def draw_pie_frame(
    width: int,
    height: int,
    center_x: int,
    center_y: int,
    radius: int,
    fill_ratio: float,
) -> bytes:
    frame = bytearray(width * height * 4)
    shadow_progress = max(0.0, min(1.0, fill_ratio))
    if shadow_progress > 0.0:
        shadow_center_x = center_x + DEFAULT_SHADOW_OFFSET_X
        shadow_center_y = center_y + DEFAULT_SHADOW_OFFSET_Y
        shadow_radius = radius + DEFAULT_SHADOW_BLUR
        shadow_radius_squared = shadow_radius * shadow_radius
        shadow_inner_radius = max(radius - 10, 1)
        shadow_inner_squared = shadow_inner_radius * shadow_inner_radius
        shadow_x_min = clamp(shadow_center_x - shadow_radius, 0, width - 1)
        shadow_x_max = clamp(shadow_center_x + shadow_radius, 0, width - 1)
        shadow_y_min = clamp(shadow_center_y - shadow_radius, 0, height - 1)
        shadow_y_max = clamp(shadow_center_y + shadow_radius, 0, height - 1)
        shadow_alpha_scale = DEFAULT_SHADOW_ALPHA * shadow_progress

        for y in range(shadow_y_min, shadow_y_max + 1):
            dy = y - shadow_center_y
            dy_squared = dy * dy
            for x in range(shadow_x_min, shadow_x_max + 1):
                dx = x - shadow_center_x
                dist_squared = dx * dx + dy_squared
                if dist_squared > shadow_radius_squared:
                    continue
                if dist_squared <= shadow_inner_squared:
                    local_alpha = shadow_alpha_scale
                else:
                    dist = math.sqrt(dist_squared)
                    edge = max(0.0, 1.0 - ((dist - shadow_inner_radius) / max(DEFAULT_SHADOW_BLUR, 1)))
                    local_alpha = shadow_alpha_scale * edge * edge
                _blend_pixel(frame, width, x, y, (0, 0, 0, round(local_alpha)))

        glow_color = _lighten_color(DEFAULT_FILL_COLOR, 0.34)
        _draw_radial_glow(
            frame,
            width,
            height,
            center_x,
            center_y,
            radius,
            DEFAULT_GLOW_SPREAD,
            (glow_color[0], glow_color[1], glow_color[2], DEFAULT_GLOW_ALPHA),
            shadow_progress,
        )

    x_min = clamp(center_x - radius - 6, 0, width - 1)
    x_max = clamp(center_x + radius + 6, 0, width - 1)
    y_min = clamp(center_y - radius - 6, 0, height - 1)
    y_max = clamp(center_y + radius + 6, 0, height - 1)
    radius_squared = radius * radius
    inner_ring_radius = max(radius - 6, 1)
    inner_ring_squared = inner_ring_radius * inner_ring_radius
    sweep = max(0.0, min(1.0, fill_ratio)) * math.tau

    for y in range(y_min, y_max + 1):
        dy = y - center_y
        dy_squared = dy * dy
        for x in range(x_min, x_max + 1):
            dx = x - center_x
            dist_squared = dx * dx + dy_squared
            if dist_squared > radius_squared:
                continue

            color = None
            if dist_squared <= inner_ring_squared:
                color = DEFAULT_BG_COLOR
            else:
                color = DEFAULT_RING_COLOR

            if sweep > 0.0:
                theta = (math.atan2(dy, dx) + math.pi / 2.0) % math.tau
                if theta <= sweep or math.isclose(theta, sweep, rel_tol=0.0, abs_tol=0.02):
                    color = DEFAULT_FILL_COLOR

            _set_pixel(frame, width, x, y, color)

    if sweep > 0.0:
        for y in range(y_min, y_max + 1):
            dy = y - center_y
            for x in range(x_min, x_max + 1):
                dx = x - center_x
                if dx * dx + dy * dy > radius_squared:
                    continue
                if abs(dx) <= 2 or abs(dy) <= 2:
                    theta = (math.atan2(dy, dx) + math.pi / 2.0) % math.tau
                    if theta <= sweep:
                        _set_pixel(frame, width, x, y, DEFAULT_FILL_COLOR)

    return bytes(frame)


def render_percent_video(
    entry: PercentEntry,
    output_path: Path,
    *,
    frame_rate: float,
    duration: float,
    fill_duration: float,
    radius: int,
    offset_x: int,
    offset_y: int,
    font_size: int,
) -> bool:
    font_file = find_font_file()
    center_x = clamp(offset_x, radius + 8, VIDEO_WIDTH - radius - 8)
    center_y = clamp(VIDEO_HEIGHT - offset_y, radius + 8, VIDEO_HEIGHT - radius - 8)
    fade_duration = min(DEFAULT_NUMBER_FADE_DURATION, duration / 2.0)
    fade_out_start = max(duration - fade_duration, 0.0)
    font_spec = f"fontfile='{font_file}':" if Path(font_file).exists() else ""
    alpha_expr = (
        f"if(lt(t\\,{fade_duration:.3f}),t/{fade_duration:.3f},"
        f"if(lt(t\\,{fade_out_start:.3f}),1,max(0,( {duration:.3f}-t )/{fade_duration:.3f})))"
    )
    drawtext = (
        "drawtext="
        f"{font_spec}"
        "expansion=none:"
        f"text='{escape_filter_text(entry.display_text)}':"
        f"fontsize={font_size}:"
        f"fontcolor={DEFAULT_TEXT_COLOR}:"
        f"alpha='{alpha_expr}':"
        "shadowx=8:"
        "shadowy=8:"
        f"shadowcolor={DEFAULT_TEXT_SHADOW}:"
        "borderw=6:"
        f"bordercolor={DEFAULT_TEXT_BORDER}:"
        f"x={center_x}-text_w/2:"
        f"y={center_y - radius - DEFAULT_FONT_GAP}-text_h"
    )
    command = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "-r",
        f"{frame_rate:g}",
        "-i",
        "-",
    ]
    if PERCENT_SOUND_PATH.exists():
        command.extend(
            [
                "-i",
                str(PERCENT_SOUND_PATH),
            ]
        )
    command.extend(
        [
            "-vf",
            drawtext,
        ]
    )
    if PERCENT_SOUND_PATH.exists():
        command.extend(
            [
                "-af",
                f"volume={PERCENT_SOUND_VOLUME_DB:.1f}dB,apad=pad_dur={duration:.3f},atrim=duration={duration:.3f},asetpts=PTS-STARTPTS",
                "-c:a",
                "pcm_s16le",
                "-shortest",
            ]
        )
    else:
        print(f"      Warning: percent sound file not found, rendering silent clip: {PERCENT_SOUND_PATH}")
        command.append("-an")
    command.extend(
        [
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            str(output_path),
        ]
    )
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    total_frames = max(1, int(round(duration * frame_rate)))
    try:
        assert process.stdin is not None
        for frame_index in range(total_frames):
            time_seconds = frame_index / frame_rate
            fill_ratio = fill_ratio_at_time(time_seconds, entry.normalized_percent, fill_duration)
            frame = draw_pie_frame(
                VIDEO_WIDTH,
                VIDEO_HEIGHT,
                center_x,
                center_y,
                radius,
                fill_ratio,
            )
            process.stdin.write(frame)
        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait()
    except BrokenPipeError:
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        process.wait()
        print(f"      FFmpeg error: {stderr.strip()[-500:]}")
        return False
    finally:
        if process.stderr:
            process.stderr.close()
    if return_code != 0:
        print(f"      FFmpeg error: {stderr.strip()[-500:]}")
        return False
    return True


def write_manifest(manifest_path: Path, records: Sequence[Dict[str, object]], source_csv: Path) -> None:
    manifest = {
        "source_csv": str(source_csv),
        "videos": records,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be positive.")
    if args.duration <= 0:
        raise ValueError("--duration must be positive.")
    if args.fill_duration < 0:
        raise ValueError("--fill-duration must be zero or positive.")
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    entries = collect_percent_entries(rows, header_map, args.frame_rate)
    if not entries:
        print("No Percent Mention entries were found in the CSV.")
        return
    output_dir = args.output_dir if args.output_dir else OUTPUT_DIR
    _, video_dir, manifest_path = prepare_output_dirs(output_dir, csv_path.stem)
    print(f"Rendering {len(entries)} percent overlays from {csv_path.name}...")
    records: List[Dict[str, object]] = []
    for idx, entry in enumerate(entries, start=1):
        output_path = video_dir / f"percent_{idx:03d}.mov"
        print(f"   📈 percent_{idx:03d}.mov ({entry.display_text})")
        success = render_percent_video(
            entry,
            output_path,
            frame_rate=args.frame_rate,
            duration=args.duration,
            fill_duration=min(args.fill_duration, args.duration),
            radius=args.pie_radius,
            offset_x=args.offset_x,
            offset_y=args.offset_y,
            font_size=args.font_size,
        )
        records.append(
            {
                "id": idx,
                "value": entry.raw_value,
                "normalized_percent": entry.normalized_percent,
                "display_text": entry.display_text,
                "video_path": str(output_path) if success else None,
                "row_index": entry.row_index,
                "transcript_number": entry.transcript_number,
                "start_timecode": entry.start_timecode,
                "end_timecode": entry.end_timecode,
                "start_seconds": entry.start_seconds,
                "end_seconds": entry.end_seconds,
                "source_text": entry.text,
                "success": success,
            }
        )
    write_manifest(manifest_path, records, csv_path)
    print("\nSummary")
    print("=" * 40)
    print(f"Source CSV : {csv_path}")
    print(f"Videos dir : {video_dir}")
    print(f"Manifest   : {manifest_path}")


if __name__ == "__main__":
    main()
