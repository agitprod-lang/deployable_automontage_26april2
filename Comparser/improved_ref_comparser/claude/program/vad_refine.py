#!/usr/bin/env python3
"""
vad_refine.py  —  Silero-VAD based boundary refinement.

Purpose
-------
Groq's whisper transcription produces word timings, but when a speaker
retries a word mid-sentence (hesitation / stumble), multiple attempts get
collapsed into one abnormally long "word" (3-8 seconds). The downstream
trim_boundaries heuristic then picks the wrong side of that word.

This module re-runs Silero-VAD on the audio span of each Keep=x row, detects
the true speech islands (retakes + final clean delivery separated by silence),
and snaps the row's Start/End to the LAST coherent cluster of islands — which
is almost always the final clean take.

Universal (no per-file tuning). Only acts when:
  - the row has a detectable long-word anomaly (>=2.5s or 3x avg) OR
  - the re-VAD reveals 2+ speech islands separated by >=300 ms silence.

Otherwise the row is passed through unchanged.
"""
from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

FPS = 30
SR = 16000

LONG_WORD_ABS_SECS = 2.5
LONG_WORD_REL_RATIO = 3.0
MIN_LONG_WORD_RATIO_SECS = 1.5

VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_MS = 100
VAD_MIN_SPEECH_MS = 150

ISLAND_MERGE_GAP_S = 0.20
BOUNDARY_PADDING_S = 0.15


_vad_model = None
_vad_utils = None


def _load_vad():
    global _vad_model, _vad_utils
    if _vad_model is not None:
        return _vad_model, _vad_utils
    import torch
    _vad_model, _vad_utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        trust_repo=True,
    )
    return _vad_model, _vad_utils


def _tc_to_frames(tc: str, fps: int = FPS) -> int:
    parts = tc.strip().split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode: {tc}")
    h, m, s, f = map(int, parts)
    return ((h * 3600) + (m * 60) + s) * fps + f


def _tc_to_seconds(tc: str, fps: int = FPS) -> float:
    return _tc_to_frames(tc, fps) / fps


def _frames_to_tc(frames: int, fps: int = FPS) -> str:
    if frames < 0:
        frames = 0
    secs, fr = divmod(frames, fps)
    mins, sec = divmod(secs, 60)
    hrs, mn = divmod(mins, 60)
    return f"{hrs:02d}:{mn:02d}:{sec:02d}:{fr:02d}"


def _seconds_to_frames(s: float, fps: int = FPS) -> int:
    return int(round(s * fps))


def _parse_note(notes: str, key: str) -> Optional[str]:
    if not notes:
        return None
    for part in notes.split(" | "):
        part = part.strip()
        if part.startswith(key + "="):
            return part.split("=", 1)[1].strip()
    return None


def _parse_range(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    m = re.match(r"(\d+)-(\d+)", value)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _decode_audio_segment(audio_path: Path, start_s: float, end_s: float):
    """Decode a single [start_s, end_s] span from audio to a torch float32 mono tensor at SR Hz."""
    import array
    import torch
    duration = max(0.0, end_s - start_s)
    if duration <= 0.01:
        return torch.zeros((0,), dtype=torch.float32)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_s:.6f}",
        "-t",
        f"{duration:.6f}",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SR),
        "-f",
        "f32le",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, check=False)
    if result.returncode != 0:
        msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg decode failed: {msg}")
    raw = result.stdout
    if not raw:
        return torch.zeros((0,), dtype=torch.float32)
    samples = array.array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return torch.tensor(samples, dtype=torch.float32)


def _run_silero(waveform, threshold: float = VAD_THRESHOLD) -> list[tuple[float, float]]:
    model, utils = _load_vad()
    get_speech_timestamps = utils[0]
    if waveform.numel() == 0:
        return []
    # Silero's LSTM model keeps state between calls; reset so each row is
    # scored independently and boundaries don't shift based on call order.
    try:
        model.reset_states()
    except AttributeError:
        pass
    stamps = get_speech_timestamps(
        waveform,
        model,
        sampling_rate=SR,
        threshold=threshold,
        min_silence_duration_ms=VAD_MIN_SILENCE_MS,
        min_speech_duration_ms=VAD_MIN_SPEECH_MS,
    )
    return [(s["start"] / SR, s["end"] / SR) for s in stamps]


def _has_long_word(row_words: list[dict]) -> bool:
    if len(row_words) < 2:
        return False
    durs = []
    for w in row_words:
        try:
            d = _tc_to_seconds(w["End Time"]) - _tc_to_seconds(w["Start Time"])
        except (KeyError, ValueError):
            d = 0.0
        durs.append(max(0.0, d))
    if not durs:
        return False
    longest = max(durs)
    if longest >= LONG_WORD_ABS_SECS:
        return True
    avg = sum(durs) / len(durs) if durs else 0.0
    if avg > 0 and longest >= max(LONG_WORD_REL_RATIO * avg, MIN_LONG_WORD_RATIO_SECS):
        return True
    return False


def _last_island_cluster(
    islands: list[tuple[float, float]],
    merge_gap_s: float = ISLAND_MERGE_GAP_S,
) -> Optional[tuple[float, float]]:
    """Merge trailing islands whose gap is below merge_gap_s; return the last cluster."""
    if not islands:
        return None
    merged = [islands[-1]]
    for start, end in reversed(islands[:-1]):
        prev_start, prev_end = merged[-1]
        if prev_start - end <= merge_gap_s:
            merged[-1] = (start, prev_end)
        else:
            break
    return merged[-1]


