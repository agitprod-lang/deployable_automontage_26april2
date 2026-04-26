#!/usr/bin/env python3
"""
Variant of program4 that clones a clap_editor XML, adds the insert clips, and always keeps
rush clips on V1/A1 while extracts occupy dedicated overlay tracks. Each extract is
inserted after the preceding rush clip without trimming it, so the sequence length grows to
accommodate every extract. Other behavior (timestamp parsing, overlays, image handling) stays
identical to program4.

Run with:
python3.11 /Users/mathieusandana/Desktop/code/deployable_auto-montage/xml_insertor/program/program6.py
"""

from __future__ import annotations

import argparse
import csv
import copy
import json
import math
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

PAGE_TURN_SOUND_PATH = Path("/Users/mathieusandana/Desktop/pdh/music et son pdh/page_turn.mp3")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OTIO_XML_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/clap_editor/output"
)
COMPARISON_OUTPUT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output"
)
INSERT_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Insert")
RUSH_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush")
UNIVERSAL_RUSH_DIR = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Rush")
INSERT_TIMING_SIDECAR_DIRNAME = ".insert_timing"
LEGACY_TIMESTAMP_PATTERN = re.compile(r"(\d+)m(\d{2})", re.IGNORECASE)
PRECISE_TIMESTAMP_PATTERN = re.compile(r"(\d{2})h(\d{2})m(\d{2})s(\d{3})ms", re.IGNORECASE)
CUT_RANGE_PATTERN = re.compile(
    r"(?P<start_min>\d+)(?:[:/]|m)(?P<start_sec>\d{1,2})(?:\.(?P<start_millis>\d{1,3}))?"
    r"(?:-(?P<end_min>\d+)(?:[:/]|m)(?P<end_sec>\d{1,2})(?:\.(?P<end_millis>\d{1,3}))?)?\s*$"
)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv", ".mpg", ".webm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aiff", ".aif", ".m4a", ".aac"}
VIDEO_TRACK_TARGET = 51
AUDIO_TRACK_TARGET = 50
SCALE_PERCENT = "200"
OVERLAY_DEFAULT_SCALE = 100.0
MAX_IMAGE_DURATION_SECONDS = 15
ZOOM_VIDEO_TRACK_INDEX = 2
ZOOM_SHIFT_VIDEO_TRACK_INDEX = 3  # INSERT_SHIFT sits above intro-zoom (V2)
ZOOM_SHIFT_MIN_TIMELINE_START_FRAMES = 81  # ~2 s 21 f at 30 fps; lets intro zoom play first
EXTRACT_VIDEO_TRACK_INDEX = 3
LOCATION_3D_VIDEO_TRACK_INDEX = 3
INSERT_VIDEO_TRACK_INDEX = 4
INSERT_AUDIO_TRACK_INDEX = 4
EXTRACT_AUDIO_TRACK_INDEX = 3
TRANSITION_VIDEO_TRACK_INDEX = 13
TRANSITION_AUDIO_TRACK_INDEX = 12
RUSH_VIDEO_TRACK_INDEX = 1
RUSH_AUDIO_TRACK_INDEX = 1
INTRO_AUDIO_TRACK_INDEX = 2
INTRO_VIDEO_OVERLAY_TRACK_INDEX = 2
INTRO_AUDIO_OVERLAY_TRACK_INDEX = 3
WOOSH_AUDIO_TRACK_INDEX = 3
AUDIO_OUTRO_TRACK_INDEX = 3
AUDIO_EFFECT_TRACK_INDEX = 2
MUTED_AUDIO_LEVEL = "-9600"  # Premiere expects hundredth dB units (~ -96 dB)
WOOSH_LEVEL = "-500"  # -5 dB
GAIN_REDUCTION_DB_UNITS = -800  # Premiere stores dB in hundredth-dB units.
TITLE_WIDTH_RATIO = 0.9
SPLIT_SCREEN_OFFSET_RATIO = 0.75
SPLIT_OVERLAY_SCALE = "200"
MAX_SCALE_VALUE = 1000.0
OUTRO_OFFSET_SECONDS = 2.0
OUTRO_DELAY_SECONDS = 2.0
WOOSH_EFFECT_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/insert_creator/asset/papersound.mp3"
)
INTRO_TAG_VIDEO_PATH = Path("/Users/mathieusandana/Desktop/AR/Génériques/Intro.mov")
COMMENT_TAG_VIDEO_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/CTA/comment.mov"
)
SUBSCRIBE_TAG_VIDEO_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/CTA/sabonner.mov"
)
TIPPEE_TAG_VIDEO_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/CTA/money_give.mov"
)
INTRO_MUSIC_PATH = Path(
    "/Users/mathieusandana/Desktop/AR/Génériques/debut AR zook fade.mp3"
)
OUTRO_MUSIC_PATH = Path(
    "/Users/mathieusandana/Desktop/AR/Génériques/zook fin ar fade.mp3"
)
OUTRO_VIDEO_PATH = Path("/Users/mathieusandana/Desktop/AR/Génériques/Outro.mov")
IMAGE_TRACK_BASE = 5
DEFAULT_OVERLAY_TRACK_BASE = IMAGE_TRACK_BASE
TITLE_TRACK_BASE = 51
LOGO_TRACK_BASE = 7
NOUN_ARROW_TRACK_BASE = 30
QUOTE_HIGHLIGHT_TRACK_BASE = 20
LOGO_TARGET_WIDTH_RATIO = 0.6
LOGO_MIN_SCALE = 50.0
LOGO_MAX_SCALE = 160.0
OUTRO_VIDEO_TRACK_INDEX = 11
OVERLAY_ALIGNMENT_OFFSET_SECONDS = 0.04
DOWNLOADED_ARTICLE_TITLE_SCALE = 130.0
TITLE_BACKGROUND_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/format/background_for_title.mov"
)
TITLE_BACKGROUND_TRACK_BASE = 50
TITLE_ASSEMBLY_ADVANCE_FRAMES = 5
TITLE_TEXT_START_SECONDS = 1.0
TITLE_TEXT_START_EXTRA_FRAMES = 22
TITLE_TEXT_END_PADDING_SECONDS = 1.0
TITLE_TEXT_END_PADDING_EXTRA_FRAMES = 10
TITLE_PREVIOUS_RUSH_OVERLAP_SECONDS = 2.0
TITLE_PREVIOUS_RUSH_OVERLAP_EXTRA_FRAMES = 17
TITLE_NEXT_RUSH_OVERLAP_SECONDS = 1.0
TITLE_NEXT_RUSH_OVERLAP_EXTRA_FRAMES = 17
GENERATED_INSERT_LEFT_SHIFT_SECONDS = 1.0
# Default left-shift (seconds) by insert label tag. The tag captures the
# animation preroll baked into the swisser template (time between clip start
# and the moment the visible text lands). Negative values push the clip
# rightward (later) relative to the spoken-word anchor.
GENERATED_INSERT_LEFT_SHIFT_BY_TAG: Dict[str, float] = {
    "LST": 2.5,
    "BLD": 1.0,
    "CTA": 1.0,
    "SOC": 1.0,
    "DUR": 1.0,
    "URL": 1.0,
    "TWT": 1.0,
    "PLR": 1.0,
    "QH": 1.0,
}
GENERATED_INSERT_URL_SCREEN_SHIFT_SECONDS = 1.0
GENERATED_INSERT_POLAROID_SHIFT_SECONDS = 1.0
GENERATED_INSERT_TWEET_SHIFT_SECONDS = 1.0
INTRO_PREROLL_SECONDS = 1.0
INTRO_GAP_SECONDS = 3.0
SINGLE_TITLE_MOV_ASSET_MODE = "single_title_mov"
SCALE_HINTS = {
    "0m05_1_logo@archiveis_web_politique_greenland_reforme_gouvernementale_insert.mov": 54.1,
    "0m05_1_titre@archiveis_web_politique_greenland_reforme_gouvernementale_insert.mov": 123.1,
    "1m09_4_image@forbescom_web_titre_synthetise_:_greenland_milliardaires_insert.mov": 166.2,
    "1m09_4_titre@forbescom_web_titre_synthetise_:_greenland_milliardaires_insert.mov": 73.6,
    "1m09_4_logo@forbescom_web_titre_synthetise_:_greenland_milliardaires_insert.mov": 88.3,
    "1m20_5_image@translategoog_web_politique_greenland_acquisition_insert.mov": 145.7,
    "1m20_5_titre@translategoog_web_politique_greenland_acquisition_insert.mov": 60.6,
    "1m20_5_logo@translategoog_web_politique_greenland_acquisition_insert.mov": 116.1,
    "1m42_7_image@euronewscom_web_politique_ue_groenland_insert.mov": 124.3,
    "1m42_7_titre@euronewscom_web_politique_ue_groenland_insert.mov": 81.7,
    "1m42_7_logo@euronewscom_web_politique_ue_groenland_insert.mov": 90.5,
}
PROBE_CACHE: Dict[Path, Tuple[float, Optional[int], Optional[int], bool, bool]] = {}
PEAK_DB_CACHE: Dict[Path, Optional[float]] = {}
LAST_COMPARISON_CSV: Optional[Path] = None
RUSH_DIRECTORIES: Tuple[Path, ...] = (RUSH_DIR, UNIVERSAL_RUSH_DIR)
ZOOM_REPLACEMENT_ATTR = "codex_zoom_replacement_id"
CTA_TAG_SPECS: Tuple[Tuple[str, str, Path], ...] = (
    ("commentez tag", "comment", COMMENT_TAG_VIDEO_PATH),
    ("tippee tag", "tippee", TIPPEE_TAG_VIDEO_PATH),
    ("abonnez tag", "abonnez", SUBSCRIBE_TAG_VIDEO_PATH),
)
CTA_FILENAME_RE = re.compile(
    r"^(?P<timestamp>(?:\d{2}h\d{2}m\d{2}s\d{3}ms)|(?:\d+m\d{2}))_(?P<order>\d+?)_(?P<label>[^_]+)_001_CTA(?P<suffix>\.[^.]+)$",
    re.IGNORECASE,
)
INSERT_ORDER_RE = re.compile(
    r"^(?P<timestamp>(?:\d{2}h\d{2}m\d{2}s\d{3}ms)|(?:\d+m\d{2}))_(?P<order>\d+?)_",
    re.IGNORECASE,
)
RESERVED_OVERLAY_TRACK_INDICES = {
    INTRO_VIDEO_OVERLAY_TRACK_INDEX,
    TRANSITION_VIDEO_TRACK_INDEX,
}


@dataclass
class SequenceMetadata:
    fps: int
    width: int
    height: int
    pixel_aspect: str
    audio_sample_rate: int
    audio_channels: int
    timecode_frame: int
    timecode_string: str
    sequence_name: str


@dataclass
class InsertClip:
    path: Path
    start_frames: int
    source_in_frames: int
    duration_frames: int
    scale_value: str
    is_extract: bool
    is_image: bool
    treat_as_overlay: bool
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    label: Optional[str] = None
    motion_center: Optional[Tuple[float, float]] = None
    overlay_track_base: Optional[int] = None
    needs_rush_split: bool = False
    video_track_override: Optional[int] = None
    preserve_duration: bool = False
    audio_track_override: Optional[int] = None
    audio_override_path: Optional[Path] = None
    timeline_gap_anchor: Optional[int] = None
    timeline_gap_duration: int = 0
    mute_audio: bool = False
    audio_gain_level: Optional[str] = None
    intercalated_insert: bool = False
    insertion_block_id: Optional[str] = None
    insertion_block_anchor: Optional[int] = None
    insertion_block_duration: int = 0


@dataclass
class AudioClipSpec:
    path: Path
    start_frames: int
    duration_frames: int
    audio_sample_rate: Optional[int] = None
    audio_channels: Optional[int] = None
    gain_level: Optional[str] = None
    source_in_frames: int = 0
    fade_in_frames: int = 0
    fade_out_frames: int = 0


@dataclass
class ZoomInstruction:
    start_frames: int
    end_frames: int
    code: str


@dataclass(frozen=True)
class ZoomReplacement:
    replacement_id: str
    row_id: str
    zoom_code: str
    timeline_start_frames: int
    timeline_end_frames: int
    source_start_frames: int
    source_end_frames: int
    output_path: Path
    focus_method: str = "unknown"
    focus_x: float = 0.0
    focus_y: float = 0.0


@dataclass(frozen=True)
class ComparisonTagRule:
    header_key: str
    media_path: Path
    anchor: str  # "start" or "end"
    label: str


COMPARISON_TAG_RULES: Sequence[ComparisonTagRule] = (
    ComparisonTagRule(
        header_key="intro tag",
        media_path=INTRO_TAG_VIDEO_PATH,
        anchor="end",
        label="csv-intro",
    ),
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy clap_editor XML and add all insert clips on separate tracks."
    )
    parser.add_argument(
        "--reference-xml",
        help="Reference XML to copy (default: latest clap_editor export).",
    )
    parser.add_argument(
        "--insert-dir",
        default=str(INSERT_DIR),
        help="Folder containing insert clips (default: %(default)s).",
    )
    parser.add_argument(
        "--output",
        help="Destination XML path (defaults to /output/<reference>_precise_inserts.xml).",
    )
    parser.add_argument(
        "--sequence-name",
        help="Optional override for the sequence name.",
    )
    parser.add_argument(
        "--fixed-scale",
        type=float,
        help="Force all insert Basic Motion scales to this percentage.",
    )
    parser.add_argument(
        "--rush-base-scale",
        type=float,
        help="Optional override for the rush fill-frame Basic Motion scale.",
    )
    parser.add_argument(
        "--disable-cta-materialization",
        action="store_true",
        help="Do not materialize CTA inserts from the comparer CSV; require them to be pre-staged.",
    )
    parser.add_argument(
        "--comparison-csv",
        help="Explicit comparer CSV path. Defaults to the latest comparison CSV when omitted.",
    )
    parser.add_argument(
        "--zoom-replacements-manifest",
        help="Optional JSON manifest describing rendered zoom replacement clips for V1 rush segments.",
    )
    return parser.parse_args(argv)


def find_latest_otio_xml(directory: Path = OTIO_XML_OUTPUT_DIR) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"OTIO export directory not found: {directory}")
    xml_files = [path for path in directory.glob("*.xml") if path.is_file()]
    if not xml_files:
        raise FileNotFoundError(f"No XML exports found inside {directory}")
    return max(xml_files, key=lambda path: path.stat().st_mtime)


