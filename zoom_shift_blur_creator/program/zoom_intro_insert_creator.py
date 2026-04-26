#!/usr/bin/env python3
"""
zoom_intro_insert_creator.py

Wraps intro_zoom_5.py with the saved preset and stages the result as a
timestamped insert file ready for xml_insertor / GROQ_WITH_HTML_PIPE.

Steps:
  1. (Optional) Parse --xml to find the first kept segment's source in-point
  2. Extract a short clip from the rush at that offset (or from t=0 if no XML)
  3. Run intro_zoom_5.py on the extracted clip (preset params + optional SFX)
  4. Trim output to OUTPUT_DURATION_SECONDS
  5. Stage as 00h00m00s000ms_0_intro_zoom.mp4 in --insert-dir

Usage:
    python3.11 zoom_intro_insert_creator.py \\
        --rush /path/to/rush.mp4 \\
        --insert-dir /path/to/Universal_pipe/Insert/ \\
        --sfx /path/to/woosh.mp3 \\
        --xml /path/to/reference.xml   # optional: use first timeline segment
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Saved preset — matches the command the user dialled in
# ---------------------------------------------------------------------------
PRESET: dict[str, object] = {
    "angle":        30,
    "zoom_end":     1.5,
    "glitch":       0.7,
    "intro_frames": 8,
    "blur_steps":   10,
    "blur_spread":  0.15,
}

# Name that xml_insertor will parse as "place at t=0"
INSERT_FILENAME = "00h00m00s000ms_0_intro_zoom.mp4"

# Maximum duration of the staged insert (seconds). The zoom effect itself is
# only ~0.27 s (8 frames at 30 fps); the rest gives the overlay some body.
OUTPUT_DURATION_SECONDS: float = 3.0

# How many seconds to extract from the rush for intro_zoom_5.py to work on.
# Must be > OUTPUT_DURATION_SECONDS; 10 s is plenty.
EXTRACT_DURATION_SECONDS: float = 10.0

# intro_zoom_5.py lives in the same directory as this script
INTRO_ZOOM_SCRIPT = Path(__file__).resolve().parent / "intro_zoom_5.py"

# Default SFX: ../asset/short_low_woosh.mp3 relative to this script
DEFAULT_SFX = Path(__file__).resolve().parent.parent / "asset" / "short_low_woosh.mp3"


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _first_segment_source_offset(xml_path: Path) -> Optional[float]:
    """
    Parse a Premiere XML and return the source in-point (seconds) of the
    first enabled clip in video track 1.  Returns None if not found.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as exc:
        print(f"[intro_zoom_insert] WARNING: could not parse XML {xml_path}: {exc}",
              file=sys.stderr)
        return None

    video = root.find(".//media/video")
    if video is None:
        return None
    track = video.find("track")
    if track is None:
        return None

    for clipitem in track.findall("clipitem"):
        enabled = (clipitem.findtext("enabled") or "TRUE").strip().upper()
        if enabled != "TRUE":
            continue
        in_text = (clipitem.findtext("in") or "").strip()
        if not in_text or in_text == "-1":
            continue
        try:
            in_frame = int(in_text)
        except ValueError:
            continue
        # Derive fps from the clip's own <rate><timebase>
        rate_el = clipitem.find("rate")
        timebase = 30
        if rate_el is not None:
            try:
                timebase = int((rate_el.findtext("timebase") or "30").strip())
            except ValueError:
                pass
        offset = in_frame / timebase
        print(f"[intro_zoom_insert] First segment source offset: frame {in_frame} "
              f"@ {timebase} fps = {offset:.3f}s")
        return offset

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HDR_TRANSFERS = {"arib-std-b67", "smpte2084", "bt2020-10", "bt2020-12", "smpte-st-2084"}


def _hdr_vf_args(src: Path) -> list[str]:
    """Return -vf tonemap chain for HLG/HDR sources, empty list for SDR."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, check=False,
        )
        if r.stdout.strip().lower() in _HDR_TRANSFERS:
            return ["-vf",
                    "zscale=transfer=linear:primaries=bt709:matrix=bt709:rangein=limited:range=pc:npl=100,"
                    "format=gbrpf32le,"
                    "zscale=primaries=bt709,"
                    "tonemap=tonemap=hable:desat=0,"
                    "zscale=transfer=bt709:matrix=bt709:range=tv,"
                    "format=yuv420p"]
    except Exception:
        pass
    return []


def extract_segment(
    src: Path,
    dst: Path,
    start_seconds: float,
    duration: float,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Extract *duration* seconds from *src* starting at *start_seconds*.
    Re-encodes (instead of -c copy) to apply HDR→SDR conversion when needed.
    """
    hdr_vf = _hdr_vf_args(src)
    if hdr_vf:
        subprocess.run(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start_seconds:.6f}",
                "-i", str(src),
                "-t", f"{duration:.6f}",
                *hdr_vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-colorspace", "1", "-color_primaries", "1", "-color_trc", "1",
                "-c:a", "copy",
                str(dst),
            ],
            check=True,
        )
    else:
        subprocess.run(
            [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{start_seconds:.6f}",
                "-i", str(src),
                "-t", f"{duration:.6f}",
                "-c", "copy",
                str(dst),
            ],
            check=True,
        )


