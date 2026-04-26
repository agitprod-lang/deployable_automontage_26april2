#!/usr/bin/env python3
"""
Convert the CSV emitted by Comparser into a Premiere-importable Final Cut Pro XML.

The script follows the structure of the previous new_xml_editor utility but lives in this
project so it can point directly at /Comparser/output by default.
"""

from __future__ import annotations

import argparse
import csv
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

# Default paths that match this project layout
DEFAULT_COMPARER_OUTPUT = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output")
DEFAULT_MEDIA_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/download/rush")
DEFAULT_PREMIERE_XML_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/premiere_automator/output/xml"
)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".mxf",
    ".m4v",
    ".mkv",
    ".avi",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".flv",
}

EXTRA_EMPTY_VIDEO_TRACKS = 10
EXTRA_EMPTY_AUDIO_TRACKS = 10


@dataclass
class Segment:
    """Timeline segment built from a keepable transcript row."""

    timeline_start: int
    timeline_end: int
    source_in: int
    source_out: int
    text: str
    clip_name: Optional[str] = None
    clip_file_id: Optional[str] = None
    file_element: Optional[ET.Element] = None
    uses_reference_media: bool = False

    @property
    def duration(self) -> int:
        return self.timeline_end - self.timeline_start


@dataclass
class ClipTiming:
    """In/out information extracted from a reference Premiere XML clipitem."""

    source_in: int
    source_out: int
    clip_name: Optional[str] = None
    file_id: Optional[str] = None
    file_element: Optional[ET.Element] = None

    @property
    def duration(self) -> int:
        return self.source_out - self.source_in


@dataclass
class KeepDecision:
    """Single CSV row decision about keeping or dropping a clip."""

    keep: bool
    start_frame: int
    start_tc: str
    csv_index: int


@dataclass
class ReferenceMediaInfo:
    """Metadata pulled from a reference Premiere XML sequence."""

    media_path: Optional[Path]
    source_base_frame: Optional[int]
    sequence_start_frame: Optional[int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a Premiere-compatible XML timeline from a Comparser CSV."
    )
    parser.add_argument(
        "--csv",
        dest="csv_path",
        help=f"CSV to convert (defaults to the latest CSV in {DEFAULT_COMPARER_OUTPUT}).",
    )
    parser.add_argument(
        "--media",
        dest="media_path",
        help=(
            "Source media used for the sequence. Defaults to the latest video found in "
            f"{DEFAULT_MEDIA_DIR}."
        ),
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        help=f"Destination XML (defaults to {DEFAULT_OUTPUT_DIR}/<csv_name>_premiere.xml).",
    )
    parser.add_argument(
        "--sequence-name",
        default=None,
        help="Custom sequence name (defaults to the CSV filename).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=25.0,
        help="Frame rate used by the CSV timecodes (integer frame rates only, default: 25).",
    )
    parser.add_argument(
        "--mode",
        choices=("copy", "rebuild"),
        default="copy",
        help=(
            "copy: duplicate the reference sequence and delete the rows marked with --delete-marker "
            "(default). rebuild: generate a fresh timeline from the CSV as in the original tool."
        ),
    )
    parser.add_argument(
        "--preserve-gaps",
        action="store_true",
        help=(
            "Keep the existing silent gaps between clips when rows are removed in copy mode. "
            "By default, the timeline is condensed so clips butt up against each other."
        ),
    )
    parser.add_argument(
        "--delete-marker",
        default="x",
        help="Value in the Keep column that marks a row for deletion (default: x).",
    )
    parser.add_argument(
        "--trim-start",
        type=int,
        default=0,
        help="Frames to trim from the start of each kept clip.",
    )
    parser.add_argument(
        "--trim-end",
        type=int,
        default=0,
        help="Frames to trim from the end of each kept clip.",
    )
    parser.add_argument(
        "--timecode-start",
        help="Explicit sequence start timecode (HH:MM:SS:FF). Defaults to earliest kept start.",
    )
    parser.add_argument(
        "--media-timecode-start",
        help="Explicit source media timecode (HH:MM:SS:FF). Defaults to earliest start in CSV.",
    )
    parser.add_argument(
        "--video-width",
        type=int,
        default=1920,
        help="Video width metadata to embed (default: 1920).",
    )
    parser.add_argument(
        "--video-height",
        type=int,
        default=1080,
        help="Video height metadata to embed (default: 1080).",
    )
    parser.add_argument(
        "--audio-channels",
        type=int,
        default=2,
        help="Audio channel count metadata (default: 2).",
    )
    parser.add_argument(
        "--audio-sample-rate",
        type=int,
        default=48000,
        help="Audio sample rate metadata (default: 48000).",
    )
    parser.add_argument(
        "--pixel-aspect",
        default="square",
        help="Pixel aspect ratio string (default: square).",
    )
    parser.add_argument(
        "--reference-xml",
        dest="reference_xml",
        help=(
            "Premiere XML exported from the existing edit. Defaults to the latest XML found in "
            f"{DEFAULT_PREMIERE_XML_DIR}. The clip in/out frames from this file are used instead "
            "of the CSV times, ensuring the regenerated timeline matches the manual edit."
        ),
    )
    parser.add_argument(
        "--reference-sequence",
        dest="reference_sequence",
        help=(
            "Name of the sequence to read from --reference-xml. Defaults to the first sequence "
            "found."
        ),
    )
    parser.add_argument(
        "--use-reference-inout",
        action="store_true",
        help=(
            "When set in rebuild mode, copy the clip in/out values from the reference XML "
            "instead of the CSV Start/End times."
        ),
    )
    return parser.parse_args()


