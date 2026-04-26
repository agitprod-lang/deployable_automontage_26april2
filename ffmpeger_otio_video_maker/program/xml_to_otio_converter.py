#!/usr/bin/env python3
"""Convert Premiere XML timelines into minimal OTIO JSON files."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FFMPEGER_DIR = PROJECT_ROOT / "ffmpeger_otio_video_maker"
INPUT_DIR = FFMPEGER_DIR / "input"
OUTPUT_DIR = FFMPEGER_DIR / "output"
FINAL_XML_DIR = PROJECT_ROOT / "xml_insertor" / "output"
FALLBACK_XML_DIR = PROJECT_ROOT / "xml_editor_after_comparser" / "output"


@dataclass
class ClipRecord:
    name: str
    source_path: Path
    start_frame: int
    end_frame: int
    in_frame: int
    out_frame: int
    track_index: int
    media_type: str = "video"
    motion: Optional[Dict[str, Any]] = None
    audio: Optional[Dict[str, Any]] = None

    @property
    def duration_frames(self) -> int:
        return max(0, self.out_frame - self.in_frame)


def parse_int(value: Optional[str], default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_pathurl(pathurl: Optional[str]) -> Optional[Path]:
    if not pathurl:
        return None
    if pathurl.startswith("file://"):
        parsed = urlparse(pathurl)
        candidate = unquote(parsed.path or "")
    else:
        candidate = unquote(pathurl)
    if not candidate:
        return None
    return Path(candidate)


def parse_float(value: Optional[str], default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_point_parameter(parameter_el: ET.Element) -> Tuple[float, float]:
    value_el = parameter_el.find("value")
    if value_el is None:
        return 0.0, 0.0
    return (
        parse_float(value_el.findtext("horiz"), 0.0),
        parse_float(value_el.findtext("vert"), 0.0),
    )


def extract_motion_and_audio_metadata(clip_el: ET.Element) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    motion: Dict[str, Any] = {}
    audio: Dict[str, Any] = {}
    for effect_el in clip_el.findall("./filter/effect"):
        effect_name = (effect_el.findtext("name") or "").strip().lower()
        effect_id = (effect_el.findtext("effectid") or "").strip().lower()
        if effect_name == "basic motion":
            for parameter_el in effect_el.findall("parameter"):
                parameter_id = (parameter_el.findtext("parameterid") or "").strip().lower()
                if parameter_id == "scale":
                    motion["scale"] = parse_float(parameter_el.findtext("value"), 100.0)
                elif parameter_id == "center":
                    horiz, vert = parse_point_parameter(parameter_el)
                    motion["center_horiz"] = horiz
                    motion["center_vert"] = vert
                elif parameter_id == "centeroffset":
                    horiz, vert = parse_point_parameter(parameter_el)
                    motion["anchor_horiz"] = horiz
                    motion["anchor_vert"] = vert
                elif parameter_id in {"leftcrop", "topcrop", "rightcrop", "bottomcrop"}:
                    motion[parameter_id] = parse_float(parameter_el.findtext("value"), 0.0)
        elif effect_name == "volume" or effect_id == "volume":
            for parameter_el in effect_el.findall("parameter"):
                parameter_id = (parameter_el.findtext("parameterid") or "").strip().lower()
                if parameter_id == "level":
                    audio["level"] = parse_float(parameter_el.findtext("value"), 0.0)
        elif effect_id == "codex_audio_meta":
            for parameter_el in effect_el.findall("parameter"):
                parameter_id = (parameter_el.findtext("parameterid") or "").strip().lower()
                if parameter_id == "fadeinframes":
                    audio["fade_in_frames"] = parse_int(parameter_el.findtext("value"), 0)
                elif parameter_id == "fadeoutframes":
                    audio["fade_out_frames"] = parse_int(parameter_el.findtext("value"), 0)
    return (motion or None, audio or None)


def build_file_lookup(sequence_el: ET.Element) -> Dict[str, Path]:
    lookup: Dict[str, Path] = {}
    for file_el in sequence_el.findall(".//file"):
        file_id = file_el.get("id")
        if not file_id:
            continue
        pathurl = file_el.findtext("pathurl")
        resolved = normalize_pathurl(pathurl)
        if resolved:
            lookup[file_id] = resolved
    return lookup


def extract_sequence_fps(sequence_el: ET.Element) -> int:
    return parse_int(sequence_el.findtext("./rate/timebase"), default=25)


def extract_sequence_dimensions(sequence_el: ET.Element) -> tuple[int, int]:
    width = parse_int(sequence_el.findtext("./media/video/format/samplecharacteristics/width"), 0)
    height = parse_int(sequence_el.findtext("./media/video/format/samplecharacteristics/height"), 0)
    return width, height


def iter_media_clips(sequence_el: ET.Element, media_type: str) -> List[ClipRecord]:
    file_lookup = build_file_lookup(sequence_el)
    clips: List[ClipRecord] = []
    track_xpath = f"./media/{media_type}/track"
    for track_index, track_el in enumerate(sequence_el.findall(track_xpath), start=1):
        for clip_el in track_el.findall("clipitem"):
            file_el = clip_el.find("file")
            source_path: Optional[Path] = None
            if file_el is not None:
                pathurl = file_el.findtext("pathurl")
                if pathurl:
                    source_path = normalize_pathurl(pathurl)
                if source_path is None:
                    reference_id = file_el.get("id")
                    if reference_id:
                        source_path = file_lookup.get(reference_id)
            if source_path is None:
                continue
            motion, audio = extract_motion_and_audio_metadata(clip_el)
            record = ClipRecord(
                name=clip_el.findtext("name", "clip"),
                source_path=source_path,
                start_frame=parse_int(clip_el.findtext("start")),
                end_frame=parse_int(clip_el.findtext("end")),
                in_frame=parse_int(clip_el.findtext("in")),
                out_frame=parse_int(clip_el.findtext("out")),
                track_index=track_index,
                media_type=media_type,
                motion=motion,
                audio=audio,
            )
            if record.duration_frames <= 0:
                continue
            clips.append(record)
    clips.sort(key=lambda item: (item.track_index, item.start_frame, item.in_frame))
    return clips


def rational_time(value: float, rate: float) -> Dict[str, float]:
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "rate": float(rate),
        "value": float(value),
    }


def clip_to_otio_dict(record: ClipRecord, fps: float) -> Dict[str, object]:
    duration = record.out_frame - record.in_frame
    premiere_metadata: Dict[str, Any] = {
        "track_index": record.track_index,
        "timeline_start": record.start_frame,
        "timeline_end": record.end_frame,
    }
    if record.motion:
        premiere_metadata["motion"] = record.motion
    if record.audio:
        premiere_metadata["audio"] = record.audio
    return {
        "OTIO_SCHEMA": "Clip.1",
        "name": record.name,
        "metadata": {
            "premiere": premiere_metadata,
            "track_type": record.media_type,
        },
        "source_range": {
            "OTIO_SCHEMA": "TimeRange.1",
            "start_time": rational_time(record.in_frame, fps),
            "duration": rational_time(duration, fps),
        },
        "effects": [],
        "markers": [],
        "enabled": True,
        "media_reference": {
            "OTIO_SCHEMA": "ExternalReference.1",
            "target_url": str(record.source_path),
            "available_range": {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": rational_time(0, fps),
                "duration": rational_time(max(record.out_frame, record.duration_frames), fps),
            },
        },
    }


def gap_to_otio_dict(duration_frames: int, fps: float) -> Dict[str, object]:
    return {
        "OTIO_SCHEMA": "Gap.1",
        "name": "",
        "metadata": {},
        "source_range": {
            "OTIO_SCHEMA": "TimeRange.1",
            "start_time": rational_time(0, fps),
            "duration": rational_time(duration_frames, fps),
        },
        "effects": [],
        "markers": [],
        "enabled": True,
    }


def build_track_children(records: List[ClipRecord], fps: float) -> List[Dict[str, object]]:
    children: List[Dict[str, object]] = []
    cursor = 0
    for record in sorted(records, key=lambda r: (r.start_frame, r.in_frame)):
        if record.start_frame > cursor:
            children.append(gap_to_otio_dict(record.start_frame - cursor, fps))
            cursor = record.start_frame
        elif record.start_frame < cursor:
            # Overlap on same track — skip to avoid invalid OTIO (readers require monotonic).
            continue
        children.append(clip_to_otio_dict(record, fps))
        tl_dur = max(0, record.end_frame - record.start_frame)
        cursor += tl_dur if tl_dur > 0 else record.duration_frames
    return children


def build_otio_payload(sequence_el: ET.Element) -> Dict[str, object]:
    fps = extract_sequence_fps(sequence_el)
    width, height = extract_sequence_dimensions(sequence_el)
    sequence_name = sequence_el.findtext("name", "Timeline")
    stack_children: List[Dict[str, object]] = []
    clips_by_type = {
        "video": iter_media_clips(sequence_el, "video"),
        "audio": iter_media_clips(sequence_el, "audio"),
    }
    for media_type, records in clips_by_type.items():
        track_records: Dict[int, List[ClipRecord]] = {}
        for record in records:
            track_records.setdefault(record.track_index, []).append(record)
        for track_index in sorted(track_records.keys()):
            stack_children.append(
                {
                    "OTIO_SCHEMA": "Track.1",
                    "kind": "Video" if media_type == "video" else "Audio",
                    "name": f"{media_type.capitalize()} {track_index}",
                    "metadata": {"track_type": media_type, "track_index": track_index},
                    "source_range": None,
                    "effects": [],
                    "markers": [],
                    "enabled": True,
                    "children": build_track_children(track_records[track_index], fps),
                }
            )
    tracks_payload = []
    tracks_payload.extend(stack_children)
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": sequence_name,
        "metadata": {
            "source": "premiere_xml",
            "sequence": {
                "fps": fps,
                "width": width,
                "height": height,
            },
        },
        "global_start_time": rational_time(0, fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "metadata": {},
            "name": "tracks",
            "children": tracks_payload,
            "effects": [],
            "markers": [],
            "enabled": True,
            "source_range": None,
        },
    }


def load_sequence_from_xml(xml_path: Path) -> ET.Element:
    tree = ET.parse(xml_path)
    sequence_el = tree.getroot().find("sequence")
    if sequence_el is None:
        raise RuntimeError(f"{xml_path} has no <sequence> element.")
    return sequence_el


def load_xml_track_records(
    xml_path: Path,
) -> tuple[int, tuple[int, int], Dict[int, List[ClipRecord]], Dict[int, List[ClipRecord]]]:
    sequence_el = load_sequence_from_xml(xml_path)
    fps = extract_sequence_fps(sequence_el)
    dimensions = extract_sequence_dimensions(sequence_el)
    track_maps: Dict[str, Dict[int, List[ClipRecord]]] = {"video": {}, "audio": {}}
    for media_type in ("video", "audio"):
        for record in iter_media_clips(sequence_el, media_type):
            track_maps[media_type].setdefault(record.track_index, []).append(record)
    return fps, dimensions, track_maps["video"], track_maps["audio"]


def find_xml_in_input() -> Optional[Path]:
    xml_files = sorted(INPUT_DIR.glob("*.xml"))
    return xml_files[0] if xml_files else None


def find_latest_pipeline_xml() -> Optional[Path]:
    for directory in (FINAL_XML_DIR, FALLBACK_XML_DIR):
        candidates = sorted(
            directory.glob("*.xml"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def convert_xml_to_otio(xml_path: Path, destination_dir: Optional[Path] = None) -> Path:
    if not xml_path.exists():
        raise FileNotFoundError(xml_path)
    destination_dir = destination_dir or OUTPUT_DIR
    destination_dir.mkdir(parents=True, exist_ok=True)
    sequence_el = load_sequence_from_xml(xml_path)
    video_clips = iter_media_clips(sequence_el, "video")
    if not video_clips:
        raise RuntimeError(f"{xml_path} does not contain any video clips.")
    payload = build_otio_payload(sequence_el)
    otio_text = json.dumps(payload, indent=2)
    output_path = destination_dir / f"{xml_path.stem}.otio"
    output_path.write_text(otio_text, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert Premiere XML to OTIO.")
    parser.add_argument("--xml", type=Path, help="Path to the Premiere XML file.")
    parser.add_argument("--output-dir", type=Path, help="Directory where the .otio file will be written.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    xml_path = args.xml
    if not xml_path:
        xml_path = find_xml_in_input()
    if not xml_path:
        xml_path = find_latest_pipeline_xml()
    if not xml_path:
        raise FileNotFoundError("No XML file found in /input or pipeline output.")
    otio_path = convert_xml_to_otio(xml_path, args.output_dir)
    print(f"Wrote {otio_path}")


if __name__ == "__main__":
    main()
