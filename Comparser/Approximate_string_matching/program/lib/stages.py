from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .alignment import (
    align_rows_between_anchors,
    assign_anchors,
    collect_unmatched_ref_indices,
    nearest_matched_ref_bounds,
    relaxed_local_search,
)
from .claude_utils import review_unmatched_rows
from .constants import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_OUTPUT_ROOT,
    FRAME_RATE,
    PUNCTUATION_SPLIT_GAP_SECONDS,
    SILENCE_GAP_SECONDS,
)
from .csv_utils import (
    infer_words_path,
    read_groq_segments,
    read_groq_words,
    read_working_rows,
    write_diagnostic_rows,
    write_pipe_compat_rows,
    write_working_rows,
)
from .html_utils import collect_reference_spans
from .legacy_step2 import get_legacy_step2_module
from .model import WorkingRow, get_note_range, upsert_note
from .repetition import assign_repetition_groups, validate_repetition_groups
from .text_utils import (
    alphabetic_character_count,
    dominant_script,
    dominant_script_for_values,
    ends_with_terminal_punctuation,
    join_ref_texts,
    script_ratio,
    token_overlap_stats,
    tokenize,
    weighted_match_metrics,
)
from .timecode_utils import format_timecode, parse_timecode


def _build_initial_row(
    row_id: int,
    kind: str,
    start_time: str,
    end_time: str,
    text: str,
) -> WorkingRow:
    return WorkingRow(
        row_id=str(row_id),
        kind=kind,
        start_time=start_time,
        end_time=end_time,
        text=text,
    )


def _with_word_range(row: WorkingRow, start_index: int | None, end_index: int | None) -> WorkingRow:
    if start_index is not None and end_index is not None and end_index >= start_index:
        row.notes = upsert_note(row.notes, "word_range", f"{start_index}-{end_index}")
    return row


def _segment_from_words(words_path: Path) -> list[WorkingRow]:
    words = read_groq_words(words_path)
    if not words:
        return []
    rows: list[WorkingRow] = []
    row_id = 1
    current_tokens: list[str] = []
    current_start = words[0].start_time
    current_end = words[0].end_time
    current_word_start = 0
    current_word_end = 0
    previous_end_seconds = parse_timecode(words[0].end_time)
    previous_token = words[0].token
    current_tokens.append(words[0].token)
    for word_index, token in enumerate(words[1:], start=1):
        start_seconds = parse_timecode(token.start_time)
        gap = start_seconds - previous_end_seconds
        should_split = gap >= SILENCE_GAP_SECONDS or (
            ends_with_terminal_punctuation(previous_token) and gap >= PUNCTUATION_SPLIT_GAP_SECONDS
        )
        if should_split and current_tokens:
            rows.append(
                _with_word_range(
                    _build_initial_row(row_id, "speech", current_start, current_end, " ".join(current_tokens)),
                    current_word_start,
                    current_word_end,
                )
            )
            row_id += 1
            if gap >= SILENCE_GAP_SECONDS:
                rows.append(
                    _build_initial_row(
                        row_id,
                        "silence",
                        current_end,
                        token.start_time,
                        "",
                    )
                )
                row_id += 1
            current_tokens = [token.token]
            current_start = token.start_time
            current_end = token.end_time
            current_word_start = word_index
            current_word_end = word_index
        else:
            current_tokens.append(token.token)
            current_end = token.end_time
            current_word_end = word_index
        previous_end_seconds = parse_timecode(token.end_time)
        previous_token = token.token
    if current_tokens:
        rows.append(
            _with_word_range(
                _build_initial_row(row_id, "speech", current_start, current_end, " ".join(current_tokens)),
                current_word_start,
                current_word_end,
            )
        )
    return rows


