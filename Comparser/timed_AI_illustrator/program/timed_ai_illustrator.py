from __future__ import annotations

from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping, Sequence

COMPARSER_ROOT = Path(__file__).resolve().parents[2]
if str(COMPARSER_ROOT) not in sys.path:
    sys.path.append(str(COMPARSER_ROOT))

from Approximate_string_matching.program.lib.csv_utils import write_dict_rows
from Approximate_string_matching.program.lib.legacy_step2 import get_legacy_step2_module
from Approximate_string_matching.program.lib.reference_features import (
    ClassifiedLink,
    extract_classified_links,
    extract_structured_titles,
    locate_structured_titles,
)
from Approximate_string_matching.program.lib.text_utils import normalize_text, ordered_token_overlap, tokenize
from Approximate_string_matching.program.lib.timecode_utils import parse_timecode

OUTPUT_HEADER = [
    "Start Time",
    "End Time",
    "Transcript Word",
    "Reference Word",
    "Illustration Type",
    "Crossing With Previous",
]

TIMING_MANIFEST_HEADER = [
    "Entry ID",
    "Source Pass",
    "Illustration Type",
    "Asset Category",
    "Start Time",
    "End Time",
    "Transcript Word",
    "Reference Word",
    "Transcript #",
    "Row ID",
    "Timing Basis",
    "Locator",
    "Normalized Match Key",
    "Link URL",
    "Link Kind",
    "HTML Insert Index",
]

DEFAULT_OUTPUT_ROOT = COMPARSER_ROOT / "timed_AI_illustrator" / "output"
DEFAULT_ASM_OUTPUT_ROOT = COMPARSER_ROOT / "Approximate_string_matching" / "output"

PIPE_HEADER = [
    "Keep",
    "Transcript #",
    "Start Time",
    "End Time",
    "Text",
    "Reference Segment",
    "Match %",
    "Status",
]

PUNCTUATION_TYPES = {
    "?": "question_mark",
    "!": "exclamation_mark",
    "...": "ellipsis",
}

SOCIAL_OUTPUT_TYPES = {
    "facebook": "facebook",
    "snapchat": "snapchat",
    "instagram": "instagram",
    "twitter": "twitter",
    "youtube": "youtube",
    "tiktok": "tiktok",
    "linkedin": "linkedin",
    "telegram": "telegram",
}

DIRECT_INSERT_ASSET_CATEGORIES = {
    "titles": {"title_h1", "title_h2", "title_h3plus"},
    "quote_highlights": {"quote"},
    "article_links": {"article_link"},
    "image_links": {"image_link"},
    "video_links": {"video_link_excerpt", "video_link_direct"},
    "tweet_links": {"tweet_link"},
    "website_links": {"website_link"},
    "cta": {"cta_comment", "cta_subscribe", "cta_tippee"},
    "money": {"money"},
    "calendar": {"date"},
    "percent": {"percent"},
    "nouns": {"person"},
    "institution_images": {"gov_institution"},
    "locations_3d": {"city", "country"},
    "animated_emoji": {"animated_emoji"},
    "animated_flag": {"animated_flag"},
    "social_ranking_punctuation": {
        "bold",
        "italic",
        "list_bullet_group",
        "list_dash_group",
        "list_number_group",
        "list_check_group",
        "question_mark",
        "exclamation_mark",
        "ellipsis",
        "facebook",
        "snapchat",
        "instagram",
        "twitter",
        "youtube",
        "tiktok",
        "linkedin",
        "telegram",
        "ranking",
        "decibel",
        "speed",
        "weight_object",
        "weight_person",
        "distance",
        "temperature",
        "surface",
        "volume",
        "duration",
    },
}

METRIC_PATTERNS = {
    "percent": lambda legacy: legacy.PERCENT_PATTERN,
    "decibel": lambda legacy: legacy.DECIBEL_PATTERN,
    "speed": lambda legacy: legacy.SPEED_PATTERN,
    "weight_object": lambda legacy: legacy.WEIGHT_OBJECT_PATTERN,
    "weight_person": lambda legacy: legacy.WEIGHT_PERSON_PATTERN,
    "temperature": lambda legacy: legacy.TEMPERATURE_PATTERN,
    "surface": lambda legacy: legacy.SURFACE_PATTERN,
    "volume": lambda legacy: legacy.VOLUME_PATTERN,
    "ranking": lambda legacy: legacy.RANKING_PATTERN,
}

DURATION_PATTERN = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:h|heures?|hrs?|hours?|mn|min(?:ute)?s?|sec(?:onde)?s?)\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b",
    re.IGNORECASE,
)
TOKEN_SPAN_RE = re.compile(r"\w+", re.UNICODE)
NEARBY_METRIC_DEDUP_SECONDS = 3.0
NEARBY_METRIC_TYPES = frozenset(
    {
        "decibel",
        "speed",
        "weight_object",
        "weight_person",
        "distance",
        "temperature",
        "surface",
        "volume",
        "duration",
        "ranking",
    }
)


@dataclass
class ResolvedInputs:
    run_dir: Path
    comparer_path: Path
    edit_timeline_path: Path
    html_path: Path | None
    summary_path: Path | None
    keep_mode: str


@dataclass
class RowContext:
    row_index: int
    transcript_number: str
    row: dict[str, str]
    text: str
    reference_segment: str
    timeline_entries: list[dict[str, str]]
    row_start_seconds: float
    row_end_seconds: float
    row_timing_basis: str


@dataclass
class LocalizedSpan:
    start_seconds: float
    end_seconds: float
    transcript_phrase: str
    reference_phrase: str
    timing_basis: str
    locator: str


@dataclass
class IllustrationRecord:
    start_seconds: float
    end_seconds: float
    transcript_word: str
    reference_word: str
    illustration_type: str
    timing_basis: str
    source_pass: str
    asset_category: str
    transcript_number: str
    row_id: str
    locator: str
    normalized_match_key: str
    link_url: str = ""
    link_kind: str = ""
    html_insert_index: int | None = None

    def as_row(self) -> dict[str, str]:
        return {
            "Start Time": _format_human_timestamp(self.start_seconds),
            "End Time": _format_human_timestamp(self.end_seconds),
            "Transcript Word": self.transcript_word,
            "Reference Word": self.reference_word,
            "Illustration Type": self.illustration_type,
            "Crossing With Previous": "",
        }

    def as_manifest_row(self, entry_id: int) -> dict[str, str]:
        return {
            "Entry ID": str(entry_id),
            "Source Pass": self.source_pass,
            "Illustration Type": self.illustration_type,
            "Asset Category": self.asset_category,
            "Start Time": _format_human_timestamp(self.start_seconds),
            "End Time": _format_human_timestamp(self.end_seconds),
            "Transcript Word": self.transcript_word,
            "Reference Word": self.reference_word,
            "Transcript #": self.transcript_number,
            "Row ID": self.row_id,
            "Timing Basis": self.timing_basis,
            "Locator": self.locator,
            "Normalized Match Key": self.normalized_match_key,
            "Link URL": self.link_url,
            "Link Kind": self.link_kind,
            "HTML Insert Index": str(self.html_insert_index or ""),
        }


