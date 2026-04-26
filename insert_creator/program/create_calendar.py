#!/usr/bin/env python3
"""Create calendar clips by overlaying date text on calend.mov."""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Set, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"

CALENDAR_BACKGROUND = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset"
    "/midjourney_animation/universal_fr/key numbers/calend.mov"
)

# Render dimensions of the exported calendar clip
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080

# Font sizes tuned for the rendered 1920x1080 calendar clip
DAY_FONT_SIZE = 66
MONTH_FONT_SIZE = 40
YEAR_FONT_SIZE = 40
LINE_GAP = 12
TEXT_START_TIME = 1.0  # text appears after this many seconds

# Visible paper area in the rendered 1920x1080 clip, located in the lower-left part of the frame
PAGE_TEXT_LEFT = 165
PAGE_TEXT_TOP = 548
PAGE_TEXT_WIDTH = 280
PAGE_TEXT_HEIGHT = 300
PAGE_TEXT_SHIFT_X = 28
PAGE_TEXT_SHIFT_Y = 76

DEFAULT_DURATION = 5.0
FRAME_RATE = 30
VIDEO_FPS = 24

TEXT_FONT_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/shared_assets/Glowing-Text-Animation-Effect/Montserrat-Bold.ttf"
)
TEXT_FALL_FRAMES = 4
TEXT_GLOW_FRAMES = 3
TEXT_HOLD_FRAMES = 2
TEXT_PHASE_FRAMES = TEXT_FALL_FRAMES + TEXT_GLOW_FRAMES + TEXT_HOLD_FRAMES
TEXT_FINAL_FRAMES = 32
TEXT_FALL_HEIGHT = 48
TEXT_FALL_BLUR = 8
TEXT_ZOOM_START = 1.0
TEXT_ZOOM_END = 1.08
TEXT_COLOR = (12, 12, 12)
TEXT_FALL_COLOR = (146, 98, 98)
TEXT_GLOW_COLOR = (255, 138, 138)
CARD_COLOR = (255, 214, 214, 190)
CARD_GLOW_COLOR = (255, 168, 168, 84)
CARD_PADDING_X = 22
CARD_PADDING_Y = 18
CARD_RADIUS = 28
SHADOW_COLOR = (110, 45, 45, 70)

VALUE_COLUMN_CANDIDATES = ("Number or Date", "Date Mention", "Number Mention")
LANGUAGE_COLUMN_CANDIDATES = (
    "Language",
    "Langue",
    "Detected Language",
    "Transcript Language",
    "Original Language",
)

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

_glow_cache: Dict[tuple[str, int, int, int], Image.Image] = {}

MONTH_NAMES_FR = {
    1: "janvier", 2: "fevrier", 3: "mars", 4: "avril",
    5: "mai", 6: "juin", 7: "juillet", 8: "aout",
    9: "septembre", 10: "octobre", 11: "novembre", 12: "decembre",
}
MONTH_NAMES_EN = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}
MONTH_ALIASES: Dict[str, tuple[int, str]] = {
    "jan": (1, "en"), "janv": (1, "fr"), "janvier": (1, "fr"), "january": (1, "en"),
    "feb": (2, "en"), "fev": (2, "fr"), "fevr": (2, "fr"), "fevrier": (2, "fr"), "february": (2, "en"),
    "mar": (3, "en"), "mars": (3, "fr"), "march": (3, "en"),
    "apr": (4, "en"), "avr": (4, "fr"), "avril": (4, "fr"), "april": (4, "en"),
    "mai": (5, "fr"), "may": (5, "en"),
    "jun": (6, "en"), "juin": (6, "fr"), "june": (6, "en"),
    "jul": (7, "en"), "juil": (7, "fr"), "juillet": (7, "fr"), "july": (7, "en"),
    "aug": (8, "en"), "aou": (8, "fr"), "aout": (8, "fr"), "august": (8, "en"),
    "sep": (9, "en"), "sept": (9, "fr"), "septembre": (9, "fr"), "september": (9, "en"),
    "oct": (10, "en"), "octobre": (10, "fr"), "october": (10, "en"),
    "nov": (11, "en"), "novembre": (11, "fr"), "november": (11, "en"),
    "dec": (12, "en"), "decembre": (12, "fr"), "december": (12, "en"),
}