def _segment_from_groq_csv(csv_path: Path) -> list[WorkingRow]:
    segments = read_groq_segments(csv_path)
    rows: list[WorkingRow] = []
    row_id = 1
    previous_end_seconds: float | None = None
    previous_end_time = ""
    for segment in segments:
        start_seconds = parse_timecode(segment.start_time)
        if previous_end_seconds is not None:
            gap = start_seconds - previous_end_seconds
            if gap >= SILENCE_GAP_SECONDS:
                rows.append(_build_initial_row(row_id, "silence", previous_end_time, segment.start_time, ""))
                row_id += 1
        rows.append(_build_initial_row(row_id, "speech", segment.start_time, segment.end_time, segment.text))
        row_id += 1
        previous_end_seconds = parse_timecode(segment.end_time)
        previous_end_time = segment.end_time
    return rows


def summarize_rows(rows: list[WorkingRow]) -> dict[str, int]:
    return {
        "rows": len(rows),
        "speech_rows": sum(1 for row in rows if row.is_speech()),
        "silence_rows": sum(1 for row in rows if row.is_silence()),
        "eliminated_rows": sum(1 for row in rows if row.is_eliminated()),
        "matched_rows": sum(1 for row in rows if row.reference_segment),
        "anchor_rows": sum(1 for row in rows if row.anchor_id),
    }


def run_stage_00(csv_path: Path, output_path: Path, words_path: Path | None = None) -> dict[str, Any]:
    resolved_words = infer_words_path(csv_path, words_path)
    if resolved_words and resolved_words.exists():
        rows = _segment_from_words(resolved_words)
        source = "words"
    else:
        rows = _segment_from_groq_csv(csv_path)
        source = "segments"
    for row in rows:
        row.notes = upsert_note(row.notes, "segment_source", source)
    write_working_rows(output_path, rows)
    return {"source": source, **summarize_rows(rows)}