def _choose_csv_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="ignore")[:8192]
    first_nonempty_line = next((line for line in sample.splitlines() if line.strip()), "")
    semicolon_count = first_nonempty_line.count(";")
    comma_count = first_nonempty_line.count(",")
    if semicolon_count > 0 and semicolon_count >= comma_count:
        return ";"
    if comma_count > 0 and comma_count > semicolon_count:
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        return ";"
    return str(getattr(dialect, "delimiter", ";") or ";")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    delimiter = _choose_csv_delimiter(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized_row = {
                (key or "").lstrip("\ufeff").strip(): (value or "")
                for key, value in row.items()
            }
            rows.append(normalized_row)
    return rows


def _format_human_timestamp(seconds: float) -> str:
    total_millis = max(0, int(round(seconds * 1000)))
    hours = total_millis // 3_600_000
    minutes = (total_millis % 3_600_000) // 60_000
    secs = (total_millis % 60_000) // 1000
    millis = total_millis % 1000
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def _safe_int(value: str | None) -> int | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _normalize_phrase(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _normalize_match_key(value: str) -> str:
    normalized = normalize_text(value or "").casefold()
    normalized = re.sub(
        r"(\d+(?:[.,]\d+)?)\s*(?:km|kilom(?:e|è)tres?)\s*(?:/|\s+)?\s*(?:h|heures?|par\s+heure)\b",
        r"\1 km h",
        normalized,
    )
    normalized = re.sub(r"(\d+(?:[.,]\d+)?)\s*kilom(?:e|è)tres?\b", r"\1 km", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _asset_category_for(illustration_type: str) -> str:
    for asset_category, illustration_types in DIRECT_INSERT_ASSET_CATEGORIES.items():
        if illustration_type in illustration_types:
            return asset_category
    return ""


def _split_pipe_values(value: str) -> list[str]:
    return [fragment.strip() for fragment in (value or "").split("|") if fragment.strip()]


def _path_from_summary(summary_path: Path | None, key: str) -> Path | None:
    if summary_path is None or not summary_path.exists():
        return None
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    inputs = data.get("inputs")
    if isinstance(inputs, Mapping):
        raw = str(inputs.get(key) or "").strip()
        if raw:
            return Path(raw)
    return None


def _discover_latest_run(output_root: Path = DEFAULT_ASM_OUTPUT_ROOT) -> Path:
    if not output_root.exists():
        raise FileNotFoundError(f"Missing Approximate_string_matching output directory: {output_root}")
    candidates = [
        path
        for path in output_root.iterdir()
        if path.is_dir()
        and (path / "13_precise_comparer.csv").exists()
        and (path / "12_edit_timeline.csv").exists()
    ]
    if not candidates:
        raise FileNotFoundError(f"No valid Approximate_string_matching run found in {output_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _detect_keep_mode(comparer_path: Path, keep_mode: str) -> str:
    if keep_mode != "auto":
        return keep_mode
    lower_name = comparer_path.name.lower()
    if lower_name == "13_precise_comparer.csv" or lower_name.endswith("07_step1_compat.csv"):
        return "xml_ready"
    if lower_name.endswith("step2_input.csv") or "step2_input" in lower_name:
        return "legacy_step2"
    raise RuntimeError(f"Unable to infer keep polarity safely from comparer path: {comparer_path}")


def resolve_inputs(
    run_dir: Path | None = None,
    comparer_path: Path | None = None,
    edit_timeline_path: Path | None = None,
    html_path: Path | None = None,
    keep_mode: str = "auto",
) -> ResolvedInputs:
    if run_dir is None:
        if comparer_path is not None:
            run_dir = comparer_path.parent
        elif edit_timeline_path is not None:
            run_dir = edit_timeline_path.parent
        else:
            run_dir = _discover_latest_run()
    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.json"
    resolved_comparer = (comparer_path or (run_dir / "13_precise_comparer.csv")).resolve()
    resolved_timeline = (edit_timeline_path or (run_dir / "12_edit_timeline.csv")).resolve()
    if not resolved_comparer.exists():
        raise FileNotFoundError(f"Missing comparer CSV: {resolved_comparer}")
    if not resolved_timeline.exists():
        raise FileNotFoundError(f"Missing edit timeline CSV: {resolved_timeline}")

    resolved_html = html_path
    if resolved_html is None:
        resolved_html = _path_from_summary(summary_path if summary_path.exists() else None, "html")
    if resolved_html is not None:
        resolved_html = resolved_html.resolve()
    mode = _detect_keep_mode(resolved_comparer, keep_mode)
    return ResolvedInputs(
        run_dir=run_dir,
        comparer_path=resolved_comparer,
        edit_timeline_path=resolved_timeline,
        html_path=resolved_html if resolved_html and resolved_html.exists() else None,
        summary_path=summary_path if summary_path.exists() else None,
        keep_mode=mode,
    )


def _row_is_kept(row: Mapping[str, str], keep_mode: str) -> bool:
    reference_segment = (row.get("Reference Segment") or "").strip()
    transcript_text = (row.get("Text") or "").strip()
    if not reference_segment or not transcript_text:
        return False
    if (row.get("Eliminate") or "").strip().lower() == "x":
        return False
    status = (row.get("Status") or "").strip().upper()
    if "UNMATCHED" in status:
        return False
    if keep_mode == "xml_ready":
        return (row.get("Keep") or "").strip().lower() == "x"
    if keep_mode == "legacy_step2":
        return True
    raise RuntimeError(f"Unsupported keep mode: {keep_mode}")


def _group_timeline_rows(timeline_rows: Sequence[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in timeline_rows:
        transcript_number = (row.get("Transcript #") or row.get("Row ID") or "").strip()
        grouped.setdefault(transcript_number, []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: parse_timecode(row.get("Edit Start Time", "")))
    return grouped


def _timing_basis_from_entries(entries: Sequence[dict[str, str]]) -> str:
    sources = {(entry.get("Timing Source") or "").strip().lower() for entry in entries if (entry.get("Timing Source") or "").strip()}
    if sources and sources == {"groq_word"}:
        return "exact"
    return "inherited_interpolated"


def _row_bounds_from_timeline(row: Mapping[str, str], timeline_entries: Sequence[dict[str, str]]) -> tuple[float, float]:
    edit_starts = [parse_timecode(entry.get("Edit Start Time", "")) for entry in timeline_entries if (entry.get("Edit Start Time") or "").strip()]
    edit_ends = [parse_timecode(entry.get("Edit End Time", "")) for entry in timeline_entries if (entry.get("Edit End Time") or "").strip()]
    if edit_starts and edit_ends:
        return min(edit_starts), max(edit_ends)
    return parse_timecode(row.get("Start Time", "")), parse_timecode(row.get("End Time", ""))


def _build_row_contexts(rows: Sequence[dict[str, str]], timeline_rows: Sequence[dict[str, str]], keep_mode: str) -> list[RowContext]:
    timeline_by_transcript = _group_timeline_rows(timeline_rows)
    contexts: list[RowContext] = []
    for row_index, row in enumerate(rows):
        if not _row_is_kept(row, keep_mode):
            continue
        transcript_number = (row.get("Transcript #") or "").strip()
        row_timeline = timeline_by_transcript.get(transcript_number, [])
        row_start_seconds, row_end_seconds = _row_bounds_from_timeline(row, row_timeline)
        contexts.append(
            RowContext(
                row_index=len(contexts),
                transcript_number=transcript_number,
                row=dict(row),
                text=(row.get("Text") or "").strip(),
                reference_segment=(row.get("Reference Segment") or "").strip(),
                timeline_entries=row_timeline,
                row_start_seconds=row_start_seconds,
                row_end_seconds=row_end_seconds,
                row_timing_basis=_timing_basis_from_entries(row_timeline),
            )
        )
    return contexts


def _pipe_row_from_context(context: RowContext) -> list[str]:
    return [
        "x",
        context.transcript_number,
        context.row.get("Start Time", ""),
        context.row.get("End Time", ""),
        context.text,
        context.reference_segment,
        context.row.get("Match %", ""),
        context.row.get("Status", ""),
    ]


def _analysis_text_and_spans(contexts: Sequence[RowContext], html_path: Path | None) -> tuple[str, list[tuple[int, int] | None], list[list[str]]]:
    legacy = get_legacy_step2_module()
    rows_as_lists = [_pipe_row_from_context(context) for context in contexts]
    if html_path and html_path.exists():
        reference_text = legacy.strip_reference_title(legacy.collect_reference_text(html_path))
    else:
        reference_text = legacy._reference_fallback_from_rows(rows_as_lists, 5, 4)
    analysis_text = legacy.prepare_reference_analysis_text(reference_text)
    spans = legacy.build_row_reference_spans(rows_as_lists, 5, analysis_text)
    return analysis_text, spans, rows_as_lists


@dataclass
class TokenSpan:
    token_index: int
    normalized: str
    raw: str
    start: int
    end: int


def _token_spans(raw_text: str) -> list[TokenSpan]:
    spans: list[TokenSpan] = []
    for token_index, match in enumerate(TOKEN_SPAN_RE.finditer(raw_text or "")):
        raw = match.group(0)
        normalized = normalize_text(raw)
        if not normalized:
            continue
        spans.append(
            TokenSpan(
                token_index=token_index,
                normalized=normalized,
                raw=raw,
                start=match.start(),
                end=match.end(),
            )
        )
    return spans


def _find_token_window(raw_text: str, phrase: str) -> tuple[int, int] | None:
    target_tokens = tokenize(phrase)
    if not target_tokens:
        return None
    spans = _token_spans(raw_text)
    normalized_tokens = [span.normalized for span in spans]
    window = len(target_tokens)
    for start in range(0, max(0, len(normalized_tokens) - window) + 1):
        if normalized_tokens[start:start + window] == target_tokens:
            return spans[start].token_index, spans[start + window - 1].token_index
    return None


def _nearest_token_window(raw_text: str, raw_fragment: str) -> tuple[int, int] | None:
    if not raw_text or not raw_fragment:
        return None
    candidates = [raw_fragment]
    if raw_fragment == "...":
        candidates.append("…")
    for candidate in candidates:
        index = raw_text.find(candidate)
        if index == -1:
            index = raw_text.lower().find(candidate.lower())
        if index == -1:
            continue
        spans = _token_spans(raw_text)
        if not spans:
            return None
        previous = None
        next_span = None
        for span in spans:
            if span.end <= index:
                previous = span
            elif next_span is None:
                next_span = span
                break
        if previous is not None:
            return previous.token_index, previous.token_index
        if next_span is not None:
            return next_span.token_index, next_span.token_index
    return None


def _entries_by_index(entries: Sequence[dict[str, str]], index_key: str, start_index: int, end_index: int) -> list[dict[str, str]]:
    matched: list[dict[str, str]] = []
    for entry in entries:
        index = _safe_int(entry.get(index_key))
        if index is None:
            continue
        if start_index <= index <= end_index:
            matched.append(entry)
    return matched


def _join_entry_tokens(entries: Sequence[dict[str, str]], key: str) -> str:
    tokens = [value.strip() for value in (entry.get(key) or "" for entry in entries) if value.strip()]
    return " ".join(tokens)


def _build_localized_span(
    context: RowContext,
    matched_entries: Sequence[dict[str, str]],
    locator: str,
    transcript_fallback: str,
    reference_fallback: str,
) -> LocalizedSpan | None:
    if not matched_entries:
        return None
    starts = [parse_timecode(entry.get("Edit Start Time", "")) for entry in matched_entries if (entry.get("Edit Start Time") or "").strip()]
    ends = [parse_timecode(entry.get("Edit End Time", "")) for entry in matched_entries if (entry.get("Edit End Time") or "").strip()]
    if not starts or not ends:
        return None
    return LocalizedSpan(
        start_seconds=min(starts),
        end_seconds=max(ends),
        transcript_phrase=_join_entry_tokens(matched_entries, "Transcript Token") or transcript_fallback,
        reference_phrase=_join_entry_tokens(matched_entries, "Reference Token") or reference_fallback,
        timing_basis=_timing_basis_from_entries(matched_entries),
        locator=locator,
    )


def _row_fallback(context: RowContext, transcript_fallback: str, reference_fallback: str) -> LocalizedSpan:
    return LocalizedSpan(
        start_seconds=context.row_start_seconds,
        end_seconds=context.row_end_seconds,
        transcript_phrase=transcript_fallback or context.text,
        reference_phrase=reference_fallback or context.reference_segment,
        timing_basis=context.row_timing_basis,
        locator="row_fallback",
    )


def _reference_timing_fallback(
    context: RowContext,
    reference_fallback: str,
    *,
    locator: str = "reference_anchor",
) -> LocalizedSpan:
    return LocalizedSpan(
        start_seconds=context.row_start_seconds,
        end_seconds=context.row_end_seconds,
        transcript_phrase="",
        reference_phrase=reference_fallback or context.reference_segment,
        timing_basis=context.row_timing_basis,
        locator=locator,
    )


def _spanning_reference_timing_fallback(
    first_context: RowContext,
    last_context: RowContext,
    reference_fallback: str,
    *,
    locator: str,
) -> LocalizedSpan:
    exact = first_context.row_timing_basis == "exact" and last_context.row_timing_basis == "exact"
    return LocalizedSpan(
        start_seconds=min(first_context.row_start_seconds, last_context.row_start_seconds),
        end_seconds=max(first_context.row_end_seconds, last_context.row_end_seconds),
        transcript_phrase="",
        reference_phrase=reference_fallback,
        timing_basis="exact" if exact else "inherited_interpolated",
        locator=locator,
    )


def _approximate_boundary_by_ref_index(
    context: RowContext,
    ref_start: int,
    ref_end: int,
    *,
    prefer: str,
    fallback_text: str,
) -> LocalizedSpan | None:
    # When we know the target block's reference-token range but the transcript
    # missed those exact words, approximate by picking the nearest-by-index
    # timeline entry. prefer="end" → largest ref idx ≤ ref_end; prefer="start"
    # → smallest ref idx ≥ ref_start. Falls back to absolute-nearest if the
    # sidedness constraint yields nothing.
    best_entry: dict[str, str] | None = None
    best_key: int | None = None
    for entry in context.timeline_entries:
        idx = _safe_int(entry.get("Reference Token Index"))
        if idx is None:
            continue
        if prefer == "end":
            if idx > ref_end:
                continue
            if best_key is None or idx > best_key:
                best_key = idx
                best_entry = entry
        else:
            if idx < ref_start:
                continue
            if best_key is None or idx < best_key:
                best_key = idx
                best_entry = entry
    if best_entry is None:
        best_distance: int | None = None
        for entry in context.timeline_entries:
            idx = _safe_int(entry.get("Reference Token Index"))
            if idx is None:
                continue
            distance = abs(idx - (ref_end if prefer == "end" else ref_start))
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_entry = entry
    if best_entry is None:
        return None
    return _build_localized_span(
        context,
        [best_entry],
        f"block_approx_ref_{prefer}",
        fallback_text,
        fallback_text,
    )


def _fallback_token_by_token_match(
    context: RowContext,
    block_tokens: Sequence[str],
    *,
    prefer: str,
    fallback_text: str,
) -> LocalizedSpan | None:
    # Last-resort word-level approximation: for each token in the block, look
    # for the same normalized token anywhere in the row's timeline entries. For
    # prefer="end" walk block tokens and timeline entries from the end; for
    # prefer="start" walk both from the start. First hit wins — that entry's
    # edit start/end gives a real word-level time.
    if not block_tokens or not context.timeline_entries:
        return None
    block_iter = list(reversed(block_tokens)) if prefer == "end" else list(block_tokens)
    entries_iter = list(reversed(context.timeline_entries)) if prefer == "end" else list(context.timeline_entries)
    for block_token in block_iter:
        if not block_token:
            continue
        for entry in entries_iter:
            ref_norm = normalize_text(entry.get("Reference Token") or "")
            trans_norm = normalize_text(entry.get("Transcript Token") or "")
            if block_token == ref_norm or block_token == trans_norm:
                return _build_localized_span(
                    context,
                    [entry],
                    f"block_approx_token_{prefer}",
                    fallback_text,
                    fallback_text,
                )
    return None


def _locate_block_boundary_span(
    context: RowContext,
    block_text: str,
    *,
    prefer: str = "end",
) -> LocalizedSpan | None:
    """Return the per-word span of `block_text` inside `context`'s row.

    The row may cover more than one HTML block (e.g. when adjacent paragraphs
    get merged into a single transcript row). Using the row bounds would land
    the gap on the wrong word. We locate the block text via the row's per-word
    timeline entries with a ladder of decreasingly strict matches, always
    ending at real word-level timings (never row-level) so the gap anchors to
    a spoken word.

    prefer: "end" (for the block preceding a title — we want its last word's
    end time) or "start" (for the block following a title — its first word's
    start time). Biases prefix/suffix order and approximation direction.
    """
    text = (block_text or "").strip()
    if not text:
        return None
    # 1. Exact phrase match in reference or transcript (current behavior).
    span = _localize_value(
        context,
        text,
        allow_row_fallback=False,
        transcript_fallback=text,
        reference_fallback=text,
    )
    if span is not None:
        return span
    tokens = tokenize(text)
    if tokens and len(tokens) > 1:
        # 2. Progressively shorter prefixes/suffixes; for prefer="end" try
        # suffix first (closer to the block's end), for prefer="start" try
        # prefix first.
        for take in (8, 6, 4, 3, 2):
            if take >= len(tokens):
                continue
            suffix_tokens = tokens[-take:]
            prefix_tokens = tokens[:take]
            slice_order = (suffix_tokens, prefix_tokens) if prefer == "end" else (prefix_tokens, suffix_tokens)
            for slice_tokens in slice_order:
                candidate = " ".join(slice_tokens)
                if not candidate:
                    continue
                span = _localize_value(
                    context,
                    candidate,
                    allow_row_fallback=False,
                    transcript_fallback=candidate,
                    reference_fallback=candidate,
                )
                if span is not None:
                    return span
    # 3. Approximation by reference-token index: the block's phrase is in the
    # row's reference segment but the transcript missed those specific words;
    # pick the nearest timeline entry by reference index.
    ref_range = _find_token_window(context.reference_segment, text)
    if ref_range is None and tokens and len(tokens) > 1:
        for take in (8, 6, 4, 3, 2):
            if take >= len(tokens):
                continue
            suffix_tokens = tokens[-take:]
            prefix_tokens = tokens[:take]
            slice_order = (suffix_tokens, prefix_tokens) if prefer == "end" else (prefix_tokens, suffix_tokens)
            for slice_tokens in slice_order:
                candidate = " ".join(slice_tokens)
                if not candidate:
                    continue
                ref_range = _find_token_window(context.reference_segment, candidate)
                if ref_range is not None:
                    break
            if ref_range is not None:
                break
    if ref_range is not None:
        span = _approximate_boundary_by_ref_index(
            context,
            ref_range[0],
            ref_range[1],
            prefer=prefer,
            fallback_text=text,
        )
        if span is not None:
            return span
    # 4. Last-resort: token-by-token match against the row's timeline entries.
    return _fallback_token_by_token_match(
        context,
        tokens,
        prefer=prefer,
        fallback_text=text,
    )


def _title_gap_anchor_span(
    previous_context: RowContext,
    previous_block_text: str,
    following_context: RowContext | None,
    following_block_text: str,
    reference_fallback: str,
) -> LocalizedSpan:
    # Prefer placing the title at the exact moment the phrase is spoken.
    for ctx in [previous_context] + ([following_context] if following_context is not None else []):
        direct = _localize_value(ctx, reference_fallback, allow_row_fallback=False, reference_fallback=reference_fallback)
        if direct is not None:
            return LocalizedSpan(
                start_seconds=direct.start_seconds,
                end_seconds=direct.end_seconds,
                transcript_phrase=direct.transcript_phrase,
                reference_phrase=reference_fallback,
                timing_basis=direct.timing_basis,
                locator="title_spoken_anchor",
            )

    # Fallback: anchor to the speech gap between surrounding HTML blocks.
    previous_span = _locate_block_boundary_span(previous_context, previous_block_text, prefer="end")
    if previous_span is not None:
        gap_start = previous_span.end_seconds
        previous_basis_exact = previous_span.timing_basis == "exact"
    else:
        print(
            f"⚠️  title gap: no word-level boundary for previous block "
            f"'{(previous_block_text or '')[:80]}' — falling back to row end"
        )
        gap_start = previous_context.row_end_seconds
        previous_basis_exact = previous_context.row_timing_basis == "exact"

    if following_context is not None:
        following_span = _locate_block_boundary_span(following_context, following_block_text, prefer="start")
        if following_span is not None:
            gap_end = following_span.start_seconds
            following_basis_exact = following_span.timing_basis == "exact"
        else:
            print(
                f"⚠️  title gap: no word-level boundary for following block "
                f"'{(following_block_text or '')[:80]}' — falling back to row start"
            )
            gap_end = following_context.row_start_seconds
            following_basis_exact = following_context.row_timing_basis == "exact"
    else:
        gap_end = gap_start
        following_basis_exact = True

    if gap_end < gap_start:
        gap_end = gap_start
    exact = previous_basis_exact and following_basis_exact
    return LocalizedSpan(
        start_seconds=gap_start,
        end_seconds=gap_end,
        transcript_phrase="",
        reference_phrase=reference_fallback,
        timing_basis="exact" if exact else "inherited_interpolated",
        locator="title_gap_anchor",
    )


def _localize_value(
    context: RowContext,
    value: str,
    *,
    allow_row_fallback: bool = False,
    transcript_fallback: str = "",
    reference_fallback: str = "",
) -> LocalizedSpan | None:
    ref_range = _find_token_window(context.reference_segment, value)
    if ref_range is not None:
        matched = _entries_by_index(context.timeline_entries, "Reference Token Index", ref_range[0], ref_range[1])
        span = _build_localized_span(context, matched, "reference_span", transcript_fallback, reference_fallback or value)
        if span is not None:
            return span
    transcript_range = _find_token_window(context.text, value)
    if transcript_range is not None:
        matched = _entries_by_index(context.timeline_entries, "Transcript Token Index", transcript_range[0], transcript_range[1])
        span = _build_localized_span(context, matched, "transcript_span", transcript_fallback or value, reference_fallback)
        if span is not None:
            return span
    ref_symbol_range = _nearest_token_window(context.reference_segment, value)
    if ref_symbol_range is not None:
        matched = _entries_by_index(context.timeline_entries, "Reference Token Index", ref_symbol_range[0], ref_symbol_range[1])
        span = _build_localized_span(context, matched, "reference_symbol", transcript_fallback or value, reference_fallback or value)
        if span is not None:
            return span
    transcript_symbol_range = _nearest_token_window(context.text, value)
    if transcript_symbol_range is not None:
        matched = _entries_by_index(context.timeline_entries, "Transcript Token Index", transcript_symbol_range[0], transcript_symbol_range[1])
        span = _build_localized_span(context, matched, "transcript_symbol", transcript_fallback or value, reference_fallback or value)
        if span is not None:
            return span
    if allow_row_fallback:
        approx = _fallback_token_by_token_match(
            context,
            tokenize(value),
            prefer="start",
            fallback_text=transcript_fallback or value,
        )
        if approx is not None:
            return approx
        return _row_fallback(context, transcript_fallback or context.text, reference_fallback or value)
    return None


def _quote_row_fallback_allowed(context: RowContext, value: str) -> bool:
    if not value:
        return False
    overlap = max(
        ordered_token_overlap(context.text, value),
        ordered_token_overlap(context.reference_segment, value),
    )
    return overlap >= 0.60


def _list_group_illustration_type(list_type: str) -> str | None:
    normalized = (list_type or "").strip().lower()
    mapping = {
        "bullet": "list_bullet_group",
        "dash": "list_dash_group",
        "number": "list_number_group",
        "check": "list_check_group",
    }
    return mapping.get(normalized)


def _record_from_localized(
    localized: LocalizedSpan | None,
    illustration_type: str,
    *,
    context: RowContext,
    source_pass: str,
    transcript_word: str | None = None,
    reference_word: str | None = None,
    asset_category: str | None = None,
    link_url: str = "",
    link_kind: str = "",
    html_insert_index: int | None = None,
) -> IllustrationRecord | None:
    if localized is None:
        return None
    resolved_transcript = _normalize_phrase(transcript_word if transcript_word is not None else localized.transcript_phrase)
    resolved_reference = _normalize_phrase(reference_word if reference_word is not None else localized.reference_phrase)
    return IllustrationRecord(
        start_seconds=localized.start_seconds,
        end_seconds=localized.end_seconds,
        transcript_word=resolved_transcript,
        reference_word=resolved_reference,
        illustration_type=illustration_type,
        timing_basis=localized.timing_basis,
        source_pass=source_pass,
        asset_category=asset_category if asset_category is not None else _asset_category_for(illustration_type),
        transcript_number=context.transcript_number,
        row_id=(context.row.get("Row ID") or context.transcript_number or "").strip(),
        locator=localized.locator,
        normalized_match_key=_normalize_match_key(link_url or resolved_reference or resolved_transcript),
        link_url=link_url,
        link_kind=link_kind,
        html_insert_index=html_insert_index,
    )


def _push_record(
    bucket: list[IllustrationRecord],
    record: IllustrationRecord | None,
    seen: set[tuple[str, str, str, int, int]],
    skipped: Counter[str],
) -> None:
    if record is None:
        skipped["no_span"] += 1
        return
    if record.end_seconds < record.start_seconds:
        skipped["no_timing"] += 1
        return
    if not record.transcript_word and not record.reference_word:
        skipped["non_illustrable"] += 1
        return
    fingerprint = (
        record.illustration_type,
        record.transcript_word.casefold(),
        record.reference_word.casefold(),
        int(round(record.start_seconds * 1000)),
        int(round(record.end_seconds * 1000)),
    )
    if fingerprint in seen:
        skipped["duplicate"] += 1
        return
    seen.add(fingerprint)
    bucket.append(record)


def _metric_record_priority(record: IllustrationRecord) -> tuple[int, int, float]:
    exact_score = 1 if record.timing_basis == "exact" else 0
    locator_score = 1 if record.locator in {"transcript_span", "reference_span"} else 0
    duration = max(0.0, record.end_seconds - record.start_seconds)
    return (exact_score, locator_score, duration)


def _same_metric_anchor_key(record: IllustrationRecord) -> tuple[str, str, str, int]:
    transcript_number = (record.transcript_number or "").strip()
    row_id = (record.row_id or "").strip()
    anchor_id = transcript_number or row_id
    return (
        record.illustration_type,
        transcript_number,
        anchor_id,
        int(round(record.start_seconds * 1000)),
    )


def _dedupe_nearby_metric_records(records: Sequence[IllustrationRecord]) -> tuple[list[IllustrationRecord], int]:
    ordered = sorted(
        records,
        key=lambda record: (
            record.start_seconds,
            record.end_seconds,
            record.illustration_type,
        ),
    )
    deduped: list[IllustrationRecord] = []
    recent_by_key: dict[tuple[str, str], int] = {}
    recent_by_anchor: dict[tuple[str, str, str, int], int] = {}
    skipped_duplicates = 0
    for record in ordered:
        if (
            record.asset_category != "social_ranking_punctuation"
            or record.illustration_type not in NEARBY_METRIC_TYPES
            or not record.normalized_match_key
        ):
            deduped.append(record)
            continue
        anchor_key = _same_metric_anchor_key(record)
        existing_anchor_index = recent_by_anchor.get(anchor_key)
        if existing_anchor_index is not None:
            skipped_duplicates += 1
            continue
        dedupe_key = (record.illustration_type, record.normalized_match_key)
        existing_index = recent_by_key.get(dedupe_key)
        if existing_index is None:
            new_index = len(deduped)
            recent_by_key[dedupe_key] = new_index
            recent_by_anchor[anchor_key] = new_index
            deduped.append(record)
            continue
        existing = deduped[existing_index]
        if record.start_seconds - existing.end_seconds > NEARBY_METRIC_DEDUP_SECONDS:
            new_index = len(deduped)
            recent_by_key[dedupe_key] = new_index
            recent_by_anchor[anchor_key] = new_index
            deduped.append(record)
            continue
        skipped_duplicates += 1
        if _metric_record_priority(record) > _metric_record_priority(existing):
            deduped[existing_index] = record
            recent_by_anchor[anchor_key] = existing_index
    return deduped, skipped_duplicates


def _normalized_quote_prefix_match(ai_key: str, quote_key: str) -> bool:
    ai_tokens = [token for token in tokenize(ai_key) if token]
    quote_tokens = [token for token in tokenize(quote_key) if token]
    if len(quote_tokens) < 4 or len(ai_tokens) < len(quote_tokens):
        return False
    return ai_tokens[: len(quote_tokens)] == quote_tokens


def _suppress_ai_quote_overlaps(
    format_records: Sequence[IllustrationRecord],
    ai_records: Sequence[IllustrationRecord],
    *,
    time_window_seconds: float = 6.0,
) -> tuple[list[IllustrationRecord], int]:
    direct_quotes = [
        record
        for record in format_records
        if record.asset_category == "quote_highlights" and record.normalized_match_key
    ]
    if not direct_quotes:
        return list(ai_records), 0
    kept_records: list[IllustrationRecord] = []
    suppressed = 0
    for record in ai_records:
        should_suppress = any(
            _normalized_quote_prefix_match(record.normalized_match_key, direct_quote.normalized_match_key)
            and abs(record.start_seconds - direct_quote.start_seconds) <= time_window_seconds
            for direct_quote in direct_quotes
        )
        if should_suppress:
            suppressed += 1
            continue
        kept_records.append(record)
    return kept_records, suppressed


def _regex_entries(pattern: re.Pattern[str], text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for match in pattern.finditer(text):
        snippet = _normalize_phrase(match.group(0))
        if not snippet:
            continue
        key = (match.start(), snippet.casefold())
        if key in seen:
            continue
        seen.add(key)
        entries.append({"text": snippet, "start_index": match.start()})
    return entries


def _distance_entries(legacy: Any, text: str) -> list[dict[str, Any]]:
    excluded = [(match.start(), match.end()) for pattern in (legacy.SPEED_PATTERN, legacy.SURFACE_PATTERN) for match in pattern.finditer(text)]
    entries: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for match in legacy.DISTANCE_PATTERN.finditer(text):
        start, end = match.start(), match.end()
        if any(left <= start < right or left < end <= right for left, right in excluded):
            continue
        suffix = text[end:end + 4]
        if re.match(r"\s*/[hH]|\s*[hH]\b|\s*[²2]", suffix):
            continue
        snippet = _normalize_phrase(match.group(0))
        key = (start, snippet.casefold())
        if key in seen:
            continue
        seen.add(key)
        entries.append({"text": snippet, "start_index": start})
    return entries


def _label_to_social_type(label: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", (label or "").strip().lower()).strip("_")
    return SOCIAL_OUTPUT_TYPES.get(normalized)


def _link_type_for(link: ClassifiedLink) -> str:
    href_lower = link.href.lower()
    if "twitter.com" in href_lower or "x.com/" in href_lower:
        return "tweet_link"
    if link.category == "image":
        return "image_link"
    if link.category == "video":
        return "video_link_excerpt" if link.is_excerpt else "video_link_direct"
    if link.category == "website":
        return "website_link"
    return "article_link"


def _link_kind_for(link: ClassifiedLink) -> str:
    link_type = _link_type_for(link)
    if link_type == "tweet_link":
        return "tweet"
    if link_type == "image_link":
        return "image"
    if link_type == "video_link_excerpt":
        return "video_excerpt"
    if link_type == "video_link_direct":
        return "video_direct"
    if link_type == "website_link":
        return "website"
    return "article"


def _map_offset_to_row_index(
    legacy: Any,
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
    text_value: str,
    offset: int | None,
    *,
    allow_neighbor: bool = False,
) -> int | None:
    if allow_neighbor:
        row_index = legacy.find_row_for_offset_or_neighbor(spans, offset)
    else:
        row_index = legacy.find_row_for_offset(spans, offset)
    if row_index is None:
        row_index = legacy.fallback_row_lookup(rows_as_lists, 5, 4, text_value)
    return row_index


def _find_title_anchor_contexts(
    legacy: Any,
    contexts: Sequence[RowContext],
    html_blocks: Sequence[Mapping[str, object]],
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
    analysis_text: str,
    title_entry: Mapping[str, object],
) -> tuple[tuple[RowContext, str] | None, tuple[RowContext, str] | None]:
    title_text = str(title_entry.get("text") or "").strip()
    if not title_text or not contexts:
        return None, None

    title_block_index: int | None = None
    normalized_title = legacy.normalize_for_matching(title_text)
    if not normalized_title:
        return None, None

    for block in html_blocks:
        block_text = str(block.get("text") or "").strip()
        if not block_text:
            continue
        if legacy.normalize_for_matching(block_text) != normalized_title:
            continue
        tag_name = str(block.get("tag") or "").strip().lower()
        if tag_name in getattr(legacy, "TITLE_TAGS", set()) or "title" in tag_name:
            title_block_index = int(block.get("block_index", -1))
            break

    if title_block_index is None:
        return None, None

    def resolve_context(block: Mapping[str, object]) -> tuple[RowContext, str] | None:
        block_text = str(block.get("text") or "").strip()
        if not block_text:
            return None
        normalized_block = legacy.normalize_for_matching(block_text)
        if not normalized_block or normalized_block == normalized_title:
            return None
        start_index = analysis_text.find(normalized_block)
        row_index = _map_offset_to_row_index(
            legacy,
            spans,
            rows_as_lists,
            block_text,
            start_index if start_index >= 0 else None,
            allow_neighbor=True,
        )
        if row_index is None or not (0 <= row_index < len(contexts)):
            return None
        return contexts[row_index], block_text

    previous_pair: tuple[RowContext, str] | None = None
    following_pair: tuple[RowContext, str] | None = None
    previous_block_text_for_fallback: str = ""

    for block in reversed(html_blocks):
        block_index = int(block.get("block_index", -1))
        if block_index >= title_block_index:
            continue
        block_text = str(block.get("text") or "").strip()
        if block_text and not previous_block_text_for_fallback:
            previous_block_text_for_fallback = block_text
        previous_pair = resolve_context(block)
        if previous_pair is not None:
            break

    for block in html_blocks:
        block_index = int(block.get("block_index", -1))
        if block_index <= title_block_index:
            continue
        following_pair = resolve_context(block)
        if following_pair is not None:
            break

    # Fallback: if we found a following context but no previous context, use
    # the row immediately preceding the following context's row. That row
    # owns the speech that ends right before the title — its end time is the
    # natural gap-open point, and its timeline entries feed the word-level
    # approximator in `_locate_block_boundary_span`.
    if previous_pair is None and following_pair is not None:
        following_context, _following_block_text = following_pair
        preceding_index = following_context.row_index - 1
        if 0 <= preceding_index < len(contexts):
            preceding_context = contexts[preceding_index]
            preceding_text = previous_block_text_for_fallback or preceding_context.reference_segment or preceding_context.text
            if preceding_text:
                previous_pair = (preceding_context, preceding_text)

    return previous_pair, following_pair


def _localize_structured_title(
    legacy: Any,
    contexts: Sequence[RowContext],
    html_blocks: Sequence[Mapping[str, object]],
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
    analysis_text: str,
    title_entry: Mapping[str, object],
) -> tuple[RowContext | None, LocalizedSpan | None]:
    text_value = str(title_entry.get("text") or "").strip()
    if not text_value:
        return None, None

    previous_pair, following_pair = _find_title_anchor_contexts(
        legacy,
        contexts,
        html_blocks,
        spans,
        rows_as_lists,
        analysis_text,
        title_entry,
    )

    # Look up the title's own comparison row. Its row_start_seconds is the edit-timeline
    # time when the phrase is actually spoken — more reliable than deriving timing from
    # adjacent blocks whose boundaries can be 6+ seconds away from the spoken phrase.
    _title_start_index = title_entry.get("start_index")
    _title_row_index = _map_offset_to_row_index(
        legacy,
        spans,
        rows_as_lists,
        text_value,
        _title_start_index if isinstance(_title_start_index, int) else None,
        allow_neighbor=True,
    )
    title_own_context: RowContext | None = (
        contexts[_title_row_index]
        if _title_row_index is not None and 0 <= _title_row_index < len(contexts)
        else None
    )

    def _spoken_span(ref_phrase: str, ctx: RowContext) -> LocalizedSpan:
        return LocalizedSpan(
            start_seconds=ctx.row_start_seconds,
            end_seconds=ctx.row_end_seconds,
            transcript_phrase=ctx.text,
            reference_phrase=ref_phrase,
            timing_basis=ctx.row_timing_basis,
            locator="title_spoken_anchor",
        )

    if previous_pair is not None:
        previous_context, previous_block_text = previous_pair
        if following_pair is not None:
            following_context, following_block_text = following_pair
            anchor_context = following_context
            # Positional fallback: title row sits immediately before following_context.
            # _map_offset_to_row_index fails when start_index is not in spans space;
            # the row at following_context.row_index - 1 is the title's own transcript row.
            if title_own_context is None:
                _pos = following_context.row_index - 1
                if 0 <= _pos < len(contexts):
                    title_own_context = contexts[_pos]
        else:
            following_context, following_block_text = None, ""
            anchor_context = previous_context
            # Positional fallback: title row sits immediately after previous_context.
            if title_own_context is None:
                _pos = previous_context.row_index + 1
                if 0 <= _pos < len(contexts):
                    title_own_context = contexts[_pos]
        if title_own_context is not None:
            return anchor_context, _spoken_span(text_value, title_own_context)
        # Titles are always spoken — use the previous spoken row as anchor.
        return anchor_context, _spoken_span(text_value, previous_context)
    if following_pair is not None:
        following_context, following_block_text = following_pair
        preceding_index = following_context.row_index - 1
        if 0 <= preceding_index < len(contexts):
            preceding_context = contexts[preceding_index]
            # Positional fallback: preceding_context IS the row immediately before the
            # following block, which is the title's own transcript row.
            if title_own_context is None:
                title_own_context = preceding_context
            return following_context, _spoken_span(text_value, title_own_context)
        return following_context, _reference_timing_fallback(
            following_context,
            text_value,
            locator="title_anchor",
        )

    # Last resort: no adjacent HTML block could be matched.
    context = title_own_context
    if context is None:
        return None, None
    localized = _localize_value(
        context,
        text_value,
        allow_row_fallback=False,
        transcript_fallback="",
        reference_fallback=text_value,
    )
    if localized is not None:
        return context, localized
    # _localize_value failed (word-level entries may be in source-timecode space);
    # row_start_seconds is always in edit-timeline space so use it directly.
    return context, _spoken_span(text_value, context)


_WEAK_LINK_LABELS = {
    "extrait",
    "video",
    "vidéo",
    "lien",
    "link",
    "source",
    "voir",
    "watch",
    "play",
}


def _is_weak_link_anchor(label: str) -> bool:
    normalized = _normalize_phrase(label)
    if not normalized:
        return True
    if normalized.casefold() in _WEAK_LINK_LABELS:
        return True
    return len(tokenize(normalized)) <= 2 and len(normalized) <= 16


def _candidate_link_context_texts(link: ClassifiedLink) -> list[str]:
    anchor = _normalize_phrase(link.text or "")
    context = _normalize_phrase(link.context or "")
    candidates: list[str] = []
    if context and context != anchor and len(context) >= len(anchor) + 12:
        candidates.append(context)
    return candidates


def _find_link_neighbor_contexts(
    legacy: Any,
    contexts: Sequence[RowContext],
    html_blocks: Sequence[Mapping[str, object]],
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
    analysis_text: str,
    link: ClassifiedLink,
) -> tuple[RowContext | None, RowContext | None]:
    anchor = _normalize_phrase(link.text or link.context or link.href)
    if not anchor:
        return None, None

    anchor_norm = legacy.normalize_for_matching(anchor)
    context_norm = legacy.normalize_for_matching(_normalize_phrase(link.context or ""))
    block_index: int | None = None

    for block in html_blocks:
        block_text = str(block.get("text") or "").strip()
        if not block_text:
            continue
        block_norm = legacy.normalize_for_matching(block_text)
        if anchor_norm and block_norm == anchor_norm:
            block_index = int(block.get("block_index", -1))
            break
        if context_norm and block_norm == context_norm:
            block_index = int(block.get("block_index", -1))
            break
        if anchor_norm and anchor_norm in block_norm.split():
            block_index = int(block.get("block_index", -1))
            break

    if block_index is None or block_index < 0:
        return None, None

    def resolve_context(block: Mapping[str, object]) -> RowContext | None:
        block_text = _normalize_phrase(str(block.get("text") or ""))
        if not block_text or _is_weak_link_anchor(block_text):
            return None
        normalized_block = legacy.normalize_for_matching(block_text)
        if not normalized_block:
            return None
        start_index = analysis_text.find(normalized_block)
        row_index = _map_offset_to_row_index(
            legacy,
            spans,
            rows_as_lists,
            block_text,
            start_index if start_index >= 0 else None,
            allow_neighbor=True,
        )
        if row_index is None or not (0 <= row_index < len(contexts)):
            return None
        return contexts[row_index]

    previous_context: RowContext | None = None
    following_context: RowContext | None = None

    for block in reversed(html_blocks):
        current_index = int(block.get("block_index", -1))
        if current_index >= block_index:
            continue
        previous_context = resolve_context(block)
        if previous_context is not None:
            break

    for block in html_blocks:
        current_index = int(block.get("block_index", -1))
        if current_index <= block_index:
            continue
        following_context = resolve_context(block)
        if following_context is not None:
            break

    return previous_context, following_context


def _resolve_link_context(
    legacy: Any,
    contexts: Sequence[RowContext],
    html_blocks: Sequence[Mapping[str, object]],
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
    analysis_text: str,
    link: ClassifiedLink,
) -> tuple[RowContext | None, str | None]:
    anchor = _normalize_phrase(link.text or link.context or link.href)
    anchor_norm = legacy.normalize_for_matching(anchor)
    if anchor_norm:
        for block in html_blocks:
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            block_norm = legacy.normalize_for_matching(block_text)
            if not block_norm or anchor_norm not in block_norm or block_norm == anchor_norm:
                continue
            start_index = analysis_text.find(block_norm)
            row_index = _map_offset_to_row_index(
                legacy,
                spans,
                rows_as_lists,
                block_text,
                start_index if start_index >= 0 else None,
                allow_neighbor=True,
            )
            if row_index is not None and 0 <= row_index < len(contexts):
                return contexts[row_index], block_text

    for candidate_text in _candidate_link_context_texts(link):
        normalized_candidate = legacy.normalize_for_matching(candidate_text)
        start_index = analysis_text.find(normalized_candidate) if normalized_candidate else -1
        row_index = _map_offset_to_row_index(
            legacy,
            spans,
            rows_as_lists,
            candidate_text,
            start_index if start_index >= 0 else None,
            allow_neighbor=True,
        )
        if row_index is not None and 0 <= row_index < len(contexts):
            return contexts[row_index], candidate_text

    previous_context, following_context = _find_link_neighbor_contexts(
        legacy,
        contexts,
        html_blocks,
        spans,
        rows_as_lists,
        analysis_text,
        link,
    )
    if previous_context is not None:
        return previous_context, previous_context.reference_segment or previous_context.text
    if following_context is not None:
        return following_context, following_context.reference_segment or following_context.text
    return None, None


def _link_localization_value(
    legacy: Any,
    link: ClassifiedLink,
    *,
    anchor: str,
    context: RowContext | None,
    resolved_context_text: str | None,
) -> str:
    if not _is_weak_link_anchor(anchor):
        return link.text or link.context or link.href
    for candidate in (
        resolved_context_text,
        _normalize_phrase(link.context or ""),
        context.reference_segment if context is not None else "",
        context.text if context is not None else "",
        link.text or link.context or link.href,
    ):
        normalized_candidate = _normalize_phrase(candidate)
        if not normalized_candidate:
            continue
        if len(tokenize(normalized_candidate)) <= 2 and len(normalized_candidate) <= 16:
            continue
        return normalized_candidate
    return link.text or link.context or link.href


def _build_format_records(
    contexts: Sequence[RowContext],
    html_path: Path | None,
    analysis_text: str,
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[Sequence[str]],
) -> tuple[list[IllustrationRecord], Counter[str], Counter[str]]:
    legacy = get_legacy_step2_module()
    records: list[IllustrationRecord] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    html_bundle = legacy.parse_html_feature_bundle(html_path)
    html_blocks = list(html_bundle.get("blocks", []))

    structured_titles = extract_structured_titles(html_path)
    for entry in locate_structured_titles(structured_titles, analysis_text):
        text_value = str(entry.get("text") or "").strip()
        if not text_value:
            skipped["non_illustrable"] += 1
            continue
        level = str(entry.get("level") or "TITLE").strip().upper()
        illustration_type = "title_h1" if level == "H1" else "title_h2" if level == "H2" else "title_h3plus"
        context, localized = _localize_structured_title(
            legacy,
            contexts,
            html_blocks,
            spans,
            rows_as_lists,
            analysis_text,
            entry,
        )
        if context is None or localized is None:
            skipped["no_span"] += 1
            continue
        record = _record_from_localized(
            localized,
            illustration_type,
            context=context,
            source_pass="format",
            transcript_word="",
            reference_word=text_value,
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts[record.illustration_type] += 1

    for group in html_bundle.get("list_groups", []):
        if not isinstance(group, Mapping):
            continue
        list_type = _list_group_illustration_type(str(group.get("list_type") or ""))
        items = [
            str(item).strip()
            for item in group.get("items", [])
            if str(item).strip()
        ]
        if list_type is None or not (3 <= len(items) <= 7):
            continue
        row_indices: list[int] = []
        for item in items:
            normalized_item = legacy.normalize_for_matching(item)
            start_index = analysis_text.find(normalized_item) if normalized_item else -1
            row_index = _map_offset_to_row_index(
                legacy,
                spans,
                rows_as_lists,
                item,
                start_index if start_index >= 0 else None,
                allow_neighbor=True,
            )
            if row_index is not None:
                row_indices.append(row_index)
        ordered_indices = sorted({idx for idx in row_indices if 0 <= idx < len(contexts)})
        if not ordered_indices:
            skipped["no_span"] += 1
            continue
        first_context = contexts[ordered_indices[0]]
        last_context = contexts[ordered_indices[-1]]
        joined_text = " | ".join(items)
        localized = _spanning_reference_timing_fallback(
            first_context,
            last_context,
            joined_text,
            locator="list_group",
        )
        record = _record_from_localized(
            localized,
            list_type,
            context=first_context,
            source_pass="format",
            transcript_word=joined_text,
            reference_word=joined_text,
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts[record.illustration_type] += 1

    for quote_entry in legacy.detect_quotes_in_text(analysis_text):
        text_value = str(quote_entry.get("text") or "").strip()
        start_index = quote_entry.get("start_index") if isinstance(quote_entry.get("start_index"), int) else None
        row_index = _map_offset_to_row_index(legacy, spans, rows_as_lists, text_value, start_index, allow_neighbor=False)
        if row_index is None:
            skipped["no_span"] += 1
            continue
        context = contexts[row_index]
        localized = _localize_value(
            context,
            text_value,
            allow_row_fallback=_quote_row_fallback_allowed(context, text_value),
            transcript_fallback=context.text,
            reference_fallback=text_value,
        )
        record = _record_from_localized(
            localized,
            "quote",
            context=context,
            source_pass="format",
            reference_word=text_value,
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts[record.illustration_type] += 1

    for html_insert_index, link in enumerate(extract_classified_links(html_path), start=1):
        anchor = _normalize_phrase(link.text or link.context or link.href)
        if not anchor:
            skipped["non_illustrable"] += 1
            continue
        context: RowContext | None = None
        resolved_context_text: str | None = None
        anchor_index = analysis_text.find(legacy.normalize_for_matching(anchor))
        row_index = _map_offset_to_row_index(
            legacy,
            spans,
            rows_as_lists,
            anchor,
            anchor_index if anchor_index >= 0 else None,
            allow_neighbor=True,
        )
        if row_index is not None and 0 <= row_index < len(contexts):
            context = contexts[row_index]
        if _is_weak_link_anchor(anchor):
            resolved_context, resolved_context_text = _resolve_link_context(
                legacy,
                contexts,
                html_blocks,
                spans,
                rows_as_lists,
                analysis_text,
                link,
            )
            if resolved_context is not None:
                context = resolved_context
        if context is None:
            skipped["no_span"] += 1
            continue
        localization_value = _link_localization_value(
            legacy,
            link,
            anchor=anchor,
            context=context,
            resolved_context_text=resolved_context_text,
        )
        localized = _localize_value(
            context,
            localization_value,
            allow_row_fallback=True,
            transcript_fallback=context.text,
            reference_fallback=localization_value,
        )
        record = _record_from_localized(
            localized,
            _link_type_for(link),
            context=context,
            source_pass="format",
            transcript_word=context.text if localized and localized.locator == "row_fallback" else None,
            reference_word=localization_value,
            link_url=link.href,
            link_kind=_link_kind_for(link),
            html_insert_index=html_insert_index,
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts[record.illustration_type] += 1

    for context in contexts:
        combined = " ".join(part for part in (context.text, context.reference_segment) if part)
        matching_blocks = legacy.match_html_blocks_to_row(context.text, context.reference_segment, html_blocks)

        for symbol, illustration_type in PUNCTUATION_TYPES.items():
            if symbol == "..." and ("..." not in context.reference_segment and "…" not in context.reference_segment):
                continue
            if symbol != "..." and symbol not in context.reference_segment:
                continue
            localized = _localize_value(context, symbol, transcript_fallback=symbol, reference_fallback=symbol)
            record = _record_from_localized(
                localized,
                illustration_type,
                context=context,
                source_pass="format",
                transcript_word=symbol,
                reference_word=symbol,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for hashtag in legacy.detect_hashtags(context.text, context.reference_segment):
            localized = _localize_value(context, hashtag, transcript_fallback=hashtag, reference_fallback=hashtag)
            record = _record_from_localized(
                localized,
                "hashtag",
                context=context,
                source_pass="format",
                transcript_word=hashtag,
                reference_word=hashtag,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for spoken_url in legacy.detect_pattern_matches(legacy.SPOKEN_URL_PATTERN, combined):
            localized = _localize_value(context, spoken_url, transcript_fallback=spoken_url, reference_fallback=spoken_url)
            record = _record_from_localized(
                localized,
                "spoken_url",
                context=context,
                source_pass="format",
                transcript_word=spoken_url,
                reference_word=spoken_url,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        lowered = combined.lower()
        emitted_socials: set[str] = set()
        for keyword, label in legacy.SOCIAL_KEYWORDS.items():
            if keyword not in lowered:
                continue
            social_type = _label_to_social_type(label)
            if social_type is None or social_type in emitted_socials:
                continue
            emitted_socials.add(social_type)
            localized = _localize_value(context, label, transcript_fallback=label, reference_fallback=label)
            record = _record_from_localized(
                localized,
                social_type,
                context=context,
                source_pass="format",
                transcript_word=label,
                reference_word=label,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for fragment in legacy.extract_matching_html_fragments(legacy.normalize_lookup_text(combined), matching_blocks, "bold_parts"):
            localized = _localize_value(context, fragment, allow_row_fallback=True, transcript_fallback=context.text, reference_fallback=fragment)
            record = _record_from_localized(
                localized,
                "bold",
                context=context,
                source_pass="format",
                transcript_word=fragment,
                reference_word=fragment,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for fragment in legacy.extract_matching_html_fragments(legacy.normalize_lookup_text(combined), matching_blocks, "italic_parts"):
            localized = _localize_value(context, fragment, allow_row_fallback=True, transcript_fallback=context.text, reference_fallback=fragment)
            record = _record_from_localized(
                localized,
                "italic",
                context=context,
                source_pass="format",
                transcript_word=fragment,
                reference_word=fragment,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        marker_signals = legacy.dedupe_texts(
            signal
            for block in matching_blocks
            for signal in block.get("marker_signals", [])
        )
        for signal in marker_signals:
            signal_type = "list_dash" if "dash" in signal.lower() or signal.strip() == "-" else "list_bullet"
            localized = _localize_value(context, signal, allow_row_fallback=True, transcript_fallback=context.text, reference_fallback=signal)
            record = _record_from_localized(
                localized,
                signal_type,
                context=context,
                source_pass="format",
                transcript_word=context.text if localized and localized.locator == "row_fallback" else signal,
                reference_word=signal,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for emoji_value in legacy.detect_concrete_emoji(combined):
            localized = _localize_value(context, emoji_value, allow_row_fallback=True, transcript_fallback=emoji_value, reference_fallback=emoji_value)
            record = _record_from_localized(
                localized,
                "concrete_emoji",
                context=context,
                source_pass="format",
                transcript_word=emoji_value,
                reference_word=emoji_value,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

    deduped_records, collapsed_duplicates = _dedupe_nearby_metric_records(records)
    if collapsed_duplicates:
        skipped["duplicate"] += collapsed_duplicates
        counts = Counter(record.illustration_type for record in deduped_records)
    return deduped_records, counts, skipped


def _first_regex_match(patterns: Sequence[re.Pattern[str]], *sources: str) -> str:
    for source in sources:
        for pattern in patterns:
            match = pattern.search(source or "")
            if match:
                return _normalize_phrase(match.group(0))
    return ""


def _build_ai_records(
    contexts: Sequence[RowContext],
    html_path: Path | None,
    analysis_text: str,
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[list[str]],
) -> tuple[list[IllustrationRecord], Counter[str], Counter[str]]:
    legacy = get_legacy_step2_module()
    api_key: str | None
    try:
        api_key = legacy.resolve_api_key(None)
    except Exception as exc:
        print(
            f"WARNING: timed AI illustrator fallback: Anthropic API unavailable ({exc}); AI-only illustration tagging will be skipped.",
            file=sys.stderr,
        )
        api_key = None
    language = legacy.detect_language_from_text(analysis_text)

    tags: dict[int, set] = {}
    if api_key:
        try:
            tags, _zoom = legacy.tag_rows_with_claude(
                rows_as_lists,
                api_key,
                legacy.DEFAULT_CTA_MODEL,
                1200,
                60,
            )
        except Exception as exc:
            print(
                f"WARNING: timed AI illustrator fallback: CTA tagging failed ({exc}); continuing without Claude CTA tags.",
                file=sys.stderr,
            )
    legacy.apply_deterministic_cta_tags(rows_as_lists, tags)

    mention_data: Mapping[str, object] | dict[str, object] = {}
    if api_key:
        try:
            mention_data = legacy.extract_mentions_with_claude(
                analysis_text,
                api_key,
                legacy.DEFAULT_NOUNS_MODEL,
                1500,
                language,
            )
        except Exception as exc:
            print(
                f"WARNING: timed AI illustrator fallback: Mention extraction failed ({exc}); continuing with deterministic fallbacks only.",
                file=sys.stderr,
            )
    legacy.normalize_feeling_annotations(mention_data)
    legacy.split_legacy_number_entries(mention_data, language)

    fallback_dates, date_spans = legacy.detect_date_candidates(analysis_text, language)
    fallback_numbers = legacy.detect_number_candidates(analysis_text, date_spans)

    merged_dates, injected_dates = legacy.merge_annotation_entries(mention_data.get("date"), fallback_dates)
    if merged_dates:
        if html_path is None and injected_dates and len(merged_dates) > legacy.FALLBACK_DATE_LIMIT:
            mention_data["date"] = legacy.limit_unique_entries(merged_dates, legacy.FALLBACK_DATE_LIMIT)
        else:
            mention_data["date"] = merged_dates

    merged_numbers, _ = legacy.merge_annotation_entries(mention_data.get("number"), fallback_numbers)
    if merged_numbers:
        mention_data["number"] = merged_numbers

    legacy.split_money_number_mentions(mention_data)
    fallback_institutions = legacy.detect_institution_candidates(analysis_text)
    merged_institutions, _ = legacy.merge_annotation_entries(mention_data.get("gov_institution"), fallback_institutions)
    if merged_institutions:
        mention_data["gov_institution"] = merged_institutions

    feeling_entries = mention_data.get("feeling")
    if not isinstance(feeling_entries, list) or not feeling_entries:
        targeted_feelings = []
        if api_key:
            try:
                targeted_feelings = legacy.extract_targeted_annotations(
                    "feeling",
                    analysis_text,
                    api_key,
                    legacy.DEFAULT_NOUNS_MODEL,
                    1500,
                    language,
                )
            except Exception as exc:
                print(
                    f"WARNING: timed AI illustrator fallback: Targeted feeling extraction failed ({exc}); skipping feeling annotations.",
                    file=sys.stderr,
                )
        merged_feelings, _ = legacy.merge_annotation_entries(feeling_entries, targeted_feelings)
        if merged_feelings:
            mention_data["feeling"] = merged_feelings

    legacy.suppress_default_country_mentions(mention_data, language)
    legacy.apply_language_location_overrides(mention_data, language)

    records: list[IllustrationRecord] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    for row_index, tag_values in tags.items():
        if row_index < 0 or row_index >= len(contexts):
            continue
        context = contexts[row_index]
        tag_patterns = [
            ("cta_comment", "commentez", legacy.CTA_PATTERNS.get("commentez", ())),
            ("cta_subscribe", "abonnez", legacy.CTA_PATTERNS.get("abonnez", ())),
            ("cta_tippee", "tippee", legacy.CTA_PATTERNS.get("tippee", ())),
        ]
        for illustration_type, slug, patterns in tag_patterns:
            if slug not in tag_values:
                continue
            phrase = _first_regex_match(patterns, context.text, context.reference_segment)
            localized = _localize_value(
                context,
                phrase or context.reference_segment,
                allow_row_fallback=True,
                transcript_fallback=phrase or context.text,
                reference_fallback=phrase or context.reference_segment,
            )
            record = _record_from_localized(
                localized,
                illustration_type,
                context=context,
                source_pass="ai",
                reference_word=phrase or context.reference_segment,
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1
    def emit_semantic_entry(row_index: int, value: str, illustration_type: str) -> None:
        if row_index < 0 or row_index >= len(contexts):
            skipped["no_span"] += 1
            return
        context = contexts[row_index]
        localized = _localize_value(context, value)
        record = _record_from_localized(
            localized,
            illustration_type,
            context=context,
            source_pass="ai",
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts[record.illustration_type] += 1

    category_type_map = {
        "person": "person",
        "gov_institution": "gov_institution",
        "brand": "brand",
        "book": "book",
        "keyword": "keyword",
        "feeling": "feeling",
        "money": "money",
        "date": "date",
    }

    for category, illustration_type in category_type_map.items():
        entries = mention_data.get(category)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            text_value = str(entry.get("text") or "").strip()
            start_index = entry.get("start_index") if isinstance(entry.get("start_index"), int) else None
            row_index = _map_offset_to_row_index(legacy, spans, rows_as_lists, text_value, start_index)
            if row_index is None:
                skipped["no_span"] += 1
                continue
            emit_semantic_entry(row_index, text_value, illustration_type)

    location_entries = mention_data.get("location")
    if isinstance(location_entries, list):
        for entry in location_entries:
            if not isinstance(entry, Mapping):
                continue
            text_value = str(entry.get("text") or "").strip()
            start_index = entry.get("start_index") if isinstance(entry.get("start_index"), int) else None
            row_index = _map_offset_to_row_index(legacy, spans, rows_as_lists, text_value, start_index)
            if row_index is None:
                skipped["no_span"] += 1
                continue
            _cities, countries = legacy.classify_locations(text_value)
            illustration_type = "country" if countries else "city"
            emit_semantic_entry(row_index, text_value, illustration_type)

    number_entries = mention_data.get("number")
    if isinstance(number_entries, list):
        for entry in number_entries:
            if not isinstance(entry, Mapping):
                continue
            text_value = str(entry.get("text") or "").strip()
            if not text_value:
                continue
            if legacy._is_money_mention(text_value):
                continue
            if any(pattern_factory(legacy).search(text_value) for pattern_factory in METRIC_PATTERNS.values()):
                continue
            if DURATION_PATTERN.search(text_value):
                continue
            if _distance_entries(legacy, text_value):
                continue
            start_index = entry.get("start_index") if isinstance(entry.get("start_index"), int) else None
            row_index = _map_offset_to_row_index(legacy, spans, rows_as_lists, text_value, start_index)
            if row_index is None:
                skipped["no_span"] += 1
                continue
            emit_semantic_entry(row_index, text_value, "number")

    for context in contexts:
        combined = " ".join(part for part in (context.reference_segment, context.text) if part)
        for metric_type, pattern_factory in METRIC_PATTERNS.items():
            values = legacy.detect_pattern_matches(pattern_factory(legacy), combined)
            for text_value in values:
                localized = _localize_value(context, text_value)
                record = _record_from_localized(
                    localized,
                    metric_type,
                    context=context,
                    source_pass="ai",
                )
                _push_record(records, record, seen, skipped)
                if record is not None:
                    counts[record.illustration_type] += 1

        for text_value in legacy.detect_pure_distances(combined):
            localized = _localize_value(context, text_value)
            record = _record_from_localized(
                localized,
                "distance",
                context=context,
                source_pass="ai",
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

        for entry in _regex_entries(DURATION_PATTERN, combined):
            text_value = str(entry.get("text") or "").strip()
            if not text_value:
                continue
            localized = _localize_value(context, text_value)
            record = _record_from_localized(
                localized,
                "duration",
                context=context,
                source_pass="ai",
            )
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts[record.illustration_type] += 1

    deduped_records, collapsed_duplicates = _dedupe_nearby_metric_records(records)
    if collapsed_duplicates:
        skipped["duplicate"] += collapsed_duplicates
        counts = Counter(record.illustration_type for record in deduped_records)
    return deduped_records, counts, skipped


def _sorted_rows(records: Sequence[IllustrationRecord]) -> list[dict[str, str]]:
    ordered = sorted(
        records,
        key=lambda record: (
            record.start_seconds,
            record.end_seconds,
            record.illustration_type,
            record.transcript_word.casefold(),
        ),
    )
    rows = [record.as_row() for record in ordered]
    previous_end = None
    for row, record in zip(rows, ordered):
        if previous_end is not None and previous_end > record.start_seconds:
            row["Crossing With Previous"] = "x"
        previous_end = record.end_seconds
    return rows


def _sorted_manifest_rows(records: Sequence[IllustrationRecord]) -> list[dict[str, str]]:
    staged_records = [
        record
        for record in sorted(
            records,
            key=lambda record: (
                record.start_seconds,
                record.end_seconds,
                record.illustration_type,
                record.transcript_word.casefold(),
                record.reference_word.casefold(),
            ),
        )
        if record.asset_category
    ]
    return [
        record.as_manifest_row(entry_id)
        for entry_id, record in enumerate(staged_records, start=1)
    ]


def _build_emoji_concept_map() -> dict[str, Path]:
    """Return {concept_label → emoji_mov_path} from emoji_output filenames.

    concept_label is human-readable with spaces, e.g. "dog face", "fire".
    """
    emoji_dir = Path("~/Desktop/code/deployable_auto-montage/animated_emoji/emoji_output_up").expanduser()
    if not emoji_dir.exists():
        return {}
    result: dict[str, Path] = {}
    for mov in sorted(emoji_dir.glob("*.mov")):
        stem = mov.stem  # e.g. "dog_face__1f436"
        concept_part = stem.split("__")[0] if "__" in stem else stem
        label = concept_part.replace("_", " ")
        if label:
            result[label] = mov
    return result


def _call_claude_for_emoji_matches(
    analysis_text: str,
    concept_labels: list[str],
    api_key: str,
    model: str,
) -> list[dict[str, object]]:
    """
    Ask Claude to identify which emoji concepts are explicitly present in
    *analysis_text*. Returns a list of dicts:
        [{"concept": "fire", "text": "the exact phrase in the text"}, ...]

    The prompt is deliberately restrictive: only return matches where the concept
    is explicitly and concretely named — no metaphors, no loose associations.
    """
    try:
        import anthropic as _anthropic  # type: ignore
    except ImportError:
        raise RuntimeError("Install the `anthropic` package to use the emoji pass.")

    concept_list = "\n".join(f"- {label}" for label in sorted(concept_labels))
    system_prompt = (
        "You are a strict content tagger for a video illustration pipeline. "
        "Your job is to identify which animated emoji concepts are EXPLICITLY mentioned "
        "in a reference text. The reference text may be in any language (French, Spanish, "
        "Arabic, etc.). The concept labels are in English — match them by semantic meaning "
        "regardless of the language of the text. Be highly conservative:\n"
        "- Only tag a concept if the text contains a word or phrase that directly and "
        "unambiguously refers to it.\n"
        "- Do NOT tag metaphors, implied meanings, or loose associations.\n"
        "- Do NOT tag a concept just because it is thematically related.\n"
        "- If in doubt, do not tag it.\n"
        "Return strict JSON only, no explanation."
    )
    user_prompt = (
        f"Available emoji concepts (labels are in English):\n{concept_list}\n\n"
        f"Reference text (may be in any language):\n{analysis_text}\n\n"
        "Return JSON in this exact shape:\n"
        '{"matches": [{"concept": "<exact concept label from the list>", '
        '"text": "<the exact word or short phrase found in the text>"}]}\n'
        "Return an empty matches list if nothing qualifies."
    )
    client = _anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=800,
        temperature=0,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = ""
    for block in getattr(message, "content", []):
        text = getattr(block, "text", None)
        if text:
            raw += text
    raw = raw.strip()
    # Extract JSON from the response
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not json_match:
        return []
    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return []
    items = data.get("matches")
    if not isinstance(items, list):
        return []
    results: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept") or "").strip()
        text_val = str(item.get("text") or "").strip()
        if concept and text_val:
            results.append({"concept": concept, "text": text_val})
    return results


def _build_emoji_records(
    contexts: Sequence[RowContext],
    analysis_text: str,
    spans: Sequence[tuple[int, int] | None],
    rows_as_lists: Sequence[list[str]],
) -> tuple[list[IllustrationRecord], Counter[str], Counter[str]]:
    """
    Use Claude API to identify animated emoji concept mentions in the reference
    text, then localize each match to word-level timing. Restrictive by design.
    """
    legacy = get_legacy_step2_module()
    records: list[IllustrationRecord] = []
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()

    concept_map = _build_emoji_concept_map()
    if not concept_map:
        print("WARNING: emoji_output dir empty or missing; skipping emoji pass.", file=sys.stderr)
        return records, counts, skipped

    try:
        api_key = legacy.resolve_api_key(None)
    except Exception as exc:
        print(f"WARNING: Emoji pass skipped — Anthropic API unavailable ({exc}).", file=sys.stderr)
        return records, counts, skipped

    try:
        matches = _call_claude_for_emoji_matches(
            analysis_text,
            list(concept_map.keys()),
            api_key,
            legacy.DEFAULT_NOUNS_MODEL,
        )
    except Exception as exc:
        print(f"WARNING: Emoji pass Claude call failed ({exc}); skipping.", file=sys.stderr)
        return records, counts, skipped

    seen: set[tuple[str, str, str, int, int]] = set()
    for match in matches:
        concept = str(match.get("concept") or "").strip()
        text_val = str(match.get("text") or "").strip()
        if not concept or not text_val or concept not in concept_map:
            skipped["unknown_concept"] += 1
            continue
        emoji_path = concept_map[concept]
        # Find which row context contains this text
        row_index = _map_offset_to_row_index(
            legacy,
            spans,
            rows_as_lists,
            text_val,
            analysis_text.find(text_val) if text_val in analysis_text else None,
            allow_neighbor=True,
        )
        if row_index is None or not (0 <= row_index < len(contexts)):
            skipped["no_span"] += 1
            continue
        context = contexts[row_index]
        localized = _localize_value(
            context,
            text_val,
            allow_row_fallback=True,
            transcript_fallback=text_val,
            reference_fallback=text_val,
        )
        record = _record_from_localized(
            localized,
            "animated_emoji",
            context=context,
            source_pass="emoji",
            transcript_word=text_val,
            reference_word=concept,
            asset_category="animated_emoji",
            link_url=str(emoji_path),
        )
        _push_record(records, record, seen, skipped)
        if record is not None:
            counts["animated_emoji"] += 1

    print(f"    Emoji pass: {counts['animated_emoji']} record(s), skipped={dict(skipped)}")
    return records, counts, skipped


FLAG_DEDUP_WINDOW_SECONDS = 5.0


def _build_flag_records(
    contexts: Sequence[RowContext],
) -> tuple[list[IllustrationRecord], Counter[str], Counter[str]]:
    """Scan each kept row for country name mentions and emit animated flag IllustrationRecords."""
    try:
        from emoji_flag_utils import build_flag_lookup, scan_text_for_flags
    except ImportError:
        print("WARNING: emoji_flag_utils not found; skipping animated flag pass.", file=sys.stderr)
        return [], Counter(), Counter()

    flag_lookup = build_flag_lookup()
    if not flag_lookup:
        print("WARNING: flag lookup is empty (check flags_output3 dir and iso-countries-languages); skipping.", file=sys.stderr)
        return [], Counter(), Counter()

    records: list[IllustrationRecord] = []
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    seen: set[tuple[str, str, str, int, int]] = set()
    last_flag_time: dict[str, float] = {}

    for context in contexts:
        combined = " ".join(part for part in (context.reference_segment, context.text) if part)
        matches = scan_text_for_flags(combined, flag_lookup)
        for matched_text, flag_path in matches:
            flag_key = str(flag_path)
            localized = _localize_value(
                context,
                matched_text,
                allow_row_fallback=True,
                transcript_fallback=matched_text,
                reference_fallback=matched_text,
            )
            record = _record_from_localized(
                localized,
                "animated_flag",
                context=context,
                source_pass="flag",
                transcript_word=matched_text,
                reference_word=matched_text,
                asset_category="animated_flag",
                link_url=str(flag_path),
            )
            if record is not None:
                last_time = last_flag_time.get(flag_key)
                if last_time is not None and (record.start_seconds - last_time) < FLAG_DEDUP_WINDOW_SECONDS:
                    skipped["nearby_flag_dup"] += 1
                    continue
                last_flag_time[flag_key] = record.start_seconds
            _push_record(records, record, seen, skipped)
            if record is not None:
                counts["animated_flag"] += 1

    print(f"    Flag pass: {counts['animated_flag']} record(s), skipped={dict(skipped)}")
    return records, counts, skipped


def run_pipeline(
    *,
    run_dir: Path | None = None,
    comparer_path: Path | None = None,
    edit_timeline_path: Path | None = None,
    html_path: Path | None = None,
    output_dir: Path | None = None,
    keep_mode: str = "auto",
) -> dict[str, Any]:
    resolved = resolve_inputs(
        run_dir=run_dir,
        comparer_path=comparer_path,
        edit_timeline_path=edit_timeline_path,
        html_path=html_path,
        keep_mode=keep_mode,
    )
    comparer_rows = _read_csv_rows(resolved.comparer_path)
    timeline_rows = _read_csv_rows(resolved.edit_timeline_path)
    contexts = _build_row_contexts(comparer_rows, timeline_rows, resolved.keep_mode)
    analysis_text, spans, rows_as_lists = _analysis_text_and_spans(contexts, resolved.html_path)

    format_records, format_counts, format_skipped = _build_format_records(
        contexts,
        resolved.html_path,
        analysis_text,
        spans,
        rows_as_lists,
    )
    ai_records, ai_counts, ai_skipped = _build_ai_records(
        contexts,
        resolved.html_path,
        analysis_text,
        spans,
        rows_as_lists,
    )
    ai_records, suppressed_ai_quote_overlaps = _suppress_ai_quote_overlaps(format_records, ai_records)
    if suppressed_ai_quote_overlaps:
        ai_skipped["quote_overlap"] += suppressed_ai_quote_overlaps
        ai_counts = Counter(record.illustration_type for record in ai_records)

    emoji_records, emoji_counts, emoji_skipped = _build_emoji_records(
        contexts, analysis_text, spans, rows_as_lists
    )
    flag_records, flag_counts, flag_skipped = _build_flag_records(contexts)

    root_output_dir = (output_dir or DEFAULT_OUTPUT_ROOT).resolve()
    run_output_dir = root_output_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_output_dir.mkdir(parents=True, exist_ok=True)

    format_rows = _sorted_rows(format_records)
    ai_rows = _sorted_rows(ai_records)

    format_output_path = run_output_dir / "01_format_illustrations.csv"
    ai_output_path = run_output_dir / "02_ai_illustrations.csv"
    manifest_csv_path = run_output_dir / "03_insert_timing_manifest.csv"
    manifest_json_path = run_output_dir / "03_insert_timing_manifest.json"
    summary_path = run_output_dir / "summary.json"

    write_dict_rows(format_output_path, format_rows, OUTPUT_HEADER)
    write_dict_rows(ai_output_path, ai_rows, OUTPUT_HEADER)
    manifest_rows = _sorted_manifest_rows([*format_records, *ai_records, *emoji_records, *flag_records])
    write_dict_rows(manifest_csv_path, manifest_rows, TIMING_MANIFEST_HEADER)
    manifest_json_path.write_text(json.dumps(manifest_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    timing_basis = Counter(record.timing_basis for record in (*format_records, *ai_records, *emoji_records, *flag_records))
    if timing_basis.get("exact") and timing_basis.get("inherited_interpolated"):
        edit_timing_mode = "mixed"
    elif timing_basis.get("exact"):
        edit_timing_mode = "exact"
    else:
        edit_timing_mode = "inherited_interpolated"

    summary = {
        "resolved_inputs": {
            "run_dir": str(resolved.run_dir),
            "comparer": str(resolved.comparer_path),
            "edit_timeline": str(resolved.edit_timeline_path),
            "html": str(resolved.html_path) if resolved.html_path else "",
            "summary": str(resolved.summary_path) if resolved.summary_path else "",
        },
        "keep_mode": resolved.keep_mode,
        "kept_rows": len(contexts),
        "counts": {
            "format": dict(sorted(format_counts.items())),
            "ai": dict(sorted(ai_counts.items())),
            "emoji": dict(sorted(emoji_counts.items())),
            "flag": dict(sorted(flag_counts.items())),
        },
        "skipped": {
            "format": dict(format_skipped),
            "ai": dict(ai_skipped),
            "emoji": dict(emoji_skipped),
            "flag": dict(flag_skipped),
        },
        "timing": {
            "basis_counts": dict(timing_basis),
            "edit_timing_mode": edit_timing_mode,
        },
        "outputs": {
            "run_output_dir": str(run_output_dir),
            "format_csv": str(format_output_path),
            "ai_csv": str(ai_output_path),
            "timing_manifest_csv": str(manifest_csv_path),
            "timing_manifest_json": str(manifest_json_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["outputs"]["summary_json"] = str(summary_path)
    return summary