def refine_row(
    row: dict,
    words: list[dict],
    audio_path: Path,
) -> Optional[tuple[str, str]]:
    """Return refined (start_tc, end_tc) for the row, or None if unchanged.

    The row may have already been trimmed by trim_boundaries (which narrows the span
    based on a bad heuristic for long words). To find the actual final take we scan
    the ORIGINAL word_range span from the diagnostic Notes, not the current Start/End.
    """
    try:
        cur_start_s = _tc_to_seconds(row.get("Start Time", "00:00:00:00"))
        cur_end_s = _tc_to_seconds(row.get("End Time", "00:00:00:00"))
    except ValueError:
        return None
    if cur_end_s - cur_start_s <= 0.0:
        return None

    notes = row.get("Notes", "") or ""
    word_range = _parse_range(_parse_note(notes, "word_range"))

    trigger = False
    original_start_s = cur_start_s
    original_end_s = cur_end_s
    if word_range is not None:
        ws, we = word_range
        if 0 <= ws < len(words) and 0 <= we < len(words) and ws <= we:
            row_words = words[ws : we + 1]
            trigger = _has_long_word(row_words)
            try:
                original_start_s = _tc_to_seconds(row_words[0]["Start Time"])
                original_end_s = _tc_to_seconds(row_words[-1]["End Time"])
            except (KeyError, ValueError):
                pass

    # When a long-word anomaly is detected, scan the ORIGINAL word_range so we
    # can see the sub-islands hidden inside a merged "word". Otherwise the row
    # has already been narrowed by trim_boundaries based on good signal — scan
    # only the current span so VAD refines, not widens.
    if trigger:
        scan_start_s = max(0.0, original_start_s - 0.20)
        scan_end_s = original_end_s + 0.20
    else:
        scan_start_s = max(0.0, cur_start_s - 0.20)
        scan_end_s = cur_end_s + 0.20

    if scan_end_s - scan_start_s <= 0.2:
        return None

    try:
        waveform = _decode_audio_segment(audio_path, scan_start_s, scan_end_s)
    except RuntimeError:
        return None

    islands_rel = _run_silero(waveform)
    if not islands_rel:
        return None
    islands_abs = [(scan_start_s + s, scan_start_s + e) for s, e in islands_rel]

    if not trigger:
        if len(islands_abs) <= 1:
            return None
        max_gap = max(
            islands_abs[i + 1][0] - islands_abs[i][1]
            for i in range(len(islands_abs) - 1)
        )
        if max_gap < 0.30:
            return None

    cluster = _last_island_cluster(islands_abs)
    if cluster is None:
        return None

    new_start_s, new_end_s = cluster
    new_start_s = max(0.0, new_start_s - BOUNDARY_PADDING_S)
    new_end_s = new_end_s + BOUNDARY_PADDING_S

    new_start_f = _seconds_to_frames(new_start_s)
    new_end_f = _seconds_to_frames(new_end_s)
    if new_end_f <= new_start_f:
        return None

    return _frames_to_tc(new_start_f), _frames_to_tc(new_end_f)


# ---------------------------------------------------------------------------
# CLI  (invoked by ref_comparser.py after the word-range-based trim)
# ---------------------------------------------------------------------------

def _read_semicolon_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _write_semicolon_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _read_words_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Silero-VAD boundary refinement on compat CSV")
    parser.add_argument("--compat", type=Path, required=True,
                        help="Input CSV (07_step1_compat_trimmed.csv after word-based trim).")
    parser.add_argument("--diagnostic", type=Path, required=True,
                        help="07_step1_diagnostic.csv (for word_range in Notes).")
    parser.add_argument("--words", type=Path, required=True,
                        help="*_words.csv from Groq.")
    parser.add_argument("--audio", type=Path, required=True,
                        help="Source audio file (e.g. output/groq/audio/<stem>.mp3).")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    compat_rows = _read_semicolon_csv(args.compat)
    diagnostic_rows = _read_semicolon_csv(args.diagnostic)
    words = _read_words_csv(args.words)

    diag_by_tn = {
        r["Transcript #"].strip(): r
        for r in diagnostic_rows
        if r.get("Transcript #", "").strip() not in ("", "-")
    }

    refined = 0
    for row in compat_rows:
        if row.get("Keep", "").strip() != "x":
            continue
        tn = row.get("Transcript #", "").strip()
        if not tn or tn == "-":
            continue
        diag = diag_by_tn.get(tn)
        enriched = dict(row)
        if diag and not enriched.get("Notes"):
            enriched["Notes"] = diag.get("Notes", "")
        new = refine_row(enriched, words, args.audio)
        if new is None:
            continue
        new_start, new_end = new
        if new_start != row.get("Start Time") or new_end != row.get("End Time"):
            row["Start Time"] = new_start
            row["End Time"] = new_end
            refined += 1

    if not compat_rows:
        args.output.write_text("", encoding="utf-8")
        return

    fieldnames = list(compat_rows[0].keys())
    _write_semicolon_csv(args.output, compat_rows, fieldnames)
    print(f"  [vad_refine] Refined {refined} row(s) via Silero-VAD → {args.output.name}")


if __name__ == "__main__":
    main()
