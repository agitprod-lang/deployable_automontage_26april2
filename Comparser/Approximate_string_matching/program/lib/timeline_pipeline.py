from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .alignment import collect_unmatched_ref_indices
from .constants import (
    EDIT_TIMELINE_HEADER,
    ENRICHED_DIAGNOSTIC_HEADER,
    ENRICHED_EXPLICIT_HEADER,
    ENRICHED_HEADER,
    ILLUSTRATION_CANDIDATE_HEADER,
    LEGACY_FEATURE_HEADER,
    LEGACY_HTML_METRIC_HEADER,
    LEGACY_TAG_HEADER,
    LEGACY_TITLE_NEWS_HEADER,
    PRECISE_ANNOTATION_HEADER,
    PRECISE_COMPARER_EXTRA_HEADER,
    PRECISE_COMPARER_HEADER,
    REF_LEFTOVER_HEADER,
    WORD_TIMELINE_HEADER,
)
from .csv_utils import (
    read_delimited_dicts,
    read_groq_words,
    read_working_rows,
    write_dict_rows,
)
from .html_utils import collect_reference_spans
from .model import TimedToken, WorkingRow, get_note_range, ref_span_range_from_notes
from .semantic_enrichment import dedupe_quote_annotation_rows
from .text_utils import tokenize
from .timecode_utils import format_timecode, parse_timecode


@dataclass
class TokenTiming:
    display: str
    normalized: str
    source_start: float | None
    source_end: float | None
    timing_source: str
    token_index: int


def _json_path_for(csv_path: Path) -> Path:
    return csv_path.with_suffix(".json")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _working_row_from_flat(data: Mapping[str, str]) -> WorkingRow:
    return WorkingRow(
        row_id=(data.get("Row ID") or data.get("Transcript #") or "").strip(),
        kind=(data.get("Kind") or "").strip(),
        start_time=(data.get("Start Time") or "").strip(),
        end_time=(data.get("End Time") or "").strip(),
        text=(data.get("Text") or "").strip(),
        eliminate=(data.get("Eliminate") or "").strip(),
        eliminate_reason=(data.get("Eliminate Reason") or "").strip(),
        repeat_group=(data.get("Repeat Group") or "").strip(),
        repeat_role=(data.get("Repeat Role") or "").strip(),
        reference_segment=(data.get("Reference Segment") or "").strip(),
        match_percent=(data.get("Match %") or "").strip(),
        status=(data.get("Status") or "").strip(),
        anchor_id=(data.get("Anchor ID") or "").strip(),
        notes=(data.get("Notes") or "").strip(),
    )


def read_stage_rows(path: Path) -> list[WorkingRow]:
    sample = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    if "Row ID;" in sample:
        return read_working_rows(path)
    return [_working_row_from_flat(row) for row in read_delimited_dicts(path)]


def _keep_value(row: WorkingRow) -> str:
    return "x" if not row.is_eliminated() and row.reference_segment.strip() else ""


def _split_normalized_tokens(value: str) -> list[str]:
    return [token for token in tokenize(value) if token]


def _build_tokens_from_word_slice(words: Sequence[TimedToken]) -> list[TokenTiming]:
    tokens: list[TokenTiming] = []
    token_index = 0
    for word in words:
        normalized_parts = _split_normalized_tokens(word.token)
        for part in normalized_parts:
            tokens.append(
                TokenTiming(
                    display=part,
                    normalized=part,
                    source_start=parse_timecode(word.start_time),
                    source_end=parse_timecode(word.end_time),
                    timing_source="groq_word",
                    token_index=token_index,
                )
            )
            token_index += 1
    return tokens


def _build_interpolated_tokens(row: WorkingRow) -> list[TokenTiming]:
    normalized_parts = _split_normalized_tokens(row.text)
    if not normalized_parts:
        return []
    start_seconds = parse_timecode(row.start_time)
    end_seconds = parse_timecode(row.end_time)
    if end_seconds < start_seconds:
        end_seconds = start_seconds
    duration = max(0.0, end_seconds - start_seconds)
    step = duration / max(len(normalized_parts), 1)
    tokens: list[TokenTiming] = []
    for index, part in enumerate(normalized_parts):
        token_start = start_seconds + (step * index)
        token_end = end_seconds if index == len(normalized_parts) - 1 else start_seconds + (step * (index + 1))
        tokens.append(
            TokenTiming(
                display=part,
                normalized=part,
                source_start=token_start,
                source_end=token_end,
                timing_source="interpolated_segment",
                token_index=index,
            )
        )
    return tokens


def _transcript_tokens_for_row(row: WorkingRow, words: Sequence[TimedToken] | None) -> list[TokenTiming]:
    if words:
        word_range = get_note_range(row.notes, "usable_word_range") or get_note_range(row.notes, "word_range")
        if word_range is not None:
            start_index, end_index = word_range
            if 0 <= start_index <= end_index < len(words):
                exact_tokens = _build_tokens_from_word_slice(words[start_index:end_index + 1])
                if exact_tokens:
                    return exact_tokens
    return _build_interpolated_tokens(row)


