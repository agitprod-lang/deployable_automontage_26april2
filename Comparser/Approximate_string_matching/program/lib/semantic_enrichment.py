from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
import sys
from typing import Any, Mapping, MutableMapping, Sequence

from .constants import ENRICHED_HEADER
from .enriched_csv import merge_pipe_values, read_table, write_enriched_rows
from .legacy_step2 import get_legacy_step2_module
from .reference_features import extract_classified_links, extract_structured_titles


def _warn_semantic_fallback(message: str) -> None:
    print(f"WARNING: semantic enrichment fallback: {message}", file=sys.stderr)


def _header_index(header: Sequence[str], column: str) -> int | None:
    lowered = column.strip().lower()
    for index, value in enumerate(header):
        if value.strip().lower() == lowered:
            return index
    return None


def _capture_existing_columns(
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    columns: Sequence[str],
) -> dict[str, list[str]]:
    captured: dict[str, list[str]] = {}
    for column in columns:
        idx = _header_index(header, column)
        if idx is None:
            captured[column] = ["" for _ in rows]
            continue
        captured[column] = [row[idx] if idx < len(row) else "" for row in rows]
    return captured


def _merge_existing_columns(
    header: Sequence[str],
    rows: list[list[str]],
    existing: Mapping[str, Sequence[str]],
    columns: Sequence[str],
) -> None:
    for column in columns:
        idx = _header_index(header, column)
        if idx is None:
            continue
        previous_values = existing.get(column, [])
        for row_index, row in enumerate(rows):
            previous = previous_values[row_index] if row_index < len(previous_values) else ""
            row[idx] = merge_pipe_values(previous, row[idx] if idx < len(row) else "")


