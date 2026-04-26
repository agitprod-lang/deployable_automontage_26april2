#!/usr/bin/env python3
"""Transcribe VAD speech clips with Groq and stitch absolute word timings."""

from __future__ import annotations

import argparse
import array
import csv
import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

PROGRAM_DIR = Path(__file__).resolve().parent
if str(PROGRAM_DIR) not in sys.path:
    sys.path.append(str(PROGRAM_DIR))

import groq_noclap_csv_maker as base


DEFAULT_VAD_TRIGGER_LEVEL = 4.9
DEFAULT_VAD_WINDOW_MS = 400
DEFAULT_VAD_HOP_MS = 50
DEFAULT_VAD_MIN_SPEECH_SECONDS = 0.18
DEFAULT_VAD_MERGE_GAP_SECONDS = 0.15
DEFAULT_VAD_PADDING_SECONDS = 0.18
DEFAULT_VAD_PRE_TRIGGER_SECONDS = 0.18
DEFAULT_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class VadSegment:
    clip_id: int
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.end_seconds - self.start_seconds)


@dataclass(frozen=True)
class VadConfig:
    trigger_level: float = DEFAULT_VAD_TRIGGER_LEVEL
    window_ms: int = DEFAULT_VAD_WINDOW_MS
    hop_ms: int = DEFAULT_VAD_HOP_MS
    min_speech_seconds: float = DEFAULT_VAD_MIN_SPEECH_SECONDS
    merge_gap_seconds: float = DEFAULT_VAD_MERGE_GAP_SECONDS
    padding_seconds: float = DEFAULT_VAD_PADDING_SECONDS
    pre_trigger_seconds: float = DEFAULT_VAD_PRE_TRIGGER_SECONDS
    sample_rate: int = DEFAULT_SAMPLE_RATE


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run VAD-first Groq transcription and emit stitched transcript/word CSVs."
    )
    parser.add_argument(
        "media",
        type=Path,
        nargs="?",
        help=f"Audio/video file to transcribe (default: newest file in {base.DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=f"Destination CSV path (defaults to {base.DEFAULT_OUTPUT_DIR}/<timestamp>.csv).",
    )
    parser.add_argument("--speaker", default="Unknown", help='Speaker name for every row (default: "Unknown").')
    parser.add_argument(
        "--frame-rate",
        type=float,
        default=base.DEFAULT_FRAME_RATE,
        help=f"Timecode frame rate (default: {base.DEFAULT_FRAME_RATE}).",
    )
    parser.add_argument(
        "--model",
        default=base.DEFAULT_MODEL,
        help=f"Groq Whisper model to use (default: {base.DEFAULT_MODEL}).",
    )
    parser.add_argument("--api-key", help="Override the GROQ_API_KEY environment variable.")
    parser.add_argument("--no-transcode", action="store_true", help="Disable automatic transcoding.")
    parser.add_argument("--vad-trigger-level", type=float, default=DEFAULT_VAD_TRIGGER_LEVEL)
    parser.add_argument("--vad-window-ms", type=int, default=DEFAULT_VAD_WINDOW_MS)
    parser.add_argument("--vad-hop-ms", type=int, default=DEFAULT_VAD_HOP_MS)
    parser.add_argument("--vad-min-speech-seconds", type=float, default=DEFAULT_VAD_MIN_SPEECH_SECONDS)
    parser.add_argument("--vad-merge-gap-seconds", type=float, default=DEFAULT_VAD_MERGE_GAP_SECONDS)
    parser.add_argument("--vad-padding-seconds", type=float, default=DEFAULT_VAD_PADDING_SECONDS)
    parser.add_argument("--vad-pre-trigger-seconds", type=float, default=DEFAULT_VAD_PRE_TRIGGER_SECONDS)
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    return parser.parse_args()


def _run_ffmpeg_bytes(cmd: Sequence[str]) -> bytes:
    try:
        result = base.subprocess.run(list(cmd), capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for VAD-first Groq transcription.") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip() or "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg failed: {message}")
    return result.stdout


def _probe_duration_seconds(media_path: Path) -> float:
    try:
        result = base.subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, base.subprocess.CalledProcessError) as exc:
        raise RuntimeError("ffprobe is required for VAD-first Groq transcription.") from exc
    try:
        return max(0.0, float((result.stdout or "").strip()))
    except ValueError:
        return 0.0


