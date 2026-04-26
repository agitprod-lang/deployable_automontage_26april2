#!/usr/bin/env python3.11
"""
tweet_screen_to_blur_mosaic_video.py

Takes a tweet screenshot and produces a transparent ProRes 4444 .mov:
  - Tweet appears via mosaic pixelation dissolve (blocks shrink to clear image)
  - Holds centered in lower half of screen for 4 seconds
  - Disappears via mosaic pixelation dissolve (image breaks into blocks)

Usage:
    python3.11 tweet_screen_to_blur_mosaic_video.py <tweet.png> [output.mov]
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
ENTRY_FRAMES  = 30        # ~0.5 s
STAY_FRAMES   = 4 * FPS   # 4 s
EXIT_FRAMES   = 30        # ~0.5 s

MAX_BLOCK     = 48        # largest mosaic block size (pixels) at peak pixelation

CLICK_SOUND_PATH = os.path.expanduser(
    "~/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/Mouse Click Sound Effect.mp3"
)


# ── Easing ───────────────────────────────────────────────────────────────────
def ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3

def ease_in(t: float) -> float:
    return t ** 3


# ── Image helpers ─────────────────────────────────────────────────────────────
def load_tweet(path: str) -> np.ndarray:
    """Load tweet PNG and scale (up or down) to fill the lower half bounds."""
    img = Image.open(path).convert("RGBA")
    max_w = int(CANVAS_W * 0.95)
    max_h = int(CANVAS_H * 0.48)
    ratio = min(max_w / img.width, max_h / img.height)
    new_w = int(img.width * ratio)
    new_h = int(img.height * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    return np.array(img, dtype=np.uint8)   # shape (H, W, 4) RGBA


def apply_alpha(img: np.ndarray, factor: float) -> np.ndarray:
    """Scale the alpha channel of an RGBA image by `factor` (0.0–1.0)."""
    out = img.copy()
    out[:, :, 3] = (out[:, :, 3].astype(np.float32) * factor).clip(0, 255).astype(np.uint8)
    return out


def pixelate(img: np.ndarray, block_size: int) -> np.ndarray:
    """
    Downscale + upscale with nearest-neighbor to create a hard mosaic block effect.
    block_size=1 returns the original image unchanged.
    """
    if block_size <= 1:
        return img
    h, w = img.shape[:2]
    small_w = max(1, w // block_size)
    small_h = max(1, h // block_size)
    small = cv2.resize(img, (small_w, small_h), interpolation=cv2.INTER_LINEAR)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


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

    # ── Entry: mosaic dissolves + fast fade in ───────────────────────────────
    for i in range(ENTRY_FRAMES):
        t      = (i + 1) / ENTRY_FRAMES
        t_e    = ease_out(t)              # fast resolve
        block  = int(MAX_BLOCK * (1.0 - t_e))
        fade   = min(1.0, t_e ** 0.5)    # sqrt for very fast snap to opaque
        mosaic = apply_alpha(pixelate(tweet, block), fade)
        yield paste_onto_canvas(mosaic, rest_x, rest_y)

    # ── Stay: hold still ────────────────────────────────────────────────────
    static = paste_onto_canvas(tweet, rest_x, rest_y)
    for _ in range(STAY_FRAMES):
        yield static

    # ── Exit: mosaic breaks up + fast fade out ───────────────────────────────
    for i in range(EXIT_FRAMES):
        t      = (i + 1) / EXIT_FRAMES
        t_e    = ease_in(t)               # slow start, fast break-up at the end
        block  = int(MAX_BLOCK * t_e)
        fade   = max(0.0, 1.0 - t_e ** 0.5)   # mirror of entry
        mosaic = apply_alpha(pixelate(tweet, block), fade)
        yield paste_onto_canvas(mosaic, rest_x, rest_y)


# ── FFmpeg writer ─────────────────────────────────────────────────────────────
def write_mov(frame_gen, total: int, output_path: str, audio_path: str | None = None) -> int:
    """Stream raw RGBA frames into FFmpeg → ProRes 4444 with alpha."""
    cmd = [
        "ffmpeg", "-y",
        "-f",       "rawvideo",
        "-vcodec",  "rawvideo",
        "-s",       f"{CANVAS_W}x{CANVAS_H}",
        "-pix_fmt", "rgba",
        "-r",       str(FPS),
        "-i",       "pipe:0",
    ]
    has_audio = bool(audio_path and os.path.exists(audio_path))
    if has_audio:
        cmd += ["-i", audio_path]
    cmd += [
        "-vcodec",  "prores_ks",
        "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        "-vendor",  "apl0",
    ]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(output_path)
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
        print("Usage: python3.11 tweet_screen_to_blur_mosaic_video.py <tweet.png> [output.mov]")
        sys.exit(1)

    input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        base    = os.path.splitext(os.path.basename(input_path))[0]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, base + "_mosaic.mov")

    print(f"Input : {input_path}")
    print(f"Output: {output_path}")

    tweet = load_tweet(input_path)
    print(f"Tweet : {tweet.shape[1]}x{tweet.shape[0]} px")

    total = ENTRY_FRAMES + STAY_FRAMES + EXIT_FRAMES
    print(f"Frames: {total}  ({total / FPS:.1f} s @ {FPS} fps)")
    print("Encoding...")

    ret = write_mov(generate_frames(tweet), total, output_path,
                    audio_path=CLICK_SOUND_PATH if os.path.exists(CLICK_SOUND_PATH) else None)

    if ret == 0:
        print(f"Done → {output_path}")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
