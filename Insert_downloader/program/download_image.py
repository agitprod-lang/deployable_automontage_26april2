#!/usr/bin/env python3.11
"""
Download an image from a URL, wrap it in a rounded polaroid-style frame,
and render an animated insert video.

Run with:
`python3.11 /Users/mathieusandana/Desktop/code/deployable_auto-montage/Insert_downloader/program/download_image.py "https://example.com/image.jpg"`
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from PIL import Image, ImageChops, ImageDraw, ImageFilter


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("insert_downloader.download_image")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
FLASH_MASK = SCRIPT_DIR / "mask" / "falsh_transparent_with_sound.mov"
PIN_IMAGE = SCRIPT_DIR / "mask" / "pin.png"

FFMPEG_BIN = os.environ.get("INSERT_DL_FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("INSERT_DL_FFPROBE_BIN", "ffprobe")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

OUTPUT_CANVAS_WIDTH = 1920
OUTPUT_CANVAS_HEIGHT = 1080
CARD_TARGET_WIDTH_RATIO = 0.64
CARD_TARGET_AREA_RATIO = 0.285
CARD_LEFT_MARGIN = 28
CARD_BOTTOM_MARGIN = 28
PHOTO_Y_OFFSET = -12
POP_IN_START = 1.05
POP_OUT_END = 5.45
OUTPUT_DURATION = 6.35
MAX_PHOTO_SIZE = (1040, 700)
FRAME_SIDE_PADDING = 18
FRAME_TOP_PADDING = 20
FRAME_BOTTOM_PADDING = 60
FRAME_RADIUS_RATIO = 0.06
PHOTO_RADIUS_RATIO = 0.055
FRAME_OUTLINE_WIDTH = 2
INNER_OUTLINE_WIDTH = 2
MASK_SCALE = 4
SHADOW_MARGIN = 96
SHADOW_OFFSET = (18, 24)
SHADOW_BLUR = 28
SHADOW_OPACITY = 0.24
PIN_TOP_OFFSET = -24
PIN_HEIGHT_RATIO = 0.14


@dataclass
class CardAssets:
    preview_path: Path
    photo_size: Tuple[int, int]
    photo_offset: Tuple[int, int]
    canvas_size: Tuple[int, int]
    mask_path: Path
    visible_size: Tuple[int, int]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download an image URL, add a rounded polaroid frame, and render "
            "a paper-style animated insert video."
        )
    )
    parser.add_argument("image_url", help="Direct image URL to download.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"Folder for downloaded image, processed PNG, and video. Default: {OUTPUT_DIR}",
    )
    parser.add_argument(
        "--skip-video",
        action="store_true",
        help="Only download and process the image; do not render the .mov.",
    )
    return parser.parse_args(argv)


def _ffmpeg_bin() -> str:
    if shutil.which(FFMPEG_BIN):
        return FFMPEG_BIN
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    raise FileNotFoundError("ffmpeg binary not found on PATH.")


def _ffprobe_bin() -> str:
    if shutil.which(FFPROBE_BIN):
        return FFPROBE_BIN
    if shutil.which("ffprobe"):
        return "ffprobe"
    raise FileNotFoundError("ffprobe binary not found on PATH.")


def _run_ffmpeg(cmd: Iterable[str]) -> None:
    try:
        subprocess.run(list(cmd), check=True, capture_output=False)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}") from exc


def _probe_duration_seconds(media_path: Path) -> float:
    ffprobe = _ffprobe_bin()
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(media_path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout or "{}")
        duration = float(payload.get("format", {}).get("duration"))
    except (subprocess.CalledProcessError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not determine media duration for {media_path}") from exc
    if duration <= 0:
        raise RuntimeError(f"Media duration must be positive for {media_path}")
    return duration


def slugify_filename(image_url: str) -> str:
    parsed = urlparse(image_url)
    raw_name = Path(unquote(parsed.path)).stem or "downloaded_image"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
    return slug or "downloaded_image"


def guess_extension(image_url: str, content_type: str) -> str:
    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed == ".jpe":
        guessed = ".jpg"
    return guessed or ".jpg"


def download_image(image_url: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    request = Request(image_url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        payload = response.read()
        content_type = response.headers.get("Content-Type", "")
    if not payload:
        raise RuntimeError(f"No data downloaded from {image_url}")
    stem = slugify_filename(image_url)
    extension = guess_extension(image_url, content_type)
    destination = output_dir / f"{stem}_downloaded{extension}"
    destination.write_bytes(payload)
    logger.info("Downloaded image to %s", destination)
    return destination


def rounded_mask(size: Tuple[int, int], radius: int) -> Image.Image:
    width, height = size
    scale = MASK_SCALE
    large_size = (width * scale, height * scale)
    mask = Image.new("L", large_size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, large_size[0] - 1, large_size[1] - 1),
        radius=radius * scale,
        fill=255,
    )
    return mask.resize(size, Image.Resampling.LANCZOS)


def resolve_corner_radius(width: int, height: int, value: int) -> int:
    shortest = min(width, height)
    return max(1, min(value, shortest // 2))


def resolve_radius_ratio(width: int, height: int, ratio: float, minimum: int) -> int:
    shortest = min(width, height)
    return max(minimum, int(round(shortest * ratio)))


def apply_shadow(
    base: Image.Image,
    mask: Image.Image,
    offset: Tuple[int, int],
    blur_radius: int,
    opacity: float,
) -> Image.Image:
    width, height = base.size
    pad = blur_radius * 2 + max(abs(offset[0]), abs(offset[1]))
    canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), (0, 0, 0, 0))

    shadow = Image.new("RGBA", base.size, (0, 0, 0, int(255 * opacity)))
    shadow_only = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_only.paste(shadow, mask=mask)
    shadow_only = shadow_only.filter(ImageFilter.GaussianBlur(blur_radius))

    shadow_pos = (pad + offset[0], pad + offset[1])
    image_pos = (pad, pad)
    canvas.alpha_composite(shadow_only, shadow_pos)
    canvas.alpha_composite(base, image_pos)
    return canvas


def _contain(image: Image.Image, max_size: Tuple[int, int]) -> Image.Image:
    image = image.copy()
    image.thumbnail(max_size, Image.Resampling.LANCZOS)
    return image


def _scaled_pin(frame_height: int) -> Optional[Image.Image]:
    if not PIN_IMAGE.exists():
        return None
    with Image.open(PIN_IMAGE) as opened:
        pin = opened.convert("RGBA")
    target_height = max(40, int(round(frame_height * PIN_HEIGHT_RATIO)))
    target_width = max(2, int(round(pin.width * target_height / pin.height)))
    return pin.resize((target_width, target_height), Image.Resampling.LANCZOS)


def build_polaroid_assets(source_path: Path, output_dir: Path) -> CardAssets:
    output_dir.mkdir(parents=True, exist_ok=True)
    mask_output = output_dir / f"{source_path.stem}_photo_mask.png"
    preview_output = output_dir / f"{source_path.stem}_polaroid.png"

    with Image.open(source_path) as opened:
        source = _contain(opened.convert("RGBA"), MAX_PHOTO_SIZE)

    photo_radius = resolve_radius_ratio(source.width, source.height, PHOTO_RADIUS_RATIO, 18)
    photo_mask = rounded_mask(source.size, photo_radius)
    rounded_photo = source.copy()
    rounded_alpha = rounded_photo.getchannel("A")
    rounded_photo.putalpha(ImageChops.multiply(rounded_alpha, photo_mask))

    frame_width = source.width + FRAME_SIDE_PADDING * 2
    frame_height = source.height + FRAME_TOP_PADDING + FRAME_BOTTOM_PADDING
    canvas_width = frame_width + SHADOW_MARGIN * 2
    canvas_height = frame_height + SHADOW_MARGIN * 2
    card_left = SHADOW_MARGIN
    card_top = SHADOW_MARGIN
    photo_left = card_left + FRAME_SIDE_PADDING
    photo_top = card_top + FRAME_TOP_PADDING

    frame_radius = resolve_radius_ratio(frame_width, frame_height, FRAME_RADIUS_RATIO, 24)
    frame_fill = Image.new("RGBA", (frame_width, frame_height), (0, 0, 0, 0))
    frame_fill_draw = ImageDraw.Draw(frame_fill)
    frame_fill_draw.rounded_rectangle(
        (0, 0, frame_width - 1, frame_height - 1),
        radius=frame_radius,
        fill=(255, 255, 255, 255),
        outline=(230, 230, 230, 255),
        width=FRAME_OUTLINE_WIDTH,
    )
    frame_fill_mask = rounded_mask((frame_width, frame_height), frame_radius)
    frame_fill.putalpha(frame_fill_mask)

    shadowed_frame = apply_shadow(
        frame_fill,
        frame_fill_mask,
        offset=SHADOW_OFFSET,
        blur_radius=SHADOW_BLUR,
        opacity=SHADOW_OPACITY,
    )

    frame_layer = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    shadow_left = card_left - (SHADOW_BLUR * 2 + max(abs(SHADOW_OFFSET[0]), abs(SHADOW_OFFSET[1])))
    shadow_top = card_top - (SHADOW_BLUR * 2 + max(abs(SHADOW_OFFSET[0]), abs(SHADOW_OFFSET[1])))
    frame_layer.alpha_composite(shadowed_frame, (shadow_left, shadow_top))

    frame_overlay = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    frame_overlay_draw = ImageDraw.Draw(frame_overlay)
    inner_rect = (
        photo_left - 8,
        photo_top - 8,
        photo_left + source.width + 7,
        photo_top + source.height + 7,
    )
    frame_overlay_draw.rounded_rectangle(
        inner_rect,
        radius=photo_radius + 10,
        outline=(238, 238, 238, 255),
        width=INNER_OUTLINE_WIDTH,
    )
    pin_image = _scaled_pin(frame_height)
    if pin_image is not None:
        pin_left = card_left + (frame_width - pin_image.width) // 2
        pin_top = card_top + PIN_TOP_OFFSET
        frame_overlay.alpha_composite(pin_image, (pin_left, pin_top))
    photo_layer = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    photo_layer.alpha_composite(rounded_photo, (photo_left, photo_top))
    photo_mask_layer = Image.new("L", (canvas_width, canvas_height), 0)
    photo_mask_layer.paste(photo_mask, (photo_left, photo_top))

    preview = Image.alpha_composite(frame_layer, photo_layer)
    preview = Image.alpha_composite(preview, frame_overlay)
    visible_bbox = preview.getbbox()
    if visible_bbox is None:
        raise RuntimeError("Generated polaroid preview is fully transparent.")
    visible_size = (
        visible_bbox[2] - visible_bbox[0],
        visible_bbox[3] - visible_bbox[1],
    )

    photo_mask_layer.save(mask_output, "PNG")
    preview.save(preview_output, "PNG")
    logger.info("Created framed PNG at %s", preview_output)
    return CardAssets(
        preview_path=preview_output,
        photo_size=source.size,
        photo_offset=(photo_left, photo_top),
        canvas_size=(canvas_width, canvas_height),
        mask_path=mask_output,
        visible_size=visible_size,
    )


def _scale_expression(start_time: float, end_time: float) -> str:
    in_peak = start_time + 0.18
    settle = start_time + 0.42
    out_start = end_time - 0.22
    return (
        f"if(lt(t,{start_time:.2f}),0.84,"
        f"if(lt(t,{in_peak:.2f}),0.84+(1.08-0.84)*(t-{start_time:.2f})/{in_peak - start_time:.2f},"
        f"if(lt(t,{settle:.2f}),1.08-(1.08-1.00)*(t-{in_peak:.2f})/{settle - in_peak:.2f},"
        f"if(lt(t,{out_start:.2f}),1.00,"
        f"if(lt(t,{end_time:.2f}),1.00-(1.00-0.90)*(t-{out_start:.2f})/{end_time - out_start:.2f},0.90)))))"
    )


def render_video(card_assets: CardAssets, output_dir: Path) -> Path:
    if not FLASH_MASK.exists():
        raise FileNotFoundError(f"Flash mask not found: {FLASH_MASK}")

    ffmpeg = _ffmpeg_bin()
    flash_duration = _probe_duration_seconds(FLASH_MASK)
    clip_duration = max(OUTPUT_DURATION, POP_IN_START + flash_duration + 0.25)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{card_assets.preview_path.stem}_insert.mov"

    scale_expr = _scale_expression(POP_IN_START, min(POP_OUT_END, clip_duration - 0.3))
    fade_out_start = max(POP_IN_START + 0.6, min(POP_OUT_END, clip_duration - 0.3) - 0.20)
    mask_delay_ms = int(round(POP_IN_START * 1000))
    photo_width, photo_height = card_assets.photo_size
    photo_left, photo_top = card_assets.photo_offset
    canvas_width, canvas_height = card_assets.canvas_size
    visible_width, visible_height = card_assets.visible_size
    target_area = OUTPUT_CANVAS_WIDTH * OUTPUT_CANVAS_HEIGHT * CARD_TARGET_AREA_RATIO
    card_area = max(1, visible_width * visible_height)
    display_scale = math.sqrt(target_area / card_area)

    filter_complex = (
        f"[0:v]format=rgba[card_src];"
        f"[1:v]format=rgba,scale={photo_width}:{photo_height}:force_original_aspect_ratio=increase,"
        f"crop={photo_width}:{photo_height},"
        f"pad={canvas_width}:{canvas_height}:{photo_left}:{photo_top}:color=black@0,"
        f"setpts=PTS-STARTPTS+{POP_IN_START:.6f}/TB,"
        f"split[flash_rgb][flash_alpha_src];"
        f"[flash_alpha_src]alphaextract,format=gray[flash_alpha];"
        f"[2:v]format=gray[mask_src];"
        f"[flash_alpha][mask_src]blend=all_mode=multiply[flash_alpha_masked];"
        f"[flash_rgb][flash_alpha_masked]alphamerge[flash_masked];"
        f"[card_src][flash_masked]overlay=x=0:y=0:format=auto:eof_action=pass[card_with_flash];"
        f"[card_with_flash]setpts=N/(30*TB),"
        f"fade=t=in:st={POP_IN_START:.2f}:d=0.18:alpha=1,"
        f"fade=t=out:st={fade_out_start:.2f}:d=0.20:alpha=1,"
        f"scale=w='trunc(iw*{scale_expr}/2)*2':h='trunc(ih*{scale_expr}/2)*2':eval=frame[assembled_card];"
        f"[assembled_card]scale=w='trunc(iw*{display_scale:.6f}/2)*2':"
        f"h='trunc(ih*{display_scale:.6f}/2)*2'[card_display];"
        f"color=c=black@0.0:s={OUTPUT_CANVAS_WIDTH}x{OUTPUT_CANVAS_HEIGHT}:d={clip_duration:.6f}:r=60,format=rgba[canvas];"
        f"[canvas][card_display]overlay=x={CARD_LEFT_MARGIN}:"
        f"y=H-h-{CARD_BOTTOM_MARGIN}{PHOTO_Y_OFFSET:+d}:format=auto[out];"
        f"[1:a]atrim=0:{flash_duration:.6f},asetpts=PTS-STARTPTS,"
        f"adelay={mask_delay_ms}|{mask_delay_ms},"
        f"apad=pad_dur={clip_duration:.6f},atrim=0:{clip_duration:.6f}[audio]"
    )

    cmd = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        f"{clip_duration:.6f}",
        "-i",
        str(card_assets.preview_path),
        "-i",
        str(FLASH_MASK),
        "-i",
        str(card_assets.mask_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-map",
        "[audio]",
        "-t",
        f"{clip_duration:.6f}",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4",
        "-pix_fmt",
        "yuva444p10le",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]

    _run_ffmpeg(cmd)
    logger.info("Rendered insert video at %s", output_path)
    return output_path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()

    try:
        downloaded = download_image(args.image_url, output_dir)
        card_assets = build_polaroid_assets(downloaded, output_dir)
        if args.skip_video:
            logger.info("Skipping video render as requested.")
            return 0
        render_video(card_assets, output_dir)
    except Exception as exc:  # pragma: no cover - CLI failure path
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