def _load_torchaudio_vad():
    try:
        import torch
        import torchaudio.functional as torchaudio_functional
    except ImportError as exc:
        raise RuntimeError("VAD-first Groq transcription requires torch and torchaudio.") from exc
    return torch, torchaudio_functional


def _decode_media_audio_tensor(media_path: Path, sample_rate: int):
    torch, _torchaudio_functional = _load_torchaudio_vad()
    raw_bytes = _run_ffmpeg_bytes(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = array.array("f")
    if raw_bytes:
        samples.frombytes(raw_bytes)
        if sys.byteorder != "little":
            samples.byteswap()
    if not samples:
        return torch.zeros((1, 0), dtype=torch.float32)
    return torch.tensor(samples, dtype=torch.float32).unsqueeze(0)


def _analysis_window_starts(total_samples: int, window_samples: int, hop_samples: int) -> list[int]:
    if total_samples <= 0:
        return []
    if total_samples <= window_samples:
        return [0]
    starts = list(range(0, total_samples - window_samples + 1, max(1, hop_samples)))
    final_start = total_samples - window_samples
    if starts[-1] != final_start:
        starts.append(final_start)
    return starts


def _merge_second_intervals_with_gap(
    intervals: Sequence[tuple[float, float]],
    max_gap_seconds: float,
) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[float, float]] = []
    current_start, current_end = ordered[0]
    max_gap_seconds = max(0.0, max_gap_seconds)
    for start, end in ordered[1:]:
        if start <= current_end + max_gap_seconds:
            current_end = max(current_end, end)
            continue
        merged.append((current_start, current_end))
        current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def _vad_parameters(config: VadConfig) -> dict[str, float]:
    return {
        "trigger_level": float(config.trigger_level),
        "trigger_time": 0.10,
        "search_time": 0.20,
        "allowed_gap": 0.12,
        "pre_trigger_time": 0.0,
        "boot_time": 0.10,
        "noise_up_time": 0.05,
        "noise_down_time": 0.01,
        "noise_reduction_amount": 1.20,
        "measure_freq": 20.0,
        "measure_duration": None,
        "measure_smooth_time": 0.10,
        "hp_filter_freq": 50.0,
        "lp_filter_freq": 6000.0,
        "hp_lifter_freq": 150.0,
        "lp_lifter_freq": 2000.0,
    }


def _detect_speech_interval_in_chunk(waveform, sample_rate: int, config: VadConfig) -> Optional[tuple[int, int]]:
    if waveform.numel() == 0:
        return None
    torch, torchaudio_functional = _load_torchaudio_vad()
    params = _vad_parameters(config)
    front = torchaudio_functional.vad(waveform, sample_rate, **params)
    if front.numel() == 0 or front.shape[-1] == 0:
        return None
    lead_trim = waveform.shape[-1] - front.shape[-1]
    reverse_front = torch.flip(front, dims=[-1])
    back = torchaudio_functional.vad(reverse_front, sample_rate, **params)
    if back.numel() == 0 or back.shape[-1] == 0:
        return None
    tail_trim = front.shape[-1] - back.shape[-1]
    pre_trigger_samples = int(round(max(0.0, float(config.pre_trigger_seconds)) * sample_rate))
    speech_start = max(0, int(lead_trim) - pre_trigger_samples)
    speech_end = max(speech_start, int(waveform.shape[-1] - tail_trim))
    if speech_end <= speech_start:
        return None
    return speech_start, speech_end


