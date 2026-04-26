#!/usr/bin/env python3
"""Render a simple clip showing a yellow rectangle expanding left-to-right."""

from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - Pillow should be available
        raise SystemExit("Pillow is required: pip install pillow") from exc
    width = 640
    height = 360
    fps = 30
    duration = 3.0
    box_height = 70
    box_y = (height - box_height) // 2
    box_x = 80
    target_width = width - box_x * 2
    start_hold = 0.5
    growth_time = 2.0
    total_frames = int(duration * fps)
    output = Path(__file__).with_name("yellow_box_expanding_right.mov")
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "4444",
        "-pix_fmt",
        "yuva444p10le",
        "-t",
        f"{duration:.2f}",
        str(output),
    ]
    process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
    assert process.stdin is not None
    for frame_idx in range(total_frames):
        time = frame_idx / fps
        progress = 0.0
        if time >= start_hold:
            progress = min((time - start_hold) / growth_time, 1.0)
        current_width = int(target_width * progress)
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        if current_width > 0:
            draw = ImageDraw.Draw(image)
            draw.rectangle(
                (
                    box_x,
                    box_y,
                    box_x + current_width,
                    box_y + box_height,
                ),
                fill=(0xFF, 0xD3, 0x4A, int(0.95 * 255)),
            )
        process.stdin.write(image.tobytes())
    process.stdin.close()
    process.wait()
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
