#!/usr/bin/env python3
"""Create illustration base clips for measurement and marker mentions from the compparser CSV.

Measurement types (base asset only; text added later as a separate layer):
  Decibel Mention    → sound.mov      (tag: DBC)
  Speed Mention      → vitesse.mov    (tag: SPD)
  Weight Object Mention → wieght.mov  (tag: WGT)
  Surface Mention    → surface_area.mov (tag: SRF)
  Distance Mention   → 20mars.mov     (tag: DST)
  Temperature Mention→ thermometer.mov (tag: TMP)

Marker types (asset copied as-is, no text overlay):
  Social Network Mention → facebook/instagram/twitter/youtube/snapchat.mov (tag: SOC)
  Ranking Mention        → gold 2/silver 2/bronze 2.mov                   (tag: RNK)
  Punctuation Signal     → exclamation/question/threedots.mov              (tag: PNC)

After rendering, all clips are renamed with timeline timestamps and moved to
Universal_pipe/Insert using the naming convention: {min}m{sec:02d}_{occ}_{name}_{seq:03d}_{TAG}.mov
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
INSERT_DIR = CODE_BASE / "swisser" / "Universal_pipe" / "Insert"

ASSET_BASE = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe"
    "/asset/midjourney_animation/universal_fr"
)

# Measurement assets (text is added later via add_text_to_animation.py)
SOUND_ASSET       = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "sound_bg.mov"
VITESSE_ASSET     = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "vitesse_bg.mov"
WEIGHT_ASSET      = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "wieght_bg.mov"
SURFACE_ASSET     = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "surface_area_bg.mov"
DISTANCE_ASSET    = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "20mars_bg.mov"
THERMOMETER_ASSET = ASSET_BASE / "key numbers" / "numbers_with_transparent_background" / "thermometer_bg.mov"

# Social network assets (no text)
SOCIAL_ASSETS: Dict[str, Path] = {
    "facebook":  ASSET_BASE / "CTA" / "social @" / "facebook.mov",
    "instagram": ASSET_BASE / "CTA" / "social @" / "instagram.mov",
    "twitter":   ASSET_BASE / "CTA" / "social @" / "twitter.mov",
    "x":         ASSET_BASE / "CTA" / "social @" / "twitter.mov",
    "youtube":   ASSET_BASE / "CTA" / "social @" / "youtube.mov",
    "snapchat":  ASSET_BASE / "CTA" / "social @" / "snapchat.mov",
}

# Ranking assets (no text) — 1=gold, 2=silver, 3=bronze
RANKING_ASSETS: Dict[int, Path] = {
    1: ASSET_BASE / "key numbers" / "gold 2.mov",
    2: ASSET_BASE / "key numbers" / "silver 2.mov",
    3: ASSET_BASE / "key numbers" / "bronze 2.mov",
}

# Punctuation assets (no text)
PUNCTUATION_ASSETS: Dict[str, Path] = {
    "!":   ASSET_BASE / "format" / "exclamation.mov",
    "?":   ASSET_BASE / "format" / "question.mov",
    "...": ASSET_BASE / "format" / "threedots.mov",
}

# ---------------------------------------------------------------------------
# Mention type config: column_name → (TAG, short_name, asset_or_None)
# asset=None means text-only clip (dark background)
# ---------------------------------------------------------------------------

MEASUREMENT_COLUMNS: Dict[str, Tuple[str, str, Optional[Path]]] = {
    "Decibel Mention":        ("DBC", "sound",    SOUND_ASSET),
    "Speed Mention":          ("SPD", "vitesse",  VITESSE_ASSET),
    "Weight Object Mention":  ("WGT", "weight",   WEIGHT_ASSET),
    "Weight Person Mention":  ("WGT", "weight",   WEIGHT_ASSET),
    "Surface Mention":        ("SRF", "surface",  SURFACE_ASSET),
    "Distance Mention":       ("DST", "distance", DISTANCE_ASSET),
    "Temperature Mention":    ("TMP", "temp",     THERMOMETER_ASSET),
}

# ---------------------------------------------------------------------------
# Font config
# ---------------------------------------------------------------------------

IMPACT_PATHS = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Impact.ttf",
]
POPPINS_FALLBACK = "/Users/mathieusandana/Library/Fonts/Poppins-Bold.ttf"
SYSTEM_FALLBACKS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSDisplay.ttf",
    "/Library/Fonts/Arial.ttf",
]

TEXT_ONLY_DURATION = 5.0
DEFAULT_INSERT_AUDIO_REDUCTION_DB = -10.0
DEFAULT_INSERT_AUDIO_MULTIPLIER = math.pow(10.0, DEFAULT_INSERT_AUDIO_REDUCTION_DB / 20.0)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MentionEntry:
    column: str
    value: str
    tag: str
    type_name: str
    asset: Optional[Path]
    row_index: int
    transcript_number: Optional[str]
    start_timecode: Optional[str]
    end_timecode: Optional[str]
    start_seconds: Optional[float]
    end_seconds: Optional[float]
    entry_id: Optional[int] = None
    illustration_type: str = ""
    asset_category: str = "social_ranking_punctuation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_font_file() -> str:
    for path in IMPACT_PATHS:
        if Path(path).exists():
            return path
    if Path(POPPINS_FALLBACK).exists():
        return POPPINS_FALLBACK
    for path in SYSTEM_FALLBACKS:
        if Path(path).exists():
            return path
    return "/System/Library/Fonts/Helvetica.ttc"


def parse_timecode(value: Optional[str], frame_rate: float) -> Optional[float]:
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        if len(parts) == 4:
            h, m, s, f = map(int, parts)
            return h * 3600 + m * 60 + s + f / frame_rate
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
    except ValueError:
        pass
    return None


def parse_human_timestamp(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    match = re.fullmatch(r"(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<millis>\d{3})", raw)
    if not match:
        return None
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    millis = int(match.group("millis"))
    return (hours * 3600) + (minutes * 60) + seconds + (millis / 1000.0)


def split_values(cell: Optional[str]) -> List[str]:
    if not cell:
        return []
    return [v.strip().strip('"').strip() for v in cell.split("|") if v.strip().strip('"').strip()]


def probe_video_size(path: Path) -> Tuple[int, int]:
    """Return (width, height) of a video file, or (768, 512) fallback."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            parts = out.stdout.strip().split(",")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return 768, 512