@dataclass
class CalendarEntry:
    text: str
    row_index: int
    transcript_number: str | None
    start_timecode: str | None
    end_timecode: str | None
    start_seconds: float | None
    end_seconds: float | None
    normalized_date: date
    precision: str
    display_text: str
    day_text: str
    month_text: str
    year_text: str
    language: str | None


@dataclass(frozen=True)
class ParsedCalendarValue:
    normalized_date: date
    precision: str
    language: str | None
    display_text: str
    day_text: str
    month_text: str
    year_text: str


def precision_rank(value: str) -> int:
    if value == "day":
        return 3
    if value == "month":
        return 2
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate calendar clips by overlaying date text on calend.mov."
    )
    parser.add_argument("--input-csv", type=Path,
                        help="Path to *_comparison*.csv (defaults to latest in Comparser/output).")
    parser.add_argument("--output-dir", type=Path,
                        help="Override output directory (defaults to insert_creator/output).")
    parser.add_argument("--frame-rate", type=float, default=25.0,
                        help="Frame rate for timecode conversion (default: 25).")
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [p for p in directory.rglob("*comparison.csv") if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No *_comparison.csv files found in {directory}")
    return candidates[0]


def load_csv(path: Path) -> tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            raise RuntimeError(f"{path} has no rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    return {name.strip().lower(): idx for idx, name in enumerate(header)}


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Column '{column_name}' missing from CSV.")
    return header_map[key]


def find_optional_column(header_map: Mapping[str, int], candidates: Sequence[str]) -> int | None:
    for column_name in candidates:
        key = column_name.strip().lower()
        if key in header_map:
            return header_map[key]
    return None


def resolve_value_columns(
    header: Sequence[str],
    header_map: Mapping[str, int],
    candidates: Sequence[str],
) -> List[tuple[int, str]]:
    indices: List[tuple[int, str]] = []
    for column_name in candidates:
        key = column_name.strip().lower()
        if key in header_map:
            idx = header_map[key]
            column_label = header[idx].strip() if idx < len(header) else column_name
            indices.append((idx, column_label))
    if not indices:
        raise KeyError(f"Columns [{', '.join(candidates)}] missing from CSV.")
    return indices


def split_values(value: str | None) -> List[str]:
    if not value:
        return []
    return [f.strip().strip('"').strip() for f in value.split("|") if f.strip().strip('"').strip()]


def parse_timecode(value: str | None, frame_rate: float) -> float | None:
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        if len(parts) == 4:
            h, m, s, f = map(int, parts)
            return h * 3600 + m * 60 + s + (f / frame_rate if frame_rate > 0 else 0.0)
        if len(parts) == 3:
            h, m, s = map(int, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(int, parts)
            return m * 60 + s
    except ValueError:
        return None
    return None


def normalize_token(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def normalize_ordinals(text: str) -> str:
    text = re.sub(r"(\d+)\s*(?:er|eme|ème)", r"\1", text, flags=re.IGNORECASE)
    return re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", text, flags=re.IGNORECASE)


def convert_year(value: int) -> int:
    if value >= 1000:
        return value
    if value < 30:
        return 2000 + value
    if value < 100:
        return 1900 + value
    return value


def find_month_alias(token: str) -> tuple[int, str] | None:
    return MONTH_ALIASES.get(normalize_token(token))


def pick_month_label(month_index: int, language: str | None) -> str:
    if language == "en":
        return MONTH_NAMES_EN.get(month_index, MONTH_NAMES_FR.get(month_index, ""))
    return MONTH_NAMES_FR.get(month_index, MONTH_NAMES_EN.get(month_index, ""))


def format_display_text(normalized_date: date, precision: str, language: str | None) -> str:
    month_label = pick_month_label(normalized_date.month, language)
    if precision == "day":
        if language == "en":
            return f"{month_label} {normalized_date.day} {normalized_date.year}"
        return f"{normalized_date.day} {month_label} {normalized_date.year}"
    if precision == "month":
        return f"{month_label} {normalized_date.year}"
    return str(normalized_date.year)


def build_date_parts(normalized_date: date, precision: str, language: str | None) -> tuple[str, str, str]:
    if precision == "day":
        return str(normalized_date.day), pick_month_label(normalized_date.month, language), str(normalized_date.year)
    if precision == "month":
        return "", pick_month_label(normalized_date.month, language), str(normalized_date.year)
    return "", "", str(normalized_date.year)


def normalize_language_tag(value: str | None) -> str | None:
    token = normalize_token(value or "")
    if not token:
        return None
    if token.startswith("fr"):
        return "fr"
    if token.startswith("en"):
        return "en"
    return None


def infer_language_from_texts(*texts: str | None) -> str | None:
    token_pool: List[str] = []
    for text in texts:
        if not text:
            continue
        token_pool.extend(re.findall(r"[A-Za-zÀ-ÿ]+", text))

    for token in token_pool:
        alias = find_month_alias(token)
        if alias:
            return alias[1]

    fr_markers = {
        "le", "la", "les", "de", "des", "du", "un", "une", "et", "dans", "sur", "pour",
        "janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout",
        "septembre", "octobre", "novembre", "decembre",
    }
    en_markers = {
        "the", "a", "an", "and", "in", "on", "for", "at", "of",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
    }

    fr_score = 0
    en_score = 0
    for token in token_pool:
        normalized = normalize_token(token)
        if normalized in fr_markers:
            fr_score += 1
        if normalized in en_markers:
            en_score += 1

    if fr_score > en_score:
        return "fr"
    if en_score > fr_score:
        return "en"
    return None


def parse_numeric_date(text: str, default_year: int, language_hint: str | None) -> ParsedCalendarValue | None:
    stripped = re.sub(r"[^\d/.\-]", " ", text).strip()
    if not stripped:
        return None
    normalized = stripped.replace(".", "/").replace("-", "/")
    match = re.search(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", normalized)
    if not match:
        return None
    day, month = int(match.group(1)), int(match.group(2))
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        return None
    year = convert_year(int(match.group(3))) if match.group(3) else default_year
    try:
        normalized_date = date(year, month, day)
    except ValueError:
        return None
    return ParsedCalendarValue(
        normalized_date=normalized_date,
        precision="day",
        language=language_hint,
        display_text=format_display_text(normalized_date, "day", language_hint),
        day_text=str(normalized_date.day),
        month_text=pick_month_label(normalized_date.month, language_hint),
        year_text=str(normalized_date.year),
    )


def parse_textual_date(text: str, default_year: int, language_hint: str | None) -> ParsedCalendarValue | None:
    cleaned = normalize_ordinals(text)
    tokens = re.findall(r"[A-Za-zÀ-ÿ]+|\d{1,4}", cleaned)
    if not tokens:
        return None
    for idx, token in enumerate(tokens):
        alias = find_month_alias(token)
        if not alias:
            continue
        month_index, language = alias
        day: int | None = None
        year: int | None = None
        left = idx - 1
        while left >= 0:
            if tokens[left].isdigit() and 1 <= int(tokens[left]) <= 31:
                day = int(tokens[left])
                break
            left -= 1
        if day is None:
            right = idx + 1
            while right < len(tokens):
                if tokens[right].isdigit() and 1 <= int(tokens[right]) <= 31:
                    day = int(tokens[right])
                    break
                right += 1
        right = idx + 1
        while right < len(tokens):
            if tokens[right].isdigit():
                n = int(tokens[right])
                if n > 31 or len(tokens[right]) == 4:
                    year = convert_year(n)
                    break
            right += 1
        if day is None and year is None:
            continue
        if year is None:
            year = default_year
        placeholder_day = day if day is not None else 1
        try:
            normalized_date = date(year, month_index, placeholder_day)
        except ValueError:
            continue
        chosen_language = language or language_hint
        if day is None:
            return ParsedCalendarValue(
                normalized_date=normalized_date,
                precision="month",
                language=chosen_language,
                display_text=format_display_text(normalized_date, "month", chosen_language),
                day_text="",
                month_text=pick_month_label(normalized_date.month, chosen_language),
                year_text=str(normalized_date.year),
            )
        return ParsedCalendarValue(
            normalized_date=normalized_date,
            precision="day",
            language=chosen_language,
            display_text=format_display_text(normalized_date, "day", chosen_language),
            day_text=str(normalized_date.day),
            month_text=pick_month_label(normalized_date.month, chosen_language),
            year_text=str(normalized_date.year),
        )
    return None


def parse_year_only(text: str, language_hint: str | None) -> ParsedCalendarValue | None:
    matches = re.findall(r"\b\d{4}\b", text or "")
    for candidate in matches:
        year = convert_year(int(candidate))
        try:
            normalized_date = date(year, 1, 1)
        except ValueError:
            continue
        return ParsedCalendarValue(
            normalized_date=normalized_date,
            precision="year",
            language=language_hint,
            display_text=format_display_text(normalized_date, "year", language_hint),
            day_text="",
            month_text="",
            year_text=str(normalized_date.year),
        )
    return None


def parse_calendar_value(
    text: str,
    default_year: int,
    *,
    allow_year_only: bool,
    language_hint: str | None,
) -> ParsedCalendarValue | None:
    if not text or not text.strip():
        return None
    numeric = parse_numeric_date(text, default_year, language_hint)
    if numeric:
        return numeric
    textual = parse_textual_date(text, default_year, language_hint)
    if textual:
        return textual
    if allow_year_only:
        return parse_year_only(text, language_hint)
    return None


def collect_calendar_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    value_columns: Sequence[tuple[int, str]],
    frame_rate: float,
) -> List[CalendarEntry]:
    start_idx = require_column(header_map, "Start Time")
    end_idx = require_column(header_map, "End Time")
    transcript_idx = require_column(header_map, "Transcript #")
    text_idx = find_optional_column(header_map, ("Text",))
    reference_idx = find_optional_column(header_map, ("Reference Segment",))
    language_idx = find_optional_column(header_map, LANGUAGE_COLUMN_CANDIDATES)
    today = date.today()
    entries: List[CalendarEntry] = []
    for row_number, row in enumerate(rows, start=1):
        values: List[tuple[str, str]] = []
        for value_idx, column_label in value_columns:
            if value_idx >= len(row):
                continue
            for fragment in split_values(row[value_idx]):
                values.append((fragment, column_label))
        if not values:
            continue
        start_time = row[start_idx] if start_idx < len(row) else ""
        end_time = row[end_idx] if end_idx < len(row) else ""
        start_seconds = parse_timecode(start_time, frame_rate)
        end_seconds = parse_timecode(end_time, frame_rate)
        transcript_number = row[transcript_idx] if transcript_idx < len(row) else ""
        text_value = row[text_idx] if text_idx is not None and text_idx < len(row) else ""
        reference_value = row[reference_idx] if reference_idx is not None and reference_idx < len(row) else ""
        language_value = row[language_idx] if language_idx is not None and language_idx < len(row) else ""
        row_language = normalize_language_tag(language_value)
        grouped_candidates: Dict[Tuple[int, int, object], CalendarEntry] = {}
        for value, column_label in values:
            allow_year_only = "date" in column_label.lower()
            language_hint = row_language or infer_language_from_texts(
                value,
                text_value,
                reference_value,
            )
            parsed = parse_calendar_value(
                value,
                today.year,
                allow_year_only=allow_year_only,
                language_hint=language_hint,
            )
            if not parsed:
                continue
            entry = CalendarEntry(
                text=parsed.display_text,
                row_index=row_number,
                transcript_number=transcript_number.strip() or None,
                start_timecode=start_time.strip() or None,
                end_timecode=end_time.strip() or None,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                normalized_date=parsed.normalized_date,
                precision=parsed.precision,
                display_text=parsed.display_text,
                day_text=parsed.day_text,
                month_text=parsed.month_text,
                year_text=parsed.year_text,
                language=parsed.language or language_hint,
            )
            if entry.precision == "day":
                group_key: Tuple[int, int, object] = (
                    entry.normalized_date.year,
                    row_number,
                    ("day", entry.normalized_date.month, entry.normalized_date.day),
                )
            elif entry.precision == "month":
                group_key = (
                    entry.normalized_date.year,
                    row_number,
                    ("month", entry.normalized_date.month),
                )
            else:
                group_key = (
                    entry.normalized_date.year,
                    row_number,
                    ("year",),
                )

            existing = grouped_candidates.get(group_key)
            if existing is None:
                grouped_candidates[group_key] = entry
                continue

            existing_rank = precision_rank(existing.precision)
            entry_rank = precision_rank(entry.precision)
            existing_matches_language = existing.language == row_language and row_language is not None
            entry_matches_language = entry.language == row_language and row_language is not None

            if entry_rank > existing_rank:
                grouped_candidates[group_key] = entry
            elif entry_rank == existing_rank and entry_matches_language and not existing_matches_language:
                grouped_candidates[group_key] = entry

        row_entries = sorted(
            grouped_candidates.values(),
            key=lambda item: (
                item.start_seconds if item.start_seconds is not None else float("inf"),
                -precision_rank(item.precision),
                item.normalized_date.year,
                item.normalized_date.month,
                item.normalized_date.day,
            ),
        )

        selected_entries: List[CalendarEntry] = []
        for entry in row_entries:
            dominated = False
            for chosen in selected_entries:
                if chosen.normalized_date.year != entry.normalized_date.year:
                    continue
                if chosen.row_index != entry.row_index:
                    continue
                if chosen.start_timecode != entry.start_timecode:
                    continue
                if precision_rank(chosen.precision) <= precision_rank(entry.precision):
                    continue
                if entry.precision == "year":
                    dominated = True
                    break
                if entry.precision == "month" and chosen.normalized_date.month == entry.normalized_date.month:
                    dominated = True
                    break
            if dominated:
                continue
            selected_entries.append(entry)

        entries.extend(selected_entries)
    return entries


def find_font_file() -> str:
    if TEXT_FONT_PATH.exists():
        return str(TEXT_FONT_PATH)
    for path in IMPACT_PATHS:
        if Path(path).exists():
            return path
    if Path(POPPINS_FALLBACK).exists():
        return POPPINS_FALLBACK
    for path in SYSTEM_FALLBACKS:
        if Path(path).exists():
            return path
    return "/System/Library/Fonts/Helvetica.ttc"


def load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path, size)


def rgb_to_rgba(rgb_img: Image.Image) -> Image.Image:
    r, g, b = rgb_img.split()
    alpha = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return Image.merge("RGBA", (r, g, b, alpha))


def get_glow_rgba(text: str, x: int, y: int, font: ImageFont.FreeTypeFont, color: tuple[int, int, int]) -> Image.Image:
    key = (text, x, y, font.size)
    if key not in _glow_cache:
        base = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0))
        ImageDraw.Draw(base).text((x, y), text, font=font, fill=color)
        result = base.copy()
        for radius in (3, 6, 12, 20):
            result = ImageChops.add(result, base.filter(ImageFilter.GaussianBlur(radius)))
        _glow_cache[key] = rgb_to_rgba(result)
    return _glow_cache[key]


def make_fall_layer_rgba(
    text: str,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont,
    phase: int,
) -> Image.Image:
    t = phase / max(TEXT_FALL_FRAMES, 1)
    ease = 1 - (1 - t) ** 3
    y_off = int(TEXT_FALL_HEIGHT * (1 - ease))
    blur = TEXT_FALL_BLUR * (1 - t)

    layer = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0))
    ImageDraw.Draw(layer).text((x, y - y_off), text, font=font, fill=TEXT_FALL_COLOR)
    if blur > 0.5:
        layer = layer.filter(ImageFilter.GaussianBlur(radius=blur))
    return rgb_to_rgba(layer)


