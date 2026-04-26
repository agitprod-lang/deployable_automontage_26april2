from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .constants import (
    ANCHOR_MAX_REF_COMBINATION,
    ANCHOR_MIN_SCORE,
    ANCHOR_MIN_TOKENS,
    BOUNDARY_REPAIR_LOW_CONFIDENCE_SCORE,
    BOUNDARY_REPAIR_MAX_LOCAL_REF_SPANS,
    BOUNDARY_REPAIR_MAX_LOCAL_ROWS,
    BOUNDARY_REPAIR_MAX_REF_COMBINATION,
    BOUNDARY_REPAIR_MIN_IMPROVEMENT,
    DP_SKIP_REF_PENALTY,
    DP_SKIP_TRANSCRIPT_PENALTY,
    GAP_MAX_REF_COMBINATION,
    MATCH_MIN_SCORE,
    MAX_REF_COMBINATION,
    RELAXED_LOCAL_MATCH_MIN_SCORE,
    RELAXED_LOCAL_SET_OVERLAP,
    TRANSPOSED_ORDERED_MAX,
    TRANSPOSED_SET_OVERLAP,
)
from .csv_utils import read_groq_words
from .model import (
    AnchorMatch,
    RefSpan,
    RefWindow,
    TimedToken,
    WorkingRow,
    get_note_range,
    ref_span_range_from_notes,
    upsert_note,
)
from .text_utils import join_ref_texts, normalize_text, weighted_match_metrics
from .timecode_utils import parse_timecode

CONNECTOR_TOKENS = {
    "and", "but", "or", "so", "because", "then",
    "et", "mais", "ou", "donc", "car", "puis", "alors", "avec", "pour", "vers",
}


def build_ref_windows(
    ref_spans: Sequence[RefSpan],
    max_len: int = MAX_REF_COMBINATION,
) -> list[RefWindow]:
    windows: list[RefWindow] = []
    for start_index in range(len(ref_spans)):
        for width in range(1, max_len + 1):
            end_index = start_index + width - 1
            if end_index >= len(ref_spans):
                break
            text = join_ref_texts(span.text for span in ref_spans[start_index:end_index + 1])
            token_count = len(normalize_text(text).split())
            windows.append(
                RefWindow(
                    start_index=start_index,
                    end_index=end_index,
                    text=text,
                    normalized=normalize_text(text),
                    token_count=token_count,
                )
            )
    return windows


def parse_match_percent(value: str) -> float:
    raw = (value or "").strip().rstrip("%")
    if not raw:
        return 0.0
    try:
        return float(raw) / 100.0
    except ValueError:
        return 0.0


def assign_anchors(rows: list[WorkingRow], ref_spans: Sequence[RefSpan]) -> list[AnchorMatch]:
    windows = build_ref_windows(ref_spans, ANCHOR_MAX_REF_COMBINATION)
    max_ref_end = len(ref_spans) - 1
    anchors: list[AnchorMatch] = []
    anchor_count = 0
    for row_index in range(len(rows) - 1, -1, -1):
        row = rows[row_index]
        if not row.is_speech() or row.is_eliminated():
            continue
        if len(normalize_text(row.text).split()) < ANCHOR_MIN_TOKENS:
            continue
        best_window: RefWindow | None = None
        best_score = 0.0
        row_token_count = len(normalize_text(row.text).split())
        for window in windows:
            if window.end_index > max_ref_end:
                continue
            metrics = weighted_match_metrics(row.text, window.text)
            if metrics.score < ANCHOR_MIN_SCORE:
                continue
            ranking = (
                metrics.score,
                min(row_token_count, window.token_count),
                -abs(row_token_count - window.token_count),
            )
            best_ranking = (
                best_score,
                min(row_token_count, best_window.token_count) if best_window else -1,
                -abs(row_token_count - best_window.token_count) if best_window else float("-inf"),
            )
            if ranking <= best_ranking:
                continue
            best_window = window
            best_score = metrics.score
        if best_window is None:
            continue
        anchor_count += 1
        row.reference_segment = best_window.text
        row.match_percent = f"{best_score * 100:.1f}%"
        row.status = "ANCHOR"
        row.anchor_id = f"A{anchor_count:03d}"
        row.notes = upsert_note(row.notes, "ref_span", f"{best_window.start_index}-{best_window.end_index}")
        row.notes = upsert_note(row.notes, "match_width", str(best_window.end_index - best_window.start_index + 1))
        anchors.append(
            AnchorMatch(
                row_index=row_index,
                ref_start_index=best_window.start_index,
                ref_end_index=best_window.end_index,
                score=best_score,
            )
        )
        max_ref_end = best_window.start_index - 1
    anchors.reverse()
    for index, anchor in enumerate(anchors, start=1):
        rows[anchor.row_index].anchor_id = f"A{index:03d}"
    return anchors