def probe_video_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            val = out.stdout.strip()
            if val:
                return float(val)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return TEXT_ONLY_DURATION


BBox = Tuple[int, int, int, int]  # x1, y1, x2, y2
_BBOX_CACHE: Dict[Path, Optional[BBox]] = {}


def alpha_bbox(path: Path) -> Optional[BBox]:
    """Return the combined alpha bounding box for the asset, if any."""
    if path in _BBOX_CACHE:
        return _BBOX_CACHE[path]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostats",
        "-i",
        str(path),
        "-filter_complex",
        "[0:v]alphaextract,bbox",
        "-an",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        _BBOX_CACHE[path] = None
        return None
    pattern = re.compile(rb"x1:(\d+)\s+x2:(\d+)\s+y1:(\d+)\s+y2:(\d+)")
    matches = list(pattern.finditer(result.stderr))
    if not matches:
        _BBOX_CACHE[path] = None
        return None
    x1 = min(int(m.group(1)) for m in matches)
    x2 = max(int(m.group(2)) for m in matches)
    y1 = min(int(m.group(3)) for m in matches)
    y2 = max(int(m.group(4)) for m in matches)
    bbox = (x1, y1, x2, y2)
    _BBOX_CACHE[path] = bbox
    return bbox


def seconds_to_insert_prefix(seconds: float) -> str:
    """Convert seconds to 'XmYY' prefix format used by Insert filenames."""
    total_secs = int(seconds)
    minutes = total_secs // 60
    secs = total_secs % 60
    return f"{minutes}m{secs:02d}"