def apply_zoom(img: Image.Image, zoom: float, cx: float, cy: float) -> Image.Image:
    inv = 1.0 / zoom
    a, b, c = inv, 0.0, cx * (1.0 - inv)
    d, e, f = 0.0, inv, cy * (1.0 - inv)
    return img.transform(
        (VIDEO_WIDTH, VIDEO_HEIGHT),
        Image.AFFINE,
        (a, b, c, d, e, f),
        resample=Image.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


def measure_text(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    probe = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def build_calendar_lines(entry: CalendarEntry, font_path: str) -> list[dict[str, object]]:
    specs: list[tuple[str, int]] = []
    if entry.day_text:
        specs.append((entry.day_text, DAY_FONT_SIZE))
    if entry.month_text:
        specs.append((entry.month_text, MONTH_FONT_SIZE))
    if entry.year_text:
        specs.append((entry.year_text, YEAR_FONT_SIZE))

    lines: list[dict[str, object]] = []
    for text, font_size in specs:
        font = load_font(font_path, font_size)
        width, height = measure_text(text, font)
        lines.append(
            {
                "text": text,
                "font": font,
                "width": width,
                "height": height,
            }
        )
    return lines


def position_calendar_lines(lines: list[dict[str, object]]) -> tuple[list[dict[str, object]], tuple[int, int, int, int]]:
    if not lines:
        raise ValueError("Calendar overlay requires at least one text line.")

    block_width = max(int(line["width"]) for line in lines)
    total_height = sum(int(line["height"]) for line in lines)
    total_height += LINE_GAP * (len(lines) - 1)

    card_width = min(PAGE_TEXT_WIDTH, block_width + 2 * CARD_PADDING_X)
    card_height = min(PAGE_TEXT_HEIGHT, total_height + 2 * CARD_PADDING_Y)
    card_left = PAGE_TEXT_LEFT + (PAGE_TEXT_WIDTH - card_width) // 2 + PAGE_TEXT_SHIFT_X
    card_top = PAGE_TEXT_TOP + (PAGE_TEXT_HEIGHT - card_height) // 2 + PAGE_TEXT_SHIFT_Y

    current_y = card_top + (card_height - total_height) // 2
    for line in lines:
        line_width = int(line["width"])
        line_height = int(line["height"])
        line["x"] = card_left + (card_width - line_width) // 2
        line["y"] = current_y
        current_y += line_height + LINE_GAP

    return lines, (card_left, card_top, card_width, card_height)


def draw_card(base: Image.Image, card_rect: tuple[int, int, int, int]) -> None:
    left, top, width, height = card_rect
    shadow = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (left + 8, top + 10, left + width + 8, top + height + 10),
        radius=CARD_RADIUS,
        fill=SHADOW_COLOR,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    base.alpha_composite(shadow)

    glow = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.rounded_rectangle(
        (left - 4, top - 4, left + width + 4, top + height + 4),
        radius=CARD_RADIUS + 6,
        fill=CARD_GLOW_COLOR,
    )
    glow = glow.filter(ImageFilter.GaussianBlur(20))
    base.alpha_composite(glow)

    card = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    card_draw = ImageDraw.Draw(card)
    card_draw.rounded_rectangle(
        (left, top, left + width, top + height),
        radius=CARD_RADIUS,
        fill=CARD_COLOR,
    )
    base.alpha_composite(card)


def render_calendar_frame(
    lines: list[dict[str, object]],
    card_rect: tuple[int, int, int, int],
    frame_num: int,
) -> Image.Image:
    total_line_frames = len(lines) * TEXT_PHASE_FRAMES
    all_lit = frame_num >= total_line_frames
    if all_lit:
        cur, phase = len(lines), 0
        zoom = TEXT_ZOOM_END
    else:
        cur = frame_num // TEXT_PHASE_FRAMES
        phase = frame_num % TEXT_PHASE_FRAMES
        t_global = frame_num / max(total_line_frames, 1)
        t_ease = t_global * t_global * (3 - 2 * t_global)
        zoom = TEXT_ZOOM_START + (TEXT_ZOOM_END - TEXT_ZOOM_START) * t_ease

    img = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw_card(img, card_rect)
    draw = ImageDraw.Draw(img)
    for index, line in enumerate(lines):
        text = str(line["text"])
        font = line["font"]
        x = int(line["x"])
        y = int(line["y"])
        if all_lit or index < cur:
            img.alpha_composite(get_glow_rgba(text, x, y, font, TEXT_GLOW_COLOR))
            draw.text((x, y), text, font=font, fill=(*TEXT_COLOR, 255))
        elif index == cur:
            if phase < TEXT_FALL_FRAMES:
                img.alpha_composite(make_fall_layer_rgba(text, x, y, font, phase))
            elif phase < TEXT_FALL_FRAMES + TEXT_GLOW_FRAMES:
                t = (phase - TEXT_FALL_FRAMES) / max(TEXT_GLOW_FRAMES, 1)
                glow = get_glow_rgba(text, x, y, font, TEXT_GLOW_COLOR)
                if t < 1.0:
                    r, g, b, a = glow.split()
                    a = ImageEnhance.Brightness(a).enhance(t)
                    glow = Image.merge("RGBA", (r, g, b, a))
                img.alpha_composite(glow)
                draw.text((x, y), text, font=font, fill=(*TEXT_COLOR, 255))
            else:
                img.alpha_composite(get_glow_rgba(text, x, y, font, TEXT_GLOW_COLOR))
                draw.text((x, y), text, font=font, fill=(*TEXT_COLOR, 255))

    left, top, width, height = card_rect
    return apply_zoom(img, zoom, left + width / 2, top + height / 2)


def probe_duration(path: Path) -> float:
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
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        try:
            duration = float(proc.stdout.strip())
            if duration > 0:
                return duration
        except ValueError:
            pass
    return DEFAULT_DURATION


def render_calendar_overlay(entry: CalendarEntry, font_path: str, destination: Path, duration: float) -> None:
    lines = build_calendar_lines(entry, font_path)
    lines, card_rect = position_calendar_lines(lines)
    start_frame = max(0, int(round(TEXT_START_TIME * VIDEO_FPS)))
    minimum_frames = start_frame + len(lines) * TEXT_PHASE_FRAMES + TEXT_FINAL_FRAMES
    total_frames = max(int(round(duration * VIDEO_FPS)), minimum_frames)

    frames_dir = Path(tempfile.mkdtemp(prefix="calendar_text_frames_"))
    try:
        for frame_num in range(total_frames):
            if frame_num < start_frame:
                frame = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
            else:
                frame = render_calendar_frame(lines, card_rect, frame_num - start_frame)
            frame.save(frames_dir / f"frame_{frame_num:05d}.png")

        cmd = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(VIDEO_FPS),
            "-i",
            str(frames_dir / "frame_%05d.png"),
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-alpha_bits",
            "16",
            str(destination),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


def composite_calendar_text(base_video: Path, entry: CalendarEntry, font_path: str, output_path: Path) -> None:
    duration = probe_duration(base_video)
    temp_dir = Path(tempfile.mkdtemp(prefix="calendar_overlay_"))
    overlay_path = temp_dir / "overlay.mov"
    rendered_path = temp_dir / "rendered.mov"
    try:
        render_calendar_overlay(entry, font_path, overlay_path, duration)
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(base_video),
            "-i",
            str(overlay_path),
            "-filter_complex",
            "[0:v]format=rgba[base];[1:v]format=rgba[text];[base][text]overlay=0:0:format=auto[out]",
            "-map",
            "[out]",
            "-map",
            "0:a?",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-c:a",
            "copy",
            "-shortest",
            str(rendered_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(rendered_path), str(output_path))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def prepare_output_dirs(base_dir: Path, stem: str) -> tuple[Path, Path]:
    base_output = base_dir / f"{stem}_calendar_media"
    video_dir = base_output / "videos"
    base_output.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(exist_ok=True)
    manifest_path = base_output / f"{stem}_calendar_manifest.json"
    return video_dir, manifest_path


def create_videos(
    entries: Sequence[CalendarEntry],
    video_dir: Path,
    font_path: str,
) -> List[Dict[str, object]]:
    if not CALENDAR_BACKGROUND.exists():
        raise FileNotFoundError(f"Background video not found: {CALENDAR_BACKGROUND}")

    results: List[Dict[str, object]] = []

    for idx, entry in enumerate(entries, start=1):
        output_path = video_dir / f"calendar_{idx:03d}.mov"
        print(f"   calendar_{idx:03d}.mov ({entry.text})")
        try:
            composite_calendar_text(CALENDAR_BACKGROUND, entry, font_path, output_path)
            success = True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError, ValueError) as exc:
            print(f"      FFmpeg error: {exc}")
            success = False
        results.append({
            "id": idx,
            "text": entry.display_text,
            "date_iso": entry.normalized_date.isoformat(),
            "day": entry.day_text,
            "month": entry.month_text,
            "year": entry.year_text,
            "video_path": str(output_path) if success else None,
            "row_index": entry.row_index,
            "transcript_number": entry.transcript_number,
            "start_timecode": entry.start_timecode,
            "end_timecode": entry.end_timecode,
        })
    return results


def write_manifest(manifest_path: Path, entries: Sequence[Dict[str, object]], source_csv: Path) -> None:
    manifest_path.write_text(
        json.dumps({"source_csv": str(source_csv), "videos": list(entries)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be positive.")
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    output_dir = args.output_dir if args.output_dir else OUTPUT_DIR
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    value_columns = resolve_value_columns(header, header_map, VALUE_COLUMN_CANDIDATES)
    entries = collect_calendar_entries(rows, header_map, value_columns, args.frame_rate)
    if not entries:
        print(f"No date entries detected inside columns: {', '.join(VALUE_COLUMN_CANDIDATES)}.")
        return
    font_file = find_font_file()
    video_dir, manifest_path = prepare_output_dirs(output_dir, csv_path.stem)
    print(f"Rendering {len(entries)} calendar clip(s)...")
    records = create_videos(entries, video_dir, font_file)
    write_manifest(manifest_path, records, csv_path)
    print(f"\nVideos : {video_dir}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
