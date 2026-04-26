#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


FPS_FALLBACK = 30.0
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mpg", ".mpeg", ".avi", ".mkv", ".webm", ".MP4", ".MOV"}
FACE_DETECTOR_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Trash/cutter")
FACE_MODEL_PATH = FACE_DETECTOR_DIR / "res10_300x300_ssd_iter_140000.caffemodel"
FACE_CONFIG_PATH = FACE_DETECTOR_DIR / "deploy.prototxt"
EYE_CASCADE_PATH = Path(cv2.data.haarcascades) / "haarcascade_eye.xml"
DEFAULT_ZOOM_END_BY_CODE = {
    "z": 1.25,
    "z1": 1.11,
    "z2": 1.26,
    "z3": 1.38,
}


@dataclass
class ZoomSegment:
    source_csv: Path
    row_id: str
    marker: str
    status: str
    text: str
    reference_segment: str
    timeline_start_seconds: float
    timeline_end_seconds: float
    source_start_seconds: float
    source_end_seconds: float

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end_seconds - self.source_start_seconds)


@dataclass
class FocusPoint:
    x: float
    y: float
    method: str


@dataclass
class RenderedSegment:
    segment: ZoomSegment
    focus: FocusPoint
    output_path: Path
    zoom_end: float
    sequence_fps: float
    timeline_start_frames: int
    timeline_end_frames: int
    source_start_frames: int
    source_end_frames: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract rush segments referenced by Zoom annotations and render "
            "slow-zoom clips that focus on the detected face or eyes from the first frame."
        )
    )
    parser.add_argument("csv_paths", nargs="+", help="Precise-annotations CSV file(s) to process.")
    parser.add_argument(
        "--rush-dir",
        default="/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush",
        help="Directory containing the original rush video files.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent.parent / "output"),
        help="Directory where rendered clips will be written.",
    )
    parser.add_argument(
        "--zoom-end",
        type=float,
        default=None,
        help="Optional override for the final zoom multiplier. Defaults to Z/Z1/Z2/Z3-specific values.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Sequence FPS used to interpret HH:MM:SS:FF CSV timecodes and emit manifest frame numbers.",
    )
    parser.add_argument(
        "--manifest-json",
        help="Optional JSON output path describing rendered zoom replacement segments.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output files.")
    parser.add_argument("--dry-run", action="store_true", help="Print the work plan without rendering.")
    return parser.parse_args()


def ensure_dependencies() -> None:
    missing = [binary for binary in ("ffmpeg", "ffprobe") if shutil.which(binary) is None]
    if missing:
        raise SystemExit(f"Missing required binaries: {', '.join(missing)}")
    if not FACE_MODEL_PATH.exists() or not FACE_CONFIG_PATH.exists():
        raise SystemExit(
            f"Missing face detector files: model={FACE_MODEL_PATH.exists()} config={FACE_CONFIG_PATH.exists()}"
        )
    if not EYE_CASCADE_PATH.exists():
        raise SystemExit(f"Missing eye cascade file: {EYE_CASCADE_PATH}")


def timecode_to_seconds(value: str, fps: float) -> float:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty timecode")
    if re.fullmatch(r"\d+:\d{2}:\d{2}:\d{2}", raw):
        hours, minutes, seconds, frames = map(int, raw.split(":"))
        safe_fps = fps if fps > 0 else FPS_FALLBACK
        return hours * 3600 + minutes * 60 + seconds + frames / safe_fps
    if re.fullmatch(r"\d+:\d{2}:\d{2}(?:\.\d+)?", raw):
        hours, minutes, seconds = raw.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Unsupported time format: {value}") from exc


