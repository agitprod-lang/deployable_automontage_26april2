#!/usr/bin/env python3
"""
align_words.py — CTC forced-alignment word timing for Groq transcripts.

Fills the gaps Groq's word-level transcription leaves behind by running
French wav2vec2 forced alignment (same approach WhisperX uses) per segment.

Inputs:
  --csv    Groq segment CSV (Speaker Name, Start Time, End Time, Text)
  --rush   Rush video or audio file (ffmpeg extracts 16 kHz mono)
  --output Destination words CSV (Word, Start Time, End Time, HH:MM:SS:FF)

The produced CSV is a drop-in replacement for the *_words.csv fed to the
comparser pipeline (--words).
"""
from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import List, Tuple

import numpy as np
import torch
import torchaudio
from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lib.timecode_utils import format_timecode, parse_timecode

MODEL_ID = "bofenghuang/asr-wav2vec2-ctc-french"
SAMPLE_RATE = 16000
SEGMENT_PAD_S = 0.25


def _select_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def _extract_audio(source: Path, scratch_dir: Path | None = None) -> np.ndarray:
    """Extract rush → float32 mono 16 kHz numpy array via ffmpeg."""
    scratch = scratch_dir or (SCRIPT_DIR.parent / "output" / "_align_scratch")
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=str(scratch)) as tmp:
        wav_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(source),
                "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav",
                str(wav_path),
            ],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        waveform, sr = torchaudio.load(str(wav_path))
        if sr != SAMPLE_RATE:
            waveform = torchaudio.functional.resample(waveform, sr, SAMPLE_RATE)
        return waveform[0].numpy().astype(np.float32)
    finally:
        wav_path.unlink(missing_ok=True)


_APOSTROPHE_RE = re.compile(r"[''`´]")


