#!/usr/bin/env python3.11
"""
Animated list renderer on a transparent background.

Input example:
    "-first element
    -second element
    -third element"

Rendered text example:
    📍first element
    📍second element
    📍third element
"""

import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 1920, 1080
FPS = 24
FONT_SIZE = 88
PADDING_X = 180
LINE_SPACING = 1.45
ITEM_GAP = 28
PREFIX_TEXT = "📍"
PREFIX_GAP = 22
MARKER_W = 56
MARKER_H = 76

FRAMES_FALL = 4
FRAMES_GLOW_IN = 3
FRAMES_HOLD = 2
FRAMES_PER_WORD = FRAMES_FALL + FRAMES_GLOW_IN + FRAMES_HOLD
FRAMES_FINAL = 90

FALL_HEIGHT = 55
FALL_MAX_BLUR = 9
FALL_COLOR = (140, 140, 140)
EMOJI_POP_START = 0.12
EMOJI_POP_OVERSHOOT = 1.18

LIT_COLOR = (0, 0, 0)
GLOW_COLOR = (255, 0, 0)
FONT_PATH = (
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/"
    "Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"
)
ZOOM_START = 1.0
ZOOM_END = 1.12


def load_font(size):
    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(f"Required font not found: {FONT_PATH}")
    return ImageFont.truetype(FONT_PATH, size)


def parse_list_items(text):
    items = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("-"):
            line = line[1:].strip()
        if line:
            items.append(line)
    return items


def draw_text(draw, pos, text, font, fill, *, embedded_color=False):
    kwargs = {"font": font, "fill": fill}
    if embedded_color:
        kwargs["embedded_color"] = True
    draw.text(pos, text, **kwargs)


def layout_items(items, font):
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    max_w = W - 2 * PADDING_X
    space_w = draw.textlength(" ", font=font)
    prefix_w = MARKER_W
    prefix_h = MARKER_H

    tokens = []
    block_w = 0
    y = 0
    for item_index, item in enumerate(items):
        words = item.split()
        if not words:
            continue
        line_x = prefix_w + PREFIX_GAP
        line_h = prefix_h
        line_tokens = [
            {
                "text": PREFIX_TEXT,
                "x": 0,
                "y": y,
                "kind": "marker",
                "font": None,
                "w": MARKER_W,
                "h": MARKER_H,
            }
        ]
        for word in words:
            bbox = draw.textbbox((0, 0), word, font=font)
            word_w = bbox[2] - bbox[0]
            word_h = bbox[3] - bbox[1]
            if line_x > prefix_w + PREFIX_GAP and line_x + word_w > max_w:
                line_x = prefix_w + PREFIX_GAP
                y += int(line_h * LINE_SPACING)
                line_h = word_h
            line_h = max(line_h, word_h, prefix_h)
            line_tokens.append(
                {
                    "text": word,
                    "x": line_x,
                    "y": y,
                    "kind": "word",
                    "font": font,
                    "w": word_w,
                    "h": word_h,
                }
            )
            line_x += word_w + space_w
        line_width = max(token["x"] + token["w"] for token in line_tokens)
        block_w = max(block_w, line_width)
        tokens.extend(line_tokens)
        y += int(line_h * LINE_SPACING)
        if item_index < len(items) - 1:
            y += ITEM_GAP

    block_h = max(y - int(prefix_h * (LINE_SPACING - 1)), prefix_h)
    return tokens, block_w, block_h


_glow_cache = {}


def rgb_to_rgba(rgb_img):
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def make_marker_rgba(width, height, fill, hole_fill=(0, 0, 0, 0)):
    icon = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    cx = width / 2
    circle_d = int(width * 0.82)
    circle_left = int((width - circle_d) / 2)
    circle_top = 0
    circle_right = circle_left + circle_d
    circle_bottom = circle_top + circle_d
    tail_top = int(circle_d * 0.58)
    tail = [
        (int(cx), height - 1),
        (circle_left + int(circle_d * 0.18), tail_top),
        (circle_right - int(circle_d * 0.18), tail_top),
    ]
    draw.polygon(tail, fill=fill)
    draw.ellipse((circle_left, circle_top, circle_right, circle_bottom), fill=fill)
    hole_d = int(circle_d * 0.38)
    hole_left = int(cx - hole_d / 2)
    hole_top = int(circle_d * 0.22)
    draw.ellipse(
        (hole_left, hole_top, hole_left + hole_d, hole_top + hole_d),
        fill=hole_fill,
    )
    return icon