def _match_status_for(row_text: str, ref_text: str) -> tuple[str | None, float]:
    metrics = weighted_match_metrics(row_text, ref_text)
    transposed = metrics.set_overlap >= TRANSPOSED_SET_OVERLAP and metrics.ordered_overlap < TRANSPOSED_ORDERED_MAX
    if metrics.score >= MATCH_MIN_SCORE or transposed:
        status = "TRANSPOSED_MATCH" if transposed else "MATCH"
        reward = max(metrics.score, MATCH_MIN_SCORE) if transposed else metrics.score
        return status, reward
    return None, metrics.score


def _plan_gap_alignment(
    row_indices: Sequence[int],
    ref_indices: Sequence[int],
    rows: list[WorkingRow],
    ref_spans: Sequence[RefSpan],
    max_ref_combination: int = GAP_MAX_REF_COMBINATION,
) -> tuple[list[tuple[str, int, float]], float]:
    row_count = len(row_indices)
    ref_count = len(ref_indices)
    scores = [[float("-inf")] * (ref_count + 1) for _ in range(row_count + 1)]
    choices: list[list[tuple[str, int, float] | None]] = [[None] * (ref_count + 1) for _ in range(row_count + 1)]
    scores[row_count][ref_count] = 0.0

    for row_cursor in range(row_count, -1, -1):
        for ref_cursor in range(ref_count, -1, -1):
            if row_cursor == row_count and ref_cursor == ref_count:
                continue
            best_score = float("-inf")
            best_choice: tuple[str, int, float] | None = None
            if row_cursor < row_count and scores[row_cursor + 1][ref_cursor] != float("-inf"):
                candidate = scores[row_cursor + 1][ref_cursor] - DP_SKIP_TRANSCRIPT_PENALTY
                if candidate > best_score:
                    best_score = candidate
                    best_choice = ("skip_row", 1, -DP_SKIP_TRANSCRIPT_PENALTY)
            if ref_cursor < ref_count and scores[row_cursor][ref_cursor + 1] != float("-inf"):
                candidate = scores[row_cursor][ref_cursor + 1] - DP_SKIP_REF_PENALTY
                if candidate > best_score:
                    best_score = candidate
                    best_choice = ("skip_ref", 1, -DP_SKIP_REF_PENALTY)
            if row_cursor < row_count:
                row = rows[row_indices[row_cursor]]
                for width in range(1, max_ref_combination + 1):
                    if ref_cursor + width > ref_count:
                        break
                    future = scores[row_cursor + 1][ref_cursor + width]
                    if future == float("-inf"):
                        continue
                    text = join_ref_texts(
                        ref_spans[ref_indices[position]].text
                        for position in range(ref_cursor, ref_cursor + width)
                    )
                    status, reward = _match_status_for(row.text, text)
                    if status is None:
                        continue
                    candidate = future + reward
                    if candidate > best_score:
                        best_score = candidate
                        best_choice = (status, width, reward)
            scores[row_cursor][ref_cursor] = best_score
            choices[row_cursor][ref_cursor] = best_choice

    plan: list[tuple[str, int, float]] = []
    row_cursor = 0
    ref_cursor = 0
    while row_cursor < row_count or ref_cursor < ref_count:
        choice = choices[row_cursor][ref_cursor]
        if choice is None:
            break
        action, width, reward = choice
        plan.append(choice)
        if action == "skip_row":
            row_cursor += 1
        elif action == "skip_ref":
            ref_cursor += 1
        else:
            row_cursor += 1
            ref_cursor += width
    return plan, scores[0][0]