def _normalize_for_vocab(text: str, vocab: set[str]) -> Tuple[str, List[Tuple[int, int]]]:
    """
    Lowercase + strip diacritics the model's vocab doesn't have, replace any
    char not in vocab with space, collapse whitespace.

    Returns (normalized, mapping) where mapping[i] = (orig_start, orig_end)
    slice in the input text that produced normalized char i. Spaces have
    mapping entries too so we can rebuild word boundaries.
    """
    text = _APOSTROPHE_RE.sub("'", text)
    out_chars: list[str] = []
    mapping: list[Tuple[int, int]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        lower = ch.lower()
        if lower in vocab:
            out_chars.append(lower)
            mapping.append((i, i + 1))
            i += 1
            continue
        # Strip diacritic and retry.
        decomposed = unicodedata.normalize("NFD", lower)
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        if stripped and stripped[0] in vocab and stripped[0] != " ":
            out_chars.append(stripped[0])
            mapping.append((i, i + 1))
            i += 1
            continue
        # Unsupported char → word boundary.
        out_chars.append(" ")
        mapping.append((i, i + 1))
        i += 1
    # Collapse consecutive spaces.
    collapsed_chars: list[str] = []
    collapsed_map: list[Tuple[int, int]] = []
    prev_space = True  # trims leading space
    for ch, m in zip(out_chars, mapping):
        if ch == " ":
            if prev_space:
                continue
            collapsed_chars.append(" ")
            collapsed_map.append(m)
            prev_space = True
        else:
            collapsed_chars.append(ch)
            collapsed_map.append(m)
            prev_space = False
    # Strip trailing space.
    while collapsed_chars and collapsed_chars[-1] == " ":
        collapsed_chars.pop()
        collapsed_map.pop()
    return "".join(collapsed_chars), collapsed_map


def _split_original_words(text: str) -> List[Tuple[str, int, int]]:
    """Return list of (word, char_start, char_end) from the raw segment text."""
    words = []
    for match in re.finditer(r"\S+", text):
        words.append((match.group(0), match.start(), match.end()))
    return words


def _align_segment(
    audio: np.ndarray,
    text: str,
    model: Wav2Vec2ForCTC,
    processor: Wav2Vec2Processor,
    device: torch.device,
    vocab: set[str],
    blank_id: int,
) -> dict[int, Tuple[str, float, float]]:
    """Return {word_index: (word, start_s, end_s)} relative to segment audio."""
    if audio.size < SAMPLE_RATE // 8:  # < 125 ms, nothing to align
        return {}
    normalized, char_map = _normalize_for_vocab(text, vocab)
    if not normalized.strip():
        return {}

    input_values = processor(
        audio, sampling_rate=SAMPLE_RATE, return_tensors="pt"
    ).input_values.to(device)
    with torch.inference_mode():
        logits = model(input_values).logits  # [1, T, V]
    log_probs = torch.log_softmax(logits.float(), dim=-1).cpu()

    tokenizer = processor.tokenizer
    word_delim = tokenizer.word_delimiter_token
    # Build target token IDs from normalized chars: letters → char token;
    # space → word_delim token.
    target_ids: list[int] = []
    target_char_indices: list[int] = []  # map token i → normalized-char index
    for idx, ch in enumerate(normalized):
        tok = word_delim if ch == " " else ch
        tid = tokenizer.convert_tokens_to_ids(tok)
        if tid is None or tid == tokenizer.unk_token_id:
            continue
        target_ids.append(tid)
        target_char_indices.append(idx)
    if not target_ids:
        return {}

    targets = torch.tensor([target_ids], dtype=torch.int32)
    try:
        aligned, scores = torchaudio.functional.forced_align(
            log_probs, targets, blank=blank_id
        )
    except (RuntimeError, ValueError):
        return {}
    token_spans = torchaudio.functional.merge_tokens(aligned[0], scores[0])

    # Map each target-token index → (start_frame, end_frame).
    span_by_token_pos: dict[int, Tuple[int, int]] = {}
    emitted = 0
    for span in token_spans:
        if int(span.token) == blank_id:
            continue
        if emitted < len(target_ids):
            span_by_token_pos[emitted] = (int(span.start), int(span.end))
        emitted += 1
    if not span_by_token_pos:
        return {}

    # Frame → seconds. wav2vec2-xlsr has 320-sample stride at 16 kHz → 20 ms/frame.
    T = log_probs.shape[1]
    seconds_per_frame = audio.size / SAMPLE_RATE / max(T, 1)

    orig_words = _split_original_words(text)
    if not orig_words:
        return {}

    span_by_norm_char: dict[int, Tuple[int, int]] = {}
    for token_pos, span in span_by_token_pos.items():
        norm_idx = target_char_indices[token_pos]
        span_by_norm_char[norm_idx] = span

    results: dict[int, Tuple[str, float, float]] = {}
    for idx, (word, wstart, wend) in enumerate(orig_words):
        norm_indices = [
            i for i, (os_, oe_) in enumerate(char_map)
            if os_ >= wstart and oe_ <= wend
        ]
        frames = [span_by_norm_char[i] for i in norm_indices if i in span_by_norm_char]
        if not frames:
            continue
        f_start = min(f[0] for f in frames)
        f_end = max(f[1] for f in frames)
        results[idx] = (word, f_start * seconds_per_frame, f_end * seconds_per_frame)
    return results


def _load_segments(csv_path: Path) -> List[dict]:
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [row for row in reader]


def _norm_word(w: str) -> str:
    w = _APOSTROPHE_RE.sub("'", w).lower()
    return re.sub(r"[^\w']", "", w, flags=re.UNICODE)


def _load_groq_words(path: Path) -> list[Tuple[str, float, float]]:
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            return [
                (r["Word"], parse_timecode(r["Start Time"]), parse_timecode(r["End Time"]))
                for r in reader
            ]
    except FileNotFoundError:
        return []


def align_file(
    csv_path: Path,
    rush_path: Path,
    output_path: Path,
    groq_words_csv: Path | None = None,
) -> int:
    device = _select_device()
    print(f"[align] device={device} model={MODEL_ID}", flush=True)
    processor = Wav2Vec2Processor.from_pretrained(MODEL_ID)
    model = Wav2Vec2ForCTC.from_pretrained(MODEL_ID, use_safetensors=True).to(device).eval()
    vocab = set(processor.tokenizer.get_vocab().keys())
    # Exclude special tokens from "usable char" vocab.
    vocab -= {processor.tokenizer.pad_token, processor.tokenizer.unk_token,
              processor.tokenizer.bos_token, processor.tokenizer.eos_token,
              processor.tokenizer.word_delimiter_token}
    vocab = {v for v in vocab if v and len(v) == 1}
    blank_id = processor.tokenizer.pad_token_id

    print(f"[align] extracting audio from {rush_path}", flush=True)
    audio = _extract_audio(rush_path)
    duration_s = audio.size / SAMPLE_RATE
    print(f"[align] audio duration: {duration_s:.1f}s", flush=True)

    segments = _load_segments(csv_path)
    print(f"[align] {len(segments)} segments to align", flush=True)

    groq_words = _load_groq_words(groq_words_csv) if groq_words_csv else []

    words_rows: list[Tuple[str, str, str]] = []
    gap_fills = 0
    for idx, seg in enumerate(segments):
        start_s = parse_timecode(seg["Start Time"])
        end_s = parse_timecode(seg["End Time"])
        text = (seg.get("Text") or "").strip()
        if not text or end_s <= start_s:
            continue
        slice_start = max(0.0, start_s - SEGMENT_PAD_S)
        slice_end = min(duration_s, end_s + SEGMENT_PAD_S)
        i0 = int(slice_start * SAMPLE_RATE)
        i1 = int(slice_end * SAMPLE_RATE)
        seg_audio = audio[i0:i1]
        word_timings = _align_segment(
            seg_audio, text, model, processor, device, vocab, blank_id
        )

        orig_words = _split_original_words(text)
        seg_groq = [
            (w, s, e) for (w, s, e) in groq_words
            if s >= start_s - SEGMENT_PAD_S and e <= end_s + SEGMENT_PAD_S
        ]

        groq_used: set[int] = set()
        aligned_count = 0
        filled_count = 0
        segment_rows: list[Tuple[str, float, float]] = []  # (word, abs_start_s, abs_end_s)
        prev_end_s = -1.0

        def _try_groq(target_word: str) -> Tuple[float, float] | None:
            target_norm = _norm_word(target_word)
            if not target_norm:
                return None
            for g_idx, (gw, gs, ge) in enumerate(seg_groq):
                if g_idx in groq_used:
                    continue
                if _norm_word(gw) != target_norm:
                    continue
                # Clamp to preserve monotonicity — let digits slot into
                # the tiny gap between two aligned words.
                clamped_start = max(gs, prev_end_s)
                clamped_end = max(ge, clamped_start + 1.0 / 30.0)
                groq_used.add(g_idx)
                return (clamped_start, clamped_end)
            return None

        for w_idx, (word, _, _) in enumerate(orig_words):
            chosen: Tuple[float, float] | None = None
            if w_idx in word_timings:
                _, ws, we = word_timings[w_idx]
                abs_start = slice_start + ws
                abs_end = slice_start + we
                # Reject a CTC span that violates monotonicity; fall through
                # to Groq so the output sequence stays ordered.
                if abs_start + 1e-3 >= prev_end_s:
                    if abs_end <= abs_start:
                        abs_end = abs_start + 1.0 / 30.0
                    chosen = (abs_start, abs_end)
                    aligned_count += 1
            if chosen is None:
                groq_hit = _try_groq(word)
                if groq_hit is not None:
                    chosen = groq_hit
                    filled_count += 1
            if chosen is None:
                continue
            segment_rows.append((word, chosen[0], chosen[1]))
            prev_end_s = max(prev_end_s, chosen[1])

        for word, abs_start, abs_end in segment_rows:
            words_rows.append((
                word, format_timecode(abs_start), format_timecode(abs_end)
            ))

        gap_fills += filled_count
        if aligned_count == 0 and filled_count == 0:
            print(f"[align] segment {idx}: no alignment ({text[:40]!r})", flush=True)
        else:
            print(
                f"[align] segment {idx}: {aligned_count} aligned + {filled_count} Groq-filled",
                flush=True,
            )

    if groq_words_csv:
        print(f"[align] total Groq gap-fills: {gap_fills}", flush=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(["Word", "Start Time", "End Time"])
        writer.writerows(words_rows)
    print(f"[align] wrote {len(words_rows)} word rows to {output_path}", flush=True)
    return len(words_rows)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", type=Path, required=True, help="Groq segment CSV.")
    p.add_argument("--rush", type=Path, required=True, help="Rush video or audio.")
    p.add_argument("--output", type=Path, required=True, help="Output words CSV.")
    p.add_argument(
        "--groq-words", type=Path, default=None,
        help="Optional Groq word CSV; its entries fill time ranges that "
             "alignment could not cover (e.g. digits). Aligned entries win.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    align_file(args.csv, args.rush, args.output, groq_words_csv=args.groq_words)


if __name__ == "__main__":
    main()