def draw_marker_layer(px, py, scale=1.0):
    marker = make_marker_rgba(MARKER_W, MARKER_H, (0, 0, 0, 255))
    px = int(round(px))
    py = int(round(py))
    if scale != 1.0:
        scaled_w = max(1, int(round(MARKER_W * scale)))
        scaled_h = max(1, int(round(MARKER_H * scale)))
        marker = marker.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
        paste_x = int(round(px + (MARKER_W - scaled_w) / 2))
        paste_y = int(round(py + (MARKER_H - scaled_h) / 2))
    else:
        paste_x = px
        paste_y = py
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layer.alpha_composite(marker, (paste_x, paste_y))
    return layer


def marker_glow_rgba(px, py):
    px = int(round(px))
    py = int(round(py))
    base = Image.new("RGB", (W, H), (0, 0, 0))
    marker = make_marker_rgba(MARKER_W, MARKER_H, GLOW_COLOR, hole_fill=(0, 0, 0, 0))
    base.paste(marker.convert("RGB"), (px, py), marker)
    result = base.copy()
    for radius in [3, 6, 12, 22, 40]:
        result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
    return rgb_to_rgba(result)


def get_glow_rgba(token, px, py):
    key = (token["text"], px, py, token["kind"])
    if key not in _glow_cache:
        if token["kind"] == "marker":
            _glow_cache[key] = marker_glow_rgba(px, py)
        else:
            base = Image.new("RGB", (W, H), (0, 0, 0))
            draw = ImageDraw.Draw(base)
            draw_text(draw, (px, py), token["text"], token["font"], GLOW_COLOR)
            result = base.copy()
            for radius in [3, 6, 12, 22, 40]:
                result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
            _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_fall_layer_rgba(token, px, py, phase):
    t = phase / FRAMES_FALL
    ease = 1 - (1 - t) ** 3
    y_off = int(FALL_HEIGHT * (1 - ease))
    blur = FALL_MAX_BLUR * (1 - t)

    layer = Image.new("RGB", (W, H), (0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw_text(draw, (px, py - y_off), token["text"], token["font"], FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def draw_scaled_token_rgba(token, px, py, scale, fill):
    if token["kind"] == "marker":
        return draw_marker_layer(px, py, scale)
    scale = max(scale, 0.01)
    bbox = token["font"].getbbox(token["text"])
    text_w = max(1, bbox[2] - bbox[0])
    text_h = max(1, bbox[3] - bbox[1])
    base = Image.new("RGBA", (text_w, text_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)
    draw_text(
        draw,
        (-bbox[0], -bbox[1]),
        token["text"],
        token["font"],
        fill,
        embedded_color=(token["kind"] == "emoji"),
    )
    scaled_w = max(1, int(round(text_w * scale)))
    scaled_h = max(1, int(round(text_h * scale)))
    scaled = base.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    paste_x = int(round(px + (text_w - scaled_w) / 2))
    paste_y = int(round(py + (text_h - scaled_h) / 2))
    layer.alpha_composite(scaled, (paste_x, paste_y))
    return layer


def draw_word_layer_rgba(token, px, py, fill):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw_text(draw, (px, py), token["text"], token["font"], fill)
    return layer


def make_emoji_pop_layer_rgba(token, px, py, phase):
    pop_frames = FRAMES_FALL + FRAMES_GLOW_IN
    if pop_frames <= 1:
        scale = 1.0
    else:
        t = phase / (pop_frames - 1)
        if t < 0.7:
            local_t = t / 0.7
            scale = EMOJI_POP_START + (EMOJI_POP_OVERSHOOT - EMOJI_POP_START) * local_t
        else:
            local_t = (t - 0.7) / 0.3
            scale = EMOJI_POP_OVERSHOOT + (1.0 - EMOJI_POP_OVERSHOOT) * local_t
    return draw_scaled_token_rgba(token, px, py, scale, (255, 255, 255, 255))


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


def render_frame(tokens, ox, oy, frame_num, n_tokens):
    total_word_frames = n_tokens * FRAMES_PER_WORD
    all_lit = frame_num >= total_word_frames

    if all_lit:
        cur, phase = n_tokens, 0
        zoom = ZOOM_END
    else:
        cur = frame_num // FRAMES_PER_WORD
        phase = frame_num % FRAMES_PER_WORD
        t_global = frame_num / total_word_frames
        t_ease = t_global * t_global * (3 - 2 * t_global)
        zoom = ZOOM_START + (ZOOM_END - ZOOM_START) * t_ease

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    for index, token in enumerate(tokens):
        px, py = ox + token["x"], oy + token["y"]

        if all_lit or index < cur:
            img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
            if token["kind"] == "marker":
                img = Image.alpha_composite(img, draw_marker_layer(px, py))
            else:
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )
        elif index == cur:
            if token["kind"] == "marker":
                if phase < FRAMES_FALL + FRAMES_GLOW_IN:
                    img = Image.alpha_composite(
                        img, make_emoji_pop_layer_rgba(token, px, py, phase)
                    )
                else:
                    img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                    img = Image.alpha_composite(img, draw_marker_layer(px, py))
            elif phase < FRAMES_FALL:
                img = Image.alpha_composite(img, make_fall_layer_rgba(token, px, py, phase))
            elif phase < FRAMES_FALL + FRAMES_GLOW_IN:
                t = (phase - FRAMES_FALL) / FRAMES_GLOW_IN
                glow = get_glow_rgba(token, px, py)
                if t < 1.0:
                    r, g, b, a = glow.split()
                    a = ImageEnhance.Brightness(a).enhance(t)
                    glow = Image.merge("RGBA", (r, g, b, a))
                img = Image.alpha_composite(img, glow)
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )
            else:
                img = Image.alpha_composite(img, get_glow_rgba(token, px, py))
                img = Image.alpha_composite(
                    img, draw_word_layer_rgba(token, px, py, (*LIT_COLOR, 255))
                )

    return apply_zoom(img, zoom, W / 2, H / 2)


def next_output_path(out_dir, items):
    words = "_".join("_".join(item.split()[:2]) for item in items[:2])
    slug = "".join(ch if ch.isalnum() or ch == "_" else "" for ch in words)[:40] or "list"
    i = 0
    while os.path.exists(os.path.join(out_dir, f"{slug}_{i}.mov")):
        i += 1
    return os.path.join(out_dir, f"{slug}_{i}.mov")


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python3.11 programme/animated_list_maker_without_background.py "
            '"-first element\\n-second element\\n-third element"'
        )
        sys.exit(1)

    raw_text = sys.argv[1].strip()
    items = parse_list_items(raw_text)
    if not items:
        print("Error: empty list")
        sys.exit(1)

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(project_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = next_output_path(out_dir, items)

    font = load_font(FONT_SIZE)
    tokens, block_w, block_h = layout_items(items, font)
    ox = (W - block_w) // 2
    oy = (H - block_h) // 2

    n_tokens = len(tokens)
    total_frames = n_tokens * FRAMES_PER_WORD + FRAMES_FINAL
    total_duration = total_frames / FPS

    print("List         :")
    for item in items:
        print(f"  {PREFIX_TEXT}{item}")
    print(f"Output       : {out_path}")
    print(f"Format       : transparent ProRes 4444 .mov at {FPS}fps")
    print(f"Items        : {len(items)}")
    print(f"Tokens       : {n_tokens}")
    print(f"Total        : {total_duration:.3f}s ({total_frames} frames)\n")

    frames_dir = tempfile.mkdtemp(prefix="glow_list_alpha_")
    try:
        for frame_num in range(total_frames):
            render_frame(tokens, ox, oy, frame_num, n_tokens).save(
                os.path.join(frames_dir, f"frame_{frame_num:05d}.png")
            )
            if (frame_num + 1) % FPS == 0 or frame_num + 1 == total_frames:
                print(
                    f"  {frame_num + 1}/{total_frames} frames "
                    f"({(frame_num + 1) / total_frames * 100:.0f}%)"
                )

        print("\nEncoding transparent list video...")
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

        print(f"\nDone -> {out_path}")
    finally:
        shutil.rmtree(frames_dir)


if __name__ == "__main__":
    main()