def normalize_header_key(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def find_latest_comparison_csv(directory: Path = COMPARISON_OUTPUT_DIR) -> Optional[Path]:
    if not directory.exists():
        return None
    candidates = [path for path in directory.rglob("*comparison.csv") if path.is_file()]
    if not candidates:
        return None
    preferred = [
        path for path in candidates if "second_comparser_output" in path.parts
    ]
    pool = preferred or candidates
    return max(pool, key=lambda path: path.stat().st_mtime)


def resolve_comparison_csv(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Comparison CSV not found: {path}")
        return path
    return find_latest_comparison_csv()


def load_zoom_replacements(manifest_path: Optional[str]) -> List[ZoomReplacement]:
    if not manifest_path:
        return []
    resolved = Path(manifest_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"Zoom replacements manifest not found: {resolved}")
    with resolved.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return []

    replacements: List[ZoomReplacement] = []
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            continue
        output_path_raw = entry.get("output_path")
        if not output_path_raw:
            continue
        output_path = Path(str(output_path_raw)).expanduser()
        if not output_path.exists():
            print(f"⚠️  Zoom replacement clip missing, skipping: {output_path}")
            continue
        replacement = ZoomReplacement(
            replacement_id=f"zoom-replacement-{index + 1}",
            row_id=str(entry.get("row_id") or ""),
            zoom_code=str(entry.get("zoom_code") or ""),
            timeline_start_frames=parse_int(str(entry.get("timeline_start_frames")), 0),
            timeline_end_frames=parse_int(str(entry.get("timeline_end_frames")), 0),
            source_start_frames=parse_int(str(entry.get("source_start_frames")), 0),
            source_end_frames=parse_int(str(entry.get("source_end_frames")), 0),
            output_path=output_path.resolve(),
            focus_method=str(entry.get("focus_method") or "unknown"),
            focus_x=float(entry.get("focus_x") or 0.0),
            focus_y=float(entry.get("focus_y") or 0.0),
        )
        if replacement.timeline_end_frames <= replacement.timeline_start_frames:
            continue
        if replacement.source_end_frames <= replacement.source_start_frames:
            continue
        replacements.append(replacement)
    replacements.sort(key=lambda item: (item.timeline_start_frames, item.timeline_end_frames))
    return replacements


def format_insert_timestamp_from_timecode(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 4:
        return None
    try:
        hours, minutes, seconds, frames = (int(part) for part in parts)
    except ValueError:
        return None
    millis = int(round((frames / 25.0) * 1000.0))
    total_seconds = (hours * 3600) + (minutes * 60) + seconds
    total_seconds += millis // 1000
    millis = millis % 1000
    final_hours = total_seconds // 3600
    final_minutes = (total_seconds % 3600) // 60
    final_seconds = total_seconds % 60
    return f"{final_hours:02}h{final_minutes:02}m{final_seconds:02}s{millis:03}ms"


def materialize_cta_inserts(
    insert_dir: Path, csv_path: Optional[Path]
) -> Tuple[List[Path], Optional[Path]]:
    if csv_path is None or "second_comparser_output" not in csv_path.parts:
        return [], csv_path
    if not insert_dir.exists():
        return [], csv_path

    created_or_reused: List[Path] = []
    existing_orders: Dict[str, set[int]] = defaultdict(set)
    existing_label_paths: Dict[Tuple[str, str], Path] = {}
    for path in insert_dir.iterdir():
        if not path.is_file():
            continue
        match = CTA_FILENAME_RE.match(path.name)
        if not match:
            continue
        timestamp_key = match.group("timestamp")
        order_value = int(match.group("order"))
        label_key = match.group("label").lower()
        existing_orders[timestamp_key].add(order_value)
        existing_label_paths[(timestamp_key, label_key)] = path

    list_file = insert_dir / "list.txt"
    listed_entries: List[str] = []
    listed_lookup = set()
    if list_file.exists():
        listed_entries = [
            line.strip()
            for line in list_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        listed_lookup = {entry.lower() for entry in listed_entries}

    def ensure_listed(path: Path) -> None:
        lower_name = path.name.lower()
        if lower_name in listed_lookup:
            return
        listed_entries.append(path.name)
        listed_lookup.add(lower_name)

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fieldnames = reader.fieldnames or []
        header_map = {
            normalize_header_key(name): name for name in fieldnames if name
        }
        start_key = header_map.get("start time")
        if start_key is None:
            return [], csv_path
        for row in reader:
            timestamp_key = format_insert_timestamp_from_timecode(row.get(start_key))
            if timestamp_key is None:
                continue
            next_order = max(existing_orders.get(timestamp_key, set()) or {0}) + 1
            for header_key, slug, source_path in CTA_TAG_SPECS:
                actual_key = header_map.get(header_key)
                if actual_key is None:
                    continue
                if not (row.get(actual_key) or "").strip():
                    continue
                if not source_path.exists():
                    print(f"⚠️  CTA source missing: {source_path}")
                    continue
                existing = existing_label_paths.get((timestamp_key, slug))
                if existing is not None:
                    ensure_listed(existing)
                    created_or_reused.append(existing)
                    continue
                while next_order in existing_orders[timestamp_key]:
                    next_order += 1
                target_name = f"{timestamp_key}_{next_order}_{slug}_001_CTA{source_path.suffix.lower()}"
                target_path = insert_dir / target_name
                shutil.copy2(source_path, target_path)
                existing_orders[timestamp_key].add(next_order)
                existing_label_paths[(timestamp_key, slug)] = target_path
                ensure_listed(target_path)
                created_or_reused.append(target_path)
                next_order += 1

    if list_file.exists():
        list_file.write_text("\n".join(listed_entries) + ("\n" if listed_entries else ""), encoding="utf-8")
    return created_or_reused, csv_path


def parse_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def extract_metadata(reference_xml: Path) -> SequenceMetadata:
    tree = ET.parse(reference_xml)
    sequence = tree.find("./sequence")
    if sequence is None:
        raise ValueError(f"No <sequence> element inside {reference_xml}")
    fps = parse_int(sequence.findtext("./rate/timebase"), 25)
    width = parse_int(sequence.findtext("./media/video/format/samplecharacteristics/width"), 1920)
    height = parse_int(
        sequence.findtext("./media/video/format/samplecharacteristics/height"), 1080
    )
    pixel_aspect = (
        sequence.findtext("./media/video/format/samplecharacteristics/pixelaspectratio")
        or "square"
    )
    audio_sample_rate = parse_int(
        sequence.findtext("./media/audio/format/samplecharacteristics/samplerate"), 48000
    )
    audio_channels = parse_int(
        sequence.findtext("./media/audio/format/samplecharacteristics/channelcount"), 2
    )
    timecode_frame = parse_int(sequence.findtext("./timecode/frame"), 0)
    timecode_string = sequence.findtext("./timecode/string") or "00:00:00:00"
    sequence_name = sequence.findtext("./name") or reference_xml.stem
    return SequenceMetadata(
        fps=fps,
        width=width,
        height=height,
        pixel_aspect=pixel_aspect,
        audio_sample_rate=audio_sample_rate,
        audio_channels=audio_channels,
        timecode_frame=timecode_frame,
        timecode_string=timecode_string,
        sequence_name=sequence_name,
    )


def timecode_to_frames(value: Optional[str], fps: int) -> Optional[int]:
    if not value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 4 or fps <= 0:
        return None
    try:
        hours, minutes, seconds, frames = (int(part) for part in parts)
    except ValueError:
        return None
    total_seconds = hours * 3600 + minutes * 60 + seconds
    total_frames = total_seconds * fps + frames
    return total_frames


def infer_sequence_dimensions(metadata: SequenceMetadata, rush_dir: Path = RUSH_DIR) -> SequenceMetadata:
    """
    Premiere sometimes reports placeholder dimensions inside the XML. Use the
    rush media (which fills the frame at 100%) as the authoritative sequence size.
    """
    if metadata.width > 0 and metadata.height > 0:
        return metadata
    rush_width, rush_height = probe_rush_dimensions(rush_dir)
    if rush_width is None or rush_height is None:
        return metadata
    if rush_width <= 0 or rush_height <= 0:
        return metadata
    if metadata.width == rush_width and metadata.height == rush_height:
        return metadata
    print(
        f"ℹ️  Using rush dimensions {rush_width}x{rush_height} instead of "
        f"{metadata.width}x{metadata.height} for layout calculations."
    )
    return replace(metadata, width=rush_width, height=rush_height)


def probe_rush_dimensions(rush_dir: Path) -> Tuple[Optional[int], Optional[int]]:
    if not rush_dir.exists():
        return (None, None)
    for entry in sorted(rush_dir.rglob("*")):
        if not entry.is_file():
            continue
        if entry.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        _, width, height, _, _ = probe_media_info(entry)
        if width and height:
            return width, height
    return (None, None)


def strip_timestamp_prefix(name: str) -> str:
    return re.sub(
        r"^(?:(?:\d{2}h\d{2}m\d{2}s\d{3}ms)|(?:\d+m\d{2}))_",
        "",
        name,
        count=1,
        flags=re.IGNORECASE,
    )


def parse_timestamp_seconds(name: str) -> Optional[float]:
    precise_match = PRECISE_TIMESTAMP_PATTERN.search(name)
    if precise_match:
        return (
            int(precise_match.group(1)) * 3600
            + int(precise_match.group(2)) * 60
            + int(precise_match.group(3))
            + int(precise_match.group(4)) / 1000.0
        )
    legacy_match = LEGACY_TIMESTAMP_PATTERN.search(name)
    if legacy_match:
        minutes = int(legacy_match.group(1))
        seconds = int(legacy_match.group(2))
        return minutes * 60 + seconds
    return None


def has_precise_timestamp_prefix(name: str) -> bool:
    return PRECISE_TIMESTAMP_PATTERN.match(name) is not None


def is_extract_clip(name: str) -> bool:
    return "extract" in Path(name).stem.lower()


def is_circle_arrow_clip(path: Path) -> bool:
    return "circle_arrow_trans" in path.stem.lower()


def is_filled_noun_clip(path: Path) -> bool:
    return path.stem.lower().endswith("_filled")


def is_noun_or_arrow_clip(path: Path) -> bool:
    return is_circle_arrow_clip(path) or is_filled_noun_clip(path)


def is_intro_zoom_clip(path: Path) -> bool:
    return "intro_zoom" in path.stem.lower()


def is_outro_dip_clip(path: Path) -> bool:
    return "outro_dip" in path.stem.lower()


def extract_insert_label(name: str) -> Optional[str]:
    stem = Path(name).stem
    if "@" in stem:
        stem = stem.split("@", 1)[0]
    parts = stem.split("_", 2)
    if len(parts) < 3:
        return None
    label = re.sub(r"^\d+_?", "", parts[2].strip()).strip("_")
    return label or None


def extract_insert_order(name: str) -> Optional[int]:
    match = INSERT_ORDER_RE.match(Path(name).name)
    if not match:
        return None
    try:
        return int(match.group("order"))
    except ValueError:
        return None


def is_image_clip(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def normalize_label(label: Optional[str]) -> Optional[str]:
    return label.lower() if label else None


def label_matches(label: Optional[str], expected: str) -> bool:
    if not label:
        return False
    label = label.lower()
    expected = expected.lower()
    if label == expected:
        return True
    if label.startswith(expected + "_") or label.endswith("_" + expected):
        return True
    return f"_{expected}_" in label or expected in label.split("_")


def label_is_title(label: Optional[str]) -> bool:
    return label_matches(label, "title")


def label_is_titre(label: Optional[str]) -> bool:
    return label_matches(label, "titre")


def label_is_quote(label: Optional[str]) -> bool:
    return label_matches(label, "quote")


def label_is_punctuation(label: Optional[str]) -> bool:
    return any(
        label_matches(label, expected)
        for expected in ("exclamation", "question", "threedots")
    )


def label_is_video_link_transition(label: Optional[str]) -> bool:
    return label_matches(label, "transitionfilburn")


def label_uses_title_layout(label: Optional[str]) -> bool:
    return label_is_title(label) or label_is_titre(label)


def overlay_keeps_source_audio(path: Path, label: Optional[str]) -> bool:
    return not is_downloaded_video_link_direct(path, label)


def is_downloaded_article_title_clip(path: Path, label: Optional[str]) -> bool:
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and "@" in path.stem
        and label_uses_title_layout(label)
    )


def is_downloaded_video_link_clip(path: Path, label: Optional[str]) -> bool:
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and "@" in path.stem
        and not label
    )


def should_use_image_track(path: Path, is_image: bool, has_alpha: bool) -> bool:
    """
    Swisser animator replacements are videos with transparency that should live
    on the graphics tracks (V4+). Heuristic: favor image tracks for real images
    and for video overlays that retain an alpha channel.
    """
    if is_image:
        return True
    return path.suffix.lower() in VIDEO_EXTENSIONS and has_alpha


GENERATED_INSERT_TAG_RE = re.compile(r"_(LST|BLD|CTA|SOC|DUR|URL|TWT|PLR|QH)(?=[._])", re.IGNORECASE)


def _detect_generated_insert_tag(path: Path) -> Optional[str]:
    match = GENERATED_INSERT_TAG_RE.search(path.name)
    if match is None:
        return None
    return match.group(1).upper()


def resolve_generated_insert_left_shift_seconds(
    path: Path,
    label: Optional[str],
    *,
    is_extract: bool,
) -> float:
    """
    Decide how far left (or right, when negative) to nudge a generated insert
    so that the visible text lands on the spoken-word anchor encoded in the
    filename. Resolution order:
      1. Extracts / title layouts / transitions → 0 (they own their timing).
      2. Sidecar field `animation_preroll_seconds` when present.
      3. Known filename tag (LST/BLD/CTA/SOC/DUR/URL/TWT/PLR) defaults.
      4. URL screen / polaroid / tweet heuristics → small rightward nudge.
      5. Remaining generated inserts → GENERATED_INSERT_LEFT_SHIFT_SECONDS.
    """
    if is_extract:
        return 0.0
    if label_uses_title_layout(label):
        return 0.0
    if label_is_video_link_transition(label):
        return 0.0

    sidecar_value = read_insert_timing_float_field(path, "animation_preroll_seconds")
    if sidecar_value is not None:
        return sidecar_value

    tag = _detect_generated_insert_tag(path)
    if tag is not None and tag in GENERATED_INSERT_LEFT_SHIFT_BY_TAG:
        return GENERATED_INSERT_LEFT_SHIFT_BY_TAG[tag]

    lower_name = path.name.lower()
    if "url_screen" in lower_name:
        return GENERATED_INSERT_URL_SCREEN_SHIFT_SECONDS
    if "polaroid" in lower_name:
        return GENERATED_INSERT_POLAROID_SHIFT_SECONDS
    stem_lower = path.stem.lower()
    if "@twitter" in stem_lower or "@x_" in stem_lower or stem_lower.startswith("@twitter"):
        return GENERATED_INSERT_TWEET_SHIFT_SECONDS

    if is_noun_or_arrow_clip(path):
        return GENERATED_INSERT_LEFT_SHIFT_SECONDS

    if "@" in path.stem:
        return 0.0

    return GENERATED_INSERT_LEFT_SHIFT_SECONDS


def parse_clip_trim_seconds(name: str) -> tuple[Optional[float], Optional[float]]:
    """
    Look for a trailing marker such as "5/22" or "02:21-02:32" inside the filename.
    Returns a tuple of (start_seconds, end_seconds). If end_seconds is None, it means
    "use the rest of the file".
    """
    stem = Path(name).stem.rstrip(" )]}>").strip()
    match = CUT_RANGE_PATTERN.search(stem)
    if not match:
        return None, None
    start_total = (
        int(match.group("start_min")) * 60
        + int(match.group("start_sec"))
        + _milliseconds_from_match(match.group("start_millis"))
    )
    end_min = match.group("end_min")
    if end_min is None:
        return float(start_total), None
    end_total = (
        int(end_min) * 60
        + int(match.group("end_sec"))
        + _milliseconds_from_match(match.group("end_millis"))
    )
    return float(start_total), float(end_total)


def _milliseconds_from_match(token: Optional[str]) -> float:
    if not token:
        return 0.0
    normalized = (token + "000")[:3]
    return int(normalized) / 1000.0


def build_insert_timing_sidecar_path(path: Path) -> Path:
    return path.parent / INSERT_TIMING_SIDECAR_DIRNAME / f"{path.name}.json"


def read_insert_timing_payload(path: Path) -> Optional[dict]:
    sidecar_path = build_insert_timing_sidecar_path(path)
    if not sidecar_path.exists():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def read_insert_requested_duration_seconds(path: Path) -> Optional[float]:
    payload = read_insert_timing_payload(path)
    if payload is None:
        return None
    value = payload.get("requested_duration_seconds")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def read_insert_timing_end_seconds(path: Path) -> Optional[float]:
    payload = read_insert_timing_payload(path)
    if payload is None:
        return None
    value = payload.get("end_seconds")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return seconds


def read_insert_timing_float_field(path: Path, field_name: str) -> Optional[float]:
    payload = read_insert_timing_payload(path)
    if payload is None:
        return None
    value = payload.get(field_name)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return seconds


def read_insert_timing_asset_mode(path: Path) -> Optional[str]:
    payload = read_insert_timing_payload(path)
    if payload is None:
        return None
    value = payload.get("asset_mode")
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def read_insert_visible_window_start_seconds(path: Path) -> Optional[float]:
    return read_insert_timing_float_field(path, "show_from_seconds")


def read_insert_visible_window_end_seconds(path: Path) -> Optional[float]:
    return read_insert_timing_float_field(path, "show_until_seconds")


def read_insert_visible_duration_seconds(path: Path) -> Optional[float]:
    value = read_insert_timing_float_field(path, "visible_duration_seconds")
    if value is not None and value > 0:
        return value
    start_seconds = read_insert_visible_window_start_seconds(path)
    end_seconds = read_insert_visible_window_end_seconds(path)
    if start_seconds is None or end_seconds is None or end_seconds <= start_seconds:
        return None
    return end_seconds - start_seconds


def stem_has_token(stem: str, token: str) -> bool:
    return (
        re.search(
            rf"(?:^|[_\-\s]){re.escape(token)}(?:$|[_\-\s])",
            stem,
            re.IGNORECASE,
        )
        is not None
    )


def classify_downloaded_video_link(path: Path, label: Optional[str]) -> Optional[str]:
    if not is_downloaded_video_link_clip(path, label):
        return None
    stem = path.stem
    if any(stem_has_token(stem, token) for token in ("EXTRACT", "EXCERPT", "EXTRAIT")):
        return "extract"
    if stem_has_token(stem, "DIRECT"):
        return "direct"
    trim_start, trim_end = parse_clip_trim_seconds(path.name)
    if trim_start is not None or trim_end is not None:
        return "extract"
    return "direct"


def is_downloaded_video_link_extract(path: Path, label: Optional[str]) -> bool:
    return classify_downloaded_video_link(path, label) == "extract"


def is_downloaded_video_link_direct(path: Path, label: Optional[str]) -> bool:
    return classify_downloaded_video_link(path, label) == "direct"


def clamp_trim_window(
    total_duration: float,
    fps: int,
    start_seconds: Optional[float],
    end_seconds: Optional[float],
) -> tuple[float, float]:
    """
    Ensures the requested trim window is valid within the media duration.
    """
    if total_duration <= 0:
        total_duration = 5.0
    min_slice = 1.0 / max(1, fps)
    start = max(0.0, float(start_seconds or 0.0))
    if start >= total_duration:
        start = max(0.0, total_duration - min_slice)
    requested_end = total_duration if end_seconds is None else float(end_seconds)
    end = min(total_duration, max(start, requested_end))
    if end - start < min_slice:
        end = min(total_duration, start + min_slice)
    if end <= start:
        start = max(0.0, min(total_duration - min_slice, start))
        end = max(start + min_slice, min(total_duration, start + min_slice))
    return start, end


def probe_media_info(path: Path) -> tuple[float, Optional[int], Optional[int], bool, bool]:
    resolved = path.resolve()
    cached = PROBE_CACHE.get(resolved)
    if cached is not None:
        return cached
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height,pix_fmt:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout or "{}")
        duration = float(payload.get("format", {}).get("duration", 0.0))
        if duration <= 0:
            duration = 5.0
        streams = payload.get("streams") or []
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        width = video_stream.get("width")
        height = video_stream.get("height")
        pix_fmt = str(video_stream.get("pix_fmt") or "").lower()
        has_alpha = "a" in pix_fmt if pix_fmt else False
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        info = (duration, width, height, has_alpha, has_audio)
        PROBE_CACHE[resolved] = info
        return info
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, IndexError):
        if path.suffix.lower() in AUDIO_EXTENSIONS:
            audio_duration = probe_audio_duration(path)
            fallback = (audio_duration, None, None, False, True)
        else:
            fallback = (5.0, None, None, False, False)
        PROBE_CACHE[resolved] = fallback
        return fallback


def probe_audio_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        value = float(result.stdout.strip())
        if value <= 0:
            return 5.0
        return value
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 5.0


def probe_audio_stream_info(path: Path) -> tuple[Optional[int], Optional[int]]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        if not streams:
            return None, None
        stream = streams[0]
        sample_rate = parse_int(stream.get("sample_rate"))
        channels = parse_int(stream.get("channels"))
        return (sample_rate or None, channels or None)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, IndexError):
        return None, None


def adjust_audio_gain_level(
    gain_level: Optional[str], delta_units: int = GAIN_REDUCTION_DB_UNITS
) -> str:
    try:
        current = int(str(gain_level)) if gain_level is not None else 0
    except (TypeError, ValueError):
        current = 0
    return str(current + delta_units)


def db_to_premiere_units(db_value: float) -> str:
    return str(int(round(db_value * 100.0)))


def linear_multiplier_to_premiere_units(multiplier: float) -> str:
    if multiplier <= 0:
        return MUTED_AUDIO_LEVEL
    return db_to_premiere_units(20.0 * math.log10(multiplier))


def premiere_units_to_multiplier(level_value: Optional[str]) -> float:
    try:
        db_value = int(str(level_value)) / 100.0 if level_value is not None else 0.0
    except (TypeError, ValueError):
        db_value = 0.0
    return math.pow(10.0, db_value / 20.0)