# ---------------------------------------------------------------------------
# Detection helpers for marker types
# ---------------------------------------------------------------------------

def detect_social_network(value: str) -> Optional[Path]:
    """Return asset path for detected social network, or None."""
    lower = value.lower()
    for keyword, asset_path in SOCIAL_ASSETS.items():
        if keyword in lower:
            return asset_path
    return None


_RANKING_GOLD_PATTERNS = re.compile(
    r"(?:^|[^\d])(?:1|1er|1ère|1st|first|premier|première|gold|or)\b",
    re.IGNORECASE,
)
_RANKING_SILVER_PATTERNS = re.compile(
    r"(?:^|[^\d])(?:2|2ème|2nd|second|deuxième|silver|argent)\b",
    re.IGNORECASE,
)
_RANKING_BRONZE_PATTERNS = re.compile(
    r"(?:^|[^\d])(?:3|3ème|3rd|third|troisième|bronze)\b",
    re.IGNORECASE,
)


def detect_ranking_tier(value: str) -> Optional[Tuple[int, Path]]:
    """Return (tier, asset_path) for 1/2/3 or None."""
    if _RANKING_GOLD_PATTERNS.search(value):
        return 1, RANKING_ASSETS[1]
    if _RANKING_SILVER_PATTERNS.search(value):
        return 2, RANKING_ASSETS[2]
    if _RANKING_BRONZE_PATTERNS.search(value):
        return 3, RANKING_ASSETS[3]
    return None


def detect_punctuation_type(value: str) -> Optional[Tuple[str, Path]]:
    """Return (symbol, asset_path) for the punctuation type, or None."""
    cleaned = value.strip()
    # Direct symbol match
    if "!" in cleaned:
        return "!", PUNCTUATION_ASSETS["!"]
    if "?" in cleaned:
        return "?", PUNCTUATION_ASSETS["?"]
    if "..." in cleaned or "\u2026" in cleaned:
        return "...", PUNCTUATION_ASSETS["..."]
    # Keyword match
    lower = cleaned.lower()
    if any(k in lower for k in ("exclamation", "exclamat")):
        return "!", PUNCTUATION_ASSETS["!"]
    if any(k in lower for k in ("interrogat", "question")):
        return "?", PUNCTUATION_ASSETS["?"]
    if any(k in lower for k in ("suspension", "ellips", "threedot", "three dot")):
        return "...", PUNCTUATION_ASSETS["..."]
    return None


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"Directory does not exist: {directory}")
    candidates = [p for p in directory.rglob("*comparison.csv") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No *comparison.csv found in {directory}")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def load_csv(path: Path) -> Tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise RuntimeError(f"{path} has no rows")
        return header, [list(row) for row in reader]


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    return {col.strip().lower(): idx for idx, col in enumerate(header)}


def require_column(header_map: Mapping[str, int], column: str) -> int:
    key = column.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column}' missing from CSV.")
    return header_map[key]


def optional_column(header_map: Mapping[str, int], column: str) -> Optional[int]:
    return header_map.get(column.strip().lower())


def load_manifest_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        return [{(key or "").strip(): (value or "") for key, value in row.items()} for row in reader]


MANIFEST_TYPE_CONFIG: Dict[str, Tuple[str, str, Optional[Path]]] = {
    "bold": ("BLD", "bold", None),
    "italic": ("ITL", "italic", None),
    "underline": ("UND", "underline", None),
    "list_bullet_group": ("LST", "list_bullet", None),
    "list_dash_group": ("LST", "list_dash", None),
    "list_number_group": ("LST", "list_number", None),
    "list_check_group": ("LST", "list_check", None),
    "facebook": ("SOC", "facebook", SOCIAL_ASSETS["facebook"]),
    "instagram": ("SOC", "instagram", SOCIAL_ASSETS["instagram"]),
    "twitter": ("SOC", "twitter", SOCIAL_ASSETS["twitter"]),
    "youtube": ("SOC", "youtube", SOCIAL_ASSETS["youtube"]),
    "snapchat": ("SOC", "snapchat", SOCIAL_ASSETS["snapchat"]),
    "speed": ("SPD", "vitesse", VITESSE_ASSET),
    "decibel": ("DBC", "sound", SOUND_ASSET),
    "weight_object": ("WGT", "weight", WEIGHT_ASSET),
    "weight_person": ("WGP", "weight_person", WEIGHT_ASSET),
    "surface": ("SRF", "surface", SURFACE_ASSET),
    "distance": ("DST", "distance", DISTANCE_ASSET),
    "temperature": ("TMP", "temp", THERMOMETER_ASSET),
    "volume": ("VOL", "volume", None),
    "duration": ("DUR", "duration", None),
    "ranking": ("RNK", "gold", RANKING_ASSETS[1]),
}


