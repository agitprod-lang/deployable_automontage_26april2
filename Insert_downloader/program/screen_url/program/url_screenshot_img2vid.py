#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
FEATURE_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = FEATURE_DIR.parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"

FPS = 30
POP_IN_FRAMES  = 15    # 0.5 s
HOLD_FRAMES    = 120   # 4.0 s
POP_OUT_FRAMES = 15    # 0.5 s
MAX_BLUR_RADIUS = 22.0

# Must match screen_url.py exactly
ADDRESS_BAR_BOX  = (360, 224, 1210, 282)
ADDRESS_BAR_FILL = (229, 233, 238, 255)
URL_FONT_SIZE    = 28
URL_TEXT_MARGIN  = 28

TYPING_INTERVAL = 2   # one new character every N frames (~15 chars/s)


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------

def ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def ease_in_cubic(t: float) -> float:
    return t ** 3


# ---------------------------------------------------------------------------
# Font / text helpers  (mirrored from screen_url.py)
# ---------------------------------------------------------------------------

def pick_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: Iterable[Path] = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttc"),
        Path("/Library/Fonts/Arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def truncate_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    current = text
    while current:
        current = current[:-1]
        candidate = current.rstrip() + ellipsis
        if draw.textlength(candidate, font=font) <= max_width:
            return candidate
    return ellipsis


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def scale_centered(
    img: Image.Image, canvas_wh: tuple[int, int], scale: float
) -> Image.Image:
    cw, ch = canvas_wh
    frame = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    if scale <= 0:
        return frame
    new_w = max(1, round(img.width * scale))
    new_h = max(1, round(img.height * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    x = (cw - new_w) // 2
    y = (ch - new_h) // 2
    frame.paste(resized, (x, y), resized)
    return frame


def blur(img: Image.Image, radius: float) -> Image.Image:
    if radius < 0.5:
        return img
    return img.filter(ImageFilter.GaussianBlur(radius=radius))


def blank_address_bar(img: Image.Image) -> Image.Image:
    """Return a copy of img with the address bar area cleared (no URL text)."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    bar_left, bar_top, bar_right, bar_bottom = ADDRESS_BAR_BOX
    draw.rounded_rectangle(
        ADDRESS_BAR_BOX,
        radius=(bar_bottom - bar_top) // 2,
        fill=ADDRESS_BAR_FILL,
    )
    return out


def draw_url_text(img: Image.Image, text: str, font: ImageFont.ImageFont) -> Image.Image:
    """Return a copy of img with *text* drawn in the address bar."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    bar_left, bar_top, bar_right, bar_bottom = ADDRESS_BAR_BOX
    max_width = (bar_right - bar_left) - URL_TEXT_MARGIN * 2
    display = truncate_text(draw, text, font, max_width)
    bbox = draw.textbbox((0, 0), display, font=font)
    text_h = bbox[3] - bbox[1]
    tx = bar_left + URL_TEXT_MARGIN
    ty = bar_top + ((bar_bottom - bar_top - text_h) // 2) - 2
    draw.text((tx, ty), display, font=font, fill=(0, 0, 0, 255))
    return out


# ---------------------------------------------------------------------------
# Frame generation
# ---------------------------------------------------------------------------

def generate_frames(
    source: Image.Image, out_dir: Path, url_text: str | None
) -> int:
    canvas = source.size
    idx = 0

    # Prepare the two base images used during the hold phase
    if url_text:
        base_blank = blank_address_bar(source)
        font = pick_font(URL_FONT_SIZE)
    else:
        base_blank = source  # unused but keeps type checker happy

    # --- pop-in -----------------------------------------------------------
    for i in range(POP_IN_FRAMES):
        t = (i + 1) / POP_IN_FRAMES
        scale = ease_out_back(t)
        blur_r = MAX_BLUR_RADIUS * (1.0 - t) ** 2
        frame = scale_centered(source, canvas, scale)
        frame = blur(frame, blur_r)
        frame.save(out_dir / f"frame_{idx:05d}.png")
        idx += 1

    # --- hold (with optional letter-by-letter typing) ---------------------
    if url_text:
        full_url_frame: Image.Image | None = None
        for hold_i in range(HOLD_FRAMES):
            n_chars = min(len(url_text), hold_i // TYPING_INTERVAL + 1)
            if n_chars >= len(url_text):
                # All characters visible — cache and reuse
                if full_url_frame is None:
                    full_url_frame = draw_url_text(base_blank, url_text, font)
                frame = full_url_frame
            else:
                frame = draw_url_text(base_blank, url_text[:n_chars], font)
            frame.save(out_dir / f"frame_{idx:05d}.png")
            idx += 1
    else:
        # Static hold — use symlinks to avoid redundant writes
        full = scale_centered(source, canvas, 1.0)
        hold_path = out_dir / f"frame_{idx:05d}.png"
        full.save(hold_path)
        idx += 1
        for _ in range(HOLD_FRAMES - 1):
            (out_dir / f"frame_{idx:05d}.png").symlink_to(hold_path.name)
            idx += 1

    # --- pop-out ----------------------------------------------------------
    pop_out_base = base_blank if url_text else source
    for i in range(POP_OUT_FRAMES):
        t = (i + 1) / POP_OUT_FRAMES
        scale = 1.0 - ease_in_cubic(t)
        blur_r = MAX_BLUR_RADIUS * t
        frame = scale_centered(pop_out_base, canvas, scale)
        frame = blur(frame, blur_r)
        frame.save(out_dir / f"frame_{idx:05d}.png")
        idx += 1

    return idx


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def encode_video(frames_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(frames_dir / "frame_%05d.png"),
        "-c:v", "prores_ks",
        "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        "-vendor", "apl0",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr}")


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def find_latest_screenshot() -> Path:
    candidates = sorted(
        OUTPUT_DIR.glob("*_screen_url.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No *_screen_url.png found in {OUTPUT_DIR}")
    return candidates[0]


def build_output_path(image_path: Path, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    stem = image_path.stem
    if stem.endswith("_screen_url"):
        stem = stem[: -len("_screen_url")]
    return OUTPUT_DIR / f"{stem}_anim.mov"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Animate a URL screenshot PNG: pop-in → hold 4s → pop-out. "
            "Pass --url to get letter-by-letter typing in the address bar."
        )
    )
    parser.add_argument(
        "image",
        nargs="?",
        help="Path to the PNG. Defaults to the most recent *_screen_url.png in /output.",
    )
    parser.add_argument(
        "--url",
        help="URL string to type letter-by-letter in the address bar during the hold phase.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output video path. Defaults to /output/<name>_anim.mov",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    try:
        image_path = (
            Path(args.image).expanduser().resolve()
            if args.image
            else find_latest_screenshot()
        )
        if not image_path.exists():
            print(f"Error: image not found: {image_path}", file=sys.stderr)
            return 1

        source = Image.open(image_path).convert("RGBA")
        output_path = build_output_path(image_path, args.output)

        total = POP_IN_FRAMES + HOLD_FRAMES + POP_OUT_FRAMES
        with tempfile.TemporaryDirectory(prefix="img2vid_") as tmp:
            tmp_path = Path(tmp)
            print(f"Generating {total} frames…")
            n = generate_frames(source, tmp_path, args.url)
            print(f"{n} frames written. Encoding → {output_path}")
            encode_video(tmp_path, output_path)

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