def require_int_timebase(fps: float) -> int:
    timebase = round(fps)
    if abs(timebase - fps) > 1e-6:
        raise ValueError("Only integer frame rates are supported in FCP XML timebases.")
    return int(timebase)


def parse_timecode(timecode: str, fps: int) -> int:
    parts = timecode.strip().split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode format: {timecode}")
    hours, minutes, seconds, frames = map(int, parts)
    total_frames = ((hours * 3600) + (minutes * 60) + seconds) * fps + frames
    return total_frames


def frames_to_timecode(frames: int, fps: int) -> str:
    if frames < 0:
        frames = 0
    seconds, frame = divmod(frames, fps)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}:{frame:02d}"


def _latest_file(directory: Path, pattern: str) -> Optional[Path]:
    if not directory.exists():
        return None
    candidates = [path for path in directory.glob(pattern) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _latest_media_file(directory: Path) -> Optional[Path]:
    if not directory.exists():
        return None
    try:
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_segments(
    csv_path: Path,
    delete_marker: str,
    fps: int,
    trim_start: int,
    trim_end: int,
    reference_clips: Optional[List[ClipTiming]] = None,
    preserve_spacing: bool = False,
) -> Tuple[List[Segment], int, int, int]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            reader: Iterable[dict] = csv.DictReader(handle, dialect=dialect)
        except csv.Error:
            reader = csv.DictReader(handle)
        rows = list(reader)

    if not rows:
        raise ValueError("CSV did not contain any rows.")

    delete_marker_lower = delete_marker.strip().lower()
    segments: List[Segment] = []
    global_min_start: Optional[int] = None
    min_keep_start: Optional[int] = None
    max_end: Optional[int] = None
    timeline_cursor = 0
    reference_index = 0
    total_reference = len(reference_clips) if reference_clips else 0
    use_absolute_positions = preserve_spacing or bool(reference_clips)

    for row in rows:
        start_tc = (row.get("Start Time") or row.get("Start") or "").strip()
        end_tc = (row.get("End Time") or row.get("End") or "").strip()
        keep_marker = (row.get("Keep") or "").strip().lower()

        if not start_tc or not end_tc:
            continue

        start_frame = parse_timecode(start_tc, fps)
        end_frame = parse_timecode(end_tc, fps)
        trimmed_start_csv = start_frame + trim_start
        trimmed_end_csv = end_frame - trim_end

        if trimmed_end_csv <= trimmed_start_csv:
            continue

        if global_min_start is None or trimmed_start_csv < global_min_start:
            global_min_start = trimmed_start_csv

        if reference_clips:
            if reference_index >= total_reference:
                raise ValueError(
                    "Reference XML provides fewer clipitems than CSV rows with timecodes."
                )
            clip_ref = reference_clips[reference_index]
            reference_index += 1
            base_start = clip_ref.source_in
            base_end = clip_ref.source_out
            clip_name = clip_ref.clip_name
            clip_file_id = clip_ref.file_id
            clip_file_element = clip_ref.file_element
            uses_reference_media = True
        else:
            base_start = start_frame
            base_end = end_frame
            clip_name = None
            clip_file_id = None
            clip_file_element = None
            uses_reference_media = False

        trimmed_start = base_start + trim_start
        trimmed_end = base_end - trim_end

        should_drop = delete_marker_lower and keep_marker == delete_marker_lower
        if should_drop:
            continue

        if trimmed_end <= trimmed_start:
            continue

        csv_duration = trimmed_end_csv - trimmed_start_csv
        if csv_duration <= 0:
            continue

        if use_absolute_positions:
            base_offset = global_min_start if global_min_start is not None else trimmed_start
            timeline_start = trimmed_start_csv - base_offset
            timeline_end = timeline_start + csv_duration
        else:
            timeline_start = timeline_cursor
            timeline_end = timeline_cursor + csv_duration
            timeline_cursor = timeline_end

        text = (row.get("Text") or "").strip()

        segment = Segment(
            timeline_start=timeline_start,
            timeline_end=timeline_end,
            source_in=trimmed_start,
            source_out=trimmed_end,
            text=text,
            clip_name=clip_name,
            clip_file_id=clip_file_id,
            file_element=clip_file_element,
            uses_reference_media=uses_reference_media,
        )
        segments.append(segment)

        if min_keep_start is None or trimmed_start < min_keep_start:
            min_keep_start = trimmed_start
        if max_end is None or trimmed_end > max_end:
            max_end = trimmed_end

    if not segments:
        raise ValueError("No keepable segments found in the CSV.")

    assert global_min_start is not None
    assert min_keep_start is not None
    assert max_end is not None

    return segments, global_min_start, min_keep_start, max_end


def read_keep_flags(csv_path: Path, delete_marker: str, fps: int) -> List[KeepDecision]:
    with open(csv_path, newline="", encoding="utf-8") as handle:
        sample = handle.read(2048)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            reader = csv.DictReader(handle, dialect=dialect)
        except csv.Error:
            reader = csv.DictReader(handle)
        rows = list(reader)

    if not rows:
        raise ValueError("CSV did not contain any rows.")

    keep_column: Optional[str] = None
    for field in reader.fieldnames or []:
        if field and field.strip().lower() == "keep":
            keep_column = field
            break

    delete_marker_lower = delete_marker.strip().lower()
    keep_decisions: List[KeepDecision] = []
    valid_row_index = 0
    for row in rows:
        start_tc = (row.get("Start Time") or row.get("Start") or "").strip()
        end_tc = (row.get("End Time") or row.get("End") or "").strip()
        if not start_tc or not end_tc:
            continue

        start_frame = parse_timecode(start_tc, fps)
        end_frame = parse_timecode(end_tc, fps)
        if end_frame <= start_frame:
            continue

        valid_row_index += 1
        keep_value = (row.get(keep_column) if keep_column else None) or ""
        keep_marker = keep_value.strip().lower()
        should_drop = bool(delete_marker_lower) and keep_marker == delete_marker_lower
        decision = KeepDecision(
            keep=not should_drop,
            start_frame=start_frame,
            start_tc=start_tc,
            csv_index=valid_row_index,
        )
        keep_decisions.append(decision)

    if not keep_decisions:
        raise ValueError("No usable Start/End rows were found in the CSV.")

    return keep_decisions


def _find_sequence_element(
    root: ET.Element, reference_xml: Path, sequence_name: Optional[str]
) -> ET.Element:
    sequences = root.findall(".//sequence")
    if not sequences:
        raise ValueError(f"No <sequence> found inside {reference_xml}.")

    if sequence_name:
        for candidate in sequences:
            name_el = candidate.find("name")
            if name_el is not None and name_el.text == sequence_name:
                return candidate
        raise ValueError(f"Sequence named '{sequence_name}' not found in {reference_xml}.")

    return sequences[0]


def load_reference_sequence(
    reference_xml: Path, sequence_name: Optional[str]
) -> Tuple[ET.ElementTree, ET.Element]:
    tree = ET.parse(reference_xml)
    root = tree.getroot()
    sequence_el = _find_sequence_element(root, reference_xml, sequence_name)
    return tree, sequence_el


def _path_from_pathurl(pathurl: str) -> Optional[Path]:
    parsed = urlparse(pathurl)
    if parsed.scheme and parsed.scheme != "file":
        return None
    decoded = unquote(parsed.path or "")
    if parsed.netloc and parsed.netloc not in ("", "localhost"):
        decoded = "/" + parsed.netloc + decoded
    if not decoded:
        return None
    return Path(decoded)


def extract_reference_media_info(sequence_el: ET.Element) -> ReferenceMediaInfo:
    media_path: Optional[Path] = None
    source_base_frame: Optional[int] = None

    file_el = sequence_el.find(".//clipitem/file")
    if file_el is not None:
        pathurl = file_el.findtext("pathurl")
        if pathurl:
            media_path = _path_from_pathurl(pathurl)
        tc_frame_text = file_el.findtext("./timecode/frame")
        if tc_frame_text:
            try:
                source_base_frame = int(tc_frame_text)
            except ValueError:
                source_base_frame = None

    seq_start_frame: Optional[int] = None
    seq_tc_frame = sequence_el.findtext("./timecode/frame")
    if seq_tc_frame:
        try:
            seq_start_frame = int(seq_tc_frame)
        except ValueError:
            seq_start_frame = None

    return ReferenceMediaInfo(
        media_path=media_path,
        source_base_frame=source_base_frame,
        sequence_start_frame=seq_start_frame,
    )


def remove_marked_clipitems(
    sequence_el: ET.Element,
    keep_decisions: List[KeepDecision],
    match_tolerance: int = 5,
) -> Tuple[int, int, List[KeepDecision], List[Tuple[Optional[int], str]]]:
    video_track = sequence_el.find("./media/video/track")
    if video_track is None:
        raise ValueError(
            f"Sequence '{sequence_el.findtext('name') or 'unknown'}' does not have a video track."
        )
    clipitems = list(video_track.findall("clipitem"))

    clip_pairs: List[Tuple[ET.Element, Optional[int]]] = []
    for clip in clipitems:
        start_text = clip.findtext("start")
        try:
            start_val = int(start_text) if start_text is not None else None
        except ValueError:
            start_val = None
        clip_pairs.append((clip, start_val))

    matched_pairs: List[Tuple[ET.Element, KeepDecision]] = []
    unmatched_decisions: List[KeepDecision] = []
    unmatched_clips: List[Tuple[Optional[int], str]] = []

    clip_index = 0
    decision_index = 0
    while clip_index < len(clip_pairs) and decision_index < len(keep_decisions):
        clip, clip_start = clip_pairs[clip_index]
        decision = keep_decisions[decision_index]
        if clip_start is None:
            clip_name = clip.findtext("name") or clip.get("id") or "unknown"
            unmatched_clips.append((None, clip_name))
            clip_index += 1
            continue

        diff = clip_start - decision.start_frame
        if abs(diff) <= match_tolerance:
            matched_pairs.append((clip, decision))
            clip_index += 1
            decision_index += 1
            continue

        if decision.start_frame < clip_start - match_tolerance:
            unmatched_decisions.append(decision)
            decision_index += 1
        else:
            clip_name = clip.findtext("name") or clip.get("id") or "unknown"
            unmatched_clips.append((clip_start, clip_name))
            clip_index += 1

    while decision_index < len(keep_decisions):
        unmatched_decisions.append(keep_decisions[decision_index])
        decision_index += 1

    while clip_index < len(clip_pairs):
        clip, clip_start = clip_pairs[clip_index]
        clip_name = clip.findtext("name") or clip.get("id") or "unknown"
        unmatched_clips.append((clip_start, clip_name))
        clip_index += 1

    ids_to_remove: set[str] = set()
    removed_video = 0
    for clip, decision in matched_pairs:
        if decision.keep:
            continue
        clip_id = clip.get("id")
        if clip_id:
            ids_to_remove.add(clip_id)
        for link in clip.findall("link"):
            ref_id = link.findtext("linkclipref")
            if ref_id:
                ids_to_remove.add(ref_id)
        video_track.remove(clip)
        removed_video += 1

    removed_audio = 0
    audio_section = sequence_el.find("./media/audio")
    if audio_section is not None and ids_to_remove:
        for track in audio_section.findall("track"):
            clip_children = list(track.findall("clipitem"))
            for clip in clip_children:
                clip_id = clip.get("id")
                if clip_id and clip_id in ids_to_remove:
                    track.remove(clip)
                    removed_audio += 1

    return removed_video, removed_audio, unmatched_decisions, unmatched_clips


def renumber_clip_indexes(sequence_el: ET.Element) -> None:
    id_to_index: dict[str, int] = {}

    video_tracks = sequence_el.findall("./media/video/track")
    for track in video_tracks:
        for index, clip in enumerate(track.findall("clipitem"), start=1):
            clip_id = clip.get("id")
            if clip_id:
                id_to_index[clip_id] = index

    audio_tracks = sequence_el.findall("./media/audio/track")
    for track in audio_tracks:
        for index, clip in enumerate(track.findall("clipitem"), start=1):
            clip_id = clip.get("id")
            if clip_id:
                id_to_index[clip_id] = index

    for link in sequence_el.findall(".//link"):
        link_ref = link.findtext("linkclipref")
        if not link_ref:
            continue
        new_index = id_to_index.get(link_ref)
        if new_index is None:
            continue
        clip_index_el = link.find("clipindex")
        if clip_index_el is None:
            clip_index_el = ET.SubElement(link, "clipindex")
        clip_index_el.text = str(new_index)


def _close_track_gaps(track: ET.Element) -> int:
    cursor = 0
    for clip in track.findall("clipitem"):
        start_text = clip.findtext("start")
        end_text = clip.findtext("end")
        if start_text is None or end_text is None:
            continue
        try:
            start_val = int(start_text)
            end_val = int(end_text)
        except ValueError:
            continue
        duration = max(0, end_val - start_val)
        clip.find("start").text = str(cursor)
        clip.find("end").text = str(cursor + duration)
        cursor += duration
    return cursor


def close_sequence_gaps(sequence_el: ET.Element) -> None:
    max_duration = 0
    video_tracks = sequence_el.findall("./media/video/track")
    for track in video_tracks:
        total = _close_track_gaps(track)
        max_duration = max(max_duration, total)
    audio_tracks = sequence_el.findall("./media/audio/track")
    for track in audio_tracks:
        _close_track_gaps(track)
    if max_duration > 0:
        duration_el = sequence_el.find("duration")
        if duration_el is not None:
            duration_el.text = str(max_duration)


def collect_file_definitions(sequence_el: ET.Element) -> dict[str, ET.Element]:
    file_defs: dict[str, ET.Element] = {}
    for file_el in sequence_el.findall(".//clipitem/file"):
        file_id = file_el.get("id")
        if not file_id:
            continue
        if len(list(file_el)):
            file_defs[file_id] = deepcopy(file_el)
    return file_defs


def restore_missing_file_definitions(
    sequence_el: ET.Element, file_defs: dict[str, ET.Element]
) -> None:
    if not file_defs:
        return

    files_with_definition = set()
    first_clips: dict[str, ET.Element] = {}

    for clip in sequence_el.findall(".//clipitem"):
        file_el = clip.find("file")
        if file_el is None:
            continue
        file_id = file_el.get("id")
        if not file_id:
            continue
        if len(list(file_el)):
            files_with_definition.add(file_id)
            continue
        first_clips.setdefault(file_id, clip)

    missing_defs = file_defs.keys() - files_with_definition
    for file_id in missing_defs:
        template = file_defs.get(file_id)
        clip_el = first_clips.get(file_id)
        if template is None or clip_el is None:
            continue
        clip_file = clip_el.find("file")
        if clip_file is None:
            clip_file = ET.SubElement(clip_el, "file")
        clip_file.clear()
        clip_file.attrib.update(template.attrib)
        for child in template:
            clip_file.append(deepcopy(child))


def _configure_video_track(track: ET.Element) -> None:
    ET.SubElement(track, "enabled").text = "TRUE"
    ET.SubElement(track, "locked").text = "FALSE"


def _configure_audio_track(track: ET.Element, channel_index: int) -> None:
    ET.SubElement(track, "enabled").text = "TRUE"
    ET.SubElement(track, "locked").text = "FALSE"
    ET.SubElement(track, "outputchannelindex").text = str(channel_index)


def build_sequence_xml(
    sequence_name: str,
    media_path: Path,
    segments: List[Segment],
    timebase: int,
    source_base_frame: int,
    sequence_start_frame: int,
    video_width: int,
    video_height: int,
    audio_channels: int,
    audio_sample_rate: int,
    pixel_aspect: str,
) -> ET.Element:
    path_url = media_path.resolve().as_uri()
    media_name = media_path.stem

    total_timeline_duration = max(segment.timeline_end for segment in segments)
    max_source_out = max(segment.source_out for segment in segments)
    source_total_duration = max_source_out if max_source_out > 0 else total_timeline_duration

    root = ET.Element("xmeml", version="5")
    sequence = ET.SubElement(root, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = sequence_name

    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = "FALSE"

    ET.SubElement(sequence, "duration").text = str(total_timeline_duration)
    ET.SubElement(sequence, "in").text = "-1"
    ET.SubElement(sequence, "out").text = "-1"

    timecode = ET.SubElement(sequence, "timecode")
    ET.SubElement(timecode, "string").text = frames_to_timecode(sequence_start_frame, timebase)
    ET.SubElement(timecode, "frame").text = str(sequence_start_frame)
    ET.SubElement(timecode, "displayformat").text = "NDF"
    tc_rate = ET.SubElement(timecode, "rate")
    ET.SubElement(tc_rate, "timebase").text = str(timebase)
    ET.SubElement(tc_rate, "ntsc").text = "FALSE"

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    video_format = ET.SubElement(video, "format")
    video_sample = ET.SubElement(video_format, "samplecharacteristics")
    ET.SubElement(video_sample, "width").text = str(video_width)
    ET.SubElement(video_sample, "height").text = str(video_height)
    ET.SubElement(video_sample, "anamorphic").text = "FALSE"
    ET.SubElement(video_sample, "pixelaspectratio").text = pixel_aspect
    video_rate = ET.SubElement(video_format, "rate")
    ET.SubElement(video_rate, "timebase").text = str(timebase)
    ET.SubElement(video_rate, "ntsc").text = "FALSE"
    audio = ET.SubElement(media, "audio")
    audio_format = ET.SubElement(audio, "format")
    audio_sample = ET.SubElement(audio_format, "samplecharacteristics")
    ET.SubElement(audio_sample, "samplerate").text = str(audio_sample_rate)
    ET.SubElement(audio_sample, "depth").text = "16"
    ET.SubElement(audio_sample, "channelcount").text = str(audio_channels)

    # Primary video track
    video_track = ET.SubElement(video, "track")
    _configure_video_track(video_track)

    # Additional empty video tracks for Premiere convenience
    for _ in range(EXTRA_EMPTY_VIDEO_TRACKS):
        empty_track = ET.SubElement(video, "track")
        _configure_video_track(empty_track)

    # Primary audio track
    audio_track = ET.SubElement(audio, "track")
    _configure_audio_track(audio_track, 1)

    # Additional empty audio tracks
    for channel_index in range(2, EXTRA_EMPTY_AUDIO_TRACKS + 2):
        empty_audio_track = ET.SubElement(audio, "track")
        _configure_audio_track(empty_audio_track, channel_index)

    default_file_id = "source-file-1"
    default_file_defined = False
    reference_file_definitions: set[str] = set()

    for index, segment in enumerate(segments, start=1):
        clip_id_video = f"clipitem-video-{index}"
        clip_id_audio = f"clipitem-audio-{index}"
        source_in = max(segment.source_in, 0)
        source_out = max(segment.source_out, 0)
        clip_duration = segment.duration

        clip = ET.SubElement(video_track, "clipitem", id=clip_id_video)
        clip_display_name = segment.clip_name or media_name
        ET.SubElement(clip, "name").text = clip_display_name
        ET.SubElement(clip, "enabled").text = "TRUE"
        ET.SubElement(clip, "duration").text = str(clip_duration)

        clip_rate = ET.SubElement(clip, "rate")
        ET.SubElement(clip_rate, "timebase").text = str(timebase)
        ET.SubElement(clip_rate, "ntsc").text = "FALSE"

        ET.SubElement(clip, "start").text = str(segment.timeline_start)
        ET.SubElement(clip, "end").text = str(segment.timeline_end)
        ET.SubElement(clip, "in").text = str(source_in)
        ET.SubElement(clip, "out").text = str(source_out)
        ET.SubElement(clip, "alphatype").text = "none"
        ET.SubElement(clip, "pixelaspectratio").text = pixel_aspect
        ET.SubElement(clip, "anamorphic").text = "FALSE"

        clip_file_id: Optional[str] = segment.clip_file_id
        if segment.uses_reference_media and segment.file_element is not None:
            if not clip_file_id:
                clip_file_id = segment.file_element.get("id") or f"reference-file-{index}"
            if clip_file_id not in reference_file_definitions:
                file_el = deepcopy(segment.file_element)
                file_el.set("id", clip_file_id)
                clip.append(file_el)
                reference_file_definitions.add(clip_file_id)
            else:
                ET.SubElement(clip, "file", id=clip_file_id)
        elif not segment.uses_reference_media:
            clip_file_id = default_file_id
            file_el = ET.SubElement(clip, "file", id=clip_file_id)
            if not default_file_defined:
                ET.SubElement(file_el, "duration").text = str(source_total_duration)
                file_rate = ET.SubElement(file_el, "rate")
                ET.SubElement(file_rate, "timebase").text = str(timebase)
                ET.SubElement(file_rate, "ntsc").text = "FALSE"
                ET.SubElement(file_el, "name").text = media_name
                ET.SubElement(file_el, "pathurl").text = path_url
                file_tc = ET.SubElement(file_el, "timecode")
                ET.SubElement(file_tc, "string").text = frames_to_timecode(source_base_frame, timebase)
                ET.SubElement(file_tc, "frame").text = str(source_base_frame)
                file_tc_rate = ET.SubElement(file_tc, "rate")
                ET.SubElement(file_tc_rate, "timebase").text = str(timebase)
                ET.SubElement(file_tc_rate, "ntsc").text = "FALSE"
                media_file = ET.SubElement(file_el, "media")
                video_media = ET.SubElement(media_file, "video")
                ET.SubElement(video_media, "duration").text = str(source_total_duration)
                sample = ET.SubElement(video_media, "samplecharacteristics")
                ET.SubElement(sample, "width").text = str(video_width)
                ET.SubElement(sample, "height").text = str(video_height)
                ET.SubElement(sample, "anamorphic").text = "FALSE"
                ET.SubElement(sample, "pixelaspectratio").text = pixel_aspect
                audio_media = ET.SubElement(media_file, "audio")
                audio_sample = ET.SubElement(audio_media, "samplecharacteristics")
                ET.SubElement(audio_sample, "samplerate").text = str(audio_sample_rate)
                ET.SubElement(audio_sample, "depth").text = "16"
                ET.SubElement(audio_sample, "channelcount").text = str(audio_channels)
                default_file_defined = True
        elif clip_file_id:
            ET.SubElement(clip, "file", id=clip_file_id)
        else:
            # Fallback: treat as default media when reference metadata is missing.
            clip_file_id = default_file_id
            ET.SubElement(clip, "file", id=clip_file_id)

        ET.SubElement(clip, "compositemode").text = "normal"
        source_track = ET.SubElement(clip, "sourcetrack")
        ET.SubElement(source_track, "mediatype").text = "video"
        ET.SubElement(source_track, "trackindex").text = "1"
        link_self = ET.SubElement(clip, "link")
        ET.SubElement(link_self, "linkclipref").text = clip_id_video

        audio_clip = ET.SubElement(audio_track, "clipitem", id=clip_id_audio)
        ET.SubElement(audio_clip, "name").text = clip_display_name
        ET.SubElement(audio_clip, "enabled").text = "TRUE"
        ET.SubElement(audio_clip, "duration").text = str(clip_duration)

        audio_rate = ET.SubElement(audio_clip, "rate")
        ET.SubElement(audio_rate, "timebase").text = str(timebase)
        ET.SubElement(audio_rate, "ntsc").text = "FALSE"

        ET.SubElement(audio_clip, "start").text = str(segment.timeline_start)
        ET.SubElement(audio_clip, "end").text = str(segment.timeline_end)
        ET.SubElement(audio_clip, "in").text = str(source_in)
        ET.SubElement(audio_clip, "out").text = str(source_out)

        if clip_file_id:
            ET.SubElement(audio_clip, "file", id=clip_file_id)
        else:
            ET.SubElement(audio_clip, "file", id=default_file_id)

        ET.SubElement(audio_clip, "compositemode").text = "normal"
        audio_source_track = ET.SubElement(audio_clip, "sourcetrack")
        ET.SubElement(audio_source_track, "mediatype").text = "audio"
        ET.SubElement(audio_source_track, "trackindex").text = "1"
        audio_link_self = ET.SubElement(audio_clip, "link")
        ET.SubElement(audio_link_self, "linkclipref").text = clip_id_audio
        audio_link_video = ET.SubElement(audio_clip, "link")
        ET.SubElement(audio_link_video, "linkclipref").text = clip_id_video

    return root


def _indent_xml(elem: ET.Element, level: int = 0) -> None:
    indent_str = "\n" + ("  " * level)
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent_str + "  "
        for child in elem:
            _indent_xml(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = indent_str + "  "
        if not child.tail or not child.tail.strip():
            child.tail = indent_str
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent_str


def write_xml(root: ET.Element, destination: Path) -> None:
    _indent_xml(root)
    xml_bytes = ET.tostring(root, encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write(b"<!DOCTYPE xmeml>\n")
        handle.write(xml_bytes)


def main() -> None:
    args = parse_args()
    timebase = require_int_timebase(args.fps)

    if args.csv_path:
        csv_path = Path(args.csv_path).expanduser()
    else:
        csv_candidate = _latest_file(DEFAULT_COMPARER_OUTPUT, "*.csv")
        if csv_candidate is None:
            raise FileNotFoundError(
                f"No CSV provided and none found in {DEFAULT_COMPARER_OUTPUT}. "
                "Pass --csv explicitly."
            )
        csv_path = csv_candidate
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    if args.reference_xml:
        reference_path = Path(args.reference_xml).expanduser()
    else:
        reference_candidate = _latest_file(DEFAULT_PREMIERE_XML_DIR, "*.xml")
        if reference_candidate is None:
            raise FileNotFoundError(
                f"No reference XML provided and none found in {DEFAULT_PREMIERE_XML_DIR}. "
                "Pass --reference-xml explicitly."
            )
        reference_path = reference_candidate
    if not reference_path.is_file():
        raise FileNotFoundError(f"Reference XML not found: {reference_path}")

    reference_tree, reference_sequence = load_reference_sequence(reference_path, args.reference_sequence)
    reference_info = extract_reference_media_info(reference_sequence)

    if args.output_path:
        output_path = Path(args.output_path).expanduser()
    else:
        DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = DEFAULT_OUTPUT_DIR / f"{csv_path.stem}_premiere.xml"

    if args.mode == "copy":
        keep_decisions = read_keep_flags(
            csv_path=csv_path,
            delete_marker=args.delete_marker,
            fps=timebase,
        )
        drop_count = sum(1 for decision in keep_decisions if not decision.keep)

        sequence_name = args.sequence_name or reference_sequence.findtext("name") or csv_path.stem

        segments, raw_start, min_keep_start, _ = read_segments(
            csv_path=csv_path,
            delete_marker=args.delete_marker,
            fps=timebase,
            trim_start=max(0, args.trim_start),
            trim_end=max(0, args.trim_end),
            reference_clips=None,
            preserve_spacing=args.preserve_gaps,
        )

        if args.media_path:
            media_path = Path(args.media_path).expanduser()
        elif reference_info.media_path is not None:
            media_path = reference_info.media_path
        else:
            media_candidate = _latest_media_file(DEFAULT_MEDIA_DIR)
            if media_candidate is None:
                raise FileNotFoundError(
                    "Could not determine media file automatically. Pass --media with your rush."
                )
            media_path = media_candidate
        if not media_path.is_file():
            raise FileNotFoundError(f"Media file not found: {media_path}")

        if args.media_timecode_start:
            source_base_frame = parse_timecode(args.media_timecode_start, timebase)
        else:
            source_base_frame = reference_info.source_base_frame or raw_start

        if args.timecode_start:
            sequence_start_frame = parse_timecode(args.timecode_start, timebase)
        else:
            sequence_start_frame = reference_info.sequence_start_frame or min_keep_start

        root = build_sequence_xml(
            sequence_name=sequence_name,
            media_path=media_path,
            segments=segments,
            timebase=timebase,
            source_base_frame=source_base_frame,
            sequence_start_frame=sequence_start_frame,
            video_width=args.video_width,
            video_height=args.video_height,
            audio_channels=args.audio_channels,
            audio_sample_rate=args.audio_sample_rate,
            pixel_aspect=args.pixel_aspect,
        )

        sequence_copy = root.find(".//sequence")
        if sequence_copy is not None:
            renumber_clip_indexes(sequence_copy)

        write_xml(root, output_path)
        print(
            f"Generated a fresh sequence from the CSV and removed {drop_count} segment(s). "
            f"XML written to {output_path}"
        )
        return

    reference_clips: Optional[List[ClipTiming]] = None
    if args.use_reference_inout:
        reference_clips = read_reference_clips(reference_path, args.reference_sequence)

    if args.media_path:
        media_path = Path(args.media_path).expanduser()
    elif reference_info.media_path is not None:
        media_path = reference_info.media_path
    else:
        media_candidate = _latest_media_file(DEFAULT_MEDIA_DIR)
        if media_candidate is None:
            raise FileNotFoundError(
                "Could not determine media file automatically. Pass --media with your rush."
            )
        media_path = media_candidate
    if not media_path.is_file():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    segments, raw_start, min_keep_start, _ = read_segments(
        csv_path=csv_path,
        delete_marker=args.delete_marker,
        fps=timebase,
        trim_start=max(0, args.trim_start),
        trim_end=max(0, args.trim_end),
        reference_clips=reference_clips,
    )

    if args.media_timecode_start:
        source_base_frame = parse_timecode(args.media_timecode_start, timebase)
    else:
        source_base_frame = reference_info.source_base_frame or raw_start

    if args.timecode_start:
        sequence_start_frame = parse_timecode(args.timecode_start, timebase)
    else:
        sequence_start_frame = reference_info.sequence_start_frame or min_keep_start

    sequence_name = args.sequence_name or csv_path.stem

    root = build_sequence_xml(
        sequence_name=sequence_name,
        media_path=media_path,
        segments=segments,
        timebase=timebase,
        source_base_frame=source_base_frame,
        sequence_start_frame=sequence_start_frame,
        video_width=args.video_width,
        video_height=args.video_height,
        audio_channels=args.audio_channels,
        audio_sample_rate=args.audio_sample_rate,
        pixel_aspect=args.pixel_aspect,
    )
    sequence_el = root.find(".//sequence")
    if sequence_el is not None:
        renumber_clip_indexes(sequence_el)

    write_xml(root, output_path)
    print(f"XML written to {output_path}")


def read_reference_clips(reference_xml: Path, sequence_name: Optional[str]) -> List[ClipTiming]:
    tree, sequence_el = load_reference_sequence(reference_xml, sequence_name)
    root = tree.getroot()

    file_definitions: dict[str, ET.Element] = {}
    for file_el in root.findall(".//file"):
        file_id = file_el.get("id")
        if not file_id:
            continue
        if not list(file_el):
            continue
        file_definitions.setdefault(file_id, deepcopy(file_el))

    video_track = sequence_el.find("./media/video/track")
    if video_track is None:
        raise ValueError(
            f"Sequence '{sequence_el.findtext('name') or 'unknown'}' does not have a video track."
        )

    clipitems = video_track.findall("clipitem")
    if not clipitems:
        raise ValueError(
            f"Reference sequence '{sequence_el.findtext('name') or 'unknown'}' does not contain clipitems."
        )

    reference: List[ClipTiming] = []
    for clip in clipitems:
        in_text = clip.findtext("in")
        out_text = clip.findtext("out")
        if in_text is None or out_text is None:
            raise ValueError("Encountered a clipitem without <in>/<out> tags.")
        try:
            source_in = int(in_text)
            source_out = int(out_text)
        except ValueError as exc:
            raise ValueError("Clipitem in/out values are not integers.") from exc
        file_el = clip.find("file")
        clip_file_id = file_el.get("id") if file_el is not None else None
        file_definition = None
        if clip_file_id and clip_file_id in file_definitions:
            file_definition = deepcopy(file_definitions[clip_file_id])
        elif file_el is not None and list(file_el):
            file_definition = deepcopy(file_el)
        clip_name = clip.findtext("name")
        reference.append(
            ClipTiming(
                source_in=source_in,
                source_out=source_out,
                clip_name=clip_name,
                file_id=clip_file_id,
                file_element=file_definition,
            )
        )
    return reference


if __name__ == "__main__":
    main()
