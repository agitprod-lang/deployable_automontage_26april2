#!/usr/bin/env python3.11
"""
Glowing word-by-word text animation with black title text on a transparent
background. Words fall in with blur, then glow (blue). Text is anchored to
the bottom-left corner. Output is a .mov with alpha channel (ProRes 4444)
so the animation can be composited over any background.

Usage: python3.11 programme/animated_light_text_black_for_title_blue_background_light_bottom_left_corner.py "your text here"
Output: output/<first_words>_N.mov
"""

import os
import shutil
import subprocess
import sys
import tempfile
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# ── Settings ──────────────────────────────────────────────────────────────────
W, H = 1920, 1080
FPS = 30
FONT_SIZE = 100
PADDING_X = 160       # distance from left edge
PADDING_BOTTOM = 120  # distance from bottom edge
LINE_SPACING = 1.6

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)

LIT_COLOR = (0, 0, 0)
GLOW_COLOR = (0, 100, 255)   # blue glow
FONT_PATH = "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"
ZOOM_START = 1.0
ZOOM_END = 1.12


# ── Font ──────────────────────────────────────────────────────────────────────
def load_font(size):
    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(f"Required font not found: {FONT_PATH}")
    return ImageFont.truetype(FONT_PATH, size)


# ── Layout ────────────────────────────────────────────────────────────────────
def layout_words(words, font):
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    max_w = W - 2 * PADDING_X
    space_w = draw.textlength(" ", font=font)

    positions, line_x, line_y, line_h = [], 0, 0, 0
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

    block_w = max(x + ww for _, x, _, ww, _ in positions)
    block_h = line_y + int(line_h * LINE_SPACING)
    return positions, block_w, block_h


# ── Glow cache (RGBA) ─────────────────────────────────────────────────────────
_glow_cache: dict = {}


def rgb_to_rgba(rgb_img):
    """Convert RGB-on-black layer to RGBA using luminance as alpha."""
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(word, px, py, font):
    """Return the full-strength RGBA glow layer."""
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), word, font=font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


# ── Falling word layer (RGBA) ─────────────────────────────────────────────────
def make_fall_layer_rgba(word, px, py, font, phase):
    """Render one word falling in with cubic ease-out and fading blur."""
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)

    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), word, font=font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


# ── Zoom ──────────────────────────────────────────────────────────────────────
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


# ── Frame renderer ────────────────────────────────────────────────────────────
def render_frame(positions, ox, oy, frame_num, n_words, font, zoom_cx, zoom_cy):
    total_word_frames = n_words * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_words, 0
        zoom = ZOOM_END
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
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


# ── Output path ───────────────────────────────────────────────────────────────
def next_output_path(out_dir, words):
    slug = "_".join(w for w in words[:4] if w.isalnum())[:40] or "output"
    i = 0
    while os.path.exists(os.path.join(out_dir, f"{slug}_{i}.mov")):
        i += 1
    return os.path.join(out_dir, f"{slug}_{i}.mov")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Usage: python3.11 programme/animated_light_text_black_for_title_blue_background_light_bottom_left_corner.py "your text here"')
        sys.exit(1)

    text = sys.argv[1].strip()
    words = text.split()
    if not words:
        print("Error: empty text")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(project_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = next_output_path(out_dir, words)

    font = load_font(FONT_SIZE)
    positions, block_w, block_h = layout_words(words, font)

    # Bottom-left corner positioning
    ox = PADDING_X
    oy = H - block_h - PADDING_BOTTOM

    # Zoom anchor: center of the text block
    zoom_cx = ox + block_w / 2
    zoom_cy = oy + block_h / 2

    n = len(words)
    total_frames = n * FRAMES_PER_WORD + FRAMES_FINAL

    print(f"Text    : {text}")
    print(f"Words   : {n}")
    print(f"Output  : {out_path}")
    print(f"Frames  : {total_frames}  ({total_frames / FPS:.1f}s at {FPS}fps)")
    print("Format  : ProRes 4444 .mov (transparent background)")
    print(f"Zoom    : {ZOOM_START:.2f}x -> {ZOOM_END:.2f}x, then static")
    print("Position: bottom-left corner")
    print("Title   : black text with blue glow\n")

    frames_dir = tempfile.mkdtemp(prefix="glow_text_black_blue_bl_")
    try:
        for f in range(total_frames):
            render_frame(positions, ox, oy, f, n, font, zoom_cx, zoom_cy).save(
                os.path.join(frames_dir, f"frame_{f:05d}.png")
            )
            if (f + 1) % FPS == 0:
                print(f"  {f+1}/{total_frames} frames ({(f+1)/total_frames*100:.0f}%)")

        print("\\nEncoding with ffmpeg (ProRes 4444 with alpha)...")
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-framerate",
                str(FPS),
                "-i",
                os.path.join(frames_dir, "frame_%05d.png"),
                "-c:v",
                "prores_ks",
                "-profile:v",
                "4444",
                "-pix_fmt",
                "yuva444p10le",
                "-alpha_bits",
                "16",
                out_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        print(f"\\nDone → {out_path}")
    finally:
        shutil.rmtree(frames_dir)


if __name__ == "__main__":
    main()