def seconds_to_ffmpeg_time(value: float) -> str:
    safe = max(0.0, value)
    hours = int(safe // 3600)
    minutes = int((safe % 3600) // 60)
    seconds = safe % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


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


def seconds_to_frames(value: float, fps: float) -> int:
    safe_fps = fps if fps > 0 else FPS_FALLBACK
    return int(round(max(0.0, value) * safe_fps))


def timecode_to_frames(value: str, fps: float) -> int:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty timecode")
    if re.fullmatch(r"\d+:\d{2}:\d{2}:\d{2}", raw):
        hours, minutes, seconds, frames = map(int, raw.split(":"))
        safe_fps = fps if fps > 0 else FPS_FALLBACK
        return int(round((hours * 3600 + minutes * 60 + seconds) * safe_fps + frames))
    seconds_value = timecode_to_seconds(raw, fps)
    safe_fps = fps if fps > 0 else FPS_FALLBACK
    return int(round(seconds_value * safe_fps))


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip())
    return cleaned.strip("_") or "segment"


def infer_stem_from_csv_name(csv_path: Path) -> str:
    # Legacy format: o_12345_...
    match = re.match(r"(?P<stem>o_\d+)", csv_path.name)
    if match:
        return match.group("stem")
    # Newer format: {rush_stem}_{YYYYMMDD}_{HHMMSS}_groq_html_comparison...
    match = re.match(r"^(?P<stem>.+?)_\d{8}_\d{6}_", csv_path.name)
    if match:
        return match.group("stem")
    raise ValueError(f"Could not infer rush stem from CSV name '{csv_path.name}'.")


def resolve_rush_file(csv_path: Path, rush_dir: Path) -> Path:
    stem = infer_stem_from_csv_name(csv_path)
    candidates = sorted(
        path
        for path in rush_dir.iterdir()
        if path.is_file() and path.suffix in VIDEO_EXTENSIONS and path.stem.lower() == stem.lower()
    )
    if not candidates:
        raise FileNotFoundError(f"No rush video found in '{rush_dir}' matching stem '{stem}'.")
    if len(candidates) > 1:
        raise FileExistsError(f"Multiple rush videos found for stem '{stem}': {', '.join(str(path) for path in candidates)}")
    return candidates[0]


def load_zoom_segments(csv_path: Path, *, timecode_fps: float) -> list[ZoomSegment]:
    segments: list[ZoomSegment] = []
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        required_columns = {
            "Annotation Column",
            "Annotation Value",
            "Row ID",
            "Status",
            "Text",
            "Reference Segment",
            "Start Time",
            "End Time",
            "Source Start Time",
            "Source End Time",
        }
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing CSV columns in '{csv_path}': {', '.join(sorted(missing))}")

        for row in reader:
            if (row.get("Annotation Column") or "").strip() != "Zoom":
                continue
            marker = (row.get("Annotation Value") or "").strip()
            if not marker:
                continue
            segment = ZoomSegment(
                source_csv=csv_path,
                row_id=(row.get("Row ID") or "").strip(),
                marker=marker,
                status=(row.get("Status") or "").strip(),
                text=(row.get("Text") or "").strip(),
                reference_segment=(row.get("Reference Segment") or "").strip(),
                timeline_start_seconds=timecode_to_seconds(row["Start Time"], timecode_fps),
                timeline_end_seconds=timecode_to_seconds(row["End Time"], timecode_fps),
                source_start_seconds=timecode_to_seconds(row["Source Start Time"], timecode_fps),
                source_end_seconds=timecode_to_seconds(row["Source End Time"], timecode_fps),
            )
            if segment.duration > 0:
                segments.append(segment)
    return segments


def determine_zoom_end_values(
    segments: list[ZoomSegment],
    *,
    override_zoom_end: float | None,
) -> dict[str, float]:
    if override_zoom_end is not None:
        return {segment.row_id: max(1.0, override_zoom_end) for segment in segments}

    sorted_segments = sorted(segments, key=lambda item: (item.timeline_start_seconds, item.row_id))
    assignments: dict[str, float] = {}
    idx = 0
    while idx < len(sorted_segments):
        segment = sorted_segments[idx]
        code = segment.marker.strip().lower()
        if code == "z":
            assignments[segment.row_id] = DEFAULT_ZOOM_END_BY_CODE["z"]
            idx += 1
            continue
        if code == "z1":
            sequence = [segment]
            if idx + 1 < len(sorted_segments) and sorted_segments[idx + 1].marker.strip().lower() == "z2":
                sequence.append(sorted_segments[idx + 1])
                if idx + 2 < len(sorted_segments) and sorted_segments[idx + 2].marker.strip().lower() == "z3":
                    sequence.append(sorted_segments[idx + 2])
            pattern = tuple(item.marker.strip().lower() for item in sequence)
            if pattern == ("z1", "z2", "z3"):
                zoom_values = [1.11, 1.26, 1.38]
            elif pattern == ("z1", "z2"):
                zoom_values = [1.15, 1.30]
            else:
                zoom_values = [DEFAULT_ZOOM_END_BY_CODE.get(item.marker.strip().lower(), 1.25) for item in sequence]
            for target, zoom_value in zip(sequence, zoom_values):
                assignments[target.row_id] = zoom_value
            idx += len(sequence)
            continue
        assignments[segment.row_id] = DEFAULT_ZOOM_END_BY_CODE.get(code, 1.25)
        idx += 1
    return assignments


def probe_video_dimensions(video_path: Path) -> tuple[int, int]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    width_text, height_text = result.stdout.strip().split("x", 1)
    return int("".join(c for c in width_text if c.isdigit())), int("".join(c for c in height_text if c.isdigit()))


def probe_video_fps(video_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    rate_text = result.stdout.strip()
    if not rate_text:
        return FPS_FALLBACK
    if "/" in rate_text:
        numerator, denominator = rate_text.split("/", 1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return FPS_FALLBACK
        return float(numerator) / denominator_value
    return float(rate_text)


def extract_segment_stream(
    rush_path: Path,
    temp_video_path: Path,
    start_seconds: float,
    duration: float,
    overwrite: bool,
) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        seconds_to_ffmpeg_time(start_seconds),
        "-t",
        f"{duration:.3f}",
        "-i",
        str(rush_path),
        *_hdr_vf_args(rush_path),
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "bgr24",
    ]
    cmd.append("-y" if overwrite else "-n")
    cmd.append(str(temp_video_path))
    subprocess.run(cmd, check=True)


def load_first_frame(video_path: Path) -> np.ndarray:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open temporary video '{video_path}'.")
    ok, frame = capture.read()
    capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"Could not read first frame from '{video_path}'.")
    return frame


def detect_face_box(frame: np.ndarray, face_net: cv2.dnn_Net) -> tuple[int, int, int, int] | None:
    frame_h, frame_w = frame.shape[:2]
    blob = cv2.dnn.blobFromImage(frame, 1.0, (300, 300), (104.0, 177.0, 123.0))
    face_net.setInput(blob)
    detections = face_net.forward()

    best_box: tuple[int, int, int, int] | None = None
    best_score = 0.0
    for index in range(detections.shape[2]):
        confidence = float(detections[0, 0, index, 2])
        if confidence < 0.55:
            continue
        box = detections[0, 0, index, 3:7] * np.array([frame_w, frame_h, frame_w, frame_h])
        x1, y1, x2, y2 = box.astype(int)
        x1 = max(0, min(x1, frame_w - 1))
        y1 = max(0, min(y1, frame_h - 1))
        x2 = max(x1 + 1, min(x2, frame_w))
        y2 = max(y1 + 1, min(y2, frame_h))
        score = confidence * max(1, (x2 - x1) * (y2 - y1))
        if score > best_score:
            best_score = score
            best_box = (x1, y1, x2, y2)
    return best_box


def detect_eye_focus(frame: np.ndarray, face_box: tuple[int, int, int, int], eye_cascade: cv2.CascadeClassifier) -> FocusPoint:
    x1, y1, x2, y2 = face_box
    roi = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    eyes = eye_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(12, 12))

    if len(eyes) >= 2:
        sorted_eyes = sorted(eyes, key=lambda box: box[2] * box[3], reverse=True)[:2]
        centers = []
        for ex, ey, ew, eh in sorted_eyes:
            centers.append((x1 + ex + ew / 2.0, y1 + ey + eh / 2.0))
        focus_x = sum(center[0] for center in centers) / len(centers)
        focus_y = sum(center[1] for center in centers) / len(centers)
        return FocusPoint(x=focus_x, y=focus_y, method="eyes")

    face_width = x2 - x1
    face_height = y2 - y1
    return FocusPoint(
        x=x1 + face_width / 2.0,
        y=y1 + face_height * 0.38,
        method="face",
    )


