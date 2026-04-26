#!/usr/bin/env python3.11
"""
small_renderer.py — HLG → SDR conversion comparison

Generates 3 test clips from the first 2 seconds of the rush at 1920×1080,
using three different HLG→SDR approaches so you can pick the best one.

Outputs (in ./output/):
  test_a_raw.mp4      — no conversion (raw HLG treated as SDR, baseline)
  test_b_zscale.mp4   — current approach: zscale transfer=bt709 (overexposed)
  test_c_tonemap.mp4  — proper HDR tonemap: linear → hable → bt709 (recommended)

Usage:
  python3.11 small_renderer.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RUSH_DIR = Path("~/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush").expanduser()
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

CLIP_DURATION = 2.0
CANVAS_W = 1920
CANVAS_H = 1080
FPS = 24

# 4K → 1080p center-crop offset: x=(1920-3840)/2=-960, y=(1080-2160)/2=-540
OVERLAY_X = "-960"
OVERLAY_Y = "-540"


def find_rush() -> Path:
    candidates = [p for p in RUSH_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".mxf"}]
    if not candidates:
        raise FileNotFoundError(f"No video found in {RUSH_DIR}")
    if len(candidates) > 1:
        raise FileExistsError(f"Multiple videos found: {[p.name for p in candidates]}")
    return candidates[0]


def run(label: str, cmd: list[str]) -> None:
    print(f"[{label}] Running…")
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        print(f"[{label}] ERROR: ffmpeg exited {r.returncode}", file=sys.stderr)
    else:
        print(f"[{label}] Done.")


def render_test(rush: Path, out: Path, overlay_vf: str) -> None:
    """Render 2-second clip: black canvas + rush via given overlay_vf."""
    filter_complex = (
        f"[0:v]format=yuv420p[base];"
        f"[1:v]{overlay_vf}[ov];"
        f"[base][ov]overlay=x='{OVERLAY_X}':y='{OVERLAY_Y}'"
        f":eof_action=pass:repeatlast=0:format=auto[out]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={FPS}:d={CLIP_DURATION}",
        "-t", f"{CLIP_DURATION}", "-i", str(rush),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-colorspace", "1", "-color_primaries", "1", "-color_trc", "1",
        "-movflags", "+faststart", str(out),
    ]
    run(out.name, cmd)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rush = find_rush()
    print(f"Rush: {rush.name}")

    # ── A: Raw — no color conversion (HLG values treated as BT.709)
    #    Shows the "flat/wrong" baseline; useful to confirm source is fine.
    render_test(
        rush,
        OUTPUT_DIR / "test_a_raw.mp4",
        overlay_vf="format=rgba,setpts=PTS-STARTPTS",
    )

    # ── B: Current approach — zscale direct HLG→BT.709 (produces overexposure)
    render_test(
        rush,
        OUTPUT_DIR / "test_b_zscale.mp4",
        overlay_vf=(
            "zscale=transfer=bt709:primaries=bt709:matrix=bt709"
            ":rangein=limited:range=limited,format=rgba,setpts=PTS-STARTPTS"
        ),
    )

    # ── C: Proper tone mapping — linear intermediary + Hable curve + bt709 out
    #    This is the standard HDR→SDR pipeline for HLG iPhone footage.
    render_test(
        rush,
        OUTPUT_DIR / "test_c_tonemap.mp4",
        overlay_vf=(
            "zscale=transfer=linear:primaries=bt709:matrix=bt709"
            ":rangein=limited:range=pc:npl=100,"
            "format=gbrpf32le,"
            "zscale=primaries=bt709,"
            "tonemap=tonemap=hable:desat=0,"
            "zscale=transfer=bt709:matrix=bt709:range=tv,"
            "format=yuv420p,"
            "setpts=PTS-STARTPTS"
        ),
    )

    print(f"\nOutputs in: {OUTPUT_DIR}")
    print("  test_a_raw.mp4    — raw HLG as SDR (baseline)")
    print("  test_b_zscale.mp4 — direct zscale (current, overexposed)")
    print("  test_c_tonemap.mp4— linear+hable tonemap (recommended)")


if __name__ == "__main__":
    main()