def _derive_assignments(
    row_indices: Sequence[int],
    ref_indices: Sequence[int],
    plan: Sequence[tuple[str, int, float]],
    rows: list[WorkingRow],
    ref_spans: Sequence[RefSpan],
) -> tuple[dict[int, dict[str, object] | None], set[int]]:
    assignments: dict[int, dict[str, object] | None] = {}
    consumed_refs: set[int] = set()
    row_cursor = 0
    ref_cursor = 0
    for action, width, _reward in plan:
        if action == "skip_row":
            assignments[row_indices[row_cursor]] = None
            row_cursor += 1
            continue
        if action == "skip_ref":
            ref_cursor += 1
            continue
        row_index = row_indices[row_cursor]
        chosen_ref_indices = ref_indices[ref_cursor:ref_cursor + width]
        text = join_ref_texts(ref_spans[index].text for index in chosen_ref_indices)
        metrics = weighted_match_metrics(rows[row_index].text, text)
        assignments[row_index] = {
            "text": text,
            "status": action,
            "score": metrics.score,
            "span_start": chosen_ref_indices[0],
            "span_end": chosen_ref_indices[-1],
        }
        consumed_refs.update(chosen_ref_indices)
        row_cursor += 1
        ref_cursor += width
    while row_cursor < len(row_indices):
        assignments[row_indices[row_cursor]] = None
        row_cursor += 1
    return assignments, consumed_refs


def _apply_assignments(
    rows: list[WorkingRow],
    row_indices: Sequence[int],
    assignments: dict[int, dict[str, object] | None],
    repaired_rows: set[int] | None = None,
) -> None:
    repaired_rows = repaired_rows or set()
    for row_index in row_indices:
        row = rows[row_index]
        assignment = assignments.get(row_index)
        if assignment is None:
            if not row.anchor_id:
                row.reference_segment = ""
                row.match_percent = ""
                row.status = "UNMATCHED_PENDING_CLAUDE"
                row.notes = upsert_note(row.notes, "ref_span", "")
                row.notes = upsert_note(row.notes, "match_width", "")
            if row_index in repaired_rows:
                row.notes = upsert_note(row.notes, "boundary_repair", "applied")
            continue
        span_start = int(assignment["span_start"])
        span_end = int(assignment["span_end"])
        row.reference_segment = str(assignment["text"])
        row.match_percent = f"{float(assignment['score']) * 100:.1f}%"
        row.status = str(assignment["status"])
        row.notes = upsert_note(row.notes, "ref_span", f"{span_start}-{span_end}")
        row.notes = upsert_note(row.notes, "match_width", str(span_end - span_start + 1))
        if row_index in repaired_rows:
            row.notes = upsert_note(row.notes, "boundary_repair", "applied")


def _row_word_map(rows: Sequence[WorkingRow], words_path: Path | None) -> dict[int, list[TimedToken]]:
    if not words_path:
        return {}
    path_tokens = read_groq_words(words_path)
    row_words: dict[int, list[TimedToken]] = {}
    for row_index, row in enumerate(rows):
        note_range = get_note_range(row.notes, "word_range")
        if note_range is None:
            continue
        start, end = note_range
        if 0 <= start <= end < len(path_tokens):
            row_words[row_index] = path_tokens[start:end + 1]
    return row_words


def _split_preference(tokens: Sequence[str], split_index: int, word_entries: Sequence[TimedToken]) -> float:
    prev_token = tokens[split_index - 1]
    next_token = tokens[split_index]
    bonus = 0.0
    if prev_token.endswith((",", ";", ":", ".", "?", "!", "…")):
        bonus += 0.15
    if normalize_text(next_token) in CONNECTOR_TOKENS:
        bonus += 0.08
    if word_entries and split_index < len(word_entries):
        gap = parse_timecode(word_entries[split_index].start_time) - parse_timecode(word_entries[split_index - 1].end_time)
        if gap > 0:
            bonus += min(gap, 1.0) * 0.12
    midpoint = len(tokens) / 2.0
    distance = abs(split_index - midpoint)
    bonus += max(0.0, 0.04 - (distance * 0.01))
    return bonus


