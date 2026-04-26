#!/usr/bin/env python3.11
"""
Glowing word-by-word text animation.
Each word lights up progressively with a blue glow, previous words stay lit.

Usage: python3.11 programme/animated_light_text_0.py "your text here"
Output: output/<first_words>_0.mp4
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
LINE_SPACING     = 1.6   # line height multiplier

FRAMES_PER_WORD  = 12    # frames each word is spotlit
FRAMES_GLOW_IN   = 6     # ramp-up frames (glow fades in)
FRAMES_FINAL     = 45    # hold all-lit at the end (1.5s)

BG_COLOR    = (0,   0,   0)
DIM_COLOR   = (30,  30,  30)
LIT_COLOR   = (230, 230, 230)
GLOW_COLOR  = (0,   179, 255)   # #00b3ff matching the original CSS

# ── Font loading ──────────────────────────────────────────────────────────────
def load_font(size):
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("Warning: no system font found, using default (quality will be poor)")
    return ImageFont.load_default()

# ── Text layout ───────────────────────────────────────────────────────────────
def layout_words(words, font):
    """
    Returns:
      positions  – list of (word, x, y, w, h) relative to text-block origin
      block_w    – total block width
      block_h    – total block height
    """
    probe = Image.new('RGB', (1, 1))
    d     = ImageDraw.Draw(probe)

    max_w   = W - 2 * PADDING_X
    space_w = d.textlength(' ', font=font)

    positions = []
    line_x, line_y, line_h = 0, 0, 0

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

# ── Glow cache ────────────────────────────────────────────────────────────────
_glow_cache: dict = {}

def get_glow(word, px, py, font):
    """Build (and cache) a full-intensity glow layer for a word."""
    key = (word, px, py)
    if key not in _glow_cache:
        base = Image.new('RGB', (W, H), (0, 0, 0))
        d    = ImageDraw.Draw(base)
        d.text((px, py), word, font=font, fill=GLOW_COLOR)

        # Accumulate multiple blur radii — simulates CSS layered text-shadow
        result = base.copy()
        for radius in [3, 6, 12, 22, 40]:
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))

        _glow_cache[key] = result
    return _glow_cache[key]

# ── Frame renderer ────────────────────────────────────────────────────────────
def render_frame(positions, ox, oy, current_idx, intensity, font, all_lit=False):
    img  = Image.new('RGB', (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    for i, (word, wx, wy, ww, wh) in enumerate(positions):
        px, py = ox + wx, oy + wy

        if all_lit or i < current_idx:
            # Already revealed — keep glow
            glow = get_glow(word, px, py, font)
            img  = ImageChops.screen(img, glow)
            draw = ImageDraw.Draw(img)
            draw.text((px, py), word, font=font, fill=LIT_COLOR)

        elif i == current_idx:
            # Currently lighting up — glow + white text
            if intensity > 0:
                glow = get_glow(word, px, py, font)
                if intensity < 1.0:
                    glow = ImageEnhance.Brightness(glow).enhance(intensity)
                img  = ImageChops.screen(img, glow)
                draw = ImageDraw.Draw(img)
            draw.text((px, py), word, font=font, fill=LIT_COLOR)

        else:
            # Not yet revealed — dim
            draw.text((px, py), word, font=font, fill=DIM_COLOR)

    return img

# ── Output path helper ────────────────────────────────────────────────────────
def next_output_path(out_dir, words):
    slug = '_'.join(w for w in words[:4] if w.isalnum() or w.isspace())
    slug = slug.strip().replace(' ', '_')[:40] or "output"
    i = 0
    while os.path.exists(os.path.join(out_dir, f"{slug}_{i}.mp4")):
        i += 1
    return os.path.join(out_dir, f"{slug}_{i}.mp4")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python3.11 programme/animated_light_text_0.py \"your text here\"")
        sys.exit(1)

    text  = sys.argv[1].strip()
    words = text.split()
    if not words:
        print("Error: empty text")
        sys.exit(1)

    script_dir  = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)
    out_dir     = os.path.join(project_dir, 'output')
    os.makedirs(out_dir, exist_ok=True)

    out_path = next_output_path(out_dir, words)

    print(f"Text    : {text}")
    print(f"Words   : {len(words)}")
    print(f"Output  : {out_path}")

    font = load_font(FONT_SIZE)
    positions, block_w, block_h = layout_words(words, font)

    ox = (W - block_w) // 2
    oy = (H - block_h) // 2

    n            = len(words)
    total_frames = n * FRAMES_PER_WORD + FRAMES_FINAL

    print(f"Frames  : {total_frames}  ({total_frames / FPS:.1f}s at {FPS}fps)\n")

    frames_dir = tempfile.mkdtemp(prefix='glow_text_')
    try:
        for f in range(total_frames):
            if f < n * FRAMES_PER_WORD:
                word_idx  = f // FRAMES_PER_WORD
                frame_in  = f %  FRAMES_PER_WORD
                intensity = min(1.0, frame_in / FRAMES_GLOW_IN)
                img = render_frame(positions, ox, oy, word_idx, intensity, font)
            else:
                img = render_frame(positions, ox, oy, n - 1, 1.0, font, all_lit=True)

            img.save(os.path.join(frames_dir, f"frame_{f:05d}.png"))

            if (f + 1) % FPS == 0:
                pct = (f + 1) / total_frames * 100
                print(f"  Rendered {f+1}/{total_frames} frames ({pct:.0f}%)")

        print("\nEncoding with ffmpeg...")
        cmd = [
            'ffmpeg', '-y',
            '-framerate', str(FPS),
            '-i', os.path.join(frames_dir, 'frame_%05d.png'),
            '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p',
            '-crf', '18',
            '-movflags', '+faststart',
            out_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"\nDone → {out_path}")

    finally:
        shutil.rmtree(frames_dir)

if __name__ == '__main__':
    main()