def analyze_audio_peak_db(path: Path) -> Optional[float]:
    resolved = path.resolve()
    cached = PEAK_DB_CACHE.get(resolved)
    if resolved in PEAK_DB_CACHE:
        return cached
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        PEAK_DB_CACHE[resolved] = None
        return None
    output = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"max_volume:\s*(-?(?:\d+(?:\.\d+)?)|inf|-inf)\s*dB", output, re.IGNORECASE)
    if not match:
        PEAK_DB_CACHE[resolved] = None
        return None
    token = match.group(1).lower()
    if token in {"inf", "-inf"}:
        peak_db = None
    else:
        try:
            peak_db = float(token)
        except ValueError:
            peak_db = None
    PEAK_DB_CACHE[resolved] = peak_db
    return peak_db


def compute_peak_safe_gain_units(
    peak_db: Optional[float],
    *,
    target_peak_db: float = -1.0,
    silence_floor_db: float = -50.0,
) -> str:
    if peak_db is None or not math.isfinite(peak_db) or peak_db <= silence_floor_db:
        return "0"
    return db_to_premiere_units(target_peak_db - peak_db)


def media_has_audio(path: Path) -> bool:
    _, _, _, _, has_audio = probe_media_info(path)
    return has_audio


def compute_scale_value(
    clip_width: Optional[int], clip_height: Optional[int], metadata: SequenceMetadata
) -> str:
    width = clip_width or metadata.width
    height = clip_height or metadata.height
    if width <= 0 or height <= 0:
        return SCALE_PERCENT
    scale_factor = max(metadata.width / width, metadata.height / height)
    return format_scale(scale_factor * 100)


def compute_fill_frame_scale(
    sequence_width: int,
    sequence_height: int,
    clip_width: Optional[int],
    clip_height: Optional[int],
    *,
    fallback_scale: float = 100.0,
) -> str:
    width = clip_width or 0
    height = clip_height or 0
    if sequence_width <= 0 or sequence_height <= 0 or width <= 0 or height <= 0:
        return format_scale(fallback_scale)
    scale_factor = max(sequence_width / width, sequence_height / height)
    return format_scale(scale_factor * 100.0)


def format_scale(value: float) -> str:
    limited = min(value, MAX_SCALE_VALUE)
    return f"{limited:.1f}"


def multiply_scale(scale_value: str, factor: float) -> str:
    try:
        numeric = float(scale_value)
    except ValueError:
        numeric = float(SCALE_PERCENT)
    return format_scale(numeric * factor)


def _clipitem_dimensions(clipitem: ET.Element) -> tuple[Optional[int], Optional[int]]:
    clip_path = clipitem_media_path(clipitem)
    if clip_path is None:
        return None, None
    _, width, height, _, _ = probe_media_info(clip_path)
    return width, height


def determine_rush_fill_frame_scale(
    rush_video_track: Optional[ET.Element],
    metadata: SequenceMetadata,
    explicit_scale: Optional[float] = None,
) -> str:
    if explicit_scale is not None:
        return format_scale(explicit_scale)
    if rush_video_track is None:
        return format_scale(100.0)
    for clipitem in rush_video_track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        width, height = _clipitem_dimensions(clipitem)
        return compute_fill_frame_scale(metadata.width, metadata.height, width, height)
    return format_scale(100.0)


def apply_default_scale_to_rush(
    rush_video_track: Optional[ET.Element],
    *,
    scale_value: str,
) -> None:
    if rush_video_track is None:
        return
    for clipitem in rush_video_track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        filter_el = ensure_basic_motion_filter(clipitem, default_scale=scale_value)
        set_motion_scale(filter_el, scale_value)


def seconds_to_frames(seconds: float, fps: int, *, allow_zero: bool = False) -> int:
    frame_value = int(round(seconds * fps))
    minimum = 0 if allow_zero else 1
    return max(minimum, frame_value)


def seconds_plus_frames_to_frames(
    seconds: float,
    extra_frames: int,
    fps: int,
    *,
    allow_zero: bool = False,
) -> int:
    total_frames = seconds_to_frames(seconds, fps, allow_zero=True) + max(0, extra_frames)
    minimum = 0 if allow_zero else 1
    return max(minimum, total_frames)


def path_from_url(pathurl: Optional[str]) -> Optional[Path]:
    if not pathurl:
        return None
    parsed = urlparse(pathurl)
    if not parsed.path:
        return None
    return Path(unquote(parsed.path))


def build_file_reference_map(root: ET.Element) -> Dict[str, ET.Element]:
    references: Dict[str, ET.Element] = {}
    for file_el in root.findall(".//file"):
        file_id = file_el.get("id")
        if not file_id:
            continue
        if file_el.findtext("pathurl"):
            references[file_id] = copy.deepcopy(file_el)
    return references


def hydrate_clipitem_file_references(root: ET.Element) -> None:
    """
    Some reference XMLs store the full <file> definition once, then reuse it as
    empty <file id="..."/> nodes inside clipitems. Expand those nodes in-place so
    downstream rush detection/splitting can inspect the actual pathurl.
    """
    file_references = build_file_reference_map(root)
    if not file_references:
        return
    for clipitem in root.findall(".//clipitem"):
        file_el = clipitem.find("file")
        if file_el is None or file_el.findtext("pathurl"):
            continue
        file_id = file_el.get("id")
        if not file_id:
            continue
        source = file_references.get(file_id)
        if source is None:
            continue
        file_el.attrib.clear()
        file_el.attrib.update(source.attrib)
        file_el.text = source.text
        file_el.tail = source.tail
        file_el[:] = [copy.deepcopy(child) for child in list(source)]


def build_rate_node(fps: int) -> ET.Element:
    node = ET.Element("rate")
    ET.SubElement(node, "timebase").text = str(fps)
    ET.SubElement(node, "ntsc").text = "FALSE"
    return node


def create_motion_effect(scale_value: str, center_point: Optional[Tuple[float, float]] = None) -> ET.Element:
    filter_el = ET.Element("filter")
    effect = ET.SubElement(filter_el, "effect")
    ET.SubElement(effect, "name").text = "Basic Motion"
    ET.SubElement(effect, "effectid").text = "basic"
    ET.SubElement(effect, "effectcategory").text = "motion"
    ET.SubElement(effect, "effecttype").text = "motion"
    ET.SubElement(effect, "mediatype").text = "video"
    ET.SubElement(effect, "pproBypass").text = "false"

    scale_param = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(scale_param, "parameterid").text = "scale"
    ET.SubElement(scale_param, "name").text = "Scale"
    ET.SubElement(scale_param, "valuemin").text = "0"
    ET.SubElement(scale_param, "valuemax").text = "1000"
    ET.SubElement(scale_param, "value").text = scale_value

    rotation = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(rotation, "parameterid").text = "rotation"
    ET.SubElement(rotation, "name").text = "Rotation"
    ET.SubElement(rotation, "valuemin").text = "-8640"
    ET.SubElement(rotation, "valuemax").text = "8640"
    ET.SubElement(rotation, "value").text = "0"

    if center_point is not None:
        center = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        ET.SubElement(center, "parameterid").text = "center"
        ET.SubElement(center, "name").text = "Center"
        center_value = ET.SubElement(center, "value")
        ET.SubElement(center_value, "horiz").text = str(center_point[0])
        ET.SubElement(center_value, "vert").text = str(center_point[1])

        anchor = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        ET.SubElement(anchor, "parameterid").text = "centerOffset"
        ET.SubElement(anchor, "name").text = "Anchor Point"
        anchor_value = ET.SubElement(anchor, "value")
        ET.SubElement(anchor_value, "horiz").text = "0"
        ET.SubElement(anchor_value, "vert").text = "0"

    for pid, name in (
        ("antiflicker", "Anti-flicker Filter"),
        ("leftcrop", "Left"),
        ("topcrop", "Top"),
        ("rightcrop", "Right"),
        ("bottomcrop", "Bottom"),
    ):
        param = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        ET.SubElement(param, "parameterid").text = pid
        ET.SubElement(param, "name").text = name
        ET.SubElement(param, "valuemin").text = "0.0"
        ET.SubElement(param, "valuemax").text = "100.0"
        ET.SubElement(param, "value").text = "0"

    return filter_el


def create_audio_gain_filter(level_value: str) -> ET.Element:
    filter_el = ET.Element("filter")
    effect = ET.SubElement(filter_el, "effect")
    ET.SubElement(effect, "name").text = "Volume"
    ET.SubElement(effect, "effectid").text = "volume"
    ET.SubElement(effect, "effectcategory").text = "volume"
    ET.SubElement(effect, "effecttype").text = "audiolevels"
    ET.SubElement(effect, "mediatype").text = "audio"
    ET.SubElement(effect, "pproBypass").text = "false"

    level_param = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
    ET.SubElement(level_param, "parameterid").text = "level"
    ET.SubElement(level_param, "name").text = "Level"
    ET.SubElement(level_param, "valuemin").text = "-9600"
    ET.SubElement(level_param, "valuemax").text = "9600"
    ET.SubElement(level_param, "value").text = level_value
    return filter_el


def create_audio_fade_metadata_filter(
    *,
    fade_in_frames: int = 0,
    fade_out_frames: int = 0,
) -> Optional[ET.Element]:
    if fade_in_frames <= 0 and fade_out_frames <= 0:
        return None
    filter_el = ET.Element("filter")
    effect = ET.SubElement(filter_el, "effect")
    ET.SubElement(effect, "name").text = "Codex Audio Meta"
    ET.SubElement(effect, "effectid").text = "codex_audio_meta"
    ET.SubElement(effect, "effectcategory").text = "audio"
    ET.SubElement(effect, "effecttype").text = "metadata"
    ET.SubElement(effect, "mediatype").text = "audio"

    if fade_in_frames > 0:
        fade_in = ET.SubElement(effect, "parameter")
        ET.SubElement(fade_in, "parameterid").text = "fadeinframes"
        ET.SubElement(fade_in, "name").text = "Fade In Frames"
        ET.SubElement(fade_in, "value").text = str(fade_in_frames)
    if fade_out_frames > 0:
        fade_out = ET.SubElement(effect, "parameter")
        ET.SubElement(fade_out, "parameterid").text = "fadeoutframes"
        ET.SubElement(fade_out, "name").text = "Fade Out Frames"
        ET.SubElement(fade_out, "value").text = str(fade_out_frames)
    return filter_el


def _effect_id(filter_el: ET.Element) -> str:
    return (filter_el.findtext("./effect/effectid") or "").strip().lower()


def set_clipitem_audio_gain(clipitem: ET.Element, level_value: str) -> None:
    for filter_el in list(clipitem.findall("filter")):
        if _effect_id(filter_el) == "volume":
            clipitem.remove(filter_el)
    clipitem.append(create_audio_gain_filter(level_value))


def append_link(
    clipitem: ET.Element,
    clip_id: str,
    mediatype: str,
    track_index: int,
    clip_index: int,
    group_index: int,
) -> None:
    link = ET.SubElement(clipitem, "link")
    ET.SubElement(link, "linkclipref").text = clip_id
    ET.SubElement(link, "mediatype").text = mediatype
    ET.SubElement(link, "trackindex").text = str(track_index)
    ET.SubElement(link, "clipindex").text = str(clip_index)
    ET.SubElement(link, "groupindex").text = str(group_index)


def find_basic_motion_filter(clipitem: ET.Element) -> Optional[ET.Element]:
    for filter_el in clipitem.findall("filter"):
        effect = filter_el.find("effect")
        if effect is None:
            continue
        if (effect.findtext("name") or "").lower() == "basic motion":
            return filter_el
    return None


def ensure_basic_motion_filter(clipitem: ET.Element, default_scale: str = "100") -> ET.Element:
    existing = find_basic_motion_filter(clipitem)
    if existing is not None:
        return existing
    filter_el = create_motion_effect(default_scale)
    clipitem.append(filter_el)
    return filter_el


def _find_effect_parameter(effect: ET.Element, parameter_id: str) -> Optional[ET.Element]:
    for param in effect.findall("parameter"):
        if (param.findtext("parameterid") or "").lower() == parameter_id.lower():
            return param
    return None


def set_motion_scale(filter_el: ET.Element, scale_value: str) -> None:
    effect = filter_el.find("effect")
    if effect is None:
        return
    parameter = _find_effect_parameter(effect, "scale")
    if parameter is None:
        parameter = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        ET.SubElement(parameter, "parameterid").text = "scale"
        ET.SubElement(parameter, "name").text = "Scale"
        ET.SubElement(parameter, "valuemin").text = "0"
        ET.SubElement(parameter, "valuemax").text = "1000"
    value_node = parameter.find("value")
    if value_node is None:
        value_node = ET.SubElement(parameter, "value")
    value_node.text = scale_value


def set_motion_center(filter_el: ET.Element, center_point: Tuple[float, float]) -> None:
    effect = filter_el.find("effect")
    if effect is None:
        return
    parameter = _find_effect_parameter(effect, "center")
    if parameter is None:
        parameter = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        ET.SubElement(parameter, "parameterid").text = "center"
        ET.SubElement(parameter, "name").text = "Center"
    value_node = parameter.find("value")
    if value_node is None:
        value_node = ET.SubElement(parameter, "value")
    horiz = value_node.find("horiz")
    if horiz is None:
        horiz = ET.SubElement(value_node, "horiz")
    vert = value_node.find("vert")
    if vert is None:
        vert = ET.SubElement(value_node, "vert")
    horiz.text = str(center_point[0])
    vert.text = str(center_point[1])


def replace_basic_motion_filter(clipitem: ET.Element, scale_value: str = "100") -> None:
    for filter_el in list(clipitem.findall("filter")):
        effect = filter_el.find("effect")
        if effect is None:
            continue
        if (effect.findtext("name") or "").strip().lower() == "basic motion":
            clipitem.remove(filter_el)
    clipitem.append(create_motion_effect(scale_value))


def get_motion_scale(filter_el: ET.Element, default: str = "100") -> str:
    effect = filter_el.find("effect")
    if effect is None:
        return default
    parameter = _find_effect_parameter(effect, "scale")
    if parameter is None:
        return default
    value_node = parameter.find("value")
    if value_node is None or not value_node.text:
        return default
    return value_node.text


def list_insert_paths(folder: Path) -> List[Path]:
    list_file = folder / "list.txt"
    if list_file.exists():
        listed = [
            line.strip()
            for line in list_file.read_text().splitlines()
            if line.strip()
        ]
        listed_set = {name.lower() for name in listed}
        # Also include any timestamped files in the folder not already in list.txt
        # (e.g. animated assets added by unified_insert_creator after list.txt was written)
        extra = [
            p.name
            for p in folder.iterdir()
            if p.is_file()
            and p.name.lower() not in listed_set
            and p.name != "list.txt"
            and parse_timestamp_seconds(p.name) is not None
        ]
        if extra:
            print(f"ℹ️  {len(extra)} extra timestamped file(s) found outside list.txt — including them.")
        entries = listed + extra
        # Sort all by timestamp so timeline order is correct
        entries.sort(key=lambda n: parse_timestamp_seconds(n) or 0)
    else:
        entries = sorted(p.name for p in folder.iterdir() if p.is_file())
    paths = []
    for entry in entries:
        resolved = resolve_media_entry(folder, entry)
        if resolved is None:
            print(f"⚠️  {entry} listed but missing from {folder}.")
            continue
        paths.append(resolved)
    # Also include zoom assets staged in the zoom/ subfolder (intro zoom, outro dip, etc.)
    zoom_subfolder = folder / "zoom"
    if zoom_subfolder.is_dir():
        zoom_names = sorted(p.name for p in zoom_subfolder.iterdir() if p.is_file())
        for name in zoom_names:
            resolved = resolve_media_entry(zoom_subfolder, name)
            if resolved is not None:
                paths.append(resolved)
    return paths


def resolve_media_entry(folder: Path, entry: str) -> Optional[Path]:
    """
    Resolve a line from list.txt to an actual file, even if animator_for_swisser
    replaced the original image with a rendered .mov clip.
    """
    candidate = folder / entry
    if candidate.exists() and candidate.suffix.lower() in MEDIA_EXTENSIONS:
        return candidate
    stem = Path(entry).stem
    for ext in MEDIA_EXTENSIONS:
        fallback = folder / f"{stem}{ext}"
        if fallback.exists():
            return fallback
    return None


def gather_comparison_tag_clips(
    metadata: SequenceMetadata,
    csv_path: Optional[Path] = None,
) -> Tuple[List[InsertClip], List[ZoomInstruction]]:
    global LAST_COMPARISON_CSV
    if csv_path is None:
        csv_path = find_latest_comparison_csv()
    if csv_path is None:
        return [], []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            fieldnames = reader.fieldnames or []
            header_map = {
                normalize_header_key(name): name for name in fieldnames if name
            }
            start_key = header_map.get("start time")
            end_key = header_map.get("end time")
            if start_key is None:
                print(f"⚠️  CSV '{csv_path.name}' missing 'Start Time' column.")
                return []
            if end_key is None:
                print(f"⚠️  CSV '{csv_path.name}' missing 'End Time' column.")
                return []
            zoom_key = header_map.get("zoom")
            available_rules: List[ComparisonTagRule] = []
            for rule in COMPARISON_TAG_RULES:
                if not rule.media_path.exists():
                    print(f"⚠️  Tagged asset missing: {rule.media_path}")
                    continue
                if rule.header_key not in header_map:
                    continue
                available_rules.append(rule)

            def value_from_row(row: Dict[str, str], logical_key: str) -> str:
                actual_key = header_map.get(logical_key)
                if actual_key is None:
                    return ""
                return (row.get(actual_key) or "").strip()

            tag_clips: List[InsertClip] = []
            zoom_instructions: List[ZoomInstruction] = []
            intro_preroll_frames = seconds_to_frames(
                INTRO_PREROLL_SECONDS, metadata.fps, allow_zero=True
            )
            intro_gap_frames = seconds_to_frames(
                INTRO_GAP_SECONDS, metadata.fps, allow_zero=True
            )
            intro_added = False
            for row in reader:
                start_frames = timecode_to_frames(row.get(start_key), metadata.fps)
                end_frames = timecode_to_frames(row.get(end_key), metadata.fps)
                if start_frames is None:
                    continue
                for rule in available_rules:
                    tag_value = value_from_row(row, rule.header_key)
                    if not tag_value:
                        continue
                    anchor_frame = start_frames if rule.anchor == "start" else end_frames
                    if anchor_frame is None:
                        anchor_frame = start_frames
                    if anchor_frame is None:
                        continue
                    clip = build_external_video_clip(rule.media_path, metadata, anchor_frame)
                    if clip is None:
                        continue
                    clip.label = rule.label
                    clip.preserve_duration = True
                    if rule.label == "csv-intro":
                        if intro_added:
                            continue
                        clip.start_frames = max(0, anchor_frame - intro_preroll_frames)
                        clip.video_track_override = INTRO_VIDEO_OVERLAY_TRACK_INDEX
                        clip.audio_track_override = INTRO_AUDIO_OVERLAY_TRACK_INDEX
                        clip.timeline_gap_anchor = anchor_frame
                        clip.timeline_gap_duration = intro_gap_frames
                        intro_added = True
                    tag_clips.append(clip)
                zoom_value = value_from_row(row, "zoom") if zoom_key else ""
                zoom_tokens = [
                    token.strip().lower()
                    for token in zoom_value.split(",")
                    if token.strip()
                ]
                if zoom_tokens and start_frames is not None:
                    zoom_code = zoom_tokens[0]
                    if zoom_code in {"z", "z1", "z2", "z3"}:
                        zoom_instructions.append(
                            ZoomInstruction(
                                start_frames=start_frames,
                                end_frames=end_frames or start_frames,
                                code=zoom_code,
                            )
                        )
    except OSError as exc:
        print(f"⚠️  Unable to read comparison CSV: {exc}")
        return [], []
    LAST_COMPARISON_CSV = csv_path
    print(f"Comparison CSV: {csv_path}")
    return tag_clips, zoom_instructions


