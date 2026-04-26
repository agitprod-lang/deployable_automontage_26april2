from __future__ import annotations

import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Iterable, List, Mapping, Sequence

from .model import MatchMetrics

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
LATIN_SCRIPT_RANGES = (
    (0x0041, 0x024F),
    (0x1E00, 0x1EFF),
)
SCRIPT_RANGES: Mapping[str, tuple[tuple[int, int], ...]] = {
    "latin": LATIN_SCRIPT_RANGES,
    "greek": ((0x0370, 0x03FF),),
    "cyrillic": ((0x0400, 0x052F),),
    "hebrew": ((0x0590, 0x05FF),),
    "arabic": ((0x0600, 0x06FF),),
    "hiragana": ((0x3040, 0x309F),),
    "katakana": ((0x30A0, 0x30FF),),
    "hangul": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF)),
    "han": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
}


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_text(value: str) -> str:
    lowered = strip_accents((value or "").lower())
    lowered = lowered.replace("\u2019", "'").replace("\u2018", "'")
    lowered = lowered.replace("\u201c", '"').replace("\u201d", '"')
    lowered = re.sub(r"[-_/]+", " ", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return normalize_whitespace(lowered)


def tokenize(value: str) -> List[str]:
    return TOKEN_RE.findall(normalize_text(value))


def token_set(value: str) -> set[str]:
    return set(tokenize(value))


def token_overlap_ratio(left_text: str, right_text: str) -> float:
    left_tokens = token_set(left_text)
    right_tokens = token_set(right_text)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens)


def _script_for_char(char: str) -> str:
    if not char or not char.isalpha():
        return ""
    codepoint = ord(char)
    for script, ranges in SCRIPT_RANGES.items():
        for start, end in ranges:
            if start <= codepoint <= end:
                return script
    return "other"


def script_counts(value: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    for char in value or "":
        script = _script_for_char(char)
        if script:
            counts[script] += 1
    return counts


def dominant_script(value: str) -> str:
    counts = script_counts(value)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def script_ratio(value: str, script: str) -> float:
    if not script:
        return 0.0
    counts = script_counts(value)
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    return counts.get(script, 0) / total


def alphabetic_character_count(value: str) -> int:
    return sum(1 for char in value or "" if char.isalpha())


def dominant_script_for_values(values: Iterable[str]) -> str:
    counts: Counter[str] = Counter()
    for value in values:
        counts.update(script_counts(value))
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def ends_with_terminal_punctuation(value: str) -> bool:
    return bool(re.search(r"[.?!:;]\s*$", value or ""))


def longest_contiguous_token_run(
    left_tokens: Sequence[str],
    right_tokens: Sequence[str],
) -> tuple[int, str]:
    if not left_tokens or not right_tokens:
        return 0, ""
    previous = [0] * (len(right_tokens) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_token in enumerate(left_tokens, start=1):
        current = [0] * (len(right_tokens) + 1)
        for right_index, right_token in enumerate(right_tokens, start=1):
            if left_token != right_token:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best_length:
                best_length = current[right_index]
                best_end = left_index
        previous = current
    if best_length <= 0:
        return 0, ""
    phrase = " ".join(left_tokens[best_end - best_length:best_end])
    return best_length, phrase


def ordered_token_overlap(left_text: str, right_text: str) -> float:
    left_tokens = tokenize(left_text)
    right_tokens = tokenize(right_text)
    if not left_tokens or not right_tokens:
        return 0.0
    run_length, _ = longest_contiguous_token_run(left_tokens, right_tokens)
    return run_length / max(len(left_tokens), len(right_tokens))


def set_token_overlap(left_text: str, right_text: str) -> float:
    left_tokens = set(tokenize(left_text))
    right_tokens = set(tokenize(right_text))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))


def char_similarity(left_text: str, right_text: str) -> float:
    left = normalize_text(left_text)
    right = normalize_text(right_text)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def length_ratio(left_text: str, right_text: str) -> float:
    left_tokens = tokenize(left_text)
    right_tokens = tokenize(right_text)
    if left_tokens and right_tokens:
        return min(len(left_tokens), len(right_tokens)) / max(len(left_tokens), len(right_tokens))
    left = normalize_text(left_text)
    right = normalize_text(right_text)
    if not left or not right:
        return 0.0
    return min(len(left), len(right)) / max(len(left), len(right))


def token_overlap_stats(left_text: str, right_text: str) -> dict[str, float | int]:
    left_tokens = tokenize(left_text)
    right_tokens = tokenize(right_text)
    if not left_tokens or not right_tokens:
        return {
            "left_count": len(left_tokens),
            "right_count": len(right_tokens),
            "overlap_count": 0,
            "support_ratio": 0.0,
            "coverage_ratio": 0.0,
            "unsupported_ratio": 1.0 if left_tokens else 0.0,
        }
    left_counts = Counter(left_tokens)
    right_counts = Counter(right_tokens)
    overlap = sum(min(left_counts[token], right_counts[token]) for token in left_counts.keys() & right_counts.keys())
    support_ratio = overlap / len(left_tokens)
    coverage_ratio = overlap / len(right_tokens)
    return {
        "left_count": len(left_tokens),
        "right_count": len(right_tokens),
        "overlap_count": overlap,
        "support_ratio": support_ratio,
        "coverage_ratio": coverage_ratio,
        "unsupported_ratio": max(0.0, 1.0 - support_ratio),
    }


def weighted_match_metrics(left_text: str, right_text: str) -> MatchMetrics:
    ordered = ordered_token_overlap(left_text, right_text)
    token_set = set_token_overlap(left_text, right_text)
    char_ratio = char_similarity(left_text, right_text)
    lengths = length_ratio(left_text, right_text)
    score = (0.40 * ordered) + (0.30 * token_set) + (0.20 * char_ratio) + (0.10 * lengths)
    return MatchMetrics(
        score=score,
        ordered_overlap=ordered,
        set_overlap=token_set,
        char_similarity=char_ratio,
        length_ratio=lengths,
    )


def join_ref_texts(parts: Iterable[str]) -> str:
    return normalize_whitespace(" ".join(part.strip() for part in parts if part.strip()))


def count_phrase_occurrences(text: str, phrase: str) -> int:
    normalized_text = normalize_text(text)
    normalized_phrase = normalize_text(phrase)
    if not normalized_text or not normalized_phrase:
        return 0
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(normalized_phrase)}(?![a-z0-9])")
    return len(pattern.findall(normalized_text))