def _reference_tokens(text: str) -> list[TokenTiming]:
    return [
        TokenTiming(
            display=part,
            normalized=part,
            source_start=None,
            source_end=None,
            timing_source="reference_only",
            token_index=index,
        )
        for index, part in enumerate(_split_normalized_tokens(text))
    ]


def _match_score(left: str, right: str) -> tuple[float, float] | None:
    if left == right:
        return 2.5, 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    if ratio >= 0.84:
        return 1.5, ratio
    return None


def _align_tokens(transcript_tokens: Sequence[TokenTiming], ref_tokens: Sequence[TokenTiming]) -> list[dict[str, Any]]:
    transcript_count = len(transcript_tokens)
    ref_count = len(ref_tokens)
    dp = [[float("-inf")] * (ref_count + 1) for _ in range(transcript_count + 1)]
    action: list[list[tuple[str, float] | None]] = [[None] * (ref_count + 1) for _ in range(transcript_count + 1)]
    dp[0][0] = 0.0
    for i in range(transcript_count + 1):
        for j in range(ref_count + 1):
            current = dp[i][j]
            if current == float("-inf"):
                continue
            if i < transcript_count:
                score = current - 0.75
                if score > dp[i + 1][j]:
                    dp[i + 1][j] = score
                    action[i + 1][j] = ("skip_transcript", 0.0)
            if j < ref_count:
                score = current - 0.55
                if score > dp[i][j + 1]:
                    dp[i][j + 1] = score
                    action[i][j + 1] = ("skip_ref", 0.0)
            if i < transcript_count and j < ref_count:
                match = _match_score(transcript_tokens[i].normalized, ref_tokens[j].normalized)
                if match is not None:
                    match_score, confidence = match
                    score = current + match_score
                    if score > dp[i + 1][j + 1]:
                        dp[i + 1][j + 1] = score
                        action[i + 1][j + 1] = ("match", confidence)
            if (
                i + 1 < transcript_count
                and j + 1 < ref_count
                and transcript_tokens[i].normalized == ref_tokens[j + 1].normalized
                and transcript_tokens[i + 1].normalized == ref_tokens[j].normalized
            ):
                score = current + 3.0
                if score > dp[i + 2][j + 2]:
                    dp[i + 2][j + 2] = score
                    action[i + 2][j + 2] = ("swap", 0.95)

    entries: list[dict[str, Any]] = []
    i = transcript_count
    j = ref_count
    while i > 0 or j > 0:
        decision = action[i][j]
        if decision is None:
            if i > 0:
                decision = ("skip_transcript", 0.0)
                i -= 1
                transcript = transcript_tokens[i]
                entries.append(
                    {
                        "transcript_token": transcript.display,
                        "transcript_index": transcript.token_index,
                        "reference_token": "",
                        "reference_index": "",
                        "alignment_type": "TRANSCRIPT_ONLY",
                        "alignment_confidence": "0.00",
                        "timing_source": transcript.timing_source,
                        "source_start": transcript.source_start,
                        "source_end": transcript.source_end,
                    }
                )
                continue
            j -= 1
            ref = ref_tokens[j]
            entries.append(
                {
                    "transcript_token": "",
                    "transcript_index": "",
                    "reference_token": ref.display,
                    "reference_index": ref.token_index,
                    "alignment_type": "REF_ONLY",
                    "alignment_confidence": "0.00",
                    "timing_source": "",
                    "source_start": None,
                    "source_end": None,
                }
            )
            continue
        kind, confidence = decision
        if kind == "match":
            i -= 1
            j -= 1
            transcript = transcript_tokens[i]
            ref = ref_tokens[j]
            entries.append(
                {
                    "transcript_token": transcript.display,
                    "transcript_index": transcript.token_index,
                    "reference_token": ref.display,
                    "reference_index": ref.token_index,
                    "alignment_type": "MATCH" if confidence >= 0.99 else "APPROX_MATCH",
                    "alignment_confidence": f"{confidence:.2f}",
                    "timing_source": transcript.timing_source,
                    "source_start": transcript.source_start,
                    "source_end": transcript.source_end,
                }
            )
        elif kind == "swap":
            i -= 2
            j -= 2
            first_transcript = transcript_tokens[i]
            second_transcript = transcript_tokens[i + 1]
            first_ref = ref_tokens[j]
            second_ref = ref_tokens[j + 1]
            entries.append(
                {
                    "transcript_token": second_transcript.display,
                    "transcript_index": second_transcript.token_index,
                    "reference_token": first_ref.display,
                    "reference_index": first_ref.token_index,
                    "alignment_type": "TRANSPOSED",
                    "alignment_confidence": f"{confidence:.2f}",
                    "timing_source": second_transcript.timing_source,
                    "source_start": second_transcript.source_start,
                    "source_end": second_transcript.source_end,
                }
            )
            entries.append(
                {
                    "transcript_token": first_transcript.display,
                    "transcript_index": first_transcript.token_index,
                    "reference_token": second_ref.display,
                    "reference_index": second_ref.token_index,
                    "alignment_type": "TRANSPOSED",
                    "alignment_confidence": f"{confidence:.2f}",
                    "timing_source": first_transcript.timing_source,
                    "source_start": first_transcript.source_start,
                    "source_end": first_transcript.source_end,
                }
            )
        elif kind == "skip_transcript":
            i -= 1
            transcript = transcript_tokens[i]
            entries.append(
                {
                    "transcript_token": transcript.display,
                    "transcript_index": transcript.token_index,
                    "reference_token": "",
                    "reference_index": "",
                    "alignment_type": "TRANSCRIPT_ONLY",
                    "alignment_confidence": "0.00",
                    "timing_source": transcript.timing_source,
                    "source_start": transcript.source_start,
                    "source_end": transcript.source_end,
                }
            )
        else:
            j -= 1
            ref = ref_tokens[j]
            entries.append(
                {
                    "transcript_token": "",
                    "transcript_index": "",
                    "reference_token": ref.display,
                    "reference_index": ref.token_index,
                    "alignment_type": "REF_ONLY",
                    "alignment_confidence": "0.00",
                    "timing_source": "",
                    "source_start": None,
                    "source_end": None,
                }
            )
    entries.reverse()
    return entries