def _annotate_split_notes(
    row: WorkingRow,
    span_start: int,
    span_end: int,
    ref_spans: Sequence[RefSpan],
    word_entries: Sequence[TimedToken],
) -> None:
    if span_end <= span_start:
        row.notes = upsert_note(row.notes, "repair_word_split", "")
        row.notes = upsert_note(row.notes, "repair_ref_split", "")
        row.notes = upsert_note(row.notes, "repair_mode", "")
        return
    tokens = [entry.token for entry in word_entries] if word_entries else row.text.split()
    if len(tokens) < 2:
        return
    candidate_positions = range(1, len(tokens))
    best_score = float("-inf")
    best_word_split = None
    best_ref_split = None
    mode = "word" if word_entries else "token"
    assigned_score = weighted_match_metrics(row.text, row.reference_segment).score
    for ref_split in range(span_start, span_end):
        left_ref = join_ref_texts(ref_spans[index].text for index in range(span_start, ref_split + 1))
        right_ref = join_ref_texts(ref_spans[index].text for index in range(ref_split + 1, span_end + 1))
        for split_index in candidate_positions:
            left_text = " ".join(tokens[:split_index])
            right_text = " ".join(tokens[split_index:])
            if not left_text or not right_text:
                continue
            score = (
                weighted_match_metrics(left_text, left_ref).score
                + weighted_match_metrics(right_text, right_ref).score
                + _split_preference(tokens, split_index, word_entries)
            )
            if score > best_score:
                best_score = score
                best_word_split = split_index
                best_ref_split = ref_split
    if best_word_split is None or best_ref_split is None:
        return
    if best_score < assigned_score + 0.05:
        return
    row.notes = upsert_note(row.notes, "repair_word_split", str(best_word_split))
    row.notes = upsert_note(row.notes, "repair_ref_split", str(best_ref_split))
    row.notes = upsert_note(row.notes, "repair_mode", mode)


def _is_problematic(row: WorkingRow) -> bool:
    if not row.is_speech() or row.is_eliminated() or row.anchor_id:
        return False
    if not row.reference_segment:
        return True
    if row.status == "TRANSPOSED_MATCH":
        return True
    return parse_match_percent(row.match_percent) < BOUNDARY_REPAIR_LOW_CONFIDENCE_SCORE


def _nearest_speech_row(
    rows: Sequence[WorkingRow],
    start_index: int,
    direction: int,
    require_match: bool = False,
    allow_anchor: bool = True,
) -> int | None:
    index = start_index + direction
    while 0 <= index < len(rows):
        row = rows[index]
        if row.is_speech() and not row.is_eliminated():
            if require_match and ref_span_range_from_notes(row) is None:
                index += direction
                continue
            if not allow_anchor and row.anchor_id:
                index += direction
                continue
            return index
        index += direction
    return None


def _problematic_blocks(rows: Sequence[WorkingRow]) -> list[list[int]]:
    speech_indices = [index for index, row in enumerate(rows) if row.is_speech() and not row.is_eliminated()]
    blocks: list[list[int]] = []
    current: list[int] = []
    previous_position = None
    position_by_row = {row_index: position for position, row_index in enumerate(speech_indices)}
    for row_index in speech_indices:
        if not _is_problematic(rows[row_index]):
            if current:
                blocks.append(current)
                current = []
            previous_position = position_by_row[row_index]
            continue
        current_position = position_by_row[row_index]
        if current and previous_position is not None and current_position != previous_position + 1:
            blocks.append(current)
            current = []
        current.append(row_index)
        previous_position = current_position
    if current:
        blocks.append(current)
    return blocks


