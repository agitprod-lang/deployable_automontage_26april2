from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

NOTE_SEPARATOR = " | "


@dataclass
class TimedToken:
    token: str
    start_time: str
    end_time: str


@dataclass
class GroqSegment:
    start_time: str
    end_time: str
    text: str


@dataclass
class WorkingRow:
    row_id: str
    kind: str
    start_time: str
    end_time: str
    text: str
    eliminate: str = ""
    eliminate_reason: str = ""
    repeat_group: str = ""
    repeat_role: str = ""
    reference_segment: str = ""
    match_percent: str = ""
    status: str = ""
    anchor_id: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "WorkingRow":
        return cls(
            row_id=(data.get("Row ID") or "").strip(),
            kind=(data.get("Kind") or "").strip(),
            start_time=(data.get("Start Time") or "").strip(),
            end_time=(data.get("End Time") or "").strip(),
            text=(data.get("Text") or "").strip(),
            eliminate=(data.get("Eliminate") or "").strip(),
            eliminate_reason=(data.get("Eliminate Reason") or "").strip(),
            repeat_group=(data.get("Repeat Group") or "").strip(),
            repeat_role=(data.get("Repeat Role") or "").strip(),
            reference_segment=(data.get("Reference Segment") or "").strip(),
            match_percent=(data.get("Match %") or "").strip(),
            status=(data.get("Status") or "").strip(),
            anchor_id=(data.get("Anchor ID") or "").strip(),
            notes=(data.get("Notes") or "").strip(),
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "Row ID": self.row_id,
            "Kind": self.kind,
            "Start Time": self.start_time,
            "End Time": self.end_time,
            "Text": self.text,
            "Eliminate": self.eliminate,
            "Eliminate Reason": self.eliminate_reason,
            "Repeat Group": self.repeat_group,
            "Repeat Role": self.repeat_role,
            "Reference Segment": self.reference_segment,
            "Match %": self.match_percent,
            "Status": self.status,
            "Anchor ID": self.anchor_id,
            "Notes": self.notes,
        }

    def is_speech(self) -> bool:
        return self.kind == "speech"

    def is_silence(self) -> bool:
        return self.kind == "silence"

    def is_eliminated(self) -> bool:
        return self.eliminate.strip().lower() == "x"


@dataclass
class RefSpan:
    index: int
    text: str
    normalized: str
    start_offset: int
    end_offset: int


@dataclass
class RefWindow:
    start_index: int
    end_index: int
    text: str
    normalized: str
    token_count: int


@dataclass
class MatchMetrics:
    score: float
    ordered_overlap: float
    set_overlap: float
    char_similarity: float
    length_ratio: float


@dataclass
class AnchorMatch:
    row_index: int
    ref_start_index: int
    ref_end_index: int
    score: float


@dataclass
class RepetitionLink:
    left_row_index: int
    right_row_index: int
    run_length: int
    coverage: float
    phrase: str


def parse_notes(value: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for piece in value.split(NOTE_SEPARATOR):
        if "=" not in piece:
            continue
        key, raw_value = piece.split("=", 1)
        key = key.strip()
        if not key:
            continue
        result[key] = raw_value.strip()
    return result


def render_notes(data: Dict[str, str]) -> str:
    parts: List[str] = []
    for key in sorted(data):
        value = data[key].strip()
        if value:
            parts.append(f"{key}={value}")
    return NOTE_SEPARATOR.join(parts)


def get_note(value: str, key: str, default: str = "") -> str:
    return parse_notes(value).get(key, default)


def get_note_range(value: str, key: str) -> Optional[tuple[int, int]]:
    raw = get_note(value, key)
    if not raw:
        return None
    if "-" not in raw:
        try:
            point = int(raw)
        except ValueError:
            return None
        return point, point
    left, right = raw.split("-", 1)
    try:
        return int(left), int(right)
    except ValueError:
        return None


def upsert_note(value: str, key: str, note_value: str) -> str:
    notes = parse_notes(value)
    if note_value:
        notes[key] = note_value
    else:
        notes.pop(key, None)
    return render_notes(notes)


def iter_grouped_rows(rows: Iterable[WorkingRow]) -> Dict[str, List[WorkingRow]]:
    grouped: Dict[str, List[WorkingRow]] = {}
    for row in rows:
        if row.repeat_group:
            grouped.setdefault(row.repeat_group, []).append(row)
    return grouped


def ref_span_range_from_notes(row: WorkingRow) -> Optional[tuple[int, int]]:
    return get_note_range(row.notes, "ref_span")