def _format_manifest_value(illustration_type: str, reference_word: str, transcript_word: str) -> str:
    value = reference_word or transcript_word
    if not value:
        return ""
    fragments = [fragment.strip() for fragment in value.split("|") if fragment.strip()]
    if not fragments:
        return value.strip()
    if illustration_type == "list_bullet_group":
        return " • ".join(fragments)
    if illustration_type == "list_dash_group":
        return " - ".join(fragments)
    if illustration_type == "list_number_group":
        return " ".join(f"{index}. {fragment}" for index, fragment in enumerate(fragments, start=1))
    if illustration_type == "list_check_group":
        return " ".join(f"✓ {fragment}" for fragment in fragments)
    return value.strip()


def collect_entries_from_timing_manifest(manifest_rows: Sequence[Mapping[str, str]]) -> List[MentionEntry]:
    entries: List[MentionEntry] = []
    seen_entry_ids: set[int] = set()
    for row in manifest_rows:
        if (row.get("Asset Category") or "").strip() != "social_ranking_punctuation":
            continue
        illustration_type = (row.get("Illustration Type") or "").strip()
        config = MANIFEST_TYPE_CONFIG.get(illustration_type)
        if config is None:
            continue
        entry_id_raw = (row.get("Entry ID") or "").strip()
        try:
            entry_id = int(entry_id_raw)
        except ValueError:
            continue
        if entry_id in seen_entry_ids:
            continue
        seen_entry_ids.add(entry_id)
        tag, type_name, asset = config
        if illustration_type == "ranking":
            lowered = ((row.get("Reference Word") or row.get("Transcript Word") or "")).lower()
            if "deux" in lowered or "2" in lowered or "second" in lowered:
                type_name, asset = "silver", RANKING_ASSETS[2]
            elif "trois" in lowered or "3" in lowered or "troisi" in lowered:
                type_name, asset = "bronze", RANKING_ASSETS[3]
        row_id_raw = (row.get("Row ID") or row.get("Transcript #") or "").strip()
        try:
            row_index = int(row_id_raw) if row_id_raw else entry_id
        except ValueError:
            row_index = entry_id
        reference_word = (row.get("Reference Word") or "").strip()
        transcript_word = (row.get("Transcript Word") or "").strip()
        value = _format_manifest_value(illustration_type, reference_word, transcript_word)
        entries.append(
            MentionEntry(
                column="Timed AI Manifest",
                value=value,
                tag=tag,
                type_name=type_name,
                asset=asset,
                row_index=row_index,
                transcript_number=(row.get("Transcript #") or "").strip() or None,
                start_timecode=(row.get("Start Time") or "").strip() or None,
                end_timecode=(row.get("End Time") or "").strip() or None,
                start_seconds=parse_human_timestamp(row.get("Start Time")),
                end_seconds=parse_human_timestamp(row.get("End Time")),
                entry_id=entry_id,
                illustration_type=illustration_type,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Entry collection
# ---------------------------------------------------------------------------

def collect_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
) -> List[MentionEntry]:
    start_idx = require_column(header_map, "Start Time")
    end_idx   = require_column(header_map, "End Time")
    tr_idx    = require_column(header_map, "Transcript #")

    entries: List[MentionEntry] = []
    # Dedup key: (column_name, normalised_value) — prevents identical clips
    seen: set = set()

    # ── Measurement types ────────────────────────────────────────────────────
    for col_name, (tag, type_name, asset) in MEASUREMENT_COLUMNS.items():
        col_idx = optional_column(header_map, col_name)
        if col_idx is None:
            continue
        for row_num, row in enumerate(rows, start=1):
            cell = row[col_idx] if col_idx < len(row) else ""
            for value in split_values(cell):
                dedup_key = (col_name, value.strip().lower(), str(row_num))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                start_tc = row[start_idx] if start_idx < len(row) else ""
                end_tc   = row[end_idx]   if end_idx   < len(row) else ""
                tr_num   = row[tr_idx]    if tr_idx    < len(row) else ""
                entries.append(MentionEntry(
                    column=col_name,
                    value=value,
                    tag=tag,
                    type_name=type_name,
                    asset=asset,
                    row_index=row_num,
                    transcript_number=tr_num.strip() or None,
                    start_timecode=start_tc.strip() or None,
                    end_timecode=end_tc.strip() or None,
                    start_seconds=parse_timecode(start_tc, frame_rate),
                    end_seconds=parse_timecode(end_tc, frame_rate),
                ))

    # ── Social Network Mention ───────────────────────────────────────────────
    soc_idx = optional_column(header_map, "Social Network Mention")
    if soc_idx is not None:
        for row_num, row in enumerate(rows, start=1):
            cell = row[soc_idx] if soc_idx < len(row) else ""
            for value in split_values(cell):
                asset_path = detect_social_network(value)
                if asset_path is None:
                    continue
                dedup_key = ("Social Network Mention", value.strip().lower())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                start_tc = row[start_idx] if start_idx < len(row) else ""
                end_tc   = row[end_idx]   if end_idx   < len(row) else ""
                tr_num   = row[tr_idx]    if tr_idx    < len(row) else ""
                net_name = next(
                    (k for k in SOCIAL_ASSETS if k in value.lower()), "social"
                )
                entries.append(MentionEntry(
                    column="Social Network Mention",
                    value=value,
                    tag="SOC",
                    type_name=net_name,
                    asset=asset_path,
                    row_index=row_num,
                    transcript_number=tr_num.strip() or None,
                    start_timecode=start_tc.strip() or None,
                    end_timecode=end_tc.strip() or None,
                    start_seconds=parse_timecode(start_tc, frame_rate),
                    end_seconds=parse_timecode(end_tc, frame_rate),
                ))

    # ── Ranking Mention ──────────────────────────────────────────────────────
    rnk_idx = optional_column(header_map, "Ranking Mention")
    if rnk_idx is not None:
        for row_num, row in enumerate(rows, start=1):
            cell = row[rnk_idx] if rnk_idx < len(row) else ""
            for value in split_values(cell):
                result = detect_ranking_tier(value)
                if result is None:
                    continue
                dedup_key = ("Ranking Mention", value.strip().lower())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                tier, asset_path = result
                tier_names = {1: "gold", 2: "silver", 3: "bronze"}
                start_tc = row[start_idx] if start_idx < len(row) else ""
                end_tc   = row[end_idx]   if end_idx   < len(row) else ""
                tr_num   = row[tr_idx]    if tr_idx    < len(row) else ""
                entries.append(MentionEntry(
                    column="Ranking Mention",
                    value=value,
                    tag="RNK",
                    type_name=tier_names[tier],
                    asset=asset_path,
                    row_index=row_num,
                    transcript_number=tr_num.strip() or None,
                    start_timecode=start_tc.strip() or None,
                    end_timecode=end_tc.strip() or None,
                    start_seconds=parse_timecode(start_tc, frame_rate),
                    end_seconds=parse_timecode(end_tc, frame_rate),
                ))

    return entries


# ---------------------------------------------------------------------------
# Video rendering
# ---------------------------------------------------------------------------

def render_asset_base(
    asset: Path,
    output_path: Path,
    volume_multiplier: float = 1.0,
) -> bool:
    """Copy asset as ProRes 4444 without any text overlay."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(asset),
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "prores_ks",
        "-profile:v", "4",
        "-pix_fmt", "yuva444p10le",
    ]
    if volume_multiplier != 1.0:
        cmd += ["-af", f"volume={volume_multiplier:.6f}", "-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "copy"]
    cmd.append(str(output_path))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"      FFmpeg error: {proc.stderr.strip()[-300:]}")
    return proc.returncode == 0


def render_transparent_base(
    output_path: Path,
    duration: float = TEXT_ONLY_DURATION,
    width: int = 1920,
    height: int = 1080,
) -> bool:
    """Create a transparent base clip for later text compositing."""
    filter_graph = (
        f"color=c=black@0.0:size={width}x{height}:duration={duration}:rate=25,"
        "format=yuva444p10le"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", filter_graph,
        "-c:v", "prores_ks",
        "-profile:v", "4",
        "-pix_fmt", "yuva444p10le",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"      FFmpeg error: {proc.stderr.strip()[-300:]}")
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Timing-based rename and move to Insert
# ---------------------------------------------------------------------------

def build_insert_filename(
    entry: MentionEntry,
    seq_num: int,
    occurrence: int,
) -> str:
    """Build Insert-style filename: {min}m{sec:02d}_{occ}_{type}_{seq:03d}_{TAG}.mov"""
    secs = entry.start_seconds if entry.start_seconds is not None else 0.0
    prefix = seconds_to_insert_prefix(secs)
    return f"{prefix}_{occurrence}_{entry.type_name}_{seq_num:03d}_{entry.tag}.mov"


def seconds_to_insert_prefix(seconds: float) -> str:
    total = int(max(0.0, seconds))
    return f"{total // 60}m{total % 60:02d}"


def rename_and_move(
    src: Path,
    entry: MentionEntry,
    seq_num: int,
    occurrence: int,
    insert_dir: Path,
) -> Optional[Path]:
    """Rename clip with timeline prefix and move to Insert."""
    dest_name = build_insert_filename(entry, seq_num, occurrence)
    dest = insert_dir / dest_name
    # Avoid overwriting an existing file — bump occurrence
    counter = occurrence
    while dest.exists():
        counter += 1
        dest_name = build_insert_filename(
            MentionEntry(**{**entry.__dict__, **{}}), seq_num, counter
        )
        dest = insert_dir / dest_name
    try:
        shutil.move(str(src), dest)
        return dest
    except OSError as exc:
        print(f"      Move failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render illustration clips for measurement and marker mentions."
    )
    parser.add_argument("--input-csv", type=Path,
                        help="Path to *_comparison*.csv (defaults to latest in Comparser/output).")
    parser.add_argument("--timing-manifest", type=Path,
                        help="Canonical timed_AI_illustrator manifest CSV. When provided, it replaces row-level CSV timing for supported categories.")
    parser.add_argument("--output-dir", type=Path,
                        help="Override output directory.")
    parser.add_argument("--frame-rate", type=float, default=25.0,
                        help="Frame rate for timecode parsing (default: 25).")
    parser.add_argument("--no-move", action="store_true",
                        help="Skip renaming / moving clips to Insert.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be positive.")

    csv_path = args.input_csv or find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    print(f"\n==> Source CSV: {csv_path}")

    if args.timing_manifest:
        manifest_path = args.timing_manifest.expanduser().resolve()
        print(f"    Timing manifest: {manifest_path}")
        entries = collect_entries_from_timing_manifest(load_manifest_rows(manifest_path))
    else:
        header, rows = load_csv(csv_path)
        header_map = build_header_map(header)
        entries = collect_entries(rows, header_map, args.frame_rate)
    if not entries:
        print("No relevant mentions found in the CSV.")
        return

    print(f"    {len(entries)} mention(s) found across all types.")

    # Prepare output directory
    output_dir = args.output_dir or OUTPUT_DIR
    media_dir = output_dir / f"{csv_path.stem}_mentions_media"
    video_dir = media_dir / "videos"
    base_video_dir = media_dir / "base_videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    base_video_dir.mkdir(parents=True, exist_ok=True)

    # Sort entries by start time for deterministic ordering
    entries.sort(key=lambda e: (e.start_seconds or 0.0, e.row_index))

    # Track occurrence per rounded-second bucket (across all types)
    occurrence_tracker: Dict[int, int] = {}

    # Track seq num per (tag, type_name) combo
    seq_tracker: Dict[str, int] = {}

    records = []
    insert_dir = INSERT_DIR if not args.no_move else None
    if insert_dir:
        insert_dir.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        bucket = int(entry.start_seconds or 0.0)
        occurrence_tracker[bucket] = occurrence_tracker.get(bucket, 0) + 1
        occurrence = occurrence_tracker[bucket]

        seq_key = f"{entry.tag}_{entry.type_name}"
        seq_tracker[seq_key] = seq_tracker.get(seq_key, 0) + 1
        seq_num = seq_tracker[seq_key]

        temp_name = f"{entry.type_name}_{seq_num:03d}_{entry.tag}.mov"
        output_path = video_dir / temp_name
        base_output_path = base_video_dir / temp_name

        print(f"\n  [{entry.tag}] {entry.type_name} #{seq_num:03d} — {entry.value!r}")
        print(f"       timecode: {entry.start_timecode} → {entry.end_timecode}")

        # ── Decide rendering strategy ──────────────────────────────────────
        is_marker = entry.tag in ("SOC", "RNK", "PNC")

        success = False
        if is_marker:
            # No text overlay: just copy/convert the asset
            if entry.asset and entry.asset.exists():
                success = render_asset_base(
                    entry.asset,
                    output_path,
                    volume_multiplier=DEFAULT_INSERT_AUDIO_MULTIPLIER,
                )
            else:
                print(f"      Asset not found: {entry.asset} — skipping.")
        elif entry.asset is not None:
            # Measurement type with background asset only; text is composited later.
            if entry.asset.exists():
                success = render_asset_base(
                    entry.asset,
                    base_output_path,
                    volume_multiplier=DEFAULT_INSERT_AUDIO_MULTIPLIER,
                )
            else:
                print(f"      Asset not found: {entry.asset} — skipping.")
        else:
            # Text-only base clip; text is composited later.
            success = render_transparent_base(base_output_path)

        final_path = None
        if success and output_path.exists():
            if insert_dir is not None:
                final_path = rename_and_move(output_path, entry, seq_num, occurrence, insert_dir)
                if final_path:
                    print(f"       → Insert: {final_path.name}")
            else:
                final_path = output_path

        records.append({
            "column": entry.column,
            "value": entry.value,
            "overlay_text": entry.value,
            "tag": entry.tag,
            "type_name": entry.type_name,
            "entry_id": entry.entry_id,
            "illustration_type": entry.illustration_type,
            "seq_num": seq_num,
            "occurrence": occurrence,
            "row_index": entry.row_index,
            "transcript_number": entry.transcript_number,
            "start_timecode": entry.start_timecode,
            "end_timecode": entry.end_timecode,
            "start_seconds": entry.start_seconds,
            "base_video_path": str(base_output_path) if (success and not is_marker) else None,
            "video_path": (
                str(final_path)
                if final_path
                else (str(output_path) if success else None)
            ),
            "needs_text_layer": not is_marker,
            "success": success,
        })

    # Write manifest
    manifest_path = video_dir.parent / f"{csv_path.stem}_mentions_manifest.json"
    manifest_path.write_text(
        json.dumps({"source_csv": str(csv_path), "clips": records}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    ok = sum(1 for r in records if r["success"])
    print(f"\n==> Done: {ok}/{len(records)} clip(s) rendered successfully.")
    print(f"    Manifest: {manifest_path}")
    if insert_dir:
        print(f"    Insert dir: {insert_dir}")
    else:
        print(f"    Videos dir: {video_dir}")


if __name__ == "__main__":
    main()
