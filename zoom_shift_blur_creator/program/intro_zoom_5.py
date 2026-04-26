#!/usr/bin/env python3
"""
intro_zoom_5.py

Punchy glitch-zoom intro effect on the first N frames of a video,
with directional (radial) zoom blur + optional sound effect.

Effects (all ramp based on frame progress):
  - Reverse zoom-out      (zoom_end → 1.0) — starts zoomed in, settles to normal
  - Spin-in               (start_angle → 0°) — starts rotated, settles upright
  - Directional zoom blur — accumulation-buffer radial blur in the zoom direction
  - Chromatic aberration  (RGB channel split)
  - Motion blur           (Gaussian, heavy at start)
  - Digital noise         (grain, heavier at start)
  - Horizontal glitch scanlines

Usage:
    python3.11 intro_zoom_5.py input.mp4 --sfx woosh.mp3 --overwrite
    python3.11 intro_zoom_5.py input.mp4 --sfx woosh.mp3 --zoom-end 1.5 --blur-steps 8 --overwrite
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

def positive_float(v: str) -> float:
    x = float(v)
    if x <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return x


def positive_int(v: str) -> int:
    x = int(v)
    if x <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return x


def clamped_float(v: str) -> float:
    x = float(v)
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------------------
# FFprobe helpers
# ---------------------------------------------------------------------------

def probe_video(ffprobe_bin: str, path: Path) -> tuple[int, int, float]:
    """Return (width, height, fps)."""
    cmd = [
        ffprobe_bin, "-v", "quiet", "-print_format", "json",
        "-select_streams", "v:0", "-show_streams", str(path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    stream = json.loads(result.stdout)["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    r = stream.get("r_frame_rate", "30/1")
    num, den = r.split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return w, h, fps


# ---------------------------------------------------------------------------
# Frame extraction
# ---------------------------------------------------------------------------

def extract_frames(video_path: Path, n: int) -> list[np.ndarray]:
    """Read first n frames from video using OpenCV. Returns BGR frames."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")
    frames: list[np.ndarray] = []
    while len(frames) < n:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    if not frames:
        raise RuntimeError("Could not read any frames from input.")
    return frames


# ---------------------------------------------------------------------------
# Individual effects
# ---------------------------------------------------------------------------

