#!/usr/bin/env python3
"""
Independent renamer that aligns Google Doc INSERT/EXTRACT links with a transcript,
applies extract offsets, and renames downloaded assets without depending on the
precise_placer module.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import subprocess
import sys
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


TOKEN_PATTERN = re.compile(r"(?:\d{2,}|[a-z0-9]{4,})")
FRAME_RATE_DEFAULT = 25
ASSETS_DEFAULT_PATH = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/Insert"
)
TRANSCRIPT_DEFAULT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/precise_placer/input"
)
DOC_DEFAULT_DIRS = [
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/precise_placer/input"),
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/html"),
]
OUTPUT_DEFAULT_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/precise_placer/output"
)
COMPARER_OUTPUT_DIRS = [
    Path("/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output"),
    Path(
        "/Users/mathieusandana/Desktop/code/deployable_auto-montage/Comparser/output/first_comparser_output"
    ),
]
SWISSER_HTML_DIR = Path(
    "/Users/mathieusandana/Desktop/code/deployable_auto-montage/swisser/Universal_pipe/html"
)
HTML_EXTENSIONS = (".html", ".htm")

INSERT_SNIPPET_PATTERN = re.compile(
    r"^\s*(?:\d+\s*[-.:])?\s*(INSERT|EXTRAIT|EXTRACT)\b", re.IGNORECASE
)
SUFFIX_TRAIL_PATTERN = re.compile(
    r"(?:[_\-\s]+)?(INSERT|EXTRAIT|EXTRACT)(?:[_\-\s]+[0-9:.,\-]+)?$",
    re.IGNORECASE,
)
TIME_COLON_PATTERN = re.compile(r"\d+(?::\d+){1,2}")
TIME_MIN_PATTERN = re.compile(r"(\d+)\s*(?:m|min|mn)\s*(\d{1,2})", re.IGNORECASE)
ORIGINAL_INDEX_PATTERN = re.compile(r"(?:\d+m\d{2}[_\s]+)?(\d+)_")


@dataclass
class TranscriptSegment:
    start_offset: int
    end_offset: int
    start_time: float
    end_time: float
    text: str
    keep: bool = True


@dataclass
class LinkReference:
    index: int
    url: str
    start: int
    end: int
    snippet: str
    context: str


@dataclass
class InsertDetails:
    label: str
    start: Optional[float]
    end: Optional[float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Align Google Doc INSERT/EXTRACT links with a transcript, "
            "rename assets, and emit Premiere snippets."
        )
    )
    parser.add_argument("--transcript", default=None, help="Transcript CSV path.")
    parser.add_argument("--doc", default=None, help="Saved Google Doc HTML path.")
    parser.add_argument(
        "--assets",
        default=str(ASSETS_DEFAULT_PATH),
        help="Folder containing downloaded inserts.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_DEFAULT_DIR),
        help="Directory for CSV reports.",
    )
    parser.add_argument(
        "--frame-rate",
        type=int,
        default=FRAME_RATE_DEFAULT,
        help="Sequence frame rate.",
    )
    parser.add_argument(
        "--context-before",
        type=int,
        default=80,
        help="Characters of context to keep before each link.",
    )
    parser.add_argument(
        "--context-after",
        type=int,
        default=80,
        help="Characters of context to keep after each link.",
    )
    parser.add_argument(
        "--base-offset",
        type=float,
        default=None,
        help="Seconds to subtract from transcript times (auto when omitted).",
    )
    parser.add_argument(
        "--rename",
        dest="rename",
        action="store_true",
        default=None,
        help="Force asset renaming.",
    )
    parser.add_argument(
        "--no-rename",
        dest="rename",
        action="store_false",
        help="Skip renaming assets.",
    )
    parser.add_argument(
        "--write-list",
        action="store_true",
        help="Update list.txt even if assets are untouched.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Similarity threshold for warnings.",
    )
    return parser.parse_args()


def latest_file_in_directories(
    directories: Sequence[Path], extensions: Sequence[str], flag_name: str
) -> Path:
    allowed = {ext.lower().lstrip(".") for ext in extensions}
    candidates: List[Path] = []
    diagnostics: List[str] = []

    for doc_dir in directories:
        if not doc_dir.exists():
            diagnostics.append(f"{doc_dir}: directory not found")
            continue
        files = [
            path
            for path in doc_dir.iterdir()
            if path.is_file() and path.suffix.lower().lstrip(".") in allowed
        ]
        if not files:
            diagnostics.append(f"{doc_dir}: no {flag_name} files found")
            continue
        candidates.extend(files)

    if not candidates:
        searched = ", ".join(str(path) for path in directories)
        extra = f" Details: {'; '.join(diagnostics)}." if diagnostics else ""
        raise FileNotFoundError(
            f"No {flag_name} file found in the searched directories: {searched}.{extra} "
            f"Provide --{flag_name} explicitly."
        )

    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name.lower()), reverse=True)
    return candidates[0]


def resolve_transcript_path(arg: Optional[str]) -> Path:
    if arg is None:
        directories = [TRANSCRIPT_DEFAULT_DIR, *COMPARER_OUTPUT_DIRS]
        return latest_file_in_directories(directories, (".csv",), "transcript")
    return Path(arg).expanduser().resolve()


def resolve_doc_path(arg: Optional[str]) -> Path:
    if arg is None:
        return latest_file_in_directories([SWISSER_HTML_DIR], HTML_EXTENSIONS, "doc")
    return Path(arg).expanduser().resolve()


def detect_transcript_delimiter(sample: str) -> str:
    if not sample:
        return ","
    header = sample.splitlines()[0]
    return ";" if header.count(";") > header.count(",") else ","


def timecode_to_seconds(timecode: str, frame_rate: int) -> float:
    hh, mm, ss, ff = timecode.split(":")
    return (
        int(hh) * 3600
        + int(mm) * 60
        + int(ss)
        + int(ff) / max(frame_rate, 1)
    )


def build_transcript_model(
    transcript_path: Path, frame_rate: int, base_offset: Optional[float]
) -> Tuple[str, List[TranscriptSegment], float, bool]:
    rows: List[Tuple[float, float, str, Optional[Dict[str, str]]]] = []
    keep_field: Optional[str] = None

    with transcript_path.open(newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
            reader = csv.DictReader(handle, dialect=dialect)
        except csv.Error:
            delimiter = detect_transcript_delimiter(sample)
            reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames:
            for field in reader.fieldnames:
                if field and field.strip().lower() == "keep":
                    keep_field = field
                    break
        for row in reader:
            text = (row.get("Text") or "").strip()
            if not text:
                continue
            start = timecode_to_seconds(row["Start Time"], frame_rate)
            end = timecode_to_seconds(row["End Time"], frame_rate)
            rows.append((start, end, text, row if keep_field else None))

    if not rows:
        raise ValueError(f"No transcript rows found in {transcript_path}")

    if base_offset is None:
        min_start = min(start for start, _end, _text, _raw in rows)
        base_offset = math.floor(min_start / 3600) * 3600 if min_start >= 3600 else 0.0

    transcript_parts: List[str] = []
    segments: List[TranscriptSegment] = []
    offset = 0

    def is_kept(row: Optional[Dict[str, str]]) -> bool:
        """
        Mirror the semantics used by universal_generate_premiere_xml.py:
        rows whose Keep column contains an \"x\" (or equivalent) survive while
        blank/other values are treated as dropped segments that collapse during
        timing calculations.
        """
        if not keep_field or row is None:
            return True
        marker = (row.get(keep_field) or "").strip().lower()
        if not marker:
            return False
        keep_markers = {"x", "✓", "✔", "keep", "1", "yes"}
        drop_markers = {"✗", "✕", "delete", "drop", "cut", "no", "0"}
        if marker in keep_markers:
            return True
        if marker in drop_markers:
            return False
        # Default to dropping unknown markers so we do not reintroduce removed gaps.
        return False

    for start, end, text, raw in rows:
        if transcript_parts:
            transcript_parts.append(" ")
            offset += 1
        seg_start = offset
        transcript_parts.append(text)
        offset += len(text)
        segments.append(
            TranscriptSegment(
                start_offset=seg_start,
                end_offset=offset,
                start_time=start - base_offset,
                end_time=end - base_offset,
                text=text,
                keep=is_kept(raw),
            )
        )

    transcript_text = "".join(transcript_parts)
    return transcript_text, segments, base_offset, bool(keep_field)


def normalize_with_map(text: str) -> Tuple[str, List[int]]:
    norm_chars: List[str] = []
    index_map: List[int] = []
    prev_space = True

    for idx, ch in enumerate(text):
        decomposed = "".join(
            c
            for c in unicodedata.normalize("NFD", ch)
            if unicodedata.category(c) != "Mn"
        )
        if not decomposed:
            continue

        lower = decomposed.lower()
        if lower.isalnum():
            norm_chars.append(lower)
            index_map.append(idx)
            prev_space = False
        elif ch in {"-", "–", "—", "/", "\\"}:
            if not prev_space:
                norm_chars.append(" ")
                index_map.append(idx)
                prev_space = True
        elif lower.isspace():
            if not prev_space:
                norm_chars.append(" ")
                index_map.append(idx)
                prev_space = True

    if norm_chars and norm_chars[-1] == " ":
        norm_chars.pop()
        index_map.pop()

    return "".join(norm_chars), index_map


def generate_candidate_starts(snippet_norm: str, transcript_norm: str) -> List[int]:
    candidates: set[int] = set()

    for match in TOKEN_PATTERN.finditer(snippet_norm):
        token = match.group(0)
        offset = match.start()
        start = transcript_norm.find(token)
        while start != -1:
            candidate = start - offset
            if candidate >= 0:
                candidates.add(candidate)
            start = transcript_norm.find(token, start + 1)

    return sorted(candidates)


def find_best_alignment(
    snippet: str,
    transcript_norm: str,
    transcript_map: Sequence[int],
) -> Optional[Tuple[int, int, float]]:
    normalized_snippet, _ = normalize_with_map(snippet)
    snippet_norm = normalized_snippet.strip()

    if not snippet_norm:
        return None

    snippet_len = len(snippet_norm)
    if snippet_len == 0 or snippet_len > len(transcript_norm):
        return None

    candidate_starts = generate_candidate_starts(snippet_norm, transcript_norm)

    if not candidate_starts:
        matcher = SequenceMatcher(None, transcript_norm, snippet_norm)
        match = matcher.find_longest_match(0, len(transcript_norm), 0, snippet_len)
        if match.size > 0:
            candidate_starts.append(max(0, match.a - match.b))

    if not candidate_starts:
        step = max(1, snippet_len // 6)
        candidate_starts = list(range(0, len(transcript_norm) - snippet_len + 1, step))

    best_start: Optional[int] = None
    best_ratio = -1.0

    for start in candidate_starts:
        start = max(0, min(start, len(transcript_norm) - snippet_len))
        window = transcript_norm[start : start + snippet_len]
        ratio = SequenceMatcher(None, snippet_norm, window).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_start is None:
        return None

    original_index = transcript_map[best_start]
    return best_start, original_index, best_ratio


def index_to_time(char_index: int, segments: Sequence[TranscriptSegment]) -> float:
    if not segments:
        raise ValueError("Transcript segments are empty.")

    starts = [segment.start_offset for segment in segments]
    idx = bisect_right(starts, char_index) - 1
    idx = max(0, min(idx, len(segments) - 1))
    segment = segments[idx]

    span = max(1, segment.end_offset - segment.start_offset)
    progress = (char_index - segment.start_offset) / span
    progress = max(0.0, min(progress, 1.0))
    return segment.start_time + progress * (segment.end_time - segment.start_time)


def compute_removed_intervals(
    segments: Sequence[TranscriptSegment],
) -> Tuple[List[Tuple[float, float]], float, float, float]:
    if not segments:
        return [], 0.0, 0.0, 0.0

    earliest = min(segment.start_time for segment in segments)
    cursor = earliest if earliest < 0 else 0.0
    intervals: List[Tuple[float, float]] = []
    drop_duration = 0.0
    silent_duration = 0.0

    for segment in segments:
        seg_start = segment.start_time
        seg_end = segment.end_time
        if seg_start > cursor:
            intervals.append((cursor, seg_start))
            silent_duration += seg_start - cursor
            cursor = seg_start
        if not segment.keep:
            intervals.append((seg_start, seg_end))
            drop_duration += max(0.0, seg_end - seg_start)
        cursor = max(cursor, seg_end)

    if not intervals:
        return [], drop_duration, silent_duration, 0.0

    intervals.sort()
    merged: List[Tuple[float, float]] = []
    for start, end in intervals:
        if not merged:
            merged.append((start, end))
            continue
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    merged = [(start, end) for start, end in merged if end > start]
    total_removed = sum(end - start for start, end in merged)
    return merged, drop_duration, silent_duration, total_removed


def close_removed_gaps(
    timestamp: float, removed_intervals: Sequence[Tuple[float, float]]
) -> float:
    if not removed_intervals:
        return max(0.0, timestamp)

    removed = 0.0
    for start, end in removed_intervals:
        if timestamp >= end:
            removed += end - start
            continue
        if timestamp <= start:
            break
        removed += timestamp - start
        break
    return max(0.0, timestamp - removed)


def seconds_to_label(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes}m{secs:02d}"


def seconds_to_timecode(seconds: float, frame_rate: int) -> str:
    frame_count = int(round(seconds * frame_rate))
    total_seconds = frame_count // frame_rate
    frames = frame_count % frame_rate
    minutes = total_seconds // 60
    secs = total_seconds % 60
    return f"{minutes:02}:{secs:02}:{frames:02}"


def _contains_numbered_assets(path: Path) -> bool:
    for item in path.iterdir():
        if item.is_file() and re.match(r"(\d+)", item.name):
            return True
    return False


def resolve_assets_folder(base_path: Path) -> Path:
    if not base_path.exists():
        raise FileNotFoundError(f"Assets folder not found: {base_path}")

    if base_path.is_file():
        raise NotADirectoryError(f"Assets path must be a directory: {base_path}")

    if _contains_numbered_assets(base_path):
        return base_path

    subdirs = [
        child for child in base_path.iterdir() if child.is_dir() and not child.name.startswith(".")
    ]

    if len(subdirs) == 1:
        candidate = subdirs[0]
        if _contains_numbered_assets(candidate):
            return candidate
        return resolve_assets_folder(candidate)

    raise ValueError(
        f"Could not find numbered assets inside {base_path}. Specify --assets explicitly."
    )


def _extract_original_index(name: str) -> Optional[int]:
    match = ORIGINAL_INDEX_PATTERN.search(name)
    if match:
        return int(match.group(1))
    return None


def _original_index_sort_key(name: str) -> Tuple[int, str]:
    index = _extract_original_index(name)
    fallback = 10**6
    return (index if index is not None else fallback, name)


def normalize_filename(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"\s+", "_", ascii_name.strip())
    ascii_name = re.sub(r"_+", "_", ascii_name)
    return ascii_name or "asset"


def extract_links_from_exported_html(
    content: str, context_before: int, context_after: int
) -> Tuple[str, List[LinkReference]]:
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", content, flags=re.S | re.I
    )

    def html_to_text(fragment: str) -> str:
        text = re.sub(r"<[^>]+>", " ", fragment)
        text = html.unescape(text)
        return " ".join(text.split())

    plain_text = html_to_text(cleaned)
    if not plain_text:
        raise ValueError("HTML document did not contain extractable text.")

    link_pattern = re.compile(
        r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        re.S | re.I,
    )

    matches = list(link_pattern.finditer(cleaned))
    if not matches:
        raise ValueError("No hyperlink tags were detected in the HTML document.")

    references: List[LinkReference] = []
    last_search_pos = 0
    plain_text_lower = plain_text.lower()

    for match in matches:
        url = html.unescape(match.group(1))
        snippet_html = match.group(2)
        snippet = html_to_text(snippet_html)
        if not snippet:
            continue

        snippet_lower = snippet.lower()
        start = plain_text_lower.find(snippet_lower, last_search_pos)
        if start == -1:
            start = plain_text_lower.find(snippet_lower)
        if start == -1:
            start = last_search_pos
        end = start + len(snippet)

        ctx_start = max(0, start - context_before)
        ctx_end = min(len(plain_text), end + context_after)
        context = plain_text[ctx_start:ctx_end]

        references.append(
            LinkReference(
                index=len(references) + 1,
                url=url,
                start=start,
                end=end,
                snippet=snippet,
                context=" ".join(context.split()),
            )
        )
        last_search_pos = end

    if not references:
        raise ValueError("No link references were detected in the HTML document.")

    return plain_text, references


def extract_doc_links(
    doc_path: Path, context_before: int, context_after: int
) -> Tuple[str, List[LinkReference]]:
    content = doc_path.read_text()

    doc_text: Optional[str] = None
    references: List[LinkReference] = []
    match = re.search(r"DOCS_modelChunk\s*=\s*(\{.+?\});", content, re.S)
    if not match:
        doc_text, references = extract_links_from_exported_html(
            content, context_before, context_after
        )
        return doc_text, references

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        doc_text, references = extract_links_from_exported_html(
            content, context_before, context_after
        )
        return doc_text, references

    doc_text = "".join(
        entry["s"] for entry in payload["chunk"] if entry.get("ty") == "is"
    )

    for entry in payload["chunk"]:
        if entry.get("ty") != "as" or entry.get("st") != "link":
            continue
        link_meta = entry.get("sm", {}).get("lnks_link", {})
        url = link_meta.get("ulnk_url") or link_meta.get("lnk_url")
        if not url:
            continue
        si = entry["si"]
        ei = entry["ei"]
        snippet = doc_text[si:ei]
        ctx_start = max(0, si - context_before)
        ctx_end = min(len(doc_text), ei + context_after)
        context = doc_text[ctx_start:ctx_end]
        references.append(
            LinkReference(
                index=len(references) + 1,
                url=url,
                start=si,
                end=ei,
                snippet=snippet.strip(),
                context=" ".join(context.split()),
            )
        )

    if references:
        return doc_text, references

    doc_text, references = extract_links_from_exported_html(
        content, context_before, context_after
    )
    return doc_text, references


def write_list_file(assets_path: Path) -> Path:
    candidates = []
    for item in assets_path.iterdir():
        if item.is_file() and (
            re.match(r"\d+m\d{2}[_\s]", item.name) or re.match(r"\d+_", item.name)
        ):
            candidates.append(item.name)
    candidates.sort(key=_original_index_sort_key)

    list_path = assets_path / "list.txt"
    with list_path.open("w") as handle:
        for name in candidates:
            handle.write(f"{name}\n")
    return list_path


def write_summary_csv(
    output_dir: Path,
    results: Sequence[Dict[str, object]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "precise_links.csv"
    fieldnames = [
        "link_index",
        "timestamp_seconds",
        "timestamp_label",
        "timecode",
        "source_timestamp_seconds",
        "source_timecode",
        "match_ratio",
        "url",
        "snippet",
        "context",
    ]
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return csv_path


def prepare_sound_effects(source_folder: Path) -> Optional[Path]:
    sound_script = Path(__file__).resolve().with_name("sound_placer.py")
    if not sound_script.exists():
        print("sound_placer.py not found; skipping sound effect preparation.", file=sys.stderr)
        return None

    repo_root = sound_script.parent.parent
    sound_output = repo_root / "effects" / "timed_effect"

    command = [
        sys.executable,
        str(sound_script),
        "--list",
        str(source_folder),
        "--output",
        str(sound_output),
    ]

    print("\n🎧 Launching sound_placer.py to prep sound effects...")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print("\n❌ sound_placer.py failed; sound effects will be skipped.", file=sys.stderr)
        if exc.stdout:
            print(exc.stdout.strip(), file=sys.stderr)
        if exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return None

    stdout = result.stdout or ""
    marker = "ExtendScript snippet for Premiere:"
    if marker in stdout:
        stdout = stdout.split(marker, 1)[0]
    stdout = stdout.strip()
    if stdout:
        print(stdout)

    stderr = (result.stderr or "").strip()
    if stderr:
        print(stderr, file=sys.stderr)

    return sound_output


def build_premiere_snippet(folder: Path, sound_folder: Optional[Path] = None) -> str:
    folder_path = folder.as_posix()
    sound_folder_path = sound_folder.as_posix() if sound_folder else None
    lines = [
        "(function () {",
        "    var sequence = app.project.activeSequence;",
        "    if (!sequence) {",
        "        alert(\"❌ No active sequence.\");",
        "        return;",
        "    }",
        "",
        "    function stop(msg) {",
        "        alert(msg);",
        "        throw new Error(msg);",
        "    }",
        "",
        "    function loadList(filePath, label) {",
        "        var file = new File(filePath);",
        "        if (!file.exists) {",
        "            stop('❌ ' + label + ' list.txt not found in: ' + filePath);",
        "        }",
        "        if (!file.open('r')) {",
        "            stop('❌ Failed to open ' + label + ' list.txt');",
        "        }",
        "        var names = [];",
        "        while (!file.eof) {",
        "            var line = file.readln().trim();",
        "            if (line) {",
        "                names.push(line);",
        "            }",
        "        }",
        "        file.close();",
        "        if (!names.length) {",
        "            stop('❌ ' + label + ' list.txt is empty.');",
        "        }",
        "        return names;",
        "    }",
        "",
        "    function findItemByName(name) {",
        "        function searchBin(bin) {",
        "            for (var j = 0; j < bin.children.numItems; j++) {",
        "                var item = bin.children[j];",
        "                if (item.type === ProjectItemType.BIN) {",
        "                    var found = searchBin(item);",
        "                    if (found) {",
        "                        return found;",
        "                    }",
        "                } else if (item.name === name) {",
        "                    return item;",
        "                }",
        "            }",
        "            return null;",
        "        }",
        "        return searchBin(app.project.rootItem);",
        "    }",
        "",
        "    function ensureProjectItem(name, fullPath) {",
        "        var existing = findItemByName(name);",
        "        if (existing) {",
        "            return existing;",
        "        }",
        "        var file = new File(fullPath);",
        "        if (!file.exists) {",
        "            $.writeln('❌ File not found: ' + name);",
        "            return null;",
        "        }",
        "        var imported = app.project.importFiles([file.fsName], false, app.project.rootItem, false);",
        "        if (!imported) {",
        "            $.writeln('❌ Failed to import: ' + name);",
        "            return null;",
        "        }",
        "        $.sleep(200);",
        "        return findItemByName(name);",
        "    }",
        "",
        "    function extractSeconds(name) {",
        "        var match = name.match(/(\\d+)m(\\d{2})/);",
        "        if (!match) {",
        "            return null;",
        "        }",
        "        return parseInt(match[1], 10) * 60 + parseInt(match[2], 10);",
        "    }",
        "",
        f"    var folderPath = \"{folder_path}\";  // Visual inserts folder",
        "    var fileNames = loadList(folderPath + \"/list.txt\", 'insert');",
        "    var placedCount = 0;",
        "",
        "    for (var i = 0; i < fileNames.length; i++) {",
        "        var filename = fileNames[i];",
        "        var fullPath = folderPath + \"/\" + filename;",
        "        var item = ensureProjectItem(filename, fullPath);",
        "        if (!item) {",
        "            $.writeln(\"❌ Could not prepare project item for: \" + filename);",
        "            continue;",
        "        }",
        "        var totalSeconds = extractSeconds(filename);",
        "        if (totalSeconds === null) {",
        "            $.writeln(\"❌ Filename missing timestamp label: \" + filename);",
        "            continue;",
        "        }",
        "        var time = new Time();",
        "        time.seconds = totalSeconds;",
        "        var trackIndex = i + 3;",
        "        while (sequence.videoTracks.numTracks <= trackIndex) {",
        "            sequence.videoTracks.addTrack();",
        "        }",
        "        try {",
        "            sequence.videoTracks[trackIndex].insertClip(item, time);",
        "            $.writeln(\"✅ Inserted \" + filename + \" on track \" + (trackIndex + 1) + \" — remember SFX live on track 2.\");",
        "            placedCount++;",
        "        } catch (e) {",
        "            $.writeln(\"❌ Failed to place clip: \" + filename + \" — \" + e);",
        "        }",
        "    }",
        "    var summary = \"✅ Done. Placed \" + placedCount + \" clips on tracks 4 and above.\";",
    ]
    if sound_folder_path:
        lines.extend(
            [
                "",
                f"    var soundFolderPath = \"{sound_folder_path}\";  // Timed sound effects folder",
                "    var soundNames = loadList(soundFolderPath + \"/list.txt\", 'sound effect');",
                "    var soundTrackIndex = 1; // Audio track 2 (zero-based)",
                "    while (sequence.audioTracks.numTracks <= soundTrackIndex) {",
                "        sequence.audioTracks.addTrack();",
                "    }",
                "    var soundTrack = sequence.audioTracks[soundTrackIndex];",
                "    var soundPlaced = 0;",
                "    for (var s = 0; s < soundNames.length; s++) {",
                "        var soundName = soundNames[s];",
                "        var soundPath = soundFolderPath + \"/\" + soundName;",
                "        var soundItem = ensureProjectItem(soundName, soundPath);",
                "        if (!soundItem) {",
                "            $.writeln(\"❌ Could not prepare sound effect: \" + soundName);",
                "            continue;",
                "        }",
                "        var soundSeconds = extractSeconds(soundName);",
                "        if (soundSeconds === null) {",
                "            $.writeln(\"❌ Cannot parse timestamp for sound effect: \" + soundName);",
                "            continue;",
                "        }",
                "        var soundTime = new Time();",
                "        soundTime.seconds = soundSeconds;",
                "        try {",
                "            soundTrack.overwriteClip(soundItem, soundTime);",
                "            $.writeln(\"🎧 Placed \" + soundName + \" at \" + soundSeconds + \"s on audio track 2\");",
                "            soundPlaced++;",
                "        } catch (err) {",
                "            $.writeln(\"❌ Failed to place sound effect: \" + soundName + \" — \" + err);",
                "        }",
                "    }",
                "    summary += \"\\n🎧 Added \" + soundPlaced + \" sound effect clip(s) on track 2.\";",
            ]
        )
    lines.extend(
        [
            "",
            "    alert(summary);",
            "})();",
        ]
    )
    return "\n".join(lines)


def build_insert_detail_map(
    references: Sequence[LinkReference],
) -> Dict[int, InsertDetails]:
    details_map: Dict[int, InsertDetails] = {}
    for ref in references:
        details = parse_insert_details(ref.snippet)
        if details:
            details_map[ref.index] = details
    return details_map


def locate_assets_folder(assets_root: Path) -> Optional[Path]:
    try:
        return resolve_assets_folder(assets_root)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None


def build_asset_index_map(folder: Optional[Path]) -> Dict[int, Path]:
    mapping: Dict[int, Path] = {}
    if folder is None:
        return mapping
    for item in folder.iterdir():
        if not item.is_file():
            continue
        index = _extract_original_index(item.name)
        if index is None or index in mapping:
            continue
        mapping[index] = item
    return mapping


def probe_media_duration(path: Path) -> Optional[float]:
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
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    output = result.stdout.strip()
    try:
        duration = float(output)
    except ValueError:
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def compute_extract_duration_map(
    details_map: Dict[int, InsertDetails],
    asset_index_map: Dict[int, Path],
) -> Tuple[Dict[int, float], List[str]]:
    durations: Dict[int, float] = {}
    warnings: List[str] = []

    for index, details in details_map.items():
        if details.label != "EXTRACT" or details.start is None:
            if details.label == "EXTRACT" and details.start is None:
                warnings.append(f"#{index}: extract snippet is missing a start time.")
            continue
        if details.end is not None:
            duration = abs(details.end - details.start)
            if duration > 0:
                durations[index] = duration
            else:
                warnings.append(f"#{index}: extract duration evaluated to 0s.")
            continue
        asset_path = asset_index_map.get(index)
        if asset_path is None:
            warnings.append(f"#{index}: asset file not found for extract duration.")
            continue
        media_duration = probe_media_duration(asset_path)
        if media_duration is None:
            warnings.append(
                f"#{index}: unable to read duration from asset '{asset_path.name}'."
            )
            continue
        remaining = media_duration - details.start
        if remaining <= 0:
            warnings.append(
                f"#{index}: extract start time exceeds asset duration ({media_duration:.2f}s)."
            )
            continue
        durations[index] = remaining

    return durations, warnings


def apply_extract_offsets(
    results: List[Dict[str, object]],
    extract_durations: Dict[int, float],
    frame_rate: int,
) -> Dict[int, float]:
    if not extract_durations or not results:
        return {}

    sortable: List[Tuple[float, Dict[str, object]]] = []
    for row in results:
        raw_value = row.get("timestamp_seconds")
        link_index = row.get("link_index")
        if raw_value in ("", None) or link_index is None:
            continue
        try:
            seconds = float(raw_value)
        except (TypeError, ValueError):
            continue
        sortable.append((seconds, row))

    sortable.sort(key=lambda item: (item[0], item[1].get("link_index", 0)))
    accumulated = 0.0
    offsets: Dict[int, float] = {}

    for base_seconds, row in sortable:
        adjusted = base_seconds + accumulated
        row["timestamp_seconds"] = f"{adjusted:.3f}"
        row["timestamp_label"] = seconds_to_label(adjusted)
        row["timecode"] = seconds_to_timecode(adjusted, frame_rate)
        link_index = row.get("link_index")
        if isinstance(link_index, int):
            offsets[link_index] = adjusted - base_seconds
            if link_index in extract_durations:
                accumulated += extract_durations[link_index]
        elif link_index in extract_durations:
            accumulated += extract_durations[link_index]
    return offsets


def canonical_label(value: str) -> str:
    upper = value.strip().upper()
    if upper.startswith("EXTRAI"):
        return "EXTRACT"
    return "INSERT"


def parse_insert_details(snippet: str) -> Optional[InsertDetails]:
    if not snippet:
        return None
    match = INSERT_SNIPPET_PATTERN.match(snippet.strip())
    if not match:
        return None
    label = canonical_label(match.group(1))
    values = parse_timecode_values(snippet)
    if not values:
        return InsertDetails(label=label, start=None, end=None)
    start = values[0]
    end = values[1] if len(values) >= 2 else None
    return InsertDetails(label=label, start=start, end=end)


def parse_timecode_values(text: str) -> List[float]:
    if not text:
        return []
    tokens = TIME_COLON_PATTERN.findall(text)
    seen = set(tokens)
    for match in TIME_MIN_PATTERN.finditer(text):
        token = f"{match.group(1)}:{match.group(2)}"
        if token not in seen:
            tokens.append(token)
            seen.add(token)
    values: List[float] = []
    for token in tokens:
        seconds = time_token_to_seconds(token)
        if seconds is not None:
            values.append(seconds)
    return values


def time_token_to_seconds(token: str) -> Optional[float]:
    parts = token.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds
    except ValueError:
        return None
    return None


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_time_range(start: Optional[float], end: Optional[float]) -> Optional[str]:
    if start is None:
        return None
    start_str = format_timestamp(start)
    if end is None:
        return start_str
    high = max(start, end)
    low = min(start, end)
    end_str = format_timestamp(high)
    start_str = format_timestamp(low)
    if end_str == start_str:
        return start_str
    return f"{start_str}-{end_str}"


def build_insert_suffix_map(
    references: Sequence[LinkReference],
) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for ref in references:
        details = parse_insert_details(ref.snippet)
        if not details:
            continue
        timing = format_time_range(details.start, details.end)
        suffix = f"{details.label} {timing}" if timing else details.label
        mapping[ref.index] = suffix
    return mapping


def strip_existing_suffix(stem: str, has_suffix: bool) -> str:
    if not stem or not has_suffix:
        return stem
    cleaned = SUFFIX_TRAIL_PATTERN.sub("", stem).rstrip("_- ")
    return cleaned if cleaned else stem


def build_target_name(
    original_name: str,
    timestamp_label: str,
    index: int,
    suffix_text: Optional[str],
) -> str:
    prefix = f"{timestamp_label}_{index}"
    stem, ext = os.path.splitext(original_name)
    remainder = re.sub(r"^(?:\d+m\d{2}[_\s]+)?\d+_", "", stem, count=1)
    remainder = remainder or stem
    remainder = strip_existing_suffix(remainder, bool(suffix_text))
    if not remainder:
        remainder = "asset"
    parts = [prefix, remainder]
    base = "_".join(part for part in parts if part)
    if suffix_text:
        base = f"{base}_{suffix_text}"
    return normalize_filename(f"{base}{ext}")


def rename_assets_with_suffix(
    assets_path: Path,
    rename_map: Dict[int, str],
    suffix_map: Dict[int, str],
) -> Tuple[List[str], List[str]]:
    updated: List[str] = []
    failures: List[str] = []

    for item in assets_path.iterdir():
        if not item.is_file():
            continue
        index = _extract_original_index(item.name)
        if index is None or index not in rename_map:
            continue
        timestamp_label = rename_map[index]
        suffix_text = suffix_map.get(index)
        target_name = build_target_name(item.name, timestamp_label, index, suffix_text)
        if item.name == target_name:
            updated.append(item.name)
            continue
        new_path = item.with_name(target_name)
        if new_path.exists():
            raise FileExistsError(f"Cannot rename {item.name}: {target_name} already exists.")
        try:
            item.rename(new_path)
            updated.append(target_name)
        except PermissionError as exc:
            failures.append(f"{item.name}: {exc}")

    updated.sort(key=_original_index_sort_key)
    return updated, failures


def ensure_asset_folder(
    assets_root: Path, rename_decision: bool, existing: Optional[Path] = None
) -> Tuple[Optional[Path], bool]:
    target_folder: Optional[Path] = existing
    if rename_decision and target_folder is None:
        try:
            target_folder = resolve_assets_folder(assets_root)
        except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
            print(f"\nUnable to rename files: {exc}", file=sys.stderr)
            rename_decision = False
    return target_folder, rename_decision


def shift_results_to_zero(
    results: List[Dict[str, object]], frame_rate: int
) -> Optional[float]:
    """Normalize so the earliest computed timestamp becomes time zero."""
    times: List[float] = []
    for row in results:
        value = row.get("timestamp_seconds")
        if not value:
            continue
        try:
            times.append(float(value))
        except (TypeError, ValueError):
            continue
    if not times:
        return None
    minimum = min(times)
    if minimum <= 1e-3:
        return None
    for row in results:
        value = row.get("timestamp_seconds")
        if not value:
            continue
        try:
            seconds = max(0.0, float(value) - minimum)
        except (TypeError, ValueError):
            continue
        row["timestamp_seconds"] = f"{seconds:.3f}"
        row["timestamp_label"] = seconds_to_label(seconds)
        row["timecode"] = seconds_to_timecode(seconds, frame_rate)
    return minimum


def main() -> None:
    args = parse_args()

    try:
        transcript_path = resolve_transcript_path(args.transcript)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Using transcript input for precise links: {transcript_path}")

    try:
        doc_path = resolve_doc_path(args.doc)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Using document input for precise links: {doc_path}")

    output_dir = Path(args.output_dir).expanduser()
    assets_root = Path(args.assets).expanduser()

    (
        transcript_text,
        segments,
        base_offset,
        has_keep_column,
    ) = build_transcript_model(transcript_path, args.frame_rate, args.base_offset)
    transcript_norm, transcript_map = normalize_with_map(transcript_text)
    (
        removed_intervals,
        drop_duration,
        silent_duration,
        total_removed,
    ) = compute_removed_intervals(segments)
    if total_removed > 0:
        details = []
        if drop_duration > 0:
            details.append(f"{drop_duration:.2f}s dropped via Keep column")
        if silent_duration > 0:
            details.append(f"{silent_duration:.2f}s of silent gaps")
        detail_str = f" ({', '.join(details)})" if details else ""
        print(f"Closing timeline gaps; removing {total_removed:.2f}s{detail_str}.")
    else:
        if has_keep_column:
            print(
                "Keep column detected but no deletions or timing gaps were found; "
                "timestamps will preserve original spacing."
            )
        else:
            print(
                "No deletions or timing gaps detected; timestamps will preserve original spacing."
            )

    _, references = extract_doc_links(
        doc_path, args.context_before, args.context_after
    )

    details_map = build_insert_detail_map(references)
    asset_probe_folder = locate_assets_folder(assets_root)
    asset_index_map = build_asset_index_map(asset_probe_folder)

    results: List[Dict[str, object]] = []

    for ref in references:
        alignment = find_best_alignment(
            ref.context, transcript_norm, transcript_map
        )
        if alignment is None:
            results.append(
                {
                    "link_index": ref.index,
                    "timestamp_seconds": "",
                    "timestamp_label": "",
                    "timecode": "",
                    "source_timestamp_seconds": "",
                    "source_timecode": "",
                    "match_ratio": 0.0,
                    "url": ref.url,
                    "snippet": ref.snippet,
                    "context": ref.context,
                }
            )
            continue

        _norm_pos, char_index, ratio = alignment
        source_timestamp_seconds = index_to_time(char_index, segments)
        timestamp_seconds = close_removed_gaps(
            source_timestamp_seconds, removed_intervals
        )
        timestamp_label = seconds_to_label(timestamp_seconds)
        timecode = seconds_to_timecode(timestamp_seconds, args.frame_rate)
        source_timecode = seconds_to_timecode(
            source_timestamp_seconds, args.frame_rate
        )

        results.append(
            {
                "link_index": ref.index,
                "timestamp_seconds": f"{timestamp_seconds:.3f}",
                "timestamp_label": timestamp_label,
                "timecode": timecode,
                "source_timestamp_seconds": f"{source_timestamp_seconds:.3f}",
                "source_timecode": source_timecode,
                "match_ratio": f"{ratio:.3f}",
                "url": ref.url,
                "snippet": ref.snippet,
                "context": ref.context,
            }
        )

    extract_durations, duration_warnings = compute_extract_duration_map(
        details_map, asset_index_map
    )
    extract_offsets: Dict[int, float] = {}
    if extract_durations:
        total_added = sum(extract_durations.values())
        print(
            f"Accounting for {len(extract_durations)} extract(s); "
            f"timeline extended by {total_added:.2f}s."
        )
        extract_offsets = apply_extract_offsets(results, extract_durations, args.frame_rate)
        decorated: List[Tuple[float, Dict[str, object], float]] = []
        if extract_offsets:
            for row in results:
                link_index = row.get("link_index")
                if not isinstance(link_index, int):
                    continue
                offset = extract_offsets.get(link_index, 0.0)
                if offset <= 0:
                    continue
                try:
                    seconds = float(row.get("timestamp_seconds") or 0.0)
                except (TypeError, ValueError):
                    seconds = 0.0
                decorated.append((seconds, row, offset))
                row["extract_offset_seconds"] = f"{offset:.3f}"
        if decorated:
            decorated.sort(key=lambda item: item[0])
            print("Applied extract offsets:")
            for _, row, offset in decorated:
                print(
                    f"  - #{row['link_index']}: +{offset:.2f}s -> {row['timestamp_label']}"
                )
    if duration_warnings:
        print("\n⚠️  Extract duration diagnostics:")
        for message in duration_warnings:
            print(f"  - {message}")

    shift_value = shift_results_to_zero(results, args.frame_rate)
    if shift_value is not None:
        print(
            f"Normalized earliest timestamp by subtracting {shift_value:.2f}s "
            "so INSERT 1 starts at 0m00."
        )

    rename_map: Dict[int, str] = {
        row["link_index"]: row["timestamp_label"]
        for row in results
        if row.get("timestamp_label")
    }

    csv_path = write_summary_csv(output_dir, results)

    print(f"Wrote detailed alignment report to: {csv_path}")
    print(f"Detected base offset: {base_offset:.2f}s")

    uncertain = [
        row
        for row in results
        if row.get("match_ratio") and float(row["match_ratio"]) < args.threshold
    ]
    if uncertain:
        print("\n⚠️  The following links have low confidence matches:")
        for row in uncertain:
            print(
                f"  - #{row['link_index']}: ratio {row['match_ratio']}, context='{row['context'][:80]}...'"
            )

    rename_decision = args.rename if args.rename is not None else bool(rename_map)
    list_requested = args.write_list
    target_folder, rename_decision = ensure_asset_folder(
        assets_root, rename_decision, asset_probe_folder
    )

    if rename_decision and not rename_map:
        print("\nNo timestamp matches were produced; skipping renaming.")
        rename_decision = False

    if rename_decision and target_folder is not None:
        suffix_map = build_insert_suffix_map(references)
        renamed_files, failures = rename_assets_with_suffix(
            target_folder, rename_map, suffix_map
        )
        print(f"\nRenamed {len(renamed_files)} files in {target_folder}")
        for name in renamed_files:
            print(f"  {name}")
        if failures:
            print("\nSome files could not be renamed:")
            for message in failures:
                print(f"  - {message}")
        list_path = write_list_file(target_folder)
        print(f"\nUpdated list.txt at: {list_path}")
        sound_effects_folder = prepare_sound_effects(target_folder)
        if sound_effects_folder is None:
            print(
                "⚠️  Sound effects could not be prepared; snippet will only handle video inserts."
            )
        print("\nCopy and paste this snippet into Premiere's ExtendScript console:\n")
        print("---")
        print("---")
        print("---")
        print(build_premiere_snippet(target_folder, sound_effects_folder))
    elif list_requested:
        if target_folder is None:
            try:
                target_folder = resolve_assets_folder(assets_root)
            except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
                print(f"\nUnable to write list.txt: {exc}", file=sys.stderr)
                target_folder = None
        if target_folder is not None:
            list_path = write_list_file(target_folder)
            print(f"\nUpdated list.txt at: {list_path}")


if __name__ == "__main__":
    main()
