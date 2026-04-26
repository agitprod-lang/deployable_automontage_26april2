#!/usr/bin/env python3
"""Convert comparser CSV quotes into structured video quote manifests."""

from __future__ import annotations

import argparse
import csv
from difflib import SequenceMatcher
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CODE_BASE = PROJECT_ROOT.parent
COMPARER_OUTPUT_DIR = CODE_BASE / "Comparser" / "output"
OUTPUT_DIR = PROJECT_ROOT / "output"
BACKGROUND_VIDEO = (
    CODE_BASE
    / "swisser"
    / "Universal_pipe"
    / "asset"
    / "Free 4K Motion Background - Glowing Compass.mp4"
).resolve()
QUOTE_BACKGROUND = (
    CODE_BASE
    / "swisser"
    / "Universal_pipe"
    / "asset"
    / "quotes"
    / "quote_background.mp4"
).resolve()
QUOTE_INTRO = (
    CODE_BASE
    / "swisser"
    / "Universal_pipe"
    / "asset"
    / "quotes"
    / "intro.mov"
).resolve()
QUOTE_OUTRO = (
    CODE_BASE
    / "swisser"
    / "Universal_pipe"
    / "asset"
    / "quotes"
    / "outro_quote.mov"
).resolve()
BACKGROUND_FADE_DURATION = 1.25
BACKGROUND_ALPHA = 0.85
TEXT_APPEAR_DELAY = 1.0
TEXT_FADE_DURATION = 0.4
BASE_VISIBLE_HOLD = 5.0
EXTRA_HOLD_WORD_THRESHOLD = 8
EXTRA_HOLD_PER_WORD = 0.7
MIN_MIDDLE_BACKGROUND_DURATION = 5.0
NORMALIZATION_REPLACEMENTS: Tuple[Tuple[str, str], ...] = (
    ("\r", " "),
    ("\n", " "),
    ("\t", " "),
    ("\u00a0", " "),
    ("\u202f", " "),
    ("“", '"'),
    ("”", '"'),
    ("«", '"'),
    ("»", '"'),
    ("‘", "'"),
    ("’", "'"),
)
QUOTE_STRIP_CHARS = ' "\'“”«»‹›‚„’‘'
REFERENCE_QUOTE_PATTERN = re.compile(r'[\"“«](.+?)[\"”»]', re.DOTALL)
ASSET_DURATION_CACHE: Dict[Path, float] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate video quote assets from the latest universal comparser CSV output."
    )
    parser.add_argument(
        "--input-csv",
        type=Path,
        help="Explicit path to a *_comparison*.csv file (defaults to the freshest file in Comparser/output).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for generated files (defaults to insert_creator/output).",
    )
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=25.0,
        help="Frame rate used to convert HH:MM:SS:FF timecodes to seconds (default: 25).",
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
        raise FileNotFoundError(f"No *_comparison.csv file found under {directory}")
    return candidates[0]


