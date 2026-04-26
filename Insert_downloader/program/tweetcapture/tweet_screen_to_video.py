#!/usr/bin/env python3.11
"""
tweet_screen_to_video.py

Takes a tweet screenshot and produces a transparent ProRes 4444 .mov:
  - Tweet slides UP fast with vertical motion blur
  - Holds centered in lower half of screen for 4 seconds
  - Slides DOWN fast with vertical motion blur

Usage:
    python3.11 tweet_screen_to_video.py <tweet.png> [output.mov]
"""

import sys
import os
import subprocess
import numpy as np
import cv2
from PIL import Image

# ── Config ──────────────────────────────────────────────────────────────────
CANVAS_W      = 1920
CANVAS_H      = 1080
FPS           = 60
ENTRY_FRAMES  = 20        # ~0.33 s
STAY_FRAMES   = 4 * FPS   # 4 s
EXIT_FRAMES   = 20        # ~0.33 s


# ── Easing ───────────────────────────────────────────────────────────────────
def ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3

def ease_in(t: float) -> float:
    return t ** 3


# ── Image helpers ─────────────────────────────────────────────────────────────
def load_tweet(path: str) -> np.ndarray:
    """Load tweet PNG and scale (up or down) to fill the lower half bounds."""
    img = Image.open(path).convert("RGBA")
    max_w = int(CANVAS_W * 0.90)
    max_h = int(CANVAS_H * 0.45)
    ratio = min(max_w / img.width, max_h / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    return np.array(img, dtype=np.uint8)   # shape (H, W, 4) RGBA


def vertical_motion_blur(img: np.ndarray, blur_px: int) -> np.ndarray:
    """
    Apply a vertical motion-blur kernel of `blur_px` pixels.
    Simulates fast upward/downward movement by smearing the image along Y.
    """
    if blur_px < 2:
        return img
    size = max(3, int(blur_px) | 1)          # nearest odd number ≥ blur_px
    kernel = np.zeros((size, size), dtype=np.float32)
    kernel[:, size // 2] = 1.0 / size        # vertical line kernel
    # filter each channel separately to keep alpha correct
    out = np.empty_like(img)
    for c in range(4):
        out[:, :, c] = cv2.filter2D(img[:, :, c].astype(np.float32),
                                     -1, kernel).clip(0, 255).astype(np.uint8)
    return out


def paste_onto_canvas(tweet: np.ndarray, pos_x: int, pos_y: int) -> np.ndarray:
    """Composite `tweet` (RGBA) onto a fully transparent canvas at (pos_x, pos_y)."""
    canvas = np.zeros((CANVAS_H, CANVAS_W, 4), dtype=np.uint8)
    th, tw = tweet.shape[:2]

    src_x0 = max(0, -pos_x);  dst_x0 = max(0, pos_x)
    src_y0 = max(0, -pos_y);  dst_y0 = max(0, pos_y)
    copy_w = min(tw - src_x0, CANVAS_W - dst_x0)
    copy_h = min(th - src_y0, CANVAS_H - dst_y0)

    if copy_w > 0 and copy_h > 0:
        canvas[dst_y0:dst_y0 + copy_h, dst_x0:dst_x0 + copy_w] = \
            tweet[src_y0:src_y0 + copy_h, src_x0:src_x0 + copy_w]
    return canvas


# ── Frame generator ───────────────────────────────────────────────────────────
def generate_frames(tweet: np.ndarray):
    th, tw = tweet.shape[:2]

    rest_x = (CANVAS_W - tw) // 2
    # Vertically centered in the lower half (y > CANVAS_H/2)
    rest_y = CANVAS_H // 2 + (CANVAS_H // 2 - th) // 2

    # Off-screen: tweet fully below the canvas
    start_y = CANVAS_H
    travel  = abs(rest_y - start_y)          # pixels to travel

    # ── Entry: slide up ──────────────────────────────────────────────────────
    for i in range(ENTRY_FRAMES):
        t      = (i + 1) / ENTRY_FRAMES
        t_e    = ease_out(t)
        y      = int(start_y + (rest_y - start_y) * t_e)
        # speed ∝ derivative of ease_out = 3(1-t)²
        speed  = travel * 3.0 * (1.0 - t) ** 2 / ENTRY_FRAMES
        blur   = int(speed * 1.2)            # slight amplification for drama
        blurred = vertical_motion_blur(tweet, blur)
        yield paste_onto_canvas(blurred, rest_x, y)

    # ── Stay: hold still ────────────────────────────────────────────────────
    static = paste_onto_canvas(tweet, rest_x, rest_y)
    for _ in range(STAY_FRAMES):
        yield static

    # ── Exit: slide down ─────────────────────────────────────────────────────
    for i in range(EXIT_FRAMES):
        t      = (i + 1) / EXIT_FRAMES
        t_e    = ease_in(t)
        y      = int(rest_y + (start_y - rest_y) * t_e)
        # speed ∝ derivative of ease_in = 3t²
        speed  = travel * 3.0 * t ** 2 / EXIT_FRAMES
        blur   = int(speed * 1.2)
        blurred = vertical_motion_blur(tweet, blur)
        yield paste_onto_canvas(blurred, rest_x, y)


# ── FFmpeg writer ─────────────────────────────────────────────────────────────
def write_mov(frame_gen, total: int, output_path: str) -> int:
    """Stream raw RGBA frames into FFmpeg → ProRes 4444 with alpha."""
    cmd = [
        "ffmpeg", "-y",
        "-f",       "rawvideo",
        "-vcodec",  "rawvideo",
        "-s",       f"{CANVAS_W}x{CANVAS_H}",
        "-pix_fmt", "rgba",
        "-r",       str(FPS),
        "-i",       "pipe:0",
        "-vcodec",  "prores_ks",
        "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        "-vendor",  "apl0",
        output_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for n, frame in enumerate(frame_gen, 1):
        proc.stdin.write(frame.tobytes())
        print(f"  encoding frame {n}/{total}", end="\r", flush=True)
    proc.stdin.close()
    ret = proc.wait()
    print()
    if ret != 0:
        print("FFmpeg encoding failed.", file=sys.stderr)
    return ret


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Usage: python3.11 tweet_screen_to_video.py <tweet.png> [output.mov]")
        sys.exit(1)

    input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        base    = os.path.splitext(os.path.basename(input_path))[0]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, base + ".mov")

    print(f"Input : {input_path}")
    print(f"Output: {output_path}")

    tweet = load_tweet(input_path)
    print(f"Tweet : {tweet.shape[1]}x{tweet.shape[0]} px")

    total = ENTRY_FRAMES + STAY_FRAMES + EXIT_FRAMES
    print(f"Frames: {total}  ({total / FPS:.1f} s @ {FPS} fps)")
    print("Encoding...")

    ret = write_mov(generate_frames(tweet), total, output_path)

    if ret == 0:
        print(f"Done → {output_path}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
