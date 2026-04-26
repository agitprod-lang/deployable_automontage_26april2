#!/usr/bin/env python3
"""Create transparent money base clips and manifest entries from the comparser CSV output."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
MONEY_ANIMATION = Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/asset/midjourney_animation/universal_fr/key numbers/numbers_with_transparent_background/moneycaching_bg.mov").resolve()
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
CURRENCY_SYMBOL_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "$": ("usd", "dollar", "dollars", "buck", "bucks"),
    "US$": ("us$", "us-dollar"),
    "€": ("eur", "euro", "euros"),
    "£": ("gbp", "pound", "pounds", "sterling"),
    "¥": ("cny", "jpy", "yen", "yens", "yuan", "renminbi"),
    "₩": ("krw", "won"),
    "₹": ("inr", "rupee", "rupees"),
    "₽": ("rub", "ruble", "rubles", "rouble", "roubles"),
    "₺": ("try", "lira", "lire"),
    "₫": ("vnd", "dong"),
    "₦": ("ngn", "naira"),
    "₱": ("php", "mxn", "ars", "clp", "cop", "peso", "pesos"),
    "R$": ("brl", "real", "reais"),
    "C$": ("cad", "canadian"),
    "A$": ("aud", "australian"),
    "NZ$": ("nzd", "new", "zealand"),
    "CHF": ("chf", "franc", "francs"),
    "kr": ("sek", "nok", "dkk", "krona", "kronor", "krone", "kroner"),
    "HK$": ("hkd",),
    "S$": ("sgd",),
    "₪": ("ils", "shekel", "shekels"),
    "zł": ("pln", "zloty", "zlotys", "zloties"),
    "Ft": ("huf", "forint"),
    "lei": ("ron", "lei"),
    "лв": ("bgn", "lev"),
    "₴": ("uah", "hryvnia"),
    "₭": ("lak", "kip"),
    "฿": ("thb", "baht"),
    "₮": ("mnt", "tugrik"),
    "₸": ("kzt", "tenge"),
    "د.إ": ("aed", "dirham"),
    "ر.س": ("sar", "riyals", "riyāl"),
    "د.ك": ("kwd", "dinar"),
    "ر.ق": ("qar",),
}
SYMBOL_SEARCH_ORDER = sorted(CURRENCY_SYMBOL_KEYWORDS.keys(), key=len, reverse=True)
AMBIGUOUS_CURRENCY_WORDS = {"new", "zealand"}
_CURRENCY_STRIP_PATTERNS: Dict[str, tuple[re.Pattern | None, re.Pattern | None, re.Pattern | None, re.Pattern | None]] = {}
AMOUNT_MAGNITUDE_KEYWORDS = tuple(
    sorted(
        (
            "mille",
            "thousand",
            "thousands",
            "hundred",
            "hundreds",
            "million",
            "millions",
            "milliard",
            "milliards",
            "billion",
            "billions",
            "trillion",
            "trillions",
            "k",
            "m",
            "b",
            "bn",
            "mds",
        ),
        key=len,
        reverse=True,
    )
)
AMOUNT_PATTERN = re.compile(
    r"\d[\d.,]*(?:\s\d[\d.,]*)*(?:\s*(?:"
    + "|".join(AMOUNT_MAGNITUDE_KEYWORDS)
    + r"))?",
    re.IGNORECASE,
)
AMOUNT_MAGNITUDE_PATTERN = re.compile("|".join(AMOUNT_MAGNITUDE_KEYWORDS), re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:)]}\"'»«“”‘’"
DEFAULT_INSERT_AUDIO_REDUCTION_DB = -10.0
DEFAULT_INSERT_AUDIO_MULTIPLIER = math.pow(10.0, DEFAULT_INSERT_AUDIO_REDUCTION_DB / 20.0)


@dataclass
class MoneyEntry:
    raw_value: str
    display_text: str
    row_index: int
    transcript_number: str | None
    start_timecode: str | None
    end_timecode: str | None
    start_seconds: float | None
    end_seconds: float | None
    text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render transparent base money clips and save overlay metadata from the universal comparser CSV."
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Path to *_comparison*.csv (defaults to the most recent file under Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for rendered videos (defaults to insert_creator/output/<stem>_money_media).",
    )
    parser.add_argument(
        "--animation",
        type=Path,
        default=MONEY_ANIMATION,
        help="Base money animation with transparency (default: moneycaching_bg.mov).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=25.0,
        help="Frame rate used to convert HH:MM:SS:FF timecodes into seconds (default: 25.0).",
    )
    return parser.parse_args()


def find_latest_comparison_csv(directory: Path) -> Path:
    if not directory.exists():
        raise FileNotFoundError(f"{directory} does not exist.")
    candidates = [
        path
        for path in directory.rglob("*comparison.csv")
        if path.is_file()
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No *_comparison.csv files found in {directory}")
    return candidates[0]


def load_csv(path: Path) -> tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:  # pragma: no cover - CSV always has rows
            raise RuntimeError(f"{path} does not contain any rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, column in enumerate(header):
        mapping[column.strip().lower()] = idx
    return mapping


def require_column(header_map: Mapping[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' is missing from the CSV.")
    return header_map[key]


def split_multi_value(value: str | None) -> List[str]:
    if not value:
        return []
    parts = value.split("|")
    cleaned: List[str] = []
    for fragment in parts:
        text = fragment.strip().strip('"').strip()
        if text:
            cleaned.append(text)
    return cleaned


def parse_timecode(value: str | None, frame_rate: float) -> float | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    pieces = candidate.split(":")
    try:
        if len(pieces) == 4:
            hours, minutes, seconds, frames = map(int, pieces)
            base = hours * 3600 + minutes * 60 + seconds
            return base + (frames / frame_rate if frame_rate > 0 else 0.0)
        if len(pieces) == 3:
            hours, minutes, seconds = map(int, pieces)
            return hours * 3600 + minutes * 60 + seconds
        if len(pieces) == 2:
            minutes, seconds = map(int, pieces)
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


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


def prepare_output_dirs(base_dir: Path, stem: str) -> tuple[Path, Path, Path, Path]:
    base_output = base_dir / f"{stem}_money_media"
    base_video_dir = base_output / "base_videos"
    video_dir = base_output / "videos"
    manifest_path = base_output / f"{stem}_money_manifest.json"
    base_output.mkdir(parents=True, exist_ok=True)
    base_video_dir.mkdir(exist_ok=True)
    video_dir.mkdir(exist_ok=True)
    return base_output, base_video_dir, video_dir, manifest_path


def _should_strip_keyword(keyword: str) -> bool:
    return keyword.lower() not in AMBIGUOUS_CURRENCY_WORDS


def _get_strip_patterns(symbol: str) -> tuple[re.Pattern | None, re.Pattern | None, re.Pattern | None, re.Pattern | None]:
    if symbol in _CURRENCY_STRIP_PATTERNS:
        return _CURRENCY_STRIP_PATTERNS[symbol]
    keywords = [kw for kw in CURRENCY_SYMBOL_KEYWORDS.get(symbol, ()) if _should_strip_keyword(kw)]
    if not keywords:
        patterns = (None, None, None, None)
        _CURRENCY_STRIP_PATTERNS[symbol] = patterns
        return patterns
    escaped_keywords = sorted((re.escape(keyword) for keyword in keywords), key=len, reverse=True)
    escaped = "|".join(escaped_keywords)
    apostrophe_pattern = re.compile(rf"(?i)\b(?:d|l)['’]\s*(?:{escaped})\b")
    connector_pattern = re.compile(
        rf"(?i)\b(?:de|des|du|del|dela|della|dei|da|do|dos|das|of|the|pour|par|per|por|en|au|aux)\b\s+(?:{escaped})\b"
    )
    trailing_number_pattern = re.compile(rf"(?i)(\d[\d\s.,]*)\s+(?:{escaped})\b")
    standalone_pattern = re.compile(rf"(?i)\b(?:{escaped})\b")
    patterns = (apostrophe_pattern, connector_pattern, trailing_number_pattern, standalone_pattern)
    _CURRENCY_STRIP_PATTERNS[symbol] = patterns
    return patterns


def strip_currency_words(value: str, symbol: str) -> str:
    apostrophe_pattern, connector_pattern, trailing_number_pattern, standalone_pattern = _get_strip_patterns(symbol)
    text = value
    if apostrophe_pattern:
        text = apostrophe_pattern.sub("", text)
    if connector_pattern:
        text = connector_pattern.sub("", text)
    if trailing_number_pattern:
        text = trailing_number_pattern.sub(lambda match: match.group(1), text)
    if standalone_pattern:
        text = standalone_pattern.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _insert_symbol_at(text: str, index: int, symbol: str) -> str:
    before = text[:index]
    after = text[index:]
    result = before
    if result and not result.endswith(" "):
        result += " "
    result += symbol
    if after:
        if after.startswith(" "):
            result += after
        elif after[0] in TRAILING_PUNCTUATION:
            result += after
        else:
            result += " " + after
    return re.sub(r"\s{2,}", " ", result).strip()


def _append_symbol_without_amount(text: str, symbol: str) -> str:
    stripped = text.rstrip()
    idx = len(stripped)
    while idx > 0 and stripped[idx - 1] in TRAILING_PUNCTUATION:
        idx -= 1
    punctuation = stripped[idx:]
    body = stripped[:idx]
    result = body
    if result and not result.endswith(" "):
        result += " "
    result += symbol
    result += punctuation
    return result.strip()


def insert_symbol_after_amount(value: str, symbol: str) -> str:
    matches: List[tuple[Tuple[int, int], bool]] = []
    for match in AMOUNT_PATTERN.finditer(value):
        token = match.group(0).strip()
        if not token:
            continue
        has_magnitude = bool(AMOUNT_MAGNITUDE_PATTERN.search(token))
        matches.append((match.span(), has_magnitude))
    if not matches:
        return _append_symbol_without_amount(value, symbol)
    for span, has_magnitude in reversed(matches):
        if has_magnitude:
            return _insert_symbol_at(value, span[1], symbol)
    return _insert_symbol_at(value, matches[-1][0][1], symbol)


def detect_currency_symbol(value: str) -> tuple[str | None, bool]:
    for symbol in SYMBOL_SEARCH_ORDER:
        if symbol.isalpha():
            if re.search(rf"\b{re.escape(symbol)}\b", value):
                return symbol, True
        elif symbol and symbol in value:
            return symbol, True
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    tokens = normalized.split()
    for token in tokens:
        for symbol, keywords in CURRENCY_SYMBOL_KEYWORDS.items():
            if token in keywords:
                return symbol, False
    return None, False


def format_display_text(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    symbol, _ = detect_currency_symbol(cleaned)
    if not symbol:
        return cleaned
    if symbol.isalpha():
        without_symbol = re.sub(rf"\b{re.escape(symbol)}\b", "", cleaned, flags=re.IGNORECASE)
    else:
        without_symbol = cleaned.replace(symbol, "")
    stripped_text = strip_currency_words(without_symbol, symbol)
    if not stripped_text:
        return symbol
    return insert_symbol_after_amount(stripped_text, symbol)


def is_money_candidate(value: str) -> bool:
    if not value or not re.search(r"\d", value):
        return False
    symbol, _ = detect_currency_symbol(value)
    return bool(symbol)


def merge_currency_tokens(words: List[str]) -> List[str]:
    if not words:
        return words
    merged: List[str] = []
    for word in words:
        if merged and word in SYMBOL_SEARCH_ORDER:
            merged[-1] = f"{merged[-1]} {word}"
        else:
            merged.append(word)
    return merged


def probe_video_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        try:
            value = float(result.stdout.strip())
            if value > 0:
                return value
        except ValueError:
            pass
    return 4.0


def collect_money_entries(
    rows: Sequence[Sequence[str]],
    header_map: Mapping[str, int],
    frame_rate: float,
) -> List[MoneyEntry]:
    money_idx = require_column(header_map, "Money Mention")
    number_idx = header_map.get("number mention")
    start_idx = require_column(header_map, "Start Time")
    end_idx = require_column(header_map, "End Time")
    text_idx = require_column(header_map, "Text")
    transcript_idx = require_column(header_map, "Transcript #")
    entries: List[MoneyEntry] = []
    for row_number, row in enumerate(rows, start=1):
        money_cell = row[money_idx] if money_idx < len(row) else ""
        values: List[str] = split_multi_value(money_cell)
        if number_idx is not None and number_idx < len(row):
            number_cell = row[number_idx]
            for fragment in split_multi_value(number_cell):
                if is_money_candidate(fragment):
                    values.append(fragment)
        if not values:
            continue
        start_time = row[start_idx] if start_idx < len(row) else ""
        end_time = row[end_idx] if end_idx < len(row) else ""
        text_value = row[text_idx] if text_idx < len(row) else ""
        transcript_number = row[transcript_idx] if transcript_idx < len(row) else ""
        seen_displays: Set[str] = set()
        for value in values:
            display_text = format_display_text(value)
            normalized_display = display_text.strip().lower()
            if not normalized_display or normalized_display in seen_displays:
                continue
            seen_displays.add(normalized_display)
            entries.append(
                MoneyEntry(
                    raw_value=value,
                    display_text=display_text,
                    row_index=row_number,
                    transcript_number=transcript_number.strip() or None,
                    start_timecode=start_time.strip() or None,
                    end_timecode=end_time.strip() or None,
                    start_seconds=parse_timecode(start_time, frame_rate),
                    end_seconds=parse_timecode(end_time, frame_rate),
                    text=text_value,
                )
            )
    return entries


def render_money_videos(
    entries: Sequence[MoneyEntry],
    animation_path: Path,
    base_video_dir: Path,
    video_dir: Path,
) -> List[Dict[str, object]]:
    if not animation_path.exists():
        raise FileNotFoundError(f"Animation clip not found at {animation_path}")
    clip_duration = probe_video_duration(animation_path)
    results: List[Dict[str, object]] = []
    for idx, entry in enumerate(entries, start=1):
        base_video_path = base_video_dir / f"money_{idx:03d}.mov"
        output_path = video_dir / f"money_{idx:03d}.mov"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(animation_path),
            "-map",
            "0:v",
            "-map",
            "0:a?",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4444",
            "-pix_fmt",
            "yuva444p10le",
            "-af",
            f"volume={DEFAULT_INSERT_AUDIO_MULTIPLIER:.6f}",
            "-c:a",
            "pcm_s16le",
            str(base_video_path),
        ]
        print(f"   💰 money_{idx:03d}.mov ({entry.display_text})")
        result = subprocess.run(cmd, capture_output=True, text=True)
        success = result.returncode == 0
        if not success:
            print(f"      ⚠️  FFmpeg error: {result.stderr.strip()}")
        results.append(
            {
                "id": idx,
                "value": entry.raw_value,
                "display_text": entry.display_text,
                "overlay_text": entry.display_text,
                "base_video_path": str(base_video_path) if success else None,
                "video_path": str(output_path) if success else None,
                "row_index": entry.row_index,
                "transcript_number": entry.transcript_number,
                "start_timecode": entry.start_timecode,
                "end_timecode": entry.end_timecode,
                "start_seconds": entry.start_seconds,
                "end_seconds": entry.end_seconds,
                "duration_seconds": clip_duration,
                "needs_text_layer": True,
                "source_text": entry.text,
                "success": success,
            }
        )
    return results


def write_manifest(manifest_path: Path, records: Sequence[Dict[str, object]], source_csv: Path) -> None:
    manifest = {
        "source_csv": str(source_csv),
        "videos": records,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be positive.")
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    entries = collect_money_entries(rows, header_map, args.frame_rate)
    if not entries:
        print("No Money Mention entries were found in the CSV.")
        return
    output_dir = args.output_dir if args.output_dir else OUTPUT_DIR
    base_output, base_video_dir, video_dir, manifest_path = prepare_output_dirs(output_dir, csv_path.stem)
    print(f"Rendering {len(entries)} money base clips from {csv_path.name}...")
    records = render_money_videos(
        entries,
        args.animation,
        base_video_dir,
        video_dir,
    )
    write_manifest(manifest_path, records, csv_path)
    print("\nSummary")
    print("=" * 40)
    print(f"Source CSV : {csv_path}")
    print(f"Animation  : {args.animation}")
    print(f"Base dir   : {base_video_dir}")
    print(f"Videos dir : {video_dir}")
    print(f"Manifest   : {manifest_path}")


if __name__ == "__main__":
    main()