def load_csv(path: Path) -> Tuple[List[str], List[List[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:  # pragma: no cover
            raise RuntimeError(f"{path} does not contain any rows") from None
        rows = [list(row) for row in reader]
    return header, rows


def build_header_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for idx, name in enumerate(header):
        mapping[name.strip().lower()] = idx
    return mapping


def require_column(header_map: Dict[str, int], column_name: str) -> int:
    key = column_name.strip().lower()
    if key not in header_map:
        raise KeyError(f"Required column '{column_name}' missing from CSV header.")
    return header_map[key]


def normalize_reference_text(value: str | None) -> str:
    if not value:
        return ""
    text = value
    for src, dst in NORMALIZATION_REPLACEMENTS:
        text = text.replace(src, dst)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_quote_text(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    text = text.strip(QUOTE_STRIP_CHARS)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _quote_token_overlap(left: str, right: str) -> float:
    left_tokens = {token for token in normalize_quote_text(left).lower().split() if token}
    right_tokens = {token for token in normalize_quote_text(right).lower().split() if token}
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / float(min(len(left_tokens), len(right_tokens)))


def _choose_reference_quote_text(fragment: str, reference_segment: str) -> str | None:
    normalized_fragment = normalize_quote_text(fragment)
    if not normalized_fragment:
        return None
    best_text: str | None = None
    best_score = 0.0
    for match in REFERENCE_QUOTE_PATTERN.finditer(reference_segment or ""):
        candidate = normalize_quote_text(match.group(1))
        if not candidate:
            continue
        sequence_score = SequenceMatcher(None, normalized_fragment.lower(), candidate.lower()).ratio()
        token_score = _quote_token_overlap(normalized_fragment, candidate)
        score = max(sequence_score, token_score)
        if score > best_score:
            best_score = score
            best_text = candidate
    if best_text and best_score >= 0.7:
        return best_text
    return None


def split_quote_cell(cell_value: str | None) -> List[str]:
    if not cell_value:
        return []
    fragments = [fragment.strip() for fragment in cell_value.split("|")]
    quotes = []
    for fragment in fragments:
        cleaned = normalize_quote_text(fragment)
        if cleaned:
            quotes.append(cleaned)
    return quotes


def quote_support_span_length(fragment: str, transcript_text: str, reference_segment: str) -> int:
    normalized_fragment = normalize_quote_text(fragment)
    for candidate in (reference_segment or "", transcript_text or ""):
        normalized_candidate = normalize_quote_text(candidate)
        if normalized_fragment and normalized_fragment in normalized_candidate:
            return len(normalized_candidate)
    return len(normalized_fragment) or 10_000


def quote_has_explicit_support(fragment: str, transcript_text: str, reference_segment: str) -> bool:
    normalized_fragment = normalize_quote_text(fragment)
    return any(
        normalized_fragment and normalized_fragment in normalize_quote_text(candidate)
        for candidate in (reference_segment or "", transcript_text or "")
    )


def quote_occurrence_priority(occurrence: Dict[str, object]) -> Tuple[int, int, float, float]:
    support_span = float(occurrence.get("support_span") or 10_000)
    explicit_support = 1 if occurrence.get("explicit_support") else 0
    start_seconds = occurrence.get("start_seconds")
    start_value = float(start_seconds) if isinstance(start_seconds, (int, float)) else float("inf")
    clip_row = float(int(occurrence.get("clip_row") or 0))
    return (explicit_support, -int(support_span), -start_value, -clip_row)


def dedupe_quote_occurrences(
    occurrences: Sequence[Dict[str, object]],
    *,
    duplicate_window_seconds: float = 6.0,
) -> List[Dict[str, object]]:
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for occurrence in occurrences:
        normalized_fragment = str(occurrence.get("normalized_fragment") or "")
        if not normalized_fragment:
            continue
        grouped.setdefault(normalized_fragment, []).append(dict(occurrence))

    deduped: List[Dict[str, object]] = []
    for key_occurrences in grouped.values():
        ordered = sorted(
            key_occurrences,
            key=lambda occurrence: (
                float(occurrence.get("start_seconds")) if isinstance(occurrence.get("start_seconds"), (int, float)) else float("inf"),
                int(occurrence.get("clip_row") or 0),
            ),
        )
        if not ordered:
            continue
        cluster: List[Dict[str, object]] = [ordered[0]]
        for occurrence in ordered[1:]:
            current_start = occurrence.get("start_seconds")
            previous_start = cluster[-1].get("start_seconds")
            if (
                isinstance(current_start, (int, float))
                and isinstance(previous_start, (int, float))
                and (float(current_start) - float(previous_start)) <= duplicate_window_seconds
            ):
                cluster.append(occurrence)
                continue
            deduped.append(max(cluster, key=quote_occurrence_priority))
            cluster = [occurrence]
        deduped.append(max(cluster, key=quote_occurrence_priority))
    return sorted(
        deduped,
        key=lambda occurrence: (
            float(occurrence.get("start_seconds")) if isinstance(occurrence.get("start_seconds"), (int, float)) else float("inf"),
            int(occurrence.get("clip_row") or 0),
        ),
    )


def build_reference_offsets(rows: Sequence[Sequence[str]], ref_idx: int) -> Tuple[List[int | None], List[str], int]:
    offsets: List[int | None] = []
    normalized_refs: List[str] = []
    cursor = 0
    for row in rows:
        ref_text = row[ref_idx] if ref_idx < len(row) else ""
        normalized = normalize_reference_text(ref_text)
        if not normalized:
            offsets.append(None)
            normalized_refs.append("")
            continue
        offsets.append(cursor)
        normalized_refs.append(normalized)
        cursor += len(normalized) + 1  # +1 for a virtual separator
    return offsets, normalized_refs, cursor


def find_quote_position(normalized_reference: str, quote_text: str) -> int | None:
    if not normalized_reference or not quote_text:
        return None
    needle = normalize_reference_text(quote_text).lower()
    haystack = normalized_reference.lower()
    idx = haystack.find(needle)
    return idx if idx >= 0 else None


def parse_timecode(value: str | None, frame_rate: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    try:
        if len(parts) == 4:
            hours, minutes, seconds, frames = map(int, parts)
            base = hours * 3600 + minutes * 60 + seconds
            return base + (frames / frame_rate if frame_rate > 0 else 0.0)
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def calculate_visible_hold(text: str) -> float:
    word_count = count_words(text)
    extra_words = max(0, word_count - EXTRA_HOLD_WORD_THRESHOLD)
    return BASE_VISIBLE_HOLD + (extra_words * EXTRA_HOLD_PER_WORD)


def estimate_duration(text: str) -> float:
    visible_hold = calculate_visible_hold(text)
    duration = TEXT_APPEAR_DELAY + visible_hold + TEXT_FADE_DURATION
    return round(max(duration, TEXT_APPEAR_DELAY + BASE_VISIBLE_HOLD + TEXT_FADE_DURATION), 1)


def suggest_font_size(char_count: int) -> int:
    if char_count < 100:
        return 60
    if char_count < 200:
        return 48
    if char_count < 300:
        return 42
    return 36


def wrap_text_for_video(text: str, max_width: int = 50) -> str:
    words = text.split()
    if not words:
        return ""
    lines: List[str] = []
    current: List[str] = []
    current_len = 0
    for word in words:
        word_len = len(word)
        projected = current_len + word_len + (1 if current else 0)
        if projected <= max_width:
            current.append(word)
            current_len += word_len + (1 if current_len else 0)
        else:
            lines.append(" ".join(current))
            current = [word]
            current_len = word_len
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def build_display_text(raw_quote: str, author: str | None = None) -> str:
    text = raw_quote.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()
    text = f"« {text} »"
    wrapped = wrap_text_for_video(text)
    if author:
        return f"{wrapped}\n\n— {author}"
    return wrapped


def find_system_font() -> str:
    preferred = [
        "/Users/mathieusandana/Library/Fonts/Anaktoria.otf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in preferred:
        if Path(candidate).exists():
            return candidate
    return "/System/Library/Fonts/Helvetica.ttc"


def probe_asset_duration(path: Path) -> float:
    cached = ASSET_DURATION_CACHE.get(path)
    if cached is not None:
        return cached
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to probe duration for {path}: {result.stderr.strip()}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Unusable duration returned for {path}: {result.stdout!r}") from exc
    ASSET_DURATION_CACHE[path] = duration
    return duration


def estimate_quote_clip_duration(text: str, intro_duration: float, outro_duration: float) -> float:
    visible_hold = calculate_visible_hold(text)
    text_fade_end = TEXT_APPEAR_DELAY + visible_hold + TEXT_FADE_DURATION
    minimum_duration = max(
        intro_duration + MIN_MIDDLE_BACKGROUND_DURATION + outro_duration,
        text_fade_end + outro_duration,
    )
    return round(minimum_duration, 1)


def build_video_quotes(
    rows: Sequence[Sequence[str]],
    header_map: Dict[str, int],
    frame_rate: float,
) -> Tuple[List[Dict[str, object]], int]:
    quote_idx = require_column(header_map, "quote extracted")
    ref_idx = require_column(header_map, "reference segment")
    text_idx = require_column(header_map, "text")
    start_idx = require_column(header_map, "start time")
    end_idx = require_column(header_map, "end time")
    transcript_idx = require_column(header_map, "transcript #")

    offsets, normalized_refs, total_chars = build_reference_offsets(rows, ref_idx)
    quotes: List[Dict[str, object]] = []
    quote_occurrences: List[Dict[str, object]] = []

    for row_number, row in enumerate(rows, start=1):
        cell_value = row[quote_idx] if quote_idx < len(row) else ""
        fragments = split_quote_cell(cell_value)
        if not fragments:
            continue
        transcript_number = row[transcript_idx] if transcript_idx < len(row) else ""
        start_time = row[start_idx] if start_idx < len(row) else ""
        end_time = row[end_idx] if end_idx < len(row) else ""
        start_seconds = parse_timecode(start_time, frame_rate)
        end_seconds = parse_timecode(end_time, frame_rate)
        transcript_text = row[text_idx] if text_idx < len(row) else ""
        reference_segment = row[ref_idx] if ref_idx < len(row) else ""
        offset = offsets[row_number - 1]
        normalized_reference = normalized_refs[row_number - 1]

        for fragment in fragments:
            canonical_text = _choose_reference_quote_text(fragment, reference_segment) or fragment
            quote_occurrences.append(
                {
                    "normalized_fragment": normalize_quote_text(canonical_text).lower(),
                    "text": canonical_text,
                    "start_timecode": start_time or None,
                    "end_timecode": end_time or None,
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "transcript_number": transcript_number.strip() or None,
                    "clip_row": row_number,
                    "transcript_text": transcript_text,
                    "reference_segment": reference_segment,
                    "normalized_reference": normalized_reference,
                    "offset": offset,
                    "explicit_support": quote_has_explicit_support(canonical_text, transcript_text, reference_segment),
                    "support_span": quote_support_span_length(canonical_text, transcript_text, reference_segment),
                }
            )

    for occurrence in dedupe_quote_occurrences(quote_occurrences):
        fragment = str(occurrence.get("text") or "")
        char_count = len(fragment)
        duration = estimate_duration(fragment)
        font_size = suggest_font_size(char_count)
        char_position = None
        normalized_reference = str(occurrence.get("normalized_reference") or "")
        offset = occurrence.get("offset")
        if isinstance(offset, int):
            local_pos = find_quote_position(normalized_reference, fragment)
            if local_pos is not None:
                char_position = offset + local_pos
        quote_entry = {
            "id": len(quotes) + 1,
            "text": fragment,
            "char_count": char_count,
            "char_position": char_position,
            "estimated_duration": duration,
            "suggested_font_size": font_size,
            "position": "center",
            "start_timecode": occurrence.get("start_timecode"),
            "end_timecode": occurrence.get("end_timecode"),
            "start_seconds": occurrence.get("start_seconds"),
            "end_seconds": occurrence.get("end_seconds"),
            "transcript_number": occurrence.get("transcript_number"),
            "clip_row": occurrence.get("clip_row"),
            "transcript_text": occurrence.get("transcript_text"),
            "reference_segment": occurrence.get("reference_segment"),
        }
        quotes.append(quote_entry)
    return quotes, total_chars


def render_quote_videos(
    quotes: Sequence[Dict[str, object]],
    base_name: str,
    output_dir: Path,
) -> Tuple[Path, int, int]:
    video_dir = output_dir / f"{base_name}_video_quotes_videos"
    text_dir = video_dir / "textfiles"
    video_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(exist_ok=True)
    width = 1920
    height = 1080
    fps = 30
    font_file = find_system_font()
    success = 0
    failures = 0
    required_assets = (QUOTE_BACKGROUND, QUOTE_INTRO, QUOTE_OUTRO)
    missing_assets = [path for path in required_assets if not path.exists()]
    if missing_assets:
        raise FileNotFoundError(
            "Quote background assets not found: " + ", ".join(str(path) for path in missing_assets)
        )
    intro_duration = probe_asset_duration(QUOTE_INTRO)
    outro_duration = probe_asset_duration(QUOTE_OUTRO)
    for quote in quotes:
        quote_text = str(quote.get("text", "")).strip()
        display_text = build_display_text(quote_text, quote.get("author"))
        if not display_text:
            continue
        text_path = text_dir / f"quote_{quote['id']:03d}.txt"
        text_path.write_text(display_text, encoding="utf-8")
        font_size = int(quote.get("suggested_font_size") or suggest_font_size(len(quote_text)))
        duration = max(
            float(quote.get("estimated_duration") or 0.0),
            estimate_quote_clip_duration(quote_text, intro_duration, outro_duration),
        )
        output_path = video_dir / f"quote_{quote['id']:03d}.mov"
        visible_hold = calculate_visible_hold(quote_text)
        text_start = TEXT_APPEAR_DELAY
        text_hold_end = text_start + visible_hold
        text_fade_end = text_hold_end + TEXT_FADE_DURATION
        text_alpha = (
            f"if(lt(t\\,{text_start:.3f})\\,0\\,"
            f"if(lt(t\\,{text_hold_end:.3f})\\,1\\,"
            f"if(lt(t\\,{text_fade_end:.3f})\\,({text_fade_end:.3f}-t)/{TEXT_FADE_DURATION:.3f}\\,0)))"
        )
        body_duration = max(0.0, duration - intro_duration - outro_duration)
        outro_start = text_fade_end
        filter_graph = (
            f"color=c=black@0.0:size={width}x{height}:rate={fps}:d={duration:.6f},format=rgba[base];"
            f"[0:v]scale={width}:{height},trim=0:{body_duration:.6f},setpts=PTS-STARTPTS+{intro_duration:.6f}/TB[body];"
            "[1:v]format=rgba,setpts=PTS-STARTPTS[intro];"
            f"[2:v]format=rgba,setpts=PTS-STARTPTS+{outro_start:.6f}/TB[outro];"
            "[base][body]overlay=eof_action=pass:repeatlast=0[bg0];"
            "[bg0][intro]overlay=eof_action=pass:repeatlast=0[bg1];"
            "[bg1][outro]overlay=eof_action=pass:repeatlast=0[comp];"
            "[comp]"
            "drawtext="
            f"fontfile={font_file}:"
            f"textfile={text_path}:"
            "fontcolor=white:"
            f"fontsize={font_size}:"
            "shadowcolor=black:"
            "shadowx=4:"
            "shadowy=4:"
            "x=(w-text_w)/2:"
            "y=(h-text_h)/2:"
            f"alpha='{text_alpha}',"
            "format=rgba[vout]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(QUOTE_BACKGROUND),
            "-i",
            str(QUOTE_INTRO),
            "-i",
            str(QUOTE_OUTRO),
            "-filter_complex",
            filter_graph,
            "-map",
            "[vout]",
            "-r",
            str(fps),
            "-an",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4",
            "-pix_fmt",
            "yuva444p10le",
            "-t",
            str(duration),
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            failures += 1
            print(f"Failed to create video for quote #{quote['id']}: {result.stderr.strip()}")
        else:
            success += 1
    return video_dir, success, failures


def write_manifest(
    quotes: Sequence[Dict[str, object]],
    csv_path: Path,
    output_dir: Path,
    total_chars: int,
    frame_rate: float,
) -> Tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = csv_path.stem
    json_path = output_dir / f"{base_name}_video_quotes.json"
    csv_out_path = output_dir / f"{base_name}_video_quotes.csv"
    text_dir = output_dir / f"{base_name}_video_quotes_txt"
    text_dir.mkdir(exist_ok=True)

    manifest = {
        "source_csv": str(csv_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_quotes": len(quotes),
        "total_chars": total_chars,
        "frame_rate": frame_rate,
        "quotes": quotes,
    }
    json_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    with csv_out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "text",
                "char_count",
                "estimated_duration",
                "start_timecode",
                "end_timecode",
                "start_seconds",
                "end_seconds",
                "transcript_number",
                "clip_row",
            ]
        )
        for quote in quotes:
            writer.writerow(
                [
                    quote.get("id"),
                    quote.get("text"),
                    quote.get("char_count"),
                    quote.get("estimated_duration"),
                    quote.get("start_timecode"),
                    quote.get("end_timecode"),
                    quote.get("start_seconds"),
                    quote.get("end_seconds"),
                    quote.get("transcript_number"),
                    quote.get("clip_row"),
                ]
            )

    for quote in quotes:
        file_path = text_dir / f"quote_{quote['id']:03d}.txt"
        file_path.write_text(quote["text"], encoding="utf-8")

    return json_path, csv_out_path, text_dir


def main() -> None:
    args = parse_args()
    if args.frame_rate <= 0:
        raise ValueError("--frame-rate must be greater than 0.")
    csv_path = args.input_csv if args.input_csv else find_latest_comparison_csv(COMPARER_OUTPUT_DIR)
    output_dir = args.output_dir if args.output_dir else OUTPUT_DIR
    header, rows = load_csv(csv_path)
    header_map = build_header_map(header)
    quotes, total_chars = build_video_quotes(rows, header_map, args.frame_rate)
    if not quotes:
        print(f"No quotes found in {csv_path}.")
        return
    json_path, csv_out_path, text_dir = write_manifest(quotes, csv_path, output_dir, total_chars, args.frame_rate)
    video_dir, success, failures = render_quote_videos(quotes, csv_path.stem, output_dir)
    print(f"Source CSV     : {csv_path}")
    print(f"Quotes found   : {len(quotes)}")
    print(f"JSON manifest  : {json_path}")
    print(f"CSV summary    : {csv_out_path}")
    print(f"Text snippets  : {text_dir}")
    print(f"Videos (ok/fail): {success}/{failures}")
    print(f"Video folder   : {video_dir}")


if __name__ == "__main__":
    main()
