#!/usr/bin/env python3
"""
trim_boundaries.py  —  post-stage-07 boundary trimmer

Reads:
  - 07_step1_diagnostic.csv  (has usable_word_range in Notes)
  - 07_step1_compat.csv      (has Keep=x rows)
  - *_words.csv              (word-level timing from Groq)

Produces:
  - 07_step1_compat_trimmed.csv  (same format as compat, with trimmed Start/End Time)

Trimming logic:
  1. For rows WITH usable_word_range: use those word indices to look up precise
     start/end times from the words CSV.
  2. For rows WITHOUT usable_word_range (anchors, etc.): compute the best-matching
     contiguous word subset against the reference text, and use those word boundaries.
  3. Detect and trim abnormally long word durations (>3s) by snapping to the
     average word duration within the segment.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

FPS = 30
LONG_WORD_THRESHOLD_SECS = 3.0


def parse_timecode_to_frames(tc: str, fps: int = FPS) -> int:
    parts = tc.strip().split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode: {tc}")
    h, m, s, f = map(int, parts)
    return ((h * 3600) + (m * 60) + s) * fps + f


def frames_to_timecode(frames: int, fps: int = FPS) -> str:
    if frames < 0:
        frames = 0
    seconds, frame = divmod(frames, fps)
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}:{frame:02d}"


def tc_to_seconds(tc: str) -> float:
    parts = tc.strip().split(":")
    if len(parts) != 4:
        return 0.0
    h, m, s, f = map(int, parts)
    return h * 3600 + m * 60 + s + f / FPS


def parse_note_value(notes: str, key: str) -> str | None:
    if not notes:
        return None
    for part in notes.split(" | "):
        if part.strip().startswith(key + "="):
            return part.strip().split("=", 1)[1].strip()
    return None


def parse_range(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    m = re.match(r"(\d+)-(\d+)", value)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z\u00C0-\u024F\u1E00-\u1EFF]+", text.lower())


def word_overlap_score(words_text: str, ref_text: str) -> float:
    w_tokens = set(tokenize(words_text))
    r_tokens = set(tokenize(ref_text))
    if not r_tokens:
        return 0.0
    if not w_tokens:
        return 0.0
    overlap = w_tokens & r_tokens
    return len(overlap) / max(len(r_tokens), len(w_tokens))


def read_words_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def read_semicolon_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            rows.append(row)
    return rows


def write_semicolon_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def compute_best_word_range(
    row_words: list[dict],
    reference_text: str,
) -> tuple[int, int] | None:
    if len(row_words) <= 1:
        return None

    best_score = -1.0
    best_range = None
    ref_tokens = set(tokenize(reference_text))

    if not ref_tokens:
        return None

    for start in range(len(row_words)):
        for end in range(start, len(row_words)):
            subset_text = " ".join(w.get("Word", w.get("word", "")) for w in row_words[start : end + 1])
            subset_tokens = set(tokenize(subset_text))
            if not subset_tokens:
                continue
            overlap = subset_tokens & ref_tokens
            noise = subset_tokens - ref_tokens
            score = len(overlap) - 0.5 * len(noise)
            if score > best_score:
                best_score = score
                best_range = (start, end)

    return best_range


def _word_duration_frames(w: dict) -> int:
    try:
        return parse_timecode_to_frames(w["End Time"]) - parse_timecode_to_frames(w["Start Time"])
    except (ValueError, KeyError):
        return 0


def _detect_long_word_trim(
    row_words: list[dict],
    new_start_tc: str,
    new_end_tc: str,
    avg_dur: float,
) -> tuple[str, str]:
    if avg_dur <= 0 or len(row_words) < 2:
        return new_start_tc, new_end_tc

    threshold = avg_dur * 4.5

    new_start_f = parse_timecode_to_frames(new_start_tc)
    new_end_f = parse_timecode_to_frames(new_end_tc)

    for w in row_words:
        w_start_f = parse_timecode_to_frames(w["Start Time"])
        w_end_f = parse_timecode_to_frames(w["End Time"])
        dur = w_end_f - w_start_f
        if dur <= threshold:
            continue

        if w_start_f <= new_start_f and w_end_f > new_start_f:
            padding = max(int(avg_dur * 2.5), 6)
            candidate = w_end_f - padding
            if candidate > new_start_f:
                new_start_f = candidate

        if w_end_f >= new_end_f and w_start_f < new_end_f:
            padding = max(int(avg_dur * 2.5), 6)
            candidate = w_start_f + padding
            if candidate < new_end_f:
                new_end_f = candidate

    return frames_to_timecode(new_start_f), frames_to_timecode(new_end_f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trim clip boundaries using word-level data")
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--compat", type=Path, required=True)
    parser.add_argument("--words", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    words = read_words_csv(args.words)
    diagnostic_rows = read_semicolon_csv(args.diagnostic)
    compat_rows = read_semicolon_csv(args.compat)

    diag_by_transcript = {}
    for row in diagnostic_rows:
        tn = row.get("Transcript #", "").strip()
        if tn and tn != "-":
            diag_by_transcript[tn] = row

    trimmed_count = 0
    for row in compat_rows:
        if row.get("Keep", "").strip() != "x":
            continue

        tn = row.get("Transcript #", "").strip()
        if not tn or tn == "-":
            continue

        diag = diag_by_transcript.get(tn)
        if not diag:
            continue

        notes = diag.get("Notes", "")
        ref_text = diag.get("Reference Segment", "") or row.get("Reference Segment", "")

        word_range = parse_range(parse_note_value(notes, "word_range"))
        usable_word_range = parse_range(parse_note_value(notes, "usable_word_range"))

        if word_range is None:
            continue

        abs_start, abs_end = word_range
        if abs_start >= len(words) or abs_end >= len(words):
            continue

        row_words = words[abs_start : abs_end + 1]
        if not row_words:
            continue

        if usable_word_range is not None:
            uw_start, uw_end = usable_word_range
            if uw_start >= len(words) or uw_end >= len(words):
                continue
            new_start_tc = words[uw_start]["Start Time"]
            new_end_tc = words[uw_end]["End Time"]
        elif ref_text:
            local_range = compute_best_word_range(row_words, ref_text)
            if local_range is None:
                continue
            local_start, local_end = local_range
            new_start_tc = row_words[local_start]["Start Time"]
            new_end_tc = row_words[local_end]["End Time"]
        else:
            continue

        durations = [_word_duration_frames(w) for w in row_words]
        avg_dur = sum(durations) / len(durations) if durations else 0

        new_start_tc, new_end_tc = _detect_long_word_trim(
            row_words, new_start_tc, new_end_tc, avg_dur
        )

        old_start = row.get("Start Time", "")
        old_end = row.get("End Time", "")
        if new_start_tc != old_start or new_end_tc != old_end:
            row["Start Time"] = new_start_tc
            row["End Time"] = new_end_tc
            trimmed_count += 1

    if not compat_rows:
        return

    fieldnames = list(compat_rows[0].keys())
    write_semicolon_csv(args.output, compat_rows, fieldnames)
    print(f"  [trim] Trimmed {trimmed_count}/{len(compat_rows)} rows → {args.output.name}")


if __name__ == "__main__":
    main()
