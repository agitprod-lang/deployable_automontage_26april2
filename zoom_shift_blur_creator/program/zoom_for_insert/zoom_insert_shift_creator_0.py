#!/usr/bin/env python3
"""
zoom_insert_shift_creator_0.py

Pan-right zoom effect for insert-heavy timeline segments.

Scans the Insert folder for "triggering" inserts (emoji, CTA, social icons,
portrait circles, polaroids, metric displays, social link references).  Groups
consecutive triggering inserts into shift windows and renders rush video clips
where the face pans right + zooms in — creating blank left-side space for those
inserts.

Effect structure per window:
  1. Transition IN  (~1s)  — face smoothly zooms in and pans to the right
  2. Hold           — face stays right while inserts are active
  3. Transition OUT (~1s)  — face pans back to centre and zooms out

Only ONE transition-in at the start of a consecutive group, ONE transition-out
at the end.  Comparser Z/Z1/Z2/Z3 zoom rows that overlap a shift window are
flagged as overridden in the output manifest.

Output manifest is compatible with program6.py --zoom-replacements-manifest.

Usage:
    python3.11 zoom_insert_shift_creator_0.py \\
        --rush /path/to/rush.mp4 \\
        --insert-dir /path/to/Insert/ \\
        --output-dir /path/to/output/ \\
        --manifest-json /path/to/manifest.json \\
        [--annotations /path/to/precise_annotations.csv] \\
        [--fps 30] [--zoom-target 1.35] [--pan-right-ratio 0.70] \\
        [--transition-frames 30] [--gap-tolerance 0.5] \\
        [--overwrite] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np


# ─── Constants ────────────────────────────────────────────────────────────────

FPS_FALLBACK = 30.0
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".MP4", ".MOV"}

FACE_DETECTOR_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Trash/cutter")
FACE_MODEL_PATH = FACE_DETECTOR_DIR / "res10_300x300_ssd_iter_140000.caffemodel"
FACE_CONFIG_PATH = FACE_DETECTOR_DIR / "deploy.prototxt"
EYE_CASCADE_PATH = Path(cv2.data.haarcascades) / "haarcascade_eye.xml"

TIMESTAMP_RE = re.compile(r"^(\d{2})h(\d{2})m(\d{2})s(\d{3})ms")

DEFAULT_ZOOM_TARGET = 1.35
DEFAULT_PAN_RIGHT_RATIO = 0.70
DEFAULT_TRANSITION_FRAMES = 30   # 1.0 s at 30 fps
DEFAULT_GAP_TOLERANCE = 0.5      # seconds between inserts before starting a new window

PAPER_SUFFIXES = ("_pct",)                                                          # only percent inserts get the paper background (baked into all others)
PLAIN_SUFFIXES = ("_polaroid_insert", "_cta", "_rnk", "_cal", "_spd", "_wgt", "_dbc", "_tmp", "_srf")  # triggers plain zoom shift (suffix-based)

ASSETS_DIR = Path(__file__).parent / "assets"
INTRO_PAPER_PATH = ASSETS_DIR / "intro_shift_paper.mov"
OUTRO_PAPER_PATH = ASSETS_DIR / "outro_shift_paper.mov"
STATIC_PNG_PATH = ASSETS_DIR / "middle_shift_papershift.png"


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class InsertInfo:
    path: Path
    start_s: float
    end_s: float
    zoom_type: str = "plain"  # "paper" or "plain"


@dataclass
class ShiftWindow:
    start_s: float
    end_s: float
    trigger_files: list[str] = field(default_factory=list)
    zoom_type: str = "plain"  # "paper" if any trigger is paper, else "plain"


@dataclass
class FocusPoint:
    x: float
    y: float
    method: str


# ─── Insert classification ────────────────────────────────────────────────────

def is_triggering_insert(path: Path) -> str:
    """Return 'paper', 'plain', or '' (not triggering).

    'paper'  → zoom right + paper animation on the left
    'plain'  → zoom right only (pan clamped to avoid mirror artefact)
    ''       → does not trigger any shift
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return ""

    stem = path.stem
    sl = stem.lower()

    # ── Hard excludes ────────────────────────────────────────────────────────
    if "intro_zoom" in sl or "outro_dip" in sl:
        return ""
    if "transitionfilburn" in sl:
        return ""
    if "url_screen" in sl:
        return ""
    if re.search(r"city_\d+_", sl):
        return ""
    if "DIRECT" in stem or "EXTRACT" in stem:
        return ""
    # Full-screen text overlays
    if sl.endswith("_qh") or "_qh_" in sl:
        return ""
    if sl.endswith("_bld") or "_bld_" in sl:
        return ""
    if sl.endswith("_lst") or "_lst_" in sl:
        return ""

    # ── Paper triggers (stats/metrics/measurement assets) ────────────────────
    if any(sl.endswith(s) for s in PAPER_SUFFIXES):
        return "paper"
    # Money animation (no tag suffix — matched by asset name; bg already baked in)
    if "moneycaching" in sl:
        return "plain"

    # ── Plain triggers ────────────────────────────────────────────────────────
    if any(sl.endswith(s) for s in PLAIN_SUFFIXES):
        return "plain"
    # Title cards (regular + article-link title cards)
    if "title_" in sl:
        return "plain"
    # Animated arrow transition circles
    if "circle_arrow_trans" in sl:
        return "plain"
    # Filled portrait / avatar graphic
    if "_filled" in sl:
        return "plain"
    # Polaroid-style image inserts
    if "polaroid_insert" in sl:
        return "plain"
    # Portrait circle images
    if "image@circle" in sl:
        return "plain"
    # Social media handles / mentions
    if "@" in stem:
        return "plain"

    return ""