def compute_vad_speech_segments(media_path: Path, config: VadConfig) -> list[VadSegment]:
    waveform = _decode_media_audio_tensor(media_path, config.sample_rate)
    total_samples = int(waveform.shape[-1])
    if total_samples <= 0:
        return []
    window_samples = max(1, int(round(config.sample_rate * config.window_ms / 1000.0)))
    hop_samples = max(1, int(round(config.sample_rate * config.hop_ms / 1000.0)))
    raw_intervals: list[tuple[float, float]] = []
    for chunk_start in _analysis_window_starts(total_samples, window_samples, hop_samples):
        chunk_end = min(total_samples, chunk_start + window_samples)
        chunk = waveform[:, chunk_start:chunk_end]
        speech_bounds = _detect_speech_interval_in_chunk(chunk, config.sample_rate, config)
        if speech_bounds is None:
            continue
        speech_start, speech_end = speech_bounds
        absolute_start = (chunk_start + speech_start) / float(config.sample_rate)
        absolute_end = (chunk_start + speech_end) / float(config.sample_rate)
        if absolute_end > absolute_start:
            raw_intervals.append((absolute_start, absolute_end))
    media_duration = max(0.0, _probe_duration_seconds(media_path))
    if not raw_intervals:
        if media_duration <= 0.0:
            return []
        return [VadSegment(1, 0.0, media_duration)]

    padded: list[tuple[float, float]] = []
    for start, end in raw_intervals:
        padded.append((max(0.0, start - config.padding_seconds), end + config.padding_seconds))
    merged = _merge_second_intervals_with_gap(padded, config.merge_gap_seconds)
    normalized: list[VadSegment] = []
    for clip_id, (start, end) in enumerate(merged, start=1):
        bounded_end = min(media_duration, end) if media_duration > 0.0 else end
        bounded_start = max(0.0, start)
        if bounded_end - bounded_start >= config.min_speech_seconds:
            normalized.append(VadSegment(clip_id, bounded_start, bounded_end))
    if normalized:
        return normalized
    if media_duration <= 0.0:
        return []
    return [VadSegment(1, 0.0, media_duration)]


def export_clip(source: Path, segment: VadSegment, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{segment.start_seconds:.6f}",
        "-t",
        f"{segment.duration_seconds:.6f}",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        "48k",
        str(destination),
    ]
    base._run_ffmpeg(cmd, f"exporting VAD clip {segment.clip_id}")
    return destination


def _shift_segment_payload(payload: dict[str, Any], offset_seconds: float) -> list[dict[str, Any]]:
    shifted: list[dict[str, Any]] = []
    for segment in base.iter_segments(payload):
        if not isinstance(segment, dict):
            continue
        clone = dict(segment)
        start = float(clone.get("start", 0.0)) + offset_seconds
        end = float(clone.get("end", clone.get("start", 0.0))) + offset_seconds
        clone["start"] = start
        clone["end"] = end
        if isinstance(clone.get("words"), list):
            shifted_words: list[dict[str, Any]] = []
            for word in clone["words"]:
                if not isinstance(word, dict):
                    continue
                shifted_word = dict(word)
                shifted_word["start"] = float(shifted_word.get("start", 0.0)) + offset_seconds
                shifted_word["end"] = float(shifted_word.get("end", shifted_word.get("start", 0.0))) + offset_seconds
                shifted_words.append(shifted_word)
            clone["words"] = shifted_words
        shifted.append(clone)
    return shifted


def _shift_word_payload(payload: dict[str, Any], offset_seconds: float) -> list[dict[str, Any]]:
    shifted: list[dict[str, Any]] = []
    for token, start, end in base.iter_word_entries(payload):
        shifted.append({"word": token, "start": start + offset_seconds, "end": end + offset_seconds})
    return shifted


def _intervals_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> bool:
    return max(start_a, start_b) < min(end_a, end_b)


def _extract_global_words_for_segment(
    global_payload: dict[str, Any],
    segment: VadSegment,
) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    for token, start, end in base.iter_word_entries(global_payload):
        midpoint = (start + end) / 2.0
        if not (
            _intervals_overlap(start, end, segment.start_seconds, segment.end_seconds)
            or segment.start_seconds <= midpoint <= segment.end_seconds
        ):
            continue
        relative_start = max(0.0, start - segment.start_seconds)
        relative_end = max(relative_start, end - segment.start_seconds)
        recovered.append({"word": token, "start": relative_start, "end": relative_end})
    return recovered


def _extract_global_segments_for_segment(
    global_payload: dict[str, Any],
    segment: VadSegment,
) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    for item in base.iter_segments(global_payload):
        if not isinstance(item, dict):
            continue
        start = float(item.get("start", 0.0))
        end = float(item.get("end", start))
        if not _intervals_overlap(start, end, segment.start_seconds, segment.end_seconds):
            continue
        recovered.append(
            {
                "start": max(0.0, start - segment.start_seconds),
                "end": max(0.0, end - segment.start_seconds),
                "text": base.clean_text(item.get("text")),
            }
        )
    return recovered


