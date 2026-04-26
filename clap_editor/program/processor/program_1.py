#!/usr/bin/env python3
"""
Relaxed clap detector that enforces the "single = drop, triple = keep" rule.

This variant reuses the sample templates from program_0 but applies a pre-emphasis
filter and a looser energy gate to surface quieter claps near the start of the
take. Segments are constructed as the intervals between successive clap groups,
trimming a bit of audio around each marker so the exported timeline mirrors the
hand-edited KEEP/DROP rhythm more closely.
"""

from __future__ import annotations

import argparse
import wave
from pathlib import Path
from typing import Iterable, List

import numpy as np

from program_0 import (
    OUTPUT_DIR,
    RUSH_DIR,
    Segment,
    TEMPLATES,
    _dtype_for_sample_width,
    extract_audio,
    find_latest_video,
    make_otio,
    make_premiere_xml,
    probe_video_metadata,
)
from reference_utils import summarize_comparison


def load_wave(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        frames = wf.getnframes()
        raw = wf.readframes(frames)
    dtype = _dtype_for_sample_width(sampwidth)
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if sampwidth == 1:
        samples -= 128.0
        denom = 128.0
    else:
        denom = float(2 ** (8 * sampwidth - 1))
    samples /= denom or 1.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sample_rate


def compute_envelope(samples: np.ndarray, sr: int, window_ms: float = 4.0) -> tuple[np.ndarray, np.ndarray]:
    block = max(int(sr * (window_ms / 1000.0)), 1)
    pad = (-len(samples)) % block
    if pad:
        samples = np.pad(samples, (0, pad), mode="constant")
    envelope = np.abs(samples).reshape(-1, block).max(axis=1)
    times = ((np.arange(len(envelope)) * block) + block / 2) / sr
    return envelope, times


def measure_group_feature(samples: np.ndarray, sr: int, start: float, end: float):
    start_idx = max(0, int(start * sr))
    end_idx = min(len(samples), int(end * sr))
    if end_idx <= start_idx:
        return None
    segment = samples[start_idx:end_idx]
    peak = float(np.max(np.abs(segment)))
    if peak <= 0:
        return None
    pre_start = max(0, start_idx - int(sr * 0.2))
    post_end = min(len(samples), end_idx + int(sr * 0.25))
    pre = samples[pre_start:start_idx]
    post = samples[end_idx:post_end]
    baseline = float(np.sqrt(np.mean(pre**2))) if pre.size else 1e-6
    tail = float(np.sqrt(np.mean(post**2))) if post.size else baseline
    transient = peak / max(baseline, 1e-6)
    decay = tail / max(peak, 1e-6)
    window = min(len(segment), 8192)
    frame = segment[:window] * np.hanning(window)
    spectrum = np.abs(np.fft.rfft(frame))
    freqs = np.fft.rfftfreq(window, 1.0 / sr)
    total = spectrum.sum() + 1e-8
    centroid = float((freqs * spectrum).sum() / total)
    bandwidth = float(np.sqrt(((freqs - centroid) ** 2 * spectrum).sum() / total))
    return {
        "duration": end - start,
        "transient_ratio": transient,
        "decay_ratio": decay,
        "centroid": centroid,
        "bandwidth": bandwidth,
    }


def matches_template(group: dict, feat: dict | None) -> bool:
    label = "triple" if group["type"] == "triple" else "single"
    template = TEMPLATES.get(label)
    if not template or not feat:
        return True
    if feat["transient_ratio"] < template.transient_ratio * 0.55:
        return False
    if feat["decay_ratio"] > template.decay_ratio * 1.4:
        return False
    if feat["centroid"] < template.centroid * 0.65:
        return False
    if feat["bandwidth"] < template.bandwidth * 0.55:
        return False
    if feat["duration"] > template.duration * 2.0:
        return False
    return True


def detect_relaxed_claps(wav_path: Path) -> List[dict]:
    samples, sample_rate = load_wave(wav_path)
    emphasized = np.append(samples[0], samples[1:] - 0.95 * samples[:-1])
    emphasized /= np.max(np.abs(emphasized)) + 1e-8
    envelope, times = compute_envelope(emphasized, sample_rate, window_ms=3.5)
    if not len(envelope):
        return []
    median = float(np.median(envelope))
    mad = float(np.median(np.abs(envelope - median))) or 1e-6
    percentile = float(np.percentile(envelope, 98))
    threshold = max(median + 3.5 * mad, percentile * 0.55, envelope.max() * 0.35)
    active = envelope >= threshold
    clap_times: List[float] = []
    idx = 0
    while idx < len(active):
        if active[idx]:
            start = idx
            while idx < len(active) and active[idx]:
                idx += 1
            end = idx
            peak_idx = start + np.argmax(envelope[start:end])
            clap_times.append(times[min(peak_idx, len(times) - 1)])
        else:
            idx += 1
    if not clap_times:
        return []
    clap_times.sort()
    deduped: List[float] = []
    min_separation = 0.06
    for t in clap_times:
        if not deduped or t - deduped[-1] >= min_separation:
            deduped.append(t)
    group_gap = 0.45
    groups: List[dict] = []
    current = [deduped[0]]
    for t in deduped[1:]:
        if t - current[-1] <= group_gap:
            current.append(t)
        else:
            groups.append(current)
            current = [t]
    if current:
        groups.append(current)
    filtered: List[dict] = []
    for points in groups:
        count = len(points)
        if count >= 3:
            marker = "triple"
        elif count == 2:
            marker = "double"
        else:
            marker = "single"
        start_time = max(0.0, points[0] - 0.02)
        end_time = points[-1] + 0.02
        feat = measure_group_feature(samples, sample_rate, start_time, end_time)
        info = {
            "count": count,
            "type": marker,
            "start": start_time,
            "end": end_time,
            "center": sum(points) / len(points),
        }
        if matches_template(info, feat):
            filtered.append(info)
    return filtered


def build_story_segments(groups: List[dict], duration: float) -> List[Segment]:
    segments: List[Segment] = []
    cursor = 0.0
    trim_pre = 0.12
    trim_post = 0.18
    min_duration = 0.35
    for group in groups:
        keep = group["type"] == "triple"
        take_end = max(cursor, group["start"] - trim_pre)
        if take_end - cursor >= min_duration:
            segments.append(
                Segment(
                    start=cursor,
                    end=take_end,
                    marker_type=group["type"],
                    keep=keep,
                )
            )
        cursor = min(duration, group["end"] + trim_post)
    if duration - cursor >= min_duration:
        segments.append(
            Segment(
                start=cursor,
                end=duration,
                marker_type="tail",
                keep=True,
            )
        )
    return segments


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, help="Video to process (default: latest rush)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    video = args.video or find_latest_video(RUSH_DIR)
    meta = probe_video_metadata(video)
    mp3_path = OUTPUT_DIR / f"{video.stem}_audio.mp3"
    wav_path, temp_dir = extract_audio(video, mp3_path)
    try:
        groups = detect_relaxed_claps(wav_path)
        segments = build_story_segments(groups, meta.duration)
        for seg in segments:
            state = "KEEP" if seg.keep else "DROP"
            print(f"{state} take {seg.start:.2f}s -> {seg.end:.2f}s ({seg.marker_type})")
        otio_path = OUTPUT_DIR / f"{video.stem}_program_1.otio"
        xml_path = OUTPUT_DIR / f"{video.stem}_program_1.xml"
        make_otio(video, segments, meta, otio_path)
        make_premiere_xml(video, segments, meta, xml_path)
        print(f"Wrote {otio_path}")
        print(f"Wrote {xml_path}")
        print(f"Audio reference exported to {mp3_path}")
        summarize_comparison("program_1", segments)
    finally:
        temp_dir.cleanup()


if __name__ == "__main__":
    main()
