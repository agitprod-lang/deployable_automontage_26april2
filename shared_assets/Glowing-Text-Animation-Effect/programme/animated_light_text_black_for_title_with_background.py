#!/usr/bin/env python3.11
"""
Glowing word-by-word title animation composited onto a three-part animated
background: fixed intro, variable/looped medium, fixed outro.

Usage:
    python3.11 programme/animated_light_text_black_for_title_with_background.py "your text here"

Output:
    output/<first_words>_N.mov
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

# ── Settings ──────────────────────────────────────────────────────────────────
W, H = 1920, 1080
FPS = 24
FONT_SIZE = 100
PADDING_X = 160
LINE_SPACING = 1.6

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90
FRAMES_FADE_OUT = 18

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)

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
_glow_cache = {}


def rgb_to_rgba(rgb_img):
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(word, px, py, font):
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
def render_frame(positions, ox, oy, frame_num, n_words, font):
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

    return apply_zoom(img, zoom, W / 2, H / 2)


def scale_image_alpha(img, alpha_scale):
    if alpha_scale >= 0.999:
        return img
    if alpha_scale <= 0.0:
        return Image.new("RGBA", img.size, (0, 0, 0, 0))
    r, g, b, a = img.split()
    a = ImageEnhance.Brightness(a).enhance(alpha_scale)
    return Image.merge("RGBA", (r, g, b, a))


# ── Media helpers ─────────────────────────────────────────────────────────────
def ffprobe_json(path, entries, scope):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            f"-show_entries",
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
                f"{name} asset has unexpected size {data['width']}x{data['height']}; "
                f"expected {W}x{H}"
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
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"No audio stream found in asset: {AUDIO_PATH}")
    return AUDIO_PATH


# ── Output path ───────────────────────────────────────────────────────────────
def next_output_path(out_dir, words):
    slug = "_".join(w for w in words[:4] if w.isalnum())[:40] or "output"
    i = 0
    while os.path.exists(os.path.join(out_dir, f"{slug}_{i}.mov")):
        i += 1
    return os.path.join(out_dir, f"{slug}_{i}.mov")


def save_overlay_frames(frames_dir, positions, ox, oy, n_words, font, outro_frames):
    medium_frames = n_words * FRAMES_PER_WORD + FRAMES_FINAL
    total_overlay_frames = medium_frames + outro_frames
    final_frame = render_frame(positions, ox, oy, medium_frames - 1, n_words, font)
    fade_start = max(0, medium_frames - FRAMES_FADE_OUT)

    for frame_num in range(total_overlay_frames):
        if frame_num < medium_frames:
            img = render_frame(positions, ox, oy, frame_num, n_words, font)
            if frame_num >= fade_start:
                fade_progress = (frame_num - fade_start + 1) / max(1, FRAMES_FADE_OUT)
                img = scale_image_alpha(img, 1.0 - fade_progress)
        else:
            img = Image.new("RGBA", final_frame.size, (0, 0, 0, 0))
        img.save(os.path.join(frames_dir, f"frame_{frame_num:05d}.png"))
        if (frame_num + 1) % FPS == 0 or frame_num + 1 == total_overlay_frames:
            print(
                f"  overlay {frame_num + 1}/{total_overlay_frames} frames "
                f"({(frame_num + 1) / total_overlay_frames * 100:.0f}%)"
            )

    return medium_frames, total_overlay_frames


def build_final_video(assets, medium_duration, intro_duration, overlay_dir, out_path):
    filter_graph = (
        "[0:v]fps=24,format=yuva444p10le[intro];"
        f"[1:v]trim=duration={medium_duration:.6f},setpts=PTS-STARTPTS,fps=24,format=yuva444p10le[medium];"
        "[2:v]fps=24,format=yuva444p10le[outro];"
        "[intro][medium][outro]concat=n=3:v=1:a=0[bg];"
        f"[3:v]format=rgba,setpts=PTS+{intro_duration:.6f}/TB[title];"
        "[bg][title]overlay=eof_action=pass:format=auto[v];"
        "[4:a]asplit=2[intro_src][outro_src];"
        f"[intro_src]atrim=duration={intro_duration:.6f},asetpts=PTS-STARTPTS[intro_a];"
        f"[5:a]atrim=duration={medium_duration:.6f},asetpts=PTS-STARTPTS[medium_a];"
        f"[outro_src]atrim=duration={assets['outro']['duration']:.6f},asetpts=PTS-STARTPTS[outro_a];"
        "[intro_a][medium_a][outro_a]concat=n=3:v=0:a=1[a]"
    )

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
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
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
            out_path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3.11 programme/"
            'animated_light_text_black_for_title_with_background.py "your text here"'
        )
        sys.exit(1)

    text = sys.argv[1].strip()
    words = text.split()
    if not words:
        print("Error: empty text")
        sys.exit(1)

    assets = validate_assets()
    validate_audio()

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(project_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = next_output_path(out_dir, words)

    font = load_font(FONT_SIZE)
    positions, block_w, block_h = layout_words(words, font)
    ox = (W - block_w) // 2
    oy = (H - block_h) // 2

    n_words = len(words)
    medium_frames = n_words * FRAMES_PER_WORD + FRAMES_FINAL
    medium_duration = medium_frames / FPS
    medium_loops = max(1, math.ceil(medium_duration / assets["medium"]["duration"]))
    intro_duration = assets["intro"]["duration"]
    outro_frames = int(round(assets["outro"]["duration"] * FPS))
    total_duration = intro_duration + medium_duration + assets["outro"]["duration"]

    print(f"Text         : {text}")
    print(f"Words        : {n_words}")
    print(f"Output       : {out_path}")
    print(f"Format       : composited ProRes 4444 .mov at {FPS}fps")
    print(f"Intro        : {intro_duration:.3f}s")
    print(
        f"Medium       : {medium_duration:.3f}s "
        f"({medium_frames} frames, {medium_loops} loop{'s' if medium_loops != 1 else ''})"
    )
    print(f"Outro        : {assets['outro']['duration']:.3f}s")
    print(f"Fade-out     : {FRAMES_FADE_OUT / FPS:.3f}s before outro")
    print(f"Total        : {total_duration:.3f}s\n")

    overlay_dir = tempfile.mkdtemp(prefix="glow_text_black_bg_overlay_")
    try:
        print("Rendering overlay frames...")
        _medium_frames, total_overlay_frames = save_overlay_frames(
            overlay_dir, positions, ox, oy, n_words, font, outro_frames
        )
        print(f"Overlay      : {total_overlay_frames} frames")

        print("\nEncoding composed title video...")
        build_final_video(assets, medium_duration, intro_duration, overlay_dir, out_path)
        print(f"\nDone -> {out_path}")
    finally:
        shutil.rmtree(overlay_dir)


if __name__ == "__main__":
    main()