def run_stage_01(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    for row in rows:
        if row.is_silence():
            row.eliminate = "x"
            row.eliminate_reason = "SILENCE"
            row.status = "SILENCE"
    write_working_rows(output_path, rows)
    return summarize_rows(rows)


def run_stage_02(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    groups = assign_repetition_groups(rows)
    write_working_rows(output_path, rows)
    summary = summarize_rows(rows)
    summary["repetition_groups"] = len(groups)
    return summary


def run_stage_03(input_path: Path, html_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    ref_spans, reference_text = collect_reference_spans(html_path)
    repetition_summary = validate_repetition_groups(rows, ref_spans, reference_text)
    write_working_rows(output_path, rows)
    summary = summarize_rows(rows)
    summary["validated_repetition_groups"] = len(repetition_summary)
    return summary


def run_stage_04(input_path: Path, html_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    ref_spans, _ = collect_reference_spans(html_path)
    anchors = assign_anchors(rows, ref_spans)
    write_working_rows(output_path, rows)
    summary = summarize_rows(rows)
    summary["anchors"] = len(anchors)
    return summary


def _dominant_reference_language(reference_text: str) -> str:
    legacy = get_legacy_step2_module()
    analysis_text = legacy.prepare_reference_analysis_text(reference_text)
    return legacy.detect_language_from_text(analysis_text)


def _detect_row_language(text: str) -> str:
    if alphabetic_character_count(text) < 4:
        return ""
    legacy = get_legacy_step2_module()
    analysis_text = legacy.prepare_reference_analysis_text(text)
    return legacy.detect_language_from_text(analysis_text)


def _local_unmatched_ref_interval(
    rows: list[WorkingRow],
    ref_count: int,
    row_index: int,
) -> tuple[int, int]:
    left_bound, right_bound = nearest_matched_ref_bounds(rows, row_index)
    start = 0 if left_bound is None else left_bound + 1
    end = (ref_count - 1) if right_bound is None else right_bound - 1
    return start, end


def _annotate_row_context(
    row: WorkingRow,
    dominant_language_code: str,
    dominant_script_name: str,
    row_language_code: str,
    row_script_name: str,
    support: dict[str, float | int],
) -> None:
    row.notes = upsert_note(row.notes, "file_lang", dominant_language_code)
    row.notes = upsert_note(row.notes, "file_script", dominant_script_name)
    row.notes = upsert_note(row.notes, "row_lang", row_language_code)
    row.notes = upsert_note(row.notes, "row_script", row_script_name)
    row.notes = upsert_note(row.notes, "ref_support", f"{float(support['support_ratio']):.2f}")
    row.notes = upsert_note(row.notes, "ref_cover", f"{float(support['coverage_ratio']):.2f}")
    row.notes = upsert_note(row.notes, "ref_noise", f"{float(support['unsupported_ratio']):.2f}")


def _support_ranking(candidate_text: str, reference_text: str) -> tuple[float, int, float, float, float]:
    metrics = weighted_match_metrics(candidate_text, reference_text)
    support = token_overlap_stats(candidate_text, reference_text)
    return (
        metrics.score + (0.25 * float(support["support_ratio"])) + (0.35 * float(support["coverage_ratio"])) - (0.20 * float(support["unsupported_ratio"])),
        int(support["overlap_count"]),
        float(support["support_ratio"]),
        float(support["coverage_ratio"]),
        float(support["unsupported_ratio"]),
    )


def _set_row_reference_assignment(
    row: WorkingRow,
    start_index: int,
    end_index: int,
    ref_spans: list[Any],
    *,
    score: float | None = None,
    status: str | None = None,
) -> None:
    text = join_ref_texts(span.text for span in ref_spans[start_index:end_index + 1])
    row.reference_segment = text
    row.notes = upsert_note(row.notes, "ref_span", f"{start_index}-{end_index}")
    row.notes = upsert_note(row.notes, "match_width", str(end_index - start_index + 1))
    if score is not None:
        row.match_percent = f"{score * 100:.1f}%"
    if status:
        row.status = status


def _assign_usable_ref_span(row: WorkingRow, ref_spans: list[Any]) -> None:
    span_range = get_note_range(row.notes, "ref_span")
    if span_range is None:
        row.notes = upsert_note(row.notes, "usable_ref_span", "")
        return
    start_index, end_index = span_range
    if start_index < 0 or end_index >= len(ref_spans) or end_index < start_index:
        row.notes = upsert_note(row.notes, "usable_ref_span", "")
        return

    full_text = join_ref_texts(span.text for span in ref_spans[start_index:end_index + 1])
    full_ranking = _support_ranking(row.text, full_text)
    best_ranking = full_ranking
    best_bounds = (start_index, end_index)
    row_token_count = max(1, len(tokenize(row.text)))
    full_length_gap = abs(row_token_count - len(tokenize(full_text)))
    minimum_overlap = 1 if row_token_count <= 3 else 2

    for candidate_start in range(start_index, end_index + 1):
        for candidate_end in range(candidate_start, end_index + 1):
            candidate_text = join_ref_texts(span.text for span in ref_spans[candidate_start:candidate_end + 1])
            candidate_ranking = _support_ranking(row.text, candidate_text)
            if candidate_ranking[1] < minimum_overlap:
                continue
            candidate_gap = abs(row_token_count - len(tokenize(candidate_text)))
            ranking = (
                candidate_ranking[0],
                candidate_ranking[1],
                candidate_ranking[3],
                -candidate_ranking[4],
                -candidate_gap,
                candidate_start,
                -(candidate_end - candidate_start),
            )
            current_gap = abs(row_token_count - len(tokenize(join_ref_texts(span.text for span in ref_spans[best_bounds[0]:best_bounds[1] + 1]))))
            best_tuple = (
                best_ranking[0],
                best_ranking[1],
                best_ranking[3],
                -best_ranking[4],
                -current_gap,
                best_bounds[0],
                -(best_bounds[1] - best_bounds[0]),
            )
            if ranking > best_tuple:
                best_ranking = candidate_ranking
                best_bounds = (candidate_start, candidate_end)

    best_text = join_ref_texts(span.text for span in ref_spans[best_bounds[0]:best_bounds[1] + 1])
    best_length_gap = abs(row_token_count - len(tokenize(best_text)))
    should_trim = (
        best_bounds != (start_index, end_index)
        and best_ranking[1] >= full_ranking[1]
        and best_ranking[0] >= full_ranking[0] - 0.02
        and (
            best_ranking[4] <= full_ranking[4] - 0.15
            or best_length_gap <= max(0, full_length_gap - 3)
            or best_ranking[3] >= min(1.0, full_ranking[3] + 0.20)
        )
    )

    if should_trim:
        row.notes = upsert_note(row.notes, "assigned_ref_span", f"{start_index}-{end_index}")
        row.notes = upsert_note(row.notes, "usable_ref_span", f"{best_bounds[0]}-{best_bounds[1]}")
        _set_row_reference_assignment(
            row,
            best_bounds[0],
            best_bounds[1],
            ref_spans,
        )
        row.match_percent = f"{best_ranking[0] * 100:.1f}%"
    else:
        row.notes = upsert_note(row.notes, "usable_ref_span", "")


def _assign_usable_word_range(row: WorkingRow, words: list[Any]) -> None:
    if not row.reference_segment.strip():
        row.notes = upsert_note(row.notes, "usable_word_range", "")
        return
    word_range = get_note_range(row.notes, "word_range")
    if word_range is None:
        row.notes = upsert_note(row.notes, "usable_word_range", "")
        return
    absolute_start, absolute_end = word_range
    if absolute_start < 0 or absolute_end >= len(words) or absolute_end < absolute_start:
        row.notes = upsert_note(row.notes, "usable_word_range", "")
        return
    row_words = words[absolute_start:absolute_end + 1]
    if len(row_words) <= 1:
        row.notes = upsert_note(row.notes, "usable_word_range", "")
        return

    full_text = " ".join(word.token for word in row_words)
    full_ranking = _support_ranking(full_text, row.reference_segment)
    best_ranking = full_ranking
    best_bounds = (absolute_start, absolute_end)
    ref_token_count = max(1, len(tokenize(row.reference_segment)))
    minimum_overlap = 1 if ref_token_count <= 2 else 2

    for start_offset in range(len(row_words)):
        for end_offset in range(start_offset, len(row_words)):
            candidate_text = " ".join(word.token for word in row_words[start_offset:end_offset + 1])
            candidate_ranking = _support_ranking(candidate_text, row.reference_segment)
            if candidate_ranking[1] < minimum_overlap:
                continue
            ranking = (
                candidate_ranking[0],
                candidate_ranking[1],
                candidate_ranking[3],
                -candidate_ranking[4],
                start_offset,
                -(end_offset - start_offset),
            )
            best_tuple = (
                best_ranking[0],
                best_ranking[1],
                best_ranking[3],
                -best_ranking[4],
                best_bounds[0] - absolute_start,
                -(best_bounds[1] - best_bounds[0]),
            )
            if ranking > best_tuple:
                best_ranking = candidate_ranking
                best_bounds = (absolute_start + start_offset, absolute_start + end_offset)

    should_trim = (
        best_bounds != (absolute_start, absolute_end)
        and best_ranking[1] >= full_ranking[1]
        and best_ranking[0] >= full_ranking[0] - 0.02
        and best_ranking[4] <= full_ranking[4] - 0.20
        and best_ranking[3] >= max(full_ranking[3] - 0.10, 0.50)
    )
    row.notes = upsert_note(row.notes, "usable_support", f"{best_ranking[2]:.2f}")
    row.notes = upsert_note(row.notes, "usable_cover", f"{best_ranking[3]:.2f}")
    row.notes = upsert_note(row.notes, "usable_noise", f"{best_ranking[4]:.2f}")
    row.notes = upsert_note(
        row.notes,
        "usable_word_range",
        f"{best_bounds[0]}-{best_bounds[1]}" if should_trim else "",
    )


def _recover_repeat_suffix_backfill(rows: list[WorkingRow], ref_spans: list[Any]) -> bool:
    changed = False
    groups: dict[str, list[tuple[int, WorkingRow]]] = {}
    for row_index, row in enumerate(rows):
        if row.repeat_group and row.repeat_role == "LAST_TAKE_SUFFIX":
            groups.setdefault(row.repeat_group, []).append((row_index, row))
    for _group_id, group_rows in groups.items():
        ordered_rows = sorted(group_rows, key=lambda item: int(item[1].row_id or "0"))
        selected_span = next(
            (get_note_range(row.notes, "repeat_selected_ref_span") for _row_index, row in ordered_rows if get_note_range(row.notes, "repeat_selected_ref_span") is not None),
            None,
        )
        if selected_span is None:
            continue
        selected_start, selected_end = selected_span
        consumed: set[int] = set()
        unmatched_rows: list[WorkingRow] = []
        for _row_index, row in ordered_rows:
            span_range = get_note_range(row.notes, "ref_span")
            if span_range is not None and row.reference_segment:
                consumed.update(range(span_range[0], span_range[1] + 1))
            else:
                unmatched_rows.append(row)
        remaining = [index for index in range(selected_start, selected_end + 1) if index not in consumed]
        if len(unmatched_rows) != 1 or not remaining:
            continue
        contiguous = all(remaining[offset] == remaining[0] + offset for offset in range(len(remaining)))
        if not contiguous:
            continue
        target_row = unmatched_rows[0]
        _set_row_reference_assignment(
            target_row,
            remaining[0],
            remaining[-1],
            ref_spans,
            score=None,
            status="SUFFIX_REF_BACKFILL",
        )
        if target_row.eliminate_reason in {"BAD_TAKE_PRE_REPEAT", "NO_REFERENCE_COVERED"}:
            target_row.eliminate = ""
            target_row.eliminate_reason = ""
        changed = True
    return changed


def _recover_local_matches(rows: list[WorkingRow], ref_spans: list[Any], words: list[Any] | None = None) -> bool:
    changed = False
    unmatched_ref = set(collect_unmatched_ref_indices(rows, ref_spans))
    for row_index, row in enumerate(rows):
        if not row.is_speech() or row.reference_segment:
            continue
        if row.eliminate_reason in {"OFF_SCRIPT", "OFF_LANGUAGE", "SILENCE"}:
            continue
        interval_start, interval_end = _local_unmatched_ref_interval(rows, len(ref_spans), row_index)
        if interval_start > interval_end:
            continue
        if not any(interval_start <= ref_index <= interval_end for ref_index in unmatched_ref):
            continue
        left_bound, right_bound = nearest_matched_ref_bounds(rows, row_index)
        window, score = relaxed_local_search(row, ref_spans, left_bound, right_bound)
        if window is None:
            continue
        _set_row_reference_assignment(
            row,
            window.start_index,
            window.end_index,
            ref_spans,
            score=score,
            status="RELAXED_LOCAL_MATCH",
        )
        if row.eliminate_reason in {"BAD_TAKE_PRE_REPEAT", "NO_REFERENCE_COVERED"}:
            row.eliminate = ""
            row.eliminate_reason = ""
        _assign_usable_ref_span(row, ref_spans)
        if words:
            _assign_usable_word_range(row, words)
        changed = True
        unmatched_ref = set(collect_unmatched_ref_indices(rows, ref_spans))
    return changed


def _apply_stage1_cleanup(
    rows: list[WorkingRow],
    ref_spans: list[Any],
    reference_text: str,
    words: list[Any] | None = None,
) -> None:
    dominant_language_code = _dominant_reference_language(reference_text)
    dominant_script_name = dominant_script_for_values(span.text for span in ref_spans) or dominant_script(reference_text)
    for row in rows:
        if not row.is_speech():
            continue
        support = token_overlap_stats(row.text, reference_text)
        row_script_name = dominant_script(row.text)
        row_language_code = _detect_row_language(row.text)
        _annotate_row_context(
            row,
            dominant_language_code,
            dominant_script_name,
            row_language_code,
            row_script_name,
            support,
        )
        if not row.is_eliminated() and row.reference_segment:
            _assign_usable_ref_span(row, ref_spans)
        if words and not row.is_eliminated() and row.reference_segment:
            _assign_usable_word_range(row, words)

    if _recover_repeat_suffix_backfill(rows, ref_spans):
        for row in rows:
            if row.is_speech() and row.reference_segment and not row.is_eliminated():
                _assign_usable_ref_span(row, ref_spans)
                if words:
                    _assign_usable_word_range(row, words)

    _recover_local_matches(rows, ref_spans, words)

    unmatched_ref = set(collect_unmatched_ref_indices(rows, ref_spans))
    for row_index, row in enumerate(rows):
        if not row.is_speech() or row.is_eliminated() or row.reference_segment:
            continue
        row_script_name = dominant_script(row.text)
        if (
            dominant_script_name
            and row_script_name
            and alphabetic_character_count(row.text) >= 1
            and script_ratio(row.text, dominant_script_name) < 0.45
        ):
            row.eliminate = "x"
            row.eliminate_reason = "OFF_SCRIPT"
            row.status = "ELIMINATE_OFF_SCRIPT"
            continue
        row_language_code = _detect_row_language(row.text)
        support = token_overlap_stats(row.text, reference_text)
        if (
            dominant_language_code
            and row_language_code
            and row_language_code != dominant_language_code
            and int(support["overlap_count"]) == 0
            and alphabetic_character_count(row.text) >= 4
        ):
            row.eliminate = "x"
            row.eliminate_reason = "OFF_LANGUAGE"
            row.status = "ELIMINATE_OFF_LANGUAGE"
            continue
        interval_start, interval_end = _local_unmatched_ref_interval(rows, len(ref_spans), row_index)
        row.notes = upsert_note(
            row.notes,
            "ref_window",
            f"{interval_start}-{interval_end}" if interval_start <= interval_end else "covered",
        )
        covered = interval_start > interval_end or not any(
            interval_start <= ref_index <= interval_end for ref_index in unmatched_ref
        )
        if covered:
            row.eliminate = "x"
            row.eliminate_reason = "NO_REFERENCE_COVERED"
            row.status = "ELIMINATE_NO_REFERENCE_COVERED"


def run_stage_05(
    input_path: Path,
    html_path: Path,
    output_path: Path,
    words_path: Path | None = None,
) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    ref_spans, _ = collect_reference_spans(html_path)
    align_rows_between_anchors(rows, ref_spans, words_path=words_path)
    words = read_groq_words(words_path) if words_path and words_path.exists() else None
    _apply_stage1_cleanup(rows, ref_spans, join_ref_texts(span.text for span in ref_spans), words)
    unmatched_ref = collect_unmatched_ref_indices(rows, ref_spans)
    write_working_rows(output_path, rows)
    summary = summarize_rows(rows)
    summary["unmatched_ref_spans"] = len(unmatched_ref)
    return summary


def run_stage_06(
    input_path: Path,
    html_path: Path,
    output_path: Path,
    model: str = DEFAULT_CLAUDE_MODEL,
    max_tokens: int = 1200,
    api_key: str | None = None,
) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    ref_spans, reference_text = collect_reference_spans(html_path)
    candidate_indices = [
        index
        for index, row in enumerate(rows)
        if row.is_speech() and not row.is_eliminated() and not row.reference_segment
    ]
    ref_context: dict[str, dict[str, str]] = {}
    for index in candidate_indices:
        row = rows[index]
        left_bound, right_bound = nearest_matched_ref_bounds(rows, index)
        left_reference = ref_spans[left_bound].text if left_bound is not None and left_bound < len(ref_spans) else ""
        right_reference = ref_spans[right_bound].text if right_bound is not None and right_bound < len(ref_spans) else ""
        ref_context[row.row_id] = {
            "left_reference": left_reference,
            "right_reference": right_reference,
        }
    decisions = review_unmatched_rows(
        rows,
        candidate_indices,
        ref_context,
        model=model,
        max_tokens=max_tokens,
        api_key=api_key,
    )
    for index in candidate_indices:
        row = rows[index]
        decision = decisions.get(row.row_id, {"decision": "KEEP_UNMATCHED", "notes": ""})
        decision_name = decision["decision"]
        row.notes = upsert_note(row.notes, "claude", decision.get("notes", ""))
        if decision_name == "ELIMINATE_OFF_TOPIC":
            row.eliminate = "x"
            row.eliminate_reason = "OFF_TOPIC"
            row.status = "ELIMINATE_OFF_TOPIC"
        elif decision_name == "ELIMINATE_META_COMMENTARY":
            row.eliminate = "x"
            row.eliminate_reason = "META_COMMENTARY"
            row.status = "ELIMINATE_META_COMMENTARY"
        elif decision_name == "KEEP_TRANSCRIPTION_ERROR":
            left_bound, right_bound = nearest_matched_ref_bounds(rows, index)
            window, score = relaxed_local_search(row, ref_spans, left_bound, right_bound)
            if window is not None:
                row.reference_segment = window.text
                row.match_percent = f"{score * 100:.1f}%"
                row.status = "TRANSCRIPTION_ERROR_MATCH"
                row.notes = upsert_note(row.notes, "ref_span", f"{window.start_index}-{window.end_index}")
                row.notes = upsert_note(row.notes, "match_width", str(window.end_index - window.start_index + 1))
            else:
                row.status = "TRANSCRIPTION_ERROR_UNRESOLVED"
        else:
            row.status = "KEEP_UNMATCHED"
    _apply_stage1_cleanup(rows, ref_spans, reference_text)
    write_working_rows(output_path, rows)
    summary = summarize_rows(rows)
    summary["claude_reviewed_rows"] = len(candidate_indices)
    return summary


def _xml_ready_keep_value(row: WorkingRow) -> str:
    return "x" if not row.is_eliminated() and row.reference_segment.strip() else ""


def _step2_input_keep_value(row: WorkingRow) -> str:
    return "" if row.reference_segment.strip() else "x"


def _xml_ready_row(row: WorkingRow) -> dict[str, str]:
    return {
        "Keep": _xml_ready_keep_value(row),
        "Transcript #": row.row_id,
        "Start Time": row.start_time,
        "End Time": row.end_time,
        "Text": row.text,
        "Reference Segment": row.reference_segment,
        "Match %": row.match_percent,
        "Status": row.status,
    }


def _step2_input_row(row: WorkingRow) -> dict[str, str]:
    return {
        "Keep": _step2_input_keep_value(row),
        "Transcript #": row.row_id,
        "Start Time": row.start_time,
        "End Time": row.end_time,
        "Text": row.text,
        "Reference Segment": row.reference_segment,
        "Match %": row.match_percent,
        "Status": row.status,
    }


def _diagnostic_row(row: WorkingRow) -> dict[str, str]:
    return {
        **_xml_ready_row(row),
        "Kind": row.kind,
        "Eliminate": row.eliminate,
        "Eliminate Reason": row.eliminate_reason,
        "Repeat Group": row.repeat_group,
        "Repeat Role": row.repeat_role,
        "Anchor ID": row.anchor_id,
        "Notes": row.notes,
    }


def run_stage_07(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    compat_rows = [_xml_ready_row(row) for row in rows if row.is_speech()]
    write_pipe_compat_rows(output_path, compat_rows)
    return {"compat_rows": len(compat_rows)}


def write_stage_07_step2_input(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    compat_rows = [_step2_input_row(row) for row in rows if row.is_speech()]
    write_pipe_compat_rows(output_path, compat_rows)
    return {"step2_input_rows": len(compat_rows)}


def write_stage_07_diagnostic(input_path: Path, output_path: Path) -> dict[str, Any]:
    rows = read_working_rows(input_path)
    diagnostic_rows = [_diagnostic_row(row) for row in rows]
    write_diagnostic_rows(output_path, diagnostic_rows)
    return {"diagnostic_rows": len(diagnostic_rows)}


def default_run_directory(output_dir: Path | None = None) -> Path:
    root = output_dir or DEFAULT_OUTPUT_ROOT
    return root / datetime.now().strftime("%Y%m%d_%H%M%S")


def run_full_pipeline(
    csv_path: Path,
    html_path: Path,
    words_path: Path | None = None,
    output_dir: Path | None = None,
    claude_model: str = DEFAULT_CLAUDE_MODEL,
    claude_max_tokens: int = 1200,
    claude_api_key: str | None = None,
    allow_segment_fallback: bool = False,
) -> dict[str, Any]:
    if not allow_segment_fallback and not words_path:
        raise FileNotFoundError(
            f"Missing Groq word-level CSV for {csv_path}. "
            "A *_words.csv artifact is now required unless allow_segment_fallback=True."
        )
    run_dir = default_run_directory(output_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    stage_outputs = {
        "00": run_dir / "00_segmented.csv",
        "01": run_dir / "01_marked_silence.csv",
        "02": run_dir / "02_repetition_groups.csv",
        "03": run_dir / "03_validated_repetitions.csv",
        "04": run_dir / "04_anchor_map.csv",
        "05": run_dir / "05_aligned.csv",
        "06": run_dir / "06_reviewed.csv",
        "07": run_dir / "07_step1_compat.csv",
        "07_step2_input": run_dir / "07_step1_step2_input.csv",
        "07_diagnostic": run_dir / "07_step1_diagnostic.csv",
    }
    summary = {
        "inputs": {
            "csv": str(csv_path),
            "html": str(html_path),
            "words": str(words_path) if words_path else "",
        },
        "outputs": {key: str(value) for key, value in stage_outputs.items()},
        "step1_xml_ready_csv": str(stage_outputs["07"]),
        "step1_step2_input_csv": str(stage_outputs["07_step2_input"]),
        "step1_diagnostic_csv": str(stage_outputs["07_diagnostic"]),
        "stages": {},
    }
    summary["stages"]["00"] = run_stage_00(csv_path, stage_outputs["00"], words_path)
    summary["stages"]["01"] = run_stage_01(stage_outputs["00"], stage_outputs["01"])
    summary["stages"]["02"] = run_stage_02(stage_outputs["01"], stage_outputs["02"])
    summary["stages"]["03"] = run_stage_03(stage_outputs["02"], html_path, stage_outputs["03"])
    summary["stages"]["04"] = run_stage_04(stage_outputs["03"], html_path, stage_outputs["04"])
    summary["stages"]["05"] = run_stage_05(stage_outputs["04"], html_path, stage_outputs["05"], words_path)
    summary["stages"]["06"] = run_stage_06(
        stage_outputs["05"],
        html_path,
        stage_outputs["06"],
        model=claude_model,
        max_tokens=claude_max_tokens,
        api_key=claude_api_key,
    )
    summary["stages"]["07"] = run_stage_07(stage_outputs["06"], stage_outputs["07"])
    summary["stages"]["07"].update(
        write_stage_07_step2_input(stage_outputs["06"], stage_outputs["07_step2_input"])
    )
    summary["stages"]["07"].update(
        write_stage_07_diagnostic(stage_outputs["06"], stage_outputs["07_diagnostic"])
    )
    final_rows = read_working_rows(stage_outputs["06"])
    ref_spans, _ = collect_reference_spans(html_path)
    summary["final"] = {
        **summarize_rows(final_rows),
        "leftover_ref_spans": len(collect_unmatched_ref_indices(final_rows, ref_spans)),
        "frame_rate": FRAME_RATE,
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary
