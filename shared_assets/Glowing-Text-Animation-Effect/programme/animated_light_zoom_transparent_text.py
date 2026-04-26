#!/usr/bin/env python3.11
"""
Glowing word-by-word text animation with zoom-out — transparent background.
Camera starts zoomed in on the first word (lower third), eases out as words
appear. Output is ProRes 4444 .mov with alpha channel for compositing.

Usage: python3.11 programme/animated_light_zoom_transparent_text.py "your text here"
Output: output/<first_words>_N.mov
"""

import sys
import os
import subprocess
import shutil
import tempfile
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageEnhance

# ── Settings ──────────────────────────────────────────────────────────────────
W, H             = 1920, 1080
FPS              = 30
FONT_SIZE        = 100
PADDING_X        = 160
PADDING_BOTTOM   = 80        # gap between text bottom and frame edge
LINE_SPACING     = 1.6

FRAMES_FALL      = 2
FRAMES_GLOW_IN   = 2
FRAMES_HOLD      = 1
FRAMES_PER_WORD  = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL     = 11

FALL_HEIGHT      = 55
FALL_MAX_BLUR    = 9
FALL_COLOR       = (140, 140, 140)
LIT_COLOR        = (230, 230, 230)
GLOW_COLOR       = (0, 179, 255)

ZOOM_START       = 2.5   # zoom factor at frame 0

# ── Font ──────────────────────────────────────────────────────────────────────
def load_font(size):
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("Warning: no system font found, using default")
    return ImageFont.load_default()

# ── Layout ────────────────────────────────────────────────────────────────────
def layout_words(words, font):
    probe   = Image.new('RGB', (1, 1))
    d       = ImageDraw.Draw(probe)
    max_w   = W - 2 * PADDING_X
    space_w = d.textlength(' ', font=font)

    positions, line_x, line_y, line_h = [], 0, 0, 0
    for word in words:
        bb = d.textbbox((0, 0), word, font=font)
        ww, wh = bb[2] - bb[0], bb[3] - bb[1]
        line_h = max(line_h, wh)
        if line_x > 0 and line_x + ww > max_w:
            line_x  = 0
            line_y += int(line_h * LINE_SPACING)
            line_h  = wh
        positions.append((word, line_x, line_y, ww, wh))
        line_x += ww + space_w

    block_w = max(x + ww for _, x, _, ww, _ in positions)
    block_h = line_y + int(line_h * LINE_SPACING)
    return positions, block_w, block_h

# ── Glow cache (RGBA) ─────────────────────────────────────────────────────────
_glow_cache: dict = {}

def rgb_to_rgba(rgb_img):
    r, g, b = rgb_img.split()
    alpha   = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge('RGBA', (r, g, b, alpha))

def get_glow_rgba(word, px, py, font):
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new('RGB', (W, H), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), word, font=font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]

# ── Falling word layer ────────────────────────────────────────────────────────
def make_fall_layer_rgba(word, px, py, font, phase):
    t     = phase / FRAMES_FALL
    ease  = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur  = FALL_MAX_BLUR * (1 - t)
    layer = Image.new('RGB', (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), word, font=font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)

# ── Zoom via affine transform — no integer rounding, no wiggle ─────────────────
def apply_zoom(img, zoom, cx, cy):
    """
    Zoom by `zoom` centered at (cx, cy) using an affine transform.

    PIL AFFINE maps each output pixel (x, y) to an input position:
        input_x = a*x + c  →  x/zoom + cx*(1 - 1/zoom)
        input_y = e*y + f  →  y/zoom + cy*(1 - 1/zoom)

    At zoom=1 with cx=W/2, cy=H/2 this is the identity transform — no jump,
    no shift. Pixels outside the canvas are transparent (alpha=0).
    """
    inv = 1.0 / zoom
    a, b, c = inv, 0.0, cx * (1.0 - inv)
    d, e, f = 0.0, inv, cy * (1.0 - inv)
    return img.transform(
        (W, H), Image.AFFINE, (a, b, c, d, e, f),
        resample=Image.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )

# ── Frame renderer ─────────────────────────────────────────────────────────────
def render_frame(positions, ox, oy, frame_num, n_words, font, first_cx, first_cy):
    total_word_frames = n_words * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_words, 0
        t_ease = 1.0
    else:
        cur      = frame_num // FRAMES_PER_WORD
        phase    = frame_num %  FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
        # Cubic ease-out: fast start, smooth arrival at zoom=1
        t_ease   = 1 - (1 - t_global) ** 3

    # Zoom: ZOOM_START → 1.0
    zoom = ZOOM_START - (ZOOM_START - 1.0) * t_ease

    # Center: first word → canvas center.
    # At t_ease=1 → cx=W/2, cy=H/2, zoom=1 → identity transform (no jump).
    cx = first_cx + (W / 2 - first_cx) * t_ease
    cy = first_cy + (H / 2 - first_cy) * t_ease

    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))

    for i, (word, wx, wy, ww, wh) in enumerate(positions):
        px, py = ox + wx, oy + wy

        if all_lit or i < cur:
            img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
            ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

        elif i == cur:
            if phase < FRAMES_FALL:
                img = Image.alpha_composite(
                    img, make_fall_layer_rgba(word, px, py, font, phase))

            elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                t    = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                glow = get_glow_rgba(word, px, py, font)
                if t < 1.0:
                    r2, g2, b2, a2 = glow.split()
                    a2   = ImageEnhance.Brightness(a2).enhance(t)
                    glow = Image.merge('RGBA', (r2, g2, b2, a2))
                img = Image.alpha_composite(img, glow)
                ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

            else:
                img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
                ImageDraw.Draw(img).text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

        # else: future word — invisible

    return apply_zoom(img, zoom, cx, cy)

# ── Output path ───────────────────────────────────────────────────────────────
def next_output_path(out_dir, words):
    slug = '_'.join(w for w in words[:4] if w.isalnum())[:40] or "output"
    i = 0
    while os.path.exists(os.path.join(out_dir, f"{slug}_{i}.mov")):
        i += 1
    return os.path.join(out_dir, f"{slug}_{i}.mov")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Usage: python3.11 programme/animated_light_zoom_transparent_text.py "your text here"')
        sys.exit(1)

    text  = sys.argv[1].strip()
    words = text.split()
    if not words:
        print("Error: empty text")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir     = os.path.join(project_dir, 'output')
    os.makedirs(out_dir, exist_ok=True)
    out_path    = next_output_path(out_dir, words)

    font = load_font(FONT_SIZE)
    positions, block_w, block_h = layout_words(words, font)

    ox = (W - block_w) // 2                   # horizontally centered
    oy = H - PADDING_BOTTOM - block_h          # lower third (anchored to bottom)

    # Zoom starts centered on the first word's center
    _, wx0, wy0, ww0, wh0 = positions[0]
    first_cx = float(ox + wx0 + ww0 / 2)
    first_cy = float(oy + wy0 + wh0 / 2)

    n            = len(words)
    total_frames = n * FRAMES_PER_WORD + FRAMES_FINAL

    print(f"Text    : {text}")
    print(f"Words   : {n}")
    print(f"Output  : {out_path}")
    print(f"Frames  : {total_frames}  ({total_frames / FPS:.1f}s at {FPS}fps)")
    print(f"Zoom    : {ZOOM_START}x → 1.0x (cubic ease-out)")
    print(f"Format  : ProRes 4444 .mov (transparent background)\n")

    frames_dir = tempfile.mkdtemp(prefix='glow_zoom_alpha_')
    try:
        for f in range(total_frames):
            frame = render_frame(positions, ox, oy, f, n, font, first_cx, first_cy)
            frame.save(os.path.join(frames_dir, f"frame_{f:05d}.png"))
            if (f + 1) % FPS == 0:
                print(f"  {f+1}/{total_frames} frames ({(f+1)/total_frames*100:.0f}%)")

        print("\nEncoding with ffmpeg (ProRes 4444 with alpha)...")
        subprocess.run([
            'ffmpeg', '-y',
            '-framerate', str(FPS),
            '-i', os.path.join(frames_dir, 'frame_%05d.png'),
            '-c:v', 'prores_ks',
            '-profile:v', '4444',
            '-pix_fmt', 'yuva444p10le',
            '-alpha_bits', '16',
            out_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        print(f"\nDone → {out_path}")
    finally:
        shutil.rmtree(frames_dir)

if __name__ == '__main__':
    main()