def _normalize_quote_dedupe_key(value: str) -> str:
    normalized = re.sub(r"[^\w]+", " ", (value or "").strip().lower(), flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _quote_token_set(value: str) -> set[str]:
    return {token for token in _normalize_quote_dedupe_key(value).split() if token}


def _quote_keys_are_similar(left: str, right: str, *, min_overlap_ratio: float = 0.55, min_sequence_ratio: float = 0.7) -> bool:
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True
    left_tokens = _quote_token_set(left)
    right_tokens = _quote_token_set(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    min_size = min(len(left_tokens), len(right_tokens))
    if min_size > 0 and (overlap / float(min_size)) >= min_overlap_ratio:
        return True
    return SequenceMatcher(None, left, right).ratio() >= min_sequence_ratio


def _parse_timecode_seconds(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    try:
        if len(parts) == 4:
            hours, minutes, seconds, frames = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds + (frames / 25.0)
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


@dataclass
class _QuoteOccurrence:
    row_index: int
    fragment_index: int
    fragment: str
    key: str
    start_seconds: float | None
    end_seconds: float | None
    locator: str
    timing_source: str
    transcript_text: str
    reference_segment: str


def _quote_support_text(transcript_text: str, reference_segment: str, fragment: str) -> str:
    normalized_fragment = _normalize_quote_dedupe_key(fragment)
    candidates = [reference_segment or "", transcript_text or ""]
    for candidate in candidates:
        if normalized_fragment and normalized_fragment in _normalize_quote_dedupe_key(candidate):
            return candidate
    return reference_segment or transcript_text or fragment


def _quote_occurrence_priority(occurrence: _QuoteOccurrence) -> tuple[int, int, int, float, float]:
    locator_score = 2 if occurrence.locator == "reference_span" else 1 if occurrence.locator == "transcript_span" else 0
    timing_score = 2 if occurrence.timing_source == "word_exact" else 1 if occurrence.timing_source != "row_fallback" else 0
    non_row_fallback = 1 if occurrence.timing_source != "row_fallback" else 0
    support_text = _quote_support_text(occurrence.transcript_text, occurrence.reference_segment, occurrence.fragment)
    support_span = len(_normalize_quote_dedupe_key(support_text)) or len(_normalize_quote_dedupe_key(occurrence.fragment)) or 10_000
    start_seconds = occurrence.start_seconds if occurrence.start_seconds is not None else float("inf")
    return (
        locator_score,
        timing_score,
        non_row_fallback,
        -float(support_span),
        -float(start_seconds),
    )


def _quote_occurrences_are_nearby(
    previous: _QuoteOccurrence,
    current: _QuoteOccurrence,
    *,
    row_window: int,
    time_window_seconds: float,
) -> bool:
    if (current.row_index - previous.row_index) <= row_window:
        return True
    if (
        current.start_seconds is not None
        and previous.start_seconds is not None
        and abs(current.start_seconds - previous.start_seconds) <= time_window_seconds
    ):
        return True
    return False


def _dedupe_quote_occurrences(
    occurrences: Sequence[_QuoteOccurrence],
    *,
    row_window: int = 2,
    time_window_seconds: float = 6.0,
) -> set[tuple[int, int]]:
    grouped: list[list[_QuoteOccurrence]] = []
    for occurrence in occurrences:
        matched_group: list[_QuoteOccurrence] | None = None
        for group in grouped:
            representative = group[0]
            if _quote_keys_are_similar(occurrence.key, representative.key):
                matched_group = group
                break
        if matched_group is None:
            matched_group = []
            grouped.append(matched_group)
        matched_group.append(occurrence)

    drop_keys: set[tuple[int, int]] = set()
    for key_occurrences in grouped:
        ordered = sorted(key_occurrences, key=lambda item: (item.row_index, item.fragment_index))
        if not ordered:
            continue
        cluster: list[_QuoteOccurrence] = [ordered[0]]
        for occurrence in ordered[1:]:
            if _quote_occurrences_are_nearby(cluster[-1], occurrence, row_window=row_window, time_window_seconds=time_window_seconds):
                cluster.append(occurrence)
                continue
            best = max(cluster, key=_quote_occurrence_priority)
            for candidate in cluster:
                if candidate is not best:
                    drop_keys.add((candidate.row_index, candidate.fragment_index))
            cluster = [occurrence]
        best = max(cluster, key=_quote_occurrence_priority)
        for candidate in cluster:
            if candidate is not best:
                drop_keys.add((candidate.row_index, candidate.fragment_index))
    return drop_keys


def _dedupe_nearby_quote_rows(
    header: Sequence[str],
    rows: list[list[str]],
    *,
    row_window: int = 2,
    time_window_seconds: float = 6.0,
) -> None:
    quote_idx = _header_index(header, "Quote Extracted")
    if quote_idx is None:
        return
    start_idx = _header_index(header, "Start Time")
    end_idx = _header_index(header, "End Time")
    locator_idx = _header_index(header, "Locator")
    timing_source_idx = _header_index(header, "Timing Source")
    text_idx = _header_index(header, "Text")
    reference_idx = _header_index(header, "Reference Segment")
    occurrences: list[_QuoteOccurrence] = []
    for row_index, row in enumerate(rows):
        cell_value = row[quote_idx] if quote_idx < len(row) else ""
        if not cell_value:
            continue
        fragments = [fragment.strip() for fragment in cell_value.split("|") if fragment.strip()]
        row_start = _parse_timecode_seconds(row[start_idx] if start_idx is not None and start_idx < len(row) else "")
        row_end = _parse_timecode_seconds(row[end_idx] if end_idx is not None and end_idx < len(row) else "")
        locator = row[locator_idx].strip() if locator_idx is not None and locator_idx < len(row) else ""
        timing_source = row[timing_source_idx].strip() if timing_source_idx is not None and timing_source_idx < len(row) else ""
        transcript_text = row[text_idx].strip() if text_idx is not None and text_idx < len(row) else ""
        reference_segment = row[reference_idx].strip() if reference_idx is not None and reference_idx < len(row) else ""
        for fragment_index, fragment in enumerate(fragments):
            key = _normalize_quote_dedupe_key(fragment)
            if not key:
                continue
            occurrences.append(
                _QuoteOccurrence(
                    row_index=row_index,
                    fragment_index=fragment_index,
                    fragment=fragment,
                    key=key,
                    start_seconds=row_start,
                    end_seconds=row_end,
                    locator=locator,
                    timing_source=timing_source,
                    transcript_text=transcript_text,
                    reference_segment=reference_segment,
                )
            )
    drop_keys = _dedupe_quote_occurrences(
        occurrences,
        row_window=row_window,
        time_window_seconds=time_window_seconds,
    )
    if not drop_keys:
        return
    for row_index, row in enumerate(rows):
        cell_value = row[quote_idx] if quote_idx < len(row) else ""
        if not cell_value:
            continue
        fragments = [fragment.strip() for fragment in cell_value.split("|") if fragment.strip()]
        row[quote_idx] = " | ".join(
            fragment
            for fragment_index, fragment in enumerate(fragments)
            if (row_index, fragment_index) not in drop_keys
        )


def dedupe_quote_annotation_rows(
    annotation_rows: Sequence[MutableMapping[str, str]],
    *,
    row_window: int = 2,
    time_window_seconds: float = 6.0,
) -> list[MutableMapping[str, str]]:
    occurrences: list[_QuoteOccurrence] = []
    for row_index, row in enumerate(annotation_rows):
        if (row.get("Annotation Column") or "").strip() != "Quote Extracted":
            continue
        fragment = (row.get("Annotation Value") or "").strip()
        key = _normalize_quote_dedupe_key(fragment)
        if not key:
            continue
        occurrences.append(
            _QuoteOccurrence(
                row_index=row_index,
                fragment_index=0,
                fragment=fragment,
                key=key,
                start_seconds=_parse_timecode_seconds(row.get("Start Time")),
                end_seconds=_parse_timecode_seconds(row.get("End Time")),
                locator=(row.get("Locator") or "").strip(),
                timing_source=(row.get("Timing Source") or "").strip(),
                transcript_text=(row.get("Text") or "").strip(),
                reference_segment=(row.get("Reference Segment") or "").strip(),
            )
        )
    drop_keys = _dedupe_quote_occurrences(
        occurrences,
        row_window=row_window,
        time_window_seconds=time_window_seconds,
    )
    filtered_rows: list[MutableMapping[str, str]] = []
    for row_index, row in enumerate(annotation_rows):
        if (row.get("Annotation Column") or "").strip() == "Quote Extracted" and (row_index, 0) in drop_keys:
            continue
        filtered_rows.append(row)
    return filtered_rows


def _normalize_money_dedupe_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip().lower())
    normalized = normalized.replace("€", " euros")
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _should_drop_money_fragment(candidate: str, others: Sequence[str]) -> bool:
    candidate_key = _normalize_money_dedupe_key(candidate)
    if not candidate_key:
        return True
    candidate_digits = "".join(ch for ch in candidate_key if ch.isdigit())
    for other in others:
        other_key = _normalize_money_dedupe_key(other)
        if not other_key or other_key == candidate_key:
            continue
        other_digits = "".join(ch for ch in other_key if ch.isdigit())
        if candidate_key in other_key and len(candidate_key) < len(other_key):
            return True
        if candidate_digits and other_digits and candidate_digits in other_digits and len(candidate_digits) < len(other_digits):
            return True
    return False


def _clean_money_mentions(header: Sequence[str], rows: list[list[str]]) -> None:
    money_idx = _header_index(header, "Money Mention")
    if money_idx is None:
        return
    for row in rows:
        cell_value = row[money_idx] if money_idx < len(row) else ""
        if not cell_value:
            continue
        fragments = [fragment.strip() for fragment in cell_value.split("|") if fragment.strip()]
        kept_fragments: list[str] = []
        for fragment in fragments:
            if _should_drop_money_fragment(fragment, fragments):
                continue
            normalized = _normalize_money_dedupe_key(fragment)
            if normalized and all(_normalize_money_dedupe_key(existing) != normalized for existing in kept_fragments):
                kept_fragments.append(fragment)
        row[money_idx] = " | ".join(kept_fragments)


def _move_global_links_to_first_kept_row(
    header: list[str],
    rows: list[list[str]],
    html_path: Path | None,
) -> None:
    if not rows:
        return
    legacy = get_legacy_step2_module()
    keep_idx = legacy.require_column(legacy.build_header_map(header), "Keep")
    article_idx = legacy.ensure_column(header, rows, "Article Links")
    video_idx = legacy.ensure_column(header, rows, "Video Links")
    image_idx = legacy.ensure_column(header, rows, "Image Links")
    excerpt_idx = legacy.ensure_column(header, rows, "Excerpt Video Links")
    direct_idx = legacy.ensure_column(header, rows, "Direct Video Links")

    target_index = None
    for index, row in enumerate(rows):
        if keep_idx < len(row) and (row[keep_idx] or "").strip().lower() == "x":
            target_index = index
            break
    if target_index is None:
        target_index = 0

    links = extract_classified_links(html_path)
    article_links = []
    video_links = []
    image_links = []
    excerpt_links = []
    direct_links = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        fingerprint = (link.category, link.href.casefold())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if link.category == "article":
            article_links.append(link.href)
        elif link.category == "image":
            image_links.append(link.href)
        elif link.category == "video":
            video_links.append(link.href)
            if link.is_excerpt:
                excerpt_links.append(link.href)
            else:
                direct_links.append(link.href)

    for row in rows:
        for idx in (article_idx, video_idx, image_idx, excerpt_idx, direct_idx):
            row[idx] = ""

    rows[target_index][article_idx] = " | ".join(sorted(article_links))
    rows[target_index][video_idx] = " | ".join(sorted(video_links))
    rows[target_index][image_idx] = " | ".join(sorted(image_links))
    rows[target_index][excerpt_idx] = " | ".join(sorted(excerpt_links))
    rows[target_index][direct_idx] = " | ".join(sorted(direct_links))


def _fallback_reference_text(rows: Sequence[Sequence[str]], ref_idx: int, text_idx: int) -> str:
    legacy = get_legacy_step2_module()
    return legacy._reference_fallback_from_rows(rows, ref_idx, text_idx)


def _rows_to_dicts(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        output.append({column: (row[index] if index < len(row) else "") for index, column in enumerate(header)})
    return output


def run_stage_09(
    input_path: Path,
    html_path: Path | None,
    output_path: Path,
    claude_api_key: str | None = None,
    claude_model: str | None = None,
    claude_max_tokens: int = 1200,
    claude_batch_size: int = 60,
    nouns_claude_model: str | None = None,
    nouns_claude_max_tokens: int = 1500,
) -> dict[str, Any]:
    legacy = get_legacy_step2_module()
    header, rows = read_table(input_path)
    existing_values = _capture_existing_columns(
        header,
        rows,
        ("Quote Extracted", "Number Mention", "Date Mention", "Titles", "Title Level"),
    )

    api_key: str | None
    try:
        api_key = legacy.resolve_api_key(claude_api_key)
    except Exception as exc:
        _warn_semantic_fallback(f"Anthropic API unavailable ({exc}); AI tags, mentions, and news links will be skipped.")
        api_key = None
    resolved_cta_model = claude_model or legacy.DEFAULT_CTA_MODEL
    resolved_nouns_model = nouns_claude_model or legacy.DEFAULT_NOUNS_MODEL

    start_seconds, total_duration = legacy.collect_timeline_metadata(rows)
    claude_tags: dict[int, set] = {}
    claude_zoom: dict[int, str] = {}
    if api_key:
        try:
            claude_tags, claude_zoom = legacy.tag_rows_with_claude(
                rows,
                api_key,
                resolved_cta_model,
                claude_max_tokens,
                claude_batch_size,
            )
        except Exception as exc:
            _warn_semantic_fallback(f"CTA tagging failed ({exc}); continuing without Claude CTA tags.")
    legacy.apply_deterministic_cta_tags(rows, claude_tags)
    legacy.adjust_zoom_bias(claude_zoom, start_seconds, total_duration)
    legacy.normalize_zoom_sequences(claude_zoom, start_seconds)
    legacy.apply_tag_columns(header, rows, claude_tags)
    legacy.apply_zoom_column(header, rows, claude_zoom)

    ref_idx = legacy.require_column(legacy.build_header_map(header), "Reference Segment")
    text_idx = legacy.require_column(legacy.build_header_map(header), "Text")
    keep_idx = legacy.require_column(legacy.build_header_map(header), legacy.KEEP_COLUMN_NAME)
    start_idx = legacy.build_header_map(header).get(legacy.START_TIME_COLUMN_NAME.lower())
    end_idx = legacy.build_header_map(header).get(legacy.END_TIME_COLUMN_NAME.lower())

    if html_path and html_path.exists():
        reference_text = legacy.strip_reference_title(legacy.collect_reference_text(html_path))
    else:
        reference_text = _fallback_reference_text(rows, ref_idx, text_idx)

    analysis_text = legacy.prepare_reference_analysis_text(reference_text)
    reference_summary = legacy.build_reference_summary(reference_text)
    language = legacy.detect_language_from_text(analysis_text)
    language_name = legacy.LANGUAGE_PROFILES.get(language, legacy.LANGUAGE_PROFILES["en"]).get("name", "Unknown")

    column_positions = legacy.ensure_feature_columns(header, rows)
    titles_column_idx = legacy.ensure_titles_column(header, rows)
    news_column_idx = legacy.ensure_relevant_news_column(header, rows)
    spans = legacy.build_row_reference_spans(rows, ref_idx, analysis_text)

    if html_path and html_path.exists():
        html_titles = legacy.extract_titles_from_html(html_path)
        located_titles = legacy.locate_titles_in_text(html_titles, analysis_text)
    else:
        html_titles = []
        located_titles = []

    mention_data: MutableMapping[str, object] = {}
    if api_key:
        try:
            mention_data = legacy.extract_mentions_with_claude(
                analysis_text,
                api_key,
                resolved_nouns_model,
                nouns_claude_max_tokens,
                language,
            )
        except Exception as exc:
            _warn_semantic_fallback(f"Mention extraction failed ({exc}); continuing with deterministic fallbacks only.")
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
                    resolved_nouns_model,
                    nouns_claude_max_tokens,
                    language,
                )
            except Exception as exc:
                _warn_semantic_fallback(f"Targeted feeling extraction failed ({exc}); skipping feeling annotations.")
        merged_feelings, _ = legacy.merge_annotation_entries(feeling_entries, targeted_feelings)
        if merged_feelings:
            mention_data["feeling"] = merged_feelings

    legacy.suppress_default_country_mentions(mention_data, language)
    legacy.apply_language_location_overrides(mention_data, language)

    annotations = legacy.map_mentions_to_rows(mention_data, spans, rows, ref_idx, text_idx)
    legacy.apply_feature_values(rows, annotations, column_positions)
    _merge_existing_columns(header, rows, existing_values, ("Quote Extracted", "Number Mention", "Date Mention"))
    _clean_money_mentions(header, rows)
    _dedupe_nearby_quote_rows(header, rows)

    title_assignments = legacy.map_titles_to_rows(located_titles, spans, rows, ref_idx, text_idx)
    legacy.apply_title_annotations(rows, title_assignments, titles_column_idx)
    _merge_existing_columns(header, rows, existing_values, ("Titles", "Title Level"))
    legacy.restrict_tag_columns_to_kept_rows(header, rows)

    durations, _ = legacy.compute_kept_row_durations(rows, keep_idx, start_idx, end_idx)
    news_targets = legacy.allocate_news_targets(durations)
    news_annotations: dict[int, list[dict[str, str]]] = {}
    if api_key:
        try:
            news_annotations = legacy.generate_relevant_news_annotations(
                rows,
                news_targets,
                text_idx,
                ref_idx,
                start_idx,
                end_idx,
                spans,
                analysis_text,
                api_key,
                resolved_nouns_model,
                nouns_claude_max_tokens,
                language_name,
                reference_summary,
            )
        except Exception as exc:
            _warn_semantic_fallback(f"Relevant news generation failed ({exc}); leaving news links empty.")
    legacy.apply_relevant_news(rows, news_column_idx, news_annotations)

    legacy.enrich_html_metrics(header, rows, html_path, None)
    legacy.suppress_zoom_for_list_rows(header, rows)
    _move_global_links_to_first_kept_row(header, rows, html_path)

    output_rows = _rows_to_dicts(header, rows)
    write_enriched_rows(output_path, output_rows)
    kept_rows = sum(1 for row in output_rows if (row.get("Keep") or "").strip().lower() == "x")
    return {
        "rows": len(output_rows),
        "kept_rows": kept_rows,
        "language": language,
        "titles_detected": len(html_titles),
        "news_rows": sum(1 for row in output_rows if row.get("Relevant News")),
    }
