from __future__ import annotations

from collections import defaultdict

from .constants import (
    GAP_MAX_REF_COMBINATION,
    REPETITION_LOOKBACK,
    REPETITION_MIN_RUN,
    REPETITION_MIN_SHORTER_COVERAGE,
)
from .model import RefSpan, RepetitionLink, WorkingRow, get_note, upsert_note
from .text_utils import (
    count_phrase_occurrences,
    join_ref_texts,
    longest_contiguous_token_run,
    token_overlap_stats,
    tokenize,
    weighted_match_metrics,
)


class DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def add(self, value: int) -> None:
        self.parent.setdefault(value, value)

    def find(self, value: int) -> int:
        if self.parent[value] != value:
            self.parent[value] = self.find(self.parent[value])
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left


def detect_repetition_links(rows: list[WorkingRow]) -> list[RepetitionLink]:
    speech_positions = [index for index, row in enumerate(rows) if row.is_speech()]
    links: list[RepetitionLink] = []
    token_cache = {index: tokenize(rows[index].text) for index in speech_positions}
    for cursor, row_index in enumerate(speech_positions):
        row_tokens = token_cache[row_index]
        if not row_tokens:
            continue
        for previous_cursor in range(max(0, cursor - REPETITION_LOOKBACK), cursor):
            previous_index = speech_positions[previous_cursor]
            previous_tokens = token_cache[previous_index]
            if not previous_tokens:
                continue
            run_length, phrase = longest_contiguous_token_run(previous_tokens, row_tokens)
            shorter = min(len(previous_tokens), len(row_tokens))
            coverage = run_length / shorter if shorter else 0.0
            short_exact_repeat = shorter <= 3 and coverage >= 1.0
            sufficient_overlap = run_length >= REPETITION_MIN_RUN
            sufficient_coverage = (
                shorter < REPETITION_MIN_RUN and run_length >= 2 and coverage >= REPETITION_MIN_SHORTER_COVERAGE
            )
            if not (sufficient_overlap or short_exact_repeat or sufficient_coverage):
                continue
            links.append(
                RepetitionLink(
                    left_row_index=previous_index,
                    right_row_index=row_index,
                    run_length=run_length,
                    coverage=coverage,
                    phrase=phrase,
                )
            )
    return links


def assign_repetition_groups(rows: list[WorkingRow]) -> dict[str, str]:
    links = detect_repetition_links(rows)
    dsu = DisjointSet()
    core_phrase_by_root: dict[int, str] = {}
    for link in links:
        dsu.add(link.left_row_index)
        dsu.add(link.right_row_index)
        dsu.union(link.left_row_index, link.right_row_index)
    members: dict[int, list[int]] = defaultdict(list)
    for value in dsu.parent:
        members[dsu.find(value)].append(value)
    phrase_candidates: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for link in links:
        root = dsu.find(link.left_row_index)
        phrase_candidates[root].append((link.run_length, link.phrase))
    for root, pairs in phrase_candidates.items():
        best = max(pairs, key=lambda item: item[0])[1]
        core_phrase_by_root[root] = best
    assigned: dict[str, str] = {}
    for group_index, root in enumerate(sorted(members, key=lambda value: min(members[value])), start=1):
        indices = sorted(members[root])
        if len(indices) <= 1:
            continue
        group_id = f"REP{group_index:03d}"
        core_phrase = core_phrase_by_root.get(root, "")
        for index in indices:
            rows[index].repeat_group = group_id
            rows[index].notes = upsert_note(rows[index].notes, "repeat_core", core_phrase)
        assigned[group_id] = core_phrase
    return assigned


def _build_ref_windows(ref_spans: list[RefSpan]) -> list[tuple[int, int, str]]:
    windows: list[tuple[int, int, str]] = []
    for start_index in range(len(ref_spans)):
        for width in range(1, GAP_MAX_REF_COMBINATION + 1):
            end_index = start_index + width - 1
            if end_index >= len(ref_spans):
                break
            windows.append(
                (
                    start_index,
                    end_index,
                    join_ref_texts(span.text for span in ref_spans[start_index:end_index + 1]),
                )
            )
    return windows


def _suffix_rows_are_adjacent(
    rows: list[WorkingRow],
    suffix_indices: list[int],
) -> bool:
    if not suffix_indices:
        return False
    suffix_set = set(suffix_indices)
    start_index = suffix_indices[0]
    end_index = suffix_indices[-1]
    for row_index in range(start_index, end_index + 1):
        if rows[row_index].is_speech() and row_index not in suffix_set:
            return False
    return True