def _warp(frame: np.ndarray, zoom: float, angle_deg: float, width: int, height: int) -> np.ndarray:
    cx, cy = width / 2.0, height / 2.0
    M = cv2.getRotationMatrix2D((cx, cy), angle_deg, zoom)
    return cv2.warpAffine(
        frame, M, (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT_101,
    )


def zoom_blur_frame(
    frame: np.ndarray,
    zoom: float,
    angle_deg: float,
    width: int,
    height: int,
    blur_steps: int = 6,
    blur_spread: float = 0.08,
) -> np.ndarray:
    """
    Directional (radial) zoom blur via accumulation buffer.

    Technique: sample `blur_steps` slightly-different zoom levels spanning
    [zoom, zoom + blur_spread], then blend with linearly decreasing weights
    (current zoom = highest weight, origin zoom = lowest).

    Since we're zooming OUT (zoom_end → 1.0), the "trail" direction is toward
    higher zoom values (where the frame just came from), so we sample from
    zoom up to zoom + blur_spread. This produces streaks radiating outward
    from the centre — classic radial motion blur matching the zoom direction.
    """
    if blur_steps <= 1 or blur_spread <= 0:
        return _warp(frame, zoom, angle_deg, width, height)

    # weights: linear falloff — current position has full weight, trail fades
    weights = np.linspace(1.0, 0.25, blur_steps)
    weights /= weights.sum()

    acc = np.zeros((height, width, 3), dtype=np.float32)
    for i, w in enumerate(weights):
        t = i / (blur_steps - 1)                  # 0 = current, 1 = trail origin
        z = zoom + blur_spread * t                 # sample toward more-zoomed (trail)
        a = angle_deg + (blur_spread * 8.0) * t   # tiny co-rotation in trail
        warped = _warp(frame, z, a, width, height).astype(np.float32)
        acc += warped * w

    return acc.clip(0, 255).astype(np.uint8)


def chromatic_aberration(frame: np.ndarray, shift: int) -> np.ndarray:
    """Shift R channel right, B channel left — classic chroma split glitch."""
    if shift <= 0:
        return frame
    b, g, r = cv2.split(frame)
    h, w = frame.shape[:2]
    # Shift using numpy roll then clamp edges (no wrap-around)
    r_shifted = np.roll(r, shift, axis=1)
    b_shifted = np.roll(b, -shift, axis=1)
    # Fill edge columns instead of wrap-around
    r_shifted[:, :shift] = r[:, :1]
    b_shifted[:, w - shift:] = b[:, w - 1:]
    return cv2.merge([b_shifted, g, r_shifted])


def motion_blur(frame: np.ndarray, radius: int) -> np.ndarray:
    """Gaussian blur to simulate motion smear."""
    if radius <= 0:
        return frame
    k = radius * 2 + 1  # must be odd
    return cv2.GaussianBlur(frame, (k, k), 0)


def digital_noise(frame: np.ndarray, std: float, rng: np.random.Generator) -> np.ndarray:
    """Add Gaussian noise grain."""
    if std < 0.5:
        return frame
    noise = rng.normal(0.0, std, frame.shape).astype(np.int16)
    return np.clip(frame.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def glitch_scanlines(frame: np.ndarray, n_lines: int, max_shift: int, rng: np.random.Generator) -> np.ndarray:
    """Shift random horizontal scanlines sideways — digital corruption look."""
    if n_lines <= 0 or max_shift <= 0:
        return frame
    result = frame.copy()
    h = frame.shape[0]
    ys = rng.integers(0, h, size=n_lines)
    shifts = rng.integers(-max_shift, max_shift + 1, size=n_lines)
    for y, dx in zip(ys, shifts):
        if dx != 0:
            result[y] = np.roll(result[y], int(dx), axis=0)
    return result


# ---------------------------------------------------------------------------
# Per-frame processing
# ---------------------------------------------------------------------------

def process_intro_frames(
    frames: list[np.ndarray],
    zoom_end: float,
    start_angle: float,
    glitch_intensity: float,
    blur_max: int,
    chroma_max: int,
    width: int,
    height: int,
    zoom_blur_steps: int = 6,
    zoom_blur_spread: float = 0.08,
    seed: int = 42,
) -> list[np.ndarray]:
    """
    Apply zoom-out + spin-in + directional zoom blur + glitch effects.

    Zoom        : zoom_end → 1.0  (starts zoomed in, settles to normal)
    Angle       : start_angle → 0° (easeOut spin, snaps into place)
    Zoom blur   : accumulation-buffer radial blur, spread scales with glitch weight
    Glitch      : heaviest at START, clears by end
    """
    rng = np.random.default_rng(seed)
    n = len(frames)
    processed: list[np.ndarray] = []

    for i, frame in enumerate(frames):
        progress = i / max(n - 1, 1)  # 0.0 → 1.0

        # --- Reverse zoom: zoom_end → 1.0 ---
        zoom = zoom_end - (zoom_end - 1.0) * progress

        # --- Spin-in: start_angle → 0° (easeOut) ---
        ease = 1.0 - (1.0 - progress) ** 2
        angle = start_angle * (1.0 - ease)

        # --- Glitch weight: 1.0 at frame 0, 0.0 at last frame ---
        w = (1.0 - progress) * glitch_intensity

        # --- Directional zoom blur — spread scales with glitch weight ---
        effective_spread = zoom_blur_spread * (0.3 + 0.7 * w)  # always some blur, more at start
        out = zoom_blur_frame(
            frame, zoom, angle, width, height,
            blur_steps=zoom_blur_steps,
            blur_spread=effective_spread,
        )

        if w > 0.01:
            # Chromatic aberration
            chroma_shift = int(round(w * chroma_max))
            out = chromatic_aberration(out, chroma_shift)

            # Motion blur
            blur_radius = int(round(w * blur_max))
            out = motion_blur(out, blur_radius)

            # Digital noise
            noise_std = w * 35.0
            out = digital_noise(out, noise_std, rng)

            # Glitch scanlines
            n_lines = max(0, int(round(w * 8)))
            max_shift = max(0, int(round(w * 50)))
            out = glitch_scanlines(out, n_lines, max_shift, rng)

        processed.append(out)

    return processed


# ---------------------------------------------------------------------------
# Save processed frames as PNGs
# ---------------------------------------------------------------------------

def save_frames(frames: list[np.ndarray], directory: Path) -> None:
    for idx, frame in enumerate(frames):
        cv2.imwrite(str(directory / f"frame_{idx:05d}.png"), frame)


# ---------------------------------------------------------------------------
# FFmpeg encode + concat
# ---------------------------------------------------------------------------

def encode_and_concat(
    ffmpeg_bin: str,
    frames_dir: Path,
    n_intro_frames: int,
    original_path: Path,
    output_path: Path,
    fps: float,
    width: int,
    height: int,
    crf: int,
    preset: str,
    overwrite: bool,
    sfx_path: Path | None = None,
    sfx_volume: float = 1.0,
) -> None:
    """
    FFmpeg inputs:
      0 = PNG sequence  (processed intro frames, video only)
      1 = original video (video + original audio)
      2 = SFX audio file (optional, mixed at t=0)

    Video filter:
      intro PNG → scale/setsar → concat with trimmed rest → [outv]

    Audio filter (when sfx provided):
      original audio + sfx (at t=0) → amix → [outa]
    Audio (no sfx):
      original audio passthrough, copy codec
    """
    rest_start = n_intro_frames / fps  # seconds into original where rest begins

    # --- video filter (same regardless of sfx) ---
    vf = (
        f"[0:v]scale={width}:{height},setsar=1[intro_v];"
        f"[1:v]trim=start={rest_start:.6f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height},setsar=1[rest_v];"
        f"[intro_v][rest_v]concat=n=2:v=1:a=0[outv]"
    )

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "error",
        "-y" if overwrite else "-n",
        # input 0: PNG sequence
        "-framerate", f"{fps:.6f}",
        "-i", str(frames_dir / "frame_%05d.png"),
        # input 1: original video
        "-i", str(original_path),
    ]

    if sfx_path is not None:
        # input 2: SFX
        cmd += ["-i", str(sfx_path)]

        # audio filter: normalise both to stereo fltp, mix, keep original duration
        af = (
            f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[orig_a];"
            f"[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume={sfx_volume:.4f}[sfx_a];"
            f"[orig_a][sfx_a]amix=inputs=2:duration=first:normalize=0[outa]"
        )
        filter_complex = vf + ";" + af

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        # no sfx — copy original audio unchanged
        cmd += [
            "-filter_complex", vf,
            "-map", "[outv]",
            "-map", "1:a?",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "copy",
        ]

    cmd.append(str(output_path))

    print("Encoding …")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print("ffmpeg failed.", file=sys.stderr)
        print("Command:", " ".join(cmd), file=sys.stderr)
        raise SystemExit(exc.returncode) from exc


# ---------------------------------------------------------------------------
# Output path helper
# ---------------------------------------------------------------------------

def build_output_path(input_path: Path) -> Path:
    stem = f"{input_path.stem}_spin_zoom_blur{input_path.suffix}"
    if input_path.parent.name == "input":
        out_dir = input_path.parent.parent / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / stem
    return input_path.with_name(stem)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a fast glitch-zoom effect to the first frames of a video."
    )
    parser.add_argument("input", help="Input video path")
    parser.add_argument("-o", "--output", help="Output video path (auto if omitted)")
    parser.add_argument(
        "--intro-frames", type=positive_int, default=10,
        help="Number of frames to apply the effect to (default: 10)",
    )
    parser.add_argument(
        "--zoom-end", type=positive_float, default=1.35,
        help="Starting zoom multiplier (frame 0) — settles back to 1.0 (default: 1.35)",
    )
    parser.add_argument(
        "--angle", type=float, default=45.0,
        help="Starting rotation in degrees — settles to 0° by last frame (default: 45)",
    )
    parser.add_argument(
        "--glitch", type=clamped_float, default=0.75,
        help="Glitch intensity 0.0–1.0 (default: 0.75)",
    )
    parser.add_argument(
        "--blur-max", type=positive_int, default=6,
        help="Max Gaussian blur radius in pixels at peak glitch (default: 6)",
    )
    parser.add_argument(
        "--blur-steps", type=positive_int, default=6,
        help="Number of accumulation samples for directional zoom blur (default: 6)",
    )
    parser.add_argument(
        "--blur-spread", type=positive_float, default=0.08,
        help="Zoom spread per accumulation sample — larger = wider blur trail (default: 0.08)",
    )
    parser.add_argument(
        "--chroma-max", type=positive_int, default=12,
        help="Max chromatic aberration shift in pixels (default: 12)",
    )
    parser.add_argument(
        "--crf", type=positive_int, default=18,
        help="x264 CRF quality (default: 18)",
    )
    parser.add_argument(
        "--preset", default="medium",
        choices=["ultrafast","superfast","veryfast","faster","fast",
                 "medium","slow","slower","veryslow"],
    )
    parser.add_argument(
        "--sfx",
        help="Path to a sound effect file (mp3/wav/…) mixed at t=0 (optional)",
    )
    parser.add_argument(
        "--sfx-volume", type=float, default=1.0,
        help="Volume multiplier for the SFX (default: 1.0)",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    for binary in (args.ffmpeg, args.ffprobe):
        if shutil.which(binary) is None and not Path(binary).is_file():
            print(f"Error: {binary} not found.", file=sys.stderr)
            sys.exit(1)

    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        print(f"Error: input not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output else build_output_path(input_path)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not args.overwrite:
        print(
            f"Error: output exists: {output_path}\nUse --overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Probe
    width, height, fps = probe_video(args.ffprobe, input_path)
    print(f"Input : {input_path.name}  {width}x{height}  {fps:.3f} fps")
    sfx_path = Path(args.sfx).expanduser().resolve() if args.sfx else None
    if sfx_path is not None and not sfx_path.is_file():
        print(f"Error: SFX file not found: {sfx_path}", file=sys.stderr)
        sys.exit(1)

    sfx_label = sfx_path.name if sfx_path else "none"
    print(f"Effect: {args.intro_frames} frames | zoom {args.zoom_end}→1.0 | angle {args.angle}°→0° | glitch={args.glitch} | zblur steps={args.blur_steps} spread={args.blur_spread} | sfx={sfx_label}")

    # Extract frames
    print(f"Extracting first {args.intro_frames} frames …")
    frames = extract_frames(input_path, args.intro_frames)
    actual_n = len(frames)
    if actual_n < args.intro_frames:
        print(
            f"Warning: only {actual_n} frames available (requested {args.intro_frames}).",
            file=sys.stderr,
        )

    # Apply effects
    print("Applying effects …")
    processed = process_intro_frames(
        frames=frames,
        zoom_end=args.zoom_end,
        start_angle=args.angle,
        glitch_intensity=args.glitch,
        blur_max=args.blur_max,
        chroma_max=args.chroma_max,
        width=width,
        height=height,
        zoom_blur_steps=args.blur_steps,
        zoom_blur_spread=args.blur_spread,
    )

    # Write PNGs to temp dir, then encode
    with tempfile.TemporaryDirectory(prefix="glitch_zoom_") as tmp:
        tmp_path = Path(tmp)
        print("Saving processed frames …")
        save_frames(processed, tmp_path)

        encode_and_concat(
            ffmpeg_bin=args.ffmpeg,
            frames_dir=tmp_path,
            n_intro_frames=actual_n,
            original_path=input_path,
            output_path=output_path,
            fps=fps,
            width=width,
            height=height,
            crf=args.crf,
            preset=args.preset,
            overwrite=args.overwrite,
            sfx_path=sfx_path,
            sfx_volume=args.sfx_volume,
        )

    print(f"Done  : {output_path}")


if __name__ == "__main__":
    main()