def collect_timeline_gap_events(clips: Sequence[InsertClip]) -> List[Tuple[int, int]]:
    events: List[Tuple[int, int]] = []
    for clip in clips:
        if clip.timeline_gap_anchor is None or clip.timeline_gap_duration <= 0:
            continue
        events.append((clip.timeline_gap_anchor, clip.timeline_gap_duration))
    events.sort(key=lambda event: event[0])
    return events


def apply_timeline_gap_offsets(
    clips: List[InsertClip], events: Sequence[Tuple[int, int]]
) -> List[Tuple[int, int]]:
    if not events:
        return []
    adjusted_events: List[Tuple[int, int]] = []
    cumulative_shift = 0
    for frame, duration in events:
        adjusted_frame = frame + cumulative_shift
        for clip in clips:
            if clip.start_frames >= adjusted_frame:
                clip.start_frames += duration
        adjusted_events.append((adjusted_frame, duration))
        cumulative_shift += duration
    return adjusted_events


def gather_insert_clips(
    folder: Path,
    metadata: SequenceMetadata,
    comparison_csv: Optional[Path] = None,
) -> Tuple[List[InsertClip], List[Tuple[int, int]], List[ZoomInstruction]]:
    clips: List[InsertClip] = []
    insert_paths = list_insert_paths(folder)
    title_gif_stems = {
        path.stem.lower()
        for path in insert_paths
        if path.suffix.lower() == ".gif" and label_is_title(extract_insert_label(path.name))
    }
    for path in insert_paths:
        if is_outro_dip_clip(path):
            print(f"ℹ️  Reserving '{path.name}' for rush V1/A1 swap (not added as overlay).")
            continue
        start_seconds = parse_timestamp_seconds(path.name)
        if start_seconds is None:
            print(f"⚠️  Skipping '{path.name}' (no timestamp).")
            continue
        label = extract_insert_label(path.name)
        if label_is_punctuation(label) or "_PNC" in path.stem.upper():
            print(f"ℹ️  Skipping punctuation insert '{path.name}'.")
            continue
        normalized_label = normalize_label(label)
        video_link_kind = classify_downloaded_video_link(path, label)
        requested_duration_seconds = (
            read_insert_requested_duration_seconds(path)
            if video_link_kind is not None
            else None
        )
        title_end_seconds = read_insert_timing_end_seconds(path) if label_is_title(label) else None
        if (
            path.suffix.lower() == ".mov"
            and path.stem.lower() in title_gif_stems
            and label_is_title(label)
        ):
            print(f"⚠️  Skipping '{path.name}' (raw title GIF will be assembled in XML).")
            continue
        duration_seconds, width, height, has_alpha, _ = probe_media_info(path)
        trim_start, trim_end = parse_clip_trim_seconds(path.name)
        if requested_duration_seconds is not None and trim_start is None and trim_end is None:
            source_start, source_end = clamp_trim_window(
                duration_seconds,
                metadata.fps,
                0.0,
                requested_duration_seconds,
            )
        else:
            source_start, source_end = clamp_trim_window(
                duration_seconds, metadata.fps, trim_start, trim_end
            )
        duration_frames = seconds_to_frames(source_end - source_start, metadata.fps)
        image_clip = is_image_clip(path)
        treat_as_overlay = should_use_image_track(path, image_clip, has_alpha)
        is_extract = (
            is_extract_clip(path.name)
            or label_matches(label, "extrait")
            or video_link_kind == "extract"
        )
        # Excerpts now overlay like direct videos (no rush gap / intercalation).
        is_extract = False
        start_frames = seconds_to_frames(float(start_seconds), metadata.fps)
        shift_seconds = resolve_generated_insert_left_shift_seconds(
            path, label, is_extract=is_extract
        )
        if shift_seconds:
            shift_frames = seconds_to_frames(
                abs(shift_seconds), metadata.fps, allow_zero=True
            )
            if shift_seconds < 0:
                start_frames = start_frames + shift_frames
            else:
                start_frames = max(0, start_frames - shift_frames)
        clip = InsertClip(
            path=path,
            start_frames=start_frames,
            source_in_frames=seconds_to_frames(source_start, metadata.fps, allow_zero=True),
            duration_frames=duration_frames,
            source_width=width,
            source_height=height,
            scale_value=compute_scale_value(width, height, metadata),
            is_extract=is_extract,
            is_image=image_clip,
            treat_as_overlay=treat_as_overlay,
            label=label,
            intercalated_insert=is_extract,
            insertion_block_id=f"extract:{path.resolve()}:{start_frames}" if is_extract else None,
            insertion_block_anchor=start_frames if is_extract else None,
            insertion_block_duration=duration_frames if is_extract else 0,
        )
        if title_end_seconds is not None and title_end_seconds > float(start_seconds):
            clip.timeline_gap_anchor = seconds_to_frames(float(start_seconds), metadata.fps, allow_zero=True)
        if requested_duration_seconds is not None:
            clip.preserve_duration = True
        if is_intro_zoom_clip(path):
            clip.preserve_duration = True
            clip.video_track_override = INTRO_VIDEO_OVERLAY_TRACK_INDEX
            clip.audio_track_override = INTRO_AUDIO_OVERLAY_TRACK_INDEX
            clip.mute_audio = True
        hint_applied = apply_label_layout_rules(clip, metadata, normalized_label)
        if (
            path.suffix.lower() in VIDEO_EXTENSIONS
            and not clip.is_extract
            and not label_uses_title_layout(label)
            and not clip.audio_override_path
            and not overlay_keeps_source_audio(path, normalized_label)
        ):
            clip.mute_audio = True
        if clip.treat_as_overlay and not hint_applied:
            clip.scale_value = format_scale(OVERLAY_DEFAULT_SCALE)
        clips.append(clip)
    csv_clips, zoom_instructions = gather_comparison_tag_clips(metadata, comparison_csv)
    if csv_clips:
        clips.extend(csv_clips)
    clips.sort(key=lambda clip: clip.start_frames)
    events = collect_timeline_gap_events(clips)
    adjusted_events = apply_timeline_gap_offsets(clips, events)
    limit_insert_durations(clips, metadata.fps)
    clips = expand_title_assemblies(clips, metadata)
    attach_video_link_transition_blocks(clips)
    # Split-screen layout is disabled for rush clips (kept only for legacy reference).
    # mark_split_screen_clips(clips, metadata)
    assign_image_tracks(clips)
    return clips, adjusted_events, zoom_instructions


def attach_video_link_transition_blocks(clips: List[InsertClip]) -> None:
    extract_blocks: Dict[int, Tuple[str, int, int]] = {}
    for clip in clips:
        order = extract_insert_order(clip.path.name)
        if order is None:
            continue
        if not clip.is_extract:
            continue
        if classify_downloaded_video_link(clip.path, clip.label) != "extract":
            continue
        block_id = clip.insertion_block_id or f"extract:{clip.path.resolve()}:{clip.start_frames}"
        block_anchor = clip.insertion_block_anchor if clip.insertion_block_anchor is not None else clip.start_frames
        block_duration = clip.insertion_block_duration or clip.duration_frames
        extract_blocks[order] = (block_id, block_anchor, block_duration)
    for clip in clips:
        if not label_is_video_link_transition(clip.label):
            continue
        order = extract_insert_order(clip.path.name)
        if order is None:
            continue
        block = extract_blocks.get(order)
        if block is None:
            continue
        block_id, block_anchor, block_duration = block
        clip.intercalated_insert = True
        clip.insertion_block_id = block_id
        clip.insertion_block_anchor = block_anchor
        clip.insertion_block_duration = block_duration


def enforce_clip_cutoff(clips: List[InsertClip], cutoff_frame: Optional[int]) -> List[InsertClip]:
    if cutoff_frame is None or cutoff_frame <= 0:
        return clips
    trimmed: List[InsertClip] = []
    for clip in clips:
        if clip.start_frames >= cutoff_frame:
            continue
        max_duration = cutoff_frame - clip.start_frames
        if max_duration <= 0:
            continue
        if clip.duration_frames > max_duration:
            clip.duration_frames = max_duration
        trimmed.append(clip)
    return trimmed


def drop_inserts_past_rush_end(
    clips: List[InsertClip],
    rush_end_frames: int,
    *,
    tolerance_frames: int = 0,
) -> List[InsertClip]:
    """
    Remove overlays that would land at or past the rush timeline's end,
    OR whose end would extend past the rush end. A label/duration overlay
    that cannot display its full content is useless, so drop rather than
    trim. Extracts and intro zoom clips are preserved since they own
    their timeline anchoring or extend the rush itself.
    """
    if rush_end_frames <= 0:
        return clips
    cutoff = rush_end_frames + max(0, tolerance_frames)
    kept: List[InsertClip] = []
    for clip in clips:
        if clip.is_extract:
            kept.append(clip)
            continue
        if is_intro_zoom_clip(clip.path):
            kept.append(clip)
            continue
        if clip.start_frames >= cutoff:
            print(
                f"ℹ️  Dropping '{clip.path.name}' — start {clip.start_frames} "
                f"is past rush end {rush_end_frames}."
            )
            continue
        clip_end = clip.start_frames + max(0, clip.duration_frames)
        if clip_end > cutoff:
            print(
                f"ℹ️  Dropping '{clip.path.name}' — end {clip_end} would "
                f"overflow rush end {rush_end_frames}."
            )
            continue
        kept.append(clip)
    return kept


def compute_title_scale_for_width(clip: InsertClip, metadata: SequenceMetadata) -> str:
    width = clip.source_width or metadata.width
    if not width or width <= 0:
        return clip.scale_value
    desired_width = metadata.width * TITLE_WIDTH_RATIO
    scale_percent = (desired_width / width) * 100.0
    scale_percent = max(57.0, min(100.0, scale_percent))
    return format_scale(scale_percent)


def is_local_generated_title_video_clip(path: Path, label: Optional[str]) -> bool:
    return (
        path.suffix.lower() in VIDEO_EXTENSIONS
        and label_uses_title_layout(label)
        and "@" not in path.stem
    )


def compute_logo_scale(clip: InsertClip, metadata: SequenceMetadata) -> str:
    width = clip.source_width or metadata.width
    if not width or width <= 0:
        return format_scale(100.0)
    frame_width = metadata.width or width
    target_width = frame_width * LOGO_TARGET_WIDTH_RATIO
    scale_percent = (target_width / width) * 100.0
    scale_percent = max(LOGO_MIN_SCALE, min(LOGO_MAX_SCALE, scale_percent))
    return format_scale(scale_percent)


def apply_label_layout_rules(
    clip: InsertClip, metadata: SequenceMetadata, label: Optional[str]
) -> bool:
    scale_overridden = False
    if classify_downloaded_video_link(clip.path, label) is not None or (
        is_extract_clip(clip.path.name) and "@" in clip.path.stem
    ):
        clip.scale_value = compute_scale_value(
            clip.source_width, clip.source_height, metadata
        )
        scale_overridden = True
    if label:
        if is_noun_or_arrow_clip(clip.path):
            clip.treat_as_overlay = True
            clip.overlay_track_base = NOUN_ARROW_TRACK_BASE
        elif label_is_video_link_transition(label):
            clip.treat_as_overlay = True
            clip.preserve_duration = True
            clip.video_track_override = TRANSITION_VIDEO_TRACK_INDEX
            clip.audio_track_override = TRANSITION_AUDIO_TRACK_INDEX
            clip.scale_value = format_scale(100.0)
            scale_overridden = True
        elif label_matches(label, "image"):
            clip.treat_as_overlay = True
            clip.overlay_track_base = IMAGE_TRACK_BASE
        elif label_matches(label, "city") or label_matches(label, "country"):
            # 3D location inserts should preserve their requested trim window
            # instead of being shortened to the next insert boundary.
            clip.preserve_duration = True
            # Sit at the lowest insert track so every other insert renders on top,
            # while still staying above rush zooms (ZOOM_VIDEO_TRACK_INDEX = 2).
            clip.video_track_override = LOCATION_3D_VIDEO_TRACK_INDEX
        elif label_uses_title_layout(label):
            clip.treat_as_overlay = True
            clip.overlay_track_base = TITLE_TRACK_BASE
            if is_downloaded_article_title_clip(clip.path, label):
                clip.scale_value = format_scale(DOWNLOADED_ARTICLE_TITLE_SCALE)
            elif is_local_generated_title_video_clip(clip.path, label):
                clip.scale_value = format_scale(OVERLAY_DEFAULT_SCALE)
            else:
                clip.scale_value = compute_title_scale_for_width(clip, metadata)
            scale_overridden = True
        elif label_matches(label, "logo"):
            clip.treat_as_overlay = True
            clip.overlay_track_base = LOGO_TRACK_BASE
            clip.scale_value = compute_logo_scale(clip, metadata)
            scale_overridden = True

    hinted_scale = SCALE_HINTS.get(clip.path.name.lower())
    if hinted_scale is None:
        hinted_scale = SCALE_HINTS.get(strip_timestamp_prefix(clip.path.name.lower()))
    if hinted_scale is not None:
        clip.scale_value = format_scale(hinted_scale)
        scale_overridden = True
    return scale_overridden


def is_legacy_title_gif_clip(clip: InsertClip) -> bool:
    return clip.path.suffix.lower() == ".gif" and label_is_title(clip.label)


def is_single_title_mov_clip(clip: InsertClip) -> bool:
    return (
        clip.path.suffix.lower() in VIDEO_EXTENSIONS
        and label_is_title(clip.label)
        and read_insert_timing_asset_mode(clip.path) == SINGLE_TITLE_MOV_ASSET_MODE
    )


def build_title_assembly_base_clip(
    clip: InsertClip,
    metadata: SequenceMetadata,
    *,
    block_id: str,
) -> InsertClip:
    title_start_frames = seconds_plus_frames_to_frames(
        TITLE_TEXT_START_SECONDS,
        TITLE_TEXT_START_EXTRA_FRAMES,
        metadata.fps,
        allow_zero=True,
    )
    # clip.start_frames now points at the natural speech gap (end of the
    # previous HTML block). Open the rush gap exactly there: slide the MOV
    # back by the in-MOV fade-in so the title text becomes visible at the gap.
    original_start_frames = clip.start_frames
    assembly_start_frames = max(
        0,
        original_start_frames - title_start_frames - TITLE_ASSEMBLY_ADVANCE_FRAMES,
    )
    block_anchor = assembly_start_frames + title_start_frames
    return replace(
        clip,
        start_frames=assembly_start_frames,
        intercalated_insert=True,
        insertion_block_id=block_id,
        insertion_block_anchor=block_anchor,
        timeline_gap_anchor=block_anchor,
        timeline_gap_duration=0,
        video_track_override=TITLE_TRACK_BASE,
        overlay_track_base=TITLE_TRACK_BASE,
    )


def build_title_background_clip(title_clip: InsertClip, metadata: SequenceMetadata) -> Optional[InsertClip]:
    if not TITLE_BACKGROUND_PATH.exists():
        print(f"⚠️  Title background missing: {TITLE_BACKGROUND_PATH}")
        return None
    duration_seconds, width, height, has_alpha, _has_audio = probe_media_info(TITLE_BACKGROUND_PATH)
    duration_frames = seconds_to_frames(duration_seconds, metadata.fps)
    block_anchor = (
        title_clip.insertion_block_anchor
        if title_clip.insertion_block_anchor is not None
        else title_clip.start_frames
    )
    intro_overlap_frames = max(0, block_anchor - title_clip.start_frames)
    title_end_padding_frames = seconds_plus_frames_to_frames(
        TITLE_TEXT_END_PADDING_SECONDS,
        TITLE_TEXT_END_PADDING_EXTRA_FRAMES,
        metadata.fps,
        allow_zero=True,
    )
    insertion_block_duration = max(
        1,
        duration_frames - intro_overlap_frames - title_end_padding_frames - TITLE_ASSEMBLY_ADVANCE_FRAMES,
    )
    return InsertClip(
        path=TITLE_BACKGROUND_PATH,
        start_frames=title_clip.start_frames,
        source_in_frames=0,
        duration_frames=duration_frames,
        source_width=width,
        source_height=height,
        scale_value=format_scale(100.0),
        is_extract=False,
        is_image=False,
        treat_as_overlay=True,
        label="title_background",
        overlay_track_base=TITLE_BACKGROUND_TRACK_BASE,
        video_track_override=TITLE_BACKGROUND_TRACK_BASE,
        preserve_duration=True,
        audio_track_override=INTRO_AUDIO_TRACK_INDEX,
        mute_audio=False,
        timeline_gap_anchor=title_clip.timeline_gap_anchor,
        timeline_gap_duration=title_clip.timeline_gap_duration,
        intercalated_insert=True,
        insertion_block_id=title_clip.insertion_block_id,
        insertion_block_anchor=block_anchor,
        insertion_block_duration=insertion_block_duration,
    )


