#!/usr/bin/env python3
"""Render an MP4 by concatenating OTIO-described clips via FFmpeg."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from xml_to_otio_converter import ClipRecord, find_latest_pipeline_xml, load_xml_track_records


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FFMPEGER_DIR = PROJECT_ROOT / "ffmpeger_otio_video_maker"
OUTPUT_DIR = FFMPEGER_DIR / "output"
AUDIO_STREAM_CACHE: dict[Path, bool] = {}
HDR_SOURCE_CACHE: dict[Path, bool] = {}

HDR_TRANSFER_FUNCTIONS = {"arib-std-b67", "smpte2084", "bt2020-10", "bt2020-12", "smpte-st-2084"}


def is_hdr_source(path: Path) -> bool:
    """Return True if the file is tagged as HDR (needs colorspace conversion to bt709)."""
    cached = HDR_SOURCE_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-select_streams", "v:0",
                "-show_entries", "stream=color_transfer",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=False,
        )
        transfer = result.stdout.strip().lower()
        hdr = transfer in HDR_TRANSFER_FUNCTIONS
    except Exception:
        hdr = False
    HDR_SOURCE_CACHE[path] = hdr
    return hdr


STILL_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}
GIF_EXTENSIONS = {".gif"}
STATIC_EXTENSIONS = STILL_IMAGE_EXTENSIONS | GIF_EXTENSIONS
TARGET_RUSH_PEAK_DB = -1.0
SILENCE_FLOOR_DB = -50.0
MAX_VIDEO_OVERLAYS_PER_CHUNK = 12
MAX_VIDEO_CHUNK_DURATION_SECONDS = 45.0
CHUNK_EPSILON = 1e-6


@dataclass
class Segment:
    source: Path
    start: float
    duration: float
    order: int
    clip_name: str
    in_time: float
    track_index: int
    fps: float
    media_type: str = "video"
    motion: Optional[Dict[str, Any]] = None
    audio: Optional[Dict[str, Any]] = None

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class TimelineMetadata:
    width: int
    height: int
    fps: float


def compute_expected_duration(tracks: Dict[int, List[Segment]]) -> float:
    durations = [segment.end for segments in tracks.values() for segment in segments]
    return max(durations) if durations else 0.0



def parse_rational_time(payload: Optional[Dict[str, float]], default_rate: float) -> tuple[float, float]:
    if not payload:
        return 0.0, default_rate
    value = float(payload.get("value", 0.0))
    rate = float(payload.get("rate", default_rate) or default_rate)
    return value, rate


def segment_from_clip_record(record: ClipRecord, fps: float) -> Segment:
    tl_duration = max(0, record.end_frame - record.start_frame)
    src_duration = max(0, record.out_frame - record.in_frame)
    duration_frames = tl_duration if tl_duration > 0 else src_duration
    return Segment(
        source=record.source_path.expanduser(),
        start=record.start_frame / fps if fps else 0.0,
        duration=duration_frames / fps if fps else 0.0,
        order=record.start_frame,
        clip_name=record.name,
        in_time=record.in_frame / fps if fps else 0.0,
        track_index=record.track_index,
        fps=float(fps or 25.0),
        media_type=record.media_type,
        motion=record.motion,
        audio=record.audio,
    )


def load_otio_tracks(
    otio_path: Path,
) -> tuple[Dict[int, List[Segment]], Dict[int, List[Segment]], TimelineMetadata]:
    data = json.loads(otio_path.read_text(encoding="utf-8"))
    sequence_metadata = (data.get("metadata") or {}).get("sequence") or {}
    tracks_payload = ((data.get("tracks") or {}).get("children")) or []
    video_tracks: Dict[int, List[Segment]] = {}
    audio_tracks: Dict[int, List[Segment]] = {}
    for track in tracks_payload:
        metadata = track.get("metadata") or {}
        track_type = metadata.get("track_type", "video")
        auto_index = len(video_tracks) + len(audio_tracks) + 1
        track_index = int(metadata.get("track_index") or auto_index)
        children = track.get("children") or []
        segments: List[Segment] = []
        for clip in children:
            media_ref = clip.get("media_reference") or {}
            path_value = media_ref.get("target_url") or media_ref.get("url")
            if not path_value:
                continue
            source_range = clip.get("source_range") or {}
            duration_payload = source_range.get("duration")
            start_payload = source_range.get("start_time")
            duration_value, duration_rate = parse_rational_time(duration_payload, 25.0)
            start_value, start_rate = parse_rational_time(start_payload, duration_rate or 25.0)
            if duration_rate <= 0:
                duration_rate = 25.0
            duration_seconds = duration_value / duration_rate if duration_rate else 0.0
            source_in_seconds = start_value / start_rate if start_rate else 0.0
            if duration_seconds <= 0:
                continue
            order = int((clip.get("metadata") or {}).get("premiere", {}).get("timeline_start", 0))
            timeline_start_seconds = order / duration_rate if duration_rate else 0.0
            premiere_metadata = (clip.get("metadata") or {}).get("premiere") or {}
            segments.append(
                Segment(
                    source=Path(path_value).expanduser(),
                    start=timeline_start_seconds,
                    duration=duration_seconds,
                    order=order,
                    clip_name=str(clip.get("name") or "clip"),
                    in_time=source_in_seconds,
                    track_index=track_index,
                    fps=duration_rate or 25.0,
                    media_type=track_type,
                    motion=premiere_metadata.get("motion"),
                    audio=premiere_metadata.get("audio"),
                )
            )
        segments.sort(key=lambda seg: (seg.order, seg.start))
        if segments:
            target = video_tracks if track_type == "video" else audio_tracks
            target[track_index] = segments
    if not video_tracks:
        raise RuntimeError("No video tracks found in OTIO file.")
    fps = float(sequence_metadata.get("fps") or next(iter(video_tracks.values()))[0].fps)
    width = int(sequence_metadata.get("width") or 0)
    height = int(sequence_metadata.get("height") or 0)
    return video_tracks, audio_tracks, TimelineMetadata(width=width, height=height, fps=fps)


def load_xml_segments(xml_path: Path) -> tuple[Dict[int, List[Segment]], Dict[int, List[Segment]], TimelineMetadata]:
    fps, (width, height), video_records, audio_records = load_xml_track_records(xml_path)
    video_tracks = {
        track_index: [segment_from_clip_record(record, fps) for record in records]
        for track_index, records in video_records.items()
    }
    audio_tracks = {
        track_index: [segment_from_clip_record(record, fps) for record in records]
        for track_index, records in audio_records.items()
    }
    if not video_tracks:
        raise RuntimeError("No video tracks found in XML file.")
    return video_tracks, audio_tracks, TimelineMetadata(width=width, height=height, fps=float(fps))


def run_ffmpeg(command: List[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {result.stderr.strip() or result.stdout.strip()}")


def run_ffprobe(args: List[str]) -> subprocess.CompletedProcess[str]:
    command = ["ffprobe", "-v", "error", *args]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def media_has_audio_stream(media_path: Path) -> bool:
    cached = AUDIO_STREAM_CACHE.get(media_path)
    if cached is not None:
        return cached
    probe = run_ffprobe(
        [
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(media_path),
        ]
    )
    has_audio = probe.stdout.strip() != ""
    AUDIO_STREAM_CACHE[media_path] = has_audio
    return has_audio


def probe_media_duration(video_path: Path) -> float:
    probe = run_ffprobe(
        [
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
    )
    try:
        return float(probe.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Unable to determine duration for {video_path}") from exc


def analyze_audio_peak_db(audio_path: Path) -> Optional[float]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(audio_path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?)|inf|-inf)\s*dB", output, re.IGNORECASE)
    if not match:
        return None
    token = match.group(1).lower()
    if token in {"inf", "-inf"}:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def compute_peak_safe_gain_multiplier(
    peak_db: Optional[float],
    *,
    target_peak_db: float = TARGET_RUSH_PEAK_DB,
    silence_floor_db: float = SILENCE_FLOOR_DB,
) -> float:
    if peak_db is None or not math.isfinite(peak_db) or peak_db <= silence_floor_db:
        return 1.0
    return math.pow(10.0, (target_peak_db - peak_db) / 20.0)


def validate_video_output(video_path: Path, expected_duration: float, tolerance: float = 0.75) -> None:
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    if not media_has_audio_stream(video_path):
        raise RuntimeError(f"{video_path} does not contain an audio stream.")
    if expected_duration > 0:
        actual_duration = probe_media_duration(video_path)
        if math.isnan(actual_duration) or abs(actual_duration - expected_duration) > tolerance:
            raise RuntimeError(
                f"{video_path} duration mismatch. Expected ~{expected_duration:.2f}s, got {actual_duration:.2f}s."
            )


def probe_video_dimensions(media_path: Path) -> tuple[int, int]:
    probe = run_ffprobe(
        [
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(media_path),
        ]
    )
    parts = probe.stdout.strip().split("x")
    if len(parts) != 2:
        raise RuntimeError(f"Unable to determine dimensions for {media_path}")
    return int(parts[0]), int(parts[1])


def resolve_timeline_metadata(
    video_tracks: Dict[int, List[Segment]],
    metadata: Optional[TimelineMetadata],
) -> TimelineMetadata:
    first_segment = next(iter(video_tracks.values()))[0]
    fps = float(metadata.fps) if metadata and metadata.fps > 0 else float(first_segment.fps or 25.0)
    width = int(metadata.width) if metadata and metadata.width > 0 else 0
    height = int(metadata.height) if metadata and metadata.height > 0 else 0
    if width <= 0 or height <= 0:
        width, height = probe_video_dimensions(first_segment.source)
    return TimelineMetadata(width=width, height=height, fps=fps)


def trim_audio_segment(segment: Segment, destination: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{segment.in_time:.6f}",
        "-i",
        str(segment.source),
        "-t",
        f"{segment.duration:.6f}",
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        str(destination),
    ]
    run_ffmpeg(cmd)


def premiere_level_to_multiplier(level: float) -> float:
    db_value = level / 100.0
    return math.pow(10.0, db_value / 20.0)


def overlay_volume(segment: Segment) -> float:
    if segment.audio and "level" in segment.audio:
        return premiere_level_to_multiplier(float(segment.audio["level"]))
    return 1.0


def overlay_fade_filters(segment: Segment) -> str:
    if not segment.audio:
        return ""
    fade_parts: List[str] = []
    fade_in_frames = int(segment.audio.get("fade_in_frames", 0) or 0)
    fade_out_frames = int(segment.audio.get("fade_out_frames", 0) or 0)
    if fade_in_frames > 0 and segment.fps > 0:
        fade_in_seconds = min(segment.duration, fade_in_frames / segment.fps)
        if fade_in_seconds > 0:
            fade_parts.append(f"afade=t=in:st=0:d={fade_in_seconds:.6f}")
    if fade_out_frames > 0 and segment.fps > 0:
        fade_out_seconds = min(segment.duration, fade_out_frames / segment.fps)
        fade_out_start = max(0.0, segment.duration - fade_out_seconds)
        if fade_out_seconds > 0:
            fade_parts.append(f"afade=t=out:st={fade_out_start:.6f}:d={fade_out_seconds:.6f}")
    return ",".join(fade_parts)


def mix_audio_tracks(
    base_audio: Path,
    overlays: List[Segment],
    destination: Path,
    *,
    base_volume_multiplier: float = 1.0,
) -> Path:
    if not overlays and math.isclose(base_volume_multiplier, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        shutil.copy2(base_audio, destination)
        return destination
    tmp_dir = tempfile.TemporaryDirectory(prefix="ffmpeger_audio_mix_")
    try:
        cmd: List[str] = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(base_audio),
        ]
        if math.isclose(base_volume_multiplier, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            filter_parts = ["[0:a]anull[a0]"]
        else:
            filter_parts = [f"[0:a]volume={base_volume_multiplier:.9f}[a0]"]
        mix_inputs = ["a0"]
        for idx, segment in enumerate(overlays, start=1):
            snippet_path = Path(tmp_dir.name) / f"overlay_{idx:04d}.wav"
            trim_audio_segment(segment, snippet_path)
            cmd.extend(["-i", str(snippet_path)])
            delay_ms = max(0, int(round(segment.start * 1000)))
            input_label = f"{idx}:a"
            overlay_label = f"a{idx}"
            volume = overlay_volume(segment)
            filter_chain = f"[{input_label}]adelay={delay_ms}|{delay_ms},aresample=async=1"
            if not math.isclose(volume, 1.0):
                filter_chain += f",volume={volume}"
            fade_chain = overlay_fade_filters(segment)
            if fade_chain:
                filter_chain += f",{fade_chain}"
            filter_parts.append(f"{filter_chain}[{overlay_label}]")
            mix_inputs.append(overlay_label)
        mix_expression = "".join(f"[{label}]" for label in mix_inputs)
        filter_parts.append(f"{mix_expression}amix=inputs={len(mix_inputs)}:normalize=0[aout]")
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[aout]",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ]
        )
        run_ffmpeg(cmd)
        return destination
    finally:
        tmp_dir.cleanup()


def combine_video_and_audio(video_path: Path, audio_path: Path, destination: Path, *, duration: Optional[float] = None) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-movflags",
        "+faststart",
    ]
    if duration is not None and duration > 0:
        cmd.extend(["-t", f"{duration:.6f}"])
    cmd.append(str(destination))
    run_ffmpeg(cmd)


def segment_signature(segment: Segment) -> tuple[str, int, int, int]:
    try:
        source = str(segment.source.resolve())
    except FileNotFoundError:
        source = str(segment.source)
    start_ms = int(round(segment.start * 1000))
    duration_ms = int(round(segment.duration * 1000))
    in_ms = int(round(segment.in_time * 1000))
    return source, start_ms, duration_ms, in_ms


def _stem_has_token(stem: str, token: str) -> bool:
    return bool(re.search(rf"(?:^|[_\-\s]){re.escape(token)}(?:$|[_\-\s])", stem, re.IGNORECASE))


def should_derive_audio_from_video(segment: Segment) -> bool:
    if segment.source.suffix.lower() in STATIC_EXTENSIONS:
        return False
    # DIRECT-tagged clips are intentionally muted in the XML pipeline — skip them.
    if _stem_has_token(segment.source.stem, "DIRECT"):
        return False
    return True


def extract_audio_track(video_path: Path, destination: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        str(destination),
    ]
    run_ffmpeg(cmd)


def find_base_audio_track(
    audio_tracks: Dict[int, List[Segment]],
    primary_video_source: Path | None,
) -> Optional[int]:
    for index, segments in audio_tracks.items():
        first = segments[0] if segments else None
        if first and primary_video_source:
            try:
                if first.source.resolve() == primary_video_source.resolve():
                    return index
            except FileNotFoundError:
                continue
    return None


def derive_audio_segments_from_video(
    overlays: List[Segment],
    existing_signatures: set[tuple[str, int, int, int]],
) -> List[Segment]:
    derived: List[Segment] = []
    for segment in overlays:
        if segment.source.suffix.lower() in STATIC_EXTENSIONS:
            continue
        if not should_derive_audio_from_video(segment):
            continue
        signature = segment_signature(segment)
        if signature in existing_signatures:
            continue
        try:
            if not media_has_audio_stream(segment.source):
                continue
        except RuntimeError:
            continue
        derived.append(segment)
    return derived


def latest_otio_file(search_dir: Path = OUTPUT_DIR) -> Path:
    candidates = sorted(
        search_dir.glob("*.otio"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"No OTIO files found in {search_dir}")
    return candidates[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create an MP4 output from an OTIO file.")
    parser.add_argument(
        "--otio",
        type=Path,
        help="Path to the OTIO file (defaults to the most recent .otio inside ffmpeger_otio_video_maker/output).",
    )
    parser.add_argument(
        "--xml",
        type=Path,
        help="Path to a Premiere XML file to render directly with FFmpeg.",
    )
    parser.add_argument("--output", type=Path, help="Destination MP4 path.")
    parser.add_argument(
        "--track-index",
        type=int,
        help="Optional track index to render (defaults to the first video track).",
    )
    return parser.parse_args()


def build_overlay_position_expr(overlay: Segment, axis: str) -> str:
    motion = overlay.motion or {}
    center_key = "center_horiz" if axis == "x" else "center_vert"
    anchor_key = "anchor_horiz" if axis == "x" else "anchor_vert"
    center_offset = float(motion.get(center_key, 0.0))
    anchor_offset = float(motion.get(anchor_key, 0.0))
    base_expr = "(W-w)/2" if axis == "x" else "(H-h)/2"
    return f"{base_expr}+({center_offset:.6f})-({anchor_offset:.6f})"


def build_overlay_filter_chain(input_label: str, overlay: Segment, output_label: str, fps: float = 30.0) -> str:
    motion = overlay.motion or {}
    if is_hdr_source(overlay.source):
        steps: List[str] = [
            f"[{input_label}]"
            f"zscale=transfer=linear:primaries=bt709:matrix=bt709:rangein=limited:range=pc:npl=100,"
            f"format=gbrpf32le,"
            f"zscale=primaries=bt709,"
            f"tonemap=tonemap=hable:desat=0,"
            f"zscale=transfer=bt709:matrix=bt709:range=tv,"
            f"format=rgba"
        ]
    else:
        steps = [f"[{input_label}]format=rgba"]
    left = max(0.0, float(motion.get("leftcrop", 0.0)))
    top = max(0.0, float(motion.get("topcrop", 0.0)))
    right = max(0.0, float(motion.get("rightcrop", 0.0)))
    bottom = max(0.0, float(motion.get("bottomcrop", 0.0)))
    if any(value > 0.0 for value in (left, top, right, bottom)):
        width_factor = max(0.01, 1.0 - ((left + right) / 100.0))
        height_factor = max(0.01, 1.0 - ((top + bottom) / 100.0))
        steps.append(
            "crop="
            f"w='max(2,trunc(iw*{width_factor:.6f}/2)*2)':"
            f"h='max(2,trunc(ih*{height_factor:.6f}/2)*2)':"
            f"x='iw*{left/100.0:.6f}':"
            f"y='ih*{top/100.0:.6f}'"
        )
    scale_percent = max(0.01, float(motion.get("scale", 100.0)))
    if not math.isclose(scale_percent, 100.0, rel_tol=1e-6, abs_tol=1e-6):
        scale_ratio = scale_percent / 100.0
        steps.append(
            f"scale='max(2,trunc(iw*{scale_ratio:.6f}/2)*2)':'max(2,trunc(ih*{scale_ratio:.6f}/2)*2)'"
        )
    # Normalize each trimmed input back to t=0 before placing it on the chunk timeline.
    # Without STARTPTS, some seeked sources can land a frame late at chunk boundaries,
    # which briefly exposes the synthetic black base instead of the rush frame.
    # Floor static inputs to the nearest output frame boundary to avoid a
    # single-frame white flash caused by sub-frame PTS offset.
    suffix = overlay.source.suffix.lower()
    if suffix in STATIC_EXTENSIONS and fps > 0:
        frame_start = math.floor(overlay.start * fps) / fps
    else:
        frame_start = overlay.start
    steps.append(f"setpts=PTS-STARTPTS+{frame_start:.6f}/TB")
    return f"{','.join(steps)}[{output_label}]"


def normalized_insert_companion_key(segment: Segment) -> Optional[str]:
    name = re.sub(r"^\d+m\d+_\d+_", "", segment.source.stem.lower())
    if "_filled" in name:
        return name.replace("_filled", "")
    if "_trans_" in name:
        return name.split("_trans_", 1)[1]
    return None


def should_render_video_overlay(overlay: Segment, overlays: List[Segment]) -> bool:
    name = overlay.source.stem.lower()
    if "_filled" not in name:
        return True
    overlay_key = normalized_insert_companion_key(overlay)
    if not overlay_key:
        return True
    for candidate in overlays:
        if candidate is overlay:
            continue
        candidate_name = candidate.source.stem.lower()
        if "_trans_" not in candidate_name:
            continue
        if not math.isclose(candidate.start, overlay.start, abs_tol=1e-6):
            continue
        if not math.isclose(candidate.duration, overlay.duration, abs_tol=1e-6):
            continue
        if normalized_insert_companion_key(candidate) == overlay_key:
            return False
    return True


def collect_render_overlays(video_tracks: Dict[int, List[Segment]]) -> List[Segment]:
    overlays = [
        segment
        for track_index in sorted(video_tracks)
        for segment in sorted(video_tracks[track_index], key=lambda seg: (seg.start, seg.order, seg.in_time))
    ]
    filtered = [segment for segment in overlays if should_render_video_overlay(segment, overlays)]
    # Outro-dip must composite on top of every other layer (dip to black covers zoom shifts etc.)
    non_outro = [s for s in filtered if "outro_dip" not in s.source.stem.lower()]
    outro = [s for s in filtered if "outro_dip" in s.source.stem.lower()]
    return non_outro + outro


def clip_segment_to_window(segment: Segment, window_start: float, window_end: float) -> Optional[Segment]:
    clipped_start = max(segment.start, window_start)
    clipped_end = min(segment.end, window_end)
    clipped_duration = clipped_end - clipped_start
    if clipped_duration <= CHUNK_EPSILON:
        return None
    source_offset = max(0.0, clipped_start - segment.start)
    clipped_in_time = segment.in_time
    if segment.source.suffix.lower() not in STATIC_EXTENSIONS:
        clipped_in_time += source_offset
    return Segment(
        source=segment.source,
        start=clipped_start - window_start,
        duration=clipped_duration,
        order=segment.order,
        clip_name=segment.clip_name,
        in_time=clipped_in_time,
        track_index=segment.track_index,
        fps=segment.fps,
        media_type=segment.media_type,
        motion=segment.motion,
        audio=segment.audio,
    )


def build_render_chunks(
    overlays: List[Segment],
    duration: float,
    *,
    max_overlays_per_chunk: int = MAX_VIDEO_OVERLAYS_PER_CHUNK,
    max_chunk_duration: float = MAX_VIDEO_CHUNK_DURATION_SECONDS,
) -> List[tuple[float, float]]:
    normalized_duration = max(0.0, duration)
    if normalized_duration <= CHUNK_EPSILON:
        return []
    points = {0.0, normalized_duration}
    for overlay in overlays:
        points.add(max(0.0, min(normalized_duration, overlay.start)))
        points.add(max(0.0, min(normalized_duration, overlay.end)))
    if max_chunk_duration > CHUNK_EPSILON:
        cursor = max_chunk_duration
        while cursor < normalized_duration - CHUNK_EPSILON:
            points.add(cursor)
            cursor += max_chunk_duration
    sorted_points = sorted(points)
    chunks: List[tuple[float, float]] = []
    chunk_start = sorted_points[0]
    last_boundary = chunk_start
    for point in sorted_points[1:]:
        if point <= chunk_start + CHUNK_EPSILON:
            last_boundary = point
            continue
        candidate_overlays = [segment for segment in overlays if segment.start < point and segment.end > chunk_start]
        candidate_duration = point - chunk_start
        exceeds_limits = (
            len(candidate_overlays) > max_overlays_per_chunk
            or candidate_duration > max_chunk_duration + CHUNK_EPSILON
        )
        if exceeds_limits and last_boundary > chunk_start + CHUNK_EPSILON:
            chunks.append((chunk_start, last_boundary))
            chunk_start = last_boundary
            candidate_duration = point - chunk_start
            if candidate_duration <= CHUNK_EPSILON:
                last_boundary = point
                continue
        last_boundary = point
    if last_boundary > chunk_start + CHUNK_EPSILON:
        chunks.append((chunk_start, last_boundary))
    return chunks


def ffmpeg_concat_quote(path: Path) -> str:
    text = str(path)
    return text.replace("'", r"'\''")


def concat_video_chunks(chunk_paths: List[Path], destination: Path) -> None:
    if not chunk_paths:
        raise RuntimeError("No rendered video chunks were produced.")
    if len(chunk_paths) == 1:
        shutil.copy2(chunk_paths[0], destination)
        return
    with tempfile.TemporaryDirectory(prefix="ffmpeger_concat_") as tmp_dir:
        list_path = Path(tmp_dir) / "chunks.txt"
        lines = [f"file '{ffmpeg_concat_quote(path)}'" for path in chunk_paths]
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        run_ffmpeg(cmd)


def render_timeline_chunk(
    overlays: List[Segment],
    destination: Path,
    metadata: TimelineMetadata,
    duration: float,
) -> Path:
    cmd: List[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={metadata.width}x{metadata.height}:r={metadata.fps:.6f}:d={duration:.6f}",
    ]
    filter_parts: List[str] = ["[0:v]format=yuv420p[base0]"]
    current_label = "base0"
    for idx, overlay in enumerate(overlays, start=1):
        if not overlay.source.exists():
            raise FileNotFoundError(f"Missing media file: {overlay.source}")
        suffix = overlay.source.suffix.lower()
        if suffix in STILL_IMAGE_EXTENSIONS:
            cmd.extend(["-loop", "1", "-t", f"{overlay.duration:.6f}", "-i", str(overlay.source)])
        elif suffix in GIF_EXTENSIONS:
            cmd.extend(["-stream_loop", "-1", "-t", f"{overlay.duration:.6f}", "-i", str(overlay.source)])
        else:
            cmd.extend(
                [
                    "-ss",
                    f"{overlay.in_time:.6f}",
                    "-t",
                    f"{overlay.duration:.6f}",
                    "-i",
                    str(overlay.source),
                ]
            )
        input_label = f"{idx}:v"
        overlay_label = f"ov{idx}"
        filter_parts.append(build_overlay_filter_chain(input_label, overlay, overlay_label, metadata.fps))
        out_label = f"base{idx}"
        x_expr = build_overlay_position_expr(overlay, "x")
        y_expr = build_overlay_position_expr(overlay, "y")
        filter_parts.append(
            f"[{current_label}][{overlay_label}]overlay="
            f"x='{x_expr}':y='{y_expr}':eof_action=pass:repeatlast=0:format=auto[{out_label}]"
        )
        current_label = out_label
    if overlays:
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                f"[{current_label}]",
            ]
        )
    else:
        cmd.extend(["-map", "0:v:0"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-crf", "18",
            "-preset", "slow",
            "-pix_fmt",
            "yuv420p",
            "-colorspace", "1",
            "-color_primaries", "1",
            "-color_trc", "1",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    run_ffmpeg(cmd)
    return destination


def render_timeline_video(
    video_tracks: Dict[int, List[Segment]],
    destination: Path,
    metadata: TimelineMetadata,
    duration: float,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    overlays = collect_render_overlays(video_tracks)
    chunks = build_render_chunks(overlays, duration)
    if not chunks:
        render_timeline_chunk([], destination, metadata, duration)
        return destination
    print(
        f"Rendering timeline video in {len(chunks)} chunk(s) "
        f"(<= {MAX_VIDEO_OVERLAYS_PER_CHUNK} inputs per chunk, <= {MAX_VIDEO_CHUNK_DURATION_SECONDS:.0f}s each)."
    )
    with tempfile.TemporaryDirectory(prefix="ffmpeger_video_chunks_") as tmp_dir:
        chunk_paths: List[Path] = []
        for idx, (chunk_start, chunk_end) in enumerate(chunks, start=1):
            local_overlays = [
                clipped
                for overlay in overlays
                if (clipped := clip_segment_to_window(overlay, chunk_start, chunk_end)) is not None
            ]
            chunk_path = Path(tmp_dir) / f"chunk_{idx:04d}.mp4"
            render_timeline_chunk(local_overlays, chunk_path, metadata, chunk_end - chunk_start)
            chunk_paths.append(chunk_path)
        concat_video_chunks(chunk_paths, destination)
    return destination


def create_silent_audio(duration: float, destination: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t",
        f"{duration:.6f}",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    run_ffmpeg(cmd)


def render_mp4_from_track_maps(
    video_tracks: Dict[int, List[Segment]],
    audio_tracks: Dict[int, List[Segment]],
    destination: Path,
    metadata: Optional[TimelineMetadata] = None,
    track_index: Optional[int] = None,
) -> Path:
    selected_video_tracks = video_tracks
    if track_index is not None:
        if track_index not in video_tracks:
            raise RuntimeError(f"Track {track_index} has no segments to render.")
        selected_video_tracks = {track_index: video_tracks[track_index]}
    expected_duration = compute_expected_duration(selected_video_tracks)
    timeline_metadata = resolve_timeline_metadata(selected_video_tracks, metadata)
    with tempfile.TemporaryDirectory(prefix="ffmpeger_base_") as tmp_dir:
        video_only_path = Path(tmp_dir) / "video_only.mp4"
        render_timeline_video(selected_video_tracks, video_only_path, timeline_metadata, expected_duration)
        overlay_audio_segments: List[Segment] = []
        existing_audio_signatures: set[tuple[str, int, int, int]] = set()
        for idx in sorted(audio_tracks):
            overlay_audio_segments.extend(sorted(audio_tracks[idx], key=lambda seg: (seg.start, seg.order, seg.in_time)))
        for seg in overlay_audio_segments:
            existing_audio_signatures.add(segment_signature(seg))
        all_video_segments = [
            segment
            for idx in sorted(selected_video_tracks)
            for segment in selected_video_tracks[idx]
        ]
        derived_from_video = derive_audio_segments_from_video(all_video_segments, existing_audio_signatures)
        overlay_audio_segments.extend(derived_from_video)
        overlay_audio_segments.sort(key=lambda seg: (seg.start, seg.track_index))
        base_audio_path = Path(tmp_dir) / "base_audio.wav"
        create_silent_audio(expected_duration, base_audio_path)
        base_peak_db = analyze_audio_peak_db(base_audio_path)
        base_volume_multiplier = compute_peak_safe_gain_multiplier(base_peak_db)
        mixed_audio_path = Path(tmp_dir) / "mixed_audio.wav"
        mix_audio_tracks(
            base_audio_path,
            overlay_audio_segments,
            mixed_audio_path,
            base_volume_multiplier=base_volume_multiplier,
        )
        combine_video_and_audio(video_only_path, mixed_audio_path, destination, duration=expected_duration)
    validate_video_output(destination, expected_duration)
    return destination


def render_mp4_from_otio(otio_path: Path, destination: Optional[Path] = None, track_index: Optional[int] = None) -> Path:
    if not otio_path.exists():
        raise FileNotFoundError(otio_path)
    video_tracks, audio_tracks, metadata = load_otio_tracks(otio_path)
    destination = destination or (OUTPUT_DIR / f"{otio_path.stem}.mp4")
    return render_mp4_from_track_maps(video_tracks, audio_tracks, destination, metadata, track_index)


def render_mp4_from_xml(xml_path: Path, destination: Optional[Path] = None, track_index: Optional[int] = None) -> Path:
    if not xml_path.exists():
        raise FileNotFoundError(xml_path)
    video_tracks, audio_tracks, metadata = load_xml_segments(xml_path)
    destination = destination or (OUTPUT_DIR / f"{xml_path.stem}.mp4")
    return render_mp4_from_track_maps(video_tracks, audio_tracks, destination, metadata, track_index)


def main() -> None:
    args = parse_args()
    if args.xml:
        xml_path = args.xml.expanduser()
        output_path = render_mp4_from_xml(xml_path, args.output, args.track_index)
    elif args.otio:
        otio_path = args.otio.expanduser()
        output_path = render_mp4_from_otio(otio_path, args.output, args.track_index)
    else:
        xml_path = find_latest_pipeline_xml()
        if xml_path:
            output_path = render_mp4_from_xml(xml_path, args.output, args.track_index)
        else:
            otio_path = latest_otio_file()
            output_path = render_mp4_from_otio(otio_path, args.output, args.track_index)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