def detect_focus_point(
    frame: np.ndarray,
    face_net: cv2.dnn_Net,
    eye_cascade: cv2.CascadeClassifier,
) -> FocusPoint:
    face_box = detect_face_box(frame, face_net)
    if face_box is None:
        frame_h, frame_w = frame.shape[:2]
        return FocusPoint(x=(frame_w - 1) / 2.0, y=(frame_h - 1) / 2.0, method="center_fallback")
    return detect_eye_focus(frame, face_box, eye_cascade)


def clamp_focus_point(focus: FocusPoint, width: int, height: int) -> FocusPoint:
    return FocusPoint(
        x=min(max(focus.x, 0.0), width - 1.0),
        y=min(max(focus.y, 0.0), height - 1.0),
        method=focus.method,
    )


def render_zoom_frames(
    input_video_path: Path,
    output_video_path: Path,
    width: int,
    height: int,
    fps: float,
    zoom_end: float,
    focus: FocusPoint,
) -> None:
    capture = cv2.VideoCapture(str(input_video_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open temporary video '{input_video_path}'.")

    frame_total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    effective_fps = capture.get(cv2.CAP_PROP_FPS)
    if effective_fps and effective_fps > 0:
        fps = effective_fps
    safe_fps = fps if fps > 0 else FPS_FALLBACK
    frame_total = max(frame_total, 1)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(output_video_path), fourcc, safe_fps, (width, height))
    if not writer.isOpened():
        capture.release()
        raise RuntimeError(f"Could not create output video '{output_video_path}'.")

    zoom_end = max(1.0, zoom_end)
    focus = clamp_focus_point(focus, width, height)

    frame_index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break

        progress = frame_index / max(frame_total - 1, 1)
        zoom = 1.0 + (zoom_end - 1.0) * progress
        matrix = np.array(
            [
                [zoom, 0.0, focus.x - zoom * focus.x],
                [0.0, zoom, focus.y - zoom * focus.y],
            ],
            dtype=np.float32,
        )
        transformed = cv2.warpAffine(
            frame,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        writer.write(transformed)
        frame_index += 1

    capture.release()
    writer.release()
    if frame_index == 0:
        raise RuntimeError(f"No frames rendered from temporary video '{input_video_path}'.")


def mux_video(output_video_path: Path, final_output_path: Path, overwrite: bool) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(output_video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    cmd.append("-y" if overwrite else "-n")
    cmd.append(str(final_output_path))
    subprocess.run(cmd, check=True)


def output_name_for_segment(csv_path: Path, segment: ZoomSegment) -> str:
    stem = infer_stem_from_csv_name(csv_path)
    label = slugify(segment.marker)
    row_id = slugify(segment.row_id or "row")
    start_tag = f"{int(round(segment.source_start_seconds * 1000)):09d}ms"
    return f"{stem}_{label}_row{row_id}_{start_tag}.mp4"


def render_segment(
    rush_path: Path,
    output_path: Path,
    segment: ZoomSegment,
    width: int,
    height: int,
    fps: float,
    zoom_end: float,
    overwrite: bool,
    face_net: cv2.dnn_Net,
    eye_cascade: cv2.CascadeClassifier,
) -> FocusPoint:
    with tempfile.TemporaryDirectory(prefix="zoom_face_") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        temp_input = temp_dir / "segment_input.mkv"
        temp_zoomed = temp_dir / "segment_zoomed.mp4"
        extract_segment_stream(
            rush_path=rush_path,
            temp_video_path=temp_input,
            start_seconds=segment.source_start_seconds,
            duration=segment.duration,
            overwrite=True,
        )
        first_frame = load_first_frame(temp_input)
        focus = detect_focus_point(first_frame, face_net=face_net, eye_cascade=eye_cascade)
        render_zoom_frames(
            input_video_path=temp_input,
            output_video_path=temp_zoomed,
            width=width,
            height=height,
            fps=fps,
            zoom_end=zoom_end,
            focus=focus,
        )
        mux_video(temp_zoomed, output_path, overwrite=overwrite)
        return focus


def process_csv(
    csv_path: Path,
    rush_dir: Path,
    output_dir: Path,
    override_zoom_end: float | None,
    timecode_fps: float | None,
    overwrite: bool,
    dry_run: bool,
    face_net: cv2.dnn_Net,
    eye_cascade: cv2.CascadeClassifier,
) -> list[RenderedSegment]:
    rush_path = resolve_rush_file(csv_path, rush_dir)
    effective_timecode_fps = timecode_fps if timecode_fps is not None and timecode_fps > 0 else probe_video_fps(rush_path)
    segments = load_zoom_segments(csv_path, timecode_fps=effective_timecode_fps)
    if not segments:
        print(f"[skip] {csv_path.name}: no Zoom annotations found", file=sys.stderr)
        return []

    width, height = probe_video_dimensions(rush_path)
    fps = probe_video_fps(rush_path)
    zoom_end_values = determine_zoom_end_values(segments, override_zoom_end=override_zoom_end)
    rendered_segments: list[RenderedSegment] = []
    for segment in segments:
        output_path = output_dir / output_name_for_segment(csv_path, segment)
        timeline_start_frames = seconds_to_frames(segment.timeline_start_seconds, effective_timecode_fps)
        timeline_end_frames = seconds_to_frames(segment.timeline_end_seconds, effective_timecode_fps)
        source_start_frames = seconds_to_frames(segment.source_start_seconds, effective_timecode_fps)
        source_end_frames = seconds_to_frames(segment.source_end_seconds, effective_timecode_fps)
        zoom_end = zoom_end_values.get(segment.row_id, DEFAULT_ZOOM_END_BY_CODE["z"])
        if dry_run:
            print(
                f"[zoom] {segment.marker} | row={segment.row_id or '?'} | "
                f"source={seconds_to_ffmpeg_time(segment.source_start_seconds)}-"
                f"{seconds_to_ffmpeg_time(segment.source_end_seconds)} | "
                f"zoom_end={zoom_end:.3f} | output={output_path.name}"
            )
            continue

        focus = render_segment(
            rush_path=rush_path,
            output_path=output_path,
            segment=segment,
            width=width,
            height=height,
            fps=fps,
            zoom_end=zoom_end,
            overwrite=overwrite,
            face_net=face_net,
            eye_cascade=eye_cascade,
        )
        rendered_segments.append(
            RenderedSegment(
                segment=segment,
                focus=focus,
                output_path=output_path.resolve(),
                zoom_end=zoom_end,
                sequence_fps=effective_timecode_fps,
                timeline_start_frames=timeline_start_frames,
                timeline_end_frames=timeline_end_frames,
                source_start_frames=source_start_frames,
                source_end_frames=source_end_frames,
            )
        )
        print(
            f"[zoom] {segment.marker} | row={segment.row_id or '?'} | "
            f"focus={focus.method}@({focus.x:.1f},{focus.y:.1f}) | "
            f"zoom_end={zoom_end:.3f} | output={output_path.name}"
        )
    return rendered_segments


def rendered_segment_to_manifest_entry(item: RenderedSegment) -> dict[str, object]:
    return {
        "row_id": item.segment.row_id,
        "zoom_code": item.segment.marker,
        "timeline_start_frames": item.timeline_start_frames,
        "timeline_end_frames": item.timeline_end_frames,
        "source_start_frames": item.source_start_frames,
        "source_end_frames": item.source_end_frames,
        "output_path": str(item.output_path),
        "focus_method": item.focus.method,
        "focus_x": item.focus.x,
        "focus_y": item.focus.y,
        "zoom_end": item.zoom_end,
    }


def write_manifest(
    manifest_path: Path,
    *,
    entries: list[RenderedSegment],
    sequence_fps: float,
) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sequence_fps": sequence_fps,
        "entries": [rendered_segment_to_manifest_entry(entry) for entry in entries],
    }
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2)
        handle.write("\n")


def main() -> int:
    args = parse_args()
    ensure_dependencies()

    face_net = cv2.dnn.readNetFromCaffe(str(FACE_CONFIG_PATH), str(FACE_MODEL_PATH))
    eye_cascade = cv2.CascadeClassifier(str(EYE_CASCADE_PATH))
    if eye_cascade.empty():
        raise SystemExit(f"Could not load eye cascade from {EYE_CASCADE_PATH}")

    rush_dir = Path(args.rush_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_rendered_segments: list[RenderedSegment] = []
    for raw_csv in args.csv_paths:
        csv_path = Path(raw_csv).expanduser().resolve()
        all_rendered_segments.extend(
            process_csv(
                csv_path=csv_path,
                rush_dir=rush_dir,
                output_dir=output_dir,
                override_zoom_end=args.zoom_end,
                timecode_fps=args.fps,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                face_net=face_net,
                eye_cascade=eye_cascade,
            )
        )

    effective_manifest_fps = (
        all_rendered_segments[0].sequence_fps
        if all_rendered_segments
        else (args.fps if args.fps is not None and args.fps > 0 else FPS_FALLBACK)
    )
    if args.manifest_json and not args.dry_run:
        manifest_path = Path(args.manifest_json).expanduser().resolve()
        write_manifest(
            manifest_path,
            entries=all_rendered_segments,
            sequence_fps=effective_manifest_fps,
        )
        print(f"Manifest written to: {manifest_path}")

    print(f"Processed {len(args.csv_paths)} CSV file(s), planned/rendered {len(all_rendered_segments)} zoom clip(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