# ─── ffprobe helpers ──────────────────────────────────────────────────────────

def _run_ffprobe(*args: str) -> str:
    result = subprocess.run(
        ["ffprobe", "-v", "error", *args],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def probe_duration(path: Path) -> float:
    text = _run_ffprobe(
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    return float(text) if text else 0.0


def probe_fps(path: Path) -> float:
    text = _run_ffprobe(
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    if not text:
        return FPS_FALLBACK
    if "/" in text:
        num, den = text.split("/", 1)
        d = float(den)
        return float(num) / d if d else FPS_FALLBACK
    try:
        return float(text)
    except ValueError:
        return FPS_FALLBACK


def probe_dimensions(path: Path) -> tuple[int, int]:
    text = _run_ffprobe(
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0:s=x",
        str(path),
    )
    w_str, h_str = text.split("x", 1)
    return int("".join(c for c in w_str if c.isdigit())), int("".join(c for c in h_str if c.isdigit()))


def seconds_to_ffmpeg_time(v: float) -> str:
    v = max(0.0, v)
    h = int(v // 3600)
    m = int((v % 3600) // 60)
    s = v % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


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


# ─── Rush XML timeline/source mapping ────────────────────────────────────────

def parse_rush_xml_segments(
    xml_path: Path, fps: float
) -> list[tuple[float, float, float, float]]:
    """Return [(tl_start_s, tl_end_s, src_start_s, src_end_s), ...] from premiere XML."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except Exception as exc:
        print(f"  WARNING: Could not parse rush XML {xml_path}: {exc}")
        return []
    segments: list[tuple[float, float, float, float]] = []
    for sequence in root.iter("sequence"):
        video = sequence.find("media/video")
        if video is None:
            continue
        tracks = video.findall("track")
        if not tracks:
            continue
        for clipitem in tracks[0].findall("clipitem"):
            try:
                tl_s = int(clipitem.findtext("start") or "0")
                tl_e = int(clipitem.findtext("end") or "0")
                src_s = int(clipitem.findtext("in") or "0")
                src_e = int(clipitem.findtext("out") or "0")
            except ValueError:
                continue
            if tl_e <= tl_s or src_e <= src_s:
                continue
            segments.append((tl_s / fps, tl_e / fps, src_s / fps, src_e / fps))
        break
    return sorted(segments, key=lambda s: s[0])


def timeline_to_source_seconds(
    tl_s: float, segments: list[tuple[float, float, float, float]]
) -> float:
    """Map a timeline position to the corresponding source position via rush XML segments."""
    for tl_start, tl_end, src_start, _ in segments:
        if tl_start <= tl_s <= tl_end:
            return src_start + (tl_s - tl_start)
    if segments:
        # Clamp to last segment's source end
        if tl_s > segments[-1][1]:
            return segments[-1][2] + (tl_s - segments[-1][0])
        return segments[0][2]
    return tl_s


def extract_timeline_aligned_clip(
    rush_path: Path,
    segments: list[tuple[float, float, float, float]],
    tl_start: float,
    tl_end: float,
    output_path: Path,
) -> None:
    """Extract source portions covering timeline [tl_start, tl_end] and concatenate."""
    portions: list[tuple[float, float]] = []
    for tl_seg_s, tl_seg_e, src_seg_s, _ in segments:
        overlap_s = max(tl_start, tl_seg_s)
        overlap_e = min(tl_end, tl_seg_e)
        if overlap_e <= overlap_s:
            continue
        src_extract_start = src_seg_s + (overlap_s - tl_seg_s)
        portions.append((src_extract_start, overlap_e - overlap_s))

    if not portions:
        src_start = timeline_to_source_seconds(tl_start, segments)
        portions = [(src_start, tl_end - tl_start)]

    hdr_vf = _hdr_vf_args(rush_path)
    if len(portions) == 1:
        src_start, duration = portions[0]
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", seconds_to_ffmpeg_time(src_start),
            "-t", f"{duration:.6f}",
            "-i", str(rush_path),
            *hdr_vf,
            "-an", "-c:v", "ffv1", "-pix_fmt", "bgr24",
            "-y", str(output_path),
        ], check=True)
    else:
        with tempfile.TemporaryDirectory(prefix="zis_concat_") as td:
            temp_dir = Path(td)
            part_files: list[Path] = []
            for i, (src_start, duration) in enumerate(portions):
                part = temp_dir / f"part_{i:04d}.mkv"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-ss", seconds_to_ffmpeg_time(src_start),
                    "-t", f"{duration:.6f}",
                    "-i", str(rush_path),
                    *hdr_vf,
                    "-an", "-c:v", "ffv1", "-pix_fmt", "bgr24",
                    "-y", str(part),
                ], check=True)
                part_files.append(part)
            concat_list = temp_dir / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{p}'" for p in part_files),
                encoding="utf-8",
            )
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c:v", "copy",
                "-y", str(output_path),
            ], check=True)


# ─── Protection zones ────────────────────────────────────────────────────────

def get_protection_zones(insert_dir: Path) -> tuple[float, float]:
    """Return (intro_end_s, outro_start_s) from intro_zoom and outro_dip files.

    Shift windows must not overlap with [0, intro_end_s] or
    [outro_start_s, outro_start_s + outro_dur].  Defaults to (0.0, inf)
    if the files are not present.
    """
    intro_end = 0.0
    outro_start = float("inf")

    for path in insert_dir.iterdir():
        if not path.is_file():
            continue
        sl = path.stem.lower()
        ts = parse_timestamp_from_name(path.name)
        if ts is None:
            continue
        try:
            dur = probe_duration(path)
        except Exception:
            continue
        if "intro_zoom" in sl:
            intro_end = max(intro_end, ts + dur)
        elif "outro_dip" in sl:
            outro_start = min(outro_start, ts)

    if outro_start == float("inf"):
        outro_start = float("inf")  # no outro found — no upper bound

    return intro_end, outro_start


def apply_protection_zones(
    windows: list[ShiftWindow],
    intro_end: float,
    outro_start: float,
) -> list[ShiftWindow]:
    """Trim or drop shift windows that overlap with the intro/outro zones."""
    result: list[ShiftWindow] = []
    for w in windows:
        start = max(w.start_s, intro_end)
        end = min(w.end_s, outro_start)
        if start >= end:
            continue  # entirely inside a protected zone
        result.append(ShiftWindow(start_s=start, end_s=end, trigger_files=w.trigger_files))
    return result


# ─── Insert scanning & grouping ───────────────────────────────────────────────

def parse_timestamp_from_name(filename: str) -> float | None:
    m = TIMESTAMP_RE.match(Path(filename).name)
    if not m:
        return None
    h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    return h * 3600 + mn * 60 + s + ms / 1000.0


def collect_inserts(insert_dir: Path) -> list[InsertInfo]:
    inserts: list[InsertInfo] = []
    for path in sorted(insert_dir.iterdir()):
        if not path.is_file():
            continue
        zoom_type = is_triggering_insert(path)
        if not zoom_type:
            continue
        start_s = parse_timestamp_from_name(path.name)
        if start_s is None:
            continue
        try:
            dur = probe_duration(path)
        except subprocess.CalledProcessError:
            continue
        if dur <= 0:
            continue
        inserts.append(InsertInfo(path=path, start_s=start_s, end_s=start_s + dur, zoom_type=zoom_type))
    return sorted(inserts, key=lambda i: i.start_s)


def group_into_windows(inserts: list[InsertInfo], gap_tolerance: float) -> list[ShiftWindow]:
    """Merge consecutive inserts within gap_tolerance seconds into one shift window.

    A window is 'paper' if any of its triggering inserts is a paper trigger;
    otherwise it is 'plain'.
    """
    if not inserts:
        return []
    windows: list[ShiftWindow] = []
    cur = ShiftWindow(
        start_s=inserts[0].start_s,
        end_s=inserts[0].end_s,
        trigger_files=[inserts[0].path.name],
        zoom_type=inserts[0].zoom_type,
    )
    for ins in inserts[1:]:
        if ins.start_s <= cur.end_s + gap_tolerance:
            cur.end_s = max(cur.end_s, ins.end_s)
            cur.trigger_files.append(ins.path.name)
            if ins.zoom_type == "paper":
                cur.zoom_type = "paper"  # paper wins over plain
        else:
            windows.append(cur)
            cur = ShiftWindow(
                start_s=ins.start_s,
                end_s=ins.end_s,
                trigger_files=[ins.path.name],
                zoom_type=ins.zoom_type,
            )
    windows.append(cur)
    return windows


# ─── Face detection (mirrored from identify_face_and_zoom_on_segment.py) ──────

def _load_detectors() -> tuple[cv2.dnn_Net, cv2.CascadeClassifier]:
    if not FACE_MODEL_PATH.exists() or not FACE_CONFIG_PATH.exists():
        raise SystemExit(
            f"Face detector files missing:\n  model : {FACE_MODEL_PATH}\n  config: {FACE_CONFIG_PATH}"
        )
    face_net = cv2.dnn.readNetFromCaffe(str(FACE_CONFIG_PATH), str(FACE_MODEL_PATH))
    eye_cascade = cv2.CascadeClassifier(str(EYE_CASCADE_PATH))
    return face_net, eye_cascade


def _detect_face_box(
    frame: np.ndarray, face_net: cv2.dnn_Net
) -> tuple[int, int, int, int] | None:
    fh, fw = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104.0, 177.0, 123.0))
    face_net.setInput(blob)
    detections = face_net.forward()
    best_box: tuple[int, int, int, int] | None = None
    best_score = 0.0
    for i in range(detections.shape[2]):
        conf = float(detections[0, 0, i, 2])
        if conf < 0.55:
            continue
        box = detections[0, 0, i, 3:7] * np.array([fw, fh, fw, fh])
        x1, y1, x2, y2 = box.astype(int)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = max(x1 + 1, min(x2, fw)), max(y1 + 1, min(y2, fh))
        score = conf * max(1, (x2 - x1) * (y2 - y1))
        if score > best_score:
            best_score = score
            best_box = (x1, y1, x2, y2)
    return best_box


def _detect_eye_focus(
    frame: np.ndarray,
    face_box: tuple[int, int, int, int],
    eye_cascade: cv2.CascadeClassifier,
) -> FocusPoint:
    x1, y1, x2, y2 = face_box
    roi = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    eyes = eye_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(12, 12))
    if len(eyes) >= 2:
        top2 = sorted(eyes, key=lambda b: b[2] * b[3], reverse=True)[:2]
        cx = sum(x1 + ex + ew / 2.0 for ex, ey, ew, eh in top2) / 2.0
        cy = sum(y1 + ey + eh / 2.0 for ex, ey, ew, eh in top2) / 2.0
        return FocusPoint(x=cx, y=cy, method="eyes")
    fw, fh = x2 - x1, y2 - y1
    return FocusPoint(x=x1 + fw / 2.0, y=y1 + fh * 0.38, method="face")


def detect_focus(
    rush_path: Path,
    at_seconds: float,
    face_net: cv2.dnn_Net,
    eye_cascade: cv2.CascadeClassifier,
    width: int,
    height: int,
) -> FocusPoint:
    """Extract one frame from rush at at_seconds and detect face/eye focus point."""
    fallback = FocusPoint(x=(width - 1) / 2.0, y=(height - 1) / 2.0, method="center_fallback")
    with tempfile.TemporaryDirectory(prefix="zis_focus_") as td:
        frame_path = Path(td) / "focus_frame.jpg"
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", seconds_to_ffmpeg_time(max(0.0, at_seconds)),
            "-i", str(rush_path),
            "-frames:v", "1", "-q:v", "2",
            "-y", str(frame_path),
        ], capture_output=True)
        if result.returncode != 0 or not frame_path.exists():
            return fallback
        frame = cv2.imread(str(frame_path))
    if frame is None:
        return fallback
    box = _detect_face_box(frame, face_net)
    if box is None:
        return fallback
    return _detect_eye_focus(frame, box, eye_cascade)


# ─── Pan-zoom rendering ───────────────────────────────────────────────────────

def _ease_in_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        return 4.0 * t * t * t
    p = 2.0 * t - 2.0
    return 1.0 + 0.5 * p * p * p


def _render_shift_frames(
    input_path: Path,
    raw_output_path: Path,
    width: int,
    height: int,
    fps: float,
    zoom_target: float,
    focus: FocusPoint,
    pan_x_full: float,
    trans_in_frames: int,
    trans_out_frames: int,
    clamp_pan: bool = False,
) -> None:
    """Process frames from input_path applying zoom+pan effect, write to raw_output_path.

    clamp_pan=True limits pan so the zoomed content always covers the left edge,
    avoiding empty margins. Use for plain (no-paper) renders only — paper renders
    intentionally leave the left empty for the paper animation to fill.
    """
    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {input_path}")

    frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    eff_fps = cap.get(cv2.CAP_PROP_FPS)
    safe_fps = eff_fps if eff_fps > 0 else fps
    frame_total = max(frame_total, 1)

    # Prevent transitions from overlapping each other
    max_trans = max(frame_total // 3, 1)
    ti = min(trans_in_frames, max_trans)
    to = min(trans_out_frames, max_trans)
    hold_end = max(ti, frame_total - to)

    # Clamp focus to frame bounds
    fx = min(max(focus.x, 0.0), width - 1.0)
    fy = min(max(focus.y, 0.0), height - 1.0)
    zt = max(1.0, zoom_target)

    if clamp_pan:
        # Limit pan so zoomed content always covers the left edge (no empty left margin).
        pan_x_full = min(pan_x_full, fx * (zt - 1.0))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(raw_output_path), fourcc, safe_fps, (width, height))
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(f"Cannot create output video: {raw_output_path}")

    fi = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if fi < ti and ti > 0:
            p = _ease_in_out_cubic(fi / ti)
            zoom = 1.0 + (zt - 1.0) * p
            pan_x = pan_x_full * p
        elif fi < hold_end:
            zoom = zt
            pan_x = pan_x_full
        else:
            elapsed = fi - hold_end
            denom = max(to, 1)
            p = _ease_in_out_cubic(min(elapsed / denom, 1.0))
            zoom = zt + (1.0 - zt) * p
            pan_x = pan_x_full * (1.0 - p)

        tx = fx * (1.0 - zoom) + pan_x
        ty = fy * (1.0 - zoom)
        M = np.array([[zoom, 0.0, tx], [0.0, zoom, ty]], dtype=np.float32)
        out = cv2.warpAffine(
            frame, M, (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        writer.write(out)
        fi += 1

    cap.release()
    writer.release()
    if fi == 0:
        raise RuntimeError(f"No frames were rendered from: {input_path}")


def render_shift_clip(
    rush_path: Path,
    output_path: Path,
    clip_start: float,
    clip_duration: float,
    focus: FocusPoint,
    width: int,
    height: int,
    fps: float,
    zoom_target: float,
    pan_x_full: float,
    trans_in_frames: int,
    trans_out_frames: int,
    overwrite: bool,
    clamp_pan: bool = False,
) -> None:
    """Extract rush segment, apply pan-zoom effect, encode final H.264 clip (no audio)."""
    with tempfile.TemporaryDirectory(prefix="zis_render_") as td:
        temp_extract = Path(td) / "segment.mkv"
        temp_raw = Path(td) / "shift_raw.mp4"

        # 1. Extract lossless segment (no audio); HDR sources get zscale → SDR first
        hdr_vf = _hdr_vf_args(rush_path)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", seconds_to_ffmpeg_time(clip_start),
            "-t", f"{clip_duration:.6f}",
            "-i", str(rush_path),
            *hdr_vf,
            "-an", "-c:v", "ffv1", "-pix_fmt", "bgr24",
            "-y", str(temp_extract),
        ], check=True)

        # 2. Render pan-zoom frames
        _render_shift_frames(
            input_path=temp_extract,
            raw_output_path=temp_raw,
            width=width, height=height, fps=fps,
            zoom_target=zoom_target,
            focus=focus,
            pan_x_full=pan_x_full,
            trans_in_frames=trans_in_frames,
            trans_out_frames=trans_out_frames,
            clamp_pan=clamp_pan,
        )

        # 3. Re-encode to H.264 (no audio — NLE handles audio via original rush track)
        mux_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(temp_raw),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an",
            "-y" if overwrite else "-n",
            str(output_path),
        ]
        subprocess.run(mux_cmd, check=True)


def render_shift_clip_from_path(
    source_path: Path,
    output_path: Path,
    focus: FocusPoint,
    width: int,
    height: int,
    fps: float,
    zoom_target: float,
    pan_x_full: float,
    trans_in_frames: int,
    trans_out_frames: int,
    overwrite: bool,
    clamp_pan: bool = False,
) -> None:
    """Apply pan-zoom to a pre-extracted clip and encode to H.264 (no ffmpeg re-extract)."""
    with tempfile.TemporaryDirectory(prefix="zis_render_") as td:
        temp_raw = Path(td) / "shift_raw.mp4"
        _render_shift_frames(
            input_path=source_path,
            raw_output_path=temp_raw,
            width=width, height=height, fps=fps,
            zoom_target=zoom_target,
            focus=focus,
            pan_x_full=pan_x_full,
            trans_in_frames=trans_in_frames,
            trans_out_frames=trans_out_frames,
            clamp_pan=clamp_pan,
        )
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(temp_raw),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-an",
            "-y" if overwrite else "-n",
            str(output_path),
        ], check=True)


# ─── Paper overlay compositing ───────────────────────────────────────────────

def _paper_assets_available() -> bool:
    return INTRO_PAPER_PATH.exists() and OUTRO_PAPER_PATH.exists() and STATIC_PNG_PATH.exists()


def composite_paper_overlay(
    zoom_clip_path: Path,
    output_path: Path,
    clip_duration: float,
    fps: float,
    overwrite: bool,
) -> None:
    """Overlay intro/static/outro paper animations on the left side of the zoom clip.

    Timeline structure (within the clip):
      [0s          → intro_dur]     intro_shift_paper.mov  (plays over transition-in)
      [intro_dur   → hold_end]      middle_shift_papershift.png  (static hold)
      [hold_end    → clip_duration] outro_shift_paper.mov  (plays over transition-out)

    All assets are 1920×1080 ProRes/RGBA with alpha — composited via overlay filter.
    """
    intro_dur = probe_duration(INTRO_PAPER_PATH)
    outro_dur = probe_duration(OUTRO_PAPER_PATH)

    # hold_end = when outro starts; clamp so it never precedes intro_end
    hold_end = max(intro_dur, clip_duration - outro_dur)
    intro_end = min(intro_dur, hold_end)

    filter_complex = (
        # Normalise intro and outro to match rush fps
        f"[1:v]fps={fps}[intro_fps];"
        f"[3:v]fps={fps}[outro_fps];"
        # Layer 1: intro paper over background (passes through after intro ends)
        "[0:v][intro_fps]overlay=0:0:eof_action=pass[v1];"
        # Layer 2: static PNG during hold phase
        f"[v1][2:v]overlay=0:0:enable='between(t,{intro_end:.6f},{hold_end:.6f})'[v2];"
        # Layer 3: outro paper (input delayed via -itsoffset so its t=0 = hold_end)
        "[v2][outro_fps]overlay=0:0:eof_action=pass[out]"
    )

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(zoom_clip_path),          # [0] background (opaque zoom clip)
        "-i", str(INTRO_PAPER_PATH),         # [1] intro paper (yuva444p12le, alpha)
        "-loop", "1", "-i", str(STATIC_PNG_PATH),  # [2] static PNG (RGBA, loops)
        "-itsoffset", f"{hold_end:.6f}",
        "-i", str(OUTRO_PAPER_PATH),         # [3] outro paper (delayed to hold_end)
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-an",
        "-t", f"{clip_duration:.6f}",
        "-y" if overwrite else "-n",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


# ─── Comparser zoom override detection ───────────────────────────────────────

def _tc_to_seconds(value: str, fps: float) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    if re.fullmatch(r"\d+:\d{2}:\d{2}:\d{2}", raw):
        h, m, s, f = map(int, raw.split(":"))
        return h * 3600 + m * 60 + s + f / max(fps, 1.0)
    if re.fullmatch(r"\d+:\d{2}:\d{2}(?:\.\d+)?", raw):
        parts = raw.split(":", 2)
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    try:
        return float(raw)
    except ValueError:
        return 0.0


def load_comparser_zoom_ranges(
    csv_path: Path, fps: float
) -> list[tuple[str, float, float]]:
    """Return list of (row_id, timeline_start_s, timeline_end_s) for Zoom rows."""
    rows: list[tuple[str, float, float]] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            if (row.get("Annotation Column") or "").strip() != "Zoom":
                continue
            if not (row.get("Annotation Value") or "").strip():
                continue
            row_id = (row.get("Row ID") or "").strip()
            start_s = _tc_to_seconds(row.get("Start Time", ""), fps)
            end_s = _tc_to_seconds(row.get("End Time", ""), fps)
            if end_s > start_s:
                rows.append((row_id, start_s, end_s))
    return rows


def find_overridden_rows(
    window: ShiftWindow, zoom_rows: list[tuple[str, float, float]]
) -> list[str]:
    """Return row_ids whose timeline range overlaps the shift window."""
    return [
        row_id
        for row_id, start_s, end_s in zoom_rows
        if start_s < window.end_s and end_s > window.start_s
    ]


# ─── CLI & main ───────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render pan-right zoom clips for insert-heavy timeline windows."
    )
    p.add_argument("--rush", required=True, help="Path to rush video file.")
    p.add_argument("--insert-dir", required=True, help="Path to Insert folder.")
    p.add_argument(
        "--output-dir", default=None,
        help="Output directory for rendered clips. Default: <insert-dir>/zoom_insert_shift_replacements/",
    )
    p.add_argument(
        "--manifest-json", default=None,
        help="Output path for JSON manifest. Default: <output-dir>/zoom_insert_shift_manifest.json",
    )
    p.add_argument(
        "--annotations", default=None,
        help="Optional precise_annotations CSV for comparser zoom override detection.",
    )
    p.add_argument("--fps", type=float, default=None,
                   help="Sequence FPS. Auto-detected from rush if omitted.")
    p.add_argument("--zoom-target", type=float, default=DEFAULT_ZOOM_TARGET,
                   help=f"Zoom factor while face is shifted right. Default: {DEFAULT_ZOOM_TARGET}")
    p.add_argument("--pan-right-ratio", type=float, default=DEFAULT_PAN_RIGHT_RATIO,
                   help=f"Face target X position as fraction of frame width. Default: {DEFAULT_PAN_RIGHT_RATIO}")
    p.add_argument(
        "--transition-frames", type=int, default=DEFAULT_TRANSITION_FRAMES,
        help=f"Frames for each transition (in / out). Default: {DEFAULT_TRANSITION_FRAMES} = 1s at 30fps",
    )
    p.add_argument(
        "--gap-tolerance", type=float, default=DEFAULT_GAP_TOLERANCE,
        help=f"Max gap in seconds between inserts before starting a new shift window. Default: {DEFAULT_GAP_TOLERANCE}",
    )
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")
    p.add_argument("--dry-run", action="store_true", help="Print plan without rendering.")
    p.add_argument("--max-windows", type=int, default=None,
                   help="Render only the first N windows. Useful for quick tests.")
    p.add_argument(
        "--rush-xml", default=None,
        help="Path to intermediate Premiere XML (from universal_generate_premiere_xml.py). "
             "When provided, insert timestamps are mapped timeline→source so zoom content "
             "aligns with the edited timeline even when the rush has cuts.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    rush_path = Path(args.rush).expanduser().resolve()
    insert_dir = Path(args.insert_dir).expanduser().resolve()

    if not rush_path.exists():
        sys.exit(f"ERROR: Rush file not found: {rush_path}")
    if not insert_dir.is_dir():
        sys.exit(f"ERROR: Insert dir not found: {insert_dir}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ERROR: ffmpeg / ffprobe not found in PATH.")

    fps = args.fps or probe_fps(rush_path)
    width, height = probe_dimensions(rush_path)
    rush_duration = probe_duration(rush_path)

    # Optional: load rush XML for timeline→source mapping (handles cut rushes)
    rush_xml_segments: list[tuple[float, float, float, float]] = []
    if args.rush_xml:
        xml_path = Path(args.rush_xml).expanduser().resolve()
        if xml_path.exists():
            rush_xml_segments = parse_rush_xml_segments(xml_path, fps)
            print(f"Rush XML : {xml_path.name}  ({len(rush_xml_segments)} segments)")
        else:
            print(f"WARNING: --rush-xml path not found: {xml_path} — using source time directly")

    print(f"Rush : {rush_path.name}")
    print(f"       {width}×{height}  {fps:.3f} fps  {rush_duration:.3f}s")

    # Collect and classify inserts
    inserts = collect_inserts(insert_dir)
    print(f"\nTriggering inserts: {len(inserts)}")
    for ins in inserts:
        print(f"  [{ins.start_s:8.3f}s – {ins.end_s:8.3f}s]  {ins.path.name}")

    if not inserts:
        print("\nNo triggering inserts found — nothing to do.")
        return

    # Group into shift windows
    windows = group_into_windows(inserts, args.gap_tolerance)
    trans_duration = args.transition_frames / max(fps, 1.0)

    # Protect intro_zoom and outro_dip from being overridden
    intro_end, outro_start = get_protection_zones(insert_dir)
    if intro_end > 0.0:
        print(f"\nProtected intro zone : [0 – {intro_end:.3f}s]")
    if outro_start < float("inf"):
        print(f"Protected outro zone : [{outro_start:.3f}s – end]")
    windows = apply_protection_zones(windows, intro_end, outro_start)

    print(f"\nShift windows ({len(windows)})  [gap tolerance: {args.gap_tolerance}s]:")
    for i, w in enumerate(windows, 1):
        clip_start = max(0.0, w.start_s - trans_duration)
        clip_end = min(rush_duration, w.end_s + trans_duration)
        print(f"  Window {i}: inserts [{w.start_s:.3f}s – {w.end_s:.3f}s]  "
              f"clip [{clip_start:.3f}s – {clip_end:.3f}s]")
        for fname in w.trigger_files:
            print(f"    • {fname}")

    if args.dry_run:
        print("\nDry run — no clips rendered.")
        return

    # Setup output directory
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else (
        insert_dir / "zoom_insert_shift_replacements"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load comparser zoom ranges (optional)
    zoom_rows: list[tuple[str, float, float]] = []
    if args.annotations:
        ann_path = Path(args.annotations).expanduser().resolve()
        if ann_path.exists():
            zoom_rows = load_comparser_zoom_ranges(ann_path, fps)
            print(f"\nComparser zoom rows loaded: {len(zoom_rows)}")
        else:
            print(f"\nWARNING: Annotations file not found: {ann_path}")

    # Load face detectors
    print("\nLoading face detectors …")
    face_net, eye_cascade = _load_detectors()

    entries: list[dict] = []

    windows_to_render = windows[:args.max_windows] if args.max_windows else windows
    for idx, window in enumerate(windows_to_render, 1):
        print(f"\n{'─' * 60}")
        print(f"Window {idx}/{len(windows)}: [{window.start_s:.3f}s – {window.end_s:.3f}s]")

        clip_start = max(0.0, max(intro_end, window.start_s - trans_duration))
        clip_end = min(rush_duration, min(outro_start, window.end_s + trans_duration))
        clip_duration = clip_end - clip_start

        if clip_duration <= 0.0:
            print("  WARNING: zero-duration clip after clamping — skipping.")
            continue

        # Actual transition lengths after edge clamping
        trans_in_frames = round((window.start_s - clip_start) * fps)
        trans_out_frames = round((clip_end - window.end_s) * fps)

        # Map timeline positions to source positions for face detection and rendering
        if rush_xml_segments:
            face_detect_src_s = timeline_to_source_seconds(window.start_s, rush_xml_segments)
        else:
            face_detect_src_s = window.start_s  # uncut rush: timeline == source

        # Detect face focus
        print(f"  Detecting face at src={face_detect_src_s:.3f}s …")
        focus = detect_focus(rush_path, face_detect_src_s, face_net, eye_cascade, width, height)
        print(f"  Focus: ({focus.x:.1f}, {focus.y:.1f})  method={focus.method}")

        # Pan target: shift face to pan_right_ratio * width
        pan_x_full = args.pan_right_ratio * width - focus.x

        # Output clip filenames (keyed by timeline milliseconds)
        start_ms = int(round(clip_start * 1000))
        end_ms = int(round(clip_end * 1000))
        zoom_name = f"{start_ms:09d}ms_to_{end_ms:09d}ms_zoom_right_shift.mp4"
        paper_name = f"{start_ms:09d}ms_to_{end_ms:09d}ms_zoom_right_shift_paper.mp4"
        zoom_path = output_dir / zoom_name
        paper_path = output_dir / paper_name
        use_paper = window.zoom_type == "paper" and _paper_assets_available()
        # final clip used in manifest — paper composite for paper windows, plain for plain windows
        out_path = paper_path if use_paper else zoom_path

        needs_render = args.overwrite or not zoom_path.exists()
        needs_paper = use_paper and (args.overwrite or not paper_path.exists())

        if not needs_render and not needs_paper:
            print(f"  SKIP (exists): {out_path.name}")
        else:
            if needs_render:
                print(f"  Rendering zoom {clip_duration:.2f}s  →  {zoom_name} …")
                if rush_xml_segments:
                    # Extract timeline-aligned source clip (handles cut rush correctly)
                    with tempfile.TemporaryDirectory(prefix="zis_tl_") as td:
                        aligned_path = Path(td) / "timeline_aligned.mkv"
                        extract_timeline_aligned_clip(
                            rush_path, rush_xml_segments,
                            clip_start, clip_end, aligned_path,
                        )
                        render_shift_clip_from_path(
                            source_path=aligned_path,
                            output_path=zoom_path,
                            focus=focus,
                            width=width, height=height, fps=fps,
                            zoom_target=args.zoom_target,
                            pan_x_full=pan_x_full,
                            trans_in_frames=trans_in_frames,
                            trans_out_frames=trans_out_frames,
                            overwrite=args.overwrite,
                            clamp_pan=(window.zoom_type == "plain"),
                        )
                else:
                    render_shift_clip(
                        rush_path=rush_path,
                        output_path=zoom_path,
                        clip_start=clip_start,
                        clip_duration=clip_duration,
                        focus=focus,
                        width=width, height=height, fps=fps,
                        zoom_target=args.zoom_target,
                        pan_x_full=pan_x_full,
                        trans_in_frames=trans_in_frames,
                        trans_out_frames=trans_out_frames,
                        overwrite=args.overwrite,
                        clamp_pan=(window.zoom_type == "plain"),
                    )
                print(f"  Zoom done: {zoom_name}")

            if use_paper and needs_paper:
                print(f"  Compositing paper overlay  →  {paper_name} …")
                composite_paper_overlay(
                    zoom_clip_path=zoom_path,
                    output_path=paper_path,
                    clip_duration=clip_duration,
                    fps=fps,
                    overwrite=args.overwrite,
                )
                print(f"  Paper done: {paper_name}")

        # Comparser override detection
        overridden = find_overridden_rows(window, zoom_rows)
        if overridden:
            print(f"  Overrides comparser row(s): {overridden}")

        tl_start_frames = int(round(clip_start * fps))
        tl_end_frames = int(round(clip_end * fps))
        # source_start/end_frames: when XML segments provided, the zoom content is
        # timeline-aligned so source frames == timeline frames.  Without XML they were
        # always the same anyway (uncut rush assumption).
        src_start_frames = tl_start_frames
        src_end_frames = tl_end_frames

        entries.append({
            "window_id": idx,
            "row_id": f"SHIFT_W_{idx}",
            "zoom_code": "INSERT_SHIFT",
            "zoom_type": window.zoom_type,
            "timeline_start_frames": tl_start_frames,
            "timeline_end_frames": tl_end_frames,
            "source_start_frames": src_start_frames,
            "source_end_frames": src_end_frames,
            "output_path": str(out_path),
            "output_path_plain": str(zoom_path),
            "output_path_paper": str(paper_path) if paper_path.exists() else None,
            "focus_method": focus.method,
            "focus_x": focus.x,
            "focus_y": focus.y,
            "zoom_end": args.zoom_target,
            "pan_right_ratio": args.pan_right_ratio,
            "trigger_inserts": window.trigger_files,
            "overridden_comparser_rows": overridden,
        })

    # Write manifest
    manifest: dict = {"sequence_fps": fps, "entries": entries}
    if args.manifest_json:
        manifest_path = Path(args.manifest_json).expanduser().resolve()
    else:
        manifest_path = output_dir / "zoom_insert_shift_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"\n{'═' * 60}")
    print(f"Manifest  : {manifest_path}")
    print(f"Windows rendered: {len(entries)}")


if __name__ == "__main__":
    main()
