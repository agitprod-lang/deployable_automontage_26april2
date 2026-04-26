#!/usr/bin/env python3
"""
Whip-style video assembler powered by ffmpeg.

Designed for the swisser workflow: every supported image downloaded into
`swisser/download/insert` is converted into an individual clip where the art
whips upward from below the frame. The script then concatenates those clips
into a single video, just like animator.py, but pointed at the swisser assets
out of the box.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CODE_VIDEO_ROOT = PROJECT_ROOT.parent
SWISSER_INSERT_DIR = CODE_VIDEO_ROOT / "swisser" / "download" / "insert"


SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MIN_TRAVEL_SEGMENT = 0.05
DEFAULT_CANVAS_WIDTH = 1920
DEFAULT_CANVAS_HEIGHT = 1080
IMAGE_ASPECT_TARGET = 16 / 9
IMAGE_ASPECT_TOLERANCE = 0.18  # Accept ~±18% drift from 16:9 before overscan fades
IMAGE_OVERSCAN_RATIO = 1.10
VERTICAL_ASPECT_THRESHOLD = 0.9
VERTICAL_MARGIN_RATIO = 0.05
VERTICAL_LEFT_MARGIN_RATIO = 0.02
IMAGE_SCALE_MULTIPLIER = 1.25
IMAGE_CENTER_Y_RATIO = 0.92
TITRE_WIDTH_RATIO = 0.78
TITRE_MAX_HEIGHT_RATIO = 0.2
TITRE_Y_RATIO = 0.78
TITRE_SCALE_MULTIPLIER = 1.95
LOGO_MAX_HEIGHT_RATIO = 0.09
LOGO_MAX_WIDTH_RATIO = 0.2
LOGO_Y_RATIO = 0.52
LOGO_SCALE_MULTIPLIER = 0.9
UPSCALE_OUTPUT_WIDTH = 3840
UPSCALE_OUTPUT_HEIGHT = 2160


@dataclass
class ClipLayout:
    width: int
    height: int
    landing_x: float
    landing_y: float


def sanitize_stem(stem: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    return safe or "clip"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render whip-in animations for the images downloaded by swisser."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=SWISSER_INSERT_DIR,
        help=f"Folder containing images (default: {SWISSER_INSERT_DIR}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output",
        help="Folder for video output.",
    )
    parser.add_argument(
        "--output-name",
        default="whip_sequence.mov",
        help="Name for the concatenated video saved in --output-dir (used with --concat).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=7.0,
        help="Seconds allocated to each clip (entry + hold + exit, default: 7.0).",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=5.0,
        help="Seconds to hold the landing pose before exiting (default: 5.0).",
    )
    parser.add_argument(
        "--overshoot",
        type=float,
        default=1.2,
        help="Overshoot factor for the whip easing curve (default: 1.2).",
    )
    parser.add_argument(
        "--blur-window",
        type=float,
        default=0.55,
        help="Seconds at the start of the move where motion blur is visible (default: 0.55).",
    )
    parser.add_argument(
        "--blur-strength",
        type=float,
        default=18.0,
        help="Gaussian blur strength while the whip is in motion (default: 18).",
    )
    parser.add_argument(
        "--blur-opacity",
        type=float,
        default=0.45,
        help="Opacity applied to the blurred smear layer (0-1, default: 0.45).",
    )
    parser.add_argument(
        "--blur-offset",
        type=float,
        default=75.0,
        help="Vertical offset (pixels) applied to the blur trail to mimic whipping (default: 75).",
    )
    parser.add_argument("--fps", type=int, default=30, help="Output frame rate for intermediate clips (default: 30).")
    parser.add_argument(
        "--background",
        default="#000000",
        help="Background color (#RRGGBB or r,g,b) for the generated video (default: #000000).",
    )
    parser.add_argument(
        "--alpha-output",
        dest="alpha_output",
        action="store_true",
        default=True,
        help="Encode clips with a transparent background (ProRes 4444). Enabled by default.",
    )
    parser.add_argument(
        "--opaque-output",
        dest="alpha_output",
        action="store_false",
        help="Disable transparency and flatten onto --background instead.",
    )
    parser.add_argument(
        "--max-blur-frames",
        type=int,
        default=6,
        help="Clamp the motion blur accumulation window (default: 6 frames) to avoid huge memory spikes.",
    )
    parser.add_argument(
        "--canvas-width",
        type=int,
        default=None,
        help="Optional override for the canvas width. Falls back to the max input width.",
    )
    parser.add_argument(
        "--canvas-height",
        type=int,
        default=None,
        help="Optional override for the canvas height. Falls back to the max input height.",
    )
    parser.add_argument("--concat", action="store_true", help="Concatenate all rendered clips into --output-name.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep per-image renders after concatenation.")
    parser.add_argument(
        "--replace-sources",
        dest="replace_sources",
        action="store_true",
        help="Overwrite the source images with the rendered animations.",
    )
    parser.add_argument(
        "--keep-sources",
        dest="replace_sources",
        action="store_false",
        help="Keep the source images untouched (clips stay inside --output-dir).",
    )
    parser.set_defaults(replace_sources=None)
    return parser.parse_args()


def ensure_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required binary '{name}' is not available on PATH.")


def load_images(input_dir: Path, fallback_dir: Path | None = None) -> List[Path]:
    search_dirs = [input_dir]
    if fallback_dir:
        search_dirs.append(fallback_dir)

    files: List[Path] = []
    for directory in search_dirs:
        if not directory.exists():
            continue
        files = sorted(p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS and p.is_file())
        if files:
            break

    if not files:
        raise FileNotFoundError(
            f"No supported image files found in {input_dir}"
            + (f" or {fallback_dir}" if fallback_dir else "")
            + "."
        )
    return files


def ffprobe_size(image_path: Path) -> Tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(image_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    width_str, height_str = result.stdout.strip().split("x")
    return int(width_str), int(height_str)


def ensure_even(value: int) -> int:
    corrected = int(round(value))
    if corrected % 2:
        corrected -= 1
    return max(corrected, 2)


def scale_with_canvas_limit(orig_w: int, orig_h: int, scale_factor: float, canvas_w: int, canvas_h: int) -> Tuple[int, int]:
    """
    Apply a scale factor and clamp the result so the asset never exceeds the canvas dimensions.
    """
    scaled_w = orig_w * scale_factor
    scaled_h = orig_h * scale_factor
    if scaled_w > canvas_w or scaled_h > canvas_h:
        width_fit = canvas_w / scaled_w if scaled_w else 1.0
        height_fit = canvas_h / scaled_h if scaled_h else 1.0
        clamp_factor = max(min(width_fit, height_fit), 0.01)
        scale_factor *= clamp_factor
        scaled_w = orig_w * scale_factor
        scaled_h = orig_h * scale_factor
    return ensure_even(scaled_w), ensure_even(scaled_h)


def compute_canvas(
    images: Sequence[Path],
    override_w: int | None,
    override_h: int | None,
) -> Tuple[int, int, List[Tuple[int, int]]]:
    raw_sizes = [ffprobe_size(path) for path in images]
    base_w = override_w if override_w else DEFAULT_CANVAS_WIDTH
    base_h = override_h if override_h else DEFAULT_CANVAS_HEIGHT
    width = ensure_even(base_w)
    height = ensure_even(base_h)
    return width, height, raw_sizes


def detect_label(image_path: Path) -> str | None:
    stem = image_path.stem.lower()
    before_tag = stem.split("@", 1)[0]
    parts = [segment for segment in before_tag.split("_") if segment]
    for token in parts[1:]:
        if token in {"image", "titre", "logo", "extrait"}:
            return token
    if "@" in stem and len(parts) <= 1:
        return "raw"
    return None


def compute_layout(orig_w: int, orig_h: int, canvas_w: int, canvas_h: int, label: str | None) -> ClipLayout:
    if orig_w <= 0 or orig_h <= 0:
        return ClipLayout(width=canvas_w, height=canvas_h, landing_x=0.0, landing_y=0.0)
    label_type = label or "image"
    aspect = orig_w / orig_h
    if label_type == "titre":
        target_width = canvas_w * TITRE_WIDTH_RATIO
        max_height = canvas_h * TITRE_MAX_HEIGHT_RATIO
        scale_w = target_width / orig_w
        scale_h = max_height / orig_h
        scale_factor = max(min(scale_w, scale_h), 0.01) * TITRE_SCALE_MULTIPLIER
        scaled_w, scaled_h = scale_with_canvas_limit(orig_w, orig_h, scale_factor, canvas_w, canvas_h)
        landing_x = (canvas_w - scaled_w) / 2
        center_y = canvas_h * TITRE_Y_RATIO
        landing_y = center_y - scaled_h / 2
        return ClipLayout(scaled_w, scaled_h, landing_x, landing_y)
    if label_type == "logo":
        target_height = canvas_h * LOGO_MAX_HEIGHT_RATIO
        max_width = canvas_w * LOGO_MAX_WIDTH_RATIO
        scale_h = target_height / orig_h
        scale_w = max_width / orig_w
        scale_factor = max(min(scale_h, scale_w), 0.01) * LOGO_SCALE_MULTIPLIER
        scaled_w, scaled_h = scale_with_canvas_limit(orig_w, orig_h, scale_factor, canvas_w, canvas_h)
        landing_x = (canvas_w - scaled_w) / 2
        center_y = canvas_h * LOGO_Y_RATIO
        landing_y = center_y - scaled_h / 2
        return ClipLayout(scaled_w, scaled_h, landing_x, landing_y)
    treat_as_vertical = label_type == "raw" or aspect < VERTICAL_ASPECT_THRESHOLD
    if treat_as_vertical:
        top_bottom_margin = canvas_h * VERTICAL_MARGIN_RATIO
        left_margin = canvas_w * VERTICAL_LEFT_MARGIN_RATIO
        right_margin = canvas_w * VERTICAL_MARGIN_RATIO
        available_height = max(canvas_h - 2 * top_bottom_margin, 2.0)
        available_width = max(canvas_w - left_margin - right_margin, 2.0)
        scale_h = available_height / orig_h
        scale_w = available_width / orig_w
        scale_factor = max(min(scale_h, scale_w), 0.01)
        scaled_w = ensure_even(orig_w * scale_factor)
        scaled_h = ensure_even(orig_h * scale_factor)
        # Stick vertical assets to the left with breathing room on the other sides.
        landing_x = left_margin
        landing_y = (canvas_h - scaled_h) / 2
        return ClipLayout(scaled_w, scaled_h, landing_x, landing_y)
    overscan = (
        IMAGE_OVERSCAN_RATIO
        if abs(aspect - IMAGE_ASPECT_TARGET) <= IMAGE_ASPECT_TARGET * IMAGE_ASPECT_TOLERANCE
        else 1.0
    )
    fill_ratio = max(canvas_w / orig_w, canvas_h / orig_h)
    scale_factor = max(fill_ratio * overscan, 0.01) * IMAGE_SCALE_MULTIPLIER
    scaled_w = ensure_even(orig_w * scale_factor)
    scaled_h = ensure_even(orig_h * scale_factor)
    landing_x = (canvas_w - scaled_w) / 2
    center_y = canvas_h * IMAGE_CENTER_Y_RATIO
    landing_y = center_y - scaled_h / 2
    landing_y = min(max(landing_y, 0.0), max(canvas_h - scaled_h, 0.0))
    return ClipLayout(scaled_w, scaled_h, landing_x, landing_y)


def parse_color(value: str) -> str:
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
        if len(value) != 6:
            raise ValueError("Background hex color must follow #RRGGBB.")
        return f"0x{value}"
    parts = [segment.strip() for segment in value.split(",")]
    if len(parts) != 3:
        raise ValueError("Background color must be #RRGGBB or r,g,b.")
    rgb = [int(part) for part in parts]
    if any(not (0 <= component <= 255) for component in rgb):
        raise ValueError("RGB components must fall between 0 and 255.")
    return f"0x{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def build_filter(
    scaled_w: int,
    scaled_h: int,
    canvas_w: int,
    canvas_h: int,
    landing_x: float,
    landing_y: float,
    clip_duration: float,
    hold: float,
    overshoot: float,
    background_color: str,
    blur_window: float,
    blur_strength: float,
    blur_opacity: float,
    blur_offset: float,
    blur_frames: int,
    fps: int,
    final_pix_fmt: str,
    transparent_output: bool,
) -> str:
    min_segment = MIN_TRAVEL_SEGMENT
    start_y = canvas_h + max(scaled_h * 0.2, 24.0)
    max_hold = max(clip_duration - 2 * min_segment, 0.0)
    hold_time = min(max(hold, 0.0), max_hold)
    move_window = clip_duration - hold_time
    travel_up = max(move_window * 0.5, min_segment)
    travel_down = max(move_window - travel_up, min_segment)
    exit_offset = travel_up + hold_time
    progress_up = f"min(max(t/{travel_up:.6f},0),1)"
    up_expr = f"({progress_up}-1)"
    ease_up = f"(pow({up_expr},2)*(({overshoot + 1:.4f})*{up_expr}+{overshoot:.4f})+1)"
    ascend_expr = f"{start_y:.4f}+({landing_y:.4f}-{start_y:.4f})*{ease_up}"
    progress_down = f"min(max((t-{exit_offset:.6f})/{travel_down:.6f},0),1)"
    ease_down = f"(pow({progress_down},2)*(({overshoot + 1:.4f})*{progress_down}-{overshoot:.4f}))"
    descend_expr = f"{landing_y:.4f}+({start_y:.4f}-{landing_y:.4f})*{ease_down}"
    dynamic_y = (
        f"if(lt(t,{travel_up:.6f}),{ascend_expr},"
        f"if(lt(t,{exit_offset:.6f}),{landing_y:.4f},{descend_expr}))"
    )
    blur_stop = min(max(blur_window, 0.05), clip_duration)
    blur_frames = max(2, blur_frames)
    weights = " ".join(str(i + 1) for i in range(blur_frames))
    blur_kernel_v = max(int(round(blur_strength)), 1)
    if blur_kernel_v % 2 == 0:
        blur_kernel_v += 1
    blur_kernel_h = max(int(round(max(blur_strength * 0.25, 1.0))), 1)
    if blur_kernel_h % 2 == 0:
        blur_kernel_h += 1
    blur_alpha = min(max(blur_opacity, 0.0), 1.0)
    soft_kernel_v = max(blur_kernel_v // 2, 1)
    if soft_kernel_v % 2 == 0:
        soft_kernel_v += 1
    soft_kernel_h = max(blur_kernel_h // 2, 1)
    if soft_kernel_h % 2 == 0:
        soft_kernel_h += 1
    blend_expr = (
        f"if(lte(T,{blur_stop:.6f}),A*(1-(T/{blur_stop:.6f}))+B*(T/{blur_stop:.6f}),B)"
    )
    bg_color_expr = (
        "black@0.0"
        if transparent_output
        else background_color
    )
    filter_graph = (
        f"[0:v]format=rgba,scale={scaled_w}:{scaled_h},setpts=PTS-STARTPTS,split=3[sharp][trail][soft];"
        f"[trail]avgblur=sizeX={blur_kernel_h}:sizeY={blur_kernel_v},setpts=PTS-STARTPTS[trailblur];"
        f"color=color=black@0.0:size={canvas_w}x{canvas_h}:duration={clip_duration:.6f}:rate={fps},format=rgba[blank];"
        f"[blank][trailblur]overlay=x={landing_x:.4f}:y='({dynamic_y})+{blur_offset:.4f}':shortest=1:format=auto[blurtrack];"
        f"[blurtrack]tmix=frames={blur_frames}:weights='{weights}',setpts=PTS-STARTPTS[blurmix];"
        f"[blurmix]colorchannelmixer=aa={blur_alpha:.3f}[blurredtrail];"
        f"color=color={bg_color_expr}:size={canvas_w}x{canvas_h}:duration={clip_duration:.6f}:rate={fps},format=rgba[bg];"
        f"[bg][blurredtrail]overlay=shortest=1:format=auto:enable='lt(t,{blur_stop:.6f})'[prepped];"
        f"[soft]avgblur=sizeX={soft_kernel_h}:sizeY={soft_kernel_v},setpts=PTS-STARTPTS[softened];"
        f"[softened][sharp]blend=all_expr='{blend_expr}'[animated];"
        f"[prepped][animated]overlay=x={landing_x:.4f}:y='{dynamic_y}':shortest=1,format={final_pix_fmt}[out]"
    )
    return filter_graph


def upscale_clip(clip_path: Path, target_w: int, target_h: int, alpha_output: bool) -> None:
    """
    Upscale the rendered clip to the final delivery resolution so the heavy whip animation can render faster.
    """
    temp_path = clip_path.with_name(f"{clip_path.stem}_4ktemp{clip_path.suffix}")
    if temp_path.exists():
        temp_path.unlink()
    codec = "prores_ks"
    codec_args: List[str] = ["-profile:v", "4"]
    pix_fmt = "yuva444p10le"
    if not alpha_output:
        codec = "libx264"
        codec_args = ["-preset", "medium", "-crf", "18"]
        pix_fmt = "yuv420p"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(clip_path),
        "-vf",
        f"scale={target_w}:{target_h}",
        "-c:v",
        codec,
        "-pix_fmt",
        pix_fmt,
        *codec_args,
        "-movflags",
        "+faststart",
        str(temp_path),
    ]
    print(f"Upscaling {clip_path.name} to {target_w}x{target_h}")
    subprocess.run(cmd, check=True)
    clip_path.unlink(missing_ok=True)
    temp_path.replace(clip_path)


def run_ffmpeg_clip(
    image_path: Path,
    clip_path: Path,
    layout: ClipLayout,
    canvas_size: Tuple[int, int],
    args: argparse.Namespace,
    bg_color: str,
) -> None:
    sw, sh = layout.width, layout.height
    cw, ch = canvas_size
    blur_frames = max(2, min(60, int(round(args.fps * args.blur_window))))
    pix_fmt = "yuva444p10le" if args.alpha_output else "yuv420p"
    clip_duration = max(args.duration, MIN_TRAVEL_SEGMENT * 2)
    filter_graph = build_filter(
        sw,
        sh,
        cw,
        ch,
        layout.landing_x,
        layout.landing_y,
        clip_duration,
        args.hold,
        args.overshoot,
        bg_color,
        args.blur_window,
        args.blur_strength,
        args.blur_opacity,
        args.blur_offset,
        blur_frames,
        args.fps,
        pix_fmt,
        args.alpha_output,
    )
    codec = "prores_ks" if args.alpha_output else "libx264"
    codec_args: List[str]
    if args.alpha_output:
        codec_args = ["-profile:v", "4"]
    else:
        codec_args = ["-preset", "medium", "-crf", "18"]
    cmd = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(args.fps),
        "-i",
        str(image_path),
        "-filter_complex",
        filter_graph,
        "-map",
        "[out]",
        "-t",
        f"{clip_duration:.6f}",
        "-c:v",
        codec,
        "-pix_fmt",
        pix_fmt,
        *codec_args,
        "-movflags",
        "+faststart",
        str(clip_path),
    ]
    print(f"Rendering whip clip for {image_path.name} -> {clip_path.name}")
    subprocess.run(cmd, check=True)


def concat_clips(clips: Sequence[Path], destination: Path, cleanup_sources: bool) -> None:
    concat_file = destination.parent / "_tmp_whip_concat.txt"
    with concat_file.open("w", encoding="utf-8") as handle:
        for clip in clips:
            safe_path = clip.resolve().as_posix().replace("'", r"'\''")
            handle.write(f"file '{safe_path}'\n")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(destination),
    ]
    print(f"Concatenating {len(clips)} clip(s) into {destination}")
    subprocess.run(cmd, check=True)
    concat_file.unlink(missing_ok=True)
    if cleanup_sources:
        for clip in clips:
            clip.unlink(missing_ok=True)


def replace_image_with_clip(image_path: Path, clip_path: Path) -> Path:
    """
    Delete the original image and move the rendered clip next to it, preserving the stem.
    """
    final_path = image_path.with_suffix(clip_path.suffix)
    image_path.unlink(missing_ok=True)
    if final_path.exists():
        final_path.unlink()
    clip_path.replace(final_path)
    return final_path


def main() -> None:
    args = parse_arguments()
    if args.replace_sources is None:
        try:
            args.replace_sources = args.input_dir.resolve() == SWISSER_INSERT_DIR.resolve()
        except FileNotFoundError:
            args.replace_sources = False
    ensure_binary("ffmpeg")
    ensure_binary("ffprobe")
    images = load_images(args.input_dir)
    canvas_w, canvas_h, sizes = compute_canvas(images, args.canvas_width, args.canvas_height)
    bg_color = parse_color(args.background)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    canvas_size = (canvas_w, canvas_h)
    concat_inputs: List[Path] = []
    clip_suffix = ".mov" if args.alpha_output else ".mp4"
    rendered_count = 0
    for idx, (image_path, raw_size) in enumerate(zip(images, sizes)):
        label = detect_label(image_path)
        if label == "extrait":
            print(f"Skipping extrait asset {image_path.name}; leaving original image untouched.")
            continue
        layout = compute_layout(raw_size[0], raw_size[1], canvas_w, canvas_h, label)
        temp_stem = sanitize_stem(image_path.stem)
        clip_path = args.output_dir / f"{idx:03d}_{temp_stem}{clip_suffix}"
        run_ffmpeg_clip(image_path, clip_path, layout, canvas_size, args, bg_color)
        upscale_clip(clip_path, UPSCALE_OUTPUT_WIDTH, UPSCALE_OUTPUT_HEIGHT, args.alpha_output)
        final_clip = clip_path
        if args.replace_sources:
            final_clip = replace_image_with_clip(image_path, clip_path)
            print(f"Replaced {image_path.name} with {final_clip.name} in {final_clip.parent}")
        if args.concat:
            concat_inputs.append(final_clip)
        rendered_count += 1

    if args.concat:
        if not concat_inputs:
            raise RuntimeError("No clips were rendered; cannot concatenate.")
        final_path = args.output_dir / args.output_name
        concat_clips(concat_inputs, final_path, cleanup_sources=not args.keep_temp)
        print(f"Concatenated sequence saved to {final_path}")
    else:
        print(f"Rendered {rendered_count} clip(s) into {args.output_dir}")


if __name__ == "__main__":
    main()
