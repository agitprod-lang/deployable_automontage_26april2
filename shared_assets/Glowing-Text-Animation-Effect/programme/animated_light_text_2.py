#!/usr/bin/env python3.11
"""
Glowing word-by-word text animation — transparent background version.
Words fall in with blur, then glow. Output is a .mov with alpha channel
(ProRes 4444) so the animation can be composited over any background.

Usage: python3.11 programme/animated_light_text_2.py "your text here"
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
LINE_SPACING     = 1.6

FRAMES_FALL      = 8     # word falls into position
FRAMES_GLOW_IN   = 6     # glow ramps up
FRAMES_HOLD      = 4     # brief hold before next word
FRAMES_PER_WORD  = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL     = 45    # final hold all-lit (1.5s)

FALL_HEIGHT      = 55    # px above final position word starts
FALL_MAX_BLUR    = 9     # blur radius at start of fall
FALL_COLOR       = (140, 140, 140)   # gray during fall

LIT_COLOR   = (230, 230, 230)
GLOW_COLOR  = (0,   179, 255)   # #00b3ff

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
    probe = Image.new('RGB', (1, 1))
    d     = ImageDraw.Draw(probe)
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
    """Convert RGB-on-black layer to RGBA using luminance as alpha."""
    r, g, b = rgb_img.split()
    # alpha = max(R, G, B) — pixels that are brighter get more opaque
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge('RGBA', (r, g, b, alpha))

def get_glow_rgba(word, px, py, font):
    """Returns full-strength RGBA glow layer (transparent where dark)."""
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new('RGB', (W, H), (0, 0, 0))
        ImageDraw.Draw(base).text((px, py), word, font=font, fill=GLOW_COLOR)
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]

# ── Falling word layer (RGBA) ─────────────────────────────────────────────────
def make_fall_layer_rgba(word, px, py, font, phase):
    """Render one word falling in: cubic ease-out, motion blur fades away."""
    t     = phase / FRAMES_FALL
    ease  = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur  = FALL_MAX_BLUR * (1 - t)

    layer = Image.new('RGB', (W, H), (0, 0, 0))
    ImageDraw.Draw(layer).text((px, py - y_off), word, font=font, fill=FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)

# ── Frame renderer ────────────────────────────────────────────────────────────
def render_frame(positions, ox, oy, frame_num, n_words, font):
    total_word_frames = n_words * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_words, 0
    else:
        cur   = frame_num // FRAMES_PER_WORD
        phase = frame_num %  FRAMES_PER_WORD

    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))  # fully transparent

    for i, (word, wx, wy, ww, wh) in enumerate(positions):
        px, py = ox + wx, oy + wy

        if all_lit or i < cur:
            # ── Already glowing ───────────────────────────────────────────
            img = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
            draw = ImageDraw.Draw(img)
            draw.text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

        elif i == cur:
            if phase < FRAMES_FALL:
                # ── Falling in ────────────────────────────────────────────
                layer = make_fall_layer_rgba(word, px, py, font, phase)
                img   = Image.alpha_composite(img, layer)

            elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                # ── Glow ramp-up ──────────────────────────────────────────
                t    = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                glow = get_glow_rgba(word, px, py, font)
                if t < 1.0:
                    # Scale alpha channel by t
                    r, g, b, a = glow.split()
                    a    = ImageEnhance.Brightness(a).enhance(t)
                    glow = Image.merge('RGBA', (r, g, b, a))
                img  = Image.alpha_composite(img, glow)
                draw = ImageDraw.Draw(img)
                draw.text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

            else:
                # ── Hold fully lit ────────────────────────────────────────
                img  = Image.alpha_composite(img, get_glow_rgba(word, px, py, font))
                draw = ImageDraw.Draw(img)
                draw.text((px, py), word, font=font, fill=(*LIT_COLOR, 255))

        # else: future word — invisible

    return img

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
        print("Usage: python3.11 programme/animated_light_text_2.py \"your text here\"")
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
    ox = (W - block_w) // 2
    oy = (H - block_h) // 2

    n            = len(words)
    total_frames = n * FRAMES_PER_WORD + FRAMES_FINAL

    print(f"Text    : {text}")
    print(f"Words   : {n}")
    print(f"Output  : {out_path}")
    print(f"Frames  : {total_frames}  ({total_frames / FPS:.1f}s at {FPS}fps)")
    print(f"Format  : ProRes 4444 .mov (transparent background)\n")

    frames_dir = tempfile.mkdtemp(prefix='glow_text_alpha_')
    try:
        for f in range(total_frames):
            render_frame(positions, ox, oy, f, n, font).save(
                os.path.join(frames_dir, f"frame_{f:05d}.png")
            )
            if (f + 1) % FPS == 0:
                print(f"  {f+1}/{total_frames} frames ({(f+1)/total_frames*100:.0f}%)")

        print("\nEncoding with ffmpeg (ProRes 4444 with alpha)...")
        subprocess.run([
            'ffmpeg', '-y',
            '-framerate', str(FPS),
            '-i', os.path.join(frames_dir, 'frame_%05d.png'),
            '-c:v', 'prores_ks',
            '-profile:v', '4444',        # ProRes 4444 — supports alpha
            '-pix_fmt', 'yuva444p10le',  # 10-bit YUV with alpha
            '-alpha_bits', '16',
            out_path
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        print(f"\nDone → {out_path}")
    finally:
        shutil.rmtree(frames_dir)

if __name__ == '__main__':
    main()