def build_single_title_mov_assembly(clip: InsertClip, metadata: SequenceMetadata) -> InsertClip:
    clip = build_title_assembly_base_clip(
        clip,
        metadata,
        block_id=f"title-mov:{clip.path.resolve()}:{clip.start_frames}",
    )
    background_clip = build_title_background_clip(clip, metadata)
    insertion_block_anchor = clip.insertion_block_anchor
    insertion_block_duration = clip.insertion_block_duration
    timeline_gap_anchor = clip.timeline_gap_anchor
    timeline_gap_duration = clip.timeline_gap_duration
    if background_clip is not None:
        insertion_block_anchor = background_clip.insertion_block_anchor
        insertion_block_duration = background_clip.insertion_block_duration
        timeline_gap_anchor = background_clip.timeline_gap_anchor
        timeline_gap_duration = background_clip.timeline_gap_duration
    return replace(
        clip,
        insertion_block_anchor=insertion_block_anchor,
        insertion_block_duration=insertion_block_duration,
        timeline_gap_anchor=timeline_gap_anchor,
        timeline_gap_duration=timeline_gap_duration,
        audio_track_override=INTRO_AUDIO_TRACK_INDEX,
        mute_audio=False,
        preserve_duration=True,
    )


def title_preroll_frames_for_clip(clip: InsertClip, metadata: SequenceMetadata) -> int:
    return seconds_plus_frames_to_frames(
        TITLE_PREVIOUS_RUSH_OVERLAP_SECONDS,
        TITLE_PREVIOUS_RUSH_OVERLAP_EXTRA_FRAMES,
        metadata.fps,
        allow_zero=True,
    )


def expand_title_assemblies(clips: List[InsertClip], metadata: SequenceMetadata) -> List[InsertClip]:
    expanded: List[InsertClip] = []
    title_start_frames = seconds_plus_frames_to_frames(
        TITLE_TEXT_START_SECONDS,
        TITLE_TEXT_START_EXTRA_FRAMES,
        metadata.fps,
        allow_zero=True,
    )
    title_end_padding_frames = seconds_plus_frames_to_frames(
        TITLE_TEXT_END_PADDING_SECONDS,
        TITLE_TEXT_END_PADDING_EXTRA_FRAMES,
        metadata.fps,
        allow_zero=True,
    )
    for clip in clips:
        if is_single_title_mov_clip(clip):
            clip = build_title_assembly_base_clip(
                clip,
                metadata,
                block_id=f"title-mov:{clip.path.resolve()}:{clip.start_frames}",
            )
            background_clip = build_title_background_clip(clip, metadata)
            if background_clip is not None:
                expanded.append(background_clip)
                clip = replace(
                    clip,
                    insertion_block_anchor=background_clip.insertion_block_anchor,
                    insertion_block_duration=background_clip.insertion_block_duration,
                    timeline_gap_anchor=background_clip.timeline_gap_anchor,
                    timeline_gap_duration=background_clip.timeline_gap_duration,
                )
            expanded.append(replace(
                clip,
                audio_track_override=INTRO_AUDIO_TRACK_INDEX,
                mute_audio=False,
                preserve_duration=True,
            ))
            continue
        if not is_legacy_title_gif_clip(clip):
            expanded.append(clip)
            continue
        clip = build_title_assembly_base_clip(
            clip,
            metadata,
            block_id=f"title:{clip.path.resolve()}:{clip.start_frames}",
        )
        background_clip = build_title_background_clip(clip, metadata)
        if background_clip is not None:
            expanded.append(background_clip)
        if background_clip is None:
            expanded.append(clip)
            continue
        clip = replace(
            clip,
            insertion_block_duration=background_clip.insertion_block_duration,
        )
        visible_window_frames = max(
            0,
            background_clip.duration_frames - title_start_frames - title_end_padding_frames,
        )
        clip = replace(
            clip,
            timeline_gap_duration=visible_window_frames,
        )
        consumed_frames = 0
        repeat_index = 0
        while consumed_frames < visible_window_frames:
            repeat_duration = min(clip.duration_frames, visible_window_frames - consumed_frames)
            if repeat_duration <= 0:
                break
            repeated_clip = replace(
                clip,
                start_frames=clip.start_frames + title_start_frames + consumed_frames,
                duration_frames=repeat_duration,
            )
            expanded.append(repeated_clip)
            consumed_frames += repeat_duration
            repeat_index += 1
    expanded.sort(
        key=lambda item: (item.start_frames, 0 if normalize_label(item.label) == "title_background" else 1)
    )
    return expanded


def mark_split_screen_clips(clips: List[InsertClip], metadata: SequenceMetadata) -> None:
    grouped: Dict[int, List[InsertClip]] = defaultdict(list)
    for clip in clips:
        grouped[clip.start_frames].append(clip)
    half_width = metadata.width / 2
    frame_center_x = metadata.width / 2
    frame_center_y = metadata.height / 2
    horizontal_offset = half_width * SPLIT_SCREEN_OFFSET_RATIO
    for start_frames, bucket in grouped.items():
        if len(bucket) != 1:
            continue
        clip = bucket[0]
        if clip.label or not clip.treat_as_overlay or clip.is_image:
            continue
        clip.needs_rush_split = True
        clip.scale_value = SPLIT_OVERLAY_SCALE
        clip.motion_center = (
            frame_center_x - horizontal_offset,
            frame_center_y,
        )


def collect_rush_clipitems(video_tracks: Iterable[ET.Element]) -> List[Tuple[int, int, ET.Element]]:
    rush_items: List[Tuple[int, int, ET.Element]] = []
    for track in video_tracks:
        for clipitem in track.findall("clipitem"):
            if not is_rush_clipitem(clipitem):
                continue
            start = parse_int(clipitem.findtext("start"), 0)
            end = parse_int(clipitem.findtext("end"), 0)
            rush_items.append((start, end, clipitem))
    return rush_items


def compute_main_track_bounds(video_tracks: Sequence[ET.Element]) -> Tuple[int, int]:
    if not video_tracks:
        return 0, 0
    first_track = video_tracks[0]
    max_start = 0
    max_end = 0
    for clipitem in first_track.findall("clipitem"):
        start = parse_int(clipitem.findtext("start"), 0)
        end = parse_int(clipitem.findtext("end"), 0)
        if start >= max_start:
            max_start = start
        if end >= max_end:
            max_end = end
    return max_start, max_end


def is_rush_clipitem(clipitem: ET.Element) -> bool:
    file_el = clipitem.find("file")
    if file_el is None:
        return False
    pathurl = file_el.findtext("pathurl")
    clip_path = path_from_url(pathurl)
    if clip_path is None:
        return False
    for rush_dir in RUSH_DIRECTORIES:
        try:
            if clip_path.is_relative_to(rush_dir):
                return True
        except AttributeError:
            if str(rush_dir) in str(clip_path):
                return True
    return False


def clipitem_media_path(clipitem: ET.Element) -> Optional[Path]:
    file_el = clipitem.find("file")
    if file_el is None:
        return None
    return path_from_url(file_el.findtext("pathurl"))


def compute_rush_peak_safe_gain_units(track: Optional[ET.Element]) -> str:
    if track is None:
        return "0"
    loudest_peak: Optional[float] = None
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        clip_path = clipitem_media_path(clipitem)
        if clip_path is None:
            continue
        peak_db = analyze_audio_peak_db(clip_path)
        if peak_db is None:
            continue
        if loudest_peak is None or peak_db > loudest_peak:
            loudest_peak = peak_db
    return compute_peak_safe_gain_units(loudest_peak)


def apply_gain_to_track_clipitems(
    track: Optional[ET.Element],
    *,
    gain_level: str,
    predicate,
) -> None:
    if track is None:
        return
    for clipitem in track.findall("clipitem"):
        if predicate(clipitem):
            set_clipitem_audio_gain(clipitem, gain_level)


def _find_rush_overlap(track: Optional[ET.Element], frame: int) -> Optional[Tuple[int, int, ET.Element]]:
    if track is None:
        return None
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        start = parse_int(clipitem.findtext("start"), 0)
        end = parse_int(clipitem.findtext("end"), 0)
        if start < frame < end:
            return start, end, clipitem
    return None


def _shift_rush_track(track: Optional[ET.Element], start_frame: int, offset_frames: int) -> None:
    if track is None or offset_frames <= 0:
        return
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        clip_start = parse_int(clipitem.findtext("start"), 0)
        if clip_start < start_frame:
            continue
        clip_end = parse_int(clipitem.findtext("end"), 0)
        start_el = clipitem.find("start")
        end_el = clipitem.find("end")
        if start_el is not None:
            start_el.text = str(clip_start + offset_frames)
        if end_el is not None:
            end_el.text = str(clip_end + offset_frames)


def _clone_clipitem(clipitem: ET.Element, suffix: str) -> ET.Element:
    cloned = copy.deepcopy(clipitem)
    clip_id = cloned.get("id")
    if clip_id:
        cloned.set("id", f"{clip_id}-{suffix}")
    return cloned


def _set_clipitem_timing(
    clipitem: ET.Element,
    *,
    start: int,
    end: int,
    in_frame: int,
    out_frame: int,
) -> None:
    duration = max(0, end - start)
    for tag, value in (
        ("start", start),
        ("end", end),
        ("in", in_frame),
        ("out", out_frame),
        ("duration", duration),
    ):
        el = clipitem.find(tag)
        if el is None:
            el = ET.SubElement(clipitem, tag)
        el.text = str(value)


def _split_rush_clip_at_boundary(track: Optional[ET.Element], frame: int) -> None:
    if track is None:
        return
    for clipitem in list(track.findall("clipitem")):
        if not is_rush_clipitem(clipitem):
            continue
        clip_start = parse_int(clipitem.findtext("start"), 0)
        clip_end = parse_int(clipitem.findtext("end"), 0)
        if not (clip_start < frame < clip_end):
            continue
        clip_in = parse_int(clipitem.findtext("in"), 0)
        clip_out = parse_int(clipitem.findtext("out"), clip_in + max(0, clip_end - clip_start))
        head_duration = frame - clip_start
        tail_duration = clip_end - frame
        if head_duration <= 0 or tail_duration <= 0:
            return
        _set_clipitem_timing(
            clipitem,
            start=clip_start,
            end=frame,
            in_frame=clip_in,
            out_frame=clip_in + head_duration,
        )
        tail = _clone_clipitem(clipitem, f"tail-{frame}")
        # The general rush shift pass runs immediately after the split; keep the
        # tail at the cut point here so it is shifted exactly once.
        tail_start = frame
        _set_clipitem_timing(
            tail,
            start=tail_start,
            end=tail_start + tail_duration,
            in_frame=clip_in + head_duration,
            out_frame=clip_out,
        )
        track.insert(list(track).index(clipitem) + 1, tail)
        return


def _split_rush_clip_at_frame(track: Optional[ET.Element], frame: int, offset_frames: int) -> None:
    if track is None or offset_frames <= 0:
        return
    _split_rush_clip_at_boundary(track, frame)


def find_previous_rush_clip(
    track: Optional[ET.Element], frame: int
) -> Optional[ET.Element]:
    if track is None:
        return None
    prior: Optional[ET.Element] = None
    prior_end = -1
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        end = parse_int(clipitem.findtext("end"), 0)
        if end <= frame and end > prior_end:
            prior = clipitem
            prior_end = end
    return prior


def find_rush_clip_containing_frame(
    track: Optional[ET.Element], frame: int
) -> Optional[ET.Element]:
    if track is None:
        return None
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        start = parse_int(clipitem.findtext("start"), 0)
        end = parse_int(clipitem.findtext("end"), 0)
        if start < frame < end:
            return clipitem
    return None


def prepare_zoom_replacement_targets(
    rush_video_track: Optional[ET.Element],
    replacements: Sequence[ZoomReplacement],
) -> None:
    if rush_video_track is None or not replacements:
        return
    boundaries = sorted(
        {
            boundary
            for replacement in replacements
            for boundary in (replacement.timeline_start_frames, replacement.timeline_end_frames)
            if boundary > 0
        }
    )
    for boundary in boundaries:
        _split_rush_clip_at_boundary(rush_video_track, boundary)

    rush_clipitems = list(rush_video_track.findall("clipitem"))
    for replacement in replacements:
        matched = 0
        for clipitem in rush_clipitems:
            if not is_rush_clipitem(clipitem):
                continue
            start = parse_int(clipitem.findtext("start"), 0)
            end = parse_int(clipitem.findtext("end"), 0)
            if end <= replacement.timeline_start_frames or start >= replacement.timeline_end_frames:
                continue
            clipitem.set(ZOOM_REPLACEMENT_ATTR, replacement.replacement_id)
            matched += 1
        if matched == 0:
            print(
                "⚠️  No rush V1 segment matched zoom replacement "
                f"{replacement.zoom_code or replacement.row_id} "
                f"({replacement.timeline_start_frames}-{replacement.timeline_end_frames})."
            )


OUTRO_DIP_FILE_ID = "source-file-outro-dip"