def _timeline_entry(
    row: WorkingRow,
    row_start_time: str,
    row_end_time: str,
    transcript_token_index: str,
    transcript_token: str,
    reference_token_index: str,
    reference_token: str,
    alignment_type: str,
    alignment_confidence: str,
    timing_source: str,
    source_start: float | None,
    source_end: float | None,
    notes: str = "",
) -> dict[str, str]:
    return {
        "Row ID": row.row_id,
        "Transcript #": row.row_id,
        "Keep": _keep_value(row),
        "Eliminate": row.eliminate,
        "Kind": row.kind,
        "Status": row.status,
        "Row Start Time": row_start_time,
        "Row End Time": row_end_time,
        "Transcript Token Index": transcript_token_index,
        "Transcript Token": transcript_token,
        "Reference Token Index": reference_token_index,
        "Reference Token": reference_token,
        "Alignment Type": alignment_type,
        "Alignment Confidence": alignment_confidence,
        "Timing Source": timing_source,
        "Source Start Time": format_timecode(source_start) if source_start is not None else "",
        "Source End Time": format_timecode(source_end) if source_end is not None else "",
        "Notes": notes,
    }


def _row_bounds_seconds(row: Mapping[str, str]) -> tuple[float, float]:
    start_seconds = parse_timecode(row.get("Row Start Time", ""))
    end_seconds = parse_timecode(row.get("Row End Time", ""))
    if end_seconds < start_seconds:
        end_seconds = start_seconds
    return start_seconds, end_seconds


def _entry_source_bounds_seconds(row: Mapping[str, str]) -> tuple[float | None, float | None]:
    start_raw = (row.get("Source Start Time") or "").strip()
    end_raw = (row.get("Source End Time") or "").strip()
    start = parse_timecode(start_raw) if start_raw else None
    end = parse_timecode(end_raw) if end_raw else None
    if start is not None and end is not None and end < start:
        end = start
    return start, end