def _current_local_objective(
    local_row_indices: Sequence[int],
    region_indices: Sequence[int],
    rows: Sequence[WorkingRow],
) -> float:
    objective = 0.0
    consumed_refs: set[int] = set()
    region_start = region_indices[0]
    region_end = region_indices[-1]
    for row_index in local_row_indices:
        row = rows[row_index]
        span_range = ref_span_range_from_notes(row)
        if row.reference_segment and span_range is not None and region_start <= span_range[0] and span_range[1] <= region_end:
            _status, reward = _match_status_for(row.text, row.reference_segment)
            objective += reward if reward > 0 else weighted_match_metrics(row.text, row.reference_segment).score
            consumed_refs.update(range(span_range[0], span_range[1] + 1))
        else:
            objective -= DP_SKIP_TRANSCRIPT_PENALTY
    objective -= DP_SKIP_REF_PENALTY * len(set(region_indices) - consumed_refs)
    return objective


def _repair_problem_blocks(
    rows: list[WorkingRow],
    ref_spans: Sequence[RefSpan],
    row_words: dict[int, list[TimedToken]],
) -> None:
    for block in _problematic_blocks(rows):
        left_index = _nearest_speech_row(rows, block[0], -1, require_match=True, allow_anchor=False)
        right_index = _nearest_speech_row(rows, block[-1], 1, require_match=True, allow_anchor=False)
        local_row_indices = list(block)
        repaired_rows = set(block)
        if left_index is not None:
            local_row_indices.insert(0, left_index)
        if right_index is not None and right_index not in local_row_indices:
            local_row_indices.append(right_index)
        if len(local_row_indices) <= len(block) or len(local_row_indices) > BOUNDARY_REPAIR_MAX_LOCAL_ROWS:
            continue

        previous_matched = left_index if left_index is not None else _nearest_speech_row(rows, block[0], -1, require_match=True)
        next_matched = right_index if right_index is not None else _nearest_speech_row(rows, block[-1], 1, require_match=True)

        if left_index is not None:
            left_span = ref_span_range_from_notes(rows[left_index])
            region_start = left_span[0] if left_span is not None else 0
        elif previous_matched is not None:
            previous_span = ref_span_range_from_notes(rows[previous_matched])
            region_start = previous_span[1] + 1 if previous_span is not None else 0
        else:
            region_start = 0

        if right_index is not None:
            right_span = ref_span_range_from_notes(rows[right_index])
            region_end = right_span[1] if right_span is not None else len(ref_spans) - 1
        elif next_matched is not None:
            next_span = ref_span_range_from_notes(rows[next_matched])
            region_end = next_span[0] - 1 if next_span is not None else len(ref_spans) - 1
        else:
            region_end = len(ref_spans) - 1

        if region_start > region_end:
            continue
        region_indices = list(range(region_start, region_end + 1))
        if len(region_indices) > BOUNDARY_REPAIR_MAX_LOCAL_REF_SPANS:
            continue

        current_objective = _current_local_objective(local_row_indices, region_indices, rows)
        plan, new_objective = _plan_gap_alignment(
            local_row_indices,
            region_indices,
            rows,
            ref_spans,
            max_ref_combination=BOUNDARY_REPAIR_MAX_REF_COMBINATION,
        )
        assignments, _consumed = _derive_assignments(local_row_indices, region_indices, plan, rows, ref_spans)
        if left_index is not None and assignments.get(left_index) is None:
            continue
        if right_index is not None and assignments.get(right_index) is None:
            continue
        changed = False
        for row_index in local_row_indices:
            current_range = ref_span_range_from_notes(rows[row_index])
            proposed = assignments.get(row_index)
            proposed_range = None if proposed is None else (int(proposed["span_start"]), int(proposed["span_end"]))
            if current_range != proposed_range:
                changed = True
                break
        if not changed or new_objective < current_objective + BOUNDARY_REPAIR_MIN_IMPROVEMENT:
            continue
        _apply_assignments(rows, local_row_indices, assignments, repaired_rows)
        for row_index in repaired_rows:
            proposed = assignments.get(row_index)
            if proposed is None:
                continue
            _annotate_split_notes(
                rows[row_index],
                int(proposed["span_start"]),
                int(proposed["span_end"]),
                ref_spans,
                row_words.get(row_index, []),
            )


