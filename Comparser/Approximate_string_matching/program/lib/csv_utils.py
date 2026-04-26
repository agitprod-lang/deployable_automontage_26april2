from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, List, Sequence

from .constants import DIAGNOSTIC_HEADER, PIPE_COMPAT_HEADER, WORKING_HEADER
from .model import GroqSegment, TimedToken, WorkingRow


def _choose_delimiter_from_sample(sample: str) -> str:
    first_nonempty_line = next((line for line in sample.splitlines() if line.strip()), "")
    semicolon_count = first_nonempty_line.count(";")
    comma_count = first_nonempty_line.count(",")
    if semicolon_count > 0 and semicolon_count >= comma_count:
        return ";"
    if comma_count > 0 and comma_count > semicolon_count:
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
    except csv.Error:
        return ";"
    delimiter = str(getattr(dialect, "delimiter", ";") or ";")
    return delimiter if delimiter in {",", ";"} else ";"


def _delimiter_for(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig", errors="ignore")[:8192]
    return _choose_delimiter_from_sample(sample)


def read_delimited_dicts(path: Path) -> list[dict[str, str]]:
    delimiter = _delimiter_for(path)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows: list[dict[str, str]] = []
        for row in reader:
            normalized_row = {
                (key or "").lstrip("\ufeff").strip(): (value or "")
                for key, value in row.items()
            }
            rows.append(normalized_row)
        return rows


def read_groq_segments(path: Path) -> list[GroqSegment]:
    rows = read_delimited_dicts(path)
    segments: list[GroqSegment] = []
    for row in rows:
        text = (row.get("Text") or row.get("text") or "").strip()
        if not text:
            continue
        start_time = (row.get("Start Time") or row.get("start_time") or "").strip()
        end_time = (row.get("End Time") or row.get("end_time") or "").strip()
        segments.append(GroqSegment(start_time=start_time, end_time=end_time, text=text))
    return segments


def read_groq_words(path: Path) -> list[TimedToken]:
    rows = read_delimited_dicts(path)
    words: list[TimedToken] = []
    for row in rows:
        token = (row.get("Word") or row.get("word") or "").strip()
        if not token:
            continue
        words.append(
            TimedToken(
                token=token,
                start_time=(row.get("Start Time") or "").strip(),
                end_time=(row.get("End Time") or "").strip(),
            )
        )
    return words


def infer_words_path(csv_path: Path, explicit_words_path: Path | None = None) -> Path | None:
    if explicit_words_path:
        return explicit_words_path
    candidate = csv_path.with_name(f"{csv_path.stem}_words.csv")
    if candidate.exists():
        return candidate
    return None


def read_working_rows(path: Path) -> list[WorkingRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        return [WorkingRow.from_dict(row) for row in reader]


def write_working_rows(path: Path, rows: Sequence[WorkingRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WORKING_HEADER, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())


def write_dict_rows(path: Path, rows: Iterable[dict[str, str]], header: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(header), delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_pipe_compat_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_dict_rows(path, rows, PIPE_COMPAT_HEADER)


def write_diagnostic_rows(path: Path, rows: Iterable[dict[str, str]]) -> None:
    write_dict_rows(path, rows, DIAGNOSTIC_HEADER)