def trim_video(src: Path, dst: Path, duration: float, ffmpeg_bin: str = "ffmpeg") -> None:
    """Copy the first *duration* seconds of *src* into *dst*."""
    subprocess.run(
        [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src),
            "-t", f"{duration:.6f}",
            "-c", "copy",
            str(dst),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def create_intro_zoom_insert(
    rush_path: Path,
    insert_dir: Path,
    sfx_path: Path | None,
    xml_path: Path | None = None,
    python_bin: str = "python3.11",
    overwrite: bool = True,
) -> Path:
    """
    Run intro_zoom_5.py on the appropriate segment of rush_path, then stage
    the result in insert_dir as INSERT_FILENAME. Returns the final insert path.

    If xml_path is provided, the first kept segment's source in-point is read
    from the XML so the zoom targets the actual opening of the edited sequence,
    not necessarily the very start of the rush file.
    """
    insert_dir.mkdir(parents=True, exist_ok=True)
    dest = insert_dir / INSERT_FILENAME

    if dest.exists() and not overwrite:
        print(f"[intro_zoom_insert] Insert already exists, skipping: {dest}")
        return dest

    # Resolve source offset from the XML (or default to 0.0)
    source_offset = 0.0
    if xml_path is not None and xml_path.is_file():
        offset = _first_segment_source_offset(xml_path)
        if offset is not None:
            source_offset = offset
    else:
        if xml_path is not None:
            print(f"[intro_zoom_insert] WARNING: XML not found, using rush from t=0: {xml_path}",
                  file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="intro_zoom_insert_") as tmp:
        tmp_dir = Path(tmp)
        tmp_out = tmp_dir / INSERT_FILENAME

        # Extract the relevant segment from the rush so intro_zoom_5.py
        # sees the correct opening frames.
        if source_offset > 0.0:
            tmp_segment = tmp_dir / "segment_for_zoom.mp4"
            print(f"[intro_zoom_insert] Extracting {EXTRACT_DURATION_SECONDS}s "
                  f"from rush at {source_offset:.3f}s …")
            extract_segment(rush_path, tmp_segment, source_offset, EXTRACT_DURATION_SECONDS)
            zoom_input = tmp_segment
        else:
            zoom_input = rush_path

        cmd = [
            python_bin,
            str(INTRO_ZOOM_SCRIPT),
            str(zoom_input),
            "-o", str(tmp_out),
            "--angle",        str(PRESET["angle"]),
            "--zoom-end",     str(PRESET["zoom_end"]),
            "--glitch",       str(PRESET["glitch"]),
            "--intro-frames", str(PRESET["intro_frames"]),
            "--blur-steps",   str(PRESET["blur_steps"]),
            "--blur-spread",  str(PRESET["blur_spread"]),
            "--overwrite",
        ]

        if sfx_path is not None and sfx_path.is_file():
            cmd += ["--sfx", str(sfx_path), "--sfx-volume", "3.9811"]
        else:
            if sfx_path is not None:
                print(f"[intro_zoom_insert] WARNING: SFX not found, skipping: {sfx_path}",
                      file=sys.stderr)

        print(f"[intro_zoom_insert] Rendering intro zoom on {zoom_input.name} …")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"intro_zoom_5.py failed with exit code {result.returncode}"
            )

        if not tmp_out.exists():
            raise FileNotFoundError(
                f"Expected intro zoom output not found: {tmp_out}"
            )

        # Trim to OUTPUT_DURATION_SECONDS — avoids staging the full rush length
        tmp_trimmed = tmp_dir / ("trimmed_" + INSERT_FILENAME)
        trim_video(tmp_out, tmp_trimmed, OUTPUT_DURATION_SECONDS)
        shutil.copy2(tmp_trimmed, dest)

    print(f"[intro_zoom_insert] Staged → {dest}")
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run intro_zoom_5.py with the saved preset and stage the result "
            "as 00h00m00s000ms_0_intro_zoom.mp4 in the Insert folder."
        )
    )
    parser.add_argument(
        "--rush", required=True,
        help="Path to the rush/input video.",
    )
    parser.add_argument(
        "--insert-dir", required=True,
        help="Path to the Insert staging folder (Universal_pipe/Insert/).",
    )
    parser.add_argument(
        "--sfx",
        default=str(DEFAULT_SFX),
        help=f"Path to the SFX file (default: {DEFAULT_SFX}).",
    )
    parser.add_argument(
        "--xml",
        default=None,
        help=(
            "Path to the Premiere reference XML. If provided, the source in-point "
            "of the first clip in video track 1 is used as the zoom offset, so the "
            "effect lands on the actual opening frame of the edited sequence."
        ),
    )
    parser.add_argument(
        "--python", default="python3.11",
        help="Python interpreter to use (default: python3.11).",
    )
    parser.add_argument(
        "--overwrite", action="store_true", default=True,
        help="Overwrite existing insert file (default: True).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    rush_path = Path(args.rush).expanduser().resolve()
    if not rush_path.is_file():
        print(f"Error: rush video not found: {rush_path}", file=sys.stderr)
        sys.exit(1)

    insert_dir = Path(args.insert_dir).expanduser().resolve()
    sfx_path = Path(args.sfx).expanduser().resolve() if args.sfx else None
    xml_path = Path(args.xml).expanduser().resolve() if args.xml else None

    try:
        dest = create_intro_zoom_insert(
            rush_path=rush_path,
            insert_dir=insert_dir,
            sfx_path=sfx_path,
            xml_path=xml_path,
            python_bin=args.python,
            overwrite=args.overwrite,
        )
        print(f"Done: {dest}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