def align_rows_between_anchors(
    rows: list[WorkingRow],
    ref_spans: Sequence[RefSpan],
    words_path: Path | None = None,
) -> list[int]:
    anchors = [
        (index, ref_span_range_from_notes(row))
        for index, row in enumerate(rows)
        if row.anchor_id and ref_span_range_from_notes(row) is not None
    ]
    anchors = [(index, span_range) for index, span_range in anchors if span_range is not None]
    transcript_start = 0
    ref_start = 0
    regions: list[tuple[list[int], list[int]]] = []
    for row_index, span_range in anchors:
        ref_span_start, ref_span_end = span_range
        transcript_rows = [
            index
            for index in range(transcript_start, row_index)
            if rows[index].is_speech() and not rows[index].is_eliminated() and not rows[index].anchor_id
        ]
        ref_region = list(range(ref_start, ref_span_start))
        regions.append((transcript_rows, ref_region))
        transcript_start = row_index + 1
        ref_start = ref_span_end + 1
    tail_rows = [
        index
        for index in range(transcript_start, len(rows))
        if rows[index].is_speech() and not rows[index].is_eliminated() and not rows[index].anchor_id
    ]
    regions.append((tail_rows, list(range(ref_start, len(ref_spans)))))

    for transcript_rows, ref_region in regions:
        plan, _score = _plan_gap_alignment(transcript_rows, ref_region, rows, ref_spans, GAP_MAX_REF_COMBINATION)
        assignments, _consumed = _derive_assignments(transcript_rows, ref_region, plan, rows, ref_spans)
        _apply_assignments(rows, transcript_rows, assignments)

    row_words = _row_word_map(rows, words_path)
    _repair_problem_blocks(rows, ref_spans, row_words)
    return collect_unmatched_ref_indices(rows, ref_spans)


def nearest_matched_ref_bounds(rows: Sequence[WorkingRow], target_index: int) -> tuple[int | None, int | None]:
    left_bound = None
    right_bound = None
    for index in range(target_index - 1, -1, -1):
        span_range = ref_span_range_from_notes(rows[index])
        if span_range is not None:
            left_bound = span_range[1]
            break
    for index in range(target_index + 1, len(rows)):
        span_range = ref_span_range_from_notes(rows[index])
        if span_range is not None:
            right_bound = span_range[0]
            break
    return left_bound, right_bound


def relaxed_local_search(
    row: WorkingRow,
    ref_spans: Sequence[RefSpan],
    left_bound: int | None,
    right_bound: int | None,
) -> tuple[RefWindow | None, float]:
    start = 0 if left_bound is None else max(0, left_bound - 1)
    end = len(ref_spans) - 1 if right_bound is None else min(len(ref_spans) - 1, right_bound + 1)
    candidates = build_ref_windows(ref_spans[start:end + 1], GAP_MAX_REF_COMBINATION)
    best_window: RefWindow | None = None
    best_score = 0.0
    for candidate in candidates:
        adjusted = RefWindow(
            start_index=candidate.start_index + start,
            end_index=candidate.end_index + start,
            text=candidate.text,
            normalized=candidate.normalized,
            token_count=candidate.token_count,
        )
        metrics = weighted_match_metrics(row.text, adjusted.text)
        if metrics.score >= RELAXED_LOCAL_MATCH_MIN_SCORE or metrics.set_overlap >= RELAXED_LOCAL_SET_OVERLAP:
            if metrics.score > best_score:
                best_score = metrics.score
                best_window = adjusted
    return best_window, best_score


def collect_unmatched_ref_indices(rows: Iterable[WorkingRow], ref_spans: Sequence[RefSpan]) -> list[int]:
    consumed: set[int] = set()
    for row in rows:
        span_range = ref_span_range_from_notes(row)
        if span_range is None:
            continue
        consumed.update(range(span_range[0], span_range[1] + 1))
    return [index for index in range(len(ref_spans)) if index not in consumed]