def _build_clip_payload_with_global_fallback(
    segment: VadSegment,
    local_payload: dict[str, Any],
    global_payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    local_words = list(base.iter_word_entries(local_payload))
    if local_words:
        return local_payload, "local"

    recovered_words = _extract_global_words_for_segment(global_payload, segment)
    if recovered_words:
        merged_payload = dict(local_payload)
        merged_payload["words"] = recovered_words
        local_segments = list(base.iter_segments(local_payload))
        if not local_segments:
            fallback_segments = _extract_global_segments_for_segment(global_payload, segment)
            if not fallback_segments:
                fallback_segments = [
                    {
                        "start": recovered_words[0]["start"],
                        "end": recovered_words[-1]["end"],
                        "text": " ".join(word["word"] for word in recovered_words).strip(),
                    }
                ]
            merged_payload["segments"] = fallback_segments
        if not base.clean_text(merged_payload.get("text")):
            merged_payload["text"] = " ".join(word["word"] for word in recovered_words).strip()
        return merged_payload, "global_word_fallback"

    local_text = base.clean_text(local_payload.get("text"))
    local_segments = list(base.iter_segments(local_payload))
    if local_text or local_segments:
        return local_payload, "local_unresolved"

    fallback_segments = _extract_global_segments_for_segment(global_payload, segment)
    fallback_text = " ".join(
        text
        for text in (base.clean_text(item.get("text")) for item in fallback_segments)
        if text
    ).strip()
    if fallback_segments or fallback_text:
        return {
            "text": fallback_text,
            "segments": fallback_segments,
            "words": [],
        }, "global_unresolved"

    return {
        "text": "",
        "segments": [],
        "words": [],
    }, "unresolved"


def build_stitched_transcription(
    clip_results: Sequence[tuple[VadSegment, dict[str, Any]]],
) -> dict[str, Any]:
    stitched_segments: list[dict[str, Any]] = []
    stitched_words: list[dict[str, Any]] = []
    stitched_text_parts: list[str] = []
    clip_payloads: list[dict[str, Any]] = []
    for segment, payload in clip_results:
        shifted_segments = _shift_segment_payload(payload, segment.start_seconds)
        shifted_words = _shift_word_payload(payload, segment.start_seconds)
        if not shifted_segments:
            segment_start = shifted_words[0]["start"] if shifted_words else segment.start_seconds
            segment_end = shifted_words[-1]["end"] if shifted_words else segment.end_seconds
            shifted_segments = [
                {
                    "start": segment_start,
                    "end": segment_end,
                    "text": base.clean_text(payload.get("text")),
                }
            ]
        stitched_segments.extend(shifted_segments)
        stitched_words.extend(shifted_words)
        text = base.clean_text(payload.get("text"))
        if text:
            stitched_text_parts.append(text)
        clip_payloads.append(
            {
                "clip_id": segment.clip_id,
                "source_start_seconds": segment.start_seconds,
                "source_end_seconds": segment.end_seconds,
                "duration_seconds": segment.duration_seconds,
                "resolution": str(payload.get("_resolution", "local")),
                "word_count": len(shifted_words),
                "segment_count": len(shifted_segments),
                "text": text,
                "transcription": payload,
            }
        )
    return {
        "text": " ".join(part for part in stitched_text_parts if part).strip(),
        "segments": stitched_segments,
        "words": stitched_words,
        "clips": clip_payloads,
    }


def write_vad_manifest(base_path: Path, segments: Sequence[VadSegment], frame_rate: float) -> Path:
    manifest_path = base_path.with_name(f"{base_path.stem}_vad_segments.csv")
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow(["Clip ID", "Source Start Time", "Source End Time", "Duration Seconds"])
        for segment in segments:
            writer.writerow(
                [
                    segment.clip_id,
                    base.seconds_to_timecode(segment.start_seconds, frame_rate),
                    base.seconds_to_timecode(segment.end_seconds, frame_rate),
                    f"{segment.duration_seconds:.6f}",
                ]
            )
    return manifest_path


def resolve_input_media(candidate: Path | None) -> Path:
    requested_media = candidate
    if not requested_media:
        requested_media = base.latest_input_file(base.DEFAULT_INPUT_DIR)
    preferred = base.prefer_audio_source(requested_media)
    if not preferred:
        raise FileNotFoundError("No input media provided and no default media found.")
    return preferred


def main() -> int:
    args = parse_args()
    output_path = base.ensure_output_path(args.output)
    requested_media = args.media.expanduser() if args.media else None
    input_media = resolve_input_media(requested_media)
    original_video = requested_media if requested_media and requested_media.suffix.lower() in base.VIDEO_EXTENSIONS else None
    reference_mp3: Path | None = None
    if original_video:
        reference_mp3 = base.export_reference_mp3(original_video, output_path.parent)
    transcription_source = reference_mp3 or input_media
    config = VadConfig(
        trigger_level=max(0.0, float(args.vad_trigger_level)),
        window_ms=max(1, int(args.vad_window_ms)),
        hop_ms=max(1, int(args.vad_hop_ms)),
        min_speech_seconds=max(0.0, float(args.vad_min_speech_seconds)),
        merge_gap_seconds=max(0.0, float(args.vad_merge_gap_seconds)),
        padding_seconds=max(0.0, float(args.vad_padding_seconds)),
        pre_trigger_seconds=max(0.0, float(args.vad_pre_trigger_seconds)),
        sample_rate=max(1000, int(args.sample_rate)),
    )
    api_key = base.resolve_api_key(args.api_key)
    client = base.load_groq_client(api_key)
    allow_transcode = not args.no_transcode
    with base.prepare_audio_file(transcription_source, allow_transcode) as media_path:
        print("Running global Groq reference transcription...")
        global_payload = base.transcribe_audio(client, media_path, args.model)
        speech_segments = compute_vad_speech_segments(media_path, config)
        if not speech_segments:
            raise RuntimeError("VAD-first transcription found no speech segments to transcribe.")
        clip_results: list[tuple[VadSegment, dict[str, Any]]] = []
        resolution_counts: dict[str, int] = {}
        with tempfile.TemporaryDirectory(prefix="groq_vad_") as temp_dir:
            temp_root = Path(temp_dir)
            for segment in speech_segments:
                clip_path = temp_root / f"{output_path.stem}_clip_{segment.clip_id:04d}.mp3"
                export_clip(media_path, segment, clip_path)
                local_payload = base.transcribe_audio(client, clip_path, args.model)
                payload, resolution = _build_clip_payload_with_global_fallback(
                    segment,
                    local_payload,
                    global_payload,
                )
                payload["_resolution"] = resolution
                resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
                if resolution != "local":
                    print(
                        f"WARNING: VAD clip {segment.clip_id} ({segment.start_seconds:.2f}-{segment.end_seconds:.2f}s) "
                        f"resolved via {resolution}.",
                        file=sys.stderr,
                    )
                clip_results.append((segment, payload))

    stitched = build_stitched_transcription(clip_results)
    rows = base.build_rows(stitched, args.speaker, args.frame_rate)
    if len(rows) == 1:
        raise RuntimeError("No transcript segments were returned after VAD stitching.")
    base.write_csv(output_path, rows)
    manifest_data = {
        "transcription_mode": "vad_groq_words",
        "source_media": str(input_media),
        "transcription_source": str(transcription_source),
        "frame_rate": args.frame_rate,
        "model": args.model,
        "vad_config": asdict(config),
        "clip_resolution_counts": resolution_counts,
        **stitched,
    }
    raw_json = base.write_raw_json(output_path, manifest_data)
    word_csv = base.write_word_level_csv(output_path, stitched, args.frame_rate)
    if word_csv is None:
        raise RuntimeError("Stitched VAD transcription did not produce *_words.csv output.")
    vad_manifest = write_vad_manifest(output_path, [segment for segment, _ in clip_results], args.frame_rate)
    if reference_mp3:
        print(f"Audio reference exported to {reference_mp3}")
    print(f"Detected {len(clip_results)} VAD speech clip(s).")
    print(f"Wrote {len(rows) - 1} transcript row(s) to {output_path}")
    print(f"Wrote raw Groq/VAD JSON to {raw_json}")
    print(f"Wrote word-level timings to {word_csv}")
    print(f"Wrote VAD segment manifest to {vad_manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
