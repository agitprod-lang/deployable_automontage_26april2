from __future__ import annotations

import math
import re

from .constants import FRAME_RATE

TIMECODE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(?::(\d{2}))?$")


def parse_timecode(value: str, frame_rate: float = FRAME_RATE) -> float:
    raw = (value or "").strip()
    if not raw:
        return 0.0
    match = TIMECODE_RE.match(raw)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        frames = int(match.group(4) or 0)
        return (hours * 3600) + (minutes * 60) + seconds + (frames / frame_rate)
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Unsupported timecode: {value!r}") from exc


def format_timecode(total_seconds: float, frame_rate: float = FRAME_RATE) -> str:
    safe_seconds = max(0.0, total_seconds)
    whole_seconds = int(math.floor(safe_seconds))
    fractional = safe_seconds - whole_seconds
    frames = int(round(fractional * frame_rate))
    if frames >= int(frame_rate):
        whole_seconds += 1
        frames = 0
    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    seconds = whole_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def timecode_gap_seconds(start_time: str, end_time: str, frame_rate: float = FRAME_RATE) -> float:
    return parse_timecode(start_time, frame_rate) - parse_timecode(end_time, frame_rate)
