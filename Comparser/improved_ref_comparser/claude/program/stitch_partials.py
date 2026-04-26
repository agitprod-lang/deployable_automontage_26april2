#!/usr/bin/env python3
"""
stitch_partials.py  —  assemble reference sentences from multiple partial takes.

Problem
-------
Sometimes a reference sentence is never delivered cleanly in one take. The
speaker produces only fragments ("Ça veut dire?" then "Regardez la situation"
then "depuis un autre an"). The hand-editor stitches those fragments into the
final cut; the upstream pipeline drops them as NO_REFERENCE_COVERED because
each fragment alone has low coverage.

This script runs AFTER 07_step1_compat.csv is produced and BEFORE the final
boundary trim. For every reference sentence whose best single-row coverage is
below a threshold, it tries to greedily assemble a chronological combination
of eliminated fragments that jointly covers >=85% of the sentence tokens. It
then flips those fragments to Keep=x with Status=STITCHED_FRAGMENT.

It also performs dedup: if a kept row's ref_span is fully contained by another
kept row with higher match%, the subset row is dropped (avoids the extra clip
at the TRANSPOSED_MATCH of sentence N when ANCHOR later covers sentence N).

Universal. No hardcoding. Walks candidate fragments in reverse chronological
order to prefer the LAST cluster of attempts (final clean take), and stops
reaching back past a MAX_CLUSTER_GAP_SECS seconds gap once COVERAGE_NEEDS_STITCH
has been met.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Optional

# Allow importing html parser from the bundled lib.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from lib.html_utils import extract_reference_blocks  # noqa: E402
from lib.text_utils import tokenize  # noqa: E402

FPS = 30

COVERAGE_GOOD_ENOUGH = 0.85
COVERAGE_NEEDS_STITCH = 0.70
MIN_NEW_TOKENS_PER_FRAGMENT = 2
MAX_CLUSTER_GAP_SECS = 10.0


def _tc_to_frames(tc: str) -> int:
    parts = tc.strip().split(":")
    if len(parts) != 4:
        return 0
    try:
        h, m, s, f = map(int, parts)
    except ValueError:
        return 0
    return ((h * 3600) + (m * 60) + s) * FPS + f


def _read_semicolon_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _write_semicolon_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _parse_note(notes: str, key: str) -> Optional[str]:
    if not notes:
        return None
    for part in notes.split(" | "):
        part = part.strip()
        if part.startswith(key + "="):
            return part.split("=", 1)[1].strip()
    return None


def _parse_ref_span(notes: str) -> Optional[tuple[int, int]]:
    raw = _parse_note(notes, "ref_span")
    if not raw:
        return None
    m = re.match(r"(\d+)-(\d+)", raw)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_float_note(notes: str, key: str) -> float:
    raw = _parse_note(notes, key)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Reference sentence extraction
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _extract_sentences(html_path: Path) -> list[str]:
    blocks = extract_reference_blocks(html_path)
    sentences: list[str] = []
    for block in blocks:
        for piece in _SENTENCE_SPLIT.split(block):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


# ---------------------------------------------------------------------------
# Dedup: drop kept rows whose ref_span is fully contained in another kept row
# ---------------------------------------------------------------------------

def _dedup_by_ref_span(compat: list[dict], diag_by_tn: dict[str, dict]) -> int:
    kept = [r for r in compat if r.get("Keep", "").strip() == "x"]
    to_drop: set[int] = set()
    for i, a in enumerate(kept):
        tn_a = a.get("Transcript #", "").strip()
        notes_a = (diag_by_tn.get(tn_a, {}) or {}).get("Notes", "") or a.get("Notes", "") or ""
        span_a = _parse_ref_span(notes_a)
        if span_a is None:
            continue
        try:
            match_a = float((a.get("Match %", "") or "0").rstrip("%") or 0)
        except ValueError:
            match_a = 0.0
        status_a = (a.get("Status", "") or "").strip()
        for j, b in enumerate(kept):
            if i == j:
                continue
            tn_b = b.get("Transcript #", "").strip()
            notes_b = (diag_by_tn.get(tn_b, {}) or {}).get("Notes", "") or b.get("Notes", "") or ""
            span_b = _parse_ref_span(notes_b)
            if span_b is None:
                continue
            try:
                match_b = float((b.get("Match %", "") or "0").rstrip("%") or 0)
            except ValueError:
                match_b = 0.0
            # a is a subset of b?
            if span_a[0] >= span_b[0] and span_a[1] <= span_b[1] and span_a != span_b:
                if match_b >= match_a:
                    to_drop.add(id(a))
                    break
            # equal span: keep the one with higher match% and (on tie) the ANCHOR
            if span_a == span_b and i < j:
                if match_b > match_a + 0.5:
                    to_drop.add(id(a))
                    break
                if abs(match_b - match_a) <= 0.5 and status_a != "ANCHOR" and (b.get("Status", "") or "").strip() == "ANCHOR":
                    to_drop.add(id(a))
                    break
    dropped = 0
    for row in compat:
        if id(row) in to_drop:
            row["Keep"] = ""
            if not row.get("Status") or row["Status"].strip() in ("MATCH", "TRANSPOSED_MATCH"):
                row["Status"] = "SUPERSEDED_BY_BETTER_MATCH"
            dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# Stitching
# ---------------------------------------------------------------------------

def _row_tokens(text: str) -> set[str]:
    return set(tokenize(text or ""))


def _score_fragment(fragment_tokens: set[str], sentence_tokens: set[str], already_covered: set[str]) -> tuple[int, int, int]:
    """Return (new_tokens_covered, ref_overlap, noise) — lexicographic score."""
    new_cov = len(fragment_tokens & sentence_tokens - already_covered)
    overlap = len(fragment_tokens & sentence_tokens)
    noise = len(fragment_tokens - sentence_tokens)
    return new_cov, overlap, -noise


def _sentence_coverage(rows: list[dict], sentence_tokens: set[str]) -> tuple[float, set[str]]:
    covered: set[str] = set()
    for r in rows:
        covered |= _row_tokens(r.get("Text", "")) & sentence_tokens
    if not sentence_tokens:
        return 1.0, covered
    return len(covered) / len(sentence_tokens), covered


def _find_fragments_for_sentence(
    sentence: str,
    sentence_idx: int,
    all_rows: list[dict],
    already_kept: list[dict],
) -> list[dict]:
    """Greedy chronological assembly of partial takes to cover the reference sentence."""
    sentence_tokens = set(tokenize(sentence))
    if not sentence_tokens:
        return []

    existing_coverage, covered = _sentence_coverage(already_kept, sentence_tokens)
    if existing_coverage >= COVERAGE_GOOD_ENOUGH:
        return []

    # Rank all non-kept speech rows by chronological order.
    candidates = []
    for r in all_rows:
        if r.get("Keep", "").strip() == "x":
            continue
        if r.get("Kind", "speech").strip() not in ("speech", ""):
            continue
        text = (r.get("Text", "") or "").strip()
        if not text:
            continue
        ftoks = set(tokenize(text))
        if not ftoks:
            continue
        overlap = len(ftoks & sentence_tokens)
        if overlap < MIN_NEW_TOKENS_PER_FRAGMENT:
            continue
        if len(ftoks - sentence_tokens) > 2 * overlap + 1:
            continue
        candidates.append(r)

    candidates.sort(key=lambda r: _tc_to_frames(r.get("Start Time", "00:00:00:00")))

    # Walk backwards so we prefer the LAST cluster of attempts. When a speaker
    # retries a line, their final delivery is what the hand-editor stitches —
    # earlier fragments are usually stumbling false-starts.
    anchor_s: Optional[float] = None
    for r in already_kept:
        try:
            s = _tc_to_frames(r.get("Start Time", "00:00:00:00")) / FPS
            anchor_s = s if anchor_s is None else min(anchor_s, s)
        except ValueError:
            pass
    picked_rev: list[dict] = []
    for r in reversed(candidates):
        ftoks = _row_tokens(r.get("Text", ""))
        new = ftoks & sentence_tokens - covered
        if len(new) < MIN_NEW_TOKENS_PER_FRAGMENT:
            continue
        r_start_s = _tc_to_frames(r.get("Start Time", "00:00:00:00")) / FPS
        current_cov = len(covered) / len(sentence_tokens)
        if (
            anchor_s is not None
            and anchor_s - r_start_s > MAX_CLUSTER_GAP_SECS
            and current_cov >= COVERAGE_NEEDS_STITCH
        ):
            break
        picked_rev.append(r)
        covered |= new
        anchor_s = r_start_s if anchor_s is None else min(anchor_s, r_start_s)
        if len(covered) / len(sentence_tokens) >= COVERAGE_GOOD_ENOUGH:
            break
    picked = list(reversed(picked_rev))

    if not picked:
        return []

    final_coverage = len(covered) / len(sentence_tokens)
    if final_coverage < COVERAGE_NEEDS_STITCH:
        return []

    return picked


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _kept_rows_covering_sentence(
    kept: list[dict],
    sentence_tokens: set[str],
) -> list[dict]:
    """Return kept rows whose Text tokens overlap significantly with the sentence.

    ref_span indexes the granular span list (split by commas AND periods) while
    sentences are split only on .!?, so the two index spaces don't align. Token
    overlap is robust across that mismatch.
    """
    if not sentence_tokens:
        return []
    min_overlap = max(3, int(len(sentence_tokens) * 0.3))
    out = []
    for r in kept:
        rtoks = _row_tokens(r.get("Text", "") or r.get("Reference Segment", ""))
        if len(rtoks & sentence_tokens) >= min_overlap:
            out.append(r)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Stitch partial takes to cover missed reference sentences.")
    parser.add_argument("--compat", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    compat = _read_semicolon_csv(args.compat)
    diagnostic = _read_semicolon_csv(args.diagnostic)
    diag_by_tn = {
        r["Transcript #"].strip(): r
        for r in diagnostic
        if r.get("Transcript #", "").strip() not in ("", "-")
    }

    compat_by_tn = {r.get("Transcript #", "").strip(): r for r in compat}

    # 1. Dedup: drop subset Keep=x rows.
    dropped = _dedup_by_ref_span(compat, diag_by_tn)

    # 2. For each ref sentence, check coverage and stitch if needed.
    sentences = _extract_sentences(args.html)
    added = 0
    for idx, sentence in enumerate(sentences):
        stoks = set(tokenize(sentence))
        if not stoks:
            continue
        kept = [r for r in compat if r.get("Keep", "").strip() == "x"]
        covering = _kept_rows_covering_sentence(kept, stoks)
        coverage, _ = _sentence_coverage(covering, stoks)
        if coverage >= COVERAGE_GOOD_ENOUGH:
            continue

        # Stitch using diagnostic rows so we see the full text even for eliminated ones.
        diagnostic_as_compat: list[dict] = []
        for d in diagnostic:
            tn = d.get("Transcript #", "").strip()
            base = compat_by_tn.get(tn, d)
            row = dict(base)
            row.setdefault("Text", d.get("Text", ""))
            row["Kind"] = d.get("Kind", "")
            diagnostic_as_compat.append(row)

        already_kept_for_sentence = covering
        picked = _find_fragments_for_sentence(
            sentence, idx, diagnostic_as_compat, already_kept_for_sentence,
        )
        if not picked:
            continue

        for frag in picked:
            tn = frag.get("Transcript #", "").strip()
            if not tn:
                continue
            target = compat_by_tn.get(tn)
            if target is None:
                continue
            target["Keep"] = "x"
            target["Status"] = "STITCHED_FRAGMENT"
            if not target.get("Reference Segment"):
                target["Reference Segment"] = sentence
            added += 1

    if not compat:
        args.output.write_text("", encoding="utf-8")
        return

    fieldnames = list(compat[0].keys())
    _write_semicolon_csv(args.output, compat, fieldnames)
    print(f"  [stitch] Dropped {dropped} subset row(s); added {added} stitched fragment(s) → {args.output.name}")


if __name__ == "__main__":
    main()
