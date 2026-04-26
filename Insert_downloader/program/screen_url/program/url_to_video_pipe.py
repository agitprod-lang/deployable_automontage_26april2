#!/usr/bin/env python3
"""
url_to_video_pipe.py  —  URL → screenshot PNG → animated MOV

Usage:
    python3.11 url_to_video_pipe.py https://example.com
    python3.11 url_to_video_pipe.py https://example.com -o /path/to/out.mov
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCREEN_URL   = SCRIPT_DIR / "screen_url_2.py"
IMG2VID      = SCRIPT_DIR / "url_screenshot_img2vid.py"
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
OUTPUT_DIR   = PROJECT_ROOT / "output"

PYTHON = sys.executable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a URL screenshot and turn it into an animated MOV."
    )
    parser.add_argument("url", help="URL to capture.")
    parser.add_argument(
        "-o", "--output",
        help="Output video path. Defaults to /output/<slug>_anim.mov",
    )
    parser.add_argument(
        "--wait-ms", type=int, default=2500,
        help="Extra wait after page load before screenshot (default 2500).",
    )
    return parser.parse_args()


def run(cmd: list[str]) -> str:
    """Run a subprocess, print its stderr live, return stdout stripped."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result.stdout.strip()


def main() -> int:
    args = parse_args()

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="url2vid_") as tmp:
            png_path = Path(tmp) / "screenshot.png"

            # Step 1 — capture URL → PNG
            print(f"[1/2] Capturing {args.url} …")
            run([
                PYTHON, str(SCREEN_URL),
                args.url,
                "-o", str(png_path),
                "--wait-ms", str(args.wait_ms),
            ])

            # Step 2 — animate PNG → MOV
            print("[2/2] Generating video …")
            img2vid_cmd = [
                PYTHON, str(IMG2VID),
                str(png_path),
                "--url", args.url,
            ]
            if args.output:
                img2vid_cmd += ["-o", args.output]

            output_path = run(img2vid_cmd)

        print(f"\nDone → {output_path}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