def _group_by_row_id(rows: Sequence[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault((row.get("Row ID") or "").strip(), []).append(row)
    return grouped


def _fill_missing_source_times(group_rows: list[dict[str, str]]) -> None:
    if not group_rows:
        return
    row_start, row_end = _row_bounds_seconds(group_rows[0])
    known_ranges = [_entry_source_bounds_seconds(row) for row in group_rows]
    index = 0
    while index < len(group_rows):
        start, end = known_ranges[index]
        if start is not None and end is not None:
            index += 1
            continue
        block_start = index
        while index < len(group_rows):
            start, end = known_ranges[index]
            if start is not None and end is not None:
                break
            index += 1
        block_end = index
        left_bound = row_start
        if block_start > 0:
            previous_end = known_ranges[block_start - 1][1]
            if previous_end is not None:
                left_bound = previous_end
        right_bound = row_end
        if block_end < len(group_rows):
            next_start = known_ranges[block_end][0]
            if next_start is not None:
                right_bound = next_start
        segment_count = max(1, block_end - block_start)
        segment = max(0.0, right_bound - left_bound) / segment_count
        for offset, row_index in enumerate(range(block_start, block_end)):
            source_start = left_bound + (segment * offset)
            source_end = right_bound if row_index == block_end - 1 else left_bound + (segment * (offset + 1))
            group_rows[row_index]["Source Start Time"] = format_timecode(source_start)
            group_rows[row_index]["Source End Time"] = format_timecode(source_end)
            if not group_rows[row_index].get("Timing Source"):
                group_rows[row_index]["Timing Source"] = "interpolated_ref"
            notes = (group_rows[row_index].get("Notes") or "").strip()
            group_rows[row_index]["Notes"] = " | ".join(part for part in (notes, "source_time=interpolated") if part)
            known_ranges[row_index] = (source_start, source_end)


def _merge_intervals(intervals: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _collapse_seconds(value: float, removed_intervals: Sequence[tuple[float, float]]) -> float:
    shift = 0.0
    for start, end in removed_intervals:
        if value <= start:
            break
        shift += max(0.0, min(value, end) - start)
    return max(0.0, value - shift)


def _split_pipe_values(value: str) -> list[str]:
    return [fragment.strip() for fragment in (value or "").split("|") if fragment.strip()]


def _search_token_sequence(token_rows: Sequence[dict[str, str]], field: str, target_tokens: Sequence[str]) -> tuple[int, int] | None:
    if not target_tokens:
        return None
    candidates = [(index, (row.get(field) or "").strip().lower()) for index, row in enumerate(token_rows)]
    tokens_only = [token for _, token in candidates]
    for start in range(0, max(0, len(tokens_only) - len(target_tokens)) + 1):
        if tokens_only[start:start + len(target_tokens)] == list(target_tokens):
            return start, start + len(target_tokens) - 1
    return None


def _structured_annotation_columns() -> set[str]:
    return {
        "Money Mention",
        "Percent Mention",
        "Number Mention",
        "Date Mention",
    }


def _annotation_value_key(column: str, value: str) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    if column == "Money Mention":
        normalized = normalized.replace("€", " euros")
    normalized = re.sub(r"[^\w%]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _annotation_occurrence_priority(row: Mapping[str, str]) -> tuple[int, int, int, int, float]:
    locator = (row.get("Locator") or "").strip()
    timing_source = (row.get("Timing Source") or "").strip()
    confidence_raw = (row.get("Confidence") or row.get("Timing Confidence") or "").strip()
    try:
        confidence = float(confidence_raw)
    except ValueError:
        confidence = 0.0
    locator_score = 3 if locator == "transcript_span" else 2 if locator == "reference_span" else 1 if locator == "segment_interpolated" else 0
    timing_score = 3 if timing_source == "word_exact" else 2 if timing_source == "segment_interpolated" else 1 if timing_source != "row_fallback" else 0
    non_row_fallback = 1 if timing_source != "row_fallback" else 0
    start_seconds = parse_timecode((row.get("Start Time") or "").strip()) if (row.get("Start Time") or "").strip() else float("inf")
    return (locator_score, timing_score, non_row_fallback, int(round(confidence * 1000)), -start_seconds)


def _dedupe_structured_annotation_rows(
    annotation_rows: Sequence[dict[str, str]],
    *,
    row_window: int = 2,
    time_window_seconds: float = 6.0,
) -> list[dict[str, str]]:
    by_key: dict[tuple[str, str], list[tuple[int, dict[str, str]]]] = {}
    for index, row in enumerate(annotation_rows):
        column = (row.get("Annotation Column") or "").strip()
        if column not in _structured_annotation_columns():
            continue
        value_key = _annotation_value_key(column, row.get("Annotation Value") or "")
        if not value_key:
            continue
        by_key.setdefault((column, value_key), []).append((index, row))

    drop_indices: set[int] = set()
    for _group_key, occurrences in by_key.items():
        ordered = sorted(
            occurrences,
            key=lambda item: (
                int((item[1].get("Transcript #") or "0").strip() or "0"),
                parse_timecode((item[1].get("Start Time") or "").strip()) if (item[1].get("Start Time") or "").strip() else float("inf"),
                item[0],
            ),
        )
        cluster: list[tuple[int, dict[str, str]]] = []

        def flush_cluster() -> None:
            if len(cluster) <= 1:
                cluster.clear()
                return
            winner = max(cluster, key=lambda item: _annotation_occurrence_priority(item[1]))
            for index, _row in cluster:
                if index != winner[0]:
                    drop_indices.add(index)
            cluster.clear()

        for index, row in ordered:
            transcript_number = int((row.get("Transcript #") or "0").strip() or "0")
            start_seconds = parse_timecode((row.get("Start Time") or "").strip()) if (row.get("Start Time") or "").strip() else None
            if not cluster:
                cluster.append((index, row))
                continue
            previous_index, previous_row = cluster[-1]
            previous_number = int((previous_row.get("Transcript #") or "0").strip() or "0")
            previous_start = parse_timecode((previous_row.get("Start Time") or "").strip()) if (previous_row.get("Start Time") or "").strip() else None
            nearby = (transcript_number - previous_number) <= row_window
            if not nearby and start_seconds is not None and previous_start is not None:
                nearby = abs(start_seconds - previous_start) <= time_window_seconds
            if not nearby:
                flush_cluster()
            cluster.append((index, row))
        flush_cluster()

    return [row for index, row in enumerate(annotation_rows) if index not in drop_indices]


def _supported_reference_window(row_timeline: Sequence[dict[str, str]]) -> tuple[int, int] | None:
    supported_indices: list[int] = []
    for entry in row_timeline:
        alignment_type = (entry.get("Alignment Type") or "").strip()
        if alignment_type not in {"MATCH", "APPROX_MATCH", "TRANSPOSED"}:
            continue
        transcript_token = (entry.get("Transcript Token") or "").strip()
        reference_token = (entry.get("Reference Token") or "").strip()
        reference_index_raw = (entry.get("Reference Token Index") or "").strip()
        if not transcript_token or not reference_token or not reference_index_raw:
            continue
        try:
            supported_indices.append(int(reference_index_raw))
        except ValueError:
            continue
    if not supported_indices:
        return None
    return min(supported_indices), max(supported_indices)


def _usable_reference_token_rows(row_timeline: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    row_ref_tokens = [entry for entry in row_timeline if (entry.get("Reference Token") or "").strip()]
    window = _supported_reference_window(row_timeline)
    if window is None:
        return []
    start_index, end_index = window
    usable_rows: list[dict[str, str]] = []
    for entry in row_ref_tokens:
        reference_index_raw = (entry.get("Reference Token Index") or "").strip()
        if not reference_index_raw:
            continue
        try:
            reference_index = int(reference_index_raw)
        except ValueError:
            continue
        if start_index <= reference_index <= end_index:
            usable_rows.append(entry)
    return usable_rows


def _row_time_bounds(row_entries: Sequence[dict[str, str]]) -> tuple[str, str, str, str]:
    source_values = []
    edit_values = []
    for entry in row_entries:
        source_start = (entry.get("Source Start Time") or "").strip()
        source_end = (entry.get("Source End Time") or "").strip()
        edit_start = (entry.get("Edit Start Time") or "").strip()
        edit_end = (entry.get("Edit End Time") or "").strip()
        if source_start:
            source_values.append(parse_timecode(source_start))
        if source_end:
            source_values.append(parse_timecode(source_end))
        if edit_start:
            edit_values.append(parse_timecode(edit_start))
        if edit_end:
            edit_values.append(parse_timecode(edit_end))
    source_start_time = format_timecode(min(source_values)) if source_values else ""
    source_end_time = format_timecode(max(source_values)) if source_values else ""
    edit_start_time = format_timecode(min(edit_values)) if edit_values else ""
    edit_end_time = format_timecode(max(edit_values)) if edit_values else ""
    return source_start_time, source_end_time, edit_start_time, edit_end_time


def _annotation_columns() -> list[str]:
    return (
        LEGACY_TAG_HEADER
        + LEGACY_FEATURE_HEADER
        + LEGACY_TITLE_NEWS_HEADER
        + LEGACY_HTML_METRIC_HEADER
        + ENRICHED_EXPLICIT_HEADER
    )


ILLUSTRATION_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "Titles": ("titles", "ransom_gifs"),
    "Quote Extracted": ("quotes", "quote_highlights"),
    "Person Mention": ("nouns",),
    "Gov Institution": ("institution_images", "institution_transitions"),
    "Location Mention": ("locations_3d",),
    "City Mention": ("city_country",),
    "Country Mention": ("city_country",),
    "Money Mention": ("money",),
    "Percent Mention": ("percent",),
    "Number Mention": ("numbers", "calendar"),
    "Date Mention": ("numbers", "calendar"),
    "Ranking Mention": ("social_ranking_punctuation",),
    "Social Network Mention": ("social_ranking_punctuation",),
    "Punctuation Signal": ("social_ranking_punctuation",),
    "Bold Text": ("social_ranking_punctuation",),
    "Italic Text": ("social_ranking_punctuation",),
    "Underlined Text": ("social_ranking_punctuation",),
    "List Block": ("social_ranking_punctuation",),
}


def _format_human_timestamp(raw_timecode: str) -> str:
    raw = (raw_timecode or "").strip()
    if not raw:
        return ""
    seconds = parse_timecode(raw)
    total_millis = max(0, int(round(seconds * 1000)))
    hours = total_millis // 3_600_000
    minutes = (total_millis % 3_600_000) // 60_000
    secs = (total_millis % 60_000) // 1000
    millis = total_millis % 1000
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def _timing_source_label(raw_sources: Sequence[str], locator: str) -> str:
    cleaned = [(value or "").strip().lower() for value in raw_sources if (value or "").strip()]
    if locator == "row_fallback":
        return "row_fallback"
    if cleaned and all(value == "groq_word" for value in cleaned):
        return "word_exact"
    if cleaned and all(value in {"interpolated_segment", "interpolated_ref"} for value in cleaned):
        return "segment_interpolated"
    if cleaned and "groq_word" in cleaned:
        return "word_exact"
    if cleaned:
        return "segment_interpolated"
    return "row_fallback"


def _annotation_to_candidate_rows(annotation_row: Mapping[str, str]) -> list[dict[str, str]]:
    categories = ILLUSTRATION_CATEGORY_MAP.get((annotation_row.get("Annotation Column") or "").strip(), ())
    candidates: list[dict[str, str]] = []
    for asset_category in categories:
        candidates.append(
            {
                "Asset Category": asset_category,
                "Annotation Column": (annotation_row.get("Annotation Column") or "").strip(),
                "Illustration Value": (annotation_row.get("Annotation Value") or "").strip(),
                "Transcript #": (annotation_row.get("Transcript #") or "").strip(),
                "Row ID": (annotation_row.get("Row ID") or "").strip(),
                "Keep": (annotation_row.get("Keep") or "").strip(),
                "Status": (annotation_row.get("Status") or "").strip(),
                "Locator": (annotation_row.get("Locator") or "").strip(),
                "Timing Source": (annotation_row.get("Timing Source") or "").strip(),
                "Timing Confidence": (annotation_row.get("Timing Confidence") or "").strip(),
                "Edit Timestamp": (annotation_row.get("Edit Timestamp") or "").strip(),
                "Start Time": (annotation_row.get("Start Time") or "").strip(),
                "End Time": (annotation_row.get("End Time") or "").strip(),
                "Source Timestamp": (annotation_row.get("Source Timestamp") or "").strip(),
                "Source Start Time": (annotation_row.get("Source Start Time") or "").strip(),
                "Source End Time": (annotation_row.get("Source End Time") or "").strip(),
                "Text": (annotation_row.get("Text") or "").strip(),
                "Reference Segment": (annotation_row.get("Reference Segment") or "").strip(),
            }
        )
    return candidates


def _annotation_row(
    row: Mapping[str, str],
    column: str,
    value: str,
    start_time: str,
    end_time: str,
    source_start_time: str,
    source_end_time: str,
    locator: str,
    confidence: str,
    timing_source: str,
) -> dict[str, str]:
    return {
        "Transcript #": (row.get("Transcript #") or "").strip(),
        "Row ID": (row.get("Transcript #") or row.get("Row ID") or "").strip(),
        "Keep": (row.get("Keep") or "").strip(),
        "Status": (row.get("Status") or "").strip(),
        "Annotation Column": column,
        "Annotation Value": value,
        "Locator": locator,
        "Confidence": confidence,
        "Timing Source": timing_source,
        "Timing Confidence": confidence,
        "Edit Timestamp": _format_human_timestamp(start_time),
        "Start Time": start_time,
        "End Time": end_time,
        "Source Timestamp": _format_human_timestamp(source_start_time),
        "Source Start Time": source_start_time,
        "Source End Time": source_end_time,
        "Text": (row.get("Text") or "").strip(),
        "Reference Segment": (row.get("Reference Segment") or "").strip(),
    }


def run_stage_10(input_path: Path, html_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_stage_rows(input_path)
    ref_spans, _reference_text = collect_reference_spans(html_path)
    unmatched_indices = collect_unmatched_ref_indices(rows, ref_spans)
    matched_rows = [
        (row, ref_span_range_from_notes(row))
        for row in rows
        if ref_span_range_from_notes(row) is not None
    ]
    leftover_rows: list[dict[str, str]] = []
    for index in unmatched_indices:
        span = ref_spans[index]
        previous_row = None
        next_row = None
        for row, span_range in matched_rows:
            assert span_range is not None
            if span_range[1] < index:
                previous_row = row
            elif span_range[0] > index and next_row is None:
                next_row = row
                break
        leftover_rows.append(
            {
                "Ref Span #": str(span.index),
                "Text": span.text,
                "Start Offset": str(span.start_offset),
                "End Offset": str(span.end_offset),
                "Token Count": str(len(_split_normalized_tokens(span.text))),
                "Previous Transcript #": previous_row.row_id if previous_row else "",
                "Previous Text": previous_row.text if previous_row else "",
                "Next Transcript #": next_row.row_id if next_row else "",
                "Next Text": next_row.text if next_row else "",
                "Status": "UNMATCHED_REF",
            }
        )
    write_dict_rows(output_path, leftover_rows, REF_LEFTOVER_HEADER)
    _write_json(
        _json_path_for(output_path),
        {
            "rows": leftover_rows,
            "summary": {
                "leftover_ref_spans": len(leftover_rows),
            },
        },
    )
    return {"leftover_ref_spans": len(leftover_rows)}


def run_stage_11(
    input_path: Path,
    output_path: Path,
    words_path: Path | None = None,
) -> dict[str, Any]:
    rows = read_stage_rows(input_path)
    words = read_groq_words(words_path) if words_path and words_path.exists() else []
    entries: list[dict[str, str]] = []
    exact_entries = 0
    interpolated_entries = 0

    for row in rows:
        if row.is_silence():
            entries.append(
                _timeline_entry(
                    row=row,
                    row_start_time=row.start_time,
                    row_end_time=row.end_time,
                    transcript_token_index="",
                    transcript_token="",
                    reference_token_index="",
                    reference_token="",
                    alignment_type="SILENCE",
                    alignment_confidence="1.00",
                    timing_source="silence_row",
                    source_start=parse_timecode(row.start_time),
                    source_end=parse_timecode(row.end_time),
                    notes="",
                )
            )
            continue

        transcript_tokens = _transcript_tokens_for_row(row, words)
        if any(token.timing_source == "groq_word" for token in transcript_tokens):
            exact_entries += len(transcript_tokens)
        else:
            interpolated_entries += len(transcript_tokens)

        if row.reference_segment and not row.is_eliminated():
            ref_tokens = _reference_tokens(row.reference_segment)
            aligned = _align_tokens(transcript_tokens, ref_tokens)
            for aligned_entry in aligned:
                entries.append(
                    _timeline_entry(
                        row=row,
                        row_start_time=row.start_time,
                        row_end_time=row.end_time,
                        transcript_token_index=str(aligned_entry["transcript_index"]) if aligned_entry["transcript_index"] != "" else "",
                        transcript_token=aligned_entry["transcript_token"],
                        reference_token_index=str(aligned_entry["reference_index"]) if aligned_entry["reference_index"] != "" else "",
                        reference_token=aligned_entry["reference_token"],
                        alignment_type=aligned_entry["alignment_type"],
                        alignment_confidence=aligned_entry["alignment_confidence"],
                        timing_source=aligned_entry["timing_source"],
                        source_start=aligned_entry["source_start"],
                        source_end=aligned_entry["source_end"],
                        notes="",
                    )
                )
            continue

        if transcript_tokens:
            alignment_type = "ELIMINATED" if row.is_eliminated() else "UNMATCHED"
            for token in transcript_tokens:
                entries.append(
                    _timeline_entry(
                        row=row,
                        row_start_time=row.start_time,
                        row_end_time=row.end_time,
                        transcript_token_index=str(token.token_index),
                        transcript_token=token.display,
                        reference_token_index="",
                        reference_token="",
                        alignment_type=alignment_type,
                        alignment_confidence="0.00",
                        timing_source=token.timing_source,
                        source_start=token.source_start,
                        source_end=token.source_end,
                        notes="",
                    )
                )
        else:
            entries.append(
                _timeline_entry(
                    row=row,
                    row_start_time=row.start_time,
                    row_end_time=row.end_time,
                    transcript_token_index="",
                    transcript_token="",
                    reference_token_index="",
                    reference_token="",
                    alignment_type="EMPTY_ROW",
                    alignment_confidence="0.00",
                    timing_source="interpolated_segment",
                    source_start=parse_timecode(row.start_time),
                    source_end=parse_timecode(row.end_time),
                    notes="",
                )
            )

    write_dict_rows(output_path, entries, WORD_TIMELINE_HEADER)
    _write_json(
        _json_path_for(output_path),
        {
            "rows": entries,
            "summary": {
                "rows": len(entries),
                "speech_rows": sum(1 for row in rows if row.is_speech()),
                "silence_rows": sum(1 for row in rows if row.is_silence()),
                "exact_timing_entries": exact_entries,
                "interpolated_entries": interpolated_entries,
            },
        },
    )
    return {
        "timeline_rows": len(entries),
        "exact_timing_entries": exact_entries,
        "interpolated_entries": interpolated_entries,
    }


def run_stage_12(input_path: Path, output_path: Path) -> dict[str, Any]:
    stage11_rows = read_delimited_dicts(input_path)
    grouped_rows = _group_by_row_id(stage11_rows)
    row_intervals: dict[str, tuple[float, float, bool]] = {}
    for row_id, group in grouped_rows.items():
        if not group:
            continue
        row_start, row_end = _row_bounds_seconds(group[0])
        keep_value = (group[0].get("Keep") or "").strip().lower()
        remove = (
            (group[0].get("Kind") or "").strip().lower() == "silence"
            or (group[0].get("Eliminate") or "").strip().lower() == "x"
            or keep_value != "x"
        )
        row_intervals[row_id] = (row_start, row_end, remove)
    removed_intervals = _merge_intervals([(start, end) for start, end, remove in row_intervals.values() if remove])

    kept_rows: list[dict[str, str]] = []
    for row_id, group in grouped_rows.items():
        _fill_missing_source_times(group)
        _start, _end, remove = row_intervals[row_id]
        if remove:
            continue
        for row in group:
            source_start, source_end = _entry_source_bounds_seconds(row)
            edit_start = _collapse_seconds(source_start, removed_intervals) if source_start is not None else None
            edit_end = _collapse_seconds(source_end, removed_intervals) if source_end is not None else None
            enriched = {column: (row.get(column) or "") for column in EDIT_TIMELINE_HEADER}
            enriched["Edit Start Time"] = format_timecode(edit_start) if edit_start is not None else ""
            enriched["Edit End Time"] = format_timecode(edit_end) if edit_end is not None else ""
            kept_rows.append(enriched)

    write_dict_rows(output_path, kept_rows, EDIT_TIMELINE_HEADER)
    _write_json(
        _json_path_for(output_path),
        {
            "rows": kept_rows,
            "summary": {
                "rows": len(kept_rows),
                "removed_intervals": [
                    {
                        "source_start_time": format_timecode(start),
                        "source_end_time": format_timecode(end),
                    }
                    for start, end in removed_intervals
                ],
            },
        },
    )
    return {
        "timeline_rows": len(kept_rows),
        "removed_intervals": len(removed_intervals),
    }


def run_stage_13(
    enriched_input_path: Path,
    timeline_input_path: Path,
    output_path: Path,
    annotations_output_path: Path,
    illustration_candidates_output_path: Path | None = None,
) -> dict[str, Any]:
    if illustration_candidates_output_path is None:
        illustration_candidates_output_path = annotations_output_path.with_name("13_illustration_candidates.csv")
    enriched_rows = read_delimited_dicts(enriched_input_path)
    timeline_rows = read_delimited_dicts(timeline_input_path)
    timeline_by_transcript: dict[str, list[dict[str, str]]] = {}
    for row in timeline_rows:
        timeline_by_transcript.setdefault((row.get("Transcript #") or "").strip(), []).append(row)

    annotation_rows: list[dict[str, str]] = []
    illustration_candidate_rows: list[dict[str, str]] = []
    precise_comparer_rows: list[dict[str, str]] = []

    for row in enriched_rows:
        transcript_number = (row.get("Transcript #") or "").strip()
        row_timeline = timeline_by_transcript.get(transcript_number, [])
        source_start_time, source_end_time, edit_start_time, edit_end_time = _row_time_bounds(row_timeline)

        precise_row = {column: (row.get(column) or "") for column in PRECISE_COMPARER_HEADER}
        precise_row["Start Time"] = edit_start_time if (row.get("Keep") or "").strip().lower() == "x" else ""
        precise_row["End Time"] = edit_end_time if (row.get("Keep") or "").strip().lower() == "x" else ""
        precise_row["Source Start Time"] = source_start_time or (row.get("Start Time") or "")
        precise_row["Source End Time"] = source_end_time or (row.get("End Time") or "")

        if (row.get("Keep") or "").strip().lower() != "x":
            for column in _annotation_columns():
                precise_row[column] = ""
            precise_comparer_rows.append(precise_row)
            continue

        usable_ref_tokens = _usable_reference_token_rows(row_timeline)
        row_transcript_tokens = [entry for entry in row_timeline if (entry.get("Transcript Token") or "").strip()]
        retained_values_by_column: dict[str, list[str]] = {}
        for column in _annotation_columns():
            cell_value = (row.get(column) or "").strip()
            if not cell_value:
                continue
            for value in _split_pipe_values(cell_value):
                target_tokens = [token.lower() for token in _split_normalized_tokens(value)]
                locator = "row_fallback"
                confidence = "0.40"
                timing_source = "row_fallback"
                start_time = edit_start_time
                end_time = edit_end_time
                source_start = source_start_time
                source_end = source_end_time
                match = _search_token_sequence(usable_ref_tokens, "Reference Token", target_tokens)
                if match is not None:
                    first, last = match
                    locator = "reference_span"
                    confidence = "0.95"
                    matched_rows = usable_ref_tokens[first:last + 1]
                    timing_source = _timing_source_label(
                        [(matched_row.get("Timing Source") or "") for matched_row in matched_rows],
                        locator,
                    )
                    start_time = matched_rows[0].get("Edit Start Time", "")
                    end_time = matched_rows[-1].get("Edit End Time", "")
                    source_start = matched_rows[0].get("Source Start Time", "")
                    source_end = matched_rows[-1].get("Source End Time", "")
                else:
                    match = _search_token_sequence(row_transcript_tokens, "Transcript Token", target_tokens)
                    if match is not None:
                        first, last = match
                        locator = "transcript_span"
                        confidence = "0.85"
                        matched_rows = row_transcript_tokens[first:last + 1]
                        timing_source = _timing_source_label(
                            [(matched_row.get("Timing Source") or "") for matched_row in matched_rows],
                            locator,
                        )
                        start_time = matched_rows[0].get("Edit Start Time", "")
                        end_time = matched_rows[-1].get("Edit End Time", "")
                        source_start = matched_rows[0].get("Source Start Time", "")
                        source_end = matched_rows[-1].get("Source End Time", "")
                    elif column in _structured_annotation_columns():
                        continue
                annotation_row = _annotation_row(
                    row=row,
                    column=column,
                    value=value,
                    start_time=start_time,
                    end_time=end_time,
                    source_start_time=source_start,
                    source_end_time=source_end,
                    locator=locator,
                    confidence=confidence,
                    timing_source=timing_source,
                )
                annotation_rows.append(annotation_row)
                retained_values_by_column.setdefault(column, []).append(value)
        for column in _structured_annotation_columns():
            if column in precise_row:
                precise_row[column] = " | ".join(retained_values_by_column.get(column, []))
        precise_comparer_rows.append(precise_row)

    annotation_rows = dedupe_quote_annotation_rows(annotation_rows)
    annotation_rows = _dedupe_structured_annotation_rows(annotation_rows)
    for annotation_row in annotation_rows:
        illustration_candidate_rows.extend(_annotation_to_candidate_rows(annotation_row))

    write_dict_rows(output_path, precise_comparer_rows, PRECISE_COMPARER_HEADER)
    write_dict_rows(annotations_output_path, annotation_rows, PRECISE_ANNOTATION_HEADER)
    write_dict_rows(illustration_candidates_output_path, illustration_candidate_rows, ILLUSTRATION_CANDIDATE_HEADER)
    _write_json(
        _json_path_for(illustration_candidates_output_path),
        {
            "rows": illustration_candidate_rows,
            "summary": {
                "illustration_candidates": len(illustration_candidate_rows),
            },
        },
    )
    return {
        "precise_comparer_rows": len(precise_comparer_rows),
        "precise_annotations": len(annotation_rows),
        "illustration_candidates": len(illustration_candidate_rows),
    }
