#!/usr/bin/env python3.11
"""
Composite a transparent background (intro + static PNG + outro) BEHIND each foreground video.
Background layers: intro_shift_paper.mov → middle_shift_papershift.png (held) → outro_shift_paper.mov
Foreground videos are placed on top, preserving full alpha channel.
Output: ProRes 4444 with alpha, placed in numbers_with_transparent_background/
"""

import subprocess
import os
import json

BASE_DIR = "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/key numbers"
ASSETS_DIR = "/Users/mathieusandana/Desktop/code/deployable_auto-montage/zoom_shift_blur_creator/program/zoom_for_insert/assets"

FOREGROUND_VIDEOS = [
    "thermometer.mov",
    "moneycaching.mov",
    "sound.mov",
    "surface_area.mov",
    "vitesse.mov",
    "wieght.mov",
    "20mars.mov",
]

INTRO  = os.path.join(ASSETS_DIR, "intro_shift_paper.mov")
PNG    = os.path.join(ASSETS_DIR, "middle_shift_papershift.png")
OUTRO  = os.path.join(ASSETS_DIR, "outro_shift_paper.mov")

OUTPUT_DIR = os.path.join(BASE_DIR, "numbers_with_transparent_background")


def probe_duration(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-select_streams", "v:0", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    d = json.loads(r.stdout)["streams"][0]
    return float(d.get("duration", 0))


def probe_fps(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-select_streams", "v:0", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    d = json.loads(r.stdout)["streams"][0]
    num, den = d.get("r_frame_rate", "25/1").split("/")
    return float(num) / float(den)


def has_audio(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-select_streams", "a:0", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        streams = json.loads(r.stdout).get("streams", [])
        return len(streams) > 0
    except Exception:
        return False


def process(fg_filename):
    fg_path = os.path.join(BASE_DIR, fg_filename)
    stem = os.path.splitext(fg_filename)[0]
    out_path = os.path.join(OUTPUT_DIR, f"{stem}_bg.mov")

    fg_dur    = probe_duration(fg_path)
    intro_dur = probe_duration(INTRO)
    outro_dur = probe_duration(OUTRO)
    fg_fps    = probe_fps(fg_path)
    mid_dur   = fg_dur - intro_dur - outro_dur

    print(f"\n{'='*60}")
    print(f"  Input : {fg_filename}")
    print(f"  FG    : {fg_dur:.3f}s  @ {fg_fps} fps")
    print(f"  Intro : {intro_dur:.3f}s | Middle: {mid_dur:.3f}s | Outro: {outro_dur:.3f}s")
    print(f"  Output: {out_path}")

    if mid_dur < 0:
        print("  ERROR: foreground shorter than intro+outro combined. Skipping.")
        return False

    # ---------- filter_complex -------------------------------------------------
    # Inputs: [0]=fg  [1]=intro  [2]=PNG(loop)  [3]=outro
    # 1. Normalise all bg pieces to fg fps + yuva444p10le
    # 2. Trim PNG to mid_dur
    # 3. Concat bg: intro → mid_png → outro
    # 4. Trim bg to fg_dur (guards against float rounding)
    # 5. overlay: [bg_trim][fg] → bg behind, fg on top
    # --------------------------------------------------------------------------
    fps_str = f"{int(fg_fps)}" if fg_fps == int(fg_fps) else f"{fg_fps}"

    fc = (
        f"[1:v]fps=fps={fps_str},format=yuva444p10le[intro_v];"
        f"[2:v]trim=duration={mid_dur:.6f},setpts=PTS-STARTPTS,"
        f"fps=fps={fps_str},format=yuva444p10le[mid_v];"
        f"[3:v]fps=fps={fps_str},format=yuva444p10le[outro_v];"
        f"[intro_v][mid_v][outro_v]concat=n=3:v=1:a=0[bg];"
        f"[bg]trim=duration={fg_dur:.6f},setpts=PTS-STARTPTS[bg_trim];"
        f"[bg_trim][0:v]overlay=0:0:shortest=1:format=auto[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", fg_path,           # [0] foreground
        "-i", INTRO,             # [1] intro bg
        "-loop", "1", "-i", PNG, # [2] middle PNG looped
        "-i", OUTRO,             # [3] outro bg
        "-filter_complex", fc,
        "-map", "[out]",
        "-c:v", "prores_ks", "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        "-an",                   # no audio (bg has none; fg anim typically none)
        out_path,
    ]

    print("  Running FFmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  FAILED\n{result.stderr[-3000:]}")
        return False

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"  OK → {size_mb:.1f} MB")
    return True


os.makedirs(OUTPUT_DIR, exist_ok=True)

ok = 0
fail = 0
for f in FOREGROUND_VIDEOS:
    if process(f):
        ok += 1
    else:
        fail += 1

print(f"\n{'='*60}")
print(f"Done: {ok} succeeded, {fail} failed.")
print(f"Outputs in: {OUTPUT_DIR}")
