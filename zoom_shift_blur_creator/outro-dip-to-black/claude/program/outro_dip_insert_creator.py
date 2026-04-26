#!/usr/bin/env python3.11
"""
outro_dip_insert_creator.py

Mirror of zoom_intro_insert_creator.py but for the outro. Locates the LAST
enabled clipitem on video track 1 of the reference XML, extracts that exact
segment from the rush via ffmpeg, runs zoom_out_dip_to_black.py on it
(proportionally shortening the zoom/dip if the segment is <9s), and stages
the processed file as HHhMMmSSsMMMms_99_outro_dip.mp4 in the Insert folder.

GROQ_WITH_HTML_PIPE.py calls this just after run_intro_zoom so program6 sees
the staged file and can swap it onto V1/A1 in place of the original tail.

Usage:
    python3.11 outro_dip_insert_creator.py \\
        --rush /path/to/rush.mp4 \\
        --insert-dir /path/to/Universal_pipe/Insert/ \\
        --xml   /path/to/reference.xml
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

OUTRO_SCRIPT = Path(__file__).resolve().parent / "zoom_out_dip_to_black.py"

DEFAULT_ZOOM_DURATION = 9.0
DEFAULT_DIP_DURATION = 3.0

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


def _format_timestamp(frame: int, fps: int) -> str:
    total_ms = int(round(frame * 1000.0 / fps))
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}h{minutes:02d}m{seconds:02d}s{ms:03d}ms"


def _last_kept_v1_segment(xml_path: Path) -> Tuple[int, int, int, int]:
    """
    Return (timeline_start_frame, timeline_end_frame, source_in_frame, fps)
    for the LAST enabled clipitem on video track 1.
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    seq = root.find(".//sequence")
    if seq is None:
        raise RuntimeError(f"No <sequence> in {xml_path}")
    rate_el = seq.find("rate")
    try:
        fps = int((rate_el.findtext("timebase") or "30").strip())
    except (AttributeError, ValueError):
        fps = 30
    video = seq.find("media/video")
    if video is None:
        raise RuntimeError(f"No <media>/<video> in {xml_path}")
    track = video.find("track")
    if track is None:
        raise RuntimeError(f"No V1 <track> in {xml_path}")

    kept = []
    for clipitem in track.findall("clipitem"):
        enabled = (clipitem.findtext("enabled") or "TRUE").strip().upper()
        if enabled != "TRUE":
            continue
        try:
            s = int((clipitem.findtext("start") or "-1").strip())
            e = int((clipitem.findtext("end") or "-1").strip())
            i = int((clipitem.findtext("in") or "-1").strip())
        except ValueError:
            continue
        if s < 0 or e <= s or i < 0:
            continue
        kept.append((s, e, i))

    if not kept:
        raise RuntimeError(f"No enabled V1 clipitems found in {xml_path}")

    kept.sort(key=lambda t: t[0])
    s, e, i = kept[-1]
    return s, e, i, fps


def _extract_segment(src: Path, dst: Path, start_sec: float, duration_sec: float) -> None:
    """
    Re-encode the segment (NOT -c copy) so frame-accurate trimming matches the
    XML's in/out. Copy-mode would snap to the nearest keyframe which would
    drift the outro effect off the intended boundary.
    """
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start_sec:.6f}",
            "-i", str(src),
            "-t", f"{duration_sec:.6f}",
            *_hdr_vf_args(src),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-colorspace", "1", "-color_primaries", "1", "-color_trc", "1",
            "-c:a", "aac", "-b:a", "192k",
            str(dst),
        ],
        check=True,
    )


def create_outro_dip_insert(
    rush_path: Path,
    insert_dir: Path,
    xml_path: Path,
    python_bin: str = "python3.11",
) -> Path:
    insert_dir.mkdir(parents=True, exist_ok=True)

    timeline_start, timeline_end, source_in, fps = _last_kept_v1_segment(xml_path)
    duration_frames = timeline_end - timeline_start
    duration_sec = duration_frames / fps
    source_in_sec = source_in / fps

    if duration_sec < DEFAULT_ZOOM_DURATION:
        ratio = duration_sec / DEFAULT_ZOOM_DURATION
        zoom_dur = duration_sec
        dip_dur = DEFAULT_DIP_DURATION * ratio
    else:
        zoom_dur = DEFAULT_ZOOM_DURATION
        dip_dur = DEFAULT_DIP_DURATION

    ts = _format_timestamp(timeline_start, fps)
    dest_name = f"{ts}_99_outro_dip.mp4"
    dest = insert_dir / dest_name

    for stale in insert_dir.glob("*_outro_dip.mp4"):
        if stale != dest:
            try:
                stale.unlink()
                print(f"[outro_dip_insert] Removed stale: {stale.name}")
            except OSError:
                pass

    print(f"[outro_dip_insert] Last V1 segment: timeline {timeline_start}-{timeline_end} "
          f"({duration_sec:.3f}s) | source in {source_in_sec:.3f}s | fps {fps}")
    print(f"[outro_dip_insert] Effect: zoom={zoom_dur:.3f}s dip={dip_dur:.3f}s")
    print(f"[outro_dip_insert] Dest: {dest}")

    with tempfile.TemporaryDirectory(prefix="outro_dip_") as tmp:
        tmp_dir = Path(tmp)
        segment_path = tmp_dir / "last_segment.mp4"
        _extract_segment(rush_path, segment_path, source_in_sec, duration_sec)

        cmd = [
            python_bin, str(OUTRO_SCRIPT),
            "--input", str(segment_path),
            "--output", str(dest),
            "--zoom-duration", f"{zoom_dur:.6f}",
            "--dip-duration", f"{dip_dur:.6f}",
        ]
        print(f"[outro_dip_insert] Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"zoom_out_dip_to_black.py failed with exit code {result.returncode}"
            )

    if not dest.exists():
        raise FileNotFoundError(f"Expected outro_dip output missing: {dest}")

    print(f"[outro_dip_insert] Staged → {dest}")
    return dest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render outro dip-to-black effect on the last kept rush segment "
                    "and stage it as a timestamped insert."
    )
    parser.add_argument("--rush", required=True, help="Path to the source rush video.")
    parser.add_argument("--insert-dir", required=True, help="Universal_pipe/Insert/ staging folder.")
    parser.add_argument("--xml", required=True, help="Reference XML with the current rush V1 segmentation.")
    parser.add_argument("--python", default="python3.11", help="Python interpreter for the outro script.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rush_path = Path(args.rush).expanduser().resolve()
    if not rush_path.is_file():
        print(f"Error: rush not found: {rush_path}", file=sys.stderr)
        sys.exit(1)
    insert_dir = Path(args.insert_dir).expanduser().resolve()
    xml_path = Path(args.xml).expanduser().resolve()
    if not xml_path.is_file():
        print(f"Error: reference XML not found: {xml_path}", file=sys.stderr)
        sys.exit(1)

    try:
        create_outro_dip_insert(
            rush_path=rush_path,
            insert_dir=insert_dir,
            xml_path=xml_path,
            python_bin=args.python,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
