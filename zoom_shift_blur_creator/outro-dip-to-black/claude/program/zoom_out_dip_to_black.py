#!/usr/bin/env python3.11
import argparse
import json
import subprocess
import sys
import tempfile
import fractions
from pathlib import Path

import cv2
import numpy as np

BASE_DIR = Path(__file__).parent.parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

ZOOM_START = 1.15           # zoom factor at beginning of zoom effect (15% zoomed in)
ZOOM_EFFECT_DURATION = 9.0  # seconds over which zoom returns to 1.0
DIP_DURATION = 3.0          # seconds for fade to black at end


def probe_video(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-select_streams", "v:0", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    w, h = int(stream["width"]), int(stream["height"])
    fps = float(fractions.Fraction(stream.get("r_frame_rate", "30/1")))
    duration = float(data["format"]["duration"])
    return w, h, fps, duration


def extract_all_frames(video_path, width, height):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open: {video_path}")
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height))
        frames.append(frame)
    cap.release()
    return frames


def zoom_frame(frame, zoom, width, height):
    """Scale frame around its centre. zoom > 1 = zoomed in (crops edges)."""
    M = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), 0.0, zoom)
    return cv2.warpAffine(
        frame, M, (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REFLECT_101,
    )


def process_frames(frames, fps, zoom_duration=ZOOM_EFFECT_DURATION, dip_duration=DIP_DURATION):
    total = len(frames)
    width = frames[0].shape[1]
    height = frames[0].shape[0]

    zoom_frames = min(int(round(zoom_duration * fps)), total)
    zoom_start = total - zoom_frames

    dip_frames = min(int(round(dip_duration * fps)), total)
    dip_start = total - dip_frames

    processed = []
    for i, frame in enumerate(frames):
        # --- zoom out (ZOOM_START → 1.0 over last ZOOM_EFFECT_DURATION seconds) ---
        if i < zoom_start:
            zoom = ZOOM_START
        else:
            p = (i - zoom_start) / max(zoom_frames - 1, 1)
            # ease-in-out smooth-step for natural deceleration at start and end
            t = p * p * (3.0 - 2.0 * p)
            zoom = ZOOM_START - (ZOOM_START - 1.0) * t

        out = zoom_frame(frame, zoom, width, height)

        # --- dip to black (fade out over last DIP_DURATION seconds) ---
        if i >= dip_start:
            p = (i - dip_start) / max(dip_frames - 1, 1)
            fade = 1.0 - p
            out = (out.astype(np.float32) * fade).clip(0, 255).astype(np.uint8)

        processed.append(out)

    return processed


def save_frames(frames, directory):
    for idx, frame in enumerate(frames):
        cv2.imwrite(str(directory / f"frame_{idx:05d}.png"), frame)


def encode(frames_dir, input_path, output_path, fps):
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner", "-loglevel", "error",
        "-framerate", f"{fps:.6f}",
        "-i", str(frames_dir / "frame_%05d.png"),
        "-i", str(input_path),
        "-map", "0:v",
        "-map", "1:a?",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR:\n{result.stderr[-1000:]}")
        return False
    return True


def process_video(input_path, output_path, zoom_duration=ZOOM_EFFECT_DURATION, dip_duration=DIP_DURATION):
    print(f"Processing: {input_path.name}")
    width, height, fps, duration = probe_video(input_path)
    print(f"  {width}x{height} | {duration:.2f}s | {fps:.2f}fps")
    print(f"  zoom={zoom_duration:.2f}s dip={dip_duration:.2f}s")

    frames = extract_all_frames(input_path, width, height)
    print(f"  Extracted {len(frames)} frames — applying effects …")

    processed = process_frames(frames, fps, zoom_duration=zoom_duration, dip_duration=dip_duration)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zoom_dip_") as tmp:
        tmp_path = Path(tmp)
        save_frames(processed, tmp_path)
        success = encode(tmp_path, input_path, output_path, fps)

    if success:
        print(f"  -> {output_path}")
    return success


def parse_args():
    parser = argparse.ArgumentParser(
        description="Apply zoom-out + fade-to-black outro effect."
    )
    parser.add_argument("--input", type=Path, help="Single input video. If omitted, batch-process INPUT_DIR.")
    parser.add_argument("--output", type=Path, help="Single output path. Required if --input is given.")
    parser.add_argument("--zoom-duration", type=float, default=ZOOM_EFFECT_DURATION,
                        help="Seconds over which zoom returns to 1.0 (default %(default)s).")
    parser.add_argument("--dip-duration", type=float, default=DIP_DURATION,
                        help="Seconds for fade-to-black at end (default %(default)s).")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.input is not None:
        if args.output is None:
            print("ERROR: --output is required when --input is given", file=sys.stderr)
            sys.exit(2)
        if not args.input.is_file():
            print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
            sys.exit(2)
        ok = process_video(
            args.input, args.output,
            zoom_duration=args.zoom_duration, dip_duration=args.dip_duration,
        )
        sys.exit(0 if ok else 1)

    extensions = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mts", ".m4v"}
    videos = sorted(f for f in INPUT_DIR.iterdir() if f.suffix.lower() in extensions)

    if not videos:
        print(f"No video files found in {INPUT_DIR}")
        sys.exit(1)

    for video in videos:
        out_name = f"{video.stem}_0{video.suffix}"
        process_video(
            video, OUTPUT_DIR / out_name,
            zoom_duration=args.zoom_duration, dip_duration=args.dip_duration,
        )


if __name__ == "__main__":
    main()