def _candidate_suffixes(
    rows: list[WorkingRow],
    ordered_group_rows: list[tuple[int, WorkingRow]],
) -> list[list[tuple[int, WorkingRow]]]:
    suffixes: list[list[tuple[int, WorkingRow]]] = []
    for start_offset in range(len(ordered_group_rows) - 1, -1, -1):
        candidate = ordered_group_rows[start_offset:]
        candidate_indices = [row_index for row_index, _row in candidate]
        if not _suffix_rows_are_adjacent(rows, candidate_indices):
            break
        suffixes.append(candidate)
    return suffixes or [ordered_group_rows[-1:]]


def _suffix_ranking(
    suffix_rows: list[tuple[int, WorkingRow]],
    ref_spans: list[RefSpan],
    ref_windows: list[tuple[int, int, str]],
) -> tuple[tuple[float, int, float, float, float, int, int], tuple[int, int, str]]:
    suffix_text = join_ref_texts(row.text for _row_index, row in suffix_rows)
    suffix_tokens = tokenize(suffix_text)
    best_ranking = (float("-inf"), 0, 0.0, float("-inf"), float("-inf"), float("-inf"), float("-inf"))
    best_window = (0, 0, "")
    for start_index, end_index, window_text in ref_windows:
        metrics = weighted_match_metrics(suffix_text, window_text)
        support = token_overlap_stats(suffix_text, window_text)
        overlap_count = int(support["overlap_count"])
        if overlap_count <= 0:
            continue
        ranking = (
            metrics.score + (0.25 * float(support["support_ratio"])) + (0.35 * float(support["coverage_ratio"])) - (0.20 * float(support["unsupported_ratio"])),
            overlap_count,
            float(support["coverage_ratio"]),
            -float(support["unsupported_ratio"]),
            -abs(len(suffix_tokens) - len(tokenize(window_text))),
            suffix_rows[0][0],
            end_index - start_index,
        )
        if ranking > best_ranking:
            best_ranking = ranking
            best_window = (start_index, end_index, window_text)
    return best_ranking, best_window


def validate_repetition_groups(
    rows: list[WorkingRow],
    ref_spans: list[RefSpan],
    reference_text: str,
) -> dict[str, dict[str, int | str]]:
    grouped: dict[str, list[tuple[int, WorkingRow]]] = defaultdict(list)
    for row_index, row in enumerate(rows):
        if row.repeat_group:
            grouped[row.repeat_group].append((row_index, row))
    ref_windows = _build_ref_windows(ref_spans)
    results: dict[str, dict[str, int | str]] = {}
    for group_id, group_rows in grouped.items():
        ordered_rows = sorted(group_rows, key=lambda item: int(item[1].row_id or "0"))
        core_phrase = next(
            (get_note(row.notes, "repeat_core") for _row_index, row in ordered_rows if get_note(row.notes, "repeat_core")),
            "",
        )
        occurrences = count_phrase_occurrences(reference_text, core_phrase)
        results[group_id] = {"occurrences": occurrences, "core_phrase": core_phrase}
        if occurrences >= len(ordered_rows):
            for _row_index, row in ordered_rows:
                row.eliminate = ""
                row.eliminate_reason = ""
                row.repeat_role = "EXPECTED_REPEAT"
                row.status = "EXPECTED_REPEAT"
        else:
            suffixes = _candidate_suffixes(rows, ordered_rows)
            kept_suffix = max(suffixes, key=lambda candidate: _suffix_ranking(candidate, ref_spans, ref_windows)[0])
            kept_indices = {row_index for row_index, _row in kept_suffix}
            _, best_window = _suffix_ranking(kept_suffix, ref_spans, ref_windows)
            window_start, window_end, _window_text = best_window
            for row_index, row in ordered_rows:
                row.notes = upsert_note(row.notes, "repeat_selected_ref_span", f"{window_start}-{window_end}")
                row.notes = upsert_note(row.notes, "repeat_suffix_size", str(len(kept_suffix)))
                if row_index in kept_indices:
                    row.eliminate = ""
                    row.eliminate_reason = ""
                    row.repeat_role = "LAST_TAKE_SUFFIX" if len(kept_suffix) > 1 else "LAST_TAKE"
                    if not row.status:
                        row.status = "LAST_TAKE_KEEP"
                    continue
                row.repeat_role = "PRE_REPEAT"
                row.eliminate = "x"
                row.eliminate_reason = "BAD_TAKE_PRE_REPEAT"
                row.status = "PRE_REPEAT_ELIMINATED"
    return results