def _latest_outro_dip_file(insert_dir: Path) -> Optional[Path]:
    if not insert_dir.exists():
        return None
    candidates = sorted(
        list(insert_dir.glob("*_outro_dip.mp4")) + list((insert_dir / "zoom").glob("*_outro_dip.mp4") if (insert_dir / "zoom").is_dir() else []),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def _find_last_rush_clipitem(track: Optional[ET.Element]) -> Optional[ET.Element]:
    if track is None:
        return None
    best: Optional[Tuple[int, ET.Element]] = None
    for clipitem in track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        end = parse_int(clipitem.findtext("end"), -1)
        if end < 0:
            continue
        if best is None or end > best[0]:
            best = (end, clipitem)
    return best[1] if best else None


def _find_audio_clipitem_matching(
    audio_track: Optional[ET.Element], video_clipitem: ET.Element
) -> Optional[ET.Element]:
    if audio_track is None:
        return None
    target_start = parse_int(video_clipitem.findtext("start"), -1)
    target_end = parse_int(video_clipitem.findtext("end"), -1)
    if target_start < 0 or target_end <= target_start:
        return None
    best: Optional[Tuple[int, ET.Element]] = None
    for clipitem in audio_track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        start = parse_int(clipitem.findtext("start"), -1)
        end = parse_int(clipitem.findtext("end"), -1)
        if start < 0 or end <= start:
            continue
        overlap = max(0, min(end, target_end) - max(start, target_start))
        if overlap <= 0:
            continue
        if best is None or overlap > best[0]:
            best = (overlap, clipitem)
    return best[1] if best else None


def swap_outro_dip_onto_rush(
    rush_video_track: Optional[ET.Element],
    rush_audio_track: Optional[ET.Element],
    insert_dir: Path,
    metadata: SequenceMetadata,
) -> None:
    """
    Replace the last rush V1/A1 clipitem's source file with the staged
    outro_dip.mp4 (tail-aligned so the dip lands on the final frame). The
    timeline start/end and clipitem duration are preserved; only the file
    reference and in/out source offsets are rewritten.
    """
    outro_path = _latest_outro_dip_file(insert_dir)
    if outro_path is None:
        return
    last_video = _find_last_rush_clipitem(rush_video_track)
    if last_video is None:
        print(f"⚠️  Outro dip {outro_path.name} staged but no rush V1 clipitem found; skipping swap.")
        return

    duration_seconds, width, height, _has_alpha, has_audio = probe_media_info(outro_path)
    outro_natural_frames = seconds_to_frames(duration_seconds, metadata.fps)
    if outro_natural_frames <= 0:
        print(f"⚠️  Outro dip {outro_path.name} has zero probed duration; skipping swap.")
        return

    timeline_start = parse_int(last_video.findtext("start"), 0)
    timeline_end   = parse_int(last_video.findtext("end"), 0)
    timeline_dur   = max(0, timeline_end - timeline_start)
    if timeline_dur <= 0:
        print(f"⚠️  Last rush V1 clipitem has non-positive timeline duration; skipping outro swap.")
        return

    usable = min(timeline_dur, outro_natural_frames)
    source_in  = max(0, outro_natural_frames - usable)
    source_out = outro_natural_frames
    new_timeline_end = timeline_start + usable

    pathurl = outro_path.resolve().as_uri()
    file_name = outro_path.name

    def _rewrite_file_element(clipitem: ET.Element, include_media: bool) -> None:
        file_el = clipitem.find("file")
        if file_el is None:
            file_el = ET.SubElement(clipitem, "file")
        file_el.clear()
        file_el.set("id", OUTRO_DIP_FILE_ID)
        if not include_media:
            return
        ET.SubElement(file_el, "duration").text = str(outro_natural_frames)
        file_el.append(build_rate_node(metadata.fps))
        ET.SubElement(file_el, "name").text = file_name
        ET.SubElement(file_el, "pathurl").text = pathurl
        media_el = ET.SubElement(file_el, "media")
        video_el = ET.SubElement(media_el, "video")
        ET.SubElement(video_el, "duration").text = str(outro_natural_frames)
        sample = ET.SubElement(video_el, "samplecharacteristics")
        ET.SubElement(sample, "width").text = str(width or metadata.width)
        ET.SubElement(sample, "height").text = str(height or metadata.height)
        ET.SubElement(sample, "anamorphic").text = "FALSE"
        ET.SubElement(sample, "pixelaspectratio").text = metadata.pixel_aspect
        audio_el = ET.SubElement(media_el, "audio")
        audio_sample = ET.SubElement(audio_el, "samplecharacteristics")
        ET.SubElement(audio_sample, "samplerate").text = "48000"
        ET.SubElement(audio_sample, "depth").text = "16"
        ET.SubElement(audio_sample, "channelcount").text = "2"

    def _retune_timing(clipitem: ET.Element) -> None:
        if (el := clipitem.find("start")) is not None:
            el.text = str(timeline_start)
        if (el := clipitem.find("end")) is not None:
            el.text = str(new_timeline_end)
        if (el := clipitem.find("in")) is not None:
            el.text = str(source_in)
        if (el := clipitem.find("out")) is not None:
            el.text = str(source_out)
        if (el := clipitem.find("duration")) is not None:
            el.text = str(usable)

    _rewrite_file_element(last_video, include_media=True)
    _retune_timing(last_video)

    last_audio = _find_audio_clipitem_matching(rush_audio_track, last_video)
    if last_audio is not None:
        if has_audio:
            _rewrite_file_element(last_audio, include_media=False)
            _retune_timing(last_audio)
        else:
            print(f"ℹ️  Outro dip {outro_path.name} has no audio; keeping original rush audio on A1.")
    else:
        print("⚠️  No matching rush A1 clipitem found for outro dip swap (video swapped without audio).")

    print(
        f"ℹ️  Outro dip swapped onto rush: {file_name} | "
        f"timeline {timeline_start}-{new_timeline_end} "
        f"(src {source_in}-{source_out} / {outro_natural_frames} frames)"
    )


def update_clipitem_video_source(
    clipitem: ET.Element,
    *,
    source_path: Path,
    metadata: SequenceMetadata,
    duration_frames: int,
) -> None:
    duration_seconds, width, height, _has_alpha, _has_audio = probe_media_info(source_path)
    media_duration_frames = seconds_to_frames(duration_seconds, metadata.fps)
    file_el = clipitem.find("file")
    file_id = file_el.get("id") if file_el is not None else None
    if file_el is None:
        file_el = ET.SubElement(clipitem, "file")
    file_el.clear()
    if file_id:
        file_el.set("id", file_id)
    ET.SubElement(file_el, "name").text = source_path.name
    ET.SubElement(file_el, "pathurl").text = source_path.resolve().as_uri()
    file_el.append(build_rate_node(metadata.fps))
    ET.SubElement(file_el, "duration").text = str(max(duration_frames, media_duration_frames))
    media_el = ET.SubElement(file_el, "media")
    video_el = ET.SubElement(media_el, "video")
    ET.SubElement(video_el, "duration").text = str(max(duration_frames, media_duration_frames))
    sample = ET.SubElement(video_el, "samplecharacteristics")
    ET.SubElement(sample, "width").text = str(width or metadata.width)
    ET.SubElement(sample, "height").text = str(height or metadata.height)
    ET.SubElement(sample, "anamorphic").text = "FALSE"
    ET.SubElement(sample, "pixelaspectratio").text = metadata.pixel_aspect


def apply_zoom_replacements_to_rush_track(
    rush_video_track: Optional[ET.Element],
    replacements: Sequence[ZoomReplacement],
    metadata: SequenceMetadata,
) -> None:
    if rush_video_track is None or not replacements:
        return
    replacement_map = {replacement.replacement_id: replacement for replacement in replacements}
    applied = 0
    for clipitem in rush_video_track.findall("clipitem"):
        replacement_id = clipitem.get(ZOOM_REPLACEMENT_ATTR)
        if not replacement_id:
            continue
        replacement = replacement_map.get(replacement_id)
        if replacement is None:
            clipitem.attrib.pop(ZOOM_REPLACEMENT_ATTR, None)
            continue
        clip_start = parse_int(clipitem.findtext("start"), 0)
        clip_end = parse_int(clipitem.findtext("end"), 0)
        rush_in = parse_int(clipitem.findtext("in"), 0)
        rush_out = parse_int(clipitem.findtext("out"), rush_in + max(0, clip_end - clip_start))
        replacement_in = max(0, rush_in - replacement.source_start_frames)
        replacement_out = max(replacement_in, rush_out - replacement.source_start_frames)
        _set_clipitem_timing(
            clipitem,
            start=clip_start,
            end=clip_end,
            in_frame=replacement_in,
            out_frame=replacement_out,
        )
        name_el = clipitem.find("name")
        if name_el is None:
            name_el = ET.SubElement(clipitem, "name")
        name_el.text = replacement.output_path.name
        update_clipitem_video_source(
            clipitem,
            source_path=replacement.output_path,
            metadata=metadata,
            duration_frames=max(0, replacement.source_end_frames - replacement.source_start_frames),
        )
        replace_basic_motion_filter(clipitem, scale_value="100")
        clipitem.attrib.pop(ZOOM_REPLACEMENT_ATTR, None)
        applied += 1
    if applied:
        print(f"Applied {applied} V1 rush zoom replacement clipitem(s).")


def build_zoom_overlay_clips(
    rush_video_track: Optional[ET.Element],
    replacements: Sequence[ZoomReplacement],
    metadata: SequenceMetadata,
) -> List[InsertClip]:
    if rush_video_track is None or not replacements:
        return []

    rush_segments: List[Tuple[int, int, int, int]] = []
    for clipitem in rush_video_track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        clip_start = parse_int(clipitem.findtext("start"), 0)
        clip_end = parse_int(clipitem.findtext("end"), 0)
        rush_in = parse_int(clipitem.findtext("in"), 0)
        rush_out = parse_int(clipitem.findtext("out"), rush_in + max(0, clip_end - clip_start))
        if clip_end <= clip_start or rush_out <= rush_in:
            continue
        rush_segments.append((clip_start, clip_end, rush_in, rush_out))

    overlays: List[InsertClip] = []
    for replacement in replacements:
        duration_seconds, clip_w, clip_h, _, _ = probe_media_info(replacement.output_path)
        zoom_scale_value = compute_fill_frame_scale(metadata.width, metadata.height, clip_w, clip_h)
        media_duration_frames = seconds_to_frames(duration_seconds, metadata.fps)
        manifest_duration_frames = max(
            0, replacement.source_end_frames - replacement.source_start_frames
        )
        max_source_frame = (
            min(media_duration_frames, manifest_duration_frames)
            if media_duration_frames > 0
            else manifest_duration_frames
        )
        if max_source_frame <= 0:
            continue

        is_shift = replacement.zoom_code == "INSERT_SHIFT"

        if is_shift:
            # INSERT_SHIFT zoom content is timeline-aligned: place using timeline_start_frames
            # directly instead of mapping source frames through rush segments.
            tl_start = replacement.timeline_start_frames
            tl_end = replacement.timeline_end_frames
            source_in_frames = 0
            source_out_frames = min(media_duration_frames, tl_end - tl_start)
            if tl_end <= tl_start or source_out_frames <= 0:
                continue
            if tl_start < ZOOM_SHIFT_MIN_TIMELINE_START_FRAMES:
                continue
            if not (0 <= source_in_frames < source_out_frames <= max_source_frame):
                print(
                    "⚠️  Skipping invalid INSERT_SHIFT span "
                    f"{replacement.output_path.name}: "
                    f"source {source_in_frames}-{source_out_frames} exceeds "
                    f"available 0-{max_source_frame}."
                )
                continue
            overlays.append(
                InsertClip(
                    path=replacement.output_path,
                    start_frames=tl_start,
                    source_in_frames=source_in_frames,
                    duration_frames=tl_end - tl_start,
                    scale_value=zoom_scale_value,
                    is_extract=False,
                    is_image=False,
                    treat_as_overlay=True,
                    label=f"zoom-{replacement.zoom_code.lower()}",
                    motion_center=None,
                    overlay_track_base=None,
                    needs_rush_split=False,
                    video_track_override=ZOOM_SHIFT_VIDEO_TRACK_INDEX,
                    preserve_duration=True,
                    mute_audio=True,
                )
            )
        else:
            # Face zoom: map source frames through rush segments (handles cuts correctly)
            for clip_start, _clip_end, rush_in, rush_out in rush_segments:
                source_overlap_start = max(rush_in, replacement.source_start_frames)
                source_overlap_end = min(rush_out, replacement.source_end_frames)
                if source_overlap_end <= source_overlap_start:
                    continue
                timeline_start = clip_start + (source_overlap_start - rush_in)
                timeline_end = clip_start + (source_overlap_end - rush_in)
                source_in_frames = source_overlap_start - replacement.source_start_frames
                source_out_frames = source_overlap_end - replacement.source_start_frames
                if not (0 <= source_in_frames < source_out_frames <= max_source_frame):
                    print(
                        "⚠️  Skipping invalid zoom overlay span "
                        f"{replacement.output_path.name}: "
                        f"source {source_in_frames}-{source_out_frames} exceeds "
                        f"available 0-{max_source_frame}."
                    )
                    continue
                overlays.append(
                    InsertClip(
                        path=replacement.output_path,
                        start_frames=timeline_start,
                        source_in_frames=source_in_frames,
                        duration_frames=timeline_end - timeline_start,
                        scale_value=zoom_scale_value,
                        is_extract=False,
                        is_image=False,
                        treat_as_overlay=True,
                        label=f"zoom-{replacement.zoom_code.lower()}",
                        motion_center=None,
                        overlay_track_base=None,
                        needs_rush_split=False,
                        video_track_override=ZOOM_VIDEO_TRACK_INDEX,
                        preserve_duration=True,
                        mute_audio=True,
                    )
                )

    overlays.sort(key=lambda clip: (clip.start_frames, clip.source_in_frames, clip.path.name))
    return overlays


def choose_title_gap_anchor(
    anchor_frame: int,
    *,
    title_start_frame: int,
    original_rush_ranges: Sequence[Tuple[int, int]],
) -> int:
    if not original_rush_ranges:
        return anchor_frame
    previous_start: Optional[int] = None
    for start, end in original_rush_ranges:
        if start <= anchor_frame < end:
            if start < title_start_frame:
                return end
            return start
        if start <= anchor_frame:
            previous_start = start
    if previous_start is not None:
        return previous_start
    return original_rush_ranges[0][0]


def previous_rush_boundary_at_or_before(
    frame: int,
    *,
    original_rush_ranges: Sequence[Tuple[int, int]],
) -> int:
    boundaries = sorted({point for start, end in original_rush_ranges for point in (start, end)})
    if not boundaries:
        return frame
    candidate = boundaries[0]
    for point in boundaries:
        if point > frame:
            break
        candidate = point
    return candidate


def next_rush_boundary_at_or_after(
    frame: int,
    *,
    original_rush_ranges: Sequence[Tuple[int, int]],
) -> int:
    boundaries = sorted({point for start, end in original_rush_ranges for point in (start, end)})
    if not boundaries:
        return frame
    for point in boundaries:
        if point >= frame:
            return point
    return boundaries[-1]


def nearest_rush_boundary(
    frame: int,
    *,
    original_rush_ranges: Sequence[Tuple[int, int]],
    minimum: Optional[int] = None,
) -> int:
    boundaries = sorted({point for start, end in original_rush_ranges for point in (start, end)})
    if minimum is not None:
        boundaries = [point for point in boundaries if point >= minimum]
    if not boundaries:
        return frame if minimum is None else max(frame, minimum)
    best = boundaries[0]
    best_distance = abs(best - frame)
    for point in boundaries[1:]:
        distance = abs(point - frame)
        if distance < best_distance:
            best = point
            best_distance = distance
            continue
        if distance == best_distance and point < best:
            best = point
    return best


def title_gap_anchor_for_clip(
    clip: InsertClip,
    *,
    gap_anchor: int,
    original_rush_ranges: Sequence[Tuple[int, int]],
) -> int:
    return nearest_rush_boundary(
        gap_anchor,
        original_rush_ranges=original_rush_ranges,
    )


def title_gap_end_for_clip(
    clip: InsertClip,
    *,
    gap_start: int,
    original_rush_ranges: Sequence[Tuple[int, int]],
    desired_end: Optional[int] = None,
) -> int:
    desired_end = desired_end if desired_end is not None else gap_start + max(0, clip.timeline_gap_duration)
    snapped_end = nearest_rush_boundary(
        desired_end,
        original_rush_ranges=original_rush_ranges,
        minimum=gap_start,
    )
    return max(gap_start, snapped_end)


def extend_rush_clipitem_end(clipitem: Optional[ET.Element], extension_frames: int) -> None:
    if clipitem is None or extension_frames <= 0:
        return
    start = parse_int(clipitem.findtext("start"), 0)
    end_el = clipitem.find("end")
    end = parse_int(end_el.text if end_el is not None else None, 0)
    new_end = end + extension_frames
    if end_el is not None:
        end_el.text = str(new_end)
    duration_el = clipitem.find("duration")
    new_duration = max(0, new_end - start)
    if duration_el is not None:
        duration_el.text = str(new_duration)
    out_el = clipitem.find("out")
    in_frames = parse_int(clipitem.findtext("in"), 0)
    if out_el is not None:
        out_el.text = str(in_frames + new_duration)


def apply_timeline_gap_events_to_rush_tracks(
    events: Sequence[Tuple[int, int]],
    rush_video_track: Optional[ET.Element],
    rush_audio_track: Optional[ET.Element],
) -> None:
    if not events:
        return
    for frame, duration in events:
        if duration <= 0:
            continue
        extend_rush_clipitem_end(find_previous_rush_clip(rush_video_track, frame), duration)
        _shift_rush_track(rush_video_track, frame, duration)
        _shift_rush_track(rush_audio_track, frame, duration)


def adjust_extract_timeline(
    insert_clips: List[InsertClip],
    rush_video_track: Optional[ET.Element],
    rush_audio_track: Optional[ET.Element],
    fps: int,
) -> int:
    """
    Re-time extract clips so their start frames account for the growing timeline and shift
    rush tracks to insert a gap underneath each extract. Returns the total number of frames
    added to the rush timeline.
    """
    original_rush_ranges: List[Tuple[int, int]] = []
    if rush_video_track is not None:
        for clipitem in rush_video_track.findall("clipitem"):
            if not is_rush_clipitem(clipitem):
                continue
            original_rush_ranges.append(
                (
                    parse_int(clipitem.findtext("start"), 0),
                    parse_int(clipitem.findtext("end"), 0),
                )
            )
    total_shift = 0
    block_shifts: Dict[str, int] = {}
    for clip in insert_clips:
        if not clip.intercalated_insert:
            clip.start_frames += total_shift
            continue
        block_id = clip.insertion_block_id or f"inline:{clip.path.resolve()}:{clip.start_frames}"
        if block_id in block_shifts:
            clip.start_frames += block_shifts[block_id]
            continue
        block_anchor = clip.insertion_block_anchor if clip.insertion_block_anchor is not None else clip.start_frames
        block_duration = clip.insertion_block_duration or clip.duration_frames
        gap_anchor = clip.timeline_gap_anchor if clip.timeline_gap_anchor is not None else block_anchor
        normalized_label = normalize_label(clip.label)
        if normalized_label in {"title", "title_background"}:
            gap_anchor = title_gap_anchor_for_clip(
                clip,
                gap_anchor=gap_anchor,
                original_rush_ranges=original_rush_ranges,
            )
        block_shift = total_shift
        block_shifts[block_id] = block_shift
        clip.start_frames += block_shift
        if normalized_label in {"title", "title_background"}:
            # Titles are overlays over ongoing speech — never interrupt the rush track.
            block_duration_for_gap = 0
        else:
            block_duration_for_gap = block_duration
        insertion_frame = gap_anchor + total_shift
        _split_rush_clip_at_frame(rush_video_track, insertion_frame, block_duration_for_gap)
        _split_rush_clip_at_frame(rush_audio_track, insertion_frame, block_duration_for_gap)
        if rush_video_track is not None:
            _shift_rush_track(rush_video_track, insertion_frame, block_duration_for_gap)
        if rush_audio_track is not None:
            _shift_rush_track(rush_audio_track, insertion_frame, block_duration_for_gap)
        total_shift += block_duration_for_gap
    return total_shift


def align_overlays_to_rush(
    insert_clips: List[InsertClip],
    rush_video_track: Optional[ET.Element],
    fps: int,
) -> None:
    """
    Delay titre/logo/image overlays so they start on the next rush boundary (end of the
    underlying clip / beginning of the next one), with a small offset to land slightly later.
    """
    if rush_video_track is None:
        return
    rush_segments: List[Tuple[int, int]] = []
    for clipitem in rush_video_track.findall("clipitem"):
        if not is_rush_clipitem(clipitem):
            continue
        start = parse_int(clipitem.findtext("start"), 0)
        end = parse_int(clipitem.findtext("end"), 0)
        rush_segments.append((start, end))
    if not rush_segments:
        return
    rush_segments.sort(key=lambda seg: seg[0])
    offset_frames = max(
        1, seconds_to_frames(OVERLAY_ALIGNMENT_OFFSET_SECONDS, fps, allow_zero=True)
    )
    boundaries = [end for _, end in rush_segments]
    for clip in insert_clips:
        if not clip.treat_as_overlay or clip.intercalated_insert:
            continue
        if has_precise_timestamp_prefix(clip.path.name):
            continue
        original = clip.start_frames
        target_boundary = None
        for boundary in boundaries:
            if boundary >= original:
                target_boundary = boundary
                break
        if target_boundary is None:
            continue
        clip.start_frames = max(original, target_boundary + offset_frames)


def delay_clips_until_extracts_finish(insert_clips: List[InsertClip], fps: int) -> None:
    """
    Ensure no insert starts before the current extract has finished.
    """
    guard_until: Optional[int] = None
    offset_frames = max(
        1, seconds_to_frames(OVERLAY_ALIGNMENT_OFFSET_SECONDS, fps, allow_zero=True)
    )
    block_ends: Dict[str, int] = {}
    for clip in insert_clips:
        if not clip.intercalated_insert or not clip.insertion_block_id:
            continue
        block_end = clip.start_frames + (clip.insertion_block_duration or clip.duration_frames)
        current = block_ends.get(clip.insertion_block_id)
        if current is None or block_end < current:
            block_ends[clip.insertion_block_id] = block_end
    for clip in insert_clips:
        if guard_until is not None and clip.start_frames >= guard_until:
            guard_until = None
        if clip.intercalated_insert:
            block_end = block_ends.get(
                clip.insertion_block_id,
                clip.start_frames + (clip.insertion_block_duration or clip.duration_frames),
            )
            guard_until = max(guard_until or 0, block_end + offset_frames)
            continue
        if guard_until is not None and clip.start_frames < guard_until:
            clip.start_frames = guard_until

def _trim_clipitem_to_end(clipitem: ET.Element, cutoff_frame: int) -> bool:
    start = parse_int(clipitem.findtext("start"), 0)
    end = parse_int(clipitem.findtext("end"), 0)
    if end <= cutoff_frame:
        return False
    if start >= cutoff_frame:
        return True
    new_duration = max(0, cutoff_frame - start)
    if new_duration <= 0:
        return True
    end_el = clipitem.find("end")
    if end_el is not None:
        end_el.text = str(start + new_duration)
    duration_el = clipitem.find("duration")
    if duration_el is not None:
        duration_el.text = str(new_duration)
    source_in = parse_int(clipitem.findtext("in"), 0)
    out_el = clipitem.find("out")
    if out_el is not None:
        out_el.text = str(source_in + new_duration)
    file_el = clipitem.find("file")
    if file_el is not None:
        file_duration = file_el.find("duration")
        if file_duration is not None:
            file_duration.text = str(new_duration)
        media_el = file_el.find("media")
        if media_el is not None:
            for tag in ("video", "audio"):
                media_child = media_el.find(tag)
                if media_child is None:
                    continue
                media_dur = media_child.find("duration")
                if media_dur is not None:
                    media_dur.text = str(new_duration)
    return False


def trim_existing_tracks(
    tracks: Sequence[ET.Element], cutoff_frame: int, allowed_names: Optional[Sequence[str]] = None
) -> None:
    allowed = {name.lower() for name in (allowed_names or [])}
    for track in tracks:
        clipitems = list(track.findall("clipitem"))
        for clipitem in clipitems:
            name = (clipitem.findtext("name") or "").lower()
            if name in allowed:
                continue
            remove_entire = _trim_clipitem_to_end(clipitem, cutoff_frame)
            if remove_entire:
                track.remove(clipitem)


def apply_split_screen_to_rush(
    video_tracks: Iterable[ET.Element],
    metadata: SequenceMetadata,
    split_clips: Iterable[InsertClip],
) -> None:
    rush_items = collect_rush_clipitems(video_tracks)
    if not rush_items:
        return
    half_width = metadata.width / 2
    frame_center_x = metadata.width / 2
    frame_center_y = metadata.height / 2
    horizontal_offset = half_width * SPLIT_SCREEN_OFFSET_RATIO
    modified_ids: set[str] = set()
    for clip in split_clips:
        target = None
        for start, end, clipitem in rush_items:
            if start <= clip.start_frames < end:
                target = clipitem
                break
        if target is None:
            print(
                f"⚠️  Unable to locate rush clip covering frame {clip.start_frames} for split layout."
            )
            continue
        clip_id = target.get("id")
        if clip_id in modified_ids:
            continue
        filter_el = ensure_basic_motion_filter(target, default_scale="100")
        current_scale = get_motion_scale(filter_el, default="100")
        set_motion_scale(filter_el, multiply_scale(current_scale, 2.0))
        set_motion_center(
            filter_el,
            (
                frame_center_x + horizontal_offset,
                frame_center_y,
            ),
        )
        if clip_id:
            modified_ids.add(clip_id)


def build_audio_spec(
    path: Path,
    metadata: SequenceMetadata,
    start_frames: int,
    *,
    source_in_frames: int = 0,
    gain_level: Optional[str] = None,
    max_duration_frames: Optional[int] = None,
    fade_in_frames: int = 0,
    fade_out_frames: int = 0,
) -> Optional[AudioClipSpec]:
    if not path.exists():
        print(f"⚠️  Audio source missing: {path}")
        return None
    duration_seconds, _, _, _, _ = probe_media_info(path)
    total_duration_frames = seconds_to_frames(duration_seconds, metadata.fps)
    source_in_frames = max(0, source_in_frames)
    if source_in_frames >= total_duration_frames:
        return None
    duration_frames = max(0, total_duration_frames - source_in_frames)
    if max_duration_frames is not None:
        duration_frames = min(duration_frames, max_duration_frames)
    if duration_frames <= 0:
        return None
    audio_sample_rate, audio_channels = probe_audio_stream_info(path)
    return AudioClipSpec(
        path=path,
        start_frames=start_frames,
        duration_frames=duration_frames,
        audio_sample_rate=audio_sample_rate,
        audio_channels=audio_channels,
        gain_level=gain_level,
        source_in_frames=source_in_frames,
        fade_in_frames=max(0, min(fade_in_frames, duration_frames)),
        fade_out_frames=max(0, min(fade_out_frames, duration_frames)),
    )


def should_include_overlay_audio(clip: InsertClip) -> bool:
    if clip.mute_audio:
        return False
    if clip.audio_override_path:
        return True
    if clip.is_image:
        return False
    try:
        return media_has_audio(clip.path)
    except FileNotFoundError:
        return False


def resolve_insert_audio_track_index(
    clip: InsertClip,
    *,
    video_track_index: int,
) -> int:
    """
    Mirror insert audio to the resolved video lane unless the clip belongs to a
    dedicated audio lane such as extracts or intro overlays.
    """
    if clip.audio_track_override is not None:
        return clip.audio_track_override
    if clip.is_extract:
        return EXTRACT_AUDIO_TRACK_INDEX
    return video_track_index


def is_highlight_quote_clip(clip: InsertClip) -> bool:
    return "_QH" in clip.path.stem.upper()


def collect_overlay_sound_cues(clips: Sequence[InsertClip], sound_duration_frames: int) -> List[int]:
    if sound_duration_frames <= 0:
        return []
    frames: List[int] = []
    for clip in clips:
        if not is_highlight_quote_clip(clip):
            continue
        frames.append(clip.start_frames)
        end_frame = clip.start_frames + max(0, clip.duration_frames - sound_duration_frames)
        frames.append(end_frame)
    return sorted(frames)


def build_external_video_clip(
    path: Path, metadata: SequenceMetadata, start_frames: int
) -> Optional[InsertClip]:
    if not path.exists():
        print(f"⚠️  Video source missing: {path}")
        return None
    duration_seconds, width, height, has_alpha, _has_audio = probe_media_info(path)
    duration_frames = seconds_to_frames(duration_seconds, metadata.fps)
    return InsertClip(
        path=path,
        start_frames=start_frames,
        source_in_frames=0,
        duration_frames=duration_frames,
        source_width=width,
        source_height=height,
        scale_value=compute_scale_value(width, height, metadata),
        is_extract=False,
        is_image=is_image_clip(path),
        treat_as_overlay=False,
        label=None,
        motion_center=None,
        overlay_track_base=None,
        needs_rush_split=False,
    )



def limit_insert_durations(clips: List[InsertClip], fps: int) -> None:
    """
    Shrink non-extract clips so they stop at the next insert/extract boundary,
    and cap still images to a maximum duration.
    """
    max_image_frames = seconds_to_frames(float(MAX_IMAGE_DURATION_SECONDS), fps)
    for idx, clip in enumerate(clips):
        if clip.preserve_duration or clip.is_extract or (clip.treat_as_overlay and not clip.is_image):
            continue
        next_start = None
        current_start = clip.start_frames
        for future in clips[idx + 1 :]:
            if future.start_frames > current_start:
                next_start = future.start_frames
                break
        if next_start is None:
            continue
        gap_frames = max(0, next_start - current_start)
        if gap_frames > 0:
            clip.duration_frames = min(clip.duration_frames, gap_frames)
    for clip in clips:
        if clip.is_image:
            clip.duration_frames = min(clip.duration_frames, max_image_frames)


def _pack_overlay_group(clips: List[InsertClip]) -> None:
    track_availability: Dict[int, int] = {}
    for clip in clips:
        if not clip.treat_as_overlay:
            continue
        if clip.video_track_override is not None:
            track_availability[clip.video_track_override] = clip.start_frames + clip.duration_frames
            continue
        track_index = clip.overlay_track_base or DEFAULT_OVERLAY_TRACK_BASE
        active_tracks = [
            active_index
            for active_index, active_end in track_availability.items()
            if active_end > clip.start_frames and active_index not in RESERVED_OVERLAY_TRACK_INDICES
        ]
        if active_tracks:
            track_index = max(track_index, max(active_tracks) + 1)
        while True:
            if track_index in RESERVED_OVERLAY_TRACK_INDICES:
                track_index += 1
                continue
            available_from = track_availability.get(track_index, -1)
            if clip.start_frames >= available_from:
                clip.video_track_override = track_index
                track_availability[track_index] = clip.start_frames + clip.duration_frames
                break
            track_index += 1


def _pack_insert_group(clips: List[InsertClip], base_track: int) -> None:
    """Assign each clip its own unique track starting from *base_track*.

    Each clip always gets a higher track than all clips that start before it,
    so later-appearing inserts render on top of earlier ones. Tracks reserved
    for zooms/transitions are skipped automatically.
    """
    next_track = base_track
    for clip in clips:
        if clip.video_track_override is not None:
            if clip.video_track_override >= next_track:
                next_track = clip.video_track_override + 1
            continue
        while next_track in RESERVED_OVERLAY_TRACK_INDICES:
            next_track += 1
        clip.video_track_override = next_track
        next_track += 1


def assign_image_tracks(clips: List[InsertClip]) -> None:
    """
    Stack all inserts in order of timeline appearance.

    All non-zoom inserts (regular, title, logo, image, noun/arrow) are packed
    together into a single consecutive block starting at INSERT_VIDEO_TRACK_INDEX.
    Clips that appear later in the timeline always get a higher track number so
    they render on top, regardless of clip type.  Within the same timestamp,
    filled-noun clips are placed below circle-arrow clips.

    Title-background clips are excluded from this group because they already
    have video_track_override set and must sit directly under their paired title.
    """
    _HIGH_OVERLAY_BASES = {
        TITLE_BACKGROUND_TRACK_BASE,
    }

    combined_inserts = [
        c for c in clips
        if c.video_track_override is None
        and not c.is_extract
        and (c.overlay_track_base is None or c.overlay_track_base not in _HIGH_OVERLAY_BASES)
    ]

    def _sort_key(c: InsertClip) -> tuple:
        arrow_rank = 1 if is_circle_arrow_clip(c.path) else 0
        return (c.start_frames, arrow_rank, c.path.name)

    combined_inserts.sort(key=_sort_key)
    _pack_insert_group(combined_inserts, INSERT_VIDEO_TRACK_INDEX)

    combined_ids = {id(c) for c in combined_inserts}
    high_overlays = [c for c in clips if id(c) not in combined_ids and c.treat_as_overlay]
    high_overlays.sort(key=lambda c: c.start_frames)
    _pack_overlay_group(high_overlays)


def determine_zoom_scales(
    instructions: Sequence[ZoomInstruction],
    *,
    base_scale: float = 100.0,
) -> List[Tuple[ZoomInstruction, str]]:
    sorted_instructions = sorted(instructions, key=lambda inst: inst.start_frames)
    assignments: List[Tuple[ZoomInstruction, str]] = []
    def scaled(multiplier: float) -> str:
        return format_scale(base_scale * multiplier)

    idx = 0
    while idx < len(sorted_instructions):
        inst = sorted_instructions[idx]
        code = inst.code.lower()
        if code == "z":
            assignments.append((inst, scaled(1.25)))
            idx += 1
            continue
        if code == "z1":
            sequence = [inst]
            if idx + 1 < len(sorted_instructions) and sorted_instructions[idx + 1].code.lower() == "z2":
                sequence.append(sorted_instructions[idx + 1])
                if (
                    idx + 2 < len(sorted_instructions)
                    and sorted_instructions[idx + 2].code.lower() == "z3"
                ):
                    sequence.append(sorted_instructions[idx + 2])
            pattern = tuple(item.code.lower() for item in sequence)
            if pattern == ("z1", "z2", "z3"):
                scales = [1.11, 1.26, 1.38]
            elif pattern == ("z1", "z2"):
                scales = [1.15, 1.30]
            else:
                default_map = {"z1": 1.11, "z2": 1.26, "z3": 1.38}
                scales = [default_map.get(item.code.lower(), 1.25) for item in sequence]
            for target, scale in zip(sequence, scales):
                assignments.append((target, scaled(scale)))
            idx += len(sequence)
            continue
        fallback_map = {"z1": 1.11, "z2": 1.26, "z3": 1.38}
        assignments.append((inst, scaled(fallback_map.get(code, 1.25))))
        idx += 1
    return assignments


def apply_zoom_instructions_to_rush(
    rush_video_track: Optional[ET.Element],
    instructions: Sequence[ZoomInstruction],
    *,
    base_scale: float = 100.0,
) -> None:
    if rush_video_track is None or not instructions:
        return
    assignments = determine_zoom_scales(instructions, base_scale=base_scale)
    rush_items = collect_rush_clipitems([rush_video_track])
    if not rush_items:
        return
    for inst, scale in assignments:
        target = None
        for start, end, clipitem in rush_items:
            if start <= inst.start_frames < end:
                target = clipitem
                break
        if target is None:
            print(
                f"⚠️  Unable to locate rush clip covering frame {inst.start_frames} for zoom '{inst.code}'."
            )
            continue
        filter_el = ensure_basic_motion_filter(target, default_scale="100")
        set_motion_scale(filter_el, scale)


def create_clipitem(
    clip: InsertClip,
    metadata: SequenceMetadata,
    track_index: int,
    clip_id_suffix: int,
    group_index: int,
    clip_index: int,
) -> ET.Element:
    clip_id = f"insert-clipitem-{clip_id_suffix}"
    file_duration_frames = max(clip.duration_frames, clip.source_in_frames + clip.duration_frames)
    clipitem = ET.Element("clipitem", id=clip_id)
    ET.SubElement(clipitem, "name").text = clip.path.name
    ET.SubElement(clipitem, "enabled").text = "TRUE"
    clipitem.append(build_rate_node(metadata.fps))
    ET.SubElement(clipitem, "start").text = str(clip.start_frames)
    ET.SubElement(clipitem, "end").text = str(clip.start_frames + clip.duration_frames)
    ET.SubElement(clipitem, "in").text = str(clip.source_in_frames)
    ET.SubElement(clipitem, "out").text = str(clip.source_in_frames + clip.duration_frames)
    ET.SubElement(clipitem, "duration").text = str(clip.duration_frames)
    ET.SubElement(clipitem, "alphatype").text = "none"
    ET.SubElement(clipitem, "pixelaspectratio").text = metadata.pixel_aspect
    ET.SubElement(clipitem, "anamorphic").text = "FALSE"

    file_el = ET.SubElement(clipitem, "file", id=f"insert-file-{clip_id_suffix}")
    ET.SubElement(file_el, "name").text = clip.path.name
    ET.SubElement(file_el, "pathurl").text = clip.path.resolve().as_uri()
    file_el.append(build_rate_node(metadata.fps))
    ET.SubElement(file_el, "duration").text = str(file_duration_frames)
    media_el = ET.SubElement(file_el, "media")
    video_el = ET.SubElement(media_el, "video")
    ET.SubElement(video_el, "duration").text = str(file_duration_frames)
    sample = ET.SubElement(video_el, "samplecharacteristics")
    ET.SubElement(sample, "width").text = str(metadata.width)
    ET.SubElement(sample, "height").text = str(metadata.height)
    ET.SubElement(sample, "anamorphic").text = "FALSE"
    ET.SubElement(sample, "pixelaspectratio").text = metadata.pixel_aspect

    ET.SubElement(clipitem, "compositemode").text = "normal"
    motion_filter = create_motion_effect(clip.scale_value, clip.motion_center)
    clipitem.append(motion_filter)

    append_link(
        clipitem,
        clip_id=clip_id,
        mediatype="video",
        track_index=track_index,
        clip_index=clip_index,
        group_index=group_index,
    )
    return clipitem


def create_audio_only_clipitem(
    clip: AudioClipSpec,
    metadata: SequenceMetadata,
    track_index: int,
    clip_id_suffix: int,
    group_index: int,
    clip_index: int,
    *,
    clip_id_prefix: str = "standalone-audio-clipitem",
    file_id_prefix: str = "standalone-audio-file",
    linked_video_id: Optional[str] = None,
    linked_video_track_index: Optional[int] = None,
    linked_video_clip_index: Optional[int] = None,
) -> ET.Element:
    clip_id = f"{clip_id_prefix}-{clip_id_suffix}"
    file_duration_frames = max(clip.duration_frames, clip.source_in_frames + clip.duration_frames)
    clipitem = ET.Element("clipitem", id=clip_id)
    ET.SubElement(clipitem, "name").text = clip.path.name
    ET.SubElement(clipitem, "enabled").text = "TRUE"
    clipitem.append(build_rate_node(metadata.fps))
    ET.SubElement(clipitem, "start").text = str(clip.start_frames)
    ET.SubElement(clipitem, "end").text = str(clip.start_frames + clip.duration_frames)
    ET.SubElement(clipitem, "in").text = str(clip.source_in_frames)
    ET.SubElement(clipitem, "out").text = str(clip.source_in_frames + clip.duration_frames)
    ET.SubElement(clipitem, "duration").text = str(clip.duration_frames)

    file_el = ET.SubElement(clipitem, "file", id=f"{file_id_prefix}-{clip_id_suffix}")
    ET.SubElement(file_el, "name").text = clip.path.name
    ET.SubElement(file_el, "pathurl").text = clip.path.resolve().as_uri()
    file_el.append(build_rate_node(metadata.fps))
    ET.SubElement(file_el, "duration").text = str(file_duration_frames)
    media_el = ET.SubElement(file_el, "media")
    audio_el = ET.SubElement(media_el, "audio")
    audio_sample_rate = clip.audio_sample_rate
    audio_channels = clip.audio_channels
    if audio_sample_rate is None or audio_channels is None:
        probed_sample_rate, probed_channels = probe_audio_stream_info(clip.path)
        audio_sample_rate = audio_sample_rate or probed_sample_rate
        audio_channels = audio_channels or probed_channels
    ET.SubElement(audio_el, "samplerate").text = str(audio_sample_rate or metadata.audio_sample_rate)
    ET.SubElement(audio_el, "channels").text = str(audio_channels or metadata.audio_channels)

    sourcetrack = ET.SubElement(clipitem, "sourcetrack")
    ET.SubElement(sourcetrack, "mediatype").text = "audio"
    ET.SubElement(sourcetrack, "trackindex").text = "1"

    if clip.gain_level is not None:
        clipitem.append(create_audio_gain_filter(clip.gain_level))
    fade_filter = create_audio_fade_metadata_filter(
        fade_in_frames=clip.fade_in_frames,
        fade_out_frames=clip.fade_out_frames,
    )
    if fade_filter is not None:
        clipitem.append(fade_filter)

    if (
        linked_video_id
        and linked_video_track_index is not None
        and linked_video_clip_index is not None
    ):
        append_link(
            clipitem,
            clip_id=linked_video_id,
            mediatype="video",
            track_index=linked_video_track_index,
            clip_index=linked_video_clip_index,
            group_index=group_index,
        )

    append_link(
        clipitem,
        clip_id=clip_id,
        mediatype="audio",
        track_index=track_index,
        clip_index=clip_index,
        group_index=group_index,
    )
    return clipitem


def ensure_track_count(parent: ET.Element, target: int, is_audio: bool = False) -> None:
    while len(parent.findall("track")) < target:
        track = ET.SubElement(parent, "track")
        ET.SubElement(track, "enabled").text = "TRUE"
        ET.SubElement(track, "locked").text = "FALSE"
        if is_audio:
            ET.SubElement(track, "outputchannelindex").text = str(len(parent.findall("track")))
            vol = ET.SubElement(track, "volume")
            ET.SubElement(vol, "level").text = "1.0"


def set_track_enabled(track: ET.Element, enabled: bool) -> None:
    node = track.find("enabled")
    if node is None:
        node = ET.SubElement(track, "enabled")
    node.text = "TRUE" if enabled else "FALSE"


def indent_xml(elem: ET.Element, level: int = 0) -> None:
    indent = "\n" + ("  " * level)
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "  "
        for child in elem:
            indent_xml(child, level + 1)
            if not child.tail or not child.tail.strip():
                child.tail = indent + "  "
        if not child.tail or not child.tail.strip():
            child.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent


def determine_output_path(reference_xml: Path, explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR / f"{reference_xml.stem}_precise_inserts.xml"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.reference_xml:
        reference_xml = Path(args.reference_xml).expanduser()
    else:
        try:
            reference_xml = find_latest_otio_xml()
        except FileNotFoundError as exc:
            raise SystemExit(str(exc)) from exc
    if not reference_xml.exists():
        raise SystemExit(f"Reference XML not found: {reference_xml}")
    insert_dir = Path(args.insert_dir).expanduser()
    if not insert_dir.exists():
        raise SystemExit(f"Insert folder not found: {insert_dir}")

    comparison_csv = resolve_comparison_csv(args.comparison_csv)
    if args.disable_cta_materialization:
        materialized_cta_paths, selected_cta_csv = [], comparison_csv
    else:
        materialized_cta_paths, selected_cta_csv = materialize_cta_inserts(
            insert_dir, comparison_csv
        )
    zoom_replacements = load_zoom_replacements(args.zoom_replacements_manifest)

    metadata = extract_metadata(reference_xml)
    metadata = infer_sequence_dimensions(metadata)
    insert_clips, timeline_gap_events, zoom_instructions = gather_insert_clips(
        insert_dir,
        metadata,
        comparison_csv,
    )
    if args.fixed_scale is not None:
        forced_scale = format_scale(args.fixed_scale)
        for clip in insert_clips:
            clip.scale_value = forced_scale
    if not insert_clips and not zoom_replacements:
        raise SystemExit(f"No valid insert clips found inside {insert_dir}")

    tree = ET.parse(reference_xml)
    root = tree.getroot()
    hydrate_clipitem_file_references(root)
    sequence = root.find("./sequence")
    if sequence is None:
        raise SystemExit("Reference XML has no <sequence> node.")
    sequence_name = args.sequence_name or metadata.sequence_name
    name_el = sequence.find("name")
    if name_el is not None:
        name_el.text = sequence_name

    video_section = sequence.find("./media/video")
    if video_section is None:
        raise SystemExit("Reference XML has no <media><video> section.")
    ensure_track_count(video_section, VIDEO_TRACK_TARGET, is_audio=False)
    video_tracks = [child for child in list(video_section) if child.tag == "track"]
    if not video_tracks:
        raise SystemExit("Reference XML lacks video tracks.")

    audio_parent = sequence.find("./media")
    if audio_parent is None:
        raise SystemExit("Reference XML missing <media> section.")
    audio_section = sequence.find("./media/audio")
    if audio_section is None:
        audio_section = ET.SubElement(audio_parent, "audio")
    audio_tracks = [child for child in list(audio_section) if child.tag == "track"]

    def ensure_track_index(tracks: List[ET.Element], desired_index: int, is_audio: bool) -> None:
        if len(tracks) >= desired_index:
            return
        target_parent = audio_section if is_audio else video_section
        ensure_track_count(target_parent, desired_index, is_audio=is_audio)
        tracks[:] = [child for child in list(target_parent) if child.tag == "track"]

    ensure_track_index(video_tracks, INSERT_VIDEO_TRACK_INDEX, is_audio=False)
    ensure_track_index(video_tracks, ZOOM_VIDEO_TRACK_INDEX, is_audio=False)
    ensure_track_index(video_tracks, EXTRACT_VIDEO_TRACK_INDEX, is_audio=False)
    ensure_track_index(video_tracks, TRANSITION_VIDEO_TRACK_INDEX, is_audio=False)
    ensure_track_index(video_tracks, OUTRO_VIDEO_TRACK_INDEX, is_audio=False)
    ensure_track_index(audio_tracks, INSERT_AUDIO_TRACK_INDEX, is_audio=True)
    ensure_track_index(audio_tracks, EXTRACT_AUDIO_TRACK_INDEX, is_audio=True)
    ensure_track_index(audio_tracks, TRANSITION_AUDIO_TRACK_INDEX, is_audio=True)
    ensure_track_index(audio_tracks, INTRO_AUDIO_TRACK_INDEX, is_audio=True)
    ensure_track_index(audio_tracks, WOOSH_AUDIO_TRACK_INDEX, is_audio=True)
    ensure_track_index(audio_tracks, AUDIO_OUTRO_TRACK_INDEX, is_audio=True)

    last_main_start, last_main_end = compute_main_track_bounds(video_tracks)
    cutoff_frame = None

    rush_video_track = (
        video_tracks[RUSH_VIDEO_TRACK_INDEX - 1]
        if len(video_tracks) >= RUSH_VIDEO_TRACK_INDEX
        else None
    )
    rush_audio_track = (
        audio_tracks[RUSH_AUDIO_TRACK_INDEX - 1]
        if len(audio_tracks) >= RUSH_AUDIO_TRACK_INDEX
        else None
    )
    rush_base_scale = determine_rush_fill_frame_scale(
        rush_video_track,
        metadata,
        explicit_scale=args.rush_base_scale,
    )
    apply_default_scale_to_rush(rush_video_track, scale_value=rush_base_scale)
    if not zoom_replacements:
        apply_zoom_instructions_to_rush(
            rush_video_track,
            zoom_instructions,
            base_scale=float(rush_base_scale),
        )
    apply_timeline_gap_events_to_rush_tracks(
        timeline_gap_events, rush_video_track, rush_audio_track
    )
    total_rush_shift = adjust_extract_timeline(
        insert_clips,
        rush_video_track,
        rush_audio_track,
        metadata.fps,
    )
    align_overlays_to_rush(insert_clips, rush_video_track, metadata.fps)
    delay_clips_until_extracts_finish(insert_clips, metadata.fps)
    swap_outro_dip_onto_rush(rush_video_track, rush_audio_track, insert_dir, metadata)
    _, rush_end_frames_after_adjust = compute_main_track_bounds(video_tracks)
    insert_clips = drop_inserts_past_rush_end(
        insert_clips,
        rush_end_frames_after_adjust,
        tolerance_frames=seconds_to_frames(0.25, metadata.fps, allow_zero=True),
    )
    zoom_overlay_clips = build_zoom_overlay_clips(rush_video_track, zoom_replacements, metadata)
    if zoom_replacements and not zoom_overlay_clips:
        print("⚠️  Zoom manifest loaded, but no valid zoom overlay clips were produced.")
    all_video_clips = insert_clips + zoom_overlay_clips
    track_candidates = [clip.video_track_override or 0 for clip in all_video_clips]
    max_image_track = max(track_candidates or [0])
    if max_image_track >= 4:
        ensure_track_index(video_tracks, max_image_track, is_audio=False)
    # Split-screen application disabled for rush tracks.

    initial_sequence_duration = parse_int(sequence.findtext("duration"), 0)

    video_clip_counters: Dict[int, int] = {
        idx + 1: len(track.findall("clipitem")) for idx, track in enumerate(video_tracks)
    }
    audio_clip_counters: Dict[int, int] = {
        idx + 1: len(track.findall("clipitem")) for idx, track in enumerate(audio_tracks)
    }
    extra_video_items = 0
    extra_audio_items = 0

    max_end_frame = max(last_main_end, cutoff_frame or initial_sequence_duration)

    def append_audio_spec_to_track(
        spec: Optional[AudioClipSpec], *, target_track_index: int = AUDIO_EFFECT_TRACK_INDEX
    ) -> None:
        nonlocal max_end_frame, extra_audio_items
        if spec is None:
            return
        track_index = target_track_index
        if track_index > len(audio_tracks):
            return
        audio_track = audio_tracks[track_index - 1]
        audio_clip_counters.setdefault(track_index, len(audio_track.findall("clipitem")))
        audio_clip_counters[track_index] += 1
        clip_index = audio_clip_counters[track_index]
        extra_audio_items += 1
        clip_id_suffix = 12000 + extra_audio_items
        group_index = 13000 + extra_audio_items
        clipitem = create_audio_only_clipitem(
            clip=spec,
            metadata=metadata,
            track_index=track_index,
            clip_id_suffix=clip_id_suffix,
            group_index=group_index,
            clip_index=clip_index,
        )
        audio_track.append(clipitem)
        max_end_frame = max(max_end_frame, spec.start_frames + spec.duration_frames)

    for idx, clip in enumerate(all_video_clips, start=1):
        default_video_track = (
            EXTRACT_VIDEO_TRACK_INDEX if clip.is_extract else INSERT_VIDEO_TRACK_INDEX
        )
        video_track_index = clip.video_track_override or default_video_track
        video_track = video_tracks[video_track_index - 1]
        video_clip_counters.setdefault(video_track_index, len(video_track.findall("clipitem")))
        video_clip_counters[video_track_index] += 1
        video_clip_index = video_clip_counters[video_track_index]
        group_index = 2000 + idx
        clip_id_suffix = 1000 + idx
        clipitem = create_clipitem(
            clip=clip,
            metadata=metadata,
            track_index=video_track_index,
            clip_id_suffix=clip_id_suffix,
            group_index=group_index,
            clip_index=video_clip_index,
        )
        video_track.append(clipitem)

        include_audio = False
        audio_gain = None
        audio_track_index: Optional[int] = None
        if clip.mute_audio:
            include_audio = False
        elif not clip.treat_as_overlay:
            include_audio = bool(clip.audio_override_path)
            if not include_audio:
                try:
                    include_audio = media_has_audio(clip.path)
                except FileNotFoundError:
                    include_audio = False
        else:
            include_audio = should_include_overlay_audio(clip)
        if include_audio:
            audio_track_index = resolve_insert_audio_track_index(
                clip,
                video_track_index=video_track_index,
            )
            ensure_track_index(audio_tracks, audio_track_index, is_audio=True)
            audio_track = audio_tracks[audio_track_index - 1]
            set_track_enabled(audio_track, enabled=True)
            audio_clip_counters.setdefault(audio_track_index, len(audio_track.findall("clipitem")))
            audio_clip_counters[audio_track_index] += 1
            audio_clip_index = audio_clip_counters[audio_track_index]
            audio_id_suffix = 5000 + idx
            audio_spec = AudioClipSpec(
                path=clip.audio_override_path or clip.path,
                start_frames=clip.start_frames,
                duration_frames=clip.duration_frames,
                audio_sample_rate=None,
                audio_channels=None,
                source_in_frames=clip.source_in_frames,
                gain_level=(
                    clip.audio_gain_level
                    if clip.audio_gain_level is not None
                    else (audio_gain if audio_track_index == video_track_index else None)
                ),
            )
            audio_clipitem = create_audio_only_clipitem(
                clip=audio_spec,
                metadata=metadata,
                track_index=audio_track_index,
                clip_id_suffix=audio_id_suffix,
                group_index=group_index,
                clip_index=audio_clip_index,
                clip_id_prefix="insert-audio-clipitem",
                file_id_prefix="insert-audio-file",
                linked_video_id=clipitem.get("id", ""),
                linked_video_track_index=video_track_index,
                linked_video_clip_index=video_clip_index,
            )
            audio_track.append(audio_clipitem)
            append_link(
                clipitem,
                clip_id=audio_clipitem.get("id", ""),
                mediatype="audio",
                track_index=audio_track_index,
                clip_index=audio_clip_index,
                group_index=group_index,
            )

        max_end_frame = max(max_end_frame, clip.start_frames + clip.duration_frames)

    if total_rush_shift > 0:
        max_end_frame = max(max_end_frame, (last_main_end or 0) + total_rush_shift)

    outro_offset_frames = seconds_to_frames(OUTRO_OFFSET_SECONDS, metadata.fps)
    outro_delay_frames = seconds_to_frames(OUTRO_DELAY_SECONDS, metadata.fps, allow_zero=True)
    outro_end_frame = last_main_end
    outro_start = max(0, max_end_frame - outro_offset_frames) + outro_delay_frames
    outro_clip = build_external_video_clip(OUTRO_VIDEO_PATH, metadata, outro_start)
    if outro_clip is not None:
        video_track_index = OUTRO_VIDEO_TRACK_INDEX
        if video_track_index <= len(video_tracks):
            video_track = video_tracks[video_track_index - 1]
            video_clip_counters.setdefault(
                video_track_index, len(video_track.findall("clipitem"))
            )
            video_clip_counters[video_track_index] += 1
            video_clip_index = video_clip_counters[video_track_index]
            extra_video_items += 1
            clip_id_suffix = 30000 + extra_video_items
            group_index = 31000 + extra_video_items
            clipitem = create_clipitem(
                clip=outro_clip,
                metadata=metadata,
                track_index=video_track_index,
                clip_id_suffix=clip_id_suffix,
                group_index=group_index,
                clip_index=video_clip_index,
            )
            video_track.append(clipitem)
            outro_end_frame = outro_clip.start_frames + outro_clip.duration_frames
            max_end_frame = max(max_end_frame, outro_end_frame)
            audio_track_index = AUDIO_OUTRO_TRACK_INDEX
            if audio_track_index <= len(audio_tracks):
                audio_track = audio_tracks[audio_track_index - 1]
                audio_clip_counters.setdefault(
                    audio_track_index, len(audio_track.findall("clipitem"))
                )
                audio_clip_counters[audio_track_index] += 1
                audio_clip_index = audio_clip_counters[audio_track_index]
                extra_audio_items += 1
                audio_id_suffix = 40000 + extra_audio_items
                outro_audio_spec = AudioClipSpec(
                    path=outro_clip.path,
                    start_frames=outro_clip.start_frames,
                    duration_frames=outro_clip.duration_frames,
                    audio_sample_rate=None,
                    audio_channels=None,
                    source_in_frames=outro_clip.source_in_frames,
                )
                audio_clipitem = create_audio_only_clipitem(
                    clip=outro_audio_spec,
                    metadata=metadata,
                    track_index=audio_track_index,
                    clip_id_suffix=audio_id_suffix,
                    group_index=group_index,
                    clip_index=audio_clip_index,
                    clip_id_prefix="insert-audio-clipitem",
                    file_id_prefix="insert-audio-file",
                    linked_video_id=clipitem.get("id", ""),
                    linked_video_track_index=video_track_index,
                    linked_video_clip_index=video_clip_index,
                )
                audio_track.append(audio_clipitem)
                append_link(
                    clipitem,
                    clip_id=audio_clipitem.get("id", ""),
                    mediatype="audio",
                    track_index=audio_track_index,
                    clip_index=audio_clip_index,
                    group_index=group_index,
                )
                outro_end_frame = outro_clip.start_frames + outro_clip.duration_frames
                max_end_frame = max(max_end_frame, outro_end_frame)

    intro_music_spec = build_audio_spec(
        INTRO_MUSIC_PATH,
        metadata,
        start_frames=0,
        gain_level=adjust_audio_gain_level(None),
    )
    append_audio_spec_to_track(intro_music_spec, target_track_index=INTRO_AUDIO_TRACK_INDEX)

    woosh_template = build_audio_spec(
        WOOSH_EFFECT_PATH,
        metadata,
        start_frames=0,
        gain_level=adjust_audio_gain_level(WOOSH_LEVEL),
    )
    if woosh_template:
        sound_cues = collect_overlay_sound_cues(insert_clips, woosh_template.duration_frames)
        for cue_frame in sound_cues:
            spec = AudioClipSpec(
                path=woosh_template.path,
                start_frames=cue_frame,
                duration_frames=woosh_template.duration_frames,
                gain_level=woosh_template.gain_level,
            )
            append_audio_spec_to_track(spec, target_track_index=WOOSH_AUDIO_TRACK_INDEX)

    outro_music_spec = build_audio_spec(
        OUTRO_MUSIC_PATH,
        metadata,
        start_frames=0,
        gain_level=adjust_audio_gain_level(None),
    )
    if outro_music_spec is not None:
        if outro_end_frame is None or outro_end_frame <= 0:
            outro_end_frame = max_end_frame
        outro_music_spec.start_frames = max(
            0, (outro_end_frame or max_end_frame) - outro_music_spec.duration_frames
        )
    append_audio_spec_to_track(outro_music_spec, target_track_index=INTRO_AUDIO_TRACK_INDEX)

    if cutoff_frame:
        if len(video_tracks) > 1:
            trim_existing_tracks(video_tracks[1:], cutoff_frame, allowed_names=["Outro.mov"])
        if len(audio_tracks) > 1:
            trim_existing_tracks(
                audio_tracks[1:],
                cutoff_frame,
                allowed_names=["Outro.mov", OUTRO_MUSIC_PATH.name.lower()],
            )

    duration_el = sequence.find("duration")
    if duration_el is None:
        duration_el = ET.SubElement(sequence, "duration")
    duration_el.text = str(max_end_frame)

    indent_xml(root)
    output_path = determine_output_path(reference_xml, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        handle.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write(b"<!DOCTYPE xmeml>\n")
        handle.write(ET.tostring(root, encoding="utf-8"))

    print(f"Reference XML: {reference_xml}")
    if selected_cta_csv is not None:
        print(f"Comparison CSV selected: {selected_cta_csv}")
    if materialized_cta_paths:
        print(f"CTA insert files ready: {len(materialized_cta_paths)}")
    print(
        f"Insert directory: {insert_dir} "
        f"({len(insert_clips)} insert clip(s), {len(zoom_overlay_clips)} zoom overlay clip(s))"
    )
    print(f"Sequence name: {sequence_name}")
    print(f"Timeline length: {max_end_frame / metadata.fps:.2f}s ({max_end_frame} frames)")
    print(f"Wrote XML to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
